"""Empirical 3-DOF Vectored USV & 6-Limb Hexapod Benchmark for NeckCord v1.1.0.

Evaluates:
  1. Intact 4-thruster vectored USV (`100%` health, `30 s`, `dt = 0.02 s`, `10` wave seeds):
     `NeckCord` (4-byte cervical link + `ShuntingThrusterCord`) vs `Static Pseudo-Inverse + PID`.
  2. Sudden Port-Aft (`T2`) weed-fouling (`90%` thrust loss at `t = 8.0 s`, `10` wave seeds):
     Peak Yaw Excursion (`deg`), Post-Fault Cross-Track Error (`m`), Fault Isolation Latency (`ms`),
     wasted fouled-thruster effort, and Cervical Command Link Bandwidth (`400 B/s` vs `2,400 B/s` @ `100 Hz`).
  3. Hexapod Single-Leg Loss (`L2` Jammed + all 6 single-leg faults):
     Duty-factor CoM static support-polygon stability (`%` time with `>=3` grounded legs enclosing CoM)
     with vs without VNC proprioceptive phase + foothold adaptation.

Writes:
  - `results/benchmark_summary.json`
  - `assets/neckcord_benchmarks.png`
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from neckcord import (
    CervicalBottleneckLink,
    DescendingFrame,
    GaitMode,
    LIMB_NAMES,
    SegmentalCPGCord,
    ShuntingThrusterCord,
)


def _build_corridor_table() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Build a C^2-smooth straight + waypoint corridor (`0..18 m` straight, `18..34 m` waypoint shift, `34..48 m` straight)."""
    xs = np.linspace(-2.0, 50.0, 1041)
    ys = np.zeros_like(xs)
    dys = np.zeros_like(xs)
    ddys = np.zeros_like(xs)

    x0, span, y_shift = 18.0, 16.0, 5.0
    for i, xv in enumerate(xs):
        if xv <= x0:
            ys[i] = 0.0
            dys[i] = 0.0
            ddys[i] = 0.0
        elif xv >= x0 + span:
            ys[i] = y_shift
            dys[i] = 0.0
            ddys[i] = 0.0
        else:
            u = (xv - x0) / span
            s5 = u * u * u * (10.0 + u * (-15.0 + 6.0 * u))
            ds5 = (30.0 * u * u * (1.0 - 2.0 * u + u * u)) / span
            dds5 = (60.0 * u * (1.0 - 3.0 * u + 2.0 * u * u)) / (span * span)
            ys[i] = y_shift * s5
            dys[i] = y_shift * ds5
            ddys[i] = y_shift * dds5

    gammas = np.arctan(dys)
    curvatures = ddys / np.power(1.0 + dys * dys, 1.5)
    return xs, ys, gammas, curvatures


CORR_X, CORR_Y, CORR_GAMMA, CORR_KAPPA = _build_corridor_table()


def _corridor_query(px: float, py: float) -> tuple[float, float, float]:
    """Return `(signed_cross_track_m, path_tangent_rad, curvature)` to nearest corridor point."""
    dists_sq = (CORR_X - px) ** 2 + (CORR_Y - py) ** 2
    idx = int(np.argmin(dists_sq))
    gamma = float(CORR_GAMMA[idx])
    kappa = float(CORR_KAPPA[idx])
    rx = px - float(CORR_X[idx])
    ry = py - float(CORR_Y[idx])
    # Positive when vessel is left (+North) of path tangent
    e_ct = -math.sin(gamma) * rx + math.cos(gamma) * ry
    return e_ct, gamma, kappa


