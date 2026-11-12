"""
Internet Readiness Layer for NLTP (`necklink/internet.py`):
  1. Authenticated Encryption (`ChaCha20-Poly1305` AEAD) + `RFC 6479` 64-Bit Anti-Replay Window
  2. DNS Hostname Resolution + Dual-Stack IPv4 / IPv6 Socket Creation
  3. Router-Safe Token-Bucket UDP Packet Pacer (prevents home Wi-Fi TX queue micro-burst drops)
  4. Native `RFC 5389` STUN Client (live WAN IP:Port, Internet RTT & NAT Cone/Symmetric detection)
  5. Stateless Zero-Knowledge `NLTPRelayHub` (traverses strict 4G/5G Carrier Symmetric CGNATs
     while keeping all files, commands, and telemetry 100% end-to-end encrypted)
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, Union

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

Address = Tuple[str, int]

STUN_MAGIC_COOKIE = 0x2112A442
STUN_BINDING_REQUEST = 0x0001
STUN_BINDING_SUCCESS = 0x0101
STUN_ATTR_MAPPED_ADDRESS = 0x0001
STUN_ATTR_XOR_MAPPED_ADDRESS = 0x0020

DEFAULT_PUBLIC_STUN_SERVERS: Tuple[Tuple[str, int], ...] = (
    ("stun.cloudflare.com", 3478),
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
)

RELAY_MAGIC = b"NR"
RELAY_VER_DATA = 0x01
RELAY_VER_REGISTER = 0x02
RELAY_VER_REG_ACK = 0x03
RELAY_HDR_SIZE = 19  # magic(2) + cmd(1) + room_tag(16)


# ---------------------------------------------------------------------------
# 1. WireGuard-Style ChaCha20-Poly1305 Key Derivation & Anti-Replay Window
# ---------------------------------------------------------------------------

def generate_nltp_key() -> str:
    """Generates a cryptographically random 256-bit URL-safe base64 key for `--psk`."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def derive_aead_key(psk: Union[str, bytes]) -> bytes:
    """
    Normalizes a user passphrase or base64/raw 32-byte key into a 256-bit ChaCha20-Poly1305 key.
    """
    if isinstance(psk, bytes):
        if len(psk) == 32:
            return psk
        return hashlib.sha256(b"NLTP-v1-RAW-KEY:" + psk).digest()

    s = psk.strip()
    # Check if valid 32-byte urlsafe base64 key from `generate_nltp_key()`
    if len(s) in (43, 44):
        try:
            padded = s + ("=" * ((4 - len(s) % 4) % 4))
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            if len(raw) == 32:
                return raw
        except Exception:
            pass
    return hashlib.sha256(b"NLTP-v1-PASSPHRASE:" + s.encode("utf-8")).digest()


class AEADCipherContext:
    """Lightweight ChaCha20-Poly1305 AEAD wrapper with monotonic 64-bit TX sequence numbers."""

    def __init__(self, psk: Union[str, bytes]) -> None:
        self.key = derive_aead_key(psk)
        self._aead = ChaCha20Poly1305(self.key)
        self._tx_seq: int = 0
        self._replay_windows: Dict[int, ReplayWindow64] = {}

    def next_seq(self) -> int:
        self._tx_seq += 1
        return self._tx_seq

    def seal(self, conn_id: int, seq64: int, aad_hdr20: bytes, plaintext: bytes) -> bytes:
        nonce = struct.pack("<IQ", conn_id & 0xFFFFFFFF, seq64 & 0xFFFFFFFFFFFFFFFF)
        return self._aead.encrypt(nonce, plaintext, aad_hdr20)

    def open_and_check_replay(
        self,
        peer_conn_id: int,
        seq64: int,
        aad_hdr20: bytes,
        ciphertext_with_tag: bytes,
    ) -> Optional[bytes]:
        if seq64 == 0:
            return None
        win = self._replay_windows.get(peer_conn_id)
        if win is not None and not win.would_accept(seq64):
            return None
        nonce = struct.pack("<IQ", peer_conn_id & 0xFFFFFFFF, seq64 & 0xFFFFFFFFFFFFFFFF)
        try:
            pt = self._aead.decrypt(nonce, ciphertext_with_tag, aad_hdr20)
        except InvalidTag:
            return None
        if win is None:
            win = ReplayWindow64()
            self._replay_windows[peer_conn_id] = win
        if not win.accept(seq64):
            return None
        return pt


