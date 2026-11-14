/*
 * NeckSense v3.0.0 - ANSI C99 Self-Test & Cross-Language Parity Emitter
 *
 * Verifies:
 *   1. Deterministic zero-heap sparse binary hashing & exact top-k WTA sparsity
 *   2. Antennal Lobe concentration invariance across 20x scaling
 *   3. 1-shot MBON-alpha'3 novelty habituation (>0.9 unseen, <0.25 after 3x, >0.85 anomaly)
 *   4. Multi-class DAN -> KC -> MBON 1-shot odor classification & valence sign
 *   5. Dual-timescale thermal drift rejection + weak 0.5x plume recovery
 *   6. Chordotonal 16-ch integer vibration fault classification across 10x load scaling
 *   7. 2-Compartment KC-gamma + KC-alpha/beta zero-forgetting continual learning
 *   8. Turbulent plume whiff up-crossing & intermittency tracking
 *   9. BANC v888 connectome weights, KCab-only consolidation mask & NULL fallback
 *
 * Zero dynamic allocation. Zero floating-point types.
 *
 * Copyright (c) 2026 Amandeep Singh Bains
 * SPDX-License-Identifier: MIT
 */

#include "necksense.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

/* Static test workspaces so stack usage remains tiny as on embedded MCUs */
static ns_antennal_t g_ant;
static ns_kenyon_t   g_ken;
static ns_novelty_t  g_nov;
static ns_valence_t  g_val;
static ns_vib_fe_t   g_vib;

#if defined(NS_TINY) && (NS_TINY != 0)
#define TEST_KC    512u
#define TEST_WORDS 8u
#define TEST_K     26u
#define TEST_DIM   16u
#else
#define TEST_KC    1024u
#define TEST_WORDS 16u
#define TEST_K     51u
#define TEST_DIM   32u
#endif

/* 32-point integer sine table in [-1024, +1024] for deterministic motor synthesis */
static const int16_t k_sin32[32] = {
       0,  200,  392,  569,  724,  851,  946, 1004,
    1024, 1004,  946,  851,  724,  569,  392,  200,
       0, -200, -392, -569, -724, -851, -946,-1004,
   -1024,-1004, -946, -851, -724, -569, -392, -200
};

static void synth_motor_window_i16(
    uint32_t mode,
    int32_t amp_scale,
    uint32_t phase_off,
    int16_t *out,
    uint32_t n
) {
    uint32_t t;
    for (t = 0u; t < n; ++t) {
        uint32_t p = t + phase_off;
        int32_t v = 0;
        if (mode == 0u) {
            /* Healthy smooth 1X shaft rotation + tiny 2X */
            v = (int32_t)k_sin32[p & 31u] + ((int32_t)k_sin32[(p * 2u) & 31u] >> 3);
        } else if (mode == 1u) {
            /* Shaft misalignment: dominant 2X + 3X harmonics */
            v = ((int32_t)k_sin32[p & 31u] >> 2)
              + (int32_t)k_sin32[(p * 2u) & 31u]
              + (((int32_t)k_sin32[(p * 3u) & 31u] * 3) >> 2);
        } else if (mode == 2u) {
            /* Outer-race bearing spall: 1X carrier + periodic damped HF impact bursts */
            uint32_t mod = p % 16u;
            int32_t burst = 0;
            if (mod == 0u) {
                burst = 2200;
            } else if (mod == 1u) {
                burst = -1600;
            } else if (mod == 2u) {
                burst = 950;
            } else if (mod == 3u) {
                burst = -450;
            }
            v = ((int32_t)k_sin32[p & 31u] >> 2) + burst;
        } else {
            /* Chipped blade / one-sided mechanical rub: clipped asymmetric buzz */
            int32_t s1 = (int32_t)k_sin32[(p * 2u) & 31u];
            int32_t s4 = (int32_t)k_sin32[(p * 5u) & 31u];
            v = (s1 > 0) ? (s1 + (s4 >> 1)) : (s1 >> 3);
        }
        v = (v * amp_scale) >> 2;
        if (v > 32000) {
            v = 32000;
        } else if (v < -32000) {
            v = -32000;
        }
        out[t] = (int16_t)v;
    }
}

static void format_q16_milli(int32_t q16, char *buf, size_t cap) {
    int neg = (q16 < 0);
    uint64_t mag = neg ? (uint64_t)(-(int64_t)q16) : (uint64_t)q16;
    uint32_t whole = (uint32_t)(mag >> 16);
    uint32_t frac = (uint32_t)(((mag & UINT64_C(0xFFFF)) * UINT64_C(1000) + UINT64_C(32768)) >> 16);
    if (frac >= 1000u) {
        whole += 1u;
        frac -= 1000u;
    }
    snprintf(buf, cap, "%s%" PRIu32 ".%03" PRIu32, neg ? "-" : "", whole, frac);
}

static uint32_t lcg_next(uint32_t *s) {
    *s = (*s * 1664525u) + 1013904223u;
    return *s;
}

