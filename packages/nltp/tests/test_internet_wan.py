"""
Internet & WAN Readiness Verification Suite (`tests/test_internet_wan.py`).

Tests the 6 real-world requirements for running NLTP over the public Internet:
  1. `ChaCha20-Poly1305` End-to-End Authenticated Encryption + `RFC 6479` Anti-Replay + `<1200 B` MTU
  2. Live Mid-Upload Wi-Fi -> 4G Connection Migration (`conn_id` socket roaming under packet loss)
  3. Dual Stateful NAT Firewalls + Bidirectional P2P UDP Hole Punching (`punch_p2p`)
  4. Strict 4G/5G Symmetric CGNAT Traversal via Zero-Knowledge `NLTPRelayHub`
  5. Router Narrow FIFO Buffer (`32-pkt` cap) Survival via `TokenBucketPacer`
  6. Live Public Internet `RFC 5389` STUN Reachability & NAT Classification (`discover_wan`)
"""
from __future__ import annotations

import collections
import hashlib
import os
import select
import socket
import threading
import time
from pathlib import Path
from typing import Deque, Dict, Optional, Set, Tuple

import numpy as np
import pytest

from necklink import (
    AEADCipherContext,
    NLTPClient,
    NLTPRelayHub,
    NLTPServer,
    generate_nltp_key,
    pack_nltp_frame,
    unpack_nltp_frame,
)
from necklink.internet import RELAY_VER_DATA, make_room_tag, wrap_relay_packet
from necklink.workloads import generate_aqms_gas_plume_telemetry

Address = Tuple[str, int]


# ---------------------------------------------------------------------------
# Realistic WAN Middleboxes for Deterministic OS Socket Testing
# ---------------------------------------------------------------------------

class DualStatefulNATBox:
    """
    Simulates two separate home/office Stateful Endpoint-Independent NAT Firewalls (`NAT A` & `NAT B`)
    sitting across the public Internet:
      - `Peer A` talks ONLY to `nat_a_lan`; `NAT A` exposes `nat_a_wan` to the Internet.
      - `Peer B` talks ONLY to `nat_b_lan`; `NAT B` exposes `nat_b_wan` to the Internet.
      - STRICT STATEFUL FIREWALL RULE:
        `NAT A` drops ANY packet arriving on `nat_a_wan` from `src_wan` unless `Peer A` has ALREADY
        sent at least one outbound packet through `NAT A` addressed to `src_wan`!
        Same strict outbound-pinhole rule enforced independently on `NAT B`!
    """

    def __init__(self) -> None:
        self.nat_a_lan = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.nat_a_wan = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.nat_b_lan = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.nat_b_wan = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for s in (self.nat_a_lan, self.nat_a_wan, self.nat_b_lan, self.nat_b_wan):
            s.bind(("127.0.0.1", 0))
            s.setblocking(False)

        self.peer_a_private: Optional[Address] = None
        self.peer_b_private: Optional[Address] = None
        self.pinholes_a: Set[Address] = set()
        self.pinholes_b: Set[Address] = set()
        self.dropped_unsolicited_a: int = 0
        self.dropped_unsolicited_b: int = 0

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def wan_a_addr(self) -> Address:
        return self.nat_a_wan.getsockname()

    @property
    def wan_b_addr(self) -> Address:
        return self.nat_b_wan.getsockname()

    @property
    def lan_a_addr(self) -> Address:
        return self.nat_a_lan.getsockname()

    @property
    def lan_b_addr(self) -> Address:
        return self.nat_b_lan.getsockname()

    def _loop(self) -> None:
        # Peer A sends to `(lan_a_ip, lan_a_port)` when targeting `wan_b`?
        # Even cleaner: Peer A sends directly to `wan_b_addr`? No — if Peer A sends directly on localhost,
        # it bypasses NAT A! So Peer A sends to `lan_a_addr` (its default gateway toward Peer B), which
        # translates source to `wan_a_addr` and forwards to `wan_b_addr`!
        while self._running:
            active = False
            # 1. Outbound from Peer A -> NAT A LAN -> punches pinhole for wan_b -> sends from NAT A WAN to NAT B WAN
            try:
                pkt, src = self.nat_a_lan.recvfrom(65535)
                active = True
                self.peer_a_private = src
                self.pinholes_a.add(self.wan_b_addr)
                self.nat_a_wan.sendto(pkt, self.wan_b_addr)
            except (BlockingIOError, ConnectionResetError):
                pass
            except OSError:
                break

            # 2. Outbound from Peer B -> NAT B LAN -> punches pinhole for wan_a -> sends from NAT B WAN to NAT A WAN
            try:
                pkt, src = self.nat_b_lan.recvfrom(65535)
                active = True
                self.peer_b_private = src
                self.pinholes_b.add(self.wan_a_addr)
                self.nat_b_wan.sendto(pkt, self.wan_a_addr)
            except (BlockingIOError, ConnectionResetError):
                pass
            except OSError:
                break

            # 3. Inbound at NAT A WAN -> ONLY allowed if `src in self.pinholes_a`!
            try:
                pkt, src_wan = self.nat_a_wan.recvfrom(65535)
                active = True
                if src_wan in self.pinholes_a and self.peer_a_private is not None:
                    self.nat_a_lan.sendto(pkt, self.peer_a_private)
                else:
                    self.dropped_unsolicited_a += 1
            except (BlockingIOError, ConnectionResetError):
                pass
            except OSError:
                break

            # 4. Inbound at NAT B WAN -> ONLY allowed if `src in self.pinholes_b`!
            try:
                pkt, src_wan = self.nat_b_wan.recvfrom(65535)
                active = True
                if src_wan in self.pinholes_b and self.peer_b_private is not None:
                    self.nat_b_lan.sendto(pkt, self.peer_b_private)
                else:
                    self.dropped_unsolicited_b += 1
            except (BlockingIOError, ConnectionResetError):
                pass
            except OSError:
                break

            if not active:
                time.sleep(0.001)

    def close(self) -> None:
        self._running = False
        self._thread.join(timeout=1.0)
        for s in (self.nat_a_lan, self.nat_a_wan, self.nat_b_lan, self.nat_b_wan):
            s.close()


