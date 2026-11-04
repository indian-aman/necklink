"""Command-line interface for `neckcord` (`demo` and `benchmark-quick`)."""

from __future__ import annotations

from . import __version__

import argparse
import json
import math
from typing import Any
import numpy as np

from .allocator import ShuntingThrusterCord
from .cervical import (
    CervicalBottleneckLink,
    DescendingFrame,
    GaitMode,
    decode_descending_4b,
    encode_descending_4b,
)
from .cpg import LIMB_NAMES, SegmentalCPGCord


def _run_usv_quick_trial(
    seed: int,
    use_neckcord: bool,
    inject_fault: bool,
    duration_s: float = 20.0,
    dt: float = 0.02,
) -> dict[str, float]:
    """Compact 3-DOF vectored 4-thruster USV trial used by `benchmark-quick`."""
    rng = np.random.default_rng(seed)
    cord = ShuntingThrusterCord.usv_vectored_4(lx=0.45, ly=0.35, cant_deg=45.0, u_max=35.0)
    w_pinv_static = np.linalg.pinv(cord.W)
    link = CervicalBottleneckLink()

    mass = 18.0
    iz = 2.4
    tau_act = 0.04

    x, y, psi = 0.0, 0.0, 0.0
    vx, vy, r = 0.0, 0.0, 0.0
    u_act = np.zeros(4, dtype=np.float64)
    true_eff = np.ones(4, dtype=np.float64)

    int_y = 0.0
    int_psi = 0.0
    fault_time_s = 6.0
    isolated_at_s: float | None = None

    peak_yaw_deg = 0.0
    ct_sq_sum = 0.0
    n_post = 0

    n_steps = int(round(duration_s / dt))
    for step_idx in range(n_steps):
        t = step_idx * dt
        if inject_fault and t >= fault_time_s:
            true_eff[2] = 0.10  # 90% loss on Port-Aft thruster (T2)

        # Target corridor: y_ref = 0.0, v_surge_ref = 1.35 m/s, heading = LOS to corridor
        ey = 0.0 - y
        int_y = max(-0.8, min(0.8, int_y + ey * dt))
        psi_ref = max(-0.45, min(0.45, 0.38 * ey + 0.05 * int_y))

        e_psi = math.atan2(math.sin(psi_ref - psi), math.cos(psi_ref - psi))
        int_psi = max(-0.6, min(0.6, int_psi + e_psi * dt))

        v_target = 1.35
        fx_cmd = 38.0 + 28.0 * (v_target - vx)
        vy_world = vx * math.sin(psi) + vy * math.cos(psi)
        fy_body = (14.0 * ey - 10.0 * vy_world) * math.cos(psi)
        tz_cmd = 26.0 * e_psi - 9.5 * r + 6.0 * int_psi

        max_surge, max_sway, max_yaw = 65.0, 40.0, 28.0
        if use_neckcord:
            frame_tx = DescendingFrame(
                drive_fwd=max(-1.0, min(1.0, fx_cmd / max_surge)),
                turn_yaw=max(-1.0, min(1.0, tz_cmd / max_yaw)),
                strafe_lat=max(-1.0, min(1.0, fy_body / max_sway)),
                gait_mode=int(GaitMode.AUTO),
                freeze_reflex=False,
            )
            wire = link.pack(frame_tx)
            frame_rx = link.unpack(wire)
            meas_fb = true_eff * u_act + rng.normal(0.0, 0.35, size=4)
            alloc = cord.step(
                frame_rx,
                feedback_thrust=meas_fb,
                dt=dt,
                max_surge=max_surge,
                max_sway=max_sway,
                max_yaw=max_yaw,
            )
            u_cmd = alloc.commands
            if (
                inject_fault
                and t >= fault_time_s
                and isolated_at_s is None
                and bool(alloc.isolated_mask[2])
            ):
                isolated_at_s = t - fault_time_s
        else:
            w_des = np.array(
                [
                    max(-max_surge, min(max_surge, fx_cmd)),
                    max(-max_sway, min(max_sway, fy_body)),
                    max(-max_yaw, min(max_yaw, tz_cmd)),
                ],
                dtype=np.float64,
            )
            u_cmd = np.clip(w_pinv_static @ w_des, -cord.u_max, cord.u_max)

        # Actuator lag + wave disturbances + 3-DOF hull dynamics
        alpha_a = 1.0 - math.exp(-dt / tau_act)
        u_act += alpha_a * (u_cmd - u_act)
        w_hull = cord.W @ (true_eff * u_act)

        wave_fx = 1.8 * math.sin(1.3 * t + seed)
        wave_fy = 2.4 * math.sin(0.95 * t + 1.7 * seed)
        wave_tz = 0.85 * math.cos(1.15 * t + 0.6 * seed)

        drag_x = 12.0 * vx + 11.5 * abs(vx) * vx
        drag_y = 22.0 * vy + 24.0 * abs(vy) * vy
        drag_r = 4.2 * r + 3.8 * abs(r) * r

        ax = (w_hull[0] + wave_fx - drag_x + mass * vy * r) / mass
        ay = (w_hull[1] + wave_fy - drag_y - mass * vx * r) / mass
        ar = (w_hull[2] + wave_tz - drag_r) / iz

        vx += ax * dt
        vy += ay * dt
        r += ar * dt
        x += (vx * math.cos(psi) - vy * math.sin(psi)) * dt
        y += (vx * math.sin(psi) + vy * math.cos(psi)) * dt
        psi = math.atan2(math.sin(psi + r * dt), math.cos(psi + r * dt))

        if not inject_fault or t >= fault_time_s:
            yaw_deg = abs(math.degrees(psi))
            if yaw_deg > peak_yaw_deg:
                peak_yaw_deg = yaw_deg
            ct_sq_sum += y * y
            n_post += 1

    return {
        "peak_yaw_deg": peak_yaw_deg,
        "rms_cross_track_m": math.sqrt(ct_sq_sum / max(1, n_post)),
        "isolation_latency_ms": (isolated_at_s * 1000.0) if isolated_at_s is not None else -1.0,
    }


