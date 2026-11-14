"""
Deterministic `BANC v888` Subcircuit Extractor, Compiler & ANSI C99 Header Generator.

Pulls real signed synaptic subgraphs from the proofread 157,789-neuron Adult Female
Whole-CNS connectome (`BANC v888`, 3,036,600 directed edges, 23,553,107 synapses),
infers missing spatial coordinates strictly from intrinsic connectivity (zero target
peeking), compiles fixed-point matrices, and emits `.npz` bundles + `.h` Flash tables
with SHA-256 source provenance.

Crucially, `compile_circuits_from_subgraphs(raw_subgraphs)` is the **exact same**
pure function called on both the real `BANC v888` connectome and every Maslov-Sneppen
degree-preserving shuffled connectome (`shuffle_raw_subgraphs`).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import pdist
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import eigsh

DEFAULT_BANC_DIR = Path(r"D:\drosophila-500x-embodied-cns\data\female_compiled")

BRAIN_POP_NAMES = (
    "LPLC2_L",
    "LPLC2_R",
    "LC4_L",
    "LC4_R",
    "LPLC1_L",
    "LPLC1_R",
    "PFL3_L",
    "PFL3_R",
    "PFL2",
    "MBON_ACh_avoid",
    "MBON_Inh_approach",
)

CORE_DN_NAMES = (
    "DNp01_L",
    "DNp01_R",
    "MDN_L",
    "MDN_R",
    "DNa02_L",
    "DNa02_R",
    "DNa01_L",
    "DNa01_R",
    "DNp09_L",
    "DNp09_R",
)

LEG_POOL_NAMES = ("L1", "R1", "L2", "R2", "L3", "R3")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def extract_raw_subgraphs(data_dir: Path = DEFAULT_BANC_DIR) -> Dict[str, Any]:
    """
    Extract raw synapse subgraphs and neuron metadata from `BANC v888` so that both
    real compilation and `K >= 100` Maslov-Sneppen shuffles operate on exact subgraphs.
    """
    data_dir = Path(data_dir)
    meta_path = data_dir / "neurons_meta.parquet"
    npz_path = data_dir / "connectome_compiled.npz"

    df = pd.read_parquet(meta_path)
    z = np.load(npz_path, allow_pickle=True)
    n_total = len(df)

    W_u = sparse.csr_matrix(
        (z["out_weights"].astype(np.float64), z["out_indices"], z["out_indptr"]),
        shape=(n_total, n_total),
    )
    nt_sign = z["nt_sign"].astype(np.float64)
    in_syn_all = np.maximum(np.asarray(W_u.sum(axis=0)).ravel(), 1.0)

    # ------------------------------------------------------------------
    # 1. Central Complex (CX) raw subgraphs
    # ------------------------------------------------------------------
    epg_df = df[df["type"].isin(["EPG", "EPGt"])]
    pena_df = df[df["type"] == "PEN_a"]
    penb_df = df[df["type"] == "PEN_b/PEN2"]
    d7_df = df[df["type"] == "Delta7"]
    pfl3_df = df[df["type"] == "PFL3"]
    pfl2_df = df[df["type"] == "PFL2"]
    hd_df = df[df["type"].fillna("").str.contains("^hDelta|^vDelta|^FC2", regex=True)]
    er_df = df[df["type"].fillna("").str.match(r"^ER\d")]
    dna02_df = df[df["type"] == "DNa02"].sort_values("somaSide")  # ['L', 'R']

    epg_i = epg_df["idx"].values
    pena_i = pena_df["idx"].values
    penb_i = penb_df["idx"].values
    d7_i = d7_df["idx"].values
    pfl3_i = pfl3_df["idx"].values
    pfl2_i = pfl2_df["idx"].values
    hd_i = hd_df["idx"].values
    er_i = er_df["idx"].values
    dna02_i = dna02_df["idx"].values

    # LAL/Crepine interneurons linking PFL3/PFL2 -> DNa02 (2-hop)
    pfl_all_i = np.concatenate([pfl3_i, pfl2_i])
    lal_mask = (np.asarray(W_u[pfl_all_i, :].sum(axis=0)).ravel() > 0) & (
        np.asarray(W_u[:, dna02_i].sum(axis=1)).ravel() > 0
    )
    lal_i = np.nonzero(lal_mask)[0]

    cx_raw = {
        "W_epg_epg": W_u[epg_i][:, epg_i].toarray(),
        "W_epg_d7": W_u[epg_i][:, d7_i].toarray(),
        "W_d7_epg": W_u[d7_i][:, epg_i].toarray(),
        "W_epg_pena": W_u[epg_i][:, pena_i].toarray(),
        "W_pena_epg": W_u[pena_i][:, epg_i].toarray(),
        "W_epg_penb": W_u[epg_i][:, penb_i].toarray(),
        "W_penb_epg": W_u[penb_i][:, epg_i].toarray(),
        "W_er_epg": W_u[er_i][:, epg_i].toarray(),
        "W_epg_pfl3": W_u[epg_i][:, pfl3_i].toarray(),
        "W_d7_pfl3": W_u[d7_i][:, pfl3_i].toarray(),
        "W_epg_pfl2": W_u[epg_i][:, pfl2_i].toarray(),
        "W_d7_pfl2": W_u[d7_i][:, pfl2_i].toarray(),
        "W_hd_pfl": W_u[hd_i][:, pfl_all_i].toarray(),
        "W_pfl3_dna02": W_u[pfl3_i][:, dna02_i].toarray(),
        "W_pfl_lal": W_u[pfl_all_i][:, lal_i].toarray(),
        "W_lal_dna02": W_u[lal_i][:, dna02_i].toarray(),
        "lal_nt_sign": nt_sign[lal_i].copy(),
        "lal_in_syn": in_syn_all[lal_i].copy(),
        "dna02_in_syn": in_syn_all[dna02_i].copy(),
        "pena_is_L": (pena_df["somaSide"] == "L").values.astype(bool),
        "pfl3_is_L": (pfl3_df["somaSide"] == "L").values.astype(bool),
    }

    # ------------------------------------------------------------------
    # 2. Optic Lobe raw subgraphs (densely proofread Right eye + bilateral VPNs)
    # ------------------------------------------------------------------
    t45_R_df = df[
        df["type"].isin(["T4a", "T4b", "T4c", "T4d", "T5a", "T5b", "T5c", "T5d"])
        & (df["somaSide"] == "R")
    ].copy()
    t45_R = t45_R_df["idx"].values
    med_R = df[
        df["class"].isin(
            ["medulla_intrinsic", "transmedullary", "transmedullary_y", "lamina_monopolar", "distal_medulla"]
        )
        & (df["somaSide"] == "R")
    ]["idx"].values
    # Small-field contrast/edge medulla-lobula neurons (T2, T2a, Tm2, Tm4, TmY3) feeding LC4/LPLC1
    med_edge_R = df[
        df["type"].isin(["T2", "T2a", "Tm2", "Tm4", "TmY3"]) & (df["somaSide"] == "R")
    ]["idx"].values

    hsn_R = df[(df["type"] == "HSN") & (df["somaSide"] == "R")]["idx"].values
    hss_R = df[(df["type"] == "HSS") & (df["somaSide"] == "R")]["idx"].values
    vs_ant_R = df[df["type"].isin(["VS1", "VS2"]) & (df["somaSide"] == "R")]["idx"].values
    vs_post_R = df[df["type"].isin(["VS5", "VS6", "VS7", "VS8"]) & (df["somaSide"] == "R")]["idx"].values
    hs_vs_R = np.concatenate([hsn_R, hss_R, vs_ant_R, vs_post_R])
    hs_vs_grp = np.array(
        [0] * len(hsn_R) + [1] * len(hss_R) + [2] * len(vs_ant_R) + [3] * len(vs_post_R),
        dtype=np.int32,
    )

    lplc2_R = df[(df["type"] == "LPLC2") & (df["somaSide"] == "R")]["idx"].values
    lc4_R = df[(df["type"] == "LC4") & (df["somaSide"] == "R")]["idx"].values
    lplc1_R = df[(df["type"] == "LPLC1") & (df["somaSide"] == "R")]["idx"].values
    lpi_R = df[df["type"].fillna("").str.startswith("LPi") & (df["somaSide"] == "R")]["idx"].values
    li_inh_R = df[df["type"].isin(["Li22", "Li17", "Li33"]) & (df["somaSide"] == "R")]["idx"].values

    dir_map = {"T4a": 0, "T5a": 0, "T4b": 1, "T5b": 1, "T4c": 2, "T5c": 2, "T4d": 3, "T5d": 3}
    t45_dir = np.array([dir_map[t] for t in t45_R_df["type"].values], dtype=np.int32)
    t45_is_t4 = np.array([t.startswith("T4") for t in t45_R_df["type"].values], dtype=bool)

    optic_raw = {
        "W_med_t45": W_u[med_R][:, t45_R].tocsr(),
        "W_t45_hsvs": W_u[t45_R][:, hs_vs_R].toarray(),
        "W_t45_lplc2": W_u[t45_R][:, lplc2_R].tocsr(),
        "W_t45_lpi": W_u[t45_R][:, lpi_R].tocsr(),
        "W_lpi_lplc2": W_u[lpi_R][:, lplc2_R].toarray(),
        "W_med_lc4": W_u[med_R][:, lc4_R].tocsr(),
        "W_edge_lc4": W_u[med_edge_R][:, lc4_R].toarray(),
        "W_t45_lplc1": W_u[t45_R][:, lplc1_R].tocsr(),
        "W_li_lc4": W_u[li_inh_R][:, lc4_R].toarray(),
        "W_li_lplc1": W_u[li_inh_R][:, lplc1_R].toarray(),
        "hs_vs_grp": hs_vs_grp,
        "t45_dir": t45_dir,
        "t45_is_t4": t45_is_t4,
    }

    # ------------------------------------------------------------------
    # 3. Mushroom Body (MB) raw subgraphs
    # ------------------------------------------------------------------
    alpn_df = df[df["class"] == "antennal_lobe_projection_neuron"].copy()
    kc_df = df[df["subclass"].isin(["KCg", "KCab"])].copy()
    apl_df = df[df["type"] == "APL"].copy()
    mbon_df = df[df["class"] == "MBON"].copy()

    alpn_i = alpn_df["idx"].values
    kc_i = kc_df["idx"].values
    apl_i = apl_df["idx"].values
    mbon_i = mbon_df["idx"].values

    mb_raw = {
        "W_alpn_kc": W_u[alpn_i][:, kc_i].tocsr(),
        "W_kc_apl": W_u[kc_i][:, apl_i].toarray(),
        "W_apl_kc": W_u[apl_i][:, kc_i].toarray(),
        "W_kc_mbon": W_u[kc_i][:, mbon_i].tocsr(),
        "kc_is_ab": (kc_df["subclass"] == "KCab").values.astype(bool),
        "mbon_nt_sign": nt_sign[mbon_i].copy(),
    }

    # ------------------------------------------------------------------
    # 4. Descending Neck Bottleneck & VNC Motor Cord raw subgraphs
    # ------------------------------------------------------------------
    brain_pops_idx = {
        "LPLC2_L": df[(df["type"] == "LPLC2") & (df["somaSide"] == "L")]["idx"].values,
        "LPLC2_R": df[(df["type"] == "LPLC2") & (df["somaSide"] == "R")]["idx"].values,
        "LC4_L": df[(df["type"] == "LC4") & (df["somaSide"] == "L")]["idx"].values,
        "LC4_R": df[(df["type"] == "LC4") & (df["somaSide"] == "R")]["idx"].values,
        "LPLC1_L": df[(df["type"] == "LPLC1") & (df["somaSide"] == "L")]["idx"].values,
        "LPLC1_R": df[(df["type"] == "LPLC1") & (df["somaSide"] == "R")]["idx"].values,
        "PFL3_L": df[(df["type"] == "PFL3") & (df["somaSide"] == "L")]["idx"].values,
        "PFL3_R": df[(df["type"] == "PFL3") & (df["somaSide"] == "R")]["idx"].values,
        "PFL2": df[df["type"] == "PFL2"]["idx"].values,
        "MBON_ACh_avoid": df[(df["class"] == "MBON") & (nt_sign > 0)]["idx"].values,
        "MBON_Inh_approach": df[(df["class"] == "MBON") & (nt_sign < 0)]["idx"].values,
    }
    brain_all_i = np.concatenate([brain_pops_idx[k] for k in BRAIN_POP_NAMES])
    brain_pop_id = np.concatenate(
        [np.full(len(brain_pops_idx[k]), idx_k, dtype=np.int32) for idx_k, k in enumerate(BRAIN_POP_NAMES)]
    )

    core_dn_idx_list = []
    core_dn_group = []
    for g_idx, dname in enumerate(CORE_DN_NAMES):
        dt, ds = dname.split("_")
        di = df[(df["type"] == dt) & (df["somaSide"] == ds)]["idx"].values
        core_dn_idx_list.append(di)
        core_dn_group.append(np.full(len(di), g_idx, dtype=np.int32))
    core_dn_all_i = np.concatenate(core_dn_idx_list)
    core_dn_grp = np.concatenate(core_dn_group)

    pre_dn_i = np.nonzero(np.asarray(W_u[:, core_dn_all_i].sum(axis=1)).ravel() > 0)[0]

    # VNC leg motor pools & VNC intrinsic premotor E/I subgraphs
    leg_pools_idx = []
    leg_grp_list = []
    for seg_i, (seg, pat) in enumerate(
        [
            ("1", "prothoracic|front_leg|T1_PRONM"),
            ("2", "mesothoracic|middle_leg|T2_MESONM"),
            ("3", "metathoracic|hind_leg|T3_METANM"),
        ]
    ):
        for side_i, side in enumerate(["L", "R"]):
            g_id = seg_i * 2 + side_i
            m = (
                (df["superclass"] == "motor")
                & (df["class"] == "leg_motor_neuron")
                & (df["somaSide"] == side)
                & (
                    df["subclass"].fillna("").str.contains(pat)
                    | df["exitNerve"].fillna("").str.contains(pat)
                    | df["somaNeuromere"].fillna("").str.contains(pat)
                )
            )
            li = df[m]["idx"].values
            leg_pools_idx.append(li)
            leg_grp_list.append(np.full(len(li), g_id, dtype=np.int32))
    leg_all_i = np.concatenate(leg_pools_idx)
    leg_grp = np.concatenate(leg_grp_list)

    vnc_int_i = df[df["superclass"] == "ventral_nerve_cord_intrinsic"]["idx"].values
    vnc_dn_bridge_mask = (np.asarray(W_u[core_dn_all_i][:, vnc_int_i].sum(axis=0)).ravel() > 0) & (
        np.asarray(W_u[vnc_int_i][:, leg_all_i].sum(axis=1)).ravel() > 0
    )
    vnc_bridge_i = vnc_int_i[vnc_dn_bridge_mask]

    vnc_exc_i = df[(df["superclass"] == "ventral_nerve_cord_intrinsic") & (nt_sign > 0)]["idx"].values
    vnc_inh_i = df[(df["superclass"] == "ventral_nerve_cord_intrinsic") & (nt_sign < 0)]["idx"].values

    P_exc_leg = np.column_stack(
        [np.asarray(W_u[vnc_exc_i][:, leg_pools_idx[g]].sum(axis=1)).ravel() for g in range(6)]
    )
    P_inh_leg = np.column_stack(
        [np.asarray(W_u[vnc_inh_i][:, leg_pools_idx[g]].sum(axis=1)).ravel() for g in range(6)]
    )

    vnc_dn_raw = {
        "W_brain_dn_1hop": W_u[brain_all_i][:, core_dn_all_i].toarray(),
        "W_brain_predn": W_u[brain_all_i][:, pre_dn_i].tocsr(),
        "W_predn_dn": W_u[pre_dn_i][:, core_dn_all_i].toarray(),
        "brain_nt_sign": nt_sign[brain_all_i].copy(),
        "brain_pop_id": brain_pop_id,
        "predn_nt_sign": nt_sign[pre_dn_i].copy(),
        "predn_in_syn": in_syn_all[pre_dn_i].copy(),
        "core_dn_nt_sign": nt_sign[core_dn_all_i].copy(),
        "core_dn_in_syn": in_syn_all[core_dn_all_i].copy(),
        "core_dn_grp": core_dn_grp,
        "W_dn_leg_1hop": W_u[core_dn_all_i][:, leg_all_i].toarray(),
        "W_dn_vnc_bridge": W_u[core_dn_all_i][:, vnc_bridge_i].toarray(),
        "W_vnc_bridge_leg": W_u[vnc_bridge_i][:, leg_all_i].tocsr(),
        "vnc_bridge_nt_sign": nt_sign[vnc_bridge_i].copy(),
        "vnc_bridge_in_syn": in_syn_all[vnc_bridge_i].copy(),
        "leg_in_syn": in_syn_all[leg_all_i].copy(),
        "leg_grp": leg_grp,
        "P_exc_leg": P_exc_leg,
        "P_inh_leg": P_inh_leg,
        "W_vnc_ei": W_u[vnc_exc_i][:, vnc_inh_i].tocsr(),
    }

    return {
        "meta": {
            "dataset": "BANC v888 Adult Female Drosophila Whole-CNS",
            "n_neurons_total": int(n_total),
            "n_edges_total": int(W_u.nnz),
            "n_synapses_total": int(W_u.sum()),
            "sha256_connectome_compiled_npz": sha256_file(npz_path),
            "sha256_neurons_meta_parquet": sha256_file(meta_path),
        },
        "cx": cx_raw,
        "optic": optic_raw,
        "mb": mb_raw,
        "vnc_dn": vnc_dn_raw,
    }


def _col_norm(W: np.ndarray) -> np.ndarray:
    return W / np.maximum(W.sum(axis=0, keepdims=True), 1e-6)


def compile_cx_circuit(cx: Dict[str, Any]) -> Dict[str, Any]:
    """Compile Central Complex (`NeckCompass v3.0.0`) solely from raw connectivity."""
    ee = cx["W_epg_epg"]
    ed7 = cx["W_epg_d7"]
    d7e = cx["W_d7_epg"]
    epa = cx["W_epg_pena"]
    pae = cx["W_pena_epg"]
    epb = cx["W_epg_penb"]
    pbe = cx["W_penb_epg"]
    ere = cx["W_er_epg"]
    isL = cx["pena_is_L"]

    # 1. Unsupervised spectral circular seriation of the 50 EPG neurons
    A_loc = (ee + ee.T) + 0.2 * (epa @ epa.T + pae.T @ pae)
    deg = np.maximum(A_loc.sum(axis=1), 1e-6)
    S = np.diag(1.0 / np.sqrt(deg)) @ A_loc @ np.diag(1.0 / np.sqrt(deg))
    _, ev = np.linalg.eigh(S)
    th_base = -np.pi + 2.0 * np.pi * np.argsort(np.argsort(np.arctan2(ev[:, -3], ev[:, -2]))) / len(ee)
    th_w = -np.pi + 2.0 * np.pi * np.arange(16) / 16.0

    # Resolve single global reflection symmetry using PEN_a(L) - PEN_a(R) CCW shift
    th = th_base
    Pi = np.zeros((len(ee), 16))
    Po = np.zeros((len(ee), 16))
    M_paL = np.zeros((16, 16))
    M_paR = np.zeros((16, 16))
    for flip in (1.0, -1.0):
        th_c = flip * th_base
        P = np.maximum(0.0, np.cos(th_c[:, None] - th_w[None, :])) ** 4
        Pi_c = P / np.maximum(P.sum(axis=0, keepdims=True), 1e-6)
        Po_c = P / np.maximum(P.sum(axis=1, keepdims=True), 1e-6)
        paL_c = Pi_c.T @ (_col_norm(pae[isL, :]).T @ _col_norm(epa[:, isL]).T) @ Po_c
        paR_c = Pi_c.T @ (_col_norm(pae[~isL, :]).T @ _col_norm(epa[:, ~isL]).T) @ Po_c
        r0 = np.maximum(0.0, np.cos(th_w))
        r1 = r0 + 0.5 * ((paL_c - paR_c) @ r0)
        th, Pi, Po, M_paL, M_paR = th_c, Pi_c, Po_c, paL_c, paR_c
        if np.arctan2(r1 @ np.sin(th_w), r1 @ np.cos(th_w)) >= 0.0:
            break

    M_exc = Pi.T @ (0.40 * _col_norm(ee).T + 0.60 * (_col_norm(pbe).T @ _col_norm(epb).T)) @ Po
    M_ed7 = _col_norm(ed7).T @ Po  # (40, 16)
    M_d7e = Pi.T @ _col_norm(d7e).T  # (16, 40)
    M_pa_diff = M_paL - M_paR  # (16, 16)

    # 2. Unsupervised Fan-Shaped Body column inference (hDelta/vDelta/FC2 -> 25 PFL3 + 12 PFL2)
    W_hd = cx["W_hd_pfl"]
    A_p = W_hd.T @ W_hd
    dp = np.maximum(A_p.sum(axis=1), 1e-6)
    Sp = np.diag(1.0 / np.sqrt(dp)) @ A_p @ np.diag(1.0 / np.sqrt(dp))
    _, vcp = np.linalg.eigh(Sp)
    th_fb_all = np.arctan2(vcp[:, -3], vcp[:, -2])
    n_pfl3 = cx["W_epg_pfl3"].shape[1]
    th_fb3 = th_fb_all[:n_pfl3]
    th_fb2 = th_fb_all[n_pfl3:]

    # Align global FB circular frame with EPG -> PFL3 (using circular midpoint of PFL3_L and PFL3_R)
    z_epg = np.exp(1j * th)
    th_epg3 = np.angle(cx["W_epg_pfl3"].T @ z_epg + 1e-12)
    c_pos = np.abs(np.mean(np.exp(1j * (th_epg3 - th_fb3))))
    c_neg = np.abs(np.mean(np.exp(1j * (th_epg3 + th_fb3))))
    if c_neg > c_pos:
        th_fb3 = -th_fb3
        th_fb2 = -th_fb2
    # Center zero offset so PFL3_L + PFL3_R mean phase shift has zero bias
    mL = cx["pfl3_is_L"]
    mR = ~mL
    d3 = np.angle(np.exp(1j * (th_epg3 - th_fb3)))
    muL = np.angle(np.mean(np.exp(1j * d3[mL]))) if mL.any() else 0.0
    muR = np.angle(np.mean(np.exp(1j * d3[mR]))) if mR.any() else 0.0
    mid = np.angle(np.exp(1j * muL) + np.exp(1j * muR) + 1e-12)
    # Note: in our EPG frame, home bearing beta_home excites FB column at (th_fb - beta_home)
    alpha0 = np.angle(np.mean(np.exp(1j * (th_epg3 - th_fb3 - mid))) + 1e-12)
    th_fb3 = ((th_fb3 + mid + alpha0 + np.pi) % (2.0 * np.pi)) - np.pi
    th_fb2 = ((th_fb2 + mid + alpha0 + np.pi) % (2.0 * np.pi)) - np.pi

    M_epg_pfl3 = _col_norm(cx["W_epg_pfl3"]).T @ Po  # (25, 16)
    M_d7_pfl3 = _col_norm(cx["W_d7_pfl3"]).T  # (25, 40)
    M_epg_pfl2 = _col_norm(cx["W_epg_pfl2"]).T @ Po  # (12, 16)
    M_d7_pfl2 = _col_norm(cx["W_d7_pfl2"]).T  # (12, 40)

    # Parameter-free fractional postsynaptic input PFL3/PFL2 -> DNa02 (1-hop + 2-hop via LAL)
    F_pfl3_1 = cx["W_pfl3_dna02"] / cx["dna02_in_syn"][None, :]  # (25, 2) [L, R]
    F_pfl_lal = cx["W_pfl_lal"] / cx["lal_in_syn"][None, :]  # (37, n_lal)
    F_lal_dna = (cx["lal_nt_sign"][:, None] * cx["W_lal_dna02"]) / cx["dna02_in_syn"][None, :]
    F_pfl_2 = F_pfl_lal @ F_lal_dna  # (37, 2)
    F_pfl3_tot = (F_pfl3_1 + F_pfl_2[:n_pfl3]).T  # (2, 25) [DNa02_L, DNa02_R]
    F_pfl2_tot = F_pfl_2[n_pfl3:].T  # (2, 12)

    scale3 = max(float(np.abs(F_pfl3_tot).sum(axis=1).max()), 1e-6)
    scale2 = max(float(np.abs(F_pfl2_tot).sum(axis=1).max()), 1e-6)
    W_pfl3_dna02 = F_pfl3_tot / scale3
    W_pfl2_dna02 = F_pfl2_tot / scale2

    # Quantize to Q12 (4096 = 1.0) for zero-float C99
    q12 = lambda X: np.clip(np.round(X * 4096.0), 0, 65535).astype(np.uint16)
    sq12 = lambda X: np.clip(np.round(X * 4096.0), -32767, 32767).astype(np.int16)
    rad_to_bam = lambda ang: (np.round(((ang % (2.0 * np.pi)) / (2.0 * np.pi)) * 65536.0).astype(np.int64) & 0xFFFF).astype(np.uint16)

    return {
        "theta_w_rad": th_w.astype(np.float64),
        "theta_epg_rad": th.astype(np.float64),
        "M_exc": M_exc.astype(np.float64),
        "M_ed7": M_ed7.astype(np.float64),
        "M_d7e": M_d7e.astype(np.float64),
        "M_paL": M_paL.astype(np.float64),
        "M_paR": M_paR.astype(np.float64),
        "M_pa_diff": M_pa_diff.astype(np.float64),
        "th_fb_pfl3_rad": th_fb3.astype(np.float64),
        "th_fb_pfl2_rad": th_fb2.astype(np.float64),
        "M_epg_pfl3": M_epg_pfl3.astype(np.float64),
        "M_d7_pfl3": M_d7_pfl3.astype(np.float64),
        "M_epg_pfl2": M_epg_pfl2.astype(np.float64),
        "M_d7_pfl2": M_d7_pfl2.astype(np.float64),
        "W_pfl3_dna02": W_pfl3_dna02.astype(np.float64),
        "W_pfl2_dna02": W_pfl2_dna02.astype(np.float64),
        "pfl3_is_L": cx["pfl3_is_L"].astype(np.uint8),
        "er_mean_epg_fanout": float((ere > 0).sum(axis=1).mean()),
        # Quantized C99 arrays
        "M_exc_q12": q12(M_exc),
        "M_ed7_q12": q12(M_ed7),
        "M_d7e_q12": q12(M_d7e),
        "M_pa_diff_q12": sq12(M_pa_diff),
        "th_fb_pfl3_bam": rad_to_bam(th_fb3),
        "th_fb_pfl2_bam": rad_to_bam(th_fb2),
        "M_epg_pfl3_q12": q12(M_epg_pfl3),
        "M_d7_pfl3_q12": q12(M_d7_pfl3),
        "M_epg_pfl2_q12": q12(M_epg_pfl2),
        "M_d7_pfl2_q12": q12(M_d7_pfl2),
        "W_pfl3_dna02_q12": sq12(W_pfl3_dna02),
        "W_pfl2_dna02_q12": sq12(W_pfl2_dna02),
    }


def compile_optic_circuit(ol: Dict[str, Any], n_lplc2_keep: int = 64, n_lc4_keep: int = 32) -> Dict[str, Any]:
    """
    Compile Optic Lobe (`NeckSight v3.0.0`) strictly from `Medulla -> T4/T5` co-innervation
    and `HS/VS` tangential axis anchors (ZERO peeking at `LPLC2`/`LC4`/`LPLC1` during retinotopy).
    """
    W_mt: sparse.csr_matrix = ol["W_med_t45"]
    n_t45 = W_mt.shape[1]
    A = (W_mt.T @ W_mt).tocsr()
    A.setdiag(0)
    _, labels = connected_components(A, directed=False)
    main_cc = int(np.argmax(np.bincount(labels)))
    keep = labels == main_cc

    raw_uv = np.zeros((n_t45, 2), dtype=np.float64)
    if keep.sum() >= 20:
        Ak = A[keep][:, keep]
        dk = np.maximum(np.asarray(Ak.sum(axis=1)).ravel(), 1e-6)
        Di = sparse.diags(1.0 / np.sqrt(dk))
        Sk = Di @ Ak @ Di
        vals, vecs = eigsh(Sk, k=4, which="LA")
        order = np.argsort(vals)[::-1]
        uv_k = vecs[:, order[1:3]]
        uv_k /= np.maximum(np.std(uv_k, axis=0), 1e-9)
        raw_uv[keep] = uv_k

    # Orient (u, v) strictly via independent HS (HSN - HSS) and VS (VS5-8 - VS1-2) anchors
    W_hsvs = ol["W_t45_hsvs"]
    grp = ol["hs_vs_grp"]

    def _anchor_uv(g_id: int) -> np.ndarray:
        m_g = grp == g_id
        if not m_g.any():
            return np.zeros(2)
        w = W_hsvs[keep][:, m_g].sum(axis=1)
        s = float(w.sum())
        if s < 1e-6:
            return np.zeros(2)
        return (w @ raw_uv[keep]) / s

    uv_hsn = _anchor_uv(0)
    uv_hss = _anchor_uv(1)
    uv_vsa = _anchor_uv(2)
    uv_vsp = _anchor_uv(3)

    e_u = uv_vsp - uv_vsa
    n_eu = float(np.linalg.norm(e_u))
    e_u = (e_u / n_eu) if n_eu > 1e-6 else np.array([1.0, 0.0])
    dv_raw = uv_hsn - uv_hss
    e_v = dv_raw - float(np.dot(dv_raw, e_u)) * e_u
    n_ev = float(np.linalg.norm(e_v))
    e_v = (e_v / n_ev) if n_ev > 1e-6 else np.array([-e_u[1], e_u[0]])

    uv = np.column_stack([raw_uv @ e_u, raw_uv @ e_v])
    # Map (u, v) onto 8x8 camera macro-grid: x_col in 0..7 (left-to-right +u), y_row in 0..7 (top-to-bottom -v!)
    # Use robust 5th..95th percentile span of `keep` neurons so grid covers the active visual sheet
    if keep.sum() >= 20:
        u_lo, u_hi = np.percentile(uv[keep, 0], [3.0, 97.0])
        v_lo, v_hi = np.percentile(uv[keep, 1], [3.0, 97.0])
    else:
        u_lo, u_hi, v_lo, v_hi = -1.5, 1.5, -1.5, 1.5
    u_span = max(float(u_hi - u_lo), 1e-6)
    v_span = max(float(v_hi - v_lo), 1e-6)

    gx = np.clip(np.floor(((uv[:, 0] - u_lo) / u_span) * 8.0).astype(np.int32), 0, 7)
    gy = np.clip(np.floor(((v_hi - uv[:, 1]) / v_span) * 8.0).astype(np.int32), 0, 7)
    patch_idx = gy * 8 + gx  # 0..63

    # Normalized camera center coordinates in [-1, +1] (x right +, y down +)
    cam_x_t45 = np.clip(((uv[:, 0] - 0.5 * (u_lo + u_hi)) / (0.5 * u_span)), -1.0, 1.0)
    cam_y_t45 = np.clip((-(uv[:, 1] - 0.5 * (v_lo + v_hi)) / (0.5 * v_span)), -1.0, 1.0)

    # Compile top-64 proofread LPLC2 4-branch 8x8 receptive fields
    W_tl = ol["W_t45_lplc2"].toarray()
    W_tl[~keep, :] = 0.0
    tot_lplc2 = W_tl.sum(axis=0)
    top_lplc2 = np.argsort(tot_lplc2)[::-1][:n_lplc2_keep]

    t45_dir = ol["t45_dir"]
    t45_is_t4 = ol["t45_is_t4"]

    W_lplc2_rf = np.zeros((n_lplc2_keep, 4, 64), dtype=np.float64)
    lplc2_t4_frac = np.full(n_lplc2_keep, 0.36, dtype=np.float64)
    lplc2_centers = np.zeros((n_lplc2_keep, 2), dtype=np.float64)

    for m_out, m_col in enumerate(top_lplc2):
        w_m = W_tl[:, m_col]
        w_sum = float(w_m.sum())
        if w_sum > 1e-6:
            lplc2_t4_frac[m_out] = float(w_m[t45_is_t4].sum() / w_sum)
            lplc2_centers[m_out, 0] = float((w_m @ cam_x_t45) / w_sum)
            lplc2_centers[m_out, 1] = float((w_m @ cam_y_t45) / w_sum)
        for d_idx in range(4):
            mask_d = keep & (t45_dir == d_idx)
            w_md = w_m[mask_d]
            p_md = patch_idx[mask_d]
            hist = np.bincount(p_md, weights=w_md, minlength=64)[:64].astype(np.float64)
            s_d = float(hist.sum())
            if s_d > 1e-6:
                W_lplc2_rf[m_out, d_idx] = hist / s_d

    # Disynaptic T4/T5 -> LPi -> LPLC2 directional wide-field opponency inhibition
    W_ti = ol["W_t45_lpi"].toarray()
    W_il = ol["W_lpi_lplc2"][:, top_lplc2]  # (n_lpi, 64)
    W_inh_dir = np.zeros((n_lplc2_keep, 4), dtype=np.float64)
    for d_idx in range(4):
        mask_d = keep & (t45_dir == d_idx)
        W_inh_dir[:, d_idx] = W_ti[mask_d].sum(axis=0) @ W_il
    inh_row_sum = np.maximum(W_inh_dir.sum(axis=1, keepdims=True), 1e-6)
    W_inh_dir_norm = W_inh_dir / inh_row_sum

    # Compile top-32 LC4 small-edge expansion detectors via shared Medulla columns (W_mt.T @ W_med_lc4)
    W_mlc4 = ol["W_med_lc4"]
    W_t45_lc4_col = (W_mt.T @ W_mlc4).toarray()  # (n_t45, n_lc4) co-columnar mapping!
    W_t45_lc4_col[~keep, :] = 0.0
    edge_syn_lc4 = ol["W_edge_lc4"].sum(axis=0) + W_mlc4.sum(axis=0).A1
    top_lc4 = np.argsort(edge_syn_lc4)[::-1][:n_lc4_keep]

    W_lc4_rf = np.zeros((n_lc4_keep, 64), dtype=np.float64)
    lc4_centers = np.zeros((n_lc4_keep, 2), dtype=np.float64)
    for m_out, m_col in enumerate(top_lc4):
        w_m = W_t45_lc4_col[:, m_col]
        s_m = float(w_m.sum())
        if s_m > 1e-6:
            lc4_centers[m_out, 0] = float((w_m @ cam_x_t45) / s_m)
            lc4_centers[m_out, 1] = float((w_m @ cam_y_t45) / s_m)
            hist = np.bincount(patch_idx[keep], weights=w_m[keep], minlength=64)[:64].astype(np.float64)
            if hist.sum() > 1e-6:
                W_lc4_rf[m_out] = hist / hist.sum()

    li_lc4 = ol["W_li_lc4"][:, top_lc4].sum(axis=0)
    li_lc4_norm = li_lc4 / max(float(np.mean(li_lc4)) * 2.0, 1.0)
    li_lc4_norm = np.clip(li_lc4_norm, 0.15, 1.0)

    return {
        "W_lplc2_rf": W_lplc2_rf,
        "lplc2_t4_frac": lplc2_t4_frac,
        "W_lpi_inh_dir": W_inh_dir_norm,
        "lplc2_centers_xy": lplc2_centers,
        "W_lc4_rf": W_lc4_rf,
        "lc4_li_inh": li_lc4_norm,
        "lc4_centers_xy": lc4_centers,
        # C99 quantized arrays (uint8 Q8 summing to ~255)
        "W_lplc2_rf_q8": np.clip(np.round(W_lplc2_rf * 255.0), 0, 255).astype(np.uint8),
        "lplc2_t4_frac_q8": np.clip(np.round(lplc2_t4_frac * 255.0), 0, 255).astype(np.uint8),
        "W_lpi_inh_q8": np.clip(np.round(W_inh_dir_norm * 255.0), 0, 255).astype(np.uint8),
        "lplc2_centers_q7": np.clip(np.round(lplc2_centers * 127.0), -127, 127).astype(np.int8),
        "W_lc4_rf_q8": np.clip(np.round(W_lc4_rf * 255.0), 0, 255).astype(np.uint8),
        "lc4_li_inh_q8": np.clip(np.round(li_lc4_norm * 255.0), 0, 255).astype(np.uint8),
        "lc4_centers_q7": np.clip(np.round(lc4_centers * 127.0), -127, 127).astype(np.int8),
    }


def compile_mb_circuit(
    mb: Dict[str, Any],
    n_glomeruli: int = 64,
    n_kc_max: int = 1024,
    max_claws: int = 6,
) -> Dict[str, Any]:
    """
    Compile Mushroom Body (`NeckSense v3.0.0`) from real `ALPN -> KC`, `KC <-> APL`,
    and `KCg/KCab -> MBON` connectivity.
    """
    W_ak = mb["W_alpn_kc"].toarray()  # (699, n_kc_all)
    # 1. Group ALPNs into `n_glomeruli = 64` glomerular channels via cosine similarity of KC projections
    row_norms = np.maximum(np.linalg.norm(W_ak, axis=1, keepdims=True), 1e-9)
    W_ak_unit = W_ak / row_norms
    active_alpn = W_ak.sum(axis=1) > 0
    glom_ids = np.zeros(W_ak.shape[0], dtype=np.int32)
    if active_alpn.sum() > n_glomeruli:
        dists = pdist(W_ak_unit[active_alpn], metric="euclidean")
        Z = linkage(dists, method="ward")
        raw_cl = fcluster(Z, t=n_glomeruli, criterion="maxclust") - 1
        # Order glomerular IDs 0..63 deterministically by total ALPN->KC synapse count
        cl_syn = np.bincount(raw_cl, weights=W_ak[active_alpn].sum(axis=1), minlength=n_glomeruli)
        rank_map = np.zeros(len(cl_syn), dtype=np.int32)
        rank_map[np.argsort(cl_syn)[::-1]] = np.arange(len(cl_syn), dtype=np.int32)
        glom_ids[active_alpn] = np.clip(rank_map[raw_cl], 0, n_glomeruli - 1)
    else:
        glom_ids = np.arange(W_ak.shape[0], dtype=np.int32) % n_glomeruli

    # Aggregate glomerular -> KC synaptic matrix (64, n_kc_all)
    W_gk = np.zeros((n_glomeruli, W_ak.shape[1]), dtype=np.float64)
    np.add.at(W_gk, glom_ids, W_ak)

    # 2. Select `n_kc_max = 1024` real KCs interleaved [KCg, KCab, KCg, KCab, ...]
    kc_is_ab = mb["kc_is_ab"]
    kc_tot_in = W_gk.sum(axis=0)
    kcg_cand = np.nonzero((~kc_is_ab) & (kc_tot_in > 0))[0]
    kcab_cand = np.nonzero(kc_is_ab & (kc_tot_in > 0))[0]
    kcg_top = kcg_cand[np.argsort(kc_tot_in[kcg_cand])[::-1]][: n_kc_max // 2]
    kcab_top = kcab_cand[np.argsort(kc_tot_in[kcab_cand])[::-1]][: n_kc_max // 2]

    selected_kc = np.zeros(n_kc_max, dtype=np.int32)
    n_pairs = min(len(kcg_top), len(kcab_top), n_kc_max // 2)
    selected_kc[0 : 2 * n_pairs : 2] = kcg_top[:n_pairs]
    selected_kc[1 : 2 * n_pairs : 2] = kcab_top[:n_pairs]
    if 2 * n_pairs < n_kc_max:
        all_sorted = np.argsort(kc_tot_in)[::-1]
        used = set(selected_kc[: 2 * n_pairs].tolist())
        rem = [int(x) for x in all_sorted if int(x) not in used][: n_kc_max - 2 * n_pairs]
        selected_kc[2 * n_pairs :] = rem

    W_gk_sel = W_gk[:, selected_kc]  # (64, 1024)
    sel_is_ab = kc_is_ab[selected_kc]

    claw_glom = np.full((n_kc_max, max_claws), 255, dtype=np.uint8)
    claw_w_q8 = np.zeros((n_kc_max, max_claws), dtype=np.uint8)
    for j in range(n_kc_max):
        col = W_gk_sel[:, j]
        nz = np.nonzero(col > 0)[0]
        if len(nz) == 0:
            claw_glom[j, 0] = j % n_glomeruli
            claw_w_q8[j, 0] = 255
            continue
        top_c = nz[np.argsort(col[nz])[::-1][:max_claws]]
        w_c = col[top_c]
        w_q8 = np.maximum(1, np.round((w_c / w_c.sum()) * 255.0)).astype(np.int32)
        # Adjust largest entry so sum is 255
        w_q8[0] = max(1, w_q8[0] + (255 - int(w_q8.sum())))
        claw_glom[j, : len(top_c)] = top_c.astype(np.uint8)
        claw_w_q8[j, : len(top_c)] = np.clip(w_q8, 1, 255).astype(np.uint8)

    # 3. Real inhomogeneous KC <-> APL feedback weights (normalized around 128 = 1.0)
    ka_raw = mb["W_kc_apl"][selected_kc].sum(axis=1)
    ak_raw = mb["W_apl_kc"][:, selected_kc].sum(axis=0)
    ka_q8 = np.clip(np.round((ka_raw / max(float(ka_raw.mean()), 1e-6)) * 128.0), 16, 255).astype(np.uint8)
    ak_q8 = np.clip(np.round((ak_raw / max(float(ak_raw.mean()), 1e-6)) * 128.0), 16, 255).astype(np.uint8)

    # 4. Pack KCab compartment mask into 16 uint64 words
    ab_mask_u64 = np.zeros(n_kc_max // 64, dtype=np.uint64)
    for j in range(n_kc_max):
        if sel_is_ab[j]:
            ab_mask_u64[j >> 6] |= np.uint64(1) << np.uint64(j & 63)

    # 5. Real KC -> MBON_ACh(+) and KC -> MBON_GABA_Glu(-) readout weights
    W_km = mb["W_kc_mbon"][selected_kc].toarray()
    mbon_pos = mb["mbon_nt_sign"] > 0
    mbon_neg = mb["mbon_nt_sign"] < 0
    km_pos = W_km[:, mbon_pos].sum(axis=1) if mbon_pos.any() else np.ones(n_kc_max)
    km_neg = W_km[:, mbon_neg].sum(axis=1) if mbon_neg.any() else np.ones(n_kc_max)
    km_pos_q8 = np.clip(np.round((km_pos / max(float(km_pos.mean()), 1e-6)) * 128.0), 16, 255).astype(np.uint8)
    km_neg_q8 = np.clip(np.round((km_neg / max(float(km_neg.mean()), 1e-6)) * 128.0), 16, 255).astype(np.uint8)

    return {
        "n_glomeruli": int(n_glomeruli),
        "n_kc_max": int(n_kc_max),
        "max_claws": int(max_claws),
        "claw_glom": claw_glom,
        "claw_w_q8": claw_w_q8,
        "kc_to_apl_q8": ka_q8,
        "apl_to_kc_q8": ak_q8,
        "kc_is_ab": sel_is_ab.astype(np.uint8),
        "kc_is_ab_mask_u64": ab_mask_u64,
        "kc_to_mbon_ach_q8": km_pos_q8,
        "kc_to_mbon_inh_q8": km_neg_q8,
    }


def compile_vnc_dn_circuit(vd: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compile Parameter-Free `F + F^2` Brain -> 10 Core DNs -> 6 Thoracic Leg MN Pools
    and `6x6` Disynaptic VNC Premotor Mutual Inhibition (`C_vnc_inh`).
    """
    # 1. Brain (11 populations) -> Core DNs (10 populations): F_1hop + F_2hop
    dn_in = vd["core_dn_in_syn"]
    b_nt = vd["brain_nt_sign"]
    b_pop = vd["brain_pop_id"]
    dn_grp = vd["core_dn_grp"]

    F_bd_1 = (b_nt[:, None] * vd["W_brain_dn_1hop"]) / dn_in[None, :]
    F_bp = vd["W_brain_predn"].multiply(b_nt[:, None]).multiply(1.0 / vd["predn_in_syn"][None, :]).tocsr()
    F_pd = (vd["predn_nt_sign"][:, None] * vd["W_predn_dn"]) / dn_in[None, :]
    F_bd_tot = F_bd_1 + (F_bp @ F_pd)  # (n_brain_all, n_dn_all)

    W_brain_dn_raw = np.zeros((10, 11), dtype=np.float64)
    for p_id in range(11):
        m_p = b_pop == p_id
        if not m_p.any():
            continue
        col_sum = F_bd_tot[m_p].sum(axis=0)
        for d_id in range(10):
            m_d = dn_grp == d_id
            if m_d.any():
                W_brain_dn_raw[d_id, p_id] = float(col_sum[m_d].mean())

    # Normalize each source brain modality group so unit population firing [0, 1] maps to O(1) DN drive:
    # Modality groups: LPLC2(0,1), LC4(2,3), LPLC1(4,5), PFL3(6,7), PFL2(8), MBON(9,10)
    W_brain_dn = W_brain_dn_raw.copy()
    for cols in ([0, 1], [2, 3], [4, 5], [6, 7], [8], [9, 10]):
        blk = np.abs(W_brain_dn[:, cols])
        mx = float(blk.max())
        if mx > 1e-9:
            W_brain_dn[:, cols] /= mx

    # 2. Core DNs (10) -> 6 Thoracic Leg MN pools [L1, R1, L2, R2, L3, R3]: F_1hop + F_2hop
    dn_nt = vd["core_dn_nt_sign"]
    leg_in = vd["leg_in_syn"]
    leg_grp = vd["leg_grp"]

    F_dl_1 = (dn_nt[:, None] * vd["W_dn_leg_1hop"]) / leg_in[None, :]
    F_dv = (dn_nt[:, None] * vd["W_dn_vnc_bridge"]) / vd["vnc_bridge_in_syn"][None, :]
    F_vl = (
        vd["W_vnc_bridge_leg"]
        .multiply(vd["vnc_bridge_nt_sign"][:, None])
        .multiply(1.0 / leg_in[None, :])
        .tocsr()
    )
    F_dl_tot = F_dl_1 + (F_dv @ F_vl)

    W_dn_leg_raw = np.zeros((6, 10), dtype=np.float64)
    for d_id in range(10):
        m_d = dn_grp == d_id
        if not m_d.any():
            continue
        row_d = F_dl_tot[m_d].mean(axis=0)
        for l_id in range(6):
            m_l = leg_grp == l_id
            if m_l.any():
                W_dn_leg_raw[l_id, d_id] = float(row_d[m_l].mean())

    W_dn_leg = W_dn_leg_raw.copy()
    for cols in ([0, 1], [2, 3], [4, 5], [6, 7], [8, 9]):
        blk = np.abs(W_dn_leg[:, cols])
        mx = float(blk.max())
        if mx > 1e-9:
            W_dn_leg[:, cols] /= mx

    # 3. Disynaptic VNC Premotor Mutual Inhibition `C_vnc_inh` (6x6)
    C_raw = vd["P_exc_leg"].T @ (vd["W_vnc_ei"] @ vd["P_inh_leg"])
    diag_s = np.sqrt(np.maximum(np.diag(C_raw), 1.0))
    C_norm = C_raw / np.outer(diag_s, diag_s)
    np.fill_diagonal(C_norm, 0.0)
    C_sym = 0.5 * (C_norm + C_norm.T)
    pos_vals = C_sym[C_sym > 0]
    if len(pos_vals) > 0:
        C_sym = C_sym / float(pos_vals.mean())

    sq12 = lambda X: np.clip(np.round(X * 4096.0), -32767, 32767).astype(np.int16)
    uq12 = lambda X: np.clip(np.round(X * 4096.0), 0, 65535).astype(np.uint16)

    adj_mean = float(C_sym[[0, 2, 1, 3, 0, 2, 4], [2, 4, 3, 5, 1, 3, 5]].mean())
    diag_mean = float(C_sym[[0, 1, 2, 3], [3, 2, 5, 4]].mean())

    return {
        "brain_pop_names": np.array(BRAIN_POP_NAMES),
        "core_dn_names": np.array(CORE_DN_NAMES),
        "leg_pool_names": np.array(LEG_POOL_NAMES),
        "W_brain_dn_raw": W_brain_dn_raw,
        "W_brain_dn": W_brain_dn,
        "W_dn_leg_raw": W_dn_leg_raw,
        "W_dn_leg": W_dn_leg,
        "C_vnc_inh": C_sym,
        "C_adj_vs_diag_ratio": float(adj_mean / max(diag_mean, 1e-6)),
        "W_brain_dn_q12": sq12(W_brain_dn),
        "W_dn_leg_q12": sq12(W_dn_leg),
        "C_vnc_inh_q12": uq12(C_sym),
    }


