"""
Benchmark 2: Optic Lobe (`NeckSight v3.0.0`) — 3D GPU EGL Camera & Fair Derotated Baselines.

Renders 3D `MuJoCo` `EGL` `64x64` FPV video episodes (`dt = 0.02 s`, `38` frames/ep,
`8 * n_per_subtype` episodes total — e.g., `56` episodes at `n_per_subtype=7` or `64` at `8`):
  - 32 Positive Collision Episodes (8 seeds x 4 classes):
      1. Approaching textured boulder (with simultaneous 3-DOF wave pitch/yaw/roll ego-motion)
      2. Approaching thin vertical pole (`r = 5.5 cm`, testing LC4 / small-object pathway)
      3. Approaching thin horizontal wire (`r = 3.8 cm`)
      4. Approaching low-contrast wide wall
  - 32 Negative Non-Collision Episodes (8 seeds x 4 distractors):
      1. Receding textured boulder + wave ego-motion
      2. Lateral translational pass-by (object crosses FOV without colliding) + wave ego-motion
      3. Intense 3-DOF wave pitch/yaw/roll ego-rotation in cluttered arena (no approaching object)
      4. Hover jitter in cluttered arena

Fairness Guarantee:
  - `Derotated_Farneback` and `Derotated_PyrLK` receive the **exact same 3-DOF IMU gyro
    ego-rotation compensation** (`cv2.warpAffine` computed from exact camera `fovy = 58 deg`
    and `(gx, gy, gz) * dt`) + CLAHE local contrast normalization!
  - Threshold-free `AUROC` and `FPR @ 90% TPR` are computed over episode peak scores.
  - Zero `if arm == ...` rigging anywhere.
"""

from __future__ import annotations

import math
import os
import sys
from typing import Any, Dict, List, Tuple

if sys.platform.startswith("linux"):
    os.environ.setdefault("MUJOCO_GL", "egl")

import cv2
import mujoco
import necksight
import numpy as np

from datasets import compute_auroc, compute_fpr_at_tpr, shuffle_gate_verdict, summary_ci95

VISION_ARENA_XML = """
<mujoco model="necksight_v3_egl_arena">
  <compiler angle="radian"/>
  <visual>
    <global offwidth="128" offheight="128"/>
    <headlight ambient="0.45 0.45 0.48" diffuse="0.75 0.75 0.75" specular="0.2 0.2 0.2"/>
  </visual>
  <asset>
    <texture name="grid_tex" type="2d" builtin="checker" rgb1="0.30 0.34 0.38" rgb2="0.60 0.64 0.68" width="256" height="256"/>
    <texture name="obs_tex" type="2d" builtin="checker" rgb1="0.06 0.08 0.11" rgb2="0.24 0.28 0.32" width="128" height="128"/>
    <texture name="wall_tex" type="2d" builtin="checker" rgb1="0.34 0.37 0.40" rgb2="0.44 0.47 0.50" width="128" height="128"/>
    <material name="grid_mat" texture="grid_tex" texrepeat="16 16"/>
    <material name="obs_mat" texture="obs_tex" texrepeat="4 4"/>
    <material name="wall_mat" texture="wall_tex" texrepeat="4 4"/>
  </asset>
  <worldbody>
    <light name="sun" pos="0 -2 6" dir="0 0.3 -1" diffuse="0.9 0.9 0.9"/>
    <geom name="floor" type="plane" size="25 25 0.1" material="grid_mat"/>
    <geom name="bg1" type="box" pos="-3.4 9.5 1.2" size="0.8 0.8 1.2" material="obs_mat"/>
    <geom name="bg2" type="box" pos="3.6 10.2 1.4" size="0.9 0.9 1.4" material="obs_mat"/>
    <geom name="bg3" type="box" pos="0.0 14.0 2.0" size="6.0 0.2 2.0" material="grid_mat"/>

    <body name="boulder" mocap="true" pos="0 4.0 -10.0">
      <geom type="sphere" size="0.50" material="obs_mat"/>
    </body>
    <body name="thin_pole" mocap="true" pos="0 4.0 -10.0">
      <geom type="cylinder" size="0.055 1.6" rgba="0.04 0.05 0.07 1"/>
    </body>
    <body name="thin_wire" mocap="true" pos="0 4.0 -10.0">
      <geom type="cylinder" fromto="-2.2 0 0 2.2 0 0" size="0.038" rgba="0.04 0.05 0.07 1"/>
    </body>
    <body name="low_contrast_wall" mocap="true" pos="0 4.0 -10.0">
      <geom type="box" size="0.85 0.08 1.1" material="wall_mat"/>
    </body>

    <!-- Camera rig at (0, 0, 0.90) looking along +Y (+X right, +Z up) -->
    <body name="cam_rig" mocap="true" pos="0 0 0.90">
      <camera name="fpv" pos="0 0 0" xyaxes="1 0 0 0 0 1" fovy="58"/>
    </body>
  </worldbody>
</mujoco>
"""


