/*
 * NeckSense v1.0.0 - Zero-Heap, Zero-Float ANSI C99 Olfactory & Mushroom Body Core
 *
 * Fixed-point Q16.16 (`1.0 == 65536`) implementation of:
 *   1. Antennal Lobe dual-timescale drift adaptation + Olsen-Wilson LN normalization
 *   2. Mushroom Body Calyx sparse binary projection (d_proj=6) + APL top-k WTA hashing
 *   3. 64-bit Popcount overlap similarity search
 *   4. MBON-alpha'3 1-shot anti-Hebbian novelty / anomaly detection
 *   5. DAN -> KC -> MBON 1-shot / few-shot associative valence classification
 *
 * Zero malloc/free. Zero float/double. Suitable for ESP32 & Cortex-M MCUs.
 *
 * Copyright (c) 2026 Amandeep Singh Bains
 * SPDX-License-Identifier: MIT
 */

#ifndef NECKSENSE_H
#define NECKSENSE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Q16.16 fixed-point unit (`1.0 == 65536`) */
#define NS_Q16_SHIFT 16
#define NS_Q16_ONE   65536

/* Compile-time static capacities (override via -DNS_MAX_... for tiny SRAM targets) */
#ifndef NS_MAX_CHANNELS
#define NS_MAX_CHANNELS 64u
#endif

#ifndef NS_MAX_KC
#define NS_MAX_KC 2048u
#endif

#define NS_MAX_WORDS (NS_MAX_KC / 64u)

#ifndef NS_MAX_WTA_WINNERS
#define NS_MAX_WTA_WINNERS 512u
#endif

#ifndef NS_MAX_CLASSES
#define NS_MAX_CLASSES 16u
#endif

/* Status codes */
#define NS_OK          0
#define NS_ERR_ARG    -1
#define NS_ERR_BOUNDS -2
#define NS_ERR_EMPTY  -3

/* -------------------------------------------------------------------------
 * 1. Antennal Lobe (`ns_antennal_t`)
 * ------------------------------------------------------------------------- */
typedef struct {
    uint32_t n_channels;
    int32_t  sigma_q16;
    int32_t  gamma_q16;
    uint32_t hill_n;                /* 1 or 2 */
    int32_t  tonic_alpha_q16;
    int32_t  tonic_down_alpha_q16;
    int32_t  tonic_slew_q16;        /* 0 disables upward slew clamp */
    int32_t  phasic_gain_q16;
    int32_t  fast_alpha_q16;
    int32_t  tonic_q16[NS_MAX_CHANNELS];
    int32_t  fast_q16[NS_MAX_CHANNELS];
} ns_antennal_t;

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
);

void ns_antennal_reset(ns_antennal_t *ctx, const int32_t *baseline_q16);

int ns_antennal_normalize(
    const ns_antennal_t *ctx,
    const int32_t *x_q16,
    int32_t *out_q16
);

int ns_antennal_step(
    ns_antennal_t *ctx,
    const int32_t *x_q16,
    int32_t *out_q16
);

/* -------------------------------------------------------------------------
 * 2. Mushroom Body Calyx Sparse Hasher (`ns_kenyon_t`)
 * ------------------------------------------------------------------------- */
typedef struct {
    uint32_t n_inputs;
    uint32_t n_kc;
    uint32_t n_words;
    uint32_t d_proj;
    uint32_t k_winners;
    uint32_t seed;
} ns_kenyon_t;

int ns_kenyon_init(
    ns_kenyon_t *ctx,
    uint32_t n_inputs,
    uint32_t n_kc,
    uint32_t d_proj,
    uint32_t k_winners,
    uint32_t seed
);

int ns_kenyon_hash(
    const ns_kenyon_t *ctx,
    const int32_t *x_q16,
    uint64_t *out_words
);

uint32_t ns_popcount_u64(uint64_t word);

uint32_t ns_popcount_overlap(
    const uint64_t *words_a,
    const uint64_t *words_b,
    uint32_t n_words
);

/* -------------------------------------------------------------------------
 * 3. MBON-alpha'3 Online 1-Shot Novelty Detector (`ns_novelty_t`)
 * ------------------------------------------------------------------------- */
typedef struct {
    uint32_t n_kc;
    uint32_t n_words;
    int32_t  lr_q16;
    int32_t  keep_q16;
    int32_t  recover_alpha_q16;
    uint32_t steps;
    int32_t  weights_q16[NS_MAX_KC];
} ns_novelty_t;

int ns_novelty_init(
    ns_novelty_t *ctx,
    uint32_t n_kc,
    int32_t lr_q16,
    int32_t recover_alpha_q16
);

void ns_novelty_reset(ns_novelty_t *ctx);

int32_t ns_novelty_score(
    const ns_novelty_t *ctx,
    const uint64_t *kc_words
);

int32_t ns_novelty_update_and_score(
    ns_novelty_t *ctx,
    const uint64_t *kc_words
);

/* -------------------------------------------------------------------------
 * 4. DAN -> KC -> MBON Associative Valence Classifier (`ns_valence_t`)
 * ------------------------------------------------------------------------- */
typedef struct {
    uint32_t n_kc;
    uint32_t n_words;
    uint32_t n_classes;
    int32_t  min_lr_q16;
    int32_t  opponent_inh_q16;
    uint32_t class_counts[NS_MAX_CLASSES];
    uint8_t  class_active[NS_MAX_CLASSES];
    int32_t  weights_q16[NS_MAX_CLASSES][NS_MAX_KC];
} ns_valence_t;

int ns_valence_init(
    ns_valence_t *ctx,
    uint32_t n_kc,
    uint32_t n_classes,
    int32_t min_lr_q16,
    int32_t opponent_inh_q16
);

void ns_valence_reset(ns_valence_t *ctx);

int ns_valence_learn(
    ns_valence_t *ctx,
    const uint64_t *kc_words,
    uint32_t class_id,
    int32_t reward_q16
);

int ns_valence_scores(
    const ns_valence_t *ctx,
    const uint64_t *kc_words,
    int32_t *out_scores_q16
);

int ns_valence_predict(
    const ns_valence_t *ctx,
    const uint64_t *kc_words,
    uint32_t *out_best_class,
    int32_t *out_best_score_q16
);

#ifdef __cplusplus
}
#endif

#endif /* NECKSENSE_H */
