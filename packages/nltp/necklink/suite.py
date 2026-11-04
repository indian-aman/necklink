"""
NeckLink Brand Suite (`necklink.suite`) — Unified Fruit-Fly Brain Autonomy & Wire Stack.

Connects all five adult Drosophila CNS-inspired modules under the NeckLink brand:
  1. NLTP (`pynltp` / `necklink.nltp`)    — Cervical Connective Bottleneck (encrypted FEC UDP + reflex wire)
  2. NeckSense (`necksense`)              — Antennal Lobe & Mushroom Body (5% WTA FlyHash + 1-shot plume memory)
  3. NeckCompass (`neckcompass`)          — Central Complex Ring Attractor (spike-immune AHRS + blind homing)
  4. NeckSight (`necksight`)              — Optic Lobe T4/T5 + LPLC2 (100x glare-immune looming E-stop)
  5. NeckCord (`neckcord`)                — Ventral Nerve Cord (4B CRC-4 command + CPG & thruster fault rescue)
"""

from __future__ import annotations

import importlib
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from .internet import generate_nltp_key
from .nltp import NLTPClient, NLTPServer

NECKLINK_SUITE_CATALOG: List[Dict[str, str]] = [
    {
        "id": "nltp",
        "name": "NLTP",
        "pypi": "pynltp",
        "repo": "https://github.com/indian-aman/necklink/tree/main/packages/nltp",
        "region": "Cervical Connective Bottleneck",
        "summary": "Zero-ACK loss-healing telemetry, Cauchy FEC file transfer, ChaCha20-Poly1305 & 4G CGNAT traversal (Py/C99/JS/Java/ESP32)",
    },
    {
        "id": "sense",
        "name": "NeckSense",
        "pypi": "necksense",
        "repo": "https://github.com/indian-aman/necklink/tree/main/packages/necksense",
        "region": "Antennal Lobe & Mushroom Body",
        "summary": "Olsen-Wilson 100x concentration-invariant adaptation, 5% WTA sparse FlyHash & 1-shot anomaly/valence memory (Py + Q16.16 C99)",
    },
    {
        "id": "compass",
        "name": "NeckCompass",
        "pypi": "neckcompass",
        "repo": "https://github.com/indian-aman/necklink/tree/main/packages/neckcompass",
        "region": "Central Complex (E-PG / CPU4 / PFL3)",
        "summary": "Topological 16-wedge ring-attractor compass (10.2x lower ferromagnetic spike error than EKF) & GPS-denied homing (Py + Q15 C99)",
    },
    {
        "id": "sight",
        "name": "NeckSight",
        "pypi": "necksight",
        "repo": "https://github.com/indian-aman/necklink/tree/main/packages/necksight",
        "region": "Optic Lobe (R1-R6 / T4-T5 / LPLC2)",
        "summary": "Log glare cancellation, 3-arm Reichardt/Barlow-Levick EMDs & 4-quadrant looming collision reflex (0.9988 AUROC, >12k FPS C99)",
    },
    {
        "id": "cord",
        "name": "NeckCord",
        "pypi": "neckcord",
        "repo": "https://github.com/indian-aman/necklink/tree/main/packages/neckcord",
        "region": "Ventral Nerve Cord (BANC / MANC)",
        "summary": "4-byte CRC-4 descending command codec, self-healing 6-limb CPG & 42 ms shunting thruster fault reallocator (Py + Q15 C99)",
    },
]

_MODULE_MAP = {
    "sense": "necksense",
    "compass": "neckcompass",
    "sight": "necksight",
    "cord": "neckcord",
}


def load_organ(organ_alias: str) -> Any:
    """Import and return one of the NeckLink organ packages ('sense', 'compass', 'sight', 'cord')."""
    pkg = _MODULE_MAP.get(organ_alias)
    if pkg is None:
        raise AttributeError(f"Unknown NeckLink organ {organ_alias!r}")
    try:
        return importlib.import_module(pkg)
    except ImportError as exc:
        raise ImportError(
            f"NeckLink organ '{organ_alias}' requires `{pkg}`. "
            f"Install it via `pip install {pkg}` or install the full suite via `pip install necklink`."
        ) from exc