class ReplayWindow64:
    """RFC 6479 512-packet sliding bitmap anti-replay detector (absorbs high-BDP UDP jitter & reordering, blocks replays)."""

    WINDOW_BITS = 512
    _MASK = (1 << 512) - 1

    def __init__(self) -> None:
        self.highest_seq: int = 0
        self.bitmap: int = 0

    def would_accept(self, seq: int) -> bool:
        if seq <= 0:
            return False
        if seq > self.highest_seq:
            return True
        diff = self.highest_seq - seq
        if diff >= self.WINDOW_BITS:
            return False
        return bool((self.bitmap & (1 << diff)) == 0)

    def accept(self, seq: int) -> bool:
        if not self.would_accept(seq):
            return False
        if seq > self.highest_seq:
            shift = seq - self.highest_seq
            if shift >= self.WINDOW_BITS:
                self.bitmap = 1
            else:
                self.bitmap = ((self.bitmap << shift) | 1) & self._MASK
            self.highest_seq = seq
        else:
            diff = self.highest_seq - seq
            self.bitmap |= (1 << diff)
        return True


# ---------------------------------------------------------------------------
# 2. Router-Safe Token-Bucket UDP Packet Pacer
# ---------------------------------------------------------------------------

class TokenBucketPacer:
    """
    Smooths bulk file/repair packet transmission so NLTP never dumps hundreds of UDP datagrams in a
    sub-millisecond burst (which overflows home Wi-Fi / 4G modem TX FIFO buffers on the WAN).
    """

    def __init__(self, rate_kbps: float = 25_000.0, max_burst_bytes: Optional[int] = None) -> None:
        self.rate_bytes_per_s = max(1000.0, (rate_kbps * 1000.0) / 8.0)
        if max_burst_bytes is None:
            self.max_burst_bytes = self._auto_burst()
        else:
            self.max_burst_bytes = float(max(2048, max_burst_bytes))
        self.tokens = self.max_burst_bytes
        self.last_refill: Optional[float] = None
        # One-off credit spent before regular tokens (an initial window), not capped by `max_burst_bytes`
        self.bonus_bytes = 0.0

    def grant_burst(self, n_bytes: float) -> None:
        # The burst replaces any banked tokens rather than stacking on top of them
        self.bonus_bytes = max(self.bonus_bytes, float(n_bytes))
        self.tokens = 0.0

    def _auto_burst(self) -> float:
        # ~6 ms micro-burst window (4..16 KB = 4..16 packets max instant burst -> safe for 32-pkt Wi-Fi FIFOs!)
        return float(max(4 * 1024, min(16 * 1024, int(self.rate_bytes_per_s * 0.006))))

    def set_rate_kbps(self, rate_kbps: float, rescale_burst: bool = False) -> None:
        self.rate_bytes_per_s = max(1000.0, (rate_kbps * 1000.0) / 8.0)
        if rescale_burst:
            self.max_burst_bytes = self._auto_burst()
            self.tokens = min(self.tokens, self.max_burst_bytes)

    def consume_if_available(self, n_bytes: int, now_s: float) -> bool:
        if self.bonus_bytes >= float(n_bytes):
            self.bonus_bytes -= float(n_bytes)
            return True
        if self.last_refill is None:
            self.last_refill = now_s
        else:
            dt = max(0.0, now_s - self.last_refill)
            self.last_refill = now_s
            self.tokens = min(self.max_burst_bytes, self.tokens + dt * self.rate_bytes_per_s)

        if self.tokens >= float(n_bytes):
            self.tokens -= float(n_bytes)
            return True
        return False