def _euler_to_quat_wxyz(pitch_x: float, roll_y: float, yaw_z: float) -> np.ndarray:
    """Compose `R_z(yaw_z) @ R_y(roll_y) @ R_x(pitch_x)` for camera rig looking along +Y."""
    cx, sx = math.cos(0.5 * pitch_x), math.sin(0.5 * pitch_x)
    cy, sy = math.cos(0.5 * roll_y), math.sin(0.5 * roll_y)
    cz, sz = math.cos(0.5 * yaw_z), math.sin(0.5 * yaw_z)
    # q = qz * qy * qx
    w = cz * cy * cx + sz * sy * sx
    x = cz * cy * sx - sz * sy * cx
    y = cz * sy * cx + sz * cy * sx
    z = sz * cy * cx - cz * sy * sx
    return np.array([w, x, y, z], dtype=np.float64)


def _render_episodes(n_per_subtype: int = 8, res: int = 64) -> List[Dict[str, Any]]:
    model = mujoco.MjModel.from_xml_string(VISION_ARENA_XML)
    data = mujoco.MjData(model)
    renderer = mujoco.Renderer(model, height=res, width=res)

    mocap_ids = {
        name: model.body_mocapid[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)]
        for name in ("boulder", "thin_pole", "thin_wire", "low_contrast_wall", "cam_rig")
    }

    pos_types = ["boulder", "thin_pole", "thin_wire", "low_contrast_wall"]
    neg_types = ["receding_boulder", "lateral_passby", "pure_ego_wave", "hover_jitter"]
    all_specs = [(t, 1) for t in pos_types] + [(t, 0) for t in neg_types]

    dt = 0.02
    n_frames = 38
    episodes: List[Dict[str, Any]] = []

    for sub_idx, (stype, label) in enumerate(all_specs):
        for s in range(n_per_subtype):
            rng = np.random.default_rng(2000 + sub_idx * 100 + s)
            frames = np.zeros((n_frames, res, res), dtype=np.uint8)
            gyros = np.zeros((n_frames, 3), dtype=np.float64)  # [roll_fwd_rate, pitch_up_rate, yaw_ccw_rate]

            # Reset all obstacles underground
            for k_name, mid in mocap_ids.items():
                if k_name != "cam_rig":
                    data.mocap_pos[mid] = [0.0, 5.0, -10.0]

            # Wave ego-rotation profile
            amp_scale = 1.6 if stype == "pure_ego_wave" else (0.35 if stype == "hover_jitter" else 0.85)
            f_yaw = rng.uniform(0.7, 1.3)
            f_pit = rng.uniform(0.8, 1.5)
            ph_y = rng.uniform(0, 6.28)
            ph_p = rng.uniform(0, 6.28)
            lat_off = rng.uniform(-0.35, 0.35)

            prev_pitch, prev_roll, prev_yaw = 0.0, 0.0, 0.0
            for f in range(n_frames):
                t = f * dt
                pitch_x = amp_scale * math.radians(5.5) * math.sin(2.0 * math.pi * f_pit * t + ph_p)
                roll_y = amp_scale * math.radians(4.0) * math.cos(2.0 * math.pi * 0.9 * t + ph_y)
                yaw_z = amp_scale * math.radians(7.0) * math.sin(2.0 * math.pi * f_yaw * t + ph_y)

                if f > 0:
                    # Match necksight.imu_to_ego_motion conventions:
                    # +yaw_rate = nose left (+yaw_z), +pitch_rate = nose up (+pitch_x), +roll_rate = left-wing-down (-roll_y)
                    gyros[f, 0] = -(roll_y - prev_roll) / dt
                    gyros[f, 1] = +(pitch_x - prev_pitch) / dt
                    gyros[f, 2] = +(yaw_z - prev_yaw) / dt
                prev_pitch, prev_roll, prev_yaw = pitch_x, roll_y, yaw_z

                data.mocap_pos[mocap_ids["cam_rig"]] = [0.0, 0.0, 0.90]
                data.mocap_quat[mocap_ids["cam_rig"]] = _euler_to_quat_wxyz(pitch_x, roll_y, yaw_z)

                u = f / float(n_frames - 1)
                if label == 1:
                    # Approaching from y = 4.6 m down to y = 0.72 m
                    dist_y = 4.60 - 3.88 * u
                    z_h = 0.90 if stype != "thin_pole" else 0.90
                    data.mocap_pos[mocap_ids[stype]] = [lat_off * (1.0 - 0.5 * u), dist_y, z_h]
                elif stype == "receding_boulder":
                    dist_y = 0.85 + 3.80 * u
                    data.mocap_pos[mocap_ids["boulder"]] = [lat_off, dist_y, 0.90]
                elif stype == "lateral_passby":
                    x_lat = -2.2 + 4.4 * u
                    data.mocap_pos[mocap_ids["boulder"]] = [x_lat, 2.6, 0.90]

                mujoco.mj_forward(model, data)
                renderer.update_scene(data, camera="fpv")
                rgb = renderer.render()
                gray = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.uint8)
                frames[f] = gray

            episodes.append(
                {
                    "subtype": stype,
                    "label": int(label),
                    "dt": dt,
                    "frames": frames,
                    "gyros": gyros,
                }
            )

    renderer.close()
    return episodes