def cmd_demo(as_json: bool = False) -> int:
    """Run an interactive end-to-end NeckCord demonstration."""
    frame = DescendingFrame(
        drive_fwd=0.82,
        turn_yaw=-0.25,
        strafe_lat=0.10,
        gait_mode=int(GaitMode.AUTO),
        freeze_reflex=False,
        seq=2,
    )
    wire = encode_descending_4b(frame)
    decoded = decode_descending_4b(wire)

    # Step hexapod CPG intact -> L2 jammed
    cpg = SegmentalCPGCord(adapt_to_damage=True)
    for _ in range(100):
        st_intact = cpg.step(decoded, dt=0.01)

    cpg.inject_limb_fault("L2")
    stable_ticks = 0
    for k in range(300):
        st_dmg = cpg.step(DescendingFrame(drive_fwd=0.82), dt=0.01)
        if k >= 50 and st_dmg.is_statically_stable:
            stable_ticks += 1
    stability_pct = 100.0 * stable_ticks / 250.0

    # Step 4-thruster vectored USV cord across Port-Aft fault
    thruster_cord = ShuntingThrusterCord.usv_vectored_4()
    w_nom = np.array([35.0, 0.0, 0.0])
    res_nom = thruster_cord.step(w_nom, dt=0.02)
    true_eff = np.array([1.0, 1.0, 0.08, 1.0])
    for _ in range(6):
        fb = true_eff * res_nom.commands
        res_nom = thruster_cord.step(w_nom, feedback_thrust=fb, dt=0.02)

    payload: dict[str, Any] = {
        "wire_hex": wire.hex(),
        "wire_bytes": len(wire),
        "bandwidth_100hz_bps": 400,
        "decoded_drive_fwd": round(decoded.drive_fwd, 4),
        "decoded_turn_yaw": round(decoded.turn_yaw, 4),
        "decoded_strafe_lat": round(decoded.strafe_lat, 4),
        "intact_gait": st_intact.active_gait,
        "damaged_L2_gait": st_dmg.active_gait,
        "damaged_L2_stability_pct": round(stability_pct, 1),
        "thruster_commands_after_T2_fault_N": [round(float(v), 2) for v in res_nom.commands],
        "thruster_isolated": [bool(x) for x in res_nom.isolated_mask],
        "delivered_wrench_Fx_Fy_Tz": [
            round(float(v), 3) for v in (thruster_cord.W @ (true_eff * res_nom.commands))
        ],
    }

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"=== NeckCord v{__version__} End-to-End Demo ===")
    print(
        f"1. Cervical Bottleneck : 0x{wire.hex().upper()} ({len(wire)} bytes = 400 B/s @ 100 Hz)"
    )
    print(
        f"   Decoded Intent      : drive_fwd={decoded.drive_fwd:+.3f}, "
        f"turn_yaw={decoded.turn_yaw:+.3f}, strafe_lat={decoded.strafe_lat:+.3f}, seq={decoded.seq}"
    )
    print(
        f"2. VNC Hexapod CPG     : intact={st_intact.active_gait} -> "
        f"L2-jammed={st_dmg.active_gait} ({stability_pct:.1f}% static polygon stability)"
    )
    grounded_names = [LIMB_NAMES[i] for i in range(6) if st_dmg.stance_mask[i]]
    print(f"   Grounded Survivors  : {', '.join(grounded_names)} (margin={st_dmg.support_margin_m:.3f} m)")
    print(
        f"3. Shunting Thrusters  : T2 (Port-Aft) shunted={bool(res_nom.isolated_mask[2])} | "
        f"u={payload['thruster_commands_after_T2_fault_N']} N"
    )
    print(
        f"   Rebalanced Wrench   : Fx={payload['delivered_wrench_Fx_Fy_Tz'][0]:.2f} N, "
        f"Fy={payload['delivered_wrench_Fx_Fy_Tz'][1]:+.3f} N, "
        f"Tz={payload['delivered_wrench_Fx_Fy_Tz'][2]:+.3f} N*m"
    )
    return 0


