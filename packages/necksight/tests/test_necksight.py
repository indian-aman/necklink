"""Comprehensive unit & biophysical selectivity tests for NeckSight."""

from __future__ import annotations

import json
import math
from typing import List, Union

import numpy as np
import pytest

from necksight import (
    GiantFiberTrigger,
    LPLC2LoomingReflex,
    NeckSightPipeline,
    RetinaLaminaPreproc,
    T4T5MotionEngine,
    __version__,
    attach_nltp_reflex,
)
from necksight.cli import _make_clutter, main as cli_main


class MockNLTPClient:
    """Minimal mock matching pynltp.NLTPClient.send_reflex(urgent_msg)."""

    def __init__(self) -> None:
        self.sent_messages: List[Union[str, bytes]] = []

    def send_reflex(self, urgent_msg: Union[str, bytes]) -> int:
        self.sent_messages.append(urgent_msg)
        return len(self.sent_messages)


def test_version_exposed() -> None:
    from pathlib import Path

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject


def test_arbitrary_resolution_downsampling() -> None:
    pre64 = RetinaLaminaPreproc(grid_size=64)
    vga = np.arange(480 * 640, dtype=np.float32).reshape(480, 640) % 255.0
    out64 = pre64.step(vga)
    assert out64.photoreceptor.shape == (64, 64)
    assert out64.l1_on.shape == (64, 64)
    assert out64.l2_off.shape == (64, 64)

    # Non-divisible prime dimensions + RGB conversion
    pre32 = RetinaLaminaPreproc(grid_size=32)
    odd_rgb = np.full((137, 251, 3), 120, dtype=np.uint8)
    out32 = pre32.step(odd_rgb)
    assert out32.photoreceptor.shape == (32, 32)
    assert math.isclose(out32.mean_luminance, 120.0, rel_tol=1e-3)


def test_lamina_on_off_pathway_split() -> None:
    pre = RetinaLaminaPreproc(grid_size=32, optical_blur=False)
    base = np.full((32, 32), 80.0, dtype=np.float32)
    pre.step(base)

    # Brightening patch -> pure L1 (ON), zero L2 (OFF)
    bright_patch = base.copy()
    bright_patch[10:18, 10:18] = 220.0
    out_on = pre.step(bright_patch)
    assert float(np.mean(out_on.l1_on[10:18, 10:18])) > 0.5
    assert float(np.max(out_on.l2_off)) == 0.0

    # Holding bright patch steady -> monotonic decay, zero false OFF rebound
    out_hold = pre.step(bright_patch)
    assert float(np.max(out_hold.l2_off)) == 0.0

    # Darkening patch -> pure L2 (OFF), zero L1 (ON)
    pre.reset()
    pre.step(base)
    dark_patch = base.copy()
    dark_patch[10:18, 10:18] = 15.0
    out_off = pre.step(dark_patch)
    assert float(np.mean(out_off.l2_off[10:18, 10:18])) > 0.5
    assert float(np.max(out_off.l1_on)) == 0.0


def test_photoreceptor_100x_sunlight_glare_immunity() -> None:
    rng = np.random.default_rng(7)
    bg = _make_clutter(64, 64, rng)
    pipe = NeckSightPipeline(grid_size=64)

    for t in range(20):
        mult = 100.0 if t >= 8 else 1.0
        trig = pipe.step(bg * mult)
        assert not trig.triggered
        assert trig.looming_score < 0.005

    # Immediately after the 100x glare jump, detector is NOT blinded and catches looming
    yy, xx = np.meshgrid(np.arange(64, dtype=np.float32), np.arange(64, dtype=np.float32), indexing="ij")
    dist = np.hypot(xx - 31.5, yy - 31.5)
    fired_after_glare = False
    for t in range(1, 24):
        ttc = max((24 - t) / 60.0, 0.025)
        rad = (0.095 * 64.0) / ttc
        edge = np.clip((rad - dist) / 1.2 + 0.5, 0.0, 1.0)
        frame = (bg * 100.0) * (1.0 - edge) + (12.0 * 100.0) * edge
        trig = pipe.step(frame)
        fired_after_glare = fired_after_glare or trig.triggered
    assert fired_after_glare


