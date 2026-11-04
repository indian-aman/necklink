#!/usr/bin/env python3
"""
End-to-end host test of the real-project ESP32 example sketches (no hardware needed):

    ESP32_ESPNow_Sensor_Node.ino --(simulated ESP-NOW radio)--> ESP32_ESPNow_Gateway.ino
        --(real UDP socket, NLTP over Wi-Fi side)--> this script's Python NLTPServer

Both .ino files are compiled unmodified as two simulated ESP32s (csrc/tools/espnow_sim/arduino).
The sensor samples once per (simulated) second and sends a 60-sample CSV batch every minute; the
script checks that every batch arrives at the shore server intact and SHA-256 verified, with and
without packet loss on both hops.

What this does NOT cover: real radio behaviour, the Arduino toolchain, ESP32 RAM/timing. Flash the
sketches on real boards before relying on them.

    python scripts/esp32_sketch_e2e_test.py            # builds csrc/build/sketch_runner if needed
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from necklink.nltp import NLTPServer  # noqa: E402

PSK = "replace-with-output-of-nltp-gen-key"  # the placeholder written in both sketches
RUNNER = ROOT / "csrc" / "build" / "sketch_runner"
SIM_SECONDS = 200  # 3 full batches (at ~60 s, ~120 s, ~180 s of simulated time)

SCENARIOS = [
    ("clean links", 0, 0.0),
    ("10% ESP-NOW loss + 10% UDP loss", 10, 0.10),
    ("20% ESP-NOW loss + 15% UDP loss", 20, 0.15),
]


def run(espnow_drop_pct: int, udp_loss: float) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as d:
        srv = NLTPServer(("127.0.0.1", 0), save_dir=d, psk=PSK, loss_drop_sim=udp_loss)
        port = srv.getsockname()[1]
        t0 = time.time()
        p = subprocess.run([str(RUNNER), str(port), str(SIM_SECONDS), str(espnow_drop_pct)],
                           capture_output=True, text=True, timeout=600)
        wall = time.time() - t0
        items = []
        while (it := srv.recv(timeout=1.0)) is not None:
            items.append(it)
        srv.close()

    names = sorted(it.filename for it in items)
    good = [it for it in items if it.kind == "file" and it.sha256_verified
            and it.data.decode().count("\n") == 61 and it.data.startswith(b"uptime_ms,value\n")]
    uniq = sorted({it.filename for it in good})
    expected = [f"0001_batch_{i:06d}.csv" for i in (1, 2, 3)]
    ok = p.returncode == 0 and uniq == expected
    detail = (f"{len(uniq)}/3 batches verified on shore ({len(items)} deliveries incl. duplicates), "
              f"sim {SIM_SECONDS} s in {wall:.1f} s wall")
    if not ok:
        detail += f"\n    got {names}\n    sketch log tail:\n" + "\n".join("      " + l for l in p.stdout.splitlines()[-25:])
    return ok, detail


def main() -> int:
    subprocess.run(["make", "-s", "-C", str(ROOT / "csrc"), "sketch-runner"], check=True)
    failures = 0
    for name, drop, loss in SCENARIOS:
        ok, detail = run(drop, loss)
        failures += not ok
        print(f"  {'OK  ' if ok else 'FAIL'} {name:34s} {detail}", flush=True)
    print(f"{'PASSED' if not failures else 'FAILED'}: ESP32 sensor -> gateway -> shore example sketches "
          f"({len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
