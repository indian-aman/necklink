"""Command-line interface for NeckCompass (`neckcompass demo`, `neckcompass benchmark-quick`)."""

from __future__ import annotations

import argparse
import json
import math
from typing import Optional, Sequence

import numpy as np

from . import __version__
from .path_integrator import FanBodyNavigator
from .ring_attractor import RingCompass, circular_error_rad, wrap_angle


class _CircularEKF2State:
    """Optimal 2-state Circular Extended Kalman Filter [heading_rad, gyro_bias_rad_s]."""

    def __init__(
        self,
        init_heading_rad: float,
        gyro_std_rad_s: float = math.radians(1.5),
        bias_rw_rad_s_rt_s: float = math.radians(0.08),
        mag_std_rad: float = math.radians(3.0),
    ) -> None:
        self.psi = float(wrap_angle(init_heading_rad))
        self.bias = 0.0
        r_mag = float(mag_std_rad) ** 2
        self.P = np.diag([r_mag, (math.radians(1.5)) ** 2]).astype(np.float64)
        self.gyro_var = float(gyro_std_rad_s) ** 2
        self.bias_rw_var = float(bias_rw_rad_s_rt_s) ** 2
        self.r_mag = r_mag

    def update(self, omega_meas: float, dt: float, mag_rad: float) -> float:
        psi_pred = float(wrap_angle(self.psi + (omega_meas - self.bias) * dt))
        p00, p01, p10, p11 = self.P[0, 0], self.P[0, 1], self.P[1, 0], self.P[1, 1]
        # Exact discrete-time covariance propagation: F = [[1, -dt], [0, 1]]
        q00 = self.gyro_var * (dt * dt)
        q11 = self.bias_rw_var * dt
        pp00 = p00 - dt * (p10 + p01) + (dt * dt) * p11 + q00
        pp01 = p01 - dt * p11
        pp10 = p10 - dt * p11
        pp11 = p11 + q11

        innov = float(wrap_angle(mag_rad - psi_pred))
        s_cov = pp00 + self.r_mag
        k0 = pp00 / s_cov
        k1 = pp10 / s_cov

        self.psi = float(wrap_angle(psi_pred + k0 * innov))
        self.bias = float(self.bias + k1 * innov)
        self.P[0, 0] = (1.0 - k0) * pp00
        self.P[0, 1] = (1.0 - k0) * pp01
        self.P[1, 0] = pp10 - k1 * pp00
        self.P[1, 1] = pp11 - k1 * pp01
        return self.psi