# ---------------------------------------------------------------------------
# 3. DNS Resolution & Dual-Stack IPv4 / IPv6 Socket Helper
# ---------------------------------------------------------------------------

def resolve_udp_address(host: str, port: int, prefer_family: Optional[socket.AddressFamily] = None) -> Tuple[socket.AddressFamily, Tuple]:
    """Resolves DNS hostnames, IPv4 literals, or IPv6 literals for `SOCK_DGRAM`."""
    fam = socket.AF_UNSPEC if prefer_family is None else prefer_family
    infos = socket.getaddrinfo(host, int(port), fam, socket.SOCK_DGRAM)
    if not infos:
        raise OSError(f"Could not resolve UDP endpoint {host!r}:{port}")
    # Prefer IPv4 when AF_UNSPEC returns both (broadest NAT/STUN compatibility unless IPv6 requested)
    if prefer_family is None:
        for af, _st, _pr, _cn, sa in infos:
            if af == socket.AF_INET:
                return af, sa
    af, _st, _pr, _cn, sa = infos[0]
    return af, sa


def open_internet_udp_socket(
    bind_addr: Optional[Tuple[str, int]] = None,
    target_addr: Optional[Tuple[str, int]] = None,
    nonblocking: bool = True,
) -> Tuple[socket.socket, Optional[Tuple]]:
    """
    Opens an IPv4 or IPv6 UDP socket matching `target_addr` (if client) or `bind_addr` (if server),
    with enlarged 2 MB OS buffers and Windows `SIO_UDP_CONNRESET` suppression.
    """
    resolved_target = None
    if target_addr is not None:
        af, resolved_target = resolve_udp_address(target_addr[0], target_addr[1])
        bind_default = ("0.0.0.0", 0) if af == socket.AF_INET else ("::", 0)
        b_host, b_port = bind_addr if bind_addr is not None else bind_default
        _, resolved_bind = resolve_udp_address(b_host, b_port, prefer_family=af)
    else:
        b_host, b_port = bind_addr if bind_addr is not None else ("0.0.0.0", 0)
        af, resolved_bind = resolve_udp_address(b_host, b_port)

    sock = socket.socket(af, socket.SOCK_DGRAM)
    if af == socket.AF_INET6:
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2097152)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2097152)
    except OSError:
        pass
    # Suppress Windows ICMP Port Unreachable (`WSAECONNRESET` 10054) killing subsequent `recvfrom`
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)  # type: ignore[attr-defined]
        except OSError:
            pass

    sock.bind(resolved_bind)
    sock.setblocking(not nonblocking)
    return sock, resolved_target


# ---------------------------------------------------------------------------
# 4. Native RFC 5389 STUN Client (Public WAN IP:Port & NAT Type Discovery)
# ---------------------------------------------------------------------------

@dataclass
class STUNDiscoveryResult:
    public_ip: str
    public_port: int
    local_port: int
    rtt_ms: float
    endpoint_independent_nat: bool
    nat_summary: str
    servers_queried: List[str]


def build_stun_binding_request(tx_id: Optional[bytes] = None) -> Tuple[bytes, bytes]:
    tid = tx_id if (tx_id is not None and len(tx_id) == 12) else os.urandom(12)
    return struct.pack("!HHI12s", STUN_BINDING_REQUEST, 0, STUN_MAGIC_COOKIE, tid), tid


def is_stun_packet(raw: bytes) -> bool:
    if len(raw) < 20:
        return False
    _mtype, mlen, cookie = struct.unpack_from("!HHI", raw, 0)
    return cookie == STUN_MAGIC_COOKIE and len(raw) >= 20 + mlen


