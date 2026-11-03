"""
Unit & Regression Suite for NeckLink (Zero-Retransmission Wire Codec & Bare-Metal C99).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest

from necklink import (
    NeckLinkConfig,
    NeckLinkDecoder,
    NeckLinkEncoder,
    ReedSolomonGF256Codec,
    SendOnDeltaUDPCodec,
    export_necklink_c99_header,
    simulate_packet_drops,
)
from necklink.workloads import generate_aqms_gas_plume_telemetry, generate_marine_buoy_usv_telemetry


FORBIDDEN_FLOAT_C_TOKENS = re.compile(r"\b(float|double|math\.h|malloc|calloc|realloc|free)\b")


def _nrmse_pct(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    std_per_ch = np.maximum(np.std(y_true, axis=0), 1e-6)
    rmse_per_ch = np.sqrt(np.mean(((y_true - y_pred) / std_per_ch) ** 2, axis=0))
    return float(100.0 * np.mean(rmse_per_ch))


def test_zero_loss_compression_and_exact_twin_mirror_sync():
    x_cal, _ = generate_marine_buoy_usv_telemetry(n_steps=2000, seed=1011)
    mean = np.mean(x_cal, axis=0)
    std = np.std(x_cal, axis=0) + 1e-5
    x, _ = generate_marine_buoy_usv_telemetry(n_steps=1200, seed=11)

    cfg = NeckLinkConfig(n_channels=16)
    enc = NeckLinkEncoder(cfg, obs_mean=mean, obs_std=std)
    dec = NeckLinkDecoder(cfg, obs_mean=mean, obs_std=std)

    raw_bytes = 1200 * (2 + 4 * 16)
    nl_bytes = 0
    rx = np.zeros_like(x)
    for t in range(len(x)):
        pkt = enc.encode_float(x[t])
        if pkt is not None:
            nl_bytes += len(pkt)
        rx[t] = dec.decode_float(pkt)
        # Verify 0-LSB internal mirror synchronization when 0 packets drop!
        assert np.array_equal(enc.m, dec.m), f"Mirror state diverged at t={t}"
        assert np.array_equal(enc.v, dec.v), f"Velocity mirror diverged at t={t}"

    compression_x = raw_bytes / max(1, nl_bytes)
    nrmse = _nrmse_pct(x, rx)
    assert compression_x > 6.0, f"Expected >6x compression over Raw Float32, got {compression_x:.2f}x"
    assert nrmse < 12.0, f"Expected <12% NRMSE at 0% loss, got {nrmse:.2f}%"


def test_50pct_packet_loss_heals_drift_vs_send_on_delta():
    x_cal, _ = generate_aqms_gas_plume_telemetry(n_steps=2400, seed=1022)
    mean = np.mean(x_cal, axis=0)
    std = np.std(x_cal, axis=0) + 1e-5
    x, _ = generate_aqms_gas_plume_telemetry(n_steps=1800, seed=22)
    dropped = simulate_packet_drops(len(x), loss_rate=0.50, mode="random", seed=99)

    cfg = NeckLinkConfig(n_channels=12)
    enc = NeckLinkEncoder(cfg, obs_mean=mean, obs_std=std)
    dec = NeckLinkDecoder(cfg, obs_mean=mean, obs_std=std)
    rx_nl = np.zeros_like(x)
    for t in range(len(x)):
        pkt = enc.encode_float(x[t])
        rx_nl[t] = dec.decode_float(None if (pkt is None or dropped[t]) else pkt)

    sod = SendOnDeltaUDPCodec(n_channels=12, obs_mean=mean, obs_std=std, deadband_q8=cfg.deadband_q8, quant_step_q8=cfg.quant_step_q8)
    rx_sod, _, _ = sod.run_stream(x, dropped)

    err_nl = _nrmse_pct(x, rx_nl)
    err_sod = _nrmse_pct(x, rx_sod)
    assert dec.recovered_echo_packets > 10, "Cryptic echo should actively recover lost packets"
    assert err_nl < err_sod * 0.65, f"NeckLink ({err_nl:.2f}%) did not beat SendOnDelta ({err_sod:.2f}%) under 50% loss"


def test_reed_solomon_gf256_exact_recovery_below_cliff():
    x, _ = generate_marine_buoy_usv_telemetry(n_steps=240, seed=7)
    mean = np.mean(x, axis=0)
    std = np.std(x, axis=0) + 1e-5
    rs = ReedSolomonGF256Codec(n_channels=16, obs_mean=mean, obs_std=std, k_data=8, m_parity=4)

    no_loss = np.zeros(500, dtype=bool)
    rx_0, _, _ = rs.run_stream(x, no_loss)

    # Drop exactly 4 shards out of every 12-shard block (33.3% deterministic erasure <= m_parity)
    det_loss = np.zeros(500, dtype=bool)
    for b in range(0, 500, 12):
        det_loss[b : min(500, b + 4)] = True
    rx_33, _, _ = rs.run_stream(x, det_loss)
    assert np.allclose(rx_0, rx_33, atol=1e-9), "GF(256) RS(12,8) failed exact erasure recovery at 4/12 loss"


def test_zero_float_c99_header_token_purity(tmp_path: Path):
    cfg = NeckLinkConfig(n_channels=16)
    hdr_path, max_pkt = export_necklink_c99_header(cfg, tmp_path / "necklink_buoy_int8.h", prefix="nl_buoy")
    assert hdr_path.exists()
    assert 20 <= max_pkt <= 64

    txt = hdr_path.read_text(encoding="utf-8")
    no_comments = re.sub(r"/\*.*?\*/", "", txt, flags=re.DOTALL)
    no_comments = re.sub(r"//.*", "", no_comments)
    m = FORBIDDEN_FLOAT_C_TOKENS.search(no_comments)
    assert m is None, f"Forbidden float/heap token {m.group(0)!r} found in {hdr_path.name}"


def test_empirical_g4_benchmarks_if_present():
    repo_root = Path(__file__).resolve().parent.parent
    res_dir = repo_root / "results"
    tel_json = res_dir / "telemetry_benchmarks.json"
    ai_json = res_dir / "split_ai_gpu_benchmarks.json"
    c_json = res_dir / "baremetal_c99_wire_audit.json"

    if not (tel_json.exists() and ai_json.exists() and c_json.exists()):
        pytest.skip("Empirical G4 benchmark results not yet downloaded")

    c_data = json.loads(c_json.read_text(encoding="utf-8"))
    for item in c_data["compiled_codecs"].values():
        assert item["gcc_mgeneral_regs_only_passed"] is True
        assert item["fp_instruction_count_objdump"] == 0
        assert item["python_vs_c_packet_byte_mismatches"] == 0
        assert item["python_vs_c_decode_max_lsb_diff"] == 0
        assert item["total_sram_bytes"] < 1024