def cmd_benchmark_quick(seeds: int = 4, as_json: bool = False) -> int:
    """Run a fast multi-seed USV fault-tolerance + hexapod L2-loss verification."""
    nc_fault = [
        _run_usv_quick_trial(seed=s, use_neckcord=True, inject_fault=True) for s in range(seeds)
    ]
    st_fault = [
        _run_usv_quick_trial(seed=s, use_neckcord=False, inject_fault=True) for s in range(seeds)
    ]

    # Hexapod L2-jammed comparison (fast command drive_fwd = 0.85)
    def _eval_hexapod(adapt: bool) -> float:
        cpg = SegmentalCPGCord(adapt_to_damage=adapt)
        cpg.inject_limb_fault("L2")
        cmd = DescendingFrame(drive_fwd=0.85, gait_mode=int(GaitMode.AUTO))
        for _ in range(80):
            cpg.step(cmd, dt=0.01)
        stable = 0
        total = 400
        for _ in range(total):
            st = cpg.step(cmd, dt=0.01)
            if st.is_statically_stable:
                stable += 1
        return 100.0 * stable / total

    hex_adapt_pct = _eval_hexapod(True)
    hex_fixed_pct = _eval_hexapod(False)

    summary = {
        "seeds": seeds,
        "usv_fault_90pct_T2": {
            "neckcord_peak_yaw_deg": round(
                float(np.mean([r["peak_yaw_deg"] for r in nc_fault])), 2
            ),
            "static_pinv_peak_yaw_deg": round(
                float(np.mean([r["peak_yaw_deg"] for r in st_fault])), 2
            ),
            "neckcord_rms_ct_m": round(
                float(np.mean([r["rms_cross_track_m"] for r in nc_fault])), 3
            ),
            "static_pinv_rms_ct_m": round(
                float(np.mean([r["rms_cross_track_m"] for r in st_fault])), 3
            ),
            "neckcord_isolation_ms": round(
                float(np.mean([r["isolation_latency_ms"] for r in nc_fault])), 1
            ),
        },
        "hexapod_L2_jammed_stability_pct": {
            "neckcord_adaptive_vnc": round(hex_adapt_pct, 1),
            "unadapted_tripod_cpg": round(hex_fixed_pct, 1),
        },
    }

    if as_json:
        print(json.dumps(summary, indent=2))
        return 0

    u = summary["usv_fault_90pct_T2"]
    h = summary["hexapod_L2_jammed_stability_pct"]
    print(f"=== NeckCord Quick Benchmark ({seeds} wave seeds) ===")
    print(
        f"USV 90% Port-Aft Loss | Peak Yaw  : NeckCord {u['neckcord_peak_yaw_deg']:.2f} deg "
        f"vs PInv without fault detection {u['static_pinv_peak_yaw_deg']:.2f} deg"
    )
    print(
        f"USV 90% Port-Aft Loss | RMS XTE   : NeckCord {u['neckcord_rms_ct_m']:.3f} m "
        f"vs PInv without fault detection {u['static_pinv_rms_ct_m']:.3f} m"
    )
    print(f"USV Fault Isolation   | Latency   : {u['neckcord_isolation_ms']:.1f} ms")
    print(
        f"Hexapod L2 Jammed     | Stability : NeckCord {h['neckcord_adaptive_vnc']:.1f}% "
        f"vs Unadapted {h['unadapted_tripod_cpg']:.1f}%"
    )
    print(
        "Note: a pseudo-inverse with a simple threshold fault detector on the same feedback\n"
        "performs about the same as NeckCord; see scripts/benchmark_neckcord.py."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neckcord",
        description=f"NeckCord v{__version__}: 4-byte descending cervical bottleneck + self-healing VNC motor/thruster cord",
    )
    parser.add_argument("--version", action="version", version=f"neckcord {__version__}")
    sub = parser.add_subparsers(dest="subcommand")

    p_demo = sub.add_parser("demo", help="Run end-to-end cervical + CPG + thruster shunting demo")
    p_demo.add_argument("--json", action="store_true", help="Emit JSON output")

    p_bench = sub.add_parser(
        "benchmark-quick",
        help="Run fast multi-seed USV thruster-loss & hexapod leg-loss verification",
    )
    p_bench.add_argument("--seeds", type=int, default=4, help="Number of wave disturbance seeds")
    p_bench.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.subcommand == "demo":
        return cmd_demo(as_json=bool(args.json))
    if args.subcommand == "benchmark-quick":
        return cmd_benchmark_quick(seeds=int(args.seeds), as_json=bool(args.json))
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