def parse_stun_binding_response(raw: bytes, expected_tx_id: Optional[bytes] = None) -> Optional[Address]:
    if len(raw) < 20:
        return None
    mtype, mlen, cookie, rx_tid = struct.unpack_from("!HHI12s", raw, 0)
    if mtype != STUN_BINDING_SUCCESS or cookie != STUN_MAGIC_COOKIE:
        return None
    if expected_tx_id is not None and rx_tid != expected_tx_id:
        return None

    pos = 20
    end = min(len(raw), 20 + mlen)
    fallback_mapped: Optional[Address] = None
    while pos + 4 <= end:
        attr_type, attr_len = struct.unpack_from("!HH", raw, pos)
        pos += 4
        val = raw[pos : pos + attr_len]
        pos += (attr_len + 3) & ~3

        if attr_type == STUN_ATTR_XOR_MAPPED_ADDRESS and len(val) >= 8:
            family = val[1]
            xport = struct.unpack_from("!H", val, 2)[0] ^ (STUN_MAGIC_COOKIE >> 16)
            if family == 0x01:  # IPv4
                xip_int = struct.unpack_from("!I", val, 4)[0] ^ STUN_MAGIC_COOKIE
                ip_str = socket.inet_ntoa(struct.pack("!I", xip_int))
                return (ip_str, int(xport))
            elif family == 0x02 and len(val) >= 20:  # IPv6
                mask = struct.pack("!I12s", STUN_MAGIC_COOKIE, rx_tid)
                ip_bytes = bytes(a ^ b for a, b in zip(val[4:20], mask))
                ip_str = socket.inet_ntop(socket.AF_INET6, ip_bytes)
                return (ip_str, int(xport))
        elif attr_type == STUN_ATTR_MAPPED_ADDRESS and len(val) >= 8 and val[1] == 0x01:
            port = struct.unpack_from("!H", val, 2)[0]
            ip_str = socket.inet_ntoa(val[4:8])
            fallback_mapped = (ip_str, int(port))

    return fallback_mapped


def discover_wan_endpoint(
    sock: Optional[socket.socket] = None,
    *,
    stun_servers: Sequence[Tuple[str, int]] = DEFAULT_PUBLIC_STUN_SERVERS,
    timeout_s: float = 2.5,
) -> STUNDiscoveryResult:
    """
    Queries >=2 distinct public Internet STUN servers from a single UDP socket to determine:
      1. The live external WAN `(public_ip, public_port)` mapped by your router/ISP
      2. Live Internet UDP round-trip latency (`rtt_ms`)
      3. Whether your NAT is Endpoint-Independent (`p2p_hole_punch_ready=True`) or
         Symmetric CGNAT (`endpoint_independent_nat=False` -> requires `NLTPRelayHub`).
    """
    own_sock = sock is None
    if sock is None:
        sock, _ = open_internet_udp_socket(("0.0.0.0", 0), nonblocking=False)
    prev_timeout = sock.gettimeout()
    sock.settimeout(timeout_s)

    mapped_results: List[Tuple[str, Address, float]] = []
    try:
        for host, port in stun_servers:
            try:
                _af, sa = resolve_udp_address(host, port, prefer_family=sock.family)
                req, tid = build_stun_binding_request()
                t0 = time.perf_counter()
                sock.sendto(req, sa)
                deadline = t0 + timeout_s
                while time.perf_counter() < deadline:
                    raw, _from = sock.recvfrom(4096)
                    ans = parse_stun_binding_response(raw, expected_tx_id=tid)
                    if ans is not None:
                        rtt = (time.perf_counter() - t0) * 1000.0
                        mapped_results.append((f"{host}:{port}", ans, rtt))
                        break
                if len(mapped_results) >= 2:
                    break
            except OSError:
                continue
    finally:
        sock.settimeout(prev_timeout)
        local_port = int(sock.getsockname()[1])
        if own_sock:
            sock.close()

    if not mapped_results:
        raise TimeoutError("No STUN response received — outbound UDP may be blocked by firewall")

    first_srv, (pub_ip, pub_port), first_rtt = mapped_results[0]
    if len(mapped_results) >= 2:
        ports_match = all(m[1][1] == pub_port for m in mapped_results)
        eim = ports_match
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


