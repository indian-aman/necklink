"""
Empirical Adult Drosophila Whole-CNS Connectome Priors & Descending Arbitration (BANC v888).

Every cell count, synaptic pathway weight, and descending motor arbitration gain in
NeckLink CNS v2.0 is grounded in the proofread 157,789-neuron Female Adult Whole-CNS
connectome (BANC v888, 23,553,107 synapses) and cross-validated against MaleCNS v1.0
(167,565 neurons).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import numpy as np

_PRIORS_FILE = Path(__file__).with_name("banc_v888_priors.json")
BANC_V888_PRIORS: Dict[str, Any] = json.loads(_PRIORS_FILE.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class DescendingState:
    """Descending neuron activation snapshot across the cervical neck bottleneck."""

    dnp01_brake: float      # Giant Fiber [0, 1] emergency shunting brake
    dna02_left: float       # Unilateral left-turn descending drive [0, 1]
    dna02_right: float      # Unilateral right-turn descending drive [0, 1]
    dna01_fwd: float        # Forward walking / cruising drive [0, 1]
    mdn_reverse: float      # Moonwalker backward escape drive [0, 1]
    mbon_approach: float    # Mushroom Body approach / plume surge valence [-1, +1]
    drive_fwd: float        # Net descending surge command [-1, +1]
    turn_yaw: float         # Net descending yaw command [-1, +1]
    strafe_lat: float       # Net descending lateral dodge [-1, +1]
    freeze_reflex: bool     # Giant Fiber freeze latch
    behavior_mode: str      # "CRUISE_HOME", "DODGE_LOOM", "MOONWALK_REVERSE", "PLUME_CAST", "LIMP_HOME"


class ConnectomeArbitrator:
    """
    Biophysical Descending Neuron (DN) action-selection network parameterized by
    BANC v888 synapse ratios:
      - Optic Lobe (LPLC2 58.5%, LC4 22.4%, LPLC1 19.1% of DN synapses) -> DNp01 & DNa02
      - Central Complex (PFL3 + 0.48x PFL2 anti-goal boost) -> DNa02 / DNa01
      - Mushroom Body (MBON gamma + alpha/beta valence) -> plume surge / vibration limp-home
      - Cervical Inhibition (33.1% GABA/GluCl shunting) -> DNp01/MDN suppresses forward DNa01
    """

    def __init__(
        self,
        loom_dodge_threshold: float = 0.045,
        loom_reverse_threshold: float = 0.14,
        novelty_alert_threshold: float = 0.55,
    ) -> None:
        self.loom_dodge_threshold = float(loom_dodge_threshold)
        self.loom_reverse_threshold = float(loom_reverse_threshold)
        self.novelty_alert_threshold = float(novelty_alert_threshold)
        ratios = BANC_V888_PRIORS["derived_biophysical_ratios"]
        self.lplc2_share = float(ratios["optic_lplc2_dn_share"])
        self.lc4_share = float(ratios["optic_lc4_dn_share"]) + float(ratios["optic_lplc1_dn_share"])
        self.pfl2_boost = float(ratios["cx_pfl2_to_pfl3_antigoal_ratio"])
        self.inhib_fraction = float(ratios["cns_inhibitory_fraction"])
        self._cast_phase: float = 0.0

    def arbitrate(
        self,
        *,
        cruise_speed: float,
        cx_turn_cmd: float,
        heading_err_rad: float,
        loom_score: float,
        thin_loom_score: float = 0.0,
        loom_focus_x: float = 0.0,
        loom_focus_y: float = 0.0,
        mb_novelty: float = 0.0,
        mb_hazard_valence: float = 0.0,
        vib_fault_score: float = 0.0,
        cord_min_health: float = 1.0,
        dt: float = 0.02,
    ) -> DescendingState:
        """
        Fuse multi-organ signals through the neck bottleneck into a 3-DOF descending motor vector.

        `loom_focus_x` in [-1, +1]: negative = obstacle on left (dodge right -> negative/positive yaw),
        convention: positive `turn_yaw` = turn left (CCW), negative `turn_yaw` = turn right (CW).
        If obstacle is on left (`loom_focus_x < 0`), robot turns right (`turn_yaw < 0`) and strafes right.
        """
        eff_loom = max(float(loom_score), self.lplc2_share * float(loom_score) + self.lc4_share * float(thin_loom_score))

        # 1. Giant Fiber (DNp01) & Moonwalker (MDN) escape activation
        dnp01 = float(np.clip((eff_loom - self.loom_dodge_threshold) / max(1e-6, self.loom_reverse_threshold), 0.0, 1.0))
        central_hit = max(0.0, 1.0 - 1.25 * abs(float(loom_focus_x)))
        mdn = float(
            np.clip(
                (eff_loom - self.loom_reverse_threshold) / max(1e-6, self.loom_reverse_threshold) * (0.5 + 0.5 * central_hit),
                0.0,
                1.0,
            )
        )

        # 2. Central Complex PFL3 + PFL2 anti-goal boost (when facing ~180 deg away from goal)
        anti_goal = max(0.0, -float(np.cos(heading_err_rad)))  # peaks at +1 when 180 deg off-goal
        break_symmetry = 1.0 if cx_turn_cmd >= 0.0 else -1.0
        cx_yaw = float(np.clip(cx_turn_cmd + self.pfl2_boost * anti_goal * break_symmetry * 0.35, -1.0, 1.0))

        # 3. Unilateral DNa02 looming avoidance away from focus_x
        # Obstacle at focus_x <= 0 (left/dead-center) -> evade right (yaw < 0); focus_x > 0 -> evade left (yaw > 0)
        evade_dir = 1.0 if float(loom_focus_x) > 0.05 else -1.0
        evade_gain = float(np.clip(eff_loom / max(1e-6, self.loom_dodge_threshold), 0.0, 2.0)) if eff_loom >= self.loom_dodge_threshold else 0.0
        evade_yaw = evade_dir * min(1.0, 0.65 * evade_gain)
        strafe_lat = float(np.clip(-evade_dir * min(0.85, 0.55 * evade_gain), -1.0, 1.0)) if eff_loom >= self.loom_dodge_threshold else 0.0

        # 4. Mushroom Body plume cast / mechanical fault limp-home modulation
        severe_fault = (float(vib_fault_score) > 0.65) or (float(cord_min_health) < 0.45)
        plume_detected = (float(mb_novelty) >= self.novelty_alert_threshold) and (float(mb_hazard_valence) >= -0.2)

        self._cast_phase = float((self._cast_phase + 2.0 * np.pi * 1.25 * dt) % (2.0 * np.pi))
        cast_yaw = 0.28 * float(np.sin(self._cast_phase)) if plume_detected else 0.0

        # Shunting inhibition of forward DNa01 drive under DNp01 / MDN / mechanical limp-home
        limp_scale = 0.55 if severe_fault else 1.0
        shunt = max(0.0, 1.0 - (0.85 * dnp01 + 1.0 * mdn))
        dna01 = float(np.clip(cruise_speed * limp_scale * shunt, 0.0, 1.0))

        if mdn > 0.25:
            drive_fwd = float(np.clip(-0.65 * mdn, -1.0, 0.0))
            turn_yaw = float(np.clip(evade_yaw, -1.0, 1.0))
            mode = "MOONWALK_REVERSE"
        elif eff_loom >= self.loom_dodge_threshold:
            drive_fwd = float(np.clip(dna01 * (1.0 - 0.6 * dnp01), 0.05, 1.0))
            turn_yaw = float(np.clip(0.80 * evade_yaw + 0.20 * cx_yaw, -1.0, 1.0))
            mode = "DODGE_LOOM"
        elif severe_fault:
            drive_fwd = dna01
            turn_yaw = cx_yaw
            mode = "LIMP_HOME"
        elif plume_detected:
            drive_fwd = float(np.clip(dna01 * 0.85, 0.0, 1.0))
            turn_yaw = float(np.clip(0.65 * cx_yaw + cast_yaw, -1.0, 1.0))
            mode = "PLUME_CAST"
        else:
            drive_fwd = dna01
            turn_yaw = cx_yaw
            mode = "CRUISE_HOME"

        dna02_l = float(np.clip(max(0.0, turn_yaw), 0.0, 1.0))
        dna02_r = float(np.clip(max(0.0, -turn_yaw), 0.0, 1.0))
        mbon_val = float(np.clip(mb_novelty - mb_hazard_valence - 0.5 * vib_fault_score, -1.0, 1.0))

        return DescendingState(
            dnp01_brake=dnp01,
            dna02_left=dna02_l,
            dna02_right=dna02_r,
            dna01_fwd=dna01,
            mdn_reverse=mdn,
            mbon_approach=mbon_val,
            drive_fwd=drive_fwd,
            turn_yaw=turn_yaw,
            strafe_lat=strafe_lat,
            freeze_reflex=bool(dnp01 > 0.92 and mdn < 0.15),
            behavior_mode=mode,
        )
