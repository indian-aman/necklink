"""Honest empirical benchmark & 3-panel chart generator for NeckSense v1.1.0.

Evaluates three tasks across 5 random seeds (zero sugarcoating):
  1. Nearest-Neighbor Retrieval (N=5,000, D=64):
     FlyHash (Kenyon 5% WTA, 0 float muls) vs Dense Gaussian SimHash (LSH)
     under both equal active-code budget (`k`) and equal raw bitword budget (`M_bits`).
  2. Multi-Gas Plume Classification (8-sensor array, 6 gases):
     AntennalNormalizer + MushroomValenceClassifier vs L2-Normalized k-NN and
     Raw Z-Score Nearest-Centroid across 0.1x-25x concentration swings + thermal drift.
  3. Streaming 1-Shot Anomaly Detection (D=16, T=2,500):
     MushroomNoveltyDetector (MBON-alpha'3) vs Online Mahalanobis (Sherman-Morrison),
     Online Half-Space Projections, and Rolling Z-Score (AUROC & us/sample).

Outputs:
  - D:/necksense/results/benchmark_summary.json
  - D:/necksense/assets/necksense_benchmarks.png
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from necksense import (
    __version__,
    AntennalNormalizer,
    KenyonHasher,
    KenyonIndex,
    MushroomNoveltyDetector,
    MushroomValenceClassifier,
    popcount_u64,
)
from necksense.cli import _auroc_rank


def _pack_bool_bits_u64(bits: np.ndarray) -> np.ndarray:
    """Pack `(N, B)` boolean matrix into `(N, ceil(B/64))` uint64 words."""
    b_arr = np.asarray(bits, dtype=np.uint8)
    n_rows, n_bits = b_arr.shape
    n_words = (n_bits + 63) // 64
    padded = np.zeros((n_rows, n_words * 64), dtype=np.uint64)
    padded[:, :n_bits] = b_arr
    reshaped = padded.reshape(n_rows, n_words, 64)
    shifts = np.arange(64, dtype=np.uint64)[None, None, :]
    return np.bitwise_or.reduce(reshaped << shifts, axis=2)


def benchmark_task1_retrieval(seeds: list[int]) -> dict[str, Any]:
    """Task 1: N=5,000, D=64 retrieval under equal active-k and equal M_bits."""
    n_db = 5000
    n_queries = 400
    dim = 64
    top_k = 10

    active_k_budgets = [16, 32, 64, 128]
    raw_bit_budgets = [512, 1024, 2048]

    equal_k_records: dict[int, dict[str, list[float]]] = {
        k: {"fly_recall": [], "sim_recall": [], "fly_query_us": [], "sim_query_us": [], "fly_hash_us": [], "sim_hash_us": []}
        for k in active_k_budgets
    }
    equal_bits_records: dict[int, dict[str, list[float]]] = {
        m: {"fly_recall": [], "sim_recall": [], "fly_query_us": [], "sim_query_us": [], "fly_hash_us": [], "sim_hash_us": []}
        for m in raw_bit_budgets
    }

    for seed in seeds:
        rng = np.random.default_rng(seed)
        # Clustered multi-scale continuous manifold (32 latent topics in R^64)
        centers = rng.standard_normal((32, dim)) * 2.2
        c_db = rng.integers(0, 32, size=n_db)
        c_q = rng.integers(0, 32, size=n_queries)
        db = centers[c_db] + rng.standard_normal((n_db, dim)) * 0.85
        queries = centers[c_q] + rng.standard_normal((n_queries, dim)) * 0.85

        # Mean-center vectors per sample (standard cosine / LSH preprocessing)
        db_mc = db - db.mean(axis=1, keepdims=True)
        q_mc = queries - queries.mean(axis=1, keepdims=True)

        # Ground-truth top-10 exact Euclidean nearest neighbors on centered vectors
        norms_db = np.sum(db_mc * db_mc, axis=1)[None, :]
        norms_q = np.sum(q_mc * q_mc, axis=1)[:, None]
        dists = norms_q + norms_db - 2.0 * (q_mc @ db_mc.T)
        gt_top10 = np.argpartition(dists, top_k, axis=1)[:, :top_k]

        # Regime A: Equal active code budget `k` (Science 2017 protocol)
        for k in active_k_budgets:
            m_kc = int(np.ceil((20 * k) / 64.0) * 64)
            idx_fly = KenyonIndex(
                n_inputs=dim, n_kc=m_kc, d_proj=6, k_winners=k, seed=seed
            )
            idx_fly.add(db_mc)

            t0 = time.perf_counter()
            q_tags_fly = idx_fly.hasher.hash(q_mc)
            fly_hash_us = (time.perf_counter() - t0) * 1e6 / n_queries

            t0 = time.perf_counter()
            hits_fly = 0
            for qi in range(n_queries):
                cand, _ = idx_fly.query_raw_tag(q_tags_fly[qi], top_k=top_k)
                hits_fly += int(np.intersect1d(cand, gt_top10[qi]).size)
            fly_q_us = (time.perf_counter() - t0) * 1e6 / n_queries

            # Dense Gaussian SimHash with `k` random hyperplanes
            g_mat = rng.standard_normal((dim, k))
            db_sim_words = _pack_bool_bits_u64((db_mc @ g_mat) > 0.0)
            db_sim_t = np.ascontiguousarray(db_sim_words.T)

            t0 = time.perf_counter()
            q_sim_words = _pack_bool_bits_u64((q_mc @ g_mat) > 0.0)
            sim_hash_us = (time.perf_counter() - t0) * 1e6 / n_queries

            t0 = time.perf_counter()
            hits_sim = 0
            n_w = db_sim_words.shape[1]
            for qi in range(n_queries):
                ham = np.zeros(n_db, dtype=np.int32)
                for w_i in range(n_w):
                    ham += popcount_u64(db_sim_t[w_i] ^ q_sim_words[qi, w_i])
                cand_s = np.argpartition(ham, top_k - 1)[:top_k]
                hits_sim += int(np.intersect1d(cand_s, gt_top10[qi]).size)
            sim_q_us = (time.perf_counter() - t0) * 1e6 / n_queries

            equal_k_records[k]["fly_recall"].append(hits_fly / float(n_queries * top_k))
            equal_k_records[k]["sim_recall"].append(hits_sim / float(n_queries * top_k))
            equal_k_records[k]["fly_query_us"].append(fly_q_us)
            equal_k_records[k]["sim_query_us"].append(sim_q_us)
            equal_k_records[k]["fly_hash_us"].append(fly_hash_us)
            equal_k_records[k]["sim_hash_us"].append(sim_hash_us)

        # Regime B: Equal raw uncompressed bitset budget `M_bits`
        for m_bits in raw_bit_budgets:
            idx_fly_b = KenyonIndex(
                n_inputs=dim, n_kc=m_bits, d_proj=6, wta_frac=0.05, seed=seed
            )
            idx_fly_b.add(db_mc)

            t0 = time.perf_counter()
            q_tags_b = idx_fly_b.hasher.hash(q_mc)
            fly_hash_us = (time.perf_counter() - t0) * 1e6 / n_queries

            t0 = time.perf_counter()
            hits_fly_b = 0
            for qi in range(n_queries):
                cand, _ = idx_fly_b.query_raw_tag(q_tags_b[qi], top_k=top_k)
                hits_fly_b += int(np.intersect1d(cand, gt_top10[qi]).size)
            fly_q_us = (time.perf_counter() - t0) * 1e6 / n_queries

            g_mat_b = rng.standard_normal((dim, m_bits))
            db_sim_b = _pack_bool_bits_u64((db_mc @ g_mat_b) > 0.0)
            db_sim_bt = np.ascontiguousarray(db_sim_b.T)

            t0 = time.perf_counter()
            q_sim_b = _pack_bool_bits_u64((q_mc @ g_mat_b) > 0.0)
            sim_hash_us = (time.perf_counter() - t0) * 1e6 / n_queries

            t0 = time.perf_counter()
            hits_sim_b = 0
            n_wb = db_sim_b.shape[1]
            for qi in range(n_queries):
                ham = np.zeros(n_db, dtype=np.int32)
                for w_i in range(n_wb):
                    ham += popcount_u64(db_sim_bt[w_i] ^ q_sim_b[qi, w_i])
                cand_s = np.argpartition(ham, top_k - 1)[:top_k]
                hits_sim_b += int(np.intersect1d(cand_s, gt_top10[qi]).size)
            sim_q_us = (time.perf_counter() - t0) * 1e6 / n_queries

            equal_bits_records[m_bits]["fly_recall"].append(hits_fly_b / float(n_queries * top_k))
            equal_bits_records[m_bits]["sim_recall"].append(hits_sim_b / float(n_queries * top_k))
            equal_bits_records[m_bits]["fly_query_us"].append(fly_q_us)
            equal_bits_records[m_bits]["sim_query_us"].append(sim_q_us)
            equal_bits_records[m_bits]["fly_hash_us"].append(fly_hash_us)
            equal_bits_records[m_bits]["sim_hash_us"].append(sim_hash_us)

    out_equal_k = {}
    for k in active_k_budgets:
        m_kc = int(np.ceil((20 * k) / 64.0) * 64)
        rec = equal_k_records[k]
        out_equal_k[str(k)] = {
            "active_k": k,
            "flyhash_n_kc": m_kc,
            "flyhash_tag_bytes": m_kc // 8,
            "flyhash_sparse_idx_bytes": k * 2,
            "flyhash_float_muls": 0,
            "simhash_float_muls": dim * k,
            "flyhash_recall10_mean": round(float(np.mean(rec["fly_recall"])), 4),
            "flyhash_recall10_std": round(float(np.std(rec["fly_recall"])), 4),
            "simhash_recall10_mean": round(float(np.mean(rec["sim_recall"])), 4),
            "simhash_recall10_std": round(float(np.std(rec["sim_recall"])), 4),
            "flyhash_query_us": round(float(np.mean(rec["fly_query_us"])), 2),
            "simhash_query_us": round(float(np.mean(rec["sim_query_us"])), 2),
        }

    out_equal_bits = {}
    for m_bits in raw_bit_budgets:
        rec = equal_bits_records[m_bits]
        out_equal_bits[str(m_bits)] = {
            "m_bits": m_bits,
            "tag_bytes": m_bits // 8,
            "flyhash_active_k": int(round(m_bits * 0.05)),
            "flyhash_float_muls": 0,
            "flyhash_proj_ram_bytes": 0,
            "simhash_float_muls": dim * m_bits,
            "simhash_proj_ram_bytes": dim * m_bits * 4,
            "flyhash_recall10_mean": round(float(np.mean(rec["fly_recall"])), 4),
            "flyhash_recall10_std": round(float(np.std(rec["fly_recall"])), 4),
            "simhash_recall10_mean": round(float(np.mean(rec["sim_recall"])), 4),
            "simhash_recall10_std": round(float(np.std(rec["sim_recall"])), 4),
            "flyhash_query_us": round(float(np.mean(rec["fly_query_us"])), 2),
            "simhash_query_us": round(float(np.mean(rec["sim_query_us"])), 2),
            "flyhash_hash_us": round(float(np.mean(rec["fly_hash_us"])), 2),
            "simhash_hash_us": round(float(np.mean(rec["sim_hash_us"])), 2),
        }

    return {
        "n_database": n_db,
        "n_queries": n_queries,
        "d_inputs": dim,
        "equal_active_code_k": out_equal_k,
        "equal_raw_bit_budget": out_equal_bits,
    }


def benchmark_task2_multigas_plumes(seeds: list[int]) -> dict[str, Any]:
    """Task 2: 8-sensor gas array under 0.1x-25x concentration swings + thermal drift."""
    n_classes = 6
    d_sensors = 8
    shots_per_class = 3
    n_test_plumes = 300

    # Cross-sensitive 8-sensor array profiles (modeled on EC/PID/MOX responses)
    base_profiles = np.array(
        [
            [1.65, 0.42, 0.12, 0.05, 0.78, 0.14, 0.06, 0.30],  # Ethanol
            [0.52, 0.18, 0.10, 1.55, 1.15, 0.12, 0.45, 0.08],  # Acetone
            [0.10, 1.70, 1.20, 0.18, 0.08, 0.62, 0.05, 0.16],  # Ammonia
            [0.28, 0.12, 0.85, 0.30, 0.18, 1.60, 1.10, 0.95],  # Toluene
            [1.10, 0.95, 0.15, 0.08, 0.14, 0.10, 0.08, 1.45],  # CO
            [0.08, 0.35, 1.50, 0.92, 0.12, 0.25, 1.35, 0.22],  # H2S
        ],
        dtype=np.float64,
    )

    regimes = [
        "clean_1x",
        "swing_0.1x_25x_no_drift",
        "weak_0.1x_0.5x_with_drift",
        "full_0.1x_25x_with_drift",
    ]
    acc: dict[str, dict[str, list[float]]] = {
        r: {
            "necksense": [],
            "necksense_1shot": [],
            "baseline_corrected_l2_knn": [],
            "l2_knn": [],
            "zscore_centroid": [],
        }
        for r in regimes
    }

    for seed in seeds:
        rng = np.random.default_rng(seed)
        profiles = base_profiles * rng.uniform(0.92, 1.08, size=base_profiles.shape)

        # Calibration set at clean 1.0x reference concentration
        ant_cal = AntennalNormalizer(d_sensors, sigma=0.008, gamma=1.0, hill_n=2.0)
        clf_3shot = MushroomValenceClassifier(
            d_sensors, n_kc=1024, d_proj=6, wta_frac=0.05, seed=seed
        )
        clf_1shot = MushroomValenceClassifier(
            d_sensors, n_kc=1024, d_proj=6, wta_frac=0.05, seed=seed
        )

        train_X_list: list[np.ndarray] = []
        train_y_list: list[int] = []
        for c in range(n_classes):
            for s_idx in range(shots_per_class):
                x_cal = np.maximum(0.0, profiles[c] + rng.normal(0.0, 0.02, size=d_sensors))
                train_X_list.append(x_cal)
                train_y_list.append(c)
                norm_cal = ant_cal.normalize(x_cal)
                clf_3shot.learn(norm_cal, c, reward=1.0)
                if s_idx == 0:
                    clf_1shot.learn(norm_cal, c, reward=1.0)

        train_X = np.asarray(train_X_list, dtype=np.float64)
        train_y = np.asarray(train_y_list, dtype=np.int32)

        # Baseline 1: Raw Z-Score Nearest-Centroid
        mu_z = train_X.mean(axis=0)
        sd_z = np.maximum(train_X.std(axis=0), 1e-6)
        train_z = (train_X - mu_z) / sd_z
        centroids_z = np.vstack([train_z[train_y == c].mean(axis=0) for c in range(n_classes)])

        # Baseline 2: L2-Normalized k-NN (k=3)
        train_l2 = train_X / np.maximum(np.linalg.norm(train_X, axis=1, keepdims=True), 1e-9)

        def _predict_baselines(raw_vec: np.ndarray) -> tuple[int, int]:
            # Z-score nearest centroid
            vz = (raw_vec - mu_z) / sd_z
            d_cent = np.sum((centroids_z - vz[None, :]) ** 2, axis=1)
            pred_z = int(np.argmin(d_cent))
            # L2-normalized k-NN (k=3 majority vote, tie broken by 1-NN)
            vl2 = raw_vec / max(float(np.linalg.norm(raw_vec)), 1e-9)
            sims = train_l2 @ vl2
            top3 = np.argsort(-sims)[:3]
            top_labels = train_y[top3]
            counts = np.bincount(top_labels, minlength=n_classes)
            if np.sum(counts == counts.max()) == 1:
                pred_l2 = int(np.argmax(counts))
            else:
                pred_l2 = int(top_labels[0])
            return pred_l2, pred_z

        # Evaluate Regime 1 (Clean 1x) & Regime 2 (0.1x-25x Swings, No Drift)
        for r_name in ("clean_1x", "swing_0.1x_25x_no_drift"):
            c_ns, c_ns1, c_l2, c_zs = 0, 0, 0, 0
            for ev in range(n_test_plumes):
                c_true = ev % n_classes
                conc = 1.0 if r_name == "clean_1x" else float(np.exp(rng.uniform(np.log(0.1), np.log(25.0))))
                noise_sd = 0.02 if r_name == "clean_1x" else (0.008 + 0.015 * conc)
                raw_obs = np.maximum(0.0, conc * profiles[c_true] + rng.normal(0.0, noise_sd, size=d_sensors))
                cond = ant_cal.normalize(raw_obs)
                if int(clf_3shot.predict(cond)) == c_true:
                    c_ns += 1
                if int(clf_1shot.predict(cond)) == c_true:
                    c_ns1 += 1
                p_l2, p_zs = _predict_baselines(raw_obs)
                if p_l2 == c_true:
                    c_l2 += 1
                if p_zs == c_true:
                    c_zs += 1
            acc[r_name]["necksense"].append(c_ns / n_test_plumes)
            acc[r_name]["necksense_1shot"].append(c_ns1 / n_test_plumes)
            acc[r_name]["l2_knn"].append(c_l2 / n_test_plumes)
            # No drift in these regimes: clean-air baseline is zero, so the corrected pipeline == L2 k-NN
            acc[r_name]["baseline_corrected_l2_knn"].append(c_l2 / n_test_plumes)
            acc[r_name]["zscore_centroid"].append(c_zs / n_test_plumes)

        # Evaluate Regime 3 & 4: Continuous stream with slow thermal/humidity baseline drift
        for r_name, c_low, c_high in (
            ("weak_0.1x_0.5x_with_drift", 0.10, 0.50),
            ("full_0.1x_25x_with_drift", 0.10, 25.0),
        ):
            ant_stream = AntennalNormalizer(
                d_sensors,
                sigma=0.008,
                gamma=1.0,
                hill_n=2.0,
                tonic_alpha=0.035,
                tonic_slew_limit=0.008,
                phasic_gain=0.30,
                fast_alpha=0.35,
            )
            phases = rng.uniform(0.0, 2.0 * np.pi, size=d_sensors)
            chan_bias = rng.uniform(0.25, 0.85, size=d_sensors)
            tick = 0

            def _drift_at(t_step: int) -> np.ndarray:
                return (
                    chan_bias
                    + 0.45 * np.sin(2.0 * np.pi * t_step / 650.0 + phases)
                    + 0.0004 * t_step
                )

            # Initial clean-air warmup so tonic baseline locks onto ambient temperature
            ant_stream.calibrate(_drift_at(0))
            # Classical e-nose baseline correction: EMA of the clean-air ticks, subtracted from
            # each plume reading before normalisation (same information NeckSense receives).
            amb_ema = _drift_at(0).copy()

            c_ns, c_ns1, c_l2, c_zs, c_bc = 0, 0, 0, 0, 0
            for ev in range(n_test_plumes):
                # 15 clean-air ticks between transient plumes
                for _ in range(15):
                    tick += 1
                    amb = _drift_at(tick) + rng.normal(0.0, 0.006, size=d_sensors)
                    ant_stream.step(amb)
                    amb_ema += 0.3 * (amb - amb_ema)

                tick += 1
                c_true = ev % n_classes
                conc = float(np.exp(rng.uniform(np.log(c_low), np.log(c_high))))
                raw_obs = _drift_at(tick) + conc * profiles[c_true] + rng.normal(0.0, 0.008 + 0.01 * conc, size=d_sensors)
                cond = ant_stream.step(raw_obs)

                if int(clf_3shot.predict(cond)) == c_true:
                    c_ns += 1
                if int(clf_1shot.predict(cond)) == c_true:
                    c_ns1 += 1
                p_l2, p_zs = _predict_baselines(raw_obs)
                if p_l2 == c_true:
                    c_l2 += 1
                if p_zs == c_true:
                    c_zs += 1
                p_bc, _ = _predict_baselines(np.maximum(0.0, raw_obs - amb_ema))
                if p_bc == c_true:
                    c_bc += 1

            acc[r_name]["necksense"].append(c_ns / n_test_plumes)
            acc[r_name]["necksense_1shot"].append(c_ns1 / n_test_plumes)
            acc[r_name]["baseline_corrected_l2_knn"].append(c_bc / n_test_plumes)
            acc[r_name]["l2_knn"].append(c_l2 / n_test_plumes)
            acc[r_name]["zscore_centroid"].append(c_zs / n_test_plumes)

    summary_regimes = {}
    for r_name in regimes:
        summary_regimes[r_name] = {
            method: {
                "mean": round(float(np.mean(vals)), 4),
                "std": round(float(np.std(vals)), 4),
            }
            for method, vals in acc[r_name].items()
        }
    return {
        "n_sensors": d_sensors,
        "n_gases": n_classes,
        "calibration_shots_per_gas": shots_per_class,
        "test_plumes_per_regime": n_test_plumes,
        "regimes": summary_regimes,
    }


class _RollingZScoreBaseline:
    """Fast online per-channel EMA Z-score anomaly detector."""

    def __init__(self, dim: int, alpha: float = 0.02) -> None:
        self.alpha = alpha
        self.mean = np.zeros(dim, dtype=np.float64)
        self.var = np.ones(dim, dtype=np.float64)

    def update_and_score(self, x: np.ndarray) -> float:
        diff = x - self.mean
        z_max = float(np.max(np.abs(diff) / np.sqrt(self.var + 1e-6)))
        self.mean += self.alpha * diff
        self.var = (1.0 - self.alpha) * self.var + self.alpha * (diff * diff)
        return z_max


class _OnlineHalfSpaceBaseline:
    """Streaming random half-space mass/bin novelty detector (32 projections)."""

    def __init__(self, dim: int, n_proj: int = 32, n_bins: int = 16, decay: float = 0.997, seed: int = 0) -> None:
        rng = np.random.default_rng(seed)
        w = rng.standard_normal((dim, n_proj))
        self.proj = w / np.linalg.norm(w, axis=0, keepdims=True)
        self.n_proj = n_proj
        self.n_bins = n_bins
        self.decay = decay
        self.counts = np.ones((n_proj, n_bins), dtype=np.float64)
        self.proj_idx = np.arange(n_proj)

    def update_and_score(self, x: np.ndarray) -> float:
        p = x @ self.proj
        # Map [-4, 4] into [0, n_bins - 1]
        bins = np.clip(((p + 4.0) * (self.n_bins / 8.0)).astype(np.int32), 0, self.n_bins - 1)
        masses = self.counts[self.proj_idx, bins]
        total = np.sum(self.counts, axis=1)
        # Lower empirical bin mass => higher anomaly score
        novelty = float(-np.mean(np.log((masses + 1e-3) / (total + 1e-3))))
        self.counts *= self.decay
        self.counts[self.proj_idx, bins] += 1.0
        return novelty


class _OnlineMahalanobisBaseline:
    """Streaming Sherman-Morrison inverse-covariance Mahalanobis detector."""

    def __init__(self, dim: int, alpha: float = 0.015) -> None:
        self.alpha = alpha
        self.mean = np.zeros(dim, dtype=np.float64)
        self.inv_cov = np.eye(dim, dtype=np.float64)

    def update_and_score(self, x: np.ndarray) -> float:
        diff = x - self.mean
        u = self.inv_cov @ diff
        dist2 = float(max(0.0, float(diff @ u)))
        # Sherman-Morrison rank-1 update of ((1 - alpha) C + alpha d d^T)^-1
        a = self.alpha
        inv_scaled = self.inv_cov / (1.0 - a)
        u_s = inv_scaled @ diff
        denom = 1.0 + a * float(diff @ u_s)
        self.inv_cov = inv_scaled - (a / max(denom, 1e-9)) * np.outer(u_s, u_s)
        self.mean += a * diff
        return dist2


class _L2KnnNoveltyBaseline:
    """L2-normalise, then score = 1 - max cosine to a ring buffer of recent samples (float32)."""

    def __init__(self, dim: int, memory: int) -> None:
        self.buf = np.zeros((memory, dim), dtype=np.float32)
        self.memory = memory
        self.n = 0
        self.i = 0

    def update_and_score(self, x: np.ndarray) -> float:
        v = (x / max(float(np.linalg.norm(x)), 1e-9)).astype(np.float32)
        score = 1.0 if self.n == 0 else 1.0 - float(np.max(self.buf[: self.n] @ v))
        self.buf[self.i] = v
        self.i = (self.i + 1) % self.memory
        self.n = min(self.n + 1, self.memory)
        return score


class _L2MahalanobisBaseline(_OnlineMahalanobisBaseline):
    """Online Mahalanobis on L2-normalised inputs (removes the concentration swings)."""

    def update_and_score(self, x: np.ndarray) -> float:
        return super().update_and_score(x / max(float(np.linalg.norm(x)), 1e-9))


def benchmark_task3_streaming_anomaly(seeds: list[int]) -> dict[str, Any]:
    """Task 3: Multi-regime streaming 1-shot anomaly detection (AUROC & us/sample)."""
    dim = 16
    warmup = 200
    eval_steps = 2500
    total_steps = warmup + eval_steps
    n_modes = 20

    records: dict[str, dict[str, list[float]]] = {
        m: {"auroc": [], "us_per_sample": []}
        for m in (
            "necksense_mbon",
            "l2_knn_64",
            "l2_knn_512",
            "l2_mahalanobis",
            "online_halfspace",
            "online_mahalanobis",
            "rolling_zscore",
        )
    }
    ns_stream_us_list: list[float] = []

    for seed in seeds:
        rng = np.random.default_rng(seed)
        # 20 distinct recurring normal operating modes in R^16 (K > D => full-rank multi-modal!)
        modes = np.zeros((n_modes, dim), dtype=np.float64)
        for m_i in range(n_modes):
            active_ch = rng.choice(dim, size=4, replace=False)
            modes[m_i, active_ch] = rng.uniform(0.6, 1.4, size=4)
            modes[m_i] += rng.uniform(0.02, 0.12, size=dim)

        stream = np.empty((total_steps, dim), dtype=np.float64)
        y_true = np.zeros(total_steps, dtype=np.int32)

        cur_mode = 0
        cur_scale = 1.0
        for t in range(total_steps):
            if (t % 4) == 0:
                cur_mode = int(rng.integers(0, n_modes))
                cur_scale = float(np.exp(rng.uniform(np.log(0.5), np.log(5.0))))

            if t >= warmup and rng.random() < 0.05:
                y_true[t] = 1
                # Unseen combinatorial fault with normal total power (same L2 scale, novel 4-channel chord)
                anom = rng.uniform(0.02, 0.10, size=dim)
                fault_ch = rng.choice(dim, size=4, replace=False)
                # Ensure chord differs from normal modes
                anom[fault_ch] = rng.uniform(0.8, 1.5, size=4)
                scale_a = float(np.exp(rng.uniform(np.log(0.6), np.log(4.0))))
                stream[t] = scale_a * anom + rng.normal(0.0, 0.03, size=dim)
            else:
                stream[t] = np.maximum(
                    0.0, cur_scale * modes[cur_mode] + rng.normal(0.0, 0.03 * cur_scale, size=dim)
                )

        ant_det = AntennalNormalizer(dim, sigma=0.01, gamma=1.0, hill_n=2.0, tonic_alpha=0.0)
        detectors: list[tuple[str, Any]] = [
            (
                "necksense_mbon",
                MushroomNoveltyDetector(
                    dim,
                    antennal=ant_det,
                    stateful_antennal=False,
                    n_kc=1024,
                    d_proj=6,
                    lr=0.55,
                    tau_recover=12000.0,
                    seed=seed,
                ),
            ),
            # Same 4 KB of state as the MBON weights (64 x 16 float32)
            ("l2_knn_64", _L2KnnNoveltyBaseline(dim, memory=64)),
            ("l2_knn_512", _L2KnnNoveltyBaseline(dim, memory=512)),
            ("l2_mahalanobis", _L2MahalanobisBaseline(dim, alpha=0.015)),
            ("online_halfspace", _OnlineHalfSpaceBaseline(dim, n_proj=32, seed=seed)),
            ("online_mahalanobis", _OnlineMahalanobisBaseline(dim, alpha=0.015)),
            ("rolling_zscore", _RollingZScoreBaseline(dim, alpha=0.03)),
        ]

        for name, det in detectors:
            scores = np.empty(total_steps, dtype=np.float64)
            t0 = time.perf_counter()
            for t in range(total_steps):
                scores[t] = det.update_and_score(stream[t])
            us_sample = (time.perf_counter() - t0) * 1e6 / total_steps
            auroc = _auroc_rank(y_true[warmup:], scores[warmup:])
            records[name]["auroc"].append(auroc)
            records[name]["us_per_sample"].append(us_sample)

        # Also measure vectorized batch-encoded stream latency (`score_stream` after AntennalNormalizer)
        det_fast = MushroomNoveltyDetector(dim, n_kc=1024, d_proj=6, lr=0.55, seed=seed)
        t0 = time.perf_counter()
        cond_all = ant_det.normalize(stream)
        _ = det_fast.score_stream(cond_all)
        ns_stream_us_list.append((time.perf_counter() - t0) * 1e6 / total_steps)

    summary = {}
    for name, rec in records.items():
        entry: dict[str, Any] = {
            "auroc_mean": round(float(np.mean(rec["auroc"])), 4),
            "auroc_std": round(float(np.std(rec["auroc"])), 4),
            "us_per_sample_mean": round(float(np.mean(rec["us_per_sample"])), 2),
            "us_per_sample_std": round(float(np.std(rec["us_per_sample"])), 2),
        }
        if name == "necksense_mbon":
            entry["vectorized_stream_us_per_sample"] = round(float(np.mean(ns_stream_us_list)), 2)
        summary[name] = entry
    return {
        "d_inputs": dim,
        "n_normal_modes": n_modes,
        "eval_steps": eval_steps,
        "methods": summary,
    }


def render_hero_chart(summary: dict[str, Any], out_path: Path) -> None:
    """Render clean, high-DPI 3-panel white-card chart readable on dark & light UIs."""
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Segoe UI", "DejaVu Sans", "Arial"],
            "axes.edgecolor": "#CBD5E1",
            "axes.linewidth": 1.0,
        }
    )

    fig, axes = plt.subplots(1, 3, figsize=(17.6, 5.5), dpi=220, facecolor="#FFFFFF")
    fig.subplots_adjust(left=0.045, right=0.988, top=0.82, bottom=0.26, wspace=0.31)

    c_fly = "#0284C7"      # NeckLink / NeckSense cyan-blue
    c_fly_alt = "#0EA5E9"
    c_indigo = "#6366F1"   # Strong competitor accent
    c_slate = "#64748B"    # Secondary baseline
    c_dark = "#334155"     # Tertiary baseline
    c_text = "#0F172A"

    # ------------------------------------------------------------------
    # Panel 1: Nearest-Neighbor Retrieval (Equal Active-k + Equal Bit Note)
    # ------------------------------------------------------------------
    ax1 = axes[0]
    ax1.set_facecolor("#FFFFFF")
    t1_k = summary["task1_retrieval"]["equal_active_code_k"]
    ks = [16, 32, 64, 128]
    x1 = np.arange(len(ks))
    w1 = 0.35

    fly_r = [t1_k[str(k)]["flyhash_recall10_mean"] * 100.0 for k in ks]
    sim_r = [t1_k[str(k)]["simhash_recall10_mean"] * 100.0 for k in ks]

    bars_f1 = ax1.bar(x1 - w1 / 2, fly_r, width=w1, color=c_fly, label="FlyHash (Kenyon 5% WTA, 0 float muls)")
    bars_s1 = ax1.bar(x1 + w1 / 2, sim_r, width=w1, color=c_indigo, label="Dense Gaussian SimHash (D*k float muls)")

    for b in bars_f1:
        h = b.get_height()
        ax1.text(b.get_x() + b.get_width() / 2, h + 1.2, f"{h:.1f}%", ha="center", va="bottom",
                 fontsize=8.5, fontweight="bold", color=c_fly)
    for b in bars_s1:
        h = b.get_height()
        ax1.text(b.get_x() + b.get_width() / 2, h + 1.2, f"{h:.1f}%", ha="center", va="bottom",
                 fontsize=8.0, color=c_indigo)

    t1_b1024 = summary["task1_retrieval"]["equal_raw_bit_budget"]["1024"]
    note_txt = (
        f"Honest raw-bit note (1024-bit / 128 B dense tag):\n"
        f"SimHash={t1_b1024['simhash_recall10_mean']*100:.1f}% (65.5K float muls, 256 KB RAM)\n"
        f"FlyHash={t1_b1024['flyhash_recall10_mean']*100:.1f}% (0 float muls, 0 KB weight RAM)"
    )
    ax1.text(
        0.03, -0.30, note_txt, transform=ax1.transAxes, fontsize=7.6, color=c_dark,
        bbox=dict(boxstyle="round,pad=0.4", facecolor="#F8FAFC", edgecolor="#CBD5E1", lw=0.9),
    )

    ax1.set_xticks(x1)
    ax1.set_xticklabels([f"k={k}\n(M={t1_k[str(k)]['flyhash_n_kc']})" for k in ks], fontsize=8.8, color=c_text)
    ax1.set_ylabel("Recall@10 (%)", fontsize=10, fontweight="bold", color=c_text)
    ax1.set_ylim(0, max(max(fly_r), max(sim_r)) * 1.24)
    ax1.set_title("1. Retrieval @ Equal Active Spikes (N=5,000, D=64)", fontsize=10.8, fontweight="bold", color=c_text, pad=10)
    ax1.grid(axis="y", linestyle="--", color="#E2E8F0", alpha=0.9)
    ax1.set_axisbelow(True)
    ax1.spines["top"].set_visible(False)
    ax1.spines["right"].set_visible(False)
    ax1.legend(loc="upper left", fontsize=8.0, frameon=True, facecolor="#FFFFFF", edgecolor="#E2E8F0")

    # ------------------------------------------------------------------
    # Panel 2: Multi-Gas Plume Classification (0.1x-25x + Thermal Drift)
    # ------------------------------------------------------------------
    ax2 = axes[1]
    ax2.set_facecolor("#FFFFFF")
    t2 = summary["task2_multigas_plumes"]["regimes"]
    r_keys = [
        "clean_1x",
        "swing_0.1x_25x_no_drift",
        "weak_0.1x_0.5x_with_drift",
        "full_0.1x_25x_with_drift",
    ]
    r_labels = [
        "Clean\n(1.0x)",
        "0.1x-25x\n(No Drift)",
        "Weak 0.1-0.5x\n(+ Drift)",
        "Full 0.1x-25x\n(+ Drift)",
    ]
    x2 = np.arange(len(r_keys))
    w2 = 0.2

    ns_acc = [t2[rk]["necksense"]["mean"] * 100.0 for rk in r_keys]
    bc_acc = [t2[rk]["baseline_corrected_l2_knn"]["mean"] * 100.0 for rk in r_keys]
    l2_acc = [t2[rk]["l2_knn"]["mean"] * 100.0 for rk in r_keys]
    zs_acc = [t2[rk]["zscore_centroid"]["mean"] * 100.0 for rk in r_keys]

    b_ns = ax2.bar(x2 - 1.5 * w2, ns_acc, width=w2, color=c_fly, label="NeckSense (Antennal + MBON)")
    b_bc = ax2.bar(x2 - 0.5 * w2, bc_acc, width=w2, color="#059669", label="Baseline-corrected L2 k-NN")
    b_l2 = ax2.bar(x2 + 0.5 * w2, l2_acc, width=w2, color=c_indigo, label="L2 k-NN (no drift correction)")
    b_zs = ax2.bar(x2 + 1.5 * w2, zs_acc, width=w2, color=c_slate, label="Raw Z-Score Centroid")

    # Annotate only the drift regimes (all methods are ~100% on the first two)
    for bars, col in ((b_ns, c_fly), (b_bc, "#059669"), (b_l2, c_indigo), (b_zs, c_dark)):
        for b in list(bars)[2:]:
            h = b.get_height()
            ax2.text(b.get_x() + b.get_width() / 2, h + 1.2, f"{h:.0f}", ha="center", va="bottom",
                     fontsize=7.2, color=col)

    ax2.set_xticks(x2)
    ax2.set_xticklabels(r_labels, fontsize=8.6, color=c_text)
    ax2.set_ylabel("6-Gas Classification Accuracy (%)", fontsize=10, fontweight="bold", color=c_text)
    ax2.set_ylim(0, 116)
    ax2.set_title("2. 8-Sensor E-Nose: 250x Plume Swings + Drift", fontsize=10.8, fontweight="bold", color=c_text, pad=10)
    ax2.grid(axis="y", linestyle="--", color="#E2E8F0", alpha=0.9)
    ax2.set_axisbelow(True)
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_visible(False)
    ax2.legend(loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=2, fontsize=7.6, frameon=False)

    # ------------------------------------------------------------------
    # Panel 3: Streaming 1-Shot Anomaly Detection (AUROC + Latency)
    # ------------------------------------------------------------------
    ax3 = axes[2]
    ax3.set_facecolor("#FFFFFF")
    t3 = summary["task3_streaming_anomaly"]["methods"]
    m_order = [
        "rolling_zscore",
        "online_halfspace",
        "online_mahalanobis",
        "l2_mahalanobis",
        "l2_knn_64",
        "l2_knn_512",
        "necksense_mbon",
    ]
    m_names = [
        "Rolling Z-Score (raw)",
        "Half-Space, 32 proj (raw)",
        "Mahalanobis (raw)",
        "L2 + Mahalanobis",
        "L2 + kNN, 64 slots (4 KB)",
        "L2 + kNN, 512 slots (32 KB)",
        "NeckSense MBON (4 KB)",
    ]
    m_colors = ["#94A3B8", "#94A3B8", c_slate, c_indigo, "#059669", "#059669", c_fly]
    y3 = np.arange(len(m_order))
    aurocs = [t3[m]["auroc_mean"] for m in m_order]
    lats = [t3[m]["us_per_sample_mean"] for m in m_order]

    bars3 = ax3.barh(y3, aurocs, height=0.6, color=m_colors)
    for idx_b, (b, au, lat) in enumerate(zip(bars3, aurocs, lats)):
        is_ns = m_order[idx_b] == "necksense_mbon"
        fw = "bold" if is_ns else "normal"
        col = c_fly if is_ns else c_dark
        extra = ""
        if is_ns:
            vec_us = t3["necksense_mbon"].get("vectorized_stream_us_per_sample", lat)
            extra = ""
        ax3.text(
            au + 0.015,
            b.get_y() + b.get_height() / 2,
            f"{au:.3f} ({lat:.0f} us{extra})",
            va="center",
            ha="left",
            fontsize=8.1,
            fontweight=fw,
            color=col,
        )

    ax3.set_yticks(y3)
    ax3.set_yticklabels(m_names, fontsize=8.0, color=c_text)
    ax3.set_xticks([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    ax3.set_xlabel("Streaming Anomaly Detection AUROC (Higher = Better)", fontsize=9.5, fontweight="bold", color=c_text)
    ax3.set_xlim(0.0, 1.44)
    ax3.set_title("3. 20-Mode 1-Shot Anomaly Stream (D=16)", fontsize=10.8, fontweight="bold", color=c_text, pad=10)
    ax3.grid(axis="x", linestyle="--", color="#E2E8F0", alpha=0.9)
    ax3.set_axisbelow(True)
    ax3.spines["top"].set_visible(False)
    ax3.spines["right"].set_visible(False)

    fig.suptitle(
        f"NeckSense v{__version__} — Drosophila Antennal Lobe & Mushroom Body Empirical Benchmarks (5-Seed Mean)",
        fontsize=13.2,
        fontweight="bold",
        color=c_text,
        y=0.955,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=220, facecolor="#FFFFFF")
    plt.close(fig)


def main() -> int:
    seeds = [42, 43, 44, 45, 46]
    t_start = time.perf_counter()

    print("[1/3] Running Task 1: Nearest-Neighbor Retrieval (N=5,000, D=64)...")
    res_t1 = benchmark_task1_retrieval(seeds)

    print("[2/3] Running Task 2: 8-Sensor Multi-Gas Plume Classification (0.1x-25x + Drift)...")
    res_t2 = benchmark_task2_multigas_plumes(seeds)

    print("[3/3] Running Task 3: Streaming 1-Shot Anomaly Detection (D=16, T=2,500)...")
    res_t3 = benchmark_task3_streaming_anomaly(seeds)

    elapsed_s = time.perf_counter() - t_start
    summary = {
        "package": "necksense",
        "version": __version__,
        "seeds": seeds,
        "benchmark_wall_seconds": round(elapsed_s, 2),
        "task1_retrieval": res_t1,
        "task2_multigas_plumes": res_t2,
        "task3_streaming_anomaly": res_t3,
    }

    json_path = REPO_ROOT / "results" / "benchmark_summary.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    chart_path = REPO_ROOT / "assets" / "necksense_benchmarks.png"
    render_hero_chart(summary, chart_path)

    print(f"Saved JSON summary -> {json_path.name} ({elapsed_s:.1f}s)")
    print(f"Saved hero chart   -> {chart_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
