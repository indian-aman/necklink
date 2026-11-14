/*
 * NeckSense v3.0.0 - Zero-Heap, Zero-Float ANSI C99 Implementation
 *
 * Copyright (c) 2026 Amandeep Singh Bains
 * SPDX-License-Identifier: MIT
 */

#include "necksense.h"

#include <limits.h>
#include <string.h>

#define NS_SHIFT_GUARD (UINT64_C(1) << 47)

static int32_t ns_clamp_i32(int64_t v) {
    if (v > (int64_t)INT32_MAX) {
        return INT32_MAX;
    }
    if (v < (int64_t)INT32_MIN) {
        return INT32_MIN;
    }
    return (int32_t)v;
}

static int16_t ns_clamp_i16(int64_t v) {
    if (v > (int64_t)INT16_MAX) {
        return INT16_MAX;
    }
    if (v < (int64_t)INT16_MIN) {
        return INT16_MIN;
    }
    return (int16_t)v;
}

/* Portable exact floor right-shift matching Python `v >> shift` */
static int64_t ns_shr_floor(int64_t v, uint32_t shift) {
    if (shift == 0u) {
        return v;
    }
    if (v >= 0) {
        return (int64_t)((uint64_t)v >> shift);
    }
    return -(int64_t)((((uint64_t)(-(v + 1))) >> shift) + UINT64_C(1));
}

static int64_t ns_shr16_floor(int64_t v) {
    return ns_shr_floor(v, 16u);
}

/* Portable exact floor division matching Python `a // b` for b > 0 */
static int64_t ns_div_floor_i64(int64_t a, int64_t b) {
    int64_t q = a / b;
    int64_t r = a % b;
    if (r != 0 && ((r < 0) != (b < 0))) {
        --q;
    }
    return q;
}

static uint64_t ns_abs_i64(int64_t v) {
    if (v >= 0) {
        return (uint64_t)v;
    }
    return ((uint64_t)(-(v + 1))) + UINT64_C(1);
}

/* Portable count-trailing-zeros for non-zero uint64_t */
static uint32_t ns_ctz_u64(uint64_t x) {
#if defined(__GNUC__) || defined(__clang__)
    return (uint32_t)__builtin_ctzll(x);
#else
    uint32_t n = 0u;
    if ((x & UINT64_C(0xFFFFFFFF)) == UINT64_C(0)) {
        n += 32u;
        x >>= 32;
    }
    if ((x & UINT64_C(0xFFFF)) == UINT64_C(0)) {
        n += 16u;
        x >>= 16;
    }
    if ((x & UINT64_C(0xFF)) == UINT64_C(0)) {
        n += 8u;
        x >>= 8;
    }
    if ((x & UINT64_C(0xF)) == UINT64_C(0)) {
        n += 4u;
        x >>= 4;
    }
    if ((x & UINT64_C(0x3)) == UINT64_C(0)) {
        n += 2u;
        x >>= 2;
    }
    if ((x & UINT64_C(0x1)) == UINT64_C(0)) {
        n += 1u;
    }
    return n;
#endif
}

/* -------------------------------------------------------------------------
 * 1. Antennal Lobe (`ns_antennal_*`)
 * ------------------------------------------------------------------------- */

int ns_antennal_init(
    ns_antennal_t *ctx,
    uint32_t n_channels,
    int32_t sigma_q16,
    int32_t gamma_q16,
    uint32_t hill_n,
    int32_t tonic_alpha_q16,
    int32_t tonic_slew_q16,
    int32_t phasic_gain_q16,
    int32_t fast_alpha_q16
) {
    int64_t down_alpha;
    int64_t w_thresh;
    if (ctx == NULL) {
        return NS_ERR_ARG;
    }
    if (n_channels == 0u || n_channels > NS_MAX_CHANNELS) {
        return NS_ERR_BOUNDS;
    }
    if (sigma_q16 <= 0 || gamma_q16 < 0 || (hill_n != 1u && hill_n != 2u)) {
        return NS_ERR_ARG;
    }
    if (tonic_alpha_q16 < 0 || tonic_alpha_q16 > NS_Q16_ONE) {
        return NS_ERR_ARG;
    }
    if (tonic_slew_q16 < 0 || phasic_gain_q16 < 0) {
        return NS_ERR_ARG;
    }
    if (fast_alpha_q16 < 0 || fast_alpha_q16 > NS_Q16_ONE) {
        return NS_ERR_ARG;
    }

    memset(ctx, 0, sizeof(*ctx));
    ctx->n_channels = n_channels;
    ctx->sigma_q16 = sigma_q16;
    ctx->gamma_q16 = gamma_q16;
    ctx->hill_n = hill_n;
    ctx->tonic_alpha_q16 = tonic_alpha_q16;
    down_alpha = (int64_t)tonic_alpha_q16 * 5;
    if (down_alpha > NS_Q16_ONE) {
        down_alpha = NS_Q16_ONE;
    }
    ctx->tonic_down_alpha_q16 = (tonic_alpha_q16 > 0) ? (int32_t)down_alpha : 0;
    ctx->tonic_slew_q16 = tonic_slew_q16;
    ctx->phasic_gain_q16 = phasic_gain_q16;
    ctx->fast_alpha_q16 = fast_alpha_q16;

    w_thresh = (int64_t)sigma_q16 * 2;
    if (w_thresh > (int64_t)INT32_MAX) {
        w_thresh = (int64_t)INT32_MAX;
    }
    ctx->whiff_thresh_q16 = (int32_t)w_thresh;
    ctx->whiff_alpha_q16 = 6554; /* 0.10 in Q16.16 */
    return NS_OK;
}

void ns_antennal_reset(ns_antennal_t *ctx, const int32_t *baseline_q16) {
    uint32_t i;
    if (ctx == NULL) {
        return;
    }
    ctx->whiff_rate_q16 = 0;
    ctx->intermittency_q16 = 0;
    ctx->steps = 0u;
    ctx->whiff_count = 0u;
    ctx->in_whiff = 0u;
    if (baseline_q16 == NULL) {
        memset(ctx->tonic_q16, 0, sizeof(ctx->tonic_q16));
        memset(ctx->fast_q16, 0, sizeof(ctx->fast_q16));
        return;
    }
    for (i = 0u; i < ctx->n_channels; ++i) {
        ctx->tonic_q16[i] = baseline_q16[i];
        ctx->fast_q16[i] = baseline_q16[i];
    }
}

