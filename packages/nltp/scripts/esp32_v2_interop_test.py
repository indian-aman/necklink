#!/usr/bin/env python3
"""
esp32_v2_interop_test.py — Live Multi-Implementation Hardware Interop Suite for
NLTP over ESP-NOW v2 (1,470 B MTU, Zero Custom Wrapper Headers).

Verifies on live ESP32 hardware (COM9 + COM12 + Host Laptop Python):
  1. Bidirectional C99 `nltp_espnow_hub_t` (COM9) <-> Arduino `NLTPESPNowNode` (COM12)
     over ESP-NOW v2 carrying raw "NL" ChaCha20-Poly1305 encrypted 960 B shards
     (1,010 B L2 frames > 250 B ESP-NOW v1 limit) across AUTO/MCS7, Fixed MCS7,
     and Espressif Long Range 250 kbps (`WIFI_PHY_MODE_LR`).
  2. 15% wireless packet loss recovery (`GF(256)` Cauchy FEC + Proprioceptive DOF ACK)
     over ESP-NOW v2 in both directions (`C99 <-> Arduino`).
  3. Multi-peer Hub MAC allow-list filtering + 3x customer deep-sleep `conn_id`
     rotation (`wake_reset` with `stream_id=1` re-use) without stream lockout.
  4. Bidirectional Host Python `pynltp` (`necklink.nltp.NLTPEndpoint`) <-> COM9 RFC 1055
     SLIP L2 Tunnel <-> 2.4 GHz ESP-NOW v2 (1,010 B frames) <-> Off-Wi-Fi ESP32 COM12
     (`NLTPESPNowNode`), proving zero-modification end-to-end encryption, FEC, and
     SHA-256 `STREAM_FIN_ACK` verification across Python, C99, and Arduino!
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import serial

REPO_NLTP = Path(__file__).resolve().parents[1]
if str(REPO_NLTP) not in sys.path:
    sys.path.insert(0, str(REPO_NLTP))

from necklink.nltp import NLTPEndpoint  # noqa: E402


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

SLIP_END = 0xC0
SLIP_ESC = 0xDB
SLIP_ESC_END = 0xDC
SLIP_ESC_ESC = 0xDD

SHARED_PSK = "necklink-v2-interop-key"


def deterministic_pattern(length: int, seed: int) -> bytes:
    x = (seed & 0xFFFFFFFF) or 0x9E3779B9
    out = bytearray(length)
    for i in range(length):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= (x >> 17) & 0xFFFFFFFF
        x ^= (x << 5) & 0xFFFFFFFF
        out[i] = x & 0xFF
    return bytes(out)


def slip_encode(pkt: bytes) -> bytes:
    buf = bytearray([SLIP_END])
    for b in pkt:
        if b == SLIP_END:
            buf.extend((SLIP_ESC, SLIP_ESC_END))
        elif b == SLIP_ESC:
            buf.extend((SLIP_ESC, SLIP_ESC_ESC))
        else:
            buf.append(b)
    buf.append(SLIP_END)
    return bytes(buf)


class SlipDecoder:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._esc = False

    def feed(self, chunk: bytes) -> List[bytes]:
        frames: List[bytes] = []
        for b in chunk:
            if b == SLIP_END:
                if self._buf:
                    frames.append(bytes(self._buf))
                    self._buf.clear()
                self._esc = False
            elif self._esc:
                if b == SLIP_ESC_END:
                    self._buf.append(SLIP_END)
                elif b == SLIP_ESC_ESC:
                    self._buf.append(SLIP_ESC)
                self._esc = False
            elif b == SLIP_ESC:
                self._esc = True
            else:
                self._buf.append(b)
        return frames


def parse_kv(line: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for tok in line.strip().split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


class BoardSerial:
    def __init__(self, port: str, name: str) -> None:
        self.port = port
        self.name = name
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 115200
        self.ser.timeout = 0.015
        self.ser.write_timeout = 1.0
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self._rx_buf = bytearray()
        self.lines: List[str] = []

    def close(self) -> None:
        if self.ser.is_open:
            self.ser.close()

    def drain_lines(self) -> List[str]:
        chunk = self.ser.read(4096)
        new_lines: List[str] = []
        if chunk:
            self._rx_buf.extend(chunk)
            while b"\n" in self._rx_buf:
                raw_line, _, rest = self._rx_buf.partition(b"\n")
                self._rx_buf = bytearray(rest)
                s = raw_line.decode("utf-8", errors="replace").strip()
                if s:
                    self.lines.append(s)
                    new_lines.append(s)
                    print(f"  [{self.name}] {s}")
        return new_lines

    def clear(self) -> None:
        self.drain_lines()
        self.lines.clear()

    def cmd(self, text: str, expect_prefix: str = "OK", timeout: float = 2.5) -> str:
        self.drain_lines()
        start_idx = len(self.lines)
        self.ser.write((text.strip() + "\n").encode("ascii"))
        self.ser.flush()
        t0 = time.perf_counter()
        while (time.perf_counter() - t0) < timeout:
            self.drain_lines()
            for line in self.lines[start_idx:]:
                if line.startswith(expect_prefix):
                    return line
            time.sleep(0.01)
        raise TimeoutError(f"{self.name} ({self.port}) timed out waiting for '{expect_prefix}' on cmd '{text}'")


def wait_pair_events(
    b_tx: BoardSerial,
    tx_prefix: str,
    b_rx: BoardSerial,
    rx_prefix: str,
    timeout: float = 12.0,
    tx_idx: int = 0,
    rx_idx: int = 0,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    got_tx: Optional[Dict[str, str]] = None
    got_rx: Optional[Dict[str, str]] = None
    t0 = time.perf_counter()
    while (time.perf_counter() - t0) < timeout:
        b_tx.drain_lines()
        b_rx.drain_lines()
        if got_tx is None:
            for ln in b_tx.lines[tx_idx:]:
                if ln.startswith(tx_prefix):
                    got_tx = parse_kv(ln)
                    break
        if got_rx is None:
            for ln in b_rx.lines[rx_idx:]:
                if ln.startswith(rx_prefix):
                    got_rx = parse_kv(ln)
                    break
        if got_tx is not None and got_rx is not None:
            return got_tx, got_rx
        time.sleep(0.008)
    raise TimeoutError(f"Timeout waiting for {tx_prefix} (got={got_tx}) and {rx_prefix} (got={got_rx})")


def run_all() -> Dict[str, Any]:
    com9 = BoardSerial("COM9", "COM9-C99Hub")
    com12 = BoardSerial("COM12", "COM12-Arduino")
    try:
        time.sleep(0.4)
        com9.clear()
        com12.clear()

        s9 = parse_kv(com9.cmd("stat", "STAT"))
        s12 = parse_kv(com12.cmd("stat", "STAT"))

        assert int(s9["enow_v"]) == 2, f"Expected ESP-NOW v2 on COM9, got {s9}"
        assert int(s12["enow_v"]) == 2, f"Expected ESP-NOW v2 on COM12, got {s12}"
        assert int(s9["mtu"]) == 1470, f"Expected MTU 1470 on COM9, got {s9}"
        assert int(s12["mtu"]) == 1470, f"Expected MTU 1470 on COM12, got {s12}"

        mac9 = s9["mac"].lower()
        mac12 = s12["mac"].lower()
        print(f"\n=== Verified ESP-NOW v2 Hardware: COM9={mac9} (C99 Hub) <-> COM12={mac12} (Arduino Node) ===")

        com9.cmd("allow all")
        com9.cmd("drop 0")
        com12.cmd("drop 0")
        com12.cmd("txkbps 25000")
        com12.cmd(f"peer {mac9}")

        results: Dict[str, Any] = {
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "boards": {
                "COM9": {"role": s9["role"], "mac": mac9, "espnow_version": int(s9["enow_v"]), "mtu": int(s9["mtu"]), "free_heap_bytes": int(s9["heap"])},
                "COM12": {"role": s12["role"], "mac": mac12, "espnow_version": int(s12["enow_v"]), "mtu": int(s12["mtu"]), "free_heap_bytes": int(s12["heap"])},
            },
            "suite1_c99_arduino_bidirectional": [],
            "suite2_fec_15pct_loss": [],
            "suite3_allowlist_and_deepsleep_rewake": {},
            "suite4_python_slip_espnow_v2_e2e": [],
        }

        # ------------------------------------------------------------------
        # SUITE 1: C99 Hub <-> Arduino Node Bidirectional ESP-NOW v2
        # ------------------------------------------------------------------
        print("\n--- Suite 1: Bidirectional C99 Hub (COM9) <-> Arduino Node (COM12) over ESP-NOW v2 ---")
        rate_matrix = [
            (4, "AUTO (HT20 MCS7)", [4, 4096, 16384, 32768]),
            (3, "Fixed HT20 MCS7", [32768]),
            (5, "Espressif Long Range 250 kbps (LR)", [4, 193, 4096]),
        ]
        for rate_code, rate_label, sizes in rate_matrix:
            com9.cmd(f"rate {rate_code}")
            com12.cmd(f"rate {rate_code}")
            time.sleep(0.08)

            for sz in sizes:
                # Direction A: Arduino COM12 -> C99 Hub COM9
                com9.clear()
                com12.clear()
                com12.cmd(f"send {sz}", "OK send_started")
                tx_ev, rx_ev = wait_pair_events(com12, "NODE_TX", com9, "HUB_RX", timeout=15.0)
                assert int(tx_ev["ok"]) == 1 and int(rx_ev["sha_ok"]) == 1, (tx_ev, rx_ev)
                if sz >= 1024:
                    assert int(rx_ev["max_l2"]) > 250, f"Expected ESP-NOW v2 frame > 250 B, got {rx_ev}"
                results["suite1_c99_arduino_bidirectional"].append({
                    "rate_mode": rate_label,
                    "direction": "Arduino_COM12 -> C99_Hub_COM9",
                    "bytes": sz,
                    "ms": float(tx_ev["ms"]),
                    "mbps": float(tx_ev["mbps"]),
                    "sha256_ok": int(rx_ev["sha_ok"]) == 1,
                    "fin_ack_verified": int(tx_ev["ok"]) == 1,
                    "rssi_dbm": int(rx_ev["rssi"]),
                    "max_l2_frame_bytes": int(rx_ev["max_l2"]),
                })

                # Direction B: C99 Hub COM9 -> Arduino COM12
                com9.clear()
                com12.clear()
                com9.cmd(f"hub_send {mac12} {sz}", "OK hub_send_started")
                tx_ev, rx_ev = wait_pair_events(com9, "HUB_TX", com12, "NODE_RX", timeout=15.0)
                assert int(tx_ev["ok"]) == 1 and int(rx_ev["sha_ok"]) == 1, (tx_ev, rx_ev)
                if sz >= 1024:
                    assert int(rx_ev["max_l2"]) > 250, f"Expected ESP-NOW v2 frame > 250 B, got {rx_ev}"
                results["suite1_c99_arduino_bidirectional"].append({
                    "rate_mode": rate_label,
                    "direction": "C99_Hub_COM9 -> Arduino_COM12",
                    "bytes": sz,
                    "ms": float(tx_ev["ms"]),
                    "mbps": float(tx_ev["mbps"]),
                    "sha256_ok": int(rx_ev["sha_ok"]) == 1,
                    "fin_ack_verified": int(tx_ev["ok"]) == 1,
                    "rssi_dbm": int(rx_ev["rssi"]),
                    "max_l2_frame_bytes": int(rx_ev["max_l2"]),
                })

        # Restore AUTO rate
        com9.cmd("rate 4")
        com12.cmd("rate 4")
        time.sleep(0.08)

        # ------------------------------------------------------------------
        # SUITE 2: 15% Wireless Packet Loss + GF(256) Cauchy FEC over ESP-NOW v2
        # ------------------------------------------------------------------
        print("\n--- Suite 2: 15% Packet Loss + GF(256) Cauchy FEC over ESP-NOW v2 (16 KB) ---")
        com9.cmd("drop 15")
        com9.clear()
        com12.clear()
        com12.cmd("send 16384", "OK send_started")
        tx_ev, rx_ev = wait_pair_events(com12, "NODE_TX", com9, "HUB_RX", timeout=15.0)
        assert int(tx_ev["ok"]) == 1 and int(rx_ev["sha_ok"]) == 1
        results["suite2_fec_15pct_loss"].append({
            "direction": "Arduino_COM12 -> C99_Hub_COM9 (15% RX drop)",
            "bytes": 16384,
            "ms": float(tx_ev["ms"]),
            "mbps": float(tx_ev["mbps"]),
            "sha256_ok": True,
            "max_l2_frame_bytes": int(rx_ev["max_l2"]),
        })
        com9.cmd("drop 0")

        com12.cmd("drop 15")
        com9.clear()
        com12.clear()
        com9.cmd(f"hub_send {mac12} 16384", "OK hub_send_started")
        tx_ev, rx_ev = wait_pair_events(com9, "HUB_TX", com12, "NODE_RX", timeout=15.0)
        assert int(tx_ev["ok"]) == 1 and int(rx_ev["sha_ok"]) == 1
        results["suite2_fec_15pct_loss"].append({
            "direction": "C99_Hub_COM9 -> Arduino_COM12 (15% RX drop)",
            "bytes": 16384,
            "ms": float(tx_ev["ms"]),
            "mbps": float(tx_ev["mbps"]),
            "sha256_ok": True,
            "max_l2_frame_bytes": int(rx_ev["max_l2"]),
        })
        com12.cmd("drop 0")

        # ------------------------------------------------------------------
        # SUITE 3: Multi-Peer Hub MAC Allow-List + Deep-Sleep conn_id Re-Wake
        # ------------------------------------------------------------------
        print("\n--- Suite 3: MAC Allow-List Enforcement + 3x Deep-Sleep conn_id Rotation ---")
        com9.cmd("allow 11:22:33:44:55:66")
        com9.clear()
        com12.clear()
        com12.cmd("send 4", "OK send_started")
        time.sleep(1.0)
        com9.drain_lines()
        com12.drain_lines()
        hub_rx_during_block = any(ln.startswith("HUB_RX") for ln in com9.lines)
        node_tx_during_block = any(ln.startswith("NODE_TX") for ln in com12.lines)
        s9_block = parse_kv(com9.cmd("stat", "STAT"))
        blocked_cnt = int(s9_block["blocked"])
        assert not hub_rx_during_block and not node_tx_during_block and blocked_cnt > 0, (
            hub_rx_during_block, node_tx_during_block, s9_block
        )

        # Reset COM12 and allow COM12 MAC on COM9
        com9.cmd(f"allow {mac12}")
        wake_runs = []
        for wake_i in range(1, 4):
            w_line = com12.cmd("wake_reset", "OK wake_reset")
            cid_hex = parse_kv(w_line)["cid"]
            com12.cmd(f"peer {mac9}")
            com9.clear()
            com12.clear()
            com12.cmd("send 4", "OK send_started")
            tx_ev, rx_ev = wait_pair_events(com12, "NODE_TX", com9, "HUB_RX", timeout=5.0)
            assert int(tx_ev["ok"]) == 1 and int(rx_ev["sha_ok"]) == 1
            wake_runs.append({
                "wake_index": wake_i,
                "conn_id": cid_hex,
                "ms": float(tx_ev["ms"]),
                "sha256_ok": True,
            })
        com9.cmd("allow all")

        results["suite3_allowlist_and_deepsleep_rewake"] = {
            "unlisted_mac_blocked": True,
            "blocked_frames_counted": blocked_cnt,
            "deep_sleep_wake_rotations": wake_runs,
        }

        # ------------------------------------------------------------------
        # SUITE 4: Host Python `pynltp` <-> COM9 SLIP Tunnel <-> ESP-NOW v2 <-> COM12
        # ------------------------------------------------------------------
        print("\n--- Suite 4: Host Python `pynltp` <-> COM9 SLIP L2 Tunnel <-> ESP-NOW v2 <-> Off-Wi-Fi COM12 ---")
        com12.cmd("txkbps 65")  # Smoothly pace COM12 -> COM9 115200-baud UART bridge
        com9.cmd(f"tunnel_on {mac12}", "OK tunnel=on")
        time.sleep(0.05)
        com9.ser.reset_input_buffer()

        slip_dec = SlipDecoder()
        py_tx_frames: List[int] = []
        py_rx_frames: List[int] = []

        def py_send_to_espnow(raw_nltp_pkt: bytes, _peer: Tuple[Any, ...]) -> None:
            py_tx_frames.append(len(raw_nltp_pkt))
            com9.ser.write(slip_encode(raw_nltp_pkt))

        py_ep = NLTPEndpoint(
            send_packet_fn=py_send_to_espnow,
            psk=SHARED_PSK,
            max_tx_kbps=65.0,
            adaptive_rate=False,
            keepalive_s=0.0,
        )
        esp_peer_addr = ("espnow_v2_com12", 0)

        def pump_slip(duration_s: float = 0.012) -> None:
            t_end = time.perf_counter() + duration_s
            while time.perf_counter() < t_end:
                chunk = com9.ser.read(4096)
                if chunk:
                    for frame in slip_dec.feed(chunk):
                        if len(frame) >= 16 and frame[:2] == b"NL":
                            py_rx_frames.append(len(frame))
                            py_ep.on_datagram(frame, esp_peer_addr)
                py_ep.step_tx()
                com12.drain_lines()
                time.sleep(0.002)

        # 4A-1: Python -> ESP-NOW v2 -> Off-Wi-Fi COM12 (4 B reflex/call stream)
        for py_sz in (4, 8192):
            com12.clear()
            py_tx_frames.clear()
            payload = b"PYV2" if py_sz == 4 else deterministic_pattern(py_sz, 0x51510000 + py_sz)
            t0 = time.perf_counter()
            xfer = py_ep.start_transfer(esp_peer_addr, payload, kind="file", filename=f"py_{py_sz}.bin")
            node_rx_kv: Optional[Dict[str, str]] = None
            while (time.perf_counter() - t0) < 15.0:
                pump_slip(0.015)
                if node_rx_kv is None:
                    for ln in com12.lines:
                        if ln.startswith("NODE_RX"):
                            node_rx_kv = parse_kv(ln)
                            break
                if xfer.completed and node_rx_kv is not None:
                    break
            dt_ms = (time.perf_counter() - t0) * 1000.0
            assert xfer.completed and xfer.verified_sha256_by_peer, f"Python TX not verified for {py_sz} B"
            assert node_rx_kv is not None and int(node_rx_kv["sha_ok"]) == 1 and int(node_rx_kv["len"]) == py_sz, node_rx_kv
            max_wire = max(py_tx_frames) if py_tx_frames else 0
            if py_sz >= 1024:
                assert max_wire == 1010 and int(node_rx_kv["max_l2"]) == 1010, (max_wire, node_rx_kv)
            print(f"  [Python -> ESP-NOW v2 -> COM12] {py_sz} B verified in {dt_ms:.1f} ms (max_l2={max_wire} B)")
            results["suite4_python_slip_espnow_v2_e2e"].append({
                "direction": "Host_Python_pynltp -> SLIP_COM9 -> ESPNowV2 -> OffWiFi_Arduino_COM12",
                "bytes": py_sz,
                "ms": round(dt_ms, 2),
                "python_fin_ack_verified": xfer.verified_sha256_by_peer,
                "esp32_sha256_ok": int(node_rx_kv["sha_ok"]) == 1,
                "max_espnow_v2_frame_bytes": max_wire,
            })

        # 4B: Off-Wi-Fi COM12 -> ESP-NOW v2 -> SLIP -> Host Python (8,192 B = 9 x 1,010 B frames)
        com12.clear()
        py_rx_frames.clear()
        py_ep.completed_inbox.clear()
        expected_8k = deterministic_pattern(8192, 0xCAFE0000 + 8192)
        t0 = time.perf_counter()
        com12.cmd("send 8192", "OK send_started")
        node_tx_kv: Optional[Dict[str, str]] = None
        while (time.perf_counter() - t0) < 15.0:
            pump_slip(0.015)
            if node_tx_kv is None:
                for ln in com12.lines:
                    if ln.startswith("NODE_TX"):
                        node_tx_kv = parse_kv(ln)
                        break
            if py_ep.completed_inbox and node_tx_kv is not None:
                break
        dt_ms = (time.perf_counter() - t0) * 1000.0
        assert len(py_ep.completed_inbox) >= 1, "Python did not receive 8192 B stream from COM12"
        rx_item = py_ep.completed_inbox[-1]
        assert rx_item.sha256_verified and rx_item.data == expected_8k, "Byte-for-byte mismatch in Python RX!"
        assert node_tx_kv is not None and int(node_tx_kv["ok"]) == 1, node_tx_kv
        max_rx_wire = max(py_rx_frames) if py_rx_frames else 0
        assert max_rx_wire == 1010, f"Expected 1010 B ESP-NOW v2 shard from COM12, got {max_rx_wire}"
        print(f"  [COM12 -> ESP-NOW v2 -> Python] 8192 B bit-exact + SHA-256 verified in {dt_ms:.1f} ms (max_l2={max_rx_wire} B)")
        results["suite4_python_slip_espnow_v2_e2e"].append({
            "direction": "OffWiFi_Arduino_COM12 -> ESPNowV2 -> SLIP_COM9 -> Host_Python_pynltp",
            "bytes": 8192,
            "ms": round(dt_ms, 2),
            "python_sha256_verified": rx_item.sha256_verified,
            "bit_exact_match": rx_item.data == expected_8k,
            "esp32_fin_ack_verified": int(node_tx_kv["ok"]) == 1,
            "max_espnow_v2_frame_bytes": max_rx_wire,
        })

        # Exit SLIP tunnel cleanly
        com9.ser.write(slip_encode(b"!tunnel_off"))
        com9.ser.flush()
        time.sleep(0.15)
        com9.drain_lines()
        com12.cmd("txkbps 25000")
        s9_end = parse_kv(com9.cmd("stat", "STAT"))
        assert s9_end["mode"] == "c99_hub", s9_end

        out_path = REPO_NLTP / "results" / "esp32_v2_interop_live.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_redact_macs(json.dumps(results, indent=2)), encoding="utf-8")
        print(f"\nALL 4 ESP-NOW v2 INTEROP SUITES PASSED! Saved -> {out_path}")
        return results
    finally:
        com9.close()
        com12.close()


if __name__ == "__main__":
    run_all()