static int run_json_parity(void) {
#if defined(NS_TINY) && (NS_TINY != 0)
    fprintf(stderr, "--json-parity requires default NS_MAX_KC >= 1024 build\n");
    return 2;
#else
    int32_t x_in[8] = {55706, 16384, 3277, 0, 39322, 7864, 1311, 26214};
    int32_t step1[8];
    int32_t step2[8];
    uint64_t tag1[16];
    uint64_t tag2[16];
    int32_t nov1, nov2, nov3, nov_after3;
    int32_t scores[4];
    int32_t ltm_scores[4];
    uint64_t v2_tag1[16];
    uint64_t v2_tag2[16];
    int32_t v2_ltm_scores[4];
    int32_t vib_in_q16[64];
    int32_t vib_feat_q16[NS_VIB_CHANNELS];
    uint32_t best_cls = 0u;
    int32_t best_sc = 0;
    uint32_t i;

    if (ns_antennal_init(&g_ant, 8u, 655, 65536, 2u, 655, 1311, 32768, 22938) != NS_OK) {
        return 1;
    }
    if (ns_kenyon_init(&g_ken, 8u, 1024u, 6u, 51u, 42u) != NS_OK) {
        return 1;
    }
    if (ns_novelty_init(&g_nov, 1024u, 36045, 0) != NS_OK) {
        return 1;
    }
    if (ns_valence_init(&g_val, 1024u, 4u, 6554, 9830) != NS_OK) {
        return 1;
    }
    if (ns_vib_init(&g_vib, 64u, 6u) != NS_OK) {
        return 1;
    }

    ns_antennal_step(&g_ant, x_in, step1);
    {
        int32_t x_in2[8];
        for (i = 0u; i < 8u; ++i) {
            x_in2[i] = x_in[i] * 5 + 1200;
        }
        ns_antennal_step(&g_ant, x_in2, step2);
    }

    ns_kenyon_hash(&g_ken, step1, tag1);
    ns_kenyon_hash(&g_ken, step2, tag2);

    nov1 = ns_novelty_update_and_score(&g_nov, tag1);
    nov2 = ns_novelty_update_and_score(&g_nov, tag1);
    nov3 = ns_novelty_update_and_score(&g_nov, tag1);
    nov_after3 = ns_novelty_score(&g_nov, tag1);

    ns_valence_learn(&g_val, tag1, 0u, NS_Q16_ONE);
    {
        int32_t alt[8] = {4000, 62000, 51000, 9000, 2000, 31000, 1000, 5000};
        int32_t alt_norm[8];
        uint64_t tag_alt[16];
        ns_antennal_normalize(&g_ant, alt, alt_norm);
        ns_kenyon_hash(&g_ken, alt_norm, tag_alt);
        ns_valence_learn(&g_val, tag_alt, 1u, NS_Q16_ONE);
    }
    ns_valence_scores(&g_val, tag1, scores);
    ns_valence_predict(&g_val, tag1, &best_cls, &best_sc);

    /* v3.0 BANC v888 parity: consolidate class 0 & 1, then hit class 1 with 15 overlapping trials */
    ns_valence_consolidate(&g_val, 16384);
    for (i = 0u; i < 15u; ++i) {
        ns_valence_learn(&g_val, tag2, 1u, NS_Q16_ONE);
    }
    ns_valence_scores(&g_val, tag1, ltm_scores);

    /* Also emit legacy v2_analytic (weights == NULL) parity vector */
    {
        int32_t alt[8] = {4000, 62000, 51000, 9000, 2000, 31000, 1000, 5000};
        int32_t alt_norm[8];
        uint64_t v2_tag_alt[16];
        ns_kenyon_set_weights(&g_ken, NULL);
        ns_valence_init(&g_val, 1024u, 4u, 6554, 9830);
        ns_valence_set_weights(&g_val, NULL);
        ns_kenyon_hash(&g_ken, step1, v2_tag1);
        ns_kenyon_hash(&g_ken, step2, v2_tag2);
        ns_antennal_normalize(&g_ant, alt, alt_norm);
        ns_kenyon_hash(&g_ken, alt_norm, v2_tag_alt);
        ns_valence_learn(&g_val, v2_tag1, 0u, NS_Q16_ONE);
        ns_valence_learn(&g_val, v2_tag_alt, 1u, NS_Q16_ONE);
        ns_valence_consolidate(&g_val, 16384);
        for (i = 0u; i < 15u; ++i) {
            ns_valence_learn(&g_val, v2_tag2, 1u, NS_Q16_ONE);
        }
        ns_valence_scores(&g_val, v2_tag1, v2_ltm_scores);
    }

    /* v2.0 parity: deterministic 64-sample vibration waveform */
    for (i = 0u; i < 64u; ++i) {
        int32_t s1 = (int32_t)k_sin32[i & 31u] * 32;
        int32_t imp = ((i % 16u) == 0u) ? 98304 : (((i % 16u) == 1u) ? -65536 : 0);
        vib_in_q16[i] = s1 + imp + 4096;
    }
    ns_vib_extract_q16(&g_vib, vib_in_q16, 64u, vib_feat_q16);

    printf("{\n");
    printf("  \"step1_q16\": [");
    for (i = 0u; i < 8u; ++i) {
        printf("%" PRId32 "%s", step1[i], (i + 1u < 8u) ? ", " : "");
    }
    printf("],\n");
    printf("  \"step2_q16\": [");
    for (i = 0u; i < 8u; ++i) {
        printf("%" PRId32 "%s", step2[i], (i + 1u < 8u) ? ", " : "");
    }
    printf("],\n");
    printf("  \"tag1_hex\": \"");
    for (i = 0u; i < 16u; ++i) {
        printf("%016" PRIx64, tag1[i]);
    }
    printf("\",\n");
    printf("  \"overlap_1_2\": %" PRIu32 ",\n", ns_popcount_overlap(tag1, tag2, 16u));
    printf("  \"nov_q16\": [%" PRId32 ", %" PRId32 ", %" PRId32 ", %" PRId32 "],\n",
           nov1, nov2, nov3, nov_after3);
    printf("  \"val_scores_q16\": [%" PRId32 ", %" PRId32 "],\n", scores[0], scores[1]);
    printf("  \"val_best_class\": %" PRIu32 ",\n", best_cls);
    printf("  \"whiff_count\": %" PRIu32 ",\n", g_ant.whiff_count);
    printf("  \"whiff_rate_q16\": %" PRId32 ",\n", g_ant.whiff_rate_q16);
    printf("  \"intermittency_q16\": %" PRId32 ",\n", g_ant.intermittency_q16);
    printf("  \"ltm_scores_q16\": [%" PRId32 ", %" PRId32 "],\n", ltm_scores[0], ltm_scores[1]);
    printf("  \"v2_tag1_hex\": \"");
    for (i = 0u; i < 16u; ++i) {
        printf("%016" PRIx64, v2_tag1[i]);
    }
    printf("\",\n");
    printf("  \"v2_ltm_scores_q16\": [%" PRId32 ", %" PRId32 "],\n", v2_ltm_scores[0], v2_ltm_scores[1]);
    printf("  \"vib_feat_q16\": [");
    for (i = 0u; i < NS_VIB_CHANNELS; ++i) {
        printf("%" PRId32 "%s", vib_feat_q16[i], (i + 1u < NS_VIB_CHANNELS) ? ", " : "");
    }
    printf("]\n");
    printf("}\n");
    return 0;
#endif
}

