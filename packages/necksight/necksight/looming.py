"""Lobula Plate LPLC2 4-quadrant radial + LC4 thin-bar looming detector and Giant Fiber/DNa02 reflex."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple, Union

import numpy as np

from .emd import MotionOutput, T4T5MotionEngine
from .retina import RetinaLaminaPreproc


@dataclass
class GiantFiberTrigger:
    """Output of the LPLC2/LC4 -> Giant Fiber (DNp01) + DNa02 collision escape reflex.

    Attributes:
        triggered: True when ``looming_score >= threshold``.
        looming_score: Giant Fiber membrane looming score (>= 0).
        est_ttc_s: Estimated time-to-collision in seconds via optical tau (``inf`` if idle).
        focus_xy: Estimated focus-of-expansion ``(x, y)`` in retinal grid coordinates.
        quadrants: Winning 4-arm outward activities ``(Q_left, Q_right, Q_up, Q_down)``.
        pan_inhibition: Wide-field LPTC tangential pan suppression applied this frame.
        reflex_dispatched: True if an attached NLTP reflex hook fired on this step.
        avoid_x: Contralateral horizontal dodge vector in ``[-1, +1]`` (``+`` right, ``-`` left).
        avoid_y: Contralateral vertical dodge vector in ``[-1, +1]`` (``+`` down, ``-`` up).
        bar_score: Leaky ``LC4`` / ``LPLC1`` thin-obstacle (pole/wire) looming score (>= 0).
    """

    triggered: bool
    looming_score: float
    est_ttc_s: float
    focus_xy: Tuple[float, float]
    quadrants: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    pan_inhibition: float = 0.0
    reflex_dispatched: bool = False
    avoid_x: float = 0.0
    avoid_y: float = 0.0
    bar_score: float = 0.0

    @property
    def avoid_xy(self) -> Tuple[float, float]:
        """Return directional evasive vector ``(avoid_x, avoid_y)`` in ``[-1, +1]^2``."""
        return (self.avoid_x, self.avoid_y)


@dataclass
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


@dataclass
class _LC4SplitStripBounds:
    radius: int
    y0_a: np.ndarray
    y1_a: np.ndarray
    x0_a: np.ndarray
    x1_a: np.ndarray
    inv_a: np.ndarray
    y0_b: np.ndarray
    y1_b: np.ndarray
    x0_b: np.ndarray
    x1_b: np.ndarray
    inv_b: np.ndarray
    y0_c: np.ndarray
    y1_c: np.ndarray
    x0_c: np.ndarray
    x1_c: np.ndarray
    inv_c: np.ndarray
    y0_d: np.ndarray
    y1_d: np.ndarray
    x0_d: np.ndarray
    x1_d: np.ndarray
    inv_d: np.ndarray


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


def _make_inv_area(
    y0: np.ndarray, y1: np.ndarray, x0: np.ndarray, x1: np.ndarray
) -> np.ndarray:
    return 1.0 / np.maximum((y1 - y0) * (x1 - x0), 1).astype(np.float32)


class LPLC2LoomingReflex:
    """Biophysical *Drosophila* LPLC2 + LC4 looming gate, Giant Fiber trigger & DNa02 steering.

    Models the Lobula columnar looming triad converging onto Descending Neurons
    (``DNp01`` Giant Fiber + ``DNa02`` contralateral steering; Klapoetke et al.,
    *Nature* 2017; Ache et al., *Current Biology* 2019; *BANC v888* connectome):
      - **LPLC2 4-arm radial gate**: ``(Q_L * Q_R * Q_U * Q_D) ** 0.25`` across multi-scale
        cruciform receptive fields via ``O(1)`` 2D Summed-Area Tables.
      - **LC4 / LPLC1 thin-obstacle bar/wire channel**: split-strip opponent expansion
        detectors catching thin vertical poles/trees (outward ``-X`` on left and ``+X``
        on right across both halves of a tall vertical strip) and horizontal wires/beams
        (outward ``-Y`` above and ``+Y`` below across both halves of a wide horizontal
        strip), weighted by ``lc4_weight`` (default ``0.42``, matching *BANC v888*
        ``2,053 / (2,896 + 2,053)`` DN synapse fraction) while rejecting camera pans,
        pure camera roll, receding motion, and lateral cross-traffic.
      - **Wide-field LPTC tangential pan inhibition** suppressing Giant Fiber firing during
        uncompensated global rotational flow.
      - **DNa02 contralateral directional avoidance** ``(avoid_x, avoid_y)`` in ``[-1, +1]``
        pointing away from ``focus_xy`` (or toward the less-occluded half when dead-center).
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
        enable_lc4_bar: bool = True,
        lc4_weight: float = 0.42,
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
        if lc4_weight < 0.0:
            raise ValueError("lc4_weight must be >= 0.0")

        self.threshold = float(threshold)
        self.arm_opponency = float(arm_opponency)
        self.pan_sub_weight = float(pan_sub_weight)
        self.pan_div_weight = float(pan_div_weight)
        self.gf_alpha = float(gf_alpha)
        self.enable_lc4_bar = bool(enable_lc4_bar)
        self.lc4_weight = float(lc4_weight)

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

        # Precompute LPLC2 4-arm bounds across scales for fast O(1) SAT evaluation
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

            inv_l = _make_inv_area(ly0, ly1, lx0, lx1)
            inv_r = _make_inv_area(ly0, ly1, rx0, rx1)
            inv_u = _make_inv_area(uy0, uy1, ux0, ux1)
            inv_d = _make_inv_area(dy0, dy1, ux0, ux1)

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

        # Precompute LC4 split-strip bounds for vertical poles & horizontal wires
        rb_min = 2 if min_dim <= 40 else 3
        rb1 = max(rb_min, round(min_dim * 0.05))
        rb2 = max(rb1 + 2, round(min_dim * 0.095))
        rb3 = max(rb2 + 2, round(min_dim * 0.16))
        self.bar_scales: Tuple[int, ...] = tuple(sorted({rb1, rb2, rb3}))

        l_half_v = max(3, int(round(0.19 * gh)))
        s_tx = max(1, gw // 32)
        s_ly = max(2, gh // 10)
        vcx = np.arange(rb1 + 2, gw - (rb1 + 2), s_tx, dtype=np.intp)
        vcy = np.arange(l_half_v + 1, gh - (l_half_v + 1), s_ly, dtype=np.intp)
        if vcx.size == 0:
            vcx = np.array([gw // 2], dtype=np.intp)
        if vcy.size == 0:
            vcy = np.array([gh // 2], dtype=np.intp)
        self._v_cy, self._v_cx = np.meshgrid(vcy, vcx, indexing="ij")

        self._lc4_vert_bounds: List[_LC4SplitStripBounds] = []
        for rb in self.bar_scales:
            ty0 = np.clip(self._v_cy - l_half_v, 0, gh - 1)
            ty1 = np.clip(self._v_cy, ty0 + 1, gh)
            by0 = np.clip(self._v_cy + 1, 0, gh - 1)
            by1 = np.clip(self._v_cy + 1 + l_half_v, by0 + 1, gh)
            lx0 = np.clip(self._v_cx - rb, 0, gw - 1)
            lx1 = np.clip(self._v_cx, lx0 + 1, gw)
            rx0 = np.clip(self._v_cx + 1, 0, gw - 1)
            rx1 = np.clip(self._v_cx + 1 + rb, rx0 + 1, gw)
            self._lc4_vert_bounds.append(
                _LC4SplitStripBounds(
                    radius=rb,
                    y0_a=ty0,
                    y1_a=ty1,
                    x0_a=lx0,
                    x1_a=lx1,
                    inv_a=_make_inv_area(ty0, ty1, lx0, lx1),
                    y0_b=ty0,
                    y1_b=ty1,
                    x0_b=rx0,
                    x1_b=rx1,
                    inv_b=_make_inv_area(ty0, ty1, rx0, rx1),
                    y0_c=by0,
                    y1_c=by1,
                    x0_c=lx0,
                    x1_c=lx1,
                    inv_c=_make_inv_area(by0, by1, lx0, lx1),
                    y0_d=by0,
                    y1_d=by1,
                    x0_d=rx0,
                    x1_d=rx1,
                    inv_d=_make_inv_area(by0, by1, rx0, rx1),
                )
            )

        l_half_h = max(3, int(round(0.19 * gw)))
        s_ty = max(1, gh // 32)
        s_lx = max(2, gw // 10)
        hcy = np.arange(rb1 + 2, gh - (rb1 + 2), s_ty, dtype=np.intp)
        hcx = np.arange(l_half_h + 1, gw - (l_half_h + 1), s_lx, dtype=np.intp)
        if hcy.size == 0:
            hcy = np.array([gh // 2], dtype=np.intp)
        if hcx.size == 0:
            hcx = np.array([gw // 2], dtype=np.intp)
        self._h_cy, self._h_cx = np.meshgrid(hcy, hcx, indexing="ij")

        self._lc4_horiz_bounds: List[_LC4SplitStripBounds] = []
        for rb in self.bar_scales:
            uy0 = np.clip(self._h_cy - rb, 0, gh - 1)
            uy1 = np.clip(self._h_cy, uy0 + 1, gh)
            dy0 = np.clip(self._h_cy + 1, 0, gh - 1)
            dy1 = np.clip(self._h_cy + 1 + rb, dy0 + 1, gh)
            lx0 = np.clip(self._h_cx - l_half_h, 0, gw - 1)
            lx1 = np.clip(self._h_cx, lx0 + 1, gw)
            rx0 = np.clip(self._h_cx + 1, 0, gw - 1)
            rx1 = np.clip(self._h_cx + 1 + l_half_h, rx0 + 1, gw)
            self._lc4_horiz_bounds.append(
                _LC4SplitStripBounds(
                    radius=rb,
                    y0_a=uy0,
                    y1_a=uy1,
                    x0_a=lx0,
                    x1_a=lx1,
                    inv_a=_make_inv_area(uy0, uy1, lx0, lx1),
                    y0_b=dy0,
                    y1_b=dy1,
                    x0_b=lx0,
                    x1_b=lx1,
                    inv_b=_make_inv_area(dy0, dy1, lx0, lx1),
                    y0_c=uy0,
                    y1_c=uy1,
                    x0_c=rx0,
                    x1_c=rx1,
                    inv_c=_make_inv_area(uy0, uy1, rx0, rx1),
                    y0_d=dy0,
                    y1_d=dy1,
                    x0_d=rx0,
                    x1_d=rx1,
                    inv_d=_make_inv_area(dy0, dy1, rx0, rx1),
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
        self._lc4_potential: float = 0.0
        self._prev_rim_radius: Optional[float] = None
        self._prev_focus: Optional[Tuple[float, float]] = None
        self._dr_dt_ema: float = 0.0
        self._hooks: List[Callable[[GiantFiberTrigger], Any]] = []
        if on_trigger is not None:
            self._hooks.append(on_trigger)

    def reset(self) -> None:
        """Reset Giant Fiber, LC4 bar state, and optical-tau tracking."""
        self._gf_potential = 0.0
        self._lc4_potential = 0.0
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

    @staticmethod
    def _centroid_3x3(
        score_map: np.ndarray,
        cx_grid: np.ndarray,
        cy_grid: np.ndarray,
        iy: int,
        ix: int,
    ) -> Tuple[float, float]:
        y_lo = max(0, iy - 1)
        y_hi = min(score_map.shape[0], iy + 2)
        x_lo = max(0, ix - 1)
        x_hi = min(score_map.shape[1], ix + 2)
        patch_w = score_map[y_lo:y_hi, x_lo:x_hi]
        w_tot = float(np.sum(patch_w))
        if w_tot > 1e-9:
            fx = float(np.sum(patch_w * cx_grid[y_lo:y_hi, x_lo:x_hi]) / w_tot)
            fy = float(np.sum(patch_w * cy_grid[y_lo:y_hi, x_lo:x_hi]) / w_tot)
            return fx, fy
        return float(cx_grid[iy, ix]), float(cy_grid[iy, ix])

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
        v_inst_px_per_s = max(float(np.mean(active)) * 6.0 / max(dt, 1e-3 if dt <= 0 else dt), 1e-3)

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

    def _compute_avoid_vector(
        self,
        motion: MotionOutput,
        fx: float,
        fy: float,
        score: float,
        *,
        horiz_bar: bool = False,
    ) -> Tuple[float, float]:
        """Compute contralateral DNa02 avoidance vector ``(avoid_x, avoid_y)`` in ``[-1, +1]``."""
        if score <= 1e-6:
            return 0.0, 0.0

        gh, gw = self.grid_shape
        cx0 = 0.5 * (gw - 1)
        cy0 = 0.5 * (gh - 1)
        dx_away = cx0 - fx
        dy_away = cy0 - fy
        dist_c = math.hypot(dx_away, dy_away)
        deadzone = max(1.0, 0.04 * min(gh, gw))

        if dist_c > deadzone:
            ux = dx_away / dist_c
            uy = dy_away / dist_c
        else:
            # Dead-center obstacle: steer toward the less-occluded side
            energy = motion.pos_x + motion.neg_x + motion.pos_y + motion.neg_y
            if horiz_bar:
                half_h = gh // 2
                top_occ = float(np.mean(energy[:half_h, :]))
                bot_occ = float(np.mean(energy[half_h:, :]))
                ux = 0.0
                uy = 1.0 if top_occ >= bot_occ else -1.0
            else:
                half_w = gw // 2
                left_occ = float(np.mean(energy[:, :half_w]))
                right_occ = float(np.mean(energy[:, half_w:]))
                ux = 1.0 if left_occ >= right_occ else -1.0
                uy = 0.0

        urgency = min(1.0, score / max(self.threshold, 1e-6))
        return (
            float(np.clip(ux * urgency, -1.0, 1.0)),
            float(np.clip(uy * urgency, -1.0, 1.0)),
        )

    def step(self, motion: MotionOutput, dt: float = 1.0 / 60.0) -> GiantFiberTrigger:
        """Evaluate LPLC2 4-arm + LC4 thin-bar looming gates and Giant Fiber/DNa02 reflex."""
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

        peak_flat_idx = int(np.argmax(best_map))
        iy, ix = np.unravel_index(peak_flat_idx, grid_shape)
        peak_lplc2 = float(best_map[iy, ix])

        # Evaluate LC4 / LPLC1 thin-obstacle bar/wire split-strip channel
        weighted_lc4 = 0.0
        lc4_is_vert = True
        v_peak = 0.0
        h_peak = 0.0
        best_v_map: Optional[np.ndarray] = None
        best_v_ql: Optional[np.ndarray] = None
        best_v_qr: Optional[np.ndarray] = None
        best_v_sc: Optional[np.ndarray] = None
        best_h_map: Optional[np.ndarray] = None
        best_h_qu: Optional[np.ndarray] = None
        best_h_qd: Optional[np.ndarray] = None
        best_h_sc: Optional[np.ndarray] = None

        if self.enable_lc4_bar and self.lc4_weight > 0.0:
            v_shape = self._v_cy.shape
            best_v_map = np.zeros(v_shape, dtype=np.float32)
            best_v_ql = np.zeros(v_shape, dtype=np.float32)
            best_v_qr = np.zeros(v_shape, dtype=np.float32)
            best_v_sc = np.full(v_shape, self.bar_scales[0], dtype=np.intp)

            for vb in self._lc4_vert_bounds:
                lt = np.maximum(
                    0.0,
                    _query_sat_precomputed(
                        self._sat_l, vb.y0_a, vb.y1_a, vb.x0_a, vb.x1_a, vb.inv_a
                    ),
                )
                rt = np.maximum(
                    0.0,
                    _query_sat_precomputed(
                        self._sat_r, vb.y0_b, vb.y1_b, vb.x0_b, vb.x1_b, vb.inv_b
                    ),
                )
                lb = np.maximum(
                    0.0,
                    _query_sat_precomputed(
                        self._sat_l, vb.y0_c, vb.y1_c, vb.x0_c, vb.x1_c, vb.inv_c
                    ),
                )
                rb_q = np.maximum(
                    0.0,
                    _query_sat_precomputed(
                        self._sat_r, vb.y0_d, vb.y1_d, vb.x0_d, vb.x1_d, vb.inv_d
                    ),
                )
                sym_v = np.sqrt(np.sqrt(lt * rt * lb * rb_q))
                better_v = sym_v > best_v_map
                if np.any(better_v):
                    best_v_map = np.where(better_v, sym_v, best_v_map)
                    best_v_ql = np.where(better_v, 0.5 * (lt + lb), best_v_ql)
                    best_v_qr = np.where(better_v, 0.5 * (rt + rb_q), best_v_qr)
                    best_v_sc = np.where(better_v, vb.radius, best_v_sc)

            h_shape = self._h_cy.shape
            best_h_map = np.zeros(h_shape, dtype=np.float32)
            best_h_qu = np.zeros(h_shape, dtype=np.float32)
            best_h_qd = np.zeros(h_shape, dtype=np.float32)
            best_h_sc = np.full(h_shape, self.bar_scales[0], dtype=np.intp)

            for hb in self._lc4_horiz_bounds:
                ul = np.maximum(
                    0.0,
                    _query_sat_precomputed(
                        self._sat_u, hb.y0_a, hb.y1_a, hb.x0_a, hb.x1_a, hb.inv_a
                    ),
                )
                dl = np.maximum(
                    0.0,
                    _query_sat_precomputed(
                        self._sat_d, hb.y0_b, hb.y1_b, hb.x0_b, hb.x1_b, hb.inv_b
                    ),
                )
                ur = np.maximum(
                    0.0,
                    _query_sat_precomputed(
                        self._sat_u, hb.y0_c, hb.y1_c, hb.x0_c, hb.x1_c, hb.inv_c
                    ),
                )
                dr = np.maximum(
                    0.0,
                    _query_sat_precomputed(
                        self._sat_d, hb.y0_d, hb.y1_d, hb.x0_d, hb.x1_d, hb.inv_d
                    ),
                )
                sym_h = np.sqrt(np.sqrt(ul * dl * ur * dr))
                better_h = sym_h > best_h_map
                if np.any(better_h):
                    best_h_map = np.where(better_h, sym_h, best_h_map)
                    best_h_qu = np.where(better_h, 0.5 * (ul + ur), best_h_qu)
                    best_h_qd = np.where(better_h, 0.5 * (dl + dr), best_h_qd)
                    best_h_sc = np.where(better_h, hb.radius, best_h_sc)

            v_col_profile = np.max(best_v_map, axis=0)
            n_vc = v_col_profile.size
            v_best_c = int(np.argmax(v_col_profile))
            v_raw_peak = float(v_col_profile[v_best_c])
            v_excl = max(2, n_vc // 4)
            v_lo = max(0, v_best_c - v_excl)
            v_hi = min(n_vc, v_best_c + v_excl + 1)
            v_rem_n = n_vc - (v_hi - v_lo)
            if v_rem_n > 0:
                v_surround = float(np.sum(v_col_profile) - np.sum(v_col_profile[v_lo:v_hi])) / v_rem_n
            else:
                v_surround = 0.0
            v_peak = max(0.0, v_raw_peak - 6.0 * v_surround)

            h_row_profile = np.max(best_h_map, axis=1)
            n_hr = h_row_profile.size
            h_best_r = int(np.argmax(h_row_profile))
            h_raw_peak = float(h_row_profile[h_best_r])
            h_excl = max(2, n_hr // 4)
            h_lo = max(0, h_best_r - h_excl)
            h_hi = min(n_hr, h_best_r + h_excl + 1)
            h_rem_n = n_hr - (h_hi - h_lo)
            if h_rem_n > 0:
                h_surround = float(np.sum(h_row_profile) - np.sum(h_row_profile[h_lo:h_hi])) / h_rem_n
            else:
                h_surround = 0.0
            h_peak = max(0.0, h_raw_peak - 6.0 * h_surround)

            # Normalize 1-px thin-rim box average to 4-arm LPLC2 perimeter density
            lc4_is_vert = v_peak >= h_peak
            weighted_lc4 = (2.0 * self.lc4_weight) * max(v_peak, h_peak)

        # Wide-field LPTC tangential pan suppression
        pan_mag = float(np.hypot(motion.vx_flow, motion.vy_flow))
        total_energy = float(
            np.mean(motion.pos_x + motion.neg_x) + np.mean(motion.pos_y + motion.neg_y)
        )
        pan_coherence = pan_mag / (total_energy + 1e-6)
        coherence_pen = 8.0 * max(0.0, pan_coherence - 0.35) ** 2
        pan_denom = 1.0 + self.pan_div_weight * pan_mag + coherence_pen

        peak_sym = max(peak_lplc2, weighted_lc4)
        gated_raw = max(0.0, peak_sym - self.pan_sub_weight * pan_mag) / pan_denom
        gated_lc4 = max(0.0, weighted_lc4 - self.pan_sub_weight * pan_mag) / pan_denom
        pan_inhib = max(0.0, peak_sym - gated_raw)

        # Giant Fiber & LC4 leaky membrane potential integration
        a = self.gf_alpha
        if gated_raw > 0.0:
            self._gf_potential = (1.0 - a) * self._gf_potential + a * gated_raw
        else:
            self._gf_potential = 0.35 * (1.0 - a) * self._gf_potential

        if gated_lc4 > 0.0:
            self._lc4_potential = (1.0 - a) * self._lc4_potential + a * gated_lc4
        else:
            self._lc4_potential = 0.35 * (1.0 - a) * self._lc4_potential

        score = float(self._gf_potential)
        bar_score = float(self._lc4_potential)

        if peak_sym > 1e-6:
            used_horiz_bar = False
            if weighted_lc4 > peak_lplc2 and best_v_map is not None and best_h_map is not None:
                if lc4_is_vert and best_v_ql is not None and best_v_qr is not None and best_v_sc is not None:
                    vi = int(np.argmax(best_v_map))
                    viy, vix = np.unravel_index(vi, best_v_map.shape)
                    x_lo = max(0, vix - 1)
                    x_hi = min(best_v_map.shape[1], vix + 2)
                    col_w = np.max(best_v_map[:, x_lo:x_hi], axis=0)
                    w_tot = float(np.sum(col_w))
                    if w_tot > 1e-9:
                        fx = float(np.sum(col_w * self._v_cx[0, x_lo:x_hi]) / w_tot)
                    else:
                        fx = float(self._v_cx[viy, vix])
                    fy = 0.5 * (gh - 1)
                    win_scale = int(best_v_sc[viy, vix])
                    quads = (float(best_v_ql[viy, vix]), float(best_v_qr[viy, vix]), 0.0, 0.0)
                elif best_h_qu is not None and best_h_qd is not None and best_h_sc is not None:
                    used_horiz_bar = True
                    hi = int(np.argmax(best_h_map))
                    hiy, hix = np.unravel_index(hi, best_h_map.shape)
                    y_lo = max(0, hiy - 1)
                    y_hi = min(best_h_map.shape[0], hiy + 2)
                    row_w = np.max(best_h_map[y_lo:y_hi, :], axis=1)
                    w_tot = float(np.sum(row_w))
                    if w_tot > 1e-9:
                        fy = float(np.sum(row_w * self._h_cy[y_lo:y_hi, 0]) / w_tot)
                    else:
                        fy = float(self._h_cy[hiy, hix])
                    fx = 0.5 * (gw - 1)
                    win_scale = int(best_h_sc[hiy, hix])
                    quads = (0.0, 0.0, float(best_h_qu[hiy, hix]), float(best_h_qd[hiy, hix]))
                else:
                    fx, fy = float(cx[iy, ix]), float(cy[iy, ix])
                    win_scale = int(best_scale_map[iy, ix])
                    quads = (0.0, 0.0, 0.0, 0.0)
            else:
                fx, fy = self._centroid_3x3(best_map, cx, cy, iy, ix)
                win_scale = int(best_scale_map[iy, ix])
                quads = (
                    float(best_ql[iy, ix]),
                    float(best_qr[iy, ix]),
                    float(best_qu[iy, ix]),
                    float(best_qd[iy, ix]),
                )
            ttc = self._estimate_ttc(motion, fx, fy, win_scale, dt)
            avoid_x, avoid_y = self._compute_avoid_vector(
                motion, fx, fy, score, horiz_bar=used_horiz_bar
            )
        else:
            self._prev_rim_radius = None
            self._dr_dt_ema = 0.0
            fx = 0.5 * (gw - 1)
            fy = 0.5 * (gh - 1)
            ttc = float("inf")
            quads = (0.0, 0.0, 0.0, 0.0)
            avoid_x, avoid_y = 0.0, 0.0

        triggered = bool(score >= self.threshold)
        trigger = GiantFiberTrigger(
            triggered=triggered,
            looming_score=score,
            est_ttc_s=ttc,
            focus_xy=(fx, fy),
            quadrants=quads,
            pan_inhibition=float(pan_inhib),
            reflex_dispatched=False,
            avoid_x=avoid_x,
            avoid_y=avoid_y,
            bar_score=bar_score,
        )

        if triggered and self._hooks:
            for hook in self._hooks:
                hook(trigger)
            trigger.reflex_dispatched = True

        return trigger

    def __call__(self, motion: MotionOutput, dt: float = 1.0 / 60.0) -> GiantFiberTrigger:
        return self.step(motion, dt=dt)


class NeckSightPipeline:
    """End-to-end Retina -> Lamina L1/L2 -> T4/T5 EMD -> LPLC2/LC4 Giant Fiber + DNa02 pipeline."""

    def __init__(
        self,
        grid_size: Union[int, Tuple[int, int]] = 64,
        *,
        threshold: float = 0.045,
        enable_lc4_bar: bool = True,
        lc4_weight: float = 0.42,
        t5_off_gain: float = 1.35,
        nltp_client: Optional[Any] = None,
        nltp_msg: Union[str, bytes] = "LOOM_ESTOP",
    ) -> None:
        self.retina = RetinaLaminaPreproc(grid_size=grid_size)
        self.emd = T4T5MotionEngine(grid_size=grid_size, t5_off_gain=t5_off_gain)
        self.looming = LPLC2LoomingReflex(
            grid_size=grid_size,
            threshold=threshold,
            enable_lc4_bar=enable_lc4_bar,
            lc4_weight=lc4_weight,
        )
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

    def derotate(
        self,
        ego_shift_px: Optional[Sequence[float]] = None,
        *,
        ego_roll_rad: float = 0.0,
    ) -> None:
        """Apply this frame's camera rotation ``(dx, dy[, dtheta_rad])`` to retina and T4/T5 state."""
        dx = 0.0
        dy = 0.0
        dtheta = float(ego_roll_rad)
        if ego_shift_px is not None:
            dx = float(ego_shift_px[0])
            dy = float(ego_shift_px[1])
            if len(ego_shift_px) >= 3:
                dtheta += float(ego_shift_px[2])
        if dx == 0.0 and dy == 0.0 and abs(dtheta) <= 1e-9:
            return
        self.retina.shift_state(dx, dy, dtheta)
        self.emd.shift_state(dx, dy, dtheta)

    def step(
        self,
        frame: np.ndarray,
        dt: float = 1.0 / 60.0,
        ego_shift_px: Optional[Sequence[float]] = None,
        *,
        ego_roll_rad: float = 0.0,
        roll_rate_rad_s: float = 0.0,
    ) -> GiantFiberTrigger:
        """Process one camera frame and return the Giant Fiber + DNa02 trigger result.

        ``ego_shift_px`` accepts either ``(dx, dy)`` from :func:`necksight.gyro_to_shift_px` or
        ``(dx, dy, dtheta_rad)`` from :func:`necksight.imu_to_ego_motion`. Optional in-plane
        roll can also be passed via ``ego_roll_rad`` or ``roll_rate_rad_s``.
        """
        total_roll = float(ego_roll_rad) + float(roll_rate_rad_s) * float(dt)
        self.derotate(ego_shift_px, ego_roll_rad=total_roll)
        lam = self.retina.step(frame, dt=dt)
        mot = self.emd.step(lam)
        return self.looming.step(mot, dt=dt)

    def step_full(
        self,
        frame: np.ndarray,
        dt: float = 1.0 / 60.0,
        ego_shift_px: Optional[Sequence[float]] = None,
        *,
        ego_roll_rad: float = 0.0,
        roll_rate_rad_s: float = 0.0,
    ) -> Tuple[Any, MotionOutput, GiantFiberTrigger]:
        """Process one frame and return ``(lamina_out, motion_out, trigger_out)``."""
        total_roll = float(ego_roll_rad) + float(roll_rate_rad_s) * float(dt)
        self.derotate(ego_shift_px, ego_roll_rad=total_roll)
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
