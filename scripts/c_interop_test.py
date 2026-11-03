"""
C <-> Python NLTP interoperability test (Linux/WSL, loopback).

Runs every sender/receiver pairing (C->C, C->Python, Python->C, Python->Python) at two loss rates, three
sizes, and three key modes (no encryption, a generate_nltp_key() key, a passphrase); success means the
receiver verified the file's SHA-256. The C side is /opt/kcpbench/bin/nltp_cli
(build: see csrc/tools/nltp_cli.c); its --loss drops packets in both directions, the Python side uses
NLTPServer/NLTPClient(loss_drop_sim=...).

    /opt/nlb/bin/python scripts/c_interop_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from necklink.internet import generate_nltp_key  # noqa: E402
from necklink.nltp import NLTPClient, NLTPServer  # noqa: E402

_DEFAULT_CLI = ROOT / "csrc" / "build" / "nltp_cli"
CLI = os.environ.get("NLTP_CLI") or (str(_DEFAULT_CLI) if _DEFAULT_CLI.exists() else "/opt/kcpbench/bin/nltp_cli")
HOST = "127.0.0.1"


def _result(proc: subprocess.Popen, timeout: float) -> dict:
    out, _ = proc.communicate(timeout=timeout)
    for line in out.splitlines()[::-1]:
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    return {"ok": False}


def run_pair(sender: str, receiver: str, path: Path, loss_pct: int, port: int, psk: str | None = None) -> dict:
    loss = loss_pct / 100.0
    c_psk = ["--psk", psk] if psk else []
    t0 = time.perf_counter()
    if receiver == "c":
        rx = subprocess.Popen([CLI, "recv", HOST, str(port), str(path) + ".rx", "--loss", str(loss_pct), *c_psk],
                              stdout=subprocess.PIPE, text=True)
        assert rx.stdout.readline().strip() == "READY"
        if sender == "c":
            tx = _result(subprocess.Popen([CLI, "send", HOST, str(port), str(path), "--loss", str(loss_pct), *c_psk],
                                          stdout=subprocess.PIPE, text=True), 120)
        else:
            with NLTPClient((HOST, port), loss_drop_sim=loss, psk=psk) as cli:
                r = cli.send_file(path, timeout=90)
            tx = {"ok": r["verified_sha256"], "elapsed_s": time.perf_counter() - t0}
        rr = _result(rx, 30)
        ok = bool(tx.get("ok")) and bool(rr.get("ok"))
        if ok:
            ok = Path(str(path) + ".rx").read_bytes() == path.read_bytes()
    else:
        with NLTPServer((HOST, port), save_dir=tempfile.mkdtemp(), loss_drop_sim=loss, psk=psk) as srv:
            if sender == "c":
                tx = _result(subprocess.Popen([CLI, "send", HOST, str(port), str(path), "--loss", str(loss_pct), *c_psk],
                                              stdout=subprocess.PIPE, text=True), 120)
            else:
                with NLTPClient((HOST, port), loss_drop_sim=loss, psk=psk) as cli:
                    r = cli.send_file(path, timeout=90)
                tx = {"ok": r["verified_sha256"], "elapsed_s": time.perf_counter() - t0}
            item = srv.recv(timeout=5)
        ok = bool(tx.get("ok")) and item is not None and item.sha256_verified and item.data == path.read_bytes()
    return {"sender": sender, "receiver": receiver, "loss_pct": loss_pct, "size": path.stat().st_size,
            "key_mode": "plain" if not psk else ("b64-key" if len(psk) == 44 else "passphrase"),
            "ok": ok, "elapsed_s": round(float(tx.get("elapsed_s", 0.0)), 3)}


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    files = []
    for size in (65536, 1048576, 1000):  # 1000 B = single short generation (k == 1 path)
        p = tmp / f"interop_{size}.bin"
        p.write_bytes(os.urandom(size))
        files.append(p)
    port = 9810
    rows = []
    for psk in (None, generate_nltp_key(), "boat radio passphrase"):
        for path in files:
            for loss in (0, 15):
                for sender in ("c", "py"):
                    for receiver in ("c", "py"):
                        port += 1
                        r = run_pair(sender, receiver, path, loss, port, psk)
                        rows.append(r)
                        print(f"{r['key_mode']:>10}  {sender:>2} -> {receiver:<2}  {r['size']:>8} B  loss {loss:>2}%  "
                              f"{'OK  ' if r['ok'] else 'FAIL'}  {r['elapsed_s']:.3f} s", flush=True)
    n_ok = sum(r["ok"] for r in rows)
    print(f"\n{n_ok}/{len(rows)} pairings verified")
    out = ROOT / "results" / "c_interop_test.json"
    out.write_text(json.dumps(rows, indent=1))
    sys.exit(0 if n_ok == len(rows) else 1)


if __name__ == "__main__":
    main()
