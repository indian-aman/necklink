"""Zero-float chordotonal vibration & acoustic spectrum front-end.

Modeled on the 2,132 frequency-tuned mechanosensory neurons of the *Drosophila*
Johnston's Organ and femoral chordotonal organs (`BANC v888` / `MaleCNS`), this
front-end converts raw 1D acoustic/vibration windows or 3-axis I2C accelerometer
windows (`MPU6050`, `ADXL345`, `LSM6DSO`) into a 16-channel feature vector:

    - Channels 0..5:   6-octave dyadic Haar wavelet filterbank (`L1` MAV)
    - Channels 6..9:   2nd-difference jerk + sub-octave comb resonators (`L=3,6,12`)
    - Channels 10..12: Degree-1 crest/impulse excess (bearing spall micro-impacts)
    - Channels 13..14: High-frequency AM envelope demodulation (`BPFI` / `BPFO`)
    - Channel 15:      Positive/negative excursion & rub polarity asymmetry

Every channel is strictly homogeneous of degree 1 (`f(a * x) = a * f(x)` for
`a > 0`), so feeding `VibrationFrontEnd` -> `AntennalNormalizer` ->
`MushroomNoveltyDetector` / `MushroomValenceClassifier` makes 1-shot motor,
bearing, pump, spindle, and propeller fault detection invariant across 10x+
load and amplitude changes while executing in pure integer math on MCUs.
"""

from __future__ import annotations

import numpy as np

from .antennal import _INT32_MAX, _INT32_MIN, float_to_q16, q16_to_float

NS_VIB_CHANNELS: int = 16

VIBRATION_CHANNEL_NAMES: tuple[str, ...] = (
    "haar_oct0_nyquist",
    "haar_oct1_hf",
    "haar_oct2_upper_mid",
    "haar_oct3_mid",
    "haar_oct4_low_mid",
    "haar_oct5_fundamental",
    "discrete_jerk_d2",
    "comb_lag3",
    "comb_lag6",
    "comb_lag12",
    "broadband_crest_excess",
    "hf_peak_impulse_excess",
    "hf_top4_impulse_excess",
    "am_env_fast",
    "am_env_bpfo",
    "polarity_rub_asymmetry",
)


def _clamp_i32(val: int) -> int:
    if val > _INT32_MAX:
        return _INT32_MAX
    if val < _INT32_MIN:
        return _INT32_MIN
    return int(val)


def _combine_3axis_q16(fx: np.ndarray, fy: np.ndarray, fz: np.ndarray) -> np.ndarray:
    out = np.empty(NS_VIB_CHANNELS, dtype=np.int32)
    for c in range(NS_VIB_CHANNELS):
        a, b, d = int(fx[c]), int(fy[c]), int(fz[c])
        hi = max(a, b, d)
        lo = min(a, b, d)
        mid = a + b + d - hi - lo
        out[c] = _clamp_i32(hi + (mid >> 1) + (lo >> 2))
    return out


