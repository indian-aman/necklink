#!/usr/bin/env python3
"""
esp32_mesh3_v2_vs_raw_v2.py — Complete Off-Wi-Fi Interconnection & Benchmark Suite (NLTP 1.3.2)

Tests all connected ESP32 modules (COM9, COM11, COM12) in 100% off-Wi-Fi connectionless
2.4 GHz mode (wifi_sta=0, ip=0.0.0.0, Channel 11) and compares:
  1) NLTP 1.3.2 over ESP-NOW v2 (zero-wrapper "NL" frames, GF(256) Cauchy FEC,
     ChaCha20-Poly1305 AEAD + RFC 6479 anti-replay / IEEE CRC32, SHA-256 FIN_ACK,
     v1.3.2 stateless pre-auth gate + LRU peer cache + NLTP_RX_TAKEOVER_MS anti-stall)
  2) Bare-Metal Raw ESP-NOW v2 (<= 1,470 B vendor action frames, no FEC/AEAD)
"""

import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import serial


CANDIDATE_PORTS = [
    ("COM9", 0),
    ("COM11", 1),
    ("COM12", 2),
]

RATE_MODES = [
    (0, "802.11b 1M", 4096),
    (1, "802.11b 11M", 16384),
    (2, "802.11n MCS4 (39M)", 32768),
    (3, "802.11n MCS7 (65M)", 32768),
    (4, "AUTO (RSSI Adaptive)", 32768),
    (5, "Espressif LR 250K", 4096),
    (6, "Espressif LR 500K", 4096),
]

TXPWR_LEVELS = [
    (78, "+19.5 dBm (Max)"),
    (60, "+15.0 dBm"),
    (44, "+11.0 dBm"),
    (28, "+7.0 dBm"),
    (8, "+2.0 dBm (Min)"),
]

PAYLOAD_SIZES = [4, 64, 250, 1024, 1446, 4096, 16384, 32768]
LOSS_RATES = [0, 5, 10, 15, 25, 35]


class MeshNode:
    def __init__(self, port: str, expected_id: int):
        self.port = port
        self.expected_id = expected_id
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 115200
        self.ser.timeout = 0.05
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.buf = ""
        self.mac = ""
        self.node_id = expected_id

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def drain(self) -> str:
        chunks = []
        while True:
            n = self.ser.in_waiting
            data = self.ser.read(n if n > 0 else 1)
            if not data:
                break
            chunks.append(data.decode("utf-8", errors="ignore").replace("\x00", ""))
        if chunks:
            self.buf += "".join(chunks)
        return self.buf

    def clear(self):
        self.drain()
        self.buf = ""

    def send_cmd(self, cmd: str):
        self.ser.write((cmd + "\n").encode("ascii"))
        self.ser.flush()

    def cmd_wait(self, cmd: str, token: str, timeout: float = 3.0) -> str:
        self.clear()
        self.send_cmd(cmd)
        t0 = time.perf_counter()
        while (time.perf_counter() - t0) < timeout:
            txt = self.drain()
            if token in txt:
                return txt
            time.sleep(0.004)
        return self.drain()

    def stat(self) -> Dict:
        for _ in range(3):
            txt = self.cmd_wait("stat", "END_STAT", timeout=1.5)
            m = re.search(
                r"STAT node=(\d+) wifi_sta=(\d+) ip=(\S+) enow_v=(\d+) mtu=(\d+) ch=(\d+) "
                r"lr=(\d+) rate=(-?\d+) txpwr=(-?\d+)\(([-0-9.]+)dBm\) drop=(\d+) enc=(\d+) "
                r"slots=(\d+) peers=(\d+) evict=(\d+) preauth_drops=(\d+) auth_drops=(\d+) "
                r"crc_drops=(\d+) raw_forged=(\d+) relayed=(\d+) mac=([0-9a-f:]+) heap=(\d+)",
                txt,
            )
            if m:
                peers = []
                for pm in re.finditer(
                    r"PEER node=(\d+) mac=([0-9a-f:]+) rssi=(-?\d+) noise=(-?\d+) snr=(-?\d+) pkts=(\d+) age_ms=(\d+)",
                    txt,
                ):
                    peers.append(
                        {
                            "node": int(pm.group(1)),
                            "mac": pm.group(2),
                            "rssi": int(pm.group(3)),
                            "noise": int(pm.group(4)),
                            "snr": int(pm.group(5)),
                            "pkts": int(pm.group(6)),
                            "age_ms": int(pm.group(7)),
                        }
                    )
                info = {
                    "node": int(m.group(1)),
                    "wifi_sta": int(m.group(2)),
                    "ip": m.group(3),
                    "enow_v": int(m.group(4)),
                    "mtu": int(m.group(5)),
                    "ch": int(m.group(6)),
                    "lr": int(m.group(7)),
                    "rate": int(m.group(8)),
                    "txpwr_qdbm": int(m.group(9)),
                    "txpwr_dbm": float(m.group(10)),
                    "drop": int(m.group(11)),
                    "enc": int(m.group(12)),
                    "slots": int(m.group(13)),
                    "peers": int(m.group(14)),
                    "evict": int(m.group(15)),
                    "preauth_drops": int(m.group(16)),
                    "auth_drops": int(m.group(17)),
                    "crc_drops": int(m.group(18)),
                    "raw_forged": int(m.group(19)),
                    "relayed": int(m.group(20)),
                    "mac": m.group(21),
                    "heap": int(m.group(22)),
                    "neighbors": peers,
                }
                self.mac = info["mac"]
                self.node_id = info["node"]
                return info
            self.send_cmd("")
            time.sleep(0.08)
        raise RuntimeError(f"{self.port} did not return valid STAT")


