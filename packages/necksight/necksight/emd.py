"""Medulla/Lobula Plate T4 (ON) and T5 (OFF) 3-arm Elementary Motion Detectors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Union

import numpy as np

from .retina import LaminaOutput


@dataclass(slots=True)
class MotionOutput:
    """Output of the T4/T5 Elementary Motion Detector and LPTC wide-field stage.

    Attributes:
        t4: 4-cardinal ON motion maps (keys ``"+X"``, ``"-X"``, ``"+Y"``, ``"-Y"``).
        t5: 4-cardinal OFF motion maps (keys ``"+X"``, ``"-X"``, ``"+Y"``, ``"-Y"``).
        pos_x: Combined (T4 + T5) rightward (+X) motion map.
        neg_x: Combined (T4 + T5) leftward (-X) motion map.
        pos_y: Combined (T4 + T5) downward (+Y) motion map.
        neg_y: Combined (T4 + T5) upward (-Y) motion map.
        flow_x: Opponent horizontal motion field ``pos_x - neg_x``.
        flow_y: Opponent vertical motion field ``pos_y - neg_y``.
        vx_flow: Wide-field horizontal LPTC (HS-like) optic flow.
        vy_flow: Wide-field vertical LPTC (VS-like) optic flow.
        yaw_pan: Wide-field equatorial horizontal tangential pan signal.
        radial_divergence: Wide-field radial expansion (+) vs contraction (-) flow.
    """

    t4: Dict[str, np.ndarray]
    t5: Dict[str, np.ndarray]
    pos_x: np.ndarray
    neg_x: np.ndarray
    pos_y: np.ndarray
    neg_y: np.ndarray
    flow_x: np.ndarray
    flow_y: np.ndarray
    vx_flow: float
    vy_flow: float
    yaw_pan: float
    radial_divergence: float

    @property
    def lptc(self) -> Tuple[float, float, float, float]:
        """Return wide-field LPTC summary tuple ``(vx_flow, vy_flow, yaw_pan, radial_divergence)``."""
        return (self.vx_flow, self.vy_flow, self.yaw_pan, self.radial_divergence)


class T4T5MotionEngine:
    """Biophysical 4-cardinal 3-arm T4 (ON) and T5 (OFF) motion engine.

    Inspired by *Drosophila* T4 and T5 Lobula Plate direction-selective neurons
    (Maisak et al., *Nature* 2013; Haag et al., *eLife* 2017):
      - **Preferred-arm Hassenstein-Reichardt correlation**: multiplies delayed
        trailing-column activity by prompt center-column activity while subtracting
        synchronous self-correlation (``delayed_back * center - delayed_center * back``),
        mathematically guaranteeing zero response to stationary contrast flicker.
      - **Null-arm Barlow-Levick divisive shunting veto**: divides excitation by
        ``1 + veto_gain * delayed_front`` so waves arriving from the null direction
        actively suppress output.
      - **Multi-stride receptive field** (1-ommatidium + weighted 2-ommatidia arms)
        maintaining clean direction selectivity across both slow drift and rapid
        pre-collision expansion speeds.
      - **LPTC wide-field tangential integration** computing ``vx_flow``, ``vy_flow``,
        ``yaw_pan``, and ``radial_divergence``.
    """

    def __init__(
        self,
        grid_size: Union[int, Tuple[int, int]] = 64,
        *,
        delay_alpha: float = 0.55,
        veto_gain: float = 12.0,
        stride2_weight: float = 0.5,
        output_gain: float = 10.0,
    ) -> None:
        if isinstance(grid_size, int):
            self.grid_shape: Tuple[int, int] = (grid_size, grid_size)
        else:
            self.grid_shape = (int(grid_size[0]), int(grid_size[1]))

        gh, gw = self.grid_shape
        if gh < 8 or gw < 8:
            raise ValueError("grid_size dimensions must be >= 8")
        if not (0.0 < delay_alpha <= 1.0):
            raise ValueError("delay_alpha must be in (0.0, 1.0]")
        if veto_gain < 0.0:
            raise ValueError("veto_gain must be >= 0.0")

        self.delay_alpha = float(delay_alpha)
        self.veto_gain = float(veto_gain)
        self.stride2_weight = float(stride2_weight)
        self.output_gain = float(output_gain)

        self._d_on: Optional[np.ndarray] = None
        self._d_off: Optional[np.ndarray] = None

        # Precompute wide-field LPTC radial unit vectors & equatorial HS weights
        yy, xx = np.meshgrid(
            np.arange(gh, dtype=np.float32),
            np.arange(gw, dtype=np.float32),
            indexing="ij",
        )
        cy = 0.5 * (gh - 1)
        cx = 0.5 * (gw - 1)
        dy = yy - cy
        dx = xx - cx
        rad = np.hypot(dx, dy)
        inv_rad = np.where(rad > 1e-3, 1.0 / np.maximum(rad, 1e-3), 0.0)
        self._ux = (dx * inv_rad).astype(np.float32)
        self._uy = (dy * inv_rad).astype(np.float32)

        # Equatorial Gaussian belt (models HS horizontal system cells)
        norm_y = dy / max(0.5 * gh, 1.0)
        hs_w = np.exp(-1.5 * (norm_y ** 2)).astype(np.float32)
        self._hs_weights = hs_w / float(np.mean(hs_w))

    def reset(self) -> None:
        """Clear delayed T4/T5 temporal filter states."""
        self._d_on = None
        self._d_off = None

    def shift_state(self, dx: float, dy: float) -> None:
        """Shift the T4/T5 delay lines by ``(dx, dy)`` grid pixels (ego-motion derotation)."""
        if self._d_on is not None and self._d_off is not None:
            from .egomotion import shift_grid

            self._d_on = shift_grid(self._d_on, dx, dy)
            self._d_off = shift_grid(self._d_off, dx, dy)

    def _compute_cardinal_stride(
        self,
        prompt: np.ndarray,
        delayed: np.ndarray,
        stride: int,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Evaluate 3-arm HR + BL motion across all 4 cardinal directions for one stride."""
        h, w = prompt.shape
        g = self.veto_gain
        s = stride

        pos_x = np.zeros((h, w), dtype=np.float32)
        neg_x = np.zeros((h, w), dtype=np.float32)
        pos_y = np.zeros((h, w), dtype=np.float32)
        neg_y = np.zeros((h, w), dtype=np.float32)

        if w > 2 * s:
            f_c = prompt[:, s : w - s]
            d_c = delayed[:, s : w - s]
            f_l = prompt[:, : w - 2 * s]
            d_l = delayed[:, : w - 2 * s]
            f_r = prompt[:, 2 * s :]
            d_r = delayed[:, 2 * s :]

            # +X (rightward: left -> center, null veto from right + Mi4 center adaptation)
            hr_px = np.maximum(0.0, d_l * f_c - 1.35 * d_c * f_l)
            pos_x[:, s : w - s] = hr_px / (1.0 + g * d_r)

            # -X (leftward: right -> center, null veto from left + Mi4 center adaptation)
            hr_nx = np.maximum(0.0, d_r * f_c - 1.35 * d_c * f_r)
            neg_x[:, s : w - s] = hr_nx / (1.0 + g * d_l)

        if h > 2 * s:
            f_c = prompt[s : h - s, :]
            d_c = delayed[s : h - s, :]
            f_u = prompt[: h - 2 * s, :]
            d_u = delayed[: h - 2 * s, :]
            f_d = prompt[2 * s :, :]
            d_d = delayed[2 * s :, :]

            # +Y (downward: up -> center, null veto from down + Mi4 center adaptation)
            hr_py = np.maximum(0.0, d_u * f_c - 1.35 * d_c * f_u)
            pos_y[s : h - s, :] = hr_py / (1.0 + g * d_d)

            # -Y (upward: down -> center, null veto from up + Mi4 center adaptation)
            hr_ny = np.maximum(0.0, d_d * f_c - 1.35 * d_c * f_d)
            neg_y[s : h - s, :] = hr_ny / (1.0 + g * d_u)

        return pos_x, neg_x, pos_y, neg_y

    def _compute_pathway(
        self, prompt: np.ndarray, delayed: np.ndarray
    ) -> Dict[str, np.ndarray]:
        px1, nx1, py1, ny1 = self._compute_cardinal_stride(prompt, delayed, stride=1)
        if self.stride2_weight > 0.0 and min(prompt.shape) > 6:
            w2 = self.stride2_weight
            px2, nx2, py2, ny2 = self._compute_cardinal_stride(prompt, delayed, stride=2)
            scale = self.output_gain / (1.0 + w2)
            px = (px1 + w2 * px2) * scale
            nx = (nx1 + w2 * nx2) * scale
            py = (py1 + w2 * py2) * scale
            ny = (ny1 + w2 * ny2) * scale
        else:
            scale = self.output_gain
            px = px1 * scale
            nx = nx1 * scale
            py = py1 * scale
            ny = ny1 * scale

        return {
            "+X": px.astype(np.float32, copy=False),
            "-X": nx.astype(np.float32, copy=False),
            "+Y": py.astype(np.float32, copy=False),
            "-Y": ny.astype(np.float32, copy=False),
        }

    def step(
        self,
        lamina_or_l1: Union[LaminaOutput, np.ndarray],
        l2_off: Optional[np.ndarray] = None,
    ) -> MotionOutput:
        """Advance T4 (ON) and T5 (OFF) motion detectors by one frame."""
        if isinstance(lamina_or_l1, LaminaOutput):
            l1 = lamina_or_l1.l1_on
            l2 = lamina_or_l1.l2_off
        else:
            if l2_off is None:
                raise ValueError("l2_off must be provided when passing raw l1_on array")
            l1 = np.asarray(lamina_or_l1, dtype=np.float32)
            l2 = np.asarray(l2_off, dtype=np.float32)

        if l1.shape != self.grid_shape or l2.shape != self.grid_shape:
            raise ValueError(
                f"Expected L1/L2 shape {self.grid_shape}, got {l1.shape} and {l2.shape}"
            )

        if self._d_on is None or self._d_off is None:
            self._d_on = (self.delay_alpha * l1).astype(np.float32)
            self._d_off = (self.delay_alpha * l2).astype(np.float32)
            zeros = np.zeros(self.grid_shape, dtype=np.float32)
            empty_dirs = {"+X": zeros, "-X": zeros.copy(), "+Y": zeros.copy(), "-Y": zeros.copy()}
            empty_dirs_off = {"+X": zeros.copy(), "-X": zeros.copy(), "+Y": zeros.copy(), "-Y": zeros.copy()}
            return MotionOutput(
                t4=empty_dirs,
                t5=empty_dirs_off,
                pos_x=zeros.copy(),
                neg_x=zeros.copy(),
                pos_y=zeros.copy(),
                neg_y=zeros.copy(),
                flow_x=zeros.copy(),
                flow_y=zeros.copy(),
                vx_flow=0.0,
                vy_flow=0.0,
                yaw_pan=0.0,
                radial_divergence=0.0,
            )

        t4 = self._compute_pathway(l1, self._d_on)
        t5 = self._compute_pathway(l2, self._d_off)

        # Update delayed low-pass states for next step
        a = self.delay_alpha
        self._d_on = ((1.0 - a) * self._d_on + a * l1).astype(np.float32, copy=False)
        self._d_off = ((1.0 - a) * self._d_off + a * l2).astype(np.float32, copy=False)

        pos_x = t4["+X"] + t5["+X"]
        neg_x = t4["-X"] + t5["-X"]
        pos_y = t4["+Y"] + t5["+Y"]
        neg_y = t4["-Y"] + t5["-Y"]

        flow_x = pos_x - neg_x
        flow_y = pos_y - neg_y

        vx_flow = float(np.mean(flow_x))
        vy_flow = float(np.mean(flow_y))
        yaw_pan = float(np.mean(flow_x * self._hs_weights))
        radial_div = float(np.mean(flow_x * self._ux + flow_y * self._uy))

        return MotionOutput(
            t4=t4,
            t5=t5,
            pos_x=pos_x,
            neg_x=neg_x,
            pos_y=pos_y,
            neg_y=neg_y,
            flow_x=flow_x,
            flow_y=flow_y,
            vx_flow=vx_flow,
            vy_flow=vy_flow,
            yaw_pan=yaw_pan,
            radial_divergence=radial_div,
        )

    def __call__(
        self,
        lamina_or_l1: Union[LaminaOutput, np.ndarray],
        l2_off: Optional[np.ndarray] = None,
    ) -> MotionOutput:
        return self.step(lamina_or_l1, l2_off)