int main(int argc, char **argv) {
    int passed = 0;
    int total = 0;
    uint32_t i;
    size_t total_sram;

    if (argc > 1 && strcmp(argv[1], "--json-parity") == 0) {
        return run_json_parity();
    }

    total_sram = sizeof(ns_antennal_t) + sizeof(ns_kenyon_t) + sizeof(ns_novelty_t)
               + sizeof(ns_valence_t) + sizeof(ns_vib_fe_t);

    printf("NeckSense v%s ANSI C99 Self-Test (%sZero-Heap, Zero-Float Q16.16)\n",
           NS_VERSION_STR,
#if defined(NS_TINY) && (NS_TINY != 0)
           "NS_TINY=1, "
#else
           ""
#endif
    );
    printf("Static footprints: antennal=%zu B, kenyon=%zu B, novelty=%zu B, valence=%zu B, vib=%zu B (total=%zu B)\n",
           sizeof(ns_antennal_t), sizeof(ns_kenyon_t), sizeof(ns_novelty_t),
           sizeof(ns_valence_t), sizeof(ns_vib_fe_t), total_sram);
    printf("------------------------------------------------------------------\n");

#if defined(NS_TINY) && (NS_TINY != 0)
    if (total_sram >= 6000u) {
        printf("[FAIL] NS_TINY total SRAM %zu B exceeds 6,000 B (< 6 KB) budget\n", total_sram);
        return 1;
    }
#else
    if (sizeof(ns_valence_t) > 32768u) {
        printf("[FAIL] Default ns_valence_t %zu B exceeds 32 KiB budget\n", sizeof(ns_valence_t));
        return 1;
    }
#endif

    /* ---------------------------------------------------------------------
     * Test 1: Zero-heap sparse hashing determinism & top-k WTA sparsity
     * --------------------------------------------------------------------- */
    total++;
    {
        int32_t vec_a[TEST_DIM];
        int32_t vec_near[TEST_DIM];
        int32_t vec_orth[TEST_DIM];
        uint64_t tag_a1[TEST_WORDS];
        uint64_t tag_a2[TEST_WORDS];
        uint64_t tag_near[TEST_WORDS];
        uint64_t tag_orth[TEST_WORDS];
        uint32_t rng = 12345u;
        uint32_t pop_a1;
        uint32_t ov_self;
        uint32_t ov_near;
        uint32_t ov_orth;

        if (ns_kenyon_init(&g_ken, TEST_DIM, TEST_KC, 6u, TEST_K, 2026u) != NS_OK) {
            printf("[FAIL] Test 1: ns_kenyon_init failed\n");
            return 1;
        }
        for (i = 0u; i < TEST_DIM; ++i) {
            int32_t v = (int32_t)((lcg_next(&rng) >> 16) & 0xFFFFu) - 32768;
            int32_t noise = (int32_t)((lcg_next(&rng) >> 20) & 0xFFFu) - 2048;
            int32_t u = (int32_t)((lcg_next(&rng) >> 16) & 0xFFFFu) - 32768;
            vec_a[i] = v;
            vec_near[i] = v + noise;
            vec_orth[i] = u;
        }

        ns_kenyon_hash(&g_ken, vec_a, tag_a1);
        ns_kenyon_hash(&g_ken, vec_a, tag_a2);
        ns_kenyon_hash(&g_ken, vec_near, tag_near);
        ns_kenyon_hash(&g_ken, vec_orth, tag_orth);

        pop_a1 = ns_popcount_overlap(tag_a1, tag_a1, TEST_WORDS);
        ov_self = ns_popcount_overlap(tag_a1, tag_a2, TEST_WORDS);
        ov_near = ns_popcount_overlap(tag_a1, tag_near, TEST_WORDS);
        ov_orth = ns_popcount_overlap(tag_a1, tag_orth, TEST_WORDS);

        if (pop_a1 == TEST_K && ov_self == TEST_K && memcmp(tag_a1, tag_a2, sizeof(tag_a1)) == 0 &&
            ov_near > (TEST_K / 2u) && ov_near > ov_orth + (TEST_K / 4u)) {
            printf("[PASS] Test 1: Deterministic sparse hashing (active=%" PRIu32
                   "/%u, near_ov=%" PRIu32 ", orth_ov=%" PRIu32 ")\n",
                   pop_a1, TEST_KC, ov_near, ov_orth);
            passed++;
        } else {
            printf("[FAIL] Test 1: pop=%" PRIu32 " self=%" PRIu32 " near=%" PRIu32 " orth=%" PRIu32 "\n",
                   pop_a1, ov_self, ov_near, ov_orth);
            return 1;
        }
    }

    /* ---------------------------------------------------------------------
     * Test 2: Concentration invariance across 20x amplitude scaling
     * --------------------------------------------------------------------- */
    total++;
    {
        const int32_t odor_1x[8] = {52429, 19661, 6554, 655, 39322, 9830, 1311, 26214};
        int32_t odor_20x[8];
        int32_t r_1x[8];
        int32_t r_20x[8];
        uint64_t tag_1x[TEST_WORDS];
        uint64_t tag_20x[TEST_WORDS];
        int32_t max_diff_q16 = 0;
        uint32_t ov_20x;

        if (ns_antennal_init(&g_ant, 8u, 64, 65536, 2u, 655, 1311, 32768, 22938) != NS_OK) {
            printf("[FAIL] Test 2: ns_antennal_init failed\n");
            return 1;
        }
        if (ns_kenyon_init(&g_ken, 8u, TEST_KC, 6u, TEST_K, 42u) != NS_OK) {
            printf("[FAIL] Test 2: ns_kenyon_init failed\n");
            return 1;
        }

        for (i = 0u; i < 8u; ++i) {
            odor_20x[i] = odor_1x[i] * 20;
        }
        ns_antennal_normalize(&g_ant, odor_1x, r_1x);
        ns_antennal_normalize(&g_ant, odor_20x, r_20x);

        for (i = 0u; i < 8u; ++i) {
            int32_t d = r_1x[i] - r_20x[i];
            if (d < 0) {
                d = -d;
            }
            if (d > max_diff_q16) {
                max_diff_q16 = d;
            }
        }

        ns_kenyon_hash(&g_ken, r_1x, tag_1x);
        ns_kenyon_hash(&g_ken, r_20x, tag_20x);
        ov_20x = ns_popcount_overlap(tag_1x, tag_20x, TEST_WORDS);

        if (max_diff_q16 <= 32 && ov_20x >= (TEST_K - 2u)) {
            char diff_str[32];
            format_q16_milli(max_diff_q16, diff_str, sizeof(diff_str));
            printf("[PASS] Test 2: 20x concentration invariance (max_channel_err=%s, KC_overlap=%" PRIu32 "/%u)\n",
                   diff_str, ov_20x, TEST_K);
            passed++;
        } else {
            printf("[FAIL] Test 2: max_diff_q16=%" PRId32 " ov_20x=%" PRIu32 "\n", max_diff_q16, ov_20x);
            return 1;
        }
    }

    /* ---------------------------------------------------------------------
     * Test 3: 1-shot MBON-alpha'3 novelty habituation & anomaly detection
     * --------------------------------------------------------------------- */
    total++;
    {
        int32_t pat_normal[TEST_DIM];
        int32_t pat_anom[TEST_DIM];
        uint64_t tag_normal[TEST_WORDS];
        uint64_t tag_anom[TEST_WORDS];
        int32_t s_unseen;
        int32_t s_exp2;
        int32_t s_exp3;
        int32_t s_after3;
        int32_t s_anom;
        uint32_t half_d = TEST_DIM >> 1;
        char b_unseen[24], b_after3[24], b_anom[24];

        if (ns_kenyon_init(&g_ken, TEST_DIM, TEST_KC, 6u, TEST_K, 99u) != NS_OK) {
            return 1;
        }
        if (ns_novelty_init(&g_nov, TEST_KC, 36045, 0) != NS_OK) {
            return 1;
        }

        for (i = 0u; i < TEST_DIM; ++i) {
            pat_normal[i] = (i < half_d) ? (int32_t)((i + 1u) * 4096u) : -8192;
            pat_anom[i]   = (i >= half_d) ? (int32_t)((TEST_DIM - i) * 4096u) : -8192;
        }
        ns_kenyon_hash(&g_ken, pat_normal, tag_normal);
        ns_kenyon_hash(&g_ken, pat_anom, tag_anom);

        s_unseen = ns_novelty_update_and_score(&g_nov, tag_normal);
        s_exp2   = ns_novelty_update_and_score(&g_nov, tag_normal);
        s_exp3   = ns_novelty_update_and_score(&g_nov, tag_normal);
        s_after3 = ns_novelty_score(&g_nov, tag_normal);
        s_anom   = ns_novelty_update_and_score(&g_nov, tag_anom);

        (void)s_exp2;
        format_q16_milli(s_unseen, b_unseen, sizeof(b_unseen));
        format_q16_milli(s_after3, b_after3, sizeof(b_after3));
        format_q16_milli(s_anom, b_anom, sizeof(b_anom));

        if (s_unseen == NS_Q16_ONE && s_exp3 < 16384 && s_after3 < 16384 && s_anom > 55705) {
            printf("[PASS] Test 3: 1-shot MBON novelty (unseen=%s == 1.000, after_3=%s < 0.25, anomaly=%s > 0.85)\n",
                   b_unseen, b_after3, b_anom);
            passed++;
        } else {
            printf("[FAIL] Test 3: unseen=%" PRId32 " exp3=%" PRId32 " after3=%" PRId32 " anom=%" PRId32 "\n",
                   s_unseen, s_exp3, s_after3, s_anom);
            return 1;
        }
    }

    /* ---------------------------------------------------------------------
     * Test 4: Multi-class associative odor classification across 20x scaling
     * --------------------------------------------------------------------- */
    total++;
    {
        const int32_t templates[4][8] = {
            {58982, 22938,  6554,  1311, 32768,  3277,   655, 13107},
            { 5243, 55706, 45875,  9830,  3277, 26214,  1311,  6554},
            {29491,  9830,  5243, 52429, 49152,  6554, 19661,  3277},
            { 3277,  6554, 49152, 13107,  6554, 58982, 42598, 52429}
        };
        uint32_t c;
        uint32_t correct = 0u;
        int32_t aversive_score = 0;

        if (ns_antennal_init(&g_ant, 8u, 128, 65536, 2u, 655, 1311, 32768, 22938) != NS_OK) {
            return 1;
        }
        if (ns_kenyon_init(&g_ken, 8u, TEST_KC, 6u, TEST_K, 42u) != NS_OK) {
            return 1;
        }
        if (ns_valence_init(&g_val, TEST_KC, 4u, 6554, 9830) != NS_OK) {
            return 1;
        }

        for (c = 0u; c < 4u; ++c) {
            int32_t norm_vec[8];
            uint64_t tag[TEST_WORDS];
            ns_antennal_normalize(&g_ant, templates[c], norm_vec);
            ns_kenyon_hash(&g_ken, norm_vec, tag);
            ns_valence_learn(&g_val, tag, c, NS_Q16_ONE);
        }

        for (c = 0u; c < 4u; ++c) {
            int32_t probe[8];
            int32_t norm_probe[8];
            uint64_t tag_probe[TEST_WORDS];
            uint32_t pred_cls = 99u;
            int32_t pred_sc = 0;
            for (i = 0u; i < 8u; ++i) {
                probe[i] = templates[c][i] * 20 + (int32_t)((i & 1u) ? 900 : -600);
            }
            ns_antennal_normalize(&g_ant, probe, norm_probe);
            ns_kenyon_hash(&g_ken, norm_probe, tag_probe);
            if (ns_valence_predict(&g_val, tag_probe, &pred_cls, &pred_sc) == NS_OK && pred_cls == c) {
                correct++;
            }
        }

        /* Verify negative-valence aversive conditioning */
        ns_valence_reset(&g_val);
        {
            int32_t norm0[8];
            uint64_t tag0[TEST_WORDS];
            int32_t av_scores[4];
            ns_antennal_normalize(&g_ant, templates[0], norm0);
            ns_kenyon_hash(&g_ken, norm0, tag0);
            ns_valence_learn(&g_val, tag0, 0u, -NS_Q16_ONE);
            ns_valence_scores(&g_val, tag0, av_scores);
            aversive_score = av_scores[0];
        }

        if (correct == 4u && aversive_score < -60000) {
            char av_buf[24];
            format_q16_milli(aversive_score, av_buf, sizeof(av_buf));
            printf("[PASS] Test 4: 1-shot 4-class odor classification at 20x (%" PRIu32 "/4 correct, aversive=%s)\n",
                   correct, av_buf);
            passed++;
        } else {
            printf("[FAIL] Test 4: correct=%" PRIu32 "/4 aversive=%" PRId32 "\n", correct, aversive_score);
            return 1;
        }
    }

    /* ---------------------------------------------------------------------
     * Test 5: Dual-timescale thermal drift adaptation (`ns_antennal_step`)
     * --------------------------------------------------------------------- */
    total++;
    {
        const int32_t plume_ref[8] = {45875, 13107, 3277, 655, 32768, 6554, 1311, 19661};
        int32_t ref_norm[8];
        int32_t unadapted_norm[8];
        int32_t drifted_sample[8];
        int32_t step_out[8];
        uint64_t tag_ref[TEST_WORDS];
        uint64_t tag_unadapted[TEST_WORDS];
        uint64_t tag_drifted[TEST_WORDS];
        uint32_t step_idx;
        uint32_t ov_drift;
        uint32_t ov_unadapted;

        ns_antennal_init(&g_ant, 8u, 256, 65536, 2u, 3277, 3277, 16384, 26214);
        ns_kenyon_init(&g_ken, 8u, TEST_KC, 6u, TEST_K, 42u);

        ns_antennal_normalize(&g_ant, plume_ref, ref_norm);
        ns_kenyon_hash(&g_ken, ref_norm, tag_ref);

        for (step_idx = 0u; step_idx < 300u; ++step_idx) {
            int32_t drift_val = (int32_t)(step_idx * 900u);
            for (i = 0u; i < 8u; ++i) {
                drifted_sample[i] = drift_val + (int32_t)(i * 512u);
            }
            ns_antennal_step(&g_ant, drifted_sample, step_out);
        }

        for (i = 0u; i < 8u; ++i) {
            drifted_sample[i] = (int32_t)(300u * 900u) + (int32_t)(i * 512u) + (plume_ref[i] >> 1);
        }
        ns_antennal_normalize(&g_ant, drifted_sample, unadapted_norm);
        ns_kenyon_hash(&g_ken, unadapted_norm, tag_unadapted);
        ov_unadapted = ns_popcount_overlap(tag_ref, tag_unadapted, TEST_WORDS);

        ns_antennal_step(&g_ant, drifted_sample, step_out);
        ns_kenyon_hash(&g_ken, step_out, tag_drifted);
        ov_drift = ns_popcount_overlap(tag_ref, tag_drifted, TEST_WORDS);

        if (ov_drift >= ((TEST_K * 3u) / 5u) && ov_drift > ov_unadapted + (TEST_K / 5u)) {
            printf("[PASS] Test 5: Thermal baseline drift rejection + 0.5x plume recovery (KC_overlap=%" PRIu32
                   "/%u vs unadapted=%" PRIu32 "/%u)\n",
                   ov_drift, TEST_K, ov_unadapted, TEST_K);
            passed++;
        } else {
            printf("[FAIL] Test 5: ov_drift=%" PRIu32 "/%u unadapted=%" PRIu32 "/%u\n",
                   ov_drift, TEST_K, ov_unadapted, TEST_K);
            return 1;
        }
    }

    /* ---------------------------------------------------------------------
     * Test 6: Chordotonal 16-ch integer vibration fault detection across 10x load
     * --------------------------------------------------------------------- */
    total++;
    {
        int16_t win_i16[128];
        int32_t feat_q16[NS_VIB_CHANNELS];
        int32_t norm_q16[NS_VIB_CHANNELS];
        uint64_t tag[TEST_WORDS];
        uint32_t mode;
        uint32_t vib_hits = 0u;

        ns_vib_init(&g_vib, 128u, 6u);
        ns_antennal_init(&g_ant, NS_VIB_CHANNELS, 256, 65536, 2u, 655, 1311, 32768, 22938);
        ns_kenyon_init(&g_ken, NS_VIB_CHANNELS, TEST_KC, 6u, TEST_K, 77u);
        ns_valence_init(&g_val, TEST_KC, 4u, 6554, 9830);

        /* 1-shot learn 4 mechanical states at 1x load (amp_scale = 4) */
        for (mode = 0u; mode < 4u; ++mode) {
            synth_motor_window_i16(mode, 4, 0u, win_i16, 128u);
            ns_vib_extract_i16(&g_vib, win_i16, 128u, feat_q16);
            ns_antennal_normalize(&g_ant, feat_q16, norm_q16);
            ns_kenyon_hash(&g_ken, norm_q16, tag);
            ns_valence_learn(&g_val, tag, mode, NS_Q16_ONE);
        }

        /* Test at 10x motor amplitude (amp_scale = 40) + phase shift */
        for (mode = 0u; mode < 4u; ++mode) {
            uint32_t pred = 99u;
            synth_motor_window_i16(mode, 40, 5u, win_i16, 128u);
            ns_vib_extract_i16(&g_vib, win_i16, 128u, feat_q16);
            ns_antennal_normalize(&g_ant, feat_q16, norm_q16);
            ns_kenyon_hash(&g_ken, norm_q16, tag);
            if (ns_valence_predict(&g_val, tag, &pred, NULL) == NS_OK && pred == mode) {
                vib_hits++;
            }
        }

        if (vib_hits == 4u) {
            printf("[PASS] Test 6: 1-shot chordotonal vibration fault ID at 10x load (%" PRIu32 "/4 modes)\n",
                   vib_hits);
            passed++;
        } else {
            printf("[FAIL] Test 6: vib_hits=%" PRIu32 "/4\n", vib_hits);
            return 1;
        }
    }

    /* ---------------------------------------------------------------------
     * Test 7: 2-Compartment KC-gamma + KC-alpha/beta anti-forgetting LTM
     * --------------------------------------------------------------------- */
    total++;
    {
        uint64_t cls_tags[4][TEST_WORDS];
        uint32_t c;
        uint32_t rep;
        uint32_t retained = 0u;
        int32_t sc_after[4];

        ns_kenyon_init(&g_ken, 16u, TEST_KC, 6u, TEST_K, 314u);
        /* Aggressive opponent inhibition (0.45 = 29491) to stress-test forgetting */
        ns_valence_init(&g_val, TEST_KC, 4u, 13107, 29491);

        for (c = 0u; c < 4u; ++c) {
            int32_t pat[16];
            for (i = 0u; i < 16u; ++i) {
                /* Shared background in channels 0..3 + class-specific channels */
                pat[i] = (i < 4u) ? 45000 : ((i == (4u + c * 3u) || i == (5u + c * 3u)) ? 60000 : 1000);
            }
            ns_kenyon_hash(&g_ken, pat, cls_tags[c]);
        }

        /* Learn early classes 0 & 1 (1-shot) and consolidate into KCab LTM */
        ns_valence_learn(&g_val, cls_tags[0], 0u, NS_Q16_ONE);
        ns_valence_learn(&g_val, cls_tags[1], 1u, NS_Q16_ONE);
        ns_valence_consolidate(&g_val, 16384);

        /* Hammer classes 2 & 3 sequentially for 80 total trials without replaying 0 or 1 */
        for (rep = 0u; rep < 40u; ++rep) {
            ns_valence_learn(&g_val, cls_tags[2], 2u, NS_Q16_ONE);
            ns_valence_learn(&g_val, cls_tags[3], 3u, NS_Q16_ONE);
        }

        for (c = 0u; c < 4u; ++c) {
            uint32_t best_c = 99u;
            if (ns_valence_predict(&g_val, cls_tags[c], &best_c, NULL) == NS_OK && best_c == c) {
                retained++;
            }
        }
        ns_valence_scores(&g_val, cls_tags[0], sc_after);

        if (retained == 4u && sc_after[0] >= 35000) {
            char s0_buf[24];
            format_q16_milli(sc_after[0], s0_buf, sizeof(s0_buf));
            printf("[PASS] Test 7: KC-gamma + KC-alpha/beta LTM consolidation (%" PRIu32 "/4 retained, early_score=%s)\n",
                   retained, s0_buf);
            passed++;
        } else {
            printf("[FAIL] Test 7: retained=%" PRIu32 "/4 sc0=%" PRId32 "\n", retained, sc_after[0]);
            return 1;
        }
    }

    /* ---------------------------------------------------------------------
     * Test 8: Turbulent plume whiff rate & intermittency vs static cloud
     * --------------------------------------------------------------------- */
    total++;
    {
        const int32_t odor[8] = {32768, 16384, 8192, 2048, 24576, 4096, 1024, 12288};
        const int32_t zero_air[8] = {0, 0, 0, 0, 0, 0, 0, 0};
        int32_t out_q16[8];
        uint32_t t;
        uint32_t static_whiffs;
        int32_t static_rate;
        uint32_t turb_whiffs;
        int32_t turb_rate;

        /* Regime A: Continuous static cloud for 80 ticks */
        ns_antennal_init(&g_ant, 8u, 655, 65536, 2u, 655, 1311, 32768, 22938);
        for (t = 0u; t < 80u; ++t) {
            ns_antennal_step(&g_ant, odor, out_q16);
        }
        static_whiffs = g_ant.whiff_count;
        static_rate = g_ant.whiff_rate_q16;

        /* Regime B: Turbulent pulsatile plume (2 ticks odor, 3 ticks clean air) */
        ns_antennal_reset(&g_ant, NULL);
        for (t = 0u; t < 80u; ++t) {
            const int32_t *inp = ((t % 5u) < 2u) ? odor : zero_air;
            ns_antennal_step(&g_ant, inp, out_q16);
        }
        turb_whiffs = g_ant.whiff_count;
        turb_rate = g_ant.whiff_rate_q16;

        if (static_whiffs == 1u && turb_whiffs >= 14u && turb_rate > static_rate * 10 + 4000) {
            char tr_buf[24];
            format_q16_milli(turb_rate, tr_buf, sizeof(tr_buf));
            printf("[PASS] Test 8: Plume whiff intermittency (turbulent=%" PRIu32 " whiffs rate=%s vs static=%" PRIu32 ")\n",
                   turb_whiffs, tr_buf, static_whiffs);
            passed++;
        } else {
            printf("[FAIL] Test 8: static=%" PRIu32 " (rate=%" PRId32 ") turb=%" PRIu32 " (rate=%" PRId32 ")\n",
                   static_whiffs, static_rate, turb_whiffs, turb_rate);
            return 1;
        }
    }

    /* ---------------------------------------------------------------------
     * Test 9: BANC v888 connectome weights, KCab-only LTM mask & NULL mode
     * --------------------------------------------------------------------- */
    total++;
    {
        int32_t probe[TEST_DIM];
        uint64_t tag_banc[TEST_WORDS];
        uint64_t tag_v2[TEST_WORDS];
        uint64_t tag_banc2[TEST_WORDS];
        uint32_t ab_locked = 0u;
        uint32_t gamma_locked = 0u;
        uint32_t w_idx;

        if (ns_kenyon_init(&g_ken, TEST_DIM, TEST_KC, 6u, TEST_K, 42u) != NS_OK ||
            ns_valence_init(&g_val, TEST_KC, 2u, 6554, 9830) != NS_OK) {
            return 1;
        }
        for (i = 0u; i < TEST_DIM; ++i) {
            probe[i] = (int32_t)(((i * 7u + 3u) % 19u) * 3200u);
        }

        ns_kenyon_hash(&g_ken, probe, tag_banc);
        ns_kenyon_set_weights(&g_ken, NULL);
        ns_kenyon_hash(&g_ken, probe, tag_v2);
        ns_kenyon_set_weights(&g_ken, ns_banc_default_weights());
        ns_kenyon_hash(&g_ken, probe, tag_banc2);

        ns_valence_learn(&g_val, tag_banc, 0u, NS_Q16_ONE);
        ns_valence_consolidate(&g_val, 16384);
        for (w_idx = 0u; w_idx < TEST_WORDS; ++w_idx) {
            uint64_t ltm_w = g_val.ltm_ab[0][w_idx];
            uint64_t ab_w = g_val.weights->kc_is_ab_mask_u64[w_idx];
            ab_locked += ns_popcount_u64(ltm_w & ab_w);
            gamma_locked += ns_popcount_u64(ltm_w & ~ab_w);
        }

        if (g_ken.weights == ns_banc_default_weights() &&
            memcmp(tag_banc, tag_banc2, sizeof(tag_banc)) == 0 &&
            memcmp(tag_banc, tag_v2, sizeof(tag_banc)) != 0 &&
            ns_popcount_overlap(tag_v2, tag_v2, TEST_WORDS) == TEST_K &&
            ab_locked > 0u && gamma_locked == 0u) {
            printf("[PASS] Test 9: BANC v888 connectome weights & KCab-only LTM mask (KCab_locked=%" PRIu32
                   ", KCg_locked=%" PRIu32 ")\n", ab_locked, gamma_locked);
            passed++;
        } else {
            printf("[FAIL] Test 9: ab_locked=%" PRIu32 " gamma_locked=%" PRIu32 "\n",
                   ab_locked, gamma_locked);
            return 1;
        }
    }

    printf("------------------------------------------------------------------\n");
    {
        /* Micro-benchmark: 20,000 full pipeline ticks (antennal + kenyon + novelty) */
        int32_t sample_q16[16];
        int32_t norm_q16[16];
        uint64_t words[TEST_WORDS];
        uint32_t iter;
        uint32_t sink = 0u;
        clock_t t0;
        clock_t dt;
        uint64_t total_ns;
        uint32_t ns_per_op;

        ns_antennal_init(&g_ant, 16u, 512, 65536, 2u, 655, 1311, 32768, 22938);
        ns_kenyon_init(&g_ken, 16u, TEST_KC, 6u, TEST_K, 42u);
        ns_novelty_init(&g_nov, TEST_KC, 36045, 16);
        for (i = 0u; i < 16u; ++i) {
            sample_q16[i] = (int32_t)((i + 1u) * 4096u);
        }

        t0 = clock();
        for (iter = 0u; iter < 20000u; ++iter) {
            sample_q16[iter & 15u] = (int32_t)(((iter * 2654435761u) >> 16) & 0xFFFFu);
            ns_antennal_step(&g_ant, sample_q16, norm_q16);
            ns_kenyon_hash(&g_ken, norm_q16, words);
            sink ^= (uint32_t)ns_novelty_update_and_score(&g_nov, words);
        }
        dt = clock() - t0;
        total_ns = ((uint64_t)dt * UINT64_C(1000000000)) / (uint64_t)CLOCKS_PER_SEC;
        ns_per_op = (uint32_t)(total_ns / UINT64_C(20000));
        printf("C99 pipeline throughput (D=16, M=%u): %" PRIu32 " ns/sample (guard=%" PRIu32 ")\n",
               TEST_KC, ns_per_op, sink & 1u);
    }
    printf("All %d/%d ANSI C99 NeckSense self-tests PASSED.\n", passed, total);
    return 0;
}