def discover_nodes() -> Tuple[Dict[int, MeshNode], Dict[str, dict]]:
    active: Dict[int, MeshNode] = {}
    hw_report: Dict[str, dict] = {}
    for port, exp_id in CANDIDATE_PORTS:
        try:
            node = MeshNode(port, exp_id)
            time.sleep(0.15)
            node.send_cmd("")
            time.sleep(0.08)
            node.clear()
            st = node.stat()
            active[st["node"]] = node
            hw_report[port] = {
                "online": True,
                "node_id": st["node"],
                "mac": st["mac"],
                "wifi_sta": st["wifi_sta"],
                "ip": st["ip"],
                "enow_v": st["enow_v"],
                "mtu": st["mtu"],
                "channel": st["ch"],
                "heap": st["heap"],
            }
            print(f"[ONLINE] {port} -> Node {st['node']} MAC={st['mac']} wifi_sta={st['wifi_sta']} ip={st['ip']} enow_v={st['enow_v']} mtu={st['mtu']}")
        except Exception as exc:
            hw_report[port] = {
                "online": False,
                "expected_node_id": exp_id,
                "reason": str(exc),
            }
            print(f"[OFFLINE] {port} (Node {exp_id}) -> {exc}")
    return active, hw_report


def prime_mesh(nodes: Dict[int, MeshNode], rounds: int = 3):
    for _ in range(rounds):
        for n in nodes.values():
            n.cmd_wait("beacon", "OK beacon", timeout=0.8)
        time.sleep(0.06)
    for n in nodes.values():
        n.clear()


def run_raw_trial(
    sender: MeshNode,
    receiver: MeshNode,
    dst_mac: str,
    size: int,
    via_mac: Optional[str] = None,
    relay_node: Optional[MeshNode] = None,
    timeout: float = 3.0,
) -> dict:
    sender.clear()
    receiver.clear()
    if relay_node and relay_node is not sender and relay_node is not receiver:
        relay_node.clear()

    cmd = f"raw_send {dst_mac} {size}" + (f" {via_mac}" if via_mac else "")
    sender.send_cmd(cmd)

    t0 = time.perf_counter()
    tx_m = None
    rx_m = None
    while (time.perf_counter() - t0) < timeout:
        stxt = sender.drain()
        rtxt = receiver.drain() if receiver is not sender else stxt
        if relay_node and relay_node is not sender and relay_node is not receiver:
            relay_node.drain()
        if not tx_m:
            tx_m = re.search(
                r"RAW_TX ok=(\d+) ack=(\d+) rcvd=(\d+)/(\d+) ms=([0-9.]+) mbps=([0-9.]+) max_tx_l2=(\d+)",
                stxt,
            )
        if not rx_m:
            rx_m = re.search(
                r"RAW_RX len=(\d+)/(\d+) pkts=(\d+)/(\d+) intact=(\d+) relayed=(\d+) max_l2=(\d+) rssi=(-?\d+)",
                rtxt,
            )
        if tx_m and rx_m:
            break
        if tx_m and (time.perf_counter() - t0) > 0.25 and not rx_m:
            # Give receiver tail window to emit RAW_RX if any packet arrived
            time.sleep(0.08)
            rtxt = receiver.drain() if receiver is not sender else sender.drain()
            rx_m = re.search(
                r"RAW_RX len=(\d+)/(\d+) pkts=(\d+)/(\d+) intact=(\d+) relayed=(\d+) max_l2=(\d+) rssi=(-?\d+)",
                rtxt,
            )
            break
        time.sleep(0.003)

    if not tx_m:
        return {"ok": 0, "ack": 0, "rcvd_bytes": 0, "total_bytes": size, "ms": timeout * 1000.0, "mbps": 0.0, "max_l2": 0}

    ok = int(tx_m.group(1))
    ack = int(tx_m.group(2))
    rcvd = int(rx_m.group(1)) if rx_m else int(tx_m.group(3))
    intact = int(rx_m.group(5)) if rx_m else ok
    ms = float(tx_m.group(5))
    mbps = float(tx_m.group(6)) if intact else 0.0
    max_l2 = max(int(tx_m.group(7)), int(rx_m.group(7)) if rx_m else 0)
    rssi = int(rx_m.group(8)) if rx_m else None
    return {
        "ok": intact,
        "ack": ack,
        "rcvd_bytes": rcvd,
        "total_bytes": size,
        "ms": ms,
        "mbps": mbps,
        "raw_wire_mbps": float(tx_m.group(6)),
        "max_l2": max_l2,
        "rssi": rssi,
    }