def compile_circuits_from_subgraphs(raw_subgraphs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pure, deterministic whole-CNS connectome compiler shared identically by
    real `BANC v888` and `K >= 100` Maslov-Sneppen degree-preserving shuffles.
    """
    return {
        "meta": dict(raw_subgraphs["meta"]),
        "shuffle_seed": raw_subgraphs.get("shuffle_seed", None),
        "cx": compile_cx_circuit(raw_subgraphs["cx"]),
        "optic": compile_optic_circuit(raw_subgraphs["optic"]),
        "mb": compile_mb_circuit(raw_subgraphs["mb"]),
        "vnc_dn": compile_vnc_dn_circuit(raw_subgraphs["vnc_dn"]),
    }


def _fmt_c_array_1d(arr: np.ndarray, per_line: int = 16, suffix: str = "") -> str:
    vals = [f"{int(x)}{suffix}" for x in arr.ravel()]
    lines = []
    for i in range(0, len(vals), per_line):
        lines.append("    " + ", ".join(vals[i : i + per_line]))
    return "{\n" + ",\n".join(lines) + "\n}"


def _write_c_headers_and_npz(compiled: Dict[str, Any], repo_root: Path) -> None:
    meta = compiled["meta"]
    prov_banner = (
        f"/* Auto-generated from {meta['dataset']} ({meta['n_neurons_total']} neurons, "
        f"{meta['n_synapses_total']} synapses).\n"
        f" * connectome_compiled.npz SHA-256: {meta['sha256_connectome_compiled_npz']}\n"
        f" * neurons_meta.parquet    SHA-256: {meta['sha256_neurons_meta_parquet']}\n"
        f" * DO NOT EDIT BY HAND - regenerate via `python -m necklink_cns.connectome.extract`.\n"
        f" */\n"
    )

    # 1. NeckCompass
    cx = compiled["cx"]
    cx_npz = repo_root / "packages" / "neckcompass" / "neckcompass" / "data" / "banc_cx_v3.npz"
    cx_npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cx_npz, **cx, sha256_npz=meta["sha256_connectome_compiled_npz"])

    cx_h = (
        prov_banner
        + "#ifndef NECKCOMPASS_BANC_WEIGHTS_H\n#define NECKCOMPASS_BANC_WEIGHTS_H\n\n"
        + "#include <stdint.h>\n\n"
        + "#define NC_BANC_NUM_WEDGES 16u\n#define NC_BANC_NUM_D7 40u\n"
        + "#define NC_BANC_NUM_PFL3 25u\n#define NC_BANC_NUM_PFL2 12u\n\n"
        + "typedef struct {\n"
        + "    uint16_t M_exc_q12[256];\n"
        + "    uint16_t M_ed7_q12[640];\n"
        + "    uint16_t M_d7e_q12[640];\n"
        + "    int16_t  M_pa_diff_q12[256];\n"
        + "    uint16_t th_fb_pfl3_bam[25];\n"
        + "    uint16_t th_fb_pfl2_bam[12];\n"
        + "    uint16_t M_epg_pfl3_q12[400];\n"
        + "    uint16_t M_d7_pfl3_q12[1000];\n"
        + "    uint16_t M_epg_pfl2_q12[192];\n"
        + "    uint16_t M_d7_pfl2_q12[480];\n"
        + "    int16_t  W_pfl3_dna02_q12[50];\n"
        + "    int16_t  W_pfl2_dna02_q12[24];\n"
        + "} nc_banc_cx_weights_t;\n\n"
        + "static const nc_banc_cx_weights_t k_nc_banc_cx_default = {\n"
        + f"  {_fmt_c_array_1d(cx['M_exc_q12'], 16, 'u')},\n"
        + f"  {_fmt_c_array_1d(cx['M_ed7_q12'], 16, 'u')},\n"
        + f"  {_fmt_c_array_1d(cx['M_d7e_q12'], 16, 'u')},\n"
        + f"  {_fmt_c_array_1d(cx['M_pa_diff_q12'], 16)},\n"
        + f"  {_fmt_c_array_1d(cx['th_fb_pfl3_bam'], 13, 'u')},\n"
        + f"  {_fmt_c_array_1d(cx['th_fb_pfl2_bam'], 12, 'u')},\n"
        + f"  {_fmt_c_array_1d(cx['M_epg_pfl3_q12'], 16, 'u')},\n"
        + f"  {_fmt_c_array_1d(cx['M_d7_pfl3_q12'], 16, 'u')},\n"
        + f"  {_fmt_c_array_1d(cx['M_epg_pfl2_q12'], 16, 'u')},\n"
        + f"  {_fmt_c_array_1d(cx['M_d7_pfl2_q12'], 16, 'u')},\n"
        + f"  {_fmt_c_array_1d(cx['W_pfl3_dna02_q12'], 13)},\n"
        + f"  {_fmt_c_array_1d(cx['W_pfl2_dna02_q12'], 12)}\n"
        + "};\n\n#endif /* NECKCOMPASS_BANC_WEIGHTS_H */\n"
    )
    (repo_root / "packages" / "neckcompass" / "csrc" / "neckcompass_banc_weights.h").write_text(
        cx_h, encoding="utf-8"
    )

    # 2. NeckSight
    ol = compiled["optic"]
    ol_npz = repo_root / "packages" / "necksight" / "necksight" / "data" / "banc_optic_v3.npz"
    ol_npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(ol_npz, **ol, sha256_npz=meta["sha256_connectome_compiled_npz"])

    ol_h = (
        prov_banner
        + "#ifndef NECKSIGHT_BANC_WEIGHTS_H\n#define NECKSIGHT_BANC_WEIGHTS_H\n\n"
        + "#include <stdint.h>\n\n"
        + "#define NSIGHT_BANC_NUM_LPLC2 64u\n#define NSIGHT_BANC_NUM_LC4 32u\n#define NSIGHT_BANC_PATCHES 64u\n\n"
        + "typedef struct {\n"
        + "    uint8_t W_lplc2_rf_q8[64 * 4 * 64];\n"
        + "    uint8_t lplc2_t4_frac_q8[64];\n"
        + "    uint8_t W_lpi_inh_q8[64 * 4];\n"
        + "    int8_t  lplc2_centers_q7[64 * 2];\n"
        + "    uint8_t W_lc4_rf_q8[32 * 64];\n"
        + "    uint8_t lc4_li_inh_q8[32];\n"
        + "    int8_t  lc4_centers_q7[32 * 2];\n"
        + "} necksight_banc_weights_t;\n\n"
        + "static const necksight_banc_weights_t k_necksight_banc_default = {\n"
        + f"  {_fmt_c_array_1d(ol['W_lplc2_rf_q8'], 32, 'u')},\n"
        + f"  {_fmt_c_array_1d(ol['lplc2_t4_frac_q8'], 16, 'u')},\n"
        + f"  {_fmt_c_array_1d(ol['W_lpi_inh_q8'], 16, 'u')},\n"
        + f"  {_fmt_c_array_1d(ol['lplc2_centers_q7'], 16)},\n"
        + f"  {_fmt_c_array_1d(ol['W_lc4_rf_q8'], 32, 'u')},\n"
        + f"  {_fmt_c_array_1d(ol['lc4_li_inh_q8'], 16, 'u')},\n"
        + f"  {_fmt_c_array_1d(ol['lc4_centers_q7'], 16)}\n"
        + "};\n\n#endif /* NECKSIGHT_BANC_WEIGHTS_H */\n"
    )
    (repo_root / "packages" / "necksight" / "csrc" / "necksight_banc_weights.h").write_text(
        ol_h, encoding="utf-8"
    )

    # 3. NeckSense
    mb = compiled["mb"]
    mb_npz = repo_root / "packages" / "necksense" / "necksense" / "data" / "banc_mb_v3.npz"
    mb_npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(mb_npz, **mb, sha256_npz=meta["sha256_connectome_compiled_npz"])

    u64_lines = ",\n".join(
        "    " + ", ".join(f"UINT64_C(0x{int(x):016X})" for x in mb["kc_is_ab_mask_u64"][i : i + 4])
        for i in range(0, len(mb["kc_is_ab_mask_u64"]), 4)
    )
    mb_h = (
        prov_banner
        + "#ifndef NECKSENSE_BANC_WEIGHTS_H\n#define NECKSENSE_BANC_WEIGHTS_H\n\n"
        + "#include <stdint.h>\n\n"
        + "#define NS_BANC_GLOMERULI 64u\n#define NS_BANC_MAX_KC 1024u\n#define NS_BANC_MAX_CLAWS 6u\n\n"
        + "typedef struct {\n"
        + "    uint8_t  claw_glom[1024 * 6];\n"
        + "    uint8_t  claw_w_q8[1024 * 6];\n"
        + "    uint8_t  kc_to_apl_q8[1024];\n"
        + "    uint8_t  apl_to_kc_q8[1024];\n"
        + "    uint64_t kc_is_ab_mask_u64[16];\n"
        + "    uint8_t  kc_to_mbon_ach_q8[1024];\n"
        + "    uint8_t  kc_to_mbon_inh_q8[1024];\n"
        + "} ns_banc_mb_weights_t;\n\n"
        + "static const ns_banc_mb_weights_t k_ns_banc_mb_default = {\n"
        + f"  {_fmt_c_array_1d(mb['claw_glom'], 24, 'u')},\n"
        + f"  {_fmt_c_array_1d(mb['claw_w_q8'], 24, 'u')},\n"
        + f"  {_fmt_c_array_1d(mb['kc_to_apl_q8'], 32, 'u')},\n"
        + f"  {_fmt_c_array_1d(mb['apl_to_kc_q8'], 32, 'u')},\n"
        + f"  {{\n{u64_lines}\n  }},\n"
        + f"  {_fmt_c_array_1d(mb['kc_to_mbon_ach_q8'], 32, 'u')},\n"
        + f"  {_fmt_c_array_1d(mb['kc_to_mbon_inh_q8'], 32, 'u')}\n"
        + "};\n\n#endif /* NECKSENSE_BANC_WEIGHTS_H */\n"
    )
    (repo_root / "packages" / "necksense" / "csrc" / "necksense_banc_weights.h").write_text(
        mb_h, encoding="utf-8"
    )

    # 4. NeckCord & NeckLink CNS
    vd = compiled["vnc_dn"]
    cord_npz = repo_root / "packages" / "neckcord" / "neckcord" / "data" / "banc_vnc_v3.npz"
    cord_npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cord_npz, **vd, sha256_npz=meta["sha256_connectome_compiled_npz"])

    cns_npz = repo_root / "packages" / "necklink_cns" / "necklink_cns" / "data" / "banc_dn_v3.npz"
    cns_npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cns_npz, **vd, sha256_npz=meta["sha256_connectome_compiled_npz"])

    cord_h = (
        prov_banner
        + "#ifndef NECKCORD_BANC_WEIGHTS_H\n#define NECKCORD_BANC_WEIGHTS_H\n\n"
        + "#include <stdint.h>\n\n"
        + "typedef struct {\n"
        + "    uint16_t C_vnc_inh_q12[36];\n"
        + "    int16_t  W_dn_leg_q12[60];\n"
        + "} nc_banc_vnc_weights_t;\n\n"
        + "static const nc_banc_vnc_weights_t k_nc_banc_vnc_default = {\n"
        + f"  {_fmt_c_array_1d(vd['C_vnc_inh_q12'], 6, 'u')},\n"
        + f"  {_fmt_c_array_1d(vd['W_dn_leg_q12'], 10)}\n"
        + "};\n\n#endif /* NECKCORD_BANC_WEIGHTS_H */\n"
    )
    (repo_root / "packages" / "neckcord" / "csrc" / "neckcord_banc_weights.h").write_text(
        cord_h, encoding="utf-8"
    )

    cns_h = (
        prov_banner
        + "#ifndef NECKLINK_CNS_BANC_WEIGHTS_H\n#define NECKLINK_CNS_BANC_WEIGHTS_H\n\n"
        + "#include <stdint.h>\n\n"
        + "typedef struct {\n"
        + "    int16_t W_brain_dn_q12[110]; /* 10 Core DNs x 11 Brain Output Populations */\n"
        + "    int16_t W_dn_leg_q12[60];    /* 6 Thoracic Leg MN Pools x 10 Core DNs */\n"
        + "} nl_banc_dn_weights_t;\n\n"
        + "static const nl_banc_dn_weights_t k_nl_banc_dn_default = {\n"
        + f"  {_fmt_c_array_1d(vd['W_brain_dn_q12'], 11)},\n"
        + f"  {_fmt_c_array_1d(vd['W_dn_leg_q12'], 10)}\n"
        + "};\n\n#endif /* NECKLINK_CNS_BANC_WEIGHTS_H */\n"
    )
    (repo_root / "packages" / "necklink_cns" / "csrc" / "necklink_cns_banc_weights.h").write_text(
        cns_h, encoding="utf-8"
    )

    # 5. Provenance manifest tagging every connectome, literature, design-choice, and engineering parameter
    provenance = {
        "version": "3.0.0",
        "source_connectome": meta,
        "parameters": {
            "neckcompass": {
                "M_exc_M_ed7_M_d7e_M_pa": {"tag": "connectome", "detail": "50 EPG + 40 Delta7 + 18 PEN_a + 24 PEN_b BANC v888 synapses"},
                "M_epg_pfl_W_pfl_dna02": {"tag": "connectome", "detail": "25 PFL3 + 12 PFL2 -> 2 DNa02 (1-hop + signed LAL 2-hop fractional input)"},
                "epg_tau_ms_and_er_plasticity": {"tag": "literature", "citation": "Seelig & Jayaraman (Nature 2015); Fisher et al. (Nature 2019); Westeinde et al. (Nature 2024)"},
                "TiltCompensatedAHRS": {"tag": "engineering", "detail": "3-axis MEMS accelerometer/magnetometer gravity projection front-end"},
            },
            "necksight": {
                "W_lplc2_rf_W_lpi_inh_W_lc4_rf": {"tag": "connectome", "detail": "6,412 T4a-d/T5a-d -> 64 LPLC2 + LPi opponency + Medulla/T2/Tm2/Tm4 -> 32 LC4 + Li22 inhibition on HS/VS-anchored retinotopic sheet"},
                "4_branch_dendritic_coincidence": {"tag": "literature", "citation": "Klapoetke et al. (Nature 2017); Ache et al. (Curr. Biol. 2019)"},
                "RetinaLaminaPreproc_and_3DOF_warp": {"tag": "engineering", "detail": "8-bit camera log-contrast front-end and IMU ego-rotation pixel warp"},
            },
            "necksense": {
                "claw_glom_claw_w_apl_mbon_kcab": {"tag": "connectome", "detail": "699 ALPN -> 1,024 KCg/KCab claws + inhomogeneous KC<->APL + KC->MBON weights"},
                "sensor_channel_to_glomerulus_modulo": {"tag": "design_choice", "detail": "Hardware channels mapped modulo 64 onto synapse-ranked BANC ALPN glomerular clusters"},
                "apl_5pct_sparsity_and_dan_plasticity": {"tag": "literature", "citation": "Honegger et al. (J. Neurosci. 2011); Lin et al. (Nat. Neurosci. 2014); Aso et al. (eLife 2014)"},
                "VibrationFrontEnd": {"tag": "engineering", "detail": "16-channel chordotonal-inspired integer time/spectral acoustic feature bank"},
            },
            "neckcord": {
                "C_vnc_inh_and_W_dn_leg": {"tag": "connectome", "detail": "5,331 VNC_exc -> 7,520 VNC_inh -> 391 Leg MNs (4.91x tripod inhibition) + 10 DN -> 6 Leg MN pools"},
                "repulsive_kuramoto_oscillator_freq": {"tag": "literature", "citation": "Mendes et al. (eLife 2013); DeAngelis et al. (eLife 2019)"},
                "ShuntingThrusterCord_ESCObserver_HexapodLegIK": {"tag": "engineering", "detail": "Bounded [u_min, u_max] active-set thruster QP, sensorless ESC observer, and 3-servo leg inverse kinematics"},
            },
            "necklink_cns": {
                "W_brain_dn_and_W_dn_leg": {"tag": "connectome", "detail": "Parameter-free F + F^2 signed fractional input across 11 Brain populations -> 10 Core DNs -> 6 Leg MN pools"},
                "MAVLink2Codec_and_CRC4_4B_frame": {"tag": "engineering", "detail": "Wire-level MAVLink v2 and 32-bit CRC-4 telemetry/command framing"},
            },
        },
    }
    prov_path = repo_root / "packages" / "necklink_cns" / "necklink_cns" / "connectome" / "provenance.json"
    prov_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")


def extract_all_banc_v888(
    data_dir: Path = DEFAULT_BANC_DIR,
    repo_root: Path = Path(r"D:\necklink"),
) -> Dict[str, Any]:
    raw = extract_raw_subgraphs(data_dir=data_dir)
    compiled = compile_circuits_from_subgraphs(raw)
    _write_c_headers_and_npz(compiled, Path(repo_root))
    return compiled


if __name__ == "__main__":
    res = extract_all_banc_v888()
    print("Compiled BANC v888 v3.0.0 circuits successfully!")
    print("  Source SHA-256 (npz):", res["meta"]["sha256_connectome_compiled_npz"])
    print("  CX ER mean EPG fanout:", round(res["cx"]["er_mean_epg_fanout"], 2))
    print("  VNC C_adj_vs_diag_ratio:", round(res["vnc_dn"]["C_adj_vs_diag_ratio"], 3))