static void ns_ln_divisive_norm(
    const ns_antennal_t *ctx,
    const uint32_t *pos_q16,
    int32_t *out_q16
) {
    uint32_t i;
    uint32_t d = ctx->n_channels;
    uint64_t sum_p = UINT64_C(0);
    uint64_t mean_pool;
    uint64_t lat_q16;

    for (i = 0u; i < d; ++i) {
        sum_p += (uint64_t)pos_q16[i];
    }
    mean_pool = sum_p / (uint64_t)d;
    lat_q16 = (mean_pool * (uint64_t)(uint32_t)ctx->gamma_q16) >> 16;

    if (ctx->hill_n == 1u) {
        uint64_t base_den = (uint64_t)(uint32_t)ctx->sigma_q16 + lat_q16;
        for (i = 0u; i < d; ++i) {
            uint64_t num = (uint64_t)pos_q16[i];
            uint64_t den = base_den + num;
            out_q16[i] = (den == UINT64_C(0)) ? 0 : (int32_t)((num << 16) / den);
        }
    } else {
        uint64_t sig = (uint64_t)(uint32_t)ctx->sigma_q16;
        uint64_t sig2 = sig * sig;
        uint64_t lat2 = (lat_q16 > UINT64_C(0xFFFFFFFF))
                            ? (UINT64_MAX >> 2)
                            : (lat_q16 * lat_q16);
        uint64_t base_den = sig2 + lat2;
        for (i = 0u; i < d; ++i) {
            uint64_t p = (uint64_t)pos_q16[i];
            uint64_t num = p * p;
            uint64_t den = base_den + num;
            if (den == UINT64_C(0)) {
                out_q16[i] = 0;
            } else {
                uint64_t n_s = num;
                uint64_t d_s = den;
                while (n_s > NS_SHIFT_GUARD) {
                    n_s >>= 2;
                    d_s >>= 2;
                }
                out_q16[i] = (d_s == UINT64_C(0)) ? NS_Q16_ONE : (int32_t)((n_s << 16) / d_s);
            }
        }
    }
}

int ns_antennal_normalize(
    const ns_antennal_t *ctx,
    const int32_t *x_q16,
    int32_t *out_q16
) {
    uint32_t pos[NS_MAX_CHANNELS];
    uint32_t i;
    if (ctx == NULL || x_q16 == NULL || out_q16 == NULL) {
        return NS_ERR_ARG;
    }
    for (i = 0u; i < ctx->n_channels; ++i) {
        pos[i] = (x_q16[i] > 0) ? (uint32_t)x_q16[i] : 0u;
    }
    ns_ln_divisive_norm(ctx, pos, out_q16);
    return NS_OK;
}

int ns_antennal_step(
    ns_antennal_t *ctx,
    const int32_t *x_q16,
    int32_t *out_q16
) {
    uint32_t pos[NS_MAX_CHANNELS];
    uint64_t pos_phasic_sum = UINT64_C(0);
    int64_t mean_phasic;
    int64_t up_cross = 0;
    int64_t active_q16;
    uint32_t i;

    if (ctx == NULL || x_q16 == NULL || out_q16 == NULL) {
        return NS_ERR_ARG;
    }

    for (i = 0u; i < ctx->n_channels; ++i) {
        int64_t xv = (int64_t)x_q16[i];
        int64_t bv = (int64_t)ctx->tonic_q16[i];
        int64_t fv = (int64_t)ctx->fast_q16[i];

        int64_t diff_tonic = xv - bv;
        int64_t diff_fast = xv - fv;
        int64_t phasic_term = ns_shr16_floor(diff_fast * (int64_t)ctx->phasic_gain_q16);
        int64_t drive = diff_tonic + phasic_term;

        if (diff_fast > 0) {
            pos_phasic_sum += (uint64_t)diff_fast;
        }

        if (drive <= 0) {
            pos[i] = 0u;
        } else if (drive > (int64_t)INT32_MAX) {
            pos[i] = (uint32_t)INT32_MAX;
        } else {
            pos[i] = (uint32_t)drive;
        }

        fv += ns_shr16_floor(diff_fast * (int64_t)ctx->fast_alpha_q16);
        ctx->fast_q16[i] = ns_clamp_i32(fv);

        if (diff_tonic >= 0) {
            int64_t step_b = ns_shr16_floor(diff_tonic * (int64_t)ctx->tonic_alpha_q16);
            if (ctx->tonic_slew_q16 > 0 && step_b > (int64_t)ctx->tonic_slew_q16) {
                step_b = (int64_t)ctx->tonic_slew_q16;
            }
            bv += step_b;
        } else {
            int64_t step_b = ns_shr16_floor(diff_tonic * (int64_t)ctx->tonic_down_alpha_q16);
            bv += step_b;
        }
        ctx->tonic_q16[i] = ns_clamp_i32(bv);
    }

    /* Update turbulent plume whiff up-crossings & intermittency (Schmitt hysteresis) */
    mean_phasic = (int64_t)(pos_phasic_sum / (uint64_t)ctx->n_channels);
    if (ctx->in_whiff == 0u) {
        if (mean_phasic >= (int64_t)ctx->whiff_thresh_q16) {
            ctx->in_whiff = 1u;
            ctx->whiff_count += 1u;
            up_cross = NS_Q16_ONE;
        }
    } else {
        int64_t low_thresh = (int64_t)(ctx->whiff_thresh_q16 >> 2);
        if (mean_phasic <= low_thresh) {
            ctx->in_whiff = 0u;
        }
    }
    active_q16 = (ctx->in_whiff != 0u) ? NS_Q16_ONE : 0;
    ctx->whiff_rate_q16 = ns_clamp_i32(
        (int64_t)ctx->whiff_rate_q16 +
        ns_shr16_floor((up_cross - (int64_t)ctx->whiff_rate_q16) * (int64_t)ctx->whiff_alpha_q16)
    );
    ctx->intermittency_q16 = ns_clamp_i32(
        (int64_t)ctx->intermittency_q16 +
        ns_shr16_floor((active_q16 - (int64_t)ctx->intermittency_q16) * (int64_t)ctx->whiff_alpha_q16)
    );
    ctx->steps += 1u;

    ns_ln_divisive_norm(ctx, pos, out_q16);
    return NS_OK;
}

int ns_antennal_step_whiff(
    ns_antennal_t *ctx,
    const int32_t *x_q16,
    int32_t *out_q16,
    int32_t *out_whiff_rate_q16
) {
    int rc = ns_antennal_step(ctx, x_q16, out_q16);
    if (rc != NS_OK) {
        return rc;
    }
    if (out_whiff_rate_q16 != NULL) {
        *out_whiff_rate_q16 = ctx->whiff_rate_q16;
    }
    return NS_OK;
}

/* -------------------------------------------------------------------------
 * 2. Mushroom Body Calyx Sparse Hasher (`ns_kenyon_*`)
 * ------------------------------------------------------------------------- */

typedef struct {
    int64_t  val;
    uint32_t idx;
} ns_kc_cand_t;

/* Returns 1 iff `a` is strictly worse than `b` under `(val, -idx)` ordering */
static int ns_cand_worse(ns_kc_cand_t a, ns_kc_cand_t b) {
    if (a.val != b.val) {
        return (a.val < b.val) ? 1 : 0;
    }
    return (a.idx > b.idx) ? 1 : 0;
}

static void ns_heap_sift_down(ns_kc_cand_t *heap, uint32_t k, uint32_t root) {
    for (;;) {
        uint32_t left = (root << 1) + 1u;
        uint32_t right = left + 1u;
        uint32_t worst = root;

        if (left < k && ns_cand_worse(heap[left], heap[worst])) {
            worst = left;
        }
        if (right < k && ns_cand_worse(heap[right], heap[worst])) {
            worst = right;
        }
        if (worst == root) {
            break;
        }
        {
            ns_kc_cand_t tmp = heap[root];
            heap[root] = heap[worst];
            heap[worst] = tmp;
        }
        root = worst;
    }
}

