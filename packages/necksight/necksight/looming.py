"""Lobula Plate LPLC2 4-quadrant radial looming detector and Giant Fiber reflex."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple, Union

import numpy as np

from .emd import MotionOutput, T4T5MotionEngine
from .retina import RetinaLaminaPreproc


@dataclass(slots=True)
class GiantFiberTrigger:
    """Output of the LPLC2 -> Giant Fiber (DNp01) collision escape reflex.

    Attributes:
        triggered: True when ``looming_score >= threshold``.
        looming_score: Giant Fiber membrane looming score (>= 0).
        est_ttc_s: Estimated time-to-collision in seconds via optical tau (``inf`` if idle).
        focus_xy: Estimated focus-of-expansion ``(x, y)`` in retinal grid coordinates.
        quadrants: Winning LPLC2 4-arm outward activities ``(Q_left, Q_right, Q_up, Q_down)``.
        pan_inhibition: Wide-field LPTC tangential pan suppression applied this frame.
        reflex_dispatched: True if an attached NLTP reflex hook fired on this step.
    """

    triggered: bool
    looming_score: float
    est_ttc_s: float
    focus_xy: Tuple[float, float]
    quadrants: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    pan_inhibition: float = 0.0
    reflex_dispatched: bool = False


@dataclass(slots=True)
class _ScaleArmBounds:
    radius: int
    ly0: np.ndarray
    ly1: np.ndarray
    lx0: np.ndarray
    lx1: np.ndarray
    inv_area_l: np.ndarray
    rx0: np.ndarray
    rx1: np.ndarray
    inv_area_r: np.ndarray
    uy0: np.ndarray
    uy1: np.ndarray
    ux0: np.ndarray
    ux1: np.ndarray
    inv_area_u: np.ndarray
    dy0: np.ndarray
    dy1: np.ndarray
    inv_area_d: np.ndarray


def _query_sat_precomputed(
    sat: np.ndarray,
    y0: np.ndarray,
    y1: np.ndarray,
    x0: np.ndarray,
    x1: np.ndarray,
    inv_area: np.ndarray,
) -> np.ndarray:
    sums = sat[y1, x1] - sat[y0, x1] - sat[y1, x0] + sat[y0, x0]
    return sums * inv_area


class LPLC2LoomingReflex:
    """Biophysical *Drosophila* LPLC2 radial looming gate + Giant Fiber trigger.

    Models the Lobula Plate-Lobula Columnar Type 2 (``LPLC2``) population
    converging onto the Giant Fiber (``GF`` / ``DNp01``) escape neuron
    (Klapoetke et al., *Nature* 2017; Ache et al., *Current Biology* 2019):
      - Evaluates an overlapping retinal array of multi-scale cruciform ``LPLC2``
        receptive fields using pre-indexed ``O(1)`` 2D Summed-Area Tables.
      - Each arm integrates outward centrifugal motion minus local inward
        ``LPi`` opponent inhibition before rectifying:
        ``Q_left_out``, ``Q_right_out``, ``Q_up_out``, ``Q_down_out``.
      - Applies a **4-quadrant geometric-mean radial symmetry gate**:
        ``(Q_L * Q_R * Q_U * Q_D) ** 0.25``.
        During fast camera yaw/pitch pans, receding motion, or passing cross-traffic,
        at least one opposing arm receives inward motion (``Q == 0``), collapsing
        the geometric product to zero.
      - Applies **wide-field LPTC tangential pan inhibition** so global rotational
        flow actively suppresses Giant Fiber firing.
      - Estimates physical Time-To-Collision (``est_ttc_s``) via Lee's optical tau
        ``r / (dr/dt)`` around the detected focus of expansion ``focus_xy``.
    """

    def __init__(
        self,
        grid_size: Union[int, Tuple[int, int]] = 64,
        *,
        threshold: float = 0.045,
        arm_opponency: float = 1.25,
        pan_sub_weight: float = 0.65,
        pan_div_weight: float = 12.0,
        gf_alpha: float = 0.65,
        scales: Optional[Sequence[int]] = None,
        stride: Optional[int] = None,
        on_trigger: Optional[Callable[[GiantFiberTrigger], Any]] = None,
    ) -> None:
        if isinstance(grid_size, int):
            self.grid_shape: Tuple[int, int] = (grid_size, grid_size)
        else:
            self.grid_shape = (int(grid_size[0]), int(grid_size[1]))

        gh, gw = self.grid_shape
        if gh < 12 or gw < 12:
            raise ValueError("grid_size dimensions must be >= 12 for LPLC2 quadrants")

        self.threshold = float(threshold)
        self.arm_opponency = float(arm_opponency)
        self.pan_sub_weight = float(pan_sub_weight)
        self.pan_div_weight = float(pan_div_weight)
        self.gf_alpha = float(gf_alpha)

        min_dim = min(gh, gw)
        if scales is None:
            r1 = max(3, round(min_dim * 0.10))
            r2 = max(5, round(min_dim * 0.22))
            r3 = max(7, round(min_dim * 0.36))
            self.scales: Tuple[int, ...] = tuple(sorted({r1, r2, r3}))
        else:
            self.scales = tuple(int(r) for r in scales)

        if stride is None:
            self.stride = max(2, min_dim // 16)
        else:
            self.stride = max(1, int(stride))

        margin = max(3, min(self.scales))
        cy_vals = np.arange(margin, gh - margin, self.stride, dtype=np.intp)
        cx_vals = np.arange(margin, gw - margin, self.stride, dtype=np.intp)
        if cy_vals.size == 0:
            cy_vals = np.array([gh // 2], dtype=np.intp)
        if cx_vals.size == 0:
            cx_vals = np.array([gw // 2], dtype=np.intp)

        cy_grid, cx_grid = np.meshgrid(cy_vals, cx_vals, indexing="ij")
        self._cy = cy_grid
        self._cx = cx_grid

        # Precompute arm bounds across scales for fast O(1) SAT evaluation
        self._scale_bounds: List[_ScaleArmBounds] = []
        for r in self.scales:
            hw = max(2, int(round(0.65 * r)))
            gap = 1
            lx0 = np.clip(cx_grid - r, 0, gw - 1)
            lx1 = np.clip(cx_grid - gap, lx0 + 1, gw)
            ly0 = np.clip(cy_grid - hw, 0, gh - 1)
            ly1 = np.clip(cy_grid + hw + 1, ly0 + 1, gh)

            rx0 = np.clip(cx_grid + gap + 1, 0, gw - 1)
            rx1 = np.clip(cx_grid + r + 1, rx0 + 1, gw)

            uy0 = np.clip(cy_grid - r, 0, gh - 1)
            uy1 = np.clip(cy_grid - gap, uy0 + 1, gh)
            ux0 = np.clip(cx_grid - hw, 0, gw - 1)
            ux1 = np.clip(cx_grid + hw + 1, ux0 + 1, gw)

            dy0 = np.clip(cy_grid + gap + 1, 0, gh - 1)
            dy1 = np.clip(cy_grid + r + 1, dy0 + 1, gh)

            inv_l = 1.0 / np.maximum((ly1 - ly0) * (lx1 - lx0), 1).astype(np.float32)
            inv_r = 1.0 / np.maximum((ly1 - ly0) * (rx1 - rx0), 1).astype(np.float32)
            inv_u = 1.0 / np.maximum((uy1 - uy0) * (ux1 - ux0), 1).astype(np.float32)
            inv_d = 1.0 / np.maximum((dy1 - dy0) * (ux1 - ux0), 1).astype(np.float32)

            self._scale_bounds.append(
                _ScaleArmBounds(
                    radius=r,
                    ly0=ly0,
                    ly1=ly1,
                    lx0=lx0,
                    lx1=lx1,
                    inv_area_l=inv_l,
                    rx0=rx0,
                    rx1=rx1,
                    inv_area_r=inv_r,
                    uy0=uy0,
                    uy1=uy1,
                    ux0=ux0,
                    ux1=ux1,
                    inv_area_u=inv_u,
                    dy0=dy0,
                    dy1=dy1,
                    inv_area_d=inv_d,
                )
            )

        self._sat_l = np.zeros((gh + 1, gw + 1), dtype=np.float32)
        self._sat_r = np.zeros((gh + 1, gw + 1), dtype=np.float32)
        self._sat_u = np.zeros((gh + 1, gw + 1), dtype=np.float32)
        self._sat_d = np.zeros((gh + 1, gw + 1), dtype=np.float32)

        yy, xx = np.meshgrid(
            np.arange(gh, dtype=np.float32),
            np.arange(gw, dtype=np.float32),
            indexing="ij",
        )
        self._yy = yy
        self._xx = xx

        self._gf_potential: float = 0.0
        self._prev_rim_radius: Optional[float] = None
        self._prev_focus: Optional[Tuple[float, float]] = None
        self._dr_dt_ema: float = 0.0
        self._hooks: List[Callable[[GiantFiberTrigger], Any]] = []
        if on_trigger is not None:
            self._hooks.append(on_trigger)

    def reset(self) -> None:
        """Reset Giant Fiber membrane state and optical-tau tracking."""
        self._gf_potential = 0.0
        self._prev_rim_radius = None
        self._prev_focus = None
        self._dr_dt_ema = 0.0

    def attach_nltp_reflex(
        self,
        nltp_client: Any,
        msg: Union[str, bytes] = "LOOM_ESTOP",
    ) -> Callable[[GiantFiberTrigger], Any]:
        """Attach a zero-dependency ``pynltp`` reflex hook that calls ``send_reflex(msg)``."""
        hook = _make_nltp_hook(nltp_client, msg=msg)
        self._hooks.append(hook)
        return hook

    @staticmethod
    def _fill_sat(arr: np.ndarray, sat: np.ndarray) -> None:
        np.cumsum(arr, axis=0, out=sat[1:, 1:])
        np.cumsum(sat[1:, 1:], axis=1, out=sat[1:, 1:])

    def _estimate_ttc(
        self,
        motion: MotionOutput,
        fx: float,
        fy: float,
        best_scale: int,
        dt: float,
    ) -> float:
        """Estimate Time-To-Collision (s) around ``(fx, fy)`` via optical tau ``r / dr_dt``."""
        gh, gw = self.grid_shape
        max_r = float(max(best_scale * 1.5, 6.0))
        r_int = int(np.ceil(max_r)) + 1
        y0 = max(0, int(fy) - r_int)
        y1 = min(gh, int(fy) + r_int + 1)
        x0 = max(0, int(fx) - r_int)
        x1 = min(gw, int(fx) + r_int + 1)

        dx = self._xx[y0:y1, x0:x1] - fx
        dy = self._yy[y0:y1, x0:x1] - fy
        rad = np.hypot(dx, dy)
        mask = (rad >= 1.0) & (rad <= max_r)
        if not np.any(mask):
            return float("inf")

        rm = rad[mask]
        ux = dx[mask] / rm
        uy = dy[mask] / rm
        fx_sub = motion.flow_x[y0:y1, x0:x1][mask]
        fy_sub = motion.flow_y[y0:y1, x0:x1][mask]
        pos_w = np.maximum(0.0, fx_sub * ux + fy_sub * uy)
        w_sum = float(np.sum(pos_w))
        if w_sum <= 1e-6:
            return float("inf")

        r_curr = float(np.sum(pos_w * rm) / w_sum)
        active = pos_w[pos_w > 0.0]
        v_inst_px_per_s = max(float(np.mean(active)) * 6.0 / max(dt, 1e-4), 1e-3)

        if (
            self._prev_rim_radius is not None
            and self._prev_focus is not None
            and np.hypot(fx - self._prev_focus[0], fy - self._prev_focus[1]) <= max_r * 0.6
        ):
            dr_dt = (r_curr - self._prev_rim_radius) / max(dt, 1e-4)
            if dr_dt > 0.0:
                if self._dr_dt_ema <= 0.0:
                    self._dr_dt_ema = dr_dt
                else:
                    self._dr_dt_ema = 0.55 * self._dr_dt_ema + 0.45 * dr_dt

        self._prev_rim_radius = r_curr
        self._prev_focus = (fx, fy)

        rate = self._dr_dt_ema if self._dr_dt_ema > 0.25 else v_inst_px_per_s
        ttc = r_curr / max(rate, 1e-3)
        return float(np.clip(ttc, 0.005, 30.0))

    def step(self, motion: MotionOutput, dt: float = 1.0 / 60.0) -> GiantFiberTrigger:
        """Evaluate LPLC2 4-quadrant radial symmetry gate and Giant Fiber trigger."""
        gh, gw = self.grid_shape
        opp = self.arm_opponency

        # Signed outward minus inward synaptic drive fields for each cardinal arm
        self._fill_sat(motion.neg_x - opp * motion.pos_x, self._sat_l)
        self._fill_sat(motion.pos_x - opp * motion.neg_x, self._sat_r)
        self._fill_sat(motion.neg_y - opp * motion.pos_y, self._sat_u)
        self._fill_sat(motion.pos_y - opp * motion.neg_y, self._sat_d)

        cy = self._cy
        cx = self._cx
        grid_shape = cy.shape

        best_map = np.zeros(grid_shape, dtype=np.float32)
        best_ql = np.zeros(grid_shape, dtype=np.float32)
        best_qr = np.zeros(grid_shape, dtype=np.float32)
        best_qu = np.zeros(grid_shape, dtype=np.float32)
        best_qd = np.zeros(grid_shape, dtype=np.float32)
        best_scale_map = np.full(grid_shape, self.scales[0], dtype=np.intp)

        for sb in self._scale_bounds:
            ql = np.maximum(
                0.0,
                _query_sat_precomputed(
                    self._sat_l, sb.ly0, sb.ly1, sb.lx0, sb.lx1, sb.inv_area_l
                ),
            )
            qr = np.maximum(
                0.0,
                _query_sat_precomputed(
                    self._sat_r, sb.ly0, sb.ly1, sb.rx0, sb.rx1, sb.inv_area_r
                ),
            )
            qu = np.maximum(
                0.0,
                _query_sat_precomputed(
                    self._sat_u, sb.uy0, sb.uy1, sb.ux0, sb.ux1, sb.inv_area_u
                ),
            )
            qd = np.maximum(
                0.0,
                _query_sat_precomputed(
                    self._sat_d, sb.dy0, sb.dy1, sb.ux0, sb.ux1, sb.inv_area_d
                ),
            )

            # Fast exact 4-quadrant geometric mean: (Q_L * Q_R * Q_U * Q_D) ** 0.25
            sym = np.sqrt(np.sqrt(ql * qr * qu * qd))

            better = sym > best_map
            if np.any(better):
                best_map = np.where(better, sym, best_map)
                best_ql = np.where(better, ql, best_ql)
                best_qr = np.where(better, qr, best_qr)
                best_qu = np.where(better, qu, best_qu)
                best_qd = np.where(better, qd, best_qd)
                best_scale_map = np.where(better, sb.radius, best_scale_map)

        # Wide-field LPTC tangential pan suppression
        pan_mag = float(np.hypot(motion.vx_flow, motion.vy_flow))
        total_energy = float(
            np.mean(motion.pos_x + motion.neg_x) + np.mean(motion.pos_y + motion.neg_y)
        )
        pan_coherence = pan_mag / (total_energy + 1e-6)
        coherence_pen = 8.0 * max(0.0, pan_coherence - 0.35) ** 2
        pan_denom = 1.0 + self.pan_div_weight * pan_mag + coherence_pen

        peak_flat_idx = int(np.argmax(best_map))
        iy, ix = np.unravel_index(peak_flat_idx, grid_shape)
        peak_sym = float(best_map[iy, ix])

        gated_raw = max(0.0, peak_sym - self.pan_sub_weight * pan_mag) / pan_denom
        pan_inhib = max(0.0, peak_sym - gated_raw)

        # Giant Fiber leaky membrane potential integration
        a = self.gf_alpha
        if gated_raw > 0.0:
            self._gf_potential = (1.0 - a) * self._gf_potential + a * gated_raw
        else:
            self._gf_potential = 0.35 * (1.0 - a) * self._gf_potential

        score = float(self._gf_potential)

        if peak_sym > 1e-6:
            y_lo = max(0, iy - 1)
            y_hi = min(grid_shape[0], iy + 2)
            x_lo = max(0, ix - 1)
            x_hi = min(grid_shape[1], ix + 2)
            patch_w = best_map[y_lo:y_hi, x_lo:x_hi]
            w_tot = float(np.sum(patch_w))
            if w_tot > 1e-9:
                fx = float(np.sum(patch_w * cx[y_lo:y_hi, x_lo:x_hi]) / w_tot)
                fy = float(np.sum(patch_w * cy[y_lo:y_hi, x_lo:x_hi]) / w_tot)
            else:
                fx = float(cx[iy, ix])
                fy = float(cy[iy, ix])
            ttc = self._estimate_ttc(motion, fx, fy, int(best_scale_map[iy, ix]), dt)
            quads = (
                float(best_ql[iy, ix]),
                float(best_qr[iy, ix]),
                float(best_qu[iy, ix]),
                float(best_qd[iy, ix]),
            )
        else:
            self._prev_rim_radius = None
            self._dr_dt_ema = 0.0
            fx = 0.5 * (gw - 1)
            fy = 0.5 * (gh - 1)
            ttc = float("inf")
            quads = (0.0, 0.0, 0.0, 0.0)

        triggered = bool(score >= self.threshold)
        trigger = GiantFiberTrigger(
            triggered=triggered,
            looming_score=score,
            est_ttc_s=ttc,
            focus_xy=(fx, fy),
            quadrants=quads,
            pan_inhibition=float(pan_inhib),
            reflex_dispatched=False,
        )

        if triggered and self._hooks:
            for hook in self._hooks:
                hook(trigger)
            trigger.reflex_dispatched = True

        return trigger

    def __call__(self, motion: MotionOutput, dt: float = 1.0 / 60.0) -> GiantFiberTrigger:
        return self.step(motion, dt=dt)


class NeckSightPipeline:
    """End-to-end Retina -> Lamina L1/L2 -> T4/T5 EMD -> LPLC2 Giant Fiber pipeline."""

    def __init__(
        self,
        grid_size: Union[int, Tuple[int, int]] = 64,
        *,
        threshold: float = 0.045,
        nltp_client: Optional[Any] = None,
        nltp_msg: Union[str, bytes] = "LOOM_ESTOP",
    ) -> None:
        self.retina = RetinaLaminaPreproc(grid_size=grid_size)
        self.emd = T4T5MotionEngine(grid_size=grid_size)
        self.looming = LPLC2LoomingReflex(grid_size=grid_size, threshold=threshold)
        if nltp_client is not None:
            self.looming.attach_nltp_reflex(nltp_client, msg=nltp_msg)

    def reset(self) -> None:
        """Reset all stages of the optic-lobe pipeline."""
        self.retina.reset()
        self.emd.reset()
        self.looming.reset()

    def attach_nltp_reflex(
        self,
        nltp_client: Any,
        msg: Union[str, bytes] = "LOOM_ESTOP",
    ) -> Callable[[GiantFiberTrigger], Any]:
        """Attach an out-of-line ``pynltp`` ``send_reflex`` callback on looming trigger."""
        return self.looming.attach_nltp_reflex(nltp_client, msg=msg)

    def derotate(self, ego_shift_px: Optional[Tuple[float, float]]) -> None:
        """Apply this frame's camera-rotation image shift ``(dx, dy)`` (grid pixels) to all state."""
        if ego_shift_px is None:
            return
        dx, dy = float(ego_shift_px[0]), float(ego_shift_px[1])
        if dx == 0.0 and dy == 0.0:
            return
        self.retina.shift_state(dx, dy)
        self.emd.shift_state(dx, dy)

    def step(
        self,
        frame: np.ndarray,
        dt: float = 1.0 / 60.0,
        ego_shift_px: Optional[Tuple[float, float]] = None,
    ) -> GiantFiberTrigger:
        """Process one camera frame and return the Giant Fiber trigger result.

        ``ego_shift_px`` is the image shift caused by camera rotation since the previous frame,
        in grid pixels (see :func:`necksight.gyro_to_shift_px`). Supplying it from a gyro keeps
        looming detection working while the platform yaws or pitches; without it, fast turns
        suppress the looming response along with the false alarms.
        """
        self.derotate(ego_shift_px)
        lam = self.retina.step(frame, dt=dt)
        mot = self.emd.step(lam)
        return self.looming.step(mot, dt=dt)

    def step_full(
        self,
        frame: np.ndarray,
        dt: float = 1.0 / 60.0,
        ego_shift_px: Optional[Tuple[float, float]] = None,
    ) -> Tuple[Any, MotionOutput, GiantFiberTrigger]:
        """Process one frame and return ``(lamina_out, motion_out, trigger_out)``."""
        self.derotate(ego_shift_px)
        lam = self.retina.step(frame, dt=dt)
        mot = self.emd.step(lam)
        trig = self.looming.step(mot, dt=dt)
        return lam, mot, trig