def test_t4_on_polarity_selectivity() -> None:
    """A dark->bright moving wavefront must selectively excite T4 (ON) over T5 (OFF)."""
    ret = RetinaLaminaPreproc(grid_size=64)
    emd = T4T5MotionEngine(grid_size=64)
    xx = np.broadcast_to(np.arange(64, dtype=np.float32)[None, :], (64, 64))

    t4_px_sum = 0.0
    t4_nx_sum = 0.0
    t5_total_sum = 0.0
    for t in range(28):
        edge_pos = 8.0 + 1.5 * t
        # Bright region expands rightward from left: pixels only ever brighten (dark -> bright)
        frame = 30.0 + 180.0 * np.clip(edge_pos - xx, 0.0, 1.0)
        mot = emd.step(ret.step(frame))
        if t >= 3:
            t4_px_sum += float(np.sum(mot.t4["+X"]))
            t4_nx_sum += float(np.sum(mot.t4["-X"]))
            t5_total_sum += float(np.sum(mot.t5["+X"] + mot.t5["-X"]))

    assert t4_px_sum > 10.0
    assert t4_px_sum > 25.0 * (t4_nx_sum + 1e-6)
    assert t4_px_sum > 25.0 * (t5_total_sum + 1e-6)


def test_t5_off_polarity_selectivity() -> None:
    """A bright->dark moving wavefront must selectively excite T5 (OFF) over T4 (ON)."""
    ret = RetinaLaminaPreproc(grid_size=64)
    emd = T4T5MotionEngine(grid_size=64)
    xx = np.broadcast_to(np.arange(64, dtype=np.float32)[None, :], (64, 64))

    t5_px_sum = 0.0
    t5_nx_sum = 0.0
    t4_total_sum = 0.0
    for t in range(28):
        edge_pos = 8.0 + 1.5 * t
        # Dark curtain sweeps rightward across bright background (bright -> dark)
        frame = 210.0 - 180.0 * np.clip(edge_pos - xx, 0.0, 1.0)
        mot = emd.step(ret.step(frame))
        if t >= 3:
            t5_px_sum += float(np.sum(mot.t5["+X"]))
            t5_nx_sum += float(np.sum(mot.t5["-X"]))
            t4_total_sum += float(np.sum(mot.t4["+X"] + mot.t4["-X"]))

    assert t5_px_sum > 10.0
    assert t5_px_sum > 25.0 * (t5_nx_sum + 1e-6)
    assert t5_px_sum > 25.0 * (t4_total_sum + 1e-6)


@pytest.mark.parametrize(
    "direction,dx,dy",
    [
        ("+X", 1.2, 0.0),
        ("-X", -1.2, 0.0),
        ("+Y", 0.0, 1.2),
        ("-Y", 0.0, -1.2),
    ],
)
def test_four_cardinal_direction_selectivity(direction: str, dx: float, dy: float) -> None:
    ret = RetinaLaminaPreproc(grid_size=64)
    emd = T4T5MotionEngine(grid_size=64)
    yy, xx = np.meshgrid(np.arange(64, dtype=np.float32), np.arange(64, dtype=np.float32), indexing="ij")

    vx_acc = 0.0
    vy_acc = 0.0
    yaw_acc = 0.0
    for t in range(24):
        phase = (xx - dx * t) if dx != 0.0 else (yy - dy * t)
        frame = 128.0 + 85.0 * np.sin(2.0 * np.pi * phase / 12.0)
        mot = emd.step(ret.step(frame))
        if t >= 3:
            vx_acc += mot.vx_flow
            vy_acc += mot.vy_flow
            yaw_acc += mot.yaw_pan

    if direction == "+X":
        assert vx_acc > 2.0 and yaw_acc > 2.0 and abs(vy_acc) < 1e-4
    elif direction == "-X":
        assert vx_acc < -2.0 and yaw_acc < -2.0 and abs(vy_acc) < 1e-4
    elif direction == "+Y":
        assert vy_acc > 2.0 and abs(vx_acc) < 1e-4
    elif direction == "-Y":
        assert vy_acc < -2.0 and abs(vx_acc) < 1e-4