class BottleneckRouterQueue:
    """
    Exact continuous-time G/D/1/K router bottleneck queue (`link_kbps` drain rate,
    `max_queue_pkts * 1024 B` physical FIFO buffer capacity) immune to OS sleep jitter.
    """

    def __init__(self, server_addr: Address, *, link_kbps: float = 12_000.0, max_queue_pkts: int = 32) -> None:
        self.server_addr = server_addr
        self.rate_Bps = (link_kbps * 1000.0) / 8.0
        self.max_queue_bytes = float(max_queue_pkts * 1024)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.client_addr: Optional[Address] = None
        self.next_free_time: float = time.perf_counter()
        self.tail_drops: int = 0
        self.forwarded: int = 0
        self.max_pkt_size_seen: int = 0
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def addr(self) -> Address:
        return self.sock.getsockname()

    def _loop(self) -> None:
        while self._running:
            got = False
            for _ in range(256):
                try:
                    pkt, src = self.sock.recvfrom(65535)
                    got = True
                except (BlockingIOError, ConnectionResetError):
                    break
                except OSError:
                    return
                t_arr = time.perf_counter()
                self.max_pkt_size_seen = max(self.max_pkt_size_seen, len(pkt))
                if src == self.server_addr:
                    if self.client_addr is not None:
                        try:
                            self.sock.sendto(pkt, self.client_addr)
                        except OSError:
                            pass
                else:
                    self.client_addr = src
                    backlog_bytes = max(0.0, (self.next_free_time - t_arr) * self.rate_Bps)
                    if backlog_bytes + len(pkt) > self.max_queue_bytes:
                        self.tail_drops += 1
                    else:
                        self.next_free_time = max(t_arr, self.next_free_time) + (len(pkt) / self.rate_Bps)
                        try:
                            self.sock.sendto(pkt, self.server_addr)
                            self.forwarded += 1
                        except OSError:
                            pass
            if not got:
                try:
                    select.select([self.sock], [], [], 0.001)
                except (OSError, ValueError):
                    break

    def close(self) -> None:
        self._running = False
        self._thread.join(timeout=1.0)
        self.sock.close()


# ---------------------------------------------------------------------------
# 1. End-to-End ChaCha20-Poly1305 Encryption, Anti-Replay & Forgery Rejection
# ---------------------------------------------------------------------------

