"""
Encrypted NLTP sessions (wire version 2, NLTP 1.4).

Why: in 1.3.x every frame was sealed directly with the long-term pre-shared key, using the nonce
conn_id || counter, where the counter restarted at 1 whenever a program started. That allowed:
  * replay: a receiver that restarted forgot its replay window, so recorded frames were accepted again;
  * nonce reuse: two sessions with the same conn_id (a reused or colliding id) reused nonces under one key.

Fix: a 1-RTT handshake gives every session its own keys, mixed from a fresh random value from EACH side:

    initiator -> responder  HELLO      { n_i (16 B) }              sealed with the long-term key K
    responder -> initiator  HELLO_ACK  { n_i (16 B) || n_r (16 B) } sealed with K

    prk   = HMAC-SHA256(K,   "NLTP-1.4 session" || n_i || n_r || LE32(cid_i) || LE32(cid_r))
    k_i2r = HMAC-SHA256(prk, "i2r"),   k_r2i = HMAC-SHA256(prk, "r2i")

Data frames keep the 20-byte header (version nibble 2) and are sealed with the session key for their
direction (nonce = sender conn_id || per-session counter from 1). Handshake frames use a random 64-bit
counter, so nonces under K do not repeat. Recorded traffic fails after any restart because the responder
contributes a new n_r to every session; counters may restart safely because every session has new keys.

What this does NOT add: forward secrecy (a leaked K decrypts recorded sessions) or per-device identity
(every holder of K is trusted). Those need public-key cryptography.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from .internet import ReplayWindow64, derive_aead_key

SESSION_VERSION = 2
PT_HELLO = 0x0B
PT_HELLO_ACK = 0x0C
PT_NO_SESSION = 0x0D          # unauthenticated hint: "I have no session for your conn_id" (see open())
NONCE_BYTES = 16
FLAG_ENCRYPTED = 0x80
MAGIC = b"NL"
HDR_FMT = "<2sBBIQI"          # magic, ver_type, flags, conn_id, seq64, ts_ms = 20 B
HDR_SIZE = 20
TAG_SIZE = 16
KDF_LABEL = b"NLTP-1.4 session"

HELLO_FIRST_RETX_S = 0.12     # HELLO retransmit interval (x1.5 per retry, capped at 0.5 s)
HELLO_MAX_RETX_S = 0.50
HELLO_GIVE_UP_S = 10.0        # a pending handshake is dropped after this; the next send starts a new one
TAKEOVER_SILENCE_S = 1.0      # a new HELLO replaces the session used for sending only after this silence
REKEY_SILENCE_S = 3.0         # re-handshake when a peer we still owe data has been silent this long
HINT_INTERVAL_S = 0.25        # rate limit for NO_SESSION hints (sent and acted on), per address
MAX_SESSIONS_PER_CID = 2      # current + one candidate: a replayed HELLO cannot knock out a live session
MAX_CIDS = 4096
MAX_QUEUED = 64               # frames (reflexes, pings, ACKs) held while a handshake is running

Address = Tuple


def hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def derive_session_keys(k_long: bytes, n_i: bytes, n_r: bytes, cid_i: int, cid_r: int) -> Tuple[bytes, bytes]:
    """Returns (k_i2r, k_r2i) for the session between initiator cid_i and responder cid_r."""
    prk = hmac_sha256(k_long, KDF_LABEL + n_i + n_r + struct.pack("<II", cid_i & 0xFFFFFFFF, cid_r & 0xFFFFFFFF))
    return hmac_sha256(prk, b"i2r"), hmac_sha256(prk, b"r2i")


def _random_seq() -> int:
    return int.from_bytes(os.urandom(8), "little") | 1


class _Session:
    __slots__ = ("role", "peer_addr", "peer_cid", "n_i", "n_r", "tx", "rx", "tx_seq", "replay",
                 "established", "est_s", "last_rx_s", "hello_next_s", "hello_interval", "deadline_s", "ack_frame",
                 "queued")

    def __init__(self, role: str, peer_addr: Address, n_i: bytes, now: float) -> None:
        self.role = role
        self.peer_addr = peer_addr
        self.peer_cid: Optional[int] = None
        self.n_i = n_i
        self.n_r: Optional[bytes] = None
        self.tx: Optional[ChaCha20Poly1305] = None
        self.rx: Optional[ChaCha20Poly1305] = None
        self.tx_seq = 0
        self.replay = ReplayWindow64()
        self.established = False
        self.est_s = now
        self.last_rx_s = now
        self.hello_next_s = now
        self.hello_interval = HELLO_FIRST_RETX_S
        self.deadline_s = now + HELLO_GIVE_UP_S
        self.ack_frame: Optional[bytes] = None
        self.queued: List[Tuple[int, bytes, int, int]] = []

    def set_keys(self, k_i2r: bytes, k_r2i: bytes, now: float) -> None:
        if self.role == "i":
            self.tx, self.rx = ChaCha20Poly1305(k_i2r), ChaCha20Poly1305(k_r2i)
        else:
            self.tx, self.rx = ChaCha20Poly1305(k_r2i), ChaCha20Poly1305(k_i2r)
        self.established = True
        self.est_s = now
        self.last_rx_s = now


HANDLED = object()  # open() result for a handshake frame that was processed (nothing to deliver)


class SessionTable:
    """Per-peer session state for one endpoint (multi-peer: sending is by address, receiving by conn_id)."""

    def __init__(self, psk, my_cid: int, send_raw: Callable[[bytes, Address], None]) -> None:
        self.k_long = derive_aead_key(psk)
        self._hs_aead = ChaCha20Poly1305(self.k_long)
        self.my_cid = my_cid & 0xFFFFFFFF
        self._send_raw = send_raw
        self.by_addr: Dict[Address, _Session] = {}
        self.by_cid: Dict[int, List[_Session]] = {}
        self.pending: Dict[Address, _Session] = {}
        self.hellos_tx = 0
        self.acks_tx = 0
        self.established = 0
        self.legacy_drops = 0
        self.hints_tx = 0
        self.rekeys_from_hint = 0
        self._hint_tx_s: Dict[Address, float] = {}
        self._hint_rx_s: Dict[Address, float] = {}

    # ---------------------------------------------------------------- framing
    def _header(self, pkt_type: int, flags: int, seq64: int, ts_ms: int) -> bytes:
        ver_type = ((SESSION_VERSION & 0x0F) << 4) | (pkt_type & 0x0F)
        return struct.pack(HDR_FMT, MAGIC, ver_type, (flags & 0x7F) | FLAG_ENCRYPTED, self.my_cid, seq64, ts_ms & 0xFFFFFFFF)

    def _seal(self, aead: ChaCha20Poly1305, pkt_type: int, flags: int, seq64: int, ts_ms: int, payload: bytes) -> bytes:
        hdr = self._header(pkt_type, flags, seq64, ts_ms)
        return hdr + aead.encrypt(struct.pack("<IQ", self.my_cid, seq64), payload, hdr)

    def _send_hello(self, s: _Session, ts_ms: int) -> None:
        self._send_raw(self._seal(self._hs_aead, PT_HELLO, 0, _random_seq(), ts_ms, s.n_i), s.peer_addr)
        self.hellos_tx += 1

    def _start_handshake(self, addr: Address, now: float) -> _Session:
        s = _Session("i", addr, os.urandom(NONCE_BYTES), now)
        self.pending[addr] = s
        self._send_hello(s, int(now * 1000.0))
        s.hello_next_s = now + s.hello_interval
        return s

    def queued_count(self) -> int:
        """Frames waiting for a handshake to finish (close() lingers briefly so they still go out)."""
        return sum(len(p.queued) for p in self.pending.values())

    # ---------------------------------------------------------------- TX side
    def ready(self, addr: Address, now: float) -> bool:
        """True when data can be sealed for `addr`; otherwise starts a handshake (if none is running)."""
        if addr in self.by_addr:
            return True
        if addr not in self.pending:
            self._start_handshake(addr, now)
        return False

    def seal(self, addr: Address, pkt_type: int, payload: bytes, flags: int, ts_ms: int, now: float) -> Optional[bytes]:
        s = self.by_addr.get(addr)
        if s is None:
            self.ready(addr, now)
            p = self.pending.get(addr)
            if p is not None and len(p.queued) < MAX_QUEUED:
                p.queued.append((pkt_type, payload, flags, ts_ms))   # sent as soon as the session is up
            return None
        s.tx_seq += 1
        return self._seal(s.tx, pkt_type, flags, s.tx_seq, ts_ms, payload)

    def service(self, now: float, busy_addrs: Sequence[Address]) -> None:
        """Retransmits pending HELLOs and re-handshakes with busy peers that went silent (e.g. restarted)."""
        for addr, s in list(self.pending.items()):
            if now >= s.deadline_s:
                del self.pending[addr]
            elif now >= s.hello_next_s:
                self._send_hello(s, int(now * 1000.0))
                s.hello_interval = min(HELLO_MAX_RETX_S, s.hello_interval * 1.5)
                s.hello_next_s = now + s.hello_interval
        for addr in busy_addrs:
            s = self.by_addr.get(addr)
            if (s is not None and addr not in self.pending and now - s.last_rx_s > REKEY_SILENCE_S
                    and now - s.est_s > REKEY_SILENCE_S):
                self._start_handshake(addr, now)

    # ---------------------------------------------------------------- RX side
    def _remember(self, cid: int, s: _Session, active: bool) -> None:
        """Slot 0 holds the session that last proved itself (authenticated traffic, or our own handshake);
        a session created from a HELLO only takes slot 1, so replayed HELLOs never evict the live session."""
        lst = self.by_cid.get(cid)
        if lst is None:
            if len(self.by_cid) >= MAX_CIDS:
                self.by_cid.pop(next(iter(self.by_cid)))
            lst = self.by_cid[cid] = []
        if s in lst:
            lst.remove(s)
        lst.insert(0 if (active or not lst) else 1, s)
        del lst[MAX_SESSIONS_PER_CID:]

    def open(self, raw: bytes, addr: Address, now: float):
        """Returns (pkt_type, flags, conn_id, ts_ms, payload), HANDLED for handshake frames, or None."""
        if len(raw) < HDR_SIZE + TAG_SIZE or raw[:2] != MAGIC:
            return None
        _m, ver_type, flags, cid, seq64, ts_ms = struct.unpack(HDR_FMT, raw[:HDR_SIZE])
        if not (flags & FLAG_ENCRYPTED):
            return None                       # plaintext frames are never accepted once a key is set
        if (ver_type >> 4) != SESSION_VERSION:
            self.legacy_drops += 1            # 1.3.x frames sealed directly with the long-term key
            return None
        pkt_type = ver_type & 0x0F
        hdr, body = raw[:HDR_SIZE], raw[HDR_SIZE:]
        nonce = struct.pack("<IQ", cid, seq64)

        if pkt_type == PT_NO_SESSION:
            self._on_no_session(body, addr, now)
            return HANDLED

        if pkt_type in (PT_HELLO, PT_HELLO_ACK):
            try:
                pt = self._hs_aead.decrypt(nonce, body, hdr)
            except InvalidTag:
                return None
            if pkt_type == PT_HELLO and len(pt) >= NONCE_BYTES:
                self._on_hello(cid, pt[:NONCE_BYTES], addr, now, ts_ms)
                return HANDLED
            if pkt_type == PT_HELLO_ACK and len(pt) >= 2 * NONCE_BYTES:
                self._on_ack(cid, pt[:NONCE_BYTES], pt[NONCE_BYTES:2 * NONCE_BYTES], addr, now)
                return HANDLED
            return None

        for s in self.by_cid.get(cid, ()):
            if not s.replay.would_accept(seq64):
                continue
            try:
                pt = s.rx.decrypt(nonce, body, hdr)
            except InvalidTag:
                continue
            s.replay.accept(seq64)
            s.last_rx_s = now
            s.peer_addr = addr                # roaming: follow the authenticated sender
            self._remember(cid, s, active=True)
            if self.by_addr.get(addr) is not s:
                self.by_addr[addr] = s        # authenticated traffic proves the peer holds these keys
            return pkt_type, flags & 0x7F, cid, ts_ms, pt
        if cid not in self.by_cid:
            self._send_no_session(cid, seq64, addr, now, ts_ms)
        return None

    # ---------------------------------------------------------------- restart recovery
    def _send_no_session(self, cid: int, seq64: int, addr: Address, now: float, ts_ms: int) -> None:
        """A peer is sealing frames for a session we do not have (we restarted): tell it, so it re-keys in
        one round trip instead of after REKEY_SILENCE_S. Unauthenticated by necessity; it echoes the frame's
        conn_id and counter so the peer only reacts to frames it really sent, and a forged hint costs at most
        one extra handshake."""
        if now - self._hint_tx_s.get(addr, -1e9) < HINT_INTERVAL_S:
            return
        if len(self._hint_tx_s) > MAX_CIDS:
            self._hint_tx_s.clear()
        self._hint_tx_s[addr] = now
        hdr = self._header(PT_NO_SESSION, 0, 0, ts_ms)
        self._send_raw(hdr + struct.pack("<IQ", cid, seq64) + bytes(4), addr)
        self.hints_tx += 1

    def _on_no_session(self, body: bytes, addr: Address, now: float) -> None:
        if len(body) < 12:
            return
        echo_cid, echo_seq = struct.unpack_from("<IQ", body, 0)
        s = self.by_addr.get(addr)
        if (s is None or echo_cid != self.my_cid or not (0 < echo_seq <= s.tx_seq) or addr in self.pending
                or now - self._hint_rx_s.get(addr, -1e9) < HINT_INTERVAL_S):
            return
        self._hint_rx_s[addr] = now
        self.rekeys_from_hint += 1
        self._start_handshake(addr, now)

    def _on_hello(self, cid: int, n_i: bytes, addr: Address, now: float, ts_ms: int) -> None:
        for s in self.by_cid.get(cid, ()):
            if s.role == "r" and s.n_i == n_i and s.ack_frame is not None:
                self._send_raw(s.ack_frame, addr)     # retransmitted HELLO: same answer
                self.acks_tx += 1
                return
        mine = self.pending.get(addr)
        if mine is not None:
            if mine.n_i < n_i:
                return                                # simultaneous open: our HELLO wins, theirs is ignored
            del self.pending[addr]                    # theirs wins; our queued frames go out on their session
        s = _Session("r", addr, n_i, now)
        s.peer_cid = cid
        s.n_r = os.urandom(NONCE_BYTES)
        s.set_keys(*derive_session_keys(self.k_long, n_i, s.n_r, cid, self.my_cid), now=now)
        self._remember(cid, s, active=False)
        cur = self.by_addr.get(addr)
        if cur is None or now - cur.last_rx_s >= TAKEOVER_SILENCE_S:
            self.by_addr[addr] = s
        s.ack_frame = self._seal(self._hs_aead, PT_HELLO_ACK, 0, _random_seq(), ts_ms, n_i + s.n_r)
        self._send_raw(s.ack_frame, addr)
        self.acks_tx += 1
        self.established += 1
        if mine is not None and mine.queued and self.by_addr.get(addr) is s:
            self._flush(s, mine.queued, addr)

    def _flush(self, s: _Session, queued: List[Tuple[int, bytes, int, int]], addr: Address) -> None:
        for pkt_type, payload, flags, ts_ms in queued:
            s.tx_seq += 1
            self._send_raw(self._seal(s.tx, pkt_type, flags, s.tx_seq, ts_ms, payload), addr)

    def _on_ack(self, cid: int, n_i: bytes, n_r: bytes, addr: Address, now: float) -> None:
        s = self.pending.get(addr)
        if s is None or s.n_i != n_i:
            s = next((p for p in self.pending.values() if p.n_i == n_i), None)
            if s is None:
                return                                # stale or replayed ACK
        for key, val in list(self.pending.items()):
            if val is s:
                del self.pending[key]
        s.peer_cid = cid
        s.n_r = n_r
        s.peer_addr = addr
        s.set_keys(*derive_session_keys(self.k_long, n_i, n_r, self.my_cid, cid), now=now)
        self._remember(cid, s, active=True)
        self.by_addr[addr] = s
        self.established += 1
        queued, s.queued = s.queued, []
        self._flush(s, queued, addr)
