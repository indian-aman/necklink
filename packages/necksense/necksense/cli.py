"""Command-line interface for NeckSense (`necksense`).

Subcommands:
    - `demo`: Interactive showcase of Antennal Lobe normalization, Kenyon sparse
      hashing, 1-shot MBON novelty habituation, and associative valence learning.
    - `benchmark-quick`: Fast reproducible verification across nearest-neighbor
      retrieval, drifting multi-gas classification, and 1-shot anomaly detection.
    - `hash-csv`: Deterministic sparse binary Kenyon hashing of numeric CSV rows.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Sequence

import numpy as np

from .antennal import AntennalNormalizer
from .kenyon import KenyonHasher, KenyonIndex, popcount_u64
from .mushroom import MushroomNoveltyDetector, MushroomValenceClassifier


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _auroc_rank(y_true: np.ndarray, scores: np.ndarray) -> float:
    """Exact Mann-Whitney U AUROC with tie averaging (no scipy/sklearn needed)."""
    y = np.asarray(y_true, dtype=np.int32)
    s = np.asarray(scores, dtype=np.float64)
    pos = y == 1
    n_pos = int(np.sum(pos))
    n_neg = int(y.size - n_pos)
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(s, kind="mergesort")
    s_sorted = s[order]
    ranks = np.empty(s.size, dtype=np.float64)
    i = 0
    n = s.size
    while i < n:
        j = i + 1
        while j < n and s_sorted[j] == s_sorted[i]:
            j += 1
        avg_rank = 0.5 * (i + 1 + j)
        ranks[order[i:j]] = avg_rank
        i = j
    sum_pos_ranks = float(np.sum(ranks[pos]))
    u_stat = sum_pos_ranks - 0.5 * n_pos * (n_pos + 1)
    return float(u_stat / (n_pos * n_neg))


def _cmd_demo(args: argparse.Namespace) -> int:
    rng = np.random.default_rng(args.seed)

    # 1. Antennal Lobe 100x concentration invariance
    ant = AntennalNormalizer(8, sigma=0.005, gamma=1.0, hill_n=2.0)
    plume_base = np.array([0.85, 0.25, 0.05, 0.00, 0.60, 0.12, 0.02, 0.40], dtype=np.float64)
    r_1x = ant.normalize(0.5 * plume_base)
    r_100x = ant.normalize(50.0 * plume_base)
    cos_100x = _cosine_sim(r_1x, r_100x)

    # 2. Kenyon sparse hashing + Popcount index
    db = rng.standard_normal((1000, 32))
    idx = KenyonIndex(n_inputs=32, n_kc=1024, d_proj=6, wta_frac=0.05, seed=args.seed)
    idx.add(db)
    q = db[7] + 0.08 * rng.standard_normal(32)
    top_match = idx.query(q, top_k=3)[0]

    # 3. 1-shot MBON-alpha'3 novelty habituation
    nov = MushroomNoveltyDetector(32, n_kc=1024, lr=0.55, seed=args.seed)
    odor_a = rng.standard_normal(32)
    odor_anom = rng.standard_normal(32)
    n1 = nov.update_and_score(odor_a)
    n2 = nov.update_and_score(odor_a)
    n3 = nov.update_and_score(odor_a)
    n_after3 = nov.score(odor_a)
    n_anom = nov.update_and_score(odor_anom)

    # 4. 1-shot DAN -> KC -> MBON associative valence learning
    clf = MushroomValenceClassifier(8, antennal=ant, n_kc=1024, seed=args.seed)
    gas_templates = {
        "ethanol": np.array([0.90, 0.35, 0.10, 0.02, 0.50, 0.05, 0.01, 0.20]),
        "ammonia": np.array([0.08, 0.85, 0.70, 0.15, 0.05, 0.40, 0.02, 0.10]),
        "acetone": np.array([0.45, 0.15, 0.08, 0.80, 0.75, 0.10, 0.30, 0.05]),
    }
    for name, vec in gas_templates.items():
        clf.learn(1.0 * vec, name, reward=1.0)
    pred_20x = clf.predict(20.0 * gas_templates["acetone"] + 0.01 * rng.standard_normal(8))

    payload = {
        "antennal_100x_cosine_similarity": round(cos_100x, 6),
        "kenyon_tag_bytes": idx.hasher.tag_bytes,
        "kenyon_active_k": idx.hasher.k_winners,
        "kenyon_top1_recovered_id": int(top_match.id),
        "kenyon_top1_overlap": int(top_match.overlap),
        "novelty_exposures_1_to_3": [round(n1, 4), round(n2, 4), round(n3, 4)],
        "novelty_after_3_exposures": round(n_after3, 4),
        "novelty_orthogonal_anomaly": round(n_anom, 4),
        "valence_1shot_20x_prediction": pred_20x,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print("NeckSense v1.1.0 - Drosophila Olfactory & Mushroom Body Circuit Demo")
    print("=" * 70)
    print(f"[1] Antennal Lobe (Olsen-Wilson LN): 100x plume cosine sim = {cos_100x:.6f}")
    print(
        f"[2] Kenyon Calyx (M=1024, d_proj=6, 0 float muls): {idx.hasher.tag_bytes} B tag, "
        f"top-1 match id={top_match.id} (expected 7, overlap={top_match.overlap}/{idx.hasher.k_winners})"
    )
    print(
        f"[3] MBON-alpha'3 1-Shot Novelty: unseen={n1:.3f} -> after 3 shots={n_after3:.3f} "
        f"| orthogonal anomaly={n_anom:.3f}"
    )
    print(f"[4] DAN->KC->MBON 1-Shot Valence: 20x scaled acetone predicted as '{pred_20x}'")
    return 0


def _cmd_benchmark_quick(args: argparse.Namespace) -> int:
    rng = np.random.default_rng(args.seed)

    # Task 1 quick check: Equal active code budget k=51 (FlyHash M=1024 vs SimHash 51-bit)
    n_db, n_q, dim = 2000, 150, 64
    centers = rng.standard_normal((24, dim)) * 2.0
    assign_db = rng.integers(0, 24, size=n_db)
    assign_q = rng.integers(0, 24, size=n_q)
    db = centers[assign_db] + rng.standard_normal((n_db, dim)) * 0.75
    queries = centers[assign_q] + rng.standard_normal((n_q, dim)) * 0.75

    # Exact L2 top-10 ground truth
    dists = np.sum((queries[:, None, :] - db[None, :, :]) ** 2, axis=2)
    gt_top10 = np.argpartition(dists, 10, axis=1)[:, :10]

    idx = KenyonIndex(n_inputs=dim, n_kc=1024, d_proj=6, wta_frac=0.05, seed=args.seed)
    idx.add(db)
    t0 = time.perf_counter()
    fly_hits = 0
    for qi in range(n_q):
        top_idx, _ = idx.query_raw_tag(idx.hasher.hash(queries[qi]), top_k=10)
        fly_hits += int(np.intersect1d(top_idx, gt_top10[qi]).size)
    fly_us = (time.perf_counter() - t0) * 1e6 / n_q
    fly_recall10 = fly_hits / float(n_q * 10)

    # SimHash at k=51 active hyperplanes (1 word uint64)
    k_sim = idx.hasher.k_winners
    proj_g = rng.standard_normal((dim, k_sim))
    shifts = np.arange(k_sim, dtype=np.uint64)
    db_sim = np.sum(((db @ proj_g) > 0).astype(np.uint64) << shifts, axis=1)
    q_sim = np.sum(((queries @ proj_g) > 0).astype(np.uint64) << shifts, axis=1)
    sim_hits = 0
    for qi in range(n_q):
        ham = popcount_u64(db_sim ^ q_sim[qi]).astype(np.int32)
        top_s = np.argpartition(ham, 10)[:10]
        sim_hits += int(np.intersect1d(top_s, gt_top10[qi]).size)
    sim_recall10_equal_k = sim_hits / float(n_q * 10)

    # Task 2 quick check: 8-sensor gas array under 0.1x-25x concentration + thermal drift
    n_classes, d_gas = 6, 8
    profiles = rng.uniform(0.05, 1.0, size=(n_classes, d_gas))
    for c in range(n_classes):
        profiles[c, c % d_gas] += 1.4
        profiles[c, (c * 3 + 1) % d_gas] += 0.9

    ant_train = AntennalNormalizer(d_gas, sigma=0.01, gamma=1.0, hill_n=2.0)
    clf = MushroomValenceClassifier(d_gas, n_kc=1024, d_proj=6, wta_frac=0.05, seed=args.seed)
    train_X = []
    train_y = []
    for c in range(n_classes):
        for _ in range(3):
            sample = profiles[c] + rng.normal(0.0, 0.015, size=d_gas)
            train_X.append(sample)
            train_y.append(c)
            clf.learn(ant_train.normalize(sample), c, reward=1.0)
    train_X_arr = np.asarray(train_X)
    train_y_arr = np.asarray(train_y)
    train_l2 = train_X_arr / np.maximum(np.linalg.norm(train_X_arr, axis=1, keepdims=True), 1e-9)

    ant_stream = AntennalNormalizer(
        d_gas,
        sigma=0.01,
        gamma=1.0,
        hill_n=2.0,
        tonic_alpha=0.02,
        tonic_slew_limit=0.015,
        phasic_gain=0.35,
    )
    drift_phase = rng.uniform(0.0, 2.0 * np.pi, size=d_gas)
    ns_correct = 0
    l2_correct = 0
    n_events = 120
    t_tick = 0
    for ev in range(n_events):
        # 12 clean-air drift ticks between plumes
        for _ in range(12):
            t_tick += 1
            drift = 0.65 + 0.45 * np.sin(2.0 * np.pi * t_tick / 400.0 + drift_phase)
            ant_stream.step(drift + rng.normal(0.0, 0.008, size=d_gas))
        t_tick += 1
        drift = 0.65 + 0.45 * np.sin(2.0 * np.pi * t_tick / 400.0 + drift_phase)
        c_true = int(ev % n_classes)
        conc = float(np.exp(rng.uniform(np.log(0.1), np.log(25.0))))
        raw_obs = drift + conc * profiles[c_true] + rng.normal(0.0, 0.01, size=d_gas)
        cond_obs = ant_stream.step(raw_obs)
        if int(clf.predict(cond_obs)) == c_true:
            ns_correct += 1
        obs_l2 = raw_obs / max(float(np.linalg.norm(raw_obs)), 1e-9)
        nn_idx = int(np.argmax(train_l2 @ obs_l2))
        if int(train_y_arr[nn_idx]) == c_true:
            l2_correct += 1

    # Task 3 quick check: Streaming 1-shot anomaly detection
    t_len, d_anom = 1200, 32
    modes = rng.standard_normal((4, d_anom)) * 1.5
    stream = np.empty((t_len, d_anom), dtype=np.float64)
    labels = np.zeros(t_len, dtype=np.int32)
    for t in range(t_len):
        if t >= 100 and (t % 37 == 0):
            labels[t] = 1
            stream[t] = rng.standard_normal(d_anom) * 1.5
        else:
            m = (t // 60) % 4
            stream[t] = modes[m] + rng.normal(0.0, 0.12, size=d_anom)

    nov = MushroomNoveltyDetector(d_anom, n_kc=1024, d_proj=6, wta_frac=0.05, lr=0.55, seed=args.seed)
    t0 = time.perf_counter()
    nov_scores = np.empty(t_len, dtype=np.float64)
    for t in range(t_len):
        nov_scores[t] = nov.update_and_score(stream[t])
    nov_us = (time.perf_counter() - t0) * 1e6 / t_len
    auroc_nov = _auroc_rank(labels[100:], nov_scores[100:])

    summary = {
        "retrieval_equal_active_k51": {
            "flyhash_recall_at_10": round(fly_recall10, 4),
            "simhash_51bit_recall_at_10": round(sim_recall10_equal_k, 4),
            "flyhash_query_us": round(fly_us, 2),
        },
        "drifting_plume_classification": {
            "necksense_accuracy": round(ns_correct / n_events, 4),
            "l2_knn_raw_accuracy": round(l2_correct / n_events, 4),
        },
        "streaming_1shot_anomaly": {
            "necksense_auroc": round(auroc_nov, 4),
            "necksense_us_per_sample": round(nov_us, 2),
        },
    }

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print("NeckSense Quick Benchmark Summary")
    print("=" * 68)
    print(
        f"1. Retrieval (N=2000, D=64, k=51 active): "
        f"FlyHash Recall@10={fly_recall10:.3f} ({fly_us:.1f} us/q) vs SimHash-51b={sim_recall10_equal_k:.3f}"
    )
    print(
        f"2. Drifting 8-Gas Plumes (0.1x-25x):      "
        f"NeckSense Acc={ns_correct / n_events:.3f} vs Raw L2-kNN={l2_correct / n_events:.3f}"
    )
    print(
        f"3. Streaming 1-Shot Anomaly (D=32):       "
        f"NeckSense AUROC={auroc_nov:.3f} ({nov_us:.1f} us/sample)"
    )
    print(
        "Note: 'Raw L2-kNN' has no drift correction. With baseline correction, or a normalised\n"
        "nearest-neighbour memory, classical pipelines match NeckSense; see the README benchmarks."
    )
    return 0


def _cmd_hash_csv(args: argparse.Namespace) -> int:
    in_path = Path(args.input)
    if not in_path.exists():
        print(f"Error: input CSV not found: {in_path}", file=sys.stderr)
        return 2

    rows_ids: list[str] = []
    rows_vals: list[list[float]] = []

    with in_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        for line_idx, row in enumerate(reader):
            if not row or all(cell.strip() == "" for cell in row):
                continue
            if line_idx == 0 and args.skip_header:
                continue
            if args.id_col is not None:
                if args.id_col < 0 or args.id_col >= len(row):
                    raise ValueError(f"id_col {args.id_col} out of bounds for row {line_idx}")
                row_id = row[args.id_col].strip()
                num_cells = [c for i, c in enumerate(row) if i != args.id_col]
            else:
                # Auto-detect header on first line if non-numeric
                if line_idx == 0:
                    try:
                        [float(c) for c in row]
                    except ValueError:
                        continue
                row_id = str(len(rows_ids))
                num_cells = row
            rows_ids.append(row_id)
            rows_vals.append([float(c) for c in num_cells])

    if not rows_vals:
        print("Error: no numeric rows found in input CSV", file=sys.stderr)
        return 2

    X = np.asarray(rows_vals, dtype=np.float64)
    n_inputs = X.shape[1]

    if args.antennal_normalize:
        ant = AntennalNormalizer(n_inputs, c_parity=args.c_parity)
        X = ant.normalize(X)

    hasher = KenyonHasher(
        n_inputs,
        n_kc=args.n_kc,
        d_proj=args.d_proj,
        wta_frac=args.wta_frac,
        seed=args.seed,
        c_parity=args.c_parity,
    )
    tags = hasher.hash(X)
    active_counts = np.sum(popcount_u64(tags), axis=1, dtype=np.int32)

    out_fh = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
    try:
        writer = csv.writer(out_fh)
        writer.writerow(["id", "n_kc", "active_bits", "tag_hex"])
        for row_id, tag, act_k in zip(rows_ids, tags, active_counts):
            writer.writerow([row_id, hasher.n_kc, int(act_k), KenyonHasher.tag_to_hex(tag)])
    finally:
        if args.output:
            out_fh.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="necksense",
        description="NeckSense: Drosophila Antennal Lobe + Mushroom Body sparse hashing & 1-shot learning.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="Run interactive 4-stage Drosophila circuit showcase")
    p_demo.add_argument("--seed", type=int, default=42, help="Deterministic PRNG seed")
    p_demo.add_argument("--json", action="store_true", help="Emit JSON output")
    p_demo.set_defaults(func=_cmd_demo)

    p_bench = sub.add_parser("benchmark-quick", help="Run fast 3-task empirical benchmark")
    p_bench.add_argument("--seed", type=int, default=42, help="Deterministic PRNG seed")
    p_bench.add_argument("--json", action="store_true", help="Emit JSON output")
    p_bench.set_defaults(func=_cmd_benchmark_quick)

    p_hash = sub.add_parser("hash-csv", help="Hash numeric CSV vectors into binary Kenyon tags")
    p_hash.add_argument("input", help="Path to input CSV file")
    p_hash.add_argument("-o", "--output", default=None, help="Optional output CSV path (default: stdout)")
    p_hash.add_argument("--skip-header", action="store_true", help="Always skip the first CSV row")
    p_hash.add_argument("--id-col", type=int, default=None, help="0-based column index to use as row ID")
    p_hash.add_argument("--n-kc", type=int, default=1024, help="Number of Kenyon cells (multiple of 64)")
    p_hash.add_argument("--d-proj", type=int, default=6, help="Inputs summed per Kenyon cell")
    p_hash.add_argument("--wta-frac", type=float, default=0.05, help="Top-k WTA active fraction")
    p_hash.add_argument("--seed", type=int, default=42, help="Deterministic connectivity seed")
    p_hash.add_argument(
        "--antennal-normalize",
        action="store_true",
        help="Apply stateless Olsen-Wilson AntennalNormalizer prior to hashing",
    )
    p_hash.add_argument("--c-parity", action="store_true", help="Use Q16.16 C99 bit-parity quantization")
    p_hash.set_defaults(func=_cmd_hash_csv)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
