"""
Benchmark 4: VNC Motor Cord (`NeckCord v3.0.0`) — 3D MuJoCo Hexapod & Hexacopter.

Strictly separated by honest provenance:
  - Part 4A (`PROVENANCE = "connectome"` — K=100 Maslov-Sneppen Shuffle Gate):
      1. Emergent Tripod Order (`R_tripod`) from near-uniform initial phases via `BANC v888` `C_vnc_inh`
      2. 3D `MuJoCo` 18-DOF Rigid-Body Hexapod Walking + Mid-Stride L2 Leg Amputation (`N = 30` seeds):
         Every arm drives the **exact same `HexapodLegIK` -> 18 MuJoCo hinge actuators**!
         Includes both `OpenLoop_Fixed_Tripod` AND `OpenLoop_Tripod_Parked_Leg` so broken-leg
         clearance parking is controlled for fairly!
      3. Contralateral Steering Yaw Velocity (`W_dn_leg[:, 4:8]`) in 3D MuJoCo (`turn_yaw = +0.85`).

  - Part 4B (`PROVENANCE = "engineering"` — Actuator Allocator & ESC Observer, non-biological):
      3D `MuJoCo` 6-DOF Unidirectional `[0, u_max]` Hexacopter Mid-Flight Rotor #2 Loss (`N = 30` seeds),
      comparing `NeckCord_Shunting4D_ESC`, `Scipy_Bounded_QP_Oracle`, and `Clipped_Damped_Pinv_ESC`
      (where the pseudo-inverse baseline ALSO receives exact `ESCObserver` health weights!).
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import math
import time
from typing import Any, Dict, List, Tuple

import mujoco
import neckcord
import numpy as np
from scipy.optimize import lsq_linear

from datasets import shuffle_gate_verdict, summary_ci95


def _build_exact_ik_hexapod_xml() -> str:
    """
    Build 3D MuJoCo 18-DOF hexapod whose link lengths (`38, 95, 135 mm`), hip coordinates,
    and mount azimuths match `neckcord.HexapodLegIK` 1-to-1 with zero heuristic scaling.
    """
    hips_m = neckcord.ik.DEFAULT_HIP_XY_MM / 1000.0
    mounts_rad = np.radians(neckcord.ik.DEFAULT_MOUNT_YAW_DEG)
    legs_xml = []
    acts_xml = []
    for i in range(6):
        hx, hy = hips_m[i]
        yaw = mounts_rad[i]
        cz, sz = math.cos(yaw * 0.5), math.sin(yaw * 0.5)
        legs_xml.append(
            f"""
      <body name="leg_{i}" pos="{hx:.4f} {hy:.4f} 0" quat="{cz:.6f} 0 0 {sz:.6f}">
        <joint name="c_{i}" type="hinge" axis="0 0 1" range="-1.2 1.2"/>
        <geom type="capsule" fromto="0 0 0 0.038 0 0" size="0.010" mass="0.035"/>
        <body pos="0.038 0 0">
          <joint name="f_{i}" type="hinge" axis="0 -1 0" range="-1.4 1.4"/>
          <geom type="capsule" fromto="0 0 0 0.095 0 0" size="0.010" mass="0.045"/>
          <body pos="0.095 0 0">
            <joint name="t_{i}" type="hinge" axis="0 -1 0" range="-1.6 1.6"/>
            <geom name="foot_{i}" type="capsule" fromto="0 0 0 0 0 -0.135" size="0.009" mass="0.045"/>
          </body>
        </body>
      </body>"""
        )
        acts_xml.append(
            f'    <position joint="c_{i}" ctrlrange="-1.2 1.2"/>'
            f'<position joint="f_{i}" ctrlrange="-1.4 1.4"/>'
            f'<position joint="t_{i}" ctrlrange="-1.6 1.6"/>'
        )

    return (
        """<mujoco model="neckcord_v3_hexapod_18dof">
  <compiler angle="radian" coordinate="local"/>
  <option timestep="0.002" gravity="0 0 -9.81" integrator="RK4"/>
  <default>
    <geom friction="1.20 0.02 0.005" condim="3"/>
    <joint damping="0.45" armature="0.010"/>
    <position kp="28.0"/>
  </default>
  <worldbody>
    <geom name="floor" type="plane" size="20 20 0.1"/>
    <body name="torso" pos="0 0 0.10">
      <freejoint name="root"/>
      <geom name="torso_box" type="box" size="0.14 0.065 0.024" mass="1.05"/>
