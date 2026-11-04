/*
 * Auto-generated NeckLink Zero-Float Bare-Metal Wire Codec Header (necklink_splitai32_int8.h)
 * Standard: ANSI C99 (-std=c99 -mgeneral-regs-only, 0 FPU ops, 0 heap allocations)
 * License: MIT
 */
#ifndef NECKLINK_SPLITAI32_INT8_H
#define NECKLINK_SPLITAI32_INT8_H

#include <stdint.h>

#define NECKLINK_SPLITAI32_N_CHANNELS        32
#define NECKLINK_SPLITAI32_SKETCH_DIM        4
#define NECKLINK_SPLITAI32_DEADBAND_Q8       24
#define NECKLINK_SPLITAI32_QUANT_STEP_Q8     8
#define NECKLINK_SPLITAI32_ECHO_SALIENCE_Q8  64
#define NECKLINK_SPLITAI32_MAX_EVENTS        16
#define NECKLINK_SPLITAI32_VEL_GAIN_Q8       176
#define NECKLINK_SPLITAI32_BETA_Q8           168
#define NECKLINK_SPLITAI32_GAMMA_Q8          128
#define NECKLINK_SPLITAI32_HEARTBEAT_TICKS   64
#define NECKLINK_SPLITAI32_SHUNT_FLOOR_Q8    24
#define NECKLINK_SPLITAI32_COL_NORM_SQ       16384
#define NECKLINK_SPLITAI32_MAX_PKT_BYTES     43
#define NECKLINK_SPLITAI32_ENABLE_MIRROR     1
#define NECKLINK_SPLITAI32_ENABLE_ECHO       1
#define NECKLINK_SPLITAI32_ENABLE_GATE       1

static const int8_t necklink_splitai32_phi_q7[NECKLINK_SPLITAI32_SKETCH_DIM][NECKLINK_SPLITAI32_N_CHANNELS] = {
    { 64, -64, -64, -64, -64, 64, -64, -64, 64, 64, 64, 64, 64, -64, 64, 64, -64, 64, -64, -64, -64, -64, -64, 64, 64, -64, -64, 64, 64, 64, -64, 64 },
    { -64, -64, 64, -64, -64, -64, 64, 64, -64, 64, -64, 64, 64, -64, -64, 64, 64, 64, 64, 64, -64, 64, -64, 64, -64, -64, 64, 64, -64, 64, 64, 64 },
    { -64, 64, -64, -64, -64, 64, 64, 64, 64, 64, -64, 64, -64, 64, 64, 64, -64, -64, 64, 64, -64, -64, 64, 64, -64, 64, -64, -64, 64, -64, 64, 64 },
    { 64, 64, 64, -64, -64, -64, -64, -64, -64, 64, 64, 64, -64, 64, -64, 64, 64, -64, -64, -64, -64, 64, 64, 64, 64, 64, 64, -64, -64, -64, -64, 64 }
};
static const int32_t necklink_splitai32_mean_q8[NECKLINK_SPLITAI32_N_CHANNELS] = { 70, 70, 45, 19, -128, -175, 243, 69, -64, -279, -210, 71, 69, -81, -23, -54, -73, 179, 81, 3, -51, -207, -119, 135, 33, 355, 125, 42, -56, -106, 172, -237 };
static const int32_t necklink_splitai32_inv_std_q8[NECKLINK_SPLITAI32_N_CHANNELS] = { 226, 350, 189, 247, 262, 224, 230, 281, 213, 167, 336, 260, 348, 281, 428, 462, 227, 274, 257, 297, 329, 244, 278, 304, 435, 229, 274, 262, 378, 286, 250, 259 };
static const int32_t necklink_splitai32_std_q8[NECKLINK_SPLITAI32_N_CHANNELS] = { 290, 187, 347, 265, 250, 293, 285, 233, 307, 391, 195, 252, 188, 233, 153, 142, 289, 239, 255, 220, 199, 268, 236, 216, 151, 286, 239, 251, 173, 229, 262, 253 };

