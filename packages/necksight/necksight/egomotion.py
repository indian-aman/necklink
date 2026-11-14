"""Gyro-based 2-DOF and 3-DOF ego-motion derotation for the NeckSight optic-lobe pipeline.

When the camera rotates in yaw, pitch, or roll, the stationary world shifts and rotates across
the retina. Warping the retina's adaptation baseline and the T4/T5 delay lines by that per-frame
ego-motion *before* motion detection makes the background look still, so a looming obstacle's
expansion is no longer masked by LPTC pan suppression or roll shear. Use an IMU gyro: estimating
ego-motion from the images themselves is biased by the approaching obstacle.
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


def warp_grid_3dof(
    grid: np.ndarray,
    dx: float,
    dy: float,
    dtheta_rad: float = 0.0,
) -> np.ndarray:
    """Warp ``grid`` by a 2D shift ``(dx, dy)`` and in-plane roll ``dtheta_rad`` around grid center.

    Models forward scene motion ``p_curr = center + R(dtheta_rad) @ (p_prev - center) + (dx, dy)``
    in image coordinates (``x`` right, ``y`` down) by evaluating the exact inverse Euclidean map
    with bilinear interpolation and clamped borders. Reduces identically to :func:`shift_grid`
    when ``dtheta_rad == 0``.
    """
    th = float(dtheta_rad)
    if abs(th) <= 1e-9:
        return shift_grid(grid, dx, dy)

    a = np.asarray(grid, dtype=np.float32)
    h, w = a.shape
    if h < 2 or w < 2:
        return a.copy()

    cx = 0.5 * (w - 1)
    cy = 0.5 * (h - 1)
    cos_t = math.cos(th)
    sin_t = math.sin(th)

    ux = (np.arange(w, dtype=np.float32) - cx - float(dx))[None, :]
    uy = (np.arange(h, dtype=np.float32) - cy - float(dy))[:, None]

    xs = np.clip(cx + ux * cos_t + uy * sin_t, 0.0, w - 1.0)
    ys = np.clip(cy - ux * sin_t + uy * cos_t, 0.0, h - 1.0)

    x0 = np.minimum(xs.astype(np.intp), w - 2)
    y0 = np.minimum(ys.astype(np.intp), h - 2)
    fx = xs - x0
    fy = ys - y0

    a00 = a[y0, x0]
    a01 = a[y0, x0 + 1]
    a10 = a[y0 + 1, x0]
    a11 = a[y0 + 1, x0 + 1]
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


def imu_to_ego_motion(
    yaw_rate_rad_s: float,
    pitch_rate_rad_s: float,
    roll_rate_rad_s: float = 0.0,
    dt: float = 1.0 / 60.0,
    hfov_deg: float = 70.0,
    grid_width: int = 64,
    *,
    grid_height: int | None = None,
    vfov_deg: float | None = None,
) -> Tuple[float, float, float]:
    """Convert 3-axis IMU angular rates into ``(dx, dy, dtheta_rad)`` for 3-DOF derotation.

    Conventions (pinhole camera looking forward, image ``x`` right, ``y`` down):

    - positive ``yaw_rate_rad_s`` = turning **left** -> scene moves right (``dx > 0``);
    - positive ``pitch_rate_rad_s`` = nose **up** -> scene moves down (``dy > 0``);
    - positive ``roll_rate_rad_s`` = rolling **counter-clockwise** looking forward (left wing down),
      which rotates the scene **clockwise** in image ``(x, y)`` coordinates (``dtheta_rad > 0``).
      If your IMU defines positive roll as right-wing-down, pass ``-roll_rate_rad_s``.
    """
    dx, dy = gyro_to_shift_px(
        yaw_rate_rad_s,
        pitch_rate_rad_s,
        dt=dt,
        hfov_deg=hfov_deg,
        grid_width=grid_width,
        grid_height=grid_height,
        vfov_deg=vfov_deg,
    )
    dtheta_rad = float(roll_rate_rad_s) * float(dt)
    return dx, dy, dtheta_rad