def simulate_usv_episode(
    *,
    seed: int,
    mode: str,
    inject_fault: bool,
    dt: float = 0.02,
    duration_s: float = 30.0,
    fault_time_s: float = 8.0,
) -> dict[str, Any]:
    """Run one 30 s 3-DOF vectored 4-thruster USV corridor-tracking episode."""
    rng = np.random.default_rng(1000 + seed)
    cord = ShuntingThrusterCord.usv_vectored_4(
        lx=0.45,
        ly=0.35,
        cant_deg=45.0,
        u_max=35.0,
        tau_actuator=0.04,
        tau_fault=0.028,
    )
    if mode not in ("neckcord", "static_pinv", "pinv_fdi"):
        raise ValueError(mode)
    use_neckcord = mode == "neckcord"
    w_pinv_static = np.linalg.pinv(cord.W)
    link = CervicalBottleneckLink()
    # Classical baseline with the SAME per-thruster thrust feedback: a ratio-threshold fault
    # detector (measured / commanded < 0.5 for 3 consecutive ticks while |cmd| > 5% u_max)
    # that drops the failed column and recomputes the pseudo-inverse.
    fdi_alive = np.ones(4, dtype=bool)
    fdi_count = np.zeros(4, dtype=np.int64)
    w_pinv_fdi = w_pinv_static.copy()
    false_isolations = 0

    # Fossen 3-DOF rigid + hydrodynamic added mass parameters (1.1 m survey USV)
    m11 = 24.0
    m22 = 38.0
    i33 = 3.8
    tau_act = 0.04

    wave_phases = rng.uniform(0.0, 2.0 * math.pi, size=(3, 3))

    x, y, psi = 0.0, 0.0, 0.0
    u_surge, v_sway, r_yaw = 0.0, 0.0, 0.0
    u_act = np.zeros(4, dtype=np.float64)
    true_eff = np.ones(4, dtype=np.float64)

    int_u = 0.0
    int_psi = 0.0
    int_ct = 0.0
    isolated_latency_ms: float | None = None

    n_steps = int(round(duration_s / dt))
    t_arr = np.zeros(n_steps, dtype=np.float64)
    x_arr = np.zeros(n_steps, dtype=np.float64)
    y_arr = np.zeros(n_steps, dtype=np.float64)
    ct_arr = np.zeros(n_steps, dtype=np.float64)
    yaw_err_deg_arr = np.zeros(n_steps, dtype=np.float64)
    fouled_cmd_arr = np.zeros(n_steps, dtype=np.float64)

    max_surge_scale = 68.0
    max_sway_scale = 36.0
    max_yaw_scale = 30.0

    for k in range(n_steps):
        t = k * dt
        t_arr[k] = t

        if inject_fault and t >= fault_time_s:
            true_eff[2] = 0.10  # Sudden 90% thrust loss on Port-Aft thruster (T2)

        e_ct, gamma_p, kappa_p = _corridor_query(x, y)
        ct_arr[k] = e_ct
        x_arr[k] = x
        y_arr[k] = y

        path_yaw_err = math.atan2(math.sin(psi - gamma_p), math.cos(psi - gamma_p))
        yaw_err_deg_arr[k] = abs(math.degrees(path_yaw_err))

        # Identical Fossen LOS corridor + PD/PI surge-sway-yaw controller for both architectures
        lookahead = 4.2
        psi_los = gamma_p + math.atan2(-e_ct, lookahead)
        e_psi = math.atan2(math.sin(psi_los - psi), math.cos(psi_los - psi))
        int_psi = max(-0.20, min(0.20, int_psi + e_psi * dt))

        u_target = 1.42
        eu = u_target - u_surge
        int_u = max(-1.2, min(1.2, int_u + eu * dt))

        r_ref = u_surge * kappa_p
        fy_centripetal = m22 * u_surge * r_ref
        fx_des = 43.0 + 26.0 * eu + 7.5 * int_u
        fy_des = -6.0 * e_ct - 6.0 * v_sway + fy_centripetal
        tz_des = 26.0 * e_psi + 11.0 * (r_ref - r_yaw) + 2.0 * int_psi + 4.2 * r_ref

        fx_des = max(-max_surge_scale, min(max_surge_scale, fx_des))
        fy_des = max(-max_sway_scale, min(max_sway_scale, fy_des))
        tz_des = max(-max_yaw_scale, min(max_yaw_scale, tz_des))

        if use_neckcord:
            frame_tx = DescendingFrame(
                drive_fwd=fx_des / max_surge_scale,
                turn_yaw=tz_des / max_yaw_scale,
                strafe_lat=fy_des / max_sway_scale,
                gait_mode=int(GaitMode.AUTO),
                freeze_reflex=False,
            )
            wire = link.pack(frame_tx)
            frame_rx = link.unpack(wire)

            fb = true_eff * u_act + rng.normal(0.0, 0.40, size=4)
            alloc = cord.step(
                frame_rx,
                feedback_thrust=fb,
                dt=dt,
                max_surge=max_surge_scale,
                max_sway=max_sway_scale,
                max_yaw=max_yaw_scale,
            )
            u_cmd = alloc.commands
            if (
                inject_fault
                and t >= fault_time_s
                and isolated_latency_ms is None
                and bool(alloc.isolated_mask[2])
            ):
                isolated_latency_ms = (t - fault_time_s) * 1000.0
            if not (inject_fault and t >= fault_time_s):
                false_isolations = max(false_isolations, int(np.count_nonzero(alloc.isolated_mask)))
        elif mode == "pinv_fdi":
            fb = true_eff * u_act + rng.normal(0.0, 0.40, size=4)
            for i in range(4):
                if fdi_alive[i] and abs(u_act[i]) > 0.05 * cord.u_max:
                    fdi_count[i] = fdi_count[i] + 1 if fb[i] / u_act[i] < 0.5 else 0
                    if fdi_count[i] >= 3:
                        fdi_alive[i] = False
                        w_pinv_fdi = np.zeros((4, 3))
                        w_pinv_fdi[fdi_alive, :] = np.linalg.pinv(cord.W[:, fdi_alive])
                        if inject_fault and t >= fault_time_s and i == 2 and isolated_latency_ms is None:
                            isolated_latency_ms = (t - fault_time_s) * 1000.0
                        elif not (inject_fault and t >= fault_time_s):
                            false_isolations += 1
            w_vec = np.array([fx_des, fy_des, tz_des], dtype=np.float64)
            u_cmd = w_pinv_fdi @ w_vec
            peak = float(np.max(np.abs(u_cmd)))
            if peak > cord.u_max:
                u_cmd = u_cmd * (cord.u_max / peak)
        else:
            w_vec = np.array([fx_des, fy_des, tz_des], dtype=np.float64)
            u_cmd = np.clip(w_pinv_static @ w_vec, -cord.u_max, cord.u_max)

        fouled_cmd_arr[k] = abs(float(u_cmd[2]))

        alpha_act = 1.0 - math.exp(-dt / tau_act)
        u_act += alpha_act * (u_cmd - u_act)
        w_hull = cord.W @ (true_eff * u_act)

        freqs = (0.75, 1.35, 2.05)
        wave_fx = sum(
            amp * math.sin(w * t + float(wave_phases[0, j]))
            for j, (amp, w) in enumerate(zip((1.6, 1.0, 0.5), freqs))
        )
        wave_fy = sum(
            amp * math.sin(w * t + float(wave_phases[1, j]))
            for j, (amp, w) in enumerate(zip((2.3, 1.4, 0.7), freqs))
        )
        wave_tz = sum(
            amp * math.cos(w * t + float(wave_phases[2, j]))
            for j, (amp, w) in enumerate(zip((0.75, 0.45, 0.25), freqs))
        )

        drag_u = 10.0 * u_surge + 12.0 * abs(u_surge) * u_surge
        drag_v = 24.0 * v_sway + 28.0 * abs(v_sway) * v_sway
        drag_r = 4.5 * r_yaw + 4.0 * abs(r_yaw) * r_yaw

        du = (w_hull[0] + wave_fx - drag_u + m22 * v_sway * r_yaw) / m11
        dv = (w_hull[1] + wave_fy - drag_v - m11 * u_surge * r_yaw) / m22
        dr = (w_hull[2] + wave_tz - drag_r) / i33

        u_surge += du * dt
        v_sway += dv * dt
        r_yaw += dr * dt

        x += (u_surge * math.cos(psi) - v_sway * math.sin(psi)) * dt
        y += (u_surge * math.sin(psi) + v_sway * math.cos(psi)) * dt
        psi = math.atan2(math.sin(psi + r_yaw * dt), math.cos(psi + r_yaw * dt))

    eval_mask = (t_arr >= fault_time_s) if inject_fault else (t_arr >= 2.0)
    post_ct = np.abs(ct_arr[eval_mask])
    post_yaw = yaw_err_deg_arr[eval_mask]

    return {
        "t": t_arr,
        "x": x_arr,
        "y": y_arr,
        "ct": ct_arr,
        "yaw_err_deg": yaw_err_deg_arr,
        "peak_yaw_excursion_deg": float(np.max(post_yaw)),
        "rms_cross_track_m": float(math.sqrt(float(np.mean(post_ct**2)))),
        "max_cross_track_m": float(np.max(post_ct)),
        "isolation_latency_ms": isolated_latency_ms if isolated_latency_ms is not None else -1.0,
        "mean_fouled_thruster_cmd_N": float(np.mean(fouled_cmd_arr[eval_mask])),
        "false_isolations": false_isolations,
    }


