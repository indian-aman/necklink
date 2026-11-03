"""
Master Empirical Benchmark & Bare-Metal C99 Audit for NeckLink (G4 GPU + GCC C99).

Executes:
  1. Multi-seed real-world telemetry & commercial link-economics benchmarks across
     3 domains (Smart Buoy/USV Iridium SBD, City AQMS LoRaWAN, Rural Tele-ECG 2G)
     x 10 codecs/ablations x 7 loss regimes (0%..65% random + 45% Gilbert-Elliott bursts).
  2. GPU-trained Edge-to-Cloud Split-AI sequential latent streaming across lossy UDP.
  3. Zero-float ANSI C99 (`gcc -std=c99 -O3 -Wall -Wextra -Werror -mgeneral-regs-only`)
     compilation, `objdump -d` FPU-zero audit, SRAM/Flash footprint, throughput, and
     live `ctypes` Python<->C cross-language 0-LSB wire compatibility under 50% loss.
"""
from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from necklink import (
    ADPCMLeakyUDPCodec,
    Int8ZlibBlockCodec,
    NeckLinkConfig,
    NeckLinkDecoder,
    NeckLinkEncoder,
    NeckLinkSplitBottleneck,
    RawFloat32UDPCodec,
    ReedSolomonGF256Codec,
    SendOnDeltaUDPCodec,
    TCPARQAnalyticalEvaluator,
    compute_link_economics,
    export_necklink_c99_header,
    measure_empirical_connectome_neck_stats,
    simulate_packet_drops,
)
from necklink.workloads import (
    generate_aqms_gas_plume_telemetry,
    generate_marine_buoy_usv_telemetry,
    generate_rural_tele_ecg_wearable,
    generate_split_ai_sequence_dataset,
)


LOSS_CONDITIONS: List[Tuple[str, float, str]] = [
    ("loss_0pct", 0.00, "random"),
    ("loss_10pct_rand", 0.10, "random"),
    ("loss_25pct_rand", 0.25, "random"),
    ("loss_40pct_rand", 0.40, "random"),
    ("loss_50pct_rand", 0.50, "random"),
    ("loss_65pct_rand", 0.65, "random"),
    ("burst_45pct_ge", 0.45, "gilbert_elliott"),
]


