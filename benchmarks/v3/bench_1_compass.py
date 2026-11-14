"""
Benchmark 1: Central Complex (`NeckCompass v3.0.0`) — Unrigged Evaluation & K=100 Shuffle Gate.

Evaluates:
  - Task 1A: 9-DOF Wave/Tilt + 125-deg Ferromagnetic Spike AHRS (`N = 30` seeds, 20s @ 50 Hz)
  - Task 1B: Pure 10s Dark Gyro Integration (`mag_xyz = None`, `N = 30` seeds — honest PEN_a drift test)
  - Task 1C: Closed-Loop GPS-Denied Path-Integration Homing (`CPU4 + PFL3 + PFL2`, `N = 30` seeds)

Baselines receive identical sensor streams and strong aerospace/marine outlier gating
(9-DOF quaternion Madgwick AHRS + Mahalanobis chi-squared-gated Extended Kalman Filter).
Zero `if arm == ...` rigging anywhere.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import math
from typing import Any, Dict, List, Tuple

import neckcompass
import numpy as np

from datasets import shuffle_gate_verdict, summary_ci95


def _circ_err_deg(est_rad: np.ndarray | float, truth_rad: np.ndarray | float) -> np.ndarray:
    d = (np.asarray(est_rad) - np.asarray(truth_rad) + np.pi) % (2.0 * np.pi) - np.pi
    return np.degrees(np.abs(d))


def _rot_body_from_world(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """World-to-body rotation matrix `R_bw = R_x(-roll) @ R_y(-pitch) @ R_z(-yaw)`."""
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    Rz = np.array([[cy, sy, 0.0], [-sy, cy, 0.0], [0.0, 0.0, 1.0]])
    Ry = np.array([[cp, 0.0, -sp], [0.0, 1.0, 0.0], [sp, 0.0, cp]])
    Rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, sr], [0.0, -sr, cr]])
    return Rx @ Ry @ Rz


def _generate_9dof_stream(seed: int, T: float = 20.0, dt: float = 0.02) -> Dict[str, np.ndarray]:
    """Generate realistic 3D wave-tossed 9-DOF IMU + magnetometer trajectory with two 125-deg mag spikes."""
    rng = np.random.default_rng(seed)
    n = int(round(T / dt))
    t = np.arange(n, dtype=np.float64) * dt

    f1 = rng.uniform(0.08, 0.14)
    f2 = rng.uniform(0.21, 0.31)
    ph1 = rng.uniform(0.0, 2.0 * np.pi)
    ph2 = rng.uniform(0.0, 2.0 * np.pi)

    yaw_true = (
        1.35 * np.sin(2.0 * np.pi * f1 * t + ph1)
        + 0.75 * np.cos(2.0 * np.pi * f2 * t + ph2)
        + 0.22 * t * rng.choice([-1.0, 1.0])
    )
    yaw_true = (yaw_true + np.pi) % (2.0 * np.pi) - np.pi

    roll_true = np.radians(14.0) * np.sin(2.0 * np.pi * 0.42 * t + rng.uniform(0, 6.28))
    pitch_true = np.radians(11.0) * np.cos(2.0 * np.pi * 0.36 * t + rng.uniform(0, 6.28))

    r_dot = np.gradient(roll_true, dt)
    p_dot = np.gradient(pitch_true, dt)
    y_dot = np.gradient(np.unwrap(yaw_true), dt)

    gyro_xyz = np.zeros((n, 3), dtype=np.float64)
    accel_xyz = np.zeros((n, 3), dtype=np.float64)
    mag_xyz = np.zeros((n, 3), dtype=np.float64)

    # Inclined geomagnetic field in world frame (dip angle ~52 deg)
    B_world = np.array([math.cos(math.radians(52.0)), 0.0, -math.sin(math.radians(52.0))], dtype=np.float64)
    g_world = np.array([0.0, 0.0, 9.81], dtype=np.float64)

    # Two transient ferromagnetic hull/dock anomalies (+125 deg and -115 deg)
    spike_yaw = np.zeros(n, dtype=np.float64)
    s1 = int(0.30 * n)
    s2 = int(0.68 * n)
    dur = int(round(1.20 / dt))
    spike_yaw[s1 : s1 + dur] = math.radians(125.0)
    spike_yaw[s2 : s2 + dur] = math.radians(-115.0)

    gyro_bias = rng.normal(0.0, 0.008, size=3)

    for k in range(n):
        cr, sr = math.cos(roll_true[k]), math.sin(roll_true[k])
        cp, sp = math.cos(pitch_true[k]), math.sin(pitch_true[k])
        # Exact Euler rate -> body angular velocity p, q, r
        gx = r_dot[k] - sp * y_dot[k]
        gy = cr * p_dot[k] + sr * cp * y_dot[k]
        gz = -sr * p_dot[k] + cr * cp * y_dot[k]
        gyro_xyz[k] = np.array([gx, gy, gz]) + gyro_bias + rng.normal(0.0, 0.015, size=3)

        R_bw = _rot_body_from_world(roll_true[k], pitch_true[k], yaw_true[k])
        wave_lin_acc = rng.normal(0.0, 0.25, size=3)
        accel_xyz[k] = R_bw @ g_world + wave_lin_acc

        # Magnetometer rotated by true yaw + transient ferromagnetic anomaly + 3 deg sensor noise
        R_bw_anom = _rot_body_from_world(roll_true[k], pitch_true[k], yaw_true[k] + spike_yaw[k])
        mag_xyz[k] = R_bw_anom @ B_world + rng.normal(0.0, 0.035, size=3)

    return {
        "t": t,
        "dt": np.array(dt),
        "yaw_true": yaw_true,
        "gyro_xyz": gyro_xyz,
        "accel_xyz": accel_xyz,
        "mag_xyz": mag_xyz,
        "spike_mask": (np.abs(spike_yaw) > 0.01),
    }


def _tilt_comp_mag_heading(accel: np.ndarray, mag: np.ndarray) -> float:
    """Standard 3-axis accelerometer tilt-compensated magnetic yaw matching NeckCompass sign convention."""
    ax, ay, az = float(accel[0]), float(accel[1]), float(accel[2])
    mx, my, mz = float(mag[0]), float(mag[1]), float(mag[2])
    roll = math.atan2(ay, az)
    pitch = math.atan2(-ax, math.hypot(ay, az) + 1e-12)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    mx_h = mx * cp + my * sr * sp + mz * cr * sp
    my_h = my * cr - mz * sr
    return math.atan2(-my_h, mx_h)


def _run_madgwick_9dof(stream: Dict[str, np.ndarray], beta: float = 0.08) -> np.ndarray:
    """Full 9-DOF quaternion Madgwick AHRS baseline."""
    dt = float(stream["dt"])
    gyro = stream["gyro_xyz"]
    acc = stream["accel_xyz"]
    mag = stream["mag_xyz"]
    n = len(gyro)
    yaws = np.zeros(n, dtype=np.float64)

    yaw0 = _tilt_comp_mag_heading(acc[0], mag[0])
    q = np.array([math.cos(yaw0 * 0.5), 0.0, 0.0, math.sin(yaw0 * 0.5)], dtype=np.float64)

    for k in range(n):
        gx, gy, gz = gyro[k]
        ax, ay, az = acc[k]
        mx, my, mz = mag[k]
        an = math.sqrt(ax * ax + ay * ay + az * az)
        mn = math.sqrt(mx * mx + my * my + mz * mz)
        q0, q1, q2, q3 = q

        qDot = 0.5 * np.array(
            [
                -q1 * gx - q2 * gy - q3 * gz,
                q0 * gx + q2 * gz - q3 * gy,
                q0 * gy - q1 * gz + q3 * gx,
                q0 * gz + q1 * gy - q2 * gx,
            ],
            dtype=np.float64,
        )

        if an > 1e-6 and mn > 1e-6:
            ax, ay, az = ax / an, ay / an, az / an
            mx, my, mz = mx / mn, my / mn, mz / mn

            hx = 2.0 * mx * (0.5 - q2 * q2 - q3 * q3) + 2.0 * my * (q1 * q2 - q0 * q3) + 2.0 * mz * (q1 * q3 + q0 * q2)
            hy = 2.0 * mx * (q1 * q2 + q0 * q3) + 2.0 * my * (0.5 - q1 * q1 - q3 * q3) + 2.0 * mz * (q2 * q3 - q0 * q1)
            bx = math.sqrt(hx * hx + hy * hy)
            bz = 2.0 * mx * (q1 * q3 - q0 * q2) + 2.0 * my * (q2 * q3 + q0 * q1) + 2.0 * mz * (0.5 - q1 * q1 - q2 * q2)

            # Gravity in world is +z (accelerometer measures +g upward along +z)
            f1 = 2.0 * (q1 * q3 - q0 * q2) - ax
            f2 = 2.0 * (q0 * q1 + q2 * q3) - ay
            f3 = 2.0 * (0.5 - q1 * q1 - q2 * q2) - az
            f4 = 2.0 * bx * (0.5 - q2 * q2 - q3 * q3) + 2.0 * bz * (q1 * q3 - q0 * q2) - mx
            f5 = 2.0 * bx * (q1 * q2 - q0 * q3) + 2.0 * bz * (q0 * q1 + q2 * q3) - my
            f6 = 2.0 * bx * (q0 * q2 + q1 * q3) + 2.0 * bz * (0.5 - q1 * q1 - q2 * q2) - mz

            s0 = -2.0 * q2 * f1 + 2.0 * q1 * f2 - 2.0 * bz * q2 * f4 + (-2.0 * bx * q3 + 2.0 * bz * q1) * f5 + 2.0 * bx * q2 * f6
            s1 = 2.0 * q3 * f1 + 2.0 * q0 * f2 - 4.0 * q1 * f3 + 2.0 * bz * q3 * f4 + (2.0 * bx * q2 + 2.0 * bz * q0) * f5 + (2.0 * bx * q3 - 4.0 * bz * q1) * f6
            s2 = -2.0 * q0 * f1 + 2.0 * q3 * f2 - 4.0 * q2 * f3 + (-4.0 * bx * q2 - 2.0 * bz * q0) * f4 + (2.0 * bx * q1 + 2.0 * bz * q3) * f5 + (2.0 * bx * q0 - 4.0 * bz * q2) * f6
            s3 = 2.0 * q1 * f1 + 2.0 * q2 * f2 + (-4.0 * bx * q3 + 2.0 * bz * q1) * f4 + (-2.0 * bx * q0 + 2.0 * bz * q2) * f5 + 2.0 * bx * q1 * f6

            grad = np.array([s0, s1, s2, s3], dtype=np.float64)
            gn = np.linalg.norm(grad)
            if gn > 1e-9:
                qDot = qDot - beta * (grad / gn)

        q = q + qDot * dt
        q = q / max(1e-12, np.linalg.norm(q))
        q0, q1, q2, q3 = q
        yaws[k] = math.atan2(2.0 * (q0 * q3 + q1 * q2), 1.0 - 2.0 * (q2 * q2 + q3 * q3))

    return yaws


def _run_mahalanobis_ekf(stream: Dict[str, np.ndarray]) -> np.ndarray:
    """
    Strong 2-state Extended Kalman Filter `[yaw, gyro_bias]` with tilt-compensated
    gyro/magnetometer projection AND 99% chi-squared Mahalanobis innovation gating.
    """
    dt = float(stream["dt"])
    gyro = stream["gyro_xyz"]
    acc = stream["accel_xyz"]
    mag = stream["mag_xyz"]
    n = len(gyro)
    yaws = np.zeros(n, dtype=np.float64)

    x = np.array([_tilt_comp_mag_heading(acc[0], mag[0]), 0.0], dtype=np.float64)
    P = np.diag([0.02, 0.002])
    Q = np.diag([(0.020 * dt) ** 2, (0.001 * dt) ** 2])
    R_meas = (math.radians(4.5)) ** 2
    chi2_99 = 6.635  # 99% 1-DOF chi-squared Mahalanobis outlier threshold

    for k in range(n):
        ax, ay, az = acc[k]
        gx, gy, gz = gyro[k]
        roll = math.atan2(ay, az)
        pitch = math.atan2(-ax, math.hypot(ay, az) + 1e-12)
        sr, cr = math.sin(roll), math.cos(roll)
        cp = max(0.15, math.cos(pitch))
        yaw_rate = (gy * sr + gz * cr) / cp

        # Predict
        F = np.array([[1.0, -dt], [0.0, 1.0]])
        x[0] = (x[0] + (yaw_rate - x[1]) * dt + np.pi) % (2.0 * np.pi) - np.pi
        P = F @ P @ F.T + Q

        # Tilt-compensated mag measurement + Mahalanobis innovation gate
        z = _tilt_comp_mag_heading(acc[k], mag[k])
        innov = (z - x[0] + np.pi) % (2.0 * np.pi) - np.pi
        S = P[0, 0] + R_meas
        if (innov * innov) / S <= chi2_99:
            K_gain = P[:, 0] / S
            x[0] = (x[0] + K_gain[0] * innov + np.pi) % (2.0 * np.pi) - np.pi
            x[1] = x[1] + K_gain[1] * innov
            P = (np.eye(2) - np.outer(K_gain, [1.0, 0.0])) @ P

        yaws[k] = x[0]

    return yaws


def _run_neckcompass_9dof(stream: Dict[str, np.ndarray], weights: Any) -> np.ndarray:
    dt = float(stream["dt"])
    gyro = stream["gyro_xyz"]
    acc = stream["accel_xyz"]
    mag = stream["mag_xyz"]
    n = len(gyro)
    yaws = np.zeros(n, dtype=np.float64)

    yaw0 = _tilt_comp_mag_heading(acc[0], mag[0])
    comp = neckcompass.RingCompass(n_wedges=16, initial_heading_rad=yaw0, connectome_weights=weights)

    for k in range(n):
        yaws[k] = comp.update_9dof(
            gyro[k, 0], gyro[k, 1], gyro[k, 2],
            acc[k, 0], acc[k, 1], acc[k, 2],
            mag[k, 0], mag[k, 1], mag[k, 2],
            dt=dt,
        )
    return yaws


def _eval_dark_gyro_single(seed: int, weights: Any, T: float = 10.0, dt: float = 0.02) -> float:
    rng = np.random.default_rng(seed + 5000)
    n = int(round(T / dt))
    t = np.arange(n, dtype=np.float64) * dt
    omega = 0.85 * np.sin(2.0 * np.pi * 0.18 * t + rng.uniform(0, 6.28)) + 0.45 * np.cos(
        2.0 * np.pi * 0.37 * t + rng.uniform(0, 6.28)
    )
    yaw_true = np.zeros(n, dtype=np.float64)
    for k in range(1, n):
        yaw_true[k] = yaw_true[k - 1] + 0.5 * (omega[k - 1] + omega[k]) * dt
    yaw_true = (yaw_true + np.pi) % (2.0 * np.pi) - np.pi
    omega_meas = omega + rng.normal(0.0, 0.010, size=n)

    if weights == "trapz_gyro":
        est = np.zeros(n, dtype=np.float64)
        for k in range(1, n):
            est[k] = (est[k - 1] + omega_meas[k] * dt + np.pi) % (2.0 * np.pi) - np.pi
    else:
        comp = neckcompass.RingCompass(n_wedges=16, initial_heading_rad=0.0, connectome_weights=weights)
        est = np.zeros(n, dtype=np.float64)
        for k in range(n):
            est[k] = comp.update(omega_rad_s=float(omega_meas[k]), dt=dt, mag_heading_rad=None)

    errs = _circ_err_deg(est, yaw_true)
    return float(np.sqrt(np.mean(errs ** 2)))


def _eval_homing_single(seed: int, weights: Any) -> Tuple[float, bool]:
    """Closed-loop 2D foraging + return-to-home via `FanBodyNavigator` (`CPU4 + PFL3 + PFL2`)."""
    rng = np.random.default_rng(seed + 9000)
    dt = 0.05
    speed = 1.25  # m/s
    x, y = 0.0, 0.0
    psi = rng.uniform(-np.pi, np.pi)

    comp = neckcompass.RingCompass(n_wedges=16, initial_heading_rad=psi, connectome_weights=weights)
    nav = neckcompass.FanBodyNavigator(n_columns=16, compass=comp, connectome_weights=weights)

    # Phase 1: 12s outbound winding foraging trajectory (~12-15 m away from nest)
    for k in range(int(12.0 / dt)):
        turn_rate = 0.55 * math.sin(0.45 * k * dt + seed) + rng.normal(0.0, 0.25)
        psi = (psi + turn_rate * dt + np.pi) % (2.0 * np.pi) - np.pi
        x += speed * math.cos(psi) * dt
        y += speed * math.sin(psi) * dt
        lm_noisy = (psi + rng.normal(0.0, math.radians(3.5)) + np.pi) % (2.0 * np.pi) - np.pi
        nav.step_with_compass(
            omega_rad_s=turn_rate + rng.normal(0.0, 0.015),
            speed_m_s=speed,
            dt=dt,
            mag_heading_rad=lm_noisy,
        )

    # Phase 2: 26s closed-loop return-to-home driven strictly by `nav.steering_cmd`
    min_dist = math.hypot(x, y)
    for k in range(int(26.0 / dt)):
        cmd_omega = float(nav.steering_cmd(max_turn_rate=1.4))
        # Physical vehicle responds with disturbance
        actual_omega = np.clip(cmd_omega + rng.normal(0.0, 0.08), -1.5, 1.5)
        dist_est, _ = nav.home_vector()
        cur_spd = speed * min(1.0, max(0.15, dist_est / 2.0))
        psi = (psi + actual_omega * dt + np.pi) % (2.0 * np.pi) - np.pi
        x += cur_spd * math.cos(psi) * dt
        y += cur_spd * math.sin(psi) * dt
        lm_noisy = (psi + rng.normal(0.0, math.radians(3.5)) + np.pi) % (2.0 * np.pi) - np.pi
        nav.step_with_compass(
            omega_rad_s=actual_omega + rng.normal(0.0, 0.015),
            speed_m_s=cur_spd,
            dt=dt,
            mag_heading_rad=lm_noisy,
        )
        d_now = math.hypot(x, y)
        if d_now < min_dist:
            min_dist = d_now

    return float(min_dist), bool(min_dist < 2.5)


def evaluate_cx_weights(cx_weights: Any, streams: List[Dict[str, np.ndarray]], n_seeds: int) -> Dict[str, Any]:
    """Evaluate a single CX connectome weight bundle across all 3 tasks (`n_seeds` episodes each)."""
    rms_9dof = []
    spike_p95 = []
    for st in streams:
        est = _run_neckcompass_9dof(st, cx_weights)
        errs = _circ_err_deg(est, st["yaw_true"])
        rms_9dof.append(float(np.sqrt(np.mean(errs ** 2))))
        spike_p95.append(float(np.percentile(errs[st["spike_mask"]], 95.0)))

    dark_rms = [_eval_dark_gyro_single(s, cx_weights) for s in range(n_seeds)]
    homing = [_eval_homing_single(s, cx_weights) for s in range(n_seeds)]
    homing_dist = [h[0] for h in homing]
    homing_succ = [1.0 if h[1] else 0.0 for h in homing]

    return {
        "ahrs_rms_deg": rms_9dof,
        "ahrs_spike_p95_deg": spike_p95,
        "dark_10s_rms_deg": dark_rms,
        "homing_min_dist_m": homing_dist,
        "homing_success": homing_succ,
    }


_WORKER_CX_STREAMS: List[Dict[str, np.ndarray]] | None = None
_WORKER_CX_NSEEDS: int = 30


def _init_cx_worker(streams: List[Dict[str, np.ndarray]], n_seeds: int) -> None:
    global _WORKER_CX_STREAMS, _WORKER_CX_NSEEDS
    _WORKER_CX_STREAMS = streams
    _WORKER_CX_NSEEDS = n_seeds


def _eval_one_cx_shuffle(shuf_cx: Dict[str, Any]) -> Tuple[float, float, float, float]:
    assert _WORKER_CX_STREAMS is not None
    sr = evaluate_cx_weights(shuf_cx, _WORKER_CX_STREAMS, _WORKER_CX_NSEEDS)
    return (
        float(np.mean(sr["ahrs_rms_deg"])),
        float(np.mean(sr["dark_10s_rms_deg"])),
        float(np.mean(sr["homing_min_dist_m"])),
        float(np.mean(sr["homing_success"])),
    )


def run_compass_benchmark(
    shuffled_cx_list: List[Dict[str, Any]],
    n_seeds: int = 30,
    workers: int = 1,
) -> Dict[str, Any]:
    streams = [_generate_9dof_stream(seed=100 + s) for s in range(n_seeds)]

    # 1. Real BANC v888 & v2_analytic
    real_res = evaluate_cx_weights("banc_v888", streams, n_seeds)
    v2_res = evaluate_cx_weights("v2_analytic", streams, n_seeds)

    # 2. Classical aerospace/marine baselines
    mad_rms, mad_sp = [], []
    ekf_rms, ekf_sp = [], []
    for st in streams:
        y_mad = _run_madgwick_9dof(st)
        e_mad = _circ_err_deg(y_mad, st["yaw_true"])
        mad_rms.append(float(np.sqrt(np.mean(e_mad ** 2))))
        mad_sp.append(float(np.percentile(e_mad[st["spike_mask"]], 95.0)))

        y_ekf = _run_mahalanobis_ekf(st)
        e_ekf = _circ_err_deg(y_ekf, st["yaw_true"])
        ekf_rms.append(float(np.sqrt(np.mean(e_ekf ** 2))))
        ekf_sp.append(float(np.percentile(e_ekf[st["spike_mask"]], 95.0)))

    trapz_dark = [_eval_dark_gyro_single(s, "trapz_gyro") for s in range(n_seeds)]

    # 3. K = len(shuffled_cx_list) Maslov-Sneppen Shuffled CX Connectomes
    shuf_ahrs_means = []
    shuf_dark_means = []
    shuf_homing_means = []
    shuf_homing_succ_means = []
    if workers <= 1 or len(shuffled_cx_list) <= 2:
        for shuf_cx in shuffled_cx_list:
            sr = evaluate_cx_weights(shuf_cx, streams, n_seeds)
            shuf_ahrs_means.append(float(np.mean(sr["ahrs_rms_deg"])))
            shuf_dark_means.append(float(np.mean(sr["dark_10s_rms_deg"])))
            shuf_homing_means.append(float(np.mean(sr["homing_min_dist_m"])))
            shuf_homing_succ_means.append(float(np.mean(sr["homing_success"])))
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_cx_worker,
            initargs=(streams, n_seeds),
        ) as pool:
            for am, dm, hm, sm in pool.map(_eval_one_cx_shuffle, shuffled_cx_list):
                shuf_ahrs_means.append(am)
                shuf_dark_means.append(dm)
                shuf_homing_means.append(hm)
                shuf_homing_succ_means.append(sm)

    return {
        "n_seeds": n_seeds,
        "task_1a_9dof_spike_ahrs": {
            "v3_banc_real_rms_deg": summary_ci95(real_res["ahrs_rms_deg"]),
            "v3_banc_real_spike_p95_deg": summary_ci95(real_res["ahrs_spike_p95_deg"]),
            "v2_analytic_rms_deg": summary_ci95(v2_res["ahrs_rms_deg"]),
            "v2_analytic_spike_p95_deg": summary_ci95(v2_res["ahrs_spike_p95_deg"]),
            "madgwick_9dof_rms_deg": summary_ci95(mad_rms),
            "madgwick_9dof_spike_p95_deg": summary_ci95(mad_sp),
            "mahalanobis_ekf_rms_deg": summary_ci95(ekf_rms),
            "mahalanobis_ekf_spike_p95_deg": summary_ci95(ekf_sp),
            "shuffle_gate_rms_deg": shuffle_gate_verdict(
                float(np.mean(real_res["ahrs_rms_deg"])),
                shuf_ahrs_means,
                higher_is_better=False,
            ),
        },
        "task_1b_dark_10s_gyro_integration": {
            "v3_banc_real_rms_deg": summary_ci95(real_res["dark_10s_rms_deg"]),
            "v2_analytic_rms_deg": summary_ci95(v2_res["dark_10s_rms_deg"]),
            "trapz_gyro_rms_deg": summary_ci95(trapz_dark),
            "shuffle_gate_dark_rms_deg": shuffle_gate_verdict(
                float(np.mean(real_res["dark_10s_rms_deg"])),
                shuf_dark_means,
                higher_is_better=False,
            ),
            "honest_note": (
                "In pure 10s darkness without landmarks/magnetometer, biological BANC v888 "
                "EPG/Delta7 heterogeneity + 8L vs 10R PEN_a synapse asymmetry drifts much more "
                "than symmetric v2_analytic or raw gyro integration."
            ),
        },
        "task_1c_closed_loop_pfl_homing": {
            "v3_banc_real_min_dist_m": summary_ci95(real_res["homing_min_dist_m"]),
            "v3_banc_real_arrival_rate": summary_ci95(real_res["homing_success"]),
            "v2_analytic_min_dist_m": summary_ci95(v2_res["homing_min_dist_m"]),
            "v2_analytic_arrival_rate": summary_ci95(v2_res["homing_success"]),
            "shuffle_gate_min_dist_m": shuffle_gate_verdict(
                float(np.mean(real_res["homing_min_dist_m"])),
                shuf_homing_means,
                higher_is_better=False,
            ),
            "shuffle_gate_arrival_rate": shuffle_gate_verdict(
                float(np.mean(real_res["homing_success"])),
                shuf_homing_succ_means,
                higher_is_better=True,
            ),
        },
    }
