"""Photoreceptor R1-R6 adaptation and Lamina L1/L2 ON-OFF split."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Union

import numpy as np


@dataclass(slots=True)
class LaminaOutput:
    """Output of the retina-lamina preprocessing stage for a single frame.

    Attributes:
        photoreceptor: Downsampled and log/Naka-Rushton adapted retinal grid (H_g, W_g).
        l1_on: Lamina L1 transient brightening (ON) pathway in [0, 1), shape (H_g, W_g).
        l2_off: Lamina L2 transient darkening (OFF) pathway in [0, 1), shape (H_g, W_g).
        global_step_log: Estimated wide-field log-illumination jump removed this step.
        mean_luminance: Raw spatial mean luminance of the downsampled frame.
    """

    photoreceptor: np.ndarray
    l1_on: np.ndarray
    l2_off: np.ndarray
    global_step_log: float
    mean_luminance: float


class RetinaLaminaPreproc:
    """Biophysical *Drosophila* R1-R6 photoreceptor + Lamina L1/L2 preprocessor.

    Performs:
      1. Fast exact box-area downsampling from arbitrary ``H x W`` grayscale (or RGB)
         frames to compact ``32 x 32`` or ``64 x 64`` grids.
      2. Ommatidial optical acceptance smoothing (``[1, 2, 1] / 4`` separable kernel)
         preventing spatial aliasing across adjacent columns.
      3. Weber-Fechner logarithmic + Naka-Rushton photoreceptor compression coupled
         with wide-field lamina global-step cancellation (surviving 100x sudden
         sunlight or auto-exposure glare steps without blinding or false transients).
      4. Temporal bandpass rectification into parallel ``L1`` (ON brightening) and
         ``L2`` (OFF darkening) pathways with monotonic adaptation (no false opposite-
         polarity rebound after a step edge).
    """

    def __init__(
        self,
        grid_size: Union[int, Tuple[int, int]] = 64,
        *,
        adapt_rate: float = 0.82,
        naka_rushton_sigma: float = 0.35,
        deadband: float = 0.002,
        dark_floor: Optional[float] = None,
        optical_blur: bool = True,
        glare_cancel: bool = True,
    ) -> None:
        if isinstance(grid_size, int):
            if grid_size < 8:
                raise ValueError("grid_size must be >= 8")
            self.grid_shape: Tuple[int, int] = (grid_size, grid_size)
        else:
            gh, gw = int(grid_size[0]), int(grid_size[1])
            if gh < 8 or gw < 8:
                raise ValueError("grid_size dimensions must be >= 8")
            self.grid_shape = (gh, gw)

        if not (0.0 < adapt_rate <= 1.0):
            raise ValueError("adapt_rate must be in (0.0, 1.0]")
        if naka_rushton_sigma <= 0.0:
            raise ValueError("naka_rushton_sigma must be > 0")
        if deadband < 0.0:
            raise ValueError("deadband must be >= 0")

        self.adapt_rate = float(adapt_rate)
        self.naka_rushton_sigma = float(naka_rushton_sigma)
        self.deadband = float(deadband)
        self._fixed_dark_floor = float(dark_floor) if dark_floor is not None else None
        self.optical_blur = bool(optical_blur)
        self.glare_cancel = bool(glare_cancel)

        self._dark_floor: Optional[float] = self._fixed_dark_floor
        self._baseline_log: Optional[np.ndarray] = None
        self._blur_tmp = np.empty(self.grid_shape, dtype=np.float32)
        self._blur_out = np.empty(self.grid_shape, dtype=np.float32)
        self._med_stride = max(1, min(self.grid_shape) // 16)

        self._bin_cache_key: Optional[Tuple[int, int, int, int]] = None
        self._y_starts: Optional[np.ndarray] = None
        self._x_starts: Optional[np.ndarray] = None
        self._inv_area: Optional[np.ndarray] = None

    def reset(self) -> None:
        """Clear photoreceptor adaptation history."""
        self._baseline_log = None
        self._dark_floor = self._fixed_dark_floor

    def downsample(self, frame: np.ndarray) -> np.ndarray:
        """Downsample an arbitrary ``H x W`` (or ``H x W x C``) frame to ``grid_shape``."""
        arr = np.asarray(frame)
        if arr.ndim == 3:
            if arr.shape[2] == 1:
                arr = arr[:, :, 0]
            elif arr.shape[2] >= 3:
                arr_f = arr.astype(np.float32, copy=False)
                arr = (
                    0.299 * arr_f[:, :, 0]
                    + 0.587 * arr_f[:, :, 1]
                    + 0.114 * arr_f[:, :, 2]
                )
            else:
                raise ValueError(f"Unsupported channel count in frame shape {arr.shape}")
        elif arr.ndim != 2:
            raise ValueError(f"Expected 2D or 3D image array, got shape {arr.shape}")

        h, w = arr.shape
        gh, gw = self.grid_shape

        if h == gh and w == gw:
            return np.maximum(arr.astype(np.float32, copy=False), 0.0)

        if h % gh == 0 and w % gw == 0:
            bh = h // gh
            bw = w // gw
            out = (
                arr.astype(np.float32, copy=False)
                .reshape(gh, bh, gw, bw)
                .mean(axis=(1, 3))
            )
            return np.maximum(out, 0.0)

        if h >= gh and w >= gw:
            key = (h, w, gh, gw)
            if self._bin_cache_key != key:
                y_edges = (np.arange(gh + 1, dtype=np.intp) * h) // gh
                x_edges = (np.arange(gw + 1, dtype=np.intp) * w) // gw
                dy = np.maximum(np.diff(y_edges), 1).astype(np.float32)
                dx = np.maximum(np.diff(x_edges), 1).astype(np.float32)
                self._y_starts = y_edges[:-1]
                self._x_starts = x_edges[:-1]
                self._inv_area = 1.0 / (dy[:, None] * dx[None, :])
                self._bin_cache_key = key

            arr_f = arr.astype(np.float32, copy=False)
            row_sums = np.add.reduceat(arr_f, self._y_starts, axis=0)
            box_sums = np.add.reduceat(row_sums, self._x_starts, axis=1)
            return np.maximum(box_sums * self._inv_area, 0.0)

        yi = (np.arange(gh, dtype=np.intp) * h) // gh
        xi = (np.arange(gw, dtype=np.intp) * w) // gw
        out = arr.astype(np.float32, copy=False)[yi[:, None], xi[None, :]]
        return np.maximum(out, 0.0)

    def _ommatidial_blur(self, grid: np.ndarray) -> np.ndarray:
        """Apply separable [1, 2, 1] / 4 optical acceptance smoothing in-place."""
        tmp = self._blur_tmp
        out = self._blur_out

        tmp[:, 1:-1] = 0.25 * (grid[:, :-2] + 2.0 * grid[:, 1:-1] + grid[:, 2:])
        tmp[:, 0] = 0.25 * (3.0 * grid[:, 0] + grid[:, 1])
        tmp[:, -1] = 0.25 * (grid[:, -2] + 3.0 * grid[:, -1])

        out[1:-1, :] = 0.25 * (tmp[:-2, :] + 2.0 * tmp[1:-1, :] + tmp[2:, :])
        out[0, :] = 0.25 * (3.0 * tmp[0, :] + tmp[1, :])
        out[-1, :] = 0.25 * (tmp[-2, :] + 3.0 * tmp[-1, :])
        return out

    def step(self, frame: np.ndarray, dt: float = 1.0 / 60.0) -> LaminaOutput:
        """Process one camera frame into adapted photoreceptor and L1/L2 ON/OFF maps."""
        _ = dt
        raw_grid = self.downsample(frame)
        mean_lum = float(np.mean(raw_grid))

        opt_grid = self._ommatidial_blur(raw_grid) if self.optical_blur else raw_grid

        if self._dark_floor is None:
            self._dark_floor = max(1e-3, 0.02 * (mean_lum + 1e-2))

        log_intensity = np.log(opt_grid + self._dark_floor, dtype=np.float32)

        if self._baseline_log is None:
            self._baseline_log = log_intensity.copy()
            zeros = np.zeros(self.grid_shape, dtype=np.float32)
            adapted = log_intensity - float(np.mean(log_intensity))
            return LaminaOutput(
                photoreceptor=adapted,
                l1_on=zeros,
                l2_off=zeros.copy(),
                global_step_log=0.0,
                mean_luminance=mean_lum,
            )

        dl_raw = log_intensity - self._baseline_log

        if self.glare_cancel:
            s = self._med_stride
            global_step = float(np.median(dl_raw[::s, ::s]))
        else:
            global_step = 0.0

        shifted_base = self._baseline_log + global_step
        delta_log = log_intensity - shifted_base

        # Update local photoreceptor adaptation state monotonically
        alpha = self.adapt_rate
        self._baseline_log = (1.0 - alpha) * shifted_base + alpha * log_intensity

        # Lamina L1 (ON brightening) & L2 (OFF darkening) Naka-Rushton half-wave split
        sigma = self.naka_rushton_sigma
        pos = np.maximum(0.0, delta_log - self.deadband)
        neg = np.maximum(0.0, -delta_log - self.deadband)

        l1_on = (pos / (pos + sigma)).astype(np.float32, copy=False)
        l2_off = (neg / (neg + sigma)).astype(np.float32, copy=False)
        adapted = (log_intensity - float(np.mean(log_intensity))).astype(np.float32, copy=False)

        return LaminaOutput(
            photoreceptor=adapted,
            l1_on=l1_on,
            l2_off=l2_off,
            global_step_log=global_step,
            mean_luminance=mean_lum,
        )

    def __call__(self, frame: np.ndarray, dt: float = 1.0 / 60.0) -> LaminaOutput:
        return self.step(frame, dt=dt)
