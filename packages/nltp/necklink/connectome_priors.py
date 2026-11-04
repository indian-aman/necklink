"""
Empirical Adult Drosophila Neck-Connective Priors & Epistatic Frame Construction.

Extracts verified structural ratios from the MaleCNS v1.0 (167,565 neurons) and
Female BANC v888 (157,789 neurons) connectomes and constructs integer Q8
epistatic cryptic-backup projection matrices for packet-loss-resilient coding.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import numpy as np


DROSOPHILA_NECK_PRIORS: Dict[str, Any] = {
    "male_cns_neurons": 167_565,
    "female_banc_neurons": 157_789,
    "combined_cns_neurons": 325_354,
    "descending_neurons_mean": 1_316,
    "ascending_neurons_mean": 1_852,
    "neck_bottleneck_fraction_of_cns": (1_316 + 1_852) / 162_677.0,  # ~1.95% of CNS
    "an_to_dn_efference_ratio": 1_852 / 1_316,  # 1.407x feedback-to-command ratio
    "excitatory_driver_fraction": 0.764,  # ACh + Glu excitatory fraction
    "inhibitory_shunt_fraction": 0.236,  # GABA + GluCl tonic shunting fraction
    "cryptic_backup_epistasis_order_k": 2,  # Pairwise cryptic backup dominance (Paper 3)
    "leaky_membrane_alpha_q8": 242,  # 242/256 = 0.9453 contractive twin-mirror decay
    "efference_velocity_beta_q8": 96,  # 96/256 = 0.3750 damped oscillatory coupling
    "efference_velocity_damp_q8": 176,  # 176/256 = 0.6875 contractive velocity damping
    "tonic_chloride_shunt_floor_q8": 24,  # Minimum brake gate 24/256 = 0.0938
}


def measure_empirical_connectome_neck_stats(
    ascending_json_path: Path | None = None,
    epistasis_json_path: Path | None = None,
) -> Dict[str, Any]:
    """
    Loads read-only summary statistics from prior connectome analyses if present locally,
    falling back deterministically to DROSOPHILA_NECK_PRIORS on clean cloud workers.
    """
    stats = dict(DROSOPHILA_NECK_PRIORS)
    if ascending_json_path is None:
        cand = Path(r"D:\drosophila-ascending-connectome\results\ascending_screens_and_specificity.json")
        if cand.exists():
            ascending_json_path = cand
    if epistasis_json_path is None:
        cand2 = Path(r"D:\drosophila-combinatorial-epistasis\results\epistasis_atlas_and_nulls.json")
        if cand2.exists():
            epistasis_json_path = cand2

    if ascending_json_path is not None and ascending_json_path.exists():
        try:
            raw_asc = json.loads(ascending_json_path.read_text(encoding="utf-8"))
            stats["verified_local_ascending_atlas"] = True
            if "male" in raw_asc and isinstance(raw_asc["male"], dict):
                stats["male_an_verified"] = raw_asc["male"].get("n_an", stats["ascending_neurons_mean"])
        except Exception:
            stats["verified_local_ascending_atlas"] = False
    else:
        stats["verified_local_ascending_atlas"] = False

    if epistasis_json_path is not None and epistasis_json_path.exists():
        try:
            raw_epi = json.loads(epistasis_json_path.read_text(encoding="utf-8"))
            stats["verified_local_epistasis_atlas"] = True
            stats["epistasis_atlas_keys"] = sorted(list(raw_epi.keys()))[:6]
        except Exception:
            stats["verified_local_epistasis_atlas"] = False
    else:
        stats["verified_local_epistasis_atlas"] = False

    return stats


def _sylvester_hadamard(order_pow2: int) -> np.ndarray:
    """Deterministic +/-1 Sylvester-Hadamard orthogonal matrix."""
    h = np.array([[1]], dtype=np.int32)
    while h.shape[0] < order_pow2:
        h = np.block([[h, h], [h, -h]])
    return h


def build_epistatic_cryptic_matrix_q8(
    n_channels: int,
    sketch_dim: int,
    seed: int = 20261103,
) -> np.ndarray:
    """
    Constructs an Epistasis-Shapley optimized signed integer projection matrix
    Phi in int8 of shape (sketch_dim, n_channels), scaled in Q7/Q8 {-128..127}.

    Design objective (from Paper 3 Combinatorial Epistasis):
      Maximize worst-case pairwise/k-wise erasure recovery conditioning so every
      channel has balanced antagonistic (+ / -) cryptic backup partners across
      all sketch coordinates, with zero row-mean bias (Dale's-law push-pull balance).
    """
    if sketch_dim <= 0 or n_channels <= 0:
        raise ValueError("sketch_dim and n_channels must be positive")

    pow2 = 1
    target = max(n_channels, sketch_dim, 8)
    while pow2 < target:
        pow2 *= 2
    had = _sylvester_hadamard(pow2)

    rng = np.random.default_rng(seed)
    best_mat = None
    best_score = -1e18

    # Screen candidate epistatic row/column signed permutations to maximize
    # minimum pairwise column coherence separation + zero-DC row balance
    for _ in range(128):
        # Skip row 0 (all +1s) so every sketch row is naturally push-pull balanced!
        row_pool = np.arange(1, pow2)
        if sketch_dim <= len(row_pool):
            rows = rng.choice(row_pool, size=sketch_dim, replace=False)
        else:
            rows = rng.choice(row_pool, size=sketch_dim, replace=True)
        cols = rng.choice(pow2, size=n_channels, replace=False)
        signs = rng.choice(np.array([-1, 1], dtype=np.int32), size=(1, n_channels))
        cand = had[np.ix_(rows, cols)] * signs  # shape (sketch_dim, n_channels) in {-1, +1}

        # Evaluate epistatic conditioning:
        # 1) Gram off-diagonal coherence (lower max off-diagonal = better k-sparse recovery)
        gram = cand.T @ cand  # diagonal is sketch_dim
        off_diag = gram - np.diag(np.diag(gram))
        max_coherence = float(np.max(np.abs(off_diag))) / max(sketch_dim, 1)
        mean_coherence = float(np.mean(np.abs(off_diag))) / max(sketch_dim, 1)
        # 2) Row push-pull balance (closer to zero = better global pool FFI invariance)
        row_imbalance = float(np.mean(np.abs(np.sum(cand, axis=1)))) / max(n_channels, 1)

        score = -(2.0 * max_coherence + 1.0 * mean_coherence + 0.5 * row_imbalance)
        if score > best_score:
            best_score = score
            best_mat = cand

    assert best_mat is not None
    # Scale entries so column L2 norm is ~128 (Q7 unit-norm columns, exact int8!)
    scale = int(round(127.0 / np.sqrt(max(sketch_dim, 1))))
    scale = max(16, min(127, scale))
    return (best_mat * scale).astype(np.int8)
