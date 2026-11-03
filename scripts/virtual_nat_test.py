"""
Virtual-internet test of NLTP NAT traversal (C core + Python), run as root in WSL/Linux:

    hostA 10.0.1.2 -- [rtrA NAT] 198.51.100.2 --+
                                                [core router] -- pub 203.0.113.10 (STUN + NLTPRelayHub)
    hostB 10.0.2.2 -- [rtrB NAT] 192.0.2.2 -----+

The core router puts at least one hop between the two NATs, as on the real internet.

Each host <-> router "last mile" gets radio-like loss and delay (netem). Tests: STUN discovery from behind
NAT, UDP hole punching (C<->C, C<->Python), relay (C<->Python, C<->C), symmetric NAT (punch must fail,
relay must work), and NAT keepalive (mapping expiry with/without keepalives).

    /opt/nlb/bin/python scripts/virtual_nat_test.py
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPT = Path(__file__).resolve()
PY = sys.executable
_DEFAULT_CLI = ROOT / "csrc" / "build" / "nltp_cli"
CLI = os.environ.get("NLTP_CLI") or (str(_DEFAULT_CLI) if _DEFAULT_CLI.exists() else "/opt/kcpbench/bin/nltp_cli")
PUB_IP = "203.0.113.10"
STUN_PORT, RELAY_PORT = 3478, 9000
NS = ["vcore", "vpub", "vrtrA", "vhostA", "vrtrB", "vhostB"]
WAN_A, WAN_B = "198.51.100.2", "192.0.2.2"
LAST_MILE = "delay 40ms loss random 5%"   # each direction, host <-> router
PSK = "virtual-nat-test-passphrase"
BLOB = Path("/tmp/vnat_blob.bin")


def sh(cmd: str, check: bool = True) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}\n{r.stderr}")
    return r.stdout


def nsx(ns: str, cmd: str, check: bool = True) -> str:
    return sh(f"ip netns exec {ns} {cmd}", check)


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------

def teardown() -> None:
    for ns in NS:
        sh(f"ip netns del {ns}", check=False)


def build(symmetric: bool = False, udp_timeout_s: int = 30) -> None:
    teardown()
    sh("modprobe nf_conntrack 2>/dev/null; true", check=False)
    for ns in NS:
        sh(f"ip netns add {ns}")
        nsx(ns, "ip link set lo up")
    def wire(a_ns: str, a_if: str, b_ns: str, b_if: str) -> None:
        sh(f"ip link add {a_if} type veth peer name {b_if}")
        sh(f"ip link set {a_if} netns {a_ns}")
        sh(f"ip link set {b_if} netns {b_ns}")
        for ns, ifc in ((a_ns, a_if), (b_ns, b_if)):
            nsx(ns, f"ip link set {ifc} up")
            nsx(ns, f"ethtool -K {ifc} tso off gso off gro off", check=False)

    # public side: the core router connects the relay/STUN host and both NAT routers, each on its own network
    nsx("vcore", "sysctl -qw net.ipv4.ip_forward=1")
    for ns, ifc, core_if, ip, gw in (("vpub", "pub0", "cpub", PUB_IP, "203.0.113.1"),
                                     ("vrtrA", "wanA", "ca", WAN_A, "198.51.100.1"),
                                     ("vrtrB", "wanB", "cb", WAN_B, "192.0.2.1")):
        wire(ns, ifc, "vcore", core_if)
        nsx("vcore", f"ip addr add {gw}/24 dev {core_if}")
        nsx(ns, f"ip addr add {ip}/24 dev {ifc}")
        nsx(ns, f"ip route add default via {gw}")

    # private sides
    for host, rtr, h_if, r_if, h_ip, r_ip, wan in (("vhostA", "vrtrA", "lanA", "rlanA", "10.0.1.2", "10.0.1.1", "wanA"),
                                                   ("vhostB", "vrtrB", "lanB", "rlanB", "10.0.2.2", "10.0.2.1", "wanB")):
        wire(host, h_if, rtr, r_if)
        nsx(host, f"ip addr add {h_ip}/24 dev {h_if}")
        nsx(rtr, f"ip addr add {r_ip}/24 dev {r_if}")
        nsx(host, f"ip route add default via {r_ip}")
        nsx(rtr, "sysctl -qw net.ipv4.ip_forward=1")
        nsx(rtr, f"sysctl -qw net.netfilter.nf_conntrack_udp_timeout={udp_timeout_s}", check=False)
        nsx(rtr, f"sysctl -qw net.netfilter.nf_conntrack_udp_timeout_stream={udp_timeout_s}", check=False)
        masq = "MASQUERADE --random-fully" if symmetric else "MASQUERADE"
        nsx(rtr, f"iptables -t nat -A POSTROUTING -o {wan} -j {masq}")
        # radio-like last mile, both directions
        nsx(host, f"tc qdisc add dev {h_if} root netem {LAST_MILE}")
        nsx(rtr, f"tc qdisc add dev {r_if} root netem {LAST_MILE}")


# ---------------------------------------------------------------------------
# Services in the public namespace + Python roles
# ---------------------------------------------------------------------------

def role_stun_server() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((PUB_IP, STUN_PORT))
    print("READY", flush=True)
    while True:
        data, (ip, port) = s.recvfrom(2048)
        if len(data) < 20 or struct.unpack_from("!HHI", data)[0] != 0x0001:
            continue
        tid = data[8:20]
        xport = port ^ 0x2112
        xip = struct.unpack("!I", socket.inet_aton(ip))[0] ^ 0x2112A442
        attr = struct.pack("!HHBBHI", 0x0020, 8, 0, 0x01, xport, xip)
        s.sendto(struct.pack("!HHI12s", 0x0101, len(attr), 0x2112A442, tid) + attr, (ip, port))


def role_relay() -> None:
    from necklink.internet import NLTPRelayHub
    hub = NLTPRelayHub((PUB_IP, RELAY_PORT))
    print("READY", flush=True)
    while True:
        time.sleep(1)
    hub.close()


def role_py_recv(mode: str, bind_port: int, psk: Optional[str]) -> None:
    from necklink.nltp import NLTPServer
    kw = {"psk": psk} if psk else {}
    srv = None
    if mode in ("punch", "punch-fallback"):
        srv = NLTPServer(("0.0.0.0", bind_port), save_dir="/tmp/vnat_inbox", **kw)
        wan = srv.discover_wan(stun_servers=[(PUB_IP, STUN_PORT)], timeout_s=3.0)
        print(f"WAN {wan.public_ip}:{wan.public_port}", flush=True)
        peer = sys.stdin.readline().split()[1]
        host, port = peer.rsplit(":", 1)
        ok = srv.punch_p2p((host, int(port)), timeout_s=3.0)
        print(f"PUNCH {'ok' if ok else 'failed'}", flush=True)
        if not ok and mode == "punch-fallback":
            srv.close()
            srv = None
            print("FALLBACK relay", flush=True)
            mode = "relay"
    if mode == "relay":
        srv = NLTPServer(("0.0.0.0", bind_port), save_dir="/tmp/vnat_inbox", relay_addr=(PUB_IP, RELAY_PORT), room="boat-7", **kw)
        time.sleep(0.5)
        print("READY", flush=True)
    item = srv.recv(timeout=60)
    time.sleep(1.5)
    srv.close()
    ok = item is not None and item.sha256_verified and item.data == BLOB.read_bytes()
    print("RESULT " + json.dumps({"ok": ok}), flush=True)


def role_py_send(mode: str, psk: Optional[str]) -> None:
    from necklink.nltp import NLTPClient
    kw = {"psk": psk} if psk else {}
    cli = None
    if mode in ("punch", "punch-fallback"):
        cli = NLTPClient(("127.0.0.1", 9), **kw)
        wan = cli.discover_wan(stun_servers=[(PUB_IP, STUN_PORT)], timeout_s=3.0)
        print(f"WAN {wan.public_ip}:{wan.public_port}", flush=True)
        peer = sys.stdin.readline().split()[1]
        host, port = peer.rsplit(":", 1)
        cli.set_peer_endpoint((host, int(port)))
        ok = cli.punch_p2p((host, int(port)), timeout_s=3.0)
        print(f"PUNCH {'ok' if ok else 'failed'}", flush=True)
        if not ok and mode == "punch-fallback":
            cli.close()
            cli = None
            print("FALLBACK relay", flush=True)
            mode = "relay"
    if mode == "relay":
        cli = NLTPClient(relay_addr=(PUB_IP, RELAY_PORT), room="boat-7", **kw)
        time.sleep(0.5)
    t0 = time.perf_counter()
    r = cli.send_file(BLOB, timeout=60)
    cli.close()
    print("RESULT " + json.dumps({"ok": bool(r["verified_sha256"]), "elapsed_s": time.perf_counter() - t0}), flush=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def spawn(ns: str, argv: List[str]) -> subprocess.Popen:
    return subprocess.Popen(["ip", "netns", "exec", ns, *argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)


class Proc:
    """Reads a child's stdout in the background so we can wait for specific lines."""

    def __init__(self, p: subprocess.Popen) -> None:
        self.p = p
        self.lines: List[str] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.p.stdout:
            self.lines.append(line.rstrip())

    def wait_for(self, prefix: str, timeout: float = 20.0) -> Optional[str]:
        t0 = time.time()
        while time.time() - t0 < timeout:
            for line in self.lines:
                if line.startswith(prefix):
                    return line
            if self.p.poll() is not None and not any(l.startswith(prefix) for l in self.lines):
                time.sleep(0.2)
                return next((l for l in self.lines if l.startswith(prefix)), None)
            time.sleep(0.05)
        return None

    def write(self, text: str) -> None:
        self.p.stdin.write(text + "\n")
        self.p.stdin.flush()

    def result(self, timeout: float = 90.0) -> Dict:
        line = self.wait_for("RESULT ", timeout)
        return json.loads(line[7:]) if line else {"ok": False, "error": "no result", "tail": self.lines[-5:]}

    def kill(self) -> None:
        if self.p.poll() is None:
            self.p.kill()


