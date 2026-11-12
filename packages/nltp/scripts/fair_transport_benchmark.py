"""
Fair transport benchmark: real Linux kernel TCP (CUBIC, BBR) vs QUIC (aioquic) vs NLTP,
over a netem-emulated link between two network namespaces (WSL2 or any Linux box, as root).

Unlike scripts/benchmark_nltp_vs_quic_vs_tcp.py, nothing here is a userspace re-implementation of
TCP: kernel TCP runs on real sockets, and every protocol crosses the same veth pair where netem
applies delay, packet loss (random or Gilbert-Elliott burst) and a bandwidth cap in BOTH directions.
TSO/GSO/GRO are disabled so netem drops real wire-sized packets. Bytes are counted with iptables
OUTPUT counters, i.e. everything each side transmitted, including packets netem later dropped.

Setup once:  wsl -u root -- bash scripts/wsl/setup_bench_env.sh
Run:         wsl -u root -- /opt/nlb/bin/python scripts/fair_transport_benchmark.py [--quick]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
PY = sys.executable
NS_A, NS_B = "nla", "nlb"  # client side, server side
IF_A, IF_B = "va", "vb"
IP_A, IP_B = "10.77.0.1", "10.77.0.2"
OUT_JSON = ROOT / "results" / "fair_transport_benchmark.json"
BLOB_DIR = Path("/tmp/nlb_blobs")

CONDITIONS = [
    # name, rate_mbit, rtt_ms, loss_pct, loss_mode
    ("clean_20M_20ms", 20, 20, 0.0, "random"),
    ("wifi_20M_40ms_2pct", 20, 40, 2.0, "random"),
    ("cell_10M_100ms_5pct", 10, 100, 5.0, "random"),
    ("radio_5M_200ms_10pct", 5, 200, 10.0, "random"),
    ("bad_2M_300ms_20pct", 2, 300, 20.0, "random"),
    ("burst_10M_100ms_10pct", 10, 100, 10.0, "burst"),
]
# --extended: harder link behaviours (jitter with reordering, explicit reordering, GEO satellite, asymmetric uplink)
EXTENDED_CONDITIONS = [
    ("jitter_10M_100ms_5pct_j20", 10, 100, 5.0, "random", {"jitter_ms": 20}),
    ("reorder_10M_60ms_2pct_r10", 10, 60, 2.0, "random", {"reorder_pct": 10}),
    ("geosat_2M_600ms_3pct", 2, 600, 3.0, "random", {}),
    ("asym_up1M_down10M_150ms_5pct", 10, 150, 5.0, "random", {"up_rate": 1}),
]
SIZES = [64 * 1024, 1024 * 1024]
NLTP_TEST_PSK = "nltp-benchmark-psk"
PROTOCOLS = ["tcp-cubic", "tcp-bbr", "quic-aioquic", "nltp-default", "nltp-ratecap"]
# Optional (need scripts/wsl/setup_kcp.sh): "kcp-fast", "kcp-fast128", "kcp-fast-cc", "kcptun"
# Optional (need scripts/wsl/setup_competitors.sh): "udt", "srt", "uftp", "quic-ngtcp2-bbr", "quic-ngtcp2-cubic"
KCP_XFER = "/opt/kcpbench/bin/kcp_xfer"
UDT_XFER = "/opt/kcpbench/bin/udt_xfer"
QUIC_CERT_DIR = Path("/opt/kcpbench/quic")
TRANSFER_TIMEOUT_S = 90.0


def sh(cmd: str, check: bool = True) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}\n{r.stderr}")
    return r.stdout


# ---------------------------------------------------------------------------
# Topology + link emulation
# ---------------------------------------------------------------------------

def setup_topology() -> None:
    teardown_topology()
    sh(f"ip netns add {NS_A} && ip netns add {NS_B}")
    sh(f"ip link add {IF_A} type veth peer name {IF_B}")
    sh(f"ip link set {IF_A} netns {NS_A} && ip link set {IF_B} netns {NS_B}")
    for ns, ifc, ip in ((NS_A, IF_A, IP_A), (NS_B, IF_B, IP_B)):
        sh(f"ip netns exec {ns} ip addr add {ip}/24 dev {ifc}")
        sh(f"ip netns exec {ns} ip link set {ifc} up && ip netns exec {ns} ip link set lo up")
        sh(f"ip netns exec {ns} ethtool -K {ifc} tso off gso off gro off", check=False)
        sh(f"ip netns exec {ns} sysctl -qw net.core.rmem_max=8388608 net.core.wmem_max=8388608", check=False)


def teardown_topology() -> None:
    sh(f"ip netns del {NS_A}", check=False)
    sh(f"ip netns del {NS_B}", check=False)


def gemodel_args(avg_loss_pct: float, burst_len: float = 8.0, p_bad: float = 0.92, p_good: float = 0.01) -> str:
    """netem Gilbert-Elliott params matching necklink.baselines.simulate_packet_drops calibration."""
    pi_b = min(0.95, max(0.01, (avg_loss_pct / 100.0 - p_good) / (p_bad - p_good)))
    r = 1.0 / burst_len
    p = pi_b * r / (1.0 - pi_b)
    return f"loss gemodel {100 * p:.4f}% {100 * r:.4f}% {100 * p_bad:.2f}% {100 * p_good:.2f}%"


def set_link(rate_mbit: float, rtt_ms: float, loss_pct: float, mode: str, extra: Optional[Dict[str, float]] = None) -> None:
    """extra: jitter_ms (normal-distributed delay variation), reorder_pct, up_rate (client->server Mbit/s)."""
    extra = extra or {}
    one_way = rtt_ms / 2.0
    jitter = f" {extra['jitter_ms']}ms distribution normal" if extra.get("jitter_ms") else ""
    reorder = f" reorder {extra['reorder_pct']}% 50%" if extra.get("reorder_pct") else ""
    if loss_pct <= 0:
        loss = ""
    elif mode == "burst":
        loss = gemodel_args(loss_pct)
    else:
        loss = f"loss random {loss_pct}%"
    for ns, ifc, rate in ((NS_A, IF_A, extra.get("up_rate", rate_mbit)), (NS_B, IF_B, rate_mbit)):
        # queue ~= 2x BDP in 1500 B packets (min 50): a sane router buffer, not bufferbloat
        bdp_pkts = rate * 1e6 / 8 * (rtt_ms / 1000.0) / 1500.0
        limit = max(50, int(2 * bdp_pkts))
        sh(f"ip netns exec {ns} tc qdisc replace dev {ifc} root netem delay {one_way}ms{jitter}{reorder} {loss} rate {rate}mbit limit {limit}")


def reset_counters() -> None:
    for ns, ifc in ((NS_A, IF_A), (NS_B, IF_B)):
        sh(f"ip netns exec {ns} iptables -F OUTPUT && ip netns exec {ns} iptables -Z && ip netns exec {ns} iptables -A OUTPUT -o {ifc}")


def read_counters() -> Dict[str, int]:
    out = {}
    for side, ns in (("client_tx", NS_A), ("server_tx", NS_B)):
        lines = sh(f"ip netns exec {ns} iptables -L OUTPUT -v -x -n").strip().splitlines()
        rule = lines[2].split()
        out[f"{side}_pkts"] = int(rule[0])
        out[f"{side}_bytes"] = int(rule[1])
    return out


# ---------------------------------------------------------------------------
# Endpoint roles (run inside a namespace as subprocesses)
# ---------------------------------------------------------------------------

def _emit(obj: Dict[str, Any]) -> None:
    print("RESULT " + json.dumps(obj), flush=True)


def role_tcp_server(port: int, size: int, sha: str) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((IP_B, port))
    srv.listen(1)
    print("READY", flush=True)
    srv.settimeout(TRANSFER_TIMEOUT_S + 10)
    conn, _ = srv.accept()
    h = hashlib.sha256()
    got = 0
    while got < size:
        b = conn.recv(min(65536, size - got))
        if not b:
            break
        h.update(b)
        got += len(b)
    ok = got == size and h.hexdigest() == sha
    conn.sendall(b"1" if ok else b"0")
    conn.close()
    _emit({"ok": ok})


def role_tcp_client(port: int, path: str, cc: str, host: str = IP_B) -> None:
    data = Path(path).read_bytes()
    t0 = time.perf_counter()
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, cc.encode())
    c.settimeout(TRANSFER_TIMEOUT_S)
    ok = False
    try:
        c.connect((host, port))
        c.sendall(data)
        ok = c.recv(1) == b"1"
    except OSError:
        ok = False
    _emit({"ok": ok, "elapsed_s": time.perf_counter() - t0})


def role_nltp_server(port: int, cap_kbps: Optional[float], n_items: int = 1, psk: Optional[str] = None) -> None:
    sys.path.insert(0, str(ROOT))
    from necklink.nltp import NLTPServer
    kw: Dict[str, Any] = {} if cap_kbps is None else {"max_tx_kbps": cap_kbps}
    if psk:
        kw["psk"] = psk
    ok = True
    with NLTPServer((IP_B, port), save_dir="/tmp/nlb_inbox", **kw) as srv:
        print("READY", flush=True)
        for _ in range(n_items):
            ok = ok and srv.recv(timeout=TRANSFER_TIMEOUT_S + 10) is not None
        time.sleep(1.5)  # keep answering the client's final acks
    _emit({"ok": ok})


def role_nltp_client(port: int, path: str, cap_kbps: Optional[float], burst: int = 0, warm: bool = False,
                     psk: Optional[str] = None, compress: bool = False) -> None:
    """`warm`: send the file once untimed on the same connection, then time a second transfer."""
    sys.path.insert(0, str(ROOT))
    from necklink.nltp import NLTPClient
    kw: Dict[str, Any] = {} if cap_kbps is None else {"max_tx_kbps": cap_kbps}
    if burst:
        kw["initial_burst_bytes"] = burst
    if psk:
        kw["psk"] = psk
    t0 = time.perf_counter()
    with NLTPClient((IP_B, port), **kw) as cli:
        if warm:
            cli.send_file(path, timeout=TRANSFER_TIMEOUT_S, compress=compress)
            time.sleep(0.2)
            t0 = time.perf_counter()
        rcpt = cli.send_file(path, timeout=TRANSFER_TIMEOUT_S, compress=compress)
    _emit({"ok": bool(rcpt["verified_sha256"]), "elapsed_s": time.perf_counter() - t0})


def role_quic_server(port: int) -> None:
    sys.path.insert(0, str(ROOT))
    from necklink.quic_adapter import NeckLinkQuicServer

    async def run() -> bool:
        async with NeckLinkQuicServer(IP_B, port, save_dir="/tmp/nlb_inbox") as srv:
            print("READY", flush=True)
            item = await srv.recv(timeout=TRANSFER_TIMEOUT_S + 10)
            await asyncio.sleep(1.5)
            return item is not None

    _emit({"ok": asyncio.run(run())})


def role_quic_client(port: int, path: str) -> None:
    sys.path.insert(0, str(ROOT))
    from necklink.quic_adapter import connect_necklink_quic

    async def run() -> bool:
        async with connect_necklink_quic(IP_B, port) as cli:
            rcpt = await cli.send_file(path, timeout=TRANSFER_TIMEOUT_S)
            return bool(rcpt["verified_sha256"])

    t0 = time.perf_counter()
    try:
        ok = asyncio.run(asyncio.wait_for(run(), TRANSFER_TIMEOUT_S))
    except Exception:
        ok = False
    _emit({"ok": ok, "elapsed_s": time.perf_counter() - t0})


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _spawn(ns: str, args: List[str]) -> subprocess.Popen:
    return subprocess.Popen(["ip", "netns", "exec", ns, PY, str(SCRIPT), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _spawn_cmd(ns: str, argv: List[str], quiet: bool = False) -> subprocess.Popen:
    out = subprocess.DEVNULL if quiet else subprocess.PIPE
    return subprocess.Popen(["ip", "netns", "exec", ns, *argv], stdout=out, stderr=out, text=True)


def _wait_ready(p: subprocess.Popen, timeout: float = 60.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = p.stdout.readline()
        if line.strip() == "READY":
            return
        if p.poll() is not None:
            raise RuntimeError(f"server died: {p.stderr.read()[-2000:]}")
    raise RuntimeError("server not ready")


def _result(p: subprocess.Popen, timeout: float) -> Dict[str, Any]:
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    for line in out.splitlines()[::-1]:
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    return {"ok": False, "error": err[-800:]}


def run_transfer(proto: str, path: Path, size: int, sha: str, rate_mbit: float, port: int) -> Dict[str, Any]:
    if proto in ("srt", "uftp") or proto.startswith("quic-ngtcp2-"):
        return run_tool_transfer(proto, path, size, rate_mbit, port)
    cap = str(round(rate_mbit * 1000 * 0.9, 1))
    if proto.startswith("tcp-"):
        srv_args, cli_args = ["tcp-server", str(port), str(size), sha], ["tcp-client", str(port), str(path), proto[4:]]
    elif proto == "quic-aioquic":
        srv_args, cli_args = ["quic-server", str(port)], ["quic-client", str(port), str(path)]
    elif proto == "nltp-default":
        srv_args, cli_args = ["nltp-server", str(port), "none"], ["nltp-client", str(port), str(path), "none"]
    elif proto == "nltp-ratecap":
        srv_args, cli_args = ["nltp-server", str(port), cap], ["nltp-client", str(port), str(path), cap]
    elif proto == "nltp-burst64":
        srv_args, cli_args = ["nltp-server", str(port), "none"], ["nltp-client", str(port), str(path), "none", "65536"]
    elif proto == "nltp-enc":
        srv_args = ["nltp-server", str(port), "none", "1", NLTP_TEST_PSK]
        cli_args = ["nltp-client", str(port), str(path), "none", "0", "cold", NLTP_TEST_PSK]
    elif proto == "nltp-lz4":
        srv_args = ["nltp-server", str(port), "none"]
        cli_args = ["nltp-client", str(port), str(path), "none", "0", "cold", "", "lz4"]
    elif proto == "nltp-enc-lz4":
        srv_args = ["nltp-server", str(port), "none", "1", NLTP_TEST_PSK]
        cli_args = ["nltp-client", str(port), str(path), "none", "0", "cold", NLTP_TEST_PSK, "lz4"]
    elif proto in ("nltp-c-enc", "nltp-c-enc-lz4"):
        srv_args = cli_args = []
    elif proto == "nltp-enc-warm":
        srv_args = ["nltp-server", str(port), "none", "2", NLTP_TEST_PSK]
        cli_args = ["nltp-client", str(port), str(path), "none", "0", "warm", NLTP_TEST_PSK]
    elif proto == "nltp-warm":
        srv_args, cli_args = ["nltp-server", str(port), "none", "2"], ["nltp-client", str(port), str(path), "none", "0", "warm"]
    elif proto in ("kcp-fast", "kcp-fast128", "kcp-fast-cc", "kcptun", "udt"):
        srv_args = cli_args = []
    else:
        raise ValueError(proto)

    tunnel: List[subprocess.Popen] = []
    if proto in ("nltp-c-enc", "nltp-c-enc-lz4"):
        nltp_cli_bin = "/opt/kcpbench/bin/nltp_cli"
        out_rx = Path(f"/tmp/nlb_crx_{port}.bin")
        sh(f"rm -f {out_rx}")
        srv = _spawn_cmd(NS_B, [nltp_cli_bin, "recv", IP_B, str(port), str(out_rx), "--psk", NLTP_TEST_PSK])
        _wait_ready(srv)
        reset_counters()
        c_argv = [nltp_cli_bin, "send", IP_B, str(port), str(path), "--psk", NLTP_TEST_PSK]
        if proto.endswith("-lz4"):
            c_argv.append("--compress")
        cli = _spawn_cmd(NS_A, c_argv)
        cres = _result(cli, TRANSFER_TIMEOUT_S + 30)
        counters = read_counters()
        sres = _result(srv, 20)
        ok = bool(cres.get("ok")) and bool(sres.get("ok")) and _same_file(path, out_rx)
        sh(f"rm -f {out_rx}", check=False)
        el = float(cres.get("elapsed_s", TRANSFER_TIMEOUT_S))
        wire = counters["client_tx_bytes"] + counters["server_tx_bytes"]
        return {
            "ok": ok,
            "elapsed_s": round(el, 3),
            "goodput_mbps": round(size * 8 / 1e6 / el, 3) if ok else 0.0,
            "link_utilization": round(size * 8 / 1e6 / el / rate_mbit, 3) if ok else 0.0,
            "wire_bytes_total": wire,
            "wire_overhead_x": round(wire / size, 3),
            **counters,
        }
    if proto == "udt":
        srv = _spawn_cmd(NS_B, [UDT_XFER, "server", IP_B, str(port), str(path)])
        _wait_ready(srv)
        reset_counters()
        cli = _spawn_cmd(NS_A, [UDT_XFER, "client", IP_B, str(port), str(path)])
    elif proto.startswith("kcp-"):
        mode = proto[4:]
        srv = _spawn_cmd(NS_B, [KCP_XFER, "server", IP_B, str(port), str(path), mode])
        _wait_ready(srv)
        reset_counters()
        cli = _spawn_cmd(NS_A, [KCP_XFER, "client", IP_B, str(port), str(path), mode])
    elif proto == "kcptun":
        # kernel TCP (CUBIC) -> kcptun-client -> [netem link, UDP] -> kcptun-server -> kernel TCP server
        tport, lport = port + 1000, port + 2000
        srv = _spawn(NS_B, ["tcp-server", str(tport), str(size), sha])
        _wait_ready(srv)
        common = ["--mode", "fast", "--crypt", "none", "--nocomp", "--quiet"]
        tunnel.append(_spawn_cmd(NS_B, ["kcptun-server", "-l", f"{IP_B}:{port}", "-t", f"{IP_B}:{tport}", *common], quiet=True))
        tunnel.append(_spawn_cmd(NS_A, ["kcptun-client", "-r", f"{IP_B}:{port}", "-l", f"127.0.0.1:{lport}", *common], quiet=True))
        time.sleep(0.5)
        reset_counters()
        cli = _spawn(NS_A, ["tcp-client", str(lport), str(path), "cubic", "127.0.0.1"])
    else:
        srv = _spawn(NS_B, srv_args)
        _wait_ready(srv)
        reset_counters()
        cli = _spawn(NS_A, cli_args)
    cres = _result(cli, TRANSFER_TIMEOUT_S + 30)
    counters = read_counters()
    sres = _result(srv, 20)
    for t in tunnel:
        t.kill()
        t.wait()
    ok = bool(cres.get("ok")) and bool(sres.get("ok"))
    el = float(cres.get("elapsed_s", TRANSFER_TIMEOUT_S))
    wire = counters["client_tx_bytes"] + counters["server_tx_bytes"]
    return {
        "ok": ok,
        "elapsed_s": round(el, 3),
        "goodput_mbps": round(size * 8 / 1e6 / el, 3) if ok else 0.0,
        "link_utilization": round(size * 8 / 1e6 / el / rate_mbit, 3) if ok else 0.0,
        "wire_bytes_total": wire,
        "wire_overhead_x": round(wire / size, 3),
        **counters,
        **({"error": cres.get("error", "")[-300:]} if not ok else {}),
    }


def _ensure_quic_cert() -> None:
    QUIC_CERT_DIR.mkdir(parents=True, exist_ok=True)
    if not (QUIC_CERT_DIR / "cert.pem").exists():
        sh(f"openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj /CN=nlb -keyout {QUIC_CERT_DIR}/key.pem -out {QUIC_CERT_DIR}/cert.pem")


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def run_tool_transfer(proto: str, path: Path, size: int, rate_mbit: float, port: int) -> Dict[str, Any]:
    """SRT / UFTP / ngtcp2: the sender's wall time until it exits, verified by comparing the received file."""
    out_dir = Path(f"/tmp/nlb_rx_{port}")
    sh(f"rm -rf {out_dir} && mkdir -p {out_dir}")
    got = out_dir / path.name
    if proto == "srt":
        srv = _spawn_cmd(NS_B, ["srt-file-transmit", f"srt://:{port}?mode=listener", f"file://{out_dir}/"], quiet=True)
        time.sleep(0.5)
        argv = ["srt-file-transmit", f"file://{path}", f"srt://{IP_B}:{port}?mode=caller"]
    elif proto == "uftp":
        srv = _spawn_cmd(NS_B, ["uftpd", "-d", "-p", str(port), "-D", str(out_dir), "-q"], quiet=True)
        time.sleep(0.5)
        argv = ["uftp", "-M", IP_B, "-p", str(port), "-C", "tfmcc", "-q", "-x", "0", str(path)]
    else:  # quic-ngtcp2-<cc>: the client downloads from the server (the links are symmetric)
        cc = proto.rsplit("-", 1)[1]
        _ensure_quic_cert()
        srv = _spawn_cmd(NS_B, ["gtlsserver", "-q", f"--cc={cc}", "-d", str(path.parent), IP_B, str(port),
                                str(QUIC_CERT_DIR / "key.pem"), str(QUIC_CERT_DIR / "cert.pem")], quiet=True)
        time.sleep(0.5)
        argv = ["gtlsclient", "-q", "--no-quic-dump", f"--cc={cc}", f"--download={out_dir}", "--exit-on-all-streams-close",
                f"--timeout={int(TRANSFER_TIMEOUT_S)}s", IP_B, str(port), f"https://{IP_B}:{port}/{path.name}"]
    reset_counters()
    t0 = time.perf_counter()
    cli = _spawn_cmd(NS_A, argv, quiet=True)
    try:
        cli.wait(timeout=TRANSFER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        cli.kill()
        cli.wait()
    el = time.perf_counter() - t0
    # receivers may finish writing a moment after the sender exits
    for _ in range(20):
        if _same_file(path, got):
            break
        time.sleep(0.1)
    counters = read_counters()
    srv.kill()
    srv.wait()
    ok = _same_file(path, got)
    wire = counters["client_tx_bytes"] + counters["server_tx_bytes"]
    return {
        "ok": ok,
        "elapsed_s": round(el, 3),
        "goodput_mbps": round(size * 8 / 1e6 / el, 3) if ok else 0.0,
        "link_utilization": round(size * 8 / 1e6 / el / rate_mbit, 3) if ok else 0.0,
        "wire_bytes_total": wire,
        "wire_overhead_x": round(wire / size, 3),
        **counters,
    }


def run_fairness(rate_mbit: float = 10, rtt_ms: float = 40, seconds: int = 20) -> Dict[str, Any]:
    """One TCP CUBIC bulk flow sharing a bottleneck with: nothing / a 2nd CUBIC flow / an NLTP upload."""
    set_link(rate_mbit, rtt_ms, 0.0, "random")
    blob = BLOB_DIR / "fair_4m.bin"
    blob.write_bytes(os.urandom(4 * 1024 * 1024))
    out: Dict[str, Any] = {"link": f"{rate_mbit} Mbit, {rtt_ms} ms RTT, 0% random loss"}

    def tcp_bulk(port: int) -> subprocess.Popen:
        return subprocess.Popen(["ip", "netns", "exec", NS_A, "iperf3", "-c", IP_B, "-p", str(port), "-t", str(seconds),
                                 "-C", "cubic", "-J"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def mbps(p: subprocess.Popen) -> float:
        o, _ = p.communicate(timeout=seconds + 30)
        try:
            return round(json.loads(o)["end"]["sum_received"]["bits_per_second"] / 1e6, 3)
        except Exception:
            return -1.0

    srvs = [subprocess.Popen(["ip", "netns", "exec", NS_B, "iperf3", "-s", "-p", str(p)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for p in (5201, 5202)]
    time.sleep(1.0)
    try:
        out["tcp_alone_mbps"] = mbps(tcp_bulk(5201))
        a, b = tcp_bulk(5201), tcp_bulk(5202)
        out["tcp_vs_tcp_mbps"] = [mbps(a), mbps(b)]
        for label, cap in (("nltp_default", "none"), ("nltp_ratecap", str(rate_mbit * 1000 * 0.9))):
            ns_srv = _spawn(NS_B, ["nltp-server", "9300", cap])
            _wait_ready(ns_srv)
            t = tcp_bulk(5201)
            time.sleep(2.0)
            ncli = _spawn(NS_A, ["nltp-client", "9300", str(blob), cap])
            tcp_mbps = mbps(t)
            nres = _result(ncli, TRANSFER_TIMEOUT_S + 30)
            _result(ns_srv, 20)
            n_mbps = round(blob.stat().st_size * 8 / 1e6 / nres["elapsed_s"], 3) if nres.get("ok") else 0.0
            out[f"tcp_vs_{label}"] = {"tcp_mbps": tcp_mbps, "nltp_ok": bool(nres.get("ok")),
                                      "nltp_goodput_mbps": n_mbps, "nltp_elapsed_s": nres.get("elapsed_s")}
    finally:
        for s in srvs:
            s.kill()
    return out


def _make_structured_aqms_log(size: int) -> bytes:
    """Realistic multi-sensor AQMS JSONL log with varying timestamps & readings (not constant padding)."""
    import random
    rng = random.Random(20261112)
    chunks: List[bytes] = []
    cur = 0
    seq = 1
    ts = 1762900000
    while cur < size:
        line = (
            f'{{"station":"AQMS-RUT200-VDR07","ts":{ts},"seq":{seq},'
            f'"pm25":{38.4 + rng.gauss(0, 6.2):.2f},"pm10":{84.1 + rng.gauss(0, 11.5):.2f},'
            f'"so2_ppb":{11.2 + rng.gauss(0, 1.8):.2f},"no2_ppb":{24.6 + rng.gauss(0, 3.4):.2f},'
            f'"o3_ppb":{31.0 + rng.gauss(0, 4.1):.2f},"co_ppm":{0.82 + rng.gauss(0, 0.09):.3f},'
            f'"temp_c":{29.4 + rng.gauss(0, 0.4):.2f},"rh_pct":{61.5 + rng.gauss(0, 1.2):.1f},'
            f'"vbat_v":{12.68 + rng.gauss(0, 0.03):.2f},"rssi_dbm":{-67 + rng.randint(-4, 4)}}}\n'
        ).encode("utf-8")
        chunks.append(line)
        cur += len(line)
        seq += 1
        ts += 1
    raw = b"".join(chunks)[:size]
    return raw


def main(quick: bool, protocols: Optional[List[str]] = None, tag: str = "", fairness_test: bool = True,
         conditions: Optional[List[str]] = None, sizes: Optional[List[int]] = None, reps: Optional[int] = None,
         extended: bool = False, structured_log: bool = False) -> None:
    global PROTOCOLS
    if os.geteuid() != 0:
        sys.exit("run as root (netns + tc)")
    if protocols:
        PROTOCOLS = protocols
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    conds = EXTENDED_CONDITIONS if extended else (CONDITIONS[:2] if quick else CONDITIONS)
    if conditions:
        conds = [c for c in conds if c[0] in conditions]
    sizes = sizes or (SIZES[:1] if quick else SIZES)
    reps = reps or (1 if quick else 3)
    setup_topology()
    results: List[Dict[str, Any]] = []
    port = 9100
    t_start = time.time()
    try:
        for size in sizes:
            path = BLOB_DIR / (f"aqms_log_{size}.jsonl" if structured_log else f"blob_{size}.bin")
            path.write_bytes(_make_structured_aqms_log(size) if structured_log else os.urandom(size))
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            for cond in conds:
                name, rate, rtt, loss, mode = cond[:5]
                set_link(rate, rtt, loss, mode, cond[5] if len(cond) > 5 else None)
                for proto in PROTOCOLS:
                    runs = []
                    for _ in range(reps):
                        port += 1
                        runs.append(run_transfer(proto, path, size, sha, rate, port))
                    okr = [r for r in runs if r["ok"]]
                    row = {
                        "condition": name, "rate_mbit": rate, "rtt_ms": rtt, "loss_pct": loss, "loss_mode": mode,
                        "size_bytes": size, "protocol": proto, "reps": reps, "success": f"{len(okr)}/{reps}",
                        "median_elapsed_s": round(statistics.median(r["elapsed_s"] for r in okr), 3) if okr else None,
                        "median_goodput_mbps": round(statistics.median(r["goodput_mbps"] for r in okr), 3) if okr else 0.0,
                        "mean_wire_overhead_x": round(statistics.mean(r["wire_overhead_x"] for r in okr), 3) if okr else None,
                        "runs": runs,
                    }
                    results.append(row)
                    print(f"[{time.time() - t_start:6.0f}s] {name:24s} {size // 1024:5d}KB {proto:14s} ok={row['success']} "
                          f"t={row['median_elapsed_s']} s  goodput={row['median_goodput_mbps']} Mbps  wire={row['mean_wire_overhead_x']}x",
                          flush=True)
        fairness = None if (quick or not fairness_test) else run_fairness()
        if fairness:
            print("fairness:", json.dumps(fairness), flush=True)
    finally:
        teardown_topology()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    out_path = OUT_JSON.with_name("fair_transport_benchmark_quick.json") if quick else OUT_JSON
    if tag:
        out_path = out_path.with_name(out_path.stem + f"_{tag}.json")
    out_path.write_text(json.dumps({
        "kernel": os.uname().release,
        "notes": "netem on both veth directions; loss_pct is per direction; TSO/GSO/GRO off; bytes from iptables OUTPUT counters",
        "results": results, "fairness": fairness,
    }, indent=1))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].endswith(("-server", "-client")):
        role, a = sys.argv[1], sys.argv[2:]
        cap = (lambda v: None if v == "none" else float(v))
        {
            "tcp-server": lambda: role_tcp_server(int(a[0]), int(a[1]), a[2]),
            "tcp-client": lambda: role_tcp_client(int(a[0]), a[1], a[2], *a[3:4]),
            "nltp-server": lambda: role_nltp_server(int(a[0]), cap(a[1]), int(a[2]) if len(a) > 2 else 1,
                                                    a[3] if len(a) > 3 and a[3] else None),
            "nltp-client": lambda: role_nltp_client(int(a[0]), a[1], cap(a[2]), int(a[3]) if len(a) > 3 else 0,
                                                    len(a) > 4 and a[4] == "warm",
                                                    a[5] if len(a) > 5 and a[5] else None,
                                                    len(a) > 6 and a[6] == "lz4"),
            "quic-server": lambda: role_quic_server(int(a[0])),
            "quic-client": lambda: role_quic_client(int(a[0]), a[1]),
        }[role]()
    else:
        ap = argparse.ArgumentParser()
        ap.add_argument("--quick", action="store_true")
        ap.add_argument("--protocols", nargs="*", help=f"subset of {PROTOCOLS}")
        ap.add_argument("--tag", default="", help="suffix for the results JSON")
        ap.add_argument("--fairness", choices=["yes", "no"], default="yes")
        ap.add_argument("--conditions", nargs="*", help="subset of condition names")
        ap.add_argument("--sizes", nargs="*", type=int, help="subset of sizes in bytes")
        ap.add_argument("--reps", type=int, default=None, help="runs per cell (default 3)")
        ap.add_argument("--timeout", type=float, default=None, help="per-transfer timeout in seconds (default 90)")
        ap.add_argument("--extended", action="store_true", help="use EXTENDED_CONDITIONS (jitter, reorder, GEO sat, asymmetric)")
        ap.add_argument("--structured-log", action="store_true", help="use realistic multi-sensor AQMS JSONL payload instead of os.urandom")
        ap.add_argument("--size-list", nargs="*", type=int, help=argparse.SUPPRESS)
        args = ap.parse_args()
        if args.timeout:
            TRANSFER_TIMEOUT_S = args.timeout
        main(args.quick, args.protocols, args.tag, args.fairness == "yes", args.conditions, args.sizes, args.reps,
             args.extended, args.structured_log)