def test_static_contrast_flicker_rejection() -> None:
    """Stationary contrast flicker (grating and stationary bright disk) must not trigger motion/looming."""
    pipe = NeckSightPipeline(grid_size=64)
    yy, xx = np.meshgrid(np.arange(64, dtype=np.float32), np.arange(64, dtype=np.float32), indexing="ij")

    # 1. Stationary high-contrast flickering sinusoidal grating
    for t in range(36):
        contrast = math.sin(2.0 * math.pi * t / 6.0)
        frame = 128.0 + 95.0 * contrast * np.sin(2.0 * np.pi * xx / 10.0)
        _, mot, trig = pipe.step_full(frame)
        assert abs(mot.vx_flow) < 1e-3
        assert abs(mot.vy_flow) < 1e-3
        assert not trig.triggered
        assert trig.looming_score < 1e-3

    # 2. Stationary sharp binary pulsing bright disk (exact space-time separability -> 0.0)
    pipe.reset()
    dist = np.hypot(xx - 31.5, yy - 31.5)
    sharp_disk = (dist <= 12.0).astype(np.float32)
    for t in range(36):
        amp = 0.5 * (1.0 + math.sin(2.0 * math.pi * t / 7.0))
        frame = 30.0 + 200.0 * amp * sharp_disk
        _, _, trig = pipe.step_full(frame)
        assert not trig.triggered
        assert trig.looming_score < 0.025


def test_yaw_pan_and_diagonal_turn_rejection() -> None:
    rng = np.random.default_rng(101)
    pano = _make_clutter(256, 384, rng, base_scale=64.0)
    pipe = NeckSightPipeline(grid_size=64)

    # Pure fast horizontal yaw pan (3.5 px/frame)
    for t in range(40):
        ox = int(round(3.5 * t))
        frame = pano[64:128, ox : ox + 64]
        trig = pipe.step(frame)
        assert not trig.triggered

    # Violent diagonal yaw + pitch snap turn (3 px/frame X + 2 px/frame Y)
    pipe.reset()
    for t in range(40):
        ox = 3 * t
        oy = 2 * t
        frame = pano[oy : oy + 64, ox : ox + 64]
        trig = pipe.step(frame)
        assert not trig.triggered


def test_receding_and_cross_traffic_rejection() -> None:
    rng = np.random.default_rng(202)
    bg = _make_clutter(64, 64, rng)
    yy, xx = np.meshgrid(np.arange(64, dtype=np.float32), np.arange(64, dtype=np.float32), indexing="ij")
    pipe = NeckSightPipeline(grid_size=64)

    # Receding (shrinking) obstacle
    dist = np.hypot(xx - 31.5, yy - 31.5)
    for t in range(32):
        rad = max(2.0, 24.0 - 0.7 * t)
        edge = np.clip((rad - dist) / 1.2 + 0.5, 0.0, 1.0)
        frame = bg * (1.0 - edge) + 15.0 * edge
        trig = pipe.step(frame)
        assert not trig.triggered

    # Pure lateral cross-traffic (constant radius=8 px translating across scene)
    pipe.reset()
    for t in range(32):
        cx = 6.0 + 1.7 * t
        d_lat = np.hypot(xx - cx, yy - 31.5)
        edge = np.clip((8.0 - d_lat) / 1.2 + 0.5, 0.0, 1.0)
        frame = bg * (1.0 - edge) + 15.0 * edge
        trig = pipe.step(frame)
        assert not trig.triggered


@pytest.mark.parametrize("dark_obstacle", [True, False])
def test_head_on_looming_triggers_early_with_valid_ttc(dark_obstacle: bool) -> None:
    rng = np.random.default_rng(303)
    bg = _make_clutter(64, 64, rng)
    yy, xx = np.meshgrid(np.arange(64, dtype=np.float32), np.arange(64, dtype=np.float32), indexing="ij")
    dist = np.hypot(xx - 31.5, yy - 31.5)

    pipe = NeckSightPipeline(grid_size=64)
    dt = 1.0 / 60.0
    frames = 36
    obj_val = 12.0 if dark_obstacle else 242.0

    triggers: List[GiantFiberTrigger] = []
    first_frame = None
    for t in range(frames):
        ttc_true = max((frames - t) * dt, 0.025)
        rad = (0.095 * 64.0) / ttc_true
        edge = np.clip((rad - dist) / 1.2 + 0.5, 0.0, 1.0)
        frame = bg * (1.0 - edge) + obj_val * edge
        trig = pipe.step(frame, dt=dt)
        if trig.triggered:
            triggers.append(trig)
            if first_frame is None:
                first_frame = t

    assert first_frame is not None
    lead_time_ms = (frames - first_frame) * dt * 1000.0
    assert lead_time_ms >= 250.0
    assert all(0.005 <= tr.est_ttc_s <= 5.0 for tr in triggers)
    # Estimated TTC late in the approach should be smaller than early in the approach
    assert triggers[-1].est_ttc_s < triggers[0].est_ttc_s


