"""
Drop-In TCP/UDP-Style Socket API for NeckLink (`NeckSocket`, `NeckUDPServer`, `AsyncNeckSocket`).

Lets developers use NeckLink directly in Python programs just like standard `socket.socket`:
  - Runs over real OS non-blocking UDP (`AF_INET` / `AF_INET6`, `SOCK_DGRAM`) or any custom
    byte transport (`send_fn` / `recv_fn` for LoRa, UART, BLE, WebSockets, or Iridium SBD).
  - Zero pre-calibration required: auto-discovers channel count, optional `dict` key names,
    and per-channel `(mean, std)` scales via compact in-band `0x3E` sync beacons so even
    late-joining or rebooted receivers self-synchronize mid-stream with 0-RTT and 0 handshakes.
  - Supports arbitrary vector widths (`>127` channels automatically striped across lanes)
    and native `list`, `np.ndarray`, `dict[str, float]`, or `torch.Tensor` payloads.
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np

from .codec import NeckLinkConfig, NeckLinkDecoder, NeckLinkEncoder

try:
    import torch
except ImportError:  # pragma: no cover
    torch = None  # type: ignore[assignment]


SYNC_BEACON_MARKER = 0x3E  # Reserved in pkt[1] bits 0..5 (since n_ev is always 0..31)
CHUNK_FRAME_MAGIC = 0x3D   # Multi-lane striping frame for >127 channels

Address = Tuple[str, int]
PayloadType = Union[Sequence[float], np.ndarray, Dict[str, float], "torch.Tensor"]


def _profile_to_config(
    n_channels: int,
    profile: str = "auto",
    deadband_q8: Optional[int] = None,
) -> NeckLinkConfig:
    """Maps high-level engineering profile names to verified NeckLinkConfig presets."""
    if profile == "sensor_plateau":
        # Optimal for sustained gas plumes / buoy telemetry / industrial SCADA
        return NeckLinkConfig(
            n_channels=n_channels,
            deadband_q8=32 if deadband_q8 is None else deadband_q8,
            enable_mirror=True,
            enable_echo=True,
            enable_brake_gate=False,
        )
    if profile == "fast_spike":
        # Optimal for 100+ Hz biphasic physiological waveforms (ECG/EMG/vibration)
        return NeckLinkConfig(
            n_channels=n_channels,
            deadband_q8=32 if deadband_q8 is None else deadband_q8,
            enable_mirror=True,
            enable_echo=True,
            enable_brake_gate=True,
        )
    if profile == "split_ai":
        # Optimal for dense neural bottleneck embeddings
        return NeckLinkConfig(
            n_channels=n_channels,
            deadband_q8=24 if deadband_q8 is None else deadband_q8,
            enable_mirror=True,
            enable_echo=True,
            enable_brake_gate=True,
        )
    if profile == "ultra_low_bytes":
        return NeckLinkConfig(
            n_channels=n_channels,
            deadband_q8=36 if deadband_q8 is None else deadband_q8,
            enable_mirror=True,
            enable_echo=False,
            enable_brake_gate=False,
        )
    # Default "auto": balanced zero-drift mirror + cryptic echo + shunting gate
    return NeckLinkConfig(
        n_channels=n_channels,
        deadband_q8=32 if deadband_q8 is None else deadband_q8,
        enable_mirror=True,
        enable_echo=True,
        enable_brake_gate=True,
    )


def _pack_sync_beacon(
    epoch: int,
    mean: np.ndarray,
    std: np.ndarray,
    keys: Optional[List[str]] = None,
) -> bytes:
    """
    Packs a self-describing float32 scale & schema beacon:
      Byte 0: epoch (uint8)
      Byte 1: SYNC_BEACON_MARKER (0x3E)
      Byte 2..3: n_channels (uint16 LE)
      Byte 4..5: key_json_len (uint16 LE, 0 if positional array)
      Next 4*N bytes: mean as IEEE float32
      Next 4*N bytes: std as IEEE float32
      Optional key_json bytes (UTF-8 compact JSON array of field names)
    """
    n = len(mean)
    key_bytes = json.dumps(keys, separators=(",", ":")).encode("utf-8") if keys else b""
    hdr = struct.pack("<BBHH", epoch & 0xFF, SYNC_BEACON_MARKER, n, len(key_bytes))
    m32 = np.asarray(mean, dtype="<f4").tobytes()
    s32 = np.maximum(np.asarray(std, dtype="<f4"), np.float32(1e-5)).tobytes()
    return hdr + m32 + s32 + key_bytes


def _unpack_sync_beacon(pkt: bytes) -> Optional[Tuple[int, np.ndarray, np.ndarray, Optional[List[str]]]]:
    if len(pkt) < 6 or (pkt[1] & 0x3F) != SYNC_BEACON_MARKER:
        return None
    epoch, _marker, n, key_len = struct.unpack("<BBHH", pkt[:6])
    expected = 6 + 8 * n + key_len
    if len(pkt) < expected or n == 0:
        return None
    off_m = 6
    off_s = 6 + 4 * n
    off_k = 6 + 8 * n
    mean = np.frombuffer(pkt[off_m:off_s], dtype="<f4").astype(np.float64)
    std = np.maximum(np.frombuffer(pkt[off_s:off_k], dtype="<f4").astype(np.float64), 1e-5)
    keys: Optional[List[str]] = None
    if key_len > 0:
        keys = json.loads(pkt[off_k : off_k + key_len].decode("utf-8"))
    return epoch, mean, std, keys


class _LaneCodecPair:
    """Manages 1..N_lanes of NeckLink (each <= 64 channels) for arbitrary payload dimensions."""

    LANE_MAX = 64

    def __init__(
        self,
        total_channels: int,
        profile: str,
        mean: np.ndarray,
        std: np.ndarray,
        deadband_q8: Optional[int] = None,
    ) -> None:
        self.total_channels = total_channels
        self.profile = profile
        self.deadband_q8 = deadband_q8
        self.slices: List[slice] = []
        self.encoders: List[NeckLinkEncoder] = []
        self.decoders: List[NeckLinkDecoder] = []

        for start in range(0, total_channels, self.LANE_MAX):
            end = min(total_channels, start + self.LANE_MAX)
            sl = slice(start, end)
            cfg = _profile_to_config(end - start, profile=profile, deadband_q8=deadband_q8)
            self.slices.append(sl)
            self.encoders.append(NeckLinkEncoder(cfg, obs_mean=mean[sl], obs_std=std[sl]))
            self.decoders.append(NeckLinkDecoder(cfg, obs_mean=mean[sl], obs_std=std[sl]))

    def update_scales(self, mean: np.ndarray, std: np.ndarray) -> None:
        m32 = np.asarray(mean, dtype="<f4").astype(np.float64)
        s32 = np.maximum(np.asarray(std, dtype="<f4").astype(np.float64), 1e-5)
        for sl, enc, dec in zip(self.slices, self.encoders, self.decoders):
            new_mean_q8 = np.round(m32[sl] * 256.0).astype(np.int32)
            std_safe = np.maximum(s32[sl], 1e-6)
            new_inv_std_q8 = np.clip(np.round(256.0 / std_safe), 1, 32767).astype(np.int32)
            new_std_q8 = np.clip(np.round(std_safe * 256.0), 1, 32767).astype(np.int32)

            # Preserve physical state across scale changes: x_q8 = m_old * std_old / 256 + mean_old
            for codec_obj in (enc, dec):
                for i in range(len(codec_obj.m)):
                    phys_m_q8 = (int(codec_obj.m[i]) * int(codec_obj.std_q8[i])) // 256 + int(codec_obj.mean_q8[i])
                    phys_b_q8 = (int(codec_obj.b[i]) * int(codec_obj.std_q8[i])) // 256 + int(codec_obj.mean_q8[i])
                    codec_obj.m[i] = np.clip(((phys_m_q8 - int(new_mean_q8[i])) * int(new_inv_std_q8[i])) // 256, -16384, 16383)
                    codec_obj.b[i] = np.clip(((phys_b_q8 - int(new_mean_q8[i])) * int(new_inv_std_q8[i])) // 256, -16384, 16383)
                    codec_obj.v[i] = np.clip((int(codec_obj.v[i]) * int(codec_obj.std_q8[i])) // max(1, int(new_std_q8[i])), -16384, 16383)

            enc.mean_q8 = new_mean_q8
            enc.inv_std_q8 = new_inv_std_q8
            enc.std_q8 = new_std_q8
            dec.mean_q8 = new_mean_q8
            dec.std_q8 = new_std_q8

    def encode_vector(self, x: np.ndarray) -> Optional[bytes]:
        if len(self.encoders) == 1:
            return self.encoders[0].encode_float(x)

        active_chunks: List[Tuple[int, bytes]] = []
        for lane_idx, (sl, enc) in enumerate(zip(self.slices, self.encoders)):
            sub_pkt = enc.encode_float(x[sl])
            if sub_pkt is not None:
                active_chunks.append((lane_idx, sub_pkt))
        if not active_chunks:
            return None

        out = bytearray([len(active_chunks) & 0xFF, CHUNK_FRAME_MAGIC])
        for lane_idx, sub_pkt in active_chunks:
            out.append(lane_idx & 0xFF)
            out.append(len(sub_pkt) & 0xFF)
            out.extend(sub_pkt)
        return bytes(out)

    def decode_packet_or_none(self, pkt: Optional[bytes]) -> np.ndarray:
        out = np.empty(self.total_channels, dtype=np.float64)
        if len(self.decoders) == 1:
            out[:] = self.decoders[0].decode_float(pkt)
            return out

        lane_pkts: Dict[int, bytes] = {}
        if pkt is not None and len(pkt) >= 2 and (pkt[1] & 0x3F) == CHUNK_FRAME_MAGIC:
            n_active = int(pkt[0])
            pos = 2
            for _ in range(n_active):
                if pos + 2 > len(pkt):
                    break
                l_idx = int(pkt[pos])
                l_len = int(pkt[pos + 1])
                pos += 2
                if pos + l_len <= len(pkt):
                    lane_pkts[l_idx] = pkt[pos : pos + l_len]
                pos += l_len

        for lane_idx, (sl, dec) in enumerate(zip(self.slices, self.decoders)):
            out[sl] = dec.decode_float(lane_pkts.get(lane_idx, None))
        return out

    @property
    def detected_lost_packets(self) -> int:
        return sum(d.detected_lost_packets for d in self.decoders)

    @property
    def recovered_echo_packets(self) -> int:
        return sum(d.recovered_echo_packets for d in self.decoders)


class NeckSocket:
    """
    Drop-in UDP/custom-transport socket with built-in NeckLink compression & 0-RTT self-healing.

    Example (Real OS UDP):
        # Receiver (Server / Cloud / Shore Station)
        rx = NeckSocket(profile="sensor_plateau")
        rx.bind(("127.0.0.1", 9100))

        # Sender (Buoy / AQMS / Drone / Edge AI)
        tx = NeckSocket(profile="sensor_plateau")
        tx.connect(("127.0.0.1", 9100))

        tx.send({"rpm": 1205.0, "h2s_ppm": 0.04, "wave_m": 1.12})
        state = rx.recv_tick()  # -> {'rpm': 1205.0, 'h2s_ppm': 0.04, 'wave_m': 1.12}
    """

    def __init__(
        self,
        channels: Optional[int] = None,
        *,
        profile: str = "auto",
        deadband_q8: Optional[int] = None,
        obs_mean: Optional[Sequence[float]] = None,
        obs_std: Optional[Sequence[float]] = None,
        beacon_interval_pkts: int = 64,
        send_fn: Optional[Callable[[bytes], Any]] = None,
        recv_fn: Optional[Callable[[], Optional[bytes]]] = None,
        family: int = socket.AF_INET,
    ) -> None:
        self.channels = channels
        self.profile = profile
        self.deadband_q8 = deadband_q8
        self.beacon_interval_pkts = max(4, int(beacon_interval_pkts))
        self._send_fn = send_fn
        self._recv_fn = recv_fn

        self._sock: Optional[socket.socket] = None
        if send_fn is None and recv_fn is None:
            self._sock = socket.socket(family, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
            # Enlarge OS socket buffers for bursty high-rate telemetry
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
            except OSError:
                pass

        self._peer_addr: Optional[Address] = None
        self._keys: Optional[List[str]] = None
        self._scale_epoch: int = 1
        self._mean: Optional[np.ndarray] = (
            np.asarray(obs_mean, dtype=np.float64) if obs_mean is not None else None
        )
        self._std: Optional[np.ndarray] = (
            np.maximum(np.asarray(obs_std, dtype=np.float64), 1e-4) if obs_std is not None else None
        )
        self._fixed_calibration: bool = (obs_mean is not None and obs_std is not None)
        self._lanes: Optional[_LaneCodecPair] = None

        if channels is not None and self._mean is not None and self._std is not None:
            self._init_lanes(channels, self._mean, self._std)

        # Live runtime telemetry counters
        self.tx_ticks: int = 0
        self.tx_packets: int = 0
        self.silent_ticks: int = 0
        self.wire_bytes_sent: int = 0
        self.raw_equiv_bytes: int = 0
        self.rx_packets: int = 0
        self.wire_bytes_rcvd: int = 0
        self._pkts_since_beacon: int = 999999
        self._beacon_burst_left: int = 0
        self._warmup_buf: List[np.ndarray] = []
        self._last_rx_state: Optional[np.ndarray] = None

    @classmethod
    def udp_sender(cls, host: str, port: int, **kwargs: Any) -> "NeckSocket":
        s = cls(**kwargs)
        s.connect((host, port))
        return s

    @classmethod
    def udp_receiver(cls, host: str = "0.0.0.0", port: int = 0, **kwargs: Any) -> "NeckSocket":
        s = cls(**kwargs)
        s.bind((host, port))
        return s

    def bind(self, addr: Address) -> None:
        if self._sock is None:
            raise RuntimeError("Cannot call bind() when using custom send_fn/recv_fn callbacks")
        self._sock.bind(addr)

    def connect(self, addr: Address) -> None:
        self._peer_addr = addr
        if self._sock is not None:
            self._sock.connect(addr)

    def getsockname(self) -> Address:
        if self._sock is None:
            return ("0.0.0.0", 0)
        return self._sock.getsockname()

    def fileno(self) -> int:
        if self._sock is None:
            return -1
        return self._sock.fileno()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "NeckSocket":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _init_lanes(self, n: int, mean: np.ndarray, std: np.ndarray) -> None:
        m32 = np.asarray(mean, dtype="<f4").astype(np.float64)
        s32 = np.maximum(np.asarray(std, dtype="<f4").astype(np.float64), 1e-5)
        self._mean = m32
        self._std = s32
        self.channels = n
        self._lanes = _LaneCodecPair(
            total_channels=n,
            profile=self.profile,
            mean=m32,
            std=s32,
            deadband_q8=self.deadband_q8,
        )

    def _coerce_input(self, payload: PayloadType) -> np.ndarray:
        if isinstance(payload, dict):
            if self._keys is None:
                self._keys = list(payload.keys())
            vec = np.array([float(payload[k]) for k in self._keys], dtype=np.float64)
            return vec
        if torch is not None and isinstance(payload, torch.Tensor):
            return payload.detach().cpu().numpy().astype(np.float64).reshape(-1)
        return np.asarray(payload, dtype=np.float64).reshape(-1)

    def _emit_wire(self, data: bytes, addr: Optional[Address] = None) -> None:
        if self._send_fn is not None:
            self._send_fn(data)
        elif self._sock is not None:
            if addr is not None and self._peer_addr is None:
                self._sock.sendto(data, addr)
            else:
                self._sock.send(data)
        self.wire_bytes_sent += len(data)
        self.tx_packets += 1

    def send(self, payload: PayloadType, addr: Optional[Address] = None) -> int:
        """
        Encodes and sends `payload` (`list`, `np.ndarray`, `dict`, or `torch.Tensor`).
        Returns actual wire bytes transmitted on this tick (`0` if steady within deadband!).
        """
        x = self._coerce_input(payload)
        n = len(x)
        self.tx_ticks += 1
        self.raw_equiv_bytes += (2 + 4 * n)
        sent_this_tick = 0

        need_beacon = False
        if not self._fixed_calibration and len(self._warmup_buf) < 24:
            self._warmup_buf.append(x.copy())

        if self._lanes is None:
            # Cold-start zero-config initialization from first sample!
            m0 = x.copy()
            s0 = np.maximum(np.abs(x) * 0.05, 0.25)
            if self._mean is not None and len(self._mean) == n:
                m0 = self._mean
            if self._std is not None and len(self._std) == n:
                s0 = self._std
            self._init_lanes(n, m0, s0)
            self._beacon_burst_left = 1
            need_beacon = True
        elif not self._fixed_calibration and self._mean is not None and self._std is not None:
            if len(self._warmup_buf) == 24 and self.tx_ticks == 24:
                # Noise-floor-aware auto-scale: deadband (0.125*std) sits >= 3.5x above 1-step thermal noise!
                w_arr = np.stack(self._warmup_buf, axis=0)
                w_mean = np.mean(w_arr, axis=0)
                noise_rms = np.std(np.diff(w_arr, axis=0), axis=0) / np.sqrt(2.0)
                w_std = np.maximum.reduce([
                    np.std(w_arr, axis=0) * 4.5,
                    noise_rms * 30.0,
                    np.maximum(np.abs(w_mean) * 0.015, 0.05),
                ])
                self._scale_epoch = (self._scale_epoch + 1) & 0xFF or 1
                self._lanes.update_scales(w_mean, w_std)
                self._mean = np.asarray(w_mean, dtype="<f4").astype(np.float64)
                self._std = np.maximum(np.asarray(w_std, dtype="<f4").astype(np.float64), 1e-5)
                self._beacon_burst_left = 2
                need_beacon = True
            else:
                # Expand scale only if channel exceeds coarse ceiling (+/- 14.5 std)
                z_abs = np.abs(x - self._mean) / np.maximum(self._std, 1e-6)
                if np.any(z_abs > 14.5):
                    over = z_abs > 14.5
                    new_std = self._std.copy()
                    new_std[over] = np.maximum(new_std[over] * 4.0, np.abs(x[over] - self._mean[over]) / 3.5)
                    self._scale_epoch = (self._scale_epoch + 1) & 0xFF or 1
                    self._lanes.update_scales(self._mean, new_std)
                    self._std = np.maximum(np.asarray(new_std, dtype="<f4").astype(np.float64), 1e-5)
                    self._beacon_burst_left = 2
                    need_beacon = True

        assert self._lanes is not None and self._mean is not None and self._std is not None

        if need_beacon or self._beacon_burst_left > 0 or self._pkts_since_beacon >= self.beacon_interval_pkts:
            beacon = _pack_sync_beacon(self._scale_epoch, self._mean, self._std, self._keys)
            self._emit_wire(beacon, addr=addr)
            sent_this_tick += len(beacon)
            self._pkts_since_beacon = 0
            if self._beacon_burst_left > 0:
                self._beacon_burst_left -= 1

        pkt = self._lanes.encode_vector(x)
        if pkt is None:
            self.silent_ticks += 1
            return sent_this_tick

        self._emit_wire(pkt, addr=addr)
        sent_this_tick += len(pkt)
        self._pkts_since_beacon += 1
        return sent_this_tick

    def sendto(self, payload: PayloadType, addr: Address) -> int:
        return self.send(payload, addr=addr)

    def _poll_raw_datagram(self) -> Optional[bytes]:
        if self._recv_fn is not None:
            return self._recv_fn()
        if self._sock is None:
            return None
        try:
            data, _addr = self._sock.recvfrom(65535)
            return data
        except (BlockingIOError, InterruptedError):
            return None
        except ConnectionResetError:
            # Windows UDP ICMP port-unreachable quirk on localhost; safe to ignore
            return None

    def _ingest_beacon(self, beacon_pkt: bytes) -> bool:
        parsed = _unpack_sync_beacon(beacon_pkt)
        if parsed is None:
            return False
        epoch, mean, std, keys = parsed
        if keys is not None:
            self._keys = keys
        if self._lanes is None or self.channels != len(mean):
            self._scale_epoch = epoch
            self._init_lanes(len(mean), mean, std)
        elif epoch != self._scale_epoch:
            self._scale_epoch = epoch
            self._lanes.update_scales(mean, std)
            self._mean = mean
            self._std = std
        return True

    def recv_tick(
        self,
        *,
        as_dict: Optional[bool] = None,
        as_tensor: bool = False,
        device: str = "cpu",
    ) -> Union[np.ndarray, Dict[str, float], "torch.Tensor", None]:
        """
        Non-blocking 0-ms receiver tick (never stalls on packet loss!).
        Drains any arrived UDP packets from the OS socket buffer, advances the
        Tonic-Phasic Efference Mirror + Cryptic Echo decoder, and returns the
        reconstructed state (`dict` if sender sent `dict`, `torch.Tensor` if `as_tensor=True`,
        else `np.ndarray`; returns `None` only before the very first packet arrives).
        """
        data_pkts: List[bytes] = []
        while True:
            raw = self._poll_raw_datagram()
            if raw is None:
                break
            self.rx_packets += 1
            self.wire_bytes_rcvd += len(raw)
            if len(raw) >= 6 and (raw[1] & 0x3F) == SYNC_BEACON_MARKER:
                self._ingest_beacon(raw)
            else:
                data_pkts.append(raw)

        if self._lanes is None:
            return None

        if not data_pkts:
            self._last_rx_state = self._lanes.decode_packet_or_none(None)
        else:
            for dp in data_pkts:
                self._last_rx_state = self._lanes.decode_packet_or_none(dp)

        assert self._last_rx_state is not None
        return self._format_output(self._last_rx_state, as_dict=as_dict, as_tensor=as_tensor, device=device)

    def step_with_packets(
        self,
        raw_pkts: Sequence[bytes],
        *,
        as_dict: Optional[bool] = None,
        as_tensor: bool = False,
        device: str = "cpu",
    ) -> Union[np.ndarray, Dict[str, float], "torch.Tensor", None]:
        """
        Tick-exact embedded step for multiplexed transports (`NLTP`, `QUIC RFC 9221`, `NeckUDPServer`).
        Ingests any arrived sync beacons and steps the Tonic-Phasic mirror once (or per arrived data packet).
        """
        data_pkts: List[bytes] = []
        for raw in raw_pkts:
            self.rx_packets += 1
            self.wire_bytes_rcvd += len(raw)
            if len(raw) >= 6 and (raw[1] & 0x3F) == SYNC_BEACON_MARKER:
                self._ingest_beacon(raw)
            else:
                data_pkts.append(raw)

        if self._lanes is None:
            return None

        if not data_pkts:
            self._last_rx_state = self._lanes.decode_packet_or_none(None)
        else:
            for dp in data_pkts:
                self._last_rx_state = self._lanes.decode_packet_or_none(dp)

        assert self._last_rx_state is not None
        return self._format_output(self._last_rx_state, as_dict=as_dict, as_tensor=as_tensor, device=device)

    def feed_raw_packet(self, raw_pkt: Optional[bytes]) -> Optional[Union[np.ndarray, Dict[str, float]]]:
        """Direct in-memory packet feed (useful for unit tests, custom framing, or WebSockets)."""
        if raw_pkt is None:
            return self.step_with_packets([])  # type: ignore[return-value]
        if len(raw_pkt) >= 6 and (raw_pkt[1] & 0x3F) == SYNC_BEACON_MARKER:
            self.rx_packets += 1
            self.wire_bytes_rcvd += len(raw_pkt)
            self._ingest_beacon(raw_pkt)
            return None if self._last_rx_state is None else self._format_output(self._last_rx_state)
        return self.step_with_packets([raw_pkt])  # type: ignore[return-value]

    def _format_output(
        self,
        vec: np.ndarray,
        as_dict: Optional[bool] = None,
        as_tensor: bool = False,
        device: str = "cpu",
    ) -> Union[np.ndarray, Dict[str, float], "torch.Tensor"]:
        if as_tensor:
            if torch is None:
                raise RuntimeError("PyTorch is not installed")
            return torch.from_numpy(vec.astype(np.float32)).to(device)
        want_dict = (self._keys is not None) if as_dict is None else as_dict
        if want_dict and self._keys is not None:
            return {k: float(vec[i]) for i, k in enumerate(self._keys)}
        return vec.copy()

    def stats(self) -> Dict[str, float]:
        """Returns live compression, silence, packet-loss, and echo-recovery metrics."""
        det_lost = self._lanes.detected_lost_packets if self._lanes is not None else 0
        rec_echo = self._lanes.recovered_echo_packets if self._lanes is not None else 0
        est_total_expected = max(1, self.rx_packets + det_lost)
        return {
            "tx_ticks": float(self.tx_ticks),
            "tx_packets": float(self.tx_packets),
            "silent_tick_pct": round(100.0 * self.silent_ticks / max(1, self.tx_ticks), 2),
            "wire_bytes_sent": float(self.wire_bytes_sent),
            "raw_equiv_bytes": float(self.raw_equiv_bytes),
            "compression_x": round(self.raw_equiv_bytes / max(1, self.wire_bytes_sent), 2)
            if self.wire_bytes_sent > 0
            else 0.0,
            "rx_packets": float(self.rx_packets),
            "wire_bytes_rcvd": float(self.wire_bytes_rcvd),
            "detected_lost_packets": float(det_lost),
            "recovered_echo_packets": float(rec_echo),
            "estimated_rx_loss_pct": round(100.0 * det_lost / est_total_expected, 2),
        }


class NeckUDPServer:
    """
    Multi-Client Fleet UDP Hub: listens on a single UDP port and automatically maintains
    an independent self-healing `NeckSocket` mirror per remote `(ip, port)` sensor node.
    """

    def __init__(self, bind_addr: Address = ("0.0.0.0", 9100), *, profile: str = "auto") -> None:
        self.profile = profile
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
        except OSError:
            pass
        self._sock.bind(bind_addr)
        self.peers: Dict[Address, NeckSocket] = {}

    def getsockname(self) -> Address:
        return self._sock.getsockname()

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "NeckUDPServer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def poll_tick(self) -> Dict[Address, Union[np.ndarray, Dict[str, float]]]:
        """Drains all arrived datagrams across all fleet nodes and steps every node's mirror."""
        arrived_by_peer: Dict[Address, List[bytes]] = {}
        while True:
            try:
                data, addr = self._sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError, ConnectionResetError):
                break
            arrived_by_peer.setdefault(addr, []).append(data)
            if addr not in self.peers:
                # Peer uses in-memory feed (`send_fn` dummy so no extra OS socket is opened per node!)
                self.peers[addr] = NeckSocket(profile=self.profile, send_fn=lambda _b: None)

        snapshot: Dict[Address, Union[np.ndarray, Dict[str, float]]] = {}
        for addr, peer_sock in self.peers.items():
            pkts = arrived_by_peer.get(addr, [])
            out = peer_sock.step_with_packets(pkts)
            if out is not None:
                snapshot[addr] = out  # type: ignore[assignment]
        return snapshot