"""
        + "\n".join(legs_xml)
        + """
    </body>
  </worldbody>
  <actuator>
"""
        + "\n".join(acts_xml)
        + """
  </actuator>
</mujoco>"""
    )


HEXAPOD_V3_XML = _build_exact_ik_hexapod_xml()

HEXACOPTER_V3_XML = """
<mujoco model="neckcord_v3_hexacopter_6dof">
  <option timestep="0.002" gravity="0 0 -9.81" integrator="RK4"/>
  <worldbody>
    <geom type="plane" size="20 20 0.1"/>
    <body name="drone" pos="0 0 1.5">
      <freejoint/>
      <geom type="cylinder" size="0.16 0.03" mass="1.80"/>
      <site name="m0" pos="0.250 0.000 0"/><site name="m1" pos="0.125 0.2165 0"/>
      <site name="m2" pos="-0.125 0.2165 0"/><site name="m3" pos="-0.250 0.000 0"/>
      <site name="m4" pos="-0.125 -0.2165 0"/><site name="m5" pos="0.125 -0.2165 0"/>
    </body>
  </worldbody>
  <actuator>
    <general site="m0" gear="0 0 1 0 0  0.018" ctrlrange="0 8.0"/>
    <general site="m1" gear="0 0 1 0 0 -0.018" ctrlrange="0 8.0"/>
    <general site="m2" gear="0 0 1 0 0  0.018" ctrlrange="0 8.0"/>
    <general site="m3" gear="0 0 1 0 0 -0.018" ctrlrange="0 8.0"/>
    <general site="m4" gear="0 0 1 0 0  0.018" ctrlrange="0 8.0"/>
    <general site="m5" gear="0 0 1 0 0 -0.018" ctrlrange="0 8.0"/>
  </actuator>
