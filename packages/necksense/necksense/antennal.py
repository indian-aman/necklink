"""Antennal Lobe sensory conditioning, plume whiff tracking, and LN normalization.

Implements dual-timescale olfactory receptor neuron (ORN) baseline adaptation,
turbulent plume phasic whiff / intermittency tracking, and Olsen-Wilson
GABAergic local interneuron (LN) divisive gain normalization:

    r_i = max(0, u_i)^n / (sigma^n + (gamma * mean_pool)^n + max(0, u_i)^n)

References:
    - Olsen, S. R. & Wilson, R. I. (2008). Lateral presynaptic inhibition
      mediates gain control in an olfactory circuit. Nature, 452, 956-960.
    - Nagel, K. I. & Wilson, R. I. (2011). Biophysical mechanisms underlying
      olfactory receptor neuron dynamics. Nature Neuroscience, 14, 208-216.
    - Celani, A., Villermaux, E. & Vergassola, M. (2014). Odor landscapes in
      turbulent environments. Physical Review X, 4(4), 041015.
"""

from __future__ import annotations

from typing import Any

import numpy as np

Q16_ONE: int = 1 << 16
_INT32_MIN: int = -(1 << 31)
_INT32_MAX: int = (1 << 31) - 1
_UINT64_SHIFT_GUARD: int = 1 << 47


def float_to_q16(values: np.ndarray | float) -> np.ndarray:
    """Convert floating-point values into clamped Q16.16 signed int32 integers."""
    arr = np.asarray(values, dtype=np.float64)
    scaled = np.rint(arr * float(Q16_ONE))
    clamped = np.clip(scaled, _INT32_MIN, _INT32_MAX)
    return clamped.astype(np.int32)


def q16_to_float(values: np.ndarray) -> np.ndarray:
    """Convert Q16.16 integer values back into float64."""
    return np.asarray(values, dtype=np.float64) / float(Q16_ONE)