def compute_signal_metrics(
    x_true: np.ndarray,
    x_rec: np.ndarray,
    spike_mask: np.ndarray | None = None,
) -> Dict[str, float]:
    # Evaluate post-warmup (skip first 5% transient)
    w = max(10, len(x_true) // 20)
    yt = x_true[w:]
    yp = x_rec[w:]
    ch_std = np.maximum(np.std(yt, axis=0), 1e-6)
    nrmse_ch = np.sqrt(np.mean(((yt - yp) / ch_std) ** 2, axis=0))
    nrmse_pct = float(100.0 * np.mean(nrmse_ch))

    corrs = []
    for c in range(yt.shape[1]):
        if np.std(yp[:, c]) < 1e-9 or np.std(yt[:, c]) < 1e-9:
            corrs.append(0.0)
        else:
            r_val = float(np.corrcoef(yt[:, c], yp[:, c])[0, 1])
            corrs.append(0.0 if np.isnan(r_val) else r_val)
    mean_r = float(np.mean(corrs))

    # Normalized DC bias drift (%)
    dc_drift_pct = float(100.0 * np.mean(np.abs(np.mean(yp - yt, axis=0)) / ch_std))

    out = {
        "nrmse_pct": round(nrmse_pct, 3),
        "pearson_r": round(mean_r, 4),
        "dc_drift_pct": round(dc_drift_pct, 3),
    }

    if spike_mask is not None:
        sm = spike_mask[w:]
        if np.any(sm):
            sp_nrmse = float(100.0 * np.mean(np.sqrt(np.mean(((yt[sm] - yp[sm]) / ch_std) ** 2, axis=0))))
            out["spike_event_nrmse_pct"] = round(sp_nrmse, 3)
    return out


def run_necklink_stream(
    x_seq: np.ndarray,
    dropped: np.ndarray,
    mean: np.ndarray,
    std: np.ndarray,
    enable_mirror: bool = True,
    enable_echo: bool = True,
    enable_brake_gate: bool = True,
    deadband_q8: int = 32,
    quant_step_q8: int = 8,
) -> Tuple[np.ndarray, int, int, Dict[str, float]]:
    cfg = NeckLinkConfig(
        n_channels=x_seq.shape[1],
        deadband_q8=deadband_q8,
        quant_step_q8=quant_step_q8,
        enable_mirror=enable_mirror,
        enable_echo=enable_echo,
        enable_brake_gate=enable_brake_gate,
    )
    enc = NeckLinkEncoder(cfg, obs_mean=mean, obs_std=std)
    dec = NeckLinkDecoder(cfg, obs_mean=mean, obs_std=std)

    rx = np.zeros_like(x_seq, dtype=np.float64)
    total_bytes = 0
    tx_pkts = 0
    gates: List[float] = []

    for t in range(len(x_seq)):
        pkt = enc.encode_float(x_seq[t])
        if pkt is not None:
            total_bytes += len(pkt)
            tx_pkts += 1
            delivered = None if bool(dropped[t]) else pkt
        else:
            delivered = None
        rx[t] = dec.decode_float(delivered)
        gates.append(dec.brake_gate_q8 / 256.0)

    diag = {
        "recovered_echo_packets": int(dec.recovered_echo_packets),
        "detected_lost_packets": int(dec.detected_lost_packets),
        "mean_brake_gate": round(float(np.mean(gates)), 4),
    }
    return rx, total_bytes, tx_pkts, diag


def stage1_telemetry_and_economics() -> Dict[str, Any]:
    print("=== Stage 1: Multi-Seed Telemetry & Commercial Link Economics ===")
    t0 = time.perf_counter()
    seeds = [101, 202, 303, 404, 505]
    workloads_spec = [
        ("marine_buoy_usv", generate_marine_buoy_usv_telemetry, 3000),
        ("city_aqms_plume", generate_aqms_gas_plume_telemetry, 3600),
        ("rural_tele_ecg", generate_rural_tele_ecg_wearable, 4000),
    ]

    protocol_names = [
        "Raw-Float32-UDP",
        "Int8-Zlib-Block16",
        "SendOnDelta-UDP",
        "ADPCM-4bit-Leaky",
        "ReedSolomon-RS12_8",
        "TCP-ARQ-Reliable",
        "NeckLink-Full",
        "NeckLink-NoEcho",
        "NeckLink-NoBrakeGate",
        "NeckLink-NoMirror",
    ]

    results: Dict[str, Any] = {
        "connectome_priors": measure_empirical_connectome_neck_stats(),
        "seeds": seeds,
        "workloads": {},
    }

    for wl_key, gen_fn, n_steps in workloads_spec:
        # Collect per-protocol, per-condition lists across seeds
        agg: Dict[str, Dict[str, List[Dict[str, float]]]] = {
            p: {cond_name: [] for cond_name, _, _ in LOSS_CONDITIONS} for p in protocol_names
        }
        wl_meta_ref: Dict[str, Any] = {}

        for s_idx, seed in enumerate(seeds):
            x_cal, _ = gen_fn(n_steps=2400, seed=seed + 1000)
            mean = np.mean(x_cal, axis=0)
            std = np.std(x_cal, axis=0) + 1e-5

            x_seq, meta = gen_fn(n_steps=n_steps, seed=seed)
            if s_idx == 0:
                wl_meta_ref = {k: v for k, v in meta.items() if k != "spike_mask"}
            spike_mask = meta.get("spike_mask", None)
            dur_s = float(meta["duration_s"])
            rtt_ms = float(meta["rtt_ms"])
            sample_dt_ms = 1000.0 / float(meta["sample_rate_hz"])
            link_type = str(meta["link_type"])
            n_ch = x_seq.shape[1]

            raw_codec = RawFloat32UDPCodec(n_ch)
            zlib_codec = Int8ZlibBlockCodec(n_ch, mean, std, block_len=16)
            sod_codec = SendOnDeltaUDPCodec(n_ch, mean, std, deadband_q8=32, quant_step_q8=8)
            adpcm_codec = ADPCMLeakyUDPCodec(n_ch, mean, std)
            rs_codec = ReedSolomonGF256Codec(n_ch, mean, std, k_data=8, m_parity=4)
            tcp_eval = TCPARQAnalyticalEvaluator(n_ch, mean, std, rtt_ms=rtt_ms, sample_dt_ms=sample_dt_ms)

            for cond_name, loss_rate, loss_mode in LOSS_CONDITIONS:
                # Generate ample drop mask (RS12_8 sends 1.5x packets)
                dropped = simulate_packet_drops(
                    int(n_steps * 2),
                    loss_rate=loss_rate,
                    mode=loss_mode,
                    mean_burst_len=9.0,
                    seed=seed * 31 + hash(cond_name) % 997,
                )
                drop_main = dropped[:n_steps]
                raw_ref_bytes = n_steps * (2 + 4 * n_ch)

                # 1. Raw-Float32-UDP
                rx_raw, b_raw, pk_raw = raw_codec.run_stream(x_seq, drop_main)
                # 2. Int8-Zlib-Block16
                rx_zl, b_zl, pk_zl = zlib_codec.run_stream(x_seq, drop_main)
                # 3. SendOnDelta-UDP
                rx_sod, b_sod, pk_sod = sod_codec.run_stream(x_seq, drop_main)
                # 4. ADPCM-4bit-Leaky
                rx_adp, b_adp, pk_adp = adpcm_codec.run_stream(x_seq, drop_main)
                # 5. ReedSolomon-RS12_8
                rx_rs, b_rs, pk_rs = rs_codec.run_stream(x_seq, dropped)
                # 6. TCP-ARQ-Reliable
                tcp_out = tcp_eval.run_stream(x_seq, drop_main, seed=seed)

                # 7..10. NeckLink & Ablations
                rx_nl, b_nl, pk_nl, d_nl = run_necklink_stream(x_seq, drop_main, mean, std, True, True, True)
                rx_ne, b_ne, pk_ne, _ = run_necklink_stream(x_seq, drop_main, mean, std, True, False, True)
                rx_ng, b_ng, pk_ng, _ = run_necklink_stream(x_seq, drop_main, mean, std, True, True, False)
                rx_nm, b_nm, pk_nm, _ = run_necklink_stream(x_seq, drop_main, mean, std, False, True, True)

                runs_map = [
                    ("Raw-Float32-UDP", rx_raw, b_raw, pk_raw, 0.0, 0.0, {}),
                    ("Int8-Zlib-Block16", rx_zl, b_zl, pk_zl, 15.0 * sample_dt_ms, 15.0 * sample_dt_ms, {}),
                    ("SendOnDelta-UDP", rx_sod, b_sod, pk_sod, 0.0, 0.0, {}),
                    ("ADPCM-4bit-Leaky", rx_adp, b_adp, pk_adp, 0.0, 0.0, {}),
                    ("ReedSolomon-RS12_8", rx_rs, b_rs, pk_rs, 7.0 * sample_dt_ms, 7.0 * sample_dt_ms, {}),
                    (
                        "TCP-ARQ-Reliable",
                        tcp_out["realtime_rx"],
                        int(tcp_out["total_bytes"]),
                        int(tcp_out["tx_pkts"]),
                        float(tcp_out["mean_stall_ms"]),
                        float(tcp_out["p99_stall_ms"]),
                        {},
                    ),
                    ("NeckLink-Full", rx_nl, b_nl, pk_nl, 0.0, 0.0, d_nl),
                    ("NeckLink-NoEcho", rx_ne, b_ne, pk_ne, 0.0, 0.0, {}),
                    ("NeckLink-NoBrakeGate", rx_ng, b_ng, pk_ng, 0.0, 0.0, {}),
                    ("NeckLink-NoMirror", rx_nm, b_nm, pk_nm, 0.0, 0.0, {}),
                ]

                for p_name, rx_arr, b_cnt, pk_cnt, m_stall, p99_stall, extra_d in runs_map:
                    sig_m = compute_signal_metrics(x_seq, rx_arr, spike_mask=spike_mask)
                    econ = compute_link_economics(b_cnt, pk_cnt, dur_s, link_type)
                    entry = {
                        **sig_m,
                        **econ,
                        "total_bytes": float(b_cnt),
                        "bytes_per_step": round(b_cnt / float(n_steps), 3),
                        "compression_vs_raw_x": round(raw_ref_bytes / max(1.0, float(b_cnt)), 2),
                        "tx_packets": float(pk_cnt),
                        "silent_tick_pct": round(100.0 * max(0.0, 1.0 - pk_cnt / float(n_steps)), 2),
                        "mean_stall_ms": round(m_stall, 2),
                        "p99_stall_ms": round(p99_stall, 2),
                    }
                    for k_ex, v_ex in extra_d.items():
                        entry[k_ex] = float(v_ex)
                    agg[p_name][cond_name].append(entry)

        # Summarize mean & SEM across seeds
        wl_summary: Dict[str, Any] = {"metadata": wl_meta_ref, "protocols": {}}
        for p_name in protocol_names:
            wl_summary["protocols"][p_name] = {}
            for cond_name, _, _ in LOSS_CONDITIONS:
                rows = agg[p_name][cond_name]
                keys = list(rows[0].keys())
                cond_stat: Dict[str, float] = {}
                for k in keys:
                    vals = np.array([r[k] for r in rows], dtype=np.float64)
                    cond_stat[k] = round(float(np.mean(vals)), 3)
                    if k in ("nrmse_pct", "pearson_r", "spike_event_nrmse_pct", "compression_vs_raw_x"):
                        cond_stat[f"{k}_sem"] = round(float(np.std(vals, ddof=1) / np.sqrt(len(vals))), 3)
                wl_summary["protocols"][p_name][cond_name] = cond_stat

        results["workloads"][wl_key] = wl_summary
        nl_0 = wl_summary["protocols"]["NeckLink-Full"]["loss_0pct"]
        nl_50 = wl_summary["protocols"]["NeckLink-Full"]["loss_50pct_rand"]
        sod_50 = wl_summary["protocols"]["SendOnDelta-UDP"]["loss_50pct_rand"]
        raw_0 = wl_summary["protocols"]["Raw-Float32-UDP"]["loss_0pct"]
        print(
            f"  [{wl_key}] NeckLink 0%-loss: {nl_0['compression_vs_raw_x']}x smaller "
            f"({nl_0['bytes_per_step']} B/step vs {raw_0['bytes_per_step']} B/step), "
            f"50%-loss NRMSE: NeckLink={nl_50['nrmse_pct']}% vs SendOnDelta={sod_50['nrmse_pct']}%"
        )

    results["wall_seconds"] = round(time.perf_counter() - t0, 2)
    return results


def stage2_gpu_split_ai_streaming() -> Tuple[Dict[str, Any], np.ndarray, np.ndarray]:
    print("=== Stage 2: GPU Edge-to-Cloud Split-AI Latent Feature Streaming ===")
    t0 = time.perf_counter()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"  Training Split-AI models on device: {device}")

    seeds = [11, 22, 33]
    split_conds = [
        ("loss_0pct", 0.00, "random"),
        ("loss_25pct_rand", 0.25, "random"),
        ("loss_50pct_rand", 0.50, "random"),
        ("loss_65pct_rand", 0.65, "random"),
        ("burst_45pct_ge", 0.45, "gilbert_elliott"),
    ]
    proto_list = [
        "Raw-Float32-UDP",
        "Int8-Zlib-Block16",
        "SendOnDelta-UDP",
        "ADPCM-4bit-Leaky",
        "ReedSolomon-RS12_8",
        "NeckLink-Full",
        "NeckLink-NoEcho",
        "NeckLink-NoBrakeGate",
        "NeckLink-NoMirror",
    ]
    agg: Dict[str, Dict[str, List[Dict[str, float]]]] = {
        p: {c[0]: [] for c in split_conds} for p in proto_list
    }

    saved_latent_mean = np.zeros(32, dtype=np.float64)
    saved_latent_std = np.ones(32, dtype=np.float64)

    for s_idx, seed in enumerate(seeds):
        torch.manual_seed(seed)
        np.random.seed(seed)
        x_tr, y_tr = generate_split_ai_sequence_dataset(n_sequences=1920, seq_len=64, in_dim=24, seed=seed)
        x_te, y_te = generate_split_ai_sequence_dataset(n_sequences=320, seq_len=64, in_dim=24, seed=seed + 5000)

        model = NeckLinkSplitBottleneck(in_dim=24, bottleneck_dim=32, num_classes=8).to(device)
        opt = torch.optim.AdamW(model.parameters(), lr=2.5e-3, weight_decay=1e-4)

        x_tr_t = torch.from_numpy(x_tr).to(device)
        y_tr_t = torch.from_numpy(y_tr).to(device)

        # Add tiny smoothness regularization on temporal latent deltas so edge features are physical
        model.train()
        bs = 128
        for _epoch in range(28):
            perm = torch.randperm(len(x_tr_t), device=device)
            for b0 in range(0, len(x_tr_t), bs):
                idx = perm[b0 : b0 + bs]
                xb = x_tr_t[idx]
                yb = y_tr_t[idx]
                z_seq = model.encode_edge(xb)
                logits = model.classify_cloud(z_seq)
                ce = F.cross_entropy(logits.reshape(-1, 8), yb.reshape(-1))
                smooth = torch.mean((z_seq[:, 1:, :] - z_seq[:, :-1, :]) ** 2)
                loss = ce + 0.08 * smooth
                opt.zero_grad()
                loss.backward()
                opt.step()

        model.eval()
        with torch.no_grad():
            z_cal = model.encode_edge(x_tr_t[:256]).cpu().numpy().reshape(-1, 32)
            lat_mean = np.mean(z_cal, axis=0).astype(np.float64)
            lat_std = (np.std(z_cal, axis=0) + 1e-5).astype(np.float64)
            if s_idx == 0:
                saved_latent_mean = lat_mean.copy()
                saved_latent_std = lat_std.copy()

            z_test = model.encode_edge(torch.from_numpy(x_te).to(device)).cpu().numpy()  # (320, 64, 32)

        # Concatenate test sequences into continuous edge feature stream (or evaluate sequence-by-sequence)
        # Evaluating per-sequence (64 steps) reflects real independent edge inference sessions;
        # concatenating 20 sequences per session (1,280 steps = 64s @ 20Hz) reflects continuous drone/USV camera link!
        n_sess = 16
        seqs_per_sess = len(z_test) // n_sess  # 20 sequences * 64 = 1280 steps per session
        z_sessions = z_test.reshape(n_sess, seqs_per_sess * 64, 32)
        y_sessions = y_te.reshape(n_sess, seqs_per_sess * 64)
        active_event_mask = y_sessions > 0  # Evaluate hazard classification accuracy on active event steps

        raw_codec = RawFloat32UDPCodec(32)
        zlib_codec = Int8ZlibBlockCodec(32, lat_mean, lat_std, block_len=16)
        sod_codec = SendOnDeltaUDPCodec(32, lat_mean, lat_std, deadband_q8=24, quant_step_q8=8)
        adpcm_codec = ADPCMLeakyUDPCodec(32, lat_mean, lat_std)
        rs_codec = ReedSolomonGF256Codec(32, lat_mean, lat_std, k_data=8, m_parity=4)

        for cond_name, loss_rate, loss_mode in split_conds:
            rec_streams: Dict[str, List[np.ndarray]] = {p: [] for p in proto_list}
            byte_totals: Dict[str, int] = {p: 0 for p in proto_list}
            total_steps = n_sess * seqs_per_sess * 64
            raw_total_ref = total_steps * (2 + 4 * 32)

            for sess_i in range(n_sess):
                zs = z_sessions[sess_i].astype(np.float64)
                t_len = len(zs)
                dropped = simulate_packet_drops(
                    t_len * 2,
                    loss_rate=loss_rate,
                    mode=loss_mode,
                    mean_burst_len=8.0,
                    seed=seed * 101 + sess_i * 17 + hash(cond_name) % 503,
                )
                d_main = dropped[:t_len]

                r_raw, b_raw, _ = raw_codec.run_stream(zs, d_main)
                r_zl, b_zl, _ = zlib_codec.run_stream(zs, d_main)
                r_sod, b_sod, _ = sod_codec.run_stream(zs, d_main)
                r_adp, b_adp, _ = adpcm_codec.run_stream(zs, d_main)
                r_rs, b_rs, _ = rs_codec.run_stream(zs, dropped)
                r_nl, b_nl, _, _ = run_necklink_stream(zs, d_main, lat_mean, lat_std, True, True, True, deadband_q8=24, quant_step_q8=8)
                r_ne, b_ne, _, _ = run_necklink_stream(zs, d_main, lat_mean, lat_std, True, False, True, deadband_q8=24, quant_step_q8=8)
                r_ng, b_ng, _, _ = run_necklink_stream(zs, d_main, lat_mean, lat_std, True, True, False, deadband_q8=24, quant_step_q8=8)
                r_nm, b_nm, _, _ = run_necklink_stream(zs, d_main, lat_mean, lat_std, False, True, True, deadband_q8=24, quant_step_q8=8)

                for p_k, r_arr, b_c in [
                    ("Raw-Float32-UDP", r_raw, b_raw),
                    ("Int8-Zlib-Block16", r_zl, b_zl),
                    ("SendOnDelta-UDP", r_sod, b_sod),
                    ("ADPCM-4bit-Leaky", r_adp, b_adp),
                    ("ReedSolomon-RS12_8", r_rs, b_rs),
                    ("NeckLink-Full", r_nl, b_nl),
                    ("NeckLink-NoEcho", r_ne, b_ne),
                    ("NeckLink-NoBrakeGate", r_ng, b_ng),
                    ("NeckLink-NoMirror", r_nm, b_nm),
                ]:
                    rec_streams[p_k].append(r_arr.astype(np.float32))
                    byte_totals[p_k] += int(b_c)

            for p_k in proto_list:
                z_rec_all = np.stack(rec_streams[p_k], axis=0).reshape(len(z_test), 64, 32)
                with torch.no_grad():
                    logits = model.classify_cloud(torch.from_numpy(z_rec_all).to(device)).cpu().numpy()
                preds = np.argmax(logits, axis=-1).reshape(n_sess, -1)
                overall_acc = float(100.0 * np.mean(preds == y_sessions))
                hazard_acc = float(100.0 * np.mean(preds[active_event_mask] == y_sessions[active_event_mask]))
                lat_nrmse = float(
                    100.0
                    * np.mean(
                        np.sqrt(np.mean(((z_test - z_rec_all) / lat_std.reshape(1, 1, 32)) ** 2, axis=(0, 1)))
                    )
                )
                b_tot = byte_totals[p_k]
                agg[p_k][cond_name].append(
                    {
                        "overall_accuracy_pct": round(overall_acc, 2),
                        "hazard_event_accuracy_pct": round(hazard_acc, 2),
                        "latent_nrmse_pct": round(lat_nrmse, 2),
                        "bytes_per_step": round(b_tot / float(total_steps), 2),
                        "compression_vs_raw_x": round(raw_total_ref / max(1.0, float(b_tot)), 2),
                    }
                )

    summary: Dict[str, Any] = {
        "device": str(device),
        "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU",
        "bottleneck_dim": 32,
        "num_classes": 8,
        "seeds": seeds,
        "protocols": {},
    }
    for p_k in proto_list:
        summary["protocols"][p_k] = {}
        for cond_name, _, _ in split_conds:
            rows = agg[p_k][cond_name]
            stat: Dict[str, float] = {}
            for k in rows[0].keys():
                vals = np.array([r[k] for r in rows], dtype=np.float64)
                stat[k] = round(float(np.mean(vals)), 2)
                stat[f"{k}_sem"] = round(float(np.std(vals, ddof=1) / np.sqrt(len(vals))), 2)
            summary["protocols"][p_k][cond_name] = stat

    nl0 = summary["protocols"]["NeckLink-Full"]["loss_0pct"]
    nl50 = summary["protocols"]["NeckLink-Full"]["loss_50pct_rand"]
    sod50 = summary["protocols"]["SendOnDelta-UDP"]["loss_50pct_rand"]
    print(
        f"  [Split-AI GPU] NeckLink 0%-loss: {nl0['compression_vs_raw_x']}x smaller, "
        f"Hazard Acc={nl0['hazard_event_accuracy_pct']}%; "
        f"50%-loss Hazard Acc: NeckLink={nl50['hazard_event_accuracy_pct']}% vs SendOnDelta={sod50['hazard_event_accuracy_pct']}%"
    )
    summary["wall_seconds"] = round(time.perf_counter() - t0, 2)
    return summary, saved_latent_mean, saved_latent_std