int ns_kenyon_init(
    ns_kenyon_t *ctx,
    uint32_t n_inputs,
    uint32_t n_kc,
    uint32_t d_proj,
    uint32_t k_winners,
    uint32_t seed
) {
    if (ctx == NULL) {
        return NS_ERR_ARG;
    }
    if (n_inputs == 0u || n_inputs > NS_MAX_CHANNELS) {
        return NS_ERR_BOUNDS;
    }
    if (n_kc < 64u || n_kc > NS_MAX_KC || (n_kc & 63u) != 0u) {
        return NS_ERR_BOUNDS;
    }
    if (d_proj == 0u || k_winners == 0u || k_winners > n_kc || k_winners > NS_MAX_WTA_WINNERS) {
        return NS_ERR_BOUNDS;
    }

    memset(ctx, 0, sizeof(*ctx));
    ctx->n_inputs = n_inputs;
    ctx->n_kc = n_kc;
    ctx->n_words = n_kc >> 6;
    ctx->d_proj = d_proj;
    ctx->k_winners = k_winners;
    ctx->seed = seed;
    ctx->weights = (n_kc <= NS_BANC_MAX_KC) ? ns_banc_default_weights() : NULL;
    return NS_OK;
}

int ns_kenyon_set_weights(
    ns_kenyon_t *ctx,
    const ns_banc_mb_weights_t *weights
) {
    if (ctx == NULL) {
        return NS_ERR_ARG;
    }
    if (weights != NULL && ctx->n_kc > NS_BANC_MAX_KC) {
        return NS_ERR_BOUNDS;
    }
    ctx->weights = weights;
    return NS_OK;
}

int ns_kenyon_hash(
    const ns_kenyon_t *ctx,
    const int32_t *x_q16,
    uint64_t *out_words
) {
    ns_kc_cand_t heap[NS_MAX_WTA_WINNERS];
    uint32_t j;
    uint32_t c;
    uint32_t k;

    if (ctx == NULL || x_q16 == NULL || out_words == NULL) {
        return NS_ERR_ARG;
    }

    k = ctx->k_winners;

    if (ctx->weights != NULL && ctx->n_kc <= NS_BANC_MAX_KC) {
        const ns_banc_mb_weights_t *w = ctx->weights;
        uint32_t glom_offset = (ctx->seed ^ 0x9E3779B9u) & 63u;
        int64_t sum_apl_num = 0;
        int64_t sum_apl_den = 0;
        int64_t mean_apl;

        /* Pass 1: Pooled KC -> APL drive */
        for (j = 0u; j < ctx->n_kc; ++j) {
            int64_t raw_kc = 0;
            uint32_t base_c = j * NS_BANC_MAX_CLAWS;
            for (c = 0u; c < NS_BANC_MAX_CLAWS; ++c) {
                uint32_t glom = (uint32_t)w->claw_glom[base_c + c];
                if (glom < NS_BANC_GLOMERULI) {
                    uint32_t in_idx = ((glom + glom_offset) & 63u) % ctx->n_inputs;
                    raw_kc += (int64_t)x_q16[in_idx] * (int64_t)w->claw_w_q8[base_c + c];
                }
            }
            sum_apl_num += raw_kc * (int64_t)w->kc_to_apl_q8[j];
            sum_apl_den += (int64_t)w->kc_to_apl_q8[j];
        }
        if (sum_apl_den < 1) {
            sum_apl_den = 1;
        }
        mean_apl = ns_div_floor_i64(sum_apl_num, sum_apl_den);

        /* Pass 2: Inhomogeneous APL -> KC inhibition + exact top-k (val, -idx) WTA */
        for (j = 0u; j < ctx->n_kc; ++j) {
            int64_t raw_kc = 0;
            int64_t eff_kc;
            uint32_t base_c = j * NS_BANC_MAX_CLAWS;
            for (c = 0u; c < NS_BANC_MAX_CLAWS; ++c) {
                uint32_t glom = (uint32_t)w->claw_glom[base_c + c];
                if (glom < NS_BANC_GLOMERULI) {
                    uint32_t in_idx = ((glom + glom_offset) & 63u) % ctx->n_inputs;
                    raw_kc += (int64_t)x_q16[in_idx] * (int64_t)w->claw_w_q8[base_c + c];
                }
            }
            eff_kc = raw_kc - ns_shr_floor(mean_apl * (int64_t)w->apl_to_kc_q8[j] * 90, 15u);

            if (j < k) {
                heap[j].val = eff_kc;
                heap[j].idx = j;
                if (j + 1u == k) {
                    uint32_t r = k >> 1;
                    while (r > 0u) {
                        --r;
                        ns_heap_sift_down(heap, k, r);
                    }
                }
            } else if (eff_kc > heap[0].val) {
                heap[0].val = eff_kc;
                heap[0].idx = j;
                ns_heap_sift_down(heap, k, 0u);
            }
        }
    } else {
        uint32_t state = ctx->seed ^ 0x9E3779B9u;
        if (state == 0u) {
            state = 0x6D2B79F5u;
        }

        for (j = 0u; j < ctx->n_kc; ++j) {
            int64_t acc = 0;
            for (c = 0u; c < ctx->d_proj; ++c) {
                uint32_t in_idx;
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                in_idx = (uint32_t)(((uint64_t)state * (uint64_t)ctx->n_inputs) >> 32);
                acc += (int64_t)x_q16[in_idx];
            }

            if (j < k) {
                heap[j].val = acc;
                heap[j].idx = j;
                if (j + 1u == k) {
                    uint32_t r = k >> 1;
                    while (r > 0u) {
                        --r;
                        ns_heap_sift_down(heap, k, r);
                    }
                }
            } else if (acc > heap[0].val) {
                heap[0].val = acc;
                heap[0].idx = j;
                ns_heap_sift_down(heap, k, 0u);
            }
        }
    }

    memset(out_words, 0, (size_t)ctx->n_words * sizeof(uint64_t));
    for (j = 0u; j < k; ++j) {
        uint32_t kc = heap[j].idx;
        out_words[kc >> 6] |= (UINT64_C(1) << (kc & 63u));
    }
    return NS_OK;
}

uint32_t ns_popcount_u64(uint64_t x) {
    x = x - ((x >> 1) & UINT64_C(0x5555555555555555));
    x = (x & UINT64_C(0x3333333333333333)) + ((x >> 2) & UINT64_C(0x3333333333333333));
    x = (x + (x >> 4)) & UINT64_C(0x0F0F0F0F0F0F0F0F);
    return (uint32_t)((x * UINT64_C(0x0101010101010101)) >> 56);
}