def test_chacha20_poly1305_aead_and_replay_window_security(tmp_path: Path) -> None:
    key = generate_nltp_key()
    tx_ctx = AEADCipherContext(key)
    rx_ctx = AEADCipherContext(key)
    wrong_ctx = AEADCipherContext(generate_nltp_key())

    # Encrypt 5 frames, verify out-of-order acceptance within 64-pkt window & strict replay rejection!
    frames = [
        pack_nltp_frame(0x06, 0xCAFE1234, f"cmd_{i}".encode(), flags=0x02, ts_ms=1000 + i, aead=tx_ctx)
        for i in range(5)
    ]
    # Deliver out of order: 2, 0, 4, 1, 3 -> all 5 must succeed!
    for idx in [2, 0, 4, 1, 3]:
        out = unpack_nltp_frame(frames[idx], aead=rx_ctx)
        assert out is not None
        assert out[4] == f"cmd_{idx}".encode()

    # Replay ANY already-accepted packet -> MUST be rejected (`None`)!
    for f in frames:
        assert unpack_nltp_frame(f, aead=rx_ctx) is None

    # Wrong key -> rejected!
    fresh = pack_nltp_frame(0x06, 0xCAFE1234, b"secret", aead=tx_ctx)
    assert unpack_nltp_frame(fresh, aead=wrong_ctx) is None

    # Plaintext downgrade attack against an encrypted endpoint -> strictly rejected!
    plain_downgrade = pack_nltp_frame(0x06, 0xCAFE1234, b"forged_estop", aead=None)
    assert unpack_nltp_frame(plain_downgrade, aead=rx_ctx) is None

    # 1-bit tamper in header AAD OR ciphertext -> strictly rejected!
    tampered_aad = bytearray(fresh)
    tampered_aad[17] ^= 0x01  # flip 1 bit in timestamp AAD
    assert unpack_nltp_frame(bytes(tampered_aad), aead=rx_ctx) is None

    tampered_ct = bytearray(fresh)
    tampered_ct[-5] ^= 0x01
    assert unpack_nltp_frame(bytes(tampered_ct), aead=rx_ctx) is None

    # Live socket E2E encrypted file transfer under 20% packet loss + active attacker injection
    blob = os.urandom(64 * 1024)
    f_src = tmp_path / "encrypted_payload.bin"
    f_src.write_bytes(blob)

    with NLTPServer(("127.0.0.1", 0), save_dir=tmp_path / "rx", psk=key, loss_drop_sim=0.10) as srv:
        s_addr = srv.getsockname()
        # Attacker floods forged plaintext & wrong-key reflex commands at the server port
        atk_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(10):
            atk_sock.sendto(plain_downgrade, s_addr)
            atk_sock.sendto(pack_nltp_frame(0x06, 0x9999, b"FAKE_KILL", aead=wrong_ctx), s_addr)
        atk_sock.close()

        with NLTPClient(s_addr, psk=key, loss_drop_sim=0.12) as cli:
            cli.send_reflex("AUTH_ESTOP_OK")
            rcpt = cli.send_file(f_src, timeout=15.0)
            assert rcpt["verified_sha256"] is True
            assert rcpt["encrypted"] is True

        got = {}
        while True:
            it = srv.recv(timeout=0.3)
            if it is None:
                break
            got[it.kind] = it

        assert srv.engine.auth_drops_rx >= 20
        assert got["reflex"].as_text() == "AUTH_ESTOP_OK"
        assert got["file"].saved_path is not None
        assert got["file"].saved_path.read_bytes() == blob


# ---------------------------------------------------------------------------
# 2. Live Mid-Upload Wi-Fi -> 4G Connection Migration (`rebind_socket`)
# ---------------------------------------------------------------------------

