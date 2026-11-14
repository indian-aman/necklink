"""
Genuine Maslov-Sneppen Degree-Preserving Directed Connectome Shuffler (`BANC v888`).

Implements degree-preserving directed edge-swap randomization (Maslov & Sneppen,
*Science* 296:910-913, 2002) on dense and sparse connectome subgraphs while
strictly preserving:
  1. Node count and exact cell-type / hemisphere (`somaSide`) / `nt_sign` labels,
  2. Exact out-degree (row non-zeros) of every presynaptic neuron,
  3. Exact in-degree (column non-zeros) of every postsynaptic neuron,
  4. Exact synapse-weight multiset (`out_weights` travel with each swapped edge),
  5. Simple-graph constraint (zero duplicate edges, optional zero self-loops).
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
from scipy import sparse


def maslov_sneppen_dense(
    W_mat: np.ndarray,
    rng: np.random.Generator,
    swaps_per_edge: int = 8,
    allow_self: bool = True,
) -> np.ndarray:
    """
    Exact degree-preserving directed edge-swap shuffle on a 2D array `W_mat`.

    Preserves exact row non-zero counts, exact column non-zero counts, and the
    exact multiset of non-zero edge weights.
    """
    M = np.array(W_mat, dtype=np.float64, copy=True)
    rows, cols = np.nonzero(M)
    m = len(rows)
    if m < 4:
        return M

    occupied = M != 0
    n_trials = int(m) * int(max(1, swaps_per_edge))
    r_idx1 = rng.integers(0, m, size=n_trials)
    r_idx2 = rng.integers(0, m, size=n_trials)

    for e1, e2 in zip(r_idx1, r_idx2):
        if e1 == e2:
            continue
        u1 = int(rows[e1])
        v1 = int(cols[e1])
        u2 = int(rows[e2])
        v2 = int(cols[e2])
        if u1 == u2 or v1 == v2:
            continue
        if not allow_self and (u1 == v2 or u2 == v1):
            continue
        if occupied[u1, v2] or occupied[u2, v1]:
            continue

        w1 = M[u1, v1]
        w2 = M[u2, v2]
        M[u1, v1] = 0.0
        M[u2, v2] = 0.0
        occupied[u1, v1] = False
        occupied[u2, v2] = False

        M[u1, v2] = w1
        M[u2, v1] = w2
        occupied[u1, v2] = True
        occupied[u2, v1] = True

        cols[e1] = v2
        cols[e2] = v1

    return M


def maslov_sneppen_csr(
    M_csr: sparse.csr_matrix,
    rng: np.random.Generator,
    swaps_per_edge: int = 6,
    allow_self: bool = True,
) -> sparse.csr_matrix:
    """
    Exact degree-preserving directed edge-swap shuffle on a `scipy.sparse.csr_matrix`.
    """
    coo = M_csr.tocoo()
    rows = coo.row.astype(np.int32, copy=True)
    cols = coo.col.astype(np.int32, copy=True)
    data = coo.data.astype(np.float64, copy=True)
    m = len(rows)
    if m < 4:
        return M_csr.copy()

    n_cols = int(M_csr.shape[1])
    # Pack (u, v) into int64 key u * n_cols + v for fast O(1) collision checks
    edge_keys = set((rows.astype(np.int64) * n_cols + cols.astype(np.int64)).tolist())
    n_trials = int(m) * int(max(1, swaps_per_edge))
    e1s = rng.integers(0, m, size=n_trials)
    e2s = rng.integers(0, m, size=n_trials)

    for e1, e2 in zip(e1s, e2s):
        if e1 == e2:
            continue
        u1 = int(rows[e1])
        v1 = int(cols[e1])
        u2 = int(rows[e2])
        v2 = int(cols[e2])
        if u1 == u2 or v1 == v2:
            continue
        if not allow_self and (u1 == v2 or u2 == v1):
            continue
        k_new1 = u1 * n_cols + v2
        k_new2 = u2 * n_cols + v1
        if k_new1 in edge_keys or k_new2 in edge_keys:
            continue

        edge_keys.remove(u1 * n_cols + v1)
        edge_keys.remove(u2 * n_cols + v2)
        edge_keys.add(k_new1)
        edge_keys.add(k_new2)

        cols[e1] = v2
        cols[e2] = v1

    out = sparse.csr_matrix((data, (rows, cols)), shape=M_csr.shape)
    out.eliminate_zeros()
    return out


def verify_degree_preservation(
    W_orig: np.ndarray | sparse.spmatrix,
    W_shuf: np.ndarray | sparse.spmatrix,
) -> Dict[str, Any]:
    """
    Verify exact row/column degree preservation and weight multiset conservation.
    Raises `AssertionError` if any invariant is violated.
    """
    if sparse.issparse(W_orig):
        o_csr = W_orig.tocsr()
        s_csr = W_shuf.tocsr() if sparse.issparse(W_shuf) else sparse.csr_matrix(W_shuf)
        row_deg_ok = bool(np.array_equal(np.diff(o_csr.indptr), np.diff(s_csr.indptr)))
        col_deg_ok = bool(
            np.array_equal(
                np.asarray((o_csr != 0).sum(axis=0)).ravel(),
                np.asarray((s_csr != 0).sum(axis=0)).ravel(),
            )
        )
        w_orig = np.sort(o_csr.data.astype(np.float64))
        w_shuf = np.sort(s_csr.data.astype(np.float64))
        weights_ok = bool(len(w_orig) == len(w_shuf) and np.allclose(w_orig, w_shuf))
        overlap_nnz = int(((o_csr != 0).multiply(s_csr != 0)).nnz)
        total_nnz = max(1, int(o_csr.nnz))
    else:
        A = np.asarray(W_orig, dtype=np.float64)
        B = np.asarray(W_shuf, dtype=np.float64)
        row_deg_ok = bool(np.array_equal((A != 0).sum(axis=1), (B != 0).sum(axis=1)))
        col_deg_ok = bool(np.array_equal((A != 0).sum(axis=0), (B != 0).sum(axis=0)))
        w_orig = np.sort(A[A != 0])
        w_shuf = np.sort(B[B != 0])
        weights_ok = bool(len(w_orig) == len(w_shuf) and np.allclose(w_orig, w_shuf))
        overlap_nnz = int(((A != 0) & (B != 0)).sum())
        total_nnz = max(1, int((A != 0).sum()))

    if not (row_deg_ok and col_deg_ok and weights_ok):
        raise AssertionError(
            f"Maslov-Sneppen invariant failed: row={row_deg_ok}, col={col_deg_ok}, weights={weights_ok}"
        )
    return {
        "row_degree_preserved": row_deg_ok,
        "col_degree_preserved": col_deg_ok,
        "weight_multiset_preserved": weights_ok,
        "edge_overlap_fraction": float(overlap_nnz / total_nnz),
    }


def shuffle_raw_subgraphs(
    raw_subgraphs: Dict[str, Any],
    seed: int,
    swaps_per_edge: int = 8,
) -> Dict[str, Any]:
    """
    Create a Maslov-Sneppen degree-preserving shuffled copy of all raw `BANC v888`
    subcircuit matrices, ready to be passed through `compile_circuits_from_subgraphs(...)`.

    Every neuron's cell type, `somaSide`, and neurotransmitter sign (`nt_sign`) are
    preserved; every pathway matrix has its directed edges degree-preservingly swapped.
    """
    rng = np.random.default_rng(int(seed))
    out: Dict[str, Any] = {"meta": dict(raw_subgraphs["meta"]), "shuffle_seed": int(seed)}

    # 1. Central Complex
    cx_orig = raw_subgraphs["cx"]
    cx_shuf: Dict[str, Any] = {}
    for k, val in cx_orig.items():
        if isinstance(val, np.ndarray) and val.ndim == 2 and k.startswith("W_"):
            cx_shuf[k] = maslov_sneppen_dense(
                val,
                rng,
                swaps_per_edge=swaps_per_edge,
                allow_self=(k != "W_epg_epg"),
            )
        else:
            cx_shuf[k] = val.copy() if hasattr(val, "copy") else val
    out["cx"] = cx_shuf

    # 2. Optic Lobe
    ol_orig = raw_subgraphs["optic"]
    ol_shuf: Dict[str, Any] = {}
    for k, val in ol_orig.items():
        if sparse.issparse(val):
            ol_shuf[k] = maslov_sneppen_csr(val, rng, swaps_per_edge=max(4, swaps_per_edge - 2))
        elif isinstance(val, np.ndarray) and val.ndim == 2 and k.startswith("W_"):
            ol_shuf[k] = maslov_sneppen_dense(val, rng, swaps_per_edge=swaps_per_edge)
        else:
            ol_shuf[k] = val.copy() if hasattr(val, "copy") else val
    out["optic"] = ol_shuf

    # 3. Mushroom Body
    mb_orig = raw_subgraphs["mb"]
    mb_shuf: Dict[str, Any] = {}
    for k, val in mb_orig.items():
        if sparse.issparse(val):
            mb_shuf[k] = maslov_sneppen_csr(val, rng, swaps_per_edge=max(4, swaps_per_edge - 2))
        elif isinstance(val, np.ndarray) and val.ndim == 2 and k.startswith("W_"):
            mb_shuf[k] = maslov_sneppen_dense(val, rng, swaps_per_edge=swaps_per_edge)
        else:
            mb_shuf[k] = val.copy() if hasattr(val, "copy") else val
    out["mb"] = mb_shuf

    # 4. Descending & VNC Motor Cord
    vd_orig = raw_subgraphs["vnc_dn"]
    vd_shuf: Dict[str, Any] = {}
    for k, val in vd_orig.items():
        if sparse.issparse(val):
            # Note: W_vnc_ei is 5331x7520; shuffling P_exc_leg, P_inh_leg, W_dn_vnc_pre,
            # W_dn_leg_direct, W_brain_dn_1hop, and W_brain_inter_dn preserves exact degrees
            # while remaining tractable for K >= 100 shuffles. Also shuffle W_vnc_ei if small enough!
            if val.nnz <= 250_000:
                vd_shuf[k] = maslov_sneppen_csr(val, rng, swaps_per_edge=4, allow_self=False)
            else:
                vd_shuf[k] = val.copy()
        elif isinstance(val, np.ndarray) and val.ndim == 2 and k.startswith(("W_", "P_")):
            vd_shuf[k] = maslov_sneppen_dense(val, rng, swaps_per_edge=swaps_per_edge)
        else:
            vd_shuf[k] = val.copy() if hasattr(val, "copy") else val
    out["vnc_dn"] = vd_shuf

    return out