typedef struct {
    int16_t m[NECKLINK_SPLITAI32_N_CHANNELS];
    int16_t b[NECKLINK_SPLITAI32_N_CHANNELS];
    int16_t v[NECKLINK_SPLITAI32_N_CHANNELS];
    uint8_t age[NECKLINK_SPLITAI32_N_CHANNELS];
    uint8_t seq;
    uint16_t ticks_since_tx;
    uint8_t h1_valid;
    uint8_t h1_coarse;
    uint8_t h1_idx;
    int8_t h1_q;
    uint8_t h1_dt_prev;
    uint8_t h1_has_sketch;
    int8_t h1_sketch[NECKLINK_SPLITAI32_SKETCH_DIM];
    uint8_t h2_valid;
    uint8_t h2_coarse;
    uint8_t h2_idx;
    int8_t h2_q;
} necklink_splitai32_tx_state_t;

typedef struct {
    int16_t m[NECKLINK_SPLITAI32_N_CHANNELS];
    int16_t b[NECKLINK_SPLITAI32_N_CHANNELS];
    int16_t v[NECKLINK_SPLITAI32_N_CHANNELS];
    uint8_t age[NECKLINK_SPLITAI32_N_CHANNELS];
    uint8_t last_seq;
    uint8_t has_synced;
    uint16_t ticks_since_pkt;
    int16_t arrive_ema_q8;
    int16_t brake_gate_q8;
    uint32_t recovered_echo_packets;
    uint32_t detected_lost_packets;
} necklink_splitai32_rx_state_t;

static inline int32_t necklink_splitai32_abs_i32(int32_t x) {
    return (x < 0) ? -x : x;
}

static inline int32_t necklink_splitai32_clip_i32(int32_t x, int32_t lo, int32_t hi) {
    if (x < lo) return lo;
    if (x > hi) return hi;
    return x;
}

static inline void necklink_splitai32_quant_dual_range(int32_t zq, int *out_coarse, int8_t *out_q, int32_t *out_zquant) {
    int32_t max_fine = 127 * NECKLINK_SPLITAI32_QUANT_STEP_Q8;
    int32_t step;
    if (necklink_splitai32_abs_i32(zq) <= max_fine) {
        step = NECKLINK_SPLITAI32_QUANT_STEP_Q8;
        *out_coarse = 0;
    } else {
        step = NECKLINK_SPLITAI32_QUANT_STEP_Q8 * 4;
        *out_coarse = 1;
    }
    int32_t half = step / 2;
    int32_t q = (zq >= 0) ? ((zq + half) / step) : -(((-zq) + half) / step);
    q = necklink_splitai32_clip_i32(q, -127, 127);
    *out_q = (int8_t)q;
    *out_zquant = q * step;
}

static inline int32_t necklink_splitai32_dequant_dual_range(int is_coarse, int8_t q) {
    int32_t step = is_coarse ? (NECKLINK_SPLITAI32_QUANT_STEP_Q8 * 4) : NECKLINK_SPLITAI32_QUANT_STEP_Q8;
    return (int32_t)q * step;
}

static inline void necklink_splitai32_tx_init(necklink_splitai32_tx_state_t *st) {
    int i;
    for (i = 0; i < NECKLINK_SPLITAI32_N_CHANNELS; ++i) {
        st->m[i] = 0;
        st->b[i] = 0;
        st->v[i] = 0;
        st->age[i] = 32u;
    }
    st->seq = 0;
    st->ticks_since_tx = 0;
    st->h1_valid = 0;
    st->h1_coarse = 0;
    st->h1_idx = 0;
    st->h1_q = 0;
    st->h1_dt_prev = 1;
    st->h1_has_sketch = 0;
    for (i = 0; i < NECKLINK_SPLITAI32_SKETCH_DIM; ++i) {
        st->h1_sketch[i] = 0;
    }
    st->h2_valid = 0;
    st->h2_coarse = 0;
    st->h2_idx = 0;
    st->h2_q = 0;
}