uint32_t ns_popcount_overlap(
    const uint64_t *words_a,
    const uint64_t *words_b,
    uint32_t n_words
) {
    uint32_t w;
    uint32_t total = 0u;
    if (words_a == NULL || words_b == NULL) {
        return 0u;
    }
    for (w = 0u; w < n_words; ++w) {
        total += ns_popcount_u64(words_a[w] & words_b[w]);
    }
    return total;
}

/* -------------------------------------------------------------------------
 * 3. MBON-alpha'3 Online 1-Shot Novelty Detector (`ns_novelty_*`)
 *    Lossless 17-bit representation of [0 .. 65536].
 * ------------------------------------------------------------------------- */

static int32_t ns_nov_get_w(const ns_novelty_t *ctx, uint32_t kc) {
    uint64_t mask = UINT64_C(1) << (kc & 63u);
    if ((ctx->full_mask[kc >> 6] & mask) != UINT64_C(0)) {
        return NS_Q16_ONE;
    }
    return (int32_t)ctx->weights_u16[kc];
}

static void ns_nov_set_w(ns_novelty_t *ctx, uint32_t kc, int32_t w) {
    uint32_t w_idx = kc >> 6;
    uint64_t mask = UINT64_C(1) << (kc & 63u);
    if (w >= NS_Q16_ONE) {
        ctx->full_mask[w_idx] |= mask;
        ctx->weights_u16[kc] = 65535u;
    } else {
        ctx->full_mask[w_idx] &= ~mask;
        ctx->weights_u16[kc] = (w <= 0) ? 0u : (uint16_t)w;
    }
}

int ns_novelty_init(
    ns_novelty_t *ctx,
    uint32_t n_kc,
    int32_t lr_q16,
    int32_t recover_alpha_q16
) {
    if (ctx == NULL) {
        return NS_ERR_ARG;
    }
    if (n_kc < 64u || n_kc > NS_MAX_KC || (n_kc & 63u) != 0u) {
        return NS_ERR_BOUNDS;
    }
    if (lr_q16 <= 0 || lr_q16 > NS_Q16_ONE) {
        return NS_ERR_ARG;
    }
    if (recover_alpha_q16 < 0 || recover_alpha_q16 > NS_Q16_ONE) {
        return NS_ERR_ARG;
    }

    memset(ctx, 0, sizeof(*ctx));
    ctx->n_kc = n_kc;
    ctx->n_words = n_kc >> 6;
    ctx->lr_q16 = lr_q16;
    ctx->keep_q16 = NS_Q16_ONE - lr_q16;
    ctx->recover_alpha_q16 = recover_alpha_q16;
    ns_novelty_reset(ctx);
    return NS_OK;
}

void ns_novelty_reset(ns_novelty_t *ctx) {
    uint32_t w_idx;
    uint32_t j;
    if (ctx == NULL) {
        return;
    }
    ctx->steps = 0u;
    for (w_idx = 0u; w_idx < ctx->n_words; ++w_idx) {
        ctx->full_mask[w_idx] = UINT64_MAX;
    }
    for (j = 0u; j < ctx->n_kc; ++j) {
        ctx->weights_u16[j] = 65535u;
    }
}

int32_t ns_novelty_score(
    const ns_novelty_t *ctx,
    const uint64_t *kc_words
) {
    uint32_t w_idx;
    uint32_t active_cnt = 0u;
    int64_t sum_w = 0;
    int64_t score;

    if (ctx == NULL || kc_words == NULL) {
        return NS_ERR_ARG;
    }

    for (w_idx = 0u; w_idx < ctx->n_words; ++w_idx) {
        uint64_t word = kc_words[w_idx];
        uint32_t base = w_idx << 6;
        while (word != UINT64_C(0)) {
            uint32_t bit = ns_ctz_u64(word);
            sum_w += (int64_t)ns_nov_get_w(ctx, base + bit);
            ++active_cnt;
            word &= (word - UINT64_C(1));
        }
    }

    if (active_cnt == 0u) {
        return 0;
    }
    score = sum_w / (int64_t)active_cnt;
    if (score < 0) {
        return 0;
    }
    if (score > NS_Q16_ONE) {
        return NS_Q16_ONE;
    }
    return (int32_t)score;
}

int32_t ns_novelty_update_and_score(
    ns_novelty_t *ctx,
    const uint64_t *kc_words
) {
    uint32_t w_idx;
    uint32_t j;
    uint32_t active_cnt = 0u;
    int64_t sum_w = 0;
    int64_t score;

    if (ctx == NULL || kc_words == NULL) {
        return NS_ERR_ARG;
    }

    if (ctx->recover_alpha_q16 > 0) {
        int64_t rec = (int64_t)ctx->recover_alpha_q16;
        for (j = 0u; j < ctx->n_kc; ++j) {
            int32_t cur_w = ns_nov_get_w(ctx, j);
            if (cur_w < NS_Q16_ONE) {
                int64_t diff = (int64_t)NS_Q16_ONE - (int64_t)cur_w;
                ns_nov_set_w(ctx, j, cur_w + (int32_t)ns_shr16_floor(diff * rec));
            }
        }
    }

    for (w_idx = 0u; w_idx < ctx->n_words; ++w_idx) {
        uint64_t word = kc_words[w_idx];
        uint32_t base = w_idx << 6;
        while (word != UINT64_C(0)) {
            uint32_t bit = ns_ctz_u64(word);
            uint32_t kc = base + bit;
            int64_t cur = (int64_t)ns_nov_get_w(ctx, kc);
            sum_w += cur;
            ++active_cnt;
            ns_nov_set_w(ctx, kc, (int32_t)ns_shr16_floor(cur * (int64_t)ctx->keep_q16));
            word &= (word - UINT64_C(1));
        }
    }

    ++ctx->steps;
    if (active_cnt == 0u) {
        return 0;
    }
    score = sum_w / (int64_t)active_cnt;
    if (score < 0) {
        return 0;
    }
    if (score > NS_Q16_ONE) {
        return NS_Q16_ONE;
    }
    return (int32_t)score;
}

/* -------------------------------------------------------------------------
 * 4. 2-Compartment KC-gamma (STM) + KC-alpha/beta (LTM) Valence Classifier
 * ------------------------------------------------------------------------- */

int ns_valence_init(
    ns_valence_t *ctx,
    uint32_t n_kc,
    uint32_t n_classes,
    int32_t min_lr_q16,
    int32_t opponent_inh_q16
) {
    if (ctx == NULL) {
        return NS_ERR_ARG;
    }
    if (n_kc < 64u || n_kc > NS_MAX_KC || (n_kc & 63u) != 0u) {
        return NS_ERR_BOUNDS;
    }
    if (n_classes == 0u || n_classes > NS_MAX_CLASSES) {
        return NS_ERR_BOUNDS;
    }
    if (min_lr_q16 <= 0 || min_lr_q16 > NS_Q16_ONE) {
        return NS_ERR_ARG;
    }
    if (opponent_inh_q16 < 0 || opponent_inh_q16 > NS_Q16_ONE) {
        return NS_ERR_ARG;
    }

    memset(ctx, 0, sizeof(*ctx));
    ctx->n_kc = n_kc;
    ctx->n_words = n_kc >> 6;
    ctx->n_classes = n_classes;
    ctx->min_lr_q16 = min_lr_q16;
    ctx->opponent_inh_q16 = opponent_inh_q16;
    ctx->ltm_floor_q14 = 9011; /* 0.55 * 16384 protected alpha/beta floor */
    ctx->weights = (n_kc <= NS_BANC_MAX_KC) ? ns_banc_default_weights() : NULL;
    return NS_OK;
}

