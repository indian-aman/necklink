"""
Clean, Unrigged Public Dataset Loaders & Statistical Helpers for NeckLink v3.0.0 Benchmarks.

Datasets:
  1. UCI Gas Sensor Array Drift Dataset (`13,910` 16-sensor MOX plume recordings across
     6 gases over 10 chronological batches / 36 months; Vergara et al., Sens. Actuators B 2012).
  2. Case Western Reserve University (CWRU) 12 kHz Drive-End Bearing Accelerometer Dataset
     (Normal, Inner Race 0.007", Ball 0.007", Outer Race 0.007" across 0 HP = 1797 RPM
     and 2 HP = 1750 RPM motor loads).
"""

from __future__ import annotations

import hashlib
import io
import os
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import scipy.io

CACHE_DIR = Path(os.environ.get("NECKLINK_BENCH_CACHE", "/tmp/necklink_v3_bench_cache"))


def _fetch_bytes(urls: List[str], timeout: int = 60) -> bytes:
    last_err = None
    headers = {"User-Agent": "Mozilla/5.0 (NeckLink-v3-AuditBench)"}
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"All mirrors failed ({urls[0]}): {last_err}")


def load_uci_gas_drift() -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns `(X16, y_gas, batch_id, conc_ppm)`:
      - `X16`: `(N, 16)` positive steady-state MOX channel responses (`|DeltaR|`)
      - `y_gas`: int class `0..5` (Ethanol, Ethylene, Ammonia, Acetaldehyde, Acetone, Toluene)
      - `batch_id`: chronological batch `1..10` spanning 36 months of sensor drift
      - `conc_ppm`: gas concentration in ppm
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    npz_cache = CACHE_DIR / "uci_gas_drift_13910.npz"
    if npz_cache.exists():
        z = np.load(npz_cache)
        return z["X16"], z["y"], z["batch"], z["conc"]

    raw_zip = _fetch_bytes(
        [
            "https://archive.ics.uci.edu/static/public/224/gas+sensor+array+drift+dataset.zip",
            "https://archive.ics.uci.edu/ml/machine-learning-databases/00224/Dataset.zip",
        ],
        timeout=90,
    )

    X_rows: List[np.ndarray] = []
    y_rows: List[int] = []
    b_rows: List[int] = []
    c_rows: List[float] = []

    def _parse_dat_text(text: str, batch_num: int) -> None:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            lbl_part = parts[0]
            if ";" in lbl_part:
                gas_str, conc_str = lbl_part.split(";", 1)
                gas_id = int(gas_str) - 1
                conc_val = float(conc_str)
            else:
                gas_id = int(float(lbl_part)) - 1
                conc_val = 100.0
            feats = np.zeros(128, dtype=np.float64)
            for tok in parts[1:]:
                if ":" in tok:
                    k_s, v_s = tok.split(":", 1)
                    idx = int(k_s) - 1
                    if 0 <= idx < 128:
                        feats[idx] = float(v_s)
            x16 = np.abs(feats[0::8])
            if np.sum(x16) > 1e-9 and 0 <= gas_id < 6:
                X_rows.append(x16)
                y_rows.append(gas_id)
                b_rows.append(batch_num)
                c_rows.append(conc_val)

    zf = zipfile.ZipFile(io.BytesIO(raw_zip))
    for name in sorted(zf.namelist()):
        low = name.lower()
        if low.endswith(".zip"):
            inner_zf = zipfile.ZipFile(io.BytesIO(zf.read(name)))
            for iname in sorted(inner_zf.namelist()):
                if iname.lower().endswith(".dat") and "batch" in iname.lower():
                    digits = "".join(ch for ch in Path(iname).stem if ch.isdigit())
                    b_id = int(digits) if digits else 1
                    _parse_dat_text(inner_zf.read(iname).decode("utf-8", errors="ignore"), b_id)
        elif low.endswith(".dat") and "batch" in low:
            digits = "".join(ch for ch in Path(name).stem if ch.isdigit())
            b_id = int(digits) if digits else 1
            _parse_dat_text(zf.read(name).decode("utf-8", errors="ignore"), b_id)

    X16 = np.asarray(X_rows, dtype=np.float64)
    y = np.asarray(y_rows, dtype=np.int64)
    batch = np.asarray(b_rows, dtype=np.int64)
    conc = np.asarray(c_rows, dtype=np.float64)
    np.savez_compressed(npz_cache, X16=X16, y=y, batch=batch, conc=conc)
    return X16, y, batch, conc