def test_live_wifi_to_4g_connection_migration_mid_upload(tmp_path: Path) -> None:
    """
    Starts uploading a 112 KB encrypted binary file at 4.5 Mbps under 15% packet loss,
    destroys and rebinds the client's OS UDP socket to a brand-new port mid-upload,
    and verifies zero session drop + 100% SHA-256 completion + bidirectional reply!
    """
    key = generate_nltp_key()
    blob = os.urandom(112 * 1024)
    src_file = tmp_path / "drone_map_delta.bin"
    src_file.write_bytes(blob)

    with NLTPServer(("127.0.0.1", 0), save_dir=tmp_path / "srv_rx", psk=key, loss_drop_sim=0.08) as srv:
        with NLTPClient(
            srv.getsockname(),
            save_dir=tmp_path / "cli_rx",
            psk=key,
            max_tx_kbps=4_500.0,
            loss_drop_sim=0.08,
        ) as cli:
            old_port = cli.getsockname()[1]
            result_holder = {}

            def _upload_worker() -> None:
                result_holder["rcpt"] = cli.send_file(src_file, timeout=20.0)

            t = threading.Thread(target=_upload_worker)
            t.start()
            # Wait ~70 ms so ~35% of the file is in flight from `old_port`, then switch network socket!
            time.sleep(0.07)
            new_addr = cli.rebind_socket()
            new_port = new_addr[1]
            t.join(timeout=20.0)

            assert new_port != old_port
            assert result_holder["rcpt"]["verified_sha256"] is True
            assert srv.engine.roam_events >= 1

            rx_item = srv.recv(timeout=5.0)
            assert rx_item is not None and rx_item.kind == "file"
            assert rx_item.data == blob

            # Server can also push an encrypted confirmation message back to the client's NEW roamed address!
            roamed_peer = srv.engine.conn_routes[cli.engine.conn_id]
            assert roamed_peer[1] == new_port
            assert srv.send_bytes(roamed_peer, b"ROAM_ACK_CONFIRMED", timeout=5.0) is True
            cli_msg = cli.recv(timeout=5.0)
            assert cli_msg is not None and cli_msg.data == b"ROAM_ACK_CONFIRMED"


# ---------------------------------------------------------------------------
# 3. Dual Stateful NAT Firewalls + P2P UDP Hole Punching (`punch_p2p`)
# ---------------------------------------------------------------------------

def test_stateful_dual_nat_firewalls_p2p_udp_hole_punching(tmp_path: Path) -> None:
    key = generate_nltp_key()
    nat = DualStatefulNATBox()
    blob = os.urandom(48 * 1024)
    f_path = tmp_path / "p2p_buoy_log.bin"
    f_path.write_bytes(blob)

    try:
        # Prove the stateful NAT firewalls genuinely block unsolicited inbound packets before hole-punching!
        rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rogue.sendto(b"unsolicited_probe", nat.wan_a_addr)
        rogue.sendto(b"unsolicited_probe", nat.wan_b_addr)
        time.sleep(0.03)
        rogue.close()
        assert nat.dropped_unsolicited_a >= 1
        assert nat.dropped_unsolicited_b >= 1

        # Peer A (`NLTPServer`) sits behind NAT A; Peer B (`NLTPClient`) sits behind NAT B.
        # Each sends to its local NAT gateway (`lan_a_addr` / `lan_b_addr`), which only opens
        # inbound WAN pinholes once BOTH sides transmit outbound (`punch_p2p`)!
        with NLTPServer(("127.0.0.1", 0), save_dir=tmp_path / "p2p_rx", psk=key) as srv_a:
            with NLTPClient(nat.lan_b_addr, psk=key) as cli_b:
                punched = {"a": False, "b": False}

                def _punch_a() -> None:
                    punched["a"] = srv_a.punch_p2p(nat.lan_a_addr, timeout_s=3.0)

                def _punch_b() -> None:
                    punched["b"] = cli_b.punch_p2p(nat.lan_b_addr, timeout_s=3.0)

                ta = threading.Thread(target=_punch_a)
                tb = threading.Thread(target=_punch_b)
                ta.start()
                tb.start()
                ta.join(timeout=4.0)
                tb.join(timeout=4.0)

                assert punched["a"] is True
                assert punched["b"] is True

                cli_b.send_reflex("P2P_HOLE_PUNCH_LIVE")
                rcpt = cli_b.send_file(f_path, timeout=15.0)
                assert rcpt["verified_sha256"] is True

                items = {}
                for _ in range(2):
                    it = srv_a.recv(timeout=5.0)
                    assert it is not None
                    items[it.kind] = it
                assert items["reflex"].as_text() == "P2P_HOLE_PUNCH_LIVE"
                assert items["file"].data == blob
    finally:
        nat.close()


# ---------------------------------------------------------------------------
# 4. Strict 4G/5G Symmetric CGNAT Traversal via Zero-Knowledge `NLTPRelayHub`
# ---------------------------------------------------------------------------