static inline void necklink_splitai32_rx_init(necklink_splitai32_rx_state_t *st) {
    int i;
    for (i = 0; i < NECKLINK_SPLITAI32_N_CHANNELS; ++i) {
        st->m[i] = 0;
        st->b[i] = 0;
        st->v[i] = 0;
        st->age[i] = 32u;
    }
    st->last_seq = 0;
    st->has_synced = 0;
    st->ticks_since_pkt = 0;
    st->arrive_ema_q8 = 256;
    st->brake_gate_q8 = 256;
    st->recovered_echo_packets = 0;
    st->detected_lost_packets = 0;
}

static inline void necklink_splitai32_step_tonic_phasic(int16_t *m, int16_t *b, int16_t *v, uint8_t *age) {
    int i;
    for (i = 0; i < NECKLINK_SPLITAI32_N_CHANNELS; ++i) {
        uint8_t a = age[i];
        age[i] = (a < 32u) ? (uint8_t)(a + 1u) : 32u;
        int32_t mi = (int32_t)m[i];
        int32_t bi = (int32_t)b[i];
        b[i] = (int16_t)necklink_splitai32_clip_i32(bi + (mi - bi) / 64, -32768, 32767);
        if (NECKLINK_SPLITAI32_ENABLE_MIRROR) {
            int32_t vi = (int32_t)v[i];
            if (vi != 0) {
                m[i] = (int16_t)necklink_splitai32_clip_i32(mi + (NECKLINK_SPLITAI32_BETA_Q8 * vi) / 256, -32768, 32767);
                v[i] = (int16_t)necklink_splitai32_clip_i32((NECKLINK_SPLITAI32_GAMMA_Q8 * vi) / 256, -32768, 32767);
            }
        } else {
            v[i] = 0;
        }
    }
}

