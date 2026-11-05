"""
NLTP v1 — NeckLink Transport Protocol (Internet-Ready Standalone Client/Server Protocol).

A general-purpose userspace transport protocol running over unprivileged UDP datagrams (IPv4/IPv6,
DNS, NAT hole-punching, or zero-knowledge CGNAT cloud relay) inspired by three adult Drosophila
neck-connective principles:
  1. Adaptive Epistatic Cryptic Coding (GF(2^8) Systematic + Cauchy Repair Shards):
     Sends raw systematic data shards immediately in 0-RTT (0% parity overhead on clean links),
     and dynamically injects algebraic repair shards proportional to measured packet loss so
     dropped file/message chunks self-heal without TCP head-of-line stalls.
  2. Ascending Proprioceptive Feedback (`PT_PROPRIOCEPTIVE_ACK`):
     Ultra-compact selective degree-of-freedom (`needed_dof`) feedback instead of per-packet
     ACK storms; every retransmitted repair shard is a fresh linearly independent Cauchy row
     guaranteed to heal ANY missing packet in its generation.
  3. Disinhibitory Congestion & Loss Separation + 3-Lane Multiplexing:
     Separates wireless packet drops from RTT queue inflation, paces bulk bursts via a router-safe
     token bucket, authenticates/encrypts frames end-to-end with `ChaCha20-Poly1305` + `RFC 6479`
     replay protection, and supports seamless Wi-Fi-to-4G IP roaming (`conn_id` migration) across:
       - `STREAM_RELIABLE` (`send_bytes`, `send_json`, `send_file` — 100% SHA-256 bit-exact!)
       - `MSG_REFLEX` (`send_reflex` — Giant-Fiber priority instant command, 0 HoL stall)
       - `STREAM_NEURAL` (`send_telemetry` — 10x–35x compressed Tonic-Phasic state stream)
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import select
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from .baselines import _GF
from .internet import (
    DEFAULT_PUBLIC_STUN_SERVERS,
    RELAY_VER_DATA,
    RELAY_VER_REG_ACK,
    RELAY_VER_REGISTER,
    AEADCipherContext,
    STUNDiscoveryResult,
    TokenBucketPacer,
    build_stun_binding_request,
    is_stun_packet,
    make_room_tag,
    open_internet_udp_socket,
    parse_stun_binding_response,
    resolve_udp_address,
    unwrap_relay_packet,
    wrap_relay_packet,
)
from .session import HANDLED, SessionTable
from .socket_api import NeckSocket

NLTP_MAGIC = b"NL"
NLTP_VERSION = 1

# Packet types (4-bit nibble in version_type byte)
PT_DATA_SHARD = 0x01
PT_CRYPTIC_PARITY = 0x02
PT_PROPRIOCEPTIVE_ACK = 0x03
PT_STREAM_META = 0x04
PT_STREAM_FIN_ACK = 0x05
PT_REFLEX_MSG = 0x06
PT_NEURAL_STATE = 0x07
PT_PING = 0x08
PT_PONG = 0x09
PT_REFLEX_ACK = 0x0A  # receiver -> sender: u32 reflex seq (reliable reflex lane, v1.3.2)

REFLEX_ACK_TIMEOUT_S = 5.0      # keep retransmitting an unacknowledged reflex for this long (incl. 1.4 handshake)
REFLEX_RETX_MIN_S = 0.015
REFLEX_RETX_FIRST_MAX_S = 0.06  # first retransmit within 60 ms even before an RTT sample exists
REFLEX_RETX_MAX_S = 0.25
REFLEX_SEEN_MAX = 8192          # remembered (conn_id, seq) pairs for duplicate suppression

FLAG_ENCRYPTED = 0x80

HEADER_FMT = "<2sBBIII"      # magic(2), ver_type(1), flags(1), conn_id(4), crc32(4), ts_ms(4) = 16 B
HEADER_SIZE = 16

ENC_HEADER_FMT = "<2sBBIQI"  # magic(2), ver_type(1), flags(1), conn_id(4), seq64(8), ts_ms(4) = 20 B (+16 B Poly1305 tag)
ENC_HEADER_SIZE = 20
AEAD_TAG_SIZE = 16

SHARD_HDR_FMT = "<IIBBH"     # stream_id(4), gen_id(4), k_shards(1), shard_idx(1), shard_payload_len(2) = 12 B
SHARD_HDR_SIZE = 12

# 960 B data -> <= 1,029 B physical UDP packet even with ChaCha20-Poly1305 + CGNAT Relay framing!
# Guaranteed zero IP fragmentation across IPv6 (1280 B), 4G/LTE GTP, PPPoE, and WireGuard tunnels.
DEFAULT_SHARD_BYTES = 960
DEFAULT_GEN_K = 8            # 8 shards/generation -> microsecond GF(256) solve & fine granularity
MAX_PARITY_IDX = 220         # Up to 192 distinct Cauchy repair rows per generation!

Address = Tuple[Any, ...]


def _now_ms() -> int:
    return int(time.perf_counter() * 1000.0) & 0xFFFFFFFF


def _cauchy_row(parity_offset: int, k_shards: int) -> np.ndarray:
    """
    Deterministic GF(2^8) Cauchy row for `parity_offset >= 0` (`len = k_shards <= 64`).
    Disjoint sets: `x_r in [64..255]`, `y_c in [0..63]` -> `x_r ^ y_c >= 64 > 0` always!
    """
    x_r = 64 + (parity_offset % 192)
    row = np.empty(k_shards, dtype=np.uint8)
    for c in range(k_shards):
        row[c] = _GF.inv(x_r ^ (c & 0x3F))
    return row


def pack_nltp_frame(
    pkt_type: int,
    conn_id: int,
    payload: bytes,
    flags: int = 0,
    ts_ms: Optional[int] = None,
    aead: Optional[AEADCipherContext] = None,
) -> bytes:
    ver_type = ((NLTP_VERSION & 0x0F) << 4) | (pkt_type & 0x0F)
    t_val = _now_ms() if ts_ms is None else (int(ts_ms) & 0xFFFFFFFF)
    cid32 = conn_id & 0xFFFFFFFF

    if aead is not None:
        enc_flags = (flags & 0x7F) | FLAG_ENCRYPTED
        seq64 = aead.next_seq()
        aad_hdr = struct.pack(ENC_HEADER_FMT, NLTP_MAGIC, ver_type, enc_flags, cid32, seq64, t_val)
        ct_and_tag = aead.seal(cid32, seq64, aad_hdr, payload)
        return aad_hdr + ct_and_tag

    clean_flags = flags & 0x7F
    hdr_zero = struct.pack(HEADER_FMT, NLTP_MAGIC, ver_type, clean_flags, cid32, 0, t_val)
    crc = zlib.crc32(payload, zlib.crc32(hdr_zero)) & 0xFFFFFFFF
    hdr = struct.pack(HEADER_FMT, NLTP_MAGIC, ver_type, clean_flags, cid32, crc, t_val)
    return hdr + payload


def unpack_nltp_frame(
    raw: bytes,
    aead: Optional[AEADCipherContext] = None,
) -> Optional[Tuple[int, int, int, int, bytes]]:
    """
    Validates magic, version, and either ChaCha20-Poly1305 AEAD + RFC 6479 anti-replay
    (when `aead` is configured — strictly rejecting unencrypted packets!) or CRC32.
    Returns `(pkt_type, flags, conn_id, ts_ms, payload)` or `None` if invalid/tampered/replayed.
    """
    if len(raw) < HEADER_SIZE or raw[:2] != NLTP_MAGIC:
        return None
    ver_type = raw[2]
    flags = raw[3]
    if (ver_type >> 4) != NLTP_VERSION:
        return None

    is_enc = bool(flags & FLAG_ENCRYPTED)
    if aead is not None:
        if not is_enc or len(raw) < ENC_HEADER_SIZE + AEAD_TAG_SIZE:
            return None
        _magic, _vt, _fl, conn_id, seq64, ts_ms = struct.unpack(ENC_HEADER_FMT, raw[:ENC_HEADER_SIZE])
        pt = aead.open_and_check_replay(conn_id, seq64, raw[:ENC_HEADER_SIZE], raw[ENC_HEADER_SIZE:])
        if pt is None:
            return None
        return (ver_type & 0x0F), (flags & 0x7F), conn_id, ts_ms, pt

    # Unencrypted mode cannot parse encrypted frames
    if is_enc:
        return None
    magic, ver_type, flags, conn_id, rx_crc, ts_ms = struct.unpack(HEADER_FMT, raw[:HEADER_SIZE])
    payload = raw[HEADER_SIZE:]
    hdr_zero = struct.pack(HEADER_FMT, magic, ver_type, flags, conn_id, 0, ts_ms)
    calc_crc = zlib.crc32(payload, zlib.crc32(hdr_zero)) & 0xFFFFFFFF
    if calc_crc != rx_crc:
        return None
    return (ver_type & 0x0F), flags, conn_id, ts_ms, payload


# ---------------------------------------------------------------------------
# Sender & Receiver Generation Algebraic State Machines (GF(2^8))
# ---------------------------------------------------------------------------

class _TxGeneration:
    """Holds one K-shard generation of a reliable stream/file and produces Cauchy repair shards on demand."""

    def __init__(self, stream_id: int, gen_id: int, raw_slice: bytes, shard_bytes: int = DEFAULT_SHARD_BYTES, k_max: int = DEFAULT_GEN_K) -> None:
        self.stream_id = stream_id
        self.gen_id = gen_id
        chunks: List[bytes] = [
            raw_slice[i : i + shard_bytes] for i in range(0, len(raw_slice), shard_bytes)
        ]
        if not chunks:
            chunks = [b""]
        self.k_shards = len(chunks)
        self.max_chunk_len = max(len(c) for c in chunks)
        self.row_width = 2 + self.max_chunk_len
        self.data_mat = np.zeros((self.k_shards, self.row_width), dtype=np.uint8)
        for idx, ch in enumerate(chunks):
            struct.pack_into("<H", self.data_mat[idx], 0, len(ch))
            if ch:
                self.data_mat[idx, 2 : 2 + len(ch)] = np.frombuffer(ch, dtype=np.uint8)

        self.next_sys_idx: int = 0
        self.proactive_done: bool = False
        # Proactive parity is decided once per generation and resumed (not restarted) when the pacer runs dry
        self.proactive_target: int = -1
        self.proactive_sent: int = 0
        self.next_parity_offset: int = 0
        self.acked_complete: bool = False
        self.last_tx_time: float = 0.0
        self.repair_in_flight: int = 0
        self.last_repair_time: float = 0.0

    def make_systematic_payload(self, sys_idx: int) -> bytes:
        row_bytes = self.data_mat[sys_idx].tobytes()
        shdr = struct.pack(SHARD_HDR_FMT, self.stream_id, self.gen_id, self.k_shards, sys_idx, self.row_width)
        return shdr + row_bytes

    def make_cryptic_parity_payload(self) -> bytes:
        p_off = self.next_parity_offset
        self.next_parity_offset = (self.next_parity_offset + 1) % 192
        shard_idx = self.k_shards + p_off
        if self.k_shards == 1:
            coded_bytes = self.data_mat[0].tobytes()
        else:
            coeff = _cauchy_row(p_off, self.k_shards).reshape(1, self.k_shards)
            coded_bytes = _GF.matmul(coeff, self.data_mat)[0].tobytes()
        shdr = struct.pack(SHARD_HDR_FMT, self.stream_id, self.gen_id, self.k_shards, shard_idx, self.row_width)
        return shdr + coded_bytes


class _RxGeneration:
    """Collects systematic + cryptic Cauchy rows and solves the generation as soon as `K` rows arrive."""

    def __init__(self, k_shards: int, row_width: int) -> None:
        self.k_shards = k_shards
        self.row_width = row_width
        self.seen_indices: set[int] = set()
        self.rows_coeff: List[np.ndarray] = []
        self.rows_data: List[np.ndarray] = []
        self.solved_bytes: Optional[bytes] = None

    @property
    def needed_dof(self) -> int:
        if self.solved_bytes is not None:
            return 0
        return max(0, self.k_shards - len(self.rows_coeff))

    def add_shard(self, shard_idx: int, shard_buf: bytes) -> bool:
        """Returns True when this shard newly completes & solves the generation."""
        if self.solved_bytes is not None or len(shard_buf) != self.row_width:
            return False
        if shard_idx in self.seen_indices:
            return False
        self.seen_indices.add(shard_idx)

        if shard_idx < self.k_shards:
            coeff = np.zeros(self.k_shards, dtype=np.uint8)
            coeff[shard_idx] = 1
        else:
            coeff = _cauchy_row(shard_idx - self.k_shards, self.k_shards)

        self.rows_coeff.append(coeff)
        self.rows_data.append(np.frombuffer(shard_buf, dtype=np.uint8).copy())

        if len(self.rows_coeff) >= self.k_shards:
            return self._try_solve()
        return False

    def _try_solve(self) -> bool:
        k = self.k_shards
        if k == 1:
            raw_row = self.rows_data[0]
            actual_len = struct.unpack_from("<H", raw_row, 0)[0]
            self.solved_bytes = bytes(raw_row[2 : 2 + actual_len])
            return True

        mat_g = np.stack(self.rows_coeff[:k], axis=0)
        mat_y = np.stack(self.rows_data[:k], axis=0)
        try:
            inv_g = _GF.invert_matrix(mat_g)
        except np.linalg.LinAlgError:
            self.rows_coeff.pop(0)
            self.rows_data.pop(0)
            return False

        rec_mat = _GF.matmul(inv_g, mat_y)
        out = bytearray()
        for i in range(k):
            actual_len = struct.unpack_from("<H", rec_mat[i], 0)[0]
            if actual_len > self.row_width - 2:
                return False
            out.extend(bytes(rec_mat[i, 2 : 2 + actual_len]))
        self.solved_bytes = bytes(out)
        self.rows_coeff.clear()
        self.rows_data.clear()
        return True


# ---------------------------------------------------------------------------
# Outbound & Inbound Reliable Stream / File Descriptors
# ---------------------------------------------------------------------------

@dataclass
class OutboundTransfer:
    stream_id: int
    peer_addr: Address
    kind: str  # "bytes" | "json" | "file"
    filename: str
    total_bytes: int
    sha256_hex: str
    generations: List[_TxGeneration]
    meta_payload: bytes
    peer_conn_id: Optional[int] = None
    base_unacked_gen: int = 0
    meta_acked: bool = False
    completed: bool = False
    verified_sha256_by_peer: bool = False
    started_at: float = field(default_factory=time.perf_counter)
    completed_at: Optional[float] = None
    last_meta_tx: float = 0.0
    pending_dof_requests: Dict[int, int] = field(default_factory=dict)


@dataclass
class ReceivedItem:
    conn_id: int
    peer_addr: Address
    stream_id: int
    kind: str  # "bytes" | "json" | "file" | "reflex"
    data: bytes
    filename: str = ""
    saved_path: Optional[Path] = None
    sha256_hex: str = ""
    sha256_verified: bool = True

    def as_json(self) -> Any:
        return json.loads(self.data.decode("utf-8"))

    def as_text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


class _InboundStreamState:
    def __init__(self, stream_id: int) -> None:
        self.stream_id = stream_id
        self.kind: Optional[str] = None
        self.filename: str = ""
        self.total_bytes: int = 0
        self.total_gens: int = 0
        self.expected_sha256: str = ""
        self.meta_received: bool = False
        self.gens: Dict[int, _RxGeneration] = {}
        self.solved_gens: Dict[int, bytes] = {}
        self.base_gen: int = 0
        self.completed: bool = False
        self.sha256_ok: bool = False
        self.shards_rcvd_count: int = 0
        self.bytes_rcvd: int = 0
        # Arrival rate over trains of >= 16 shards: shows the bottleneck rate whenever the sender outpaces it
        self.arrival_kbps: float = 0.0
        self._arr_t0: Optional[float] = None
        self._arr_b0: int = 0
        self._arr_n0: int = 0
        self.sys_shards_rcvd: int = 0
        self.gen_k_map: Dict[int, int] = {}
        self.gen_max_sys_seen: Dict[int, int] = {}
        self.max_gen_seen: int = -1

    def ingest_meta(self, meta_dict: Dict[str, Any]) -> None:
        self.kind = str(meta_dict["kind"])
        self.filename = str(meta_dict.get("filename", ""))
        self.total_bytes = int(meta_dict["total_bytes"])
        self.total_gens = int(meta_dict["total_gens"])
        self.expected_sha256 = str(meta_dict["sha256"])
        self.meta_received = True

    def advance_base(self) -> None:
        while self.base_gen in self.solved_gens:
            self.base_gen += 1

    def try_finalize(self) -> Optional[Tuple[bytes, bool]]:
        if self.completed or not self.meta_received:
            return None
        if self.total_gens == 0:
            self.completed = True
            self.sha256_ok = (hashlib.sha256(b"").hexdigest() == self.expected_sha256)
            return b"", self.sha256_ok
        if len(self.solved_gens) < self.total_gens:
            return None
        full = b"".join(self.solved_gens[g] for g in range(self.total_gens))
        full = full[: self.total_bytes]
        digest = hashlib.sha256(full).hexdigest()
        self.completed = True
        self.sha256_ok = (digest == self.expected_sha256)
        self.gens.clear()
        self.solved_gens.clear()
        return full, self.sha256_ok


# ---------------------------------------------------------------------------
# Adaptive Pacing: delivery-rate startup + queueing-delay cruise (random loss is NOT a slowdown signal)
# ---------------------------------------------------------------------------

class _DeliveryRateController:
    """
    Sets the token-bucket pacing rate from measurements instead of a fixed `max_tx_kbps`.

    Delivery is measured from the receiver's cumulative `bytes_rcvd` ACK trailer, once per round trip:
      - STARTUP: grow to 2x the measured delivery rate each round. Leave as soon as one RTT sample shows
        queueing delay (HyStart-style), or when delivery stops growing for 3 rounds.
      - CRUISE: when a queue builds, pace just under the estimated capacity (delivery corrected for
        random loss); when the path is idle, probe +10% per round; otherwise hold.
    Random loss is estimated only from rounds without a standing queue, so drops caused by our own
    overshoot are not mistaken for link loss. It drives proactive repair shards, never a slowdown.
    `max_tx_kbps` stays a hard ceiling.
    """

    STARTUP, CRUISE = 0, 1
    INIT_KBPS = 2_000.0
    MIN_KBPS = 64.0
    INIT_BURST_BYTES = 32 * 1024  # cold-start burst: fits a 32-packet home-router / 4G modem FIFO

    def __init__(self, cap_kbps: float) -> None:
        self.cap_kbps = float(cap_kbps)
        self.rate_kbps = min(self.INIT_KBPS, self.cap_kbps)
        self.mode = self.STARTUP
        self.min_rtt_ms = float("inf")
        self.round_min_rtt_ms = float("inf")
        self.n_rtt_samples = 0
        self.startup_exit_pending = False
        self.tracked_stream: Optional[int] = None
        self.round_t0: Optional[float] = None
        self.round_rx0 = 0
        self.sent_bytes = 0
        self.round_sent0 = 0
        self.prev_round_sent = 0
        self.prev_queue_ms = 0.0
        self.best_deliv_kbps = 0.0
        self.plateau_rounds = 0
        self.random_loss = 0.0
        self.random_loss_samples = 0
        self.last_deliv_kbps = 0.0
        self.last_queue_ms = 0.0
        self.btlbw_rounds: List[float] = []   # per-round max arrival rate, last 6 rounds
        self.round_arrival_max = 0.0
        self.train_probe_done = False
        # Path jitter (mean deviation of RTT samples): delay variation below ~1.5x this is not a queue
        self.srtt_ms = 0.0
        self.rttvar_ms = 0.0
        # (time, cumulative bytes sent) at each ACK, to compare deliveries with what was sent one RTT earlier
        self.sent_history: List[Tuple[float, int]] = []
        self.rounds_done = 0

    def btlbw_kbps(self) -> float:
        return max(self.btlbw_rounds + [self.round_arrival_max]) if (self.btlbw_rounds or self.round_arrival_max) else 0.0

    def start_stream(self, stream_id: int, now_s: float) -> None:
        """Measure delivery on this stream from now on (the trailer counts bytes per stream)."""
        self.tracked_stream = stream_id
        self.train_probe_done = self.mode == self.CRUISE and bool(self.btlbw_rounds)
        self.rounds_done = 0
        self.round_t0, self.round_rx0, self.round_sent0 = now_s, 0, self.sent_bytes
        self.prev_round_sent = 0
        self.round_min_rtt_ms = float("inf")

    def on_sent(self, n_bytes: int) -> None:
        self.sent_bytes += n_bytes

    def on_rtt_sample(self, rtt_ms: float) -> None:
        self.min_rtt_ms = min(self.min_rtt_ms, rtt_ms)
        self.round_min_rtt_ms = min(self.round_min_rtt_ms, rtt_ms)
        self.n_rtt_samples += 1
        if self.n_rtt_samples == 1:
            self.srtt_ms, self.rttvar_ms = rtt_ms, rtt_ms / 4.0
        else:
            self.rttvar_ms = 0.75 * self.rttvar_ms + 0.25 * abs(rtt_ms - self.srtt_ms)
            self.srtt_ms = 0.875 * self.srtt_ms + 0.125 * rtt_ms
        if self.mode == self.STARTUP and self.n_rtt_samples >= 4:
            if rtt_ms > self.min_rtt_ms + max(4.0, self.min_rtt_ms / 8.0, 2.0 * self.rttvar_ms):
                self.startup_exit_pending = True

    def _queue_target_ms(self) -> float:
        if self.min_rtt_ms == float("inf"):
            return 4.0
        return max(4.0, 0.05 * self.min_rtt_ms, 1.5 * self.rttvar_ms)

    def _sent_at(self, t: float) -> int:
        """Cumulative bytes sent as of time `t` (latest history entry not after `t`)."""
        for ts, sent in reversed(self.sent_history):
            if ts <= t:
                return sent
        return self.sent_history[0][1] if self.sent_history else 0

    def initial_burst(self, cold_bytes: int) -> int:
        """Unpaced bytes when a transfer starts on an idle connection. Only a cold start (rate not yet
        measured) gets a burst; a warm connection already paces at its learned rate from the first packet,
        and a burst on top of that overflowed the bottleneck queue and cost a repair timeout in testing."""
        return cold_bytes if self.mode == self.STARTUP else 0

    def loss_for_fec(self, fallback: float) -> float:
        return self.random_loss if self.random_loss_samples >= 2 else fallback

    def _capacity_kbps(self, deliv_kbps: float) -> float:
        return deliv_kbps / max(0.3, 1.0 - self.random_loss)

    def on_ack(self, now_s: float, stream_id: int, rx_bytes: int, srtt_ms: float, arrival_kbps: float = 0.0) -> bool:
        """Returns True when the pacing rate changed."""
        if self.tracked_stream != stream_id or self.round_t0 is None:
            return False
        self.sent_history.append((now_s, self.sent_bytes))
        if len(self.sent_history) > 512:
            del self.sent_history[:256]
        if arrival_kbps > 0.0:
            self.round_arrival_max = max(self.round_arrival_max, arrival_kbps)
            if self.mode == self.STARTUP and not self.train_probe_done:
                # First train = the unpaced initial burst, which reaches the receiver at the bottleneck rate:
                # jump straight to it instead of doubling from INIT_KBPS for several round trips.
                self.train_probe_done = True
                self.mode = self.CRUISE
                old = self.rate_kbps
                self.rate_kbps = max(self.MIN_KBPS, min(self.cap_kbps, max(self.rate_kbps, 1.05 * arrival_kbps)))
                return self.rate_kbps != old
        base_rtt = self.min_rtt_ms if self.min_rtt_ms < float("inf") else srtt_ms
        dt = now_s - self.round_t0
        early_exit = self.mode == self.STARTUP and self.startup_exit_pending and dt >= 0.005
        if dt < max(0.02, base_rtt / 1000.0) and not early_exit:
            return False

        delivered = (rx_bytes - self.round_rx0) & 0xFFFFFFFF
        deliv_kbps = delivered * 8.0 / 1000.0 / dt
        round_sent = self.sent_bytes - self.round_sent0
        queue_ms = self.round_min_rtt_ms - self.min_rtt_ms if self.round_min_rtt_ms < float("inf") else 0.0
        target_ms = self._queue_target_ms()
        old = self.rate_kbps

        # Random-loss sample: delivered this round vs what was sent one smoothed RTT earlier (so packets still
        # in flight or reordered are not counted as lost), skipping the first rounds while the pipe fills and
        # any round with a standing queue (whose drops are self-inflicted, not link loss)
        lag_s = (self.srtt_ms or base_rtt) / 1000.0
        sent_window = self._sent_at(now_s - lag_s) - self._sent_at(self.round_t0 - lag_s)
        self.rounds_done += 1
        if (self.rounds_done > 2 and sent_window > 8 * 1024 and queue_ms < target_ms
                and self.prev_queue_ms < target_ms):
            sample = max(0.0, min(0.9, 1.0 - delivered / float(sent_window)))
            self.random_loss = sample if self.random_loss_samples == 0 else 0.8 * self.random_loss + 0.2 * sample
            self.random_loss_samples += 1

        if self.mode == self.STARTUP:
            if deliv_kbps >= 1.25 * self.best_deliv_kbps:
                self.best_deliv_kbps = deliv_kbps
                self.plateau_rounds = 0
            else:
                self.plateau_rounds += 1
            if early_exit or queue_ms > target_ms:
                self.mode = self.CRUISE
                cap = self._capacity_kbps(max(deliv_kbps, self.best_deliv_kbps))
                self.rate_kbps = max(0.95 * cap, 0.5 * self.rate_kbps) if cap > 0 else 0.5 * self.rate_kbps
            elif self.plateau_rounds >= 3:
                self.mode = self.CRUISE
            elif deliv_kbps >= 0.5 * self.rate_kbps:
                self.rate_kbps = max(2.0 * self.rate_kbps, 2.0 * deliv_kbps)
        else:
            cap = max(self._capacity_kbps(deliv_kbps), self.btlbw_kbps())
            if queue_ms > 2.0 * target_ms:
                # Queue is building: pace just under capacity so it drains, instead of cutting again and again
                self.rate_kbps = min(self.rate_kbps * 0.9, 0.95 * cap) if cap > 0 else self.rate_kbps * 0.85
            elif queue_ms < target_ms:
                ceiling = 1.25 * cap if cap > 0 else self.rate_kbps * 1.10
                self.rate_kbps = min(self.rate_kbps * 1.10, max(self.rate_kbps, ceiling))

        self.rate_kbps = max(self.MIN_KBPS, min(self.cap_kbps, self.rate_kbps))
        self.last_deliv_kbps, self.last_queue_ms = deliv_kbps, queue_ms
        self.prev_round_sent, self.prev_queue_ms = round_sent, queue_ms
        self.round_t0, self.round_rx0, self.round_sent0 = now_s, rx_bytes, self.sent_bytes
        self.round_min_rtt_ms = float("inf")
        self.startup_exit_pending = False
        if self.round_arrival_max > 0.0:
            self.btlbw_rounds = (self.btlbw_rounds + [self.round_arrival_max])[-6:]
        self.round_arrival_max = 0.0
        return self.rate_kbps != old


# ---------------------------------------------------------------------------
# Core Transport Endpoint Engine (Shared by NLTPClient, NLTPServer & Simulator)
# ---------------------------------------------------------------------------

class NLTPEndpoint:
    """
    Transport-agnostic Internet-ready NLTP v1 protocol engine.

    Features:
      - Optional end-to-end `ChaCha20-Poly1305` encryption + `RFC 6479` replay window (`psk`)
      - Seamless `conn_id` Connection Migration across Wi-Fi <-> 4G IP/port changes
      - Router-safe `TokenBucketPacer` (`max_tx_kbps`) + Disinhibitory RTT congestion scaling
      - Automatic NAT pinhole keepalives (`keepalive_s`)
    """

    def __init__(
        self,
        conn_id: Optional[int] = None,
        *,
        send_packet_fn: Callable[[bytes, Address], None],
        save_dir: Optional[Union[str, Path]] = None,
        window_generations: int = 32,
        shard_bytes: int = DEFAULT_SHARD_BYTES,
        gen_k: int = DEFAULT_GEN_K,
        psk: Optional[Union[str, bytes]] = None,
        max_tx_kbps: Optional[float] = 50_000.0,
        keepalive_s: float = 15.0,
        adaptive_rate: bool = True,
        initial_burst_bytes: Optional[int] = None,
    ) -> None:
        self.conn_id = int(conn_id if conn_id is not None else int.from_bytes(os.urandom(4), "little") | 1)
        self._send_pkt = send_packet_fn
        self.save_dir = Path(save_dir) if save_dir is not None else None
        if self.save_dir is not None:
            self.save_dir.mkdir(parents=True, exist_ok=True)

        self.window_generations = window_generations
        self.shard_bytes = shard_bytes
        self.gen_k = gen_k
        self._aead: Optional[AEADCipherContext] = AEADCipherContext(psk) if psk is not None else None
        # Encrypted mode (NLTP 1.4): every peer gets per-session keys from a 1-RTT handshake (session.py)
        self._sessions: Optional[SessionTable] = (
            SessionTable(psk, self.conn_id, self._send_handshake) if psk is not None else None
        )
        self.pacer: Optional[TokenBucketPacer] = (
            TokenBucketPacer(rate_kbps=max_tx_kbps)
            if (max_tx_kbps is not None and max_tx_kbps > 0.0)
            else None
        )
        # With a pacer, `max_tx_kbps` is the ceiling and the actual rate is measured; `max_tx_kbps=None` stays unpaced.
        self.rate_ctl: Optional[_DeliveryRateController] = None
        if self.pacer is not None and adaptive_rate:
            self.rate_ctl = _DeliveryRateController(float(max_tx_kbps))
            self.pacer.set_rate_kbps(self.rate_ctl.rate_kbps, rescale_burst=True)
        # Cold-start burst; raise it (e.g. 64 KB) only on links known to buffer that much
        self.initial_burst_bytes = int(initial_burst_bytes) if initial_burst_bytes else _DeliveryRateController.INIT_BURST_BYTES
        self.keepalive_s = float(keepalive_s)
        self._keepalive_targets: Dict[Address, float] = {}

        self._next_stream_id: int = 1
        self._reflex_seq: int = 0
        # Bounded duplicate filter (insertion-ordered dict used as an LRU set)
        self._seen_reflex_ids: Dict[Tuple[int, int], None] = {}
        # Unacknowledged reflexes: seq -> [peer, payload, next_retx_s, interval_s, deadline_s]
        self._reflex_pending: Dict[int, list] = {}
        self._reflex_acked: Dict[int, None] = {}
        self.reflex_retransmits = 0
        self.reflex_acks_rx = 0
        self.reflex_expired = 0
        self._current_tx_now_s: Optional[float] = None

        # Connection Migration table: peer_conn_id -> latest verified (ip, port)
        self.conn_routes: Dict[int, Address] = {}

        # Outbound transfers indexed by stream_id (unique per sender endpoint) so peer IP roaming never orphans ACKs!
        self.outbound: Dict[int, OutboundTransfer] = {}
        # Inbound streams indexed by (peer_conn_id, stream_id) so client Wi-Fi -> 4G IP changes continue seamlessly!
        self.inbound: Dict[Tuple[int, int], _InboundStreamState] = {}
        self.completed_inbox: List[ReceivedItem] = []

        # Embedded NeckLink telemetry channels (keyed by peer_conn_id on RX for roaming, mirrored to Address)
        self.telemetry_tx: Dict[Address, NeckSocket] = {}
        self._telemetry_rx_by_cid: Dict[int, NeckSocket] = {}
        self._telemetry_rx_queue_by_cid: Dict[int, List[bytes]] = {}
        self.telemetry_rx: Dict[Address, NeckSocket] = {}
        self.latest_telemetry: Dict[Address, Any] = {}

        # Adaptive connection metrics (Disinhibitory Loss vs RTT Congestion State)
        self.srtt_ms: float = 150.0
        self._has_rtt_sample: bool = False
        self.min_rtt_ms: float = 999999.0
        self.peer_loss_ema: float = 0.0
        self.wire_bytes_tx: int = 0
        self.wire_bytes_rx: int = 0
        self.auth_drops_rx: int = 0
        self.roam_events: int = 0
        self.pkts_tx: int = 0
        self.pkts_rx: int = 0
        self.pongs_rx: int = 0
        self._pong_peers: set[Address] = set()
        self._last_ack_emit: Dict[Tuple[int, int], float] = {}
        self._pending_acks: Dict[Tuple[int, int], Tuple[int, bool]] = {}

    def register_keepalive_peer(self, peer: Address) -> None:
        self._keepalive_targets.setdefault(peer, 0.0)

    def _send_handshake(self, raw: bytes, peer: Address) -> None:
        self.wire_bytes_tx += len(raw)
        self.pkts_tx += 1
        self._send_pkt(raw, peer)

    def _emit(self, pkt_type: int, peer: Address, payload: bytes, flags: int = 0, now_s: Optional[float] = None) -> None:
        t_ref = time.perf_counter() if now_s is None else now_s
        ts_ms = int(t_ref * 1000.0) & 0xFFFFFFFF
        if self._sessions is not None:
            raw = self._sessions.seal(peer, pkt_type, payload, flags, ts_ms, t_ref)
            if raw is None:
                return  # no session with this peer yet: a handshake is running; senders retry
        else:
            raw = pack_nltp_frame(pkt_type, self.conn_id, payload, flags=flags, ts_ms=ts_ms)
        self.wire_bytes_tx += len(raw)
        self.pkts_tx += 1
        if peer in self._keepalive_targets:
            self._keepalive_targets[peer] = t_ref
        self._send_pkt(raw, peer)

    def send_ping(self, peer: Address, now_s: Optional[float] = None) -> None:
        t_ref = time.perf_counter() if now_s is None else now_s
        ts_ms = int(t_ref * 1000.0) & 0xFFFFFFFF
        self._emit(PT_PING, peer, struct.pack("<I", ts_ms), now_s=t_ref)

    def _can_send_bulk(self, n_bytes: int, now_s: float) -> bool:
        if self.pacer is None:
            return True
        overhead = (ENC_HEADER_SIZE + AEAD_TAG_SIZE) if self._aead is not None else HEADER_SIZE
        ok = self.pacer.consume_if_available(n_bytes + overhead, now_s)
        if ok and self.rate_ctl is not None:
            self.rate_ctl.on_sent(n_bytes + overhead)
        return ok

    def _fec_loss(self) -> float:
        """Link loss used to size repair shards: the controller's queue-free estimate when available."""
        if self.rate_ctl is not None:
            return self.rate_ctl.loss_for_fec(self.peer_loss_ema)
        return self.peer_loss_ema

    def start_transfer(
        self,
        peer: Address,
        data: bytes,
        *,
        kind: str = "bytes",
        filename: str = "",
        now_s: Optional[float] = None,
    ) -> OutboundTransfer:
        t_now = time.perf_counter() if now_s is None else now_s
        sid = self._next_stream_id
        self._next_stream_id += 1

        gen_slice_len = self.shard_bytes * self.gen_k
        gens: List[_TxGeneration] = []
        if len(data) == 0:
            gens.append(_TxGeneration(sid, 0, b"", self.shard_bytes, self.gen_k))
        else:
            for g_idx, off in enumerate(range(0, len(data), gen_slice_len)):
                gens.append(_TxGeneration(sid, g_idx, data[off : off + gen_slice_len], self.shard_bytes, self.gen_k))

        sha_hex = hashlib.sha256(data).hexdigest()
        meta_obj = {
            "stream_id": sid,
            "kind": kind,
            "filename": Path(filename).name if filename else "",
            "total_bytes": len(data),
            "total_gens": len(gens),
            "sha256": sha_hex,
        }
        meta_bytes = json.dumps(meta_obj, separators=(",", ":")).encode("utf-8")
        # Bind known peer_conn_id if we already have a route for this peer
        known_cid = next((cid for cid, addr in self.conn_routes.items() if addr == peer), None)
        xfer = OutboundTransfer(
            stream_id=sid,
            peer_addr=peer,
            peer_conn_id=known_cid,
            kind=kind,
            filename=meta_obj["filename"],
            total_bytes=len(data),
            sha256_hex=sha_hex,
            generations=gens,
            meta_payload=meta_bytes,
            started_at=t_now,
        )
        if self.rate_ctl is not None and self.pacer is not None and not any(
            not x.completed for x in self.outbound.values()
        ):
            burst = self.rate_ctl.initial_burst(self.initial_burst_bytes)
            if burst > 0:
                self.pacer.grant_burst(min(burst, len(data) + 2 * self.shard_bytes))
            self.rate_ctl.start_stream(sid, t_now)
        self.outbound[sid] = xfer
        self.register_keepalive_peer(peer)
        # 0-RTT immediate launch: send metadata + initial paced window right away!
        self.step_tx(now_s=t_now)
        return xfer

    def send_reflex(self, peer: Address, msg_bytes: bytes, *, cryptic_twin: bool = True, now_s: Optional[float] = None) -> int:
        """
        Giant-Fiber Reflex Lane: bypasses bulk file queues & pacer for 0-HoL-stall urgent commands!
        """
        self._reflex_seq = (self._reflex_seq + 1) & 0xFFFFFFFF
        payload = struct.pack("<IH", self._reflex_seq, len(msg_bytes)) + msg_bytes
        self._emit(PT_REFLEX_MSG, peer, payload, flags=0x02, now_s=now_s)
        if cryptic_twin or self.peer_loss_ema > 0.05:
            self._emit(PT_REFLEX_MSG, peer, payload, flags=0x02, now_s=now_s)
        # Reliable lane: retransmit until the receiver's PT_REFLEX_ACK arrives (or the deadline passes).
        # Receivers de-duplicate by (conn_id, seq), so retransmissions are delivered once.
        t_now = time.perf_counter() if now_s is None else now_s
        interval = min(REFLEX_RETX_FIRST_MAX_S, max(REFLEX_RETX_MIN_S, 1.25 * self.srtt_ms / 1000.0))
        self._reflex_pending[self._reflex_seq] = [peer, payload, t_now + interval, interval, t_now + REFLEX_ACK_TIMEOUT_S]
        return self._reflex_seq

    def reflex_acked(self, seq: int) -> bool:
        """True once the receiver has acknowledged reflex `seq` (peers older than v1.3.2 never acknowledge)."""
        return seq in self._reflex_acked

    def reflex_pending(self) -> int:
        """Number of reflexes still being retransmitted while waiting for an acknowledgement."""
        return len(self._reflex_pending)

    def _service_reflexes(self, t_now: float) -> None:
        for seq in list(self._reflex_pending):
            item = self._reflex_pending[seq]
            peer, payload, next_s, interval, deadline = item
            if t_now >= deadline:
                del self._reflex_pending[seq]
                self.reflex_expired += 1
                continue
            if t_now >= next_s:
                if self._sessions is not None and not self._sessions.ready(peer, t_now):
                    item[2] = t_now + interval
                    continue
                self._emit(PT_REFLEX_MSG, peer, payload, flags=0x02, now_s=t_now)
                self.reflex_retransmits += 1
                item[3] = min(REFLEX_RETX_MAX_S, interval * 1.5)
                item[2] = t_now + item[3]

    def send_telemetry(self, peer: Address, values: Any, *, profile: str = "sensor_plateau", now_s: Optional[float] = None) -> int:
        """Streams live sensor/AI values over the embedded zero-retransmission NeckLink lane."""
        self._current_tx_now_s = now_s
        if peer not in self.telemetry_tx:
            self.telemetry_tx[peer] = NeckSocket(
                profile=profile,
                send_fn=lambda pkt, _p=peer: self._emit(PT_NEURAL_STATE, _p, pkt, now_s=self._current_tx_now_s),
            )
        return self.telemetry_tx[peer].send(values)

    def poll_telemetry_tick(self, peer: Optional[Address] = None) -> Dict[Address, Any]:
        """Steps the embedded NeckLink mirror once per tick across peers (supports live `conn_id` IP roaming)."""
        snapshot: Dict[Address, Any] = {}
        for cid, rx_sock in list(self._telemetry_rx_by_cid.items()):
            cur_addr = self.conn_routes.get(cid)
            if cur_addr is None:
                continue
            if peer is not None and cur_addr != peer:
                continue
            pkts = self._telemetry_rx_queue_by_cid.pop(cid, [])
            out = rx_sock.step_with_packets(pkts)
            if out is not None:
                self.latest_telemetry[cur_addr] = out
                snapshot[cur_addr] = out
        return snapshot

    def drain_telemetry_async(self) -> Dict[Address, Any]:
        """Drains any queued telemetry frames without stepping empty ticks (for event-driven callers)."""
        for cid, pkts in list(self._telemetry_rx_queue_by_cid.items()):
            if pkts and cid in self._telemetry_rx_by_cid:
                self._telemetry_rx_queue_by_cid[cid] = []
                out = self._telemetry_rx_by_cid[cid].step_with_packets(pkts)
                cur_addr = self.conn_routes.get(cid)
                if out is not None and cur_addr is not None:
                    self.latest_telemetry[cur_addr] = out
        return dict(self.latest_telemetry)

    def _proactive_shards(self, xfer: OutboundTransfer, gen: _TxGeneration) -> int:
        p = self._fec_loss()
        n = int(np.ceil(gen.k_shards * p * 1.1)) if p >= 0.03 else 0
        # Tail: the last ~1 RTT of data has no later traffic to piggyback repairs on, so cover it up front
        if p >= 0.005 and self.rate_ctl is not None:
            gen_bytes = gen.k_shards * self.shard_bytes
            bdp_bytes = self.rate_ctl.rate_kbps * 1000.0 / 8.0 * (self.srtt_ms / 1000.0)
            tail_gens = max(1, int(np.ceil(bdp_bytes / max(1, gen_bytes))))
            if gen.gen_id >= len(xfer.generations) - tail_gens:
                n = max(n + 1, 2 if p >= 0.1 else 1)
        # Tail insurance: nothing follows the final generation to reveal a lost last packet, so it would
        # otherwise wait a full repair timeout; one parity shard covers that single loss immediately.
        if gen.gen_id == len(xfer.generations) - 1 and gen.k_shards > 1:
            n = max(n, 1)
        # Never more proactive repair than half the data: a mis-estimated loss must not flood the link
        return min(n, max(1, gen.k_shards // 2))

    def _effective_window_generations(self) -> int:
        """
        Disinhibitory Congestion Control:
          - Never shrinks window on random wireless packet drops alone (`peer_loss_ema` triggers repair, not choke).
          - Shrinks in-flight window ONLY when real RTT queue inflation (`srtt_ms > 2x min_rtt_ms + 35 ms`) proves
            an intermediate router/4G queue is filling up!
        """
        if self._has_rtt_sample and self.min_rtt_ms < 900000.0:
            queue_thresh = max(self.min_rtt_ms * 2.0, self.min_rtt_ms + 35.0)
            if self.srtt_ms > queue_thresh:
                return max(3, self.window_generations // 4)
        return self.window_generations

    def step_tx(self, now_s: Optional[float] = None) -> None:
        t_now = time.perf_counter() if now_s is None else now_s
        rto_s = max(0.055, min(2.0, (self.srtt_ms / 1000.0) * 1.85))
        inflight_guard_s = max(0.035, (self.srtt_ms / 1000.0) * 1.40)

        # 0. Session handshakes: HELLO retransmits, and re-keying with busy peers that went silent
        if self._sessions is not None:
            busy = {x.peer_addr for x in self.outbound.values() if not x.completed}
            busy.update(item[0] for item in self._reflex_pending.values())
            self._sessions.service(t_now, busy)

        # 0a. Retransmit unacknowledged reflexes (reliable reflex lane)
        if self._reflex_pending:
            self._service_reflexes(t_now)

        # 0. Flush any consolidated Proprioceptive ACKs accumulated during the latest UDP burst
        if self._pending_acks:
            pending = dict(self._pending_acks)
            self._pending_acks.clear()
            for (peer_cid, sid), (echo_ts_ms, force_ack) in pending.items():
                st = self.inbound.get((peer_cid, sid))
                peer_addr = self.conn_routes.get(peer_cid)
                if st is not None and peer_addr is not None and not st.completed:
                    self._check_stream_complete_or_ack(peer_addr, peer_cid, st, echo_ts_ms, t_now, force_ack=force_ack)

        # 1. Service active outbound streams/files
        eff_win = self._effective_window_generations()
        pacer_exhausted = False

        for sid, xfer in list(self.outbound.items()):
            if xfer.completed:
                continue
            # Follow roamed peer route if peer_conn_id is known!
            if xfer.peer_conn_id is not None and xfer.peer_conn_id in self.conn_routes:
                xfer.peer_addr = self.conn_routes[xfer.peer_conn_id]
            peer = xfer.peer_addr
            if self._sessions is not None and not self._sessions.ready(peer, t_now):
                continue  # wait for the session handshake before sending this transfer

            # Ensure stream metadata reaches receiver (0-RTT + periodic retry until acked, or FIN_ACK probe)
            need_fin_probe = (xfer.base_unacked_gen >= len(xfer.generations)) and (not xfer.completed)
            if (not xfer.meta_acked or need_fin_probe) and (xfer.last_meta_tx == 0.0 or (t_now - xfer.last_meta_tx) >= rto_s):
                self._emit(PT_STREAM_META, peer, xfer.meta_payload, now_s=t_now)
                xfer.last_meta_tx = t_now

            if pacer_exhausted:
                continue

            # Service explicit Proprioceptive DOF repair requests first (metered precision!)
            if xfer.pending_dof_requests:
                for g_id, dof in list(xfer.pending_dof_requests.items()):
                    if g_id >= len(xfer.generations) or xfer.generations[g_id].acked_complete:
                        del xfer.pending_dof_requests[g_id]
                        continue
                    gen = xfer.generations[g_id]
                    if (t_now - gen.last_repair_time) >= inflight_guard_s:
                        gen.repair_in_flight = 0
                    additional_needed = max(0, dof - gen.repair_in_flight)
                    if additional_needed <= 0:
                        del xfer.pending_dof_requests[g_id]
                        continue

                    extra = int(np.ceil(additional_needed / max(0.45, 1.0 - self._fec_loss() * 0.70)))
                    n_send = min(extra, gen.k_shards + 2)
                    sent_now = 0
                    for _ in range(n_send):
                        if not self._can_send_bulk(SHARD_HDR_SIZE + gen.row_width, t_now):
                            pacer_exhausted = True
                            break
                        self._emit(PT_CRYPTIC_PARITY, peer, gen.make_cryptic_parity_payload(), now_s=t_now)
                        sent_now += 1
                    if sent_now > 0:
                        gen.repair_in_flight += sent_now
                        gen.last_repair_time = t_now
                        gen.last_tx_time = t_now
                    if sent_now >= additional_needed:
                        del xfer.pending_dof_requests[g_id]
                    # else: keep `dof` as is — `repair_in_flight` already counts `sent_now`
                    if pacer_exhausted:
                        break

            if pacer_exhausted:
                continue

            # Advance sliding generation window (`base_unacked_gen .. base + eff_win`)
            win_end = min(len(xfer.generations), xfer.base_unacked_gen + eff_win)
            for g_id in range(xfer.base_unacked_gen, win_end):
                gen = xfer.generations[g_id]
                if gen.acked_complete:
                    continue

                if gen.next_sys_idx < gen.k_shards or not gen.proactive_done:
                    while gen.next_sys_idx < gen.k_shards:
                        if not self._can_send_bulk(SHARD_HDR_SIZE + gen.row_width, t_now):
                            pacer_exhausted = True
                            break
                        self._emit(PT_DATA_SHARD, peer, gen.make_systematic_payload(gen.next_sys_idx), now_s=t_now)
                        gen.next_sys_idx += 1

                    if pacer_exhausted:
                        break

                    if not gen.proactive_done:
                        if gen.proactive_target < 0:
                            gen.proactive_target = self._proactive_shards(xfer, gen)
                        while gen.proactive_sent < gen.proactive_target:
                            if not self._can_send_bulk(SHARD_HDR_SIZE + gen.row_width, t_now):
                                pacer_exhausted = True
                                break
                            self._emit(PT_CRYPTIC_PARITY, peer, gen.make_cryptic_parity_payload(), now_s=t_now)
                            gen.proactive_sent += 1
                        if not pacer_exhausted:
                            gen.proactive_done = True
                            gen.last_tx_time = t_now
                        else:
                            break

                elif (t_now - gen.last_tx_time) >= rto_s:
                    n_tail = 1 if self.peer_loss_ema < 0.22 else 2
                    sent_tail = 0
                    for _ in range(min(n_tail, gen.k_shards)):
                        if not self._can_send_bulk(SHARD_HDR_SIZE + gen.row_width, t_now):
                            pacer_exhausted = True
                            break
                        self._emit(PT_CRYPTIC_PARITY, peer, gen.make_cryptic_parity_payload(), now_s=t_now)
                        sent_tail += 1
                    if sent_tail > 0:
                        gen.repair_in_flight = 0
                        gen.last_tx_time = t_now
                    if pacer_exhausted:
                        break

        # 2. Emit periodic NAT/CGNAT pinhole keepalives on idle links
        if self.keepalive_s > 0.0 and self._keepalive_targets:
            for peer_addr, last_tx in list(self._keepalive_targets.items()):
                if last_tx == 0.0 or (t_now - last_tx) >= self.keepalive_s:
                    self.send_ping(peer_addr, now_s=t_now)

    @staticmethod
    def _ack_from_transfer_peer(xfer: "OutboundTransfer", peer_conn_id: int, peer: Address) -> bool:
        """An ACK belongs to this transfer if it comes from the receiver's conn_id, or from the transfer's
        address under a new conn_id: the receiver restarted (start_transfer pre-fills the last conn_id seen
        at that address, which is stale after a restart and would otherwise make every ACK be ignored)."""
        return xfer.peer_conn_id is None or xfer.peer_conn_id == peer_conn_id or peer == xfer.peer_addr

    def _update_peer_route(self, peer_conn_id: int, peer: Address) -> None:
        prev = self.conn_routes.get(peer_conn_id)
        if prev is not None and prev != peer:
            self.roam_events += 1
            if prev in self.telemetry_rx:
                self.telemetry_rx[peer] = self.telemetry_rx.pop(prev)
        self.conn_routes[peer_conn_id] = peer

    def _record_rtt_sample(self, rtt_ms: float) -> None:
        eff_rtt = max(1.0, rtt_ms)
        self.min_rtt_ms = min(self.min_rtt_ms, eff_rtt)
        if self.rate_ctl is not None:
            self.rate_ctl.on_rtt_sample(eff_rtt)
        if not self._has_rtt_sample:
            self.srtt_ms = eff_rtt
            self._has_rtt_sample = True
        else:
            self.srtt_ms = 0.80 * self.srtt_ms + 0.20 * eff_rtt

    def on_datagram(self, raw: bytes, peer: Address, now_s: Optional[float] = None) -> None:
        t_now = time.perf_counter() if now_s is None else now_s
        self.wire_bytes_rx += len(raw)
        self.pkts_rx += 1

        if self._sessions is not None:
            parsed = self._sessions.open(raw, peer, t_now)
            if parsed is HANDLED:
                return
        else:
            parsed = unpack_nltp_frame(raw)
        if parsed is None:
            self.auth_drops_rx += 1
            return
        pkt_type, _flags, peer_conn_id, pkt_ts_ms, payload = parsed

        # Verified frame -> update live Connection Migration route table!
        self._update_peer_route(peer_conn_id, peer)

        if pkt_type == PT_REFLEX_MSG and len(payload) >= 6:
            r_seq, m_len = struct.unpack_from("<IH", payload, 0)
            key = (peer_conn_id, r_seq)
            if key not in self._seen_reflex_ids:
                self._seen_reflex_ids[key] = None
                if len(self._seen_reflex_ids) > REFLEX_SEEN_MAX:
                    self._seen_reflex_ids.pop(next(iter(self._seen_reflex_ids)))
                msg = payload[6 : 6 + m_len]
                self.completed_inbox.append(
                    ReceivedItem(conn_id=peer_conn_id, peer_addr=peer, stream_id=0, kind="reflex", data=msg)
                )
            # Acknowledge every copy (the sender keeps retransmitting until one ACK gets through)
            self._emit(PT_REFLEX_ACK, peer, struct.pack("<I", r_seq), flags=0x02, now_s=t_now)
            return

        if pkt_type == PT_REFLEX_ACK and len(payload) >= 4:
            (a_seq,) = struct.unpack_from("<I", payload, 0)
            if self._reflex_pending.pop(a_seq, None) is not None:
                self.reflex_acks_rx += 1
                self._reflex_acked[a_seq] = None
                if len(self._reflex_acked) > REFLEX_SEEN_MAX:
                    self._reflex_acked.pop(next(iter(self._reflex_acked)))
            return

        if pkt_type == PT_NEURAL_STATE:
            if peer_conn_id not in self._telemetry_rx_by_cid:
                rx_s = NeckSocket(send_fn=lambda _b: None)
                self._telemetry_rx_by_cid[peer_conn_id] = rx_s
                self.telemetry_rx[peer] = rx_s
            else:
                self.telemetry_rx[peer] = self._telemetry_rx_by_cid[peer_conn_id]
            self._telemetry_rx_queue_by_cid.setdefault(peer_conn_id, []).append(payload)
            return

        if pkt_type == PT_PING:
            self._emit(PT_PONG, peer, struct.pack("<I", pkt_ts_ms), now_s=t_now)
            return

        if pkt_type == PT_PONG and len(payload) >= 4:
            orig_ts = struct.unpack_from("<I", payload, 0)[0]
            now_ms = int(t_now * 1000.0) & 0xFFFFFFFF
            rtt = float((now_ms - orig_ts) & 0xFFFFFFFF)
            if 0.0 <= rtt < 30000.0:
                self._record_rtt_sample(rtt)
            self.pongs_rx += 1
            self._pong_peers.add(peer)
            return

        if pkt_type == PT_STREAM_META:
            try:
                meta = json.loads(payload.decode("utf-8"))
                sid = int(meta["stream_id"])
            except Exception:
                return
            st = self.inbound.get((peer_conn_id, sid))
            if st is None:
                st = _InboundStreamState(sid)
                self.inbound[(peer_conn_id, sid)] = st
            if st.completed:
                self._emit(PT_STREAM_FIN_ACK, peer, struct.pack("<IB", sid, 1 if st.sha256_ok else 0), now_s=t_now)
                return
            st.ingest_meta(meta)
            self._check_stream_complete_or_ack(peer, peer_conn_id, st, pkt_ts_ms, t_now, force_ack=True)
            return

        if pkt_type in (PT_DATA_SHARD, PT_CRYPTIC_PARITY) and len(payload) >= SHARD_HDR_SIZE:
            sid, gen_id, k_shards, shard_idx, row_w = struct.unpack_from(SHARD_HDR_FMT, payload, 0)
            shard_buf = payload[SHARD_HDR_SIZE : SHARD_HDR_SIZE + row_w]
            if len(shard_buf) != row_w or k_shards == 0:
                return

            st = self.inbound.get((peer_conn_id, sid))
            if st is None:
                st = _InboundStreamState(sid)
                self.inbound[(peer_conn_id, sid)] = st

            if st.completed:
                self._emit(PT_STREAM_FIN_ACK, peer, struct.pack("<IB", sid, 1 if st.sha256_ok else 0), now_s=t_now)
                return

            st.shards_rcvd_count += 1
            st.bytes_rcvd += len(payload)
            if st._arr_t0 is None:
                st._arr_t0, st._arr_b0, st._arr_n0 = t_now, st.bytes_rcvd, st.shards_rcvd_count
            elif st.shards_rcvd_count - st._arr_n0 >= 16 and t_now - st._arr_t0 >= 0.005:
                st.arrival_kbps = (st.bytes_rcvd - st._arr_b0) * 8.0 / 1000.0 / (t_now - st._arr_t0)
                st._arr_t0, st._arr_b0, st._arr_n0 = t_now, st.bytes_rcvd, st.shards_rcvd_count
            st.gen_k_map[gen_id] = k_shards
            if pkt_type == PT_DATA_SHARD and shard_idx < k_shards:
                rx_existing = st.gens.get(gen_id)
                already_seen = (gen_id in st.solved_gens) or (rx_existing is not None and shard_idx in rx_existing.seen_indices)
                if not already_seen:
                    st.sys_shards_rcvd += 1
                st.gen_max_sys_seen[gen_id] = max(st.gen_max_sys_seen.get(gen_id, -1), shard_idx)
            elif pkt_type == PT_CRYPTIC_PARITY:
                st.gen_max_sys_seen[gen_id] = k_shards - 1
            st.max_gen_seen = max(st.max_gen_seen, gen_id)

            if gen_id not in st.solved_gens:
                rx_gen = st.gens.get(gen_id)
                if rx_gen is None:
                    rx_gen = _RxGeneration(k_shards, row_w)
                    st.gens[gen_id] = rx_gen
                newly_solved = rx_gen.add_shard(shard_idx, shard_buf)
                if newly_solved and rx_gen.solved_bytes is not None:
                    st.solved_gens[gen_id] = rx_gen.solved_bytes
                    del st.gens[gen_id]
                    st.advance_base()
                    if st.meta_received and len(st.solved_gens) >= st.total_gens:
                        self._check_stream_complete_or_ack(peer, peer_conn_id, st, pkt_ts_ms, t_now, force_ack=True)
                    else:
                        prev_f = self._pending_acks.get((peer_conn_id, sid), (pkt_ts_ms, False))[1]
                        self._pending_acks[(peer_conn_id, sid)] = (pkt_ts_ms, True or prev_f)
                    return

            last_a = self._last_ack_emit.get((peer_conn_id, sid), 0.0)
            if (st.shards_rcvd_count % 8 == 0) or ((t_now - last_a) >= 0.025):
                # Always answer (rate-limited): if the sender's view of `base_gen` is stale it is only sending
                # repairs for generations we already solved, and staying silent would stall it forever.
                self._pending_acks[(peer_conn_id, sid)] = (pkt_ts_ms, True)
            return

        if pkt_type == PT_PROPRIOCEPTIVE_ACK and len(payload) >= 14:
            sid, base_gen, echo_ts, loss_q8, n_rep = struct.unpack_from("<IIIBB", payload, 0)
            xfer = self.outbound.get(sid)
            if xfer is None or xfer.completed:
                return
            if not self._ack_from_transfer_peer(xfer, peer_conn_id, peer):
                return
            xfer.peer_conn_id = peer_conn_id
            xfer.peer_addr = peer
            xfer.meta_acked = True

            now_ms = int(t_now * 1000.0) & 0xFFFFFFFF
            rtt = float((now_ms - echo_ts) & 0xFFFFFFFF)
            if 0.0 <= rtt < 30000.0:
                self._record_rtt_sample(rtt)
            rep_loss = float(loss_q8) / 255.0
            self.peer_loss_ema = 0.65 * self.peer_loss_ema + 0.35 * rep_loss

            for g in range(xfer.base_unacked_gen, min(base_gen, len(xfer.generations))):
                xfer.generations[g].acked_complete = True
            xfer.base_unacked_gen = max(xfer.base_unacked_gen, base_gen)

            pos = 14
            for _ in range(n_rep):
                if pos + 4 > len(payload):
                    break
                g_off, _have, needed_dof = struct.unpack_from("<HBB", payload, pos)
                pos += 4
                g_abs = base_gen + g_off
                if needed_dof > 0 and g_abs < len(xfer.generations) and not xfer.generations[g_abs].acked_complete:
                    xfer.pending_dof_requests[g_abs] = needed_dof

            if self.rate_ctl is not None and self.pacer is not None and pos + 4 <= len(payload):
                rx_bytes = struct.unpack_from("<I", payload, pos)[0]
                arrival_kbps = float(struct.unpack_from("<I", payload, pos + 4)[0]) if pos + 8 <= len(payload) else 0.0
                if self.rate_ctl.on_ack(t_now, sid, rx_bytes, self.srtt_ms, arrival_kbps):
                    self.pacer.set_rate_kbps(self.rate_ctl.rate_kbps, rescale_burst=True)

            self.step_tx(now_s=t_now)
            return

        if pkt_type == PT_STREAM_FIN_ACK and len(payload) >= 5:
            sid, sha_ok = struct.unpack_from("<IB", payload, 0)
            xfer = self.outbound.get(sid)
            if xfer is not None and not xfer.completed:
                if not self._ack_from_transfer_peer(xfer, peer_conn_id, peer):
                    return
                xfer.peer_conn_id = peer_conn_id
                xfer.peer_addr = peer
                xfer.meta_acked = True
                xfer.completed = True
                xfer.verified_sha256_by_peer = bool(sha_ok == 1)
                xfer.completed_at = t_now
                for g in xfer.generations:
                    g.acked_complete = True
            return

    def _check_stream_complete_or_ack(
        self,
        peer: Address,
        peer_conn_id: int,
        st: _InboundStreamState,
        echo_ts_ms: int,
        t_now: float,
        force_ack: bool,
    ) -> None:
        fin = st.try_finalize()
        if fin is not None:
            full_bytes, sha_ok = fin
            saved_path: Optional[Path] = None
            if st.kind == "file" and self.save_dir is not None:
                safe_name = Path(st.filename).name or f"stream_{st.stream_id}.bin"
                saved_path = self.save_dir / safe_name
                saved_path.write_bytes(full_bytes)

            self.completed_inbox.append(
                ReceivedItem(
                    conn_id=peer_conn_id,
                    peer_addr=peer,
                    stream_id=st.stream_id,
                    kind=st.kind or "bytes",
                    data=full_bytes,
                    filename=st.filename,
                    saved_path=saved_path,
                    sha256_hex=hashlib.sha256(full_bytes).hexdigest(),
                    sha256_verified=sha_ok,
                )
            )
            self._emit(PT_STREAM_FIN_ACK, peer, struct.pack("<IB", st.stream_id, 1 if sha_ok else 0), now_s=t_now)
            return

        max_g = st.max_gen_seen
        expected_sys = 0
        for g_i in range(max_g):
            expected_sys += st.gen_k_map.get(g_i, self.gen_k)
        if max_g >= 0:
            expected_sys += st.gen_max_sys_seen.get(max_g, 0) + 1
        expected_sys = max(1, expected_sys)
        est_loss = max(0.0, min(0.92, 1.0 - (st.sys_shards_rcvd / float(expected_sys))))
        loss_q8 = int(round(est_loss * 255.0))

        reports: List[bytes] = []
        for g_id in range(st.base_gen, max_g + 1):
            if g_id in st.solved_gens:
                continue
            rx_g = st.gens.get(g_id)
            have = len(rx_g.rows_coeff) if rx_g is not None else 0
            need = rx_g.needed_dof if rx_g is not None else self.gen_k
            if g_id < max_g and need > 0:
                reports.append(struct.pack("<HBB", g_id - st.base_gen, have, need))
            elif g_id == max_g and rx_g is not None and rx_g.seen_indices:
                max_seen_idx = min(rx_g.k_shards, max(rx_g.seen_indices) + 1)
                confirmed_missing = min(need, max(0, max_seen_idx - have))
                if confirmed_missing > 0:
                    reports.append(struct.pack("<HBB", g_id - st.base_gen, have, confirmed_missing))
            if len(reports) >= 16:
                break

        if force_ack or reports:
            ack_hdr = struct.pack("<IIIBB", st.stream_id, st.base_gen, echo_ts_ms, loss_q8, len(reports))
            # Optional trailer: cumulative shard bytes received + latest arrival rate (kbps), for the sender's
            # rate controller. Older parsers stop after the reports and ignore it.
            trailer = struct.pack("<II", st.bytes_rcvd & 0xFFFFFFFF, min(0xFFFFFFFF, int(st.arrival_kbps)))
            self._emit(PT_PROPRIOCEPTIVE_ACK, peer, ack_hdr + b"".join(reports) + trailer, now_s=t_now)
            self._last_ack_emit[(peer_conn_id, st.stream_id)] = t_now


# ---------------------------------------------------------------------------
# Shared Socket / STUN / Hole-Punching / CGNAT Relay Base Mixin
# ---------------------------------------------------------------------------

class _NLTPSocketBase:
    """Common Internet UDP I/O loop supporting DNS, IPv4/IPv6, live STUN, P2P hole-punching & RelayHub."""

    def _linger_for_handshake(self, max_s: float = 1.0) -> None:
        """Frames sent just before close() may still be waiting for a session handshake: give it up to
        `max_s` to finish so they go out (e.g. `send_reflex(...)` immediately followed by `close()`)."""
        sessions = getattr(self.engine, "_sessions", None)
        deadline = time.perf_counter() + max_s
        while sessions is not None and time.perf_counter() < deadline:
            with self._lock:
                if sessions.queued_count() == 0:
                    return
            time.sleep(0.005)

    def wait_reflex_ack(self, seq: int, timeout: float = 5.0) -> bool:
        """Block until reflex `seq` is acknowledged; False on timeout (or with a peer older than v1.3.2).
        Keep the socket open until then: closing it stops the retransmissions."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            with self._lock:
                if self.engine.reflex_acked(seq):
                    return True
            time.sleep(0.002)
        return False

    _sock: socket.socket
    engine: NLTPEndpoint
    _lock: threading.Lock
    _running: bool
    _stun_inbox: "queue.Queue[Tuple[bytes, Address]]"
    _relay_target: Optional[Address]
    _room_tag: Optional[bytes]
    _relay_registered: bool
    _last_relay_reg: float
    _loss_sim: float
    _rng: np.random.Generator

    def _init_net_common(
        self,
        *,
        relay_addr: Optional[Tuple[str, int]],
        room: Optional[str],
        loss_drop_sim: float,
        rng_seed: int,
    ) -> None:
        self._stun_inbox = queue.Queue()
        self._relay_registered = False
        self._last_relay_reg = 0.0
        self._loss_sim = float(loss_drop_sim)
        self._rng = np.random.default_rng(rng_seed)

        if relay_addr is not None:
            if not room:
                raise ValueError("`room` name is required when `relay_addr` is enabled")
            _af, r_sa = resolve_udp_address(relay_addr[0], relay_addr[1], prefer_family=self._sock.family)
            self._relay_target = r_sa
            self._room_tag = make_room_tag(room)
        else:
            self._relay_target = None
            self._room_tag = None

    def _send_relay_register(self) -> None:
        if self._relay_target is not None and self._room_tag is not None:
            reg_pkt = wrap_relay_packet(self._room_tag, b"", cmd=RELAY_VER_REGISTER)
            try:
                self._sock.sendto(reg_pkt, self._relay_target)
                self._last_relay_reg = time.perf_counter()
            except OSError:
                pass

    def _raw_sendto(self, pkt: bytes, addr: Address) -> None:
        if self._loss_sim > 0.0 and self._rng.random() < self._loss_sim:
            return
        try:
            if self._relay_target is not None and self._room_tag is not None:
                wrapped = wrap_relay_packet(self._room_tag, pkt, cmd=RELAY_VER_DATA)
                self._sock.sendto(wrapped, self._relay_target)
            else:
                self._sock.sendto(pkt, addr)
        except OSError:
            pass

    def _io_loop(self) -> None:
        if self._relay_target is not None:
            with self._lock:
                self._send_relay_register()

        while self._running:
            got_pkt = False
            with self._lock:
                now = time.perf_counter()
                if self._relay_target is not None:
                    reg_interval = 0.25 if not self._relay_registered else max(5.0, self.engine.keepalive_s)
                    if (now - self._last_relay_reg) >= reg_interval:
                        self._send_relay_register()

                for _ in range(128):
                    try:
                        data, addr = self._sock.recvfrom(65535)
                        got_pkt = True
                    except (BlockingIOError, InterruptedError, ConnectionResetError):
                        break
                    except OSError:
                        if not self._running:
                            return
                        break

                    if is_stun_packet(data):
                        self._stun_inbox.put((data, addr))
                        continue

                    if self._room_tag is not None:
                        unwrapped = unwrap_relay_packet(data)
                        if unwrapped is None:
                            continue
                        cmd, r_tag, inner = unwrapped
                        if r_tag != self._room_tag:
                            continue
                        if cmd == RELAY_VER_REG_ACK:
                            self._relay_registered = True
                            continue
                        if cmd == RELAY_VER_DATA and inner:
                            self.engine.on_datagram(inner, addr)
                        continue

                    self.engine.on_datagram(data, addr)

                self.engine.step_tx()
                sock_ref = self._sock
            if not got_pkt and self._running:
                try:
                    select.select([sock_ref], [], [], 0.001)
                except (OSError, ValueError):
                    time.sleep(0.001)

    def discover_wan(
        self,
        *,
        stun_servers: Sequence[Tuple[str, int]] = DEFAULT_PUBLIC_STUN_SERVERS,
        timeout_s: float = 2.5,
    ) -> STUNDiscoveryResult:
        """
        Queries public Internet STUN servers directly from THIS live NLTP socket (zero port race!)
        to discover the exact router-mapped external `(public_ip, public_port)` and NAT type.
        """
        mapped_results: List[Tuple[str, Tuple[str, int], float]] = []
        for host, port in stun_servers:
            try:
                _af, sa = resolve_udp_address(host, port, prefer_family=self._sock.family)
                req, tid = build_stun_binding_request()
                t0 = time.perf_counter()
                with self._lock:
                    self._sock.sendto(req, sa)
                deadline = t0 + timeout_s
                # Lossy links drop STUN packets too: resend the same transaction (RFC 5389 7.2.1)
                next_resend = t0 + 0.5
                while time.perf_counter() < deadline:
                    now = time.perf_counter()
                    if now >= next_resend:
                        with self._lock:
                            self._sock.sendto(req, sa)
                        next_resend = now + 0.5
                    rem = max(0.01, min(deadline, next_resend) - time.perf_counter())
                    try:
                        raw, _from = self._stun_inbox.get(timeout=rem)
                    except queue.Empty:
                        continue
                    ans = parse_stun_binding_response(raw, expected_tx_id=tid)
                    if ans is not None:
                        rtt = (time.perf_counter() - t0) * 1000.0
                        mapped_results.append((f"{host}:{port}", ans, rtt))
                        break
                if len(mapped_results) >= 2:
                    break
            except OSError:
                continue

        if not mapped_results:
            raise TimeoutError("No STUN response received on NLTP socket — outbound UDP may be blocked")

        first_srv, (pub_ip, pub_port), first_rtt = mapped_results[0]
        with self._lock:
            local_port = int(self._sock.getsockname()[1])

        if len(mapped_results) >= 2:
            eim = all(m[1][1] == pub_port for m in mapped_results)
            summary = (
                "Endpoint-Independent NAT (Direct P2P UDP Hole-Punching Supported)"
                if eim
                else "Symmetric CGNAT Detected (Use `NLTPRelayHub` `--relay` Mode)"
            )
        else:
            eim = True
            summary = f"Single STUN server reachable ({first_srv}); WAN mapping verified"

        return STUNDiscoveryResult(
            public_ip=pub_ip,
            public_port=pub_port,
            local_port=local_port,
            rtt_ms=round(first_rtt, 2),
            endpoint_independent_nat=eim,
            nat_summary=summary,
            servers_queried=[m[0] for m in mapped_results],
        )

    def punch_p2p(self, peer_wan_addr: Tuple[str, int], *, timeout_s: float = 5.0, pre_punch_ttl: int = 2) -> bool:
        """
        Executes bidirectional UDP NAT hole-punching against `peer_wan_addr` using authenticated
        `PT_PING` / `PT_PONG` frames. Returns `True` as soon as two-way WAN reachability is verified!

        `pre_punch_ttl` (IPv4): first sends a few pings with that IP TTL. They open this side's NAT mapping but
        expire in the network before reaching the peer's NAT, so they cannot create an unanswered inbound entry
        there that would make the peer's NAT re-map its port when its own pings go out (a port-clash race seen
        with Linux-based routers). 0 disables it.
        """
        _af, target_sa = resolve_udp_address(peer_wan_addr[0], peer_wan_addr[1], prefer_family=self._sock.family)
        with self._lock:
            base_pongs = self.engine.pongs_rx
            self.engine.register_keepalive_peer(target_sa)

        if pre_punch_ttl > 0 and self._sock.family == socket.AF_INET:
            try:
                old_ttl = self._sock.getsockopt(socket.IPPROTO_IP, socket.IP_TTL)
                self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, pre_punch_ttl)
                for _ in range(3):
                    with self._lock:
                        self.engine.send_ping(target_sa)
                    time.sleep(0.03)
                self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, old_ttl)
                time.sleep(0.3)
            except OSError:
                pass

        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            with self._lock:
                if self.engine.pongs_rx > base_pongs or target_sa in self.engine._pong_peers:
                    return True
                self.engine.send_ping(target_sa)
            time.sleep(0.055)

        with self._lock:
            return self.engine.pongs_rx > base_pongs or target_sa in self.engine._pong_peers


# ---------------------------------------------------------------------------
# User-Facing Standalone Server & Client (Real Internet UDP Sockets)
# ---------------------------------------------------------------------------

class NLTPServer(_NLTPSocketBase):
    """
    Internet-Ready Standalone NLTP v1 Server.

    Supports direct public IPv4/IPv6/DNS listening, P2P STUN NAT hole-punching (`discover_wan` +
    `punch_p2p`), or zero-port-forwarding CGNAT cloud relay (`relay_addr` + `room`), with optional
    `ChaCha20-Poly1305` end-to-end encryption (`psk`) and seamless client Wi-Fi-to-4G roaming!
    """

    def __init__(
        self,
        bind_addr: Tuple[str, int] = ("0.0.0.0", 9000),
        *,
        save_dir: Optional[Union[str, Path]] = None,
        psk: Optional[Union[str, bytes]] = None,
        relay_addr: Optional[Tuple[str, int]] = None,
        room: Optional[str] = None,
        max_tx_kbps: Optional[float] = 50_000.0,
        keepalive_s: float = 15.0,
        loss_drop_sim: float = 0.0,
        adaptive_rate: bool = True,
        initial_burst_bytes: Optional[int] = None,
    ) -> None:
        self._sock, _ = open_internet_udp_socket(
            bind_addr=bind_addr,
            target_addr=relay_addr,
            nonblocking=True,
        )
        self._lock = threading.Lock()
        self._init_net_common(relay_addr=relay_addr, room=room, loss_drop_sim=loss_drop_sim, rng_seed=2026)

        self.engine = NLTPEndpoint(
            send_packet_fn=self._raw_sendto,
            save_dir=save_dir,
            psk=psk,
            max_tx_kbps=max_tx_kbps,
            keepalive_s=keepalive_s,
            adaptive_rate=adaptive_rate,
            initial_burst_bytes=initial_burst_bytes,
        )
        self._running = True
        self._worker = threading.Thread(target=self._io_loop, daemon=True)
        self._worker.start()

    def getsockname(self) -> Address:
        return self._sock.getsockname()

    def recv(self, timeout: float = 5.0) -> Optional[ReceivedItem]:
        """Blocks up to `timeout` seconds for the next completed file, message, JSON, or reflex alert."""
        deadline = time.perf_counter() + timeout
        while True:  # checks at least once, so recv(timeout=0) is a non-blocking poll
            with self._lock:
                if self.engine.completed_inbox:
                    return self.engine.completed_inbox.pop(0)
            if time.perf_counter() >= deadline:
                return None
            time.sleep(0.002)

    def poll_telemetry(self) -> Dict[Address, Any]:
        with self._lock:
            return self.engine.drain_telemetry_async()

    def poll_telemetry_tick(self, peer: Optional[Address] = None) -> Dict[Address, Any]:
        with self._lock:
            return self.engine.poll_telemetry_tick(peer)

    def send_reflex(self, client_addr: Address, urgent_msg: Union[str, bytes]) -> int:
        """Urgent reflex (e.g. an E-stop) to a connected client; retransmitted until acknowledged.
        Pair with `wait_reflex_ack(seq)` to confirm delivery."""
        b = urgent_msg.encode("utf-8") if isinstance(urgent_msg, str) else urgent_msg
        with self._lock:
            return self.engine.send_reflex(client_addr, b)

    def send_bytes(self, client_addr: Address, data: bytes, timeout: float = 15.0) -> bool:
        with self._lock:
            xfer = self.engine.start_transfer(client_addr, data, kind="bytes")
        return self._wait_transfer(xfer, timeout)

    def send_file(self, client_addr: Address, filepath: Union[str, Path], timeout: float = 60.0) -> bool:
        p = Path(filepath)
        raw = p.read_bytes()
        with self._lock:
            xfer = self.engine.start_transfer(client_addr, raw, kind="file", filename=p.name)
        return self._wait_transfer(xfer, timeout)

    def _wait_transfer(self, xfer: OutboundTransfer, timeout: float) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if xfer.completed:
                return xfer.verified_sha256_by_peer
            time.sleep(0.003)
        return False

    def close(self) -> None:
        self._linger_for_handshake()
        self._running = False
        self._worker.join(timeout=1.0)
        self._sock.close()

    def __enter__(self) -> "NLTPServer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class NLTPClient(_NLTPSocketBase):
    """
    Internet-Ready Standalone NLTP v1 Client.

    Connects in 0-RTT to any `NLTPServer` over DNS/IPv4/IPv6, STUN P2P hole-punching, or
    `NLTPRelayHub`, with end-to-end `ChaCha20-Poly1305` encryption (`psk`), router-safe packet
    pacing (`max_tx_kbps`), NAT keepalives, and live Wi-Fi-to-4G socket roaming (`rebind_socket`).
    """

    def __init__(
        self,
        server_addr: Optional[Tuple[str, int]] = None,
        *,
        bind_addr: Optional[Tuple[str, int]] = None,
        save_dir: Optional[Union[str, Path]] = None,
        psk: Optional[Union[str, bytes]] = None,
        relay_addr: Optional[Tuple[str, int]] = None,
        room: Optional[str] = None,
        max_tx_kbps: Optional[float] = 50_000.0,
        keepalive_s: float = 15.0,
        loss_drop_sim: float = 0.0,
        adaptive_rate: bool = True,
        initial_burst_bytes: Optional[int] = None,
    ) -> None:
        effective_target = relay_addr if relay_addr is not None else server_addr
        if effective_target is None:
            raise ValueError("Provide either `server_addr` or `relay_addr`")

        self._target_spec = effective_target
        self._sock, resolved_target = open_internet_udp_socket(
            bind_addr=bind_addr,
            target_addr=effective_target,
            nonblocking=True,
        )
        assert resolved_target is not None
        self.server_addr: Address = resolved_target
        self._lock = threading.Lock()
        self._init_net_common(relay_addr=relay_addr, room=room, loss_drop_sim=loss_drop_sim, rng_seed=9090)

        self.engine = NLTPEndpoint(
            send_packet_fn=self._raw_sendto,
            save_dir=save_dir,
            psk=psk,
            max_tx_kbps=max_tx_kbps,
            keepalive_s=keepalive_s,
            adaptive_rate=adaptive_rate,
            initial_burst_bytes=initial_burst_bytes,
        )
        self.engine.register_keepalive_peer(self.server_addr)
        self._running = True
        self._worker = threading.Thread(target=self._io_loop, daemon=True)
        self._worker.start()

    def getsockname(self) -> Address:
        return self._sock.getsockname()

    def set_peer_endpoint(self, new_server_addr: Tuple[str, int]) -> None:
        """Updates the target peer WAN address (e.g. after exchanging STUN coordinates)."""
        _af, sa = resolve_udp_address(new_server_addr[0], new_server_addr[1], prefer_family=self._sock.family)
        with self._lock:
            self._target_spec = new_server_addr
            self.server_addr = sa
            self.engine.register_keepalive_peer(sa)

    def rebind_socket(self, new_bind_addr: Optional[Tuple[str, int]] = None) -> Address:
        """
        Live Connection Migration (simulates or executes Wi-Fi -> 4G LTE interface switch):
        swaps the underlying OS UDP socket to a brand-new local port/interface mid-transfer
        without dropping active file uploads, encryption state, or telemetry streams!
        """
        new_sock, resolved_target = open_internet_udp_socket(
            bind_addr=new_bind_addr,
            target_addr=self._target_spec,
            nonblocking=True,
        )
        with self._lock:
            old_sock = self._sock
            self._sock = new_sock
            if resolved_target is not None:
                self.server_addr = resolved_target
            for xfer in self.engine.outbound.values():
                if not xfer.completed:
                    xfer.peer_addr = self.server_addr
            if self._relay_target is not None:
                self._relay_registered = False
                self._send_relay_register()
            # Announce new source IP:port to server immediately in 0-RTT!
            self.engine.send_ping(self.server_addr)
            new_local = self._sock.getsockname()
        try:
            old_sock.close()
        except OSError:
            pass
        return new_local

    def send_bytes(self, data: bytes, *, timeout: float = 20.0) -> Dict[str, Any]:
        """Sends arbitrary bytes reliably with end-to-end SHA-256 verification."""
        with self._lock:
            xfer = self.engine.start_transfer(self.server_addr, data, kind="bytes")
        ok = self._wait_transfer(xfer, timeout)
        return self._transfer_receipt(xfer, ok)

    def send_message(self, text: str, *, timeout: float = 15.0) -> Dict[str, Any]:
        return self.send_bytes(text.encode("utf-8"), timeout=timeout)

    def send_json(self, obj: Any, *, timeout: float = 15.0) -> Dict[str, Any]:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        with self._lock:
            xfer = self.engine.start_transfer(self.server_addr, raw, kind="json")
        ok = self._wait_transfer(xfer, timeout)
        return self._transfer_receipt(xfer, ok)

    def send_file(self, filepath: Union[str, Path], *, timeout: float = 60.0) -> Dict[str, Any]:
        """Transfers a file of any format (.bin, .png, .pdf, .zip, .pt) with 100% SHA-256 verification."""
        p = Path(filepath)
        raw = p.read_bytes()
        with self._lock:
            xfer = self.engine.start_transfer(self.server_addr, raw, kind="file", filename=p.name)
        ok = self._wait_transfer(xfer, timeout)
        return self._transfer_receipt(xfer, ok)

    def send_reflex(self, urgent_msg: Union[str, bytes]) -> int:
        """Urgent reflex message: bypasses the file queue (no head-of-line blocking) and is retransmitted
        until the receiver acknowledges it (or ~3 s pass). Returns the reflex sequence number."""
        b = urgent_msg.encode("utf-8") if isinstance(urgent_msg, str) else urgent_msg
        with self._lock:
            return self.engine.send_reflex(self.server_addr, b)


    def send_telemetry(self, values: Any, *, profile: str = "sensor_plateau") -> int:
        with self._lock:
            return self.engine.send_telemetry(self.server_addr, values, profile=profile)

    def recv(self, timeout: float = 5.0) -> Optional[ReceivedItem]:
        deadline = time.perf_counter() + timeout
        while True:  # checks at least once, so recv(timeout=0) is a non-blocking poll
            with self._lock:
                if self.engine.completed_inbox:
                    return self.engine.completed_inbox.pop(0)
            if time.perf_counter() >= deadline:
                return None
            time.sleep(0.002)

    def _wait_transfer(self, xfer: OutboundTransfer, timeout: float) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if xfer.completed:
                return xfer.verified_sha256_by_peer
            time.sleep(0.002)
        return False

    def _transfer_receipt(self, xfer: OutboundTransfer, ok: bool) -> Dict[str, Any]:
        elapsed = max(1e-6, (xfer.completed_at or time.perf_counter()) - xfer.started_at)
        return {
            "stream_id": xfer.stream_id,
            "filename": xfer.filename,
            "bytes": xfer.total_bytes,
            "sha256": xfer.sha256_hex,
            "verified_sha256": ok,
            "elapsed_s": round(elapsed, 4),
            "goodput_kbps": round((xfer.total_bytes * 8.0 / 1000.0) / elapsed, 2),
            "estimated_link_loss_pct": round(self.engine.peer_loss_ema * 100.0, 2),
            "encrypted": self.engine._aead is not None,
        }

    def close(self) -> None:
        self._linger_for_handshake()
        self._running = False
        self._worker.join(timeout=1.0)
        self._sock.close()

    def __enter__(self) -> "NLTPClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
