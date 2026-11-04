"""Central Complex Fan-Shaped Body (CPU4 / hDelta) path integrator and PFL3 steering.

Biophysical inspirations:
- Stone et al. (Current Biology, 2017): anatomically constrained Central Complex path
  integration and homebound steering in bee/insect brain
- Lyu, Abbott & Maimon (Nature, 2022): 2D vector arithmetic via sinusoidal activity bumps
  in the *Drosophila* Fan-Shaped Body
- Mussells Pires et al. (Nature, 2024) & Westeinde et al. (Nature, 2024): PFL3 (+/-67.5 deg
  phase-shifted left/right steering) + PFL2 (180 deg anti-goal turn boost)
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .ring_attractor import RingCompass, wrap_angle


class FanBodyNavigator:
    """N-column opponent CPU4 vector path integrator and PFL3/PFL2 homebound controller.

    Maintains ``n_columns`` (default 16) opponent Fan-Shaped Body memory columns that
    accumulate translational speed along the current E-PG compass heading during
    GPS-denied outbound navigation, continuously encoding the metric displacement vector
    from origin ``(0, 0)`` and generating neural PFL3/PFL2 yaw-rate commands to steer
    straight back home during complete GPS blackouts.
    """

    def __init__(
        self,
        n_columns: int = 16,
        compass: Optional[RingCompass] = None,
        *,
        leak_rate: float = 0.0,
        steering_gain: float = 2.2,
        pfl2_boost_gain: float = 2.5,
        arrival_radius_m: float = 1.5,
    ) -> None:
        if n_columns < 8 or (n_columns % 2) != 0:
            raise ValueError(f"n_columns must be an even integer >= 8, got {n_columns}")
        if leak_rate < 0.0:
            raise ValueError("leak_rate must be >= 0")
        if arrival_radius_m <= 0.0:
            raise ValueError("arrival_radius_m must be > 0")

        self.n_columns: int = int(n_columns)
        self.compass: Optional[RingCompass] = compass
        self.leak_rate: float = float(leak_rate)
        self.steering_gain: float = float(steering_gain)
        self.pfl2_boost_gain: float = float(pfl2_boost_gain)
        self.arrival_radius_m: float = float(arrival_radius_m)

        self.thetas: np.ndarray = -math.pi + (
            2.0 * math.pi * np.arange(self.n_columns, dtype=np.float64)
        ) / self.n_columns
        self._cos_thetas: np.ndarray = np.cos(self.thetas)
        self._sin_thetas: np.ndarray = np.sin(self.thetas)

        # Opponent CPU4 column memories (excitatory PFN drive vs 180-deg hDelta opponent)
        self._cpu4_pos: np.ndarray = np.zeros(self.n_columns, dtype=np.float64)
        self._cpu4_neg: np.ndarray = np.zeros(self.n_columns, dtype=np.float64)
        self._last_heading_rad: float = compass.heading_rad if (compass and compass.initialized) else 0.0

    def reset(self) -> None:
        """Zero the CPU4 path-integrator memory (marks current location as new home origin)."""
        self._cpu4_pos.fill(0.0)
        self._cpu4_neg.fill(0.0)

    @property
    def cpu4_columns(self) -> np.ndarray:
        """Net opponent CPU4 column activity ``(n_columns,)`` encoding outbound displacement."""
        return self._cpu4_pos - self._cpu4_neg

    @property
    def cpu4_rates(self) -> tuple[np.ndarray, np.ndarray]:
        """Return copies of ``(cpu4_pos, cpu4_neg)`` non-negative opponent column accumulators."""
        return self._cpu4_pos.copy(), self._cpu4_neg.copy()

    def _resolve_heading(self, heading_rad: Optional[float]) -> float:
        if heading_rad is not None:
            return float(wrap_angle(heading_rad))
        if self.compass is not None and self.compass.initialized:
            return self.compass.heading_rad
        return self._last_heading_rad

    def update(
        self,
        speed_m_s: float,
        dt: float,
        heading_rad: Optional[float] = None,
    ) -> tuple[float, float]:
        """Integrate forward velocity ``speed_m_s`` for ``dt`` seconds along ``heading_rad``.

        If ``heading_rad`` is omitted, reads the current heading from the attached
        ``RingCompass`` (if any). Returns ``(distance_m, home_bearing_rad)``.
        """
        if dt < 0.0:
            raise ValueError(f"dt must be non-negative, got {dt}")

        psi = self._resolve_heading(heading_rad)
        self._last_heading_rad = psi

        if dt > 0.0:
            if self.leak_rate > 0.0:
                decay = math.exp(-self.leak_rate * dt)
                self._cpu4_pos *= decay
                self._cpu4_neg *= decay

            ds = float(speed_m_s) * float(dt)
            proj = np.cos(self.thetas - psi)
            # Push-pull opponent accumulation across the 16 Fan-Shaped Body columns
            pos_drive = np.maximum(0.0, proj)
            neg_drive = np.maximum(0.0, -proj)
            if ds >= 0.0:
                self._cpu4_pos += ds * pos_drive
                self._cpu4_neg += ds * neg_drive
            else:
                self._cpu4_pos += (-ds) * neg_drive
                self._cpu4_neg += (-ds) * pos_drive

            # Subtract common-mode baseline between paired columns to prevent saturation on long runs
            common = np.minimum(self._cpu4_pos, self._cpu4_neg)
            self._cpu4_pos -= common
            self._cpu4_neg -= common

        return self.home_vector()

    def step_with_compass(
        self,
        omega_rad_s: float,
        speed_m_s: float,
        dt: float,
        mag_heading_rad: Optional[float] = None,
        sun_heading_rad: Optional[float] = None,
        mag_confidence: float = 1.0,
        sun_confidence: float = 1.0,
    ) -> tuple[float, float, float]:
        """Update the attached ``RingCompass`` and integrate path in one step.

        Returns ``(heading_rad, distance_m, home_bearing_rad)``.
        """
        if self.compass is None:
            raise RuntimeError("FanBodyNavigator has no attached RingCompass; pass compass=... or call update()")
        heading = self.compass.update(
            omega_rad_s=omega_rad_s,
            dt=dt,
            mag_heading_rad=mag_heading_rad,
            sun_heading_rad=sun_heading_rad,
            mag_confidence=mag_confidence,
            sun_confidence=sun_confidence,
        )
        dist_m, home_bearing = self.update(speed_m_s=speed_m_s, dt=dt, heading_rad=heading)
        return heading, dist_m, home_bearing

    @property
    def displacement_xy_m(self) -> tuple[float, float]:
        """Decoded outbound Cartesian displacement ``(x_m, y_m)`` from home origin."""
        net = self.cpu4_columns
        x_m = (2.0 / self.n_columns) * float(np.dot(net, self._cos_thetas))
        y_m = (2.0 / self.n_columns) * float(np.dot(net, self._sin_thetas))
        return x_m, y_m

    def home_vector(self) -> tuple[float, float]:
        """Compute ``(distance_m, home_bearing_rad)`` pointing from current position to origin."""
        x_m, y_m = self.displacement_xy_m
        dist_m = float(math.hypot(x_m, y_m))
        if dist_m <= 1e-9:
            return 0.0, 0.0
        home_bearing = float(wrap_angle(math.atan2(-y_m, -x_m)))
        return dist_m, home_bearing

    def pfl3_columns(
        self, current_heading_rad: Optional[float] = None
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Compute ``(pfl3_left, pfl3_right, pfl2_boost)`` column firing rates.

        Implements the *Drosophila* +/-67.5 deg (3*pi/8) PFL3 phase shift and 180 deg
        PFL2 anti-goal boost across the Fan-Shaped Body columns.
        """
        psi = self._resolve_heading(current_heading_rad)
        dist_m, _ = self.home_vector()
        if dist_m <= 1e-9:
            zeros = np.zeros(self.n_columns, dtype=np.float64)
            return zeros, zeros.copy(), zeros.copy()

        # hDelta 180-deg inverted CPU4 profile normalized to unit vector amplitude
        hdelta_home = -self.cpu4_columns / dist_m

        phi_shift = 3.0 * math.pi / 8.0  # 67.5 deg biological PFL3 shift
        epg_left = np.cos(self.thetas - (psi - phi_shift))
        epg_right = np.cos(self.thetas - (psi + phi_shift))
        epg_anti = np.cos(self.thetas - (psi + math.pi))

        pfl3_left = np.square(np.maximum(0.0, 0.5 * (hdelta_home + epg_left)))
        pfl3_right = np.square(np.maximum(0.0, 0.5 * (hdelta_home + epg_right)))
        pfl2_boost = np.square(np.maximum(0.0, 0.5 * (hdelta_home + epg_anti)))
        return pfl3_left, pfl3_right, pfl2_boost

    def steering_cmd(
        self,
        max_turn_rate: float = 1.0,
        current_heading_rad: Optional[float] = None,
    ) -> float:
        """Compute bounded PFL3/PFL2 homebound yaw-rate command in ``[-max_turn_rate, +max_turn_rate]``."""
        if max_turn_rate <= 0.0:
            raise ValueError("max_turn_rate must be > 0")

        dist_m, home_bearing = self.home_vector()
        if dist_m <= 1e-6:
            return 0.0

        psi = self._resolve_heading(current_heading_rad)
        pfl3_left, pfl3_right, pfl2_boost = self.pfl3_columns(psi)

        # Normalized PFL3 lateralized motor drive in Lateral Accessory Lobe (LAL)
        phi_shift = 3.0 * math.pi / 8.0
        norm_factor = 0.5 * self.n_columns * math.sin(phi_shift)
        raw_asym = float(np.sum(pfl3_right) - np.sum(pfl3_left)) / max(norm_factor, 1e-9)

        # PFL2 boosts turning when facing away from home (resolves 180-deg anti-goal saddle; max mean = 0.25)
        anti_drive = float(np.mean(pfl2_boost))
        if abs(raw_asym) < 1e-4 and anti_drive > 0.18:
            # Break exact 180-deg symmetry deterministically using shortest signed wrap
            err = float(wrap_angle(home_bearing - psi))
            raw_asym = math.copysign(0.25, err if err != 0.0 else 1.0)

        boosted = raw_asym * (1.0 + self.pfl2_boost_gain * anti_drive)
        arrival_taper = min(1.0, dist_m / self.arrival_radius_m)
        cmd = max_turn_rate * arrival_taper * math.tanh(self.steering_gain * boosted)
        return float(np.clip(cmd, -max_turn_rate, max_turn_rate))
