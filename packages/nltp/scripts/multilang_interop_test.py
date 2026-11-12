"""
4-Language NLTP v1.6 Live Wire, Encryption & LZ4 Interoperability Suite:
Python <-> ANSI C99 <-> JavaScript (Node.js ESM) <-> Java (JDK 11+ NIO).

Verifies:
  1. All 16 directional sender->receiver pairings across {Python, C99, JavaScript, Java}
     at {0%, 15%} bidirectional packet loss, {1000 B (k=1), 65536 B} payload sizes,
     and 3 cryptographic modes (plaintext, 44-char Base64 256-bit key, passphrase) = 192 transfers.
  2. 8 heterogeneous 256 KB encrypted cross-language transfers under 15% packet loss.
  3. 6 cross-language Zero-Knowledge NLTPRelayHub CGNAT traversals (Python, JS, and Java hubs)
     under 10% packet loss.
  4. 8 cross-language out-of-line `send_reflex` urgent frame deliveries (py, c, js, java).
  5. 14 cross-language reliable-reflex runs at 15% loss each way: 5 reflexes per pair, each must be
     delivered exactly once and acknowledged (reliable reflex lane).
  6. 16 full 4x4 encrypted X25519 + LZ4 block compression transfers @ 15% loss + 4 incompressible
     auto-bypass transfers @ 10% loss = 20 LZ4 wire-contract checks.

Total: 248 live cross-language protocol verifications.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from necklink.internet import NLTPRelayHub, generate_nltp_key  # noqa: E402
from necklink.nltp import NLTPClient, NLTPServer  # noqa: E402

NODE_BIN = os.environ.get("NODE_BIN") or shutil.which("node") or "node"
JS_CLI = str(ROOT / "js" / "bin" / "nltp.mjs")

JAVA_BIN = os.environ.get("JAVA_BIN") or shutil.which("java") or "java"
# Newest JAR built by `mvn -f java/pom.xml package` (java/target/nltp-<version>.jar); override with NLTP_JAVA_JAR
_JARS = sorted((ROOT / "java" / "target").glob("nltp-*.jar"), key=lambda p: p.stat().st_mtime)
_DEFAULT_JAR = _JARS[-1] if _JARS else ROOT / "java" / "target" / "nltp.jar"
JAVA_JAR = os.environ.get("NLTP_JAVA_JAR") or str(_DEFAULT_JAR)


def _setup_c_runtime() -> Tuple[List[str], str, str, bool]:
    """
    Returns `(c_cmd_prefix, c_listen_target_ip, host_listen_target_ip_from_c, use_wsl_paths)`.
    On Linux/macOS uses native `csrc/build/nltp_cli` over `127.0.0.1`.
    On Windows uses native `.exe` if provided, otherwise invokes WSL `csrc/build/nltp_cli`.
    """
    env_cli = os.environ.get("NLTP_CLI")
    if env_cli and Path(env_cli).exists() and (os.name != "nt" or env_cli.lower().endswith(".exe")):
        return ([env_cli], "127.0.0.1", "127.0.0.1", False)

    local_cli = ROOT / "csrc" / "build" / ("nltp_cli.exe" if os.name == "nt" else "nltp_cli")
    if os.name != "nt":
        if not local_cli.exists():
            alt = Path("/opt/kcpbench/bin/nltp_cli")
            if alt.exists():
                return ([str(alt)], "127.0.0.1", "127.0.0.1", False)
            subprocess.check_call(["make", "-C", str(ROOT / "csrc"), "all"])
        return ([str(local_cli)], "127.0.0.1", "127.0.0.1", False)

    if local_cli.exists():
        return ([str(local_cli)], "127.0.0.1", "127.0.0.1", False)

    # Windows + WSL bridge for POSIX `nltp_cli`
    wsl_root = _win_to_wsl_path(ROOT)
    wsl_cli = f"{wsl_root}/csrc/build/nltp_cli"
    subprocess.check_call([
        "wsl.exe", "bash", "-lc",
        f"mkdir -p '{wsl_root}/csrc/build' && "
        f"gcc -O2 -std=c99 -Wall -Wextra -Werror -I '{wsl_root}/csrc' "
        f"-o '{wsl_cli}' '{wsl_root}/csrc/tools/nltp_cli.c' "
        f"'{wsl_root}/csrc/nltp_core.c' '{wsl_root}/csrc/nltp_net.c' '{wsl_root}/csrc/nltp_sha256.c'"
    ])
    wsl_ip = subprocess.check_output(["wsl.exe", "hostname", "-I"], text=True).strip().split()[0]
    route_out = subprocess.check_output(["wsl.exe", "ip", "route", "show", "default"], text=True).strip().split()
    win_ip = route_out[2]
    return (["wsl.exe", wsl_cli], wsl_ip, win_ip, True)


def _win_to_wsl_path(p: Path) -> str:
    s = str(p.resolve()).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        return f"/mnt/{s[0].lower()}{s[2:]}"
    return s


C_PREFIX, C_TARGET_IP, HOST_IP_FROM_C, C_USES_WSL = _setup_c_runtime()


def _path_for(lang: str, p: Path) -> str:
    return _win_to_wsl_path(p) if (lang == "c" and C_USES_WSL) else str(p)



def _next_free_udp_port(candidate: int) -> int:
    """Return >= candidate that can currently bind UDP on both 0.0.0.0 and 127.0.0.1."""
    p = candidate
    for _ in range(500):
        ok = True
        for host in ("0.0.0.0", "127.0.0.1"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind((host, p))
            except OSError:
                ok = False
                break
            finally:
                sock.close()
        if ok:
            return p
        p += 1
    return p


def _parse_result(proc: subprocess.Popen, timeout: float = 90.0) -> Dict:
    if proc.stdin is not None and proc.stdin.closed:
        proc.stdin = None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return {"ok": False, "error": "timeout", "stderr": err[-300:] if err else ""}
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("RESULT "):
            try:
                return json.loads(line[7:])
            except json.JSONDecodeError:
                pass
    return {"ok": False, "stdout": out[-300:], "stderr": err[-300:] if err else ""}


def _wait_line_prefix(proc: subprocess.Popen, prefixes: Tuple[str, ...], timeout: float = 15.0) -> str:
    deadline = time.perf_counter() + timeout
    assert proc.stdout is not None
    while time.perf_counter() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.01)
            continue
        s = line.strip()
        if any(s.startswith(px) for px in prefixes):
            return s
    detail = ""
    if proc.poll() is not None:
        err = proc.stderr.read() if proc.stderr is not None else ""
        lines = [l for l in (err or "").splitlines() if "Picked up JAVA_TOOL_OPTIONS" not in l]
        detail = f" (exited with code {proc.returncode}; stderr: {' | '.join(lines)[-600:]})"
    raise RuntimeError(f"Subprocess did not emit {prefixes} within {timeout}s{detail}")


def _cli_cmd(lang: str, mode: str, host: str, port: int, file_arg: str, loss_pct: int,
             psk: Optional[str], relay_spec: Optional[str] = None, room: Optional[str] = None,
             compress: bool = False) -> List[str]:
    extra: List[str] = ["--loss", str(loss_pct)]
    if psk:
        extra += ["--psk", psk]
    if relay_spec and room:
        extra += ["--relay", relay_spec, "--room", room]
    if compress and mode == "send":
        extra.append("--compress")

    if lang == "c":
        return [*C_PREFIX, mode, host, str(port), file_arg, *extra]
    if lang == "js":
        return [NODE_BIN, JS_CLI, mode, host, str(port), file_arg, *extra]
    if lang == "java":
        return [JAVA_BIN, "-jar", JAVA_JAR, mode, host, str(port), file_arg, *extra]
    raise ValueError(f"Unsupported CLI lang: {lang}")


def run_pair(
    sender: str,
    receiver: str,
    src_path: Path,
    rx_dir: Path,
    loss_pct: int,
    port: int,
    psk: Optional[str] = None,
    relay_ports: Optional[Tuple[int, str]] = None,
    compress: bool = False,
) -> Dict:
    """
    Runs a single live UDP transfer from `sender` to `receiver`.
    If `relay_ports` is `(hub_port, room_name)`, routes both endpoints through the relay hub.
    """
    loss = loss_pct / 100.0
    out_file = rx_dir / f"rx_{sender}_{receiver}_{port}.bin"
    if out_file.exists():
        out_file.unlink()

    # Determine bind IP for receiver and target IP for sender
    if C_USES_WSL and (sender == "c") != (receiver == "c"):
        bind_ip = "0.0.0.0"
        target_ip = C_TARGET_IP if receiver == "c" else HOST_IP_FROM_C
    else:
        bind_ip = "0.0.0.0" if relay_ports else "127.0.0.1"
        target_ip = "127.0.0.1"

    def relay_spec_for(lang: str) -> Optional[str]:
        if not relay_ports:
            return None
        r_port, _ = relay_ports
        r_host = HOST_IP_FROM_C if (lang == "c" and C_USES_WSL) else "127.0.0.1"
        return f"{r_host}:{r_port}"

    room_name = relay_ports[1] if relay_ports else None
    expected_bytes = src_path.read_bytes()

    if receiver == "py":
        r_spec = relay_spec_for("py")
        r_addr = ("127.0.0.1", relay_ports[0]) if relay_ports else None
        with NLTPServer(
            (bind_ip, 0 if relay_ports else port),
            save_dir=str(rx_dir),
            loss_drop_sim=loss,
            psk=psk,
            relay_addr=r_addr,
            room=room_name,
        ) as srv:
            if relay_ports:
                time.sleep(0.08)
            t0 = time.perf_counter()
            if sender == "py":
                s_relay = ("127.0.0.1", relay_ports[0]) if relay_ports else None
                with NLTPClient(
                    (target_ip, 9 if relay_ports else port),
                    loss_drop_sim=loss,
                    psk=psk,
                    relay_addr=s_relay,
                    room=room_name,
                ) as cli:
                    if relay_ports:
                        time.sleep(0.05)
                    rep = cli.send_file(src_path, timeout=90.0, compress=compress)
                tx_ok = bool(rep["verified_sha256"])
                tx_compressed = bool(rep.get("compressed", False))
                tx_wire_payload = int(rep.get("wire_payload_bytes", len(expected_bytes)))
                elapsed_s = time.perf_counter() - t0
            else:
                cmd = _cli_cmd(
                    sender, "send", target_ip, 9 if relay_ports else port,
                    _path_for(sender, src_path), loss_pct, psk,
                    relay_spec=relay_spec_for(sender), room=room_name,
                    compress=compress,
                )
                tx_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                tx_res = _parse_result(tx_proc, timeout=90.0)
                tx_ok = bool(tx_res.get("ok"))
                tx_compressed = bool(tx_res.get("compressed", False))
                tx_wire_payload = int(tx_res.get("wire_payload_bytes", len(expected_bytes)))
                elapsed_s = float(tx_res.get("elapsed_s", time.perf_counter() - t0))

            item = srv.recv(timeout=10.0)
            rx_ok = item is not None and item.sha256_verified and item.data == expected_bytes
    else:
        rx_cmd = _cli_cmd(
            receiver, "recv", bind_ip, 0 if relay_ports else port,
            _path_for(receiver, out_file), loss_pct, psk,
            relay_spec=relay_spec_for(receiver), room=room_name,
        )
        use_wait_stdin = receiver in ("js", "java")
        if use_wait_stdin:
            rx_cmd.append("--wait-stdin")
        rx_proc = subprocess.Popen(
            rx_cmd,
            stdin=subprocess.PIPE if use_wait_stdin else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_line_prefix(rx_proc, ("READY", "RELAY registered"), timeout=20.0)

            t0 = time.perf_counter()
            if sender == "py":
                s_relay = ("127.0.0.1", relay_ports[0]) if relay_ports else None
                with NLTPClient(
                    (target_ip, 9 if relay_ports else port),
                    loss_drop_sim=loss,
                    psk=psk,
                    relay_addr=s_relay,
                    room=room_name,
                ) as cli:
                    if relay_ports:
                        time.sleep(0.05)
                    rep = cli.send_file(src_path, timeout=90.0, compress=compress)
                tx_ok = bool(rep["verified_sha256"])
                tx_compressed = bool(rep.get("compressed", False))
                tx_wire_payload = int(rep.get("wire_payload_bytes", len(expected_bytes)))
                elapsed_s = time.perf_counter() - t0
            else:
                cmd = _cli_cmd(
                    sender, "send", target_ip, 9 if relay_ports else port,
                    _path_for(sender, src_path), loss_pct, psk,
                    relay_spec=relay_spec_for(sender), room=room_name,
                    compress=compress,
                )
                tx_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                tx_res = _parse_result(tx_proc, timeout=90.0)
                tx_ok = bool(tx_res.get("ok"))
                tx_compressed = bool(tx_res.get("compressed", False))
                tx_wire_payload = int(tx_res.get("wire_payload_bytes", len(expected_bytes)))
                elapsed_s = float(tx_res.get("elapsed_s", time.perf_counter() - t0))

            if use_wait_stdin and rx_proc.stdin is not None:
                try:
                    rx_proc.stdin.close()
                except OSError:
                    pass
                finally:
                    rx_proc.stdin = None
            rx_res = _parse_result(rx_proc, timeout=30.0)
            rx_ok = bool(rx_res.get("ok")) and out_file.exists() and out_file.read_bytes() == expected_bytes
        finally:
            if rx_proc.poll() is None:
                rx_proc.kill()
                rx_proc.communicate()

    if out_file.exists():
        out_file.unlink()

    key_mode = "plain" if not psk else ("b64-key" if len(psk) == 44 else "passphrase")
    return {
        "category": "lz4" if compress else ("relay" if relay_ports else "direct"),
        "sender": sender,
        "receiver": receiver,
        "loss_pct": loss_pct,
        "size": len(expected_bytes),
        "wire_payload_bytes": tx_wire_payload,
        "compressed": tx_compressed,
        "key_mode": key_mode,
        "ok": bool(tx_ok and rx_ok),
        "elapsed_s": round(elapsed_s, 4),
    }


def _cli_base(lang: str) -> List[str]:
    if lang == "js":
        return [NODE_BIN, JS_CLI]
    if lang == "java":
        return [JAVA_BIN, "-jar", JAVA_JAR]
    if lang == "c":
        return list(C_PREFIX)
    raise ValueError(f"Unsupported CLI lang: {lang}")


def run_reflex_pair(sender: str, receiver: str, port: int, psk: str) -> Dict:
    msg = f"ESTOP_{sender.upper()}_TO_{receiver.upper()}"
    t0 = time.perf_counter()
    if receiver == "py":
        bind_ip = "0.0.0.0" if (sender == "c" and C_USES_WSL) else "127.0.0.1"
        dst_ip = HOST_IP_FROM_C if (sender == "c" and C_USES_WSL) else "127.0.0.1"
        with NLTPServer((bind_ip, port), psk=psk) as srv:
            cmd = [*_cli_base(sender), "send-reflex", dst_ip, str(port), msg, "--psk", psk]
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL)
            item = srv.recv(timeout=5.0)
            ok = item is not None and item.kind == "reflex" and item.data.decode("utf-8") == msg
    else:
        bind_ip = "0.0.0.0" if (receiver == "c" and C_USES_WSL) else "127.0.0.1"
        dst_ip = C_TARGET_IP if (receiver == "c" and C_USES_WSL) else "127.0.0.1"
        rx_cmd = [*_cli_base(receiver), "serve", "--host", bind_ip, "--port", str(port), "--psk", psk, "--max-items", "1"]
        rx_proc = subprocess.Popen(rx_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            _wait_line_prefix(rx_proc, ("NLTP ",), timeout=15.0)
            if sender == "py":
                with NLTPClient((dst_ip, port), psk=psk) as cli:
                    cli.wait_reflex_ack(cli.send_reflex(msg), timeout=5.0)   # the documented way to confirm delivery
            else:
                tx_dst = (
                    HOST_IP_FROM_C if (sender == "c" and receiver != "c" and C_USES_WSL)
                    else (C_TARGET_IP if (receiver == "c" and sender != "c" and C_USES_WSL) else "127.0.0.1")
                )
                subprocess.check_call([*_cli_base(sender), "send-reflex", tx_dst, str(port), msg, "--psk", psk],
                                      stdout=subprocess.DEVNULL)
            out, _ = rx_proc.communicate(timeout=10.0)
        finally:
            if rx_proc.poll() is None:
                rx_proc.kill()
                rx_proc.communicate()
        ok = f"RECEIVED kind=reflex bytes={len(msg)} sha256_ok=True" in out or f"RECEIVED kind=reflex bytes={len(msg)} sha256_ok=true" in out

    return {
        "category": "reflex",
        "sender": sender,
        "receiver": receiver,
        "loss_pct": 0,
        "size": len(msg),
        "key_mode": "b64-key",
        "ok": bool(ok),
        "elapsed_s": round(time.perf_counter() - t0, 4),
    }


def run_reflex_loss_pair(sender: str, receiver: str, port: int, psk: str, n: int = 5, loss: float = 0.15) -> Dict:
    """Reliable reflex lane under loss: `n` reflexes, each delivered exactly once and acknowledged."""
    msgs = [f"ESTOP_{sender.upper()}_{receiver.upper()}_{i}" for i in range(n)]
    t0 = time.perf_counter()
    acks = 0

    def cli_send(lang: str, msg: str, dst_ip: str) -> bool:
        base = _cli_base(lang)
        proc = subprocess.run(
            [*base, "send-reflex", dst_ip, str(port), msg, "--psk", psk, "--loss", str(loss)],
            capture_output=True, text=True, timeout=30,
        )
        return '"acked":true' in proc.stdout.replace(" ", "")

    if receiver == "py":
        bind_ip = "0.0.0.0" if (sender == "c" and C_USES_WSL) else "127.0.0.1"
        dst_ip = HOST_IP_FROM_C if (sender == "c" and C_USES_WSL) else "127.0.0.1"
        with NLTPServer((bind_ip, port), psk=psk, loss_drop_sim=loss) as srv:
            for m in msgs:
                acks += cli_send(sender, m, dst_ip)
            got = []
            while True:
                item = srv.recv(timeout=1.0)
                if item is None:
                    break
                if item.kind == "reflex":
                    got.append(item.data.decode("utf-8"))
        ok = sorted(got) == sorted(msgs) and acks == n
    else:
        bind_ip = "0.0.0.0" if (receiver == "c" and C_USES_WSL) else "127.0.0.1"
        dst_ip = (
            HOST_IP_FROM_C if (sender == "c" and receiver != "c" and C_USES_WSL)
            else (C_TARGET_IP if (receiver == "c" and sender != "c" and C_USES_WSL) else "127.0.0.1")
        )
        base = _cli_base(receiver)
        rx_proc = subprocess.Popen(
            [*base, "serve", "--host", bind_ip, "--port", str(port), "--psk", psk,
             "--max-items", str(n), "--loss", str(loss)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            _wait_line_prefix(rx_proc, ("NLTP ",), timeout=15.0)
            if sender == "py":
                py_dst = C_TARGET_IP if (receiver == "c" and C_USES_WSL) else "127.0.0.1"
                with NLTPClient((py_dst, port), psk=psk, loss_drop_sim=loss) as cli:
                    for m in msgs:
                        acks += cli.wait_reflex_ack(cli.send_reflex(m), timeout=5.0)
            else:
                for m in msgs:
                    acks += cli_send(sender, m, dst_ip)
            out, _ = rx_proc.communicate(timeout=25.0)
        except subprocess.TimeoutExpired:
            rx_proc.kill()
            out, _ = rx_proc.communicate()
        finally:
            if rx_proc.poll() is None:
                rx_proc.kill()
                rx_proc.communicate()
        ok = out.count("RECEIVED kind=reflex") == n and acks == n

    return {
        "category": "reflex-loss",
        "sender": sender,
        "receiver": receiver,
        "loss_pct": int(loss * 100),
        "size": n,
        "key_mode": "b64-key",
        "acked": acks,
        "ok": bool(ok),
        "elapsed_s": round(time.perf_counter() - t0, 4),
    }


def main() -> None:
    work_dir = ROOT / "results" / "_tmp_multilang_interop"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    langs = ("py", "c", "js", "java")
    rows: List[Dict] = []
    port = 21100

    try:
        # ------------------------------------------------------------------
        # 1. Full 4x4 Matrix: 16 pairs x 3 key modes x 2 sizes x 2 losses = 192
        # ------------------------------------------------------------------
        print("=== Part 1/6: Full 4x4 Language Matrix (py, c, js, java) — 192 Live Transfers ===", flush=True)
        files = {}
        for sz in (1000, 65536, 262144):
            fp = work_dir / f"payload_{sz}.bin"
            fp.write_bytes(os.urandom(sz))
            files[sz] = fp

        b64_key = generate_nltp_key()
        for psk in (None, b64_key, "boat radio passphrase"):
            for sz in (1000, 65536):
                for loss in (0, 15):
                    for s in langs:
                        for r in langs:
                            port = _next_free_udp_port(port + 1)
                            res = run_pair(s, r, files[sz], work_dir, loss, port, psk=psk)
                            rows.append(res)
                            status = "OK  " if res["ok"] else "FAIL"
                            print(
                                f"  [{len(rows):>3}/248] {res['key_mode']:>10}  {s:>4} -> {r:<4}  "
                                f"{sz:>7} B  loss {loss:>2}%  {status}  {res['elapsed_s']:.3f}s",
                                flush=True,
                            )
                            if not res["ok"]:
                                raise RuntimeError(f"Interop failure: {res}")

        # ------------------------------------------------------------------
        # 2. 256 KB Encrypted Lossy Stress (15% loss) across 8 cross-lang pairs
        # ------------------------------------------------------------------
        print("\n=== Part 2/6: 256 KB Encrypted Multi-Window Stress @ 15% Loss (8 Cross-Language Pairs) ===", flush=True)
        stress_pairs = [
            ("py", "js"), ("js", "java"), ("java", "c"), ("c", "py"),
            ("py", "java"), ("java", "js"), ("js", "c"), ("c", "java"),
        ]
        for s, r in stress_pairs:
            port = _next_free_udp_port(port + 1)
            res = run_pair(s, r, files[262144], work_dir, 15, port, psk=b64_key)
            res["category"] = "stress_256k"
            rows.append(res)
            print(
                f"  [{len(rows):>3}/248]   256KB-enc  {s:>4} -> {r:<4}  "
                f"262144 B  loss 15%  {'OK  ' if res['ok'] else 'FAIL'}  {res['elapsed_s']:.3f}s",
                flush=True,
            )
            if not res["ok"]:
                raise RuntimeError(f"256 KB stress failure: {res}")

        # ------------------------------------------------------------------
        # 3. Cross-Language Zero-Knowledge NLTPRelayHub CGNAT Traversal (10% loss)
        # ------------------------------------------------------------------
        print("\n=== Part 3/6: Cross-Language Zero-Knowledge NLTPRelayHub @ 10% Loss (Python / JS / Java Hubs) ===", flush=True)
        # 3a. Python NLTPRelayHub
        with NLTPRelayHub(("0.0.0.0", 0)) as py_hub:
            hub_port = py_hub.getsockname()[1]
            for s, r in (("js", "java"), ("c", "js")):
                port = _next_free_udp_port(port + 1)
                res = run_pair(
                    s, r, files[65536], work_dir, 10, port,
                    psk=b64_key, relay_ports=(hub_port, f"room-{port}"),
                )
                res["relay_hub"] = "py"
                rows.append(res)
                print(f"  [{len(rows):>3}/248]  py-relay   {s:>4} -> {r:<4}   65536 B  loss 10%  {'OK  ' if res['ok'] else 'FAIL'}  {res['elapsed_s']:.3f}s", flush=True)
                if not res["ok"]:
                    raise RuntimeError(f"Python RelayHub failure: {res}")

        # 3b. JavaScript NLTPRelayHub
        port = _next_free_udp_port(port + 1)
        js_hub_port = port
        js_hub = subprocess.Popen(
            [NODE_BIN, JS_CLI, "relay", "--host", "0.0.0.0", "--port", str(js_hub_port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            _wait_line_prefix(js_hub, ("NLTPRelayHub listening",), timeout=10.0)
            for s, r in (("java", "py"), ("py", "java")):
                port = _next_free_udp_port(port + 1)
                res = run_pair(
                    s, r, files[65536], work_dir, 10, port,
                    psk=b64_key, relay_ports=(js_hub_port, f"room-{port}"),
                )
                res["relay_hub"] = "js"
                rows.append(res)
                print(f"  [{len(rows):>3}/248]  js-relay   {s:>4} -> {r:<4}   65536 B  loss 10%  {'OK  ' if res['ok'] else 'FAIL'}  {res['elapsed_s']:.3f}s", flush=True)
                if not res["ok"]:
                    raise RuntimeError(f"JS RelayHub failure: {res}")
        finally:
            js_hub.kill()
            js_hub.communicate()

        # 3c. Java NLTPRelayHub
        port = _next_free_udp_port(port + 1)
        java_hub_port = port
        java_hub = subprocess.Popen(
            [JAVA_BIN, "-jar", JAVA_JAR, "relay", "--host", "0.0.0.0", "--port", str(java_hub_port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            _wait_line_prefix(java_hub, ("NLTPRelayHub listening",), timeout=15.0)
            for s, r in (("py", "js"), ("js", "c")):
                port = _next_free_udp_port(port + 1)
                res = run_pair(
                    s, r, files[65536], work_dir, 10, port,
                    psk=b64_key, relay_ports=(java_hub_port, f"room-{port}"),
                )
                res["relay_hub"] = "java"
                rows.append(res)
                print(f"  [{len(rows):>3}/248] java-relay  {s:>4} -> {r:<4}   65536 B  loss 10%  {'OK  ' if res['ok'] else 'FAIL'}  {res['elapsed_s']:.3f}s", flush=True)
                if not res["ok"]:
                    raise RuntimeError(f"Java RelayHub failure: {res}")
        finally:
            java_hub.kill()
            java_hub.communicate()

        # ------------------------------------------------------------------
        # 4. Cross-Language Out-of-Line Reflex Messages (All 4 Languages)
        # ------------------------------------------------------------------
        print("\n=== Part 4/6: Cross-Language Out-of-Line Reflex Verification (py, c, js, java) ===", flush=True)
        reflex_pairs = (("py", "c"), ("c", "py"), ("py", "js"), ("py", "java"), ("js", "py"), ("java", "py"), ("c", "js"), ("java", "c"))
        for s, r in reflex_pairs:
            port = _next_free_udp_port(port + 1)
            res = run_reflex_pair(s, r, port, b64_key)
            rows.append(res)
            print(f"  [{len(rows):>3}/248]    reflex   {s:>4} -> {r:<4}      {res['size']:>2} B  loss  0%  {'OK  ' if res['ok'] else 'FAIL'}  {res['elapsed_s']:.3f}s", flush=True)
            if not res["ok"]:
                raise RuntimeError(f"Reflex interop failure: {res}")

        print("\n=== Part 5/6: Reliable Reflex Lane @ 15% Loss (5 reflexes per pair, delivered once + acked, all 4 langs) ===", flush=True)
        reflex_loss_pairs = (
            ("py", "c"), ("c", "py"), ("py", "js"), ("py", "java"), ("js", "py"),
            ("java", "py"), ("js", "java"), ("java", "js"), ("c", "js"), ("js", "c"),
            ("c", "java"), ("java", "c"), ("c", "c"), ("js", "js"),
        )
        for s, r in reflex_loss_pairs:
            port = _next_free_udp_port(port + 1)
            res = run_reflex_loss_pair(s, r, port, b64_key)
            rows.append(res)
            print(f"  [{len(rows):>3}/248] reflex x5  {s:>4} -> {r:<4}  acked {res['acked']}/5  loss 15%  {'OK  ' if res['ok'] else 'FAIL'}  {res['elapsed_s']:.3f}s", flush=True)
            if not res["ok"]:
                raise RuntimeError(f"Reliable reflex interop failure: {res}")

        # ------------------------------------------------------------------
        # 6. Full 4x4 Encrypted LZ4 Block Codec Matrix @ 15% Loss + Auto-Bypass
        # ------------------------------------------------------------------
        print("\n=== Part 6/6: Full 4x4 Encrypted LZ4 Compression Matrix @ 15% Loss + Auto-Bypass (20 Live Transfers) ===", flush=True)
        jsonl_lines = [
            f'{{"station":"AQMS-RUT200-04","seq":{i},"pm25":42.5,"pm10":89.1,"so2":12.3,"no2":24.8,"o3":31.2,"co":0.82}}\n'
            for i in range(900)
        ]
        jsonl_file = work_dir / "telemetry_aqms.jsonl"
        jsonl_bytes = "".join(jsonl_lines).encode("utf-8")
        jsonl_file.write_bytes(jsonl_bytes)

        for s in langs:
            for r in langs:
                port = _next_free_udp_port(port + 1)
                res = run_pair(s, r, jsonl_file, work_dir, 15, port, psk=b64_key, compress=True)
                lz4_ok = res["ok"] and res["compressed"] and (res["wire_payload_bytes"] * 3 < len(jsonl_bytes))
                res["ok"] = bool(lz4_ok)
                rows.append(res)
                ratio = len(jsonl_bytes) / max(1, res["wire_payload_bytes"])
                print(
                    f"  [{len(rows):>3}/248]    lz4-enc  {s:>4} -> {r:<4}  "
                    f"{len(jsonl_bytes):>6}->{res['wire_payload_bytes']:<5}B ({ratio:.1f}x)  loss 15%  "
                    f"{'OK  ' if res['ok'] else 'FAIL'}  {res['elapsed_s']:.3f}s",
                    flush=True,
                )
                if not res["ok"]:
                    raise RuntimeError(f"LZ4 interop failure: {res}")

        for s, r in (("py", "c"), ("c", "js"), ("js", "java"), ("java", "py")):
            port = _next_free_udp_port(port + 1)
            res = run_pair(s, r, files[65536], work_dir, 10, port, psk=b64_key, compress=True)
            res["category"] = "lz4-bypass"
            bypass_ok = res["ok"] and (not res["compressed"]) and (res["wire_payload_bytes"] == 65536)
            res["ok"] = bool(bypass_ok)
            rows.append(res)
            print(
                f"  [{len(rows):>3}/248] lz4-bypass  {s:>4} -> {r:<4}  "
                f" 65536->65536B (1.0x)  loss 10%  {'OK  ' if res['ok'] else 'FAIL'}  {res['elapsed_s']:.3f}s",
                flush=True,
            )
            if not res["ok"]:
                raise RuntimeError(f"LZ4 auto-bypass interop failure: {res}")

    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

    n_ok = sum(1 for r in rows if r["ok"])
    summary = {
        "total_tests": len(rows),
        "passed": n_ok,
        "languages": list(langs),
        "all_passed": n_ok == len(rows),
        "results": rows,
    }
    out_json = ROOT / "results" / "multilang_interop_test.json"
    out_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nVERIFIED {n_ok}/{len(rows)} 4-language NLTP v1.6 pairings -> {out_json.name}")
    sys.exit(0 if n_ok == len(rows) else 1)


if __name__ == "__main__":
    main()
