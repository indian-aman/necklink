"""Comprehensive pytest suite for NeckCompass (E-PG ring attractor, R-neuron plasticity, CPU4/PFL3)."""

from __future__ import annotations

import ctypes
import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from neckcompass import (
    BANC_CONNECTOME_COUNTS,
    FanBodyNavigator,
    PlasticLandmarkRing,
    RingCompass,
    TiltCompensatedAHRS,
    TiltCompensatedCompass,
    __version__,
    basin_sigma_for_noise,
    circular_error_rad,
    synthesize_9dof_frame,
    wrap_angle,
)
from neckcompass.cli import main as cli_main


def test_version_and_angle_wrapping() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject
    for k in range(-5, 6):
        ang = 0.73 + 2.0 * math.pi * k
        assert math.isclose(float(wrap_angle(ang)), 0.73, abs_tol=1e-12)
    # Shortest signed difference across the +/-pi seam (+179 deg vs -179 deg == -2 deg)
    err = float(circular_error_rad(math.radians(179.0), math.radians(-179.0)))
    assert math.isclose(math.degrees(err), -2.0, abs_tol=1e-9)


@pytest.mark.parametrize("n_wedges", [16, 32])
def test_ring_compass_zero_discontinuity_across_pi_seam(n_wedges: int) -> None:
    compass = RingCompass(n_wedges=n_wedges, initial_heading_rad=math.radians(170.0))
    dt = 0.02
    omega = math.radians(25.0)  # Crosses +180 -> -180 smoothly over 0.8 s

    truth = math.radians(170.0)
    prev_est = compass.heading_rad
    for _ in range(50):
        truth = float(wrap_angle(truth + omega * dt))
        est = compass.update(omega, dt)
        # Step change must match omega*dt with zero discontinuity across +/-pi
        step_delta = float(circular_error_rad(est, prev_est))
        assert math.isclose(step_delta, omega * dt, abs_tol=1e-6)
        assert abs(math.degrees(float(circular_error_rad(est, truth)))) < 0.01
        prev_est = est

    # Sweep back in reverse across -180 -> +180
    for _ in range(50):
        truth = float(wrap_angle(truth - omega * dt))
        est = compass.update(-omega, dt)
        assert abs(math.degrees(float(circular_error_rad(est, truth)))) < 0.01


