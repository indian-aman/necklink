"""
NeckLink Reference Encoder, Decoder, and PyTorch Split-AI Bottleneck Layer.

Implements the Compact Dual-Range Place-Rate + Inter-Spike-Interval (ISI) Tonic-Phasic
Neck-Connective Wire Protocol in bit-exact signed integer arithmetic (int8/int16/int32
with C99 truncation-toward-zero `c_div`), guaranteeing 0-LSB divergence between Python
cloud endpoints and bare-metal C99 microcontrollers:
  1. ISI-Scaled Tonic-Phasic Efference Mirror (Paper 2):
     Extrapolates active oscillatory swells using inter-spike-interval velocity
     (`dv = (vel_gain * u_innov) / (256 * age)`), holds calm baselines at 0 bytes/tick,
     and locks absolute dual-range quantized levels via bit-7 companding for zero drift.
  2. Salience-Gated 2-Packet Epistatic Cryptic-Backup Echo + Hadamard Sketch (Paper 3):
     Piggybacks compact cryptic backups of salient transients (`|ri| >= echo_salience_q8`),
     healing 1- and 2-packet drops in 0 ms RTT with 0 ACKs or retransmissions.
  3. Disinhibitory Chloride Shunting Gate (Paper 1 / FlyNeck):
     Modulates velocity gain and relaxes frozen high-excursion spikes toward the slow
     tonic posture `b` during multi-packet loss blackouts without disturbing calm silence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

try:
    import torch
    import torch.nn as nn

    _NN_MODULE_BASE = nn.Module
except ImportError:  # Optional dependency (`pip install "pynltp[torch]"`)
    torch = None  # type: ignore[assignment]
    nn = None  # type: ignore[assignment]
    _NN_MODULE_BASE = object

from .connectome_priors import DROSOPHILA_NECK_PRIORS, build_epistatic_cryptic_matrix_q8


def c_div(num: int, den: int) -> int:
    """Exact ANSI C99 integer division (truncation toward zero)."""
    if den == 0:
        raise ZeroDivisionError("Integer division by zero")
    sign = -1 if ((num < 0) ^ (den < 0)) else 1
    return sign * (abs(int(num)) // abs(int(den)))


def clip_i32(val: int, lo: int, hi: int) -> int:
    if val < lo:
        return lo
    if val > hi:
        return hi
    return int(val)


@dataclass
class NeckLinkConfig:
    n_channels: int
    deadband_q8: int = 32           # 0.125 std event trigger threshold in Q8
    quant_step_q8: int = 8          # Fine step = 8 Q8 (+/- 3.97 std); Coarse (bit 7) = 32 Q8 (+/- 15.87 std)
    echo_salience_q8: int = 64      # Salience threshold in Q8 (0.25 std) for piggybacking cryptic backup echo
    max_events_per_pkt: int = 16    # Cap sparse events per packet
    sketch_dim: int = 4             # 4-byte Epistatic Hadamard residual sketch on >=5-ch bursts
    vel_gain_q8: int = 176          # 176/256 = 0.6875 ISI-scaled velocity coupling
    beta_q8: int = 168              # 168/256 = 0.6563 phasic velocity step
    gamma_q8: int = 128             # 128/256 = 0.5000 contractive velocity damping
    heartbeat_ticks: int = 64       # Max silent ticks before keepalive packet
    enable_mirror: bool = True      # Paper 2: ISI Tonic-Phasic Efference Mirror
    enable_echo: bool = True        # Paper 3: Salience-Gated 2-Packet Epistatic Cryptic Echo
    enable_brake_gate: bool = True  # Paper 1/FlyNeck: Disinhibitory Chloride Shunting Gate
    seed: int = 20261103

    def __post_init__(self) -> None:
        if not (1 <= self.n_channels <= 127):
            raise ValueError("n_channels must be in [1, 127] for 7-bit channel indexing + 1-bit range flag")
        if not (1 <= self.max_events_per_pkt <= 31):
            raise ValueError("max_events_per_pkt must be in [1, 31]")


def quant_dual_range(zq: int, qs_fine: int) -> Tuple[int, int, int]:
    max_fine = 127 * qs_fine
    if abs(int(zq)) <= max_fine:
        step = qs_fine
        is_coarse = 0
    else:
        step = qs_fine * 4
        is_coarse = 1
    half = step // 2
    if zq >= 0:
        q = (int(zq) + half) // step
    else:
        q = -(((-int(zq)) + half) // step)
    q = clip_i32(q, -127, 127)
    return is_coarse, q, q * step


def dequant_dual_range(is_coarse: int, q: int, qs_fine: int) -> int:
    step = (qs_fine * 4) if is_coarse else qs_fine
    return int(q) * step


def _step_tonic_phasic_inplace(
    m: np.ndarray,
    b: np.ndarray,
    v: np.ndarray,
    age: np.ndarray,
    beta_q8: int,
    gamma_q8: int,
    enable_mirror: bool,
) -> None:
    for i in range(len(m)):
        age[i] = min(32, int(age[i]) + 1)
        mi = int(m[i])
        bi = int(b[i])
        b[i] = clip_i32(bi + c_div(mi - bi, 64), -32768, 32767)
        if enable_mirror:
            vi = int(v[i])
            if vi != 0:
                m[i] = clip_i32(mi + c_div(beta_q8 * vi, 256), -32768, 32767)
                v[i] = clip_i32(c_div(gamma_q8 * vi, 256), -32768, 32767)
        else:
            v[i] = 0


class NeckLinkEncoder:
    """Bit-exact integer sender ('Brain -> Neck' Descending Encoder)."""

    def __init__(
        self,
        cfg: NeckLinkConfig,
        obs_mean: Optional[np.ndarray] = None,
        obs_std: Optional[np.ndarray] = None,
    ) -> None:
        self.cfg = cfg
        n = cfg.n_channels
        if obs_mean is None:
            self.mean_q8 = np.zeros(n, dtype=np.int32)
        else:
            self.mean_q8 = np.round(np.asarray(obs_mean, dtype=np.float64) * 256.0).astype(np.int32)

        if obs_std is None:
            self.inv_std_q8 = np.full(n, 256, dtype=np.int32)
            self.std_q8 = np.full(n, 256, dtype=np.int32)
        else:
            std_safe = np.maximum(np.asarray(obs_std, dtype=np.float64), 1e-6)
            self.inv_std_q8 = np.clip(np.round(256.0 / std_safe), 1, 32767).astype(np.int32)
            self.std_q8 = np.clip(np.round(std_safe * 256.0), 1, 32767).astype(np.int32)

        self.phi_q7 = build_epistatic_cryptic_matrix_q8(n, cfg.sketch_dim, seed=cfg.seed)
        self.reset()

    def reset(self) -> None:
        n = self.cfg.n_channels
        self.m = np.zeros(n, dtype=np.int32)
        self.b = np.zeros(n, dtype=np.int32)
        self.v = np.zeros(n, dtype=np.int32)
        self.age = np.full(n, 32, dtype=np.int32)
        self.seq: int = 0
        self.ticks_since_tx: int = 0

        self.h1_valid: bool = False
        self.h1_coarse: int = 0
        self.h1_idx: int = 0
        self.h1_q: int = 0
        self.h1_dt_prev: int = 1
        self.h1_has_sketch: bool = False
        self.h1_sketch: np.ndarray = np.zeros(self.cfg.sketch_dim, dtype=np.int8)

        self.h2_valid: bool = False
        self.h2_coarse: int = 0
        self.h2_idx: int = 0
        self.h2_q: int = 0

    def standardize_float_to_q8(self, x_float: np.ndarray) -> np.ndarray:
        x_raw_q8 = np.clip(
            np.round(np.asarray(x_float, dtype=np.float64) * 256.0),
            -2147483648,
            2147483647,
        ).astype(np.int64)
        z = np.empty(self.cfg.n_channels, dtype=np.int32)
        for i in range(self.cfg.n_channels):
            diff = int(x_raw_q8[i]) - int(self.mean_q8[i])
            zi = c_div(diff * int(self.inv_std_q8[i]), 256)
            z[i] = clip_i32(zi, -16384, 16383)
        return z

    def encode_q8(self, z_q8: np.ndarray) -> Optional[bytes]:
        cfg = self.cfg
        n = cfg.n_channels

        _step_tonic_phasic_inplace(
            self.m, self.b, self.v, self.age, cfg.beta_q8, cfg.gamma_q8, cfg.enable_mirror
        )
        self.ticks_since_tx += 1

        cand: List[Tuple[int, int, int]] = []
        for i in range(n):
            zq = int(z_q8[i])
            ri = zq - int(self.m[i])
            ari = abs(ri)
            if ari >= cfg.deadband_q8:
                cand.append((ari, i, zq))

        if not cand and self.ticks_since_tx < cfg.heartbeat_ticks:
            return None

        cand.sort(key=lambda t: (-t[0], t[1]))
        chosen = cand[: cfg.max_events_per_pkt]

        ev_list: List[Tuple[int, int, int, int, int]] = []
        for ari, idx, zq in chosen:
            is_c, q, z_quant = quant_dual_range(zq, cfg.quant_step_q8)
            if z_quant != int(self.m[idx]):
                ev_list.append((is_c, idx, q, z_quant, ari))

        if not ev_list and self.ticks_since_tx < cfg.heartbeat_ticks:
            return None

        u_innov_vec = np.zeros(n, dtype=np.int32)
        for is_c, idx, q, z_quant, _ari in ev_list:
            u_innov = z_quant - int(self.m[idx])
            u_innov_vec[idx] = u_innov
            self.m[idx] = clip_i32(z_quant, -32768, 32767)
            if cfg.enable_mirror:
                isi = max(1, int(self.age[idx]))
                dv = c_div(cfg.vel_gain_q8 * u_innov, 256 * isi)
                self.v[idx] = clip_i32(int(self.v[idx]) + dv, -32768, 32767)
            self.age[idx] = 0

        self.seq = (self.seq + 1) & 0xFF
        dt_curr = min(31, self.ticks_since_tx)
        include_echo = bool(cfg.enable_echo and self.h1_valid and self.ticks_since_tx <= 31)
        inc_h2 = bool(include_echo and self.h2_valid and self.h1_dt_prev <= 15)

        n_ev = len(ev_list)
        pkt = bytearray()
        pkt.append(self.seq & 0xFF)
        pkt.append((0x80 if include_echo else 0x00) | (n_ev & 0x3F))

        for is_c, idx, q, _zq, _ari in ev_list:
            pkt.append(((0x80 if is_c else 0x00) | (idx & 0x7F)) & 0xFF)
            pkt.append(q & 0xFF)

        if include_echo:
            eh1 = (
                (0x80 if self.h1_has_sketch else 0x00)
                | (0x40 if inc_h2 else 0x00)
                | (dt_curr & 0x1F)
            )
            pkt.append(eh1)
            pkt.append(((0x80 if self.h1_coarse else 0x00) | (self.h1_idx & 0x7F)) & 0xFF)
            pkt.append(self.h1_q & 0xFF)
            if self.h1_has_sketch:
                for s_i in range(cfg.sketch_dim):
                    pkt.append(int(self.h1_sketch[s_i]) & 0xFF)
            if inc_h2:
                pkt.append(((0x80 if self.h2_coarse else 0x00) | (self.h2_idx & 0x7F)) & 0xFF)
                pkt.append(self.h2_q & 0xFF)

        salient = [e for e in ev_list if e[4] >= cfg.echo_salience_q8]
        if salient:
            if self.h1_valid:
                self.h2_valid = True
                self.h2_coarse = self.h1_coarse
                self.h2_idx = self.h1_idx
                self.h2_q = self.h1_q
            else:
                self.h2_valid = False

            s0 = salient[0]
            self.h1_valid = True
            self.h1_coarse = s0[0]
            self.h1_idx = s0[1]
            self.h1_q = s0[2]
            self.h1_dt_prev = dt_curr

            if len(salient) >= 5:
                self.h1_has_sketch = True
                res_vec = u_innov_vec.copy()
                res_vec[self.h1_idx] = 0
                sk = np.zeros(cfg.sketch_dim, dtype=np.int8)
                for r_idx in range(cfg.sketch_dim):
                    acc = 0
                    for c_idx in range(n):
                        acc += int(self.phi_q7[r_idx, c_idx]) * int(res_vec[c_idx])
                    sk_val = c_div(acc, 128 * cfg.quant_step_q8)
                    sk[r_idx] = clip_i32(sk_val, -127, 127)
                self.h1_sketch = sk
            else:
                self.h1_has_sketch = False
        else:
            self.h1_valid = False
            self.h2_valid = False

        self.ticks_since_tx = 0
        return bytes(pkt)

    def encode_float(self, x_float: np.ndarray) -> Optional[bytes]:
        return self.encode_q8(self.standardize_float_to_q8(x_float))


class NeckLinkDecoder:
    """Bit-exact integer receiver ('Neck -> VNC' Efference & Disinhibitory Decoder)."""

    def __init__(
        self,
        cfg: NeckLinkConfig,
        obs_mean: Optional[np.ndarray] = None,
        obs_std: Optional[np.ndarray] = None,
    ) -> None:
        self.cfg = cfg
        n = cfg.n_channels
        if obs_mean is None:
            self.mean_q8 = np.zeros(n, dtype=np.int32)
        else:
            self.mean_q8 = np.round(np.asarray(obs_mean, dtype=np.float64) * 256.0).astype(np.int32)

        if obs_std is None:
            self.std_q8 = np.full(n, 256, dtype=np.int32)
        else:
            std_safe = np.maximum(np.asarray(obs_std, dtype=np.float64), 1e-6)
            self.std_q8 = np.clip(np.round(std_safe * 256.0), 1, 32767).astype(np.int32)

        self.phi_q7 = build_epistatic_cryptic_matrix_q8(n, cfg.sketch_dim, seed=cfg.seed)
        self.col_norm_sq = max(1, int(np.sum(self.phi_q7[:, 0].astype(np.int32) ** 2)))
        self.reset()

    def reset(self) -> None:
        n = self.cfg.n_channels
        self.m = np.zeros(n, dtype=np.int32)
        self.b = np.zeros(n, dtype=np.int32)
        self.v = np.zeros(n, dtype=np.int32)
        self.age = np.full(n, 32, dtype=np.int32)
        self.last_seq: int = 0
        self.has_synced: bool = False
        self.ticks_since_pkt: int = 0
        self.arrive_ema_q8: int = 256
        self.brake_gate_q8: int = 256
        self.recovered_echo_packets: int = 0
        self.detected_lost_packets: int = 0

    def destandardize_q8_to_float(self, z_q8: np.ndarray) -> np.ndarray:
        out = np.empty(self.cfg.n_channels, dtype=np.float64)
        for i in range(self.cfg.n_channels):
            x_q8 = c_div(int(z_q8[i]) * int(self.std_q8[i]), 256) + int(self.mean_q8[i])
            out[i] = float(x_q8) / 256.0
        return out

    def decode_q8(self, pkt: Optional[bytes]) -> np.ndarray:
        cfg = self.cfg
        n = cfg.n_channels

        _step_tonic_phasic_inplace(
            self.m, self.b, self.v, self.age, cfg.beta_q8, cfg.gamma_q8, cfg.enable_mirror
        )
        self.ticks_since_pkt = min(1000, self.ticks_since_pkt + 1)

        if pkt is None or len(pkt) < 2:
            if cfg.enable_brake_gate and self.arrive_ema_q8 < 216 and self.ticks_since_pkt >= 2:
                shunt = 256 - self.arrive_ema_q8
                for i in range(n):
                    dev = int(self.m[i]) - int(self.b[i])
                    if abs(dev) > 224:
                        self.m[i] = clip_i32(int(self.m[i]) - c_div(dev * shunt, 512), -32768, 32767)
            return self.m.copy()

        self.ticks_since_pkt = 0
        seq = int(pkt[0]) & 0xFF
        flags_count = int(pkt[1])
        has_echo = bool(flags_count & 0x80)
        n_ev = flags_count & 0x3F

        pos = 2
        ev_curr: List[Tuple[int, int]] = []
        active_curr = np.zeros(n, dtype=bool)
        for _ in range(n_ev):
            if pos + 2 > len(pkt):
                break
            b0 = int(pkt[pos])
            qi_raw = int(pkt[pos + 1])
            pos += 2
            is_c = 1 if (b0 & 0x80) else 0
            idx = b0 & 0x7F
            qi = qi_raw - 256 if qi_raw >= 128 else qi_raw
            if idx < n:
                zq = dequant_dual_range(is_c, qi, cfg.quant_step_q8)
                ev_curr.append((idx, zq))
                active_curr[idx] = True

        if not self.has_synced:
            gap = seq if seq >= 1 else 1
            self.has_synced = True
        else:
            gap = (seq - self.last_seq) & 0xFF
            if gap == 0:
                gap = 1
        self.last_seq = seq

        if gap > 1:
            self.detected_lost_packets += (gap - 1)

        inst_arrive_q8 = c_div(256, min(gap, 8))
        if inst_arrive_q8 < self.arrive_ema_q8:
            self.arrive_ema_q8 = c_div(96 * self.arrive_ema_q8 + 160 * inst_arrive_q8, 256)
        else:
            self.arrive_ema_q8 = c_div(216 * self.arrive_ema_q8 + 40 * inst_arrive_q8, 256)

        if cfg.enable_brake_gate:
            self.brake_gate_q8 = clip_i32(
                self.arrive_ema_q8,
                DROSOPHILA_NECK_PRIORS["tonic_chloride_shunt_floor_q8"],
                256,
            )
        else:
            self.brake_gate_q8 = 256

        eff_vg = c_div(cfg.vel_gain_q8 * self.brake_gate_q8, 256)

        if has_echo and pos < len(pkt):
            eh1 = int(pkt[pos])
            pos += 1
            h1_has_sketch = bool(eh1 & 0x80)
            inc_h2 = bool(eh1 & 0x40)
            dt1 = eh1 & 0x1F

            h1_valid_wire = False
            h1_idx = 0
            h1_zq = 0
            if pos + 2 <= len(pkt):
                eb0 = int(pkt[pos])
                eq_raw = int(pkt[pos + 1])
                pos += 2
                h1_c = 1 if (eb0 & 0x80) else 0
                h1_idx = eb0 & 0x7F
                h1_q = eq_raw - 256 if eq_raw >= 128 else eq_raw
                if h1_idx < n:
                    h1_zq = dequant_dual_range(h1_c, h1_q, cfg.quant_step_q8)
                    h1_valid_wire = True

            sk_vals = np.zeros(cfg.sketch_dim, dtype=np.int32)
            if h1_has_sketch and pos + cfg.sketch_dim <= len(pkt):
                for s_i in range(cfg.sketch_dim):
                    sv_raw = int(pkt[pos + s_i])
                    sk_vals[s_i] = sv_raw - 256 if sv_raw >= 128 else sv_raw
                pos += cfg.sketch_dim

            h2_valid_wire = False
            h2_idx = 0
            h2_zq = 0
            if inc_h2 and pos + 2 <= len(pkt):
                eb2 = int(pkt[pos])
                eq2_raw = int(pkt[pos + 1])
                pos += 2
                h2_c = 1 if (eb2 & 0x80) else 0
                h2_idx = eb2 & 0x7F
                h2_q = eq2_raw - 256 if eq2_raw >= 128 else eq2_raw
                if h2_idx < n:
                    h2_zq = dequant_dual_range(h2_c, h2_q, cfg.quant_step_q8)
                    h2_valid_wire = True

            if cfg.enable_echo and gap >= 2:
                if gap >= 3 and h2_valid_wire and not active_curr[h2_idx]:
                    self.m[h2_idx] = clip_i32(h2_zq, -32768, 32767)
                    self.recovered_echo_packets += 1

                if h1_valid_wire and not active_curr[h1_idx]:
                    u1 = h1_zq - int(self.m[h1_idx])
                    self.m[h1_idx] = clip_i32(h1_zq, -32768, 32767)
                    if cfg.enable_mirror:
                        isi1 = max(1, int(self.age[h1_idx]))
                        dv1 = c_div(eff_vg * u1, 256 * isi1)
                        v1 = clip_i32(int(self.v[h1_idx]) + dv1, -32768, 32767)
                        m_now = int(self.m[h1_idx])
                        for _ in range(dt1):
                            if v1 != 0:
                                m_now = clip_i32(m_now + c_div(cfg.beta_q8 * v1, 256), -32768, 32767)
                                v1 = clip_i32(c_div(cfg.gamma_q8 * v1, 256), -32768, 32767)
                        self.m[h1_idx] = m_now
                        self.v[h1_idx] = v1
                    self.age[h1_idx] = min(32, max(1, dt1))

                if h1_has_sketch:
                    for c_idx in range(n):
                        if active_curr[c_idx] or (h1_valid_wire and c_idx == h1_idx):
                            continue
                        acc = 0
                        for r_idx in range(cfg.sketch_dim):
                            acc += int(self.phi_q7[r_idx, c_idx]) * int(sk_vals[r_idx])
                        rec_val = c_div(acc * 128 * cfg.quant_step_q8, self.col_norm_sq)
                        if abs(rec_val) > cfg.deadband_q8:
                            self.m[c_idx] = clip_i32(
                                int(self.m[c_idx]) + c_div(rec_val * 144, 256), -32768, 32767
                            )

                self.recovered_echo_packets += 1

        for idx, zq in ev_curr:
            u_innov = zq - int(self.m[idx])
            self.m[idx] = clip_i32(zq, -32768, 32767)
            if cfg.enable_mirror:
                isi = max(1, int(self.age[idx]))
                dv = c_div(eff_vg * u_innov, 256 * isi)
                self.v[idx] = clip_i32(int(self.v[idx]) + dv, -32768, 32767)
            self.age[idx] = 0

        return self.m.copy()

    def decode_float(self, pkt: Optional[bytes]) -> np.ndarray:
        return self.destandardize_q8_to_float(self.decode_q8(pkt))


class NeckLinkSplitBottleneck(_NN_MODULE_BASE):  # type: ignore[misc,valid-type]
    """PyTorch Edge-to-Cloud Split-AI Bottleneck Layer."""

    def __init__(self, in_dim: int, bottleneck_dim: int = 32, num_classes: int = 8) -> None:
        if nn is None:
            raise ImportError("NeckLinkSplitBottleneck requires PyTorch (`pip install 'pynltp[torch]'`).")
        super().__init__()
        self.bottleneck_dim = bottleneck_dim
        self.edge_encoder = nn.Sequential(
            nn.Linear(in_dim, 96),
            nn.GELU(),
            nn.Linear(96, bottleneck_dim),
            nn.LayerNorm(bottleneck_dim),
        )
        self.cloud_head = nn.Sequential(
            nn.Linear(bottleneck_dim * 2, 96),
            nn.GELU(),
            nn.Linear(96, num_classes),
        )

    def encode_edge(self, x_seq: torch.Tensor) -> torch.Tensor:
        return self.edge_encoder(x_seq)

    def classify_cloud(self, z_rx_seq: torch.Tensor) -> torch.Tensor:
        bsz, t_steps, d = z_rx_seq.shape
        ema = torch.zeros(bsz, d, device=z_rx_seq.device, dtype=z_rx_seq.dtype)
        outs = []
        for t in range(t_steps):
            zt = z_rx_seq[:, t, :]
            ema = 0.85 * ema + 0.15 * zt
            outs.append(self.cloud_head(torch.cat([zt, ema], dim=-1)))
        return torch.stack(outs, dim=1)

    def forward(self, x_seq: torch.Tensor) -> torch.Tensor:
        z = self.encode_edge(x_seq)
        return self.classify_cloud(z)
