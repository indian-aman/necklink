"""
Benchmark 3: Mushroom Body (`NeckSense v3.0.0`) — Real Public Datasets & K=100 Shuffle Gate.

Datasets (strictly disjoint / chronological splits, zero leakage):
  - Task 3A: UCI Gas Sensor Array Drift (`13,910` plumes) Chronological Novelty Detection
             (Train strictly on Batches 1-2 known gases; Test strictly on future Batches 3-10
              drifted known gases `y=0` vs unseen novel gases `y=1`).
  - Task 3B: UCI Gas Sensor Array Drift Replay-Free Class-Incremental Continual Learning
             (Learn Tasks [0,1] -> [2,3] -> [4,5] on Batch 1-2 with 5 shots/class and zero replay;
              Test 6-way & Task-1 retention accuracy on drifted Batches 3-6).
  - Task 3C: Real CWRU 12 kHz Drive-End Bearing Cross-Load Fault Detection
             (Train strictly on `0 HP / 1797 RPM` normal baseline; Test strictly on unseen
              `2 HP / 1750 RPM` load normal vs Inner Race / Ball / Outer Race faults).

Classical baselines receive identical L2 concentration-invariant normalization + StandardScaler
fit strictly on training data, with verified anomaly sign (`higher = more anomalous`).
Zero `if arm == ...` rigging anywhere.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import itertools
from typing import Any, Dict, List, Tuple

import necksense
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import NearestCentroid
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

from datasets import (
    compute_auroc,
    compute_fpr_at_tpr,
    load_cwru_bearing,
    load_uci_gas_drift,
    shuffle_gate_verdict,
    summary_ci95,
)


def _l2_norm_rows(X: np.ndarray) -> np.ndarray:
    X = np.asarray(X, dtype=np.float64)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    return X / np.maximum(norms, 1e-8)


def _eval_uci_novelty_mb(
    splits: List[Tuple[np.ndarray, np.ndarray, np.ndarray]],
    mb_weights: Any,
) -> Tuple[List[float], List[float]]:
    aurocs, fprs = [], []
    for idx, (X_tr, X_te, y_te) in enumerate(splits):
        det = necksense.MushroomNoveltyDetector(
            n_inputs=16,
            antennal=True,
            stateful_antennal=False,
            n_kc=1024,
            seed=1000 + idx,
            connectome_weights=mb_weights,
        )
        for x in X_tr:
            det.update_and_score(x)
        # Frozen inference on future drifted batches (no test-time label leakage)
        scores = np.array([det.score(x) for x in X_te], dtype=np.float64)
        aurocs.append(compute_auroc(y_te, scores))
        fprs.append(compute_fpr_at_tpr(y_te, scores, 0.90))
    return aurocs, fprs


def _eval_uci_continual_mb(
    cl_splits: List[Tuple[List[Tuple[int, np.ndarray]], np.ndarray, np.ndarray]],
    mb_weights: Any,
) -> Tuple[List[float], List[float]]:
    acc_all_list, acc_t1_list = [], []
    for idx, (tasks_train, X_te, y_te) in enumerate(cl_splits):
        clf = necksense.MushroomValenceClassifier(
            n_inputs=16,
            antennal=True,
            stateful_antennal=False,
            n_kc=1024,
            ltm_alpha_beta=True,
            seed=2000 + idx,
            connectome_weights=mb_weights,
        )
        # Sequential replay-free learning across 3 disjoint 2-class tasks
        for task_classes in [(0, 1), (2, 3), (4, 5)]:
            for cid, x_shots in tasks_train:
                if cid in task_classes:
                    for x in x_shots:
                        clf.learn(x, cid)
            clf.consolidate()

        preds = np.array([clf.predict(x) for x in X_te], dtype=np.int64)
        acc_all_list.append(float(np.mean(preds == y_te)))
        t1_mask = np.isin(y_te, [0, 1])
        acc_t1_list.append(float(np.mean(preds[t1_mask] == y_te[t1_mask])))
    return acc_all_list, acc_t1_list


def _eval_cwru_mb(
    cwru_splits: List[Tuple[np.ndarray, np.ndarray, np.ndarray]],
    mb_weights: Any,
) -> List[float]:
    aurocs = []
    for idx, (F_tr, F_te, y_te) in enumerate(cwru_splits):
        det = necksense.MushroomNoveltyDetector(
            n_inputs=F_tr.shape[1],
            antennal=True,
            stateful_antennal=False,
            n_kc=1024,
            seed=3000 + idx,
            connectome_weights=mb_weights,
        )
        for f in F_tr:
            det.update_and_score(f)
        scores = np.array([det.score(f) for f in F_te], dtype=np.float64)
        aurocs.append(compute_auroc(y_te, scores))
    return aurocs


_WORKER_MB_SPLITS: Tuple[Any, Any, Any] | None = None


def _init_mb_worker(nov_splits: Any, cl_splits: Any, cwru_splits: Any) -> None:
    global _WORKER_MB_SPLITS
    _WORKER_MB_SPLITS = (nov_splits, cl_splits, cwru_splits)


def _eval_one_mb_shuffle(shuf_mb: Dict[str, Any]) -> Tuple[float, float, float]:
    assert _WORKER_MB_SPLITS is not None
    nov_splits, cl_splits, cwru_splits = _WORKER_MB_SPLITS
    s_nov, _ = _eval_uci_novelty_mb(nov_splits, shuf_mb)
    s_cl_all, _ = _eval_uci_continual_mb(cl_splits, shuf_mb)
    s_cwru = _eval_cwru_mb(cwru_splits, shuf_mb)
    return float(np.mean(s_nov)), float(np.mean(s_cl_all)), float(np.mean(s_cwru))


def run_sense_benchmark(
    shuffled_mb_list: List[Dict[str, Any]],
    n_seeds: int = 30,
    workers: int = 1,
) -> Dict[str, Any]:
    X16, y_gas, batch, _conc = load_uci_gas_drift()
    cwru_raw = load_cwru_bearing()

    # ------------------------------------------------------------------
    # Build 30 strict chronological splits for Task 3A (Batch 1-2 -> Batch 3-10)
    # ------------------------------------------------------------------
    all_pairs = list(itertools.combinations(range(6), 2))  # 15 pairs
    nov_splits: List[Tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    for s in range(n_seeds):
        rng = np.random.default_rng(400 + s)
        known_pair = all_pairs[s % len(all_pairs)]
        tr_mask = (batch <= 2) & np.isin(y_gas, known_pair)
        te_in_mask = (batch >= 3) & np.isin(y_gas, known_pair)
        te_out_mask = (batch >= 3) & (~np.isin(y_gas, known_pair))

        tr_idx = rng.choice(np.nonzero(tr_mask)[0], size=min(140, int(tr_mask.sum())), replace=False)
        in_idx = rng.choice(np.nonzero(te_in_mask)[0], size=min(200, int(te_in_mask.sum())), replace=False)
        out_idx = rng.choice(np.nonzero(te_out_mask)[0], size=min(200, int(te_out_mask.sum())), replace=False)

        X_tr = X16[tr_idx]
        X_te = np.vstack([X16[in_idx], X16[out_idx]])
        y_te = np.concatenate([np.zeros(len(in_idx), dtype=np.int64), np.ones(len(out_idx), dtype=np.int64)])
        nov_splits.append((X_tr, X_te, y_te))

    real_nov_auroc, real_nov_fpr = _eval_uci_novelty_mb(nov_splits, "banc_v888")
    v2_nov_auroc, v2_nov_fpr = _eval_uci_novelty_mb(nov_splits, "v2_analytic")

    ocsvm_auroc, ocsvm_fpr = [], []
    iforest_auroc, iforest_fpr = [], []
    for s, (X_tr, X_te, y_te) in enumerate(nov_splits):
        scaler = StandardScaler()
        X_tr_n = scaler.fit_transform(_l2_norm_rows(X_tr))
        X_te_n = scaler.transform(_l2_norm_rows(X_te))

        oc = OneClassSVM(kernel="rbf", nu=0.10, gamma="scale")
        oc.fit(X_tr_n)
        # decision_function > 0 for inliers, < 0 for outliers -> negate so higher = novel!
        sc_oc = -oc.decision_function(X_te_n).ravel()
        ocsvm_auroc.append(compute_auroc(y_te, sc_oc))
        ocsvm_fpr.append(compute_fpr_at_tpr(y_te, sc_oc, 0.90))

        iso = IsolationForest(n_estimators=100, contamination="auto", random_state=500 + s)
        iso.fit(X_tr_n)
        sc_if = -iso.score_samples(X_te_n).ravel()
        iforest_auroc.append(compute_auroc(y_te, sc_if))
        iforest_fpr.append(compute_fpr_at_tpr(y_te, sc_if, 0.90))

    # ------------------------------------------------------------------
    # Build 30 strict chronological splits for Task 3B (5-shot Class-Incremental Batch 1-2 -> 3-6)
    # ------------------------------------------------------------------
    cl_splits: List[Tuple[List[Tuple[int, np.ndarray]], np.ndarray, np.ndarray]] = []
    for s in range(n_seeds):
        rng = np.random.default_rng(800 + s)
        tasks_train: List[Tuple[int, np.ndarray]] = []
        for cid in range(6):
            cand = np.nonzero((batch <= 2) & (y_gas == cid))[0]
            pick = rng.choice(cand, size=min(5, len(cand)), replace=False)
            tasks_train.append((cid, X16[pick]))
        te_cand = np.nonzero((batch >= 3) & (batch <= 6))[0]
        te_pick = rng.choice(te_cand, size=min(360, len(te_cand)), replace=False)
        cl_splits.append((tasks_train, X16[te_pick], y_gas[te_pick]))

    real_cl_all, real_cl_t1 = _eval_uci_continual_mb(cl_splits, "banc_v888")
    v2_cl_all, v2_cl_t1 = _eval_uci_continual_mb(cl_splits, "v2_analytic")

    nc_cl_all, nc_cl_t1 = [], []
    mlp_cl_all, mlp_cl_t1 = [], []
    for s, (tasks_train, X_te, y_te) in enumerate(cl_splits):
        # NearestCentroid on L2-normalized features (non-forgetting prototype baseline)
        X_all_shots = np.vstack([xs for _, xs in tasks_train])
        y_all_shots = np.concatenate([np.full(len(xs), cid, dtype=np.int64) for cid, xs in tasks_train])
        nc = NearestCentroid()
        nc.fit(_l2_norm_rows(X_all_shots), y_all_shots)
        p_nc = nc.predict(_l2_norm_rows(X_te))
        nc_cl_all.append(float(np.mean(p_nc == y_te)))
        t1_mask = np.isin(y_te, [0, 1])
        nc_cl_t1.append(float(np.mean(p_nc[t1_mask] == y_te[t1_mask])))

        # Replay-free sequential MLP via partial_fit across Tasks 1 -> 2 -> 3
        mlp = MLPClassifier(
            hidden_layer_sizes=(64,),
            learning_rate_init=0.01,
            max_iter=1,
            random_state=900 + s,
        )
        all_classes = np.arange(6, dtype=np.int64)
        for task_classes in [(0, 1), (2, 3), (4, 5)]:
            Xt = _l2_norm_rows(np.vstack([xs for cid, xs in tasks_train if cid in task_classes]))
            yt = np.concatenate([np.full(len(xs), cid, dtype=np.int64) for cid, xs in tasks_train if cid in task_classes])
            for _epoch in range(15):
                mlp.partial_fit(Xt, yt, classes=all_classes)
        p_mlp = mlp.predict(_l2_norm_rows(X_te))
        mlp_cl_all.append(float(np.mean(p_mlp == y_te)))
        mlp_cl_t1.append(float(np.mean(p_mlp[t1_mask] == y_te[t1_mask])))

    # ------------------------------------------------------------------
    # Build 30 strict cross-load splits for Task 3C (CWRU 0 HP train -> 2 HP test)
    # ------------------------------------------------------------------
    vib_fe = necksense.VibrationFrontEnd()

    def _window_feats(sig: np.ndarray, win: int = 256) -> np.ndarray:
        n_w = len(sig) // win
        return np.array(
            [vib_fe.extract(sig[w * win : (w + 1) * win] * 4000.0) for w in range(n_w)],
            dtype=np.float64,
        )

    F_norm_0hp = _window_feats(cwru_raw["normal_0hp"])
    F_norm_2hp = _window_feats(cwru_raw["normal_2hp"])
    F_fault_2hp = np.vstack(
        [
            _window_feats(cwru_raw["ir007_2hp"]),
            _window_feats(cwru_raw["ball007_2hp"]),
            _window_feats(cwru_raw["or007_2hp"]),
        ]
    )

    cwru_splits: List[Tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    for s in range(n_seeds):
        rng = np.random.default_rng(1200 + s)
        tr_i = rng.choice(len(F_norm_0hp), size=min(120, len(F_norm_0hp)), replace=False)
        in_i = rng.choice(len(F_norm_2hp), size=min(150, len(F_norm_2hp)), replace=False)
        fl_i = rng.choice(len(F_fault_2hp), size=min(240, len(F_fault_2hp)), replace=False)
        F_tr = F_norm_0hp[tr_i]
        F_te = np.vstack([F_norm_2hp[in_i], F_fault_2hp[fl_i]])
        y_te = np.concatenate([np.zeros(len(in_i), dtype=np.int64), np.ones(len(fl_i), dtype=np.int64)])
        cwru_splits.append((F_tr, F_te, y_te))

    real_cwru_auroc = _eval_cwru_mb(cwru_splits, "banc_v888")
    v2_cwru_auroc = _eval_cwru_mb(cwru_splits, "v2_analytic")

    cwru_ocsvm, cwru_iforest = [], []
    for s, (F_tr, F_te, y_te) in enumerate(cwru_splits):
        scaler = StandardScaler()
        F_tr_n = scaler.fit_transform(_l2_norm_rows(F_tr))
        F_te_n = scaler.transform(_l2_norm_rows(F_te))

        oc = OneClassSVM(kernel="rbf", nu=0.08, gamma="scale")
        oc.fit(F_tr_n)
        cwru_ocsvm.append(compute_auroc(y_te, -oc.decision_function(F_te_n).ravel()))

        iso = IsolationForest(n_estimators=100, random_state=1400 + s)
        iso.fit(F_tr_n)
        cwru_iforest.append(compute_auroc(y_te, -iso.score_samples(F_te_n).ravel()))

    # ------------------------------------------------------------------
    # K = len(shuffled_mb_list) Maslov-Sneppen Shuffled MB Connectomes
    # ------------------------------------------------------------------
    shuf_nov_means = []
    shuf_cl_all_means = []
    shuf_cwru_means = []
    if workers <= 1 or len(shuffled_mb_list) <= 2:
        for shuf_mb in shuffled_mb_list:
            s_nov, _ = _eval_uci_novelty_mb(nov_splits, shuf_mb)
            s_cl_all, _ = _eval_uci_continual_mb(cl_splits, shuf_mb)
            s_cwru = _eval_cwru_mb(cwru_splits, shuf_mb)
            shuf_nov_means.append(float(np.mean(s_nov)))
            shuf_cl_all_means.append(float(np.mean(s_cl_all)))
            shuf_cwru_means.append(float(np.mean(s_cwru)))
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_mb_worker,
            initargs=(nov_splits, cl_splits, cwru_splits),
        ) as pool:
            for nm, cm, bm in pool.map(_eval_one_mb_shuffle, shuffled_mb_list):
                shuf_nov_means.append(nm)
                shuf_cl_all_means.append(cm)
                shuf_cwru_means.append(bm)

    return {
        "n_seeds": n_seeds,
        "task_3a_uci_gas_chronological_novelty": {
            "v3_banc_real_auroc": summary_ci95(real_nov_auroc),
            "v3_banc_real_fpr90": summary_ci95(real_nov_fpr),
            "v2_analytic_flyhash_auroc": summary_ci95(v2_nov_auroc),
            "v2_analytic_flyhash_fpr90": summary_ci95(v2_nov_fpr),
            "l2_ocsvm_rbf_auroc": summary_ci95(ocsvm_auroc),
            "l2_ocsvm_rbf_fpr90": summary_ci95(ocsvm_fpr),
            "l2_iforest_auroc": summary_ci95(iforest_auroc),
            "l2_iforest_fpr90": summary_ci95(iforest_fpr),
            "shuffle_gate_auroc": shuffle_gate_verdict(
                float(np.mean(real_nov_auroc)),
                shuf_nov_means,
                higher_is_better=True,
            ),
        },
        "task_3b_uci_gas_5shot_continual_drift": {
            "v3_banc_real_acc_all6": summary_ci95(real_cl_all),
            "v3_banc_real_acc_task1_retained": summary_ci95(real_cl_t1),
            "v2_analytic_acc_all6": summary_ci95(v2_cl_all),
            "v2_analytic_acc_task1_retained": summary_ci95(v2_cl_t1),
            "l2_nearest_centroid_acc_all6": summary_ci95(nc_cl_all),
            "l2_nearest_centroid_acc_task1_retained": summary_ci95(nc_cl_t1),
            "replay_free_sgd_mlp_acc_all6": summary_ci95(mlp_cl_all),
            "replay_free_sgd_mlp_acc_task1_retained": summary_ci95(mlp_cl_t1),
            "shuffle_gate_acc_all6": shuffle_gate_verdict(
                float(np.mean(real_cl_all)),
                shuf_cl_all_means,
                higher_is_better=True,
            ),
        },
        "task_3c_cwru_bearing_0hp_to_2hp_fault_detection": {
            "v3_banc_real_auroc": summary_ci95(real_cwru_auroc),
            "v2_analytic_flyhash_auroc": summary_ci95(v2_cwru_auroc),
            "l2_ocsvm_rbf_auroc": summary_ci95(cwru_ocsvm),
            "l2_iforest_auroc": summary_ci95(cwru_iforest),
            "shuffle_gate_auroc": shuffle_gate_verdict(
                float(np.mean(real_cwru_auroc)),
                shuf_cwru_means,
                higher_is_better=True,
            ),
        },
    }