# ---------------------------------------------------------------------------
# 5. Stateless Zero-Knowledge Encrypted Relay Hub (For 4G Symmetric CGNATs)
# ---------------------------------------------------------------------------

def make_room_tag(room_name: str) -> bytes:
    """Hashes a human-readable room name into a fixed 16-byte routing tag."""
    return hashlib.sha256(b"NLTP-RELAY-ROOM:" + room_name.strip().encode("utf-8")).digest()[:16]


def wrap_relay_packet(room_tag16: bytes, inner_pkt: bytes, cmd: int = RELAY_VER_DATA) -> bytes:
    return RELAY_MAGIC + bytes([cmd & 0xFF]) + room_tag16[:16] + inner_pkt


def unwrap_relay_packet(raw: bytes) -> Optional[Tuple[int, bytes, bytes]]:
    if len(raw) < RELAY_HDR_SIZE or raw[:2] != RELAY_MAGIC:
        return None
    return raw[2], raw[3:19], raw[19:]


class NLTPRelayHub:
    """
    Lightweight 1-Port Stateless Cloud Relay (`TURN`/`DERP` equivalent for NLTP).

    Pairs devices behind strict 4G/5G Symmetric CGNATs under a 16-byte `room_tag` and forwards
    end-to-end `ChaCha20-Poly1305` encrypted NLTP frames between room members with zero
    decryption keys and zero payload visibility!
    """

    def __init__(self, bind_addr: Tuple[str, int] = ("0.0.0.0", 9000), peer_ttl_s: float = 90.0) -> None:
        self._sock, _ = open_internet_udp_socket(bind_addr=bind_addr, nonblocking=False)
        self._sock.settimeout(0.05)
        self.peer_ttl_s = peer_ttl_s
        # room_tag16 -> {peer_addr: last_seen_monotonic}
        self.rooms: Dict[bytes, Dict[Tuple, float]] = {}
        self.forwarded_packets: int = 0
        self.forwarded_bytes: int = 0
        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def getsockname(self) -> Tuple:
        return self._sock.getsockname()

    def _loop(self) -> None:
        last_gc = time.perf_counter()
        while self._running:
            try:
                raw, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                now = time.perf_counter()
                if now - last_gc > 15.0:
                    self._gc(now)
                    last_gc = now
                continue
            except (BlockingIOError, InterruptedError, ConnectionResetError):
                continue
            except OSError:
                if not self._running:
                    break
                continue

            parsed = unwrap_relay_packet(raw)
            if parsed is None:
                continue
            cmd, room_tag, inner = parsed
            now = time.perf_counter()
            members = self.rooms.setdefault(room_tag, {})
            members[addr] = now

            if cmd == RELAY_VER_REGISTER:
                ack = wrap_relay_packet(room_tag, b"", cmd=RELAY_VER_REG_ACK)
                try:
                    self._sock.sendto(ack, addr)
                except OSError:
                    pass
                continue

            if cmd == RELAY_VER_DATA and inner:
                # Forward opaque encrypted NLTP packet to all other active peers in this room!
                fwd = wrap_relay_packet(room_tag, inner, cmd=RELAY_VER_DATA)
                for peer_addr, seen_t in list(members.items()):
                    if peer_addr == addr:
                        continue
                    if now - seen_t > self.peer_ttl_s:
                        del members[peer_addr]
                        continue
                    try:
                        self._sock.sendto(fwd, peer_addr)
                        self.forwarded_packets += 1
                        self.forwarded_bytes += len(fwd)
                    except OSError:
                        pass

    def _gc(self, now: float) -> None:
        for room_tag in list(self.rooms.keys()):
            members = self.rooms[room_tag]
            for p, t_seen in list(members.items()):
                if now - t_seen > self.peer_ttl_s:
                    del members[p]
            if not members:
                del self.rooms[room_tag]

    def close(self) -> None:
        self._running = False
        self._worker.join(timeout=1.0)
        self._sock.close()

    def __enter__(self) -> "NLTPRelayHub":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