def c_cmd(mode: str, file: str, extra: List[str]) -> List[str]:
    target = ["0.0.0.0", "7000"] if mode == "recv" else ["203.0.113.250", "9"]
    return [CLI, mode, *target, file, *extra]


def py_cmd(role: str, *args: str) -> List[str]:
    return [PY, str(SCRIPT), role, *args]


def run_punch(sender: str, receiver: str, psk: Optional[str], extra_c: Optional[List[str]] = None,
              fallback: bool = False) -> Dict:
    """Hole punch through both NATs; with fallback=True both sides switch to the relay if punching fails."""
    extra_c = (extra_c or []) + (["--relay", f"{PUB_IP}:{RELAY_PORT}", "--room", "boat-7"] if fallback else [])
    py_mode = "punch-fallback" if fallback else "punch"
    stun = ["--stun", f"{PUB_IP}:{STUN_PORT}", "--punch"]
    pskargs = ["--psk", psk] if psk else []
    rx = Proc(spawn("vhostB", c_cmd("recv", "/tmp/vnat_rx.bin", stun + pskargs + extra_c) if receiver == "c"
                    else py_cmd("py-recv", py_mode, "7000", psk or "")))
    tx = Proc(spawn("vhostA", c_cmd("send", str(BLOB), stun + pskargs + ["--bind-port", "7001"] + extra_c) if sender == "c"
                    else py_cmd("py-send", py_mode, psk or "")))
    wan_rx, wan_tx = rx.wait_for("WAN "), tx.wait_for("WAN ")
    if not wan_rx or not wan_tx:
        rx.kill(); tx.kill()
        return {"ok": False, "error": "stun", "rx": rx.lines[-3:], "tx": tx.lines[-3:]}
    rx.write("PEER " + wan_tx.split()[1])
    tx.write("PEER " + wan_rx.split()[1])
    punch_tx = tx.wait_for("PUNCH ", 15)
    res_tx, res_rx = tx.result(), rx.result(10)
    rx.kill(); tx.kill()
    path = "relay (fallback)" if any("FALLBACK" in l for l in rx.lines + tx.lines) else "direct"
    return {"ok": bool(res_tx.get("ok")) and bool(res_rx.get("ok")), "path": path, "sender_wan": wan_tx.split()[1],
            "receiver_wan": wan_rx.split()[1], "punch": punch_tx, "elapsed_s": res_tx.get("elapsed_s"),
            "sent": [l for l in tx.lines if l.startswith("SENT")],
            **({} if res_tx.get("ok") and res_rx.get("ok") else {"rx_tail": rx.lines[-6:], "tx_tail": tx.lines[-6:]})}


