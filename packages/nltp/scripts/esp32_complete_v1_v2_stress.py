#!/usr/bin/env python3
"""
esp32_complete_v1_v2_stress.py — Complete Live Hardware Benchmark, Cross-Version Interop,
and Torture-Stress Suite for:
  1. Raw ESP-NOW v1 (<= 250 B frames, bare-metal)
  2. Raw ESP-NOW v2 (<= 1,470 B frames, bare-metal)
  3. NLTP over ESP-NOW v1 (<= 250 B frames: 192 B shards = 242 B AEAD L2 frames)
  4. NLTP over ESP-NOW v2 (<= 1,470 B frames: 960 B shards = 1,010 B AEAD L2 frames)

Executes 5 comprehensive hardware suites across COM9 (C99 Hub + Fuzzer + SLIP Bridge),
COM12 (Arduino NLTPESPNowNode), and Host Laptop Python (`necklink.nltp.NLTPEndpoint`).
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import serial

REPO_NLTP = Path(__file__).resolve().parents[1]
if str(REPO_NLTP) not in sys.path:
    sys.path.insert(0, str(REPO_NLTP))

from necklink.nltp import NLTPEndpoint  # noqa: E402

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
    def __init__(self, port: str, name: str, verbose: bool = True) -> None:
        self.port = port
        self.name = name
        self.verbose = verbose
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 115200
        self.ser.timeout = 0.012
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
                    if self.verbose:
                        print(f"  [{self.name}] {s}")
        return new_lines

    def clear(self) -> None:
        self.drain_lines()
        self.lines.clear()

    def send_nowait(self, text: str) -> None:
        self.ser.write((text.strip() + "\n").encode("ascii"))
        self.ser.flush()

    def cmd(self, text: str, expect_prefix: str = "OK", timeout: float = 3.0) -> str:
        self.drain_lines()
        start_idx = len(self.lines)
        self.send_nowait(text)
        t0 = time.perf_counter()
        while (time.perf_counter() - t0) < timeout:
            self.drain_lines()
            for line in self.lines[start_idx:]:
                if line.startswith(expect_prefix):
                    return line
            time.sleep(0.006)
        raise TimeoutError(f"{self.name} ({self.port}) timed out waiting for '{expect_prefix}' on cmd '{text}'")


def wait_pair_events(
    b_tx: BoardSerial,
    tx_prefix: str,
    b_rx: BoardSerial,
    rx_prefix: str,
    timeout: float = 15.0,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    got_tx: Optional[Dict[str, str]] = None
    got_rx: Optional[Dict[str, str]] = None
    t0 = time.perf_counter()
    while (time.perf_counter() - t0) < timeout:
        b_tx.drain_lines()
        b_rx.drain_lines()
        if got_tx is None:
            for ln in b_tx.lines:
                if ln.startswith(tx_prefix):
                    got_tx = parse_kv(ln)
                    break
        if got_rx is None:
            for ln in b_rx.lines:
                if ln.startswith(rx_prefix):
                    got_rx = parse_kv(ln)
                    break
        if got_tx is not None and got_rx is not None:
            return got_tx, got_rx
        time.sleep(0.005)
    raise TimeoutError(f"Timeout waiting for {tx_prefix} (got={got_tx}) and {rx_prefix} (got={got_rx})")


def run_raw_trial(b_tx: BoardSerial, b_rx: BoardSerial, ver: int, size: int, cmd_str: str, timeout: float = 3.5) -> Dict[str, Any]:
    b_tx.clear()
    b_rx.clear()
    tx_line = b_tx.cmd(cmd_str, "RAW_TX", timeout=timeout)
    tx_kv = parse_kv(tx_line)
    # Give RX up to 120 ms to flush RAW_RX line if any chunk arrived
    t_rx = time.perf_counter()
    rx_kv: Optional[Dict[str, str]] = None
    while (time.perf_counter() - t_rx) < 0.14:
        b_rx.drain_lines()
        for ln in b_rx.lines:
            if ln.startswith("RAW_RX"):
                rx_kv = parse_kv(ln)
                break
        if rx_kv is not None:
            break
        time.sleep(0.008)

    rcvd_str = rx_kv["len"].split("/")[0] if rx_kv else tx_kv["rcvd"].split("/")[0]
    pkts_str = rx_kv["pkts"] if rx_kv else "0/0"
    hash_ok = (int(rx_kv["hash_ok"]) == 1) if rx_kv else (int(tx_kv["ok"]) == 1)
    max_tx_l2 = int(tx_kv["max_tx_l2"])
    if ver == 1:
        assert max_tx_l2 <= 250, f"Raw ESP-NOW v1 exceeded 250 B: {max_tx_l2}"
    else:
        assert max_tx_l2 <= 1470, f"Raw ESP-NOW v2 exceeded 1470 B: {max_tx_l2}"

    return {
        "protocol": f"Raw_ESPNow_v{ver}",
        "bytes": size,
        "rcvd_bytes": int(rcvd_str),
        "pkts": pkts_str,
        "intact": hash_ok,
        "ack_rcvd": int(tx_kv["ack"]) == 1,
        "ms": float(tx_kv["ms"]),
        "mbps": float(tx_kv["mbps"]) if hash_ok else 0.0,
        "raw_wire_mbps": float(tx_kv["mbps"]),
        "max_tx_l2_bytes": max_tx_l2,
    }


def run_nltp_trial(
    b_tx: BoardSerial,
    b_rx: BoardSerial,
    v1_mode: bool,
    size: int,
    send_cmd: str,
    ok_prefix: str,
    tx_ev_prefix: str,
    rx_ev_prefix: str,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    b_tx.cmd(f"v1mode {1 if v1_mode else 0}")
    b_tx.clear()
    b_rx.clear()
    b_tx.cmd(send_cmd, ok_prefix)
    tx_kv, rx_kv = wait_pair_events(b_tx, tx_ev_prefix, b_rx, rx_ev_prefix, timeout=timeout)
    sha_ok = int(rx_kv["sha_ok"]) == 1
    fin_ok = int(tx_kv["ok"]) == 1
    max_tx_l2 = int(tx_kv["max_tx_l2"])
    if v1_mode:
        assert max_tx_l2 <= 250, f"NLTP ESP-NOW v1 exceeded 250 B: {max_tx_l2}"
    else:
        assert max_tx_l2 <= 1470, f"NLTP ESP-NOW v2 exceeded 1470 B: {max_tx_l2}"
    assert sha_ok and fin_ok, f"NLTP integrity failure: tx={tx_kv}, rx={rx_kv}"
    return {
        "protocol": "NLTP_ESPNow_v1" if v1_mode else "NLTP_ESPNow_v2",
        "bytes": size,
        "rcvd_bytes": size,
        "intact": sha_ok and fin_ok,
        "ms": float(tx_kv["ms"]),
        "mbps": float(tx_kv["mbps"]),
        "max_tx_l2_bytes": max_tx_l2,
        "rssi_dbm": int(rx_kv["rssi"]),
    }


def percentile(vals: List[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return round(s[idx], 2)


def run_complete_suite() -> Dict[str, Any]:
    com9 = BoardSerial("COM9", "COM9-C99Hub")
    com12 = BoardSerial("COM12", "COM12-Arduino")
    try:
        time.sleep(0.4)
        com9.send_nowait("")
        com12.send_nowait("")
        time.sleep(0.1)
        com9.clear()
        com12.clear()

        s9 = parse_kv(com9.cmd("stat", "STAT"))
        s12 = parse_kv(com12.cmd("stat", "STAT"))
        assert int(s9["enow_v"]) == 2 and int(s12["enow_v"]) == 2
        assert int(s9["mtu"]) == 1470 and int(s12["mtu"]) == 1470

        mac9 = s9["mac"].lower()
        mac12 = s12["mac"].lower()

        def reset_clean_state() -> None:
            com9.cmd("allow all")
            com9.cmd("drop 0")
            com9.cmd("rxmtu 1470")
            com9.cmd("v1mode 0")
            com9.cmd(f"psk {SHARED_PSK}")
            com9.cmd("rate 4")
            com12.cmd("drop 0")
            com12.cmd("rxmtu 1470")
            com12.cmd("v1mode 0")
            com12.cmd(f"psk {SHARED_PSK}")
            com12.cmd("rate 4")
            com12.cmd("txkbps 25000")
            com12.cmd(f"peer {mac9}")
            time.sleep(0.20)
            # Ensure bidirectional ESP-NOW peer entry & PHY rate table are primed
            com9.cmd(f"raw_send 1 {mac12} 4", "RAW_TX")
            com12.clear()
            com9.clear()

        reset_clean_state()

        report: Dict[str, Any] = {
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hardware": {
                "COM9": {"chip": "ESP32-D0WDQ6", "role": "C99 nltp_espnow_hub_t + Raw v1/v2 + Fuzzer + SLIP", "mac": mac9, "espnow_v": 2, "mtu": 1470},
                "COM12": {"chip": "ESP32-D0WD-V3", "role": "Arduino NLTPESPNowNode + Raw v1/v2", "mac": mac12, "espnow_v": 2, "mtu": 1470},
            },
            "cat1_4way_protocol_matrix_0pct_loss": [],
            "cat2_7mode_phy_rate_sweep": [],
            "cat3_packet_loss_sweep_16kb": [],
            "cat4_interop_matrix": {},
            "cat5_stress_and_security_torture": {},
        }

        # =====================================================================
        # CATEGORY 1: 4-Protocol Head-to-Head Matrix (0% Loss, AUTO / HT20 MCS7)
        # =====================================================================
        print("\n=== CATEGORY 1: 4-Protocol Head-to-Head (Raw v1 vs Raw v2 vs NLTP v1 vs NLTP v2) ===")
        for sz in (4, 193, 1024, 4096, 16384, 32768):
            r_v1 = run_raw_trial(com12, com9, 1, sz, f"raw_send 1 {sz}")
            r_v2 = run_raw_trial(com12, com9, 2, sz, f"raw_send 2 {sz}")
            n_v1 = run_nltp_trial(com12, com9, True, sz, f"send {sz}", "OK send_started", "NODE_TX", "HUB_RX")
            n_v2 = run_nltp_trial(com12, com9, False, sz, f"send {sz}", "OK send_started", "NODE_TX", "HUB_RX")
            report["cat1_4way_protocol_matrix_0pct_loss"].extend([r_v1, r_v2, n_v1, n_v2])

        # =====================================================================
        # CATEGORY 2: All 7 PHY Rate Modes Sweep (1M, 11M, MCS4, MCS7, AUTO, LR 250K, LR 500K)
        # =====================================================================
        print("\n=== CATEGORY 2: 7-Mode PHY Rate Sweep Across All 4 Protocols ===")
        phy_modes = [
            (0, "802.11b_1M", 4096),
            (1, "802.11b_11M", 16384),
            (2, "HT20_MCS4", 32768),
            (3, "HT20_MCS7", 32768),
            (4, "AUTO_RSSI", 32768),
            (5, "LR_250K", 4096),
            (6, "LR_500K", 4096),
        ]
        for rate_id, rate_name, bulk_sz in phy_modes:
            com9.cmd(f"rate {rate_id}")
            com12.cmd(f"rate {rate_id}")
            time.sleep(0.08)

            # 4 B call latency + bulk goodput across all 4 protocols
            rv1_4 = run_raw_trial(com12, com9, 1, 4, "raw_send 1 4")
            rv2_4 = run_raw_trial(com12, com9, 2, 4, "raw_send 2 4")
            nv1_4 = run_nltp_trial(com12, com9, True, 4, "send 4", "OK send_started", "NODE_TX", "HUB_RX")
            nv2_4 = run_nltp_trial(com12, com9, False, 4, "send 4", "OK send_started", "NODE_TX", "HUB_RX")

            rv1_b = run_raw_trial(com12, com9, 1, bulk_sz, f"raw_send 1 {bulk_sz}", timeout=5.0)
            rv2_b = run_raw_trial(com12, com9, 2, bulk_sz, f"raw_send 2 {bulk_sz}", timeout=5.0)
            nv1_b = run_nltp_trial(com12, com9, True, bulk_sz, f"send {bulk_sz}", "OK send_started", "NODE_TX", "HUB_RX", timeout=20.0)
            nv2_b = run_nltp_trial(com12, com9, False, bulk_sz, f"send {bulk_sz}", "OK send_started", "NODE_TX", "HUB_RX", timeout=20.0)

            report["cat2_7mode_phy_rate_sweep"].append({
                "rate_id": rate_id,
                "rate_name": rate_name,
                "call_4B_ms": {
                    "raw_v1": rv1_4["ms"],
                    "raw_v2": rv2_4["ms"],
                    "nltp_v1": nv1_4["ms"],
                    "nltp_v2": nv2_4["ms"],
                },
                "bulk_bytes": bulk_sz,
                "bulk_mbps": {
                    "raw_v1": rv1_b["mbps"],
                    "raw_v2": rv2_b["mbps"],
                    "nltp_v1": nv1_b["mbps"],
                    "nltp_v2": nv2_b["mbps"],
                },
                "bulk_ms": {
                    "raw_v1": rv1_b["ms"],
                    "raw_v2": rv2_b["ms"],
                    "nltp_v1": nv1_b["ms"],
                    "nltp_v2": nv2_b["ms"],
                },
                "bulk_intact": {
                    "raw_v1": rv1_b["intact"],
                    "raw_v2": rv2_b["intact"],
                    "nltp_v1": nv1_b["intact"],
                    "nltp_v2": nv2_b["intact"],
                },
            })

        reset_clean_state()

        # =====================================================================
        # CATEGORY 3: Packet-Loss Resilience Sweep (0%, 5%, 15%, 25%, 35% Drop, 16 KB, 3x Trials)
        # =====================================================================
        print("\n=== CATEGORY 3: Packet-Loss Sweep (0%, 5%, 15%, 25%, 35% RX Drop on 16 KB, 3x Trials) ===")
        N_LOSS_TRIALS = 3
        for drop_pct in (0, 5, 15, 25, 35):
            com12.cmd(f"drop {drop_pct}")
            rv1_runs, rv2_runs, nv1_runs, nv2_runs = [], [], [], []
            for _ in range(N_LOSS_TRIALS):
                # Test COM9 -> COM12 across all 4 protocols so RX drop is applied identically on COM12!
                rv1_runs.append(run_raw_trial(com9, com12, 1, 16384, f"raw_send 1 {mac12} 16384"))
                rv2_runs.append(run_raw_trial(com9, com12, 2, 16384, f"raw_send 2 {mac12} 16384"))
                nv1_runs.append(run_nltp_trial(com9, com12, True, 16384, f"hub_send {mac12} 16384", "OK hub_send_started", "HUB_TX", "NODE_RX", timeout=25.0))
                nv2_runs.append(run_nltp_trial(com9, com12, False, 16384, f"hub_send {mac12} 16384", "OK hub_send_started", "HUB_TX", "NODE_RX", timeout=25.0))

            def _agg_raw(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
                ok_cnt = sum(1 for r in runs if r["intact"])
                return {
                    "delivery_rate": f"{ok_cnt}/{len(runs)}",
                    "intact_pct": round(100.0 * ok_cnt / len(runs), 1),
                    "avg_rcvd_bytes": int(round(sum(r["rcvd_bytes"] for r in runs) / len(runs))),
                    "avg_ms": round(sum(r["ms"] for r in runs) / len(runs), 2),
                    "effective_goodput_mbps": round(sum(r["mbps"] for r in runs) / len(runs), 3),
                }

            def _agg_nltp(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
                ok_cnt = sum(1 for r in runs if r["intact"])
                return {
                    "delivery_rate": f"{ok_cnt}/{len(runs)}",
                    "intact_pct": round(100.0 * ok_cnt / len(runs), 1),
                    "avg_rcvd_bytes": 16384,
                    "avg_ms": round(sum(r["ms"] for r in runs) / len(runs), 2),
                    "effective_goodput_mbps": round(sum(r["mbps"] for r in runs) / len(runs), 3),
                    "max_tx_l2": max(r["max_tx_l2_bytes"] for r in runs),
                }

            report["cat3_packet_loss_sweep_16kb"].append({
                "drop_pct": drop_pct,
                "trials": N_LOSS_TRIALS,
                "raw_v1": _agg_raw(rv1_runs),
                "raw_v2": _agg_raw(rv2_runs),
                "nltp_v1": _agg_nltp(nv1_runs),
                "nltp_v2": _agg_nltp(nv2_runs),
            })
        reset_clean_state()

        # =====================================================================
        # CATEGORY 4: Complete Cross-Version, Cross-Language & Crypto Interop Matrix
        # =====================================================================
        print("\n=== CATEGORY 4: Complete Cross-Version (v1<->v2), Cross-Language & Crypto Interop ===")
        interop: Dict[str, Any] = {}

        # 4.1 Asymmetric Arduino v1 (242 B) -> C99 Hub v2 (default 960 B rx)
        com9.cmd("v1mode 0")
        com9.cmd("reset_max")
        asym_ard_v1_to_c99_v2 = run_nltp_trial(com12, com9, True, 16384, "send 16384", "OK send_started", "NODE_TX", "HUB_RX")
        assert asym_ard_v1_to_c99_v2["max_tx_l2_bytes"] == 242
        interop["arduino_v1_242B_to_c99_hub_v2_auto"] = asym_ard_v1_to_c99_v2

        # 4.2 Asymmetric C99 Hub v1 (242 B) -> Arduino Node v2 (default 960 B rx)
        com12.cmd("v1mode 0")
        com12.cmd("reset_max")
        asym_c99_v1_to_ard_v2 = run_nltp_trial(com9, com12, True, 16384, f"hub_send {mac12} 16384", "OK hub_send_started", "HUB_TX", "NODE_RX")
        assert asym_c99_v1_to_ard_v2["max_tx_l2_bytes"] == 242
        interop["c99_hub_v1_242B_to_arduino_v2_auto"] = asym_c99_v1_to_ard_v2

        # 4.3 Strict 250 B ESP-NOW v1 Hardware Receiver Cap (`rxmtu 250`)
        com12.cmd("rxmtu 250")
        com12.cmd("v1mode 1")
        com9.cmd("v1mode 0")  # COM9 sends 1,010 B v2 shards -> must be rejected by strict 250 B v1 cap!
        com9.clear()
        com12.clear()
        com9.cmd(f"hub_send {mac12} 4096", "OK hub_send_started")
        time.sleep(0.6)
        com12.drain_lines()
        s12_strict = parse_kv(com12.cmd("stat", "STAT"))
        v2_mtu_drops = int(s12_strict["mtu_drops"])
        v2_blocked_on_strict_v1 = (v2_mtu_drops > 0) and not any(ln.startswith("NODE_RX") for ln in com12.lines)
        assert v2_blocked_on_strict_v1, s12_strict
        com9.cmd("cancel_tx")

        # Switch COM9 to v1mode=1 (242 B frames) -> strict 250 B receiver accepts 16 KB with 0 MTU drops!
        com12.cmd("reset_max")
        strict_v1_ok = run_nltp_trial(com9, com12, True, 16384, f"hub_send {mac12} 16384", "OK hub_send_started", "HUB_TX", "NODE_RX")
        s12_after = parse_kv(com12.cmd("stat", "STAT"))
        assert int(s12_after["mtu_drops"]) == 0 and int(s12_after["max_l2"]) <= 250
        interop["strict_250B_v1_hardware_cap"] = {
            "oversized_1010B_v2_rejected": v2_blocked_on_strict_v1,
            "oversized_mtu_drops": v2_mtu_drops,
            "v1_242B_16KB_verified": strict_v1_ok,
            "post_v1_mtu_drops": int(s12_after["mtu_drops"]),
        }
        reset_clean_state()

        # 4.4 Plaintext (`psk off`, CRC32) Interop in BOTH v1 (222 B) and v2 (990 B)
        com9.cmd("psk off")
        com12.cmd("psk off")
        pt_v1 = run_nltp_trial(com12, com9, True, 16384, "send 16384", "OK send_started", "NODE_TX", "HUB_RX")
        pt_v2 = run_nltp_trial(com12, com9, False, 16384, "send 16384", "OK send_started", "NODE_TX", "HUB_RX")
        assert pt_v1["max_tx_l2_bytes"] == 222 and pt_v2["max_tx_l2_bytes"] == 990
        interop["plaintext_crc32_v1_and_v2"] = {"v1_222B": pt_v1, "v2_990B": pt_v2}
        reset_clean_state()

        # 4.5 Host Python `pynltp` <-> SLIP COM9 <-> Off-Wi-Fi ESP32 COM12 in BOTH v2 (960B) AND Strict v1 (192B <= 250B)!
        python_runs: List[Dict[str, Any]] = []
        for py_mode_name, py_shard, strict_v1 in [("ESPNow_v2_960B", 960, False), ("ESPNow_v1_192B_Strict250B", 192, True)]:
            com12.cmd(f"v1mode {1 if strict_v1 else 0}")
            com12.cmd(f"rxmtu {250 if strict_v1 else 1470}")
            com12.cmd("reset_max")
            com12.cmd("txkbps 65")
            com9.cmd(f"tunnel_on {mac12}", "OK tunnel=on")
            time.sleep(0.05)
            com9.ser.reset_input_buffer()

            slip_dec = SlipDecoder()
            py_tx_lens: List[int] = []
            py_rx_lens: List[int] = []

            def py_send_cb(raw_pkt: bytes, _peer: Tuple[Any, ...]) -> None:
                py_tx_lens.append(len(raw_pkt))
                com9.ser.write(slip_encode(raw_pkt))

            py_ep = NLTPEndpoint(
                send_packet_fn=py_send_cb,
                shard_bytes=py_shard,
                psk=SHARED_PSK,
                max_tx_kbps=65.0,
                adaptive_rate=False,
                keepalive_s=0.0,
            )
            peer_tag = ("espnow_peer", 0)

            def pump(dur: float = 0.014) -> None:
                t_e = time.perf_counter() + dur
                while time.perf_counter() < t_e:
                    ch = com9.ser.read(4096)
                    if ch:
                        for fr in slip_dec.feed(ch):
                            if len(fr) >= 16 and fr[:2] == b"NL":
                                py_rx_lens.append(len(fr))
                                py_ep.on_datagram(fr, peer_tag)
                    py_ep.step_tx()
                    com12.drain_lines()
                    time.sleep(0.002)

            test_len = 4096
            # Python -> ESP32
            com12.clear()
            py_tx_lens.clear()
            tx_payload = deterministic_pattern(test_len, 0xABCD0000 + py_shard)
            t0 = time.perf_counter()
            xfer = py_ep.start_transfer(peer_tag, tx_payload, kind="file", filename="py.bin")
            nrx: Optional[Dict[str, str]] = None
            while (time.perf_counter() - t0) < 15.0:
                pump(0.015)
                if nrx is None:
                    for ln in com12.lines:
                        if ln.startswith("NODE_RX"):
                            nrx = parse_kv(ln)
                            break
                if xfer.completed and nrx is not None:
                    break
            dt_py_tx = round((time.perf_counter() - t0) * 1000.0, 2)
            assert xfer.completed and xfer.verified_sha256_by_peer and nrx is not None and int(nrx["sha_ok"]) == 1
            max_py_tx = max(py_tx_lens)
            if strict_v1:
                assert max_py_tx <= 250, f"Python v1 frame exceeded 250 B: {max_py_tx}"
            else:
                assert max_py_tx == 1010

            # ESP32 -> Python
            com12.clear()
            py_rx_lens.clear()
            py_ep.completed_inbox.clear()
            exp_esp = deterministic_pattern(test_len, 0xCAFE0000 + test_len)
            t0 = time.perf_counter()
            com12.cmd(f"send {test_len}", "OK send_started")
            ntx: Optional[Dict[str, str]] = None
            while (time.perf_counter() - t0) < 15.0:
                pump(0.015)
                if ntx is None:
                    for ln in com12.lines:
                        if ln.startswith("NODE_TX"):
                            ntx = parse_kv(ln)
                            break
                if py_ep.completed_inbox and ntx is not None:
                    break
            dt_py_rx = round((time.perf_counter() - t0) * 1000.0, 2)
            assert py_ep.completed_inbox and py_ep.completed_inbox[-1].data == exp_esp
            assert ntx is not None and int(ntx["ok"]) == 1
            max_py_rx = max(py_rx_lens)
            if strict_v1:
                assert max_py_rx <= 250

            com9.ser.write(slip_encode(b"!tunnel_off"))
            com9.ser.flush()
            time.sleep(0.15)
            com9.clear()

            s12_py = parse_kv(com12.cmd("stat", "STAT"))
            if strict_v1:
                assert int(s12_py["mtu_drops"]) == 0

            python_runs.append({
                "mode": py_mode_name,
                "shard_bytes": py_shard,
                "bytes_each_way": test_len,
                "py_to_esp_ms": dt_py_tx,
                "esp_to_py_ms": dt_py_rx,
                "max_tx_l2_bytes": max_py_tx,
                "max_rx_l2_bytes": max_py_rx,
                "bit_exact_both_ways": True,
            })

        interop["python_slip_espnow_v1_and_v2_bidirectional"] = python_runs
        report["cat4_interop_matrix"] = interop
        reset_clean_state()

        # =====================================================================
        # CATEGORY 5: Hardware Stress, Full-Duplex Collision, Churn & Fuzz Suite
        # =====================================================================
        print("\n=== CATEGORY 5: Hardware Torture Stress, Full-Duplex Collision & Adversarial Radio Fuzz ===")
        stress: Dict[str, Any] = {}

        # 5.1 Simultaneous Full-Duplex Bi-Directional 32 KB Collision Stress (5 rounds, round 5 at 10% loss!)
        print("\n--- Stress 5.1: 5x Simultaneous Full-Duplex 32 KB Collisions (320 KB Concurrent Air Traffic) ---")
        duplex_rounds = []
        for r_idx in range(1, 6):
            loss = 10 if r_idx == 5 else 0
            com9.cmd(f"drop {loss}")
            com12.cmd(f"drop {loss}")
            com9.clear()
            com12.clear()
            t0 = time.perf_counter()
            # Fire BOTH 32 KB transfers simultaneously!
            com12.send_nowait("send 32768")
            com9.send_nowait(f"hub_send {mac12} 32768")

            got = {"NODE_TX": None, "HUB_RX": None, "HUB_TX": None, "NODE_RX": None}
            while (time.perf_counter() - t0) < 20.0:
                com9.drain_lines()
                com12.drain_lines()
                for ln in com12.lines:
                    if ln.startswith("NODE_TX") and got["NODE_TX"] is None:
                        got["NODE_TX"] = parse_kv(ln)
                    elif ln.startswith("NODE_RX") and got["NODE_RX"] is None:
                        got["NODE_RX"] = parse_kv(ln)
                for ln in com9.lines:
                    if ln.startswith("HUB_TX") and got["HUB_TX"] is None:
                        got["HUB_TX"] = parse_kv(ln)
                    elif ln.startswith("HUB_RX") and got["HUB_RX"] is None:
                        got["HUB_RX"] = parse_kv(ln)
                if all(v is not None for v in got.values()):
                    break
                time.sleep(0.005)

            wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            assert all(v is not None for v in got.values()), f"Full-duplex timeout on round {r_idx}: {got}"
            assert int(got["NODE_TX"]["ok"]) == 1 and int(got["HUB_RX"]["sha_ok"]) == 1
            assert int(got["HUB_TX"]["ok"]) == 1 and int(got["NODE_RX"]["sha_ok"]) == 1
            agg_mbps = round((65536 * 8.0) / (wall_ms * 1000.0), 3)
            duplex_rounds.append({
                "round": r_idx,
                "loss_pct": loss,
                "wall_ms": wall_ms,
                "ard_to_hub_ms": float(got["NODE_TX"]["ms"]),
                "hub_to_ard_ms": float(got["HUB_TX"]["ms"]),
                "aggregate_duplex_mbps": agg_mbps,
                "both_sha256_ok": True,
            })
        reset_clean_state()
        stress["full_duplex_32kb_collision_rounds"] = duplex_rounds

        # 5.2 50-Stream Zero-Pause Rapid-Fire Soak + Heap-Leak Audit (alternating v1 and v2!)
        print("\n--- Stress 5.2: 50-Stream Rapid-Fire Soak + Heap-Leak Audit ---")
        com12.verbose = False
        com9.verbose = False
        # Warm-up 1 stream so hub slot is active before recording baseline steady heap
        run_nltp_trial(com12, com9, False, 64, "send 64", "OK send_started", "NODE_TX", "HUB_RX")
        heap9_before = int(parse_kv(com9.cmd("stat", "STAT"))["heap"])
        heap12_before = int(parse_kv(com12.cmd("stat", "STAT"))["heap"])

        soak_sizes = [4, 64, 256, 1024, 4096]
        soak_latencies_v1: List[float] = []
        soak_latencies_v2: List[float] = []
        for idx in range(50):
            sz = soak_sizes[idx % len(soak_sizes)]
            use_v1 = (idx % 2 == 1)
            res = run_nltp_trial(com12, com9, use_v1, sz, f"send {sz}", "OK send_started", "NODE_TX", "HUB_RX")
            if use_v1:
                soak_latencies_v1.append(res["ms"])
            else:
                soak_latencies_v2.append(res["ms"])

        heap9_after = int(parse_kv(com9.cmd("stat", "STAT"))["heap"])
        heap12_after = int(parse_kv(com12.cmd("stat", "STAT"))["heap"])
        com12.verbose = True
        com9.verbose = True
        print(f"  50/50 streams verified! Heap COM9: {heap9_before} -> {heap9_after} B | COM12: {heap12_before} -> {heap12_after} B")
        assert abs(heap9_after - heap9_before) <= 128, f"Heap leak on COM9: {heap9_before} -> {heap9_after}"
        assert abs(heap12_after - heap12_before) <= 128, f"Heap leak on COM12: {heap12_before} -> {heap12_after}"

        stress["rapid_fire_50_streams_soak"] = {
            "streams_verified": 50,
            "v1_25_streams_ms": {
                "p50": percentile(soak_latencies_v1, 50),
                "p95": percentile(soak_latencies_v1, 95),
                "max": max(soak_latencies_v1),
            },
            "v2_25_streams_ms": {
                "p50": percentile(soak_latencies_v2, 50),
                "p95": percentile(soak_latencies_v2, 95),
                "max": max(soak_latencies_v2),
            },
            "com9_heap_before": heap9_before,
            "com9_heap_after": heap9_after,
            "com9_heap_delta_bytes": heap9_after - heap9_before,
            "com12_heap_before": heap12_before,
            "com12_heap_after": heap12_after,
            "com12_heap_delta_bytes": heap12_after - heap12_before,
        }
        reset_clean_state()

        # 5.3 20x Rapid Deep-Sleep `conn_id` Churn Stress
        print("\n--- Stress 5.3: 20x Rapid Deep-Sleep conn_id Rotations (`wake_reset`) ---")
        com12.verbose = False
        com9.verbose = False
        wake_ms: List[float] = []
        seen_cids = set()
        for _ in range(20):
            wl = com12.cmd("wake_reset", "OK wake_reset")
            seen_cids.add(parse_kv(wl)["cid"])
            com12.cmd(f"peer {mac9}")
            com9.clear()
            com12.clear()
            com12.cmd("send 4", "OK send_started")
            tx_ev, rx_ev = wait_pair_events(com12, "NODE_TX", com9, "HUB_RX", timeout=5.0)
            assert int(tx_ev["ok"]) == 1 and int(rx_ev["sha_ok"]) == 1
            wake_ms.append(float(tx_ev["ms"]))
        com12.verbose = True
        com9.verbose = True
        print(f"  20/20 deep-sleep wakes verified across {len(seen_cids)} unique conn_ids (p50={percentile(wake_ms, 50)} ms, max={max(wake_ms)} ms)")
        stress["deep_sleep_20x_conn_id_churn"] = {
            "wakes_verified": 20,
            "unique_conn_ids": len(seen_cids),
            "p50_ms": percentile(wake_ms, 50),
            "p95_ms": percentile(wake_ms, 95),
            "max_ms": max(wake_ms),
        }

        # 5.4 5x Mid-Stream Cancellation + Urgent 4 B Preemption Stress
        print("\n--- Stress 5.4: 5x Mid-Stream 32 KB Cancellation + Immediate 4 B Preemption ---")
        preempt_ms: List[float] = []
        for p_idx in range(5):
            com9.cmd("drop 20")
            com12.cmd("rate 0")  # 1 Mbps slow radio so 32 KB is guaranteed mid-flight after 20 ms
            com9.cmd("rate 0")
            com9.clear()
            com12.clear()
            com12.cmd("send 32768", "OK send_started")
            time.sleep(0.025)
            com12.cmd("cancel_tx")
            com9.cmd("drop 0")
            com12.cmd("rate 4")
            com9.cmd("rate 4")
            com9.clear()
            com12.clear()
            com12.cmd("send 4", "OK send_started")
            tx_ev, rx_ev = wait_pair_events(com12, "NODE_TX", com9, "HUB_RX", timeout=5.0)
            assert int(tx_ev["ok"]) == 1 and int(rx_ev["sha_ok"]) == 1
            preempt_ms.append(float(tx_ev["ms"]))
        print(f"  5/5 mid-stream cancellations preempted cleanly: {preempt_ms} ms")
        stress["mid_stream_cancel_preemption_5x"] = {
            "preemptions_verified": 5,
            "urgent_4B_ms": preempt_ms,
        }
        reset_clean_state()

        # 5.5 Live Over-the-Air Adversarial Radio Fuzz Flood (120 malicious frames + concurrent 16 KB stream)
        print("\n--- Stress 5.5: Over-the-Air Adversarial Radio Fuzz Flood (120 Forged/Tampered/Replay Frames) ---")
        s12_pre = parse_kv(com12.cmd("stat", "STAT"))
        auth0 = int(s12_pre["auth_drops"])
        com9.cmd(f"fuzz {mac12} 60", "OK fuzz_sent=60")
        com9.clear()
        com12.clear()
        # Launch legitimate 16 KB stream AND immediately blast 60 more adversarial frames mid-stream!
        com9.cmd(f"hub_send {mac12} 16384", "OK hub_send_started")
        com9.cmd(f"fuzz {mac12} 60", "OK fuzz_sent=60")
        tx_ev, rx_ev = wait_pair_events(com9, "HUB_TX", com12, "NODE_RX", timeout=15.0)
        assert int(tx_ev["ok"]) == 1 and int(rx_ev["sha_ok"]) == 1
        s12_post = parse_kv(com12.cmd("stat", "STAT"))
        auth_delta = int(s12_post["auth_drops"]) - auth0
        assert auth_delta > 0, f"Expected auth_drops to increment under fuzzing: {s12_pre} -> {s12_post}"
        print(f"  Adversarial fuzz flood survived! auth_drops caught={auth_delta}, legitimate 16 KB sha_ok=1 in {tx_ev['ms']} ms")
        stress["adversarial_radio_fuzz_120_frames"] = {
            "malicious_frames_transmitted": 120,
            "receiver_auth_drops_increment": auth_delta,
            "concurrent_16kb_sha256_ok": True,
            "concurrent_16kb_ms": float(tx_ev["ms"]),
        }

        report["cat5_stress_and_security_torture"] = stress

        out_path = REPO_NLTP / "results" / "esp32_complete_v1_v2_stress.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nALL 5 COMPLETE BENCHMARK, INTEROP & STRESS CATEGORIES PASSED! -> {out_path}")
        return report
    finally:
        com9.close()
        com12.close()


if __name__ == "__main__":
    run_complete_suite()
