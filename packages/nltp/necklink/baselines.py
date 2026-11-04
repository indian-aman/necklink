"""
Industry-Standard Telemetry & Data-Transfer Baselines, Channel Loss Models,
and Commercial Link Economics Calculators (Iridium SBD, LoRaWAN, Rural Cellular, Split-AI).
"""
from __future__ import annotations

import struct
import zlib
from typing import Dict, List, Optional, Tuple

import numpy as np

from .codec import c_div, clip_i32


# ---------------------------------------------------------------------------
# Packet-Loss Channel Simulators (Bernoulli + Gilbert-Elliott Burst Fading)
# ---------------------------------------------------------------------------

def simulate_packet_drops(
    n_steps: int,
    loss_rate: float,
    mode: str = "random",
    mean_burst_len: float = 8.0,
    seed: int = 42,
) -> np.ndarray:
    """
    Returns boolean array `dropped[t]` of length `n_steps` (True = packet lost).
    Supports:
      - mode='random': i.i.d. Bernoulli packet loss
      - mode='gilbert_elliott': 2-state Markov bursty wireless fading channel
        calibrated to target stationary `loss_rate` and `mean_burst_len`.
    """
    rng = np.random.default_rng(seed)
    if loss_rate <= 0.0:
        return np.zeros(n_steps, dtype=bool)
    if loss_rate >= 1.0:
        return np.ones(n_steps, dtype=bool)

    if mode == "random":
        return rng.random(n_steps) < loss_rate

    if mode == "gilbert_elliott":
        # Bad state drops with p_B = 0.92; Good state drops with p_G = 0.01
        p_g, p_b = 0.01, 0.92
        if loss_rate <= p_g:
            return rng.random(n_steps) < loss_rate
        pi_b = min(0.95, max(0.01, (loss_rate - p_g) / (p_b - p_g)))
        # Transition probabilities: r = P(B -> G) = 1 / mean_burst_len
        r_bg = 1.0 / max(1.5, mean_burst_len)
        p_gb = (pi_b * r_bg) / max(1e-6, (1.0 - pi_b))
        p_gb = min(0.95, max(1e-5, p_gb))

        dropped = np.zeros(n_steps, dtype=bool)
        in_bad = bool(rng.random() < pi_b)
        u_trans = rng.random(n_steps)
        u_drop = rng.random(n_steps)
        for t in range(n_steps):
            dropped[t] = u_drop[t] < (p_b if in_bad else p_g)
            if in_bad:
                if u_trans[t] < r_bg:
                    in_bad = False
            else:
                if u_trans[t] < p_gb:
                    in_bad = True
        return dropped

    raise ValueError(f"Unknown channel loss mode: {mode!r}")


# ---------------------------------------------------------------------------
# Baseline 1: Raw IEEE-754 Float32 over UDP (Zero-Order Hold on Loss)
# ---------------------------------------------------------------------------

class RawFloat32UDPCodec:
    def __init__(self, n_channels: int) -> None:
        self.n_channels = n_channels

    def run_stream(self, x_seq: np.ndarray, dropped: np.ndarray) -> Tuple[np.ndarray, int, int]:
        t_steps, n = x_seq.shape
        rx = np.zeros_like(x_seq, dtype=np.float64)
        last = np.zeros(n, dtype=np.float64)
        total_bytes = 0
        tx_pkts = 0
        pkt_size = 2 + 4 * n  # 2B seq + 4B*N float32
        for t in range(t_steps):
            pkt = struct.pack(f"<H{n}f", t & 0xFFFF, *x_seq[t].astype(np.float32))
            total_bytes += len(pkt)
            tx_pkts += 1
            if not dropped[t]:
                vals = struct.unpack(f"<H{n}f", pkt)[1:]
                last = np.array(vals, dtype=np.float64)
            rx[t] = last
        assert pkt_size == 2 + 4 * n
        return rx, total_bytes, tx_pkts


# ---------------------------------------------------------------------------
# Baseline 2: Standardized Int8 + Zlib (DEFLATE) 16-Tick Micro-Blocks over UDP
# ---------------------------------------------------------------------------