def _make_nltp_hook(
    nltp_client: Any,
    msg: Union[str, bytes] = "LOOM_ESTOP",
) -> Callable[[GiantFiberTrigger], Any]:
    if not hasattr(nltp_client, "send_reflex") or not callable(nltp_client.send_reflex):
        raise TypeError(
            "nltp_client must expose a callable send_reflex(msg) method (compatible with pynltp)"
        )

    def _dispatch(trigger: GiantFiberTrigger) -> Any:
        if trigger.triggered:
            return nltp_client.send_reflex(msg)
        return None

    return _dispatch


def attach_nltp_reflex(
    nltp_client: Any,
    msg: Union[str, bytes] = "LOOM_ESTOP",
    *,
    detector: Optional[Union[LPLC2LoomingReflex, NeckSightPipeline]] = None,
) -> Callable[[GiantFiberTrigger], Any]:
    """Create or bind a zero-dependency ``pynltp`` Giant-Fiber reflex callback."""
    if isinstance(nltp_client, (LPLC2LoomingReflex, NeckSightPipeline)) and hasattr(
        msg, "send_reflex"
    ):
        raise TypeError(
            "Pass (nltp_client, msg='LOOM_ESTOP', detector=...) or call detector.attach_nltp_reflex(nltp_client, msg)"
        )

    hook = _make_nltp_hook(nltp_client, msg=msg)
    if detector is not None:
        if isinstance(detector, LPLC2LoomingReflex):
            detector._hooks.append(hook)
        else:
            detector.looming._hooks.append(hook)
    return hook
