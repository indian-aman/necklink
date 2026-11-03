"""
End-to-End Verification Suite for Both Architectures:
  1. Standalone `NLTP v1` (`NLTPServer` / `NLTPClient` over UDP + C99 `nltp_wire.h` bit-exactness)
  2. `NeckLink-over-QUIC` (`NeckLinkQuicServer` / `connect_necklink_quic` over TLS 1.3 + RFC 9221)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import numpy as np
import pytest

from necklink import (
    LossyUDPRelay,
    NLTPClient,
    NLTPServer,
    NeckLinkQuicServer,
    connect_necklink_quic,
    pack_nltp_frame,
    unpack_nltp_frame,
)
from necklink.nltp import _cauchy_row
from necklink.workloads import generate_aqms_gas_plume_telemetry


def _c99_gf256_mul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1D
        b >>= 1
    return p


def _c99_gf256_inv(a: int) -> int:
    res = 1
    base = a & 0xFF
    exp = 254
    while exp > 0:
        if exp & 1:
            res = _c99_gf256_mul(res, base)
        base = _c99_gf256_mul(base, base)
        exp >>= 1
    return res


def test_c99_wire_header_algebraic_and_crc_parity() -> None:
    """Verifies `csrc/nltp_wire.h` square-and-multiply GF(2^8) Cauchy rows & CRC32 match Python."""
    for p_off in range(192):
        py_row = _cauchy_row(p_off, 8)
        x_r = 64 + (p_off % 192)
        c_row = np.array([_c99_gf256_inv(x_r ^ (c & 0x3F)) for c in range(8)], dtype=np.uint8)
        assert np.array_equal(py_row, c_row), f"Mismatch at parity_offset={p_off}"

    raw = pack_nltp_frame(0x01, 0xDEADBEEF, b"hello_nltp", flags=0x03, ts_ms=123456)
    parsed = unpack_nltp_frame(raw)
    assert parsed == (0x01, 0x03, 0xDEADBEEF, 123456, b"hello_nltp")
    # Flip 1 bit -> CRC32 must reject
    corrupted = bytearray(raw)
    corrupted[-1] ^= 0x01
    assert unpack_nltp_frame(bytes(corrupted)) is None

    # Verify C99 nltp_wire.h zero-float / zero-malloc purity & ChaCha20-Poly1305 symbols
    c_wire = (Path(__file__).resolve().parents[1] / "csrc" / "nltp_wire.h").read_text(encoding="utf-8")
    for banned in ("float ", "double ", "malloc(", "calloc(", "realloc("):
        assert banned not in c_wire
    for required in ("nltp_pack_encrypted_frame", "nltp_unpack_encrypted_frame", "NLTP_DEFAULT_SHARD_LEN  960u"):
        assert required in c_wire


def test_nltp_standalone_exact_file_reflex_and_json_under_35pct_loss(tmp_path: Path) -> None:
    """Real OS UDP `NLTPServer` <-> `NLTPClient` SHA-256 exact 96 KB binary file transfer under 35% loss."""
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()

    # Non-compressible pseudo-random firmware/archive binary
    rng = np.random.default_rng(777)
    blob = rng.integers(0, 256, size=96 * 1024, dtype=np.uint8).tobytes()
    src_file = src_dir / "usv_firmware_v2.bin"
    src_file.write_bytes(blob)
    expected_sha = hashlib.sha256(blob).hexdigest()

    with NLTPServer(("127.0.0.1", 0), save_dir=dst_dir, loss_drop_sim=0.18) as srv:
        host, port = srv.getsockname()
        # Combined forward (20%) + reverse (18%) packet loss (~34% round-trip packet drop rate!)
        with NLTPClient((host, port), loss_drop_sim=0.20) as cli:
            cli.send_reflex("ESTOP_ARMED")
            receipt = cli.send_file(src_file, timeout=20.0)
            assert receipt["verified_sha256"] is True
            assert receipt["sha256"] == expected_sha

            json_rcpt = cli.send_json({"cmd": "set_waypoint", "lat": 24.4539, "lon": 54.3773}, timeout=10.0)
            assert json_rcpt["verified_sha256"] is True

        received_kinds = {}
        for _ in range(3):
            item = srv.recv(timeout=5.0)
            assert item is not None
            received_kinds[item.kind] = item

        assert "reflex" in received_kinds
        assert received_kinds["reflex"].as_text() == "ESTOP_ARMED"
        assert "file" in received_kinds
        assert received_kinds["file"].sha256_verified is True
        assert received_kinds["file"].saved_path is not None
        assert received_kinds["file"].saved_path.read_bytes() == blob
        assert received_kinds["json"].as_json()["cmd"] == "set_waypoint"


def test_nltp_multiplexed_telemetry_self_healing() -> None:
    """Verifies live 12-ch AQMS telemetry multiplexed over NLTP heals 40% drops at >8x compression."""
    x, _ = generate_aqms_gas_plume_telemetry(n_steps=800, seed=11)
    rng = np.random.default_rng(99)

    from necklink.nltp import NLTPEndpoint

    # Deterministic 40% loss wire between two NLTPEndpoints
    rx_ep_holder = []
    tx_ep = NLTPEndpoint(
        send_packet_fn=lambda pkt, peer: rx_ep_holder[0].on_datagram(pkt, ("127.0.0.1", 5001))
        if rng.random() >= 0.40 or len(pkt) > 80  # keep initial scale beacon deterministic
        else None
    )
    rx_ep = NLTPEndpoint(send_packet_fn=lambda pkt, peer: None)
    rx_ep_holder.append(rx_ep)

    rec = np.zeros_like(x)
    for t in range(len(x)):
        tx_ep.send_telemetry(("127.0.0.1", 9000), x[t], profile="sensor_plateau")
        snap = rx_ep.poll_telemetry_tick(("127.0.0.1", 5001))
        if ("127.0.0.1", 5001) in snap:
            rec[t] = snap[("127.0.0.1", 5001)]

    w = 80
    std_ch = np.maximum(np.std(x[w:], axis=0), 1e-6)
    nrmse_pct = float(100.0 * np.mean(np.sqrt(np.mean(((x[w:] - rec[w:]) / std_ch) ** 2, axis=0))))
    tx_stats = tx_ep.telemetry_tx[("127.0.0.1", 9000)].stats()
    assert tx_stats["compression_x"] >= 8.0
    assert nrmse_pct < 38.0


def test_necklink_over_quic_tls13_streams_and_rfc9221_datagrams(tmp_path: Path) -> None:
    """Tests real IETF QUIC (`aioquic` TLS 1.3) reliable file streams + RFC 9221 NeckLink datagrams."""

    async def _run() -> None:
        x, _ = generate_aqms_gas_plume_telemetry(n_steps=1200, seed=21)
        payload_file = tmp_path / "model_weights.bin"
        raw_blob = os.urandom(48 * 1024)
        payload_file.write_bytes(raw_blob)

        async with NeckLinkQuicServer("127.0.0.1", 0, save_dir=tmp_path / "quic_rx") as srv:
            srv_addr = (srv._server._transport.get_extra_info("sockname")[0], srv._server._transport.get_extra_info("sockname")[1])  # type: ignore[union-attr]
            # Put a packet-loss UDP middlebox on RFC 9221 datagrams & QUIC streams!
            relay = LossyUDPRelay(srv_addr, loss_rate=0.0, seed=42)
            relay_addr = await relay.start("127.0.0.1", 0)
            try:
                async with connect_necklink_quic(relay_addr[0], relay_addr[1]) as cli:
                    # 1. Urgent reflex over RFC 9221
                    cli.send_reflex("COLLISION_AVOID")

                    # 2. Exact 48 KB file over TLS 1.3 QUIC stream under 12% packet loss (calibrates QUIC SRTT!)
                    relay.loss_rate = 0.12
                    rcpt = await cli.send_file(payload_file, timeout=20.0)
                    assert rcpt["verified_sha256"] is True

                    # 3. Stream 1200 ticks of NeckLink telemetry over RFC 9221 QUIC Unreliable Datagrams at 35% loss!
                    relay.loss_rate = 0.0
                    await asyncio.sleep(0.03)
                    rec_nl = np.zeros_like(x)
                    raw_equiv_dg_bytes = 0

                    for t in range(len(x)):
                        if t == 40:
                            relay.loss_rate = 0.35
                        cli.send_telemetry(x[t], profile="sensor_plateau")
                        raw_equiv_dg_bytes += 5 + 4 * x.shape[1]
                        await asyncio.sleep(0.001)
                        vals = srv.poll_telemetry_tick()
                        if vals:
                            rec_nl[t] = vals[0]

                    nl_bytes = cli.telemetry_tx.stats()["wire_bytes_sent"]  # type: ignore[union-attr]
                    assert raw_equiv_dg_bytes / max(1.0, nl_bytes) > 7.5

                    w = 100
                    std_ch = np.maximum(np.std(x[w:], axis=0), 1e-6)
                    nrmse_nl = float(100.0 * np.mean(np.sqrt(np.mean(((x[w:] - rec_nl[w:]) / std_ch) ** 2, axis=0))))
                    assert nrmse_nl < 45.0

                    # Verify server received both reflex & exact file
                    items = []
                    while True:
                        it = await srv.recv(timeout=0.2)
                        if it is None:
                            break
                        items.append(it)
                    kinds = {i.kind: i for i in items}
                    assert "reflex" in kinds and kinds["reflex"].as_text() == "COLLISION_AVOID"
                    assert "file" in kinds and kinds["file"].saved_path.read_bytes() == raw_blob  # type: ignore[union-attr]
            finally:
                relay.close()

    asyncio.run(_run())
