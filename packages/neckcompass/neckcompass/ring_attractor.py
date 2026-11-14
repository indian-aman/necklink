"""Central Complex Ellipsoid Body (E-PG) ring attractor and plastic R-neuron compass.

Biophysical inspirations & empirical connectome grounding (BANC v888, 157,789 proofread neurons):
- EPG: 50 compass wedge neurons (45 EPG + 5 EPGt)
- PEN: 42 angular-velocity shifter neurons (24 PEN_b + 18 PEN_a; 7,998 PEN -> EPG synapses)
- Delta7: 40 glutamatergic inhibitory bridge neurons (6,998 EPG -> Delta7, 4,646 Delta7 -> EPG,
  6,066 Delta7 -> PFL3 synapses) enforcing sinusoidal anti-phase bump sharpening
- PFL3: 25 bilateral lateralized steering neurons (259 direct synapses onto DNa02)
- PFL2: 12 anti-goal 180-deg turn-boost neurons breaking the anti-home dead-zone
- Seelig & Jayaraman (Nature, 2015); Turner-Evans et al. (eLife, 2017); Fisher et al. (Nature, 2019);
  Kim et al. (Nature, 2019); Lyu, Abbott & Maimon (Nature, 2022); Westeinde et al. (Nature, 2024).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

if TYPE_CHECKING:
    from .tilt_ahrs import TiltCompensatedAHRS


BANC_CONNECTOME_COUNTS: dict[str, int] = {
    "EPG": 50,
    "PEN": 42,
    "Delta7": 40,
    "PFL3": 25,
    "PFL2": 12,
}

_BANC_DATA_PATH = Path(__file__).resolve().parent / "data" / "banc_cx_v3.npz"
_BANC_CX_CACHE: Optional[dict[str, np.ndarray]] = None


def load_banc_connectome() -> dict[str, np.ndarray]:
    """Load and cache the extracted BANC v888 Central Complex synaptic matrices."""
    global _BANC_CX_CACHE
    if _BANC_CX_CACHE is None:
        with np.load(_BANC_DATA_PATH, allow_pickle=False) as npz:
            _BANC_CX_CACHE = {k: np.array(npz[k], copy=True) for k in npz.files}
    return {k: v.copy() for k, v in _BANC_CX_CACHE.items()}


def _resolve_cx_weights(
    connectome_weights: Optional[str | Mapping[str, Any]],
    n_units: int,
) -> Optional[dict[str, Any]]:
    """Resolve ``connectome_weights`` into a dict of numpy arrays, or ``None`` for v2 analytic mode."""
    if n_units != 16 or connectome_weights is None or connectome_weights == "v2_analytic":
        return None
    if isinstance(connectome_weights, str):
        if connectome_weights == "banc_v888":
            if _BANC_CX_CACHE is None:
                load_banc_connectome()
            assert _BANC_CX_CACHE is not None
            return _BANC_CX_CACHE
        raise ValueError(
            f"Unsupported connectome_weights={connectome_weights!r}; "
            "expected 'banc_v888', 'v2_analytic', or a connectome weight dict"
        )
    if isinstance(connectome_weights, Mapping):
        return dict(connectome_weights)
    raise TypeError(
        f"connectome_weights must be 'banc_v888', 'v2_analytic', or a dict, got {type(connectome_weights).__name__}"
    )


def wrap_angle(angle_rad: float | np.ndarray) -> float | np.ndarray:
    """Wrap angle(s) in radians cleanly into (-pi, pi] with zero discontinuity."""
    wrapped = (np.asarray(angle_rad) + math.pi) % (2.0 * math.pi) - math.pi
    if np.ndim(angle_rad) == 0:
        val = float(wrapped)
        return math.pi if val == -math.pi else val
    return wrapped


def circular_error_rad(est_rad: float | np.ndarray, truth_rad: float | np.ndarray) -> float | np.ndarray:
    """Signed shortest circular angular difference (est - truth) in (-pi, pi]."""
    return wrap_angle(np.asarray(est_rad) - np.asarray(truth_rad))


_GATE_HALF_RATIO = math.sqrt(math.log(2.0))  # gate = 0.5 where (1 - cos d) = 0.8326 * basin_sigma
_REANCHOR_TAU_S = 1.0
_REANCHOR_MIN_COHERENCE = 0.8


def basin_sigma_for_noise(cue_noise_deg: float) -> float:
    """Return the ``basin_sigma`` whose gate half-width suits a cue with ``cue_noise_deg`` 1-sigma noise.

    The basin half-width (gate = 0.5) is ``4 deg + 1.0 * cue_noise_deg``: tight enough to
    shunt the leading and trailing edges of ferromagnetic spikes, wide enough to accept
    ordinary sensor noise plus a few degrees of gyro drift between cue updates.
    """
    if not (cue_noise_deg > 0.0):
        raise ValueError("cue_noise_deg must be positive")
    half_width_rad = math.radians(4.0 + float(cue_noise_deg))
    return (1.0 - math.cos(half_width_rad)) / _GATE_HALF_RATIO


@dataclass(frozen=True)
class CompassDiagnostics:
    """Snapshot of internal neural state from a RingCompass update step."""

    heading_rad: float
    bump_amplitude: float
    estimated_gyro_bias_rad_s: float
    mag_gate: float
    sun_gate: float


class RingCompass:
    """Topological N-wedge E-PG ring attractor compass driven by the BANC v888 connectome.

    When ``connectome_weights="banc_v888"`` (the v3.0 default for ``n_wedges=16``) or a
    custom/shuffled connectome dict is passed, the 16-wedge E-PG bump ``r`` evolves directly
    through the empirical *Drosophila* Central Complex synaptic matrices (``M_exc`` recurrent
    excitation, ``M_ed7`` / ``M_d7e`` 40-neuron Delta7 bridge inhibition, and ``M_pa_diff``
    bilateral PEN_a angular-velocity shifters with fixed biophysical gain ``10.2``).

    Pass ``connectome_weights="v2_analytic"`` (or ``n_wedges != 16``, e.g. ``32``) to run the
    idealized v2 first-harmonic phasor ring attractor.
    """

    PROVENANCE: str = "connectome"

    def __init__(
        self,
        n_wedges: int = 16,
        initial_heading_rad: Optional[float] = None,
        *,
        connectome_weights: Optional[str | Mapping[str, Any]] = "banc_v888",
        cue_gain: float = 2.2,
        cue_kappa: float = 4.0,
        cue_noise_deg: float = 3.0,
        basin_sigma: Optional[float] = None,
        mag_gain: float = 1.6,
        sun_gain: float = 3.2,
        bias_adapt_rate: float = 0.55,
        max_bias_rad_s: float = 0.15,
        inhibition_threshold: float = 0.0,
        reanchor_seconds: Optional[float] = 10.0,
    ) -> None:
        if n_wedges < 8 or (n_wedges % 2) != 0:
            raise ValueError(f"n_wedges must be an even integer >= 8, got {n_wedges}")
        if cue_gain < 0.0:
            raise ValueError("cue_gain must be non-negative")
        if cue_kappa <= 0.0:
            raise ValueError("cue_kappa must be positive")
        if not (cue_noise_deg > 0.0):
            raise ValueError("cue_noise_deg must be positive")
        if basin_sigma is None:
            basin_sigma = basin_sigma_for_noise(cue_noise_deg)
        if basin_sigma <= 0.0:
            raise ValueError("basin_sigma must be positive")
        if reanchor_seconds is not None and reanchor_seconds <= 0.0:
            raise ValueError("reanchor_seconds must be positive or None")

        self.n_wedges: int = int(n_wedges)
        self.cue_gain: float = float(cue_gain)
        self.cue_kappa: float = float(cue_kappa)
        self.cue_noise_deg: float = float(cue_noise_deg)
        self.basin_sigma: float = float(basin_sigma)
        self.mag_gain: float = float(mag_gain)
        self.sun_gain: float = float(sun_gain)
        self.bias_adapt_rate: float = float(bias_adapt_rate)
        self.max_bias_rad_s: float = float(max_bias_rad_s)
        self.inhibition_threshold: float = float(inhibition_threshold)
        self.reanchor_seconds: Optional[float] = (
            None if reanchor_seconds is None else float(reanchor_seconds)
        )

        cx = _resolve_cx_weights(connectome_weights, self.n_wedges)
        self._cx: Optional[dict[str, Any]] = cx
        self.connectome_mode: str = (
            "v2_analytic"
            if cx is None
            else (connectome_weights if isinstance(connectome_weights, str) else "custom_dict")
        )
        if cx is not None:
            self._M_exc: np.ndarray = np.asarray(cx["M_exc"], dtype=np.float64)
            self._M_ed7: np.ndarray = np.asarray(cx["M_ed7"], dtype=np.float64)
            self._M_d7e: np.ndarray = np.asarray(cx["M_d7e"], dtype=np.float64)
            self._M_pa_diff: np.ndarray = np.asarray(cx["M_pa_diff"], dtype=np.float64)
            if (
                self._M_exc.shape != (16, 16)
                or self._M_ed7.shape != (40, 16)
                or self._M_d7e.shape != (16, 40)
                or self._M_pa_diff.shape != (16, 16)
            ):
                raise ValueError("Invalid connectome matrix shapes for 16-wedge BANC CX ring")

        # Preferred wedge angles uniformly tiling [-pi, pi)
        self.thetas: np.ndarray = -math.pi + (
            2.0 * math.pi * np.arange(self.n_wedges, dtype=np.float64)
        ) / self.n_wedges
        self._cos_thetas: np.ndarray = np.cos(self.thetas)
        self._sin_thetas: np.ndarray = np.sin(self.thetas)

        self._rates: np.ndarray = np.zeros(self.n_wedges, dtype=np.float64)
        self._delta7: np.ndarray = np.zeros(self.n_wedges, dtype=np.float64)
        self._d7_neurons: np.ndarray = np.zeros(40, dtype=np.float64)
        self._initialized: bool = False
        self._pen_bias_rad_s: float = 0.0
        self._bias_ref_rad: float = 0.0
        self._tilt_ahrs: Optional["TiltCompensatedAHRS"] = None

        self._offbasin_c: float = 0.0
        self._offbasin_s: float = 0.0
        self._coherent_offbasin_time: float = 0.0
        self._last_mag_gate: float = 0.0
        self._last_sun_gate: float = 0.0

        if initial_heading_rad is not None:
            self.reset(initial_heading_rad)

    def _shape_bump_from_angle(self, angle_rad: float) -> np.ndarray:
        """Construct the canonical E-PG bump centered at ``angle_rad``."""
        if self._cx is not None:
            r = np.maximum(0.0, np.cos(self.thetas - angle_rad))
            s = float(r.sum())
            return r * (8.0 / max(s, 1e-6))
        raw = np.maximum(0.0, np.cos(self.thetas - angle_rad) - self.inhibition_threshold)
        peak = float(np.max(raw))
        return raw / peak if peak > 1e-12 else raw

    def reset(self, heading_rad: float = 0.0, *, reset_bias: bool = True) -> None:
        """Seed or reset the E-PG compass bump to ``heading_rad``."""
        wrapped = float(wrap_angle(heading_rad))
        self._rates = self._shape_bump_from_angle(wrapped)
        if self._cx is not None:
            self._d7_neurons = self._M_ed7 @ self._rates
            self._delta7 = self._M_d7e @ self._d7_neurons
        else:
            self._delta7 = 0.5 * (1.0 - np.cos(self.thetas - wrapped))
        self._initialized = True
        self._bias_ref_rad = wrapped
        if reset_bias:
            self._pen_bias_rad_s = 0.0
        if self._tilt_ahrs is not None:
            self._tilt_ahrs.reset_unseeded()
        self._reset_reanchor_tracker()

    def _reset_reanchor_tracker(self) -> None:
        self._offbasin_c = 0.0
        self._offbasin_s = 0.0
        self._coherent_offbasin_time = 0.0

    @property
    def initialized(self) -> bool:
        """Whether the E-PG ring bump has been seeded."""
        return self._initialized

    @property
    def bump(self) -> np.ndarray:
        """Copy of current E-PG activity bump ``r`` (sum == 8.0 in BANC connectome mode)."""
        return self._rates.copy()

    @bump.setter
    def bump(self, value: np.ndarray) -> None:
        arr = np.asarray(value, dtype=np.float64)
        if arr.shape != (self.n_wedges,):
            raise ValueError(f"bump must have shape ({self.n_wedges},), got {arr.shape}")
        self._rates = np.maximum(0.0, arr.copy())
        if self._cx is not None:
            self._d7_neurons = self._M_ed7 @ self._rates
            self._delta7 = self._M_d7e @ self._d7_neurons
        self._initialized = True

    @property
    def wedges(self) -> np.ndarray:
        """Copy of current N-wedge non-negative E-PG firing rates."""
        return self._rates.copy()

    @property
    def delta7_rates(self) -> np.ndarray:
        """Copy of 40-neuron BANC Delta7 firing rates (or N-wedge profile in analytic mode)."""
        if self._cx is not None:
            return self._d7_neurons.copy()
        return self._delta7.copy()

    @property
    def delta7_wedges(self) -> np.ndarray:
        """Copy of current N-wedge anti-phase Delta7 glutamatergic bridge inhibition."""
        return self._delta7.copy()

    @property
    def delta7_neurons(self) -> np.ndarray:
        """Copy of current 40-neuron BANC v888 Delta7 bridge population vector."""
        return self._d7_neurons.copy()

    @property
    def last_mag_gate(self) -> float:
        """Latest magnetometer coincidence gate in [0.0, 1.0]."""
        return self._last_mag_gate

    @property
    def last_sun_gate(self) -> float:
        """Latest sun/landmark coincidence gate in [0.0, 1.0]."""
        return self._last_sun_gate

    @property
    def tilt_ahrs(self) -> "TiltCompensatedAHRS":
        """Lazily initialized 3D gravity/tilt front-end used by :meth:`update_9dof`."""
        if self._tilt_ahrs is None:
            from .tilt_ahrs import TiltCompensatedAHRS

            self._tilt_ahrs = TiltCompensatedAHRS()
        return self._tilt_ahrs

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
    def bump_coherence(self) -> float:
        """First-harmonic phasor coherence ``hypot(r@cos, r@sin) / max(r.sum(), 1e-6)`` in [0, 1]."""
        if not self._initialized:
            return 0.0
        total = float(np.sum(self._rates))
        if total <= 1e-12:
            return 0.0
        c1 = float(np.dot(self._rates, self._cos_thetas))
        s1 = float(np.dot(self._rates, self._sin_thetas))
        return float(math.hypot(c1, s1) / max(total, 1e-6))

    @property
    def bump_amplitude(self) -> float:
        """Alias of :attr:`bump_coherence` for v1/v2 API compatibility."""
        return self.bump_coherence

    @property
    def estimated_gyro_bias_rad_s(self) -> float:
        """Homeostatic P-EN angular-velocity bias estimate in rad/s."""
        return self._pen_bias_rad_s

    def diagnostics(self) -> CompassDiagnostics:
        """Return a structured diagnostic snapshot of the latest compass state."""
        return CompassDiagnostics(
            heading_rad=self.heading_rad,
            bump_amplitude=self.bump_coherence,
            estimated_gyro_bias_rad_s=self._pen_bias_rad_s,
            mag_gate=self._last_mag_gate,
            sun_gate=self._last_sun_gate,
        )

    def _von_mises_profile(self, cue_angle_rad: float) -> np.ndarray:
        """Localized R-neuron afferent excitation profile normalized to match E-PG 1st harmonic."""
        prof = np.power(0.5 * (1.0 + np.cos(self.thetas - cue_angle_rad)), 3)
        c1 = abs(float(np.dot(prof, np.cos(self.thetas - cue_angle_rad))))
        target_c1 = 0.25 * self.n_wedges
        return prof * (target_c1 / max(c1, 1e-12))

    def _basin_coincidence_gate(self, cue_angle_rad: float, pred_heading_rad: float) -> float:
        """Cooperative dendritic coincidence gate between R-neuron input and E-PG bump."""
        dist = 1.0 - math.cos(cue_angle_rad - pred_heading_rad)
        ratio = dist / self.basin_sigma
        return math.exp(-(ratio * ratio))

    @staticmethod
    def _connectome_cue_coincidence(
        cz: np.ndarray,
        cz_norm: float,
        r: np.ndarray,
    ) -> float:
        """Compute cosine-overlap coincidence ``max(0, (ov - 0.5)/0.5)**2`` in [0, 1]."""
        if cz_norm <= 1e-12:
            return 0.0
        rz = r - float(np.mean(r))
        rz_norm = float(np.linalg.norm(rz))
        if rz_norm <= 1e-12:
            return 0.0
        ov = float(np.dot(cz, rz) / (cz_norm * rz_norm))
        if ov <= 0.50:
            return 0.0
        u = (ov - 0.50) / 0.50
        return float(u * u)

    def _update_connectome(
        self,
        omega_rad_s: float,
        dt: float,
        mag_heading_rad: Optional[float],
        sun_heading_rad: Optional[float],
        mag_conf: float,
        sun_conf: float,
    ) -> float:
        """Advance the 16-wedge BANC v888 connectome ring attractor with dt_sub <= 0.01 s."""
        self._last_mag_gate = 0.0
        self._last_sun_gate = 0.0
        if dt == 0.0:
            return self.heading_rad

        n_sub = max(1, int(math.ceil(dt / 0.01 - 1e-12)))
        dt_sub = float(dt) / n_sub
        alpha = min(0.25, dt_sub / 0.16)
        omega_f = float(omega_rad_s) - self._pen_bias_rad_s
        cue_scale = 28.0 * (self.cue_gain / 2.2)

        mag_ex: Optional[np.ndarray] = None
        mag_cz: Optional[np.ndarray] = None
        mag_cz_norm = 0.0
        mag_ang = 0.0
        if mag_heading_rad is not None and mag_conf > 0.0:
            mag_ang = float(wrap_angle(mag_heading_rad))
            mag_ex = np.maximum(0.0, np.cos(self.thetas - mag_ang)) ** 3
            mag_cz = mag_ex - float(np.mean(mag_ex))
            mag_cz_norm = float(np.linalg.norm(mag_cz))

        sun_ex: Optional[np.ndarray] = None
        sun_cz: Optional[np.ndarray] = None
        sun_cz_norm = 0.0
        sun_ang = 0.0
        if sun_heading_rad is not None and sun_conf > 0.0:
            sun_ang = float(wrap_angle(sun_heading_rad))
            sun_ex = np.maximum(0.0, np.cos(self.thetas - sun_ang)) ** 3
            sun_cz = sun_ex - float(np.mean(sun_ex))
            sun_cz_norm = float(np.linalg.norm(sun_cz))

        r = self._rates
        d7 = self._d7_neurons
        inh = self._delta7
        mag_coinc = 0.0
        sun_coinc = 0.0
        for _ in range(n_sub):
            d7 = self._M_ed7 @ r
            inh = self._M_d7e @ d7
            exc = self._M_exc @ r
            shift = (10.2 * omega_f) * (self._M_pa_diff @ r)

            cue_drive = 0.0
            if mag_ex is not None and mag_cz is not None:
                mag_coinc = self._connectome_cue_coincidence(mag_cz, mag_cz_norm, r)
                mg = mag_coinc * mag_conf
                self._last_mag_gate = mg
                if mg > 0.0:
                    cue_drive = cue_drive + (dt_sub * cue_scale * mg) * mag_ex

            if sun_ex is not None and sun_cz is not None:
                sun_coinc = self._connectome_cue_coincidence(sun_cz, sun_cz_norm, r)
                sg = sun_coinc * sun_conf
                self._last_sun_gate = sg
                if sg > 0.0:
                    sun_mult = max(1.0, self.sun_gain / max(self.mag_gain, 1e-6))
                    cue_drive = cue_drive + (dt_sub * cue_scale * sun_mult * sg) * sun_ex

            r = np.maximum(
                0.0,
                (1.0 - alpha) * r
                + alpha * np.maximum(0.0, 1.4 * exc - 1.1 * inh)
                + cue_drive
                + dt_sub * shift,
            )
            r = r * (8.0 / max(float(r.sum()), 1e-6))

        self._rates = r
        self._d7_neurons = self._M_ed7 @ r
        self._delta7 = self._M_d7e @ self._d7_neurons

        # P-EN kinematic gyro-bias homeostasis driven only by coincidence-gated cues
        if dt > 0.0 and self.bias_adapt_rate > 0.0:
            self._bias_ref_rad = float(wrap_angle(self._bias_ref_rad + omega_f * dt))
            if sun_ex is not None and sun_coinc > 0.05 and self._last_sun_gate > 0.0:
                err_b = float(wrap_angle(sun_ang - self._bias_ref_rad))
                self._bias_ref_rad = float(
                    wrap_angle(self._bias_ref_rad + self.sun_gain * self._last_sun_gate * dt * err_b)
                )
                self._pen_bias_rad_s -= self.bias_adapt_rate * self._last_sun_gate * err_b * dt
                self._pen_bias_rad_s = float(
                    np.clip(self._pen_bias_rad_s, -self.max_bias_rad_s, self.max_bias_rad_s)
                )
            elif mag_ex is not None and mag_coinc > 0.05 and self._last_mag_gate > 0.0:
                err_b = float(wrap_angle(mag_ang - self._bias_ref_rad))
                self._bias_ref_rad = float(
                    wrap_angle(self._bias_ref_rad + self.mag_gain * self._last_mag_gate * dt * err_b)
                )
                self._pen_bias_rad_s -= self.bias_adapt_rate * self._last_mag_gate * err_b * dt
                self._pen_bias_rad_s = float(
                    np.clip(self._pen_bias_rad_s, -self.max_bias_rad_s, self.max_bias_rad_s)
                )

        # Sustained coherent off-basin re-anchoring after genuine heading upsets
        if (
            self.reanchor_seconds is not None
            and mag_ex is not None
            and mag_coinc < 0.05
            and sun_heading_rad is None
        ):
            pred_heading = self.heading_rad
            innov = float(wrap_angle(mag_ang - pred_heading))
            alpha_ra = 1.0 if self._coherent_offbasin_time == 0.0 else min(1.0, dt / _REANCHOR_TAU_S)
            self._offbasin_c += alpha_ra * (math.cos(innov) - self._offbasin_c)
            self._offbasin_s += alpha_ra * (math.sin(innov) - self._offbasin_s)
            if math.hypot(self._offbasin_c, self._offbasin_s) >= _REANCHOR_MIN_COHERENCE:
                self._coherent_offbasin_time += dt
            else:
                self._coherent_offbasin_time = 0.0
            if self._coherent_offbasin_time >= self.reanchor_seconds:
                mean_innov = math.atan2(self._offbasin_s, self._offbasin_c)
                self.reset(pred_heading + mean_innov, reset_bias=False)
                return self.heading_rad
        else:
            self._reset_reanchor_tracker()

        return self.heading_rad

    def update(
        self,
        omega_rad_s: float,
        dt: float,
        mag_heading_rad: Optional[float] = None,
        sun_heading_rad: Optional[float] = None,
        mag_confidence: float = 1.0,
        sun_confidence: float = 1.0,
    ) -> float:
        """Advance the E-PG ring attractor by ``dt`` seconds and return ``heading_rad``."""
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
            return self.heading_rad

        if self._cx is not None:
            return self._update_connectome(
                omega_rad_s=omega_rad_s,
                dt=dt,
                mag_heading_rad=mag_heading_rad,
                sun_heading_rad=sun_heading_rad,
                mag_conf=mag_conf,
                sun_conf=sun_conf,
            )

        # v2_analytic mode: exact first-harmonic phasor ring attractor
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

        raw_cos_rot = c_rot * self._cos_thetas + s_rot * self._sin_thetas
        delta7_inh = 0.5 * (1.0 - raw_cos_rot)
        rec_drive = 1.0 - 2.0 * delta7_inh
        u_ring = np.maximum(0.0, rec_drive - self.inhibition_threshold)
        u_peak = float(np.max(u_ring))
        if u_peak > 1e-12:
            u_ring = u_ring / u_peak

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

            if (
                self.reanchor_seconds is not None
                and mag_gate < 0.05
                and sun_heading_rad is None
                and dt > 0.0
            ):
                innov = float(wrap_angle(mag_ang - pred_heading))
                alpha = 1.0 if self._coherent_offbasin_time == 0.0 else min(1.0, dt / _REANCHOR_TAU_S)
                self._offbasin_c += alpha * (math.cos(innov) - self._offbasin_c)
                self._offbasin_s += alpha * (math.sin(innov) - self._offbasin_s)
                if math.hypot(self._offbasin_c, self._offbasin_s) >= _REANCHOR_MIN_COHERENCE:
                    self._coherent_offbasin_time += dt
                else:
                    self._coherent_offbasin_time = 0.0
                if self._coherent_offbasin_time >= self.reanchor_seconds:
                    mean_innov = math.atan2(self._offbasin_s, self._offbasin_c)
                    self.reset(pred_heading + mean_innov, reset_bias=False)
                    return self.heading_rad
            else:
                self._reset_reanchor_tracker()

            eff_mag = self.mag_gain * mag_conf * mag_gate * dt
            if eff_mag > 0.0:
                u_ring = u_ring + eff_mag * self._von_mises_profile(mag_ang)
                mag_err = float(wrap_angle(mag_ang - pred_heading))
                net_innovation += (self.mag_gain * mag_conf * mag_gate) * mag_err
                net_gate_weight += self.mag_gain * mag_conf * mag_gate

        c_new = float(np.dot(u_ring, self._cos_thetas))
        s_new = float(np.dot(u_ring, self._sin_thetas))
        new_heading = float(wrap_angle(math.atan2(s_new, c_new)))
        self._rates = self._shape_bump_from_angle(new_heading)
        self._delta7 = 0.5 * (1.0 - np.cos(self.thetas - new_heading))

        if dt > 0.0 and net_gate_weight > 1e-6 and self.bias_adapt_rate > 0.0:
            weighted_err = net_innovation / max(self.mag_gain, self.sun_gain, 1.0)
            self._pen_bias_rad_s -= self.bias_adapt_rate * weighted_err * dt
            self._pen_bias_rad_s = float(
                np.clip(self._pen_bias_rad_s, -self.max_bias_rad_s, self.max_bias_rad_s)
            )

        return self.heading_rad

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
        g_ref: Optional[float] = None,
    ) -> float:
        """Advance 3D roll/pitch tilt compensation + E-PG ring attractor from a 9-axis IMU+Mag frame."""
        ahrs = self.tilt_ahrs
        if g_ref is not None:
            if g_ref <= 0.0:
                raise ValueError("g_ref must be positive")
            ahrs.g_ref = float(g_ref)
        diag = ahrs.update_9dof(gx, gy, gz, ax, ay, az, mx, my, mz, dt)
        return self.update(
            omega_rad_s=diag.yaw_rate_rad_s,
            dt=dt,
            mag_heading_rad=diag.mag_heading_rad,
            sun_heading_rad=sun_heading_rad,
            mag_confidence=mag_confidence,
            sun_confidence=sun_confidence,
        )


class PlasticLandmarkRing:
    """Hebbian / anti-Hebbian R-neuron -> E-PG plastic ring mapping for warped magnetic scenes.

    Models associative synaptic plasticity between ``n_ring_neurons`` circularly tuned
    sensory Ring Neurons (R2/R4d; mean EPG fanout ~10.92 in BANC v888) and the ``n_wedges``
    Ellipsoid Body E-PG compass wedges (Fisher et al., *Nature* 2019; Kim et al., *Nature* 2019).
    """

    PROVENANCE: str = "connectome"

    def __init__(
        self,
        n_wedges: int = 16,
        n_ring_neurons: int = 32,
        *,
        connectome_weights: Optional[str | Mapping[str, Any]] = "banc_v888",
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
            compass
            if compass is not None
            else RingCompass(n_wedges=self.n_wedges, connectome_weights=connectome_weights)
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
        self._map_offset_rad: float = 0.0
        self._has_absolute_reference: bool = False
        self._learn_steps: int = 0
        self._dr_compass: RingCompass = RingCompass(
            n_wedges=self.n_wedges,
            initial_heading_rad=compass.heading_rad if (compass is not None and compass.initialized) else None,
            connectome_weights="v2_analytic",
            mag_gain=self.compass.mag_gain,
            sun_gain=self.compass.sun_gain,
            basin_sigma=self.compass.basin_sigma,
            bias_adapt_rate=self.compass.bias_adapt_rate,
            max_bias_rad_s=self.compass.max_bias_rad_s,
            reanchor_seconds=self.compass.reanchor_seconds,
        )
        self._grid: np.ndarray = np.linspace(-math.pi, math.pi, 36, endpoint=False)
        self._grid_r_act: np.ndarray = np.stack([self.encode_r_neurons(a) for a in self._grid], axis=1)

        diff = self.epg_thetas[:, None] - self.r_thetas[None, :]
        self.weights: np.ndarray = float(prior_strength) * np.cos(diff)
        self._tilt_ahrs: Optional["TiltCompensatedAHRS"] = None

    @property
    def tilt_ahrs(self) -> "TiltCompensatedAHRS":
        """Lazily initialized 3D gravity/tilt front-end used by :meth:`update_9dof`."""
        if self._tilt_ahrs is None:
            from .tilt_ahrs import TiltCompensatedAHRS

            self._tilt_ahrs = TiltCompensatedAHRS()
        return self._tilt_ahrs

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
        corrected = float(wrap_angle(math.atan2(s1, c1) - self._map_offset_rad))
        conf = float(np.clip(amp, 0.0, 1.0))
        return corrected, conf

    def _recenter_zero_mean(self) -> None:
        """Set the map's output offset so the mean correction over the circle is zero."""
        drive = self.weights @ self._grid_r_act
        mapped = np.arctan2(self._sin_epg @ drive, self._cos_epg @ drive)
        diff = mapped - self._grid
        self._map_offset_rad = float(math.atan2(float(np.mean(np.sin(diff))), float(np.mean(np.cos(diff)))))

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
        """Advance compass, decode plastic landmark heading, and apply Hebbian/anti-Hebbian learning."""
        raw_ang = float(wrap_angle(raw_mag_heading_rad))
        if reference_heading_rad is not None and not self._has_absolute_reference:
            self._has_absolute_reference = True
            self._map_offset_rad = 0.0
        if not self.compass.initialized:
            seed = float(reference_heading_rad) if reference_heading_rad is not None else raw_ang
            self.compass.reset(seed)
            self._dr_compass.reset(seed)
            heading = self.compass.heading_rad
        elif reference_heading_rad is not None or self.compass._cx is None:
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
            self._dr_compass.reset(heading, reset_bias=False)
        else:
            if not self._dr_compass.initialized:
                self._dr_compass.reset(self.compass.heading_rad)
            prev_dr = self._dr_compass.heading_rad
            corr_mag, map_conf = self.map_raw_heading(raw_ang)
            eff_mag_conf = map_conf * float(np.clip(anchor_gain_scale, 0.0, 1.0))
            dr_heading = self._dr_compass.update(
                omega_rad_s=omega_rad_s,
                dt=dt,
                mag_heading_rad=corr_mag if eff_mag_conf > 0.0 else None,
                mag_confidence=eff_mag_conf,
            )
            net_omega = float(omega_rad_s) - self._dr_compass.estimated_gyro_bias_rad_s
            if abs(float(circular_error_rad(dr_heading, prev_dr))) > abs(net_omega * dt) + 0.25:
                self.compass.reset(dr_heading, reset_bias=False)
            self.compass._pen_bias_rad_s = 0.0
            heading = self.compass.update(
                omega_rad_s=net_omega,
                dt=dt,
                mag_heading_rad=dr_heading,
                mag_confidence=1.0,
            )
            self.compass._pen_bias_rad_s = self._dr_compass.estimated_gyro_bias_rad_s

        if learn and dt > 0.0:
            r_act = self.encode_r_neurons(raw_ang)
            target_epg = np.cos(self.epg_thetas - heading)
            pred_epg = self.weights @ r_act
            d_turn = abs(float(omega_rad_s)) * dt
            if self._prev_raw_ang is None:
                d_raw = d_turn
            else:
                d_raw = min(abs(float(wrap_angle(raw_ang - self._prev_raw_ang))), 2.0 * d_turn)
            self._prev_raw_ang = raw_ang
            step_rate = min(0.95, self.learning_rate * max(d_raw, 0.05 * dt))
            self.weights += step_rate * np.outer(target_epg - pred_epg, r_act)
            np.clip(self.weights, -8.0, 8.0, out=self.weights)
            self._learn_steps += 1
            if not self._has_absolute_reference and self._learn_steps % 25 == 0:
                self._recenter_zero_mean()

        return heading

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
        reference_heading_rad: Optional[float] = None,
        learn: bool = True,
        anchor_gain_scale: float = 0.25,
        g_ref: Optional[float] = None,
    ) -> float:
        """Tilt-compensate a 9-axis IMU+Magnetometer frame and update plastic R -> E-PG compass."""
        ahrs = self.tilt_ahrs
        if g_ref is not None:
            if g_ref <= 0.0:
                raise ValueError("g_ref must be positive")
            ahrs.g_ref = float(g_ref)
        diag = ahrs.update_9dof(gx, gy, gz, ax, ay, az, mx, my, mz, dt)
        return self.update(
            omega_rad_s=diag.yaw_rate_rad_s,
            dt=dt,
            raw_mag_heading_rad=diag.mag_heading_rad,
            reference_heading_rad=reference_heading_rad,
            learn=learn,
            anchor_gain_scale=anchor_gain_scale,
        )