class VibrationFrontEnd:
    """16-channel chordotonal vibration & acoustic feature extractor.

    Parameters
    ----------
    window_size : int | None, default=None
        Expected number of samples per window (`N >= 16`). If `None` or `0`,
        any window length `N >= 16` is accepted dynamically.
    input_shift : int, default=6
        Left-shift applied in :meth:`extract_i16` when converting raw `int16`
        ADC / milli-g samples into `Q16.16` integers (`0 <= input_shift <= 15`).
    c_parity : bool, default=False
        If `True`, quantizes floating-point windows to `Q16.16` `int32` and
        executes exact integer math bit-identical to `ns_vib_extract_q16` in
        `csrc/necksense.c`.
    """

    n_channels: int = NS_VIB_CHANNELS
    channel_names: tuple[str, ...] = VIBRATION_CHANNEL_NAMES

    def __init__(
        self,
        window_size: int | None = None,
        *,
        n_samples: int | None = None,
        input_shift: int = 6,
        c_parity: bool = False,
    ) -> None:
        if window_size is not None and n_samples is not None and int(window_size) != int(n_samples):
            raise ValueError("window_size and n_samples must match when both are provided")
        eff_win = n_samples if window_size is None else window_size
        w_sz = 0 if eff_win is None else int(eff_win)
        if w_sz != 0 and w_sz < 16:
            raise ValueError("window_size must be >= 16 or None")
        if not (0 <= int(input_shift) <= 15):
            raise ValueError("input_shift must be in [0, 15]")

        self.window_size = w_sz
        self.n_samples = w_sz
        self.input_shift = int(input_shift)
        self.c_parity = bool(c_parity)

    def _check_len(self, n: int) -> None:
        if n < 16:
            raise ValueError(f"Vibration window length must be >= 16, got {n}")
        if self.window_size != 0 and n != self.window_size:
            raise ValueError(f"Expected window_size={self.window_size}, got {n}")

    def extract_q16(self, samples_q16: np.ndarray) -> np.ndarray:
        """Extract 16 `int32` Q16.16 features bit-identically to `ns_vib_extract_q16`."""
        arr = np.asarray(samples_q16, dtype=np.int32)
        if arr.ndim == 2 and arr.shape[1] == 3 and arr.shape[0] >= 16:
            fx = self.extract_q16(arr[:, 0])
            fy = self.extract_q16(arr[:, 1])
            fz = self.extract_q16(arr[:, 2])
            return _combine_3axis_q16(fx, fy, fz)
        if arr.ndim != 1:
            raise ValueError("samples_q16 must be 1D (N,) or 3-axis 2D (N, 3)")

        n = int(arr.shape[0])
        self._check_len(n)

        x_py = [int(v) for v in arr]
        mean_x = sum(x_py) // n
        u = [v - mean_x for v in x_py]

        abs_u = [abs(v) for v in u]
        sum_abs_u = sum(abs_u)
        max_abs_u = max(abs_u)

        pos_vals = [v for v in u if v > 0]
        neg_vals = [-v for v in u if v < 0]
        mean_pos_exc = (sum(pos_vals) // len(pos_vals)) if pos_vals else 0
        mean_neg_exc = (sum(neg_vals) // len(neg_vals)) if neg_vals else 0
        max_pos_u = max(pos_vals) if pos_vals else 0
        max_neg_u = max(neg_vals) if neg_vals else 0

        hf = [abs(u[t] - u[t - 1]) for t in range(1, n)]
        sum_hf = sum(hf)
        top4_sorted = sorted(hf, reverse=True)[:4]
        while len(top4_sorted) < 4:
            top4_sorted.append(0)

        sum_d2 = sum(abs(u[t] - 2 * u[t - 1] + u[t - 2]) for t in range(2, n))
        sum_c3 = sum(abs(u[t] - u[t - 3]) for t in range(3, n))
        sum_c6 = sum(abs(u[t] - u[t - 6]) for t in range(6, n))
        sum_c12 = sum(abs(u[t] - u[t - 12]) for t in range(12, n))

        mav_u = sum_abs_u // n
        mav_hf = sum_hf // (n - 1)
        top4_mean_hf = (top4_sorted[0] + top4_sorted[1] + top4_sorted[2] + top4_sorted[3]) >> 2
        asym_peak = abs(max_pos_u - max_neg_u)
        asym_exc = abs(mean_pos_exc - mean_neg_exc)

        out = np.zeros(NS_VIB_CHANNELS, dtype=np.int32)
        out[6] = _clamp_i32((sum_d2 // (n - 2)) >> 2)
        out[7] = _clamp_i32((sum_c3 // (n - 3)) >> 1)
        out[8] = _clamp_i32((sum_c6 // (n - 6)) >> 1)
        out[9] = _clamp_i32((sum_c12 // (n - 12)) >> 1)
        out[10] = _clamp_i32((max_abs_u - (mav_u << 1)) >> 1) if max_abs_u > (mav_u << 1) else 0
        out[11] = (
            _clamp_i32((top4_sorted[0] - (mav_hf << 1)) >> 1)
            if top4_sorted[0] > (mav_hf << 1)
            else 0
        )
        out[12] = (
            _clamp_i32((top4_mean_hf - (mav_hf << 1)) >> 1)
            if top4_mean_hf > (mav_hf << 1)
            else 0
        )
        out[15] = _clamp_i32((asym_peak >> 2) + asym_exc)

        # Shift-invariant 6-octave undecimated stationary Haar (`a trous`)
        pref = [0] * (n + 1)
        for t in range(n):
            pref[t + 1] = pref[t] + u[t]

        band_mav = [0] * 6
        for s in range(6):
            w = 1 << s
            span = w << 1
            if span <= n:
                cnt = n - span + 1
                sum_d = 0
                sh = s + 1
                for t in range(cnt):
                    dw = (2 * pref[t + w] - pref[t] - pref[t + span]) >> sh
                    sum_d += abs(dw)
                band_mav[s] = sum_d // cnt
            else:
                band_mav[s] = 0

        w_lp = 32 if n >= 64 else (n >> 1)
        shift_lp = 5 if n >= 64 else 3
        cnt_lp = n - w_lp + 1
        sum_lp = sum(abs((pref[t + w_lp] - pref[t]) >> shift_lp) for t in range(cnt_lp))
        band_mav[5] += sum_lp // cnt_lp

        for s in range(6):
            out[s] = _clamp_i32(band_mav[s])

        m_env = n - 3
        env = [0] * m_env
        for t in range(m_env):
            d0 = (2 * pref[t + 1] - pref[t] - pref[t + 2]) >> 1
            d1 = (2 * pref[t + 2] - pref[t] - pref[t + 4]) >> 2
            env[t] = abs(d0) + abs(d1)

        mean_env = sum(env) // m_env
        epref = [0] * (m_env + 1)
        for t in range(m_env):
            epref[t + 1] = epref[t] + (env[t] - mean_env)

        cnt_efast = m_env - 4 + 1
        sum_efast = sum(abs((2 * epref[t + 2] - epref[t] - epref[t + 4]) >> 2) for t in range(cnt_efast))
        out[13] = _clamp_i32(sum_efast // cnt_efast)

        w_mid = 6 if m_env >= 13 else 2
        span_mid = w_mid << 1
        cnt_emid = m_env - span_mid + 1
        sum_emid = sum(
            abs(2 * epref[t + w_mid] - epref[t] - epref[t + span_mid]) // span_mid
            for t in range(cnt_emid)
        )
        out[14] = _clamp_i32(sum_emid // cnt_emid)

        return out

    def extract_i16(self, samples_i16: np.ndarray) -> np.ndarray:
        """Extract 16 `int32` Q16.16 features from raw `int16` 1D `(N,)` or 3-axis `(N, 3)` data."""
        arr = np.asarray(samples_i16, dtype=np.int16)
        scaled = arr.astype(np.int32) << self.input_shift
        return self.extract_q16(scaled)

    def _extract_1d_float(self, x: np.ndarray) -> np.ndarray:
        n = int(x.shape[0])
        self._check_len(n)
        if self.c_parity:
            return q16_to_float(self.extract_q16(float_to_q16(x)))

        u = x - np.mean(x)
        abs_u = np.abs(u)
        mav_u = float(np.mean(abs_u))
        max_abs_u = float(np.max(abs_u))

        pos_mask = u > 0.0
        neg_mask = u < 0.0
        mean_pos_exc = float(np.mean(u[pos_mask])) if np.any(pos_mask) else 0.0
        mean_neg_exc = float(np.mean(-u[neg_mask])) if np.any(neg_mask) else 0.0
        max_pos_u = float(np.max(u[pos_mask])) if np.any(pos_mask) else 0.0
        max_neg_u = float(np.max(-u[neg_mask])) if np.any(neg_mask) else 0.0

        hf = np.abs(u[1:] - u[:-1])
        mav_hf = float(np.mean(hf))
        max_hf = float(np.max(hf))
        top4_hf = np.partition(hf, max(0, hf.size - 4))[-4:]
        top4_mean_hf = float(np.mean(top4_hf))

        out = np.zeros(NS_VIB_CHANNELS, dtype=np.float64)
        out[6] = 0.25 * float(np.mean(np.abs(u[2:] - 2.0 * u[1:-1] + u[:-2])))
        out[7] = 0.50 * float(np.mean(np.abs(u[3:] - u[:-3])))
        out[8] = 0.50 * float(np.mean(np.abs(u[6:] - u[:-6])))
        out[9] = 0.50 * float(np.mean(np.abs(u[12:] - u[:-12])))
        out[10] = max(0.0, 0.5 * (max_abs_u - 2.0 * mav_u))
        out[11] = max(0.0, 0.5 * (max_hf - 2.0 * mav_hf))
        out[12] = max(0.0, 0.5 * (top4_mean_hf - 2.0 * mav_hf))
        out[15] = 0.25 * abs(max_pos_u - max_neg_u) + abs(mean_pos_exc - mean_neg_exc)

        pref = np.empty(n + 1, dtype=np.float64)
        pref[0] = 0.0
        np.cumsum(u, out=pref[1:])

        for s in range(6):
            w = 1 << s
            span = w << 1
            if span <= n:
                dw = (2.0 * pref[w : n - w + 1] - pref[: n - span + 1] - pref[span:]) / float(span)
                out[s] = float(np.mean(np.abs(dw)))
            else:
                out[s] = 0.0

        w_lp = 32 if n >= 64 else (n >> 1)
        denom_lp = float(32 if n >= 64 else 8)
        avg_lp = (pref[w_lp:] - pref[: n - w_lp + 1]) / denom_lp
        out[5] += float(np.mean(np.abs(avg_lp)))

        m_env = n - 3
        d0 = 0.5 * np.abs(2.0 * pref[1 : m_env + 1] - pref[:m_env] - pref[2 : m_env + 2])
        d1 = 0.25 * np.abs(2.0 * pref[2 : m_env + 2] - pref[:m_env] - pref[4 : m_env + 4])
        env = d0 + d1
        eac = env - np.mean(env)

        epref = np.empty(m_env + 1, dtype=np.float64)
        epref[0] = 0.0
        np.cumsum(eac, out=epref[1:])

        ef = 0.25 * (2.0 * epref[2 : m_env - 1] - epref[: m_env - 3] - epref[4 : m_env + 1])
        out[13] = float(np.mean(np.abs(ef)))

        w_mid = 6 if m_env >= 13 else 2
        span_mid = w_mid << 1
        em = (2.0 * epref[w_mid : m_env - w_mid + 1] - epref[: m_env - span_mid + 1] - epref[span_mid:]) / float(span_mid)
        out[14] = float(np.mean(np.abs(em)))

        return out

    def extract(self, window: np.ndarray) -> np.ndarray:
        """Extract a `(16,)` feature vector from a 1D `(N,)` or 3-axis `(N, 3)` window."""
        arr = np.asarray(window, dtype=np.float64)
        if arr.ndim == 1:
            return self._extract_1d_float(arr)
        if arr.ndim == 2 and arr.shape[1] == 3 and arr.shape[0] >= 16:
            if self.c_parity:
                return q16_to_float(self.extract_q16(float_to_q16(arr)))
            fx = self._extract_1d_float(arr[:, 0])
            fy = self._extract_1d_float(arr[:, 1])
            fz = self._extract_1d_float(arr[:, 2])
            stacked = np.sort(np.vstack((fx, fy, fz)), axis=0)
            return stacked[2] + 0.5 * stacked[1] + 0.25 * stacked[0]
        raise ValueError("window must be 1D (N,) or 3-axis 2D (N, 3) with N >= 16")

    def transform(self, windows: np.ndarray) -> np.ndarray:
        """Extract features for a single window or batch `(B, N)` / `(B, N, 3)`."""
        arr = np.asarray(windows, dtype=np.float64)
        if arr.ndim == 1 or (arr.ndim == 2 and arr.shape[1] == 3 and arr.shape[0] >= 16):
            return self.extract(arr)
        if arr.ndim in (2, 3):
            return np.vstack([self.extract(arr[b]) for b in range(arr.shape[0])])
        raise ValueError("windows must be (N,), (N, 3), (B, N), or (B, N, 3)")

    def __call__(self, windows: np.ndarray) -> np.ndarray:
        return self.transform(windows)


VibrationAnalyzer = VibrationFrontEnd
