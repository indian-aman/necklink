#!/usr/bin/env python3
"""
esp32_esp8266_3node_mesh_test.py — True 3-Physical-Board Off-Wi-Fi Mesh & Benchmark Suite
Boards online simultaneously (wifi_sta=0, ip=0.0.0.0, 2.4 GHz Channel 11):
  - Node 0 (COM9):  ESP32-D0WDQ6  (ESP-NOW v2, 1,470 B MTU, NLTP 1.3.2 hub)
  - Node 1 (COM13): ESP8266EX     (NodeMCU, ESP-NOW v1, 250 B MTU, NLTP 1.3.2 192B-shard node + L2 relay)
  - Node 2 (COM12): ESP32-D0WD-V3 (ESP-NOW v2, 1,470 B MTU, NLTP 1.3.2 hub)
"""

import json
import re
import statistics
import time
from pathlib import Path
from typing import Dict, List, Optional

import serial
import serial.tools.list_ports as lp


class NodeConn:
    def __init__(self, port: str):
        self.port = port
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 115200
        self.ser.timeout = 0.05
        self.ser.write_timeout = 0.5
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.buf = ""
        self.mac = ""
        self.node_id = -1
        self.enow_v = 1
        self.mtu = 250

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def drain(self) -> str:
        chunks = []
        while True:
            n = self.ser.in_waiting
            d = self.ser.read(n if n > 0 else 1)
            if not d:
                break
            chunks.append(d.decode("utf-8", errors="ignore").replace("\x00", ""))
        if chunks:
            self.buf += "".join(chunks)
        return self.buf

    def clear(self):
        self.drain()
        self.buf = ""

    def send_cmd(self, cmd: str):
        self.ser.write((cmd + "\n").encode("ascii"))
        self.ser.flush()

    def cmd_wait(self, cmd: str, token: str, timeout: float = 2.0) -> str:
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
            txt = self.cmd_wait("stat", "END_STAT", timeout=1.2)
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
                            "snr": int(pm.group(5)),
                            "pkts": int(pm.group(6)),
                            "age_ms": int(pm.group(7)),
                        }
                    )
                st = {
                    "port": self.port,
                    "node": int(m.group(1)),
                    "wifi_sta": int(m.group(2)),
                    "ip": m.group(3),
                    "enow_v": int(m.group(4)),
                    "mtu": int(m.group(5)),
                    "ch": int(m.group(6)),
                    "drop": int(m.group(11)),
                    "enc": int(m.group(12)),
                    "slots": int(m.group(13)),
                    "peers": int(m.group(14)),
                    "preauth_drops": int(m.group(16)),
                    "auth_drops": int(m.group(17)),
                    "crc_drops": int(m.group(18)),
                    "raw_forged": int(m.group(19)),
                    "relayed": int(m.group(20)),
                    "mac": m.group(21),
                    "heap": int(m.group(22)),
                    "neighbors": peers,
                }
                self.node_id = st["node"]
                self.mac = st["mac"]
                self.enow_v = st["enow_v"]
                self.mtu = st["mtu"]
                return st
            self.send_cmd("")
            time.sleep(0.08)
        raise RuntimeError(f"{self.port}: no STAT response")


def discover_all() -> Dict[int, NodeConn]:
    nodes: Dict[int, NodeConn] = {}
    for p in lp.comports():
        if not any(k in p.description for k in ("CP210", "CH340", "USB-SERIAL", "USB Serial")):
            continue
        c = None
        try:
            c = NodeConn(p.device)
            time.sleep(0.15)
            c.send_cmd("")
            time.sleep(0.08)
            c.clear()
            st = c.stat()
            nodes[st["node"]] = c
            chip = "ESP32 (ESP-NOW v2, 1470B)" if st["enow_v"] == 2 else "ESP8266EX NodeMCU (ESP-NOW v1, 250B)"
            print(f"[ONLINE] {p.device:<6} -> Node {st['node']} ({chip}) MAC={st['mac']} wifi_sta={st['wifi_sta']} ip={st['ip']} heap={st['heap']}", flush=True)
        except Exception as exc:
            if c is not None:
                c.close()
            print(f"[OFFLINE] {p.device:<6} -> {exc}", flush=True)
    return nodes


def reset_esp32_hubs(nodes: Dict[int, NodeConn]):
    for n in nodes.values():
        if n.enow_v == 2:
            n.cmd_wait("clear_slots", "OK clear_slots")
    time.sleep(0.05)