@pytest.mark.parametrize("n_wedges", [16, 32])
def test_ring_compass_full_360_gyro_integration(n_wedges: int) -> None:
    start = -1.234
    compass = RingCompass(n_wedges=n_wedges, initial_heading_rad=start)
    dt = 0.01
    steps = 1000
    omega = (2.0 * math.pi) / (steps * dt)  # Full 360-deg revolution in 10 s

    for _ in range(steps):
        compass.update(omega, dt)

    err_deg = abs(math.degrees(float(circular_error_rad(compass.heading_rad, start))))
    assert err_deg < 0.05
    assert 0.20 < compass.bump_amplitude <= 1.0
    # Half the ring is strictly silent due to Delta7 zero-mean inhibition
    assert np.sum(compass.wedges == 0.0) >= (n_wedges // 2 - 1)


def test_ring_compass_gyro_bias_anchoring_and_homeostasis() -> None:
    true_heading = -0.85
    compass = RingCompass(n_wedges=16, initial_heading_rad=true_heading)
    true_bias = math.radians(1.5)  # 1.5 deg/s steady drift (90 deg over 60 s unanchored!)
    dt = 0.02

    for _ in range(int(60.0 / dt)):
        compass.update(omega_rad_s=true_bias, dt=dt, mag_heading_rad=true_heading)

    err_deg = abs(math.degrees(float(circular_error_rad(compass.heading_rad, true_heading))))
    assert err_deg < 0.25
    assert math.isclose(compass.estimated_gyro_bias_rad_s, true_bias, abs_tol=math.radians(0.15))


@pytest.mark.parametrize("spike_deg", [65.0, 95.0, 125.0, -150.0])
def test_ring_compass_ferromagnetic_spike_rejection(spike_deg: float) -> None:
    true_heading = 0.42
    compass = RingCompass(n_wedges=16, initial_heading_rad=true_heading)
    dt = 0.02

    # Warm up steady state for 5 s with +0.8 deg/s gyro bias
    bias = math.radians(0.8)
    for _ in range(int(5.0 / dt)):
        compass.update(bias, dt, mag_heading_rad=true_heading)
    bias_before = compass.estimated_gyro_bias_rad_s

    # Inject a 1.8 s ferromagnetic hull/cable spike
    spiked_mag = float(wrap_angle(true_heading + math.radians(spike_deg)))
    max_err_deg = 0.0
    for _ in range(int(1.8 / dt)):
        est = compass.update(bias, dt, mag_heading_rad=spiked_mag)
        err_deg = abs(math.degrees(float(circular_error_rad(est, true_heading))))
        max_err_deg = max(max_err_deg, err_deg)

    assert max_err_deg < 1.2
    # Verify off-basin spike did not corrupt P-EN gyro bias estimate
    assert abs(compass.estimated_gyro_bias_rad_s - bias_before) < math.radians(0.05)


def test_ring_compass_dual_cue_sun_overrides_corrupted_mag() -> None:
    rng = np.random.default_rng(7)
    compass = RingCompass(n_wedges=16, initial_heading_rad=0.0)
    dt = 0.02
    truth = 0.0
    bias = math.radians(1.0)

    for k in range(int(20.0 / dt)):
        omega = 0.35 * math.sin(0.2 * k * dt)
        truth = float(wrap_angle(truth + omega * dt))
        sun_cue = float(wrap_angle(truth + rng.normal(0.0, math.radians(1.0))))
        corrupted_mag = float(wrap_angle(truth + math.radians(130.0)))
        est = compass.update(
            omega + bias,
            dt,
            mag_heading_rad=corrupted_mag,
            sun_heading_rad=sun_cue,
        )
        if k > 100:
            assert abs(math.degrees(float(circular_error_rad(est, truth)))) < 1.5


def test_ring_compass_sustained_coherent_reanchor() -> None:
    compass = RingCompass(n_wedges=16, initial_heading_rad=0.0, reanchor_seconds=3.0)
    dt = 0.05
    teleported_heading = math.radians(110.0)
    # Steady kinematically coherent magnetic cue at +110 deg for 3.5 s re-seeds the bump
    for _ in range(int(3.5 / dt)):
        compass.update(0.0, dt, mag_heading_rad=teleported_heading)
    assert abs(math.degrees(float(circular_error_rad(compass.heading_rad, teleported_heading)))) < 1.0


def test_plastic_landmark_ring_supervised_harbor_distortion() -> None:
    def harbor_warp(psi: float) -> float:
        return float(
            wrap_angle(
                psi
                + math.radians(18.0)
                + math.radians(22.0) * math.sin(psi - 0.5)
                + math.radians(16.0) * math.sin(2.0 * psi + 0.3)
            )
        )

    plastic = PlasticLandmarkRing(n_wedges=16, n_ring_neurons=48, learning_rate=6.0, r_kappa=80.0)
    dt = 0.02
    omega = 2.0 * math.pi / 8.0  # One full turn every 8 s

    # Measure raw uncalibrated RMSE across full circle
    test_angles = np.linspace(-math.pi, math.pi, 72, endpoint=False)
    raw_errs = [abs(math.degrees(float(circular_error_rad(harbor_warp(float(a)), float(a))))) for a in test_angles]
    raw_rmse = float(np.sqrt(np.mean(np.square(raw_errs))))
    assert raw_rmse > 20.0

    # Train online across 2 full rotations (16 s) with sun/visual reference cue
    psi = -math.pi
    for _ in range(int(16.0 / dt)):
        psi = float(wrap_angle(psi + omega * dt))
        plastic.update(omega, dt, harbor_warp(psi), reference_heading_rad=psi, learn=True)

    learned_errs = []
    for a in test_angles:
        mapped, conf = plastic.map_raw_heading(harbor_warp(float(a)))
        assert conf > 0.5
        learned_errs.append(abs(math.degrees(float(circular_error_rad(mapped, float(a))))))
    learned_rmse = float(np.sqrt(np.mean(np.square(learned_errs))))
    assert learned_rmse < 2.0


def test_plastic_landmark_ring_self_supervised_soft_iron_learning() -> None:
    """Learn a 24-deg soft-iron room distortion using ONLY gyro yaw rate + warped mag."""

    def soft_iron_warp(psi: float) -> float:
        return float(wrap_angle(psi + math.radians(24.0) * math.sin(2.0 * psi - 0.4)))

    compass = RingCompass(n_wedges=16, initial_heading_rad=0.0, bias_adapt_rate=0.0)
    plastic = PlasticLandmarkRing(
        n_wedges=16, n_ring_neurons=32, learning_rate=2.5, compass=compass
    )
    dt = 0.02
    omega = 2.0 * math.pi / 6.0  # Full rotation every 6 s

    test_angles = np.linspace(-math.pi, math.pi, 72, endpoint=False)
    raw_rmse = float(
        np.sqrt(
            np.mean(
                [
                    math.degrees(float(circular_error_rad(soft_iron_warp(float(a)), float(a)))) ** 2
                    for a in test_angles
                ]
            )
        )
    )
    assert raw_rmse > 16.0

    psi = 0.0
    for _ in range(int(18.0 / dt)):  # 3 rotations self-supervised
        psi = float(wrap_angle(psi + omega * dt))
        plastic.update(
            omega,
            dt,
            soft_iron_warp(psi),
            reference_heading_rad=None,
            learn=True,
            anchor_gain_scale=0.08,
        )

    post_rmse = float(
        np.sqrt(
            np.mean(
                [
                    math.degrees(
                        float(circular_error_rad(plastic.map_raw_heading(soft_iron_warp(float(a)))[0], float(a)))
                    )
                    ** 2
                    for a in test_angles
                ]
            )
        )
    )
    assert post_rmse < 3.5


def test_fan_body_navigator_closed_loop_square_and_opponent_cancellation() -> None:
    nav = FanBodyNavigator(n_columns=16)
    dt = 0.05
    speed = 2.0  # 2 m/s * 10 s = 20 m per side

    for leg_heading in [0.0, math.pi / 2.0, math.pi, -math.pi / 2.0]:
        for _ in range(int(10.0 / dt)):
            nav.update(speed, dt, heading_rad=leg_heading)

    dist_m, _ = nav.home_vector()
    assert dist_m < 1e-6
    pos_cols, neg_cols = nav.cpu4_rates
    # Opponent common-mode cancellation keeps column accumulators near zero at home!
    assert float(np.max(pos_cols)) < 1e-6
    assert float(np.max(neg_cols)) < 1e-6


def test_fan_body_navigator_pfl3_steering_and_180_antigoal_escape() -> None:
    nav = FanBodyNavigator(n_columns=16)
    # Move +20 m East -> home is due West (bearing = pi / -pi)
    nav.update(speed_m_s=20.0, dt=1.0, heading_rad=0.0)
    dist_m, home_bearing = nav.home_vector()
    assert math.isclose(dist_m, 20.0, rel_tol=1e-6)
    assert math.isclose(abs(home_bearing), math.pi, abs_tol=1e-6)

    # Facing +135 deg -> shortest turn to +180 deg is positive (+CCW)
    cmd_left = nav.steering_cmd(max_turn_rate=1.2, current_heading_rad=math.radians(135.0))
    assert 0.2 < cmd_left <= 1.2

    # Facing -135 deg -> shortest turn to -180 deg is negative (-CW)
    cmd_right = nav.steering_cmd(max_turn_rate=1.2, current_heading_rad=math.radians(-135.0))
    assert -1.2 <= cmd_right < -0.2

    # Facing exact 0 deg (180 deg away from home!) -> PFL2 breaks anti-goal saddle strongly
    cmd_anti = nav.steering_cmd(max_turn_rate=1.2, current_heading_rad=0.0)
    assert abs(cmd_anti) > 0.4


def test_fan_body_navigator_multileg_gps_blackout_return_with_spikes() -> None:
    rng = np.random.default_rng(123)
    compass = RingCompass(n_wedges=16)
    nav = FanBodyNavigator(n_columns=16, compass=compass)

    dt = 0.02
    speed = 1.8
    bias = math.radians(0.9)
    true_x, true_y, true_psi = 0.0, 0.0, 0.25
    compass.reset(true_psi)

    # 4-leg outbound survey (120 s = 216 m) with intermittent 130-deg ferromagnetic spikes
    legs = [(30.0, 0.0), (30.0, 0.04), (30.0, -0.05), (30.0, 0.02)]
    t = 0.0
    for dur, omega_leg in legs:
        for _ in range(int(dur / dt)):
            t += dt
            true_psi = float(wrap_angle(true_psi + omega_leg * dt))
            true_x += speed * math.cos(true_psi) * dt
            true_y += speed * math.sin(true_psi) * dt

            spike = math.radians(130.0) if (40.0 <= t <= 41.8 or 88.0 <= t <= 89.6) else 0.0
            mag = float(wrap_angle(true_psi + spike + rng.normal(0.0, math.radians(2.0))))
            omega_meas = omega_leg + bias + rng.normal(0.0, math.radians(1.0))
            nav.step_with_compass(omega_meas, speed, dt, mag_heading_rad=mag)

    assert math.hypot(true_x, true_y) > 80.0

    # Engage PFL3 GPS-denied return-to-home
    for _ in range(int(180.0 / dt)):
        t += dt
        est_dist, _ = nav.home_vector()
        if est_dist < 0.5:
            break
        turn_cmd = nav.steering_cmd(max_turn_rate=0.9)
        v_cmd = speed * min(1.0, max(0.2, est_dist / 3.0))
        true_psi = float(wrap_angle(true_psi + turn_cmd * dt))
        true_x += v_cmd * math.cos(true_psi) * dt
        true_y += v_cmd * math.sin(true_psi) * dt

        spike = -math.radians(120.0) if (145.0 <= t <= 146.5) else 0.0
        mag = float(wrap_angle(true_psi + spike + rng.normal(0.0, math.radians(2.0))))
        omega_meas = turn_cmd + bias + rng.normal(0.0, math.radians(1.0))
        nav.step_with_compass(omega_meas, v_cmd, dt, mag_heading_rad=mag)

    final_miss_m = math.hypot(true_x, true_y)
    assert final_miss_m < 2.5


def test_input_validation_guards() -> None:
    with pytest.raises(ValueError):
        RingCompass(n_wedges=7)
    with pytest.raises(ValueError):
        RingCompass(n_wedges=15)
    rc = RingCompass(n_wedges=16, initial_heading_rad=0.0)
    with pytest.raises(ValueError):
        rc.update(0.1, -0.01)

    nav = FanBodyNavigator(n_columns=16)
    with pytest.raises(RuntimeError):
        nav.step_with_compass(0.0, 1.0, 0.02)
    with pytest.raises(ValueError):
        nav.steering_cmd(max_turn_rate=0.0)


def test_cli_demo_and_benchmark_quick_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc1 = cli_main(["demo", "--seed", "42", "--wedges", "16", "--json"])
    assert rc1 == 0
    demo_out = json.loads(capsys.readouterr().out)
    assert demo_out["final_return_miss_m"] < 3.0
    assert demo_out["max_error_during_136deg_spike_deg"] < 8.0

    rc2 = cli_main(["benchmark-quick", "--seed", "101", "--json"])
    assert rc2 == 0
    bench_out = json.loads(capsys.readouterr().out)
    clean = bench_out["scenario_a_clean_gaussian"]
    spiky = bench_out["scenario_b_ferromagnetic_spikes"]
    assert clean["ekf"]["rmse_deg"] <= clean["neckcompass"]["rmse_deg"]
    assert spiky["neckcompass"]["rmse_deg"] < 0.25 * spiky["ekf"]["rmse_deg"]


def _spiky_stream(seed: int, mag_noise_deg: float, duration_s: float = 90.0):
    """Biased gyro + noisy magnetometer with 70-150 deg half-sine spikes every ~15 s."""
    rng = np.random.default_rng(seed)
    dt = 0.02
    psi = 0.3
    bias = math.radians(0.9)
    out = []
    next_spike = 8.0
    spike = None
    for k in range(int(duration_s / dt)):
        t = k * dt
        omega = 0.32 * math.sin(0.14 * t) + 0.19 * math.cos(0.08 * t)
        psi = float(wrap_angle(psi + omega * dt))
        anom = 0.0
        if spike is None and t >= next_spike:
            spike = (t, float(rng.uniform(1.0, 2.0)), math.radians(float(rng.uniform(70, 150))) * rng.choice([-1, 1]))
        if spike is not None:
            t0, dur, peak = spike
            if t <= t0 + dur:
                anom = peak * math.sin(math.pi * (t - t0) / dur)
            else:
                spike = None
                next_spike = t + float(rng.uniform(11, 19))
        gyro = omega + bias + rng.normal(0.0, math.radians(1.5))
        mag = float(wrap_angle(psi + anom + rng.normal(0.0, math.radians(mag_noise_deg))))
        out.append((t, psi, gyro, mag))
    return dt, out


@pytest.mark.parametrize("mag_noise_deg", [1.5, 3.0, 6.0])
def test_spiky_magnetometer_rmse_stays_near_sensor_noise_floor(mag_noise_deg: float) -> None:
    dt, stream = _spiky_stream(seed=7, mag_noise_deg=mag_noise_deg)
    compass = RingCompass(initial_heading_rad=stream[0][3], cue_noise_deg=mag_noise_deg)
    errs = []
    for t, psi, gyro, mag in stream:
        est = compass.update(gyro, dt, mag_heading_rad=mag)
        if t > 15.0:
            errs.append(math.degrees(float(circular_error_rad(est, psi))))
    rmse = float(np.sqrt(np.mean(np.square(errs))))
    # Spikes reach 150 deg; the compass must stay well below the raw sensor noise.
    assert rmse < 0.4 * mag_noise_deg + 0.3, rmse


def test_reanchor_works_with_realistic_magnetometer_noise() -> None:
    """Regression: re-anchoring used to compare per-sample d(mag)/dt with the gyro, so 3 deg of
    noise at 50 Hz (~3.7 rad/s apparent rate) blocked it forever."""
    rng = np.random.default_rng(3)
    dt = 0.02
    truth = math.radians(40.0)
    compass = RingCompass(initial_heading_rad=truth + math.radians(90.0), reanchor_seconds=10.0)
    for k in range(int(12.0 / dt)):
        omega = 0.2 * math.sin(0.3 * k * dt)
        truth = float(wrap_angle(truth + omega * dt))
        mag = float(wrap_angle(truth + rng.normal(0.0, math.radians(3.0))))
        compass.update(omega, dt, mag_heading_rad=mag)
    assert abs(math.degrees(float(circular_error_rad(compass.heading_rad, truth)))) < 3.0


def test_short_spike_never_triggers_reanchor() -> None:
    compass = RingCompass(initial_heading_rad=0.0, reanchor_seconds=3.0)
    dt = 0.02
    for _ in range(int(2.5 / dt)):  # 2.5 s spike < 3 s re-anchor window
        compass.update(0.0, dt, mag_heading_rad=math.radians(120.0))
    for _ in range(int(1.0 / dt)):
        compass.update(0.0, dt, mag_heading_rad=0.0)
    assert abs(compass.heading_deg) < 0.5


def test_basin_sigma_for_noise_half_width() -> None:
    for noise in (1.0, 3.0, 10.0):
        sigma = basin_sigma_for_noise(noise)
        half = math.radians(4.0 + noise)
        gate = math.exp(-(((1.0 - math.cos(half)) / sigma) ** 2))
        assert math.isclose(gate, 0.5, rel_tol=1e-9)
    with pytest.raises(ValueError):
        basin_sigma_for_noise(0.0)


def _build_c_core(tmp_path: Path):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler available")
    src = Path(__file__).resolve().parents[1] / "csrc" / "neckcompass.c"
    lib_path = tmp_path / "libneckcompass.so"
    subprocess.run([cc, "-O2", "-std=c99", "-shared", "-fPIC", "-o", str(lib_path), str(src)], check=True)
    lib = ctypes.CDLL(str(lib_path))
    lib.nc_compass_update.restype = ctypes.c_int16
    return lib


def test_c99_core_matches_python_on_spiky_stream(tmp_path: Path) -> None:
    lib = _build_c_core(tmp_path)
    bam_per_rad = 65536.0 / (2.0 * math.pi)

    def to_bam(rad: float) -> int:
        v = int(round(float(wrap_angle(rad)) * bam_per_rad))
        return ((v + 32768) % 65536) - 32768

    dt, stream = _spiky_stream(seed=11, mag_noise_deg=3.0)
    state = ctypes.create_string_buffer(256)
    lib.nc_compass_init(state)
    lib.nc_compass_reset(state, ctypes.c_int16(to_bam(stream[0][3])))
    py = RingCompass(initial_heading_rad=stream[0][3])
    c_errs, py_errs = [], []
    for t, psi, gyro, mag in stream:
        bam = lib.nc_compass_update(
            state, ctypes.c_int32(int(round(gyro * bam_per_rad))), ctypes.c_uint32(20),
            ctypes.c_int16(to_bam(mag)), ctypes.c_uint8(1), ctypes.c_int16(32767),
            ctypes.c_int16(0), ctypes.c_uint8(0), ctypes.c_int16(0),
        )
        est_py = py.update(gyro, dt, mag_heading_rad=mag)
        if t > 15.0:
            c_errs.append(math.degrees(float(circular_error_rad(bam / bam_per_rad, psi))))
            py_errs.append(math.degrees(float(circular_error_rad(est_py, psi))))
    c_rmse = float(np.sqrt(np.mean(np.square(c_errs))))
    py_rmse = float(np.sqrt(np.mean(np.square(py_errs))))
    assert c_rmse < 1.0, c_rmse
    assert abs(c_rmse - py_rmse) < 0.25, (c_rmse, py_rmse)


def test_plastic_self_supervised_stays_calibrated_with_noise_and_gyro_bias() -> None:
    """Regression: without an absolute reference the learned map used to absorb gyro drift
    (33 deg error after 60 s with 0.9 deg/s bias; 56 deg after 4 min with 2 deg mag noise)."""

    def warp(psi: float) -> float:
        return float(wrap_angle(psi + math.radians(20.0) * math.sin(psi) + math.radians(16.0) * math.sin(2.0 * psi)))

    rng = np.random.default_rng(5)
    plastic = PlasticLandmarkRing(n_wedges=16, n_ring_neurons=32)
    dt = 0.02
    psi = 0.0
    gyro_bias = math.radians(0.9)
    for k in range(int(240.0 / dt)):
        omega = 0.6 + 0.3 * math.sin(0.05 * k * dt)
        psi = float(wrap_angle(psi + omega * dt))
        plastic.update(
            omega + gyro_bias + rng.normal(0.0, math.radians(1.5)),
            dt,
            raw_mag_heading_rad=warp(psi) + rng.normal(0.0, math.radians(2.0)),
        )
    grid = np.linspace(-math.pi, math.pi, 72, endpoint=False)
    errs = [math.degrees(float(circular_error_rad(plastic.map_raw_heading(warp(float(a)))[0], float(a)))) for a in grid]
    assert float(np.sqrt(np.mean(np.square(errs)))) < 3.0


# ---------------------------------------------------------------------------
# NeckCompass v2.0.0 Tests (BANC v888, 9-DOF Tilt+Wave Gate, C99 Plasticity,
# PFL2 180-deg Anti-Goal Turn Boost, and Arduino SHA-256 Parity)
# ---------------------------------------------------------------------------


def test_banc_connectome_counts_and_delta7_antiphase_readout() -> None:
    assert __version__ == "2.0.0"
    assert BANC_CONNECTOME_COUNTS == {
        "EPG": 50,
        "PEN": 42,
        "Delta7": 40,
        "PFL3": 25,
        "PFL2": 12,
    }
    compass = RingCompass(n_wedges=16, initial_heading_rad=math.radians(45.0))
    d7 = compass.delta7_wedges
    assert d7.shape == (16,)
    assert float(np.min(d7)) >= 0.0
    assert float(np.max(d7)) <= 1.0 + 1e-12
    # Delta7 glutamatergic inhibition is strictly anti-phase to the E-PG bump heading
    expected = 0.5 * (1.0 - np.cos(compass.thetas - compass.heading_rad))
    np.testing.assert_allclose(d7, expected, atol=1e-12)


@pytest.mark.parametrize("dip_deg", [60.0, 65.0])
def test_tilt_compensated_ahrs_static_roll25_pitch20_under_steep_dip(dip_deg: float) -> None:
    roll_rad = math.radians(25.0)
    pitch_rad = math.radians(20.0)
    flat_errs_deg = []
    comp_errs_deg = []

    for yaw_deg in (-165.0, -120.0, -60.0, -15.0, 0.0, 37.0, 95.0, 150.0):
        yaw_rad = math.radians(yaw_deg)
        gx, gy, gz, ax, ay, az, mx, my, mz = synthesize_9dof_frame(
            yaw_rad, pitch_rad, roll_rad, dip_deg=dip_deg
        )
        # Flat 2D magnetometer suffers severe 15-50+ deg geomagnetic dip distortion when banked
        flat_mag = float(wrap_angle(math.atan2(-my, mx)))
        flat_errs_deg.append(abs(math.degrees(float(circular_error_rad(flat_mag, yaw_rad)))))

        tc = TiltCompensatedCompass()
        est = tc.update_9dof(gx, gy, gz, ax, ay, az, mx, my, mz, dt=0.02)
        comp_errs_deg.append(abs(math.degrees(float(circular_error_rad(est, yaw_rad)))))

        rc = RingCompass(n_wedges=16)
        est_rc = rc.update_9dof(gx, gy, gz, ax, ay, az, mx, my, mz, dt=0.02)
        assert abs(math.degrees(float(circular_error_rad(est_rc, yaw_rad)))) < 0.25

    assert max(flat_errs_deg) > 25.0
    assert max(comp_errs_deg) < 0.25


def test_tilt_compensation_wave_slam_gravity_gate_rejects_linear_accel() -> None:
    dt = 0.02
    rc = RingCompass(n_wedges=16)
    nav = FanBodyNavigator(n_columns=16, compass=rc)

    yaw = 0.25
    roll = math.radians(18.0)
    pitch = math.radians(-14.0)
    gated_steps = 0
    max_heading_err_deg = 0.0

    for k in range(250):
        t = k * dt
        roll_dot = 0.35 * math.cos(1.1 * t)
        pitch_dot = 0.28 * math.sin(0.9 * t)
        yaw_dot = 0.22 * math.sin(0.4 * t)
        if k > 0:
            roll = float(wrap_angle(roll + roll_dot * dt))
            pitch = float(np.clip(pitch + pitch_dot * dt, math.radians(-45.0), math.radians(45.0)))
            yaw = float(wrap_angle(yaw + yaw_dot * dt))

        # Inject heavy +0.85g lateral/vertical wave slam from t=1.5 s to t=2.5 s (|a - g| > 0.25g)
        slam = (2.5, 6.8, 4.5) if (1.5 <= t < 2.5) else (0.0, 0.0, 0.0)
        gx, gy, gz, ax, ay, az, mx, my, mz = synthesize_9dof_frame(
            yaw,
            pitch,
            roll,
            dip_deg=65.0,
            euler_rates_rad_s=(roll_dot, pitch_dot, yaw_dot),
            linear_accel_body=slam,
        )
        est, _, _ = nav.step_with_compass_9dof(gx, gy, gz, ax, ay, az, mx, my, mz, speed_m_s=1.5, dt=dt)
        if 1.5 <= t < 2.5:
            assert rc.tilt_ahrs.accel_gated is True
            gated_steps += 1
        err_deg = abs(math.degrees(float(circular_error_rad(est, yaw))))
        max_heading_err_deg = max(max_heading_err_deg, err_deg)

    assert gated_steps == 50
    assert max_heading_err_deg < 0.8


def test_plastic_landmark_ring_update_9dof_tilted_harbor_warp() -> None:
    def harbor_warp(psi: float) -> float:
        return float(wrap_angle(psi + math.radians(20.0) * math.sin(psi) + math.radians(15.0) * math.sin(2.0 * psi)))

    plastic = PlasticLandmarkRing(n_wedges=16, n_ring_neurons=32, learning_rate=3.5)
    dt = 0.02
    yaw = 0.0
    roll = math.radians(20.0)
    pitch = math.radians(-15.0)
    omega_yaw = 2.0 * math.pi / 8.0

    for _ in range(int(32.0 / dt)):
        yaw = float(wrap_angle(yaw + omega_yaw * dt))
        # Synthesize 9-DOF frame where horizontal magnetic azimuth has a 30-deg harbor warp
        gx, gy, gz, ax, ay, az, _, _, _ = synthesize_9dof_frame(
            yaw, pitch, roll, dip_deg=62.0, euler_rates_rad_s=(0.0, 0.0, omega_yaw)
        )
        _, _, _, _, _, _, mx, my, mz = synthesize_9dof_frame(
            harbor_warp(yaw), pitch, roll, dip_deg=62.0
        )
        plastic.update_9dof(gx, gy, gz, ax, ay, az, mx, my, mz, dt, learn=True, anchor_gain_scale=0.12)

    grid = np.linspace(-math.pi, math.pi, 72, endpoint=False)
    errs = [
        math.degrees(float(circular_error_rad(plastic.map_raw_heading(harbor_warp(float(a)))[0], float(a))))
        for a in grid
    ]
    assert float(np.sqrt(np.mean(np.square(errs)))) < 3.5


@pytest.mark.parametrize("anti_offset_deg", [179.0, -179.0, 179.9, -179.9, 180.0])
def test_pfl2_180_antigoal_turn_boost_no_hesitation(anti_offset_deg: float) -> None:
    nav = FanBodyNavigator(n_columns=16)
    # Displace 25 m along +X -> home bearing is pi (180 deg)
    nav.update(speed_m_s=25.0, dt=1.0, heading_rad=0.0)
    max_rate = 1.2
    # Heading error = wrap_angle(home_bearing - psi) = anti_offset_deg
    _, home_bearing = nav.home_vector()
    psi = float(wrap_angle(home_bearing - math.radians(anti_offset_deg)))
    cmd = nav.steering_cmd(max_turn_rate=max_rate, current_heading_rad=psi)

    # Must command > 80% of full turn rate immediately (zero sin(180)=0 hesitation!)
    assert abs(cmd) > 0.80 * max_rate
    if abs(anti_offset_deg) < 180.0:
        expected_sign = 1.0 if anti_offset_deg > 0.0 else -1.0
        assert math.copysign(1.0, cmd) == expected_sign

    # And on-course (0 deg error) must command exact 0.0 turn rate
    assert math.isclose(
        nav.steering_cmd(max_turn_rate=max_rate, current_heading_rad=home_bearing),
        0.0,
        abs_tol=1e-12,
    )


class _NcImu9(ctypes.Structure):
    _fields_ = [
        ("gx_bam_s", ctypes.c_int32),
        ("gy_bam_s", ctypes.c_int32),
        ("gz_bam_s", ctypes.c_int32),
        ("ax_mg", ctypes.c_int32),
        ("ay_mg", ctypes.c_int32),
        ("az_mg", ctypes.c_int32),
        ("mx", ctypes.c_int32),
        ("my", ctypes.c_int32),
        ("mz", ctypes.c_int32),
    ]


class _NcTilt(ctypes.Structure):
    _fields_ = [
        ("roll_sub_bam", ctypes.c_int32),
        ("pitch_sub_bam", ctypes.c_int32),
        ("tau_ms", ctypes.c_uint16),
        ("gate_tol_mg", ctypes.c_uint16),
        ("accel_gated", ctypes.c_uint8),
        ("initialized", ctypes.c_uint8),
    ]


def test_c99_v2_header_version_and_struct_sizes(tmp_path: Path) -> None:
    header = (Path(__file__).resolve().parents[1] / "csrc" / "neckcompass.h").read_text(encoding="utf-8")
    assert '#define NC_VERSION_STRING    "2.0.0"' in header
    assert "#define NC_BANC_EPG_COUNT    50" in header
    assert "#define NC_BANC_PFL2_COUNT   12" in header
    assert ctypes.sizeof(_NcImu9) == 36
    assert ctypes.sizeof(_NcTilt) == 16


def test_c99_tilt_9dof_dip65_and_wave_slam_ctypes(tmp_path: Path) -> None:
    lib = _build_c_core(tmp_path)
    lib.nc_tilt_update_9dof.restype = ctypes.c_int16
    lib.nc_compass_update_9dof.restype = ctypes.c_int16

    bam_per_rad = 65536.0 / (2.0 * math.pi)
    for yaw_deg in (-140.0, -35.0, 25.0, 115.0):
        yaw_rad = math.radians(yaw_deg)
        gx, gy, gz, ax, ay, az, mx, my, mz = synthesize_9dof_frame(
            yaw_rad, math.radians(20.0), math.radians(25.0), dip_deg=65.0, g_ref=1000.0, field_strength=50000.0
        )
        tilt = _NcTilt()
        lib.nc_tilt_init(ctypes.byref(tilt))
        out_yaw_rate = ctypes.c_int32(0)
        mag_bam = lib.nc_tilt_update_9dof(
            ctypes.byref(tilt),
            ctypes.c_int32(int(round(gx * bam_per_rad))),
            ctypes.c_int32(int(round(gy * bam_per_rad))),
            ctypes.c_int32(int(round(gz * bam_per_rad))),
            ctypes.c_int32(int(round(ax))),
            ctypes.c_int32(int(round(ay))),
            ctypes.c_int32(int(round(az))),
            ctypes.c_int32(int(round(mx))),
            ctypes.c_int32(int(round(my))),
            ctypes.c_int32(int(round(mz))),
            ctypes.c_uint32(20),
            ctypes.byref(out_yaw_rate),
        )
        err_deg = abs(math.degrees(float(circular_error_rad(mag_bam / bam_per_rad, yaw_rad))))
        assert err_deg < 0.5
        assert tilt.accel_gated == 0

        # Inject +850 mg wave slam -> must gate gravity update cleanly
        lib.nc_tilt_update_9dof(
            ctypes.byref(tilt),
            ctypes.c_int32(0),
            ctypes.c_int32(0),
            ctypes.c_int32(0),
            ctypes.c_int32(int(round(ax))),
            ctypes.c_int32(int(round(ay)) + 850),
            ctypes.c_int32(int(round(az))),
            ctypes.c_int32(int(round(mx))),
            ctypes.c_int32(int(round(my))),
            ctypes.c_int32(int(round(mz))),
            ctypes.c_uint32(20),
            ctypes.byref(out_yaw_rate),
        )
        assert tilt.accel_gated == 1


def test_c99_plastic_ring_self_supervised_and_supervised_ctypes(tmp_path: Path) -> None:
    lib = _build_c_core(tmp_path)
    lib.nc_plastic_ring_update.restype = ctypes.c_int16

    bam_per_rad = 65536.0 / (2.0 * math.pi)

    def to_bam(rad: float) -> int:
        v = int(round(float(wrap_angle(rad)) * bam_per_rad))
        return ((v + 32768) % 65536) - 32768

    def harbor_warp(psi: float) -> float:
        return float(wrap_angle(psi + math.radians(20.0) * math.sin(psi) + math.radians(16.0) * math.sin(2.0 * psi)))

    rng = np.random.default_rng(19)
    pr_buf = ctypes.create_string_buffer(1200)
    lib.nc_plastic_ring_init(pr_buf)

    dt = 0.02
    psi = 0.0
    bias = math.radians(0.9)
    for k in range(int(240.0 / dt)):
        omega = 0.6 + 0.3 * math.sin(0.05 * k * dt)
        psi = float(wrap_angle(psi + omega * dt))
        gyro = omega + bias + rng.normal(0.0, math.radians(1.5))
        raw_mag = harbor_warp(psi) + rng.normal(0.0, math.radians(2.0))
        lib.nc_plastic_ring_update(
            pr_buf,
            ctypes.c_int32(int(round(gyro * bam_per_rad))),
            ctypes.c_uint32(20),
            ctypes.c_int16(to_bam(raw_mag)),
            ctypes.c_int16(0),
            ctypes.c_uint8(0),
            ctypes.c_uint8(1),
        )

    grid = np.linspace(-math.pi, math.pi, 72, endpoint=False)
    errs = []
    corr_bam = ctypes.c_int16(0)
    conf_q15 = ctypes.c_int16(0)
    for a in grid:
        lib.nc_plastic_ring_map(
            pr_buf,
            ctypes.c_int16(to_bam(harbor_warp(float(a)))),
            ctypes.byref(corr_bam),
            ctypes.byref(conf_q15),
        )
        assert conf_q15.value > 8000
        errs.append(abs(math.degrees(float(circular_error_rad(corr_bam.value / bam_per_rad, float(a))))))
    assert float(np.mean(errs)) < 3.5


def test_c99_pfl2_180_antigoal_turn_boost_ctypes(tmp_path: Path) -> None:
    lib = _build_c_core(tmp_path)
    lib.nc_navigator_steering_cmd.restype = ctypes.c_int32
    lib.nc_cdeg_to_bam.restype = ctypes.c_int16
    lib.nc_cdeg_s_to_bam_s.restype = ctypes.c_int32
    lib.nc_bam_s_to_cdeg_s.restype = ctypes.c_int32

    nav_buf = ctypes.create_string_buffer(512)
    lib.nc_navigator_init(nav_buf, ctypes.c_int32(1500))
    # Move +20 m at 0 deg -> home is at 180 deg
    lib.nc_navigator_update(nav_buf, ctypes.c_int32(20000), ctypes.c_uint32(1000), ctypes.c_int16(0))

    max_rate_bam_s = lib.nc_cdeg_s_to_bam_s(ctypes.c_int32(6000))  # 60 deg/s
    # Heading = +1 deg -> err = +179 deg; Heading = -1 deg -> err = -179 deg; Heading = 0 deg -> err = 180 deg
    cmd_p179 = lib.nc_bam_s_to_cdeg_s(
        lib.nc_navigator_steering_cmd(nav_buf, lib.nc_cdeg_to_bam(100), max_rate_bam_s)
    )
    cmd_m179 = lib.nc_bam_s_to_cdeg_s(
        lib.nc_navigator_steering_cmd(nav_buf, lib.nc_cdeg_to_bam(-100), max_rate_bam_s)
    )
    cmd_180 = lib.nc_bam_s_to_cdeg_s(
        lib.nc_navigator_steering_cmd(nav_buf, ctypes.c_int16(0), max_rate_bam_s)
    )
    cmd_home = lib.nc_bam_s_to_cdeg_s(
        lib.nc_navigator_steering_cmd(nav_buf, ctypes.c_int16(-32768), max_rate_bam_s)
    )

    assert cmd_p179 > 4800
    assert cmd_m179 < -4800
    assert abs(cmd_180) > 4800
    assert abs(cmd_home) <= 5


def test_arduino_library_structure_and_c99_sha256_parity() -> None:
    import hashlib

    root = Path(__file__).resolve().parents[1]
    ard_dir = root / "arduino" / "NeckCompass"
    props_text = (ard_dir / "library.properties").read_text(encoding="utf-8")
    assert "name=NeckCompass" in props_text
    assert f"version={__version__}" in props_text
    assert "architectures=*" in props_text

    assert (ard_dir / "src" / "NeckCompass.h").is_file()
    assert (
        ard_dir
        / "examples"
        / "ESP32_9DOF_SelfCalibrating_Compass"
        / "ESP32_9DOF_SelfCalibrating_Compass.ino"
    ).is_file()

    for fname in ("neckcompass.h", "neckcompass.c"):
        h_csrc = hashlib.sha256((root / "csrc" / fname).read_bytes()).hexdigest()
        h_ard = hashlib.sha256((ard_dir / "src" / "csrc" / fname).read_bytes()).hexdigest()
        assert h_csrc == h_ard, f"SHA-256 mismatch for {fname}: {h_csrc} != {h_ard}"