def run_relay(sender: str, receiver: str, psk: Optional[str]) -> Dict:
    relay = ["--relay", f"{PUB_IP}:{RELAY_PORT}", "--room", "boat-7"] + (["--psk", psk] if psk else [])
    rx = Proc(spawn("vhostB", c_cmd("recv", "/tmp/vnat_rx.bin", relay) if receiver == "c"
                    else py_cmd("py-recv", "relay", "7000", psk or "")))
    rx.wait_for("RELAY registered" if receiver == "c" else "READY", 10)
    tx = Proc(spawn("vhostA", c_cmd("send", str(BLOB), relay) if sender == "c" else py_cmd("py-send", "relay", psk or "")))
    res_tx, res_rx = tx.result(), rx.result(10)
    rx.kill(); tx.kill()
    return {"ok": bool(res_tx.get("ok")) and bool(res_rx.get("ok")), "elapsed_s": res_tx.get("elapsed_s")}


def check_stun() -> Dict:
    p = Proc(spawn("vhostA", c_cmd("send", str(BLOB), ["--stun", f"{PUB_IP}:{STUN_PORT}", "--bind-port", "7005"])))
    wan = p.wait_for("WAN ", 10)
    p.kill()
    ok = bool(wan) and wan.split()[1].startswith(WAN_A + ":")
    return {"ok": ok, "wan": wan}


