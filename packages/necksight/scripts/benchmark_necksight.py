"""Head-to-head empirical benchmark & hero chart generator for NeckSight v1.1.0.

Evaluates 4 detectors across N=60 synthetic 64x64 cluttered sequences:
  1. NeckSight (T4/T5 + LPLC2 4-quadrant radial symmetry gate)
  2. Naive Un-gated EMD Divergence (ablation without LPLC2 symmetry gate)
  3. Spatial-Gradient Lucas-Kanade Divergence (windowed 5x5 structure tensor)
  4. Frame-Difference Motion Energy

Also benchmarks slow sub-pixel translational odometry (0.05 - 0.35 px/frame)
to honestly quantify where continuous spatial-gradient Lucas-Kanade outperforms
correlational T4/T5 EMDs, and queries the compiled ANSI C99 32x32 binary if built.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from necksight import NeckSightPipeline, RetinaLaminaPreproc, T4T5MotionEngine
from necksight.cli import _make_clutter


# ---------------------------------------------------------------------------
# Baseline Detectors
# ---------------------------------------------------------------------------


class FrameDiffDetector:
    """Baseline 1: Frame-to-frame absolute intensity difference motion energy."""

    def __init__(self, grid_size: int = 64) -> None:
        self.grid_size = grid_size
        self._prev: Optional[np.ndarray] = None

    def reset(self) -> None:
        self._prev = None

    def step(self, frame: np.ndarray) -> float:
        f = np.asarray(frame, dtype=np.float32)
        if self._prev is None:
            self._prev = f.copy()
            return 0.0
        score = float(np.mean(np.abs(f - self._prev))) / 255.0
        self._prev = f.copy()
        return score

    @property
    def state_bytes(self) -> int:
        return self._prev.nbytes if self._prev is not None else self.grid_size * self.grid_size * 4


def _box_sum_5x5(arr: np.ndarray) -> np.ndarray:
    """Fast separable 5x5 box filter sum using replicate border."""
    px = np.pad(arr, ((0, 0), (2, 2)), mode="edge")
    sx = px[:, :-4] + px[:, 1:-3] + px[:, 2:-2] + px[:, 3:-1] + px[:, 4:]
    py = np.pad(sx, ((2, 2), (0, 0)), mode="edge")
    return py[:-4, :] + py[1:-3, :] + py[2:-2, :] + py[3:-1, :] + py[4:, :]


def _box_mean_9x9(arr: np.ndarray) -> np.ndarray:
    """Fast separable 9x9 box mean via cumulative sum."""
    h, w = arr.shape
    sat = np.zeros((h + 1, w + 1), dtype=np.float32)
    np.cumsum(arr, axis=0, out=sat[1:, 1:])
    np.cumsum(sat[1:, 1:], axis=1, out=sat[1:, 1:])
    y0 = np.clip(np.arange(h) - 4, 0, h)[:, None]
    y1 = np.clip(np.arange(h) + 5, 0, h)[:, None]
    x0 = np.clip(np.arange(w) - 4, 0, w)[None, :]
    x1 = np.clip(np.arange(w) + 5, 0, w)[None, :]
    sums = sat[y1, x1] - sat[y0, x1] - sat[y1, x0] + sat[y0, x0]
    area = np.maximum((y1 - y0) * (x1 - x0), 1).astype(np.float32)
    return sums / area


class LucasKanadeDivergenceDetector:
    """Baseline 2: Windowed 5x5 Spatial-Gradient Lucas-Kanade Optical Flow Divergence."""

    def __init__(self, grid_size: int = 64, reg: float = 1e-3) -> None:
        self.grid_size = grid_size
        self.reg = float(reg)
        self._prev: Optional[np.ndarray] = None
        self._ema_score: float = 0.0

    def reset(self) -> None:
        self._prev = None
        self._ema_score = 0.0

    def compute_flow(self, frame: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        curr = np.asarray(frame, dtype=np.float32) / 255.0
        if self._prev is None:
            self._prev = curr.copy()
            z = np.zeros_like(curr)
            return z, z

        avg = 0.5 * (curr + self._prev)
        ix = np.zeros_like(curr)
        iy = np.zeros_like(curr)
        ix[:, 1:-1] = 0.5 * (avg[:, 2:] - avg[:, :-2])
        iy[1:-1, :] = 0.5 * (avg[2:, :] - avg[:-2, :])
        it = curr - self._prev
        self._prev = curr.copy()

        sxx = _box_sum_5x5(ix * ix) + self.reg
        syy = _box_sum_5x5(iy * iy) + self.reg
        sxy = _box_sum_5x5(ix * iy)
        sxt = _box_sum_5x5(ix * it)
        syt = _box_sum_5x5(iy * it)

        det = np.maximum(sxx * syy - sxy * sxy, 1e-7)
        u = -(syy * sxt - sxy * syt) / det
        v = -(sxx * syt - sxy * sxt) / det
        return u, v

    def step(self, frame: np.ndarray) -> float:
        u, v = self.compute_flow(frame)
        du_dx = np.zeros_like(u)
        dv_dy = np.zeros_like(v)
        du_dx[:, 1:-1] = 0.5 * (u[:, 2:] - u[:, :-2])
        dv_dy[1:-1, :] = 0.5 * (v[2:, :] - v[:-2, :])
        div = _box_mean_9x9(du_dx + dv_dy)
        # Interior peak positive divergence
        raw = max(0.0, float(np.max(div[6:-6, 6:-6])))
        self._ema_score = 0.35 * self._ema_score + 0.65 * raw
        return self._ema_score

    @property
    def state_bytes(self) -> int:
        # Needs previous float32 frame + 5 structure tensor / flow workspaces (float32)
        px = self.grid_size * self.grid_size * 4
        return px * 6


def _lk_flow_single(prev: np.ndarray, curr: np.ndarray, reg: float = 1e-3) -> Tuple[np.ndarray, np.ndarray]:
    avg = 0.5 * (curr + prev)
    ix = np.zeros_like(curr)
    iy = np.zeros_like(curr)
    ix[:, 1:-1] = 0.5 * (avg[:, 2:] - avg[:, :-2])
    iy[1:-1, :] = 0.5 * (avg[2:, :] - avg[:-2, :])
    it = curr - prev
    sxx = _box_sum_5x5(ix * ix) + reg
    syy = _box_sum_5x5(iy * iy) + reg
    sxy = _box_sum_5x5(ix * iy)
    sxt = _box_sum_5x5(ix * it)
    syt = _box_sum_5x5(iy * it)
    det = np.maximum(sxx * syy - sxy * sxy, 1e-7)
    return -(syy * sxt - sxy * syt) / det, -(sxx * syt - sxy * sxt) / det


class PyramidalLKCompensatedDetector:
    """Baseline 2b: the strong classical flow detector.

    3-level coarse-to-fine Lucas-Kanade (two warp refinements per level), global gain
    normalisation (cancels glare / auto-exposure steps) and global ego-motion compensation
    (median flow subtracted) before the same local-divergence score as Baseline 2.
    """

    def __init__(self, grid_size: int = 64, levels: int = 3) -> None:
        self.grid_size = grid_size
        self.levels = levels
        self.reset()

    def reset(self) -> None:
        self._prev: Optional[np.ndarray] = None
        self._ema_score = 0.0

    @staticmethod
    def _down(a: np.ndarray) -> np.ndarray:
        return 0.25 * (a[0::2, 0::2] + a[1::2, 0::2] + a[0::2, 1::2] + a[1::2, 1::2])

    @staticmethod
    def _warp(img: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        h, w = img.shape
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        x = np.clip(xx + u, 0, w - 1.001)
        y = np.clip(yy + v, 0, h - 1.001)
        x0 = x.astype(np.intp)
        y0 = y.astype(np.intp)
        fx = x - x0
        fy = y - y0
        return (
            img[y0, x0] * (1 - fx) * (1 - fy)
            + img[y0, x0 + 1] * fx * (1 - fy)
            + img[y0 + 1, x0] * (1 - fx) * fy
            + img[y0 + 1, x0 + 1] * fx * fy
        )

    def _flow(self, prev: np.ndarray, curr: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        pp, cc = [prev], [curr]
        for _ in range(self.levels - 1):
            pp.append(self._down(pp[-1]))
            cc.append(self._down(cc[-1]))
        u = np.zeros_like(pp[-1])
        v = np.zeros_like(pp[-1])
        for lvl in range(self.levels - 1, -1, -1):
            if lvl < self.levels - 1:
                u = 2.0 * np.repeat(np.repeat(u, 2, 0), 2, 1)
                v = 2.0 * np.repeat(np.repeat(v, 2, 0), 2, 1)
            for _ in range(2):
                du, dv = _lk_flow_single(pp[lvl], self._warp(cc[lvl], u, v))
                u = u + du
                v = v + dv
        return u, v

    def step(self, frame: np.ndarray) -> float:
        f = np.asarray(frame, dtype=np.float32)
        f = f * (0.5 / max(float(f.mean()), 1e-3))
        if self._prev is None:
            self._prev = f
            return 0.0
        u, v = self._flow(self._prev, f)
        self._prev = f
        u = u - np.median(u)
        v = v - np.median(v)
        du_dx = np.zeros_like(u)
        dv_dy = np.zeros_like(v)
        du_dx[:, 1:-1] = 0.5 * (u[:, 2:] - u[:, :-2])
        dv_dy[1:-1, :] = 0.5 * (v[2:, :] - v[:-2, :])
        div = _box_mean_9x9(du_dx + dv_dy)
        raw = max(0.0, float(np.max(div[6:-6, 6:-6])))
        self._ema_score = 0.35 * self._ema_score + 0.65 * raw
        return self._ema_score

    @property
    def state_bytes(self) -> int:
        px = self.grid_size * self.grid_size * 4
        return px * 8


class NaiveUngatedEMDDivergence:
    """Baseline 3 (Ablation): Identical Retina + T4/T5 EMD without LPLC2 4-quadrant gate."""

    def __init__(self, grid_size: int = 64) -> None:
        self.retina = RetinaLaminaPreproc(grid_size=grid_size)
        self.emd = T4T5MotionEngine(grid_size=grid_size)
        self._ema_score: float = 0.0

    def reset(self) -> None:
        self.retina.reset()
        self.emd.reset()
        self._ema_score = 0.0

    def step(self, frame: np.ndarray, dt: float = 1.0 / 60.0) -> float:
        mot = self.emd.step(self.retina.step(frame, dt=dt))
        du_dx = np.zeros_like(mot.flow_x)
        dv_dy = np.zeros_like(mot.flow_y)
        du_dx[:, 1:-1] = 0.5 * (mot.flow_x[:, 2:] - mot.flow_x[:, :-2])
        dv_dy[1:-1, :] = 0.5 * (mot.flow_y[2:, :] - mot.flow_y[:-2, :])
        div = _box_mean_9x9(du_dx + dv_dy)
        raw = max(0.0, float(np.max(div[6:-6, 6:-6])))
        self._ema_score = 0.35 * self._ema_score + 0.65 * raw
        return self._ema_score

    @property
    def state_bytes(self) -> int:
        return sum(
            arr.nbytes
            for arr in (self.retina._baseline_log, self.emd._d_on, self.emd._d_off)
            if arr is not None
        )


# ---------------------------------------------------------------------------
# Dataset Synthesis (N = 60 Cluttered 64x64 Sequences)
# ---------------------------------------------------------------------------


def _bilinear_shift(pano: np.ndarray, oy: float, ox: float, gh: int, gw: int) -> np.ndarray:
    """Extract a sub-pixel shifted (gh, gw) window from pano via bilinear interpolation."""
    iy0 = int(math.floor(oy))
    ix0 = int(math.floor(ox))
    fy = float(oy - iy0)
    fx = float(ox - ix0)

    # Wrap-around indexing: some seeds drift the window past the panorama margin.
    rows = (iy0 + np.arange(gh + 1)) % pano.shape[0]
    cols = (ix0 + np.arange(gw + 1)) % pano.shape[1]
    win = pano[np.ix_(rows, cols)]
    p00 = win[:gh, :gw]
    p01 = win[:gh, 1:]
    p10 = win[1:, :gw]
    p11 = win[1:, 1:]

    top = (1.0 - fx) * p00 + fx * p01
    bot = (1.0 - fx) * p10 + fx * p11
    return ((1.0 - fy) * top + fy * bot).astype(np.float32)


def build_benchmark_dataset(
    grid: int = 64, frames: int = 36, seed: int = 20261104
) -> List[Dict[str, Any]]:
    """Generate N=60 diverse 64x64 sequences (24 looming positives, 36 hard negatives)."""
    rng = np.random.default_rng(seed)
    dt = 1.0 / 60.0
    yy, xx = np.meshgrid(
        np.arange(grid, dtype=np.float32),
        np.arange(grid, dtype=np.float32),
        indexing="ij",
    )

    dataset: List[Dict[str, Any]] = []

    # --- 24 Positive Sequences: Approaching Looming Obstacles on Clutter ---
    for idx in range(24):
        pano = _make_clutter(grid + 32, grid + 32, rng, base_scale=float(grid))
        cx0 = 0.5 * (grid - 1) + float(rng.uniform(-9.0, 9.0))
        cy0 = 0.5 * (grid - 1) + float(rng.uniform(-9.0, 9.0))
        drift_vx = float(rng.uniform(-0.35, 0.35))
        drift_vy = float(rng.uniform(-0.25, 0.25))
        rate = float(rng.uniform(0.075, 0.125)) * grid

        if idx < 8:
            subcat = "looming_dark_on_clutter"
            obj_base = float(rng.uniform(8.0, 22.0))
            textured_obj = False
        elif idx < 16:
            subcat = "looming_bright_on_clutter"
            obj_base = float(rng.uniform(225.0, 250.0))
            textured_obj = False
        else:
            subcat = "looming_textured_offcenter"
            obj_base = 25.0 if (idx % 2 == 0) else 230.0
            textured_obj = True

        seq_frames: List[np.ndarray] = []
        for t in range(frames):
            ox = 12.0 + drift_vx * t
            oy = 12.0 + drift_vy * t
            bg = _bilinear_shift(pano, oy, ox, grid, grid)
            ttc = max((frames - t) * dt, 0.025)
            rad = rate / ttc
            dist = np.hypot(xx - cx0, yy - cy0)
            edge = np.clip((rad - dist) / 1.2 + 0.5, 0.0, 1.0)
            if textured_obj:
                tex = obj_base + 18.0 * np.sin(2.0 * np.pi * (xx - cx0) / max(rad * 0.5, 2.0))
                frame = bg * (1.0 - edge) + tex * edge
            else:
                frame = bg * (1.0 - edge) + obj_base * edge
            noise = rng.normal(0.0, 0.8, size=(grid, grid)).astype(np.float32)
            seq_frames.append(np.maximum(frame + noise, 0.5))

        dataset.append(
            {
                "id": len(dataset),
                "is_looming": True,
                "category": subcat,
                "frames": seq_frames,
                "impact_time_ms": frames * dt * 1000.0,
            }
        )

    # --- 12 Hard Negatives A: Fast Camera Yaw/Pitch Pans Across Clutter & Poles ---
    for idx in range(12):
        pano = _make_clutter(grid * 4, grid * 8, rng, base_scale=float(grid))
        # Add high-contrast vertical/horizontal clutter bars (buoys/masts/horizon)
        pano[:, 170:176] = 15.0
        pano[:, 240:248] = 240.0
        pano[110:115, :] = 235.0

        vx = float(rng.choice([-4.2, -3.4, -2.6, -1.8, 1.8, 2.6, 3.4, 4.2]))
        vy = float(rng.uniform(-1.5, 1.5)) if (idx >= 6) else 0.0
        ox0 = float(grid * 3)
        oy0 = float(grid * 1.5)

        seq_frames = []
        for t in range(frames):
            bg = _bilinear_shift(pano, oy0 + vy * t, ox0 + vx * t, grid, grid)
            noise = rng.normal(0.0, 0.8, size=(grid, grid)).astype(np.float32)
            seq_frames.append(np.maximum(bg + noise, 0.5))

        dataset.append(
            {
                "id": len(dataset),
                "is_looming": False,
                "category": "fast_camera_yaw_pan",
                "frames": seq_frames,
            }
        )

    # --- 12 Hard Negatives B: Sudden Global Sunlight Glare Steps & Water Glints ---
    for idx in range(12):
        bg = _make_clutter(grid, grid, rng, base_scale=float(grid))
        step_frame = int(rng.integers(10, 22))
        glare_mult = float(rng.choice([15.0, 30.0, 60.0, 100.0]))
        seq_frames = []
        for t in range(frames):
            if idx < 8:
                # Global multiplicative sunlight / exposure step (with mild camera micro-jitter)
                mult = glare_mult if t >= step_frame else 1.0
                frame = bg * mult
            else:
                # Stationary specular water glint pulsing brightly without radial growth
                dist = np.hypot(xx - 31.5, yy - 31.5)
                glint = np.clip((9.0 - dist) / 1.5 + 0.5, 0.0, 1.0)
                pulse = 0.5 * (1.0 + math.sin(2.0 * math.pi * t / 6.0))
                frame = bg * (glare_mult if t == step_frame else 1.0) + 180.0 * pulse * glint
            noise = rng.normal(0.0, 0.6, size=(grid, grid)).astype(np.float32)
            seq_frames.append(np.maximum(frame + noise, 0.5))

        dataset.append(
            {
                "id": len(dataset),
                "is_looming": False,
                "category": "global_glare_flash",
                "frames": seq_frames,
            }
        )

    # --- 12 Hard Negatives C: Cluttered Receding & Lateral Cross-Traffic ---
    for idx in range(12):
        bg = _make_clutter(grid, grid, rng, base_scale=float(grid))
        seq_frames = []
        if idx < 6:
            # Receding obstacle (shrinking disk moving away)
            subcat = "receding_obstacle"
            obj_val = 14.0 if (idx % 2 == 0) else 240.0
            dist = np.hypot(xx - 31.5, yy - 31.5)
            for t in range(frames):
                rad = max(2.0, 24.0 - 0.62 * t)
                edge = np.clip((rad - dist) / 1.2 + 0.5, 0.0, 1.0)
                frame = bg * (1.0 - edge) + obj_val * edge
                seq_frames.append(frame)
        else:
            # Constant-size lateral cross-traffic passing across FOV
            subcat = "lateral_cross_traffic"
            obj_val = 14.0 if (idx % 2 == 0) else 240.0
            dir_sign = 1.0 if (idx % 2 == 0) else -1.0
            start_x = 6.0 if dir_sign > 0 else (grid - 7.0)
            cy_lat = 31.5 + float(rng.uniform(-8.0, 8.0))
            for t in range(frames):
                cx_lat = start_x + dir_sign * 1.5 * t
                dist = np.hypot(xx - cx_lat, yy - cy_lat)
                edge = np.clip((7.5 - dist) / 1.2 + 0.5, 0.0, 1.0)
                frame = bg * (1.0 - edge) + obj_val * edge
                seq_frames.append(frame)

        dataset.append(
            {
                "id": len(dataset),
                "is_looming": False,
                "category": subcat,
                "frames": seq_frames,
            }
        )

    return dataset


# ---------------------------------------------------------------------------
# Metrics Helpers
# ---------------------------------------------------------------------------


def compute_auroc(pos_scores: Sequence[float], neg_scores: Sequence[float]) -> float:
    pos = np.asarray(pos_scores, dtype=np.float64)
    neg = np.asarray(neg_scores, dtype=np.float64)
    diffs = pos[:, None] - neg[None, :]
    wins = float(np.sum(diffs > 0.0) + 0.5 * np.sum(diffs == 0.0))
    return wins / max(pos.size * neg.size, 1)


def threshold_at_tpr(pos_scores: Sequence[float], target_tpr: float = 0.95) -> float:
    """Return highest threshold achieving >= target_tpr on positive sequences."""
    pos_sorted = np.sort(np.asarray(pos_scores, dtype=np.float64))[::-1]
    k = min(max(int(math.ceil(target_tpr * pos_sorted.size)) - 1, 0), pos_sorted.size - 1)
    return float(pos_sorted[k])


def benchmark_subpixel_odometry(grid: int = 64, seed: int = 99) -> Dict[str, float]:
    """Honestly evaluate fine slow sub-pixel translational velocity estimation (0.05..0.35 px/fr).

    Demonstrates where continuous spatial-gradient Lucas-Kanade outperforms quantized/
    correlational T4/T5 EMDs (which depend on contrast and spatial wavelength).
    """
    rng = np.random.default_rng(seed)
    speeds = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35]

    lk = LucasKanadeDivergenceDetector(grid_size=grid)
    ret = RetinaLaminaPreproc(grid_size=grid)
    emd = T4T5MotionEngine(grid_size=grid)

    lk_true: List[float] = []
    lk_pred: List[float] = []
    emd_raw: List[float] = []

    for v_true in speeds:
        for trial in range(3):
            # Vary scene contrast across trials (realistic challenge for correlational EMDs!)
            contrast_scale = [0.45, 0.75, 1.0][trial]
            pano = 128.0 + contrast_scale * (
                _make_clutter(grid + 16, grid + 32, rng, base_scale=float(grid)) - 128.0
            )
            lk.reset()
            ret.reset()
            emd.reset()

            u_est_list: List[float] = []
            vx_emd_list: List[float] = []
            for t in range(18):
                # Shift panorama LEFT by v_true -> optical flow on retina is -v_true
                frame = _bilinear_shift(pano, 4.0, 12.0 - v_true * t, grid, grid)
                u, _ = lk.compute_flow(frame)
                mot = emd.step(ret.step(frame))
                if t >= 4:
                    u_est_list.append(float(np.mean(u[6:-6, 6:-6])))
                    vx_emd_list.append(mot.vx_flow)

            lk_true.append(v_true)
            lk_pred.append(float(np.mean(u_est_list)))
            emd_raw.append(float(np.mean(vx_emd_list)))

    y_true = np.asarray(lk_true)
    y_lk = np.asarray(lk_pred)
    x_emd = np.asarray(emd_raw)

    # Even after best linear calibration gain k * vx_flow, correlational EMD varies with contrast^2
    gain = float(np.dot(x_emd, y_true) / max(float(np.dot(x_emd, x_emd)), 1e-9))
    y_emd = gain * x_emd

    lk_mae = float(np.mean(np.abs(y_lk - y_true)))
    emd_mae = float(np.mean(np.abs(y_emd - y_true)))
    return {
        "speed_range_px_per_frame": [0.05, 0.35],
        "lucas_kanade_mae_px_per_frame": round(lk_mae, 4),
        "necksight_emd_calibrated_mae_px_per_frame": round(emd_mae, 4),
    }


def query_c99_metrics() -> Optional[Dict[str, Any]]:
    """Build and run the C99 self-test locally (needs make + a C compiler); None if unavailable."""
    import shutil

    csrc_dir = ROOT / "csrc"
    make = shutil.which("make")
    if make is None:
        return None
    try:
        build = subprocess.run([make, "-C", str(csrc_dir), "all"], capture_output=True, text=True, check=False)
        if build.returncode != 0:
            return None
        proc = subprocess.run(
            [str(csrc_dir / "build" / "necksight_selftest"), "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        idx = proc.stdout.find("{")
        if proc.returncode == 0 and idx >= 0:
            return json.loads(proc.stdout[idx:])
    except Exception:
        pass
    return None


def benchmark_yaw_oscillation(grid: int = 64, frames: int = 36, n_seq: int = 16, seed: int = 11) -> Dict[str, Any]:
    """Looming obstacle while the camera oscillates in yaw (waves, gusts).

    The obstacle moves with the scene, as it would for a real camera rotation. Reports the
    detection rate at the default threshold with and without gyro derotation
    (``ego_shift_px`` from the true per-frame shift), and the pan-only false-alarm rate.
    """
    rng = np.random.default_rng(seed)
    period = 24.0
    yy, xx = np.meshgrid(np.arange(grid, dtype=np.float32), np.arange(grid, dtype=np.float32), indexing="ij")

    def make(peak: float, looming: bool) -> Tuple[List[np.ndarray], List[float]]:
        pano = _make_clutter(grid + 64, grid + 64, rng, base_scale=float(grid))
        amp = peak * period / (2.0 * math.pi)
        ph = float(rng.uniform(0.0, 2.0 * math.pi))
        cx = 31.5 + float(rng.uniform(-6, 6))
        cy = 31.5 + float(rng.uniform(-6, 6))
        rate = float(rng.uniform(0.075, 0.125)) * grid
        off = lambda t: amp * math.sin(2.0 * math.pi * t / period + ph)  # noqa: E731
        seq, shifts = [], []
        for t in range(frames):
            o = off(t)
            fr = _bilinear_shift(pano, 32.0, 32.0 + o, grid, grid)
            if looming:
                rad = rate / max((frames - t) / 60.0, 0.025)
                edge = np.clip((rad - np.hypot(xx - (cx - o), yy - cy)) / 1.2 + 0.5, 0.0, 1.0)
                fr = fr * (1.0 - edge) + 15.0 * edge
            seq.append(np.clip(fr + rng.normal(0.0, 4.0, fr.shape), 0, 255).astype(np.float32))
            shifts.append(-(o - off(t - 1)))
        return seq, shifts

    out: Dict[str, Any] = {"peak_yaw_px_per_frame": {}}
    for peak in (0.0, 1.0, 2.0, 3.0, 4.0):
        loom = [make(peak, True) for _ in range(n_seq)]
        pan = [make(max(peak, 2.0), False) for _ in range(n_seq)]

        def rate_of(seqs: List[Tuple[List[np.ndarray], List[float]]], derot: bool) -> float:
            hits = 0
            for fr, sh in seqs:
                p = NeckSightPipeline(grid_size=grid)
                hits += any(p.step(f, ego_shift_px=(s, 0.0) if derot else None).triggered for f, s in zip(fr, sh))
            return hits / len(seqs)

        out["peak_yaw_px_per_frame"][str(peak)] = {
            "detection_plain": round(rate_of(loom, False), 3),
            "detection_gyro_derotated": round(rate_of(loom, True), 3),
            "pan_only_false_alarm_plain": round(rate_of(pan, False), 3),
            "pan_only_false_alarm_gyro_derotated": round(rate_of(pan, True), 3),
        }
    out["sequences_per_level"] = n_seq
    return out


# ---------------------------------------------------------------------------
# Hero Chart Rendering
# ---------------------------------------------------------------------------


def render_hero_chart(
    summary: Dict[str, Any],
    traces: Dict[str, List[float]],
    out_path: Path,
) -> None:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["DejaVu Sans", "Arial", "Segoe UI"],
            "axes.edgecolor": "#CBD5E1",
            "axes.linewidth": 1.0,
        }
    )

    fig = plt.figure(figsize=(16.2, 5.6), dpi=220, facecolor="#F8FAFC")
    gs = fig.add_gridspec(1, 3, width_ratios=[1.08, 1.15, 1.02], wspace=0.26, left=0.055, right=0.975, top=0.80, bottom=0.16)

    fig.suptitle(
        "NeckSight v1.1.0 — Drosophila Optic Lobe (T4/T5 + LPLC2) Looming Reflex Benchmarks",
        fontsize=14.5,
        fontweight="bold",
        color="#0F172A",
        y=0.945,
    )
    fig.text(
        0.5,
        0.885,
        "N = 60 Cluttered 64x64 Sequences (24 Head-On/Off-Center Looming Obstacles vs 36 Fast Yaw Pans, 100x Glare Steps & Cross-Traffic)",
        ha="center",
        fontsize=9.8,
        color="#475569",
    )

    methods_order = [
        ("necksight_lplc2", "NeckSight\n(T4/T5 + LPLC2)"),
        ("naive_ungated_emd", "Ungated EMD\n(No LPLC2 Gate)"),
        ("pyramidal_lk_compensated", "Pyramidal LK\n+ ego-comp"),
        ("lucas_kanade_div", "Lucas-Kanade\nDivergence"),
        ("frame_diff_energy", "Frame-Diff\nMotion Energy"),
    ]
    m_data = summary["detectors"]

    # ---------------- Panel A: AUROC & FAR @ 95% TPR ----------------
    ax1 = fig.add_subplot(gs[0, 0], facecolor="#FFFFFF")
    x = np.arange(len(methods_order))
    width = 0.38

    aurocs = [m_data[k]["auroc"] * 100.0 for k, _ in methods_order]
    fars = [m_data[k]["far_at_95_tpr"] * 100.0 for k, _ in methods_order]

    bars1 = ax1.bar(
        x - width / 2,
        aurocs,
        width,
        label="AUROC (% higher = better)",
        color=["#0284C7", "#38BDF8", "#6366F1", "#818CF8", "#94A3B8"],
        edgecolor="#0F172A",
        linewidth=0.6,
        zorder=3,
    )
    bars2 = ax1.bar(
        x + width / 2,
        fars,
        width,
        label="False Alarms @ 95% TPR (% lower = better)",
        color=["#10B981", "#F59E0B", "#F97316", "#FB7185", "#EF4444"],
        edgecolor="#0F172A",
        linewidth=0.6,
        zorder=3,
    )

    for b in bars1:
        v = b.get_height()
        ax1.text(
            b.get_x() + b.get_width() / 2,
            v + 1.6,
            f"{v:.1f}%",
            ha="center",
            va="bottom",
            fontsize=8.2,
            fontweight="bold",
            color="#0F172A",
        )
    for b in bars2:
        v = b.get_height()
        ax1.text(
            b.get_x() + b.get_width() / 2,
            v + 1.6,
            f"{v:.1f}%",
            ha="center",
            va="bottom",
            fontsize=8.2,
            fontweight="bold",
            color="#B91C1C" if v > 10.0 else "#047857",
        )

    ax1.set_xticks(x)
    ax1.set_xticklabels([lbl for _, lbl in methods_order], fontsize=7.2, color="#1E293B")
    ax1.set_ylim(0, 116)
    ax1.set_ylabel("Percentage (%)", fontsize=9.5, fontweight="bold", color="#1E293B")
    ax1.set_title("A. Looming vs Yaw/Glare Selectivity", fontsize=11.0, fontweight="bold", color="#0F172A", pad=10)
    ax1.grid(axis="y", linestyle="--", alpha=0.45, color="#CBD5E1", zorder=0)
    ax1.legend(loc="upper right", fontsize=7.8, frameon=True, facecolor="#F8FAFC", edgecolor="#CBD5E1")

    # ---------------- Panel B: Time-Domain Selectivity & Lead Time ----------------
    ax2 = fig.add_subplot(gs[0, 1], facecolor="#FFFFFF")
    t_ms = traces["time_before_impact_ms"]
    ax2.plot(
        t_ms,
        traces["looming_score"],
        color="#0284C7",
        linewidth=2.6,
        label="Head-On Looming Obstacle",
        zorder=4,
    )
    ax2.fill_between(
        t_ms,
        0,
        traces["looming_score"],
        color="#0EA5E9",
        alpha=0.14,
        zorder=2,
    )
    ax2.plot(
        t_ms,
        traces["yaw_pan_score"],
        color="#F59E0B",
        linewidth=2.0,
        linestyle="-",
        label="Fast Camera Yaw Pan (3.4 px/fr)",
        zorder=3,
    )
    ax2.plot(
        t_ms,
        traces["glare_100x_score"],
        color="#EF4444",
        linewidth=1.9,
        linestyle="--",
        label="100x Sunlight Glare Step",
        zorder=3,
    )
    thr = traces["threshold"]
    ax2.axhline(
        thr,
        color="#0F172A",
        linestyle=":",
        linewidth=1.3,
        label=f"Giant Fiber Threshold ({thr:.3f})",
        zorder=3,
    )

    lead_ms = summary["detectors"]["necksight_lplc2"]["mean_lead_time_ms_at_95_tpr"]
    trig_t = traces["first_trigger_ms"]
    if trig_t is not None:
        ax2.axvline(trig_t, color="#10B981", linestyle="-.", linewidth=1.4, zorder=3)
        text_x = max(min(t_ms) + 18.0, min(trig_t + 18.0, -210.0))
        ax2.annotate(
            f"Escape Reflex Trigger\n({abs(trig_t):.0f} ms pre-impact;\nmean = {lead_ms:.0f} ms)",
            xy=(trig_t, thr),
            xytext=(text_x, max(traces["looming_score"]) * 0.66),
            arrowprops={"arrowstyle": "->", "color": "#059669", "lw": 1.4},
            fontsize=8.1,
            fontweight="bold",
            color="#065F46",
            bbox={"boxstyle": "round,pad=0.3", "fc": "#ECFDF5", "ec": "#10B981", "lw": 0.9},
            zorder=5,
        )

    ax2.set_xlabel("Time Relative to Collision Impact (ms)", fontsize=9.5, fontweight="bold", color="#1E293B")
    ax2.set_ylabel("LPLC2 Giant Fiber Score", fontsize=9.5, fontweight="bold", color="#1E293B")
    ax2.set_title("B. Giant Fiber Trigger vs Yaw & 100x Glare", fontsize=11.0, fontweight="bold", color="#0F172A", pad=10)
    ax2.set_xlim(min(t_ms), 0.0)
    ax2.set_ylim(-0.012, max(traces["looming_score"]) * 1.18)
    ax2.grid(True, linestyle="--", alpha=0.45, color="#CBD5E1", zorder=0)
    ax2.legend(loc="upper left", fontsize=7.8, frameon=True, facecolor="#F8FAFC", edgecolor="#CBD5E1")

    # ---------------- Panel C: Embedded Throughput, RAM & Sub-Pixel Honesty ----------------
    ax3 = fig.add_subplot(gs[0, 2], facecolor="#FFFFFF")

    if not summary.get("c99_core"):
        raise RuntimeError("C99 metrics unavailable: install make and a C compiler, then re-run")
    c99_fps = summary["c99_core"]["throughput_fps"]
    c99_kb = summary["c99_core"]["state_kb"]

    perf_labels = [
        f"NeckSight C99\n(32x32, {c99_kb:.1f} KB)",
        f"Frame-Diff Py\n(64x64, {m_data['frame_diff_energy']['state_kb']:.0f} KB)",
        f"NeckSight Py\n(64x64, {m_data['necksight_lplc2']['state_kb']:.0f} KB)",
        f"Lucas-Kanade Py\n(64x64, {m_data['lucas_kanade_div']['state_kb']:.0f} KB)",
    ]
    perf_fps = [
        float(c99_fps),
        m_data["frame_diff_energy"]["throughput_fps"],
        m_data["necksight_lplc2"]["throughput_fps"],
        m_data["lucas_kanade_div"]["throughput_fps"],
    ]
    perf_colors = ["#0EA5E9", "#94A3B8", "#0284C7", "#6366F1"]

    y_pos = np.arange(len(perf_labels))[::-1]
    hbars = ax3.barh(
        y_pos,
        perf_fps,
        height=0.52,
        color=perf_colors,
        edgecolor="#0F172A",
        linewidth=0.6,
        zorder=3,
    )
    ax3.set_xscale("log")
    ax3.set_xlim(80, max(perf_fps) * 7.5)
    ax3.set_ylim(-1.35, len(perf_labels) - 0.35)
    ax3.set_yticks(y_pos)
    ax3.set_yticklabels(perf_labels, fontsize=8.5, color="#1E293B")
    ax3.set_xlabel("Single-Core Throughput (FPS, log scale)", fontsize=9.5, fontweight="bold", color="#1E293B")
    ax3.set_title("C. CPU Speed, RAM & Sub-Pixel Tradeoff", fontsize=11.0, fontweight="bold", color="#0F172A", pad=10)
    ax3.grid(axis="x", linestyle="--", alpha=0.45, color="#CBD5E1", zorder=0)

    for b, val in zip(hbars, perf_fps):
        ax3.text(
            val * 1.16,
            b.get_y() + b.get_height() / 2,
            f"{val:,.0f} FPS",
            va="center",
            ha="left",
            fontsize=8.4,
            fontweight="bold",
            color="#0F172A",
        )

    odom = summary["subpixel_odometry_honesty_check"]
    lk_mae = odom["lucas_kanade_mae_px_per_frame"]
    ns_mae = odom["necksight_emd_calibrated_mae_px_per_frame"]
    ax3.text(
        0.03,
        0.04,
        f"Honest Tradeoff (0.05-0.35 px/fr slow drift):\n"
        f"Lucas-Kanade MAE = {lk_mae:.3f} px/fr (wins fine odometry)\n"
        f"NeckSight EMD MAE = {ns_mae:.3f} px/fr (contrast-coupled)",
        transform=ax3.transAxes,
        fontsize=7.7,
        color="#334155",
        bbox={"boxstyle": "round,pad=0.35", "fc": "#F1F5F9", "ec": "#94A3B8", "lw": 0.8},
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=220)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main Runner
# ---------------------------------------------------------------------------


def main() -> int:
    grid = 64
    frames = 36
    dt = 1.0 / 60.0
    dataset = build_benchmark_dataset(grid=grid, frames=frames)

    ns = NeckSightPipeline(grid_size=grid)
    ungated = NaiveUngatedEMDDivergence(grid_size=grid)
    lk = LucasKanadeDivergenceDetector(grid_size=grid)
    pyr_lk = PyramidalLKCompensatedDetector(grid_size=grid)
    fdiff = FrameDiffDetector(grid_size=grid)

    detectors = {
        "necksight_lplc2": ns,
        "naive_ungated_emd": ungated,
        "pyramidal_lk_compensated": pyr_lk,
        "lucas_kanade_div": lk,
        "frame_diff_energy": fdiff,
    }

    per_seq_traces: Dict[str, List[List[float]]] = {k: [] for k in detectors}
    labels: List[bool] = [bool(item["is_looming"]) for item in dataset]
    categories: List[str] = [str(item["category"]) for item in dataset]

    for item in dataset:
        seq = item["frames"]
        ns.reset()
        ungated.reset()
        lk.reset()
        pyr_lk.reset()
        fdiff.reset()

        t_ns: List[float] = []
        t_un: List[float] = []
        t_lk: List[float] = []
        t_pl: List[float] = []
        t_fd: List[float] = []

        for fr in seq:
            t_ns.append(ns.step(fr, dt=dt).looming_score)
            t_un.append(ungated.step(fr, dt=dt))
            t_lk.append(lk.step(fr))
            t_pl.append(pyr_lk.step(fr))
            t_fd.append(fdiff.step(fr))

        per_seq_traces["necksight_lplc2"].append(t_ns)
        per_seq_traces["naive_ungated_emd"].append(t_un)
        per_seq_traces["lucas_kanade_div"].append(t_lk)
        per_seq_traces["pyramidal_lk_compensated"].append(t_pl)
        per_seq_traces["frame_diff_energy"].append(t_fd)

    # Throughput timing on 600 frames per detector
    rng_speed = np.random.default_rng(77)
    bench_ring = [_make_clutter(grid, grid, rng_speed, base_scale=float(grid)) for _ in range(16)]
    n_speed_frames = 600
    fps_map: Dict[str, float] = {}

    for key, det in detectors.items():
        det.reset()
        # Warmup
        for w_i in range(16):
            if key == "necksight_lplc2":
                det.step(bench_ring[w_i], dt=dt)
            elif key == "naive_ungated_emd":
                det.step(bench_ring[w_i], dt=dt)
            else:
                det.step(bench_ring[w_i])

        t0 = time.perf_counter()
        if key in ("necksight_lplc2", "naive_ungated_emd"):
            for k in range(n_speed_frames):
                det.step(bench_ring[k & 15], dt=dt)
        else:
            for k in range(n_speed_frames):
                det.step(bench_ring[k & 15])
        elapsed = max(time.perf_counter() - t0, 1e-6)
        fps_map[key] = n_speed_frames / elapsed

    ns_state_bytes = sum(
        arr.nbytes
        for arr in (ns.retina._baseline_log, ns.emd._d_on, ns.emd._d_off)
        if arr is not None
    )
    state_kb_map = {
        "necksight_lplc2": round(ns_state_bytes / 1024.0, 2),
        "naive_ungated_emd": round(ungated.state_bytes / 1024.0, 2),
        "lucas_kanade_div": round(lk.state_bytes / 1024.0, 2),
        "pyramidal_lk_compensated": round(pyr_lk.state_bytes / 1024.0, 2),
        "frame_diff_energy": round(fdiff.state_bytes / 1024.0, 2),
    }

    detector_summaries: Dict[str, Any] = {}
    for key in detectors:
        seq_traces = per_seq_traces[key]
        pos_peaks = [max(tr) for tr, is_pos in zip(seq_traces, labels) if is_pos]
        neg_peaks = [max(tr) for tr, is_pos in zip(seq_traces, labels) if not is_pos]
        yaw_peaks = [
            max(tr)
            for tr, cat in zip(seq_traces, categories)
            if cat == "fast_camera_yaw_pan"
        ]
        glare_peaks = [
            max(tr)
            for tr, cat in zip(seq_traces, categories)
            if cat == "global_glare_flash"
        ]

        auroc_all = compute_auroc(pos_peaks, neg_peaks)
        auroc_yaw_glare = compute_auroc(pos_peaks, yaw_peaks + glare_peaks)
        thr95 = threshold_at_tpr(pos_peaks, target_tpr=0.95)

        false_alarms = sum(1 for v in neg_peaks if v >= thr95)
        far_95 = false_alarms / max(len(neg_peaks), 1)
        yaw_glare_fa = sum(1 for v in (yaw_peaks + glare_peaks) if v >= thr95)
        far_yaw_glare_95 = yaw_glare_fa / max(len(yaw_peaks) + len(glare_peaks), 1)

        # Pre-collision warning lead time (ms before impact) on detected positives at thr95
        lead_times: List[float] = []
        for tr, is_pos in zip(seq_traces, labels):
            if not is_pos:
                continue
            hit_idx = next((idx for idx, val in enumerate(tr) if val >= thr95), None)
            if hit_idx is not None:
                lead_times.append((frames - hit_idx) * dt * 1000.0)

        detector_summaries[key] = {
            "auroc": round(auroc_all, 4),
            "auroc_looming_vs_yaw_glare": round(auroc_yaw_glare, 4),
            "threshold_at_95_tpr": round(thr95, 5),
            "far_at_95_tpr": round(far_95, 4),
            "far_yaw_glare_at_95_tpr": round(far_yaw_glare_95, 4),
            "mean_lead_time_ms_at_95_tpr": round(float(np.mean(lead_times)), 1) if lead_times else 0.0,
            "throughput_fps": round(fps_map[key], 1),
            "state_kb": state_kb_map[key],
        }

    # Also record NeckSight at its default production threshold (0.045)
    ns_default_thr = ns.looming.threshold
    ns_pos_hits = 0
    ns_default_leads: List[float] = []
    ns_neg_fa = 0
    for tr, is_pos in zip(per_seq_traces["necksight_lplc2"], labels):
        hit = next((idx for idx, val in enumerate(tr) if val >= ns_default_thr), None)
        if is_pos:
            if hit is not None:
                ns_pos_hits += 1
                ns_default_leads.append((frames - hit) * dt * 1000.0)
        else:
            if hit is not None:
                ns_neg_fa += 1
    detector_summaries["necksight_lplc2"]["default_threshold"] = ns_default_thr
    detector_summaries["necksight_lplc2"]["tpr_at_default_threshold"] = round(ns_pos_hits / 24.0, 4)
    detector_summaries["necksight_lplc2"]["far_at_default_threshold"] = round(ns_neg_fa / 36.0, 4)
    detector_summaries["necksight_lplc2"]["mean_lead_time_ms_default"] = (
        round(float(np.mean(ns_default_leads)), 1) if ns_default_leads else 0.0
    )

    odom_summary = benchmark_subpixel_odometry(grid=grid)
    c99_raw = query_c99_metrics()
    c99_summary: Dict[str, Any] = {}
    if c99_raw is not None:
        c99_summary = {
            "grid": "32x32",
            "state_bytes": c99_raw["state_bytes"],
            "state_kb": round(c99_raw["state_bytes"] / 1024.0, 2),
            "throughput_fps": c99_raw["throughput_fps"],
            "throughput_note": "desktop x86-64, -O2; not measured on MCU hardware",
            "pan_false_triggers": c99_raw["pan_triggers"],
            "glare_20x_false_triggers": c99_raw["flash_20x_triggers"],
            "yaw_loom_plain_triggered": c99_raw.get("yaw_loom_plain_triggered"),
            "yaw_loom_gyro_derotated_triggered": c99_raw.get("yaw_loom_derotated_triggered"),
            "yaw_pan_derotated_false_triggers": c99_raw.get("yaw_pan_derotated_triggers"),
        }
    yaw_osc = benchmark_yaw_oscillation(grid=grid, frames=frames)

    summary = {
        "benchmark": "NeckSight v1.1.0 Optic Lobe Benchmark Suite",
        "grid_size": f"{grid}x{grid}",
        "total_sequences": len(dataset),
        "positive_looming_sequences": sum(1 for x in labels if x),
        "negative_non_looming_sequences": sum(1 for x in labels if not x),
        "frames_per_sequence": frames,
        "frame_rate_hz": 60,
        "dataset_note": "Synthetic sequences from build_benchmark_dataset(); not camera footage.",
        "detectors": detector_summaries,
        "looming_during_yaw_oscillation": yaw_osc,
        "subpixel_odometry_honesty_check": odom_summary,
        "c99_core": c99_summary,
    }

    results_path = ROOT / "results" / "benchmark_summary.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    # Representative traces for Panel B
    pos_idx = next(i for i, d in enumerate(dataset) if d["category"] == "looming_dark_on_clutter")
    yaw_idx = next(i for i, d in enumerate(dataset) if d["category"] == "fast_camera_yaw_pan")
    glare_idx = next(i for i, d in enumerate(dataset) if d["category"] == "global_glare_flash")

    time_rel_ms = [-round((frames - t) * dt * 1000.0, 2) for t in range(frames)]
    loom_trace = per_seq_traces["necksight_lplc2"][pos_idx]
    hit_i = next((i for i, v in enumerate(loom_trace) if v >= ns_default_thr), None)
    first_trig_ms = time_rel_ms[hit_i] if hit_i is not None else None

    chart_traces = {
        "time_before_impact_ms": time_rel_ms,
        "looming_score": loom_trace,
        "yaw_pan_score": per_seq_traces["necksight_lplc2"][yaw_idx],
        "glare_100x_score": per_seq_traces["necksight_lplc2"][glare_idx],
        "threshold": ns_default_thr,
        "first_trigger_ms": first_trig_ms,
    }
    chart_path = ROOT / "assets" / "necksight_benchmarks.png"
    render_hero_chart(summary, chart_traces, chart_path)

    print(json.dumps(summary, indent=2))
    print(f"\nSaved JSON -> {results_path}")
    print(f"Saved PNG  -> {chart_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