class _NeckDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, parent: "AsyncNeckSocket") -> None:
        self.parent = parent

    def datagram_received(self, data: bytes, addr: Address) -> None:
        self.parent._on_datagram(data, addr)


class AsyncNeckSocket:
    """Native `asyncio` UDP wrapper around `NeckSocket` for FastAPI / async telemetry loops."""

    def __init__(self, *, profile: str = "auto", **kwargs: Any) -> None:
        self._queue: List[bytes] = []
        self._transport: Optional[asyncio.DatagramTransport] = None
        self.core = NeckSocket(
            profile=profile,
            send_fn=self._transport_send,
            recv_fn=self._queue_pop,
            **kwargs,
        )

    def _transport_send(self, data: bytes) -> None:
        if self._transport is not None:
            self._transport.sendto(data)

    def _queue_pop(self) -> Optional[bytes]:
        return self._queue.pop(0) if self._queue else None

    def _on_datagram(self, data: bytes, _addr: Address) -> None:
        self._queue.append(data)

    @classmethod
    async def create(
        cls,
        *,
        local_addr: Optional[Address] = None,
        remote_addr: Optional[Address] = None,
        profile: str = "auto",
        **kwargs: Any,
    ) -> "AsyncNeckSocket":
        loop = asyncio.get_running_loop()
        inst = cls(profile=profile, **kwargs)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _NeckDatagramProtocol(inst),
            local_addr=local_addr,
            remote_addr=remote_addr,
        )
        inst._transport = transport  # type: ignore[assignment]
        return inst

    def send(self, payload: PayloadType) -> int:
        return self.core.send(payload)

    def recv_tick(self, **kwargs: Any) -> Any:
        return self.core.recv_tick(**kwargs)

    def stats(self) -> Dict[str, float]:
        return self.core.stats()

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