FP_MNEMONIC_RE = re.compile(
    r"\b(addss|subss|mulss|divss|addsd|subsd|mulsd|divsd|addps|mulps|cvtsi2ss|cvtsi2sd|cvtss2si|cvtsd2si|movss|movsd|movaps|movups|xmm\d+|ymm\d+|zmm\d+|fld|fstp|fmul|fadd)\b"
)


def stage3_baremetal_c99_hardware_audit(
    lat_mean: np.ndarray,
    lat_std: np.ndarray,
) -> Dict[str, Any]:
    print("=== Stage 3: Bare-Metal Zero-Float ANSI C99 Compilation & Python<->C Live Parity ===")
    t0 = time.perf_counter()
    csrc_dir = REPO_ROOT / "csrc"
    csrc_dir.mkdir(parents=True, exist_ok=True)
    build_dir = REPO_ROOT / "build_c_audit"
    build_dir.mkdir(parents=True, exist_ok=True)

    x_buoy, _ = generate_marine_buoy_usv_telemetry(n_steps=2500, seed=701)
    x_aqms, _ = generate_aqms_gas_plume_telemetry(n_steps=2500, seed=702)
    x_ecg, _ = generate_rural_tele_ecg_wearable(n_steps=2500, seed=703)
    x_ai, _ = generate_split_ai_sequence_dataset(n_sequences=40, seq_len=64, in_dim=24, seed=704)
    # Synthetic 32-ch stream matching latent statistics for C wire parity check
    rng = np.random.default_rng(705)
    z_ai_stream = rng.normal(lat_mean, lat_std * 0.7, size=(2500, 32))
    for k in range(1, 2500):
        z_ai_stream[k] = 0.90 * z_ai_stream[k - 1] + 0.10 * z_ai_stream[k]

    targets = [
        ("necklink_buoy16", NeckLinkConfig(n_channels=16), x_buoy, np.mean(x_buoy, axis=0), np.std(x_buoy, axis=0) + 1e-5),
        ("necklink_aqms12", NeckLinkConfig(n_channels=12), x_aqms, np.mean(x_aqms, axis=0), np.std(x_aqms, axis=0) + 1e-5),
        ("necklink_ecg8", NeckLinkConfig(n_channels=8), x_ecg, np.mean(x_ecg, axis=0), np.std(x_ecg, axis=0) + 1e-5),
        ("necklink_splitai32", NeckLinkConfig(n_channels=32, deadband_q8=24, quant_step_q8=8), z_ai_stream, lat_mean, lat_std),
    ]

    compiled_results: Dict[str, Any] = {}

    for prefix, cfg, stream_data, mean_arr, std_arr in targets:
        hdr_path = csrc_dir / f"{prefix}_int8.h"
        export_necklink_c99_header(cfg, hdr_path, prefix=prefix, obs_mean=mean_arr, obs_std=std_arr)

        # Write C99 wrapper exposing ctypes entrypoints + native 500k-iteration benchmark
        c_wrap = build_dir / f"{prefix}_wrap.c"
        so_path = build_dir / f"lib{prefix}.so"
        obj_path = build_dir / f"{prefix}.o"

        c_wrap.write_text(
            f"""#include <stdint.h>
#include <stddef.h>
#include "{hdr_path.as_posix()}"

static {prefix}_tx_state_t g_tx;
static {prefix}_rx_state_t g_rx;

void c_reset_all(void) {{
    {prefix}_tx_init(&g_tx);
    {prefix}_rx_init(&g_rx);
}}

size_t c_tx_sram_bytes(void) {{ return sizeof({prefix}_tx_state_t); }}
size_t c_rx_sram_bytes(void) {{ return sizeof({prefix}_rx_state_t); }}

int c_encode_step(const int32_t *z_q8, uint8_t *out_pkt) {{
    return {prefix}_encode_q8(&g_tx, z_q8, out_pkt);
}}

void c_decode_step(const uint8_t *pkt, int pkt_len, int32_t *out_z_q8) {{
    {prefix}_decode_q8(&g_rx, pkt, pkt_len, out_z_q8);
}}

int32_t c_bench_loop(int iters) {{
    {prefix}_tx_state_t tx;
    {prefix}_rx_state_t rx;
    {prefix}_tx_init(&tx);
    {prefix}_rx_init(&rx);
    int32_t z_in[{prefix.upper()}_N_CHANNELS];
    int32_t z_out[{prefix.upper()}_N_CHANNELS];
    uint8_t pkt[{prefix.upper()}_MAX_PKT_BYTES];
    int32_t acc = 0;
    int it, c;
    for (it = 0; it < iters; ++it) {{
        for (c = 0; c < {prefix.upper()}_N_CHANNELS; ++c) {{
            z_in[c] = (int32_t)(((it * 13 + c * 37) & 255) - 128);
        }}
        int plen = {prefix}_encode_q8(&tx, z_in, pkt);
        if ((it & 3) == 1) plen = 0; /* 25% simulated packet drop inside tight loop */
        {prefix}_decode_q8(&rx, (plen > 0) ? pkt : 0, plen, z_out);
        acc += z_out[it % {prefix.upper()}_N_CHANNELS];
    }}
    return acc;
}}
""",
            encoding="utf-8",
        )

        # 1. Compile object with strict -mgeneral-regs-only (hardware ban on FPU registers!)
        cmd_obj = [
            "gcc", "-std=c99", "-O3", "-Wall", "-Wextra", "-Werror",
            "-mgeneral-regs-only", "-fPIC", "-c", str(c_wrap), "-o", str(obj_path),
        ]
        subprocess.run(cmd_obj, check=True)

        cmd_so = ["gcc", "-shared", str(obj_path), "-o", str(so_path)]
        subprocess.run(cmd_so, check=True)

        # 2. Disassemble with objdump -d and verify 0 floating-point instructions
        disasm = subprocess.check_output(["objdump", "-d", str(obj_path)], text=True)
        fp_hits = FP_MNEMONIC_RE.findall(disasm)

        # 3. Extract exact section sizes (`size -A`)
        size_out = subprocess.check_output(["size", "-A", str(obj_path)], text=True)
        rodata_b = 0
        text_b = 0
        for line in size_out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                if parts[0] == ".rodata":
                    rodata_b += int(parts[1])
                elif parts[0] == ".text":
                    text_b += int(parts[1])

        # 4. Load via ctypes: verify SRAM size, 500k-step speed, and 0-LSB Python<->C wire parity
        lib = ctypes.CDLL(str(so_path))
        lib.c_reset_all.argtypes = []
        lib.c_tx_sram_bytes.restype = ctypes.c_size_t
        lib.c_rx_sram_bytes.restype = ctypes.c_size_t
        lib.c_encode_step.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_uint8)]
        lib.c_encode_step.restype = ctypes.c_int
        lib.c_decode_step.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_int, ctypes.POINTER(ctypes.c_int32)]
        lib.c_bench_loop.argtypes = [ctypes.c_int]
        lib.c_bench_loop.restype = ctypes.c_int32

        tx_sram = int(lib.c_tx_sram_bytes())
        rx_sram = int(lib.c_rx_sram_bytes())

        # Warmup + time 500,000 full encode+decode ticks
        _ = lib.c_bench_loop(5000)
        iters = 500_000
        tb0 = time.perf_counter()
        chk = int(lib.c_bench_loop(iters))
        tb1 = time.perf_counter()
        ns_per_pair = ((tb1 - tb0) * 1e9) / iters
        khz_x86 = 1e6 / max(ns_per_pair, 1.0)

        # Cross-language live wire parity under 50% Gilbert-Elliott burst drops!
        lib.c_reset_all()
        py_enc = NeckLinkEncoder(cfg, obs_mean=mean_arr, obs_std=std_arr)
        py_dec = NeckLinkDecoder(cfg, obs_mean=mean_arr, obs_std=std_arr)
        drops = simulate_packet_drops(len(stream_data), loss_rate=0.50, mode="gilbert_elliott", mean_burst_len=8.0, seed=888)

        n_ch = cfg.n_channels
        c_in = (ctypes.c_int32 * n_ch)()
        c_pkt = (ctypes.c_uint8 * 128)()
        c_out = (ctypes.c_int32 * n_ch)()

        pkt_mismatches = 0
        max_lsb_diff = 0

        for t_idx in range(len(stream_data)):
            z_q8 = py_enc.standardize_float_to_q8(stream_data[t_idx])
            py_pkt = py_enc.encode_q8(z_q8)

            for c_i in range(n_ch):
                c_in[c_i] = int(z_q8[c_i])
            c_plen = int(lib.c_encode_step(c_in, c_pkt))
            c_bytes = bytes(c_pkt[:c_plen]) if c_plen > 0 else None

            if py_pkt != c_bytes:
                pkt_mismatches += 1

            # Drop 50% bursty packets; decode C-encoded packet in BOTH Python and C!
            if c_bytes is None or bool(drops[t_idx]):
                py_z_rec = py_dec.decode_q8(None)
                lib.c_decode_step(None, 0, c_out)
            else:
                py_z_rec = py_dec.decode_q8(c_bytes)
                pkt_buf = (ctypes.c_uint8 * len(c_bytes)).from_buffer_copy(c_bytes)
                lib.c_decode_step(pkt_buf, len(c_bytes), c_out)

            c_z_rec = np.array([int(c_out[i]) for i in range(n_ch)], dtype=np.int32)
            diff = int(np.max(np.abs(py_z_rec - c_z_rec)))
            if diff > max_lsb_diff:
                max_lsb_diff = diff

        # Conservative embedded MCU throughput estimates based on integer op counts per tick
        ops_per_step = n_ch * 14 + cfg.sketch_dim * n_ch * 2
        cortex_m4_khz = (168e6 / (ops_per_step * 4.5)) / 1e3
        esp32s3_khz = (240e6 / (ops_per_step * 6.5)) / 1e3
        atmega328p_khz = (16e6 / (ops_per_step * 28.0)) / 1e3

        compiled_results[prefix] = {
            "header_file": hdr_path.name,
            "n_channels": n_ch,
            "gcc_mgeneral_regs_only_passed": True,
            "fp_instruction_count_objdump": len(fp_hits),
            "rodata_flash_bytes": rodata_b,
            "text_code_bytes": text_b,
            "total_flash_bytes": rodata_b + text_b,
            "tx_sram_bytes": tx_sram,
            "rx_sram_bytes": rx_sram,
            "total_sram_bytes": tx_sram + rx_sram,
            "ns_per_encode_decode_pair_x86_64": round(ns_per_pair, 2),
            "throughput_khz_x86_64": round(khz_x86, 2),
            "est_stm32f4_168mhz_khz": round(cortex_m4_khz, 2),
            "est_esp32s3_240mhz_khz": round(esp32s3_khz, 2),
            "est_atmega328p_16mhz_khz": round(atmega328p_khz, 2),
            "python_vs_c_packet_byte_mismatches": pkt_mismatches,
            "python_vs_c_decode_max_lsb_diff": max_lsb_diff,
            "bench_checksum": chk,
        }
        print(
            f"  [{prefix}] Flash={rodata_b + text_b} B, SRAM={tx_sram + rx_sram} B, "
            f"FP_ops={len(fp_hits)}, x86={khz_x86:.1f} kHz, "
            f"pkt_mismatch={pkt_mismatches}, LSB_diff={max_lsb_diff}"
        )

    return {
        "compiler": "gcc -std=c99 -O3 -Wall -Wextra -Werror -mgeneral-regs-only",
        "wall_seconds": round(time.perf_counter() - t0, 2),
        "compiled_codecs": compiled_results,
    }


def main() -> None:
    t_all = time.perf_counter()
    res_dir = REPO_ROOT / "results"
    res_dir.mkdir(parents=True, exist_ok=True)

    tel_res = stage1_telemetry_and_economics()
    (res_dir / "telemetry_benchmarks.json").write_text(json.dumps(tel_res, indent=2), encoding="utf-8")

    ai_res, lat_mean, lat_std = stage2_gpu_split_ai_streaming()
    (res_dir / "split_ai_gpu_benchmarks.json").write_text(json.dumps(ai_res, indent=2), encoding="utf-8")

    c_res = stage3_baremetal_c99_hardware_audit(lat_mean, lat_std)
    c_res["total_suite_wall_seconds"] = round(time.perf_counter() - t_all, 2)
    (res_dir / "baremetal_c99_wire_audit.json").write_text(json.dumps(c_res, indent=2), encoding="utf-8")

    print(f"=== ALL NECKLINK BENCHMARKS COMPLETED CLEANLY IN {c_res['total_suite_wall_seconds']}s ===")


if __name__ == "__main__":
    main()