static inline int necklink_splitai32_encode_q8(
    necklink_splitai32_tx_state_t *st,
    const int32_t *z_q8,
    uint8_t *out_pkt
) {
    int i, j, r_idx, c_idx;
    int32_t cand_ari[NECKLINK_SPLITAI32_N_CHANNELS];
    int32_t cand_zq[NECKLINK_SPLITAI32_N_CHANNELS];
    uint8_t cand_idx[NECKLINK_SPLITAI32_N_CHANNELS];
    int n_cand = 0;

    necklink_splitai32_step_tonic_phasic(st->m, st->b, st->v, st->age);
    st->ticks_since_tx = (uint16_t)(st->ticks_since_tx + 1u);

    for (i = 0; i < NECKLINK_SPLITAI32_N_CHANNELS; ++i) {
        int32_t zq = z_q8[i];
        int32_t ari = necklink_splitai32_abs_i32(zq - (int32_t)st->m[i]);
        if (ari >= NECKLINK_SPLITAI32_DEADBAND_Q8) {
            j = n_cand;
            while (j > 0 && cand_ari[j - 1] < ari) {
                cand_ari[j] = cand_ari[j - 1];
                cand_zq[j] = cand_zq[j - 1];
                cand_idx[j] = cand_idx[j - 1];
                --j;
            }
            cand_ari[j] = ari;
            cand_zq[j] = zq;
            cand_idx[j] = (uint8_t)i;
            ++n_cand;
        }
    }

    if (n_cand == 0 && st->ticks_since_tx < NECKLINK_SPLITAI32_HEARTBEAT_TICKS) {
        return 0;
    }

    int limit = (n_cand < NECKLINK_SPLITAI32_MAX_EVENTS) ? n_cand : NECKLINK_SPLITAI32_MAX_EVENTS;
    uint8_t ev_coarse[NECKLINK_SPLITAI32_MAX_EVENTS];
    uint8_t ev_idx[NECKLINK_SPLITAI32_MAX_EVENTS];
    int8_t ev_q[NECKLINK_SPLITAI32_MAX_EVENTS];
    int32_t ev_zquant[NECKLINK_SPLITAI32_MAX_EVENTS];
    int32_t ev_ari[NECKLINK_SPLITAI32_MAX_EVENTS];
    int n_ev = 0;

    for (i = 0; i < limit; ++i) {
        int is_c = 0;
        int8_t q = 0;
        int32_t zquant = 0;
        uint8_t idx = cand_idx[i];
        necklink_splitai32_quant_dual_range(cand_zq[i], &is_c, &q, &zquant);
        if (zquant != (int32_t)st->m[idx]) {
            ev_coarse[n_ev] = (uint8_t)is_c;
            ev_idx[n_ev] = idx;
            ev_q[n_ev] = q;
            ev_zquant[n_ev] = zquant;
            ev_ari[n_ev] = cand_ari[i];
            ++n_ev;
        }
    }

    if (n_ev == 0 && st->ticks_since_tx < NECKLINK_SPLITAI32_HEARTBEAT_TICKS) {
        return 0;
    }

    int32_t u_innov_vec[NECKLINK_SPLITAI32_N_CHANNELS];
    for (i = 0; i < NECKLINK_SPLITAI32_N_CHANNELS; ++i) u_innov_vec[i] = 0;

    for (i = 0; i < n_ev; ++i) {
        uint8_t idx = ev_idx[i];
        int32_t zquant = ev_zquant[i];
        int32_t u_innov = zquant - (int32_t)st->m[idx];
        u_innov_vec[idx] = u_innov;
        st->m[idx] = (int16_t)necklink_splitai32_clip_i32(zquant, -32768, 32767);
        if (NECKLINK_SPLITAI32_ENABLE_MIRROR) {
            int32_t isi = (st->age[idx] >= 1u) ? (int32_t)st->age[idx] : 1;
            int32_t dv = (NECKLINK_SPLITAI32_VEL_GAIN_Q8 * u_innov) / (256 * isi);
            st->v[idx] = (int16_t)necklink_splitai32_clip_i32((int32_t)st->v[idx] + dv, -32768, 32767);
        }
        st->age[idx] = 0u;
    }

    st->seq = (uint8_t)((st->seq + 1u) & 0xFFu);
    uint8_t dt_curr = (st->ticks_since_tx > 31u) ? 31u : (uint8_t)st->ticks_since_tx;
    int include_echo = (NECKLINK_SPLITAI32_ENABLE_ECHO && st->h1_valid && st->ticks_since_tx <= 31u) ? 1 : 0;
    int inc_h2 = (include_echo && st->h2_valid && st->h1_dt_prev <= 15u) ? 1 : 0;

    int pos = 0;
    out_pkt[pos++] = st->seq;
    out_pkt[pos++] = (uint8_t)((include_echo ? 0x80u : 0x00u) | ((uint8_t)n_ev & 0x3Fu));

    for (i = 0; i < n_ev; ++i) {
        out_pkt[pos++] = (uint8_t)((ev_coarse[i] ? 0x80u : 0x00u) | (ev_idx[i] & 0x7Fu));
        out_pkt[pos++] = (uint8_t)ev_q[i];
    }

    if (include_echo) {
        uint8_t eh1 = (uint8_t)(
            (st->h1_has_sketch ? 0x80u : 0x00u) |
            (inc_h2 ? 0x40u : 0x00u) |
            (dt_curr & 0x1Fu)
        );
        out_pkt[pos++] = eh1;
        out_pkt[pos++] = (uint8_t)((st->h1_coarse ? 0x80u : 0x00u) | (st->h1_idx & 0x7Fu));
        out_pkt[pos++] = (uint8_t)st->h1_q;
        if (st->h1_has_sketch) {
            for (i = 0; i < NECKLINK_SPLITAI32_SKETCH_DIM; ++i) {
                out_pkt[pos++] = (uint8_t)st->h1_sketch[i];
            }
        }
        if (inc_h2) {
            out_pkt[pos++] = (uint8_t)((st->h2_coarse ? 0x80u : 0x00u) | (st->h2_idx & 0x7Fu));
            out_pkt[pos++] = (uint8_t)st->h2_q;
        }
    }

    int n_sal = 0;
    int first_sal = -1;
    for (i = 0; i < n_ev; ++i) {
        if (ev_ari[i] >= NECKLINK_SPLITAI32_ECHO_SALIENCE_Q8) {
            if (first_sal < 0) first_sal = i;
            ++n_sal;
        }
    }

    if (n_sal > 0) {
        if (st->h1_valid) {
            st->h2_valid = 1;
            st->h2_coarse = st->h1_coarse;
            st->h2_idx = st->h1_idx;
            st->h2_q = st->h1_q;
        } else {
            st->h2_valid = 0;
        }

        st->h1_valid = 1;
        st->h1_coarse = ev_coarse[first_sal];
        st->h1_idx = ev_idx[first_sal];
        st->h1_q = ev_q[first_sal];
        st->h1_dt_prev = dt_curr;

        if (n_sal >= 5) {
            st->h1_has_sketch = 1;
            u_innov_vec[st->h1_idx] = 0;
            for (r_idx = 0; r_idx < NECKLINK_SPLITAI32_SKETCH_DIM; ++r_idx) {
                int32_t acc = 0;
                for (c_idx = 0; c_idx < NECKLINK_SPLITAI32_N_CHANNELS; ++c_idx) {
                    acc += (int32_t)necklink_splitai32_phi_q7[r_idx][c_idx] * u_innov_vec[c_idx];
                }
                int32_t sk_val = acc / (128 * NECKLINK_SPLITAI32_QUANT_STEP_Q8);
                st->h1_sketch[r_idx] = (int8_t)necklink_splitai32_clip_i32(sk_val, -127, 127);
            }
        } else {
            st->h1_has_sketch = 0;
        }
    } else {
        st->h1_valid = 0;
        st->h2_valid = 0;
    }

    st->ticks_since_tx = 0;
    return pos;
}

