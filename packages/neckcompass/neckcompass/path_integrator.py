"""Central Complex Fan-Shaped Body (CPU4 / hDelta) path integrator and BANC v888 PFL3/PFL2 steering.

Biophysical inspirations & empirical connectome grounding (BANC v888, 157,789 proofread neurons):
- Stone et al. (Current Biology, 2017): anatomically constrained Central Complex path
  integration and homebound steering in insect brain
- Lyu, Abbott & Maimon (Nature, 2022): 2D vector arithmetic via sinusoidal activity bumps
  in the *Drosophila* Fan-Shaped Body
- Mussells Pires et al. (Nature, 2024) & Westeinde et al. (Nature, 2024): 25 PFL3 bilateral
  lateralized steering neurons -> DNa02 (259 direct synapses) + 12 PFL2 anti-goal 180-deg
  turn-boost neurons in BANC v888.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any, Optional

import numpy as np

from .ring_attractor import RingCompass, _resolve_cx_weights, wrap_angle


class FanBodyNavigator:
    """N-column opponent CPU4 vector path integrator and BANC v888 PFL3/PFL2 -> DNa02 controller.

    When ``connectome_weights="banc_v888"`` (default for ``n_columns=16``) or a connectome dict
    is supplied, ``steering_cmd`` evaluates the 25 biological ``PFL3`` and 12 ``PFL2`` neurons
    from the live 16-wedge ``EPG`` bump ``r``, 40 ``Delta7`` bridge neurons, and Fan-Shaped Body
    home-vector column phases, projecting directly through ``W_pfl3_dna02`` (and ``W_pfl2_dna02``)
    onto bilateral ``DNa02`` descending steering neurons.

    Pass ``connectome_weights="v2_analytic"`` (or ``n_columns != 16``) for the idealized v2
    16-column sinusoidal PFL3/PFL2 controller.
    """

    PROVENANCE: str = "connectome"

    def __init__(
        self,
        n_columns: int = 16,
        compass: Optional[RingCompass] = None,
        *,
        connectome_weights: Optional[str | Mapping[str, Any]] = "banc_v888",
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

        cx = _resolve_cx_weights(connectome_weights, self.n_columns)
        self._cx: Optional[dict[str, Any]] = cx
        self.connectome_mode: str = (
            "v2_analytic"
            if cx is None
            else (connectome_weights if isinstance(connectome_weights, str) else "custom_dict")
        )
        if cx is not None:
            self._M_ed7: np.ndarray = np.asarray(cx["M_ed7"], dtype=np.float64)
            self._th_fb_pfl3: np.ndarray = np.asarray(cx["th_fb_pfl3_rad"], dtype=np.float64)
            self._th_fb_pfl2: np.ndarray = np.asarray(cx["th_fb_pfl2_rad"], dtype=np.float64)
            self._M_epg_pfl3: np.ndarray = np.asarray(cx["M_epg_pfl3"], dtype=np.float64)
            self._M_d7_pfl3: np.ndarray = np.asarray(cx["M_d7_pfl3"], dtype=np.float64)
            self._M_epg_pfl2: np.ndarray = np.asarray(cx["M_epg_pfl2"], dtype=np.float64)
            self._M_d7_pfl2: np.ndarray = np.asarray(cx["M_d7_pfl2"], dtype=np.float64)
            self._W_pfl3_dna02: np.ndarray = np.asarray(cx["W_pfl3_dna02"], dtype=np.float64)
            self._W_pfl2_dna02: Optional[np.ndarray] = (
                np.asarray(cx["W_pfl2_dna02"], dtype=np.float64)
                if "W_pfl2_dna02" in cx
                else None
            )
            if "pfl3_is_L" in cx:
                self._pfl3_is_L: np.ndarray = np.asarray(cx["pfl3_is_L"], dtype=bool)
            else:
                self._pfl3_is_L = self._W_pfl3_dna02[1] >= self._W_pfl3_dna02[0]

        self.thetas: np.ndarray = -math.pi + (
            2.0 * math.pi * np.arange(self.n_columns, dtype=np.float64)
        ) / self.n_columns
        self._cos_thetas: np.ndarray = np.cos(self.thetas)
        self._sin_thetas: np.ndarray = np.sin(self.thetas)

        self._cpu4_pos: np.ndarray = np.zeros(self.n_columns, dtype=np.float64)
        self._cpu4_neg: np.ndarray = np.zeros(self.n_columns, dtype=np.float64)
        self._last_heading_rad: float = (
            compass.heading_rad if (compass and compass.initialized) else 0.0
        )

        self.last_pfl3_left_rate: float = 0.0
        self.last_pfl3_right_rate: float = 0.0
        self.last_pfl2_rate: float = 0.0

    def reset(self) -> None:
        """Zero the CPU4 path-integrator memory (marks current location as new home origin)."""
        self._cpu4_pos.fill(0.0)
        self._cpu4_neg.fill(0.0)
        self.last_pfl3_left_rate = 0.0
        self.last_pfl3_right_rate = 0.0
        self.last_pfl2_rate = 0.0

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

    def _resolve_bump_16(self, current_heading_rad: Optional[float]) -> np.ndarray:
        if (
            current_heading_rad is None
            and self.compass is not None
            and self.compass.initialized
            and self.compass.n_wedges == 16
        ):
            r = self.compass.bump
            s = float(r.sum())
            if s > 1e-6:
                return r * (8.0 / s)
        psi = self._resolve_heading(current_heading_rad)
        r = np.maximum(0.0, np.cos(self.thetas - psi))
        return r * (8.0 / max(float(r.sum()), 1e-6))

    def update(
        self,
        speed_m_s: float,
        dt: float,
        heading_rad: Optional[float] = None,
    ) -> tuple[float, float]:
        """Integrate forward velocity ``speed_m_s`` for ``dt`` seconds along ``heading_rad``."""
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
            pos_drive = np.maximum(0.0, proj)
            neg_drive = np.maximum(0.0, -proj)
            if ds >= 0.0:
                self._cpu4_pos += ds * pos_drive
                self._cpu4_neg += ds * neg_drive
            else:
                self._cpu4_pos += (-ds) * neg_drive
                self._cpu4_neg += (-ds) * pos_drive

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
        """Update the attached ``RingCompass`` and integrate path in one step."""
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

    def step_with_compass_9dof(
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
        speed_m_s: float,
        dt: float,
        *,
        sun_heading_rad: Optional[float] = None,
        mag_confidence: float = 1.0,
        sun_confidence: float = 1.0,
        g_ref: Optional[float] = None,
    ) -> tuple[float, float, float]:
        """Update attached ``RingCompass`` via 9-DOF tilt compensation and integrate path in one step."""
        if self.compass is None:
            raise RuntimeError("FanBodyNavigator has no attached RingCompass; pass compass=... or call update()")
        heading = self.compass.update_9dof(
            gx,
            gy,
            gz,
            ax,
            ay,
            az,
            mx,
            my,
            mz,
            dt,
            sun_heading_rad=sun_heading_rad,
            mag_confidence=mag_confidence,
            sun_confidence=sun_confidence,
            g_ref=g_ref,
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
        """Compute ``(pfl3_left, pfl3_right, pfl2_boost)`` firing rates.

        In ``banc_v888`` / connectome mode, returns the firing rates of the biological
        Left ``PFL3`` population, Right ``PFL3`` population, and ``12 PFL2`` anti-goal neurons.
        In ``v2_analytic`` mode, returns 16-column sinusoidal PFL3/PFL2 profiles.
        """
        dist_m, home_bearing = self.home_vector()
        if self._cx is not None:
            if dist_m <= 1e-9:
                return (
                    np.zeros(int(np.sum(self._pfl3_is_L)), dtype=np.float64),
                    np.zeros(int(np.sum(~self._pfl3_is_L)), dtype=np.float64),
                    np.zeros(self._th_fb_pfl2.shape[0], dtype=np.float64),
                )
            r = self._resolve_bump_16(current_heading_rad)
            d7 = self._M_ed7 @ r
            r_norm = r / max(float(np.max(r)), 1e-6)
            d7_norm = d7 / max(float(np.max(d7)), 1e-6)
            goal_fb3 = np.cos(self._th_fb_pfl3 - home_bearing)
            goal_fb2 = np.cos(self._th_fb_pfl2 - home_bearing)
            pfl3_act = np.square(
                np.maximum(
                    0.0,
                    (self._M_epg_pfl3 @ r_norm) - 0.70 * (self._M_d7_pfl3 @ d7_norm) + 0.85 * goal_fb3,
                )
            )
            pfl2_act = np.square(
                np.maximum(
                    0.0,
                    (self._M_epg_pfl2 @ r_norm) - 0.70 * (self._M_d7_pfl2 @ d7_norm) + 0.85 * goal_fb2,
                )
            )
            return pfl3_act[self._pfl3_is_L], pfl3_act[~self._pfl3_is_L], pfl2_act

        psi = self._resolve_heading(current_heading_rad)
        if dist_m <= 1e-9:
            zeros = np.zeros(self.n_columns, dtype=np.float64)
            return zeros, zeros.copy(), zeros.copy()

        hdelta_home = -self.cpu4_columns / dist_m
        phi_shift = 3.0 * math.pi / 8.0
        epg_left = np.cos(self.thetas - (psi - phi_shift))
        epg_right = np.cos(self.thetas - (psi + phi_shift))
        epg_anti = np.cos(self.thetas - (psi + math.pi))

        pfl3_left = np.square(np.maximum(0.0, 0.5 * (hdelta_home + epg_left)))
        pfl3_right = np.square(np.maximum(0.0, 0.5 * (hdelta_home + epg_right)))
        pfl2_boost = np.square(np.maximum(0.0, 0.5 * (hdelta_home + epg_anti)))
        return pfl3_left, pfl3_right, pfl2_boost

    def steering_cmd(
        self,
        current_heading_rad: Optional[float] = None,
        max_turn_rate: float = 1.5,
    ) -> float:
        """Compute bounded PFL3/PFL2 -> DNa02 yaw-rate command in ``[-max_turn_rate, +max_turn_rate]``."""
        if max_turn_rate <= 0.0:
            raise ValueError("max_turn_rate must be > 0")

        dist_m, home_bearing = self.home_vector()
        if dist_m <= 1e-6:
            self.last_pfl3_left_rate = 0.0
            self.last_pfl3_right_rate = 0.0
            self.last_pfl2_rate = 0.0
            return 0.0

        arrival_taper = min(1.0, dist_m / self.arrival_radius_m)

        if self._cx is not None:
            r = self._resolve_bump_16(current_heading_rad)
            d7 = self._M_ed7 @ r
            r_norm = r / max(float(np.max(r)), 1e-6)
            d7_norm = d7 / max(float(np.max(d7)), 1e-6)
            goal_fb3 = np.cos(self._th_fb_pfl3 - home_bearing)
            goal_fb2 = np.cos(self._th_fb_pfl2 - home_bearing)

            pfl3_act = np.square(
                np.maximum(
                    0.0,
                    (self._M_epg_pfl3 @ r_norm) - 0.70 * (self._M_d7_pfl3 @ d7_norm) + 0.85 * goal_fb3,
                )
            )
            pfl2_act = np.square(
                np.maximum(
                    0.0,
                    (self._M_epg_pfl2 @ r_norm) - 0.70 * (self._M_d7_pfl2 @ d7_norm) + 0.85 * goal_fb2,
                )
            )

            mask_l = self._pfl3_is_L
            self.last_pfl3_left_rate = float(np.mean(pfl3_act[mask_l])) if np.any(mask_l) else 0.0
            self.last_pfl3_right_rate = float(np.mean(pfl3_act[~mask_l])) if np.any(~mask_l) else 0.0
            self.last_pfl2_rate = float(np.mean(pfl2_act))

            dna3 = self._W_pfl3_dna02 @ pfl3_act
            raw_asym = float(dna3[0] - dna3[1])
            asym = raw_asym * (1.0 + 0.45 * self.last_pfl2_rate)
            if self.last_pfl2_rate > 0.25 and abs(asym) < 0.35:
                turn_sign = 1.0 if asym >= 0.0 else -1.0
                asym = turn_sign * max(abs(asym), 0.42 * self.pfl2_boost_gain * self.last_pfl2_rate)

            eff_gain = self.steering_gain * (3.2 / 2.2)
            cmd = max_turn_rate * arrival_taper * math.tanh(eff_gain * asym)
            return float(np.clip(cmd, -max_turn_rate, max_turn_rate))

        # v2_analytic mode
        psi = self._resolve_heading(current_heading_rad)
        pfl3_left, pfl3_right, pfl2_boost = self.pfl3_columns(psi)
        self.last_pfl3_left_rate = float(np.mean(pfl3_left))
        self.last_pfl3_right_rate = float(np.mean(pfl3_right))
        self.last_pfl2_rate = float(np.mean(pfl2_boost))

        phi_shift = 3.0 * math.pi / 8.0
        norm_factor = 0.5 * self.n_columns * math.sin(phi_shift)
        raw_asym = float(np.sum(pfl3_right) - np.sum(pfl3_left)) / max(norm_factor, 1e-9)

        anti_drive = self.last_pfl2_rate
        if abs(raw_asym) > 1e-6:
            turn_sign = 1.0 if raw_asym > 0.0 else -1.0
        else:
            err = float(wrap_angle(home_bearing - psi))
            turn_sign = 1.0 if err >= 0.0 else -1.0

        pfl2_term = self.pfl2_boost_gain * 4.0 * (anti_drive * anti_drive)
        boosted = raw_asym + turn_sign * pfl2_term
        cmd = max_turn_rate * arrival_taper * math.tanh(self.steering_gain * boosted)
        return float(np.clip(cmd, -max_turn_rate, max_turn_rate))