def _run_demo(seed: int = 42, n_wedges: int = 16, as_json: bool = False) -> dict:
    rng = np.random.default_rng(seed)
    dt = 0.02
    speed = 1.6  # m/s
    gyro_bias = math.radians(1.0)  # 1.0 deg/s steady MEMS drift

    compass = RingCompass(n_wedges=n_wedges)
    nav = FanBodyNavigator(n_columns=16, compass=compass)

    true_x, true_y, true_psi = 0.0, 0.0, 0.35
    compass.reset(true_psi + rng.normal(0.0, math.radians(2.0)))

    outbound_steps = int(90.0 / dt)  # 90 s outbound survey (~144 m path)
    spike_errs_deg: list[float] = []

    for k in range(outbound_steps):
        t = k * dt
        omega_true = 0.22 * math.sin(0.11 * t) + 0.14 * math.cos(0.07 * t)
        true_psi = float(wrap_angle(true_psi + omega_true * dt))
        true_x += speed * math.cos(true_psi) * dt
        true_y += speed * math.sin(true_psi) * dt

        omega_meas = omega_true + gyro_bias + rng.normal(0.0, math.radians(1.2))
        spike = 0.0
        if 25.0 <= t <= 26.6:
            spike = math.radians(118.0) * math.sin(math.pi * (t - 25.0) / 1.6)
        elif 58.0 <= t <= 59.8:
            spike = -math.radians(136.0) * math.sin(math.pi * (t - 58.0) / 1.8)
        mag_meas = float(wrap_angle(true_psi + spike + rng.normal(0.0, math.radians(2.5))))

        est_psi, _, _ = nav.step_with_compass(omega_meas, speed, dt, mag_heading_rad=mag_meas)
        if abs(spike) > math.radians(60.0):
            spike_errs_deg.append(abs(math.degrees(float(circular_error_rad(est_psi, true_psi)))))

    outbound_dist = math.hypot(true_x, true_y)

    # Engage blind GPS-denied return-to-home via PFL3 steering
    max_return_steps = int(130.0 / dt)
    for k in range(max_return_steps):
        t = 90.0 + k * dt
        est_dist, _ = nav.home_vector()
        if est_dist < 0.6:
            break
        omega_cmd = nav.steering_cmd(max_turn_rate=0.9)
        true_psi = float(wrap_angle(true_psi + omega_cmd * dt))
        step_v = speed * min(1.0, max(0.25, est_dist / 3.0))
        true_x += step_v * math.cos(true_psi) * dt
        true_y += step_v * math.sin(true_psi) * dt

        omega_meas = omega_cmd + gyro_bias + rng.normal(0.0, math.radians(1.2))
        spike = (
            math.radians(125.0) * math.sin(math.pi * (t - 115.0) / 1.5)
            if 115.0 <= t <= 116.5
            else 0.0
        )
        mag_meas = float(wrap_angle(true_psi + spike + rng.normal(0.0, math.radians(2.5))))
        nav.step_with_compass(omega_meas, step_v, dt, mag_heading_rad=mag_meas)

    miss_m = math.hypot(true_x, true_y)
    summary = {
        "version": __version__,
        "n_wedges": n_wedges,
        "outbound_distance_from_origin_m": round(outbound_dist, 2),
        "max_error_during_136deg_spike_deg": round(max(spike_errs_deg) if spike_errs_deg else 0.0, 2),
        "estimated_gyro_bias_deg_s": round(math.degrees(compass.estimated_gyro_bias_rad_s), 3),
        "final_return_miss_m": round(miss_m, 2),
    }

    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"NeckCompass v{__version__} -- Central Complex Ring Attractor & Path Integrator Demo")
        print("-" * 74)
        print(f"  E-PG Ring Wedges               : {summary['n_wedges']}")
        print(f"  Outbound Survey Displacement   : {summary['outbound_distance_from_origin_m']:.2f} m (GPS blackout)")
        print(f"  Max Error in 118-136 deg Spikes: {summary['max_error_during_136deg_spike_deg']:.2f} deg")
        print(f"  Learned P-EN Gyro Bias Trim    : {summary['estimated_gyro_bias_deg_s']:.3f} deg/s (true: 1.000 deg/s)")
        print(f"  Final PFL3 Return-to-Home Miss : {summary['final_return_miss_m']:.2f} m")
    return summary