def test_off_center_looming_focus_localization() -> None:
    rng = np.random.default_rng(404)
    bg = _make_clutter(64, 64, rng)
    yy, xx = np.meshgrid(np.arange(64, dtype=np.float32), np.arange(64, dtype=np.float32), indexing="ij")
    true_cx, true_cy = 44.0, 20.0
    dist = np.hypot(xx - true_cx, yy - true_cy)

    pipe = NeckSightPipeline(grid_size=64)
    dt = 1.0 / 60.0
    frames = 34
    trig_focus = None
    for t in range(frames):
        ttc_true = max((frames - t) * dt, 0.03)
        rad = (0.085 * 64.0) / ttc_true
        edge = np.clip((rad - dist) / 1.2 + 0.5, 0.0, 1.0)
        frame = bg * (1.0 - edge) + 10.0 * edge
        trig = pipe.step(frame, dt=dt)
        if trig.triggered and trig_focus is None:
            trig_focus = trig.focus_xy

    assert trig_focus is not None
    err_px = math.hypot(trig_focus[0] - true_cx, trig_focus[1] - true_cy)
    assert err_px <= 6.0


def test_nltp_reflex_bridge_callback() -> None:
    mock_cli = MockNLTPClient()
    reflex = LPLC2LoomingReflex(grid_size=64)
    attach_nltp_reflex(mock_cli, msg="LOOM_ESTOP", detector=reflex)

    ret = RetinaLaminaPreproc(grid_size=64)
    emd = T4T5MotionEngine(grid_size=64)

    # Yaw pan must dispatch 0 reflex packets
    yy, xx = np.meshgrid(np.arange(64, dtype=np.float32), np.arange(64, dtype=np.float32), indexing="ij")
    for t in range(20):
        frame = 128.0 + 80.0 * np.sin(2.0 * np.pi * (xx - 2.0 * t) / 14.0)
        trig = reflex.step(emd.step(ret.step(frame)))
        assert not trig.reflex_dispatched
    assert len(mock_cli.sent_messages) == 0

    # Head-on looming must immediately dispatch "LOOM_ESTOP"
    ret.reset()
    emd.reset()
    reflex.reset()
    dist = np.hypot(xx - 31.5, yy - 31.5)
    dispatched = False
    for t in range(32):
        ttc = max((32 - t) / 60.0, 0.025)
        rad = (0.095 * 64.0) / ttc
        frame = 200.0 - 180.0 * np.clip((rad - dist) / 1.2 + 0.5, 0.0, 1.0)
        trig = reflex.step(emd.step(ret.step(frame)))
        if trig.triggered:
            assert trig.reflex_dispatched
            dispatched = True
    assert dispatched
    assert len(mock_cli.sent_messages) >= 1
    assert mock_cli.sent_messages[0] == "LOOM_ESTOP"

    # Invalid client object must raise TypeError cleanly
    with pytest.raises(TypeError):
        attach_nltp_reflex(object(), msg="LOOM_ESTOP")


def test_pipeline_reset_determinism() -> None:
    rng = np.random.default_rng(505)
    frames = [_make_clutter(64, 64, rng) for _ in range(12)]
    pipe = NeckSightPipeline(grid_size=64)

    run1 = [pipe.step(f).looming_score for f in frames]
    pipe.reset()
    run2 = [pipe.step(f).looming_score for f in frames]
    assert run1 == run2


def test_cli_demo_and_benchmark_quick(capsys: pytest.CaptureFixture[str]) -> None:
    rc_demo = cli_main(["demo", "--grid", "64", "--frames", "28", "--json"])
    assert rc_demo == 0
    demo_data = json.loads(capsys.readouterr().out)
    by_name = {s["scenario"]: s for s in demo_data["scenarios"]}
    assert by_name["rightward_grating"]["triggered"] is False
    assert by_name["rightward_grating"]["mean_vx_flow"] > 0.0
    assert by_name["fast_camera_yaw_pan"]["triggered"] is False
    assert by_name["glare_flash_100x"]["triggered"] is False
    assert by_name["head_on_looming_obstacle"]["triggered"] is True

    rc_bench = cli_main(["benchmark-quick", "--grid", "64", "--sequences", "16", "--json"])
    assert rc_bench == 0
    bench_data = json.loads(capsys.readouterr().out)
    assert bench_data["auroc"] >= 0.98
    assert bench_data["false_alarm_rate"] == 0.0
    assert bench_data["throughput_fps"] > 100.0


