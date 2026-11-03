"""
Fair telemetry benchmark: NeckLink vs strong conventional baselines.

Every codec here shares the same per-channel standardization (mean/std) and the same
quantizer settings (deadband, quant step), so differences come from the coding scheme,
not from giving one side coarser quantization. Baselines:

  - AbsSoD-KF32      : send-on-delta that transmits ABSOLUTE quantized values (not increments),
                       bitmask-packed, plus a full keyframe every 32 ticks. No feedback link.
  - AckedDelta       : snapshot delta against the last state the receiver acknowledged
                       (game-netcode style). Needs a return link; ack bytes reported separately.
  - Batch16-zstd     : store-and-forward 16 ticks, time-delta, zstd. No feedback; adds latency.
  - SendOnDelta-UDP  : the original increment-only strawman from baselines.py (reference).
  - Int8-Zlib-Block16: original baseline from baselines.py (reference).

Datasets: the three synthetic workloads from necklink/workloads.py plus real AQMS recordings
(Campi H2S station V202, Tridel test unit) and the synthetic AQMS raw-count fixture.

Run:  python scripts/fair_telemetry_benchmark.py
      python scripts/fair_telemetry_benchmark.py --full --seeds 5 --workers 46 --aqms-dir /content/aqms   (Colab G4)
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import struct
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import zstandard

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from necklink.baselines import Int8ZlibBlockCodec, SendOnDeltaUDPCodec, simulate_packet_drops  # noqa: E402
from necklink.codec import NeckLinkConfig, NeckLinkDecoder, NeckLinkEncoder  # noqa: E402
from necklink.workloads import (  # noqa: E402
    generate_aqms_gas_plume_telemetry,
    generate_marine_buoy_usv_telemetry,
    generate_rural_tele_ecg_wearable,
)

# Private AQMS recordings (not in the repo): pass --aqms-dir or set NECKLINK_AQMS_DIR
AQMS_DIR = Path(os.environ.get("NECKLINK_AQMS_DIR", "data/AQMS"))
OUT_JSON = ROOT / "results" / "fair_telemetry_benchmark.json"

QUANT_STEP_Q8 = 8
DEADBANDS_Q8 = (16, 32, 64)
SEEDS = (11, 22)
LOSS_CONDITIONS = [
    ("loss_0", 0.0, "random"),
    ("loss_10_rand", 0.10, "random"),
    ("loss_30_rand", 0.30, "random"),
    ("loss_50_rand", 0.50, "random"),
    ("loss_30_burst", 0.30, "gilbert_elliott"),
]
UDP_IPV4_OVERHEAD_B = 28


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

def _clean(x: np.ndarray) -> np.ndarray:
    keep = np.std(x, axis=0) > 1e-9
    return x[:, keep]


def load_datasets(full: bool = False) -> List[Tuple[str, np.ndarray, Dict[str, Any]]]:
    """`full=True` uses every Campi station and the whole 30-day fixture instead of the first ~5k rows."""
    out: List[Tuple[str, np.ndarray, Dict[str, Any]]] = []
    cap = None if full else 6000
    for name, gen in (
        ("synthetic_buoy16", generate_marine_buoy_usv_telemetry),
        ("synthetic_aqms12", generate_aqms_gas_plume_telemetry),
        ("synthetic_ecg8", generate_rural_tele_ecg_wearable),
    ):
        x, meta = gen()
        out.append((name, _clean(x), {"real": False, "dt_ms": 1000.0 / float(meta["sample_rate_hz"]), "rtt_ms": float(meta["rtt_ms"])}))

    raw_dir = AQMS_DIR / "sensor raw data"
    campi = pd.read_csv(raw_dir / "campi_h2s_real.csv")
    cols = ["h2s_ppm", "so2_ppm", "air_t_c", "air_rh_pct", "p_hpa"]
    for station in (sorted(campi.station.unique()) if full else ["V202"]):
        st = campi[campi.station == station].sort_values("ts")
        xc = st[cols].ffill().dropna().to_numpy(dtype=np.float64)[:cap]
        if len(xc) >= 500:
            out.append((f"real_campi_{station.lower().replace('-', '')}_5ch_hourly", _clean(xc),
                        {"real": True, "dt_ms": 3_600_000.0, "rtt_ms": 1000.0}))

    unit = pd.read_csv(AQMS_DIR / "Handover_Terra_Team" / "03_Templates_and_test_values" / "Sample_station_data_test_unit.csv")
    ucols = ["a0_nh3_counts", "a1_h2s_op1_counts", "a2_h2s_op2_counts", "a3_voc_counts", "h2s_ppm", "nh3_ppm", "voc_ppm", "battery_v"]
    xu = unit[ucols].ffill().dropna().to_numpy(dtype=np.float64)
    out.append(("real_tridel_test_unit_8ch_1min", _clean(xu), {"real": True, "dt_ms": 60_000.0, "rtt_ms": 1000.0}))

    fix = pd.read_csv(raw_dir / "aqms_raw_fixture.csv")
    fcols = [c for c in fix.columns if c != "ts" and not c.startswith("true_")]
    xf = fix[fcols].to_numpy(dtype=np.float64)[: (None if full else 5000)]
    out.append(("fixture_aqms_rawcounts_1min", _clean(xf), {"real": False, "dt_ms": 60_000.0, "rtt_ms": 1000.0}))
    return out


# ---------------------------------------------------------------------------
# Shared quantizer + metrics
# ---------------------------------------------------------------------------

def hold_quantize(z: np.ndarray, deadband: float, step: float) -> np.ndarray:
    """Per-channel send-on-delta state (integer steps): update only when |z - state| >= deadband."""
    t_steps, n = z.shape
    s = np.zeros((t_steps, n), dtype=np.int32)
    cur = np.round(z[0] / step).astype(np.int32)
    for t in range(t_steps):
        move = np.abs(z[t] - cur * step) >= deadband
        if np.any(move):
            cur = np.where(move, np.round(z[t] / step).astype(np.int32), cur)
        s[t] = cur
    return np.clip(s, -32768, 32767)


def nrmse_pct(x_true: np.ndarray, x_rec: np.ndarray) -> float:
    w = max(10, len(x_true) // 20)  # same warm-up skip as experiments/run_necklink_all_g4.py
    yt, yp = x_true[w:], x_rec[w:]
    ch_std = np.maximum(np.std(yt, axis=0), 1e-6)
    return float(100.0 * np.mean(np.sqrt(np.mean(((yt - yp) / ch_std) ** 2, axis=0))))


def _mask_bytes(n: int) -> int:
    return (n + 7) // 8


def _varint_len(v: int) -> int:
    u = (v << 1) ^ (v >> 31)  # zigzag
    n = 1
    while u >= 0x80:
        u >>= 7
        n += 1
    return n


# ---------------------------------------------------------------------------
# Strong baselines (operate on standardized z, return standardized reconstruction)
# ---------------------------------------------------------------------------

def run_abs_sod_keyframe(s: np.ndarray, dropped: np.ndarray, kf_every: int = 32) -> Dict[str, Any]:
    t_steps, n = s.shape
    rx = np.zeros(n, dtype=np.int32)
    have = np.zeros(n, dtype=bool)
    out = np.zeros((t_steps, n), dtype=np.int32)
    prev = np.full(n, np.iinfo(np.int32).min, dtype=np.int64)
    up_b = pkts = 0
    since_kf = kf_every
    for t in range(t_steps):
        changed = s[t] != prev
        prev = s[t].astype(np.int64)
        if since_kf >= kf_every:
            send = np.ones(n, dtype=bool)
            since_kf = 0
        else:
            send = changed
        since_kf += 1
        if np.any(send):
            up_b += 2 + _mask_bytes(n) + 2 * int(send.sum())  # seq + bitmask + int16 per channel
            pkts += 1
            if not dropped[t]:
                rx = np.where(send, s[t], rx)
                have |= send
        out[t] = rx
    return {"rec_s": out, "up_bytes": up_b, "pkts": pkts, "down_bytes": 0, "latency_ticks": 0}


def run_acked_delta(s: np.ndarray, dropped_up: np.ndarray, dropped_down: np.ndarray, rtt_ticks: int) -> Dict[str, Any]:
    t_steps, n = s.shape
    zero = np.zeros(n, dtype=np.int32)
    tx_hist: Dict[int, np.ndarray] = {-1: zero}
    rx_hist: Dict[int, np.ndarray] = {-1: zero}
    base_seq = -1
    last_sent = None
    last_send_t = -10**9
    seq = 0
    acks_in_flight: List[Tuple[int, int]] = []  # (arrival_tick, acked_seq)
    rx_state = zero.copy()
    out = np.zeros((t_steps, n), dtype=np.int32)
    up_b = pkts = down_b = 0
    one_way = max(1, rtt_ticks // 2)
    for t in range(t_steps):
        still = []
        for arr, a in acks_in_flight:
            if arr <= t:
                if a > base_seq:
                    base_seq = a
                    for k in [k for k in tx_hist if -1 < k < base_seq]:
                        del tx_hist[k]
            else:
                still.append((arr, a))
        acks_in_flight = still

        base = tx_hist[base_seq]
        diff = s[t] != base
        new_info = last_sent is None or np.any(s[t] != last_sent)
        if np.any(diff) and (new_info or t - last_send_t >= max(1, rtt_ticks)):
            delta = (s[t] - base)[diff]
            up_b += 2 + 1 + _mask_bytes(n) + sum(_varint_len(int(v)) for v in delta)  # seq + base offset + mask + varints
            pkts += 1
            tx_hist[seq] = s[t].copy()
            last_sent = s[t].copy()
            last_send_t = t
            if not dropped_up[t]:
                rb = rx_hist.get(base_seq)
                if rb is not None:
                    rx_state = rb + np.where(diff, s[t] - base, 0).astype(np.int32)
                    rx_hist[seq] = rx_state.copy()
                    for k in [k for k in rx_hist if -1 < k < base_seq]:
                        del rx_hist[k]
                    down_b += 2
                    if not dropped_down[t]:
                        acks_in_flight.append((t + 2 * one_way, seq))
            seq += 1
        out[t] = rx_state
    return {"rec_s": out, "up_bytes": up_b, "pkts": pkts, "down_bytes": down_b, "latency_ticks": 0}


def run_batch_zstd(s: np.ndarray, dropped: np.ndarray, batch: int = 16) -> Dict[str, Any]:
    t_steps, n = s.shape
    cctx = zstandard.ZstdCompressor(level=19, write_checksum=False, write_content_size=False, write_dict_id=False)
    out = np.zeros((t_steps, n), dtype=np.int32)
    last = np.zeros(n, dtype=np.int32)
    up_b = pkts = 0
    n_batches = math.ceil(t_steps / batch)
    for b in range(n_batches):
        blk = s[b * batch:(b + 1) * batch]
        d = np.vstack([blk[:1], np.diff(blk, axis=0)]).astype(np.int16)
        payload = cctx.compress(np.ascontiguousarray(d.T).tobytes())
        up_b += 2 + len(payload)
        pkts += 1
        if not dropped[b]:
            out[b * batch:(b + 1) * batch] = blk
            last = blk[-1]
        else:
            out[b * batch:(b + 1) * batch] = last
    return {"rec_s": out, "up_bytes": up_b, "pkts": pkts, "down_bytes": 0, "latency_ticks": batch - 1}


def run_necklink(x: np.ndarray, dropped: np.ndarray, mean: np.ndarray, std: np.ndarray, deadband_q8: int) -> Dict[str, Any]:
    cfg = NeckLinkConfig(n_channels=x.shape[1], deadband_q8=deadband_q8, quant_step_q8=QUANT_STEP_Q8)
    enc = NeckLinkEncoder(cfg, obs_mean=mean, obs_std=std)
    dec = NeckLinkDecoder(cfg, obs_mean=mean, obs_std=std)
    rx = np.zeros_like(x)
    up_b = pkts = 0
    for t in range(len(x)):
        pkt = enc.encode_float(x[t])
        if pkt is not None:
            up_b += len(pkt)
            pkts += 1
        rx[t] = dec.decode_float(None if (pkt is None or dropped[t]) else pkt)
    return {"rec_x": rx, "up_bytes": up_b, "pkts": pkts, "down_bytes": 0, "latency_ticks": 0}


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

def run_job(job: Tuple[str, np.ndarray, Dict[str, Any], int, int, str, float, str]) -> List[Dict[str, Any]]:
    ds_name, x, meta, deadband_q8, seed, cond, loss, mode = job
    t_steps, n = x.shape
    mean = np.mean(x, axis=0)
    std = np.std(x, axis=0) + 1e-5
    z = (x - mean) / std
    step = QUANT_STEP_Q8 / 256.0
    s = hold_quantize(z, deadband_q8 / 256.0, step)
    rtt_ticks = max(1, int(math.ceil(meta["rtt_ms"] / meta["dt_ms"])))

    drop = simulate_packet_drops(t_steps, loss, mode=mode, seed=seed)
    drop_down = simulate_packet_drops(t_steps, loss, mode=mode, seed=seed + 7919)
    raw_bytes = t_steps * (2 + 4 * n)

    results: Dict[str, Dict[str, Any]] = {
        "NeckLink-Full": run_necklink(x, drop, mean, std, deadband_q8),
        "AbsSoD-KF32": run_abs_sod_keyframe(s, drop),
        "AckedDelta": run_acked_delta(s, drop, drop_down, rtt_ticks),
        "Batch16-zstd": run_batch_zstd(s, drop),
    }
    sod_x, sod_b, sod_p = SendOnDeltaUDPCodec(n, mean, std, deadband_q8=deadband_q8, quant_step_q8=QUANT_STEP_Q8).run_stream(x, drop)
    results["SendOnDelta-UDP(ref)"] = {"rec_x": sod_x, "up_bytes": sod_b, "pkts": sod_p, "down_bytes": 0, "latency_ticks": 0}
    zl_x, zl_b, zl_p = Int8ZlibBlockCodec(n, mean, std).run_stream(x, drop)
    results["Int8-Zlib-Block16(ref)"] = {"rec_x": zl_x, "up_bytes": zl_b, "pkts": zl_p, "down_bytes": 0, "latency_ticks": 15}

    rows = []
    for codec, r in results.items():
        rec_x = r["rec_x"] if "rec_x" in r else r["rec_s"] * step * std + mean
        rows.append({
            "dataset": ds_name,
            "real_data": meta["real"],
            "n_channels": n,
            "n_ticks": t_steps,
            "deadband_q8": deadband_q8,
            "seed": seed,
            "condition": cond,
            "codec": codec,
            "nrmse_pct": round(nrmse_pct(x, rec_x), 3),
            "up_payload_bytes": int(r["up_bytes"]),
            "up_packets": int(r["pkts"]),
            "up_bytes_with_udp_ip": int(r["up_bytes"] + UDP_IPV4_OVERHEAD_B * r["pkts"]),
            "down_bytes": int(r["down_bytes"]),
            "compression_vs_raw_payload_x": round(raw_bytes / max(1, r["up_bytes"]), 2),
            "compression_vs_raw_with_udp_ip_x": round((raw_bytes + UDP_IPV4_OVERHEAD_B * t_steps) / max(1, r["up_bytes"] + UDP_IPV4_OVERHEAD_B * r["pkts"]), 2),
            "latency_ticks": r["latency_ticks"],
        })
    return rows


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    df = pd.DataFrame(rows)
    keys = ["dataset", "deadband_q8", "condition", "codec"]
    agg = df.groupby(keys, as_index=False).agg(
        nrmse_pct=("nrmse_pct", "mean"),
        up_payload_bytes=("up_payload_bytes", "mean"),
        up_bytes_with_udp_ip=("up_bytes_with_udp_ip", "mean"),
        up_packets=("up_packets", "mean"),
        down_bytes=("down_bytes", "mean"),
        compression_vs_raw_payload_x=("compression_vs_raw_payload_x", "mean"),
        compression_vs_raw_with_udp_ip_x=("compression_vs_raw_with_udp_ip_x", "mean"),
    )

    # Pareto check per (dataset, condition): is a NeckLink point dominated by a no-feedback strong baseline
    # (fewer-or-equal payload bytes AND lower-or-equal error) at ANY deadband setting?
    verdicts = []
    strong_nofb = {"AbsSoD-KF32", "Batch16-zstd"}
    for (ds, cond), g in agg.groupby(["dataset", "condition"]):
        nl = g[g.codec == "NeckLink-Full"]
        for _, p in nl.iterrows():
            for bytes_col in ("up_payload_bytes", "up_bytes_with_udp_ip"):
                dom = g[g.codec.isin(strong_nofb | {"AckedDelta"})
                        & (g[bytes_col] <= p[bytes_col]) & (g.nrmse_pct <= p.nrmse_pct)]
                verdicts.append({
                    "dataset": ds, "condition": cond, "necklink_deadband_q8": int(p.deadband_q8),
                    "bytes_metric": bytes_col,
                    "necklink_bytes": round(float(p[bytes_col]), 1), "necklink_nrmse_pct": round(float(p.nrmse_pct), 2),
                    "dominated_by": sorted({f"{r.codec}@db{int(r.deadband_q8)}" for _, r in dom.iterrows()}),
                    "dominated_by_no_feedback": sorted({f"{r.codec}@db{int(r.deadband_q8)}" for _, r in dom.iterrows() if r.codec in strong_nofb}),
                })
    return {"table": agg.round(3).to_dict(orient="records"), "pareto_verdicts": verdicts}


def main() -> None:
    global AQMS_DIR, OUT_JSON
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="all Campi stations + full 30-day fixture")
    ap.add_argument("--seeds", type=int, default=len(SEEDS))
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--aqms-dir", type=Path, default=AQMS_DIR)
    args = ap.parse_args()
    AQMS_DIR = args.aqms_dir
    seeds = tuple(11 * (i + 1) for i in range(args.seeds))
    if args.full:
        OUT_JSON = OUT_JSON.with_name("fair_telemetry_benchmark_full.json")

    t0 = time.time()
    datasets = load_datasets(args.full)
    for name, x, meta in datasets:
        print(f"{name}: {x.shape[0]} ticks x {x.shape[1]} ch (real={meta['real']})", flush=True)
    jobs = [
        (name, x, meta, db, seed, cond, loss, mode)
        for (name, x, meta), db, seed, (cond, loss, mode) in itertools.product(datasets, DEADBANDS_Q8, seeds, LOSS_CONDITIONS)
    ]
    # longest jobs first so the pool does not idle on a straggler at the end
    jobs.sort(key=lambda j: -j[1].size)
    rows: List[Dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, r in enumerate(ex.map(run_job, jobs, chunksize=1)):
            rows.extend(r)
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(jobs)} jobs ({time.time() - t0:.0f} s)", flush=True)
    summary = summarize(rows)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({
        "settings": {"quant_step_q8": QUANT_STEP_Q8, "deadbands_q8": DEADBANDS_Q8, "seeds": seeds, "full": args.full,
                     "loss_conditions": LOSS_CONDITIONS, "udp_ipv4_overhead_b": UDP_IPV4_OVERHEAD_B},
        "rows": rows, **summary,
    }, indent=1))
    print(f"wrote {OUT_JSON} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
