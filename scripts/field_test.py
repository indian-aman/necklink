"""
Real-link field test: NLTP vs kernel TCP (and KCP if built) between two machines you control,
e.g. a laptop and a Raspberry Pi over the boat/buoy radio, or a laptop and a cloud VPS over 4G.

On the receiving machine (must be reachable on the chosen UDP+TCP ports):
    python scripts/field_test.py server --bind 0.0.0.0 --port 9500

On the sending machine:
    python scripts/field_test.py client --server <receiver-ip> --port 9500 --sizes 65536 1048576 --reps 5

The client writes results/field_test_<timestamp>.json and prints a summary table. Each protocol
sends the same random file; success means the receiver verified it byte-for-byte (SHA-256).
TCP congestion control (cubic/bbr) can only be chosen on Linux senders; elsewhere "tcp" uses the OS default.
KCP runs only if /opt/kcpbench/bin/kcp_xfer exists on both machines (see scripts/wsl/setup_kcp.sh).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
KCP_XFER = Path("/opt/kcpbench/bin/kcp_xfer")
TIMEOUT_S = 120.0


# ---------------------------------------------------------------------------
# Receiver: one control TCP port (port) + NLTP UDP (port+1) + data TCP (port+2) + KCP UDP (port+3)
# ---------------------------------------------------------------------------

def _tcp_data_server(bind: str, port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(4)
    while True:
        conn, _ = srv.accept()
        with conn:
            hdr = b""
            while len(hdr) < 40:
                chunk = conn.recv(40 - len(hdr))
                if not chunk:
                    break
                hdr += chunk
            if len(hdr) < 40:
                continue
            size = struct.unpack("<Q", hdr[:8])[0]
            want_sha = hdr[8:40].hex()
            h = hashlib.sha256()
            got = 0
            while got < size:
                b = conn.recv(min(1 << 16, size - got))
                if not b:
                    break
                h.update(b)
                got += len(b)
            conn.sendall(b"1" if (got == size and h.hexdigest() == want_sha) else b"0")


def run_server(bind: str, port: int, psk: str | None) -> None:
    from necklink.nltp import NLTPServer

    threading.Thread(target=_tcp_data_server, args=(bind, port + 2), daemon=True).start()
    nltp = NLTPServer((bind, port + 1), save_dir=ROOT / "results" / "field_inbox", psk=psk)
    ctl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ctl.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ctl.bind((bind, port))
    ctl.listen(1)
    print(f"field-test receiver ready: control tcp/{port}, nltp udp/{port + 1}, tcp data tcp/{port + 2}, kcp udp/{port + 3}")
    while True:
        conn, peer = ctl.accept()
        with conn:
            f = conn.makefile("rwb")
            for line in f:
                cmd = json.loads(line)
                if cmd["op"] == "kcp-expect":
                    # The client uploads the reference file first over the control channel, then the server
                    # starts kcp_xfer, which compares what arrives over KCP against it.
                    ref = Path("/tmp/field_kcp_ref.bin")
                    ref.write_bytes(f.read(cmd["size"]))
                    proc = subprocess.Popen([str(KCP_XFER), "server", bind, str(port + 3), str(ref), "fast128"],
                                            stdout=subprocess.PIPE, text=True)
                    proc.stdout.readline()  # READY
                    f.write(b'{"ready": true}\n')
                    f.flush()
                    out = proc.communicate(timeout=TIMEOUT_S + 20)[0]
                    ok = '"ok": true' in out
                    f.write((json.dumps({"ok": ok}) + "\n").encode())
                    f.flush()
                elif cmd["op"] == "nltp-check":
                    item = nltp.recv(timeout=5.0)
                    ok = item is not None and item.sha256_verified
                    f.write((json.dumps({"ok": ok}) + "\n").encode())
                    f.flush()
                elif cmd["op"] == "bye":
                    break


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------

def _tcp_send(server: str, port: int, data: bytes, cc: str) -> Dict[str, Any]:
    t0 = time.perf_counter()
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if cc != "default" and hasattr(socket, "TCP_CONGESTION"):
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, cc.encode())
    c.settimeout(TIMEOUT_S)
    try:
        c.connect((server, port))
        c.sendall(struct.pack("<Q", len(data)) + hashlib.sha256(data).digest() + data)
        ok = c.recv(1) == b"1"
    except OSError:
        ok = False
    finally:
        c.close()
    return {"ok": ok, "elapsed_s": time.perf_counter() - t0}


def run_client(server: str, port: int, sizes: List[int], reps: int, psk: str | None) -> None:
    from necklink.nltp import NLTPClient

    tcp_ccs = ["cubic", "bbr"] if sys.platform.startswith("linux") else ["default"]
    ctl = socket.create_connection((server, port), timeout=TIMEOUT_S + 30)
    f = ctl.makefile("rwb")

    def ask(obj: Dict[str, Any], payload: bytes = b"") -> Dict[str, Any]:
        f.write((json.dumps(obj) + "\n").encode() + payload)
        f.flush()
        return json.loads(f.readline())

    rows: List[Dict[str, Any]] = []
    with NLTPClient((server, port + 1), psk=psk) as cli:
        for size in sizes:
            data = os.urandom(size)
            path = ROOT / "results" / f"field_blob_{size}.bin"
            path.write_bytes(data)
            for rep in range(reps):
                tx0 = cli.engine.wire_bytes_tx + cli.engine.wire_bytes_rx
                r = cli.send_file(path, timeout=TIMEOUT_S)
                ok = bool(r["verified_sha256"]) and ask({"op": "nltp-check"})["ok"]
                wire = cli.engine.wire_bytes_tx + cli.engine.wire_bytes_rx - tx0
                rows.append({"protocol": "nltp" + (" (warm)" if rep else " (cold)"), "size": size, "rep": rep, "ok": ok,
                             "elapsed_s": r["elapsed_s"], "wire_overhead_x": round(wire / size, 3)})
                for cc in tcp_ccs:
                    res = _tcp_send(server, port + 2, data, cc)
                    rows.append({"protocol": f"tcp-{cc}", "size": size, "rep": rep, **res})
                if KCP_XFER.exists():
                    if ask({"op": "kcp-expect", "size": size}, data).get("ready"):
                        out = subprocess.run([str(KCP_XFER), "client", server, str(port + 3), str(path), "fast128"],
                                             capture_output=True, text=True, timeout=TIMEOUT_S + 10).stdout
                        res = json.loads(out.split("RESULT ", 1)[1]) if "RESULT " in out else {"ok": False}
                        res["ok"] = res.get("ok", False) and json.loads(f.readline())["ok"]
                        rows.append({"protocol": "kcp-fast128", "size": size, "rep": rep, **res})
                print(f"size {size} rep {rep + 1}/{reps} done", flush=True)
    f.write(b'{"op": "bye"}\n')
    f.flush()

    out = ROOT / "results" / f"field_test_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"server": "<redacted>", "sizes": sizes, "reps": reps, "rows": rows}, indent=1))
    print(f"\nwrote {out}\n")
    print(f"{'protocol':16s} {'size':>8s} {'ok':>5s} {'median s':>9s} {'min s':>7s} {'max s':>7s}")
    for size in sizes:
        for proto in sorted({r["protocol"] for r in rows}):
            rs = [r for r in rows if r["protocol"] == proto and r["size"] == size]
            okr = [r["elapsed_s"] for r in rs if r["ok"]]
            if not rs:
                continue
            print(f"{proto:16s} {size:8d} {len(okr):>2d}/{len(rs):<2d} "
                  + (f"{statistics.median(okr):9.3f} {min(okr):7.3f} {max(okr):7.3f}" if okr else "     FAIL"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="role", required=True)
    s = sub.add_parser("server")
    s.add_argument("--bind", default="0.0.0.0")
    s.add_argument("--port", type=int, default=9500)
    s.add_argument("--psk", default=None, help="optional NLTP pre-shared key (same on both sides)")
    c = sub.add_parser("client")
    c.add_argument("--server", required=True)
    c.add_argument("--port", type=int, default=9500)
    c.add_argument("--sizes", type=int, nargs="+", default=[65536, 1048576])
    c.add_argument("--reps", type=int, default=5)
    c.add_argument("--psk", default=None, help="optional NLTP pre-shared key (same on both sides)")
    a = ap.parse_args()
    if a.role == "server":
        run_server(a.bind, a.port, a.psk)
    else:
        run_client(a.server, a.port, a.sizes, a.reps, a.psk)


if __name__ == "__main__":
    main()
