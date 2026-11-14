"""Segmental Ventral Nerve Cord (`VNC`) 6-limb phase-coupled CPG (`SegmentalCPGCord`).

Inspired by the *Drosophila* `BANC`/`MANC` thoracic premotor connectome:
  - Contralateral commissural anti-phase inhibition (`T1`, `T2`, `T3` left-right pairs)
  - Ipsilateral intersegmental phase-lag couplings (`L3 -> L2 -> L1`, `R3 -> R2 -> R1`)
  - Smooth descending-drive transitions across **Metachronal Wave**, **Tetrapod**, and **Tripod**
    gaits from scalar `drive_fwd` (`DNp09` / `MDN`), `turn_yaw` (`DNa02`), and `strafe_lat`
  - `Giant Fiber` instant `freeze_reflex` stance lock
  - Self-healing proprioceptive limb-loss adaptation: when 1 leg (e.g., `L2`) jams or loses
    campaniform load feedback, presynaptic shunting decouples the damaged oscillator while
    surviving limbs reorganize into a 5-phase ring (`Delta_phi = 2*pi/5`, duty `0.82`) with
    ipsilateral postural gap-closure so the robot's CoM remains inside a `>=3`-leg static
    support polygon throughout the stride cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np

from .cervical import DescendingFrame, GaitMode, decode_descending_4b
from .ik import HexapodLegIK, LegIKResult

LIMB_NAMES: tuple[str, ...] = ("L1", "R1", "L2", "R2", "L3", "R3")

# Nominal body-frame neutral footholds (x forward [m], y port/left [m]) relative to CoM (0, 0)
NOMINAL_FOOTHOLDS_XY = np.array(
    [
        [+0.20, +0.13],  # 0: L1 (prothoracic left)
        [+0.20, -0.13],  # 1: R1 (prothoracic right)
        [+0.00, +0.16],  # 2: L2 (mesothoracic left)
        [+0.00, -0.16],  # 3: R2 (mesothoracic right)
        [-0.20, +0.13],  # 4: L3 (metathoracic left)
        [-0.20, -0.13],  # 5: R3 (metathoracic right)
    ],
    dtype=np.float64,
)

# Canonical target phase offsets (radians, L1 anchored at 0.0)
_OFFSETS_TRIPOD = np.array(
    [0.0, math.pi, math.pi, 0.0, 0.0, math.pi],
    dtype=np.float64,
)
_OFFSETS_TETRAPOD = np.array(
    [
        0.0,
        2.0 * math.pi / 3.0,
        2.0 * math.pi / 3.0,
        4.0 * math.pi / 3.0,
        4.0 * math.pi / 3.0,
        0.0,
    ],
    dtype=np.float64,
)
_OFFSETS_WAVE = np.array(
    [
        0.0,
        math.pi,
        math.pi / 3.0,
        4.0 * math.pi / 3.0,
        2.0 * math.pi / 3.0,
        5.0 * math.pi / 3.0,
    ],
    dtype=np.float64,
)

_DUTY_TRIPOD = 0.54
_DUTY_TETRAPOD = 0.68
_DUTY_WAVE = 0.82
_DUTY_PENTAPOD = 0.82


def compute_support_margin(grounded_xy: np.ndarray) -> float:
    """Compute signed distance [m] from CoM `(0, 0)` to the 2D convex hull of `grounded_xy`.

    Returns `> 0` when `(0, 0)` lies strictly inside a non-degenerate `>=3`-foot support
    polygon (the static stability radius in meters), or `< 0` when `<3` feet are grounded
    or CoM falls outside / on the tipping edge of the polygon.
    """
    pts = np.asarray(grounded_xy, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 3 or pts.shape[1] != 2:
        return -1.0

    # Andrew's monotone chain 2D CCW convex hull
    order = np.lexsort((pts[:, 1], pts[:, 0]))
    sorted_pts = pts[order]

    def _cross(o: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
        return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))

    lower: list[np.ndarray] = []
    for p in sorted_pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 1e-12:
            lower.pop()
        lower.append(p)

    upper: list[np.ndarray] = []
    for p in reversed(sorted_pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 1e-12:
            upper.pop()
        upper.append(p)

    hull = lower[:-1] + upper[:-1]
    if len(hull) < 3:
        return -1.0

    min_margin = float("inf")
    m = len(hull)
    for idx in range(m):
        pa = hull[idx]
        pb = hull[(idx + 1) % m]
        edge_len = float(math.hypot(pb[0] - pa[0], pb[1] - pa[1]))
        if edge_len < 1e-9:
            return -1.0
        # Signed inward distance of origin (0, 0) from CCW edge pa -> pb
        signed_dist = float(pa[0] * pb[1] - pa[1] * pb[0]) / edge_len
        if signed_dist < min_margin:
            min_margin = signed_dist
    return min_margin


@dataclass
class CPGState:
    """Instantaneous 6-limb VNC motor coordination snapshot."""

    phases: np.ndarray
    stance_mask: np.ndarray
    swing_progress: np.ndarray
    joint_targets: np.ndarray
    foot_positions_xy: np.ndarray
    limb_health: np.ndarray
    duty_factor: float
    active_gait: str
    support_margin_m: float
    is_statically_stable: bool
    leg_ik: LegIKResult | None = None


class SegmentalCPGCord:
    """6-limb phase-coupled Kuramoto CPG with self-healing single-leg-loss adaptation."""

    def __init__(
        self,
        *,
        base_freq_hz: float = 0.8,
        max_freq_hz: float = 2.8,
        stride_length_m: float = 0.08,
        k_contra: float = 15.0,
        k_ipsi: float = 12.0,
        k_cross: float = 6.5,
        adapt_to_damage: bool = True,
        fault_threshold: float = 0.35,
        ik: HexapodLegIK | None = None,
    ) -> None:
        self.base_freq_hz = float(base_freq_hz)
        self.max_freq_hz = float(max_freq_hz)
        self.stride_length_m = float(stride_length_m)
        self.k_contra = float(k_contra)
        self.k_ipsi = float(k_ipsi)
        self.k_cross = float(k_cross)
        self.adapt_to_damage = bool(adapt_to_damage)
        self.fault_threshold = float(fault_threshold)
        self.ik = ik

        self.phases = _OFFSETS_TRIPOD.copy()
        self.limb_health = np.ones(6, dtype=np.float64)
        self._coupling_matrix = self._build_coupling_topology()

    def _build_coupling_topology(self) -> np.ndarray:
        k_mat = np.full((6, 6), self.k_cross, dtype=np.float64)
        np.fill_diagonal(k_mat, 0.0)
        # Contralateral commissural pairs: (L1,R1), (L2,R2), (L3,R3)
        for left_i, right_i in ((0, 1), (2, 3), (4, 5)):
            k_mat[left_i, right_i] = self.k_contra
            k_mat[right_i, left_i] = self.k_contra
        # Ipsilateral intersegmental neighbors: (L1,L2), (L2,L3), (R1,R2), (R2,R3)
        for a, b in ((0, 2), (2, 4), (1, 3), (3, 5)):
            k_mat[a, b] = self.k_ipsi
            k_mat[b, a] = self.k_ipsi
        # Ipsilateral T1-T3 long-propriospinal link (weaker than nearest-neighbor so Tripod->Wave has no saddle)
        for a, b in ((0, 4), (1, 5)):
            k_mat[a, b] = 0.55 * self.k_ipsi
            k_mat[b, a] = 0.55 * self.k_ipsi
        return k_mat

    @staticmethod
    def _resolve_limb_index(limb: int | str) -> int:
        if isinstance(limb, str):
            key = limb.strip().upper()
            if key not in LIMB_NAMES:
                raise ValueError(f"Unknown limb name {limb!r}; expected one of {LIMB_NAMES}")
            return LIMB_NAMES.index(key)
        idx = int(limb)
        if not 0 <= idx < 6:
            raise ValueError(f"Limb index must be in 0..5, got {limb!r}")
        return idx

    def set_limb_health(self, limb: int | str, health: float) -> None:
        """Set proprioceptive health `h_i in [0, 1]` for `limb` (`0` = jammed/amputated)."""
        idx = self._resolve_limb_index(limb)
        self.limb_health[idx] = float(max(0.0, min(1.0, health)))

    def inject_limb_fault(self, limb: int | str) -> None:
        """Convenience helper to mark `limb` jammed/amputated (`health = 0.0`)."""
        self.set_limb_health(limb, 0.0)

    def update_proprioceptive_load(
        self,
        measured_stance_load: np.ndarray,
        expected_stance_mask: np.ndarray,
        dt: float,
        tau_fault: float = 0.05,
        tau_recover: float = 0.80,
    ) -> np.ndarray:
        """Update limb health online from campaniform sensilla stance-load discrepancy."""
        loads = np.clip(np.asarray(measured_stance_load, dtype=np.float64), 0.0, 1.5)
        mask = np.asarray(expected_stance_mask, dtype=bool)
        for i in range(6):
            if mask[i]:
                target_h = min(1.0, loads[i])
                tau = tau_fault if target_h < self.limb_health[i] else tau_recover
                alpha = 1.0 - math.exp(-max(dt, 1e-4) / max(tau, 1e-4))
                self.limb_health[i] += alpha * (target_h - self.limb_health[i])
        self.limb_health = np.clip(self.limb_health, 0.0, 1.0)
        return self.limb_health.copy()

    def _compute_pentapod_offsets(self, damaged_idx: int) -> np.ndarray:
        """Construct 5-limb metachronal phase ring separating damaged-side survivors by 144 deg."""
        dmg_side = damaged_idx % 2  # 0 = Left, 1 = Right
        intact_side = 1 - dmg_side

        dmg_survivors = [i for i in (dmg_side, dmg_side + 2, dmg_side + 4) if i != damaged_idx]
        # Hind-to-front on intact side
        intact_limbs = [intact_side + 4, intact_side + 2, intact_side]

        ring_order = [
            dmg_survivors[0],
            intact_limbs[0],
            intact_limbs[1],
            dmg_survivors[1],
            intact_limbs[2],
        ]
        offsets = np.zeros(6, dtype=np.float64)
        step = 2.0 * math.pi / 5.0
        for rank, limb_i in enumerate(ring_order):
            offsets[limb_i] = rank * step
        offsets[damaged_idx] = 0.0
        return offsets

    def _select_target_gait(
        self,
        speed_cmd: float,
        gait_mode: int,
    ) -> tuple[np.ndarray, float, str]:
        healthy_mask = self.limb_health >= self.fault_threshold
        n_healthy = int(np.sum(healthy_mask))

        if self.adapt_to_damage and n_healthy == 5:
            damaged_idx = int(np.argmin(self.limb_health))
            return (
                self._compute_pentapod_offsets(damaged_idx),
                _DUTY_PENTAPOD,
                "adaptive_pentapod",
            )

        if self.adapt_to_damage and n_healthy < 5:
            return _OFFSETS_WAVE.copy(), 0.85, "wave"

        if gait_mode == int(GaitMode.TRIPOD):
            return _OFFSETS_TRIPOD.copy(), _DUTY_TRIPOD, "tripod"
        if gait_mode == int(GaitMode.TETRAPOD):
            return _OFFSETS_TETRAPOD.copy(), _DUTY_TETRAPOD, "tetrapod"
        if gait_mode == int(GaitMode.WAVE):
            return _OFFSETS_WAVE.copy(), _DUTY_WAVE, "wave"

        # AUTO mode: continuous speed-driven transition Wave -> Tetrapod -> Tripod
        if speed_cmd <= 0.30:
            return _OFFSETS_WAVE.copy(), _DUTY_WAVE, "wave"
        if speed_cmd >= 0.75:
            return _OFFSETS_TRIPOD.copy(), _DUTY_TRIPOD, "tripod"

        if speed_cmd < 0.525:
            u = (speed_cmd - 0.30) / 0.225
            w_wave, w_tetra, w_tri = 1.0 - u, u, 0.0
            label = "wave" if u < 0.35 else "tetrapod"
        else:
            u = (speed_cmd - 0.525) / 0.225
            w_wave, w_tetra, w_tri = 0.0, 1.0 - u, u
            label = "tetrapod" if u < 0.65 else "tripod"

        sin_sum = (
            w_wave * np.sin(_OFFSETS_WAVE)
            + w_tetra * np.sin(_OFFSETS_TETRAPOD)
            + w_tri * np.sin(_OFFSETS_TRIPOD)
        )
        cos_sum = (
            w_wave * np.cos(_OFFSETS_WAVE)
            + w_tetra * np.cos(_OFFSETS_TETRAPOD)
            + w_tri * np.cos(_OFFSETS_TRIPOD)
        )
        blended_offsets = np.mod(np.arctan2(sin_sum, cos_sum), 2.0 * math.pi)
        blended_duty = (
            w_wave * _DUTY_WAVE + w_tetra * _DUTY_TETRAPOD + w_tri * _DUTY_TRIPOD
        )
        return blended_offsets, float(blended_duty), label

    def _compute_postural_anchors(self) -> np.ndarray:
        """Apply VNC ipsilateral foothold gap-closure reflex when a limb loses health."""
        anchors = NOMINAL_FOOTHOLDS_XY.copy()
        if not self.adapt_to_damage:
            return anchors

        for side in (0, 1):  # 0 = Left (+y), 1 = Right (-y)
            y_sign = 1.0 if side == 0 else -1.0
            i_front, i_mid, i_hind = side, side + 2, side + 4
            loss_front = 1.0 - self.limb_health[i_front]
            loss_mid = 1.0 - self.limb_health[i_mid]
            loss_hind = 1.0 - self.limb_health[i_hind]

            if loss_mid > 0.2:
                # Front shifts caudally & hind shifts rostrally to close middle-leg gap
                anchors[i_front, 0] -= 0.09 * loss_mid
                anchors[i_hind, 0] += 0.09 * loss_mid
                anchors[i_front, 1] += 0.02 * y_sign * loss_mid
                anchors[i_hind, 1] += 0.02 * y_sign * loss_mid
            if loss_front > 0.2:
                anchors[i_mid, 0] += 0.10 * loss_front
                anchors[i_hind, 0] += 0.08 * loss_front
                anchors[i_mid, 1] += 0.015 * y_sign * loss_front
                anchors[i_hind, 1] += 0.02 * y_sign * loss_front
            if loss_hind > 0.2:
                anchors[i_mid, 0] -= 0.10 * loss_hind
                anchors[i_front, 0] -= 0.08 * loss_hind
                anchors[i_mid, 1] += 0.015 * y_sign * loss_hind
                anchors[i_front, 1] += 0.02 * y_sign * loss_hind
        return anchors

    def compute_leg_ik(
        self,
        state: CPGState,
        *,
        ik: HexapodLegIK | None = None,
    ) -> LegIKResult:
        """Compute exact 3-DOF `(coxa, femur, tibia)` angles + `1000..2000 us` PWM for `state`."""
        solver = self.ik if ik is None else ik
        if solver is None:
            solver = HexapodLegIK()
        return solver.solve_cpg_state(state, fault_threshold=self.fault_threshold)

    def step(
        self,
        command: DescendingFrame | bytes | bytearray | float = 0.0,
        dt: float = 0.01,
        *,
        turn_yaw: float = 0.0,
        strafe_lat: float = 0.0,
        gait_mode: int = int(GaitMode.AUTO),
        freeze_reflex: bool = False,
    ) -> CPGState:
        """Advance the 6-limb VNC oscillator network by `dt` seconds."""
        if isinstance(command, (bytes, bytearray, memoryview)):
            frame = decode_descending_4b(command)
        elif isinstance(command, DescendingFrame):
            frame = command
        else:
            frame = DescendingFrame(
                drive_fwd=float(command),
                turn_yaw=turn_yaw,
                strafe_lat=strafe_lat,
                gait_mode=gait_mode,
                freeze_reflex=freeze_reflex,
            )

        healthy_mask = self.limb_health >= self.fault_threshold
        anchors = self._compute_postural_anchors()

        if frame.freeze_reflex:
            stance_mask = healthy_mask.copy()
            swing_prog = np.zeros(6, dtype=np.float64)
            joints = np.zeros((6, 3), dtype=np.float64)
            joints[~healthy_mask, 1] = 1.0  # Damaged limbs parked clear of ground
            grounded_xy = anchors[stance_mask]
            margin = compute_support_margin(grounded_xy)
            st_freeze = CPGState(
                phases=self.phases.copy(),
                stance_mask=stance_mask,
                swing_progress=swing_prog,
                joint_targets=joints,
                foot_positions_xy=anchors,
                limb_health=self.limb_health.copy(),
                duty_factor=1.0,
                active_gait="freeze",
                support_margin_m=margin,
                is_statically_stable=(int(np.sum(stance_mask)) >= 3 and margin > 0.005),
            )
            if self.ik is not None:
                st_freeze.leg_ik = self.ik.solve_cpg_state(
                    st_freeze, fault_threshold=self.fault_threshold
                )
            return st_freeze

        speed_mag = min(
            1.0,
            max(
                abs(frame.drive_fwd),
                0.75 * abs(frame.strafe_lat),
                0.55 * abs(frame.turn_yaw),
            ),
        )
        target_offsets, duty, gait_name = self._select_target_gait(
            speed_mag, frame.gait_mode
        )

        # Intrinsic frequency signed by forward/reverse drive (`MDN` negative drive reverses wave)
        dir_sign = -1.0 if frame.drive_fwd < -1e-4 else 1.0
        freq_hz = (
            0.0
            if speed_mag < 1e-3
            else (self.base_freq_hz + (self.max_freq_hz - self.base_freq_hz) * speed_mag)
        )
        omega = 2.0 * math.pi * freq_hz * dir_sign

        # Sub-step Kuramoto integration for stiff coupling accuracy at arbitrary dt
        n_sub = max(1, int(math.ceil(max(dt, 0.0) / 0.005)))
        sub_dt = float(dt) / n_sub
        shunt_gate = np.where(healthy_mask, self.limb_health, 0.0)

        for _ in range(n_sub):
            # Pairwise differences: diff[i, j] = phi_j - phi_i - (psi_j - psi_i)
            phi_diff = self.phases[None, :] - self.phases[:, None]
            psi_diff = target_offsets[None, :] - target_offsets[:, None]
            coupling = np.sum(
                self._coupling_matrix * shunt_gate[None, :] * np.sin(phi_diff - psi_diff),
                axis=1,
            )
            dphi = omega + coupling
            self.phases = np.mod(self.phases + sub_dt * dphi, 2.0 * math.pi)

        # Compute stance vs swing, foot trajectories, and 3-joint motor targets
        stance_span = 2.0 * math.pi * duty
        swing_span = max(1e-6, 2.0 * math.pi - stance_span)

        stance_mask = np.zeros(6, dtype=bool)
        swing_prog = np.zeros(6, dtype=np.float64)
        joints = np.zeros((6, 3), dtype=np.float64)
        foot_xy = anchors.copy()

        # Differential bilateral step amplitude from DNa02 `turn_yaw`
        # Positive `turn_yaw` (CCW left turn) -> right legs longer stride, left legs shorter
        left_scale = max(-1.2, min(1.4, frame.drive_fwd - 0.65 * frame.turn_yaw))
        right_scale = max(-1.2, min(1.4, frame.drive_fwd + 0.65 * frame.turn_yaw))
        lat_shift = 0.035 * frame.strafe_lat

        for i in range(6):
            if not healthy_mask[i]:
                stance_mask[i] = False
                swing_prog[i] = 0.0
                joints[i] = np.array([0.0, 1.0, -0.5], dtype=np.float64)
                continue

            phi = float(self.phases[i])
            amp_x = 0.5 * self.stride_length_m * (left_scale if (i % 2 == 0) else right_scale)

            if phi < stance_span:
                # Stance phase: foot sweeps from anterior (+amp_x) to posterior (-amp_x)
                stance_mask[i] = True
                u_st = phi / max(stance_span, 1e-6)
                coxa_norm = 1.0 - 2.0 * u_st
                femur_lift = 0.0
                foot_xy[i, 0] = anchors[i, 0] + amp_x * coxa_norm
                foot_xy[i, 1] = anchors[i, 1] + lat_shift * (1.0 - 2.0 * u_st)
            else:
                # Swing phase: foot lifts and returns from posterior (-amp_x) to anterior (+amp_x)
                stance_mask[i] = False
                u_sw = (phi - stance_span) / swing_span
                swing_prog[i] = u_sw
                coxa_norm = -1.0 + 2.0 * u_sw
                femur_lift = math.sin(math.pi * u_sw)
                foot_xy[i, 0] = anchors[i, 0] + amp_x * coxa_norm
                foot_xy[i, 1] = anchors[i, 1] - lat_shift * (1.0 - 2.0 * u_sw)

            joints[i, 0] = coxa_norm * (left_scale if (i % 2 == 0) else right_scale)
            joints[i, 1] = femur_lift
            joints[i, 2] = 0.25 * coxa_norm + 0.15 * frame.strafe_lat

        grounded_xy = foot_xy[stance_mask]
        margin = compute_support_margin(grounded_xy)
        stable = bool(int(np.sum(stance_mask)) >= 3 and margin > 0.005)

        out_state = CPGState(
            phases=self.phases.copy(),
            stance_mask=stance_mask,
            swing_progress=swing_prog,
            joint_targets=joints,
            foot_positions_xy=foot_xy,
            limb_health=self.limb_health.copy(),
            duty_factor=duty,
            active_gait=gait_name,
            support_margin_m=margin,
            is_statically_stable=stable,
        )
        if self.ik is not None:
            out_state.leg_ik = self.ik.solve_cpg_state(
                out_state, fault_threshold=self.fault_threshold
            )
        return out_state