int ns_valence_set_weights(
    ns_valence_t *ctx,
    const ns_banc_mb_weights_t *weights
) {
    if (ctx == NULL) {
        return NS_ERR_ARG;
    }
    if (weights != NULL && ctx->n_kc > NS_BANC_MAX_KC) {
        return NS_ERR_BOUNDS;
    }
    ctx->weights = weights;
    return NS_OK;
}

void ns_valence_reset(ns_valence_t *ctx) {
    if (ctx == NULL) {
        return;
    }
    ctx->consolidations = 0u;
    memset(ctx->class_counts, 0, sizeof(ctx->class_counts));
    memset(ctx->class_active, 0, sizeof(ctx->class_active));
    memset(ctx->ltm_ab, 0, sizeof(ctx->ltm_ab));
    memset(ctx->weights_q14, 0, sizeof(ctx->weights_q14));
}

int ns_valence_learn(
    ns_valence_t *ctx,
    const uint64_t *kc_words,
    uint32_t class_id,
    int32_t reward_q16
) {
    uint32_t n_c;
    int32_t eta_q16;
    int32_t keep_q16;
    int32_t reward_q14;
    int32_t delta_q14;
    int32_t opp_step_q16;
    int32_t floor_q14;
    uint32_t w_idx;
    uint32_t c2;

    if (ctx == NULL || kc_words == NULL) {
        return NS_ERR_ARG;
    }
    if (class_id >= ctx->n_classes) {
        return NS_ERR_BOUNDS;
    }

    ctx->class_active[class_id] = 1u;
    ctx->class_counts[class_id] += 1u;
    n_c = ctx->class_counts[class_id];

    eta_q16 = (int32_t)((uint32_t)NS_Q16_ONE / n_c);
    if (eta_q16 < ctx->min_lr_q16) {
        eta_q16 = ctx->min_lr_q16;
    }
    keep_q16 = NS_Q16_ONE - eta_q16;
    reward_q14 = (int32_t)ns_shr_floor((int64_t)reward_q16, 2u);
    delta_q14 = (int32_t)ns_shr16_floor((int64_t)eta_q16 * (int64_t)reward_q14);
    opp_step_q16 = (int32_t)ns_shr16_floor((int64_t)eta_q16 * (int64_t)ctx->opponent_inh_q16);
    floor_q14 = (ctx->weights != NULL)
                    ? (int32_t)ns_clamp_i16((int64_t)ctx->ltm_floor_q14 * 2)
                    : (int32_t)ctx->ltm_floor_q14;

    /* Homeostatic decay on target class, respecting consolidated KCab floors */
    for (w_idx = 0u; w_idx < ctx->n_words; ++w_idx) {
        uint64_t lock_word = ctx->ltm_ab[class_id][w_idx];
        uint64_t active_word = kc_words[w_idx];
        uint32_t base = w_idx << 6;
        uint32_t bit;
        for (bit = 0u; bit < 64u; ++bit) {
            uint32_t kc = base + bit;
            int64_t w_cur = (int64_t)ctx->weights_q14[class_id][kc];
            int64_t w_dec = ns_shr16_floor(w_cur * (int64_t)keep_q16);
            uint64_t mask = UINT64_C(1) << bit;

            if ((lock_word & mask) != UINT64_C(0)) {
                /* Allow direct extinction if active KC receives opposite-sign reward */
                int opposite_active = ((active_word & mask) != UINT64_C(0)) &&
                                      ((w_cur > 0 && reward_q14 < 0) || (w_cur < 0 && reward_q14 > 0));
                if (!opposite_active) {
                    if (w_cur >= (int64_t)floor_q14 && w_dec < (int64_t)floor_q14) {
                        w_dec = (int64_t)floor_q14;
                    } else if (w_cur <= -(int64_t)floor_q14 && w_dec > -(int64_t)floor_q14) {
                        w_dec = -(int64_t)floor_q14;
                    }
                }
            }
            ctx->weights_q14[class_id][kc] = ns_clamp_i16(w_dec);
        }
    }

    /* Hebbian / anti-Hebbian update on active KCs + cross-class opponent inhibition */
    for (w_idx = 0u; w_idx < ctx->n_words; ++w_idx) {
        uint64_t word = kc_words[w_idx];
        uint32_t base = w_idx << 6;
        while (word != UINT64_C(0)) {
            uint32_t bit = ns_ctz_u64(word);
            uint64_t mask = UINT64_C(1) << bit;
            uint32_t kc = base + bit;
            int64_t updated = (int64_t)ctx->weights_q14[class_id][kc] + (int64_t)delta_q14;
            int16_t clamped = ns_clamp_i16(updated);
            ctx->weights_q14[class_id][kc] = clamped;

            /* Reconsolidation / extinction unlock if opposite reward depressed below half-floor */
            if ((ctx->ltm_ab[class_id][w_idx] & mask) != UINT64_C(0)) {
                int32_t mag = (clamped >= 0) ? (int32_t)clamped : -(int32_t)clamped;
                if (mag < (floor_q14 >> 1)) {
                    ctx->ltm_ab[class_id][w_idx] &= ~mask;
                }
            }

            if (reward_q16 > 0 && opp_step_q16 > 0) {
                for (c2 = 0u; c2 < ctx->n_classes; ++c2) {
                    if (c2 != class_id && ctx->class_active[c2] != 0u) {
                        /* Consolidated alpha/beta synapses are immune to cross-class erosion */
                        if ((ctx->ltm_ab[c2][w_idx] & mask) != UINT64_C(0)) {
                            continue;
                        }
                        {
                            int32_t w_other = (int32_t)ctx->weights_q14[c2][kc];
                            if (w_other > 0) {
                                int32_t dec = (int32_t)ns_shr16_floor(
                                    (int64_t)w_other * (int64_t)opp_step_q16
                                );
                                ctx->weights_q14[c2][kc] = ns_clamp_i16((int64_t)(w_other - dec));
                            }
                        }
                    }
                }
            }
            word &= (word - UINT64_C(1));
        }
    }

    return NS_OK;
}

