"""
Laptop <-> ESP32 NLTP test over Wi-Fi, driving the board on its USB serial port.

Needs the esp32/ firmware flashed (python -m platformio run -d esp32 -t upload) and the laptop on the same
Wi-Fi network. Runs, for each size:
  1. laptop -> ESP32 over NLTP (first transfer cold, later ones warm on the same connection)
  2. laptop -> ESP32 over plain TCP to the board's sink on port 9702 (same radio, for comparison)
  3. ESP32 -> laptop over NLTP (serial command "send N"; data regenerated here and compared byte-for-byte)

    python scripts/esp32_test.py --port COM9 --sizes 16384 65536 90000 --reps 3
    python scripts/esp32_test.py --psk auto ...   (encrypted: a fresh random key is sent to the board over USB)
    python scripts/esp32_test.py --radio marine-radio ...   (NLTP through an emulated radio link, see
                                                             scripts/radio_emulator.py; TCP is skipped)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import statistics
import struct
import sys
import tempfile
import time
from pathlib import Path

import serial  # pyserial (installed with esptool / platformio)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from necklink.internet import generate_nltp_key  # noqa: E402
from necklink.nltp import NLTPClient  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from radio_emulator import PROFILES, RadioEmulator  # noqa: E402

NLTP_PORT, TCP_PORT = 9700, 9702


def esp_test_data(n: int) -> bytes:
    """Same xorshift32 stream as fill_test_data() in esp32/src/main.cpp."""
    x = 0x9E3779B9
    out = bytearray(n)
    for i in range(n):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        out[i] = x & 0xFF
    return bytes(out)


class Board:
    def __init__(self, port: str) -> None:
        self.ser = serial.Serial(port, 115200, timeout=0.2)

    def reset_and_wait_ip(self, timeout: float = 30.0) -> str:
        self.ser.dtr = False
        self.ser.rts = True
        time.sleep(0.15)
        self.ser.rts = False
        ip = ""
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.ser.readline().decode(errors="replace").strip()
            if line:
                print(f"  [esp32] {line}")
            m = re.match(r"IP (\S+)", line)
            if m:
                ip = m.group(1)
            if line.startswith("READY") and ip:
                return ip
            if line.startswith("ERROR"):
                raise RuntimeError(line)
        raise TimeoutError("board did not join Wi-Fi (check esp32/include/secrets.h)")

    def command(self, text: str) -> None:
        self.ser.write((text + "\n").encode())

    def wait_line(self, prefix: str, timeout: float = 5.0) -> str:
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.ser.readline().decode(errors="replace").strip()
            if line.startswith(prefix):
                return line
        raise TimeoutError(f"no '{prefix}' from board")

    def wait_result(self, kind: str, timeout: float = 30.0) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.ser.readline().decode(errors="replace").strip()
            if line.startswith(f"RESULT {kind} "):
                return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", line)}
            if line.startswith("ERROR"):
                raise RuntimeError(line)
        return {}


def tcp_send(ip: str, data: bytes) -> tuple[bool, float]:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((ip, TCP_PORT), timeout=30) as c:
            c.sendall(struct.pack("<Q", len(data)) + data)
            ok = c.recv(1) == b"1"
    except OSError:
        ok = False
    return ok, time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM9")
    ap.add_argument("--sizes", type=int, nargs="+", default=[16384, 65536, 90000])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--psk", default=None, help="'auto' = fresh random key per run, or a key/passphrase")
    ap.add_argument("--radio", default=None, choices=sorted(PROFILES), help="route NLTP through an emulated radio")
    a = ap.parse_args()

    board = Board(a.port)
    print("resetting board and waiting for Wi-Fi ...")
    ip = board.reset_and_wait_ip()
    print(f"board IP {ip}\n")
    psk = generate_nltp_key() if a.psk == "auto" else a.psk
    if psk:
        board.command(f"psk {psk}")
        board.wait_line("PSK on")
        print("encryption on (ChaCha20-Poly1305)\n")
    rows = []
    tmp = Path(tempfile.mkdtemp())
    emu = None
    target = (ip, NLTP_PORT)
    if a.radio:
        emu = RadioEmulator((ip, NLTP_PORT), PROFILES[a.radio])
        target = emu.address
        p = PROFILES[a.radio]
        print(f"emulated radio '{a.radio}': {p.loss_pct}% loss, {p.burst_pct}% of time in fades, "
              f"{p.delay_ms}+-{p.jitter_ms} ms each way, {p.rate_kbps:.0f} kbit/s (TCP skipped)\n")
    with NLTPClient(target, max_tx_kbps=20000, psk=psk) as cli:
        for size in a.sizes:
            data = os.urandom(size)
            path = tmp / f"to_esp_{size}.bin"
            path.write_bytes(data)
            for rep in range(a.reps):
                r = cli.send_file(path, timeout=60)
                esp = board.wait_result("rx", 10)
                rows.append({"test": "nltp laptop->esp32" + (" (cold)" if not rows else ""), "size": size, "rep": rep,
                             "ok": bool(r["verified_sha256"]) and esp.get("ok") == 1, "elapsed_s": r["elapsed_s"]})
                if not emu:
                    ok, el = tcp_send(ip, data)
                    board.wait_result("tcp", 10)
                    rows.append({"test": "tcp laptop->esp32", "size": size, "rep": rep, "ok": ok, "elapsed_s": el})
                board.command(f"send {size}")
                t0 = time.perf_counter()
                item = cli.recv(timeout=60)
                el = time.perf_counter() - t0
                esp = board.wait_result("tx", 10)
                good = item is not None and item.data == esp_test_data(size)
                rows.append({"test": "nltp esp32->laptop", "size": size, "rep": rep,
                             "ok": good and esp.get("ok") == 1, "elapsed_s": el, "esp_ms": esp.get("ms")})
                print(f"size {size} rep {rep + 1}/{a.reps}: "
                      + ", ".join(f"{r_['test']} {'OK' if r_['ok'] else 'FAIL'} {r_['elapsed_s']:.3f}s"
                                  for r_ in rows[-(2 if emu else 3):]),
                      flush=True)
        board.command("stat")
        time.sleep(0.5)
        print("  [esp32] " + board.ser.read(400).decode(errors="replace").strip())

    radio_stats = emu.stats() if emu else None
    if emu:
        emu.close()
        print(f"emulated radio packets: {radio_stats}")
    name = "esp32_test" + (f"_radio_{a.radio}" if a.radio else "") + ("_encrypted" if psk else "")
    out = ROOT / "results" / f"{name}.json"
    out.write_text(json.dumps({"board_ip": "<redacted>", "encrypted": bool(psk), "radio": a.radio,
                               "radio_stats": radio_stats, "rows": rows}, indent=1))
    print(f"\nwrote {out}\n")
    print(f"{'test':24s} {'size':>7s} {'ok':>5s} {'median s':>9s} {'min s':>7s}")
    for size in a.sizes:
        for test in ("nltp laptop->esp32", "tcp laptop->esp32", "nltp esp32->laptop"):
            rs = [r for r in rows if r["test"].startswith(test) and r["size"] == size]
            if not rs:
                continue
            okt = [r["elapsed_s"] for r in rs if r["ok"]]
            print(f"{test:24s} {size:7d} {len(okt):>2d}/{len(rs):<2d}"
                  + (f" {statistics.median(okt):9.3f} {min(okt):7.3f}" if okt else "      FAIL"))


if __name__ == "__main__":
    main()