def main() -> None:
    if os.geteuid() != 0:
        sys.exit("run as root")
    BLOB.write_bytes(os.urandom(1024 * 1024))
    rows: List[Dict] = []
    services: List[subprocess.Popen] = []

    def record(name: str, r: Dict, expect_ok: bool = True) -> None:
        passed = bool(r.get("ok")) == expect_ok
        rows.append({"test": name, "expected_ok": expect_ok, "passed": passed, **r})
        el = r.get("elapsed_s")
        print(f"{'PASS' if passed else 'FAIL'}  {name:58s} "
              f"{'transfer ok' if r.get('ok') else 'no transfer'}"
              + (f" via {r['path']}" if r.get("ok") and r.get("path") else "")
              + (f"  {el:.2f} s" if isinstance(el, (int, float)) else ""), flush=True)

    def start_services() -> None:
        for role in ("stun-server", "relay"):
            p = Proc(spawn("vpub", py_cmd(role)))
            p.wait_for("READY", 15)
            services.append(p.p)

    def stop_services() -> None:
        for p in services:
            p.kill()
        services.clear()

    try:
        print(f"virtual internet: two hosts behind NAT, last mile '{LAST_MILE}' each way, 1 MB files\n", flush=True)
        build(symmetric=False)
        start_services()
        record("STUN: C client learns its public address behind NAT", check_stun())
        for i in range(3):  # repeated: the direct path can lose a NAT port-clash race
            record(f"punch + relay fallback, C -> C, encrypted (run {i + 1})", run_punch("c", "c", PSK, fallback=True))
            record(f"punch + relay fallback, C -> Python, encrypted (run {i + 1})", run_punch("c", "py", PSK, fallback=True))
            record(f"punch + relay fallback, Python -> C (run {i + 1})", run_punch("py", "c", None, fallback=True))
        record("relay, C -> Python, encrypted", run_relay("c", "py", PSK))
        record("relay, Python -> C, encrypted", run_relay("py", "c", PSK))
        record("relay, C -> C", run_relay("c", "c", None))
        stop_services()

        build(symmetric=True)
        start_services()
        record("symmetric NAT: hole punch C -> C must fail", run_punch("c", "c", None), expect_ok=False)
        record("symmetric NAT: punch + automatic relay fallback, C -> C", run_punch("c", "c", PSK, fallback=True))
        record("symmetric NAT: relay C -> Python still works", run_relay("c", "py", PSK))
        stop_services()

        build(symmetric=False, udp_timeout_s=4)
        start_services()
        idle = ["--count", "2", "--idle-ms", "8000"]
        record("NAT forgets after 4 s; 2nd send after 8 s idle, no keepalive",
               run_punch("c", "c", None, idle), expect_ok=False)
        record("same, with 1 s keepalives: 2nd send works",
               run_punch("c", "c", None, idle + ["--keepalive-ms", "1000"]))
        stop_services()
    finally:
        stop_services()
        teardown()

    n_pass = sum(r["passed"] for r in rows)
    print(f"\n{n_pass}/{len(rows)} checks passed")
    (ROOT / "results" / "virtual_nat_test.json").write_text(json.dumps(rows, indent=1, default=str))
    sys.exit(0 if n_pass == len(rows) else 1)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--repeat-cc":
        # diagnostics: repeat the C -> C encrypted punch transfer N times, print failures in full
        BLOB.write_bytes(os.urandom(1024 * 1024))
        build(symmetric=False)
        procs = [Proc(spawn("vpub", py_cmd(r))) for r in ("stun-server", "relay")]
        for q in procs:
            q.wait_for("READY", 15)
        try:
            fails = 0
            for i in range(int(sys.argv[2])):
                r = run_punch("c", "c", PSK, fallback=True)
                fails += not r["ok"]
                print(("ok  " if r["ok"] else "FAIL") + f" {i + 1}: {r.get('path')} {r.get('elapsed_s')}", flush=True)
                if not r["ok"]:
                    print(json.dumps(r, indent=1), flush=True)
                    rx_file = Path("/tmp/vnat_rx.bin")
                    if rx_file.exists():
                        a, b = BLOB.read_bytes(), rx_file.read_bytes()
                        bad = sorted({i // 7680 for i in range(min(len(a), len(b))) if a[i] != b[i]})
                        print(f"received {len(b)} of {len(a)} bytes; differing generations: {bad[:40]}", flush=True)
                    rx_file.unlink(missing_ok=True)
            print(f"{fails} failures")
        finally:
            for q in procs:
                q.kill()
            teardown()
    elif len(sys.argv) > 1 and sys.argv[1] in ("stun-server", "relay", "py-recv", "py-send"):
        role, a = sys.argv[1], sys.argv[2:]
        psk_arg = (a[-1] or None) if a else None
        {"stun-server": role_stun_server, "relay": role_relay,
         "py-recv": lambda: role_py_recv(a[0], int(a[1]), psk_arg),
         "py-send": lambda: role_py_send(a[0], psk_arg)}[role]()
    else:
        main()
