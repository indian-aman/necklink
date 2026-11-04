"""Comprehensive pytest suite for NeckCompass (E-PG ring attractor, R-neuron plasticity, CPU4/PFL3)."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from neckcompass import (
    FanBodyNavigator,
    PlasticLandmarkRing,
    RingCompass,
    __version__,
    circular_error_rad,
    wrap_angle,
)
from neckcompass.cli import main as cli_main


def test_version_and_angle_wrapping() -> None:
    assert __version__ == "1.0.0"
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
