"""Command-line interface for NeckSight (demo & benchmark-quick)."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from . import __version__
from .looming import NeckSightPipeline


def _make_clutter(
    h: int, w: int, rng: np.random.Generator, base_scale: float = 64.0
) -> np.ndarray:
    """Synthesize a multi-scale natural-like textured clutter array in [40, 210]."""
    yy, xx = np.meshgrid(
        np.arange(h, dtype=np.float32),
        np.arange(w, dtype=np.float32),
        indexing="ij",
    )
    bg = np.zeros((h, w), dtype=np.float32)
    for cycles in (1.0, 2.0, 4.0, 7.0):
        wl = base_scale / cycles
        phx = float(rng.uniform(0.0, 2.0 * np.pi))
        phy = float(rng.uniform(0.0, 2.0 * np.pi))
        amp = 1.0 / cycles
        bg += amp * np.sin(2.0 * np.pi * xx / wl + phx) * np.cos(
            2.0 * np.pi * yy / wl + phy
        )
    bg = (bg - float(np.min(bg))) / (float(np.ptp(bg)) + 1e-6)
    return (45.0 + 160.0 * bg).astype(np.float32)


def _run_demo(grid: int = 64, frames: int = 36, as_json: bool = False) -> Dict[str, Any]:
    dt = 1.0 / 60.0
    rng = np.random.default_rng(42)
    yy, xx = np.meshgrid(
        np.arange(grid, dtype=np.float32),
        np.arange(grid, dtype=np.float32),
        indexing="ij",
    )
    pano_w = grid + 4 * frames + 16
    pano = _make_clutter(grid * 2, pano_w, rng, base_scale=float(grid))

    scenarios: List[Dict[str, Any]] = []

    # 1. Rightward translating sinusoidal grating
    pipe = NeckSightPipeline(grid_size=grid)
    vx_vals: List[float] = []
    max_loom = 0.0
    trig_any = False
    for t in range(frames):
        frame = 128.0 + 90.0 * np.sin(2.0 * np.pi * (xx - 1.2 * t) / 12.0)
        _, mot, trig = pipe.step_full(frame, dt=dt)
        if t >= 3:
            vx_vals.append(mot.vx_flow)
        max_loom = max(max_loom, trig.looming_score)
        trig_any = trig_any or trig.triggered
    scenarios.append(
        {
            "scenario": "rightward_grating",
            "mean_vx_flow": round(float(np.mean(vx_vals)), 5),
            "peak_looming_score": round(max_loom, 5),
            "triggered": trig_any,
        }
    )

    # 2. Fast camera yaw pan across cluttered scene (continuous 3.0 px/frame)
    pipe.reset()
    vx_vals = []
    max_loom = 0.0
    trig_any = False
    for t in range(frames):
        ox = 3 * t
        frame = pano[grid // 2 : grid // 2 + grid, ox : ox + grid]
        _, mot, trig = pipe.step_full(frame, dt=dt)
        if t >= 3:
            vx_vals.append(mot.vx_flow)
        max_loom = max(max_loom, trig.looming_score)
        trig_any = trig_any or trig.triggered
    scenarios.append(
        {
            "scenario": "fast_camera_yaw_pan",
            "mean_vx_flow": round(float(np.mean(vx_vals)), 5),
            "peak_looming_score": round(max_loom, 5),
            "triggered": trig_any,
        }
    )

    # 3. 100x global sunlight glare step on cluttered scene
    pipe.reset()
    max_loom = 0.0
    trig_any = False
    crop = pano[:grid, :grid]
    for t in range(frames):
        mult = 100.0 if t >= frames // 2 else 1.0
        frame = crop * mult
        _, _, trig = pipe.step_full(frame, dt=dt)
        max_loom = max(max_loom, trig.looming_score)
        trig_any = trig_any or trig.triggered
    scenarios.append(
        {
            "scenario": "glare_flash_100x",
            "mean_vx_flow": 0.0,
            "peak_looming_score": round(max_loom, 5),
            "triggered": trig_any,
        }
    )

    # 4. Head-on approaching dark obstacle on cluttered background
    pipe.reset()
    max_loom = 0.0
    trig_any = False
    first_trig_frame: Optional[int] = None
    trig_ttc: float = float("inf")
    cy = 0.5 * (grid - 1)
    cx = 0.5 * (grid - 1)
    dist = np.hypot(xx - cx, yy - cy)
    for t in range(frames):
        ttc_true = max((frames - t) * dt, 0.025)
        radius = 0.09 * grid / ttc_true
        edge = np.clip((radius - dist) / 1.2 + 0.5, 0.0, 1.0)
        frame = crop * (1.0 - edge) + 12.0 * edge
        _, _, trig = pipe.step_full(frame, dt=dt)
        max_loom = max(max_loom, trig.looming_score)
        if trig.triggered and not trig_any:
            trig_any = True
            first_trig_frame = t
            trig_ttc = trig.est_ttc_s
    lead_ms = (
        round((frames - first_trig_frame) * dt * 1000.0, 1)
        if first_trig_frame is not None
        else 0.0
    )
    scenarios.append(
        {
            "scenario": "head_on_looming_obstacle",
            "peak_looming_score": round(max_loom, 5),
            "triggered": trig_any,
            "lead_time_ms": lead_ms,
            "est_ttc_at_trigger_s": round(trig_ttc, 3) if np.isfinite(trig_ttc) else None,
        }
    )

    # 5. Thin vertical pole looming off-center on the left (LC4 bar channel + DNa02 dodge)
    pipe.reset()
    max_loom = 0.0
    max_bar = 0.0
    trig_any = False
    trig_avoid_x = 0.0
    pole_cx = 0.32 * (grid - 1)
    dist_x = np.abs(xx - pole_cx)
    for t in range(frames):
        ttc_true = max((frames - t) * dt, 0.025)
        half_w = np.clip(0.03 * grid / ttc_true, 1.0, 0.14 * grid)
        edge = np.clip((half_w - dist_x) / 1.0 + 0.5, 0.0, 1.0)
        frame = crop * (1.0 - edge) + 12.0 * edge
        _, _, trig = pipe.step_full(frame, dt=dt)
        max_loom = max(max_loom, trig.looming_score)
        max_bar = max(max_bar, trig.bar_score)
        if trig.triggered and not trig_any:
            trig_any = True
            trig_avoid_x = trig.avoid_x
    scenarios.append(
        {
            "scenario": "thin_pole_looming_lc4",
            "peak_looming_score": round(max_loom, 5),
            "peak_bar_score": round(max_bar, 5),
            "triggered": trig_any,
            "avoid_x": round(trig_avoid_x, 3),
        }
    )

    summary = {"version": __version__, "grid": grid, "scenarios": scenarios}
    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"NeckSight v{__version__} -- Optic Lobe T4/T5 + LPLC2/LC4 Demo ({grid}x{grid})")
        print("-" * 74)
        for item in scenarios:
            name = item["scenario"]
            score = item["peak_looming_score"]
            trig_flag = "TRIGGERED" if item["triggered"] else "quiet (0 false alarm)"
            extra = ""
            if "lead_time_ms" in item:
                extra = (
                    f" | lead={item['lead_time_ms']} ms"
                    f" | est_ttc={item['est_ttc_at_trigger_s']} s"
                )
            elif "avoid_x" in item:
                extra = f" | bar={item['peak_bar_score']:.4f} | avoid_x={item['avoid_x']:+.2f}"
            elif "mean_vx_flow" in item:
                extra = f" | vx_flow={item['mean_vx_flow']:+.4f}"
            print(f"  {name:<26s} | score={score:.4f} | {trig_flag}{extra}")
    return summary


def _compute_auroc(pos_scores: Sequence[float], neg_scores: Sequence[float]) -> float:
    pos = np.asarray(pos_scores, dtype=np.float64)
    neg = np.asarray(neg_scores, dtype=np.float64)
    diffs = pos[:, None] - neg[None, :]
    wins = float(np.sum(diffs > 0.0) + 0.5 * np.sum(diffs == 0.0))
    return wins / max(pos.size * neg.size, 1)


def _run_benchmark_quick(
    grid: int = 64, sequences: int = 24, as_json: bool = False
) -> Dict[str, Any]:
    rng = np.random.default_rng(2026)
    dt = 1.0 / 60.0
    frames = 32
    n_pos = max(4, sequences // 2)
    n_neg = max(4, sequences - n_pos)

    pipe = NeckSightPipeline(grid_size=grid)
    yy, xx = np.meshgrid(
        np.arange(grid, dtype=np.float32),
        np.arange(grid, dtype=np.float32),
        indexing="ij",
    )

    pos_scores: List[float] = []
    lead_times_ms: List[float] = []
    for i in range(n_pos):
        pipe.reset()
        bg = _make_clutter(grid, grid, rng, base_scale=float(grid))
        cx = 0.5 * (grid - 1) + float(rng.uniform(-6.0, 6.0))
        cy = 0.5 * (grid - 1) + float(rng.uniform(-6.0, 6.0))
        dist = np.hypot(xx - cx, yy - cy)
        dark_obj = (i % 2) == 0
        obj_val = 14.0 if dark_obj else 240.0
        rate = float(rng.uniform(0.075, 0.115)) * grid
        peak = 0.0
        first_t: Optional[int] = None
        for t in range(frames):
            ttc = max((frames - t) * dt, 0.025)
            rad = rate / ttc
            edge = np.clip((rad - dist) / 1.2 + 0.5, 0.0, 1.0)
            frame = bg * (1.0 - edge) + obj_val * edge
            trig = pipe.step(frame, dt=dt)
            peak = max(peak, trig.looming_score)
            if trig.triggered and first_t is None:
                first_t = t
        pos_scores.append(peak)
        if first_t is not None:
            lead_times_ms.append((frames - first_t) * dt * 1000.0)

    neg_scores: List[float] = []
    false_alarms = 0
    pano_w = grid + 4 * frames + 32
    pano_h = grid + 2 * frames + 32
    for i in range(n_neg):
        pipe.reset()
        pano = _make_clutter(pano_h, pano_w, rng, base_scale=float(grid))
        peak = 0.0
        fired = False
        mode = i % 3
        speed = int(rng.choice([-3, -2, 2, 3]))
        start_x = 0 if speed > 0 else (-speed * frames + 4)
        for t in range(frames):
            if mode == 0:
                ox = start_x + speed * t
                oy = 4 + (t // 2)
                frame = pano[oy : oy + grid, ox : ox + grid]
            elif mode == 1:
                mult = 80.0 if t >= frames // 2 else 1.0
                frame = pano[:grid, :grid] * mult
            else:
                dist = np.hypot(xx - 0.5 * grid, yy - 0.5 * grid)
                rad = max(2.0, 22.0 - 0.65 * t)
                edge = np.clip((rad - dist) / 1.2 + 0.5, 0.0, 1.0)
                frame = pano[:grid, :grid] * (1.0 - edge) + 18.0 * edge
            trig = pipe.step(frame, dt=dt)
            peak = max(peak, trig.looming_score)
            fired = fired or trig.triggered
        neg_scores.append(peak)
        if fired:
            false_alarms += 1

    # Throughput timing on 600 frames
    pipe.reset()
    test_frames = [
        _make_clutter(grid, grid, rng, base_scale=float(grid)) for _ in range(16)
    ]
    n_bench = 600
    t0 = time.perf_counter()
    for k in range(n_bench):
        pipe.step(test_frames[k & 15], dt=dt)
    elapsed = max(time.perf_counter() - t0, 1e-6)
    fps = n_bench / elapsed

    state_bytes = sum(
        arr.nbytes
        for arr in (
            pipe.retina._baseline_log,
            pipe.emd._d_on,
            pipe.emd._d_off,
        )
        if arr is not None
    )

    auroc = _compute_auroc(pos_scores, neg_scores)
    far = false_alarms / max(n_neg, 1)
    mean_lead = float(np.mean(lead_times_ms)) if lead_times_ms else 0.0

    result = {
        "version": __version__,
        "grid": f"{grid}x{grid}",
        "sequences": n_pos + n_neg,
        "auroc": round(auroc, 4),
        "false_alarm_rate": round(far, 4),
        "mean_lead_time_ms": round(mean_lead, 1),
        "throughput_fps": round(fps, 1),
        "persistent_state_kb": round(state_bytes / 1024.0, 2),
    }

    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"NeckSight v{__version__} Quick Benchmark ({grid}x{grid}, N={n_pos + n_neg}):\n"
            f"  Looming vs Yaw/Glare AUROC : {result['auroc']:.4f}\n"
            f"  False-Alarm Rate (negatives): {result['false_alarm_rate'] * 100.0:.1f}%\n"
            f"  Mean Warning Lead Time     : {result['mean_lead_time_ms']:.1f} ms\n"
            f"  CPU Throughput (Python)    : {result['throughput_fps']:.1f} FPS\n"
            f"  Persistent State Memory    : {result['persistent_state_kb']:.2f} KB"
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="necksight",
        description="NeckSight: Drosophila Optic Lobe T4/T5 Motion, LPLC2/LC4 Looming & DNa02 Avoidance.",
    )
    parser.add_argument(
        "--version", action="version", version=f"necksight {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser(
        "demo", help="Run 4 synthetic optic-lobe scenarios (grating, yaw, glare, looming)."
    )
    p_demo.add_argument("--grid", type=int, default=64, help="Retinal grid size (default: 64)")
    p_demo.add_argument("--frames", type=int, default=36, help="Frames per scenario (default: 36)")
    p_demo.add_argument("--json", action="store_true", help="Emit JSON output")

    p_bench = sub.add_parser(
        "benchmark-quick", help="Run a fast accuracy & throughput benchmark."
    )
    p_bench.add_argument("--grid", type=int, default=64, help="Retinal grid size (default: 64)")
    p_bench.add_argument(
        "--sequences", type=int, default=24, help="Number of sequences (default: 24)"
    )
    p_bench.add_argument("--json", action="store_true", help="Emit JSON output")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "demo":
        _run_demo(grid=args.grid, frames=args.frames, as_json=args.json)
        return 0
    if args.command == "benchmark-quick":
        _run_benchmark_quick(
            grid=args.grid, sequences=args.sequences, as_json=args.json
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
