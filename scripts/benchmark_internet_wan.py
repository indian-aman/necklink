"""
Empirical Internet & WAN Readiness Benchmark (`scripts/benchmark_internet_wan.py`).

Measures exact, reproducible metrics across the 6 Internet features added to NLTP:
  1. `ChaCha20-Poly1305` + `RFC 6479` Anti-Replay overhead vs unencrypted NLTP (latency & wire bytes)
  2. Router `32-packet` FIFO queue survival: Unpaced UDP burst vs `TokenBucketPacer`
  3. Live Mid-Upload Wi-Fi -> 4G Connection Migration (`rebind_socket` under 15% packet loss)
  4. Dual Stateful NAT Firewalls + P2P UDP Hole Punching (`punch_p2p` time & throughput)
  5. Strict 4G/5G Symmetric CGNAT Traversal via `NLTPRelayHub` (overhead, MTU ceiling, intruder drops)
  6. Live Public Internet `RFC 5389` STUN check (masked IP for privacy, exact RTT & NAT type)
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from necklink import NLTPClient, NLTPRelayHub, NLTPServer, generate_nltp_key, pack_nltp_frame
from necklink.internet import RELAY_VER_DATA, make_room_tag, wrap_relay_packet
from tests.test_internet_wan import BottleneckRouterQueue, DualStatefulNATBox


def main() -> None:
    results: dict = {}
    tmp_dir = Path(tempfile.mkdtemp(prefix="nltp_wan_bench_"))
    blob_64k = os.urandom(64 * 1024)
    blob_128k = os.urandom(128 * 1024)
    f64 = tmp_dir / "test_64k.bin"
    f128 = tmp_dir / "test_128k.bin"
    f64.write_bytes(blob_64k)
    f128.write_bytes(blob_128k)
    psk = generate_nltp_key()

    # 1. Encryption Overhead (Plaintext vs ChaCha20-Poly1305 AEAD on 64 KB file)
    with NLTPServer(("127.0.0.1", 0), save_dir=tmp_dir / "plain") as srv_p:
        with NLTPClient(srv_p.getsockname(), max_tx_kbps=None) as cli_p:
            r_plain = cli_p.send_file(f64, timeout=10.0)
            plain_wire = cli_p.engine.wire_bytes_tx + srv_p.engine.wire_bytes_tx

    with NLTPServer(("127.0.0.1", 0), save_dir=tmp_dir / "enc", psk=psk) as srv_e:
        with NLTPClient(srv_e.getsockname(), psk=psk, max_tx_kbps=None) as cli_e:
            r_enc = cli_e.send_file(f64, timeout=10.0)
            enc_wire = cli_e.engine.wire_bytes_tx + srv_e.engine.wire_bytes_tx

    results["encryption_64kb"] = {
        "plaintext_ms": round(r_plain["elapsed_s"] * 1000.0, 2),
        "plaintext_wire_bytes": plain_wire,
        "chacha20_poly1305_ms": round(r_enc["elapsed_s"] * 1000.0, 2),
        "chacha20_poly1305_wire_bytes": enc_wire,
        "extra_wire_overhead_pct": round((enc_wire / plain_wire - 1.0) * 100.0, 2),
        "verified_sha256": r_enc["verified_sha256"],
    }

    # 2. Router 32-Packet Narrow FIFO Queue: Unpaced (`max_tx_kbps=None`) vs Paced (`9.5 Mbps` on `12 Mbps` link)
    with NLTPServer(("127.0.0.1", 0), save_dir=tmp_dir / "unpaced", psk=psk) as srv_u:
        rtr_u = BottleneckRouterQueue(srv_u.getsockname(), link_kbps=12_000.0, max_queue_pkts=32)
        try:
            with NLTPClient(rtr_u.addr, psk=psk, max_tx_kbps=None) as cli_u:
                r_unpaced = cli_u.send_file(f128, timeout=15.0)
                unpaced_wire = cli_u.engine.wire_bytes_tx
        finally:
            unpaced_drops = rtr_u.tail_drops
            rtr_u.close()

    with NLTPServer(("127.0.0.1", 0), save_dir=tmp_dir / "paced", psk=psk) as srv_pc:
        rtr_p = BottleneckRouterQueue(srv_pc.getsockname(), link_kbps=12_000.0, max_queue_pkts=32)
        try:
            with NLTPClient(rtr_p.addr, psk=psk, max_tx_kbps=9_500.0) as cli_pc:
                r_paced = cli_pc.send_file(f128, timeout=15.0)
                paced_wire = cli_pc.engine.wire_bytes_tx
                max_mtu = rtr_p.max_pkt_size_seen
        finally:
            paced_drops = rtr_p.tail_drops
            rtr_p.close()

    results["router_32pkt_fifo_128kb"] = {
        "unpaced_tail_drops": unpaced_drops,
        "unpaced_ms": round(r_unpaced["elapsed_s"] * 1000.0, 2),
        "unpaced_tx_bytes": unpaced_wire,
        "paced_tail_drops": paced_drops,
        "paced_ms": round(r_paced["elapsed_s"] * 1000.0, 2),
        "paced_tx_bytes": paced_wire,
        "max_udp_packet_bytes": max_mtu,
    }

    # 3. Live Wi-Fi -> 4G Socket Roaming Mid-Upload under ~15% Round-Trip Packet Loss
    with NLTPServer(("127.0.0.1", 0), save_dir=tmp_dir / "roam", psk=psk, loss_drop_sim=0.08) as srv_r:
        with NLTPClient(srv_r.getsockname(), psk=psk, max_tx_kbps=5_000.0, loss_drop_sim=0.08) as cli_r:
            p_before = cli_r.getsockname()[1]
            out_box = {}
            th = threading.Thread(target=lambda: out_box.setdefault("r", cli_r.send_file(f128, timeout=20.0)))
            th.start()
            time.sleep(0.08)
            p_after = cli_r.rebind_socket()[1]
            th.join(timeout=20.0)
            results["wifi_to_4g_roaming_128kb_15pct_loss"] = {
                "port_changed": p_before != p_after,
                "roam_events_detected": srv_r.engine.roam_events,
                "elapsed_ms": round(out_box["r"]["elapsed_s"] * 1000.0, 2),
                "verified_sha256": out_box["r"]["verified_sha256"],
            }

    # 4. Dual Stateful NAT Firewalls + P2P UDP Hole Punching
    nat = DualStatefulNATBox()
    try:
        with NLTPServer(("127.0.0.1", 0), save_dir=tmp_dir / "p2p", psk=psk) as srv_a:
            with NLTPClient(nat.lan_b_addr, psk=psk) as cli_b:
                t0 = time.perf_counter()
                ok_box = {"a": False, "b": False}
                ta = threading.Thread(target=lambda: ok_box.__setitem__("a", srv_a.punch_p2p(nat.lan_a_addr, timeout_s=3.0)))
                tb = threading.Thread(target=lambda: ok_box.__setitem__("b", cli_b.punch_p2p(nat.lan_b_addr, timeout_s=3.0)))
                ta.start()
                tb.start()
                ta.join()
                tb.join()
                punch_ms = (time.perf_counter() - t0) * 1000.0
                r_p2p = cli_b.send_file(f64, timeout=10.0)
                results["dual_stateful_nat_p2p_hole_punch"] = {
                    "punched_both_sides": ok_box["a"] and ok_box["b"],
                    "hole_punch_ms": round(punch_ms, 2),
                    "file_64kb_ms": round(r_p2p["elapsed_s"] * 1000.0, 2),
                    "verified_sha256": r_p2p["verified_sha256"],
                }
    finally:
        nat.close()

    # 5. Strict 4G Symmetric CGNAT Traversal via Zero-Knowledge `NLTPRelayHub` under ~19% Loss
    with NLTPRelayHub(("127.0.0.1", 0)) as hub:
        h_addr = hub.getsockname()
        with NLTPServer(("127.0.0.1", 0), save_dir=tmp_dir / "relay", psk=psk, relay_addr=h_addr, room="wan-bench", loss_drop_sim=0.10) as srv_rel:
            with NLTPClient(relay_addr=h_addr, room="wan-bench", psk=psk, loss_drop_sim=0.10) as cli_rel:
                cli_rel.punch_p2p(h_addr, timeout_s=3.0)
                rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                rtag = make_room_tag("wan-bench")
                for _ in range(10):
                    rogue.sendto(wrap_relay_packet(rtag, pack_nltp_frame(0x06, 0xDEAD, b"FAKE"), cmd=RELAY_VER_DATA), h_addr)
                rogue.close()
                r_rel = cli_rel.send_file(f64, timeout=15.0)
                results["cgnat_zero_knowledge_relay_64kb_19pct_loss"] = {
                    "elapsed_ms": round(r_rel["elapsed_s"] * 1000.0, 2),
                    "verified_sha256": r_rel["verified_sha256"],
                    "relay_forwarded_packets": hub.forwarded_packets,
                    "forged_packets_dropped": srv_rel.engine.auth_drops_rx,
                }

    # 6. Live Public Internet STUN Check (Privacy-Safe: strips raw public IP before saving/printing!)
    with NLTPClient(("127.0.0.1", 9999)) as cli_stun:
        try:
            st = cli_stun.discover_wan(timeout_s=2.5)
            results["live_public_internet_stun"] = {
                "reachable": True,
                "masked_wan_endpoint": f"xxx.xxx.xxx.xxx:{st.public_port}",
                "rtt_ms": st.rtt_ms,
                "endpoint_independent_nat": st.endpoint_independent_nat,
                "nat_summary": st.nat_summary,
                "servers_queried": st.servers_queried,
            }
        except Exception as exc:
            results["live_public_internet_stun"] = {"reachable": False, "error": str(exc)}

    out_json = ROOT / "results" / "nltp_internet_wan_verification.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
