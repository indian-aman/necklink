"""Tests for NLTP v1.6.0: zero-dependency LZ4 block codec, smart auto-bypass, and zero-GF(2^8) systematic fast-path."""
from __future__ import annotations

import os
import pytest

from necklink import NLTPClient, NLTPServer, generate_nltp_key, lz4_compress, lz4_decompress
from necklink.nltp import _RxGeneration, _TxGeneration


def test_lz4_codec_roundtrip_overlap_and_fuzz() -> None:
    # 1. Repetitive AQMS CSV/JSON telemetry (7x+ compression)
    rows = [
        f'{{"ts":"2026-11-12T03:{i // 60:02d}:{i % 60:02d}Z","station":"AQMS-01","pm25":{22.1 + (i % 9) * 0.3:.1f},"pm10":54.0,"status":"OK"}}\n'
        for i in range(400)
    ]
    raw = "".join(rows).encode("utf-8")
    comp = lz4_compress(raw)
    assert comp is not None
    assert len(comp) < len(raw) // 5
    assert lz4_decompress(comp, len(raw)) == raw

    # 2. Overlapping RLE matches (off=1 and off=3 < mlen)
    rle = (b"A" * 500) + (b"XYZ" * 300) + b"tail-end-literals"
    comp_rle = lz4_compress(rle)
    assert comp_rle is not None and len(comp_rle) < 80
    assert lz4_decompress(comp_rle, len(rle)) == rle

    # 3. Incompressible high-entropy payload returns None (auto-bypass)
    rnd = os.urandom(16384)
    assert lz4_compress(rnd) is None

    # 4. Fuzz / bounds safety on malformed compressed payloads
    assert lz4_decompress(comp[:-3], len(raw)) is None
    assert lz4_decompress(comp, len(raw) - 1) is None
    assert lz4_decompress(comp, len(raw) + 1) is None
    assert lz4_decompress(b"\x1fA\x00\x00", 20) is None  # zero offset rejected


def test_systematic_fast_path_skips_gf_matrix_allocation() -> None:
    payload = b"0123456789abcdef" * 400  # 6400 B -> 7 shards of 960 B
    tx_g = _TxGeneration(1, 0, payload)
    assert tx_g._data_mat is None  # lazy: not allocated on systematic path!

    rx_g = _RxGeneration(tx_g.k_shards, tx_g.row_width)
    for idx in range(tx_g.k_shards):
        pkt = tx_g.make_systematic_payload(idx)
        shard_buf = pkt[12:]
        done = rx_g.add_shard(idx, shard_buf)
        if idx < tx_g.k_shards - 1:
            assert not done
        else:
            assert done
    assert tx_g._data_mat is None  # still zero numpy matrix allocation!
    assert rx_g._sys_only is True
    assert rx_g.solved_bytes == payload

    # Now verify mixed systematic + Cauchy parity fallback solves identically
    rx_mix = _RxGeneration(tx_g.k_shards, tx_g.row_width)
    for idx in range(tx_g.k_shards - 2):
        rx_mix.add_shard(idx, tx_g.make_systematic_payload(idx)[12:])
    for _ in range(2):
        p_pkt = tx_g.make_cryptic_parity_payload()
        p_idx = p_pkt[9]
        rx_mix.add_shard(p_idx, p_pkt[12:])
    assert tx_g._data_mat is not None
    assert rx_mix._sys_only is False
    assert rx_mix.solved_bytes == payload


def test_encrypted_lz4_transfer_and_incompressible_bypass(tmp_path) -> None:
    psk = generate_nltp_key()
    log_bytes = (
        b"2026-11-12T03:00:00Z,RUT200-STATION-07,PM25=24.8,PM10=58.1,NO2=19.4,O3=33.0,BATT=13.8V\n" * 600
    )
    incomp_bytes = os.urandom(24576)
    src_file = tmp_path / "station_log.csv"
    src_file.write_bytes(log_bytes)

    with NLTPServer(("127.0.0.1", 0), save_dir=tmp_path / "inbox", psk=psk, loss_drop_sim=0.12) as srv:
        port = srv.getsockname()[1]
        with NLTPClient(("127.0.0.1", port), psk=psk, loss_drop_sim=0.12) as cli:
            rec1 = cli.send_file(src_file, compress=True, timeout=15.0)
            assert rec1["verified_sha256"] is True
            assert rec1["compressed"] is True
            assert rec1["bytes"] == len(log_bytes)
            assert rec1["wire_payload_bytes"] < len(log_bytes) // 8

            item1 = srv.recv(timeout=5.0)
            assert item1 is not None
            assert item1.sha256_verified is True
            assert item1.data == log_bytes
            assert item1.saved_path is not None and item1.saved_path.read_bytes() == log_bytes

            # High-entropy payload with compress=True must auto-bypass cleanly without inflation
            rec2 = cli.send_bytes(incomp_bytes, compress=True, timeout=15.0)
            assert rec2["verified_sha256"] is True
            assert rec2["compressed"] is False
            assert rec2["wire_payload_bytes"] == len(incomp_bytes)

            item2 = srv.recv(timeout=5.0)
            assert item2 is not None
            assert item2.sha256_verified is True
            assert item2.data == incomp_bytes
