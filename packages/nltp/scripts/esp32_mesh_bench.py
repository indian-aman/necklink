"""
Live 3-Node ESP32 Hardware Mesh, Range & Speed Benchmark for NLTP-NOW vs SoftAP-UDP vs Raw ESP-NOW.

Benchmarks interconnected ESP32 nodes where ONLY Node 0 (Gateway) connects to Wi-Fi,
while Room Nodes (Node 1, Node 2) stay 100% off home Wi-Fi:
  1. Mesh discovery, channel lock, RSSI / Noise / SNR neighbor matrix
  2. Stepped hardware TX-power range sweep (+19.5 dBm down to +2.0 dBm)
  3. Standard NLTP over Private ESP32 SoftAP + UDP (encrypted + SHA-256)
  4. Raw ESP-NOW v2 (1 Mbps default & 72.2 Mbps MCS7) at 0%, 5%, 12%, 20% loss
  5. NLTP-NOW (NLTP C99 ChaCha20-Poly1305 + Cauchy FEC directly over ESP-NOW v2) at 0..20% loss
  6. Laptop (Wi-Fi) -> Node 0 Gateway -> Off-Wi-Fi Room Node end-to-end encrypted bridge
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

import serial

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from necklink import NLTPClient, generate_nltp_key  # noqa: E402


def _redact_macs(text: str) -> str:
    """Replace board MAC addresses with stable labels before results are saved (they are committed)."""
    import re as _re
    seen: dict = {}

    def _sub(m: "_re.Match") -> str:
        mac = m.group(0).lower()
        if mac == "ff:ff:ff:ff:ff:ff":
            return m.group(0)
        seen.setdefault(mac, "<board-%s-mac>" % chr(ord("A") + len(seen)))
        return seen[mac]

    return _re.sub(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", _sub, text)


class MeshBoard:
    def __init__(self, port: str, node_id: int) -> None:
        self.port = port
        self.node_id = node_id
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 115200
        self.ser.timeout = 0.25
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.ip = "0.0.0.0"
        self.mac = ""
        self.channel = 11
        self.log_tail: List[str] = []

    def close(self) -> None:
        if self.ser and self.ser.is_open:
            self.ser.close()

    def _read_line(self) -> str:
        raw = self.ser.readline()
        if not raw:
            return ""
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            self.log_tail.append(line)
            if len(self.log_tail) > 80:
                self.log_tail.pop(0)
        return line

    def drain(self, duration: float = 0.1) -> List[str]:
        out: List[str] = []
        t0 = time.time()
        while time.time() - t0 < duration:
            line = self._read_line()
            if line:
                out.append(line)
        return out

    def reset_and_wait_ready(self, timeout: float = 22.0) -> Dict[str, str]:
        self.ser.reset_input_buffer()
        self.ser.dtr = False
        self.ser.rts = True
        time.sleep(0.15)
        self.ser.rts = False
        t0 = time.time()
        info: Dict[str, str] = {}
        while time.time() - t0 < timeout:
            line = self._read_line()
            if not line:
                continue
            if line.startswith("GATEWAY_WIFI "):
                for k, v in re.findall(r"(\w+)=([^\s]+)", line):
                    info[f"gw_{k}"] = v
                self.ip = info.get("gw_ip", "0.0.0.0")
            if line.startswith("READY "):
                for k, v in re.findall(r"(\w+)=([^\s]+)", line):
                    info[k] = v
                self.mac = info.get("mac", "")
                self.channel = int(info.get("ch", "11"))
                return info
        raise TimeoutError(f"{self.port} (node {self.node_id}) did not reach READY. Tail: {self.log_tail[-8:]}")

    def cmd(self, text: str, expect_prefix: Optional[str] = None, timeout: float = 8.0) -> str:
        self.drain(0.03)
        self.ser.write((text + "\n").encode("utf-8"))
        self.ser.flush()
        if not expect_prefix:
            return ""
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self._read_line()
            if line.startswith(expect_prefix):
                return line
            if line.startswith("ERROR"):
                raise RuntimeError(f"{self.port} error on '{text}': {line}")
        raise TimeoutError(f"{self.port} timed out waiting for '{expect_prefix}' on '{text}'. Tail: {self.log_tail[-6:]}")

    def get_stat(self) -> dict:
        self.drain(0.03)
        self.ser.write(b"stat\n")
        self.ser.flush()
        stat_info: dict = {"peers": {}}
        t0 = time.time()
        while time.time() - t0 < 5.0:
            line = self._read_line()
            if not line:
                continue
            if line.startswith("STAT "):
                for k, v in re.findall(r"(\w+)=([^\s]+)", line):
                    stat_info[k] = v
            elif line.startswith("PEER "):
                kv = dict(re.findall(r"(\w+)=([^\s]+)", line))
                pid = int(kv["id"])
                stat_info["peers"][pid] = {
                    "mac": kv.get("mac", ""),
                    "rssi_dbm": int(kv.get("rssi", "-100")),
                    "noise_dbm": int(kv.get("noise", "-95")),
                    "snr_db": int(kv.get("snr", "0")),
                    "pkts": int(kv.get("pkts", "0")),
                    "age_ms": int(kv.get("age_ms", "0")),
                }
            elif line == "END_STAT":
                return stat_info
        return stat_info

    def wait_result(self, kind: str, timeout: float = 25.0) -> Dict[str, float | str]:
        t0 = time.time()
        prefix = f"RESULT {kind} "
        while time.time() - t0 < timeout:
            line = self._read_line()
            if not line:
                continue
            if line.startswith(prefix):
                parsed: Dict[str, float | str] = {}
                for k, v in re.findall(r"(\w+)=([^\s]+)", line):
                    try:
                        parsed[k] = int(v) if re.fullmatch(r"-?\d+", v) else float(v)
                    except ValueError:
                        parsed[k] = v
                return parsed
            if line.startswith("ERROR"):
                raise RuntimeError(f"{self.port}: {line}")
        return {}


def estimate_indoor_rooms(margin_db: float) -> str:
    """Standard ITU-R P.1238 2.4 GHz indoor model (~30 dB @ 1m + 28*log10(d) + 9 dB/brick wall)."""
    if margin_db >= 42:
        return "3-4 rooms (~25-40 m indoor / ~120+ m LOS)"
    if margin_db >= 32:
        return "2-3 rooms (~15-25 m through 2 walls)"
    if margin_db >= 20:
        return "1-2 rooms (~8-15 m through 1 wall)"
    if margin_db >= 10:
        return "Same / adjacent room (~4-8 m)"
    return "Marginal (< 4 m, needs relay hop)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gw-port", default="COM9")
    ap.add_argument("--relay-port", default="COM11")
    ap.add_argument("--leaf-port", default="COM12")
    ap.add_argument("--out", default=str(ROOT / "results" / "esp32_3node_mesh_live.json"))
    args = ap.parse_args()

    boards: Dict[int, MeshBoard] = {}
    missing_nodes: Dict[int, str] = {}

    for nid, port in [(0, args.gw_port), (1, args.relay_port), (2, args.leaf_port)]:
        try:
            b = MeshBoard(port, nid)
            info = b.reset_and_wait_ready(timeout=18.0 if nid == 0 else 10.0)
            boards[nid] = b
            print(f"[ONLINE] Node {nid} ({port}): {info}")
        except Exception as exc:
            missing_nodes[nid] = f"{port}: {exc}"
            print(f"[OFFLINE] Node {nid} ({port}): {exc}")

    if 0 not in boards or len(boards) < 2:
        for b in boards.values():
            b.close()
        raise SystemExit("Need Gateway Node 0 plus at least one off-Wi-Fi Room Node online.")

    gw = boards[0]
    remote_ids = sorted(nid for nid in boards if nid != 0)
    primary_leaf_id = remote_ids[-1]
    leaf = boards[primary_leaf_id]

    # Exchange beacons so every online node populates its neighbor table
    for _ in range(3):
        for b in boards.values():
            b.cmd("beacon", "OK beacon")
        time.sleep(0.25)

    psk = generate_nltp_key()
    for b in boards.values():
        b.cmd(f"psk {psk}", "PSK on")

    report: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "online_nodes": {},
        "offline_nodes": missing_nodes,
        "neighbor_matrix": {},
        "range_txpower_sweep": [],
        "protocol_speed_comparison": [],
        "wall_loss_resilience": [],
        "laptop_gateway_bridge": [],
    }

    for nid, b in boards.items():
        st = b.get_stat()
        report["online_nodes"][nid] = {
            "port": b.port,
            "mac": b.mac,
            "wifi_connected": st.get("wifi_sta") == "1",
            "ip": st.get("ip", "0.0.0.0"),
            "channel": int(st.get("ch", b.channel)),
            "free_heap_bytes": int(st.get("heap", 0)),
        }
        report["neighbor_matrix"][nid] = st.get("peers", {})
        print(f"Node {nid} ({b.port}) Wi-Fi={st.get('wifi_sta')} IP={st.get('ip')} Peers={st.get('peers')}")

    # =========================================================================
    # 1. RANGE & HARDWARE TX-POWER ATTENUATION SWEEP (+19.5 dBm -> +2.0 dBm)
    # =========================================================================
    print("\n=== 1. Range & Stepped RF TX-Power Sweep ===")
    for rate_mode, rate_name, sens_limit_dbm in [
        (3, "MCS7_72Mbps", -72.0),
        (0, "1M_DSSS", -95.0),
        (5, "LR_250kbps", -102.0),
    ]:
        gw.cmd(f"rate {rate_mode}", "OK rate=")
        leaf.cmd(f"rate {rate_mode}", "OK rate=")
        for qdbm, label in [(78, "+19.5 dBm (Max)"), (52, "+13.0 dBm"), (28, "+7.0 dBm"), (8, "+2.0 dBm (Min)")]:
            gw.cmd(f"txpwr {qdbm}", "OK txpwr=")
            leaf.cmd(f"txpwr {qdbm}", "OK txpwr=")
            time.sleep(0.08)
            gw.cmd(f"ping {primary_leaf_id} 40 512")
            res = gw.wait_result("ping", timeout=9.0)
            rx_rssi = float(res.get("rx_rssi", -100.0))
            margin = rx_rssi - sens_limit_dbm
            row = {
                "phy_mode": rate_name,
                "tx_power": label,
                "pdr_pct": res.get("pdr", 0.0),
                "avg_rtt_ms": res.get("avg_ms", 0.0),
                "min_rtt_ms": res.get("min_ms", 0.0),
                "rx_rssi_dbm": rx_rssi,
                "tx_rssi_dbm": res.get("tx_rssi", -100.0),
                "link_margin_db": round(margin, 1),
                "estimated_coverage": estimate_indoor_rooms(margin) if qdbm == 78 else "Attenuated bench test",
            }
            report["range_txpower_sweep"].append(row)
            print(f"  {rate_name:14s} | {label:16s} | RSSI={rx_rssi:5.1f} dBm | Margin={margin:4.1f} dB | "
                  f"PDR={row['pdr_pct']:5.1f}% | RTT={row['avg_rtt_ms']:.2f} ms")

    # Restore full +19.5 dBm TX power
    for b in boards.values():
        b.cmd("txpwr 78", "OK txpwr=")

    # =========================================================================
    # 2. SPEED BENCHMARK: NLTP-NOW vs RAW ESP-NOW vs STANDARD NLTP-SOFTAP-UDP
    # =========================================================================
    print("\n=== 2. Speed Benchmark: NLTP-NOW (L2) vs Raw ESP-NOW vs NLTP-SoftAP-UDP ===")
    sizes = [4096, 16384, 32768]

    # 2A-0. 4-Byte Short Call Latency (Waiter-Call / Reflex Sensor Profile)
    for r_mode, r_label in [
        (5, "NLTP-NOW + LR 250kbps (4B Call, AEAD+SHA256)"),
        (3, "NLTP-NOW MCS7 72Mbps (4B Call, AEAD+SHA256)"),
    ]:
        gw.cmd(f"rate {r_mode}", "OK rate=")
        leaf.cmd(f"rate {r_mode}", "OK rate=")
        runs_ms = []
        ok_cnt = 0
        for _ in range(5):
            leaf.drain(0.02)
            gw.cmd(f"nltp_send {primary_leaf_id} 4")
            tx_r = gw.wait_result("nltp_tx", timeout=5.0)
            rx_r = leaf.wait_result("nltp_rx", timeout=3.0)
            if tx_r.get("ok") == 1 and rx_r.get("ok") == 1:
                ok_cnt += 1
                runs_ms.append(float(tx_r["ms"]))
        med_ms = round(statistics.median(runs_ms), 2) if runs_ms else 0.0
        row = {"protocol": r_label, "size_bytes": 4, "ok": f"{ok_cnt}/5", "median_ms": med_ms, "median_mbps": 0.0}
        report["protocol_speed_comparison"].append(row)
        print(f"  {r_label:42s} |    4 B | ok={ok_cnt}/5 | {med_ms:6.2f} ms (verified FIN_ACK)")

    # 2A. NLTP-NOW across PHY rates (LR_250K, 1M_L, 11M_S, MCS7, AUTO)
    for r_mode, r_label in [
        (5, "NLTP-NOW (LR 250kbps, Encrypted+FEC)"),
        (0, "NLTP-NOW (1M Long-Range, Encrypted+FEC)"),
        (1, "NLTP-NOW (11M Indoor, Encrypted+FEC)"),
        (3, "NLTP-NOW (MCS7 72M, Encrypted+FEC)"),
        (4, "NLTP-NOW (Auto RSSI-Rate, Encrypted+FEC)"),
    ]:
        gw.cmd(f"rate {r_mode}", "OK rate=")
        leaf.cmd(f"rate {r_mode}", "OK rate=")
        for sz in sizes:
            runs_mbps = []
            runs_ms = []
            ok_cnt = 0
            for _ in range(3):
                leaf.drain(0.02)
                gw.cmd(f"nltp_send {primary_leaf_id} {sz}")
                tx_r = gw.wait_result("nltp_tx", timeout=15.0)
                rx_r = leaf.wait_result("nltp_rx", timeout=5.0)
                if tx_r.get("ok") == 1 and rx_r.get("ok") == 1:
                    ok_cnt += 1
                    runs_mbps.append(float(tx_r["mbps"]))
                    runs_ms.append(float(tx_r["ms"]))
            med_mbps = round(statistics.median(runs_mbps), 3) if runs_mbps else 0.0
            med_ms = round(statistics.median(runs_ms), 2) if runs_ms else 0.0
            row = {"protocol": r_label, "size_bytes": sz, "ok": f"{ok_cnt}/3", "median_ms": med_ms, "median_mbps": med_mbps}
            report["protocol_speed_comparison"].append(row)
            print(f"  {r_label:42s} | {sz // 1024:2d} KB | ok={ok_cnt}/3 | {med_ms:6.2f} ms | {med_mbps:5.3f} Mbps")

    # 2B. Raw ESP-NOW v2 (1 Mbps Default & MCS7 72.2 Mbps, No FEC/AEAD)
    for r_mode, r_label in [
        (0, "Raw ESP-NOW v2 (1M Default, No FEC)"),
        (3, "Raw ESP-NOW v2 (MCS7 72M, No FEC)"),
    ]:
        gw.cmd(f"rate {r_mode}", "OK rate=")
        leaf.cmd(f"rate {r_mode}", "OK rate=")
        for sz in sizes:
            runs_mbps = []
            runs_ms = []
            ok_cnt = 0
            for _ in range(3):
                gw.cmd(f"raw_bench {primary_leaf_id} {sz} 1080")
                rr = gw.wait_result("raw_espnow", timeout=8.0)
                if rr:
                    if rr.get("ok") == 1:
                        ok_cnt += 1
                    runs_mbps.append(float(rr.get("mbps", 0.0)))
                    runs_ms.append(round(float(rr.get("us", 0)) / 1000.0, 2))
            med_mbps = round(statistics.median(runs_mbps), 3) if runs_mbps else 0.0
            med_ms = round(statistics.median(runs_ms), 2) if runs_ms else 0.0
            row = {"protocol": r_label, "size_bytes": sz, "ok": f"{ok_cnt}/3", "median_ms": med_ms, "median_mbps": med_mbps}
            report["protocol_speed_comparison"].append(row)
            print(f"  {r_label:42s} | {sz // 1024:2d} KB | ok={ok_cnt}/3 | {med_ms:6.2f} ms | {med_mbps:5.3f} Mbps")

    # 2C. Standard NLTP over Private ESP32 SoftAP + UDP (Baseline off-home-Wi-Fi IP approach)
    print("\n=== 3. Baseline Comparison: Standard NLTP over Private ESP32 SoftAP + UDP ===")
    t_ap0 = time.perf_counter()
    gw.cmd("softap on", "OK softap=on")
    join_line = leaf.cmd("softap join", "OK softap_join=", timeout=12.0)
    assoc_ms = round((time.perf_counter() - t_ap0) * 1000.0, 1)
    m_ip = re.search(r"ip=([\d\.]+)", join_line)
    leaf_ap_ip = m_ip.group(1) if m_ip else "192.168.4.2"
    print(f"  SoftAP association took {assoc_ms} ms ({join_line})")
    if "softap_join=1" in join_line:
        gw.cmd(f"mode_udp {leaf_ap_ip}", "OK mode_udp=")
        for sz in sizes:
            runs_mbps = []
            runs_ms = []
            ok_cnt = 0
            for _ in range(3):
                leaf.drain(0.02)
                gw.cmd(f"nltp_send {primary_leaf_id} {sz}")
                tx_r = gw.wait_result("nltp_tx", timeout=20.0)
                rx_r = leaf.wait_result("nltp_rx", timeout=6.0)
                if tx_r.get("ok") == 1 and rx_r.get("ok") == 1:
                    ok_cnt += 1
                    runs_mbps.append(float(tx_r["mbps"]))
                    runs_ms.append(float(tx_r["ms"]))
            med_mbps = round(statistics.median(runs_mbps), 3) if runs_mbps else 0.0
            med_ms = round(statistics.median(runs_ms), 2) if runs_ms else 0.0
            row = {
                "protocol": "Standard NLTP over Private SoftAP UDP",
                "size_bytes": sz,
                "ok": f"{ok_cnt}/3",
                "median_ms": med_ms,
                "median_mbps": med_mbps,
                "softap_assoc_ms": assoc_ms,
            }
            report["protocol_speed_comparison"].append(row)
            print(f"  {'Standard NLTP over Private SoftAP UDP':42s} | {sz // 1024:2d} KB | ok={ok_cnt}/3 | {med_ms:6.2f} ms | {med_mbps:5.3f} Mbps")

    # Return cleanly to pure off-Wi-Fi NLTP-NOW mode!
    gw.cmd("mode_udp off", "OK mode_udp=off")
    leaf.cmd("softap leave", "OK softap_leave")
    gw.cmd("softap off", "OK softap=off")
    time.sleep(0.3)
    gw.cmd("rate 3", "OK rate=")
    leaf.cmd("rate 3", "OK rate=")
    gw.cmd("beacon", "OK beacon")
    leaf.cmd("beacon", "OK beacon")
    time.sleep(0.2)

    # =========================================================================
    # 3. WALL-LOSS & INTERFERENCE RESILIENCE (0%, 5%, 12%, 20% Packet Drop)
    # =========================================================================
    print("\n=== 4. Multi-Room Wall-Loss Stress Test (32 KB Payload @ 0%, 5%, 12%, 20% Loss) ===")
    for loss_pct in [0, 5, 12, 20]:
        leaf.cmd(f"drop {loss_pct}", "OK drop=")
        # Test Raw ESP-NOW v2 (MCS7)
        raw_ok = 0
        raw_rx_ratios = []
        for _ in range(3):
            gw.cmd(f"raw_bench {primary_leaf_id} 32768 1080")
            rr = gw.wait_result("raw_espnow", timeout=6.0)
            if rr.get("ok") == 1:
                raw_ok += 1
            raw_rx_ratios.append(float(rr.get("rx_bytes", 0)) / 32768.0)

        # Test NLTP-NOW (MCS7 + Cauchy GF(256) FEC + ChaCha20-Poly1305 + SHA-256)
        nltp_ok = 0
        nltp_mbps = []
        nltp_ms = []
        for _ in range(3):
            leaf.drain(0.02)
            gw.cmd(f"nltp_send {primary_leaf_id} 32768")
            tx_r = gw.wait_result("nltp_tx", timeout=20.0)
            rx_r = leaf.wait_result("nltp_rx", timeout=6.0)
            if tx_r.get("ok") == 1 and rx_r.get("ok") == 1:
                nltp_ok += 1
                nltp_mbps.append(float(tx_r["mbps"]))
                nltp_ms.append(float(tx_r["ms"]))

        loss_row = {
            "loss_pct": loss_pct,
            "raw_espnow_intact_files": f"{raw_ok}/3",
            "raw_espnow_avg_bytes_rcvd_pct": round(100.0 * statistics.mean(raw_rx_ratios), 1),
            "nltp_now_intact_sha256_files": f"{nltp_ok}/3",
            "nltp_now_median_ms": round(statistics.median(nltp_ms), 2) if nltp_ms else None,
            "nltp_now_median_mbps": round(statistics.median(nltp_mbps), 3) if nltp_mbps else 0.0,
        }
        report["wall_loss_resilience"].append(loss_row)
        print(f"  Loss {loss_pct:2d}% | Raw ESP-NOW intact={raw_ok}/3 ({loss_row['raw_espnow_avg_bytes_rcvd_pct']:5.1f}% bytes) | "
              f"NLTP-NOW SHA-256 intact={nltp_ok}/3 ({loss_row['nltp_now_median_mbps']:.3f} Mbps, {loss_row['nltp_now_median_ms']} ms)")

    leaf.cmd("drop 0", "OK drop=")

    # =========================================================================
    # 4. END-TO-END LAPTOP (WIFI) -> NODE 0 GATEWAY -> OFF-WIFI ROOM NODE
    # =========================================================================
    rejoin_line = gw.cmd("wifi_rejoin", "OK wifi_rejoin=", timeout=15.0)
    m_re = re.search(r"ip=([\d\.]+)", rejoin_line)
    if m_re and m_re.group(1) != "0.0.0.0":
        report["online_nodes"][0]["ip"] = m_re.group(1)
        report["online_nodes"][0]["wifi_connected"] = True
    gw_ip = report["online_nodes"][0]["ip"]
    if gw_ip and gw_ip != "0.0.0.0":
        print(f"\n=== 5. End-to-End Laptop (Wi-Fi) -> Gateway ({gw_ip}) -> Off-Wi-Fi Node {primary_leaf_id} ===")
        tmp_dir = Path(tempfile.mkdtemp())
        for target_nid in [0, primary_leaf_id]:
            udp_port = 9700 + target_nid
            target_b = boards[target_nid]
            for sz in [4096, 32768]:
                payload = os.urandom(sz)
                fpath = tmp_dir / f"laptop_to_node{target_nid}_{sz}.bin"
                fpath.write_bytes(payload)
                runs_s = []
                ok_c = 0
                with NLTPClient((gw_ip, udp_port), psk=psk, max_tx_kbps=15000) as cli:
                    if sz == 4096:
                        cli.send_reflex(f"ROOM_{target_nid}_PING")
                    for _ in range(3):
                        target_b.drain(0.02)
                        r = cli.send_file(fpath, timeout=25.0)
                        esp_rx = target_b.wait_result("nltp_rx", timeout=5.0)
                        if r.get("verified_sha256") and esp_rx.get("ok") == 1:
                            ok_c += 1
                            runs_s.append(float(r["elapsed_s"]))
                med_s = statistics.median(runs_s) if runs_s else 0.0
                mbps = round((sz * 8.0) / (med_s * 1e6), 3) if med_s > 0 else 0.0
                hop_desc = "1-Hop Wi-Fi (Laptop -> Gateway Node 0)" if target_nid == 0 else f"2-Hop Hybrid (Laptop Wi-Fi -> Node 0 -> NLTP-NOW Off-Wi-Fi Node {target_nid})"
                b_row = {
                    "route": hop_desc,
                    "size_bytes": sz,
                    "ok": f"{ok_c}/3",
                    "median_ms": round(med_s * 1000.0, 2),
                    "median_mbps": mbps,
                }
                report["laptop_gateway_bridge"].append(b_row)
                print(f"  {hop_desc:66s} | {sz // 1024:2d} KB | ok={ok_c}/3 | {b_row['median_ms']:6.2f} ms | {mbps:.3f} Mbps")

    # Leave all nodes in Auto RSSI-Adaptive NLTP-NOW mode, ready to unplug & place in rooms!
    for b in boards.values():
        b.cmd("rate 4", "OK rate=")
        b.cmd("txpwr 78", "OK txpwr=")
        b.cmd("drop 0", "OK drop=")
        b.close()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_redact_macs(json.dumps(report, indent=2)), encoding="utf-8")
    print(f"\nSaved complete live hardware report -> {out_path}")


if __name__ == "__main__":
    main()