def run_nltp_trial(
    sender: MeshNode,
    receiver: MeshNode,
    dst_mac: str,
    size: int,
    wake: bool = False,
    relay_node: Optional[MeshNode] = None,
    inject_stray_to: Optional[Tuple[MeshNode, str]] = None,
    timeout: float = 8.0,
) -> dict:
    sender.clear()
    receiver.clear()
    if relay_node and relay_node is not sender and relay_node is not receiver:
        relay_node.clear()

    verb = "wake_send" if wake else "nltp_send"
    sender.send_cmd(f"{verb} {dst_mac} {size}")

    t0 = time.perf_counter()
    tx_m = None
    rx_m = None
    injected = 0
    while (time.perf_counter() - t0) < timeout:
        stxt = sender.drain()
        rtxt = receiver.drain() if receiver is not sender else stxt
        if relay_node and relay_node is not sender and relay_node is not receiver:
            relay_node.drain()

        if inject_stray_to and injected < 5 and (time.perf_counter() - t0) >= (0.012 + injected * 0.015):
            inj_node, inj_dst = inject_stray_to
            inj_node.send_cmd(f"stray_inject {inj_dst}")
            injected += 1

        if not tx_m:
            tx_m = re.search(
                r"NLTP_TX ok=(\d+) len=(\d+) ms=([0-9.]+) mbps=([0-9.]+) max_tx_l2=(\d+) pkts_tx=(\d+)",
                stxt,
            )
        if not rx_m:
            rx_m = re.search(
                r"NLTP_RX src=([0-9a-f:]+) rssi=(-?\d+) len=(\d+) sha_ok=(\d+) file=\S* max_rx_l2=(\d+)",
                rtxt,
            )
        if tx_m and rx_m:
            break
        time.sleep(0.003)

    if not tx_m and not rx_m:
        return {"ok": 0, "sha_ok": 0, "rcvd_bytes": 0, "total_bytes": size, "ms": timeout * 1000.0, "mbps": 0.0, "max_l2": 0, "pkts_tx": 0}

    tx_ok = int(tx_m.group(1)) if tx_m else 0
    sha_ok = int(rx_m.group(4)) if rx_m else tx_ok
    rcvd = int(rx_m.group(3)) if rx_m else (size if tx_ok else 0)
    ms = float(tx_m.group(3)) if tx_m else (time.perf_counter() - t0) * 1000.0
    mbps = float(tx_m.group(4)) if (tx_m and sha_ok) else ((rcvd * 8.0 / (ms * 1000.0)) if sha_ok and ms > 0 else 0.0)
    max_l2 = max(int(tx_m.group(5)) if tx_m else 0, int(rx_m.group(5)) if rx_m else 0)
    pkts_tx = int(tx_m.group(6)) if tx_m else 0
    rssi = int(rx_m.group(2)) if rx_m else None
    return {
        "ok": 1 if (tx_ok and sha_ok and rcvd == size) else 0,
        "tx_verified": tx_ok,
        "sha_ok": sha_ok,
        "rcvd_bytes": rcvd,
        "total_bytes": size,
        "ms": ms,
        "mbps": mbps,
        "max_l2": max_l2,
        "pkts_tx": pkts_tx,
        "rssi": rssi,
        "stray_injected": injected,
    }


def set_rate_all(nodes: Dict[int, MeshNode], rate: int):
    for n in nodes.values():
        n.cmd_wait(f"rate {rate}", "OK rate=", timeout=1.5)
    time.sleep(0.18)
    prime_mesh(nodes, rounds=2)
    time.sleep(0.12)


def set_drop_all(nodes: Dict[int, MeshNode], drop: int):
    for n in nodes.values():
        n.cmd_wait(f"drop {drop}", "OK drop=", timeout=1.0)


def set_psk_all(nodes: Dict[int, MeshNode], psk_arg: str):
    for n in nodes.values():
        n.cmd_wait(f"psk {psk_arg}", "OK psk=", timeout=1.0)


def set_txpwr_all(nodes: Dict[int, MeshNode], qdbm: int):
    for n in nodes.values():
        n.cmd_wait(f"txpwr {qdbm}", "OK txpwr=", timeout=1.0)


def run_wsl_espnow_sim() -> dict:
    """Runs NLTP 1.3.2 C99 espnow_sim (3 concurrent sensors + 30 sequential sensors + forged flood)."""
    try:
        proc = subprocess.run(
            ["wsl", "bash", "-lc", "cd /mnt/d/necklink/packages/nltp/csrc && make -s test espnow-sim"],
            capture_output=True,
            text=True,
            timeout=45,
        )
        return {
            "returncode": proc.returncode,
            "passed": proc.returncode == 0,
            "output": proc.stdout.strip(),
        }
    except Exception as exc:
        return {"returncode": -1, "passed": False, "output": str(exc)}


def summarize_trials(trials: List[dict], key_ok: str = "ok") -> dict:
    n = len(trials)
    oks = [t for t in trials if t.get(key_ok)]
    return {
        "trials": n,
        "pass_count": len(oks),
        "delivery_pct": round(100.0 * len(oks) / max(1, n), 1),
        "avg_rcvd_bytes": round(statistics.mean(t["rcvd_bytes"] for t in trials), 1),
        "median_ms": round(statistics.median(t["ms"] for t in trials), 2),
        "median_mbps": round(statistics.median(t["mbps"] for t in trials), 3),
        "max_mbps": round(max(t["mbps"] for t in trials), 3),
        "max_l2": max(t.get("max_l2", 0) for t in trials),
        "raw": trials,
    }