class Int8ZlibBlockCodec:
    def __init__(self, n_channels: int, obs_mean: np.ndarray, obs_std: np.ndarray, block_len: int = 16) -> None:
        self.n_channels = n_channels
        self.mean = np.asarray(obs_mean, dtype=np.float64)
        self.std = np.maximum(np.asarray(obs_std, dtype=np.float64), 1e-6)
        self.block_len = block_len

    def run_stream(self, x_seq: np.ndarray, dropped: np.ndarray) -> Tuple[np.ndarray, int, int]:
        t_steps, n = x_seq.shape
        rx = np.zeros_like(x_seq, dtype=np.float64)
        last = self.mean.copy()
        total_bytes = 0
        tx_pkts = 0

        for start in range(0, t_steps, self.block_len):
            end = min(t_steps, start + self.block_len)
            blk = x_seq[start:end]
            # Quantize +/- 3.2 std into int8 [-127, 127]
            z_norm = (blk - self.mean) / self.std
            q_blk = np.clip(np.round(z_norm * (127.0 / 3.2)), -127, 127).astype(np.int8)
            comp = zlib.compress(q_blk.tobytes(), level=6)
            pkt_bytes = 3 + len(comp)  # 2B block_seq + 1B len + compressed payload
            total_bytes += pkt_bytes
            tx_pkts += 1

            # Packet arrives or drops at end of block
            pkt_idx = end - 1
            if not dropped[pkt_idx]:
                dec_q = np.frombuffer(zlib.decompress(comp), dtype=np.int8).reshape(end - start, n)
                rec_blk = (dec_q.astype(np.float64) * (3.2 / 127.0)) * self.std + self.mean
                rx[start:end] = rec_blk
                last = rec_blk[-1].copy()
            else:
                rx[start:end] = last

        return rx, total_bytes, tx_pkts


# ---------------------------------------------------------------------------
# Baseline 3: Standard Neuromorphic / IoT Send-on-Delta (MQTT Sparkplug / AER)
# ---------------------------------------------------------------------------

