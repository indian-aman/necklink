"""
Zero-Float, Zero-Malloc ANSI C99 Header Generator for NeckLink (`necklink_*_int8.h`).

Emits a single self-contained C99 header implementing the Compact Dual-Range Place-Rate
+ ISI Tonic-Phasic NeckLink protocol (`encode_q8` and `decode_q8`) with 0 floating-point
instructions (`gcc -mgeneral-regs-only` clean) and 0-LSB / 0-byte cross-language wire
parity with Python `NeckLinkEncoder` and `NeckLinkDecoder`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

import numpy as np

from .codec import NeckLinkConfig
from .connectome_priors import DROSOPHILA_NECK_PRIORS, build_epistatic_cryptic_matrix_q8


def _fmt_i8_array_2d(arr: np.ndarray) -> str:
    rows = []
    for r in range(arr.shape[0]):
        vals = ", ".join(str(int(v)) for v in arr[r])
        rows.append(f"    {{ {vals} }}")
    return "{\n" + ",\n".join(rows) + "\n}"


def _fmt_i32_array_1d(arr: np.ndarray) -> str:
    vals = ", ".join(str(int(v)) for v in arr)
    return "{ " + vals + " }"


def export_necklink_c99_header(
    cfg: NeckLinkConfig,
    out_path: Path,
    prefix: str = "necklink",
    obs_mean: np.ndarray | None = None,
    obs_std: np.ndarray | None = None,
) -> Tuple[Path, int]:
    """
    Exports a standalone zero-float ANSI C99 wire-codec header (`out_path`).
    Returns `(written_path, max_packet_bytes)`.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = cfg.n_channels
    sk_dim = cfg.sketch_dim

    if obs_mean is None:
        mean_q8 = np.zeros(n, dtype=np.int32)
    else:
        mean_q8 = np.round(np.asarray(obs_mean, dtype=np.float64) * 256.0).astype(np.int32)

    if obs_std is None:
        inv_std_q8 = np.full(n, 256, dtype=np.int32)
        std_q8 = np.full(n, 256, dtype=np.int32)
    else:
        std_safe = np.maximum(np.asarray(obs_std, dtype=np.float64), 1e-6)
        inv_std_q8 = np.clip(np.round(256.0 / std_safe), 1, 32767).astype(np.int32)
        std_q8 = np.clip(np.round(std_safe * 256.0), 1, 32767).astype(np.int32)

    phi_q7 = build_epistatic_cryptic_matrix_q8(n, sk_dim, seed=cfg.seed)
    col_norm_sq = max(1, int(np.sum(phi_q7[:, 0].astype(np.int32) ** 2)))
    max_pkt_bytes = 2 + 2 * cfg.max_events_per_pkt + 1 + 2 + sk_dim + 2
    guard = f"{prefix.upper()}_INT8_H"
    up = prefix.upper()
    shunt_floor = int(DROSOPHILA_NECK_PRIORS["tonic_chloride_shunt_floor_q8"])

    en_mirror = 1 if cfg.enable_mirror else 0
    en_echo = 1 if cfg.enable_echo else 0
    en_gate = 1 if cfg.enable_brake_gate else 0

    header_code = f"""/*
 * Auto-generated NeckLink Zero-Float Bare-Metal Wire Codec Header ({out_path.name})
 * Standard: ANSI C99 (-std=c99 -mgeneral-regs-only, 0 FPU ops, 0 heap allocations)
 * License: MIT
 */
#ifndef {guard}
#define {guard}

#include <stdint.h>

#define {up}_N_CHANNELS        {n}
#define {up}_SKETCH_DIM        {sk_dim}
#define {up}_DEADBAND_Q8       {cfg.deadband_q8}
#define {up}_QUANT_STEP_Q8     {cfg.quant_step_q8}
#define {up}_ECHO_SALIENCE_Q8  {cfg.echo_salience_q8}
#define {up}_MAX_EVENTS        {cfg.max_events_per_pkt}
#define {up}_VEL_GAIN_Q8       {cfg.vel_gain_q8}
#define {up}_BETA_Q8           {cfg.beta_q8}
#define {up}_GAMMA_Q8          {cfg.gamma_q8}
#define {up}_HEARTBEAT_TICKS   {cfg.heartbeat_ticks}
#define {up}_SHUNT_FLOOR_Q8    {shunt_floor}
#define {up}_COL_NORM_SQ       {col_norm_sq}
#define {up}_MAX_PKT_BYTES     {max_pkt_bytes}
#define {up}_ENABLE_MIRROR     {en_mirror}
#define {up}_ENABLE_ECHO       {en_echo}
#define {up}_ENABLE_GATE       {en_gate}

static const int8_t {prefix}_phi_q7[{up}_SKETCH_DIM][{up}_N_CHANNELS] = {_fmt_i8_array_2d(phi_q7)};
static const int32_t {prefix}_mean_q8[{up}_N_CHANNELS] = {_fmt_i32_array_1d(mean_q8)};
static const int32_t {prefix}_inv_std_q8[{up}_N_CHANNELS] = {_fmt_i32_array_1d(inv_std_q8)};
static const int32_t {prefix}_std_q8[{up}_N_CHANNELS] = {_fmt_i32_array_1d(std_q8)};

typedef struct {{
    int16_t m[{up}_N_CHANNELS];
    int16_t b[{up}_N_CHANNELS];
    int16_t v[{up}_N_CHANNELS];
    uint8_t age[{up}_N_CHANNELS];
    uint8_t seq;
    uint16_t ticks_since_tx;
    uint8_t h1_valid;
    uint8_t h1_coarse;
    uint8_t h1_idx;
    int8_t h1_q;
    uint8_t h1_dt_prev;
    uint8_t h1_has_sketch;
    int8_t h1_sketch[{up}_SKETCH_DIM];
    uint8_t h2_valid;
    uint8_t h2_coarse;
    uint8_t h2_idx;
    int8_t h2_q;
}} {prefix}_tx_state_t;

typedef struct {{
    int16_t m[{up}_N_CHANNELS];
    int16_t b[{up}_N_CHANNELS];
    int16_t v[{up}_N_CHANNELS];
    uint8_t age[{up}_N_CHANNELS];
    uint8_t last_seq;
    uint8_t has_synced;
    uint16_t ticks_since_pkt;
    int16_t arrive_ema_q8;
    int16_t brake_gate_q8;
    uint32_t recovered_echo_packets;
    uint32_t detected_lost_packets;
}} {prefix}_rx_state_t;

static inline int32_t {prefix}_abs_i32(int32_t x) {{
    return (x < 0) ? -x : x;
}}

static inline int32_t {prefix}_clip_i32(int32_t x, int32_t lo, int32_t hi) {{
    if (x < lo) return lo;
    if (x > hi) return hi;
    return x;
}}

static inline void {prefix}_quant_dual_range(int32_t zq, int *out_coarse, int8_t *out_q, int32_t *out_zquant) {{
    int32_t max_fine = 127 * {up}_QUANT_STEP_Q8;
    int32_t step;
    if ({prefix}_abs_i32(zq) <= max_fine) {{
        step = {up}_QUANT_STEP_Q8;
        *out_coarse = 0;
    }} else {{
        step = {up}_QUANT_STEP_Q8 * 4;
        *out_coarse = 1;
    }}
    int32_t half = step / 2;
    int32_t q = (zq >= 0) ? ((zq + half) / step) : -(((-zq) + half) / step);
    q = {prefix}_clip_i32(q, -127, 127);
    *out_q = (int8_t)q;
    *out_zquant = q * step;
}}

static inline int32_t {prefix}_dequant_dual_range(int is_coarse, int8_t q) {{
    int32_t step = is_coarse ? ({up}_QUANT_STEP_Q8 * 4) : {up}_QUANT_STEP_Q8;
    return (int32_t)q * step;
}}

static inline void {prefix}_tx_init({prefix}_tx_state_t *st) {{
    int i;
    for (i = 0; i < {up}_N_CHANNELS; ++i) {{
        st->m[i] = 0;
        st->b[i] = 0;
        st->v[i] = 0;
        st->age[i] = 32u;
    }}
    st->seq = 0;
    st->ticks_since_tx = 0;
    st->h1_valid = 0;
    st->h1_coarse = 0;
    st->h1_idx = 0;
    st->h1_q = 0;
    st->h1_dt_prev = 1;
    st->h1_has_sketch = 0;
    for (i = 0; i < {up}_SKETCH_DIM; ++i) {{
        st->h1_sketch[i] = 0;
    }}
    st->h2_valid = 0;
    st->h2_coarse = 0;
    st->h2_idx = 0;
    st->h2_q = 0;
}}

static inline void {prefix}_rx_init({prefix}_rx_state_t *st) {{
    int i;
    for (i = 0; i < {up}_N_CHANNELS; ++i) {{
        st->m[i] = 0;
        st->b[i] = 0;
        st->v[i] = 0;
        st->age[i] = 32u;
    }}
    st->last_seq = 0;
    st->has_synced = 0;
    st->ticks_since_pkt = 0;
    st->arrive_ema_q8 = 256;
    st->brake_gate_q8 = 256;
    st->recovered_echo_packets = 0;
    st->detected_lost_packets = 0;
}}

static inline void {prefix}_step_tonic_phasic(int16_t *m, int16_t *b, int16_t *v, uint8_t *age) {{
    int i;
    for (i = 0; i < {up}_N_CHANNELS; ++i) {{
        uint8_t a = age[i];
        age[i] = (a < 32u) ? (uint8_t)(a + 1u) : 32u;
        int32_t mi = (int32_t)m[i];
        int32_t bi = (int32_t)b[i];
        b[i] = (int16_t){prefix}_clip_i32(bi + (mi - bi) / 64, -32768, 32767);
        if ({up}_ENABLE_MIRROR) {{
            int32_t vi = (int32_t)v[i];
            if (vi != 0) {{
                m[i] = (int16_t){prefix}_clip_i32(mi + ({up}_BETA_Q8 * vi) / 256, -32768, 32767);
                v[i] = (int16_t){prefix}_clip_i32(({up}_GAMMA_Q8 * vi) / 256, -32768, 32767);
            }}
        }} else {{
            v[i] = 0;
        }}
    }}
}}

static inline int {prefix}_encode_q8(
    {prefix}_tx_state_t *st,
    const int32_t *z_q8,
    uint8_t *out_pkt
) {{
    int i, j, r_idx, c_idx;
    int32_t cand_ari[{up}_N_CHANNELS];
    int32_t cand_zq[{up}_N_CHANNELS];
    uint8_t cand_idx[{up}_N_CHANNELS];
    int n_cand = 0;

    {prefix}_step_tonic_phasic(st->m, st->b, st->v, st->age);
    st->ticks_since_tx = (uint16_t)(st->ticks_since_tx + 1u);

    for (i = 0; i < {up}_N_CHANNELS; ++i) {{
        int32_t zq = z_q8[i];
        int32_t ari = {prefix}_abs_i32(zq - (int32_t)st->m[i]);
        if (ari >= {up}_DEADBAND_Q8) {{
            j = n_cand;
            while (j > 0 && cand_ari[j - 1] < ari) {{
                cand_ari[j] = cand_ari[j - 1];
                cand_zq[j] = cand_zq[j - 1];
                cand_idx[j] = cand_idx[j - 1];
                --j;
            }}
            cand_ari[j] = ari;
            cand_zq[j] = zq;
            cand_idx[j] = (uint8_t)i;
            ++n_cand;
        }}
    }}

    if (n_cand == 0 && st->ticks_since_tx < {up}_HEARTBEAT_TICKS) {{
        return 0;
    }}

    int limit = (n_cand < {up}_MAX_EVENTS) ? n_cand : {up}_MAX_EVENTS;
    uint8_t ev_coarse[{up}_MAX_EVENTS];
    uint8_t ev_idx[{up}_MAX_EVENTS];
    int8_t ev_q[{up}_MAX_EVENTS];
    int32_t ev_zquant[{up}_MAX_EVENTS];
    int32_t ev_ari[{up}_MAX_EVENTS];
    int n_ev = 0;

    for (i = 0; i < limit; ++i) {{
        int is_c = 0;
        int8_t q = 0;
        int32_t zquant = 0;
        uint8_t idx = cand_idx[i];
        {prefix}_quant_dual_range(cand_zq[i], &is_c, &q, &zquant);
        if (zquant != (int32_t)st->m[idx]) {{
            ev_coarse[n_ev] = (uint8_t)is_c;
            ev_idx[n_ev] = idx;
            ev_q[n_ev] = q;
            ev_zquant[n_ev] = zquant;
            ev_ari[n_ev] = cand_ari[i];
            ++n_ev;
        }}
    }}

    if (n_ev == 0 && st->ticks_since_tx < {up}_HEARTBEAT_TICKS) {{
        return 0;
    }}

    int32_t u_innov_vec[{up}_N_CHANNELS];
    for (i = 0; i < {up}_N_CHANNELS; ++i) u_innov_vec[i] = 0;

    for (i = 0; i < n_ev; ++i) {{
        uint8_t idx = ev_idx[i];
        int32_t zquant = ev_zquant[i];
        int32_t u_innov = zquant - (int32_t)st->m[idx];
        u_innov_vec[idx] = u_innov;
        st->m[idx] = (int16_t){prefix}_clip_i32(zquant, -32768, 32767);
        if ({up}_ENABLE_MIRROR) {{
            int32_t isi = (st->age[idx] >= 1u) ? (int32_t)st->age[idx] : 1;
            int32_t dv = ({up}_VEL_GAIN_Q8 * u_innov) / (256 * isi);
            st->v[idx] = (int16_t){prefix}_clip_i32((int32_t)st->v[idx] + dv, -32768, 32767);
        }}
        st->age[idx] = 0u;
    }}

    st->seq = (uint8_t)((st->seq + 1u) & 0xFFu);
    uint8_t dt_curr = (st->ticks_since_tx > 31u) ? 31u : (uint8_t)st->ticks_since_tx;
    int include_echo = ({up}_ENABLE_ECHO && st->h1_valid && st->ticks_since_tx <= 31u) ? 1 : 0;
    int inc_h2 = (include_echo && st->h2_valid && st->h1_dt_prev <= 15u) ? 1 : 0;

    int pos = 0;
    out_pkt[pos++] = st->seq;
    out_pkt[pos++] = (uint8_t)((include_echo ? 0x80u : 0x00u) | ((uint8_t)n_ev & 0x3Fu));

    for (i = 0; i < n_ev; ++i) {{
        out_pkt[pos++] = (uint8_t)((ev_coarse[i] ? 0x80u : 0x00u) | (ev_idx[i] & 0x7Fu));
        out_pkt[pos++] = (uint8_t)ev_q[i];
    }}

    if (include_echo) {{
        uint8_t eh1 = (uint8_t)(
            (st->h1_has_sketch ? 0x80u : 0x00u) |
            (inc_h2 ? 0x40u : 0x00u) |
            (dt_curr & 0x1Fu)
        );
        out_pkt[pos++] = eh1;
        out_pkt[pos++] = (uint8_t)((st->h1_coarse ? 0x80u : 0x00u) | (st->h1_idx & 0x7Fu));
        out_pkt[pos++] = (uint8_t)st->h1_q;
        if (st->h1_has_sketch) {{
            for (i = 0; i < {up}_SKETCH_DIM; ++i) {{
                out_pkt[pos++] = (uint8_t)st->h1_sketch[i];
            }}
        }}
        if (inc_h2) {{
            out_pkt[pos++] = (uint8_t)((st->h2_coarse ? 0x80u : 0x00u) | (st->h2_idx & 0x7Fu));
            out_pkt[pos++] = (uint8_t)st->h2_q;
        }}
    }}

    int n_sal = 0;
    int first_sal = -1;
    for (i = 0; i < n_ev; ++i) {{
        if (ev_ari[i] >= {up}_ECHO_SALIENCE_Q8) {{
            if (first_sal < 0) first_sal = i;
            ++n_sal;
        }}
    }}

    if (n_sal > 0) {{
        if (st->h1_valid) {{
            st->h2_valid = 1;
            st->h2_coarse = st->h1_coarse;
            st->h2_idx = st->h1_idx;
            st->h2_q = st->h1_q;
        }} else {{
            st->h2_valid = 0;
        }}

        st->h1_valid = 1;
        st->h1_coarse = ev_coarse[first_sal];
        st->h1_idx = ev_idx[first_sal];
        st->h1_q = ev_q[first_sal];
        st->h1_dt_prev = dt_curr;

        if (n_sal >= 5) {{
            st->h1_has_sketch = 1;
            u_innov_vec[st->h1_idx] = 0;
            for (r_idx = 0; r_idx < {up}_SKETCH_DIM; ++r_idx) {{
                int32_t acc = 0;
                for (c_idx = 0; c_idx < {up}_N_CHANNELS; ++c_idx) {{
                    acc += (int32_t){prefix}_phi_q7[r_idx][c_idx] * u_innov_vec[c_idx];
                }}
                int32_t sk_val = acc / (128 * {up}_QUANT_STEP_Q8);
                st->h1_sketch[r_idx] = (int8_t){prefix}_clip_i32(sk_val, -127, 127);
            }}
        }} else {{
            st->h1_has_sketch = 0;
        }}
    }} else {{
        st->h1_valid = 0;
        st->h2_valid = 0;
    }}

    st->ticks_since_tx = 0;
    return pos;
}}

static inline void {prefix}_decode_q8(
    {prefix}_rx_state_t *st,
    const uint8_t *pkt,
    int pkt_len,
    int32_t *out_z_q8
) {{
    int i, s, r_idx, c_idx;

    {prefix}_step_tonic_phasic(st->m, st->b, st->v, st->age);
    if (st->ticks_since_pkt < 1000u) {{
        st->ticks_since_pkt = (uint16_t)(st->ticks_since_pkt + 1u);
    }}

    if (pkt == 0 || pkt_len < 2) {{
        if ({up}_ENABLE_GATE && st->arrive_ema_q8 < 216 && st->ticks_since_pkt >= 2u) {{
            int32_t shunt = 256 - (int32_t)st->arrive_ema_q8;
            for (i = 0; i < {up}_N_CHANNELS; ++i) {{
                int32_t dev = (int32_t)st->m[i] - (int32_t)st->b[i];
                if ({prefix}_abs_i32(dev) > 224) {{
                    st->m[i] = (int16_t){prefix}_clip_i32((int32_t)st->m[i] - (dev * shunt) / 512, -32768, 32767);
                }}
            }}
        }}
        for (i = 0; i < {up}_N_CHANNELS; ++i) out_z_q8[i] = (int32_t)st->m[i];
        return;
    }}

    st->ticks_since_pkt = 0u;
    uint8_t seq = pkt[0];
    uint8_t flags_count = pkt[1];
    int has_echo = (flags_count & 0x80u) ? 1 : 0;
    int n_ev = (int)(flags_count & 0x3Fu);

    int pos = 2;
    uint8_t curr_idx[{up}_MAX_EVENTS];
    int32_t curr_zq[{up}_MAX_EVENTS];
    uint8_t active_curr[{up}_N_CHANNELS];
    for (i = 0; i < {up}_N_CHANNELS; ++i) active_curr[i] = 0;
    int actual_ev = 0;

    for (i = 0; i < n_ev; ++i) {{
        if (pos + 2 > pkt_len) break;
        uint8_t b0 = pkt[pos];
        int8_t qi = (int8_t)pkt[pos + 1];
        pos += 2;
        int is_c = (b0 & 0x80u) ? 1 : 0;
        uint8_t idx = (uint8_t)(b0 & 0x7Fu);
        if (idx < {up}_N_CHANNELS && actual_ev < {up}_MAX_EVENTS) {{
            curr_idx[actual_ev] = idx;
            curr_zq[actual_ev] = {prefix}_dequant_dual_range(is_c, qi);
            active_curr[idx] = 1;
            ++actual_ev;
        }}
    }}

    uint8_t gap;
    if (!st->has_synced) {{
        gap = (seq >= 1u) ? seq : 1u;
        st->has_synced = 1;
    }} else {{
        gap = (uint8_t)((seq - st->last_seq) & 0xFFu);
        if (gap == 0u) gap = 1u;
    }}
    st->last_seq = seq;

    if (gap > 1u) {{
        st->detected_lost_packets += (uint32_t)(gap - 1u);
    }}

    int32_t g_cap = (gap > 8u) ? 8 : (int32_t)gap;
    int32_t inst_arrive_q8 = 256 / g_cap;
    if (inst_arrive_q8 < (int32_t)st->arrive_ema_q8) {{
        st->arrive_ema_q8 = (int16_t)((96 * (int32_t)st->arrive_ema_q8 + 160 * inst_arrive_q8) / 256);
    }} else {{
        st->arrive_ema_q8 = (int16_t)((216 * (int32_t)st->arrive_ema_q8 + 40 * inst_arrive_q8) / 256);
    }}

    if ({up}_ENABLE_GATE) {{
        st->brake_gate_q8 = (int16_t){prefix}_clip_i32((int32_t)st->arrive_ema_q8, {up}_SHUNT_FLOOR_Q8, 256);
    }} else {{
        st->brake_gate_q8 = 256;
    }}

    int32_t eff_vg = ({up}_VEL_GAIN_Q8 * (int32_t)st->brake_gate_q8) / 256;

    if (has_echo && pos < pkt_len) {{
        uint8_t eh1 = pkt[pos++];
        int h1_has_sketch = (eh1 & 0x80u) ? 1 : 0;
        int inc_h2 = (eh1 & 0x40u) ? 1 : 0;
        int dt1 = (int)(eh1 & 0x1Fu);

        int h1_valid_wire = 0;
        uint8_t h1_idx = 0;
        int32_t h1_zq = 0;
        if (pos + 2 <= pkt_len) {{
            uint8_t eb0 = pkt[pos];
            int8_t eq = (int8_t)pkt[pos + 1];
            pos += 2;
            int h1_c = (eb0 & 0x80u) ? 1 : 0;
            h1_idx = (uint8_t)(eb0 & 0x7Fu);
            if (h1_idx < {up}_N_CHANNELS) {{
                h1_zq = {prefix}_dequant_dual_range(h1_c, eq);
                h1_valid_wire = 1;
            }}
        }}

        int32_t sk_vals[{up}_SKETCH_DIM];
        for (i = 0; i < {up}_SKETCH_DIM; ++i) sk_vals[i] = 0;
        if (h1_has_sketch && pos + {up}_SKETCH_DIM <= pkt_len) {{
            for (i = 0; i < {up}_SKETCH_DIM; ++i) {{
                sk_vals[i] = (int32_t)((int8_t)pkt[pos + i]);
            }}
            pos += {up}_SKETCH_DIM;
        }}

        int h2_valid_wire = 0;
        uint8_t h2_idx = 0;
        int32_t h2_zq = 0;
        if (inc_h2 && pos + 2 <= pkt_len) {{
            uint8_t eb2 = pkt[pos];
            int8_t eq2 = (int8_t)pkt[pos + 1];
            pos += 2;
            int h2_c = (eb2 & 0x80u) ? 1 : 0;
            h2_idx = (uint8_t)(eb2 & 0x7Fu);
            if (h2_idx < {up}_N_CHANNELS) {{
                h2_zq = {prefix}_dequant_dual_range(h2_c, eq2);
                h2_valid_wire = 1;
            }}
        }}

        if ({up}_ENABLE_ECHO && gap >= 2u) {{
            if (gap >= 3u && h2_valid_wire && !active_curr[h2_idx]) {{
                st->m[h2_idx] = (int16_t){prefix}_clip_i32(h2_zq, -32768, 32767);
                st->recovered_echo_packets += 1u;
            }}

            if (h1_valid_wire && !active_curr[h1_idx]) {{
                int32_t u1 = h1_zq - (int32_t)st->m[h1_idx];
                st->m[h1_idx] = (int16_t){prefix}_clip_i32(h1_zq, -32768, 32767);
                if ({up}_ENABLE_MIRROR) {{
                    int32_t isi1 = (st->age[h1_idx] >= 1u) ? (int32_t)st->age[h1_idx] : 1;
                    int32_t dv1 = (eff_vg * u1) / (256 * isi1);
                    int32_t v1 = {prefix}_clip_i32((int32_t)st->v[h1_idx] + dv1, -32768, 32767);
                    int32_t m_now = (int32_t)st->m[h1_idx];
                    for (s = 0; s < dt1; ++s) {{
                        if (v1 != 0) {{
                            m_now = {prefix}_clip_i32(m_now + ({up}_BETA_Q8 * v1) / 256, -32768, 32767);
                            v1 = {prefix}_clip_i32(({up}_GAMMA_Q8 * v1) / 256, -32768, 32767);
                        }}
                    }}
                    st->m[h1_idx] = (int16_t)m_now;
                    st->v[h1_idx] = (int16_t)v1;
                }}
                uint8_t a1 = (dt1 >= 1) ? (uint8_t)dt1 : 1u;
                st->age[h1_idx] = (a1 > 32u) ? 32u : a1;
            }}

            if (h1_has_sketch) {{
                for (c_idx = 0; c_idx < {up}_N_CHANNELS; ++c_idx) {{
                    if (active_curr[c_idx] || (h1_valid_wire && c_idx == (int)h1_idx)) continue;
                    int32_t acc = 0;
                    for (r_idx = 0; r_idx < {up}_SKETCH_DIM; ++r_idx) {{
                        acc += (int32_t){prefix}_phi_q7[r_idx][c_idx] * sk_vals[r_idx];
                    }}
                    int32_t rec_val = (acc * 128 * {up}_QUANT_STEP_Q8) / {up}_COL_NORM_SQ;
                    if ({prefix}_abs_i32(rec_val) > {up}_DEADBAND_Q8) {{
                        st->m[c_idx] = (int16_t){prefix}_clip_i32(
                            (int32_t)st->m[c_idx] + (rec_val * 144) / 256, -32768, 32767
                        );
                    }}
                }}
            }}

            st->recovered_echo_packets += 1u;
        }}
    }}

    for (i = 0; i < actual_ev; ++i) {{
        uint8_t idx = curr_idx[i];
        int32_t zq = curr_zq[i];
        int32_t u_innov = zq - (int32_t)st->m[idx];
        st->m[idx] = (int16_t){prefix}_clip_i32(zq, -32768, 32767);
        if ({up}_ENABLE_MIRROR) {{
            int32_t isi = (st->age[idx] >= 1u) ? (int32_t)st->age[idx] : 1;
            int32_t dv = (eff_vg * u_innov) / (256 * isi);
            st->v[idx] = (int16_t){prefix}_clip_i32((int32_t)st->v[idx] + dv, -32768, 32767);
        }}
        st->age[idx] = 0u;
    }}

    for (i = 0; i < {up}_N_CHANNELS; ++i) {{
        out_z_q8[i] = (int32_t)st->m[i];
    }}
}}

#endif /* {guard} */
"""
    out_path.write_text(header_code, encoding="utf-8")
    return out_path, max_pkt_bytes