int ns_valence_consolidate(
    ns_valence_t *ctx,
    int32_t thresh_q16
) {
    int32_t thresh_q14;
    uint32_t c;
    uint32_t kc;

    if (ctx == NULL) {
        return NS_ERR_ARG;
    }
    if (thresh_q16 <= 0) {
        thresh_q16 = 32768; /* default 0.50 in Q16.16 */
    }
    thresh_q14 = (int32_t)ns_shr_floor((int64_t)thresh_q16, 2u);
    if (thresh_q14 < 1) {
        thresh_q14 = 1;
    }

    for (c = 0u; c < ctx->n_classes; ++c) {
        if (ctx->class_active[c] == 0u) {
            continue;
        }
        if (ctx->weights != NULL) {
            int has_new_ab = 0;
            for (kc = 0u; kc < ctx->n_kc; ++kc) {
                uint32_t w_idx = kc >> 6;
                uint64_t mask = UINT64_C(1) << (kc & 63u);
                int is_ab = ((ctx->weights->kc_is_ab_mask_u64[w_idx] & mask) != UINT64_C(0));
                int is_locked = ((ctx->ltm_ab[c][w_idx] & mask) != UINT64_C(0));
                int32_t w = (int32_t)ctx->weights_q14[c][kc];
                if (is_ab && !is_locked && (w >= thresh_q14 || w <= -thresh_q14)) {
                    has_new_ab = 1;
                    break;
                }
            }
            if (has_new_ab) {
                for (kc = 0u; kc < ctx->n_kc; ++kc) {
                    uint32_t w_idx = kc >> 6;
                    uint64_t mask = UINT64_C(1) << (kc & 63u);
                    int is_ab = ((ctx->weights->kc_is_ab_mask_u64[w_idx] & mask) != UINT64_C(0));
                    int is_locked = ((ctx->ltm_ab[c][w_idx] & mask) != UINT64_C(0));
                    int64_t w = (int64_t)ctx->weights_q14[c][kc];
                    if (is_ab) {
                        if (!is_locked && (w >= (int64_t)thresh_q14 || w <= -(int64_t)thresh_q14)) {
                            ctx->weights_q14[c][kc] = ns_clamp_i16(w * 2);
                            ctx->ltm_ab[c][w_idx] |= mask;
                        }
                    } else {
                        ctx->weights_q14[c][kc] = ns_clamp_i16(ns_shr_floor(w, 2u));
                    }
                }
            }
        } else {
            for (kc = 0u; kc < ctx->n_kc; ++kc) {
                int32_t w = (int32_t)ctx->weights_q14[c][kc];
                if (w >= thresh_q14 || w <= -thresh_q14) {
                    ctx->ltm_ab[c][kc >> 6] |= (UINT64_C(1) << (kc & 63u));
                }
            }
        }
    }
    if (ctx->consolidations < UINT16_MAX) {
        ctx->consolidations += 1u;
    }
    return NS_OK;
}

int ns_valence_scores(
    const ns_valence_t *ctx,
    const uint64_t *kc_words,
    int32_t *out_scores_q16
) {
    int64_t gain_den = 0;
    uint32_t w_idx;
    uint32_t c;
    int any_learned = 0;

    if (ctx == NULL || kc_words == NULL || out_scores_q16 == NULL) {
        return NS_ERR_ARG;
    }

    for (c = 0u; c < ctx->n_classes; ++c) {
        if (ctx->class_active[c] != 0u) {
            any_learned = 1;
            break;
        }
    }
    if (!any_learned) {
        return NS_ERR_EMPTY;
    }

    for (w_idx = 0u; w_idx < ctx->n_words; ++w_idx) {
        uint64_t word = kc_words[w_idx];
        if (ctx->weights != NULL) {
            uint32_t base = w_idx << 6;
            while (word != UINT64_C(0)) {
                uint32_t bit = ns_ctz_u64(word);
                uint32_t kc = base + bit;
                gain_den += (int64_t)ctx->weights->kc_to_mbon_ach_q8[kc]
                          + (int64_t)ctx->weights->kc_to_mbon_inh_q8[kc];
                word &= (word - UINT64_C(1));
            }
        } else {
            gain_den += (int64_t)ns_popcount_u64(word);
        }
    }

    for (c = 0u; c < ctx->n_classes; ++c) {
        if (ctx->class_active[c] == 0u || gain_den <= 0) {
            out_scores_q16[c] = 0;
        } else {
            int64_t sum_q14 = 0;
            for (w_idx = 0u; w_idx < ctx->n_words; ++w_idx) {
                uint64_t word = kc_words[w_idx];
                uint32_t base = w_idx << 6;
                while (word != UINT64_C(0)) {
                    uint32_t bit = ns_ctz_u64(word);
                    uint32_t kc = base + bit;
                    int64_t gain = (ctx->weights != NULL)
                        ? ((int64_t)ctx->weights->kc_to_mbon_ach_q8[kc]
                           + (int64_t)ctx->weights->kc_to_mbon_inh_q8[kc])
                        : 1;
                    sum_q14 += (int64_t)ctx->weights_q14[c][kc] * gain;
                    word &= (word - UINT64_C(1));
                }
            }
            /* Convert Q14 weighted sum to Q16 (`* 4`) and divide (truncating toward zero) by gain_den */
            out_scores_q16[c] = ns_clamp_i32((sum_q14 * 4) / gain_den);
        }
    }
    return NS_OK;
}

int ns_valence_predict(
    const ns_valence_t *ctx,
    const uint64_t *kc_words,
    uint32_t *out_best_class,
    int32_t *out_best_score_q16
) {
    int32_t scores[NS_MAX_CLASSES];
    uint32_t c;
    uint32_t best_c = 0u;
    int32_t best_s = INT32_MIN;
    int found = 0;
    int rc;

    if (ctx == NULL || kc_words == NULL || out_best_class == NULL) {
        return NS_ERR_ARG;
    }

    rc = ns_valence_scores(ctx, kc_words, scores);
    if (rc != NS_OK) {
        return rc;
    }

    for (c = 0u; c < ctx->n_classes; ++c) {
        if (ctx->class_active[c] != 0u) {
            if (!found || scores[c] > best_s) {
                best_s = scores[c];
                best_c = c;
                found = 1;
            }
        }
    }
    if (!found) {
        return NS_ERR_EMPTY;
    }

    *out_best_class = best_c;
    if (out_best_score_q16 != NULL) {
        *out_best_score_q16 = best_s;
    }
    return NS_OK;
}

/* -------------------------------------------------------------------------
 * 5. Chordotonal Organ Vibration & Acoustic Spectrum Front-End (`ns_vib_*`)
 * ------------------------------------------------------------------------- */

int ns_vib_init(
    ns_vib_fe_t *ctx,
    uint32_t window_size,
    uint32_t input_shift
) {
    if (ctx == NULL) {
        return NS_ERR_ARG;
    }
    if (window_size != 0u && (window_size < 16u || window_size > NS_VIB_MAX_SAMPLES)) {
        return NS_ERR_BOUNDS;
    }
    if (input_shift > 15u) {
        return NS_ERR_ARG;
    }
    ctx->window_size = window_size;
    ctx->input_shift = input_shift;
    ctx->n_channels = NS_VIB_CHANNELS;
    return NS_OK;
}

static void ns_top4_insert(uint64_t top4[4], uint64_t val) {
    if (val <= top4[3]) {
        return;
    }
    if (val >= top4[0]) {
        top4[3] = top4[2];
        top4[2] = top4[1];
        top4[1] = top4[0];
        top4[0] = val;
    } else if (val >= top4[1]) {
        top4[3] = top4[2];
        top4[2] = top4[1];
        top4[1] = val;
    } else if (val >= top4[2]) {
        top4[3] = top4[2];
        top4[2] = val;
    } else {
        top4[3] = val;
    }
}

