"""CLI entrypoint for `necklink-cns` (`v3.0.0`)."""

from __future__ import annotations

import argparse
import json
import sys
import numpy as np

from . import __version__
from .cns import NeckLinkCNS
from .connectome import BANC_V888_PRIORS


def run_demo() -> dict:
    cns = NeckLinkCNS(vehicle="usv_t200", grid_size=32)
    yy, xx = np.mgrid[0:32, 0:32]
    bg = 150.0 + 15.0 * np.sin(xx * 0.4) * np.cos(yy * 0.4)

    out = None
    dodged = False
    for step in range(24):
        frame = bg.copy()
        if step >= 6:
            hw = 1 + (step - 6) // 3
            frame[:, max(0, 9 - hw) : min(32, 9 + hw + 1)] = 10.0  # Left pole looming
        fb = cns._last_cmds.copy()
        if step >= 8:
            fb[1] *= 0.10  # 90% weed-fouled thruster #1
        out = cns.step(
            dt=0.02,
            speed_m_s=1.5,
            gyro_xyz=np.array([0.05, -0.03, 0.0]),
            accel_xyz=np.array([0.8, -0.6, 9.75]),
            mag_xyz=np.array([0.42, 0.0, 0.90]),
            camera_frame=frame,
            feedback_thrust=fb,
        )
        if out.descending.behavior_mode in ("DODGE_LOOM", "MOONWALK_REVERSE"):
            dodged = True

    assert out is not None
    return {
        "version": __version__,
        "banc_v888_neurons": BANC_V888_PRIORS["total_neurons"],
        "thin_pole_dodged": dodged,
        "final_mode": out.descending.behavior_mode,
        "fouled_thruster1_health": round(float(out.actuator_health[1]), 3),
        "cervical_4b_hex": "0x" + out.cervical_wire_4b.hex().upper(),
        "mavlink_v2_bytes": len(out.mavlink_manual_pkt),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="necklink-cns", description="NeckLink Unified Whole-CNS Autonomy CLI")
    parser.add_argument("--version", action="store_true", help="Print package version")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("demo", help="Run closed-loop 4-organ + MAVLink v2 survival demo")
    sub.add_parser("priors", help="Print empirical BANC v888 connectome priors")

    args = parser.parse_args(argv)
    if args.version:
        print(f"necklink-cns {__version__}")
        return 0
    if args.cmd == "priors":
        print(json.dumps(BANC_V888_PRIORS["derived_biophysical_ratios"], indent=2))
        return 0
    res = run_demo()
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