</mujoco>
"""


def _eval_hexapod_episode(
    model: mujoco.MjModel,
    seed: int,
    arm: str,
    vnc_weights: Any = "banc_v888",
    turn_cmd: float = 0.0,
    amputate_l2: bool = True,
) -> Dict[str, float]:
    rng = np.random.default_rng(3000 + seed)
    data = mujoco.MjData(model)
    mujoco.mj_resetData(model, data)

    ik = neckcord.HexapodLegIK()
    # Settle in neutral standing posture for 0.3s
    neutral_xyz = np.zeros((6, 3), dtype=np.float64)
    neutral_xyz[:, 0:2] = neckcord.cpg.NOMINAL_FOOTHOLDS_XY * 1000.0
    stand_rad = np.radians(ik.solve_all(neutral_xyz).angles_deg).ravel()
    data.ctrl[:] = stand_rad
    data.qpos[7:] = stand_rad
    for _ in range(150):
        mujoco.mj_step(model, data)

    dt_ctrl = 0.01
    substeps = int(round(dt_ctrl / model.opt.timestep))  # 5 physics steps per 10ms CPG tick
    n_ticks = 500  # 5.0 seconds
    t_amp_tick = int(round(rng.uniform(1.6, 2.0) / dt_ctrl))

    init_phases = rng.uniform(0.0, 0.35, size=6)  # Near-in-phase random start (tests spontaneous tripod emergence!)
    if arm.startswith("openloop"):
        cpg = None
        ol_phases = np.array([0.0, math.pi, math.pi, 0.0, 0.0, math.pi], dtype=np.float64)
    else:
        cpg = neckcord.SegmentalCPGCord(
            adapt_to_damage=(arm != "v3_no_damage_adapt"),
            connectome_weights=vnc_weights,
            ik=ik,
        )
        cpg.phases = init_phases.copy()

    x_pre_amp = float(data.qpos[0])
    tripod_orders = []
    post_amp_min_z = 1.0
    post_amp_tilt_sq = []
    qw0, qx0, qy0, qz0 = data.qpos[3:7]
    yaw_trace = [math.atan2(2.0 * (qw0 * qz0 + qx0 * qy0), 1.0 - 2.0 * (qy0 * qy0 + qz0 * qz0))]

    for tick in range(n_ticks):
        if amputate_l2 and tick == t_amp_tick:
            x_pre_amp = float(data.qpos[0])
            if cpg is not None:
                cpg.set_limb_health(2, 0.0)  # Amputate / jam L2 mesothoracic leg

        if cpg is not None:
            st = cpg.step(drive_fwd if False else 0.90, dt=dt_ctrl, turn_yaw=turn_cmd, gait_mode=int(neckcord.GaitMode.TRIPOD))
            if tick >= 80 and (not amputate_l2 or tick < t_amp_tick):
                tripod_orders.append(neckcord.compute_tripod_order(st.phases))
            assert st.leg_ik is not None
            ctrl_rad = np.radians(st.leg_ik.angles_deg).ravel()
        else:
            # Open-loop classical fixed tripod through the EXACT SAME HexapodLegIK!
            ol_phases = (ol_phases + 2.0 * math.pi * 2.5 * dt_ctrl) % (2.0 * math.pi)
            if tick >= 80 and (not amputate_l2 or tick < t_amp_tick):
                tripod_orders.append(neckcord.compute_tripod_order(ol_phases))
            duty = 0.54
            u = ol_phases / (2.0 * math.pi)
            stance = u < duty
            xyz = np.zeros((6, 3), dtype=np.float64)
            anchors = neckcord.cpg.NOMINAL_FOOTHOLDS_XY * 1000.0
            stride_mm = 42.0
            for i in range(6):
                if stance[i]:
                    prog = u[i] / duty
                    xyz[i, 0] = anchors[i, 0] + stride_mm * (0.5 - prog)
                    xyz[i, 1] = anchors[i, 1]
                    xyz[i, 2] = 0.0
                else:
                    prog = (u[i] - duty) / (1.0 - duty)
                    xyz[i, 0] = anchors[i, 0] + stride_mm * (-0.5 + prog)
                    xyz[i, 1] = anchors[i, 1]
                    xyz[i, 2] = 30.0 * math.sin(math.pi * prog)
            park_mask = np.zeros(6, dtype=bool)
            if amputate_l2 and tick >= t_amp_tick and arm == "openloop_tripod_parked":
                park_mask[2] = True
            ik_res = ik.solve_all(xyz, parked_mask=park_mask)
            ctrl_rad = np.radians(ik_res.angles_deg).ravel()
            if amputate_l2 and tick >= t_amp_tick and arm == "openloop_tripod_limp_drag":
                # Dead limp joint torque on amputated leg 2
                ctrl_rad[6:9] = data.qpos[7 + 6 : 7 + 9]

        data.ctrl[:] = ctrl_rad
        for _ in range(substeps):
            mujoco.mj_step(model, data)

        qw, qx, qy, qz = data.qpos[3:7]
        yaw_trace.append(math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz)))

        if amputate_l2 and tick >= t_amp_tick:
            z_torso = float(data.qpos[2])
            if z_torso < post_amp_min_z:
                post_amp_min_z = z_torso
            roll = math.atan2(2.0 * (qw * qx + qy * qz), 1.0 - 2.0 * (qx * qx + qy * qy))
            pitch = math.asin(max(-1.0, min(1.0, 2.0 * (qw * qy - qz * qx))))
            post_amp_tilt_sq.append(math.degrees(math.hypot(roll, pitch)) ** 2)

    yaw_unwrapped = np.unwrap(np.asarray(yaw_trace, dtype=np.float64))
    yaw_end = float(yaw_unwrapped[-1] - yaw_unwrapped[0])
    post_amp_fwd = float(data.qpos[0]) - x_pre_amp
    return {
        "tripod_order": float(np.mean(tripod_orders)) if tripod_orders else 0.0,
        "total_fwd_m": float(data.qpos[0]),
        "post_amp_fwd_m": post_amp_fwd,
        "post_amp_tilt_rms_deg": float(math.sqrt(np.mean(post_amp_tilt_sq))) if post_amp_tilt_sq else 0.0,
        "post_amp_min_z_m": post_amp_min_z,
        "yaw_end_rad": yaw_end,
    }


def _eval_hexacopter_rotor_loss(model: mujoco.MjModel, seed: int, solver_arm: str) -> Dict[str, float]:
    """
    3D MuJoCo 6-DOF unidirectional `[0, u_max]` hexacopter with Rotor #2 blade loss at t = 1.0s.
    Every arm receives the exact same `ESCObserver` health vector!
    """
    rng = np.random.default_rng(6000 + seed)
    data = mujoco.MjData(model)
    mujoco.mj_resetData(model, data)

    cord = neckcord.ShuntingThrusterCord.multicopter_hex_6(arm_m=0.25, k_yaw=0.018, u_max=8.0)
    esc = neckcord.ESCObserver(n_actuators=6, u_nom=8.0, rpm_max=9000.0)
    B = cord.W.copy()  # (4, 6)

    dt_ctrl = 0.01
    substeps = int(round(dt_ctrl / model.opt.timestep))
    n_ticks = 450  # 4.5s
    last_u = np.full(6, 1.80 * 9.81 / 6.0, dtype=np.float64)

    tilt_sq = []
    solve_us = []
    crashed = False

    for tick in range(n_ticks):
        t = tick * dt_ctrl
        # Simulate ESC RPM & load current (Rotor #2 loses blade at t >= 1.0s)
        rpm = np.sqrt(np.clip(last_u, 0.0, 8.0) / 8.0) * 9000.0 + rng.normal(0.0, 25.0, size=6)
        curr = 0.45 + 18.0 * ((np.clip(rpm, 0.0, 12000.0) / 9000.0) ** 3) + rng.normal(0.0, 0.08, size=6)
        if t >= 1.0:
            curr[2] = 0.50 + rng.normal(0.0, 0.04)  # Snapped propeller -> unloaded current!

        obs = esc.update(last_u, rpm, curr, dt=dt_ctrl)

        # Outer-loop PD hover controller -> desired 4-DOF wrench [Fz, Tx, Ty, Tz]
        z = float(data.qpos[2])
        vz = float(data.qvel[2])
        qw, qx, qy, qz = data.qpos[3:7]
        roll = math.atan2(2.0 * (qw * qx + qy * qz), 1.0 - 2.0 * (qx * qx + qy * qy))
        pitch = math.asin(max(-1.0, min(1.0, 2.0 * (qw * qy - qz * qx))))
        yaw = math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))
        wx, wy, wz = data.qvel[3:6]

        fz_des = 1.80 * 9.81 + 18.0 * (1.50 - z) - 9.0 * vz
        tx_des = -3.2 * roll - 0.55 * wx + rng.normal(0.0, 0.015)
        ty_des = -3.2 * pitch - 0.55 * wy + rng.normal(0.0, 0.015)
        # Note: with 1 dead rotor on a coplanar hexacopter, sacrificing yaw torque preserves roll/pitch/altitude!
        tz_weight = 0.15 if np.min(obs.estimated_thrust / np.maximum(last_u, 0.5)) < 0.4 else 1.0
        tz_des = tz_weight * (-0.8 * yaw - 0.20 * wz)
        w_des = np.array([fz_des, tx_des, ty_des, tz_des], dtype=np.float64)

        t0 = time.perf_counter()
        if solver_arm == "neckcord_shunting4d":
            # Feed ESC estimated thrust into ShuntingThrusterCord
            res = cord.step(w_des, feedback_thrust=obs.estimated_thrust, dt=dt_ctrl)
            u_cmd = res.commands
        elif solver_arm == "scipy_bounded_qp":
            h_w = np.where(obs.broken_prop_mask, 0.0, obs.health)
            W_row = np.diag([2.0, 4.0, 4.0, 0.25])
            A_qp = W_row @ (B * h_w[None, :])
            b_qp = W_row @ w_des
            ub = np.where(h_w > 0.1, 8.0, 1e-6)
            sol = lsq_linear(A_qp, b_qp, bounds=(np.zeros(6), ub), method="bvls")
            u_cmd = sol.x * h_w
        else:
            # Fair Clipped Health-Weighted Damped Pseudo-Inverse (gets exact ESC health & broken_prop_mask!)
            h_w = np.where(obs.broken_prop_mask, 0.0, obs.health)
            Bh = B * h_w[None, :]
            # Drop yaw row weight when faulted OR solve 3-DOF [Fz, Tx, Ty] if faulted? Standard 4D damped pinv:
            W_row = np.diag([2.0, 4.0, 4.0, 0.25])
            Bw = W_row @ Bh
            ww = W_row @ w_des
            u_raw = Bw.T @ np.linalg.solve(Bw @ Bw.T + 1e-3 * np.eye(4), ww)
            u_cmd = np.clip(u_raw * h_w, 0.0, 8.0)
        solve_us.append((time.perf_counter() - t0) * 1e6)

        last_u = u_cmd.copy()
        actual_thrust = u_cmd.copy()
        if t >= 1.0:
            actual_thrust[2] = 0.0  # Physical Rotor #2 produces zero lift/torque!
        data.ctrl[:] = actual_thrust

        for _ in range(substeps):
            mujoco.mj_step(model, data)

        tilt_deg = math.degrees(math.hypot(roll, pitch))
        if t >= 1.0:
            tilt_sq.append(tilt_deg ** 2)
        if float(data.qpos[2]) < 0.35 or tilt_deg > 55.0:
            crashed = True
            break

    return {
        "survived": 0.0 if crashed else 1.0,
        "post_fault_tilt_rms_deg": float(math.sqrt(np.mean(tilt_sq))) if tilt_sq else 90.0,
        "final_alt_m": float(data.qpos[2]),
        "solve_latency_us": float(np.mean(solve_us)),
    }


_WORKER_HEX_MODEL: mujoco.MjModel | None = None


def _init_cord_worker() -> None:
    global _WORKER_HEX_MODEL
    _WORKER_HEX_MODEL = mujoco.MjModel.from_xml_string(HEXAPOD_V3_XML)


def _eval_one_cord_shuffle(args: Tuple[Dict[str, Any], int]) -> Tuple[float, float, float]:
    shuf_vd, n_seeds = args
    model = _WORKER_HEX_MODEL if _WORKER_HEX_MODEL is not None else mujoco.MjModel.from_xml_string(HEXAPOD_V3_XML)
    t_ords = []
    for s in range(n_seeds):
        rng = np.random.default_rng(3000 + s)
        cpg = neckcord.SegmentalCPGCord(adapt_to_damage=True, connectome_weights=shuf_vd)
        cpg.phases = rng.uniform(0.0, 0.35, size=6)
        ords_s = []
        for tick in range(180):
            st = cpg.step(0.90, dt=0.01, gait_mode=int(neckcord.GaitMode.TRIPOD))
            if tick >= 80:
                ords_s.append(neckcord.compute_tripod_order(st.phases))
        t_ords.append(float(np.mean(ords_s)))

    fwds = [_eval_hexapod_episode(model, s, "v3_shuf", shuf_vd, amputate_l2=True)["post_amp_fwd_m"] for s in range(6)]
    yaws = [_eval_hexapod_episode(model, s, "v3_shuf", shuf_vd, turn_cmd=0.85, amputate_l2=False)["yaw_end_rad"] for s in range(6)]
    return float(np.mean(t_ords)), float(np.mean(fwds)), float(np.mean(yaws))


def run_cord_benchmark(
    shuffled_vd_list: List[Dict[str, Any]],
    n_seeds: int = 30,
    workers: int = 1,
) -> Dict[str, Any]:
    hex_model = mujoco.MjModel.from_xml_string(HEXAPOD_V3_XML)
    cop_model = mujoco.MjModel.from_xml_string(HEXACOPTER_V3_XML)

    # Part 4A: Connectome Hexapod Walking + Amputation + Contralateral Steering
    arms_4a = ["v3_banc_real", "v2_analytic", "openloop_tripod_parked", "openloop_tripod_limp_drag"]
    res_4a: Dict[str, Dict[str, List[float]]] = {a: {"tripod": [], "post_fwd": [], "tilt": [], "min_z": []} for a in arms_4a}

    for s in range(n_seeds):
        r_real = _eval_hexapod_episode(hex_model, s, "v3_banc_real", "banc_v888", amputate_l2=True)
        r_v2 = _eval_hexapod_episode(hex_model, s, "v2_analytic", "v2_analytic", amputate_l2=True)
        r_ol_pk = _eval_hexapod_episode(hex_model, s, "openloop_tripod_parked", amputate_l2=True)
        r_ol_dr = _eval_hexapod_episode(hex_model, s, "openloop_tripod_limp_drag", amputate_l2=True)
        for name, rr in [
            ("v3_banc_real", r_real),
            ("v2_analytic", r_v2),
            ("openloop_tripod_parked", r_ol_pk),
            ("openloop_tripod_limp_drag", r_ol_dr),
        ]:
            res_4a[name]["tripod"].append(rr["tripod_order"])
            res_4a[name]["post_fwd"].append(rr["post_amp_fwd_m"])
            res_4a[name]["tilt"].append(rr["post_amp_tilt_rms_deg"])
            res_4a[name]["min_z"].append(rr["post_amp_min_z_m"])

    # Contralateral steering test (`turn_yaw = +0.85`, intact hexapod, 12 seeds)
    real_steer_yaw = [
        _eval_hexapod_episode(hex_model, s, "v3_banc_real", "banc_v888", turn_cmd=0.85, amputate_l2=False)["yaw_end_rad"]
        for s in range(12)
    ]

    # K = len(shuffled_vd_list) Maslov-Sneppen Shuffled VNC Connectomes
    shuf_tripod_means = []
    shuf_post_fwd_means = []
    shuf_steer_yaw_means = []
    shuf_tasks = [(shuf_vd, n_seeds) for shuf_vd in shuffled_vd_list]
    if workers <= 1 or len(shuf_tasks) <= 2:
        for t_arg in shuf_tasks:
            tm, fm, ym = _eval_one_cord_shuffle(t_arg)
            shuf_tripod_means.append(tm)
            shuf_post_fwd_means.append(fm)
            shuf_steer_yaw_means.append(ym)
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_cord_worker) as pool:
            for tm, fm, ym in pool.map(_eval_one_cord_shuffle, shuf_tasks):
                shuf_tripod_means.append(tm)
                shuf_post_fwd_means.append(fm)
                shuf_steer_yaw_means.append(ym)

    # Also measure 6-seed real means for exact paired comparison with 6-seed MuJoCo shuffles
    real_fwd_6 = float(np.mean(res_4a["v3_banc_real"]["post_fwd"][:6]))
    real_yaw_6 = float(np.mean(real_steer_yaw[:6]))

    # Part 4B: Actuator Engineering (explicitly labeled PROVENANCE="engineering")
    cop_arms = ["neckcord_shunting4d", "scipy_bounded_qp", "clipped_pinv_with_esc"]
    res_4b: Dict[str, Dict[str, List[float]]] = {a: {"surv": [], "tilt": [], "lat_us": []} for a in cop_arms}
    for s in range(n_seeds):
        for a in cop_arms:
            rc = _eval_hexacopter_rotor_loss(cop_model, s, a)
            res_4b[a]["surv"].append(rc["survived"])
            res_4b[a]["tilt"].append(rc["post_fault_tilt_rms_deg"])
            res_4b[a]["lat_us"].append(rc["solve_latency_us"])

    return {
        "n_seeds": n_seeds,
        "part_4a_connectome_vnc_hexapod": {
            "provenance": "connectome",
            "v3_banc_real": {k: summary_ci95(v) for k, v in res_4a["v3_banc_real"].items()},
            "v2_analytic": {k: summary_ci95(v) for k, v in res_4a["v2_analytic"].items()},
            "openloop_tripod_parked": {k: summary_ci95(v) for k, v in res_4a["openloop_tripod_parked"].items()},
            "openloop_tripod_limp_drag": {k: summary_ci95(v) for k, v in res_4a["openloop_tripod_limp_drag"].items()},
            "steering_yaw_rad_real": summary_ci95(real_steer_yaw),
            "shuffle_gate_emergent_tripod_order": shuffle_gate_verdict(
                float(np.mean(res_4a["v3_banc_real"]["tripod"])),
                shuf_tripod_means,
                higher_is_better=True,
            ),
            "shuffle_gate_mujoco_post_amp_fwd_m": shuffle_gate_verdict(
                real_fwd_6,
                shuf_post_fwd_means,
                higher_is_better=True,
            ),
            "shuffle_gate_mujoco_steering_yaw_rad": shuffle_gate_verdict(
                real_yaw_6,
                shuf_steer_yaw_means,
                higher_is_better=True,
            ),
        },
        "part_4b_engineering_hexacopter_allocator": {
            "provenance": "engineering (NOT claimed as connectome-derived)",
            "neckcord_shunting4d": {k: summary_ci95(v) for k, v in res_4b["neckcord_shunting4d"].items()},
            "scipy_bounded_qp": {k: summary_ci95(v) for k, v in res_4b["scipy_bounded_qp"].items()},
            "clipped_pinv_with_esc": {k: summary_ci95(v) for k, v in res_4b["clipped_pinv_with_esc"].items()},
        },
    }
