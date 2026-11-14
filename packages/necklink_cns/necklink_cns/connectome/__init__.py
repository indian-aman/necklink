"""
Empirical Adult Drosophila Whole-CNS Connectome Extraction, Shuffling & Descending Arbitration (`BANC v888`).

In NeckLink v3.0.0, `ConnectomeArbitrator` replaces v2's hand-coded `if/elif` action-selection
rules with genuine matrix multiplication through the parameter-free `F + F^2` signed fractional
synaptic matrices extracted from `BANC v888` (`11 Brain Populations -> 10 Core DNs -> 6 Thoracic Leg MN Pools`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

import numpy as np

# Runtime only needs NumPy + the shipped `data/*.npz` weights. The extraction and
# Maslov-Sneppen shuffling tools need SciPy/pandas (and the raw BANC v888 dataset), so
# they are loaded lazily: `pip install "necklink-cns[connectome]"`.
_LAZY_EXTRACT = {
    "BRAIN_POP_NAMES",
    "CORE_DN_NAMES",
    "LEG_POOL_NAMES",
    "compile_circuits_from_subgraphs",
    "extract_all_banc_v888",
    "extract_raw_subgraphs",
}
_LAZY_SHUFFLE = {
    "maslov_sneppen_csr",
    "maslov_sneppen_dense",
    "shuffle_raw_subgraphs",
    "verify_degree_preservation",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXTRACT or name in _LAZY_SHUFFLE:
        try:
            if name in _LAZY_EXTRACT:
                from . import extract as _mod
            else:
                from . import shuffle as _mod
        except ImportError as exc:  # pragma: no cover - depends on optional extras
            raise ImportError(
                f"necklink_cns.connectome.{name} needs the optional connectome tooling: "
                'pip install "necklink-cns[connectome]"'
            ) from exc
        return getattr(_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_PKG_DIR = Path(__file__).resolve().parent.parent
_PRIORS_FILE = _PKG_DIR / "banc_v888_priors.json"
_DN_NPZ_FILE = _PKG_DIR / "data" / "banc_dn_v3.npz"
_PROVENANCE_FILE = Path(__file__).resolve().parent / "provenance.json"

BANC_V888_PRIORS: Dict[str, Any] = json.loads(_PRIORS_FILE.read_text(encoding="utf-8"))


def load_provenance_manifest() -> Dict[str, Any]:
    if _PROVENANCE_FILE.exists():
        return json.loads(_PROVENANCE_FILE.read_text(encoding="utf-8"))
    return {}


def _load_default_dn_bundle() -> Dict[str, np.ndarray]:
    with np.load(_DN_NPZ_FILE, allow_pickle=False) as z:
        return {k: z[k].copy() for k in z.files}


@dataclass(frozen=True)
class DescendingState:
    """Descending neuron activation snapshot across the cervical neck bottleneck."""

    dnp01_brake: float      # Giant Fiber [0, 1] emergency shunting brake
    dna02_left: float       # Unilateral left-turn descending drive [0, 1]
    dna02_right: float      # Unilateral right-turn descending drive [0, 1]
    dna01_fwd: float        # Forward walking / cruising drive [0, 1]
    mdn_reverse: float      # Moonwalker backward escape drive [0, 1]
    mbon_approach: float    # Mushroom Body approach / plume valence [-1, +1]
    drive_fwd: float        # Net descending surge command [-1, +1]
    turn_yaw: float         # Net descending yaw command [-1, +1]
    strafe_lat: float       # Net descending lateral dodge [-1, +1]
    freeze_reflex: bool     # Giant Fiber freeze latch
    behavior_mode: str      # Diagnostic readout of dominant DN population


class ConnectomeArbitrator:
    """
    `BANC v888` Synaptic Descending Neuron (DN) Competition Network (`v3.0.0`).

    Supports:
      - `connectome_weights="banc_v888"` (default): real `11 -> 10 -> 6` `BANC v888` `F + F^2` matrices
      - `connectome_weights=<dict>`: custom/shuffled `vnc_dn` bundle from `compile_circuits_from_subgraphs`
      - `connectome_weights="v2_analytic"`: legacy v2 hand-coded priority rules for A/B benchmarking
    """

    PROVENANCE = "connectome"

    def __init__(
        self,
        loom_dodge_threshold: float = 0.045,
        loom_reverse_threshold: float = 0.14,
        novelty_alert_threshold: float = 0.55,
        connectome_weights: Union[str, Dict[str, Any]] = "banc_v888",
    ) -> None:
        self.loom_dodge_threshold = float(loom_dodge_threshold)
        self.loom_reverse_threshold = float(loom_reverse_threshold)
        self.novelty_alert_threshold = float(novelty_alert_threshold)
        self._cast_phase: float = 0.0

        if isinstance(connectome_weights, str) and connectome_weights == "v2_analytic":
            self.mode = "v2_analytic"
            self.PROVENANCE = "v2_analytic"
            ratios = BANC_V888_PRIORS["derived_biophysical_ratios"]
            self.lplc2_share = float(ratios["optic_lplc2_dn_share"])
            self.lc4_share = float(ratios["optic_lc4_dn_share"]) + float(ratios["optic_lplc1_dn_share"])
            self.pfl2_boost = float(ratios["cx_pfl2_to_pfl3_antigoal_ratio"])
            self.W_brain_dn = np.zeros((10, 11), dtype=np.float64)
            self.W_dn_leg = np.zeros((6, 10), dtype=np.float64)
        else:
            self.mode = "banc_v888"
            self.PROVENANCE = "connectome"
            bundle = (
                _load_default_dn_bundle()
                if isinstance(connectome_weights, str)
                else connectome_weights
            )
            self.W_brain_dn = np.asarray(bundle["W_brain_dn"], dtype=np.float64)
            self.W_dn_leg = np.asarray(bundle["W_dn_leg"], dtype=np.float64)

    def _arbitrate_v2(
        self,
        *,
        cruise_speed: float,
        cx_turn_cmd: float,
        heading_err_rad: float,
        loom_score: float,
        thin_loom_score: float,
        loom_focus_x: float,
        mb_novelty: float,
        mb_hazard_valence: float,
        vib_fault_score: float,
        cord_min_health: float,
        dt: float,
    ) -> DescendingState:
        eff_loom = max(float(loom_score), self.lplc2_share * float(loom_score) + self.lc4_share * float(thin_loom_score))
        dnp01 = float(np.clip((eff_loom - self.loom_dodge_threshold) / max(1e-6, self.loom_reverse_threshold), 0.0, 1.0))
        central_hit = max(0.0, 1.0 - 1.25 * abs(float(loom_focus_x)))
        mdn = float(
            np.clip(
                (eff_loom - self.loom_reverse_threshold) / max(1e-6, self.loom_reverse_threshold) * (0.5 + 0.5 * central_hit),
                0.0,
                1.0,
            )
        )
        anti_goal = max(0.0, -float(np.cos(heading_err_rad)))
        break_symmetry = 1.0 if cx_turn_cmd >= 0.0 else -1.0
        cx_yaw = float(np.clip(cx_turn_cmd + self.pfl2_boost * anti_goal * break_symmetry * 0.35, -1.0, 1.0))
        evade_dir = 1.0 if float(loom_focus_x) > 0.05 else -1.0
        evade_gain = float(np.clip(eff_loom / max(1e-6, self.loom_dodge_threshold), 0.0, 2.0)) if eff_loom >= self.loom_dodge_threshold else 0.0
        evade_yaw = evade_dir * min(1.0, 0.65 * evade_gain)
        strafe_lat = float(np.clip(-evade_dir * min(0.85, 0.55 * evade_gain), -1.0, 1.0)) if eff_loom >= self.loom_dodge_threshold else 0.0
        severe_fault = (float(vib_fault_score) > 0.65) or (float(cord_min_health) < 0.45)
        plume_detected = (float(mb_novelty) >= self.novelty_alert_threshold) and (float(mb_hazard_valence) >= -0.2)
        self._cast_phase = float((self._cast_phase + 2.0 * np.pi * 1.25 * dt) % (2.0 * np.pi))
        cast_yaw = 0.28 * float(np.sin(self._cast_phase)) if plume_detected else 0.0
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
        return DescendingState(
            dnp01_brake=dnp01,
            dna02_left=float(np.clip(max(0.0, turn_yaw), 0.0, 1.0)),
            dna02_right=float(np.clip(max(0.0, -turn_yaw), 0.0, 1.0)),
            dna01_fwd=dna01,
            mdn_reverse=mdn,
            mbon_approach=float(np.clip(mb_novelty - mb_hazard_valence - 0.5 * vib_fault_score, -1.0, 1.0)),
            drive_fwd=drive_fwd,
            turn_yaw=turn_yaw,
            strafe_lat=strafe_lat,
            freeze_reflex=bool(dnp01 > 0.92 and mdn < 0.15),
            behavior_mode=mode,
        )

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
        pfl3_left_rate: Optional[float] = None,
        pfl3_right_rate: Optional[float] = None,
        pfl2_rate: Optional[float] = None,
        dt: float = 0.02,
    ) -> DescendingState:
        """
        Compute 10 Core Descending Neuron rates and 6 Thoracic Leg Motor Pool drives
        directly via `W_brain_dn` and `W_dn_leg` from `BANC v888`.
        """
        if self.mode == "v2_analytic":
            return self._arbitrate_v2(
                cruise_speed=cruise_speed,
                cx_turn_cmd=cx_turn_cmd,
                heading_err_rad=heading_err_rad,
                loom_score=loom_score,
                thin_loom_score=thin_loom_score,
                loom_focus_x=loom_focus_x,
                mb_novelty=mb_novelty,
                mb_hazard_valence=mb_hazard_valence,
                vib_fault_score=vib_fault_score,
                cord_min_health=cord_min_health,
                dt=dt,
            )

        # 1. Assemble 11-dimensional Brain Output Population vector `u_brain`
        fx = float(np.clip(loom_focus_x, -1.0, 1.0))
        # Compound-eye hemifields with +-15% frontal binocular overlap (slight left bias at exact 0.0)
        w_left_eye = float(np.clip((0.16 - fx) / 0.65, 0.0, 1.0))
        w_right_eye = float(np.clip((0.14 + fx) / 0.65, 0.0, 1.0))

        loom_suprathresh = max(0.0, float(loom_score) - 0.5 * self.loom_dodge_threshold) / max(
            1e-6, self.loom_reverse_threshold
        )
        lc4_suprathresh = max(0.0, float(thin_loom_score) - 0.5 * self.loom_dodge_threshold) / max(
            1e-6, self.loom_reverse_threshold
        )
        # Central hit boosts high-threshold LC4/LPLC2 escape drive onto MDN/DNp01
        lplc2_drive = float(np.clip(loom_suprathresh, 0.0, 2.5))
        lc4_drive = float(np.clip(lc4_suprathresh + 0.6 * max(0.0, loom_suprathresh - 0.5), 0.0, 2.5))

        # PFL3_L drives right turn (DNa02_R), PFL3_R drives left turn (DNa02_L)
        if pfl3_left_rate is not None and pfl3_right_rate is not None:
            p3_L = float(np.clip(pfl3_left_rate * 1.6, 0.0, 2.5))
            p3_R = float(np.clip(pfl3_right_rate * 1.6, 0.0, 2.5))
        else:
            p3_L = float(np.clip(max(0.0, -cx_turn_cmd), 0.0, 1.5))
            p3_R = float(np.clip(max(0.0, cx_turn_cmd), 0.0, 1.5))

        p2_val = (
            float(np.clip(pfl2_rate * 1.6, 0.0, 2.0))
            if pfl2_rate is not None
            else float(max(0.0, -np.cos(heading_err_rad)) * 0.5)
        )

        # Mushroom Body cholinergic avoidance vs GABA/Glu approach MBON populations
        self._cast_phase = float((self._cast_phase + 2.0 * np.pi * 1.25 * dt) % (2.0 * np.pi))
        nov_excess = max(0.0, float(mb_novelty) - self.novelty_alert_threshold) / max(
            1e-6, 1.0 - self.novelty_alert_threshold
        )
        vib_excess = max(0.0, float(vib_fault_score) - 0.55) / 0.45
        mbon_ach_avoid = float(np.clip(max(0.0, mb_hazard_valence) + 0.45 * nov_excess + 0.65 * vib_excess, 0.0, 2.0))
        mbon_inh_app = float(np.clip(max(0.0, -float(mb_hazard_valence)), 0.0, 1.5))

        # LPLC1 is a visual object/looming columnar population (Wu et al. 2016; Tanaka & Clark 2020)
        lplc1_drive = 0.5 * (lplc2_drive + lc4_drive)

        u_brain = np.array(
            [
                lplc2_drive * w_left_eye,   # 0: LPLC2_L
                lplc2_drive * w_right_eye,  # 1: LPLC2_R
                lc4_drive * w_left_eye,     # 2: LC4_L
                lc4_drive * w_right_eye,    # 3: LC4_R
                lplc1_drive * w_left_eye,   # 4: LPLC1_L
                lplc1_drive * w_right_eye,  # 5: LPLC1_R
                p3_L,                       # 6: PFL3_L
                p3_R,                       # 7: PFL3_R
                p2_val,                     # 8: PFL2
                mbon_ach_avoid,             # 9: MBON_ACh_avoid
                mbon_inh_app,               # 10: MBON_Inh_approach
            ],
            dtype=np.float64,
        )

        # 2. Synaptic competition onto the 10 Core Descending Neurons:
        # [DNp01_L, DNp01_R, MDN_L, MDN_R, DNa02_L, DNa02_R, DNa01_L, DNa01_R, DNp09_L, DNp09_R]
        health_gate = 0.55 if (float(cord_min_health) < 0.45 or float(vib_fault_score) > 0.65) else 1.0
        cruise_clamped = float(np.clip(cruise_speed, 0.0, 1.0))
        tonic_dn = np.zeros(10, dtype=np.float64)
        # Tonic central forward-walking state onto DNp09_L/R & reciprocal inhibition onto MDN_L/R (Bidaye et al., Nature 2020)
        tonic_dn[2] = -0.65 * cruise_clamped
        tonic_dn[3] = -0.65 * cruise_clamped
        tonic_dn[8] = cruise_clamped * health_gate
        tonic_dn[9] = cruise_clamped * health_gate
        # Mild lateralized casting bias on PFL/LAL -> DNa02 when novel odor alert is active
        if nov_excess > 0.05:
            cast_osc = 0.25 * nov_excess * float(np.sin(self._cast_phase))
            tonic_dn[4] += max(0.0, cast_osc)
            tonic_dn[5] += max(0.0, -cast_osc)

        syn_dn = self.W_brain_dn @ u_brain
        # Note: intense central looming also recruits MDN via PVLP/PLP looming interneurons
        if loom_suprathresh > 0.95 and abs(fx) < 0.75:
            mdn_loom_boost = 2.2 * (loom_suprathresh - 0.95) * (1.0 - abs(fx))
            syn_dn[2] += mdn_loom_boost * max(0.0, float(self.W_brain_dn[2, [0, 2, 9]].sum()))
            syn_dn[3] += mdn_loom_boost * max(0.0, float(self.W_brain_dn[3, [1, 3, 9]].sum()))

        r_dn = np.clip(tonic_dn + syn_dn, 0.0, 2.0)
        # Supralinear DNa02 input-output curve (Westeinde et al., Nature 2024): bilateral PFL2 excitation multiplies PFL3 steering asymmetry
        r_eff = r_dn.copy()
        r_eff[4:6] = r_dn[4:6] * (0.5 + 1.5 * r_dn[4:6])

        # 3. Downstream VNC Motor Pool Recruitment: `W_dn_leg` (6 Leg Pools x 10 Core DNs)
        # Separate functional DN groups through the exact `W_dn_leg` columns:
        #   - Jump/shove columns [0, 1] (DNp01_L/R -> mesothoracic jump muscles)
        #   - Backward reversal columns [2, 3] (MDN_L/R -> hindleg reversal pools)
        #   - Steering columns [4, 5, 6, 7] (DNa02_L/R + DNa01_L/R -> left vs right thoracic pools)
        jump_leg = self.W_dn_leg[:, 0:2] @ r_eff[0:2]
        rev_leg = self.W_dn_leg[:, 2:4] @ r_eff[2:4]
        steer_leg = self.W_dn_leg[:, 4:8] @ r_eff[4:8]

        left_idx = [0, 2, 4]   # L1, L2, L3
        right_idx = [1, 3, 5]  # R1, R2, R3

        # Left jump leg extension pushes body RIGHT (+strafe_lat, -turn_yaw)
        jump_asym = float(jump_leg[left_idx].sum() - jump_leg[right_idx].sum())
        # Left DNa02/DNa01 recruitment pivots body LEFT (+turn_yaw)
        steer_asym = float(steer_leg[left_idx].sum() - steer_leg[right_idx].sum())

        dnp01 = float(np.clip(0.5 * (r_dn[0] + r_dn[1]), 0.0, 1.0))
        mdn = float(np.clip(float(rev_leg.clip(min=0.0).sum()) * 0.65, 0.0, 1.0))
        dnp09_fwd = float(np.clip(0.5 * (r_dn[8] + r_dn[9]), 0.0, 1.0))

        # Continuous shunting forward/reverse surge (zero discontinuous if/elif switching)
        fwd_surviving = dnp09_fwd * max(0.0, 1.0 - 0.70 * dnp01)
        drive_fwd = float(np.clip(fwd_surviving - 0.85 * mdn, -1.0, 1.0))

        turn_yaw = float(np.clip(1.10 * steer_asym - 1.35 * jump_asym, -1.0, 1.0))
        strafe_lat = float(np.clip(1.40 * jump_asym, -0.85, 0.85))

        dna02_l = float(np.clip(r_dn[4], 0.0, 1.0))
        dna02_r = float(np.clip(r_dn[5], 0.0, 1.0))
        mbon_val = float(np.clip(mbon_inh_app - mbon_ach_avoid, -1.0, 1.0))

        # Pure diagnostic readout label of whichever DN population won the competition
        if drive_fwd < -0.05:
            mode = "MOONWALK_REVERSE"
        elif dnp01 > 0.12 or abs(jump_asym) > 0.08:
            mode = "DODGE_LOOM"
        elif health_gate < 1.0:
            mode = "LIMP_HOME"
        elif nov_excess > 0.05:
            mode = "PLUME_CAST"
        else:
            mode = "CRUISE_HOME"

        return DescendingState(
            dnp01_brake=dnp01,
            dna02_left=dna02_l,
            dna02_right=dna02_r,
            dna01_fwd=dnp09_fwd,
            mdn_reverse=mdn,
            mbon_approach=mbon_val,
            drive_fwd=drive_fwd,
            turn_yaw=turn_yaw,
            strafe_lat=strafe_lat,
            freeze_reflex=bool(dnp01 > 0.92 and mdn < 0.12),
            behavior_mode=mode,
        )


__all__ = [
    "BANC_V888_PRIORS",
    "DescendingState",
    "ConnectomeArbitrator",
    "load_provenance_manifest",
]
# Lazily importable (needs `necklink-cns[connectome]`): BRAIN_POP_NAMES, CORE_DN_NAMES,
# LEG_POOL_NAMES, extract_raw_subgraphs, compile_circuits_from_subgraphs,
# extract_all_banc_v888, maslov_sneppen_dense, maslov_sneppen_csr,
# shuffle_raw_subgraphs, verify_degree_preservation.
