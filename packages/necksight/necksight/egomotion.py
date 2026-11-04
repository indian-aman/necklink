"""Gyro-based ego-motion derotation for the NeckSight optic-lobe pipeline.

When the camera rotates, every pixel moves by roughly the same amount. Shifting the retina's
adaptation baseline and the T4/T5 delay lines by that per-frame image shift *before* motion
detection makes the background look still, so a looming obstacle's expansion is no longer
masked by the LPTC pan suppression. Use a gyro: estimating the shift from the images
themselves is biased by the looming object.
"""

from __future__ import annotations

import math
from typing import Tuple

import numpy as np


def shift_grid(arr: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Return ``out(y, x) = arr(y - dy, x - dx)`` with bilinear interpolation and edge replication."""
    a = np.asarray(arr, dtype=np.float32)
    h, w = a.shape
    if h < 2 or w < 2:
        return a.copy()
    ys = np.clip(np.arange(h, dtype=np.float32) - float(dy), 0.0, h - 1.0)
    xs = np.clip(np.arange(w, dtype=np.float32) - float(dx), 0.0, w - 1.0)
    y0 = np.minimum(ys.astype(np.intp), h - 2)
    x0 = np.minimum(xs.astype(np.intp), w - 2)
    fy = (ys - y0)[:, None]
    fx = (xs - x0)[None, :]
    a00 = a[np.ix_(y0, x0)]
    a01 = a[np.ix_(y0, x0 + 1)]
    a10 = a[np.ix_(y0 + 1, x0)]
    a11 = a[np.ix_(y0 + 1, x0 + 1)]
    top = a00 * (1.0 - fx) + a01 * fx
    bot = a10 * (1.0 - fx) + a11 * fx
    return (top * (1.0 - fy) + bot * fy).astype(np.float32)


def gyro_to_shift_px(
    yaw_rate_rad_s: float,
    pitch_rate_rad_s: float,
    dt: float,
    hfov_deg: float,
    grid_width: int,
    *,
    grid_height: int | None = None,
    vfov_deg: float | None = None,
) -> Tuple[float, float]:
    """Convert body rates to the per-frame image shift ``(dx, dy)`` in NeckSight grid pixels.

    Conventions (pinhole camera looking forward, image ``x`` right, ``y`` down):

    - positive ``yaw_rate_rad_s`` = turning **left** (counter-clockwise seen from above), which
      moves the scene **right** in the image, so ``dx > 0``;
    - positive ``pitch_rate_rad_s`` = nose **up**, which moves the scene **down**, so ``dy > 0``.

    If your IMU axes differ, flip the signs. The approximation ignores lens distortion and is
    exact at the image centre; it is good to a few percent for fields of view under ~90 deg.
    """
    if hfov_deg <= 0.0 or hfov_deg >= 180.0:
        raise ValueError("hfov_deg must be in (0, 180)")
    gh = grid_width if grid_height is None else int(grid_height)
    focal_x = 0.5 * grid_width / math.tan(math.radians(hfov_deg) / 2.0)
    if vfov_deg is None:
        focal_y = focal_x
    else:
        focal_y = 0.5 * gh / math.tan(math.radians(vfov_deg) / 2.0)
    return float(yaw_rate_rad_s) * dt * focal_x, float(pitch_rate_rad_s) * dt * focal_y
