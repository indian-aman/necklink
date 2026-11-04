/*
 * NeckSense v1.1.0 - Zero-Heap, Zero-Float ANSI C99 Implementation
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

/* Portable exact floor right-shift by 16 matching Python `v >> 16` */
static int64_t ns_shr16_floor(int64_t v) {
    if (v >= 0) {
        return (int64_t)((uint64_t)v >> 16);
    }
    return -(int64_t)((((uint64_t)(-(v + 1))) >> 16) + UINT64_C(1));
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
    return NS_OK;
}

void ns_antennal_reset(ns_antennal_t *ctx, const int32_t *baseline_q16) {
    uint32_t i;
    if (ctx == NULL) {
        return;
    }
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

    ns_ln_divisive_norm(ctx, pos, out_q16);
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
    return NS_OK;
}

int ns_kenyon_hash(
    const ns_kenyon_t *ctx,
    const int32_t *x_q16,
    uint64_t *out_words
) {
    ns_kc_cand_t heap[NS_MAX_WTA_WINNERS];
    uint32_t state;
    uint32_t j;
    uint32_t c;
    uint32_t k;

    if (ctx == NULL || x_q16 == NULL || out_words == NULL) {
        return NS_ERR_ARG;
    }

    k = ctx->k_winners;
    state = ctx->seed ^ 0x9E3779B9u;
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
 * ------------------------------------------------------------------------- */

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
    uint32_t j;
    if (ctx == NULL) {
        return;
    }
    ctx->steps = 0u;
    for (j = 0u; j < ctx->n_kc; ++j) {
        ctx->weights_q16[j] = NS_Q16_ONE;
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
        uint32_t bit;
        if (word == UINT64_C(0)) {
            continue;
        }
        for (bit = 0u; bit < 64u; ++bit) {
            if ((word & (UINT64_C(1) << bit)) != UINT64_C(0)) {
                sum_w += (int64_t)ctx->weights_q16[base + bit];
                ++active_cnt;
            }
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
            int64_t diff = (int64_t)NS_Q16_ONE - (int64_t)ctx->weights_q16[j];
            ctx->weights_q16[j] += (int32_t)ns_shr16_floor(diff * rec);
        }
    }

    for (w_idx = 0u; w_idx < ctx->n_words; ++w_idx) {
        uint64_t word = kc_words[w_idx];
        uint32_t base = w_idx << 6;
        uint32_t bit;
        if (word == UINT64_C(0)) {
            continue;
        }
        for (bit = 0u; bit < 64u; ++bit) {
            if ((word & (UINT64_C(1) << bit)) != UINT64_C(0)) {
                uint32_t kc = base + bit;
                int64_t cur = (int64_t)ctx->weights_q16[kc];
                sum_w += cur;
                ++active_cnt;
                ctx->weights_q16[kc] = (int32_t)ns_shr16_floor(cur * (int64_t)ctx->keep_q16);
            }
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
 * 4. DAN -> KC -> MBON Associative Valence Classifier (`ns_valence_*`)
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
    return NS_OK;
}

void ns_valence_reset(ns_valence_t *ctx) {
    if (ctx == NULL) {
        return;
    }
    memset(ctx->class_counts, 0, sizeof(ctx->class_counts));
    memset(ctx->class_active, 0, sizeof(ctx->class_active));
    memset(ctx->weights_q16, 0, sizeof(ctx->weights_q16));
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
    int32_t delta_q16;
    int32_t opp_step_q16;
    uint32_t j;
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
    delta_q16 = (int32_t)ns_shr16_floor((int64_t)eta_q16 * (int64_t)reward_q16);
    opp_step_q16 = (int32_t)ns_shr16_floor((int64_t)eta_q16 * (int64_t)ctx->opponent_inh_q16);

    for (j = 0u; j < ctx->n_kc; ++j) {
        int64_t w_cur = (int64_t)ctx->weights_q16[class_id][j];
        ctx->weights_q16[class_id][j] = ns_clamp_i32(ns_shr16_floor(w_cur * (int64_t)keep_q16));
    }

    for (w_idx = 0u; w_idx < ctx->n_words; ++w_idx) {
        uint64_t word = kc_words[w_idx];
        uint32_t base = w_idx << 6;
        uint32_t bit;
        if (word == UINT64_C(0)) {
            continue;
        }
        for (bit = 0u; bit < 64u; ++bit) {
            if ((word & (UINT64_C(1) << bit)) != UINT64_C(0)) {
                uint32_t kc = base + bit;
                int64_t updated = (int64_t)ctx->weights_q16[class_id][kc] + (int64_t)delta_q16;
                ctx->weights_q16[class_id][kc] = ns_clamp_i32(updated);

                if (reward_q16 > 0 && opp_step_q16 > 0) {
                    for (c2 = 0u; c2 < ctx->n_classes; ++c2) {
                        if (c2 != class_id && ctx->class_active[c2] != 0u) {
                            int32_t w_other = ctx->weights_q16[c2][kc];
                            if (w_other > 0) {
                                int32_t dec = (int32_t)ns_shr16_floor(
                                    (int64_t)w_other * (int64_t)opp_step_q16
                                );
                                ctx->weights_q16[c2][kc] = w_other - dec;
                            }
                        }
                    }
                }
            }
        }
    }

    return NS_OK;
}

int ns_valence_scores(
    const ns_valence_t *ctx,
    const uint64_t *kc_words,
    int32_t *out_scores_q16
) {
    uint32_t active_kcs[NS_MAX_KC];
    uint32_t active_cnt = 0u;
    uint32_t w_idx;
    uint32_t c;
    uint32_t i;
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
        uint32_t base = w_idx << 6;
        uint32_t bit;
        if (word == UINT64_C(0)) {
            continue;
        }
        for (bit = 0u; bit < 64u; ++bit) {
            if ((word & (UINT64_C(1) << bit)) != UINT64_C(0)) {
                active_kcs[active_cnt++] = base + bit;
            }
        }
    }

    for (c = 0u; c < ctx->n_classes; ++c) {
        if (ctx->class_active[c] == 0u || active_cnt == 0u) {
            out_scores_q16[c] = 0;
        } else {
            int64_t sum_w = 0;
            for (i = 0u; i < active_cnt; ++i) {
                sum_w += (int64_t)ctx->weights_q16[c][active_kcs[i]];
            }
            out_scores_q16[c] = ns_clamp_i32(sum_w / (int64_t)active_cnt);
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