def _yawing_scene(rng: np.random.Generator, peak_px: float, looming: bool, grid: int = 64, frames: int = 36):
    """Clutter seen by a camera oscillating in yaw (waves / gusts); optional dark obstacle on
    a collision course that moves with the scene. Returns frames and true per-frame shifts."""
    pano = _make_clutter(grid + 64, grid + 64, rng, base_scale=float(grid))
    period = 24.0
    amp = peak_px * period / (2.0 * math.pi)
    phase = float(rng.uniform(0.0, 2.0 * math.pi))
    yy, xx = np.meshgrid(np.arange(grid, dtype=np.float32), np.arange(grid, dtype=np.float32), indexing="ij")
    cx, cy = 31.5 + float(rng.uniform(-6, 6)), 31.5 + float(rng.uniform(-6, 6))
    rate = float(rng.uniform(0.075, 0.125)) * grid

    def offset(t: int) -> float:
        return amp * math.sin(2.0 * math.pi * t / period + phase)

    out, shifts = [], []
    for t in range(frames):
        o = offset(t)
        x0 = 32.0 + o
        ix = int(math.floor(x0))
        fx = x0 - ix
        bg = (1.0 - fx) * pano[32 : 32 + grid, ix : ix + grid] + fx * pano[32 : 32 + grid, ix + 1 : ix + 1 + grid]
        frame = bg
        if looming:
            rad = rate / max((frames - t) / 60.0, 0.025)
            edge = np.clip((rad - np.hypot(xx - (cx - o), yy - cy)) / 1.2 + 0.5, 0.0, 1.0)
            frame = bg * (1.0 - edge) + 15.0 * edge
        out.append(np.clip(frame + rng.normal(0.0, 4.0, frame.shape), 0, 255).astype(np.float32))
        shifts.append(-(o - offset(t - 1)))
    return out, shifts


def test_gyro_derotation_keeps_looming_detection_during_yaw_oscillation() -> None:
    """Regression for 1.1.0: without derotation, 3-4 px/frame yaw oscillation hid 30-50% of
    looming obstacles. With gyro-derived ego_shift_px every one is caught, with no false alarms."""
    rng = np.random.default_rng(11)
    detected_plain = detected_derot = false_alarms = 0
    n = 8
    for _ in range(n):
        frames, shifts = _yawing_scene(rng, peak_px=4.0, looming=True)
        plain = NeckSightPipeline(grid_size=64)
        derot = NeckSightPipeline(grid_size=64)
        detected_plain += any(plain.step(f).triggered for f in frames)
        detected_derot += any(derot.step(f, ego_shift_px=(s, 0.0)).triggered for f, s in zip(frames, shifts))
        frames, shifts = _yawing_scene(rng, peak_px=4.0, looming=False)
        pan_only = NeckSightPipeline(grid_size=64)
        false_alarms += any(pan_only.step(f, ego_shift_px=(s, 0.0)).triggered for f, s in zip(frames, shifts))
    assert detected_derot == n
    assert false_alarms == 0
    assert detected_plain < n  # documents the failure mode the feature fixes


def test_gyro_to_shift_px_conventions_and_shift_grid() -> None:
    from necksight import gyro_to_shift_px, shift_grid

    # 90 deg HFOV on a 64-px grid: focal = 32 px; 1 rad/s for 1/60 s -> 0.533 px
    dx, dy = gyro_to_shift_px(1.0, -0.5, 1.0 / 60.0, 90.0, 64)
    assert math.isclose(dx, 32.0 / 60.0, rel_tol=1e-9)
    assert math.isclose(dy, -16.0 / 60.0, rel_tol=1e-9)
    with pytest.raises(ValueError):
        gyro_to_shift_px(0.0, 0.0, 0.01, 0.0, 64)
    img = np.arange(64, dtype=np.float32).reshape(8, 8)
    moved = shift_grid(img, 2.0, 1.0)  # content moves right 2, down 1
    assert np.allclose(moved[3:, 4:], img[2:-1, 2:-2])