def evaluate_hexapod_leg_loss(dt: float = 0.005, eval_duration_s: float = 8.0) -> dict[str, Any]:
    """Compare hexapod static support-polygon stability with vs without VNC adaptation."""
    cmd = DescendingFrame(drive_fwd=0.85, gait_mode=int(GaitMode.AUTO))
    warmup_steps = int(round(1.5 / dt))
    eval_steps = int(round(eval_duration_s / dt))

    per_limb: dict[str, dict[str, float]] = {}
    for limb in LIMB_NAMES:
        cpg_adapt = SegmentalCPGCord(adapt_to_damage=True)
        cpg_fixed = SegmentalCPGCord(adapt_to_damage=False)
        cpg_adapt.inject_limb_fault(limb)
        cpg_fixed.inject_limb_fault(limb)

        for _ in range(warmup_steps):
            cpg_adapt.step(cmd, dt=dt)
            cpg_fixed.step(cmd, dt=dt)

        stable_a = 0
        stable_f = 0
        margins_a: list[float] = []
        margins_f: list[float] = []

        for _ in range(eval_steps):
            sa = cpg_adapt.step(cmd, dt=dt)
            sf = cpg_fixed.step(cmd, dt=dt)
            if sa.is_statically_stable:
                stable_a += 1
            if sf.is_statically_stable:
                stable_f += 1
            margins_a.append(sa.support_margin_m)
            margins_f.append(sf.support_margin_m)

        per_limb[limb] = {
            "adaptive_stability_pct": round(100.0 * stable_a / eval_steps, 2),
            "unadapted_stability_pct": round(100.0 * stable_f / eval_steps, 2),
            "adaptive_min_margin_m": round(float(np.min(margins_a)), 4),
            "unadapted_min_margin_m": round(float(np.min(margins_f)), 4),
        }

    cpg_intact = SegmentalCPGCord(adapt_to_damage=True)
    for _ in range(warmup_steps):
        cpg_intact.step(cmd, dt=dt)
    intact_states = [cpg_intact.step(cmd, dt=dt) for _ in range(eval_steps)]
    intact_stable = sum(1 for st in intact_states if st.is_statically_stable)
    intact_pct = round(100.0 * intact_stable / eval_steps, 2)

    return {
        "intact_6leg_tripod_stability_pct": intact_pct,
        "intact_6leg_min_margin_m": round(float(min(st.support_margin_m for st in intact_states)), 4),
        "L2_jammed": per_limb["L2"],
        "per_limb": per_limb,
    }


