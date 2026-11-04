"""
Example 5 (real project): vessel <-> shore link with store-and-forward telemetry and a confirmed E-stop.

A survey boat (or buoy, rover, drone) logs sensor samples, packs them into compressed batch files and
uploads them to a shore server over a lossy radio / 4G link. The shore operator can stop the vessel
at any time with an urgent reflex that is retransmitted until the vessel acknowledges it.

Patterns shown (the parts you would copy into a real project):
  * The shared key comes from the NLTP_PSK environment variable (create one with `nltp gen-key`).
  * Store-and-forward spool: batches are written to disk first and deleted only after the shore
    has verified their SHA-256, so nothing is lost across link outages or reboots.
  * Commands from shore: an E-stop reflex (urgent, retransmitted until acknowledged) and JSON settings.
  * Status heartbeats as small JSON messages.

Run the two ends on two machines:
    export NLTP_PSK="$(nltp gen-key)"            # same key on both machines
    python 05_vessel_shore_link.py shore  --port 9700 --save-dir ./inbox
    python 05_vessel_shore_link.py vessel --shore 203.0.113.10:9700 --spool ./spool
    # in the shore terminal, type `estop` + Enter to stop the vessel, `interval 2` to change its rate

Or try both ends locally with 15% simulated packet loss in each direction:
    python 05_vessel_shore_link.py demo --loss 0.15
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from nltp import NLTPClient, NLTPServer, generate_nltp_key


# --------------------------------------------------------------------------------------------
# Vessel side
# --------------------------------------------------------------------------------------------
class Vessel:
    def __init__(self, shore: tuple, psk: str, spool: Path, *, interval_s: float = 1.0,
                 samples_per_batch: int = 30, loss: float = 0.0) -> None:
        self.spool = spool
        self.spool.mkdir(parents=True, exist_ok=True)
        self.interval_s = interval_s
        self.samples_per_batch = samples_per_batch
        self.thrusters_enabled = True
        self.batches_verified = 0
        self._samples: list = []
        self._t0 = time.time()
        self._lat, self._lon = 59.4370, 24.7536
        self.client = NLTPClient(shore, psk=psk, max_tx_kbps=2_000, loss_drop_sim=loss)

    # A real vessel reads GPS / IMU / battery here.
    def read_sensors(self) -> dict:
        t = time.time() - self._t0
        if self.thrusters_enabled:
            self._lat += 0.00001 * math.cos(t / 30.0)
            self._lon += 0.00001 * math.sin(t / 30.0)
        return {
            "t": round(time.time(), 3),
            "lat": round(self._lat, 6),
            "lon": round(self._lon, 6),
            "heading_deg": round((t * 3.0) % 360.0, 1),
            "battery_v": round(12.6 - t * 0.001 + random.uniform(-0.02, 0.02), 3),
            "water_temp_c": round(14.0 + random.uniform(-0.1, 0.1), 2),
            "thrusters": self.thrusters_enabled,
        }

    def _spool_batch(self) -> None:
        name = f"batch_{int(time.time() * 1000)}.jsonl.gz"
        tmp = self.spool / (name + ".part")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            for s in self._samples:
                f.write(json.dumps(s) + "\n")
        tmp.rename(self.spool / name)  # atomic: a half-written batch is never uploaded
        self._samples.clear()

    def _upload_spool(self) -> None:
        for path in sorted(self.spool.glob("batch_*.jsonl.gz")):
            receipt = self.client.send_file(path, timeout=20.0)
            if receipt["verified_sha256"]:
                path.unlink()  # shore has a verified copy
                self.batches_verified += 1
                print(f"[vessel] uploaded {path.name} ({receipt['bytes']} B in {receipt['elapsed_s'] * 1000:.0f} ms)",
                      flush=True)
            else:
                print(f"[vessel] {path.name} not confirmed; kept in spool for the next attempt", flush=True)
                return  # link is bad right now; keep order and retry later

    def _handle_commands(self) -> None:
        while True:
            item = self.client.recv(timeout=0.0)
            if item is None:
                return
            if item.kind == "reflex" and item.as_text() == "ESTOP":
                self.thrusters_enabled = False  # a real vessel cuts thrust here (and has a hardware interlock)
                print("[vessel] E-STOP received: thrusters disabled", flush=True)
            elif item.kind == "json":
                cmd = item.as_json()
                if "interval_s" in cmd:
                    self.interval_s = float(cmd["interval_s"])
                    print(f"[vessel] sample interval set to {self.interval_s} s", flush=True)

    def run(self, stop: threading.Event, max_batches: Optional[int] = None) -> None:
        next_sample = time.time()
        while not stop.is_set():
            self._handle_commands()
            if time.time() >= next_sample:
                self._samples.append(self.read_sensors())
                next_sample += self.interval_s
                if len(self._samples) >= self.samples_per_batch:
                    self._spool_batch()
                    self._upload_spool()
                    self.client.send_json({"status": "ok", "thrusters": self.thrusters_enabled,
                                           "spooled": len(list(self.spool.glob("batch_*.jsonl.gz")))}, timeout=5.0)
                    if max_batches is not None and self.batches_verified >= max_batches:
                        return
            time.sleep(0.01)

    def close(self) -> None:
        self.client.close()


# --------------------------------------------------------------------------------------------
# Shore side
# --------------------------------------------------------------------------------------------
class Shore:
    def __init__(self, port: int, psk: str, save_dir: Path, *, host: str = "0.0.0.0", loss: float = 0.0) -> None:
        self.server = NLTPServer((host, port), save_dir=save_dir, psk=psk, loss_drop_sim=loss)
        self.vessel_addr = None
        self.batches = 0
        self.samples = 0

    def address(self) -> tuple:
        return self.server.getsockname()

    def poll(self) -> None:
        item = self.server.recv(timeout=0.05)
        if item is None:
            return
        self.vessel_addr = item.peer_addr  # follows the vessel if its IP changes (roaming)
        if item.kind == "file" and item.sha256_verified:
            rows = gzip.decompress(item.data).decode("utf-8").splitlines()
            last = json.loads(rows[-1])
            self.batches += 1
            self.samples += len(rows)
            print(f"[shore]  batch {item.filename}: {len(rows)} samples, last fix "
                  f"{last['lat']:.5f},{last['lon']:.5f}, battery {last['battery_v']} V", flush=True)
        elif item.kind == "json":
            print(f"[shore]  status: {item.as_json()}", flush=True)

    def estop(self, timeout: float = 3.0) -> bool:
        if self.vessel_addr is None:
            print("[shore]  no vessel seen yet", flush=True)
            return False
        seq = self.server.send_reflex(self.vessel_addr, "ESTOP")
        acked = self.server.wait_reflex_ack(seq, timeout=timeout)
        print(f"[shore]  E-STOP sent -> {'acknowledged by vessel' if acked else 'NOT acknowledged (check link!)'}",
              flush=True)
        return acked

    def set_interval(self, seconds: float) -> None:
        if self.vessel_addr is not None:
            self.server.send_bytes(self.vessel_addr, json.dumps({"interval_s": seconds}).encode(), timeout=5.0)

    def close(self) -> None:
        self.server.close()


# --------------------------------------------------------------------------------------------
def _psk() -> str:
    psk = os.environ.get("NLTP_PSK")
    if not psk:
        sys.exit("Set NLTP_PSK first (create one with `nltp gen-key`; use the same key on both ends).")
    return psk


def run_demo(loss: float) -> int:
    psk = generate_nltp_key()
    with tempfile.TemporaryDirectory() as tmp:
        shore = Shore(0, psk, Path(tmp) / "inbox", host="127.0.0.1", loss=loss)
        vessel = Vessel(shore.address(), psk, Path(tmp) / "spool", interval_s=0.02, samples_per_batch=25, loss=loss)
        stop = threading.Event()
        worker = threading.Thread(target=vessel.run, args=(stop, 4), daemon=True)
        print(f"demo: vessel and shore on localhost, {loss:.0%} simulated loss each way\n", flush=True)
        worker.start()
        estop_acked = False
        deadline = time.time() + 60
        while time.time() < deadline and (worker.is_alive() or shore.batches < 4):
            shore.poll()
            if shore.batches >= 2 and not estop_acked:
                estop_acked = shore.estop()
            if not worker.is_alive() and shore.batches >= 4:
                break
        stop.set()
        worker.join(timeout=5)
        for _ in range(20):
            shore.poll()
        ok = shore.batches >= 4 and estop_acked and not vessel.thrusters_enabled
        print(f"\nresult: {shore.batches} batches / {shore.samples} samples verified on shore, "
              f"E-stop acknowledged={estop_acked}, vessel thrusters enabled={vessel.thrusters_enabled}")
        vessel.close()
        shore.close()
        return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="role", required=True)
    s = sub.add_parser("shore")
    s.add_argument("--port", type=int, default=9700)
    s.add_argument("--save-dir", default="./inbox")
    v = sub.add_parser("vessel")
    v.add_argument("--shore", required=True, help="HOST:PORT of the shore server")
    v.add_argument("--spool", default="./spool")
    v.add_argument("--interval", type=float, default=1.0, help="seconds between sensor samples")
    v.add_argument("--batch", type=int, default=60, help="samples per uploaded batch")
    d = sub.add_parser("demo")
    d.add_argument("--loss", type=float, default=0.15)
    args = ap.parse_args()

    if args.role == "demo":
        return run_demo(args.loss)

    if args.role == "shore":
        shore = Shore(args.port, _psk(), Path(args.save_dir))
        print(f"[shore]  listening on UDP {args.port}; type `estop` or `interval <s>`", flush=True)

        def console() -> None:
            for line in sys.stdin:
                parts = line.split()
                if parts[:1] == ["estop"]:
                    shore.estop()
                elif parts[:1] == ["interval"] and len(parts) == 2:
                    shore.set_interval(float(parts[1]))

        threading.Thread(target=console, daemon=True).start()
        try:
            while True:
                shore.poll()
        except KeyboardInterrupt:
            shore.close()
        return 0

    host, port = args.shore.rsplit(":", 1)
    vessel = Vessel((host, int(port)), _psk(), Path(args.spool), interval_s=args.interval, samples_per_batch=args.batch)
    try:
        vessel.run(threading.Event())
    except KeyboardInterrupt:
        vessel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
