"""Security properties of encrypted NLTP sessions (NLTP 1.5, wire version 2, X25519+PSK PFS; see necklink/session.py)."""
import socket
import struct
import threading
import time

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from nltp import NLTPClient, NLTPServer, generate_nltp_key
from necklink.internet import AEADCipherContext, ReplayWindow64, derive_aead_key
from necklink.nltp import NLTPEndpoint, pack_nltp_frame
from necklink.session import (
    HELLO_ACK_PT_BYTES,
    HELLO_PT_BYTES,
    PT_HELLO,
    PT_HELLO_ACK,
    SESSION_VERSION,
    derive_session_keys,
    x25519_scalarmult,
)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Recorder:
    """UDP forwarder on the path client -> server that records what an eavesdropper would see."""

    def __init__(self, server_port: int) -> None:
        self.server = ("127.0.0.1", server_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.client = None
        self.captured = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            if addr == self.server:
                if self.client:
                    self.sock.sendto(data, self.client)
            else:
                self.client = addr
                self.captured.append(data)
                self.sock.sendto(data, self.server)

    def close(self) -> None:
        self.sock.close()


def _drain(srv, timeout=1.5):
    items = []
    while (it := srv.recv(timeout)) is not None:
        items.append(it)
    return items


def test_recorded_traffic_is_rejected_after_receiver_restart():
    key, port = generate_nltp_key(), _free_port()
    rec = _Recorder(port)
    with NLTPServer(("127.0.0.1", port), psk=key) as srv:
        with NLTPClient(("127.0.0.1", rec.port), psk=key) as cli:
            seq = cli.send_reflex("OPEN_VALVE_3")
            assert cli.wait_reflex_ack(seq, timeout=3.0)
            assert cli.send_bytes(b"setpoint=95C", timeout=5.0)["verified_sha256"]
            assert {i.data for i in _drain(srv, 1.0)} == {b"OPEN_VALVE_3", b"setpoint=95C"}
    rec.close()
    assert rec.captured

    with NLTPServer(("127.0.0.1", port), psk=key) as srv2:   # the receiver restarted
        attacker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for d in rec.captured:
            attacker.sendto(d, ("127.0.0.1", port))
            time.sleep(0.002)
        assert _drain(srv2, 1.5) == []                       # nothing replayed is delivered


def _pair(key, cid_a, cid_b):
    """Two engines wired back to back in memory; returns (a, b, wire) where wire logs every datagram."""
    wire = []
    queues = {"A": [], "B": []}
    a = NLTPEndpoint(conn_id=cid_a, psk=key, send_packet_fn=lambda raw, peer: (wire.append(raw), queues["B"].append(raw)))
    b = NLTPEndpoint(conn_id=cid_b, psk=key, send_packet_fn=lambda raw, peer: (wire.append(raw), queues["A"].append(raw)))

    def pump(rounds=60):
        for _ in range(rounds):
            time.sleep(0.005)  # let retransmit timers run (the first reflex copy is sent before the handshake)
            for name, ep, src in (("A", a, "B"), ("B", b, "A")):
                q = queues[name]
                while q:
                    ep.on_datagram(q.pop(0), src)
            a.step_tx()
            b.step_tx()
    return a, b, wire, pump


def test_sessions_with_the_same_conn_id_never_share_keys():
    """1.3.x sealed every frame with the long-term key and nonce conn_id||counter (counter from 1), so two
    sessions with the same conn_id produced identical keystreams. Sessions now get fresh keys each time."""
    key = generate_nltp_key()
    first_data_frames = []
    for _ in range(2):
        a, b, wire, pump = _pair(key, 0x1234, 0x5678)       # same conn_ids both times
        a.send_reflex("B", b"identical payload")
        pump()
        data = [w for w in wire if (w[2] >> 4) == SESSION_VERSION and (w[2] & 0x0F) == 0x06]
        assert data, "reflex frame not sent"
        assert b.completed_inbox and b.completed_inbox[0].data == b"identical payload"
        first_data_frames.append(data[0])
    h0, h1 = first_data_frames[0][:20], first_data_frames[1][:20]
    assert struct.unpack_from("<IQ", h0, 4) == struct.unpack_from("<IQ", h1, 4)   # same nonce ...
    assert first_data_frames[0][20:] != first_data_frames[1][20:]               # ... different ciphertext


def test_sender_recovers_quickly_when_receiver_restarts():
    key, port = generate_nltp_key(), _free_port()
    with NLTPClient(("127.0.0.1", port), psk=key) as cli:
        with NLTPServer(("127.0.0.1", port), psk=key):
            assert cli.send_bytes(b"before restart", timeout=5.0)["verified_sha256"]
        with NLTPServer(("127.0.0.1", port), psk=key) as srv2:
            t0 = time.perf_counter()
            receipt = cli.send_bytes(b"after restart", timeout=5.0)
            assert receipt["verified_sha256"]
            assert time.perf_counter() - t0 < 2.0              # NO_SESSION hint -> re-key in ~1 RTT
            assert cli.engine._sessions.rekeys_from_hint >= 1
            assert srv2.recv(1.0).data == b"after restart"


def test_old_unsessioned_frames_and_wrong_keys_are_rejected():
    key, port = generate_nltp_key(), _free_port()
    with NLTPServer(("127.0.0.1", port), psk=key) as srv:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        legacy = pack_nltp_frame(0x06, 0xABCD, struct.pack("<IH", 1, 4) + b"STOP", aead=AEADCipherContext(key))
        sock.sendto(legacy, ("127.0.0.1", port))               # a 1.3.x frame sealed with the long-term key
        time.sleep(0.2)
        assert srv.recv(0.3) is None
        assert srv.engine._sessions.legacy_drops >= 1
        with NLTPClient(("127.0.0.1", port), psk=generate_nltp_key()) as wrong:
            assert not wrong.send_bytes(b"x", timeout=1.5)["verified_sha256"]
        assert srv.recv(0.3) is None


def test_replayed_hellos_do_not_break_a_live_session():
    key, port = generate_nltp_key(), _free_port()
    rec = _Recorder(port)
    with NLTPServer(("127.0.0.1", port), psk=key) as srv:
        with NLTPClient(("127.0.0.1", rec.port), psk=key) as cli:
            assert cli.send_bytes(b"one", timeout=5.0)["verified_sha256"]
            hello = next(d for d in rec.captured if (d[2] & 0x0F) == PT_HELLO)
            attacker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for _ in range(3):
                attacker.sendto(hello, ("127.0.0.1", port))
            # a different-looking replay: an older HELLO from another session of the same client cid
            time.sleep(0.2)
            assert cli.send_bytes(b"two", timeout=5.0)["verified_sha256"]
            assert [i.data for i in _drain(srv, 1.0)] == [b"one", b"two"]
    rec.close()


def test_server_can_start_a_session_towards_a_client():
    key, port = generate_nltp_key(), _free_port()
    with NLTPServer(("127.0.0.1", port), psk=key) as srv:
        with NLTPClient(("127.0.0.1", port), psk=key) as cli:
            assert cli.send_bytes(b"hello", timeout=5.0)["verified_sha256"]
            item = srv.recv(2.0)
            assert srv.send_bytes(item.peer_addr, b"reply", timeout=5.0)
            assert cli.recv(2.0).data == b"reply"
            seq = srv.send_reflex(item.peer_addr, "ESTOP")
            assert srv.wait_reflex_ack(seq, timeout=3.0)


def test_simultaneous_open_from_both_sides_settles_on_one_session():
    key = generate_nltp_key()
    a, b, wire, pump = _pair(key, 0x1111, 0x2222)
    a.send_reflex("B", b"from a")       # both start a handshake at the same moment
    b.send_reflex("A", b"from b")
    pump()
    assert [i.data for i in b.completed_inbox] == [b"from a"]
    assert [i.data for i in a.completed_inbox] == [b"from b"]
    assert a.reflex_pending() == 0 and b.reflex_pending() == 0


def test_reflex_sent_right_before_close_still_goes_out():
    """A reflex sent before the session exists waits for the handshake; close() lingers until it is out."""
    key, port = generate_nltp_key(), _free_port()
    with NLTPServer(("127.0.0.1", port), psk=key) as srv:
        with NLTPClient(("127.0.0.1", port), psk=key) as cli:
            cli.send_reflex("LAST_WORDS")      # no wait: the client closes immediately
        item = srv.recv(2.0)
        assert item is not None and item.kind == "reflex" and item.data == b"LAST_WORDS"


def test_rfc7748_x25519_vector_and_low_order_rejection():
    """Verifies RFC 7748 Section 5.2 Test Vector 1 and rejects the all-zero low-order point."""
    alice_sk = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
    bob_pk = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
    expected = bytes.fromhex("c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")
    assert x25519_scalarmult(alice_sk, bob_pk) == expected
    assert x25519_scalarmult(alice_sk, b"\x00" * 32) is None


def test_forward_secrecy_compromised_psk_cannot_decrypt_recorded_session():
    """Even an eavesdropper who records both directions (HELLO + HELLO_ACK + data) AND later steals the
    static PSK cannot reconstruct the ephemeral X25519 shared secret or decrypt any session frame."""
    key = generate_nltp_key()
    a, b, wire, pump = _pair(key, 0xCAFE1111, 0xBEEF2222)
    a.send_reflex("B", b"CONFIDENTIAL_TELEMETRY_42")
    pump()
    assert b.completed_inbox and b.completed_inbox[0].data == b"CONFIDENTIAL_TELEMETRY_42"

    # Ephemeral private keys are wiped from both endpoints after establishment
    assert a._sessions.pending == {} and b._sessions.pending == {}
    for s in (*a._sessions.by_addr.values(), *b._sessions.by_addr.values()):
        assert s.sk_i is None

    # Attacker steals long-term PSK and unseals captured HELLO & HELLO_ACK
    k_long = derive_aead_key(key)
    hs_aead = ChaCha20Poly1305(k_long)
    hello_raw = next(w for w in wire if (w[2] & 0x0F) == PT_HELLO)
    ack_raw = next(w for w in wire if (w[2] & 0x0F) == PT_HELLO_ACK)
    data_raw = next(w for w in wire if (w[2] & 0x0F) == 0x06)

    _, _, _, cid_i, seq_h, _ = struct.unpack("<2sBBIQI", hello_raw[:20])
    _, _, _, cid_r, seq_a, _ = struct.unpack("<2sBBIQI", ack_raw[:20])
    hello_pt = hs_aead.decrypt(struct.pack("<IQ", cid_i, seq_h), hello_raw[20:], hello_raw[:20])
    ack_pt = hs_aead.decrypt(struct.pack("<IQ", cid_r, seq_a), ack_raw[20:], ack_raw[:20])
    assert len(hello_pt) == HELLO_PT_BYTES and len(ack_pt) == HELLO_ACK_PT_BYTES
    n_i, pk_i = hello_pt[:16], hello_pt[16:48]
    n_r, pk_r = ack_pt[16:32], ack_pt[32:64]

    # Without sk_i or sk_r, any guessed DH value fails ChaCha20-Poly1305 authentication on recorded data
    _, _, _, cid_d, seq_d, _ = struct.unpack("<2sBBIQI", data_raw[:20])
    for wrong_dh in (b"\x00" * 32, pk_i, pk_r, k_long):
        k_i2r, _ = derive_session_keys(k_long, wrong_dh, n_i, n_r, cid_i, cid_r)
        with pytest.raises(InvalidTag):
            ChaCha20Poly1305(k_i2r).decrypt(struct.pack("<IQ", cid_d, seq_d), data_raw[20:], data_raw[:20])


def test_replay_window_absorbs_512_packet_reordering():
    rw = ReplayWindow64()
    assert rw.accept(600)
    assert rw.accept(89)         # 600 - 89 == 511 -> accepted inside 512-packet window
    assert not rw.accept(89)     # duplicate blocked
    assert not rw.accept(88)     # 600 - 88 == 512 -> outside 512-packet window