def check_suite_installed() -> Dict[str, str]:
    """Return installed version strings (or 'not-installed') for all 5 NeckLink packages."""
    from . import __version__ as nltp_ver

    status: Dict[str, str] = {"pynltp": nltp_ver}
    for pkg in ("necksense", "neckcompass", "necksight", "neckcord"):
        try:
            mod = importlib.import_module(pkg)
            status[pkg] = getattr(mod, "__version__", "installed")
        except ImportError:
            status[pkg] = "not-installed"
    return status


def run_autonomous_reflex_demo(loss: float = 0.15, seed: int = 42) -> Dict[str, Any]:
    """
    Run a live 5-organ closed-loop Drosophila CNS reflex mission over encrypted UDP:
      - NeckCompass rejects a 120 deg ferromagnetic spike and steers home without GPS.
      - NeckSense habituates to 25x background gusts and flags a 1-shot hydrocarbon anomaly.
      - NeckSight ignores a 50x sunlight glare flash, then catches a head-on looming obstacle
        and dispatches an encrypted Giant Fiber `LOOM_ESTOP` reflex over NLTP.
      - NeckCord encodes 4-byte CRC-4 descending motor commands and isolates a 90%-fouled
        USV thruster in ~42 ms.
      - NLTP delivers the encrypted reflex, 4-byte cervical frames, and SHA-256 mission log
        across a lossy OS UDP socket.
    """
    necksense = load_organ("sense")
    neckcompass = load_organ("compass")
    necksight = load_organ("sight")
    neckcord = load_organ("cord")

    rng = np.random.default_rng(seed)

    # 1. Start live encrypted NLTP server & client on localhost with simulated packet loss
    psk = generate_nltp_key()

    with tempfile.TemporaryDirectory() as tmpdir:
        with NLTPServer(
            ("127.0.0.1", 0),
            save_dir=tmpdir,
            psk=psk,
            loss_drop_sim=loss,
        ) as srv, NLTPClient(
            ("127.0.0.1", srv.getsockname()[1]),
            psk=psk,
            loss_drop_sim=loss,
        ) as cli:
            # 2. Central Complex: outbound survey + 120 deg magnetic dock spike + homebound turn
            ring = neckcompass.RingCompass(n_wedges=16, initial_heading_rad=0.0)
            nav = neckcompass.FanBodyNavigator(n_columns=16, compass=ring)
            true_hdg = 0.35
            ring.reset(true_hdg)
            for step_i in range(60):
                # Inject a violent +120 deg ferromagnetic hull spike on steps 25..30
                spike = np.deg2rad(120.0) if 25 <= step_i < 30 else 0.0
                mag_meas = neckcompass.wrap_angle(true_hdg + spike + float(rng.normal(0.0, 0.02)))
                est_hdg, dist_home, _ = nav.step_with_compass(
                    omega_rad_s=0.0,
                    speed_m_s=1.8,
                    dt=0.02,
                    mag_heading_rad=mag_meas,
                )
            spike_err_deg = float(np.rad2deg(abs(neckcompass.circular_error_rad(est_hdg, true_hdg))))
            home_turn = float(nav.steering_cmd(max_turn_rate=1.0))

            # 3. Antennal Lobe & Mushroom Body: 25x background gusts -> 1-shot plume anomaly
            novelty_det = necksense.MushroomNoveltyDetector(
                n_inputs=8,
                stateful_antennal=True,
                n_kc=1024,
                seed=seed,
            )
            bg_proto = np.array([0.8, 0.2, 0.5, 0.1, 0.9, 0.3, 0.4, 0.2], dtype=np.float64)
            bg_nov = 1.0
            for i in range(18):
                conc = 0.2 + 5.0 * ((i % 5) + 1)  # 1.2x .. 25.2x concentration swings
                bg_nov = novelty_det.update_and_score(bg_proto * conc + rng.uniform(0.0, 0.005, size=8))
            hydrocarbon_plume = np.array([0.1, 1.4, 0.05, 1.6, 0.1, 1.2, 0.05, 1.5], dtype=np.float64)
            anomaly_nov = novelty_det.update_and_score(hydrocarbon_plume * 3.0)

            # 4. Optic Lobe -> Giant Fiber: 50x glare step (0 false alarm) -> looming disk -> NLTP E-Stop
            optic = necksight.NeckSightPipeline(
                grid_size=64,
                threshold=0.045,
                nltp_client=cli,
                nltp_msg="LOOM_ESTOP",
            )
            yy, xx = np.mgrid[0:64, 0:64]
            base_bg = 40.0 + 12.0 * np.sin(xx * 0.25) * np.cos(yy * 0.25)
            glare_false_alarm = False
            for f_idx in range(8):
                glare_mult = 50.0 if f_idx >= 4 else 1.0
                trig = optic.step(base_bg * glare_mult, dt=1.0 / 60.0)
                if trig.triggered:
                    glare_false_alarm = True

            loom_triggered = False
            peak_loom_score = 0.0
            for r in np.linspace(3.0, 29.0, 22):
                frame = base_bg.copy() * 50.0
                mask = (xx - 32.0) ** 2 + (yy - 32.0) ** 2 <= r ** 2
                frame[mask] = 6.0  # Approaching dark obstacle
                trig = optic.step(frame, dt=1.0 / 60.0)
                peak_loom_score = max(peak_loom_score, float(trig.looming_score))
                if trig.triggered:
                    loom_triggered = True

            # 5. Cervical 4B Codec + VNC Shunting Thruster Cord under 90% T2 weed-fouling
            c45 = float(np.sqrt(0.5))
            B_usv = np.array(
                [
                    [c45, c45, c45, c45],
                    [c45, -c45, -c45, c45],
                    [0.45, -0.45, 0.45, -0.45],
                ],
                dtype=np.float64,
            )
            cord_alloc = neckcord.ShuntingThrusterCord(B_usv, u_max=35.0)
            cmd_frame = neckcord.DescendingFrame(
                drive_fwd=0.65,
                turn_yaw=float(np.clip(home_turn, -1.0, 1.0)),
                strafe_lat=0.0,
                gait_mode=int(neckcord.GaitMode.TRIPOD),
                freeze_reflex=False,
                seq=1,
            )
            wire_4b = neckcord.encode_descending_4b(cmd_frame)
            cli.send_bytes(wire_4b, timeout=6.0)

            alloc_res = None
            actual_cmd = np.zeros(4, dtype=np.float64)
            for _ in range(12):
                fb = actual_cmd.copy()
                fb[1] *= 0.10  # Thruster #1 suffers 90% thrust loss (weed-fouling)
                alloc_res = cord_alloc.step(wire_4b, feedback_thrust=fb, dt=0.02)
                actual_cmd = alloc_res.commands.copy()

            # 6. Send SHA-256 verified mission summary over NLTP
            summary = {
                "loss_simulated": loss,
                "compass_spike_error_deg": round(spike_err_deg, 3),
                "compass_home_distance_m": round(float(dist_home), 2),
                "sense_background_novelty": round(float(bg_nov), 4),
                "sense_plume_anomaly_novelty": round(float(anomaly_nov), 4),
                "sight_glare_50x_false_alarm": glare_false_alarm,
                "sight_looming_triggered": loom_triggered,
                "sight_peak_loom_score": round(peak_loom_score, 4),
                "cord_cervical_frame_hex": "0x" + wire_4b.hex().upper(),
                "cord_fouled_thruster_health": round(float(alloc_res.health[1]), 3),
                "cord_fouled_thruster_cmd_N": round(float(abs(alloc_res.commands[1])), 3),
            }
            report_path = Path(tmpdir) / "mission_report.json"
            report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            receipt = cli.send_file(report_path, timeout=8.0)

            rx_items = []
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                item = srv.recv(timeout=0.05)
                if item is None:
                    if len(rx_items) >= 3:
                        break
                    continue
                rx_items.append(item)

            summary["nltp_reflex_received"] = any(
                it.kind == "reflex" and b"LOOM_ESTOP" in it.data for it in rx_items
            )
            summary["nltp_4b_command_received"] = any(
                it.data == wire_4b for it in rx_items
            )
            summary["nltp_file_sha256_verified"] = bool(receipt.get("verified_sha256", False))
            return summary