def _run_quick_benchmark(seed: int = 101, as_json: bool = False) -> dict:
    rng = np.random.default_rng(seed)
    dt = 0.02
    steps = int(120.0 / dt)
    gyro_bias_init = math.radians(0.9)
    bias_rw_std = math.radians(0.08)
    gyro_std = math.radians(1.5)
    mag_std = math.radians(3.0)

    def eval_heading(spiky: bool) -> dict[str, dict[str, float]]:
        local_rng = np.random.default_rng(seed + (17 if spiky else 0))
        psi_true = 0.2
        gyro_bias = gyro_bias_init
        init_mag = float(wrap_angle(psi_true + local_rng.normal(0.0, mag_std)))

        nc = RingCompass(n_wedges=16, initial_heading_rad=init_mag)
        ekf = _CircularEKF2State(
            init_mag,
            gyro_std_rad_s=gyro_std,
            bias_rw_rad_s_rt_s=bias_rw_std,
            mag_std_rad=mag_std,
        )
        psi_gyro = init_mag
        psi_comp = init_mag
        tau_comp = 2.5
        alpha_comp = dt / (tau_comp + dt)

        errs = {"neckcompass": [], "ekf": [], "complementary": [], "gyro_dr": []}
        for k in range(steps):
            t = k * dt
            omega_true = 0.30 * math.sin(0.15 * t) + 0.18 * math.cos(0.09 * t)
            psi_true = float(wrap_angle(psi_true + omega_true * dt))
            gyro_bias += local_rng.normal(0.0, bias_rw_std * math.sqrt(dt))
            omega_meas = omega_true + gyro_bias + local_rng.normal(0.0, gyro_std)

            anom = 0.0
            if spiky:
                # Periodic 1.4 s ferromagnetic hull/cable spikes every 15 s
                phase = t % 15.0
                if 6.0 <= phase <= 7.4:
                    sign = 1.0 if (int(t // 15.0) % 2 == 0) else -1.0
                    amp = math.radians(115.0 if sign > 0 else 135.0)
                    anom = sign * amp * math.sin(math.pi * (phase - 6.0) / 1.4)

            mag_meas = float(wrap_angle(psi_true + anom + local_rng.normal(0.0, mag_std)))

            psi_nc = nc.update(omega_meas, dt, mag_heading_rad=mag_meas)
            psi_ekf = ekf.update(omega_meas, dt, mag_meas)
            psi_gyro = float(wrap_angle(psi_gyro + omega_meas * dt))
            pred_c = float(wrap_angle(psi_comp + omega_meas * dt))
            psi_comp = float(wrap_angle(pred_c + alpha_comp * float(wrap_angle(mag_meas - pred_c))))

            if k >= int(15.0 / dt):  # Steady-state window after 15 s warmup
                errs["neckcompass"].append(abs(math.degrees(float(circular_error_rad(psi_nc, psi_true)))))
                errs["ekf"].append(abs(math.degrees(float(circular_error_rad(psi_ekf, psi_true)))))
                errs["complementary"].append(abs(math.degrees(float(circular_error_rad(psi_comp, psi_true)))))
                errs["gyro_dr"].append(abs(math.degrees(float(circular_error_rad(psi_gyro, psi_true)))))

        out: dict[str, dict[str, float]] = {}
        for name, vals in errs.items():
            arr = np.asarray(vals, dtype=np.float64)
            out[name] = {
                "rmse_deg": round(float(np.sqrt(np.mean(arr * arr))), 2),
                "p95_deg": round(float(np.percentile(arr, 95)), 2),
            }
        return out

    clean_res = eval_heading(spiky=False)
    spiky_res = eval_heading(spiky=True)
    _ = rng  # keep seed signature clean

    result = {
        "seed": seed,
        "scenario_a_clean_gaussian": clean_res,
        "scenario_b_ferromagnetic_spikes": spiky_res,
    }

    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"NeckCompass v{__version__} -- Quick Head-to-Head Benchmark (120 s @ 50 Hz, seed={seed})")
        print("-" * 82)
        print(
            f"{'Estimator':<28} | {'Clean RMSE (deg)':>16} | {'Spiky RMSE (deg)':>16} | {'Spiky P95 (deg)':>15}"
        )
        print("-" * 82)
        labels = [
            ("NeckCompass (E-PG 16w)", "neckcompass"),
            ("2-State Circular EKF", "ekf"),
            ("Complementary IMU+Mag", "complementary"),
            ("Gyro Dead-Reckoning", "gyro_dr"),
        ]
        for label, key in labels:
            c_rmse = clean_res[key]["rmse_deg"]
            s_rmse = spiky_res[key]["rmse_deg"]
            s_p95 = spiky_res[key]["p95_deg"]
            print(f"{label:<28} | {c_rmse:>16.2f} | {s_rmse:>16.2f} | {s_p95:>15.2f}")
        print("-" * 82)
        print(
            "Note: Under ideal Gaussian noise, 2-State EKF is minimum-variance optimal; "
            "under 115-135 deg ferromagnetic spikes, NeckCompass rejects off-basin outliers."
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neckcompass",
        description="NeckCompass: Drosophila Central Complex ring-attractor compass & GPS-denied navigator.",
    )
    parser.add_argument("--version", action="version", version=f"neckcompass {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="Run GPS-denied outbound + PFL3 homebound navigation demo")
    p_demo.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    p_demo.add_argument("--wedges", type=int, default=16, choices=[16, 32], help="Ring wedges (16 or 32)")
    p_demo.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    p_bench = sub.add_parser(
        "benchmark-quick",
        help="Run fast head-to-head comparison (Clean Gaussian vs Ferromagnetic Spikes)",
    )
    p_bench.add_argument("--seed", type=int, default=101, help="Random seed (default: 101)")
    p_bench.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "demo":
        _run_demo(seed=args.seed, n_wedges=args.wedges, as_json=args.json)
        return 0
    if args.command == "benchmark-quick":
        _run_quick_benchmark(seed=args.seed, as_json=args.json)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