int ns_vib_extract_q16(
    const ns_vib_fe_t *ctx,
    const int32_t *samples_q16,
    uint32_t n_samples,
    int32_t *out_features_q16
) {
    int64_t pref[NS_VIB_MAX_SAMPLES + 1u];
    int64_t epref[NS_VIB_MAX_SAMPLES];
    int64_t sum_x = 0;
    int64_t mean_x;
    uint64_t sum_abs_u = UINT64_C(0);
    uint64_t max_abs_u = UINT64_C(0);
    uint64_t sum_pos_u = UINT64_C(0);
    uint64_t sum_neg_u = UINT64_C(0);
    uint64_t max_pos_u = UINT64_C(0);
    uint64_t max_neg_u = UINT64_C(0);
    uint32_t cnt_pos = 0u;
    uint32_t cnt_neg = 0u;
    uint64_t sum_hf = UINT64_C(0);
    uint64_t top4_hf[4] = {UINT64_C(0), UINT64_C(0), UINT64_C(0), UINT64_C(0)};
    uint64_t sum_d2 = UINT64_C(0);
    uint64_t sum_c3 = UINT64_C(0);
    uint64_t sum_c6 = UINT64_C(0);
    uint64_t sum_c12 = UINT64_C(0);
    uint64_t mav_u;
    uint64_t mav_hf;
    uint64_t top4_mean_hf;
    uint64_t mean_pos_exc;
    uint64_t mean_neg_exc;
    uint64_t asym_peak;
    uint64_t asym_exc;
    uint64_t band_mav[6];
    uint32_t m_env;
    uint32_t t;
    uint32_t s;

    if (ctx == NULL || samples_q16 == NULL || out_features_q16 == NULL) {
        return NS_ERR_ARG;
    }
    if (n_samples < 16u || n_samples > NS_VIB_MAX_SAMPLES) {
        return NS_ERR_BOUNDS;
    }
    if (ctx->window_size != 0u && n_samples != ctx->window_size) {
        return NS_ERR_BOUNDS;
    }

    /* Step 1: Remove window DC offset and build prefix sum pref[0..n_samples] */
    for (t = 0u; t < n_samples; ++t) {
        sum_x += (int64_t)samples_q16[t];
    }
    mean_x = ns_div_floor_i64(sum_x, (int64_t)n_samples);
    pref[0] = 0;
    for (t = 0u; t < n_samples; ++t) {
        int64_t ut = (int64_t)samples_q16[t] - mean_x;
        pref[t + 1u] = pref[t] + ut;
    }

    /* Step 2: Resonators, discrete jerk, crest-excess impulse & rub asymmetry */
    for (t = 0u; t < n_samples; ++t) {
        int64_t ut = pref[t + 1u] - pref[t];
        uint64_t au = ns_abs_i64(ut);
        sum_abs_u += au;
        if (au > max_abs_u) {
            max_abs_u = au;
        }
        if (ut > 0) {
            uint64_t pu = (uint64_t)ut;
            sum_pos_u += pu;
            ++cnt_pos;
            if (pu > max_pos_u) {
                max_pos_u = pu;
            }
        } else if (ut < 0) {
            uint64_t nu = ns_abs_i64(ut);
            sum_neg_u += nu;
            ++cnt_neg;
            if (nu > max_neg_u) {
                max_neg_u = nu;
            }
        }

        if (t >= 1u) {
            int64_t ut1 = pref[t] - pref[t - 1u];
            uint64_t hf = ns_abs_i64(ut - ut1);
            sum_hf += hf;
            ns_top4_insert(top4_hf, hf);
            if (t >= 2u) {
                int64_t ut2 = pref[t - 1u] - pref[t - 2u];
                sum_d2 += ns_abs_i64(ut - (2 * ut1) + ut2);
            }
        }
        if (t >= 3u) {
            int64_t ut3 = pref[t - 2u] - pref[t - 3u];
            sum_c3 += ns_abs_i64(ut - ut3);
        }
        if (t >= 6u) {
            int64_t ut6 = pref[t - 5u] - pref[t - 6u];
            sum_c6 += ns_abs_i64(ut - ut6);
        }
        if (t >= 12u) {
            int64_t ut12 = pref[t - 11u] - pref[t - 12u];
            sum_c12 += ns_abs_i64(ut - ut12);
        }
    }

    mav_u = sum_abs_u / (uint64_t)n_samples;
    mav_hf = sum_hf / (uint64_t)(n_samples - 1u);
    top4_mean_hf = (top4_hf[0] + top4_hf[1] + top4_hf[2] + top4_hf[3]) >> 2;

    mean_pos_exc = (cnt_pos > 0u) ? (sum_pos_u / (uint64_t)cnt_pos) : UINT64_C(0);
    mean_neg_exc = (cnt_neg > 0u) ? (sum_neg_u / (uint64_t)cnt_neg) : UINT64_C(0);
    asym_peak = (max_pos_u >= max_neg_u) ? (max_pos_u - max_neg_u) : (max_neg_u - max_pos_u);
    asym_exc = (mean_pos_exc >= mean_neg_exc)
                   ? (mean_pos_exc - mean_neg_exc)
                   : (mean_neg_exc - mean_pos_exc);

    out_features_q16[6]  = ns_clamp_i32((int64_t)((sum_d2 / (uint64_t)(n_samples - 2u)) >> 2));
    out_features_q16[7]  = ns_clamp_i32((int64_t)((sum_c3 / (uint64_t)(n_samples - 3u)) >> 1));
    out_features_q16[8]  = ns_clamp_i32((int64_t)((sum_c6 / (uint64_t)(n_samples - 6u)) >> 1));
    out_features_q16[9]  = ns_clamp_i32((int64_t)((sum_c12 / (uint64_t)(n_samples - 12u)) >> 1));
    out_features_q16[10] = (max_abs_u > (mav_u << 1))
                               ? ns_clamp_i32((int64_t)((max_abs_u - (mav_u << 1)) >> 1))
                               : 0;
    out_features_q16[11] = (top4_hf[0] > (mav_hf << 1))
                               ? ns_clamp_i32((int64_t)((top4_hf[0] - (mav_hf << 1)) >> 1))
                               : 0;
    out_features_q16[12] = (top4_mean_hf > (mav_hf << 1))
                               ? ns_clamp_i32((int64_t)((top4_mean_hf - (mav_hf << 1)) >> 1))
                               : 0;
    out_features_q16[15] = ns_clamp_i32((int64_t)((asym_peak >> 2) + asym_exc));

    /* Step 3: Shift-invariant 6-octave undecimated stationary Haar (`a trous`) */
    for (s = 0u; s < 6u; ++s) {
        uint32_t w = 1u << s;
        uint32_t span = w << 1;
        if (span <= n_samples) {
            uint32_t cnt = n_samples - span + 1u;
            uint64_t sum_d = UINT64_C(0);
            for (t = 0u; t < cnt; ++t) {
                int64_t num = (2 * pref[t + w]) - pref[t] - pref[t + span];
                int64_t dw = ns_shr_floor(num, s + 1u);
                sum_d += ns_abs_i64(dw);
            }
            band_mav[s] = sum_d / (uint64_t)cnt;
        } else {
            band_mav[s] = UINT64_C(0);
        }
    }
    /* Add low-pass boxcar residual below fs/64 into fundamental channel 5 */
    {
        uint32_t w_lp = (n_samples >= 64u) ? 32u : (n_samples >> 1);
        uint32_t shift_lp = (n_samples >= 64u) ? 5u : 3u;
        uint32_t cnt_lp = n_samples - w_lp + 1u;
        uint64_t sum_lp = UINT64_C(0);
        for (t = 0u; t < cnt_lp; ++t) {
            int64_t avg_lp = ns_shr_floor(pref[t + w_lp] - pref[t], shift_lp);
            sum_lp += ns_abs_i64(avg_lp);
        }
        band_mav[5] += sum_lp / (uint64_t)cnt_lp;
    }
    for (s = 0u; s < 6u; ++s) {
        out_features_q16[s] = ns_clamp_i32((int64_t)band_mav[s]);
    }

    /* Step 4: Shift-invariant HF envelope demodulation (AM modulation of octaves 0+1) */
    m_env = n_samples - 3u;
    {
        int64_t sum_env = 0;
        int64_t mean_env;
        uint64_t sum_efast = UINT64_C(0);
        uint64_t sum_emid = UINT64_C(0);
        uint32_t cnt_efast = m_env - 4u + 1u;
        uint32_t w_mid = (m_env >= 13u) ? 6u : 2u;
        uint32_t span_mid = w_mid << 1;
        uint32_t cnt_emid = m_env - span_mid + 1u;

        for (t = 0u; t < m_env; ++t) {
            int64_t d0 = ns_shr_floor((2 * pref[t + 1u]) - pref[t] - pref[t + 2u], 1u);
            int64_t d1 = ns_shr_floor((2 * pref[t + 2u]) - pref[t] - pref[t + 4u], 2u);
            int64_t ev = (int64_t)(ns_abs_i64(d0) + ns_abs_i64(d1));
            epref[t] = ev;
            sum_env += ev;
        }
        mean_env = ns_div_floor_i64(sum_env, (int64_t)m_env);
        {
            int64_t acc_e = 0;
            for (t = 0u; t < m_env; ++t) {
                int64_t eac = epref[t] - mean_env;
                epref[t] = acc_e;
                acc_e += eac;
            }
            epref[m_env] = acc_e;
        }

        for (t = 0u; t < cnt_efast; ++t) {
            int64_t ef = ns_shr_floor((2 * epref[t + 2u]) - epref[t] - epref[t + 4u], 2u);
            sum_efast += ns_abs_i64(ef);
        }
        out_features_q16[13] = ns_clamp_i32((int64_t)(sum_efast / (uint64_t)cnt_efast));

        for (t = 0u; t < cnt_emid; ++t) {
            int64_t num_m = (2 * epref[t + w_mid]) - epref[t] - epref[t + span_mid];
            uint64_t em = ns_abs_i64(num_m) / (uint64_t)span_mid;
            sum_emid += em;
        }
        out_features_q16[14] = ns_clamp_i32((int64_t)(sum_emid / (uint64_t)cnt_emid));
    }

    return NS_OK;
}