def main():
    print("=" * 78)
    print("NLTP 1.3.2 + ESP-NOW v2  vs  RAW ESP-NOW v2 — OFF-WI-FI MESH TEST SUITE")
    print("=" * 78)

    nodes, hw_report = discover_nodes()
    if len(nodes) < 2:
        print("FATAL: Fewer than 2 online ESP32 nodes available.", file=sys.stderr)
        sys.exit(2)

    ordered_ids = sorted(nodes.keys())
    node_a = nodes[ordered_ids[0]]
    node_b = nodes[ordered_ids[-1]]
    node_mid = nodes[ordered_ids[1]] if len(ordered_ids) >= 3 else None

    # Clean baseline state
    set_drop_all(nodes, 0)
    set_txpwr_all(nodes, 78)
    set_psk_all(nodes, "necklink-1.3.2-mesh3-key")
    for n in nodes.values():
        n.cmd_wait("l2relay off", "OK l2relay=off", timeout=1.0)
        n.cmd_wait("reset_counts", "OK reset_counts", timeout=1.0)
    set_rate_all(nodes, 4)

    # Verify 100% Off-Wi-Fi status & L2 neighbor table
    prime_mesh(nodes, rounds=4)
    mesh_topology = {}
    for nid, n in nodes.items():
        st = n.stat()
        assert st["wifi_sta"] == 0 and st["ip"] == "0.0.0.0", f"Node {nid} is connected to Wi-Fi!"
        assert st["enow_v"] == 2 and st["mtu"] == 1470, f"Node {nid} not running ESP-NOW v2!"
        mesh_topology[f"node_{nid}"] = st

    results = {
        "repo_version": "NLTP 1.3.2 (commit 7f05c5a)",
        "wifi_router_connected": False,
        "hardware": hw_report,
        "mesh_topology": mesh_topology,
        "cat1_txpwr_sweep": [],
        "cat2_payload_sweep": [],
        "cat3_phy_rates": [],
        "cat4_packet_loss": [],
        "cat5_two_hop_relay": [],
        "cat6_stress_and_security": {},
        "c99_3node_30sensor_sim": {},
    }

    # If all 3 physical nodes are online, also test all 3 pairwise links first!
    if len(ordered_ids) >= 3:
        pairwise = []
        pairs = [(ordered_ids[0], ordered_ids[1]), (ordered_ids[1], ordered_ids[2]), (ordered_ids[0], ordered_ids[2])]
        for s_id, d_id in pairs:
            s_n, d_n = nodes[s_id], nodes[d_id]
            rt = run_raw_trial(s_n, d_n, d_n.mac, 16384)
            nt = run_nltp_trial(s_n, d_n, d_n.mac, 16384)
            pairwise.append({"link": f"node{s_id}->node{d_id}", "raw_v2": rt, "nltp_v2": nt})
        results["pairwise_3node_links"] = pairwise

    # =========================================================================
    # Category 1: TX Power / Link-Budget Sweep (+19.5 dBm down to +2.0 dBm)
    # =========================================================================
    print("\n[Cat 1] Running TX Power / Link-Budget Sweep (+19.5 dBm -> +2.0 dBm)...")
    for qdbm, label in TXPWR_LEVELS:
        set_txpwr_all(nodes, qdbm)
        prime_mesh(nodes, rounds=3)
        st_b = node_b.stat()
        peer_rssi = next((p["rssi"] for p in st_b["neighbors"] if p["mac"] == node_a.mac), None)
        peer_snr = next((p["snr"] for p in st_b["neighbors"] if p["mac"] == node_a.mac), None)

        raw_16k = summarize_trials([run_raw_trial(node_a, node_b, node_b.mac, 16384) for _ in range(3)])
        nltp_16k = summarize_trials([run_nltp_trial(node_a, node_b, node_b.mac, 16384) for _ in range(3)])
        row = {
            "qdbm": qdbm,
            "label": label,
            "rssi_dbm": peer_rssi,
            "snr_db": peer_snr,
            "raw_v2_16k": raw_16k,
            "nltp_v2_16k": nltp_16k,
        }
        results["cat1_txpwr_sweep"].append(row)
        print(
            f"  {label:<18} RSSI={peer_rssi}dBm SNR={peer_snr}dB | "
            f"Raw v2: {raw_16k['median_mbps']:.3f} Mbps ({raw_16k['delivery_pct']}%) | "
            f"NLTP v2: {nltp_16k['median_mbps']:.3f} Mbps ({nltp_16k['delivery_pct']}%)"
        )
    set_txpwr_all(nodes, 78)
    prime_mesh(nodes, rounds=2)

    # =========================================================================
    # Category 2: Payload Size Scaling (4 B -> 32 KB: Raw v2 vs NLTP AEAD vs NLTP Plaintext)
    # =========================================================================
    print("\n[Cat 2] Running Payload Size Sweep (4 B -> 32 KB)...")
    for sz in PAYLOAD_SIZES:
        set_psk_all(nodes, "necklink-1.3.2-mesh3-key")
        raw_s = summarize_trials([run_raw_trial(node_a, node_b, node_b.mac, sz) for _ in range(3)])
        nltp_enc = summarize_trials([run_nltp_trial(node_a, node_b, node_b.mac, sz) for _ in range(3)])

        set_psk_all(nodes, "off")
        nltp_pln = summarize_trials([run_nltp_trial(node_a, node_b, node_b.mac, sz) for _ in range(3)])
        set_psk_all(nodes, "necklink-1.3.2-mesh3-key")

        row = {
            "bytes": sz,
            "raw_v2": raw_s,
            "nltp_v2_aead": nltp_enc,
            "nltp_v2_crc32": nltp_pln,
        }
        results["cat2_payload_sweep"].append(row)
        print(
            f"  {sz:>6} B | Raw v2: {raw_s['median_ms']:>6.2f} ms ({raw_s['median_mbps']:.3f} Mbps, L2={raw_s['max_l2']}B) | "
            f"NLTP AEAD: {nltp_enc['median_ms']:>6.2f} ms ({nltp_enc['median_mbps']:.3f} Mbps, L2={nltp_enc['max_l2']}B) | "
            f"NLTP CRC32: {nltp_pln['median_ms']:>6.2f} ms ({nltp_pln['median_mbps']:.3f} Mbps, L2={nltp_pln['max_l2']}B)"
        )

    # =========================================================================
    # Category 3: All 7 PHY Rate Modes (1M, 11M, MCS4, MCS7, AUTO, LR 250K, LR 500K)
    # =========================================================================
    print("\n[Cat 3] Running All 7 Wi-Fi / ESP-NOW PHY Rate Modes...")
    for r_code, r_name, bulk_sz in RATE_MODES:
        set_rate_all(nodes, r_code)
        raw_4b = summarize_trials([run_raw_trial(node_a, node_b, node_b.mac, 4, timeout=3.5) for _ in range(3)])
        nltp_4b = summarize_trials([run_nltp_trial(node_a, node_b, node_b.mac, 4, timeout=5.0) for _ in range(3)])
        raw_bulk = summarize_trials([run_raw_trial(node_a, node_b, node_b.mac, bulk_sz, timeout=4.5) for _ in range(3)])
        nltp_bulk = summarize_trials([run_nltp_trial(node_a, node_b, node_b.mac, bulk_sz, timeout=12.0) for _ in range(3)])
        row = {
            "rate_code": r_code,
            "rate_name": r_name,
            "bulk_bytes": bulk_sz,
            "raw_v2_4b_ms": raw_4b["median_ms"],
            "nltp_v2_4b_ms": nltp_4b["median_ms"],
            "raw_v2_bulk": raw_bulk,
            "nltp_v2_bulk": nltp_bulk,
        }
        results["cat3_phy_rates"].append(row)
        print(
            f"  {r_name:<22} ({bulk_sz//1024}KB) | "
            f"Raw v2: 4B={raw_4b['median_ms']:.2f}ms, Bulk={raw_bulk['median_mbps']:.3f}Mbps ({raw_bulk['delivery_pct']}%) | "
            f"NLTP v2: 4B={nltp_4b['median_ms']:.2f}ms, Bulk={nltp_bulk['median_mbps']:.3f}Mbps ({nltp_bulk['delivery_pct']}%)"
        )
    set_rate_all(nodes, 4)

    # =========================================================================
    # Category 4: Packet-Loss Resilience Sweep (0% -> 35% RX Drop on 16 KB)
    # =========================================================================
    print("\n[Cat 4] Running Packet-Loss Resilience Sweep (0% -> 35% on 16 KB)...")
    for loss in LOSS_RATES:
        node_b.cmd_wait(f"drop {loss}", "OK drop=", timeout=1.0)
        node_a.cmd_wait("drop 0", "OK drop=", timeout=1.0)

        raw_l = summarize_trials([run_raw_trial(node_a, node_b, node_b.mac, 16384) for _ in range(3)])
        nltp_l = summarize_trials([run_nltp_trial(node_a, node_b, node_b.mac, 16384, timeout=10.0) for _ in range(3)])
        row = {
            "loss_pct": loss,
            "raw_v2": raw_l,
            "nltp_v2": nltp_l,
        }
        results["cat4_packet_loss"].append(row)
        print(
            f"  Loss {loss:>2}% | Raw v2: intact={raw_l['delivery_pct']:>5.1f}% (avg {raw_l['avg_rcvd_bytes']:.0f}/16384 B) | "
            f"NLTP v2: SHA-256={nltp_l['delivery_pct']:>5.1f}% ({nltp_l['median_ms']:.2f} ms, {nltp_l['median_mbps']:.3f} Mbps)"
        )
    set_drop_all(nodes, 0)

    # =========================================================================
    # Category 5: 2-Hop L2 Multi-Room Wireless Relay (0% and 15% Loss on 16 KB)
    # =========================================================================
    print("\n[Cat 5] Running 2-Hop L2 Multi-Room Wireless Relay (0% and 15% Loss)...")
    if node_mid is not None:
        # 3 physical nodes: Node A -> Node Mid (Relay) -> Node B
        relay_mode = f"3-Board Linear Relay ({node_a.port} -> {node_mid.port} -> {node_b.port})"
        relay_dev = node_mid
        dst_dev = node_b
        for loss in [0, 15]:
            dst_dev.cmd_wait(f"drop {loss}", "OK drop=", timeout=1.0)
            relay_dev.cmd_wait("reset_counts", "OK reset_counts", timeout=1.0)
            relay_dev.cmd_wait("l2relay off", "OK l2relay=off", timeout=1.0)
            raw_r = summarize_trials(
                [run_raw_trial(node_a, dst_dev, dst_dev.mac, 16384, via_mac=relay_dev.mac, relay_node=relay_dev) for _ in range(3)]
            )
            raw_relayed = relay_dev.stat()["relayed"]

            relay_dev.cmd_wait("reset_counts", "OK reset_counts", timeout=1.0)
            relay_dev.cmd_wait(f"l2relay {node_a.mac} {dst_dev.mac}", "OK l2relay=on", timeout=1.0)
            nltp_r = summarize_trials(
                [run_nltp_trial(node_a, dst_dev, relay_dev.mac, 16384, relay_node=relay_dev, timeout=10.0) for _ in range(3)]
            )
            nltp_relayed = relay_dev.stat()["relayed"]
            relay_dev.cmd_wait("l2relay off", "OK l2relay=off", timeout=1.0)
            set_drop_all(nodes, 0)
            results["cat5_two_hop_relay"].append(
                {
                    "topology": relay_mode,
                    "loss_pct": loss,
                    "raw_v2": raw_r,
                    "raw_relayed_frames": raw_relayed,
                    "nltp_v2": nltp_r,
                    "nltp_relayed_frames": nltp_relayed,
                }
            )
            print(
                f"  2-Hop ({loss}% loss) | Raw v2: intact={raw_r['delivery_pct']}% ({raw_r['median_ms']:.2f}ms, relayed={raw_relayed}) | "
                f"NLTP v2: SHA-256={nltp_r['delivery_pct']}% ({nltp_r['median_ms']:.2f}ms, {nltp_r['median_mbps']:.3f}Mbps, relayed={nltp_relayed})"
            )
    else:
        # 2 physical nodes online: Real Over-the-Air 2-Hop RF Hairpin Relay (COM9 -> COM12 [Hop 1] -> COM9 [Hop 2])
        relay_mode = f"2-Hop Over-the-Air Hairpin Relay ({node_a.port} -> {node_b.port} [RF Hop 1] -> {node_a.port} [RF Hop 2])"
        for loss in [0, 15]:
            node_a.cmd_wait(f"drop {loss}", "OK drop=", timeout=1.0)
            node_b.cmd_wait("drop 0", "OK drop=", timeout=1.0)

            node_b.cmd_wait("reset_counts", "OK reset_counts", timeout=1.0)
            node_b.cmd_wait("l2relay off", "OK l2relay=off", timeout=1.0)
            raw_r = summarize_trials(
                [run_raw_trial(node_a, node_a, node_a.mac, 16384, via_mac=node_b.mac, relay_node=node_b) for _ in range(3)]
            )
            raw_relayed = node_b.stat()["relayed"]

            node_b.cmd_wait("reset_counts", "OK reset_counts", timeout=1.0)
            node_b.cmd_wait(f"l2relay {node_a.mac} {node_a.mac}", "OK l2relay=on", timeout=1.0)
            nltp_r = summarize_trials(
                [run_nltp_trial(node_a, node_a, node_b.mac, 16384, relay_node=node_b, timeout=10.0) for _ in range(3)]
            )
            nltp_relayed = node_b.stat()["relayed"]
            node_b.cmd_wait("l2relay off", "OK l2relay=off", timeout=1.0)
            set_drop_all(nodes, 0)
            results["cat5_two_hop_relay"].append(
                {
                    "topology": relay_mode,
                    "loss_pct": loss,
                    "raw_v2": raw_r,
                    "raw_relayed_frames": raw_relayed,
                    "nltp_v2": nltp_r,
                    "nltp_relayed_frames": nltp_relayed,
                }
            )
            print(
                f"  2-Hop RF ({loss}% loss) | Raw v2: intact={raw_r['delivery_pct']}% ({raw_r['median_ms']:.2f}ms, relayed={raw_relayed}) | "
                f"NLTP v2: SHA-256={nltp_r['delivery_pct']}% ({nltp_r['median_ms']:.2f}ms, {nltp_r['median_mbps']:.3f}Mbps, relayed={nltp_relayed})"
            )

    # Clear slots after loopback relay so node_a/node_b start clean for Cat 6
    node_a.cmd_wait("clear_slots", "OK clear_slots", timeout=1.0)
    node_b.cmd_wait("clear_slots", "OK clear_slots", timeout=1.0)
    prime_mesh(nodes, rounds=2)

    # =========================================================================
    # Category 6: Stress, Simultaneous Full-Duplex Collisions, & v1.3.2 Security
    # =========================================================================
    print("\n[Cat 6A] Simultaneous Full-Duplex 32 KB + 32 KB Bidirectional Collisions (0% and 10% Loss)...")
    duplex_summary = {}
    for loss in [0, 10]:
        set_drop_all(nodes, loss)
        # 1) Raw ESP-NOW v2 Full-Duplex (5 rounds)
        raw_duplex_rounds = []
        for _ in range(5):
            node_a.clear()
            node_b.clear()
            t0 = time.perf_counter()
            node_a.send_cmd(f"raw_async {node_b.mac} 32768")
            node_b.send_cmd(f"raw_async {node_a.mac} 32768")
            a_rx = b_rx = a_tx = b_tx = None
            while (time.perf_counter() - t0) < 2.0:
                ta = node_a.drain()
                tb = node_b.drain()
                if not a_tx:
                    a_tx = re.search(r"RAW_TX ok=(\d+) ack=(\d+) rcvd=(\d+)/(\d+) ms=([0-9.]+)", ta)
                if not b_tx:
                    b_tx = re.search(r"RAW_TX ok=(\d+) ack=(\d+) rcvd=(\d+)/(\d+) ms=([0-9.]+)", tb)
                if not a_rx:
                    a_rx = re.search(r"RAW_RX len=(\d+)/(\d+) pkts=(\d+)/(\d+) intact=(\d+)", ta)
                if not b_rx:
                    b_rx = re.search(r"RAW_RX len=(\d+)/(\d+) pkts=(\d+)/(\d+) intact=(\d+)", tb)
                if a_tx and b_tx and a_rx and b_rx:
                    break
                time.sleep(0.004)
            dt_ms = (time.perf_counter() - t0) * 1000.0
            a_ok = int(a_rx.group(5)) if a_rx else 0
            b_ok = int(b_rx.group(5)) if b_rx else 0
            both_ok = 1 if (a_ok and b_ok) else 0
            raw_duplex_rounds.append(
                {
                    "both_intact": both_ok,
                    "a_rcvd": int(a_rx.group(1)) if a_rx else 0,
                    "b_rcvd": int(b_rx.group(1)) if b_rx else 0,
                    "wall_ms": round(dt_ms, 2),
                    "combined_mbps": round((65536 * 8.0) / (dt_ms * 1000.0), 3) if both_ok else 0.0,
                }
            )
            time.sleep(0.05)

        # 2) NLTP + ESP-NOW v2 Full-Duplex (5 rounds)
        nltp_duplex_rounds = []
        for _ in range(5):
            node_a.clear()
            node_b.clear()
            t0 = time.perf_counter()
            node_a.send_cmd(f"nltp_send {node_b.mac} 32768")
            node_b.send_cmd(f"nltp_send {node_a.mac} 32768")
            a_rx = b_rx = a_tx = b_tx = None
            while (time.perf_counter() - t0) < 8.0:
                ta = node_a.drain()
                tb = node_b.drain()
                if not a_tx:
                    a_tx = re.search(r"NLTP_TX ok=(\d+) len=(\d+) ms=([0-9.]+)", ta)
                if not b_tx:
                    b_tx = re.search(r"NLTP_TX ok=(\d+) len=(\d+) ms=([0-9.]+)", tb)
                if not a_rx:
                    a_rx = re.search(r"NLTP_RX src=\S+ rssi=-?\d+ len=(\d+) sha_ok=(\d+)", ta)
                if not b_rx:
                    b_rx = re.search(r"NLTP_RX src=\S+ rssi=-?\d+ len=(\d+) sha_ok=(\d+)", tb)
                if a_tx and b_tx and a_rx and b_rx:
                    break
                time.sleep(0.004)
            dt_ms = max(
                float(a_tx.group(3)) if a_tx else 8000.0,
                float(b_tx.group(3)) if b_tx else 8000.0,
            )
            a_ok = int(a_rx.group(2)) if a_rx else 0
            b_ok = int(b_rx.group(2)) if b_rx else 0
            both_ok = 1 if (a_ok and b_ok) else 0
            nltp_duplex_rounds.append(
                {
                    "both_sha_ok": both_ok,
                    "wall_ms": round(dt_ms, 2),
                    "combined_mbps": round((65536 * 8.0) / (dt_ms * 1000.0), 3) if both_ok else 0.0,
                }
            )
            time.sleep(0.05)

        duplex_summary[f"loss_{loss}pct"] = {
            "raw_v2_both_intact_pct": round(100.0 * sum(r["both_intact"] for r in raw_duplex_rounds) / 5.0, 1),
            "raw_v2_median_mbps": round(statistics.median(r["combined_mbps"] for r in raw_duplex_rounds), 3),
            "raw_v2_rounds": raw_duplex_rounds,
            "nltp_v2_both_sha_ok_pct": round(100.0 * sum(r["both_sha_ok"] for r in nltp_duplex_rounds) / 5.0, 1),
            "nltp_v2_median_mbps": round(statistics.median(r["combined_mbps"] for r in nltp_duplex_rounds), 3),
            "nltp_v2_median_ms": round(statistics.median(r["wall_ms"] for r in nltp_duplex_rounds), 2),
            "nltp_v2_rounds": nltp_duplex_rounds,
        }
        print(
            f"  Duplex (loss={loss}%) | Raw v2 both-intact={duplex_summary[f'loss_{loss}pct']['raw_v2_both_intact_pct']}% "
            f"({duplex_summary[f'loss_{loss}pct']['raw_v2_median_mbps']:.3f} Mbps) | "
            f"NLTP v2 both-SHA256={duplex_summary[f'loss_{loss}pct']['nltp_v2_both_sha_ok_pct']}% "
            f"({duplex_summary[f'loss_{loss}pct']['nltp_v2_median_mbps']:.3f} Mbps)"
        )
    set_drop_all(nodes, 0)
    results["cat6_stress_and_security"]["full_duplex_32k_x2"] = duplex_summary

    # 6B: 50-Stream Zero-Pause Rapid-Fire Soak + Heap Audit
    print("\n[Cat 6B] 50-Stream Rapid-Fire Soak (50 x 8 KB = 400 KB) + Heap Leak Audit...")
    # Warm slot allocation once before recording baseline heap so lazy rx_buf allocation is already live
    _ = run_nltp_trial(node_a, node_b, node_b.mac, 8192)
    heap_a0 = node_a.stat()["heap"]
    heap_b0 = node_b.stat()["heap"]
    soak_ok = 0
    soak_ms = []
    t_soak0 = time.perf_counter()
    for _ in range(50):
        tr = run_nltp_trial(node_a, node_b, node_b.mac, 8192, timeout=5.0)
        if tr["ok"]:
            soak_ok += 1
            soak_ms.append(tr["ms"])
    soak_wall_s = time.perf_counter() - t_soak0
    heap_a1 = node_a.stat()["heap"]
    heap_b1 = node_b.stat()["heap"]
    soak_res = {
        "streams": 50,
        "bytes_per_stream": 8192,
        "total_bytes": 50 * 8192,
        "verified_streams": soak_ok,
        "wall_seconds": round(soak_wall_s, 2),
        "median_stream_ms": round(statistics.median(soak_ms), 2) if soak_ms else 0.0,
        "heap_tx_before": heap_a0,
        "heap_tx_after": heap_a1,
        "heap_tx_delta": heap_a1 - heap_a0,
        "heap_rx_before": heap_b0,
        "heap_rx_after": heap_b1,
        "heap_rx_delta": heap_b1 - heap_b0,
    }
    results["cat6_stress_and_security"]["soak_50x8kb"] = soak_res
    print(
        f"  Soak: {soak_ok}/50 SHA-256 verified in {soak_wall_s:.2f}s (median {soak_res['median_stream_ms']:.2f}ms) | "
        f"Heap delta TX={soak_res['heap_tx_delta']} B, RX={soak_res['heap_rx_delta']} B"
    )

    # 6C: 20x Deep-Sleep Cold-Wake conn_id Turnover
    print("\n[Cat 6C] 20x Deep-Sleep Cold-Wake Random conn_id Turnover (20 x 2 KB)...")
    wake_ok = 0
    wake_ms = []
    for _ in range(20):
        tr = run_nltp_trial(node_a, node_b, node_b.mac, 2048, wake=True, timeout=4.0)
        if tr["ok"]:
            wake_ok += 1
            wake_ms.append(tr["ms"])
    wake_res = {
        "wakes": 20,
        "verified_wakes": wake_ok,
        "median_ms": round(statistics.median(wake_ms), 2) if wake_ms else 0.0,
    }
    results["cat6_stress_and_security"]["deep_sleep_wakes_20x"] = wake_res
    print(f"  Cold Wakes: {wake_ok}/20 SHA-256 verified (0 ms handshake, median {wake_res['median_ms']:.2f} ms)")

    # 6D: v1.3.2 (commit 3775512) Stray-conn_id Mid-Stream Injection Anti-Stall Test
    print("\n[Cat 6D] v1.3.2 Mid-Stream Stray-conn_id Anti-Stall Injection Test...")
    set_rate_all(nodes, 0)  # 802.11b 1M so 16 KB stream lasts ~180 ms and 5 stray shards arrive mid-flight!
    stray_trials = []
    inj_source = node_mid if node_mid is not None else node_a
    for _ in range(3):
        tr = run_nltp_trial(
            node_a,
            node_b,
            node_b.mac,
            16384,
            inject_stray_to=(inj_source, node_b.mac),
            timeout=6.0,
        )
        stray_trials.append(tr)
    set_rate_all(nodes, 4)
    stray_res = {
        "trials": 3,
        "stream_survived_pct": round(100.0 * sum(t["ok"] for t in stray_trials) / 3.0, 1),
        "stray_shards_injected_per_trial": stray_trials[0]["stray_injected"],
        "median_ms_at_1m": round(statistics.median(t["ms"] for t in stray_trials), 2),
        "raw": stray_trials,
    }
    results["cat6_stress_and_security"]["v132_stray_conn_id_antistall"] = stray_res
    print(
        f"  Mid-Stream Stray Injection: {stray_res['stream_survived_pct']}% survived with SHA-256 intact "
        f"({stray_res['stray_shards_injected_per_trial']} authenticated stray-conn_id shards injected mid-flight)"
    )

    # 6E: v1.3.2 (commit ff2681f) Forged / Spoofed / Replay Fuzz Flood (200 Adversarial Frames)
    print("\n[Cat 6E] v1.3.2 Stateless Pre-Auth Gate + AEAD/Replay Fuzz Flood (200 Frames)...")
    # Phase 1: Unknown sender (clear_slots on node_b first -> tests stateless nltp_espnow_frame_ok!)
    node_b.cmd_wait("clear_slots", "OK clear_slots", timeout=1.0)
    node_b.cmd_wait("reset_counts", "OK reset_counts", timeout=1.0)
    node_a.cmd_wait(f"fuzz_unknown {node_b.mac} 100", "OK fuzz_sent=", timeout=3.0)
    time.sleep(0.15)
    st_unk = node_b.stat()

    # Phase 2: Known active slot (establish valid stream first, then flood 100 forged/tampered/replayed frames)
    warmup = run_nltp_trial(node_a, node_b, node_b.mac, 4096)
    node_b.cmd_wait("reset_counts", "OK reset_counts", timeout=1.0)
    node_a.cmd_wait(f"fuzz {node_b.mac} 100", "OK fuzz_sent=", timeout=3.0)
    time.sleep(0.15)
    st_known = node_b.stat()
    recovery = run_nltp_trial(node_a, node_b, node_b.mac, 16384)

    fuzz_res = {
        "unknown_sender_100_frames": {
            "raw_v2_forged_sent": 20,
            "raw_v2_forged_accepted": st_unk["raw_forged"],
            "nltp_forged_sent": 80,
            "nltp_stateless_preauth_drops": st_unk["preauth_drops"],
            "nltp_slots_allocated": st_unk["slots"],
        },
        "known_slot_100_frames": {
            "raw_v2_forged_sent": 20,
            "raw_v2_forged_accepted": st_known["raw_forged"],
            "nltp_forged_and_replay_sent": 80,
            "nltp_endpoint_auth_drops": st_known["auth_drops"],
            "post_flood_16kb_sha_ok": recovery["ok"],
            "post_flood_16kb_mbps": recovery["mbps"],
        },
    }
    results["cat6_stress_and_security"]["v132_adversarial_fuzz_200"] = fuzz_res
    print(
        f"  Unknown-Peer Flood (100 frames): Raw v2 accepted {st_unk['raw_forged']}/20 forged! | "
        f"NLTP v1.3.2 blocked {st_unk['preauth_drops']}/80 pre-auth with slots={st_unk['slots']} allocated!"
    )
    print(
        f"  Known-Slot Flood   (100 frames): Raw v2 accepted {st_known['raw_forged']}/20 forged! | "
        f"NLTP v1.3.2 blocked {st_known['auth_drops']}/80 (AEAD+RFC6479 replay), post-flood 16KB={recovery['mbps']:.3f} Mbps (ok={recovery['ok']})"
    )

    # Also run C99 3-node concurrent + 30-sensor sequential espnow_sim from updated repo
    print("\n[Cat 7] Verifying NLTP 1.3.2 C99 3-Node Concurrent + 30-Sensor LRU Hub Simulation...")
    sim_res = run_wsl_espnow_sim()
    results["c99_3node_30sensor_sim"] = sim_res
    print(f"  C99 espnow_sim passed={sim_res['passed']}")

    for n in nodes.values():
        n.close()

    out_dir = Path(r"D:\necklink\packages\nltp\results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "esp32_mesh3_v2_vs_raw_v2.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nSaved complete JSON telemetry -> {out_path}")


if __name__ == "__main__":
    main()
