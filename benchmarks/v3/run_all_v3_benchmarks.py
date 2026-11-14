"""
Master Unrigged Benchmark & K=100 Maslov-Sneppen Shuffle-Gate Runner (`NeckLink v3.0.0`).

Usage:
  Local smoke test (`K = 2`):
    python run_all_v3_benchmarks.py --k-shuffles 2 --n-seeds 6 --workers 2
  Full Colab G4 production audit (`K = 100`, `N = 30` seeds):
    python run_all_v3_benchmarks.py --k-shuffles 100 --n-seeds 30 --workers 24
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import gzip
import json
import os
from pathlib import Path
import pickle
import platform
import sys
import time
from typing import Any, Dict, List, Tuple

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
if sys.platform.startswith("linux"):
    os.environ.setdefault("MUJOCO_GL", "egl")

import numpy as np

from necklink_cns.connectome.extract import compile_circuits_from_subgraphs
from necklink_cns.connectome.shuffle import shuffle_raw_subgraphs, verify_degree_preservation

from bench_1_compass import run_compass_benchmark
from bench_2_sight import run_sight_benchmark
from bench_3_sense import run_sense_benchmark
from bench_4_cord import run_cord_benchmark
from bench_5_whole_cns import run_whole_cns_benchmark
from datasets import sha256_path

_GLOBAL_RAW_SUBGRAPHS: Dict[str, Any] | None = None


def _init_shuffle_worker(raw_pkl_gz_path: str) -> None:
    global _GLOBAL_RAW_SUBGRAPHS
    with gzip.open(raw_pkl_gz_path, "rb") as f:
        _GLOBAL_RAW_SUBGRAPHS = pickle.load(f)


def _generate_one_shuffle(seed: int) -> Tuple[int, Dict[str, Any], Dict[str, float]]:
    assert _GLOBAL_RAW_SUBGRAPHS is not None
    raw = _GLOBAL_RAW_SUBGRAPHS
    shuf_raw = shuffle_raw_subgraphs(raw, seed=seed, swaps_per_edge=8)

    # Explicitly assert Maslov-Sneppen degree & weight-multiset preservation on every organ!
    inv_cx = verify_degree_preservation(raw["cx"]["W_epg_d7"], shuf_raw["cx"]["W_epg_d7"])
    inv_ol = verify_degree_preservation(raw["optic"]["W_t45_lplc2"], shuf_raw["optic"]["W_t45_lplc2"])
    inv_mb = verify_degree_preservation(raw["mb"]["W_alpn_kc"], shuf_raw["mb"]["W_alpn_kc"])
    inv_vd = verify_degree_preservation(raw["vnc_dn"]["W_brain_predn"], shuf_raw["vnc_dn"]["W_brain_predn"])

    compiled = compile_circuits_from_subgraphs(shuf_raw)
    overlap_stats = {
        "cx_epg_d7_overlap": inv_cx["edge_overlap_fraction"],
        "optic_t45_lplc2_overlap": inv_ol["edge_overlap_fraction"],
        "mb_alpn_kc_overlap": inv_mb["edge_overlap_fraction"],
        "dn_brain_predn_overlap": inv_vd["edge_overlap_fraction"],
    }
    return seed, compiled, overlap_stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-pkl", type=str, default=str(Path(__file__).resolve().parent / "artifacts" / "banc_v888_raw_subgraphs.pkl.gz"))
    ap.add_argument("--out", "--out-json", dest="out_json", type=str, default=str(Path(__file__).resolve().parent / "artifacts" / "v3_audit_benchmarks.json"))
    ap.add_argument("--k-shuffles", type=int, default=100)
    ap.add_argument("--n-seeds", type=int, default=30)
    ap.add_argument("--workers", type=int, default=min(24, max(1, (os.cpu_count() or 4) // 2)))
    args = ap.parse_args()

    t_start = time.time()
    raw_path = Path(args.raw_pkl)
    raw_sha = sha256_path(raw_path)
    with gzip.open(raw_path, "rb") as f:
        raw_check = pickle.load(f)

    print(f"[0/6] Generating & compiling K={args.k_shuffles} Maslov-Sneppen degree-preserving shuffles across {args.workers} workers...")
    t0 = time.time()
    seeds = [10000 + k for k in range(args.k_shuffles)]
    compiled_shuffles: List[Dict[str, Any]] = []
    overlaps: List[Dict[str, float]] = []

    if args.workers <= 1 or args.k_shuffles <= 2:
        _init_shuffle_worker(str(raw_path))
        for s in seeds:
            _, comp, ov = _generate_one_shuffle(s)
            compiled_shuffles.append(comp)
            overlaps.append(ov)
    else:
        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=_init_shuffle_worker,
            initargs=(str(raw_path),),
        ) as pool:
            for _, comp, ov in pool.map(_generate_one_shuffle, seeds):
                compiled_shuffles.append(comp)
                overlaps.append(ov)

    shuf_gen_sec = time.time() - t0
    print(f"      Compiled {len(compiled_shuffles)} verified shuffles in {shuf_gen_sec:.1f}s.")

    shuf_cx = [c["cx"] for c in compiled_shuffles]
    shuf_optic = [c["optic"] for c in compiled_shuffles]
    shuf_mb = [c["mb"] for c in compiled_shuffles]
    shuf_vd = [c["vnc_dn"] for c in compiled_shuffles]

    print("[1/6] Running Benchmark 1: Central Complex (NeckCompass v3.0.0)...")
    t1 = time.time()
    res_compass = run_compass_benchmark(shuf_cx, n_seeds=args.n_seeds, workers=args.workers)
    print(f"      Done in {time.time() - t1:.1f}s.")

    print("[2/6] Running Benchmark 2: Optic Lobe 3D MuJoCo EGL (NeckSight v3.0.0)...")
    t2 = time.time()
    n_sub = max(2, args.n_seeds // 4)
    res_sight = run_sight_benchmark(shuf_optic, n_per_subtype=n_sub)
    print(f"      Done in {time.time() - t2:.1f}s.")

    print("[3/6] Running Benchmark 3: Mushroom Body UCI Gas & CWRU Bearing (NeckSense v3.0.0)...")
    t3 = time.time()
    res_sense = run_sense_benchmark(shuf_mb, n_seeds=args.n_seeds, workers=args.workers)
    print(f"      Done in {time.time() - t3:.1f}s.")

    print("[4/6] Running Benchmark 4: VNC Motor Cord 3D MuJoCo Hexapod & Hexacopter (NeckCord v3.0.0)...")
    t4 = time.time()
    res_cord = run_cord_benchmark(shuf_vd, n_seeds=args.n_seeds, workers=args.workers)
    print(f"      Done in {time.time() - t4:.1f}s.")

    print("[5/6] Running Benchmark 5: Closed-Loop Whole-CNS USV Survival (NeckLinkCNS v3.0.0)...")
    t5 = time.time()
    res_cns = run_whole_cns_benchmark(compiled_shuffles, n_seeds=args.n_seeds, workers=args.workers)
    print(f"      Done in {time.time() - t5:.1f}s.")

    payload = {
        "suite_version": "3.0.0",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "python_version": platform.python_version(),
        },
        "provenance": {
            "raw_subgraphs_sha256": raw_sha,
            "source_connectome_compiled_npz_sha256": raw_check["meta"]["sha256_connectome_compiled_npz"],
            "source_neurons_meta_parquet_sha256": raw_check["meta"]["sha256_neurons_meta_parquet"],
            "k_shuffles": args.k_shuffles,
            "degree_preservation_verified_all_k": True,
            "mean_edge_overlap_vs_real": {
                k: float(np.mean([o[k] for o in overlaps])) for k in overlaps[0].keys()
            },
            "shuffle_compile_wall_s": shuf_gen_sec,
            "total_wall_s": time.time() - t_start,
        },
        "bench_1_neckcompass": res_compass,
        "bench_2_necksight": res_sight,
        "bench_3_necksense": res_sense,
        "bench_4_neckcord": res_cord,
        "bench_5_necklink_cns": res_cns,
    }

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[6/6] Saved complete audited v3.0.0 benchmark suite -> {out_path} (total {time.time() - t_start:.1f}s)")


if __name__ == "__main__":
    main()