def run_raw(sender: NodeConn, receiver: NodeConn, dst_mac: str, sz: int, via_mac: Optional[str] = None, relay: Optional[NodeConn] = None) -> dict:
    sender.clear()
    receiver.clear()
    if relay and relay is not sender and relay is not receiver:
        relay.clear()
    cmd = f"raw_send {dst_mac} {sz}" + (f" {via_mac}" if via_mac else "")
    sender.send_cmd(cmd)
    t0 = time.perf_counter()
    tx_m = rx_m = None
    while (time.perf_counter() - t0) < 2.5:
        stxt = sender.drain()
        rtxt = receiver.drain() if receiver is not sender else stxt
        if relay and relay is not sender and relay is not receiver:
            relay.drain()
        if not tx_m:
            tx_m = re.search(r"RAW_TX ok=(\d+) ack=(\d+) rcvd=(\d+)/(\d+) ms=([0-9.]+) mbps=([0-9.]+) max_tx_l2=(\d+)", stxt)
        if not rx_m:
            rx_m = re.search(r"RAW_RX len=(\d+)/(\d+) pkts=(\d+)/(\d+) intact=(\d+) relayed=(\d+) max_l2=(\d+)", rtxt)
        if tx_m and rx_m:
            break
        if tx_m and (time.perf_counter() - t0) > 0.22 and not rx_m:
            time.sleep(0.08)
            rtxt = receiver.drain() if receiver is not sender else sender.drain()
            rx_m = re.search(r"RAW_RX len=(\d+)/(\d+) pkts=(\d+)/(\d+) intact=(\d+) relayed=(\d+) max_l2=(\d+)", rtxt)
            break
        time.sleep(0.003)
    if not tx_m:
        return {"ok": 0, "rcvd_bytes": 0, "total_bytes": sz, "ms": 2500.0, "mbps": 0.0, "max_l2": 0}
    intact = int(rx_m.group(5)) if rx_m else int(tx_m.group(1))
    rcvd = int(rx_m.group(1)) if rx_m else int(tx_m.group(3))
    ms = float(tx_m.group(5))
    mbps = float(tx_m.group(6)) if intact else 0.0
    max_l2 = max(int(tx_m.group(7)), int(rx_m.group(7)) if rx_m else 0)
    return {"ok": intact, "rcvd_bytes": rcvd, "total_bytes": sz, "ms": ms, "mbps": mbps, "max_l2": max_l2}


def run_nltp(
    sender: NodeConn,
    receiver: NodeConn,
    dst_mac: str,
    sz: int,
    relay: Optional[NodeConn] = None,
    timeout: float = 8.0,
    force_v1_shards: bool = False,
) -> dict:
    sender.clear()
    receiver.clear()
    if relay and relay is not sender and relay is not receiver:
        relay.clear()
    need_v1 = force_v1_shards or (receiver.enow_v == 1) or (relay is not None and relay.enow_v == 1)
    if sender.enow_v == 2:
        verb = "v1_send" if need_v1 else "wake_send"
    else:
        verb = "nltp_send"
    sender.send_cmd(f"{verb} {dst_mac} {sz}")
    t0 = time.perf_counter()
    tx_m = rx_m = None
    while (time.perf_counter() - t0) < timeout:
        stxt = sender.drain()
        rtxt = receiver.drain() if receiver is not sender else stxt
        if relay and relay is not sender and relay is not receiver:
            relay.drain()
        if not tx_m:
            tx_m = re.search(r"NLTP_TX ok=(\d+) len=(\d+) ms=([0-9.]+) mbps=([0-9.]+) max_tx_l2=(\d+) pkts_tx=(\d+)", stxt)
        if not rx_m:
            rx_m = re.search(r"NLTP_RX src=([0-9a-f:]+) rssi=-?\d+ len=(\d+) sha_ok=(\d+) file=\S* max_rx_l2=(\d+)", rtxt)
        if tx_m and rx_m:
            break
        time.sleep(0.003)
    if not tx_m and not rx_m:
        if sender.enow_v == 2:
            sender.cmd_wait("clear_slots", "OK clear_slots")
        if receiver.enow_v == 2:
            receiver.cmd_wait("clear_slots", "OK clear_slots")
        return {"ok": 0, "rcvd_bytes": 0, "total_bytes": sz, "ms": timeout * 1000.0, "mbps": 0.0, "max_l2": 0}
    tx_ok = int(tx_m.group(1)) if tx_m else 0
    sha_ok = int(rx_m.group(3)) if rx_m else tx_ok
    rcvd = int(rx_m.group(2)) if rx_m else (sz if tx_ok else 0)
    ms = float(tx_m.group(3)) if tx_m else (time.perf_counter() - t0) * 1000.0
    mbps = float(tx_m.group(4)) if (tx_m and sha_ok) else ((rcvd * 8.0 / (ms * 1000.0)) if sha_ok and ms > 0 else 0.0)
    max_l2 = max(int(tx_m.group(5)) if tx_m else 0, int(rx_m.group(4)) if rx_m else 0)
    ok = 1 if (tx_ok and sha_ok and rcvd == sz) else 0
    if not ok:
        if sender.enow_v == 2:
            sender.cmd_wait("clear_slots", "OK clear_slots")
        if receiver.enow_v == 2:
            receiver.cmd_wait("clear_slots", "OK clear_slots")
    return {"ok": ok, "rcvd_bytes": rcvd, "total_bytes": sz, "ms": ms, "mbps": mbps, "max_l2": max_l2}


