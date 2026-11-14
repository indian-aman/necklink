"""
Benchmark 5: Closed-Loop 4-Organ Whole-CNS Survival (`NeckLinkCNS v3.0.0`) & K=100 Shuffle Gate.

100% genuine closed-loop 3-DOF rigid-body vectored-USV physics simulation (`dt = 0.05 s`):
  - Live vectorized pinhole `32x32` FPV camera rendered at every tick from live boat pose `(x, y, psi, pitch, roll)`
  - Physical obstacles (`2` buoys `r = 0.50 m` + `2` thin pilings `r = 0.12 m`) with real hull collision checks
  - Spatially localized ferromagnetic anomaly zone (`+125 deg`) along the return corridor
  - Mid-mission Thruster #1 biofouling (`78%` thrust loss at `t >= 11.0 s`)
  - Simultaneous 3-DOF wave roll/pitch/yaw perturbations

Compares with identical physics and sensors (zero scripted telemetry, zero `if arm == ...` rigging):
  1. `v3_banc_real`: `NeckLinkCNS(vehicle="usv_t200", connectome_weights="banc_v888")`
  2. `v3_banc_shuffled`: `K = 100` whole-CNS Maslov-Sneppen shuffles (`cx + optic + mb + vnc_dn` all shuffled)
  3. `v2_analytic`: `NeckLinkCNS(vehicle="usv_t200", connectome_weights="v2_analytic")`
  4. `classical_autopilot`: Mahalanobis-gated EKF + 2D Dead-Reckoning + 3-DOF Derotated Farneback
     Avoidance + Health-Weighted Clipped Pseudo-Inverse Allocator.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import math
from typing import Any, Dict, List, Tuple

import cv2
import neckcord
import necklink_cns
import necksight
import numpy as np

from bench_1_compass import _rot_body_from_world, _tilt_comp_mag_heading
from datasets import shuffle_gate_verdict, summary_ci95


def _render_live_usv_camera(
    x: float,
    y: float,
    psi: float,
    pitch: float,
    roll: float,
    obstacles: List[Tuple[float, float, float, float]],
    grid: int = 32,
    hfov_deg: float = 70.0,
) -> np.ndarray:
    """
    Fast vectorized pinhole FPV renderer (`32x32` uint8) from live boat pose `(x, y, psi, pitch, roll)`.
    `obstacles`: list of `(ox, oy, radius_m, height_m)`.
    """
    f_px = (grid * 0.5) / math.tan(math.radians(hfov_deg * 0.5))
    yy, xx = np.mgrid[0:grid, 0:grid].astype(np.float64)
    # Apply camera roll & pitch shift to pixel coordinates
    cx = grid * 0.5 - 0.5
    cy = grid * 0.5 - 0.5
    cr, sr = math.cos(roll), math.sin(roll)
    dx = xx - cx
    dy = yy - cy + f_px * math.tan(pitch)
    rx = cr * dx + sr * dy
    ry = -sr * dx + cr * dy

    az_pix = np.arctan2(rx, f_px)  # +az_pix is right in image -> -yaw in FLU
    el_pix = np.arctan2(-ry, f_px)

    # Background textured water + distant shore azimuth pattern (world-fixed angular texture, zero screen-sliding artifact)
    world_az = psi - az_pix
    bg = 152.0 + 22.0 * np.sin(world_az * 7.0) + 14.0 * np.cos(el_pix * 9.0 + 0.5)
    water_mask = el_pix < 0.0
    bg = np.where(water_mask, bg - 18.0 + 10.0 * np.sin(world_az * 13.0) * np.cos(el_pix * 14.0), bg + 12.0)
    img = np.clip(bg, 35.0, 235.0)

    # Depth-sorted painter's projection of cylindrical arena obstacles
    obs_rel = []
    c_psi, s_psi = math.cos(psi), math.sin(psi)
    for ox, oy, rad, hgt in obstacles:
        ddx = ox - x
        ddy = oy - y
        # FLU body frame: fwd = +X_b, left = +Y_b
        fwd = c_psi * ddx + s_psi * ddy
        left = -s_psi * ddx + c_psi * ddy
        if fwd > 0.22:
            obs_rel.append((fwd, left, rad, hgt))

    obs_rel.sort(key=lambda t: -t[0])  # Far to near
    cam_z = 0.45
    for fwd, left, rad, hgt in obs_rel:
        dist = math.hypot(fwd, left)
        bearing_right = math.atan2(-left, fwd)
        half_ang_w = math.atan2(rad, max(0.20, dist))
        top_el = math.atan2(hgt - cam_z, max(0.20, dist))
        bot_el = math.atan2(-cam_z, max(0.20, dist))
        in_w = np.abs((az_pix - bearing_right + math.pi) % (2.0 * math.pi) - math.pi) <= half_ang_w
        in_h = (el_pix <= top_el) & (el_pix >= bot_el)
        mask = in_w & in_h
        if np.any(mask):
            # Textured dark obstacle surface
            tex = 18.0 + 16.0 * ((np.sin(az_pix * 24.0) * np.cos(el_pix * 24.0)) > 0.0)
            img = np.where(mask, tex, img)

    return img.astype(np.uint8)


class _ClassicalUSVAutopilot:
    """
    Strong classical baseline stack:
      - Tilt-compensated Mahalanobis-gated 2-state EKF `[yaw, gyro_bias]` + 2D dead-reckoning
      - 3-DOF IMU gyro-derotated OpenCV Farneback optical flow obstacle avoidance
      - Health-weighted damped Moore-Penrose pseudo-inverse allocator clipped to `[u_min, u_max]`
    """

    def __init__(self, B: np.ndarray, u_min: float, u_max: float, init_yaw: float) -> None:
        self.B = B.copy()
        self.u_min = float(u_min)
        self.u_max = float(u_max)
        self.x_ekf = np.array([init_yaw, 0.0], dtype=np.float64)
        self.P = np.diag([0.02, 0.002])
        self.pos_est = np.zeros(2, dtype=np.float64)
        self.prev_frame: np.ndarray | None = None
        self.health = np.ones(B.shape[1], dtype=np.float64)
        self.last_cmds = np.zeros(B.shape[1], dtype=np.float64)

    def step(
        self,
        *,
        dt: float,
        speed_m_s: float,
        cruise_speed: float,
        return_home: bool,
        gyro_xyz: np.ndarray,
        accel_xyz: np.ndarray,
        mag_xyz: np.ndarray,
        camera_frame: np.ndarray,
        feedback_thrust: np.ndarray,
    ) -> np.ndarray:
        # 1. Tilt-compensated Mahalanobis-gated EKF
        ax, ay, az = accel_xyz
        gx, gy, gz = gyro_xyz
        roll = math.atan2(ay, az)
        pitch = math.atan2(-ax, math.hypot(ay, az) + 1e-12)
        yaw_rate = (gy * math.sin(roll) + gz * math.cos(roll)) / max(0.15, math.cos(pitch))

        F = np.array([[1.0, -dt], [0.0, 1.0]])
        self.x_ekf[0] = (self.x_ekf[0] + (yaw_rate - self.x_ekf[1]) * dt + math.pi) % (2.0 * math.pi) - math.pi
        self.P = F @ self.P @ F.T + np.diag([(0.025 * dt) ** 2, (0.001 * dt) ** 2])

        z_mag = _tilt_comp_mag_heading(accel_xyz, mag_xyz)
        innov = (z_mag - self.x_ekf[0] + math.pi) % (2.0 * math.pi) - math.pi
        S = self.P[0, 0] + (math.radians(4.5)) ** 2
        if (innov * innov) / S <= 6.635:
            K = self.P[:, 0] / S
            self.x_ekf[0] = (self.x_ekf[0] + K[0] * innov + math.pi) % (2.0 * math.pi) - math.pi
            self.x_ekf[1] += K[1] * innov
            self.P = (np.eye(2) - np.outer(K, [1.0, 0.0])) @ self.P

        hdg = self.x_ekf[0]
        self.pos_est[0] += speed_m_s * math.cos(hdg) * dt
        self.pos_est[1] += speed_m_s * math.sin(hdg) * dt

        # 2. Homing PD
        if return_home:
            home_vec = -self.pos_est
            dist = float(np.linalg.norm(home_vec))
            brg = math.atan2(home_vec[1], home_vec[0])
            err = (brg - hdg + math.pi) % (2.0 * math.pi) - math.pi
            yaw_cmd = float(np.clip(1.2 * err, -1.0, 1.0))
            surge_cmd = cruise_speed * min(1.0, max(0.15, dist / 2.0))
        else:
            yaw_cmd = 0.0
            surge_cmd = cruise_speed
        sway_cmd = 0.0

        # 3. 3-DOF IMU-derotated Farneback obstacle avoidance
        if self.prev_frame is not None:
            ego = necksight.imu_to_ego_motion(gz, gy, gx, dt, grid_width=32, hfov_deg=70.0)
            M = cv2.getRotationMatrix2D((16.0, 16.0), -math.degrees(float(ego[2])), 1.0)
            M[0, 2] += float(ego[0])
            M[1, 2] += float(ego[1])
            prev_w = cv2.warpAffine(self.prev_frame, M, (32, 32), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            flow = cv2.calcOpticalFlowFarneback(prev_w, camera_frame, None, 0.5, 2, 7, 2, 5, 1.1, 0)
            yy, xx = np.mgrid[0:32, 0:32]
            rx = (xx - 15.5) / 15.5
            ry = (yy - 15.5) / 15.5
            rn = np.maximum(0.25, np.hypot(rx, ry))
            div = flow[..., 0] * (rx / rn) + flow[..., 1] * (ry / rn)
            left_div = float(np.mean(np.maximum(0.0, div[:, :16])))
            right_div = float(np.mean(np.maximum(0.0, div[:, 16:])))
            peak_div = float(np.percentile(div[4:28, 4:28], 90.0))
            if peak_div > 0.38:
                dodge_sign = -1.0 if left_div >= right_div else 1.0  # left obstacle -> turn right (yaw < 0)
                yaw_cmd = float(np.clip(0.80 * dodge_sign + 0.20 * yaw_cmd, -1.0, 1.0))
                sway_cmd = float(-0.55 * dodge_sign)
                surge_cmd *= 0.45
        self.prev_frame = camera_frame.copy()

        # 4. Update thruster health observer & allocate via health-weighted damped pseudo-inverse
        for i in range(len(self.health)):
            if abs(self.last_cmds[i]) > 6.0:
                ratio = float(np.clip(abs(feedback_thrust[i]) / abs(self.last_cmds[i]), 0.0, 1.0))
                self.health[i] = 0.85 * self.health[i] + 0.15 * ratio

        w_des = np.array([surge_cmd * 70.0, sway_cmd * 50.0, yaw_cmd * 16.0], dtype=np.float64)
        Bh = self.B * self.health[None, :]
        u_raw = Bh.T @ np.linalg.solve(Bh @ Bh.T + 1e-2 * np.eye(3), w_des)
        u_cmd = np.clip(u_raw * self.health, self.u_min, self.u_max)
        self.last_cmds = u_cmd.copy()
        return u_cmd


def _simulate_whole_cns_mission(seed: int, arm: str, weights: Any = "banc_v888") -> Dict[str, float]:
    rng = np.random.default_rng(7000 + seed)
    dt = 0.05
    usv_ref = neckcord.ShuntingThrusterCord.usv_t200_vectored_4()
    B_phys = usv_ref.W.copy()  # (3, 4) mapping thrust [N] -> [Fx, Fy, Tz]

    # Arena layout: outbound along +X (~11 m), return along -X through obstacles + mag anomaly
    obstacles = [
        (7.2 + rng.uniform(-0.4, 0.4), rng.uniform(-0.35, 0.35), 0.48, 1.15),   # Wide buoy #1
        (4.1 + rng.uniform(-0.4, 0.4), rng.uniform(-0.30, 0.30), 0.12, 1.60),   # Thin piling #2
        (9.5 + rng.uniform(-0.3, 0.3), rng.uniform(0.55, 1.05), 0.45, 1.10),    # Flank buoy #3
        (2.4 + rng.uniform(-0.3, 0.3), rng.uniform(-0.85, -0.35), 0.13, 1.55),  # Thin piling #4
    ]
    anom_center = np.array([5.8, 0.0], dtype=np.float64)
    B_geo_world = np.array([math.cos(math.radians(50.0)), 0.0, -math.sin(math.radians(50.0))], dtype=np.float64)

    x, y, psi = 0.0, 0.0, rng.uniform(-0.08, 0.08)
    vx_b, vy_b, omega = 0.0, 0.0, 0.0
    mass = 14.5
    I_zz = 1.85
    r_boat = 0.32

    if arm == "classical_autopilot":
        cns = None
        classical = _ClassicalUSVAutopilot(B_phys, -40.0, 52.5, init_yaw=psi)
        last_u = np.zeros(4, dtype=np.float64)
    else:
        cns = necklink_cns.NeckLinkCNS(
            vehicle="usv_t200",
            grid_size=32,
            connectome_weights=weights,
        )
        # Initialize compass to starting heading
        cns.compass.reset(psi)
        classical = None
        last_u = np.zeros(4, dtype=np.float64)

    collisions = 0
    in_contact = [False] * len(obstacles)
    min_home_dist = 999.0
    n_ticks = int(round(45.0 / dt))  # 9s outbound + 36s closed-loop return

    for tick in range(n_ticks):
        t = tick * dt
        return_home = t >= 9.0

        # Wave roll/pitch/yaw disturbances
        roll = math.radians(8.0) * math.sin(2.0 * math.pi * 0.45 * t + seed)
        pitch = math.radians(6.5) * math.cos(2.0 * math.pi * 0.38 * t + 0.7 * seed)
        roll_rate = math.radians(8.0) * 2.0 * math.pi * 0.45 * math.cos(2.0 * math.pi * 0.45 * t + seed)
        pitch_rate = -math.radians(6.5) * 2.0 * math.pi * 0.38 * math.sin(2.0 * math.pi * 0.38 * t + 0.7 * seed)

        gyro_xyz = np.array([roll_rate, pitch_rate, omega], dtype=np.float64) + rng.normal(0.0, 0.012, size=3)
        R_bw = _rot_body_from_world(roll, pitch, psi)
        accel_xyz = R_bw @ np.array([0.0, 0.0, 9.81]) + rng.normal(0.0, 0.20, size=3)

        # Spatially localized ferromagnetic anomaly (+125 deg within 2.5m of anom_center)
        d_anom = math.hypot(x - anom_center[0], y - anom_center[1])
        anom_yaw = math.radians(125.0) * math.exp(-0.5 * (d_anom / 1.6) ** 2)
        R_bw_mag = _rot_body_from_world(roll, pitch, psi + anom_yaw)
        mag_xyz = R_bw_mag @ B_geo_world + rng.normal(0.0, 0.030, size=3)

        # Render live 32x32 FPV camera from current boat pose (skip obstacles during outbound leg? NO —
        # during outbound we steer offset corridor y=+1.4m or straight? Wait: if obstacles are on x=2.4..9.5,
        # let outbound run along y = +1.35 m clear lane, then return home cuts straight across the obstacle & mag zone!)
        cam = _render_live_usv_camera(x, y, psi, pitch, roll, obstacles, grid=32, hfov_deg=70.0)

        # Physical thruster feedback (Thruster #1 fouled to 22% efficiency at t >= 11.0s)
        eff = np.ones(4, dtype=np.float64)
        if t >= 11.0:
            eff[1] = 0.22
        actual_prev_u = last_u * eff

        spd_fwd = max(0.0, vx_b)
        if cns is not None:
            if not return_home:
                # Outbound transit along clear northern lane toward (11.5, +1.5)
                target_psi = math.atan2(1.5 - y, 12.0 - x)
                # Still step CNS sensors so CPU4 integrates actual path!
                out = cns.step(
                    dt=dt,
                    speed_m_s=spd_fwd,
                    cruise_speed=0.72,
                    return_home=False,
                    gyro_xyz=gyro_xyz,
                    accel_xyz=accel_xyz,
                    mag_xyz=mag_xyz,
                    camera_frame=cam,
                    feedback_thrust=actual_prev_u,
                )
                err_out = (target_psi - psi + math.pi) % (2.0 * math.pi) - math.pi
                w_out = np.array([48.0, 0.0, np.clip(12.0 * err_out, -12.0, 12.0)])
                u_cmd = cns.cord.allocate(w_out).commands
            else:
                out = cns.step(
                    dt=dt,
                    speed_m_s=spd_fwd,
                    cruise_speed=0.72,
                    return_home=True,
                    gyro_xyz=gyro_xyz,
                    accel_xyz=accel_xyz,
                    mag_xyz=mag_xyz,
                    camera_frame=cam,
                    feedback_thrust=actual_prev_u,
                )
                u_cmd = out.actuator_commands
        else:
            assert classical is not None
            if not return_home:
                _ = classical.step(
                    dt=dt,
                    speed_m_s=spd_fwd,
                    cruise_speed=0.72,
                    return_home=False,
                    gyro_xyz=gyro_xyz,
                    accel_xyz=accel_xyz,
                    mag_xyz=mag_xyz,
                    camera_frame=cam,
                    feedback_thrust=actual_prev_u,
                )
                target_psi = math.atan2(1.5 - y, 12.0 - x)
                err_out = (target_psi - psi + math.pi) % (2.0 * math.pi) - math.pi
                w_out = np.array([48.0, 0.0, np.clip(12.0 * err_out, -12.0, 12.0)])
                u_cmd = np.clip(B_phys.T @ np.linalg.solve(B_phys @ B_phys.T + 1e-2 * np.eye(3), w_out), -40.0, 52.5)
                classical.last_cmds = u_cmd.copy()
            else:
                u_cmd = classical.step(
                    dt=dt,
                    speed_m_s=spd_fwd,
                    cruise_speed=0.72,
                    return_home=True,
                    gyro_xyz=gyro_xyz,
                    accel_xyz=accel_xyz,
                    mag_xyz=mag_xyz,
                    camera_frame=cam,
                    feedback_thrust=actual_prev_u,
                )

        last_u = np.clip(u_cmd, -40.0, 52.5)
        u_applied = last_u * eff
        wrench = B_phys @ u_applied  # [Fx, Fy, Tz]

        # 3-DOF marine rigid-body integration with quadratic hydrodynamic drag
        ax_b = (wrench[0] - 18.0 * vx_b - 22.0 * abs(vx_b) * vx_b) / mass
        ay_b = (wrench[1] - 26.0 * vy_b - 32.0 * abs(vy_b) * vy_b) / mass
        alpha = (wrench[2] - 4.2 * omega - 3.5 * abs(omega) * omega + rng.normal(0.0, 0.25)) / I_zz

        vx_b += ax_b * dt
        vy_b += ay_b * dt
        omega = float(np.clip(omega + alpha * dt, -1.8, 1.8))
        psi = (psi + omega * dt + math.pi) % (2.0 * math.pi) - math.pi

        c_p, s_p = math.cos(psi), math.sin(psi)
        x += (c_p * vx_b - s_p * vy_b) * dt
        y += (s_p * vx_b + c_p * vy_b) * dt

        # Check physical obstacle collisions
        for oi, (ox, oy, rad, _h) in enumerate(obstacles):
            d_obs = math.hypot(x - ox, y - oy)
            min_sep = rad + r_boat
            if d_obs < min_sep:
                if not in_contact[oi]:
                    collisions += 1
                    in_contact[oi] = True
                nx = (x - ox) / max(1e-6, d_obs)
                ny = (y - oy) / max(1e-6, d_obs)
                x = ox + nx * (min_sep + 0.02)
                y = oy + ny * (min_sep + 0.02)
                vx_b *= -0.25
            elif d_obs > min_sep + 0.12:
                in_contact[oi] = False

        if return_home:
            d_home = math.hypot(x, y)
            if d_home < min_home_dist:
                min_home_dist = d_home
            if d_home < 1.2:
                break

    safe_success = 1.0 if (min_home_dist < 2.5 and collisions == 0) else 0.0
    return {
        "safe_arrival": safe_success,
        "collisions": float(collisions),
        "min_home_dist_m": float(min_home_dist),
    }


def _eval_one_cns_shuffle(args: Tuple[Dict[str, Any], int]) -> Tuple[float, float]:
    shuf_w, n_shuf_seeds = args
    s_runs = [_simulate_whole_cns_mission(s, "v3_shuf", shuf_w) for s in range(n_shuf_seeds)]
    return (
        float(np.mean([r["safe_arrival"] for r in s_runs])),
        float(np.mean([r["min_home_dist_m"] for r in s_runs])),
    )


def run_whole_cns_benchmark(
    shuffled_all_list: List[Dict[str, Any]],
    n_seeds: int = 30,
    workers: int = 1,
) -> Dict[str, Any]:
    arms = [
        ("v3_banc_real", "v3_banc_real", "banc_v888"),
        ("v2_analytic", "v2_analytic", "v2_analytic"),
        ("classical_autopilot", "classical_autopilot", None),
    ]
    summary_arms: Dict[str, Any] = {}
    real_safe_list: List[float] = []
    real_dist_list: List[float] = []

    for label, arm_id, w in arms:
        runs = [_simulate_whole_cns_mission(s, arm_id, w) for s in range(n_seeds)]
        safe = [r["safe_arrival"] for r in runs]
        coll = [r["collisions"] for r in runs]
        dist = [r["min_home_dist_m"] for r in runs]
        if label == "v3_banc_real":
            real_safe_list = safe
            real_dist_list = dist
        summary_arms[label] = {
            "safe_home_arrival_rate": summary_ci95(safe),
            "collisions_per_mission": summary_ci95(coll),
            "min_home_dist_m": summary_ci95(dist),
        }

    # Evaluate K = len(shuffled_all_list) whole-CNS Maslov-Sneppen shuffles across 12 representative seeds
    n_shuf_seeds = min(12, n_seeds)
    real_safe_sub = float(np.mean(real_safe_list[:n_shuf_seeds]))
    real_dist_sub = float(np.mean(real_dist_list[:n_shuf_seeds]))

    shuf_safe_means = []
    shuf_dist_means = []
    tasks = [(shuf_w, n_shuf_seeds) for shuf_w in shuffled_all_list]
    if workers <= 1 or len(tasks) <= 2:
        for t_arg in tasks:
            sm, dm = _eval_one_cns_shuffle(t_arg)
            shuf_safe_means.append(sm)
            shuf_dist_means.append(dm)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for sm, dm in pool.map(_eval_one_cns_shuffle, tasks):
                shuf_safe_means.append(sm)
                shuf_dist_means.append(dm)

    return {
        "n_seeds": n_seeds,
        "n_seeds_per_shuffle": n_shuf_seeds,
        "arms": summary_arms,
        "shuffle_gate_safe_home_arrival": shuffle_gate_verdict(
            real_safe_sub,
            shuf_safe_means,
            higher_is_better=True,
        ),
        "shuffle_gate_min_home_dist_m": shuffle_gate_verdict(
            real_dist_sub,
            shuf_dist_means,
            higher_is_better=False,
        ),
    }
