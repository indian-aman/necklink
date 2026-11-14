"""9-Axis 3D Tilt-Compensated IMU + Magnetometer Front-End for NeckCompass.

Compensates steep geomagnetic dip angles (e.g. 60-68 deg across North America / Europe)
on pitching boats, banking drones, and heeled USVs while rejecting linear wave-slam
accelerations via a |norm(a) - g| <= 0.25g gravity gate before feeding horizontal yaw
rate and tilt-compensated magnetic azimuth into the Central Complex E-PG ring attractor.

Right-handed body coordinate convention (intrinsic Z-Y-X Euler angles yaw -> pitch -> roll):
  - Standard FLU / NWU (+X forward/North, +Y left/West, +Z up; level rest accelerometer
    specific force = (0, 0, +g_ref), positive gz = counter-clockwise yaw rate matching
    RingCompass). Algebraically identical in NED (+X North, +Y East, +Z down) when feeding
    the body-frame gravity vector (0, 0, +g_ref) at level rest.
  - Horizontal magnetic projection:
      mx_h = mx*cos(pitch) + my*sin(roll)*sin(pitch) + mz*cos(roll)*sin(pitch)
      my_h = my*cos(roll) - mz*sin(roll)
      mag_heading_rad = atan2(-my_h, mx_h)
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Optional

import numpy as np

from .ring_attractor import wrap_angle

if TYPE_CHECKING:
    from .ring_attractor import PlasticLandmarkRing, RingCompass


_MIN_COS_PITCH = math.cos(math.radians(85.0))


@dataclass(frozen=True)
class TiltDiagnostics:
    """Snapshot of 9-DOF 3D attitude & tilt-compensated magnetometer state."""

    roll_rad: float
    pitch_rad: float
    yaw_rate_rad_s: float
    mag_heading_rad: float
    accel_norm: float
    accel_gated: bool


def synthesize_9dof_frame(
    yaw_rad: float,
    pitch_rad: float,
    roll_rad: float,
    *,
    dip_deg: float = 60.0,
    g_ref: float = 9.80665,
    field_strength: float = 50.0,
    euler_rates_rad_s: tuple[float, float, float] = (0.0, 0.0, 0.0),
    linear_accel_body: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> tuple[float, float, float, float, float, float, float, float, float]:
    """Synthesize body-frame ``(gx, gy, gz, ax, ay, az, mx, my, mz)`` for given 3D attitude.

    Parameters
    ----------
    yaw_rad, pitch_rad, roll_rad:
        Intrinsic Z-Y-X Euler angles in radians.
    dip_deg:
        Geomagnetic inclination (dip) angle in degrees downward into the ground.
    g_ref:
        Nominal gravity magnitude (default ``9.80665`` m/s^2; pass ``1.0`` for g-units).
    field_strength:
        Total geomagnetic scalar intensity (e.g. ``50.0`` uT).
    euler_rates_rad_s:
        ``(roll_dot, pitch_dot, yaw_dot)`` Euler angle rates in rad/s mapped to body gyro.
    linear_accel_body:
        Extra non-gravitational body acceleration ``(lax, lay, laz)`` (e.g. wave slam).
    """
    cy, sy = math.cos(yaw_rad), math.sin(yaw_rad)
    cp, sp = math.cos(pitch_rad), math.sin(pitch_rad)
    cr, sr = math.cos(roll_rad), math.sin(roll_rad)

    dip_rad = math.radians(dip_deg)
    bh = field_strength * math.cos(dip_rad)
    bz = -field_strength * math.sin(dip_rad)

    # R_z(-yaw) * m_world
    hx = bh * cy
    hy = -bh * sy
    hz = bz

    # R_y(-pitch) * [hx, hy, hz]
    px = hx * cp - hz * sp
    py = hy
    pz = hx * sp + hz * cp

    # R_x(-roll) * [px, py, pz]
    mx = px
    my = py * cr + pz * sr
    mz = -py * sr + pz * cr

    # Body accelerometer specific force = R_x(-roll) * R_y(-pitch) * [0, 0, +g_ref] + linear_accel
    ax = -g_ref * sp + linear_accel_body[0]
    ay = g_ref * sr * cp + linear_accel_body[1]
    az = g_ref * cr * cp + linear_accel_body[2]

    # Exact body angular velocity [gx, gy, gz] from Euler rates (roll_dot, pitch_dot, yaw_dot)
    roll_dot, pitch_dot, yaw_dot = euler_rates_rad_s
    gx = roll_dot - yaw_dot * sp
    gy = pitch_dot * cr + yaw_dot * sr * cp
    gz = -pitch_dot * sr + yaw_dot * cr * cp
    return (gx, gy, gz, ax, ay, az, mx, my, mz)


class TiltCompensatedAHRS:
    """Complementary 3D gravity roll/pitch estimator and tilt-compensated magnetic front-end.

    Integrates 3-axis strapdown body gyro ``(gx, gy, gz)`` through exact 3D Z-Y-X Euler
    kinematics and fuses accelerometer gravity direction ``(ax, ay, az)`` ONLY when
    ``|‖a‖ - g_ref| <= accel_gate_tol * g_ref`` (default ``0.25 * g_ref``), preventing
    vessel wave slams, drone banking thrust spikes, or rover jolts from corrupting roll
    and pitch.
    """

    def __init__(
        self,
        *,
        tau_s: float = 0.5,
        g_ref: float = 9.80665,
        accel_gate_tol: float = 0.25,
        max_pitch_rad: float = math.radians(85.0),
    ) -> None:
        if tau_s <= 0.0:
            raise ValueError("tau_s must be positive")
        if g_ref <= 0.0:
            raise ValueError("g_ref must be positive")
        if not (0.0 < accel_gate_tol < 1.0):
            raise ValueError("accel_gate_tol must be in (0, 1)")
        if not (0.0 < max_pitch_rad < 0.5 * math.pi):
            raise ValueError("max_pitch_rad must be in (0, pi/2)")

        self.tau_s: float = float(tau_s)
        self.g_ref: float = float(g_ref)
        self.accel_gate_tol: float = float(accel_gate_tol)
        self.max_pitch_rad: float = float(max_pitch_rad)

        self._roll_rad: float = 0.0
        self._pitch_rad: float = 0.0
        self._initialized: bool = False
        self._accel_gated: bool = False
        self._last_accel_norm: float = 0.0
        self._last_yaw_rate_rad_s: float = 0.0
        self._last_mag_heading_rad: float = 0.0

    def reset(self, roll_rad: float = 0.0, pitch_rad: float = 0.0) -> None:
        """Reset or seed the 3D roll and pitch estimate."""
        self._roll_rad = float(wrap_angle(roll_rad))
        self._pitch_rad = float(np.clip(pitch_rad, -self.max_pitch_rad, self.max_pitch_rad))
        self._initialized = True
        self._accel_gated = False

    def reset_unseeded(self) -> None:
        """Clear state so the next valid 9-DOF sample seeds roll/pitch directly from gravity."""
        self._roll_rad = 0.0
        self._pitch_rad = 0.0
        self._initialized = False
        self._accel_gated = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def roll_rad(self) -> float:
        return self._roll_rad

    @property
    def pitch_rad(self) -> float:
        return self._pitch_rad

    @property
    def roll_deg(self) -> float:
        return math.degrees(self._roll_rad)

    @property
    def pitch_deg(self) -> float:
        return math.degrees(self._pitch_rad)

    @property
    def accel_gated(self) -> bool:
        """True if the most recent sample gated out accelerometer gravity due to linear accel."""
        return self._accel_gated

    @staticmethod
    def compensate_mag(
        roll_rad: float,
        pitch_rad: float,
        mx: float,
        my: float,
        mz: float,
    ) -> float:
        """Project body magnetometer ``(mx, my, mz)`` onto horizontal plane -> ``mag_heading_rad``."""
        cr, sr = math.cos(roll_rad), math.sin(roll_rad)
        cp, sp = math.cos(pitch_rad), math.sin(pitch_rad)
        mx_h = mx * cp + my * sr * sp + mz * cr * sp
        my_h = my * cr - mz * sr
        if abs(mx_h) < 1e-15 and abs(my_h) < 1e-15:
            return 0.0
        return float(wrap_angle(math.atan2(-my_h, mx_h)))

    @staticmethod
    def project_yaw_rate(
        roll_rad: float,
        pitch_rad: float,
        gy: float,
        gz: float,
    ) -> float:
        """Project body pitch/yaw gyro rates ``(gy, gz)`` onto horizontal world yaw rate ``dot(psi)``."""
        cp = math.cos(pitch_rad)
        cp_safe = max(abs(cp), _MIN_COS_PITCH)
        if cp < 0.0:
            cp_safe = -cp_safe
        return float((gy * math.sin(roll_rad) + gz * math.cos(roll_rad)) / cp_safe)

    def update_9dof(
        self,
        gx: float,
        gy: float,
        gz: float,
        ax: float,
        ay: float,
        az: float,
        mx: float,
        my: float,
        mz: float,
        dt: float,
    ) -> TiltDiagnostics:
        """Advance 3D roll/pitch filter by ``dt`` s and compute tilt-compensated yaw rate & mag heading."""
        if dt < 0.0:
            raise ValueError(f"dt must be non-negative, got {dt}")

        gx_f, gy_f, gz_f = float(gx), float(gy), float(gz)
        ax_f, ay_f, az_f = float(ax), float(ay), float(az)
        mx_f, my_f, mz_f = float(mx), float(my), float(mz)
        dt_f = float(dt)

        acc_norm = math.hypot(ax_f, ay_f, az_f)
        gated = (
            acc_norm <= 1e-12
            or abs(acc_norm - self.g_ref) > (self.accel_gate_tol * self.g_ref)
        )
        self._last_accel_norm = acc_norm
        self._accel_gated = gated

        if not self._initialized:
            if not gated:
                roll_acc = math.atan2(ay_f, az_f)
                pitch_acc = math.atan2(-ax_f, math.hypot(ay_f, az_f))
                self.reset(roll_acc, pitch_acc)
            else:
                self.reset(0.0, 0.0)
            self._accel_gated = gated
        elif dt_f > 0.0:
            cr, sr = math.cos(self._roll_rad), math.sin(self._roll_rad)
            cp = max(math.cos(self._pitch_rad), _MIN_COS_PITCH)
            tan_p = math.sin(self._pitch_rad) / cp

            q_sin_r_cos = gy_f * sr + gz_f * cr
            roll_dot = gx_f + q_sin_r_cos * tan_p
            pitch_dot = gy_f * cr - gz_f * sr

            roll_pred = float(wrap_angle(self._roll_rad + roll_dot * dt_f))
            pitch_pred = float(
                np.clip(self._pitch_rad + pitch_dot * dt_f, -self.max_pitch_rad, self.max_pitch_rad)
            )

            if not gated:
                roll_acc = math.atan2(ay_f, az_f)
                pitch_acc = float(
                    np.clip(
                        math.atan2(-ax_f, math.hypot(ay_f, az_f)),
                        -self.max_pitch_rad,
                        self.max_pitch_rad,
                    )
                )
                alpha = dt_f / (self.tau_s + dt_f)
                self._roll_rad = float(
                    wrap_angle(roll_pred + alpha * float(wrap_angle(roll_acc - roll_pred)))
                )
                self._pitch_rad = float(
                    np.clip(
                        pitch_pred + alpha * float(wrap_angle(pitch_acc - pitch_pred)),
                        -self.max_pitch_rad,
                        self.max_pitch_rad,
                    )
                )
            else:
                self._roll_rad = roll_pred
                self._pitch_rad = pitch_pred

        omega_yaw = self.project_yaw_rate(self._roll_rad, self._pitch_rad, gy_f, gz_f)
        mag_heading = self.compensate_mag(self._roll_rad, self._pitch_rad, mx_f, my_f, mz_f)
        self._last_yaw_rate_rad_s = omega_yaw
        self._last_mag_heading_rad = mag_heading

        return TiltDiagnostics(
            roll_rad=self._roll_rad,
            pitch_rad=self._pitch_rad,
            yaw_rate_rad_s=omega_yaw,
            mag_heading_rad=mag_heading,
            accel_norm=acc_norm,
            accel_gated=gated,
        )


class TiltCompensatedCompass:
    """Full 9-DOF Central Complex compass combining ``TiltCompensatedAHRS`` + ``RingCompass``."""

    def __init__(
        self,
        compass: Optional["RingCompass"] = None,
        ahrs: Optional[TiltCompensatedAHRS] = None,
        *,
        plastic_ring: Optional["PlasticLandmarkRing"] = None,
        n_wedges: int = 16,
        cue_noise_deg: float = 3.0,
        tau_s: float = 0.5,
        g_ref: float = 9.80665,
        accel_gate_tol: float = 0.25,
    ) -> None:
        from .ring_attractor import RingCompass

        self.plastic_ring: Optional["PlasticLandmarkRing"] = plastic_ring
        if plastic_ring is not None:
            self.compass: "RingCompass" = plastic_ring.compass
        elif compass is not None:
            self.compass = compass
        else:
            self.compass = RingCompass(n_wedges=n_wedges, cue_noise_deg=cue_noise_deg)

        self.ahrs: TiltCompensatedAHRS = (
            ahrs
            if ahrs is not None
            else TiltCompensatedAHRS(tau_s=tau_s, g_ref=g_ref, accel_gate_tol=accel_gate_tol)
        )
        self._last_tilt: Optional[TiltDiagnostics] = None

    @property
    def heading_rad(self) -> float:
        return self.compass.heading_rad

    @property
    def heading_deg(self) -> float:
        return self.compass.heading_deg

    @property
    def roll_rad(self) -> float:
        return self.ahrs.roll_rad

    @property
    def pitch_rad(self) -> float:
        return self.ahrs.pitch_rad

    @property
    def accel_gated(self) -> bool:
        return self.ahrs.accel_gated

    @property
    def last_tilt_diagnostics(self) -> Optional[TiltDiagnostics]:
        return self._last_tilt

    def reset(
        self,
        heading_rad: float = 0.0,
        *,
        roll_rad: Optional[float] = None,
        pitch_rad: Optional[float] = None,
        reset_bias: bool = True,
    ) -> None:
        self.compass.reset(heading_rad, reset_bias=reset_bias)
        if roll_rad is not None or pitch_rad is not None:
            self.ahrs.reset(roll_rad or 0.0, pitch_rad or 0.0)
        else:
            self.ahrs.reset_unseeded()

    def update_9dof(
        self,
        gx: float,
        gy: float,
        gz: float,
        ax: float,
        ay: float,
        az: float,
        mx: float,
        my: float,
        mz: float,
        dt: float,
        *,
        sun_heading_rad: Optional[float] = None,
        mag_confidence: float = 1.0,
        sun_confidence: float = 1.0,
        learn: bool = True,
    ) -> float:
        """Process a 9-axis IMU+Magnetometer frame and return stabilized ``heading_rad``."""
        diag = self.ahrs.update_9dof(gx, gy, gz, ax, ay, az, mx, my, mz, dt)
        self._last_tilt = diag
        if self.plastic_ring is not None:
            return self.plastic_ring.update(
                diag.yaw_rate_rad_s,
                dt,
                diag.mag_heading_rad,
                reference_heading_rad=sun_heading_rad,
                learn=learn,
            )
        return self.compass.update(
            diag.yaw_rate_rad_s,
            dt,
            mag_heading_rad=diag.mag_heading_rad,
            sun_heading_rad=sun_heading_rad,
            mag_confidence=mag_confidence,
            sun_confidence=sun_confidence,
        )