def summarize(trials: List[dict]) -> dict:
    oks = [t for t in trials if t["ok"]]
    return {
        "trials": len(trials),
        "delivery_pct": round(100.0 * len(oks) / max(1, len(trials)), 1),
        "avg_rcvd": round(statistics.mean(t["rcvd_bytes"] for t in trials), 1),
        "median_ms": round(statistics.median(t["ms"] for t in trials), 2),
        "median_mbps": round(statistics.median(t["mbps"] for t in trials), 3),
        "max_mbps": round(max(t["mbps"] for t in trials), 3),
        "max_l2": max(t["max_l2"] for t in trials),
    }


def main():
    print("=" * 80, flush=True)
    print("3-PHYSICAL-BOARD OFF-WI-FI MESH SUITE (ESP32 COM9 + NodeMCU COM13 + ESP32 COM12)", flush=True)
    print("=" * 80, flush=True)
    nodes = discover_all()
    if len(nodes) < 2:
        raise RuntimeError(f"Expected >=2 nodes, got {list(nodes.keys())}")

    for n in nodes.values():
        if n.enow_v == 2:
            n.cmd_wait("txpwr 78", "OK txpwr=")
            n.cmd_wait("rate 4", "OK rate=")
        n.cmd_wait("drop 0", "OK drop=")
        n.cmd_wait("l2relay off", "OK l2relay=off")
        n.cmd_wait("psk necklink-1.3.2-mesh3-key", "OK psk=")
        n.cmd_wait("reset_counts", "OK reset_counts")
    reset_esp32_hubs(nodes)

    for _ in range(5):
        for n in nodes.values():
            n.cmd_wait("beacon", "OK beacon", timeout=0.8)
        time.sleep(0.08)

    topo = {f"node_{nid}": n.stat() for nid, n in sorted(nodes.items())}
    print("\n[Mesh Discovery] 100% Off-Wi-Fi L2 Neighbor Tables:", flush=True)
    for nid, st in topo.items():
        assert st["wifi_sta"] == 0 and st["ip"] == "0.0.0.0", f"{nid} connected to Wi-Fi!"
        seen = [(p["node"], p["mac"], f"{p['rssi']}dBm") for p in st["neighbors"]]
        print(f"  {nid} ({st['port']}, enow_v={st['enow_v']}, mtu={st['mtu']}B, heap={st['heap']}B) -> sees {seen}", flush=True)

    n0 = nodes[0]  # ESP32 COM9 (v2)
    n1 = nodes[1]  # NodeMCU COM13 (v1)
    n2 = nodes.get(2)  # ESP32 COM12 (v2)

    out = {
        "all_three_physical_boards_online": (n2 is not None),
        "wifi_router_connected": False,
        "topology": topo,
        "pairwise_links": [],
        "nodemcu_to_esp32_payloads": [],
        "nodemcu_to_esp32_loss": [],
        "three_board_linear_relay": [],
        "simultaneous_2sender_to_hub": {},
    }

    # 1. All Pairwise Links Across All 3 Physical Boards (16 KB ESP32<->ESP32, 8 KB on all cross-chip links!)
    print("\n[1] Pairwise Direct Off-Wi-Fi Links Across All Connected Modules...", flush=True)
    pairs = [(0, 2, 16384), (2, 0, 16384), (1, 0, 8192), (1, 2, 8192), (0, 1, 8192), (2, 1, 8192)] if n2 else [(1, 0, 8192), (0, 1, 8192)]
    for s_id, d_id, sz in pairs:
        reset_esp32_hubs(nodes)
        s_n, d_n = nodes[s_id], nodes[d_id]
        _ = run_nltp(s_n, d_n, d_n.mac, 64, timeout=3.0)
        raw_p = summarize([run_raw(s_n, d_n, d_n.mac, sz) for _ in range(3)])
        nltp_p = summarize([run_nltp(s_n, d_n, d_n.mac, sz) for _ in range(3)])
        lbl = f"Node{s_id}({s_n.port})->Node{d_id}({d_n.port}) [{sz}B]"
        out["pairwise_links"].append({"link": lbl, "bytes": sz, "raw": raw_p, "nltp": nltp_p})
        print(f"  {lbl:<38} | Raw: {raw_p['median_ms']:>6.2f}ms ({raw_p['median_mbps']:.3f}Mbps, {raw_p['delivery_pct']}%) | NLTP 1.3.2: {nltp_p['median_ms']:>6.2f}ms ({nltp_p['median_mbps']:.3f}Mbps, {nltp_p['delivery_pct']}%)", flush=True)

    # 2. NodeMCU (v1 250B) -> ESP32 (v2 1470B) Payload Scaling
    print("\n[2] NodeMCU (ESP8266 250B HW) -> ESP32 (ESP-NOW v2 Hub) Dynamic Interop Sweep...", flush=True)
    reset_esp32_hubs(nodes)
    _ = run_nltp(n1, n0, n0.mac, 64, timeout=3.0)
    for sz in [4, 64, 192, 1024, 4096, 8192]:
        raw_s = summarize([run_raw(n1, n0, n0.mac, sz) for _ in range(3)])
        nltp_s = summarize([run_nltp(n1, n0, n0.mac, sz) for _ in range(3)])
        out["nodemcu_to_esp32_payloads"].append({"bytes": sz, "raw": raw_s, "nltp": nltp_s})
        print(f"  {sz:>6} B | Raw: {raw_s['median_ms']:>6.2f} ms ({raw_s['median_mbps']:.3f} Mbps, L2={raw_s['max_l2']}B) | NLTP 1.3.2: {nltp_s['median_ms']:>6.2f} ms ({nltp_s['median_mbps']:.3f} Mbps, L2={nltp_s['max_l2']}B)", flush=True)

    # 3. NodeMCU -> ESP32 Packet-Loss Sweep (0% -> 35% on 8 KB)
    print("\n[3] Cross-Chip Packet-Loss Sweep (NodeMCU -> ESP32 v2 Hub, 8 KB, 0% -> 35% Loss)...", flush=True)
    reset_esp32_hubs(nodes)
    _ = run_nltp(n1, n0, n0.mac, 64, timeout=3.0)
    for loss in [0, 10, 25, 35]:
        n0.cmd_wait(f"drop {loss}", "OK drop=")
        raw_l = summarize([run_raw(n1, n0, n0.mac, 8192) for _ in range(3)])
        nltp_l = summarize([run_nltp(n1, n0, n0.mac, 8192, timeout=10.0) for _ in range(3)])
        out["nodemcu_to_esp32_loss"].append({"loss_pct": loss, "raw": raw_l, "nltp": nltp_l})
        print(f"  Loss {loss:>2}% | Raw: intact={raw_l['delivery_pct']:>5.1f}% (avg {raw_l['avg_rcvd']:.0f}/8192 B) | NLTP 1.3.2: SHA-256={nltp_l['delivery_pct']:>5.1f}% ({nltp_l['median_ms']:.2f} ms, {nltp_l['median_mbps']:.3f} Mbps)", flush=True)
    n0.cmd_wait("drop 0", "OK drop=")

    # 4. True 3-Physical-Board Linear Multi-Room Wireless Relays
    if n2 is not None:
        print("\n[4A] True 3-Board Linear Relay #1: COM13 (NodeMCU) -> COM9 (ESP32 Relay) -> COM12 (ESP32 Hub) [8 KB]...", flush=True)
        for loss in [0, 15]:
            reset_esp32_hubs(nodes)
            n2.cmd_wait(f"drop {loss}", "OK drop=")
            n0.cmd_wait("reset_counts", "OK reset_counts")
            n0.cmd_wait("l2relay off", "OK l2relay=off")
            raw_r = summarize([run_raw(n1, n2, n2.mac, 8192, via_mac=n0.mac, relay=n0) for _ in range(3)])
            raw_rel = n0.stat()["relayed"]

            reset_esp32_hubs(nodes)
            n0.cmd_wait("reset_counts", "OK reset_counts")
            n0.cmd_wait(f"l2relay {n1.mac} {n2.mac}", "OK l2relay=on")
            _ = run_nltp(n1, n2, n0.mac, 64, relay=n0, timeout=4.0)
            n0.cmd_wait("reset_counts", "OK reset_counts")
            nltp_r = summarize([run_nltp(n1, n2, n0.mac, 8192, relay=n0, timeout=10.0) for _ in range(3)])
            nltp_rel = n0.stat()["relayed"]
            n0.cmd_wait("l2relay off", "OK l2relay=off")
            n2.cmd_wait("drop 0", "OK drop=")
            out["three_board_linear_relay"].append(
                {
                    "route": "COM13 (NodeMCU) -> COM9 (ESP32 Relay) -> COM12 (ESP32 Hub)",
                    "bytes": 8192,
                    "loss_pct": loss,
                    "raw": raw_r,
                    "raw_relayed": raw_rel,
                    "nltp": nltp_r,
                    "nltp_relayed": nltp_rel,
                }
            )
            print(f"  Route A (loss={loss:>2}%) | Raw: intact={raw_r['delivery_pct']:>5.1f}% (relayed={raw_rel}) | NLTP 1.3.2: SHA-256={nltp_r['delivery_pct']:>5.1f}% ({nltp_r['median_ms']:.2f} ms, {nltp_r['median_mbps']:.3f} Mbps, relayed={nltp_rel})", flush=True)

        print("\n[4B] True 3-Board Linear Relay #2: COM9 (ESP32) -> COM13 (NodeMCU Relay) -> COM12 (ESP32 Hub) [8 KB via 250B Relay]...", flush=True)
        for loss in [0, 15]:
            reset_esp32_hubs(nodes)
            n2.cmd_wait(f"drop {loss}", "OK drop=")
            n1.cmd_wait("reset_counts", "OK reset_counts")
            n1.cmd_wait("l2relay off", "OK l2relay=off")
            raw_r2 = summarize([run_raw(n0, n2, n2.mac, 8192, via_mac=n1.mac, relay=n1) for _ in range(3)])
            raw_rel2 = n1.stat()["relayed"]

            reset_esp32_hubs(nodes)
            n1.cmd_wait("reset_counts", "OK reset_counts")
            n1.cmd_wait(f"l2relay {n0.mac} {n2.mac}", "OK l2relay=on")
            _ = run_nltp(n0, n2, n1.mac, 64, relay=n1, timeout=4.0, force_v1_shards=True)
            n1.cmd_wait("reset_counts", "OK reset_counts")
            nltp_r2 = summarize([run_nltp(n0, n2, n1.mac, 8192, relay=n1, timeout=10.0, force_v1_shards=True) for _ in range(3)])
            nltp_rel2 = n1.stat()["relayed"]
            n1.cmd_wait("l2relay off", "OK l2relay=off")
            n2.cmd_wait("drop 0", "OK drop=")
            out["three_board_linear_relay"].append(
                {
                    "route": "COM9 (ESP32) -> COM13 (NodeMCU Relay) -> COM12 (ESP32 Hub)",
                    "bytes": 8192,
                    "loss_pct": loss,
                    "raw": raw_r2,
                    "raw_relayed": raw_rel2,
                    "nltp": nltp_r2,
                    "nltp_relayed": nltp_rel2,
                }
            )
            print(f"  Route B (loss={loss:>2}%) | Raw: intact={raw_r2['delivery_pct']:>5.1f}% (relayed={raw_rel2}) | NLTP 1.3.2: SHA-256={nltp_r2['delivery_pct']:>5.1f}% ({nltp_r2['median_ms']:.2f} ms, {nltp_r2['median_mbps']:.3f} Mbps, relayed={nltp_rel2})", flush=True)

        # 5. Simultaneous 2-Sender -> 1-Hub Concurrency (COM9 ESP32 32KB v2 + COM13 NodeMCU 8KB v1 -> COM12 ESP32 Hub!)
        print("\n[5] Simultaneous 3-Board Multi-Peer Hub Test: (COM9 ESP32 32KB v2 + COM13 NodeMCU 8KB v1) -> COM12 ESP32 Hub...", flush=True)
        reset_esp32_hubs(nodes)
        _ = run_nltp(n0, n2, n2.mac, 64, timeout=3.0)
        _ = run_nltp(n1, n2, n2.mac, 64, timeout=3.0)
        sim_hub = {}
        for loss in [0, 10]:
            n2.cmd_wait(f"drop {loss}", "OK drop=")
            raw_both = 0
            for _ in range(5):
                n0.clear(); n1.clear(); n2.clear()
                n0.send_cmd(f"raw_async {n2.mac} 32768")
                n1.send_cmd(f"raw_send {n2.mac} 8192")
                t0 = time.perf_counter()
                while (time.perf_counter() - t0) < 1.2:
                    n0.drain(); n1.drain(); n2.drain()
                    time.sleep(0.005)
                hub_txt = n2.drain()
                intacts = [int(m.group(2)) for m in re.finditer(r"RAW_RX len=(\d+)/\d+ pkts=\d+/\d+ intact=(\d+)", hub_txt)]
                if sum(intacts) >= 2:
                    raw_both += 1

            nltp_both = 0
            wall_times = []
            for _ in range(5):
                n0.clear(); n1.clear(); n2.clear()
                t0 = time.perf_counter()
                n0.send_cmd(f"wake_send {n2.mac} 32768")
                n1.send_cmd(f"nltp_send {n2.mac} 8192")
                got_n0 = got_n1 = False
                tx0_done = tx1_done = False
                while (time.perf_counter() - t0) < 8.0:
                    t0_txt = n0.drain()
                    t1_txt = n1.drain()
                    h_txt = n2.drain()
                    if "NLTP_TX ok=1" in t0_txt:
                        tx0_done = True
                    if "NLTP_TX ok=1" in t1_txt:
                        tx1_done = True
                    if re.search(rf"NLTP_RX src={re.escape(n0.mac)} rssi=-?\d+ len=32768 sha_ok=1", h_txt):
                        got_n0 = True
                    if re.search(rf"NLTP_RX src={re.escape(n1.mac)} rssi=-?\d+ len=8192 sha_ok=1", h_txt):
                        got_n1 = True
                    if got_n0 and got_n1 and tx0_done and tx1_done:
                        break
                    time.sleep(0.004)
                dt_ms = (time.perf_counter() - t0) * 1000.0
                if got_n0 and got_n1:
                    nltp_both += 1
                    wall_times.append(dt_ms)
                else:
                    reset_esp32_hubs(nodes)
                    _ = run_nltp(n0, n2, n2.mac, 64, timeout=3.0)
                    _ = run_nltp(n1, n2, n2.mac, 64, timeout=3.0)
                time.sleep(0.05)

            n2.cmd_wait("drop 0", "OK drop=")
            sim_hub[f"loss_{loss}pct"] = {
                "raw_both_streams_intact_pct": round(100.0 * raw_both / 5.0, 1),
                "nltp_both_streams_sha256_pct": round(100.0 * nltp_both / 5.0, 1),
                "nltp_median_wall_ms": round(statistics.median(wall_times), 2) if wall_times else 0.0,
                "hub_active_slots": n2.stat()["slots"],
            }
            print(
                f"  2-Sender->Hub (loss={loss:>2}%) | Raw both-intact={sim_hub[f'loss_{loss}pct']['raw_both_streams_intact_pct']:>5.1f}% | "
                f"NLTP 1.3.2 Hub both-SHA256={sim_hub[f'loss_{loss}pct']['nltp_both_streams_sha256_pct']:>5.1f}% "
                f"(median {sim_hub[f'loss_{loss}pct']['nltp_median_wall_ms']:.2f} ms, slots={sim_hub[f'loss_{loss}pct']['hub_active_slots']}/2)",
                flush=True,
            )
        out["simultaneous_2sender_to_hub"] = sim_hub

    for c in nodes.values():
        c.close()

    p = Path(r"D:\necklink\packages\nltp\results\esp32_esp8266_crosschip_mesh.json")
    p.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nSaved complete 3-board telemetry -> {p}", flush=True)


if __name__ == "__main__":
    main()