def render_hero_chart(
    fault_nc: list[dict[str, Any]],
    fault_st: list[dict[str, Any]],
    fault_fdi: list[dict[str, Any]],
    hex_res: dict[str, Any],
    summary: dict[str, Any],
    out_path: Path,
) -> None:
    """Render a crisp, high-DPI 3-panel benchmark figure readable on dark and light themes."""
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 11.5,
            "axes.titleweight": "bold",
            "axes.labelsize": 10,
        }
    )

    fig = plt.figure(figsize=(17.5, 5.8), dpi=220, facecolor="#F8FAFC")
    gs = fig.add_gridspec(
        1,
        3,
        width_ratios=[1.12, 1.08, 1.0],
        wspace=0.25,
        left=0.05,
        right=0.98,
        top=0.83,
        bottom=0.13,
    )

    fig.suptitle(
        "NeckCord v1.1.0 — 4-Byte Descending Cervical Bottleneck & Self-Healing VNC Motor/Thruster Cord",
        fontsize=14,
        fontweight="bold",
        color="#0F172A",
        y=0.955,
    )
    fig.text(
        0.5,
        0.905,
        "3-DOF X-Vectored 4-Thruster USV (dt = 0.02 s, 30 s, 10 Irregular Wave Seeds) + 6-Limb Hexapod VNC Proprioceptive Adaptation",
        ha="center",
        fontsize=10.2,
        color="#475569",
    )

    c_nc = "#0284C7"
    c_st = "#DC2626"
    c_fdi = "#7C3AED"
    c_ref = "#334155"
    c_ok = "#059669"

    # Panel A: 2D USV Trajectory on Straight + Waypoint Corridor
    ax_a = fig.add_subplot(gs[0, 0], facecolor="#FFFFFF")
    mask_corr = (CORR_X >= 0.0) & (CORR_X <= 43.0)
    ax_a.plot(
        CORR_X[mask_corr],
        CORR_Y[mask_corr],
        linestyle="--",
        linewidth=2.0,
        color=c_ref,
        label="Target Waypoint Corridor",
        zorder=2,
    )

    for run in fault_st:
        ax_a.plot(run["x"], run["y"], color=c_st, alpha=0.18, linewidth=1.0, zorder=3)
    for run in fault_nc:
        ax_a.plot(run["x"], run["y"], color=c_nc, alpha=0.22, linewidth=1.0, zorder=4)

    mean_st_x = np.mean([r["x"] for r in fault_st], axis=0)
    mean_st_y = np.mean([r["y"] for r in fault_st], axis=0)
    mean_nc_x = np.mean([r["x"] for r in fault_nc], axis=0)
    mean_nc_y = np.mean([r["y"] for r in fault_nc], axis=0)

    st_rms = summary["usv_fault_90pct_T2"]["static_pinv"]["rms_cross_track_m"]
    nc_rms = summary["usv_fault_90pct_T2"]["neckcord"]["rms_cross_track_m"]
    ax_a.plot(
        mean_st_x,
        mean_st_y,
        color=c_st,
        linewidth=2.5,
        label=f"Static PInv + PID (RMS XTE {st_rms:.2f} m)",
        zorder=5,
    )
    fdi_rms = summary["usv_fault_90pct_T2"]["pinv_fdi"]["rms_cross_track_m"]
    ax_a.plot(
        np.mean([r["x"] for r in fault_fdi], axis=0),
        np.mean([r["y"] for r in fault_fdi], axis=0),
        color=c_fdi,
        linewidth=1.8,
        linestyle="--",
        label=f"PInv + threshold FDI (RMS XTE {fdi_rms:.2f} m)",
        zorder=6,
    )
    ax_a.plot(
        mean_nc_x,
        mean_nc_y,
        color=c_nc,
        linewidth=2.6,
        label=f"NeckCord 4B + Shunting (RMS XTE {nc_rms:.2f} m)",
        zorder=7,
    )

    fault_idx = int(round(8.0 / 0.02))
    fx_pt = float(mean_nc_x[fault_idx])
    fy_pt = float(mean_nc_y[fault_idx])
    ax_a.scatter([fx_pt], [fy_pt], s=75, color="#F59E0B", edgecolor="#0F172A", linewidth=1.2, zorder=8)
    ax_a.annotate(
        "t = 8.0 s: Port-Aft\n90% Thrust Loss",
        xy=(fx_pt, fy_pt),
        xytext=(fx_pt - 8.5, fy_pt + 2.3),
        fontsize=8.8,
        fontweight="bold",
        color="#92400E",
        bbox=dict(boxstyle="round,pad=0.28", facecolor="#FEF3C7", edgecolor="#F59E0B", lw=0.9),
        arrowprops=dict(arrowstyle="->", color="#B45309", lw=1.2),
        zorder=9,
    )

    intact_nc_rms = summary["usv_intact"]["neckcord"]["rms_cross_track_m"]
    intact_st_rms = summary["usv_intact"]["static_pinv"]["rms_cross_track_m"]
    intact_nc_yaw = summary["usv_intact"]["neckcord"]["peak_yaw_deg"]
    intact_st_yaw = summary["usv_intact"]["static_pinv"]["peak_yaw_deg"]
    ax_a.text(
        0.03,
        0.96,
        (
            f"Intact 100% Fleet Parity:\n"
            f"  XTE RMS: {intact_nc_rms:.3f} m vs {intact_st_rms:.3f} m\n"
            f"  Peak Yaw: {intact_nc_yaw:.2f}° vs {intact_st_yaw:.2f}°"
        ),
        transform=ax_a.transAxes,
        va="top",
        fontsize=8.4,
        color="#1E293B",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#F1F5F9", edgecolor="#CBD5E1", lw=0.8),
    )

    ax_a.set_title("A. 4-Thruster USV Waypoint Corridor (10 Wave Seeds)")
    ax_a.set_xlabel("East Position x (m)")
    ax_a.set_ylabel("North Position y (m)")
    ax_a.set_xlim(-1.0, 44.0)
    ax_a.set_ylim(-2.4, 6.8)
    ax_a.grid(True, linestyle=":", color="#CBD5E1", alpha=0.85)
    ax_a.legend(loc="lower right", fontsize=8.4, framealpha=0.95, edgecolor="#CBD5E1")

    # Panel B: Heading Excursion & Millisecond Presynaptic Shunting Isolation
    ax_b = fig.add_subplot(gs[0, 1], facecolor="#FFFFFF")
    t_vec = fault_nc[0]["t"]
    yaw_st_mat = np.array([r["yaw_err_deg"] for r in fault_st])
    yaw_nc_mat = np.array([r["yaw_err_deg"] for r in fault_nc])

    st_mu, st_sd = np.mean(yaw_st_mat, axis=0), np.std(yaw_st_mat, axis=0)
    nc_mu, nc_sd = np.mean(yaw_nc_mat, axis=0), np.std(yaw_nc_mat, axis=0)

    ax_b.fill_between(t_vec, np.maximum(0.0, st_mu - st_sd), st_mu + st_sd, color=c_st, alpha=0.16)
    ax_b.fill_between(t_vec, np.maximum(0.0, nc_mu - nc_sd), nc_mu + nc_sd, color=c_nc, alpha=0.20)
    ax_b.plot(
        t_vec,
        st_mu,
        color=c_st,
        linewidth=2.2,
        label=f"Static PInv Peak Spin: {summary['usv_fault_90pct_T2']['static_pinv']['peak_yaw_deg']:.1f}°",
    )
    ax_b.plot(
        t_vec,
        nc_mu,
        color=c_nc,
        linewidth=2.3,
        label=f"NeckCord Peak Yaw: {summary['usv_fault_90pct_T2']['neckcord']['peak_yaw_deg']:.1f}°",
    )
    ax_b.plot(
        t_vec,
        np.mean(np.array([r["yaw_err_deg"] for r in fault_fdi]), axis=0),
        color=c_fdi,
        linewidth=1.8,
        linestyle="--",
        label=f"PInv + FDI Peak Yaw: {summary['usv_fault_90pct_T2']['pinv_fdi']['peak_yaw_deg']:.1f}°",
    )
    ax_b.axvline(8.0, color="#D97706", linestyle="--", linewidth=1.4)

    iso_ms = summary["usv_fault_90pct_T2"]["neckcord"]["isolation_latency_ms"]
    fouled_nc_n = summary["usv_fault_90pct_T2"]["neckcord"]["mean_fouled_thruster_cmd_N"]
    fouled_st_n = summary["usv_fault_90pct_T2"]["static_pinv"]["mean_fouled_thruster_cmd_N"]

    ax_b.text(
        0.97,
        0.34,
        (
            f"Isolation: NeckCord {iso_ms:.0f} ms, PInv+FDI {summary['usv_fault_90pct_T2']['pinv_fdi']['isolation_latency_ms']:.0f} ms\n"
            f"Fouled T2 Drive: {fouled_nc_n:.2f} N vs {fouled_st_n:.1f} N (no FDI)\n"
            f"Command Link @ 100 Hz: 400 B/s (4 B frames)"
        ),
        transform=ax_b.transAxes,
        ha="right",
        va="top",
        fontsize=8.1,
        color="#0F172A",
        bbox=dict(boxstyle="round,pad=0.32", facecolor="#ECFEFF", edgecolor="#67E8F9", lw=0.9, alpha=0.94),
    )

    ax_b.set_title("B. Heading Error After T2 Fault (mean of 10 seeds)")
    ax_b.set_xlabel("Mission Time t (s)")
    ax_b.set_ylabel("Corridor Heading Error |Δψ| (deg)")
    ax_b.set_xlim(0.0, 30.0)
    ax_b.set_ylim(0.0, max(30.0, float(np.max(st_mu + st_sd)) * 1.25))
    ax_b.grid(True, linestyle=":", color="#CBD5E1", alpha=0.85)
    ax_b.legend(loc="upper left", fontsize=8.5, framealpha=0.95, edgecolor="#CBD5E1")

    # Panel C: Hexapod Single-Leg Loss (L2 Jammed) Static Polygon Stability
    ax_c = fig.add_subplot(gs[0, 2], facecolor="#FFFFFF")

    categories = [
        "Intact 6-Leg\nTripod",
        "L2 Jammed\nUnadapted CPG",
        "L2 Jammed\nNeckCord VNC",
        "All 6 1-Leg\nMean (VNC)",
    ]
    all6_mean_adapt = float(
        np.mean([hex_res["per_limb"][k]["adaptive_stability_pct"] for k in LIMB_NAMES])
    )
    vals = [
        hex_res["intact_6leg_tripod_stability_pct"],
        hex_res["L2_jammed"]["unadapted_stability_pct"],
        hex_res["L2_jammed"]["adaptive_stability_pct"],
        all6_mean_adapt,
    ]
    bar_colors = ["#64748B", c_st, c_ok, c_nc]
    bars = ax_c.bar(
        categories, vals, color=bar_colors, width=0.56, edgecolor="#0F172A", linewidth=0.9, zorder=3
    )

    for bar, val in zip(bars, vals):
        ax_c.text(
            bar.get_x() + bar.get_width() / 2.0,
            val + 2.0,
            f"{val:.1f}%",
            ha="center",
            va="bottom",
            fontsize=9.2,
            fontweight="bold",
            color="#0F172A",
        )

    ax_c.axhline(100.0, color="#94A3B8", linestyle=":", linewidth=1.0)
    ax_c.set_ylim(0.0, 116.0)
    ax_c.set_ylabel("Time in >=3-Leg CoM Support Polygon (%)")
    ax_c.set_title("C. Hexapod Single-Leg Jam (L2) Stability")
    ax_c.grid(True, axis="y", linestyle=":", color="#CBD5E1", alpha=0.85, zorder=1)
    ax_c.tick_params(axis="x", labelsize=8.6)

    ax_c.text(
        0.5,
        0.10,
        "VNC shifts surviving 5 limbs to Δφ = 72° ring\n(duty 0.82) + ipsilateral foothold gap closure",
        transform=ax_c.transAxes,
        ha="center",
        va="bottom",
        fontsize=8.3,
        color="#065F46",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#ECFDF5", edgecolor="#6EE7B7", lw=0.85),
    )

    for ax in (ax_a, ax_b, ax_c):
        for spine in ax.spines.values():
            spine.set_color("#94A3B8")
            spine.set_linewidth(0.9)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=220)
    plt.close(fig)