def test_symmetric_4g_cgnat_traversal_via_zero_knowledge_relay_hub(tmp_path: Path) -> None:
    secret_key = generate_nltp_key()
    x_aqms, _ = generate_aqms_gas_plume_telemetry(n_steps=400, seed=19)
    blob = os.urandom(64 * 1024)
    src_file = tmp_path / "cgnat_ota_patch.bin"
    src_file.write_bytes(blob)

    with NLTPRelayHub(("127.0.0.1", 0)) as hub:
        hub_addr = hub.getsockname()

        # Both Server (e.g. USV on 4G SIM) and Client (e.g. Operator on 5G hotspot) connect
        # outbound-only to `hub_addr` in `room="usv-alpha-01"`. Neither knows the other's IP:port!
        with NLTPServer(
            ("127.0.0.1", 0),
            save_dir=tmp_path / "relay_rx",
            psk=secret_key,
            relay_addr=hub_addr,
            room="usv-alpha-01",
            loss_drop_sim=0.10,
        ) as srv:
            with NLTPClient(
                relay_addr=hub_addr,
                room="usv-alpha-01",
                psk=secret_key,
                loss_drop_sim=0.10,
            ) as cli:
                # Wait briefly for both outbound REGISTER packets to reach hub
                assert cli.punch_p2p(hub_addr, timeout_s=3.0) is True

                # Intruder joins the exact same public relay room WITHOUT `secret_key` and injects fake packets
                rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                rtag = make_room_tag("usv-alpha-01")
                forged_inner = pack_nltp_frame(0x06, 0xBADF00D, b"HIJACK_THRUSTERS")
                for _ in range(8):
                    rogue.sendto(wrap_relay_packet(rtag, forged_inner, cmd=RELAY_VER_DATA), hub_addr)
                rogue.close()

                # Send urgent reflex, 64 KB file, and 400 ticks of compressed telemetry through RelayHub!
                cli.send_reflex("RELAY_ESTOP_TEST")
                rcpt = cli.send_file(src_file, timeout=20.0)
                assert rcpt["verified_sha256"] is True

                for t in range(len(x_aqms)):
                    cli.send_telemetry(x_aqms[t], profile="sensor_plateau")
                time.sleep(0.05)
                tel_snap = srv.poll_telemetry()
                assert len(tel_snap) >= 1

                items = {}
                for _ in range(2):
                    it = srv.recv(timeout=5.0)
                    assert it is not None
                    items[it.kind] = it

                assert items["reflex"].as_text() == "RELAY_ESTOP_TEST"
                assert items["file"].data == blob
                assert hub.forwarded_packets > 60
                assert srv.engine.auth_drops_rx >= 8  # Intruder's packets 100% blocked!


# ---------------------------------------------------------------------------
# 5. Router Narrow Queue (`32-pkt` FIFO) Survival via `TokenBucketPacer`
# ---------------------------------------------------------------------------

def test_router_narrow_fifo_queue_token_bucket_pacer(tmp_path: Path) -> None:
    key = generate_nltp_key()
    blob = os.urandom(128 * 1024)
    src_file = tmp_path / "paced_128kb.bin"
    src_file.write_bytes(blob)

    with NLTPServer(("127.0.0.1", 0), save_dir=tmp_path / "paced_rx", psk=key) as srv:
        # Strict 12 Mbps bottleneck router with only a 32-packet FIFO buffer!
        router = BottleneckRouterQueue(srv.getsockname(), link_kbps=12_000.0, max_queue_pkts=32)
        try:
            with NLTPClient(router.addr, psk=key, max_tx_kbps=9_500.0) as cli:
                rcpt = cli.send_file(src_file, timeout=15.0)
                assert rcpt["verified_sha256"] is True

            rx_item = srv.recv(timeout=5.0)
            assert rx_item is not None and rx_item.data == blob
            # Verify every single encrypted UDP packet stayed safely below 1200 B Internet MTU!
            assert 0 < router.max_pkt_size_seen <= 1050
            # Verify TokenBucketPacer prevented self-inflicted router FIFO collapse!
            assert router.tail_drops <= 2
        finally:
            router.close()


# ---------------------------------------------------------------------------
# 6. Live Public Internet STUN Integration on Bound `NLTPClient` Socket
# ---------------------------------------------------------------------------

def test_live_public_internet_stun_discovery_on_nltp_socket() -> None:
    with NLTPClient(("127.0.0.1", 9999)) as cli:
        try:
            res = cli.discover_wan(timeout_s=2.0)
        except (TimeoutError, OSError):
            pytest.skip("Public Internet UDP STUN unreachable in this environment")
        assert res.public_port > 0
        assert res.local_port == cli.getsockname()[1]
        assert res.rtt_ms >= 0.0
        assert len(res.servers_queried) >= 1
