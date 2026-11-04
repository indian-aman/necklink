"""Central Complex Ellipsoid Body (E-PG) ring attractor and plastic R-neuron compass.

Biophysical inspirations:
- Seelig & Jayaraman (Nature, 2015): single-bump E-PG landmark + angular velocity dynamics
- Turner-Evans et al. (eLife, 2017) & Green et al. (Nature, 2017): P-EN angular velocity shifters
- Fisher et al. (Nature, 2019) & Kim et al. (Nature, 2019): Hebbian/anti-Hebbian R-neuron -> E-PG
  associative plasticity for warped visual/magnetic landmark scenes
- Lyu, Abbott & Maimon (Nature, 2022): phasor harmonic arithmetic across Central Complex columns
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

import numpy as np


def wrap_angle(angle_rad: float | np.ndarray) -> float | np.ndarray:
    """Wrap angle(s) in radians cleanly into (-pi, pi] with zero discontinuity."""
    wrapped = (np.asarray(angle_rad) + math.pi) % (2.0 * math.pi) - math.pi
    if np.ndim(angle_rad) == 0:
        # Map -pi to +pi consistently if desired, or keep standard [-pi, pi)
        val = float(wrapped)
        return math.pi if val == -math.pi else val
    return wrapped


def circular_error_rad(est_rad: float | np.ndarray, truth_rad: float | np.ndarray) -> float | np.ndarray:
    """Signed shortest circular angular difference (est - truth) in (-pi, pi]."""
    return wrap_angle(np.asarray(est_rad) - np.asarray(truth_rad))


@dataclass(frozen=True)
class CompassDiagnostics:
    """Snapshot of internal neural state from a RingCompass update step."""

    heading_rad: float
    bump_amplitude: float
    estimated_gyro_bias_rad_s: float
    mag_gate: float
    sun_gate: float


class RingCompass:
    """Topological N-wedge E-PG ring attractor compass with P-EN angular-velocity shifters.

    Maintains a localized activity bump across ``n_wedges`` (default 16, matching the
    16 Ellipsoid Body wedges of *Drosophila*; also supports ``n_wedges=32`` or any
    even ``N >= 8``). Recurrent E-PG excitation and global Delta7 inhibition sustain
    a single constant-amplitude bump, while asymmetric Left/Right P-EN projections
    rotate the bump proportionally to ``omega_rad_s * dt``.

    External cues (``mag_heading_rad``, ``sun_heading_rad``) excite localized von-Mises
    Ring-Neuron (R2 / R4d) receptive fields gated by cooperative dendritic coincidence
    with the active E-PG bump. Cues within the attractor basin (~30 deg) smoothly pull
    the bump and adapt a slow homeostatic P-EN bias trim; transient 60-160 deg
    ferromagnetic ship-hull or motor-cable spikes fall onto Delta7-inhibited wedges
    and are naturally rejected without yanking the heading or corrupting bias state.
    """

    def __init__(
        self,
        n_wedges: int = 16,
        initial_heading_rad: Optional[float] = None,
        *,
        cue_kappa: float = 4.0,
        basin_sigma: float = 0.14,
        mag_gain: float = 1.6,
        sun_gain: float = 3.2,
        bias_adapt_rate: float = 0.55,
        max_bias_rad_s: float = 0.15,
        inhibition_threshold: float = 0.0,
        reanchor_seconds: Optional[float] = 15.0,
    ) -> None:
        if n_wedges < 8 or (n_wedges % 2) != 0:
            raise ValueError(f"n_wedges must be an even integer >= 8, got {n_wedges}")
        if cue_kappa <= 0.0:
            raise ValueError("cue_kappa must be positive")
        if basin_sigma <= 0.0:
            raise ValueError("basin_sigma must be positive")

        self.n_wedges: int = int(n_wedges)
        self.cue_kappa: float = float(cue_kappa)
        self.basin_sigma: float = float(basin_sigma)
        self.mag_gain: float = float(mag_gain)
        self.sun_gain: float = float(sun_gain)
        self.bias_adapt_rate: float = float(bias_adapt_rate)
        self.max_bias_rad_s: float = float(max_bias_rad_s)
        self.inhibition_threshold: float = float(inhibition_threshold)
        self.reanchor_seconds: Optional[float] = (
            None if reanchor_seconds is None else float(reanchor_seconds)
        )

        # Preferred wedge angles uniformly tiling [-pi, pi)
        self.thetas: np.ndarray = -math.pi + (2.0 * math.pi * np.arange(self.n_wedges, dtype=np.float64)) / self.n_wedges
        self._cos_thetas: np.ndarray = np.cos(self.thetas)
        self._sin_thetas: np.ndarray = np.sin(self.thetas)

        self._rates: np.ndarray = np.zeros(self.n_wedges, dtype=np.float64)
        self._initialized: bool = False
        self._pen_bias_rad_s: float = 0.0

        # Track sustained kinematically coherent off-basin cue (for genuine cold re-orientation)
        self._prev_mag_rad: Optional[float] = None
        self._coherent_offbasin_time: float = 0.0
        self._last_mag_gate: float = 0.0
        self._last_sun_gate: float = 0.0

        if initial_heading_rad is not None:
            self.reset(initial_heading_rad)

    def _shape_bump_from_angle(self, angle_rad: float) -> np.ndarray:
        """Construct the canonical Delta7-inhibited E-PG bump centered at ``angle_rad``."""
        raw = np.maximum(0.0, np.cos(self.thetas - angle_rad) - self.inhibition_threshold)
        peak = float(np.max(raw))
        return raw / peak if peak > 1e-12 else raw

    def reset(self, heading_rad: float = 0.0, *, reset_bias: bool = True) -> None:
        """Seed or reset the E-PG compass bump to ``heading_rad``."""
        wrapped = float(wrap_angle(heading_rad))
        self._rates = self._shape_bump_from_angle(wrapped)
        self._initialized = True
        if reset_bias:
            self._pen_bias_rad_s = 0.0
        self._prev_mag_rad = None
        self._coherent_offbasin_time = 0.0

    @property
    def initialized(self) -> bool:
        """Whether the E-PG ring bump has been seeded."""
        return self._initialized

    @property
    def wedges(self) -> np.ndarray:
        """Copy of current N-wedge non-negative E-PG firing rates."""
        return self._rates.copy()

    @property
    def heading_rad(self) -> float:
        """Decoded circular population-vector heading in (-pi, pi]."""
        if not self._initialized:
            return 0.0
        c1 = float(np.dot(self._rates, self._cos_thetas))
        s1 = float(np.dot(self._rates, self._sin_thetas))
        if abs(c1) < 1e-15 and abs(s1) < 1e-15:
            return 0.0
        return float(wrap_angle(math.atan2(s1, c1)))

    @property
    def heading_deg(self) -> float:
        """Decoded heading in degrees within (-180, 180]."""
        return math.degrees(self.heading_rad)

    @property
    def bump_amplitude(self) -> float:
        """First-harmonic phasor coherence of the E-PG activity bump in [0, 1]."""
        if not self._initialized:
            return 0.0
        total = float(np.sum(self._rates))
        if total <= 1e-12:
            return 0.0
        c1 = float(np.dot(self._rates, self._cos_thetas))
        s1 = float(np.dot(self._rates, self._sin_thetas))
        return float(math.hypot(c1, s1) / total)

    @property
    def estimated_gyro_bias_rad_s(self) -> float:
        """Homeostatic P-EN angular-velocity bias estimate in rad/s."""
        return self._pen_bias_rad_s

    def diagnostics(self) -> CompassDiagnostics:
        """Return a structured diagnostic snapshot of the latest compass state."""
        return CompassDiagnostics(
            heading_rad=self.heading_rad,
            bump_amplitude=self.bump_amplitude,
            estimated_gyro_bias_rad_s=self._pen_bias_rad_s,
            mag_gate=self._last_mag_gate,
            sun_gate=self._last_sun_gate,
        )

    def _von_mises_profile(self, cue_angle_rad: float) -> np.ndarray:
        """Localized R-neuron afferent excitation profile normalized to match E-PG 1st harmonic."""
        # Raised cosine (degree <= 3) has zero harmonic aliasing on N >= 8 rings while staying localized
        prof = np.power(0.5 * (1.0 + np.cos(self.thetas - cue_angle_rad)), 3)
        # Match 1st-harmonic norm to half-wave rectified E-PG bump (which has 1st harmonic = N/4)
        c1 = abs(float(np.dot(prof, np.cos(self.thetas - cue_angle_rad))))
        target_c1 = 0.25 * self.n_wedges
        return prof * (target_c1 / max(c1, 1e-12))

    def _basin_coincidence_gate(self, cue_angle_rad: float, pred_heading_rad: float) -> float:
        """Cooperative dendritic coincidence gate between R-neuron input and E-PG bump."""
        dist = 1.0 - math.cos(cue_angle_rad - pred_heading_rad)
        ratio = dist / self.basin_sigma
        return math.exp(-(ratio * ratio))

    def update(
        self,
        omega_rad_s: float,
        dt: float,
        mag_heading_rad: Optional[float] = None,
        sun_heading_rad: Optional[float] = None,
        mag_confidence: float = 1.0,
        sun_confidence: float = 1.0,
    ) -> float:
        """Advance the E-PG ring attractor by ``dt`` seconds and return ``heading_rad``.

        Parameters
        ----------
        omega_rad_s:
            Body yaw angular velocity in rad/s (positive = counter-clockwise).
        dt:
            Timestep in seconds (must be >= 0).
        mag_heading_rad:
            Optional raw/calibrated magnetic heading cue in radians.
        sun_heading_rad:
            Optional sky-polarization / sun / visual azimuth cue in radians.
        mag_confidence:
            Scalar confidence multiplier in [0, 1] for ``mag_heading_rad``.
        sun_confidence:
            Scalar confidence multiplier in [0, 1] for ``sun_heading_rad``.
        """
        if dt < 0.0:
            raise ValueError(f"dt must be non-negative, got {dt}")

        mag_conf = float(np.clip(mag_confidence, 0.0, 1.0))
        sun_conf = float(np.clip(sun_confidence, 0.0, 1.0))

        # Auto-seed bump on the very first step if not yet initialized
        if not self._initialized:
            if sun_heading_rad is not None and sun_conf > 0.0:
                self.reset(float(sun_heading_rad), reset_bias=False)
            elif mag_heading_rad is not None and mag_conf > 0.0:
                self.reset(float(mag_heading_rad), reset_bias=False)
            else:
                self.reset(float(omega_rad_s) * float(dt), reset_bias=False)
            if mag_heading_rad is not None:
                self._prev_mag_rad = float(wrap_angle(mag_heading_rad))
            return self.heading_rad

        # 1. Delta7 first-harmonic readout + P-EN asymmetric velocity shift
        c1 = (2.0 / self.n_wedges) * float(np.dot(self._rates, self._cos_thetas))
        s1 = (2.0 / self.n_wedges) * float(np.dot(self._rates, self._sin_thetas))
        norm1 = math.hypot(c1, s1)
        if norm1 <= 1e-12:
            c1, s1, norm1 = 1.0, 0.0, 1.0

        dphi = (float(omega_rad_s) - self._pen_bias_rad_s) * float(dt)
        cos_dphi = math.cos(dphi)
        sin_dphi = math.sin(dphi)

        c_rot = (c1 * cos_dphi - s1 * sin_dphi) / norm1
        s_rot = (c1 * sin_dphi + s1 * cos_dphi) / norm1
        pred_heading = float(wrap_angle(math.atan2(s_rot, c_rot)))

        # Reconstruct P-EN shifted recurrent E-PG excitation
        rec_drive = c_rot * self._cos_thetas + s_rot * self._sin_thetas
        u_ring = np.maximum(0.0, rec_drive - self.inhibition_threshold)
        u_peak = float(np.max(u_ring))
        if u_peak > 1e-12:
            u_ring = u_ring / u_peak

        # 2. Multi-cue R-neuron afferent injection with attractor-basin gating
        self._last_mag_gate = 0.0
        self._last_sun_gate = 0.0
        net_innovation = 0.0
        net_gate_weight = 0.0

        if sun_heading_rad is not None and sun_conf > 0.0:
            sun_ang = float(wrap_angle(sun_heading_rad))
            sun_gate = self._basin_coincidence_gate(sun_ang, pred_heading)
            self._last_sun_gate = sun_gate
            eff_sun = self.sun_gain * sun_conf * sun_gate * dt
            if eff_sun > 0.0:
                u_ring = u_ring + eff_sun * self._von_mises_profile(sun_ang)
                sun_err = float(wrap_angle(sun_ang - pred_heading))
                net_innovation += (self.sun_gain * sun_conf * sun_gate) * sun_err
                net_gate_weight += self.sun_gain * sun_conf * sun_gate

        if mag_heading_rad is not None and mag_conf > 0.0:
            mag_ang = float(wrap_angle(mag_heading_rad))
            mag_gate = self._basin_coincidence_gate(mag_ang, pred_heading)
            self._last_mag_gate = mag_gate

            # Check long-horizon kinematic coherence for genuine cold re-anchoring
            if (
                self.reanchor_seconds is not None
                and mag_gate < 0.05
                and sun_heading_rad is None
                and dt > 0.0
            ):
                if self._prev_mag_rad is not None:
                    d_mag = float(wrap_angle(mag_ang - self._prev_mag_rad))
                    rate_mismatch = abs(d_mag - (float(omega_rad_s) - self._pen_bias_rad_s) * dt) / max(dt, 1e-6)
                    if rate_mismatch < 0.25:  # < ~14 deg/s discrepancy -> kinematically coherent field
                        self._coherent_offbasin_time += dt
                    else:
                        self._coherent_offbasin_time = 0.0
                if self._coherent_offbasin_time >= self.reanchor_seconds:
                    self.reset(mag_ang, reset_bias=False)
                    self._prev_mag_rad = mag_ang
                    return self.heading_rad
            else:
                self._coherent_offbasin_time = 0.0

            self._prev_mag_rad = mag_ang

            eff_mag = self.mag_gain * mag_conf * mag_gate * dt
            if eff_mag > 0.0:
                u_ring = u_ring + eff_mag * self._von_mises_profile(mag_ang)
                mag_err = float(wrap_angle(mag_ang - pred_heading))
                net_innovation += (self.mag_gain * mag_conf * mag_gate) * mag_err
                net_gate_weight += self.mag_gain * mag_conf * mag_gate

        # 3. Winner-take-all Delta7 divisive/subtractive sharpening back onto canonical bump manifold
        c_new = float(np.dot(u_ring, self._cos_thetas))
        s_new = float(np.dot(u_ring, self._sin_thetas))
        new_heading = float(wrap_angle(math.atan2(s_new, c_new)))
        self._rates = self._shape_bump_from_angle(new_heading)

        # 4. Slow homeostatic P-EN bias adaptation (only when cues coherently overlap the bump!)
        # If omega_meas = omega_true + bias (bias > 0), pred_heading drifts positive, so
        # cue_err = (cue - pred_heading) < 0; thus bias estimate increases with (-cue_err).
        if dt > 0.0 and net_gate_weight > 1e-6 and self.bias_adapt_rate > 0.0:
            weighted_err = net_innovation / max(self.mag_gain, self.sun_gain, 1.0)
            self._pen_bias_rad_s -= self.bias_adapt_rate * weighted_err * dt
            self._pen_bias_rad_s = float(
                np.clip(self._pen_bias_rad_s, -self.max_bias_rad_s, self.max_bias_rad_s)
            )

        return self.heading_rad


class PlasticLandmarkRing:
    """Hebbian / anti-Hebbian R-neuron -> E-PG plastic ring mapping for warped magnetic scenes.

    Models associative synaptic plasticity between ``n_ring_neurons`` circularly tuned
    sensory Ring Neurons (R2/R4d) and the ``n_wedges`` Ellipsoid Body E-PG compass wedges
    (Fisher et al., *Nature* 2019; Kim et al., *Nature* 2019).

    Around steel docks, harbors, or indoor rooms where soft-iron and hard-iron effects
    nonlinearly warp magnetic azimuth (e.g. ``+25 deg * sin(2*psi)``), the plastic
    ``W_r_to_epg`` weight matrix learns the inverse distortion online from either:
    1. A clean intermittent reference cue (``reference_heading_rad``, e.g. sun/sky/visual), or
    2. Self-supervised rotational consistency with IMU yaw rate ``omega_rad_s`` across turns.
    """

    def __init__(
        self,
        n_wedges: int = 16,
        n_ring_neurons: int = 32,
        *,
        learning_rate: float = 4.0,
        r_kappa: float = 56.0,
        prior_strength: float = 0.25,
        compass: Optional[RingCompass] = None,
    ) -> None:
        if n_ring_neurons < 8:
            raise ValueError("n_ring_neurons must be >= 8")
        self.n_wedges: int = int(n_wedges)
        self.n_ring_neurons: int = int(n_ring_neurons)
        self.learning_rate: float = float(learning_rate)
        self.r_kappa: float = float(r_kappa)

        self.compass: RingCompass = (
            compass if compass is not None else RingCompass(n_wedges=self.n_wedges)
        )
        if self.compass.n_wedges != self.n_wedges:
            raise ValueError("compass.n_wedges must match PlasticLandmarkRing.n_wedges")

        self.r_thetas: np.ndarray = -math.pi + (
            2.0 * math.pi * np.arange(self.n_ring_neurons, dtype=np.float64)
        ) / self.n_ring_neurons
        self.epg_thetas: np.ndarray = self.compass.thetas.copy()
        self._cos_epg: np.ndarray = np.cos(self.epg_thetas)
        self._sin_epg: np.ndarray = np.sin(self.epg_thetas)
        self._prev_raw_ang: Optional[float] = None

        # Initialize R -> E-PG synaptic matrix with weak topographic identity prior
        diff = self.epg_thetas[:, None] - self.r_thetas[None, :]
        self.weights: np.ndarray = float(prior_strength) * np.cos(diff)

    def encode_r_neurons(self, raw_angle_rad: float) -> np.ndarray:
        """Compute L2-normalized R-neuron population vector for ``raw_angle_rad``."""
        act = np.exp(self.r_kappa * (np.cos(self.r_thetas - float(raw_angle_rad)) - 1.0))
        norm = float(np.linalg.norm(act))
        return act / norm if norm > 1e-12 else act

    def map_raw_heading(self, raw_mag_heading_rad: float) -> tuple[float, float]:
        """Map a warped raw magnetic angle through ``W_r_to_epg`` -> ``(corrected_rad, confidence)``."""
        r_act = self.encode_r_neurons(raw_mag_heading_rad)
        epg_drive = self.weights @ r_act
        c1 = (2.0 / self.n_wedges) * float(np.dot(epg_drive, self._cos_epg))
        s1 = (2.0 / self.n_wedges) * float(np.dot(epg_drive, self._sin_epg))
        amp = float(math.hypot(c1, s1))
        if amp <= 1e-9:
            return float(wrap_angle(raw_mag_heading_rad)), 0.0
        corrected = float(wrap_angle(math.atan2(s1, c1)))
        conf = float(np.clip(amp, 0.0, 1.0))
        return corrected, conf

    def update(
        self,
        omega_rad_s: float,
        dt: float,
        raw_mag_heading_rad: float,
        *,
        reference_heading_rad: Optional[float] = None,
        learn: bool = True,
        anchor_gain_scale: float = 0.25,
    ) -> float:
        """Advance compass, decode plastic landmark heading, and apply Hebbian/anti-Hebbian learning.

        When ``reference_heading_rad`` is None (self-supervised mode), ``anchor_gain_scale``
        keeps magnetic pull gentle so accurate short-term P-EN gyro integration sweeps smoothly
        across soft-iron warps while learning their inverse into ``self.weights``.
        """
        raw_ang = float(wrap_angle(raw_mag_heading_rad))
        if not self.compass.initialized:
            seed = float(reference_heading_rad) if reference_heading_rad is not None else raw_ang
            self.compass.reset(seed)
            heading = self.compass.heading_rad
        else:
            corr_mag, map_conf = self.map_raw_heading(raw_ang)
            eff_mag_conf = (
                map_conf if reference_heading_rad is None else 0.0
            ) * float(np.clip(anchor_gain_scale, 0.0, 1.0))

            heading = self.compass.update(
                omega_rad_s=omega_rad_s,
                dt=dt,
                mag_heading_rad=corr_mag if eff_mag_conf > 0.0 else None,
                sun_heading_rad=reference_heading_rad,
                mag_confidence=eff_mag_conf,
                sun_confidence=1.0,
            )

        if learn and dt > 0.0:
            r_act = self.encode_r_neurons(raw_ang)
            target_epg = np.cos(self.epg_thetas - heading)
            pred_epg = self.weights @ r_act
            # Movement-gated Hebbian / anti-Hebbian update (uniform exposure in R-neuron space)
            if self._prev_raw_ang is None:
                d_raw = abs(float(omega_rad_s)) * dt
            else:
                d_raw = abs(float(wrap_angle(raw_ang - self._prev_raw_ang)))
            self._prev_raw_ang = raw_ang
            step_rate = min(0.95, self.learning_rate * max(d_raw, 0.05 * dt))
            self.weights += step_rate * np.outer(target_epg - pred_epg, r_act)
            np.clip(self.weights, -8.0, 8.0, out=self.weights)

        return heading