class SendOnDeltaUDPCodec:
    """
    Standard event-triggered Send-on-Delta with identical deadband_q8 and quant_step_q8
    to NeckLink, using a non-leaky integrator (alpha = 1.0) without cryptic backup
    or disinhibitory gain rescue.
    """

    def __init__(
        self,
        n_channels: int,
        obs_mean: np.ndarray,
        obs_std: np.ndarray,
        deadband_q8: int = 32,
        quant_step_q8: int = 8,
    ) -> None:
        self.n_channels = n_channels
        self.mean_q8 = np.round(np.asarray(obs_mean, dtype=np.float64) * 256.0).astype(np.int32)
        std_safe = np.maximum(np.asarray(obs_std, dtype=np.float64), 1e-6)
        self.inv_std_q8 = np.clip(np.round(256.0 / std_safe), 1, 32767).astype(np.int32)
        self.std_q8 = np.clip(np.round(std_safe * 256.0), 1, 32767).astype(np.int32)
        self.deadband_q8 = deadband_q8
        self.quant_step_q8 = quant_step_q8

    def run_stream(self, x_seq: np.ndarray, dropped: np.ndarray) -> Tuple[np.ndarray, int, int]:
        t_steps, n = x_seq.shape
        tx_hat = np.zeros(n, dtype=np.int32)
        rx_hat = np.zeros(n, dtype=np.int32)
        rx_out = np.zeros_like(x_seq, dtype=np.float64)
        total_bytes = 0
        tx_pkts = 0
        half = self.quant_step_q8 // 2

        for t in range(t_steps):
            x_raw_q8 = np.round(x_seq[t] * 256.0).astype(np.int64)
            events: List[Tuple[int, int]] = []
            for i in range(n):
                zi = clip_i32(c_div(int(x_raw_q8[i] - self.mean_q8[i]) * int(self.inv_std_q8[i]), 256), -16384, 16383)
                ri = zi - int(tx_hat[i])
                if abs(ri) >= self.deadband_q8:
                    qi = ((ri + half) // self.quant_step_q8) if ri >= 0 else -(((-ri) + half) // self.quant_step_q8)
                    qi = clip_i32(qi, -127, 127)
                    if qi != 0:
                        events.append((i, qi))
                        tx_hat[i] = clip_i32(int(tx_hat[i]) + qi * self.quant_step_q8, -32768, 32767)

            if events:
                # 2B seq + 1B count + 2B * len(events)
                pkt_len = 3 + 2 * len(events)
                total_bytes += pkt_len
                tx_pkts += 1
                if not dropped[t]:
                    for idx, qi in events:
                        rx_hat[idx] = clip_i32(int(rx_hat[idx]) + qi * self.quant_step_q8, -32768, 32767)

            for i in range(n):
                xq = c_div(int(rx_hat[i]) * int(self.std_q8[i]), 256) + int(self.mean_q8[i])
                rx_out[t, i] = float(xq) / 256.0

        return rx_out, total_bytes, tx_pkts


# ---------------------------------------------------------------------------
# Baseline 4: Classical Telecom 4-Bit Leaky ADPCM over UDP
# ---------------------------------------------------------------------------

class ADPCMLeakyUDPCodec:
    """Fixed-rate 4-bit per channel leaky DPCM (packed 2 channels/byte + 2B seq)."""

    def __init__(self, n_channels: int, obs_mean: np.ndarray, obs_std: np.ndarray) -> None:
        self.n_channels = n_channels
        self.mean_q8 = np.round(np.asarray(obs_mean, dtype=np.float64) * 256.0).astype(np.int32)
        std_safe = np.maximum(np.asarray(obs_std, dtype=np.float64), 1e-6)
        self.inv_std_q8 = np.clip(np.round(256.0 / std_safe), 1, 32767).astype(np.int32)
        self.std_q8 = np.clip(np.round(std_safe * 256.0), 1, 32767).astype(np.int32)
        self.alpha_q8 = 242
        self.step_q8 = 28

    def run_stream(self, x_seq: np.ndarray, dropped: np.ndarray) -> Tuple[np.ndarray, int, int]:
        t_steps, n = x_seq.shape
        tx_m = np.zeros(n, dtype=np.int32)
        rx_m = np.zeros(n, dtype=np.int32)
        rx_out = np.zeros_like(x_seq, dtype=np.float64)
        pkt_bytes = 2 + (n + 1) // 2
        total_bytes = 0
        tx_pkts = 0

        for t in range(t_steps):
            x_raw_q8 = np.round(x_seq[t] * 256.0).astype(np.int64)
            q4 = np.zeros(n, dtype=np.int32)
            for i in range(n):
                tx_m[i] = c_div(self.alpha_q8 * int(tx_m[i]), 256)
                rx_m[i] = c_div(self.alpha_q8 * int(rx_m[i]), 256)
                zi = clip_i32(c_div(int(x_raw_q8[i] - self.mean_q8[i]) * int(self.inv_std_q8[i]), 256), -4096, 4095)
                ri = zi - int(tx_m[i])
                qi = clip_i32(int(round(ri / self.step_q8)), -7, 7)
                q4[i] = qi
                tx_m[i] = clip_i32(int(tx_m[i]) + qi * self.step_q8, -32768, 32767)

            total_bytes += pkt_bytes
            tx_pkts += 1
            if not dropped[t]:
                for i in range(n):
                    rx_m[i] = clip_i32(int(rx_m[i]) + int(q4[i]) * self.step_q8, -32768, 32767)

            for i in range(n):
                xq = c_div(int(rx_m[i]) * int(self.std_q8[i]), 256) + int(self.mean_q8[i])
                rx_out[t, i] = float(xq) / 256.0

        return rx_out, total_bytes, tx_pkts


# ---------------------------------------------------------------------------
# Baseline 5: Exact Systematic GF(2^8) Reed-Solomon Erasure Coding (n=12, k=8)
# ---------------------------------------------------------------------------

class _GF256:
    """Exact Galois Field GF(2^8) with primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11d)."""

    def __init__(self) -> None:
        self.exp = np.zeros(512, dtype=np.uint8)
        self.log = np.zeros(256, dtype=np.int32)
        x = 1
        for i in range(255):
            self.exp[i] = x
            self.log[x] = i
            x <<= 1
            if x & 0x100:
                x ^= 0x11D
        for i in range(255, 512):
            self.exp[i] = self.exp[i - 255]

    def mul(self, a: int, b: int) -> int:
        if a == 0 or b == 0:
            return 0
        return int(self.exp[self.log[a] + self.log[b]])

    def inv(self, a: int) -> int:
        if a == 0:
            raise ZeroDivisionError("GF(256) inverse of 0")
        return int(self.exp[255 - self.log[a]])

    def matmul(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """Matrix product over GF(256): (R, K) x (K, C) -> (R, C) uint8."""
        r, k = a.shape
        _, c = b.shape
        out = np.zeros((r, c), dtype=np.uint8)
        for i in range(r):
            for p in range(k):
                ap = int(a[i, p])
                if ap == 0:
                    continue
                log_ap = self.log[ap]
                nz = b[p] != 0
                if np.any(nz):
                    logs = self.log[b[p, nz]] + log_ap
                    out[i, nz] ^= self.exp[logs]
        return out

    def invert_matrix(self, m: np.ndarray) -> np.ndarray:
        k = m.shape[0]
        aug = np.hstack([m.astype(np.uint8).copy(), np.eye(k, dtype=np.uint8)])
        for col in range(k):
            piv = col
            while piv < k and aug[piv, col] == 0:
                piv += 1
            if piv == k:
                raise np.linalg.LinAlgError("Singular GF(256) submatrix")
            if piv != col:
                aug[[col, piv]] = aug[[piv, col]]
            inv_p = self.inv(int(aug[col, col]))
            for j in range(2 * k):
                aug[col, j] = self.mul(int(aug[col, j]), inv_p)
            for row in range(k):
                if row != col and aug[row, col] != 0:
                    factor = int(aug[row, col])
                    for j in range(2 * k):
                        aug[row, j] ^= self.mul(factor, int(aug[col, j]))
        return aug[:, k:]


_GF = _GF256()


class ReedSolomonGF256Codec:
    """
    Systematic Cauchy/Vandermonde RS(n=12, k=8) erasure code over standardized Int8 frames (+50% FEC).
    Recovers 100% of any <=4 dropped packets per 12-packet block; falls back to surviving
    systematic packets + ZOH when >4 packets drop in a block.
    """

    def __init__(self, n_channels: int, obs_mean: np.ndarray, obs_std: np.ndarray, k_data: int = 8, m_parity: int = 4) -> None:
        self.n_channels = n_channels
        self.mean = np.asarray(obs_mean, dtype=np.float64)
        self.std = np.maximum(np.asarray(obs_std, dtype=np.float64), 1e-6)
        self.k_data = k_data
        self.m_parity = m_parity
        self.n_total = k_data + m_parity

        # Build systematic generator G of shape (n_total, k_data) over GF(256) using Cauchy matrix
        g_sys = np.zeros((self.n_total, k_data), dtype=np.uint8)
        g_sys[:k_data, :k_data] = np.eye(k_data, dtype=np.uint8)
        for r in range(m_parity):
            for c in range(k_data):
                g_sys[k_data + r, c] = _GF.inv((r + 1) ^ (c + 16))
        self.gen = g_sys

    def run_stream(self, x_seq: np.ndarray, dropped: np.ndarray) -> Tuple[np.ndarray, int, int]:
        t_steps, n = x_seq.shape
        rx = np.zeros_like(x_seq, dtype=np.float64)
        last = self.mean.copy()
        total_bytes = 0
        tx_pkts = 0

        pkt_idx_global = 0
        n_drop_len = len(dropped)

        for start in range(0, t_steps, self.k_data):
            end = min(t_steps, start + self.k_data)
            actual_k = end - start
            blk = np.zeros((self.k_data, n), dtype=np.uint8)
            for j in range(actual_k):
                z_norm = (x_seq[start + j] - self.mean) / self.std
                q_i8 = np.clip(np.round(z_norm * (127.0 / 3.2)), -127, 127).astype(np.int8)
                blk[j] = q_i8.view(np.uint8)

            coded = _GF.matmul(self.gen, blk)  # (12, N) uint8
            arrived_rows: List[int] = []
            for p_i in range(self.n_total):
                # 3B FEC header (block_id + shard_idx) + N bytes
                total_bytes += (3 + n)
                tx_pkts += 1
                is_lost = bool(dropped[pkt_idx_global % n_drop_len])
                pkt_idx_global += 1
                if not is_lost:
                    arrived_rows.append(p_i)

            if len(arrived_rows) >= self.k_data:
                use_rows = arrived_rows[: self.k_data]
                sub_g = self.gen[use_rows]
                inv_g = _GF.invert_matrix(sub_g)
                rec_u8 = _GF.matmul(inv_g, coded[use_rows])
                for j in range(actual_k):
                    q_rec = rec_u8[j].view(np.int8).astype(np.float64)
                    val = (q_rec * (3.2 / 127.0)) * self.std + self.mean
                    rx[start + j] = val
                    last = val.copy()
            else:
                # Beyond FEC cliff (>33% loss in block): salvage surviving systematic shards, ZOH lost ones
                arrived_set = set(arrived_rows)
                for j in range(actual_k):
                    if j in arrived_set:
                        q_rec = coded[j].view(np.int8).astype(np.float64)
                        last = (q_rec * (3.2 / 127.0)) * self.std + self.mean
                    rx[start + j] = last

        return rx, total_bytes, tx_pkts


# ---------------------------------------------------------------------------
# Baseline 6: Reliable TCP / ARQ Retransmission Evaluator (Latency & Energy Cost)
# ---------------------------------------------------------------------------

class TCPARQAnalyticalEvaluator:
    """
    Simulates reliable in-order ARQ / TCP delivery over a lossy link with round-trip time `rtt_ms`.
    Reconstructs Int8 quantized values faithfully once delivered, but suffers:
      1. Retransmission + ACK wire byte inflation: bytes * (1 + ack_ratio) / (1 - p_loss_eff)
      2. Head-of-line blocking stall latency: when packet t requires k retries, all samples
         t..t+delay are stalled on the receiver (evaluated both as stall_ms_p99 and real-time ZOH RMSE!).
    """

    def __init__(self, n_channels: int, obs_mean: np.ndarray, obs_std: np.ndarray, rtt_ms: float, sample_dt_ms: float) -> None:
        self.n_channels = n_channels
        self.mean = np.asarray(obs_mean, dtype=np.float64)
        self.std = np.maximum(np.asarray(obs_std, dtype=np.float64), 1e-6)
        self.rtt_ms = rtt_ms
        self.sample_dt_ms = sample_dt_ms

    def run_stream(self, x_seq: np.ndarray, dropped: np.ndarray, seed: int = 123) -> Dict[str, object]:
        rng = np.random.default_rng(seed)
        t_steps, n = x_seq.shape
        emp_loss = min(0.88, float(np.mean(dropped)))

        # Quantize frames to Int8 + 12B compact TCP/MQTT framing overhead
        q_frames = np.clip(np.round(((x_seq - self.mean) / self.std) * (127.0 / 3.2)), -127, 127).astype(np.int8)
        dec_frames = (q_frames.astype(np.float64) * (3.2 / 127.0)) * self.std + self.mean

        total_wire_bytes = 0
        total_tx_pkts = 0
        delivery_time_ms = np.zeros(t_steps, dtype=np.float64)
        ho_line_time_ms = 0.0

        for t in range(t_steps):
            send_t_ms = t * self.sample_dt_ms
            retries = 0
            # First attempt uses `dropped[t]`; subsequent retries experience channel loss `emp_loss`
            lost = bool(dropped[t])
            while lost and retries < 8:
                retries += 1
                lost = bool(rng.random() < emp_loss)

            attempts = retries + 1
            # Forward payload (N + 12B header) * attempts + 8B ACK
            total_wire_bytes += attempts * (n + 12) + 8
            total_tx_pkts += attempts
            arr_t_ms = send_t_ms + 0.5 * self.rtt_ms + retries * self.rtt_ms
            # In-order TCP head-of-line blocking: packet t cannot surface before packet t-1!
            ho_line_time_ms = max(ho_line_time_ms, arr_t_ms)
            delivery_time_ms[t] = ho_line_time_ms

        stalls_ms = delivery_time_ms - (np.arange(t_steps) * self.sample_dt_ms + 0.5 * self.rtt_ms)
        # Real-time control/monitoring output at wall-clock time `t * sample_dt_ms + 0.5 * rtt_ms`:
        rt_rx = np.zeros_like(x_seq, dtype=np.float64)
        latest_avail = self.mean.copy()
        avail_ptr = 0
        for t in range(t_steps):
            now_ms = t * self.sample_dt_ms + 0.5 * self.rtt_ms
            while avail_ptr <= t and delivery_time_ms[avail_ptr] <= now_ms + 1e-9:
                latest_avail = dec_frames[avail_ptr]
                avail_ptr += 1
            rt_rx[t] = latest_avail

        return {
            "realtime_rx": rt_rx,
            "total_bytes": int(total_wire_bytes),
            "tx_pkts": int(total_tx_pkts),
            "mean_stall_ms": float(np.mean(stalls_ms)),
            "p99_stall_ms": float(np.percentile(stalls_ms, 99)),
        }


# ---------------------------------------------------------------------------
# Commercial Satellite / LoRaWAN / Cellular Cost & Battery Calculators
# ---------------------------------------------------------------------------

def compute_link_economics(
    total_payload_bytes: int,
    tx_packets: int,
    duration_seconds: float,
    link_type: str,
) -> Dict[str, float]:
    """
    Computes annualized dollar airtime cost and radio TX energy consumption from
    measured wire payload bytes and packet counts.
    """
    sec_per_year = 365.25 * 86400.0
    scale_to_year = sec_per_year / max(duration_seconds, 1e-6)
    bytes_per_year = total_payload_bytes * scale_to_year
    pkts_per_year = tx_packets * scale_to_year

    if link_type == "iridium_sbd_satellite":
        # Commercial Iridium SBD metered rate: $0.08 per 50-byte SBD credit (min 1 credit/msg)
        # We evaluate continuous 50-byte burst packing over 10-second buoy reporting windows
        windows_per_year = sec_per_year / 10.0
        avg_bytes_per_10s = bytes_per_year / windows_per_year
        if avg_bytes_per_10s < 1.0:
            credits_per_year = avg_bytes_per_10s * windows_per_year / 50.0
        else:
            credits_per_10s = max(1.0, np.ceil(avg_bytes_per_10s / 50.0))
            # Fraction of 10s windows that actually have >=1 transmitted packet
            active_win_frac = min(1.0, (tx_packets / max(1.0, duration_seconds / 10.0)))
            credits_per_year = windows_per_year * active_win_frac * credits_per_10s
        annual_usd = float(credits_per_year * 0.08)
        # Iridium 9603 transceiver: ~1.5W for ~0.35s per 50B burst = 0.525 J / credit
        tx_wh_per_year = float((credits_per_year * 0.525) / 3600.0)
        return {
            "annual_cost_usd_per_node": round(annual_usd, 2),
            "annual_fleet_100_nodes_usd": round(annual_usd * 100.0, 2),
            "annual_radio_tx_wh": round(tx_wh_per_year, 2),
            "bytes_per_day": round(bytes_per_year / 365.25, 1),
        }

    if link_type == "lorawan_sf9":
        # Semtech SX1276 SF9/125kHz: ~4.1 ms per byte + 13B LoRa MAC header when a packet is sent
        # Sub-packets within a 2s window coalesce into 1 LoRaWAN frame up to 120B
        windows_2s = max(1.0, duration_seconds / 2.0)
        active_frames = min(float(tx_packets), windows_2s)
        total_air_bytes = total_payload_bytes + 13.0 * active_frames
        airtime_s = (total_air_bytes * 0.0041)
        duty_cycle_pct = 100.0 * (airtime_s / max(duration_seconds, 1e-6))
        # Max nodes supported on a single 1%-duty-cycle LoRaWAN channel
        max_nodes_1pct_channel = float(1.0 / max(duty_cycle_pct, 1e-6))
        # SX1276 @ +14dBm TX: 44mA * 3.3V = 0.1452 W -> Wh/year
        tx_wh_per_year = float((airtime_s * scale_to_year * 0.1452) / 3600.0)
        # AA Li-SOCl2 cell capacity = 8.64 Wh (allocate 6.0 Wh to radio TX)
        battery_years = min(20.0, 6.0 / max(tx_wh_per_year, 1e-4))
        # Commercial LoRaWAN + Cloud IoT Core metered cost ($1.25 / MB + $1.00 / million msgs) across 1,000-node city AQMS
        mb_per_year = bytes_per_year / 1e6
        annual_usd_node = float(mb_per_year * 1.25 + (pkts_per_year / 1e6) * 1.00)
        return {
            "duty_cycle_pct": round(duty_cycle_pct, 5),
            "max_nodes_per_1pct_channel": round(max_nodes_1pct_channel, 1),
            "aa_battery_life_years": round(battery_years, 2),
            "annual_fleet_1000_nodes_usd": round(annual_usd_node * 1000.0, 2),
            "bytes_per_day": round(bytes_per_year / 365.25, 1),
        }

    # Cellular / Edge Wi-Fi / 5G metered M2M ($4.00 / GB rural 2G/M2M + radio TX 0.8W @ 250 kbps)
    gb_per_year = bytes_per_year / 1e9
    annual_usd_node = float(gb_per_year * 4.00)
    airtime_s_yr = (bytes_per_year * 8.0) / 250_000.0
    tx_wh_per_year = float((airtime_s_yr * 0.80) / 3600.0)
    return {
        "annual_cost_usd_per_node": round(annual_usd_node, 2),
        "annual_fleet_1000_nodes_usd": round(annual_usd_node * 1000.0, 2),
        "annual_radio_tx_wh": round(tx_wh_per_year, 2),
        "bytes_per_day": round(bytes_per_year / 365.25, 1),
    }