class AntennalNormalizer:
    """Dual-timescale receptor drift removal, whiff tracking & LN normalization.

    Subtracts slow tonic baseline drift (thermal, humidity, or sensor aging)
    while preserving fast phasic plume/event transients, tracks turbulent
    phasic whiff up-crossings (`whiff_rate` and `intermittency`) for chemical
    source-proximity estimation, and applies lateral GABAergic divisive
    normalization so combinatorial channel patterns remain invariant across
    100x+ concentration or load swings without amplifying dark sensor noise.
    """

    def __init__(
        self,
        n_channels: int,
        *,
        sigma: float = 0.01,
        gamma: float = 1.0,
        hill_n: float = 2.0,
        tonic_alpha: float = 0.01,
        tonic_down_alpha: float | None = None,
        tonic_slew_limit: float | None = 0.02,
        phasic_gain: float = 0.5,
        fast_alpha: float = 0.35,
        whiff_threshold: float | None = None,
        whiff_alpha: float = 0.10,
        c_parity: bool = False,
    ) -> None:
        if n_channels < 1:
            raise ValueError("n_channels must be >= 1")
        if sigma <= 0.0:
            raise ValueError("sigma must be > 0")
        if gamma < 0.0:
            raise ValueError("gamma must be >= 0")
        if hill_n <= 0.0:
            raise ValueError("hill_n must be > 0")
        if not (0.0 <= tonic_alpha <= 1.0):
            raise ValueError("tonic_alpha must be in [0, 1]")
        if phasic_gain < 0.0:
            raise ValueError("phasic_gain must be >= 0")
        if not (0.0 <= fast_alpha <= 1.0):
            raise ValueError("fast_alpha must be in [0, 1]")
        if whiff_threshold is not None and whiff_threshold <= 0.0:
            raise ValueError("whiff_threshold must be > 0 or None")
        if not (0.0 < whiff_alpha <= 1.0):
            raise ValueError("whiff_alpha must be in (0, 1]")
        if c_parity and int(hill_n) not in (1, 2):
            raise ValueError("c_parity=True supports integer hill_n in {1, 2}")

        self.n_channels = int(n_channels)
        self.sigma = float(sigma)
        self.gamma = float(gamma)
        self.hill_n = float(int(hill_n) if c_parity else hill_n)
        self.tonic_alpha = float(tonic_alpha)
        if tonic_down_alpha is None:
            self.tonic_down_alpha = min(1.0, self.tonic_alpha * 5.0) if self.tonic_alpha > 0.0 else 0.0
        else:
            if not (0.0 <= tonic_down_alpha <= 1.0):
                raise ValueError("tonic_down_alpha must be in [0, 1]")
            self.tonic_down_alpha = float(tonic_down_alpha)
        if tonic_slew_limit is not None and tonic_slew_limit < 0.0:
            raise ValueError("tonic_slew_limit must be >= 0 or None")
        self.tonic_slew_limit = None if tonic_slew_limit is None else float(tonic_slew_limit)
        self.phasic_gain = float(phasic_gain)
        self.fast_alpha = float(fast_alpha)
        self.whiff_threshold = 2.0 * self.sigma if whiff_threshold is None else float(whiff_threshold)
        self.whiff_alpha = float(whiff_alpha)
        self.c_parity = bool(c_parity)

        # Q16.16 fixed-point parameters for exact C99 parity
        self._sigma_q16 = max(1, int(round(self.sigma * Q16_ONE)))
        self._gamma_q16 = int(round(self.gamma * Q16_ONE))
        self._tonic_alpha_q16 = int(round(self.tonic_alpha * Q16_ONE))
        self._tonic_down_q16 = int(round(self.tonic_down_alpha * Q16_ONE))
        self._tonic_slew_q16 = (
            0 if self.tonic_slew_limit is None else max(1, int(round(self.tonic_slew_limit * Q16_ONE)))
        )
        self._phasic_gain_q16 = int(round(self.phasic_gain * Q16_ONE))
        self._fast_alpha_q16 = int(round(self.fast_alpha * Q16_ONE))
        if whiff_threshold is None:
            self._whiff_thresh_q16 = min(_INT32_MAX, self._sigma_q16 * 2)
        else:
            self._whiff_thresh_q16 = max(1, min(_INT32_MAX, int(round(self.whiff_threshold * Q16_ONE))))
        self._whiff_alpha_q16 = int(round(self.whiff_alpha * Q16_ONE))

        self.tonic_baseline = np.zeros(self.n_channels, dtype=np.float64)
        self.fast_state = np.zeros(self.n_channels, dtype=np.float64)
        self._tonic_q16 = np.zeros(self.n_channels, dtype=np.int32)
        self._fast_q16 = np.zeros(self.n_channels, dtype=np.int32)

        self.steps: int = 0
        self.whiff_count: int = 0
        self.whiff_rate: float = 0.0
        self.intermittency: float = 0.0
        self._in_whiff: bool = False
        self._whiff_rate_q16: int = 0
        self._intermittency_q16: int = 0

    def reset(self, baseline: np.ndarray | float | None = None) -> None:
        """Reset tonic, fast, and plume whiff states to zero or an explicit baseline."""
        self.steps = 0
        self.whiff_count = 0
        self.whiff_rate = 0.0
        self.intermittency = 0.0
        self._in_whiff = False
        self._whiff_rate_q16 = 0
        self._intermittency_q16 = 0

        if baseline is None:
            self.tonic_baseline.fill(0.0)
            self.fast_state.fill(0.0)
            self._tonic_q16.fill(0)
            self._fast_q16.fill(0)
            return

        b = np.broadcast_to(np.asarray(baseline, dtype=np.float64), (self.n_channels,)).copy()
        self.tonic_baseline[:] = b
        self.fast_state[:] = b
        b_q16 = float_to_q16(b)
        self._tonic_q16[:] = b_q16
        self._fast_q16[:] = b_q16

    def calibrate(self, ambient_samples: np.ndarray) -> np.ndarray:
        """Lock tonic and fast receptor baselines to clean-air / ambient data."""
        arr = np.asarray(ambient_samples, dtype=np.float64)
        if arr.ndim == 1:
            if arr.shape[0] != self.n_channels:
                raise ValueError(f"Expected {self.n_channels} channels, got {arr.shape[0]}")
            mean_vec = arr.copy()
        elif arr.ndim == 2:
            if arr.shape[1] != self.n_channels:
                raise ValueError(f"Expected {self.n_channels} channels, got {arr.shape[1]}")
            mean_vec = arr.mean(axis=0)
        else:
            raise ValueError("ambient_samples must be 1D (D,) or 2D (T, D)")
        self.reset(mean_vec)
        return mean_vec

    def _ln_normalize_q16_vec(self, pos_q16: np.ndarray) -> np.ndarray:
        """Exact Q16.16 integer Olsen-Wilson LN normalization for a 1D vector."""
        d = self.n_channels
        p_u64 = pos_q16.astype(np.uint64)
        mean_pool = int(np.sum(p_u64, dtype=np.uint64)) // d
        lat_q16 = (mean_pool * self._gamma_q16) >> 16
        out = np.zeros(d, dtype=np.int32)
        hill_n_int = int(self.hill_n)

        if hill_n_int == 1:
            base_den = self._sigma_q16 + lat_q16
            for i in range(d):
                num = int(p_u64[i])
                den = base_den + num
                out[i] = 0 if den == 0 else int((num << 16) // den)
        else:
            sig2 = self._sigma_q16 * self._sigma_q16
            lat2 = lat_q16 * lat_q16
            base_den = sig2 + lat2
            for i in range(d):
                p_val = int(p_u64[i])
                num = p_val * p_val
                den = base_den + num
                if den == 0:
                    out[i] = 0
                else:
                    n_s = num
                    d_s = den
                    while n_s > _UINT64_SHIFT_GUARD:
                        n_s >>= 2
                        d_s >>= 2
                    out[i] = Q16_ONE if d_s == 0 else int((n_s << 16) // d_s)
        return out

    def normalize_q16(self, x_q16: np.ndarray) -> np.ndarray:
        """Stateless Q16.16 Olsen-Wilson LN normalization returning int32 Q16.16."""
        arr = np.asarray(x_q16, dtype=np.int32)
        if arr.ndim == 1:
            if arr.shape[0] != self.n_channels:
                raise ValueError(f"Expected {self.n_channels} channels, got {arr.shape[0]}")
            pos = np.maximum(arr, 0)
            return self._ln_normalize_q16_vec(pos)
        if arr.ndim == 2:
            if arr.shape[1] != self.n_channels:
                raise ValueError(f"Expected {self.n_channels} channels, got {arr.shape[1]}")
            out = np.empty_like(arr, dtype=np.int32)
            for t in range(arr.shape[0]):
                out[t] = self._ln_normalize_q16_vec(np.maximum(arr[t], 0))
            return out
        raise ValueError("x_q16 must be 1D (D,) or 2D (N, D)")

    def normalize(self, x: np.ndarray) -> np.ndarray:
        """Stateless Olsen-Wilson LN divisive gain normalization (no state update)."""
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim not in (1, 2):
            raise ValueError("x must be 1D (D,) or 2D (N, D)")
        if arr.shape[-1] != self.n_channels:
            raise ValueError(f"Expected {self.n_channels} channels, got {arr.shape[-1]}")

        if self.c_parity:
            q_in = float_to_q16(arr)
            q_out = self.normalize_q16(q_in)
            return q16_to_float(q_out)

        pos = np.maximum(arr, 0.0)
        mean_pool = np.mean(pos, axis=-1, keepdims=True)
        if self.hill_n == 2.0:
            num = pos * pos
            lat = self.gamma * mean_pool
            den = (self.sigma * self.sigma) + (lat * lat) + num
        elif self.hill_n == 1.0:
            num = pos
            den = self.sigma + (self.gamma * mean_pool) + num
        else:
            num = np.power(pos, self.hill_n)
            den = (self.sigma ** self.hill_n) + np.power(self.gamma * mean_pool, self.hill_n) + num
        return num / den

    def step_q16(self, x_q16: np.ndarray) -> np.ndarray:
        """Single-step stateful Q16.16 adaptation, whiff tracking & LN normalization."""
        arr = np.asarray(x_q16, dtype=np.int32)
        if arr.ndim != 1 or arr.shape[0] != self.n_channels:
            raise ValueError(f"Expected 1D vector of length {self.n_channels}")

        pos = np.zeros(self.n_channels, dtype=np.int32)
        pos_phasic_sum = 0
        for i in range(self.n_channels):
            xv = int(arr[i])
            bv = int(self._tonic_q16[i])
            fv = int(self._fast_q16[i])

            diff_tonic = xv - bv
            diff_fast = xv - fv
            if diff_fast > 0:
                pos_phasic_sum += diff_fast
            phasic_term = (diff_fast * self._phasic_gain_q16) >> 16
            drive = diff_tonic + phasic_term
            if drive > 0:
                pos[i] = _INT32_MAX if drive > _INT32_MAX else int(drive)

            fv_next = fv + ((diff_fast * self._fast_alpha_q16) >> 16)
            self._fast_q16[i] = int(np.clip(fv_next, _INT32_MIN, _INT32_MAX))

            if diff_tonic >= 0:
                step_b = (diff_tonic * self._tonic_alpha_q16) >> 16
                if self._tonic_slew_q16 > 0 and step_b > self._tonic_slew_q16:
                    step_b = self._tonic_slew_q16
                bv_next = bv + step_b
            else:
                step_b = (diff_tonic * self._tonic_down_q16) >> 16
                bv_next = bv + step_b
            self._tonic_q16[i] = int(np.clip(bv_next, _INT32_MIN, _INT32_MAX))

        mean_phasic = pos_phasic_sum // self.n_channels
        up_cross = 0
        if not self._in_whiff:
            if mean_phasic >= self._whiff_thresh_q16:
                self._in_whiff = True
                self.whiff_count += 1
                up_cross = Q16_ONE
        else:
            if mean_phasic <= (self._whiff_thresh_q16 >> 2):
                self._in_whiff = False

        active_q16 = Q16_ONE if self._in_whiff else 0
        self._whiff_rate_q16 = int(
            np.clip(
                self._whiff_rate_q16 + (((up_cross - self._whiff_rate_q16) * self._whiff_alpha_q16) >> 16),
                _INT32_MIN,
                _INT32_MAX,
            )
        )
        self._intermittency_q16 = int(
            np.clip(
                self._intermittency_q16
                + (((active_q16 - self._intermittency_q16) * self._whiff_alpha_q16) >> 16),
                _INT32_MIN,
                _INT32_MAX,
            )
        )
        self.whiff_rate = self._whiff_rate_q16 / float(Q16_ONE)
        self.intermittency = self._intermittency_q16 / float(Q16_ONE)
        self.steps += 1

        self.tonic_baseline[:] = q16_to_float(self._tonic_q16)
        self.fast_state[:] = q16_to_float(self._fast_q16)
        return self._ln_normalize_q16_vec(pos)

    def step(self, x: np.ndarray) -> np.ndarray:
        """Process one sensory sample `(D,)` online with drift adaptation + LN gain."""
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim != 1 or arr.shape[0] != self.n_channels:
            raise ValueError(f"Expected 1D vector of length {self.n_channels}, got {arr.shape}")

        if self.c_parity:
            q_out = self.step_q16(float_to_q16(arr))
            return q16_to_float(q_out)

        diff_tonic = arr - self.tonic_baseline
        diff_fast = arr - self.fast_state
        drive = diff_tonic + self.phasic_gain * diff_fast

        mean_phasic = float(np.mean(np.maximum(diff_fast, 0.0)))
        up_cross = 0.0
        if not self._in_whiff:
            if mean_phasic >= self.whiff_threshold:
                self._in_whiff = True
                self.whiff_count += 1
                up_cross = 1.0
        else:
            if mean_phasic <= 0.25 * self.whiff_threshold:
                self._in_whiff = False

        active_flag = 1.0 if self._in_whiff else 0.0
        self.whiff_rate += self.whiff_alpha * (up_cross - self.whiff_rate)
        self.intermittency += self.whiff_alpha * (active_flag - self.intermittency)
        self.steps += 1

        # Update fast phasic tracker
        self.fast_state += self.fast_alpha * diff_fast

        # Update slow tonic baseline with asymmetric recovery and optional upward slew limit
        up_step = self.tonic_alpha * diff_tonic
        if self.tonic_slew_limit is not None:
            up_step = np.minimum(up_step, self.tonic_slew_limit)
        down_step = self.tonic_down_alpha * diff_tonic
        self.tonic_baseline += np.where(diff_tonic >= 0.0, up_step, down_step)

        return self.normalize(drive)

    def step_with_whiff(self, x: np.ndarray) -> tuple[np.ndarray, float]:
        """Process one sensory sample `(D,)` and return `(normalized_vec, whiff_rate)`."""
        norm = self.step(x)
        return norm, float(self.whiff_rate)

    def diagnostics(self, *, sample_rate_hz: float = 1.0) -> dict[str, Any]:
        """Return turbulent plume whiff & baseline adaptation diagnostics."""
        if sample_rate_hz <= 0.0:
            raise ValueError("sample_rate_hz must be > 0")
        return {
            "steps": int(self.steps),
            "whiff_count": int(self.whiff_count),
            "whiff_rate": float(self.whiff_rate),
            "whiff_hz": float(self.whiff_rate * sample_rate_hz),
            "intermittency": float(self.intermittency),
            "in_whiff": bool(self._in_whiff),
            "tonic_mean": float(np.mean(self.tonic_baseline)),
            "whiff_rate_q16": int(self._whiff_rate_q16),
            "intermittency_q16": int(self._intermittency_q16),
        }

    def transform(self, X: np.ndarray, *, reset_state: bool = False) -> np.ndarray:
        """Process a 1D sample `(D,)` or sequential 2D stream `(T, D)`."""
        if reset_state:
            self.reset()
        arr = np.asarray(X, dtype=np.float64)
        if arr.ndim == 1:
            return self.step(arr)
        if arr.ndim == 2:
            if arr.shape[1] != self.n_channels:
                raise ValueError(f"Expected {self.n_channels} channels, got {arr.shape[1]}")
            out = np.empty_like(arr, dtype=np.float64)
            for t in range(arr.shape[0]):
                out[t] = self.step(arr[t])
            return out
        raise ValueError("X must be 1D (D,) or 2D (T, D)")

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return self.transform(x)