def main() -> int:
    seeds = list(range(10))
    dt = 0.02
    duration_s = 30.0

    intact_nc = [
        simulate_usv_episode(seed=s, mode="neckcord", inject_fault=False, dt=dt, duration_s=duration_s)
        for s in seeds
    ]
    intact_st = [
        simulate_usv_episode(seed=s, mode="static_pinv", inject_fault=False, dt=dt, duration_s=duration_s)
        for s in seeds
    ]
    fault_nc = [
        simulate_usv_episode(seed=s, mode="neckcord", inject_fault=True, dt=dt, duration_s=duration_s)
        for s in seeds
    ]
    fault_st = [
        simulate_usv_episode(seed=s, mode="static_pinv", inject_fault=True, dt=dt, duration_s=duration_s)
        for s in seeds
    ]

    intact_fdi = [
        simulate_usv_episode(seed=s, mode="pinv_fdi", inject_fault=False, dt=dt, duration_s=duration_s)
        for s in seeds
    ]
    fault_fdi = [
        simulate_usv_episode(seed=s, mode="pinv_fdi", inject_fault=True, dt=dt, duration_s=duration_s)
        for s in seeds
    ]

    hex_res = evaluate_hexapod_leg_loss()

    def _agg(runs: list[dict[str, Any]], include_iso: bool = False) -> dict[str, float]:
        out = {
            "peak_yaw_deg": round(float(np.mean([r["peak_yaw_excursion_deg"] for r in runs])), 2),
            "peak_yaw_std_deg": round(float(np.std([r["peak_yaw_excursion_deg"] for r in runs])), 2),
            "rms_cross_track_m": round(float(np.mean([r["rms_cross_track_m"] for r in runs])), 3),
            "max_cross_track_m": round(float(np.mean([r["max_cross_track_m"] for r in runs])), 3),
            "mean_fouled_thruster_cmd_N": round(
                float(np.mean([r["mean_fouled_thruster_cmd_N"] for r in runs])), 2
            ),
        }
        out["false_isolation_runs"] = int(sum(1 for r in runs if r["false_isolations"] > 0))
        if include_iso:
            lat = [r["isolation_latency_ms"] for r in runs if r["isolation_latency_ms"] >= 0.0]
            out["isolation_latency_ms"] = round(float(np.mean(lat)), 1) if lat else None
            out["isolated_runs"] = len(lat)
        return out

    summary: dict[str, Any] = {
        "benchmark": "NeckCord v1.1.0 Empirical USV & Hexapod Suite",
        "config": {
            "dt_s": dt,
            "duration_s": duration_s,
            "n_seeds": len(seeds),
            "fault_event": "Port-Aft thruster (T2) sudden 90% loss of thrust at t = 8.0 s",
        },
        "bandwidth": {
            "neckcord_frame_bytes": 4,
            "raw_4x_float32_plus_8b_hdr_bytes": 24,
            "plain_int8_3dof_plus_flags_bytes": 4,
            "note": "The 6x is versus 4 x float32 + an 8-byte header. Plain int8 quantisation of the same 3 axes is also ~4 B; the frame's value is the fixed layout, flags, sequence counter and CRC, not compression.",
            "neckcord_100hz_bytes_per_s": 400,
            "raw_actuator_100hz_bytes_per_s": 2400,
            "compression_ratio_x": 6.0,
        },
        "usv_intact": {
            "neckcord": _agg(intact_nc),
            "static_pinv": _agg(intact_st),
            "pinv_fdi": _agg(intact_fdi),
        },
        "usv_fault_90pct_T2": {
            "neckcord": _agg(fault_nc, include_iso=True),
            "static_pinv": _agg(fault_st, include_iso=False),
            "pinv_fdi": _agg(fault_fdi, include_iso=True),
        },
        "hexapod_single_leg_loss": {
            "intact_6leg_tripod_stability_pct": hex_res["intact_6leg_tripod_stability_pct"],
            "intact_6leg_min_margin_m": hex_res["intact_6leg_min_margin_m"],
            "L2_jammed": hex_res["L2_jammed"],
            "all_6_single_leg_faults": hex_res["per_limb"],
        },
    }

    results_path = REPO_ROOT / "results" / "benchmark_summary.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    chart_path = REPO_ROOT / "assets" / "neckcord_benchmarks.png"
    render_hero_chart(
        fault_nc=fault_nc,
        fault_st=fault_st,
        fault_fdi=fault_fdi,
        hex_res=hex_res,
        summary=summary,
        out_path=chart_path,
    )

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
