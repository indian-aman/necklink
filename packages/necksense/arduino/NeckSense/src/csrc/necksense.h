/*
 * NeckSense v2.0.0 - Zero-Heap, Zero-Float ANSI C99 Sensory & Mushroom Body Core
 *
 * Fixed-point Q16.16 (`1.0 == 65536`) implementation of:
 *   1. Antennal Lobe dual-timescale drift adaptation, Olsen-Wilson LN normalization,
 *      and turbulent plume whiff / intermittency tracking
 *   2. Mushroom Body Calyx sparse binary projection (d_proj=6) + APL top-k WTA hashing
 *   3. 64-bit Popcount overlap similarity search
 *   4. MBON-alpha'3 1-shot anti-Hebbian novelty / anomaly detection (17-bit lossless Q16)
 *   5. 2-Compartment KC-gamma (STM) + KC-alpha/beta (LTM) associative valence classifier
 *   6. Chordotonal Organ 16-channel integer vibration & acoustic spectrum front-end
 *
 * Zero malloc/free. Zero float/double.
 * Default build: ~20 KB total SRAM. `-DNS_TINY=1` MCU build: < 6 KB total SRAM.
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

#define NS_VERSION_MAJOR 2
#define NS_VERSION_MINOR 0
#define NS_VERSION_PATCH 0
#define NS_VERSION_STR   "2.0.0"

/* Fixed-point units: Q16.16 (`1.0 == 65536`) and compact Q14 (`1.0 == 16384`) */
#define NS_Q16_SHIFT 16
#define NS_Q16_ONE   65536
#define NS_Q14_SHIFT 14
#define NS_Q14_ONE   16384

/* Auto-enable NS_TINY (< 6 KB total SRAM) on constrained 8/32-bit MCU targets */
#if !defined(NS_TINY) && (defined(__AVR__) || defined(ESP8266))
#define NS_TINY 1
#endif

#if defined(NS_TINY) && (NS_TINY != 0)
#ifndef NS_MAX_CHANNELS
#define NS_MAX_CHANNELS 24u
#endif
#ifndef NS_MAX_KC
#define NS_MAX_KC 512u
#endif
#ifndef NS_MAX_WTA_WINNERS
#define NS_MAX_WTA_WINNERS 64u
#endif
#ifndef NS_MAX_CLASSES
#define NS_MAX_CLASSES 4u
#endif
#ifndef NS_VIB_MAX_SAMPLES
#define NS_VIB_MAX_SAMPLES 256u
#endif
#else
#ifndef NS_MAX_CHANNELS
#define NS_MAX_CHANNELS 64u
#endif
#ifndef NS_MAX_KC
#define NS_MAX_KC 1024u
#endif
#ifndef NS_MAX_WTA_WINNERS
#define NS_MAX_WTA_WINNERS 256u
#endif
#ifndef NS_MAX_CLASSES
#define NS_MAX_CLASSES 8u
#endif
#ifndef NS_VIB_MAX_SAMPLES
#define NS_VIB_MAX_SAMPLES 512u
#endif
#endif

#define NS_MAX_WORDS    (NS_MAX_KC / 64u)
#define NS_VIB_CHANNELS 16u

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
    int32_t  whiff_thresh_q16;      /* phasic up-crossing threshold */
    int32_t  whiff_alpha_q16;       /* EMA smoothing for whiff_rate & intermittency */
    int32_t  whiff_rate_q16;        /* Q16.16 up-crossings per tick */
    int32_t  intermittency_q16;     /* Q16.16 active-whiff duty cycle in [0, 65536] */
    uint32_t steps;
    uint32_t whiff_count;
    uint8_t  in_whiff;
    uint8_t  reserved[3];
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

int ns_antennal_step_whiff(
    ns_antennal_t *ctx,
    const int32_t *x_q16,
    int32_t *out_q16,
    int32_t *out_whiff_rate_q16
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
 *    Lossless 17-bit representation of [0 .. 65536] in ~50% SRAM.
 * ------------------------------------------------------------------------- */
typedef struct {
    uint32_t n_kc;
    uint32_t n_words;
    int32_t  lr_q16;
    int32_t  keep_q16;
    int32_t  recover_alpha_q16;
    uint32_t steps;
    uint64_t full_mask[NS_MAX_WORDS];   /* bit=1 => exact NS_Q16_ONE (65536) */
    uint16_t weights_u16[NS_MAX_KC];    /* bit=0 => exact 0..65535 */
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
 * 4. 2-Compartment KC-gamma (STM) + KC-alpha/beta (LTM) Valence Classifier
 * ------------------------------------------------------------------------- */
typedef struct {
    uint32_t n_kc;
    uint32_t n_words;
    uint32_t n_classes;
    int32_t  min_lr_q16;
    int32_t  opponent_inh_q16;
    int16_t  ltm_floor_q14;             /* protected KCab floor (default 0.55 == 9011) */
    uint16_t consolidations;
    uint32_t class_counts[NS_MAX_CLASSES];
    uint8_t  class_active[NS_MAX_CLASSES];
    uint64_t ltm_ab[NS_MAX_CLASSES][NS_MAX_WORDS];   /* consolidated KCab bitmask */
    int16_t  weights_q14[NS_MAX_CLASSES][NS_MAX_KC]; /* signed Q14 weights [-2.0, +2.0) */
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

int ns_valence_consolidate(
    ns_valence_t *ctx,
    int32_t thresh_q16
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

/* -------------------------------------------------------------------------
 * 5. Chordotonal Organ Vibration & Acoustic Spectrum Front-End (`ns_vib_fe_t`)
 *    Extracts 16 homogeneous degree-1 spectral, resonator, impulse & AM features.
 * ------------------------------------------------------------------------- */
typedef struct {
    uint32_t window_size;   /* 0 accepts any N in [16 .. NS_VIB_MAX_SAMPLES] */
    uint32_t input_shift;   /* left-shift on int16 raw samples -> Q16 (default 6) */
    uint32_t n_channels;    /* NS_VIB_CHANNELS (16) */
} ns_vib_fe_t;

int ns_vib_init(
    ns_vib_fe_t *ctx,
    uint32_t window_size,
    uint32_t input_shift
);

int ns_vib_extract_q16(
    const ns_vib_fe_t *ctx,
    const int32_t *samples_q16,
    uint32_t n_samples,
    int32_t *out_features_q16
);

int ns_vib_extract_i16(
    const ns_vib_fe_t *ctx,
    const int16_t *samples_i16,
    uint32_t n_samples,
    int32_t *out_features_q16
);

int ns_vib_extract_3axis_i16(
    const ns_vib_fe_t *ctx,
    const int16_t *xyz_interleaved,
    uint32_t n_samples,
    int32_t *out_features_q16
);

#ifdef __cplusplus
}
#endif

#endif /* NECKSENSE_H */
