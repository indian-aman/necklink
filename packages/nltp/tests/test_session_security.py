"""Security properties of encrypted NLTP sessions (NLTP 1.4, wire version 2; see necklink/session.py)."""
import socket
import struct
import threading
import time

import pytest

from nltp import NLTPClient, NLTPServer, generate_nltp_key
from necklink.internet import AEADCipherContext
from necklink.nltp import NLTPEndpoint, pack_nltp_frame
from necklink.session import PT_HELLO, SESSION_VERSION


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