int ns_vib_extract_i16(
    const ns_vib_fe_t *ctx,
    const int16_t *samples_i16,
    uint32_t n_samples,
    int32_t *out_features_q16
) {
    int32_t scaled_q16[NS_VIB_MAX_SAMPLES];
    uint32_t t;
    uint32_t sh;

    if (ctx == NULL || samples_i16 == NULL || out_features_q16 == NULL) {
        return NS_ERR_ARG;
    }
    if (n_samples < 16u || n_samples > NS_VIB_MAX_SAMPLES) {
        return NS_ERR_BOUNDS;
    }
    if (ctx->window_size != 0u && n_samples != ctx->window_size) {
        return NS_ERR_BOUNDS;
    }

    sh = ctx->input_shift;
    for (t = 0u; t < n_samples; ++t) {
        int64_t v = (int64_t)samples_i16[t] * ((int64_t)1 << sh);
        scaled_q16[t] = (int32_t)v;
    }
    return ns_vib_extract_q16(ctx, scaled_q16, n_samples, out_features_q16);
}

static int32_t ns_combine_3axis_ch(int32_t a, int32_t b, int32_t c) {
    int32_t hi = a;
    int32_t lo = a;
    int64_t mid;
    if (b > hi) {
        hi = b;
    }
    if (c > hi) {
        hi = c;
    }
    if (b < lo) {
        lo = b;
    }
    if (c < lo) {
        lo = c;
    }
    mid = (int64_t)a + (int64_t)b + (int64_t)c - (int64_t)hi - (int64_t)lo;
    return ns_clamp_i32((int64_t)hi + (mid >> 1) + ((int64_t)lo >> 2));
}

int ns_vib_extract_3axis_i16(
    const ns_vib_fe_t *ctx,
    const int16_t *xyz_interleaved,
    uint32_t n_samples,
    int32_t *out_features_q16
) {
    int32_t axis_buf[NS_VIB_MAX_SAMPLES];
    int32_t fx[NS_VIB_CHANNELS];
    int32_t fy[NS_VIB_CHANNELS];
    int32_t fz[NS_VIB_CHANNELS];
    int64_t scale;
    uint32_t t;
    uint32_t c;
    int rc;

    if (ctx == NULL || xyz_interleaved == NULL || out_features_q16 == NULL) {
        return NS_ERR_ARG;
    }
    if (n_samples < 16u || n_samples > NS_VIB_MAX_SAMPLES) {
        return NS_ERR_BOUNDS;
    }

    scale = (int64_t)1 << ctx->input_shift;
    for (t = 0u; t < n_samples; ++t) {
        axis_buf[t] = (int32_t)((int64_t)xyz_interleaved[3u * t] * scale);
    }
    rc = ns_vib_extract_q16(ctx, axis_buf, n_samples, fx);
    if (rc != NS_OK) {
        return rc;
    }

    for (t = 0u; t < n_samples; ++t) {
        axis_buf[t] = (int32_t)((int64_t)xyz_interleaved[3u * t + 1u] * scale);
    }
    rc = ns_vib_extract_q16(ctx, axis_buf, n_samples, fy);
    if (rc != NS_OK) {
        return rc;
    }

    for (t = 0u; t < n_samples; ++t) {
        axis_buf[t] = (int32_t)((int64_t)xyz_interleaved[3u * t + 2u] * scale);
    }
    rc = ns_vib_extract_q16(ctx, axis_buf, n_samples, fz);
    if (rc != NS_OK) {
        return rc;
    }

    for (c = 0u; c < NS_VIB_CHANNELS; ++c) {
        out_features_q16[c] = ns_combine_3axis_ch(fx[c], fy[c], fz[c]);
    }
    return NS_OK;
}