def load_cwru_bearing() -> Dict[str, np.ndarray]:
    """
    Download real CWRU 12 kHz Drive-End Bearing Accelerometer waveforms across
    `0 HP` (`1797 RPM`) and `2 HP` (`1750 RPM`) motor loads.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    npz_cache = CACHE_DIR / "cwru_bearing_real.npz"
    if npz_cache.exists():
        z = np.load(npz_cache)
        return {k: z[k] for k in z.files}

    files_spec = {
        "normal_0hp": "97",
        "normal_2hp": "99",
        "ir007_0hp": "105",
        "ir007_2hp": "107",
        "ball007_0hp": "118",
        "ball007_2hp": "120",
        "or007_0hp": "130",
        "or007_2hp": "132",
    }
    r1 = "https://raw.githubusercontent.com/cathysiyu/Mechanical-datasets/master/dataset"
    r1_map = {
        "97": f"{r1}/Normal_0.mat",
        "99": f"{r1}/Normal_2.mat",
        "105": f"{r1}/IR007_0.mat",
        "107": f"{r1}/IR007_2.mat",
        "118": f"{r1}/B007_0.mat",
        "120": f"{r1}/B007_2.mat",
        "130": f"{r1}/OR007@6_0.mat",
        "132": f"{r1}/OR007@6_2.mat",
    }
    r2 = "https://raw.githubusercontent.com/Litchiware/CWRU-dataset/main"
    r2_map = {
        "97": f"{r2}/Normal/97.mat",
        "99": f"{r2}/Normal/99.mat",
        "105": f"{r2}/12k_Drive_End_Bearing_Fault_Data/IR/007/105.mat",
        "107": f"{r2}/12k_Drive_End_Bearing_Fault_Data/IR/007/107.mat",
        "118": f"{r2}/12k_Drive_End_Bearing_Fault_Data/B/007/118.mat",
        "120": f"{r2}/12k_Drive_End_Bearing_Fault_Data/B/007/120.mat",
        "130": f"{r2}/12k_Drive_End_Bearing_Fault_Data/OR/007/@6/130.mat",
        "132": f"{r2}/12k_Drive_End_Bearing_Fault_Data/OR/007/@6/132.mat",
    }
    r3 = "https://github.com/srigas/CWRU_Bearing_NumPy/raw/main/raw"

    official = "https://engineering.case.edu/sites/default/files"
    out: Dict[str, np.ndarray] = {}
    for label, fid in files_spec.items():
        urls = [f"{official}/{fid}.mat", r1_map[fid], r2_map[fid], f"{r3}/{fid}.mat"]
        raw_mat = _fetch_bytes(urls, timeout=60)
        mat = scipy.io.loadmat(io.BytesIO(raw_mat))
        exact_key = f"X{int(fid):03d}_DE_time"
        de_key = exact_key if exact_key in mat else next(k for k in sorted(mat.keys()) if "DE_time" in k)
        sig = np.asarray(mat[de_key], dtype=np.float64).ravel()
        out[label] = sig[:120000]

    np.savez_compressed(npz_cache, **out)
    return out


def compute_auroc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """Exact rank-based Mann-Whitney U / trapezoidal AUROC (`y_true` in {0, 1})."""
    y = np.asarray(y_true, dtype=np.int64).ravel()
    s = np.asarray(y_score, dtype=np.float64).ravel()
    pos = s[y == 1]
    neg = s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    # Exact pairwise comparison with 0.5 tie-breaking
    diff = pos[:, None] - neg[None, :]
    wins = float(np.sum(diff > 0.0) + 0.5 * np.sum(diff == 0.0))
    return wins / float(len(pos) * len(neg))


def compute_fpr_at_tpr(y_true: np.ndarray, y_score: np.ndarray, target_tpr: float = 0.90) -> float:
    """False positive rate at `TPR >= target_tpr`."""
    y = np.asarray(y_true, dtype=np.int64).ravel()
    s = np.asarray(y_score, dtype=np.float64).ravel()
    pos = np.sort(s[y == 1])
    neg = s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return 1.0
    idx = int(np.floor((1.0 - target_tpr) * len(pos)))
    idx = max(0, min(len(pos) - 1, idx))
    thresh = pos[idx]
    return float(np.mean(neg >= thresh))


def summary_ci95(values: np.ndarray | List[float], seed: int = 20261114) -> Dict[str, Any]:
    """Compute sample mean, SD, median, and non-parametric 10,000-resample bootstrap 95% CI."""
    arr = np.asarray(values, dtype=np.float64).ravel()
    n = len(arr)
    if n == 0:
        return {"n": 0, "mean": 0.0, "std": 0.0, "median": 0.0, "ci95_low": 0.0, "ci95_high": 0.0}
    if n == 1:
        v = float(arr[0])
        return {"n": 1, "mean": v, "std": 0.0, "median": v, "ci95_low": v, "ci95_high": v}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(10000, n))
    boot_means = arr[idx].mean(axis=1)
    lo, hi = np.percentile(boot_means, [2.5, 97.5])
    return {
        "n": int(n),
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr, ddof=1)),
        "median": float(np.median(arr)),
        "ci95_low": float(lo),
        "ci95_high": float(hi),
    }


def shuffle_gate_verdict(
    real_metric: float,
    shuf_metrics: np.ndarray | List[float],
    higher_is_better: bool = True,
    alpha: float = 0.01,
) -> Dict[str, Any]:
    """
    Exact finite-sample Monte Carlo permutation p-value (Phipson & Smyth 2010):
      `p = (1 + #{shuffled >= real}) / (K + 1)` (or `<=` if lower is better).
    """
    shuf = np.asarray(shuf_metrics, dtype=np.float64).ravel()
    K = len(shuf)
    if higher_is_better:
        n_beat_or_tie = int(np.sum(shuf >= real_metric - 1e-12))
        effect_z = float((real_metric - np.mean(shuf)) / max(1e-12, np.std(shuf, ddof=1)))
    else:
        n_beat_or_tie = int(np.sum(shuf <= real_metric + 1e-12))
        effect_z = float((np.mean(shuf) - real_metric) / max(1e-12, np.std(shuf, ddof=1)))
    p_val = float((1 + n_beat_or_tie) / (K + 1))
    return {
        "K_shuffles": int(K),
        "real_value": float(real_metric),
        "shuffled_summary": summary_ci95(shuf),
        "shuffled_p05": float(np.percentile(shuf, 5.0)),
        "shuffled_p95": float(np.percentile(shuf, 95.0)),
        "shuffled_best": float(np.max(shuf) if higher_is_better else np.min(shuf)),
        "n_shuffles_beating_or_tying_real": n_beat_or_tie,
        "p_value_one_sided": p_val,
        "z_score_vs_shuffled": effect_z,
        "higher_is_better": bool(higher_is_better),
        "passed_gate": bool(p_val <= alpha and effect_z >= 2.0),
    }


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