static inline void necklink_splitai32_decode_q8(
    necklink_splitai32_rx_state_t *st,
    const uint8_t *pkt,
    int pkt_len,
    int32_t *out_z_q8
) {
    int i, s, r_idx, c_idx;

    necklink_splitai32_step_tonic_phasic(st->m, st->b, st->v, st->age);
    if (st->ticks_since_pkt < 1000u) {
        st->ticks_since_pkt = (uint16_t)(st->ticks_since_pkt + 1u);
    }

    if (pkt == 0 || pkt_len < 2) {
        if (NECKLINK_SPLITAI32_ENABLE_GATE && st->arrive_ema_q8 < 216 && st->ticks_since_pkt >= 2u) {
            int32_t shunt = 256 - (int32_t)st->arrive_ema_q8;
            for (i = 0; i < NECKLINK_SPLITAI32_N_CHANNELS; ++i) {
                int32_t dev = (int32_t)st->m[i] - (int32_t)st->b[i];
                if (necklink_splitai32_abs_i32(dev) > 224) {
                    st->m[i] = (int16_t)necklink_splitai32_clip_i32((int32_t)st->m[i] - (dev * shunt) / 512, -32768, 32767);
                }
            }
        }
        for (i = 0; i < NECKLINK_SPLITAI32_N_CHANNELS; ++i) out_z_q8[i] = (int32_t)st->m[i];
        return;
    }

    st->ticks_since_pkt = 0u;
    uint8_t seq = pkt[0];
    uint8_t flags_count = pkt[1];
    int has_echo = (flags_count & 0x80u) ? 1 : 0;
    int n_ev = (int)(flags_count & 0x3Fu);

    int pos = 2;
    uint8_t curr_idx[NECKLINK_SPLITAI32_MAX_EVENTS];
    int32_t curr_zq[NECKLINK_SPLITAI32_MAX_EVENTS];
    uint8_t active_curr[NECKLINK_SPLITAI32_N_CHANNELS];
    for (i = 0; i < NECKLINK_SPLITAI32_N_CHANNELS; ++i) active_curr[i] = 0;
    int actual_ev = 0;

    for (i = 0; i < n_ev; ++i) {
        if (pos + 2 > pkt_len) break;
        uint8_t b0 = pkt[pos];
        int8_t qi = (int8_t)pkt[pos + 1];
        pos += 2;
        int is_c = (b0 & 0x80u) ? 1 : 0;
        uint8_t idx = (uint8_t)(b0 & 0x7Fu);
        if (idx < NECKLINK_SPLITAI32_N_CHANNELS && actual_ev < NECKLINK_SPLITAI32_MAX_EVENTS) {
            curr_idx[actual_ev] = idx;
            curr_zq[actual_ev] = necklink_splitai32_dequant_dual_range(is_c, qi);
            active_curr[idx] = 1;
            ++actual_ev;
        }
    }

    uint8_t gap;
    if (!st->has_synced) {
        gap = (seq >= 1u) ? seq : 1u;
        st->has_synced = 1;
    } else {
        gap = (uint8_t)((seq - st->last_seq) & 0xFFu);
        if (gap == 0u) gap = 1u;
    }
    st->last_seq = seq;

    if (gap > 1u) {
        st->detected_lost_packets += (uint32_t)(gap - 1u);
    }

    int32_t g_cap = (gap > 8u) ? 8 : (int32_t)gap;
    int32_t inst_arrive_q8 = 256 / g_cap;
    if (inst_arrive_q8 < (int32_t)st->arrive_ema_q8) {
        st->arrive_ema_q8 = (int16_t)((96 * (int32_t)st->arrive_ema_q8 + 160 * inst_arrive_q8) / 256);
    } else {
        st->arrive_ema_q8 = (int16_t)((216 * (int32_t)st->arrive_ema_q8 + 40 * inst_arrive_q8) / 256);
    }

    if (NECKLINK_SPLITAI32_ENABLE_GATE) {
        st->brake_gate_q8 = (int16_t)necklink_splitai32_clip_i32((int32_t)st->arrive_ema_q8, NECKLINK_SPLITAI32_SHUNT_FLOOR_Q8, 256);
    } else {
        st->brake_gate_q8 = 256;
    }

    int32_t eff_vg = (NECKLINK_SPLITAI32_VEL_GAIN_Q8 * (int32_t)st->brake_gate_q8) / 256;

    if (has_echo && pos < pkt_len) {
        uint8_t eh1 = pkt[pos++];
        int h1_has_sketch = (eh1 & 0x80u) ? 1 : 0;
        int inc_h2 = (eh1 & 0x40u) ? 1 : 0;
        int dt1 = (int)(eh1 & 0x1Fu);

        int h1_valid_wire = 0;
        uint8_t h1_idx = 0;
        int32_t h1_zq = 0;
        if (pos + 2 <= pkt_len) {
            uint8_t eb0 = pkt[pos];
            int8_t eq = (int8_t)pkt[pos + 1];
            pos += 2;
            int h1_c = (eb0 & 0x80u) ? 1 : 0;
            h1_idx = (uint8_t)(eb0 & 0x7Fu);
            if (h1_idx < NECKLINK_SPLITAI32_N_CHANNELS) {
                h1_zq = necklink_splitai32_dequant_dual_range(h1_c, eq);
                h1_valid_wire = 1;
            }
        }

        int32_t sk_vals[NECKLINK_SPLITAI32_SKETCH_DIM];
        for (i = 0; i < NECKLINK_SPLITAI32_SKETCH_DIM; ++i) sk_vals[i] = 0;
        if (h1_has_sketch && pos + NECKLINK_SPLITAI32_SKETCH_DIM <= pkt_len) {
            for (i = 0; i < NECKLINK_SPLITAI32_SKETCH_DIM; ++i) {
                sk_vals[i] = (int32_t)((int8_t)pkt[pos + i]);
            }
            pos += NECKLINK_SPLITAI32_SKETCH_DIM;
        }

        int h2_valid_wire = 0;
        uint8_t h2_idx = 0;
        int32_t h2_zq = 0;
        if (inc_h2 && pos + 2 <= pkt_len) {
            uint8_t eb2 = pkt[pos];
            int8_t eq2 = (int8_t)pkt[pos + 1];
            pos += 2;
            int h2_c = (eb2 & 0x80u) ? 1 : 0;
            h2_idx = (uint8_t)(eb2 & 0x7Fu);
            if (h2_idx < NECKLINK_SPLITAI32_N_CHANNELS) {
                h2_zq = necklink_splitai32_dequant_dual_range(h2_c, eq2);
                h2_valid_wire = 1;
            }
        }

        if (NECKLINK_SPLITAI32_ENABLE_ECHO && gap >= 2u) {
            if (gap >= 3u && h2_valid_wire && !active_curr[h2_idx]) {
                st->m[h2_idx] = (int16_t)necklink_splitai32_clip_i32(h2_zq, -32768, 32767);
                st->recovered_echo_packets += 1u;
            }

            if (h1_valid_wire && !active_curr[h1_idx]) {
                int32_t u1 = h1_zq - (int32_t)st->m[h1_idx];
                st->m[h1_idx] = (int16_t)necklink_splitai32_clip_i32(h1_zq, -32768, 32767);
                if (NECKLINK_SPLITAI32_ENABLE_MIRROR) {
                    int32_t isi1 = (st->age[h1_idx] >= 1u) ? (int32_t)st->age[h1_idx] : 1;
                    int32_t dv1 = (eff_vg * u1) / (256 * isi1);
                    int32_t v1 = necklink_splitai32_clip_i32((int32_t)st->v[h1_idx] + dv1, -32768, 32767);
                    int32_t m_now = (int32_t)st->m[h1_idx];
                    for (s = 0; s < dt1; ++s) {
                        if (v1 != 0) {
                            m_now = necklink_splitai32_clip_i32(m_now + (NECKLINK_SPLITAI32_BETA_Q8 * v1) / 256, -32768, 32767);
                            v1 = necklink_splitai32_clip_i32((NECKLINK_SPLITAI32_GAMMA_Q8 * v1) / 256, -32768, 32767);
                        }
                    }
                    st->m[h1_idx] = (int16_t)m_now;
                    st->v[h1_idx] = (int16_t)v1;
                }
                uint8_t a1 = (dt1 >= 1) ? (uint8_t)dt1 : 1u;
                st->age[h1_idx] = (a1 > 32u) ? 32u : a1;
            }

            if (h1_has_sketch) {
                for (c_idx = 0; c_idx < NECKLINK_SPLITAI32_N_CHANNELS; ++c_idx) {
                    if (active_curr[c_idx] || (h1_valid_wire && c_idx == (int)h1_idx)) continue;
                    int32_t acc = 0;
                    for (r_idx = 0; r_idx < NECKLINK_SPLITAI32_SKETCH_DIM; ++r_idx) {
                        acc += (int32_t)necklink_splitai32_phi_q7[r_idx][c_idx] * sk_vals[r_idx];
                    }
                    int32_t rec_val = (acc * 128 * NECKLINK_SPLITAI32_QUANT_STEP_Q8) / NECKLINK_SPLITAI32_COL_NORM_SQ;
                    if (necklink_splitai32_abs_i32(rec_val) > NECKLINK_SPLITAI32_DEADBAND_Q8) {
                        st->m[c_idx] = (int16_t)necklink_splitai32_clip_i32(
                            (int32_t)st->m[c_idx] + (rec_val * 144) / 256, -32768, 32767
                        );
                    }
                }
            }

            st->recovered_echo_packets += 1u;
        }
    }

    for (i = 0; i < actual_ev; ++i) {
        uint8_t idx = curr_idx[i];
        int32_t zq = curr_zq[i];
        int32_t u_innov = zq - (int32_t)st->m[idx];
        st->m[idx] = (int16_t)necklink_splitai32_clip_i32(zq, -32768, 32767);
        if (NECKLINK_SPLITAI32_ENABLE_MIRROR) {
            int32_t isi = (st->age[idx] >= 1u) ? (int32_t)st->age[idx] : 1;
            int32_t dv = (eff_vg * u_innov) / (256 * isi);
            st->v[idx] = (int16_t)necklink_splitai32_clip_i32((int32_t)st->v[idx] + dv, -32768, 32767);
        }
        st->age[idx] = 0u;
    }

    for (i = 0; i < NECKLINK_SPLITAI32_N_CHANNELS; ++i) {
        out_z_q8[i] = (int32_t)st->m[i];
    }
}

#endif /* NECKLINK_SPLITAI32_INT8_H */
