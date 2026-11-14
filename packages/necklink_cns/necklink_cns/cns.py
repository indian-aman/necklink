"""
Unified Adult Drosophila Whole-CNS Python Controller (`necklink_cns.cns`).

Integrates all four `v2.0.0` organs (`NeckSight`, `NeckCompass`, `NeckSense`, `NeckCord`)
with empirical `BANC v888` descending action selection, 4-byte CRC-4 cervical framing,
and wire-valid `MAVLink v2` packet generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import neckcompass
import neckcord
import necksense
import necksight
import numpy as np

from .connectome import BANC_V888_PRIORS, ConnectomeArbitrator, DescendingState
from .mavlink import MavLink2Codec, MavManualControl


@dataclass
class CNSOutputs:
    """Complete per-tick readout across all four CNS organs + cervical bottleneck."""

    heading_rad: float
    home_distance_m: float
    home_bearing_rad: float
    loom_score: float
    thin_loom_score: float
    loom_triggered: bool
    avoid_x: float
    avoid_y: float
    novelty_score: float
    whiff_rate: float
    vib_novelty_score: float
    predicted_class: Optional[int]
    descending: DescendingState
    cervical_wire_4b: bytes
    actuator_commands: np.ndarray
    actuator_health: np.ndarray
    cord_min_health: float
    mavlink_manual_pkt: bytes


class NeckLinkCNS:
    """
    Closed-loop 4-organ *Drosophila* Whole-CNS controller supporting:
      - `vehicle="usv_t200"` (4-thruster asymmetric BlueRobotics T200 USV)
      - `vehicle="hexacopter"` (6-rotor unidirectional `[0, u_max]` hexacopter)
      - `vehicle="octocopter"` (8-rotor unidirectional `[0, u_max]` octocopter)
      - `vehicle="hexapod"` (6-limb, 18-servo legged hexapod with 3-DOF IK)
    """

    PROVENANCE = "connectome"
    SENSOR_ACTUATOR_IO_PROVENANCE = "engineering"

    def __init__(
        self,
        vehicle: str = "usv_t200",
        grid_size: int = 32,
        n_chem_channels: int = 8,
        enable_plastic_mag: bool = False,
        seed: int = 42,
        connectome_weights: str | Dict[str, Any] = "banc_v888",
    ) -> None:
        self.vehicle = vehicle
        self.grid_size = int(grid_size)
        self.enable_plastic_mag = bool(enable_plastic_mag)
        self.connectome_weights = connectome_weights

        if isinstance(connectome_weights, dict):
            w_optic: Any = connectome_weights.get("optic", connectome_weights.get("necksight", "banc_v888"))
            w_cx: Any = connectome_weights.get("cx", connectome_weights.get("neckcompass", "banc_v888"))
            w_mb: Any = connectome_weights.get("mb", connectome_weights.get("necksense", "banc_v888"))
            w_vd: Any = connectome_weights.get("vnc_dn", connectome_weights.get("necklink_cns", "banc_v888"))
        else:
            w_optic = w_cx = w_mb = w_vd = connectome_weights

        # 1. Optic Lobe (NeckSight v3.0.0)
        self.sight = necksight.NeckSightPipeline(
            grid_size=self.grid_size,
            threshold=0.045,
            enable_lc4_bar=True,
            connectome_weights=w_optic,
        )

        # 2. Central Complex (NeckCompass v3.0.0)
        self.compass = neckcompass.RingCompass(
            n_wedges=16,
            initial_heading_rad=0.0,
            connectome_weights=w_cx,
        )
        self.plastic_compass = (
            neckcompass.PlasticLandmarkRing(n_wedges=16, connectome_weights=w_cx)
            if enable_plastic_mag
            else None
        )
        self.navigator = neckcompass.FanBodyNavigator(
            n_columns=16,
            compass=self.compass,
            connectome_weights=w_cx,
        )

        # 3. Antennal Lobe, Chordotonal Organ & Mushroom Body (NeckSense v3.0.0)
        self.vib_fe = necksense.VibrationFrontEnd()
        self.chem_nov = necksense.MushroomNoveltyDetector(
            n_inputs=n_chem_channels,
            antennal=True,
            stateful_antennal=True,
            n_kc=512,
            seed=seed,
            connectome_weights=w_mb,
        )
        self.vib_nov = necksense.MushroomNoveltyDetector(
            n_inputs=necksense.NS_VIB_CHANNELS,
            antennal=True,
            stateful_antennal=False,
            n_kc=512,
            seed=seed + 1,
            connectome_weights=w_mb,
        )
        self.chem_clf = necksense.MushroomValenceClassifier(
            n_inputs=n_chem_channels,
            antennal=True,
            stateful_antennal=False,
            n_kc=512,
            ltm_alpha_beta=True,
            seed=seed + 2,
            connectome_weights=w_mb,
        )
        self._has_learned_classes = False

        # 4. Descending Arbitrator & Ventral Nerve Cord (NeckCord v3.0.0)
        self.arbitrator = ConnectomeArbitrator(connectome_weights=w_vd)
        if vehicle == "hexacopter":
            self.cord = neckcord.ShuntingThrusterCord.multicopter_hex_6()
        elif vehicle == "octocopter":
            self.cord = neckcord.ShuntingThrusterCord.multicopter_octo_8()
        else:
            self.cord = neckcord.ShuntingThrusterCord.usv_t200_vectored_4()

        self.esc_obs = neckcord.ESCObserver(
            n_actuators=self.cord.n_actuators,
            u_nom=self.cord.u_max,
            rpm_max=9000.0,
        )
        self.cpg = neckcord.SegmentalCPGCord(adapt_to_damage=True, connectome_weights=w_vd)
        self.mav = MavLink2Codec(sysid=42, compid=191)
        self._seq = 0
        self._last_cmds = np.zeros(self.cord.n_actuators, dtype=np.float64)

    def learn_chemical(self, vec: np.ndarray, class_id: int, consolidate: bool = True) -> None:
        self.chem_clf.learn(np.asarray(vec, dtype=np.float64), int(class_id))
        if consolidate:
            self.chem_clf.consolidate()
        self._has_learned_classes = True

    def step(
        self,
        *,
        dt: float = 0.02,
        speed_m_s: float = 1.0,
        cruise_speed: float = 0.65,
        return_home: bool = True,
        gyro_xyz: Optional[np.ndarray] = None,
        accel_xyz: Optional[np.ndarray] = None,
        mag_xyz: Optional[np.ndarray] = None,
        camera_frame: Optional[np.ndarray] = None,
        chemical_vec: Optional[np.ndarray] = None,
        vibration_window: Optional[np.ndarray] = None,
        feedback_thrust: Optional[np.ndarray] = None,
        esc_rpm: Optional[np.ndarray] = None,
        esc_current_a: Optional[np.ndarray] = None,
    ) -> CNSOutputs:
        g = np.zeros(3, dtype=np.float64) if gyro_xyz is None else np.asarray(gyro_xyz, dtype=np.float64).reshape(3)
        a = np.array([0.0, 0.0, 9.81], dtype=np.float64) if accel_xyz is None else np.asarray(accel_xyz, dtype=np.float64).reshape(3)

        # 1. Central Complex 9-DOF Tilt-Compensated Heading + CPU4 Homing
        if mag_xyz is not None:
            m = np.asarray(mag_xyz, dtype=np.float64).reshape(3)
            hdg, dist_m, home_brg = self.navigator.step_with_compass_9dof(
                float(g[0]), float(g[1]), float(g[2]),
                float(a[0]), float(a[1]), float(a[2]),
                float(m[0]), float(m[1]), float(m[2]),
                speed_m_s=float(speed_m_s),
                dt=float(dt),
            )
        else:
            hdg, dist_m, home_brg = self.navigator.step_with_compass(
                omega_rad_s=float(g[2]),
                speed_m_s=float(speed_m_s),
                dt=float(dt),
            )

        cx_turn = float(self.navigator.steering_cmd(max_turn_rate=1.0)) if return_home else 0.0
        hdg_err = float(neckcompass.wrap_angle(home_brg - hdg)) if return_home else 0.0

        # 2. Optic Lobe 3-DOF Derotation + LPLC2 / LC4 Looming
        loom_score = 0.0
        bar_score = 0.0
        loom_trig = False
        avoid_x = 0.0
        avoid_y = 0.0
        focus_x_norm = 0.0
        if camera_frame is not None:
            ego = necksight.imu_to_ego_motion(
                yaw_rate_rad_s=float(g[2]),
                pitch_rate_rad_s=float(g[1]),
                roll_rate_rad_s=float(g[0]),
                dt=float(dt),
                grid_width=self.grid_size,
            )
            trig = self.sight.step(np.asarray(camera_frame), dt=float(dt), ego_shift_px=ego)
            loom_score = float(trig.looming_score)
            bar_score = float(trig.bar_score)
            loom_trig = bool(trig.triggered)
            avoid_x = float(trig.avoid_x)
            avoid_y = float(trig.avoid_y)
            # Note: in ConnectomeArbitrator, focus_x < 0 => obstacle on left => evade right (avoid_x > 0)
            focus_x_norm = -avoid_x if abs(avoid_x) > 1e-3 else (float(trig.focus_xy[0]) / max(1.0, self.grid_size - 1.0)) * 2.0 - 1.0

        # 3. Antennal Lobe, Chordotonal Vibration & Mushroom Body
        nov_score = 0.0
        whiff_rate = 0.0
        pred_cls: Optional[int] = None
        if chemical_vec is not None:
            cvec = np.asarray(chemical_vec, dtype=np.float64).ravel()
            nov_score = float(self.chem_nov.update_and_score(cvec))
            if self.chem_nov.antennal is not None:
                whiff_rate = float(getattr(self.chem_nov.antennal, "whiff_rate", 0.0))
            if self._has_learned_classes:
                pred_cls = int(self.chem_clf.predict(cvec))

        vib_score = 0.0
        if vibration_window is not None:
            v_feat = self.vib_fe.extract(np.asarray(vibration_window, dtype=np.float64))
            vib_score = float(self.vib_nov.update_and_score(v_feat))

        # 4. BANC v888 Descending Neck Bottleneck Arbitration
        min_h = float(np.min(self.cord.health))
        taper = min(1.0, float(dist_m) / max(0.1, float(self.navigator.arrival_radius_m))) if return_home else 0.0
        p3_l = (
            float(self.navigator.last_pfl3_left_rate) * taper
            if (return_home and hasattr(self.navigator, "last_pfl3_left_rate"))
            else (0.0 if not return_home else None)
        )
        p3_r = (
            float(self.navigator.last_pfl3_right_rate) * taper
            if (return_home and hasattr(self.navigator, "last_pfl3_right_rate"))
            else (0.0 if not return_home else None)
        )
        p2_v = (
            float(self.navigator.last_pfl2_rate) * taper
            if (return_home and hasattr(self.navigator, "last_pfl2_rate"))
            else (0.0 if not return_home else None)
        )
        desc = self.arbitrator.arbitrate(
            cruise_speed=cruise_speed,
            cx_turn_cmd=cx_turn,
            heading_err_rad=hdg_err,
            loom_score=loom_score,
            thin_loom_score=bar_score,
            loom_focus_x=focus_x_norm,
            mb_novelty=nov_score,
            vib_fault_score=vib_score,
            cord_min_health=min_h,
            pfl3_left_rate=p3_l,
            pfl3_right_rate=p3_r,
            pfl2_rate=p2_v,
            dt=dt,
        )

        dframe = neckcord.DescendingFrame(
            drive_fwd=desc.drive_fwd,
            turn_yaw=desc.turn_yaw,
            strafe_lat=desc.strafe_lat,
            gait_mode=int(neckcord.GaitMode.AUTO),
            freeze_reflex=desc.freeze_reflex,
            seq=self._seq & 0x03,
        )
        self._seq += 1
        wire_4b = neckcord.encode_descending_4b(dframe)

        # 5. Ventral Nerve Cord Allocation / ESC Proprioception
        fb = feedback_thrust
        if fb is None and esc_rpm is not None and esc_current_a is not None:
            esc_res = self.esc_obs.update(self._last_cmds, esc_rpm, esc_current_a, dt=dt)
            fb = esc_res.estimated_thrust

        alloc = self.cord.step(wire_4b, feedback_thrust=fb, dt=dt)
        self._last_cmds = alloc.commands.copy()

        mav_pkt = self.mav.encode_manual_control(
            MavManualControl(
                x=int(round(desc.drive_fwd * 1000.0)),
                y=int(round(desc.strafe_lat * 1000.0)),
                z=500,
                r=int(round(desc.turn_yaw * 1000.0)),
                buttons=(1 if loom_trig else 0),
                target=1,
            )
        )

        return CNSOutputs(
            heading_rad=float(hdg),
            home_distance_m=float(dist_m),
            home_bearing_rad=float(home_brg),
            loom_score=loom_score,
            thin_loom_score=bar_score,
            loom_triggered=loom_trig,
            avoid_x=avoid_x,
            avoid_y=avoid_y,
            novelty_score=nov_score,
            whiff_rate=whiff_rate,
            vib_novelty_score=vib_score,
            predicted_class=pred_cls,
            descending=desc,
            cervical_wire_4b=wire_4b,
            actuator_commands=alloc.commands,
            actuator_health=alloc.health,
            cord_min_health=float(np.min(alloc.health)),
            mavlink_manual_pkt=mav_pkt,
        )
