"""Exact 3-DOF Hexapod Leg Inverse & Forward Kinematics (`HexapodLegIK`).

Maps body-frame 3D foot positions `(x_mm, y_mm, z_mm)` for all 6 thoracic legs
(`L1, R1, L2, R2, L3, R3`, `18` servos total) to exact `(coxa, femur, tibia)` joint
angles (`cdeg` & `deg`) and `1000..2000 us` hobby/robotic servo PWM pulse widths.

Integrates directly with `SegmentalCPGCord`:
  - Grounded stance feet track substrate contact (`z = 0 mm`).
  - Swing feet follow sinusoidal clearance (`z = swing_height_mm * sin(pi * u_sw)`).
  - Damaged / jammed limbs (`limb_health < fault_threshold`) are automatically retracted
    and parked safely lifted (`z = +45 mm` above ground) during 5-leg pentapod walking.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING
import numpy as np

if TYPE_CHECKING:
    from .cpg import CPGState

# Default thoracic coxa hip mounts [mm] in FLU body frame (+x forward, +y left, +z up)
DEFAULT_HIP_XY_MM = np.array(
    [
        [+120.0, +55.0],  # 0: L1 (prothoracic left)
        [+120.0, -55.0],  # 1: R1 (prothoracic right)
        [+0.0, +65.0],    # 2: L2 (mesothoracic left)
        [+0.0, -65.0],    # 3: R2 (mesothoracic right)
        [-120.0, +55.0],  # 4: L3 (metathoracic left)
        [-120.0, -55.0],  # 5: R3 (metathoracic right)
    ],
    dtype=np.float64,
)

# Outward coxa mount azimuth angles [deg] in body XY plane
DEFAULT_MOUNT_YAW_DEG = np.array(
    [+45.0, -45.0, +90.0, -90.0, +135.0, -135.0],
    dtype=np.float64,
)

# Retracted neutral XY foothold [mm] used when parking a damaged/amputated leg at z = +45 mm
PARKED_FOOTHOLD_XY_MM = np.array(
    [
        [+190.0, +120.0],
        [+190.0, -120.0],
        [+0.0, +150.0],
        [+0.0, -150.0],
        [-190.0, +120.0],
        [-190.0, -120.0],
    ],
    dtype=np.float64,
)


@dataclass
class LegIKResult:
    """18-DOF hexapod inverse kinematics solution (`6 legs x 3 joints`)."""

    coxa_cdeg: np.ndarray
    femur_cdeg: np.ndarray
    tibia_cdeg: np.ndarray
    angles_deg: np.ndarray
    angles_cdeg: np.ndarray
    pwm_us: np.ndarray
    foot_xyz_mm: np.ndarray
    parked_mask: np.ndarray
    reachable_mask: np.ndarray


class HexapodLegIK:
    """Exact 3-DOF law-of-cosines inverse & forward kinematics for 6-legged robots.

    Provenance (`PROVENANCE = "engineering"`):
      Analytical 3-servo `(coxa, femur, tibia)` law-of-cosines inverse kinematics, hobby PWM
      `1000..2000 us` pulse mapping, and `+45 mm` damaged-limb clearance parking are
      non-biological robotic actuator engineering.
    """

    PROVENANCE: str = "engineering"

    def __init__(
        self,
        *,
        l_coxa_mm: float = 38.0,
        l_femur_mm: float = 95.0,
        l_tibia_mm: float = 135.0,
        body_z_mm: float = 85.0,
        swing_height_mm: float = 30.0,
        park_z_mm: float = 45.0,
        hip_xy_mm: np.ndarray | None = None,
        mount_yaw_deg: np.ndarray | None = None,
        pwm_center_us: int = 1500,
        pwm_half_span_us: int = 500,
        pwm_min_us: int = 1000,
        pwm_max_us: int = 2000,
    ) -> None:
        if l_coxa_mm <= 0.0 or l_femur_mm <= 0.0 or l_tibia_mm <= 0.0 or body_z_mm <= 0.0:
            raise ValueError("Limb segment lengths and body_z_mm must be positive")
        self.l_coxa_mm = float(l_coxa_mm)
        self.l_femur_mm = float(l_femur_mm)
        self.l_tibia_mm = float(l_tibia_mm)
        self.body_z_mm = float(body_z_mm)
        self.swing_height_mm = float(swing_height_mm)
        self.park_z_mm = float(park_z_mm)

        hips = DEFAULT_HIP_XY_MM.copy() if hip_xy_mm is None else np.asarray(hip_xy_mm, dtype=np.float64)
        mounts = (
            DEFAULT_MOUNT_YAW_DEG.copy()
            if mount_yaw_deg is None
            else np.asarray(mount_yaw_deg, dtype=np.float64)
        )
        if hips.shape != (6, 2) or mounts.shape != (6,):
            raise ValueError("hip_xy_mm must have shape (6, 2) and mount_yaw_deg shape (6,)")
        self.hip_xy_mm = hips.copy()
        self.mount_yaw_deg = mounts.copy()

        self.pwm_center_us = int(pwm_center_us)
        self.pwm_half_span_us = int(pwm_half_span_us)
        self.pwm_min_us = int(pwm_min_us)
        self.pwm_max_us = int(pwm_max_us)

    def angle_cdeg_to_pwm_us(self, angle_cdeg: int | float | np.ndarray) -> np.ndarray | int:
        """Map centi-degrees (`-9000..+9000` = `-90..+90 deg`) to servo PWM `1000..2000 us`."""
        if isinstance(angle_cdeg, np.ndarray):
            raw = np.rint(
                self.pwm_center_us + (angle_cdeg.astype(np.float64) * self.pwm_half_span_us) / 9000.0
            ).astype(np.int32)
            return np.clip(raw, self.pwm_min_us, self.pwm_max_us)
        val = int(round(self.pwm_center_us + (float(angle_cdeg) * self.pwm_half_span_us) / 9000.0))
        return max(self.pwm_min_us, min(self.pwm_max_us, val))

    def solve_leg(
        self,
        limb_idx: int,
        foot_x_mm: float,
        foot_y_mm: float,
        foot_z_mm: float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray, bool]:
        """Solve `(coxa, femur, tibia)` [deg], `pwm_us[3]`, and `reachable` for a single leg."""
        if not 0 <= int(limb_idx) < 6:
            raise ValueError(f"limb_idx must be in 0..5, got {limb_idx}")
        idx = int(limb_idx)

        dx = float(foot_x_mm) - float(self.hip_xy_mm[idx, 0])
        dy = float(foot_y_mm) - float(self.hip_xy_mm[idx, 1])
        dz = float(foot_z_mm) - self.body_z_mm

        az_rad = math.atan2(dy, dx)
        mount_rad = math.radians(float(self.mount_yaw_deg[idx]))
        coxa_rad = math.atan2(math.sin(az_rad - mount_rad), math.cos(az_rad - mount_rad))

        r_horiz = math.hypot(dx, dy)
        r_femur = r_horiz - self.l_coxa_mm
        reachable = True
        if r_femur < 1.0:
            r_femur = 1.0
            reachable = False

        d_raw = math.hypot(r_femur, dz)
        d_min = abs(self.l_femur_mm - self.l_tibia_mm) + 0.5
        d_max = (self.l_femur_mm + self.l_tibia_mm) - 0.5
        if d_raw < d_min or d_raw > d_max:
            reachable = False
        d = max(d_min, min(d_max, d_raw))

        lf = self.l_femur_mm
        lt = self.l_tibia_mm
        gamma = math.atan2(dz, r_femur)

        cos_b1 = (lf * lf + d * d - lt * lt) / (2.0 * lf * d)
        cos_b1 = max(-1.0, min(1.0, cos_b1))
        beta_1 = math.acos(cos_b1)
        femur_rad = gamma + beta_1

        cos_b2 = (lf * lf + lt * lt - d * d) / (2.0 * lf * lt)
        cos_b2 = max(-1.0, min(1.0, cos_b2))
        beta_2 = math.acos(cos_b2)
        tibia_rad = beta_2 - (0.5 * math.pi)

        angles_deg = np.array(
            [math.degrees(coxa_rad), math.degrees(femur_rad), math.degrees(tibia_rad)],
            dtype=np.float64,
        )
        cdeg = np.rint(angles_deg * 100.0).astype(np.int32)
        pwm = self.angle_cdeg_to_pwm_us(cdeg)
        assert isinstance(pwm, np.ndarray)
        return angles_deg, pwm, reachable

    def forward_leg(
        self,
        limb_idx: int,
        coxa_deg: float,
        femur_deg: float,
        tibia_deg: float,
    ) -> np.ndarray:
        """Exact 3D forward kinematics `[foot_x_mm, foot_y_mm, foot_z_mm]` in body FLU frame."""
        if not 0 <= int(limb_idx) < 6:
            raise ValueError(f"limb_idx must be in 0..5, got {limb_idx}")
        idx = int(limb_idx)

        az_rad = math.radians(float(self.mount_yaw_deg[idx]) + float(coxa_deg))
        fem_rad = math.radians(float(femur_deg))
        tib_world_rad = math.radians(float(femur_deg) - 90.0 + float(tibia_deg))

        r_horiz = (
            self.l_coxa_mm
            + self.l_femur_mm * math.cos(fem_rad)
            + self.l_tibia_mm * math.cos(tib_world_rad)
        )
        dz = self.l_femur_mm * math.sin(fem_rad) + self.l_tibia_mm * math.sin(tib_world_rad)

        fx = float(self.hip_xy_mm[idx, 0]) + r_horiz * math.cos(az_rad)
        fy = float(self.hip_xy_mm[idx, 1]) + r_horiz * math.sin(az_rad)
        fz = self.body_z_mm + dz
        return np.array([fx, fy, fz], dtype=np.float64)

    def forward_all(self, angles_deg: np.ndarray) -> np.ndarray:
        """Compute `[x_mm, y_mm, z_mm]` for all 6 legs from `(6, 3)` joint angles [deg]."""
        ang = np.asarray(angles_deg, dtype=np.float64)
        if ang.shape != (6, 3):
            raise ValueError(f"angles_deg must have shape (6, 3), got {ang.shape}")
        out = np.zeros((6, 3), dtype=np.float64)
        for i in range(6):
            out[i] = self.forward_leg(i, float(ang[i, 0]), float(ang[i, 1]), float(ang[i, 2]))
        return out

    def solve_all(
        self,
        foot_xyz_mm: np.ndarray,
        *,
        parked_mask: np.ndarray | None = None,
    ) -> LegIKResult:
        """Solve 18-DOF leg IK for `(6, 3)` body-frame foot targets `[x_mm, y_mm, z_mm]`."""
        xyz = np.asarray(foot_xyz_mm, dtype=np.float64)
        if xyz.shape != (6, 3):
            raise ValueError(f"foot_xyz_mm must have shape (6, 3), got {xyz.shape}")
        p_mask = (
            np.zeros(6, dtype=bool)
            if parked_mask is None
            else np.asarray(parked_mask, dtype=bool).copy()
        )
        if p_mask.shape != (6,):
            raise ValueError(f"parked_mask must have shape (6,), got {p_mask.shape}")

        angles_deg = np.zeros((6, 3), dtype=np.float64)
        pwm_us = np.zeros((6, 3), dtype=np.int32)
        reachable = np.zeros(6, dtype=bool)
        eff_xyz = xyz.copy()

        for i in range(6):
            if p_mask[i]:
                eff_xyz[i, 0] = PARKED_FOOTHOLD_XY_MM[i, 0]
                eff_xyz[i, 1] = PARKED_FOOTHOLD_XY_MM[i, 1]
                eff_xyz[i, 2] = self.park_z_mm
            ang_i, pwm_i, ok_i = self.solve_leg(
                i, float(eff_xyz[i, 0]), float(eff_xyz[i, 1]), float(eff_xyz[i, 2])
            )
            angles_deg[i] = ang_i
            pwm_us[i] = pwm_i
            reachable[i] = ok_i

        angles_cdeg = np.rint(angles_deg * 100.0).astype(np.int32)
        return LegIKResult(
            coxa_cdeg=angles_cdeg[:, 0].copy(),
            femur_cdeg=angles_cdeg[:, 1].copy(),
            tibia_cdeg=angles_cdeg[:, 2].copy(),
            angles_deg=angles_deg,
            angles_cdeg=angles_cdeg,
            pwm_us=pwm_us,
            foot_xyz_mm=eff_xyz,
            parked_mask=p_mask,
            reachable_mask=reachable,
        )

    def solve_cpg_state(
        self,
        state: "CPGState",
        *,
        fault_threshold: float = 0.35,
    ) -> LegIKResult:
        """Convert a `SegmentalCPGCord` `CPGState` into 18-servo angles & PWM pulse widths."""
        xyz = np.zeros((6, 3), dtype=np.float64)
        xyz[:, 0:2] = state.foot_positions_xy * 1000.0
        parked = state.limb_health < float(fault_threshold)

        for i in range(6):
            if parked[i]:
                xyz[i, 0] = PARKED_FOOTHOLD_XY_MM[i, 0]
                xyz[i, 1] = PARKED_FOOTHOLD_XY_MM[i, 1]
                xyz[i, 2] = self.park_z_mm
            elif bool(state.stance_mask[i]):
                xyz[i, 2] = 0.0
            else:
                xyz[i, 2] = self.swing_height_mm * math.sin(math.pi * float(state.swing_progress[i]))

        return self.solve_all(xyz, parked_mask=parked)