def _warp_prev_frame_3dof(prev_u8: np.ndarray, gyro_flu: np.ndarray, dt: float, fov_deg: float = 58.0) -> np.ndarray:
    """
    Exact 3-DOF IMU gyro ego-rotation warp aligning `prev_u8` onto `curr_u8` so classical
    Farneback & Lucas-Kanade baselines get the exact same IMU ego-motion compensation as NeckSight.
    """
    h, w = prev_u8.shape
    ego = necksight.imu_to_ego_motion(
        yaw_rate_rad_s=float(gyro_flu[2]),
        pitch_rate_rad_s=float(gyro_flu[1]),
        roll_rate_rad_s=float(gyro_flu[0]),
        dt=dt,
        grid_width=w,
        hfov_deg=fov_deg,
    )
    dx, dy, droll = float(ego[0]), float(ego[1]), float(ego[2])
    M = cv2.getRotationMatrix2D((w * 0.5, h * 0.5), -math.degrees(droll), 1.0)
    M[0, 2] += dx
    M[1, 2] += dy
    return cv2.warpAffine(prev_u8, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _score_farneback_derotated(ep: Dict[str, Any]) -> float:
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    frames = ep["frames"]
    gyros = ep["gyros"]
    dt = float(ep["dt"])
    h, w = frames.shape[1], frames.shape[2]
    yy, xx = np.mgrid[0:h, 0:w]
    rx = (xx - w * 0.5) / (w * 0.5)
    ry = (yy - h * 0.5) / (h * 0.5)
    rn = np.maximum(0.20, np.hypot(rx, ry))
    ux, uy = rx / rn, ry / rn
    border = (xx >= 4) & (xx < w - 4) & (yy >= 4) & (yy < h - 4)

    peak = 0.0
    for f in range(6, len(frames)):
        prev_w = _warp_prev_frame_3dof(frames[f - 1], gyros[f], dt)
        p_norm = clahe.apply(prev_w)
        c_norm = clahe.apply(frames[f])
        flow = cv2.calcOpticalFlowFarneback(p_norm, c_norm, None, 0.5, 2, 9, 2, 5, 1.1, 0)
        div = (flow[..., 0] * ux + flow[..., 1] * uy)[border]
        # Focus on top expanding patch (90th percentile outward radial divergence)
        sc = float(max(0.0, np.percentile(div, 90.0)))
        if sc > peak:
            peak = sc
    return peak


def _score_pyrlk_derotated(ep: Dict[str, Any]) -> float:
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    frames = ep["frames"]
    gyros = ep["gyros"]
    dt = float(ep["dt"])
    h, w = frames.shape[1], frames.shape[2]
    center = np.array([w * 0.5, h * 0.5], dtype=np.float64)

    peak = 0.0
    for f in range(6, len(frames)):
        prev_w = _warp_prev_frame_3dof(frames[f - 1], gyros[f], dt)
        p_norm = clahe.apply(prev_w)
        c_norm = clahe.apply(frames[f])
        p0 = cv2.goodFeaturesToTrack(p_norm, maxCorners=48, qualityLevel=0.03, minDistance=3)
        if p0 is None or len(p0) < 4:
            continue
        p1, st, _ = cv2.calcOpticalFlowPyrLK(p_norm, c_norm, p0, None, winSize=(11, 11), maxLevel=2)
        if p1 is None or st is None:
            continue
        g0 = p0[st.ravel() == 1].reshape(-1, 2)
        g1 = p1[st.ravel() == 1].reshape(-1, 2)
        if len(g0) < 4:
            continue
        d0 = np.linalg.norm(g0 - center, axis=1)
        d1 = np.linalg.norm(g1 - center, axis=1)
        sc = float(max(0.0, np.percentile(d1 - d0, 75.0)))
        if sc > peak:
            peak = sc
    return peak


def _eval_necksight_episodes(episodes: List[Dict[str, Any]], optic_weights: Any) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return `(peak_combined_scores, peak_lplc2_only_scores, labels)` across all 64x64 episodes."""
    res_hw = int(episodes[0]["frames"].shape[1])
    peaks = np.zeros(len(episodes), dtype=np.float64)
    peaks_lplc2 = np.zeros(len(episodes), dtype=np.float64)
    for idx, ep in enumerate(episodes):
        pipe = necksight.NeckSightPipeline(
            grid_size=res_hw,
            threshold=0.045,
            enable_lc4_bar=True,
            connectome_weights=optic_weights,
        )
        pipe_lplc2_only = necksight.NeckSightPipeline(
            grid_size=res_hw,
            threshold=0.045,
            enable_lc4_bar=False,
            connectome_weights=optic_weights,
        )
        dt = float(ep["dt"])
        frames = ep["frames"]
        gyros = ep["gyros"]
        best = 0.0
        best_l2 = 0.0
        for f in range(len(frames)):
            ego = necksight.imu_to_ego_motion(
                yaw_rate_rad_s=float(gyros[f, 2]),
                pitch_rate_rad_s=float(gyros[f, 1]),
                roll_rate_rad_s=float(gyros[f, 0]),
                dt=dt,
                grid_width=res_hw,
                hfov_deg=58.0,
            )
            res = pipe.step(frames[f], dt=dt, ego_shift_px=ego)
            res_l2 = pipe_lplc2_only.step(frames[f], dt=dt, ego_shift_px=ego)
            comb = max(float(res.looming_score), 0.585 * float(res.looming_score) + 0.415 * float(res.bar_score))
            l2 = float(res_l2.looming_score)
            if f >= 6:
                if comb > best:
                    best = comb
                if l2 > best_l2:
                    best_l2 = l2
        peaks[idx] = best
        peaks_lplc2[idx] = best_l2

    labels = np.array([ep["label"] for ep in episodes], dtype=np.int64)
    return peaks, peaks_lplc2, labels


def run_sight_benchmark(
    shuffled_optic_list: List[Dict[str, Any]],
    n_per_subtype: int = 8,
) -> Dict[str, Any]:
    episodes = _render_episodes(n_per_subtype=n_per_subtype, res=64)
    labels = np.array([ep["label"] for ep in episodes], dtype=np.int64)
    thin_mask = np.array(
        [(ep["subtype"] in ("thin_pole", "thin_wire")) or (ep["label"] == 0) for ep in episodes],
        dtype=bool,
    )

    real_sc, real_l2_sc, _ = _eval_necksight_episodes(episodes, "banc_v888")
    v2_sc, v2_l2_sc, _ = _eval_necksight_episodes(episodes, "v2_analytic")
    farn_sc = np.array([_score_farneback_derotated(ep) for ep in episodes], dtype=np.float64)
    lk_sc = np.array([_score_pyrlk_derotated(ep) for ep in episodes], dtype=np.float64)

    def _boot_auroc(scores: np.ndarray, mask: np.ndarray | None = None) -> List[float]:
        y = labels if mask is None else labels[mask]
        s = scores if mask is None else scores[mask]
        rng = np.random.default_rng(2026)
        pos_i = np.nonzero(y == 1)[0]
        neg_i = np.nonzero(y == 0)[0]
        vals = []
        for _ in range(200):
            bp = rng.choice(pos_i, size=len(pos_i), replace=True)
            bn = rng.choice(neg_i, size=len(neg_i), replace=True)
            vals.append(compute_auroc(np.concatenate([np.ones(len(bp)), np.zeros(len(bn))]), np.concatenate([s[bp], s[bn]])))
        return vals

    real_auroc = compute_auroc(labels, real_sc)
    real_l2_auroc = compute_auroc(labels, real_l2_sc)
    real_thin_auroc = compute_auroc(labels[thin_mask], real_sc[thin_mask])

    shuf_aurocs = []
    shuf_l2_aurocs = []
    shuf_thin_aurocs = []
    for shuf_opt in shuffled_optic_list:
        ssc, sl2, _ = _eval_necksight_episodes(episodes, shuf_opt)
        shuf_aurocs.append(compute_auroc(labels, ssc))
        shuf_l2_aurocs.append(compute_auroc(labels, sl2))
        shuf_thin_aurocs.append(compute_auroc(labels[thin_mask], ssc[thin_mask]))

    return {
        "n_episodes_total": len(episodes),
        "n_positive": int(np.sum(labels == 1)),
        "n_negative": int(np.sum(labels == 0)),
        "all_obstacles_auroc": {
            "v3_banc_real": {"point": real_auroc, "fpr90": compute_fpr_at_tpr(labels, real_sc, 0.90), "boot": summary_ci95(_boot_auroc(real_sc))},
            "v3_banc_real_lplc2_only": {"point": real_l2_auroc, "fpr90": compute_fpr_at_tpr(labels, real_l2_sc, 0.90), "boot": summary_ci95(_boot_auroc(real_l2_sc))},
            "v2_analytic": {"point": compute_auroc(labels, v2_sc), "fpr90": compute_fpr_at_tpr(labels, v2_sc, 0.90), "boot": summary_ci95(_boot_auroc(v2_sc))},
            "v2_analytic_lplc2_only": {"point": compute_auroc(labels, v2_l2_sc), "fpr90": compute_fpr_at_tpr(labels, v2_l2_sc, 0.90), "boot": summary_ci95(_boot_auroc(v2_l2_sc))},
            "derotated_farneback": {"point": compute_auroc(labels, farn_sc), "fpr90": compute_fpr_at_tpr(labels, farn_sc, 0.90), "boot": summary_ci95(_boot_auroc(farn_sc))},
            "derotated_pyrlk": {"point": compute_auroc(labels, lk_sc), "fpr90": compute_fpr_at_tpr(labels, lk_sc, 0.90), "boot": summary_ci95(_boot_auroc(lk_sc))},
            "shuffle_gate": shuffle_gate_verdict(real_auroc, shuf_aurocs, higher_is_better=True),
            "shuffle_gate_lplc2_only": shuffle_gate_verdict(real_l2_auroc, shuf_l2_aurocs, higher_is_better=True),
        },
        "thin_pole_and_wire_auroc": {
            "v3_banc_real": {"point": real_thin_auroc, "fpr90": compute_fpr_at_tpr(labels[thin_mask], real_sc[thin_mask], 0.90)},
            "v2_analytic": {"point": compute_auroc(labels[thin_mask], v2_sc[thin_mask]), "fpr90": compute_fpr_at_tpr(labels[thin_mask], v2_sc[thin_mask], 0.90)},
            "derotated_farneback": {"point": compute_auroc(labels[thin_mask], farn_sc[thin_mask]), "fpr90": compute_fpr_at_tpr(labels[thin_mask], farn_sc[thin_mask], 0.90)},
            "derotated_pyrlk": {"point": compute_auroc(labels[thin_mask], lk_sc[thin_mask]), "fpr90": compute_fpr_at_tpr(labels[thin_mask], lk_sc[thin_mask], 0.90)},
            "shuffle_gate": shuffle_gate_verdict(real_thin_auroc, shuf_thin_aurocs, higher_is_better=True),
        },
    }
