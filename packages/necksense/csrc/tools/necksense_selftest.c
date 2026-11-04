/*
 * NeckSense v1.0.0 - ANSI C99 Self-Test & Cross-Language Parity Emitter
 *
 * Verifies:
 *   1. Deterministic zero-heap sparse binary hashing & exact top-k WTA sparsity
 *   2. Antennal Lobe concentration invariance across 20x scaling + drift tracking
 *   3. 1-shot MBON-alpha'3 novelty habituation (>0.9 unseen, <0.25 after 3x, >0.85 anomaly)
 *   4. Multi-class DAN -> KC -> MBON 1-shot odor classification & valence sign
 *
 * Zero malloc/free. Zero float/double.
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
    int32_t x_in[8] = {55706, 16384, 3277, 0, 39322, 7864, 1311, 26214};
    int32_t step1[8];
    int32_t step2[8];
    uint64_t tag1[16];
    uint64_t tag2[16];
    int32_t nov1, nov2, nov3, nov_after3;
    int32_t scores[4];
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
    printf("  \"val_best_class\": %" PRIu32 "\n", best_cls);
    printf("}\n");
    return 0;
}

int main(int argc, char **argv) {
    int passed = 0;
    int total = 0;
    uint32_t i;

    if (argc > 1 && strcmp(argv[1], "--json-parity") == 0) {
        return run_json_parity();
    }

    printf("NeckSense v1.0.0 ANSI C99 Self-Test (Zero-Heap, Zero-Float Q16.16)\n");
    printf("Static footprints: antennal=%zu B, kenyon=%zu B, novelty=%zu B, valence=%zu B\n",
           sizeof(ns_antennal_t), sizeof(ns_kenyon_t), sizeof(ns_novelty_t), sizeof(ns_valence_t));
    printf("------------------------------------------------------------------\n");

    /* ---------------------------------------------------------------------
     * Test 1: Zero-heap sparse hashing determinism & top-k WTA sparsity
     * --------------------------------------------------------------------- */
    total++;
    {
        int32_t vec_a[32];
        int32_t vec_near[32];
        int32_t vec_orth[32];
        uint64_t tag_a1[16];
        uint64_t tag_a2[16];
        uint64_t tag_near[16];
        uint64_t tag_orth[16];
        uint32_t rng = 12345u;
        uint32_t pop_a1;
        uint32_t ov_self;
        uint32_t ov_near;
        uint32_t ov_orth;

        if (ns_kenyon_init(&g_ken, 32u, 1024u, 6u, 51u, 2026u) != NS_OK) {
            printf("[FAIL] Test 1: ns_kenyon_init failed\n");
            return 1;
        }
        for (i = 0u; i < 32u; ++i) {
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

        pop_a1 = ns_popcount_overlap(tag_a1, tag_a1, 16u);
        ov_self = ns_popcount_overlap(tag_a1, tag_a2, 16u);
        ov_near = ns_popcount_overlap(tag_a1, tag_near, 16u);
        ov_orth = ns_popcount_overlap(tag_a1, tag_orth, 16u);

        if (pop_a1 == 51u && ov_self == 51u && memcmp(tag_a1, tag_a2, sizeof(tag_a1)) == 0 &&
            ov_near > 30u && ov_near > ov_orth + 15u) {
            printf("[PASS] Test 1: Deterministic sparse hashing (active=%" PRIu32
                   "/1024, near_ov=%" PRIu32 ", orth_ov=%" PRIu32 ")\n",
                   pop_a1, ov_near, ov_orth);
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
        uint64_t tag_1x[16];
        uint64_t tag_20x[16];
        int32_t max_diff_q16 = 0;
        uint32_t ov_20x;

        if (ns_antennal_init(&g_ant, 8u, 64, 65536, 2u, 655, 1311, 32768, 22938) != NS_OK) {
            printf("[FAIL] Test 2: ns_antennal_init failed\n");
            return 1;
        }
        if (ns_kenyon_init(&g_ken, 8u, 1024u, 6u, 51u, 42u) != NS_OK) {
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
        ov_20x = ns_popcount_overlap(tag_1x, tag_20x, 16u);

        if (max_diff_q16 <= 32 && ov_20x >= 49u) {
            char diff_str[32];
            format_q16_milli(max_diff_q16, diff_str, sizeof(diff_str));
            printf("[PASS] Test 2: 20x concentration invariance (max_channel_err=%s, KC_overlap=%" PRIu32 "/51)\n",
                   diff_str, ov_20x);
            passed++;
        } else {
            printf("[FAIL] Test 2: max_diff_q16=%" PRId32 " ov_20x=%" PRIu32 "\n", max_diff_q16, ov_20x);
            return 1;
        }
    }

    /* ---------------------------------------------------------------------
     * Test 3: 1-shot MBON-alpha'3 novelty habituation & anomaly detection
     *   - unseen pattern: novelty > 0.90 (58982 in Q16.16)
     *   - after 3 exposures: novelty < 0.25 (16384 in Q16.16)
     *   - orthogonal anomaly: novelty > 0.85 (55705 in Q16.16)
     * --------------------------------------------------------------------- */
    total++;
    {
        int32_t pat_normal[32];
        int32_t pat_anom[32];
        uint64_t tag_normal[16];
        uint64_t tag_anom[16];
        int32_t s_unseen;
        int32_t s_exp2;
        int32_t s_exp3;
        int32_t s_after3;
        int32_t s_anom;
        char b_unseen[24], b_after3[24], b_anom[24];

        if (ns_kenyon_init(&g_ken, 32u, 1024u, 6u, 51u, 99u) != NS_OK) {
            return 1;
        }
        /* lr = 0.55 => 36045 in Q16.16 */
        if (ns_novelty_init(&g_nov, 1024u, 36045, 0) != NS_OK) {
            return 1;
        }

        for (i = 0u; i < 32u; ++i) {
            pat_normal[i] = (i < 16u) ? (int32_t)((i + 1u) * 4096u) : -8192;
            pat_anom[i]   = (i >= 16u) ? (int32_t)((32u - i) * 4096u) : -8192;
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

        if (s_unseen > 58982 && s_exp3 < 16384 && s_after3 < 16384 && s_anom > 55705) {
            printf("[PASS] Test 3: 1-shot MBON novelty (unseen=%s > 0.90, after_3=%s < 0.25, anomaly=%s > 0.85)\n",
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
        int32_t aversive_scores[5];

        if (ns_antennal_init(&g_ant, 8u, 128, 65536, 2u, 655, 1311, 32768, 22938) != NS_OK) {
            return 1;
        }
        if (ns_kenyon_init(&g_ken, 8u, 1024u, 6u, 51u, 42u) != NS_OK) {
            return 1;
        }
        if (ns_valence_init(&g_val, 1024u, 5u, 6554, 9830) != NS_OK) {
            return 1;
        }

        /* 1-shot training at 1x reference concentration */
        for (c = 0u; c < 4u; ++c) {
            int32_t norm_vec[8];
            uint64_t tag[16];
            ns_antennal_normalize(&g_ant, templates[c], norm_vec);
            ns_kenyon_hash(&g_ken, norm_vec, tag);
            ns_valence_learn(&g_val, tag, c, NS_Q16_ONE);
        }

        /* Also test aversive negative-valence conditioning on compartment 4 */
        {
            int32_t norm0[8];
            uint64_t tag0[16];
            ns_antennal_normalize(&g_ant, templates[0], norm0);
            ns_kenyon_hash(&g_ken, norm0, tag0);
            ns_valence_learn(&g_val, tag0, 4u, -NS_Q16_ONE);
            ns_valence_scores(&g_val, tag0, aversive_scores);
        }

        /* Query at 20x amplitude + channel perturbation */
        for (c = 0u; c < 4u; ++c) {
            int32_t probe[8];
            int32_t norm_probe[8];
            uint64_t tag_probe[16];
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

        if (correct == 4u && aversive_scores[4] < -45000) {
            char av_buf[24];
            format_q16_milli(aversive_scores[4], av_buf, sizeof(av_buf));
            printf("[PASS] Test 4: 1-shot 4-class odor classification at 20x (%" PRIu32 "/4 correct, aversive=%s)\n",
                   correct, av_buf);
            passed++;
        } else {
            printf("[FAIL] Test 4: correct=%" PRIu32 "/4 aversive=%" PRId32 "\n", correct, aversive_scores[4]);
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
        int32_t drifted_sample[8];
        int32_t step_out[8];
        uint64_t tag_ref[16];
        uint64_t tag_drifted[16];
        uint32_t step_idx;
        uint32_t ov_drift;

        /* tonic_alpha = 0.05 (3277), slew = 0.05 (3277), phasic_gain = 0.25 (16384) */
        ns_antennal_init(&g_ant, 8u, 256, 65536, 2u, 3277, 3277, 16384, 26214);
        ns_kenyon_init(&g_ken, 8u, 1024u, 6u, 51u, 42u);

        ns_antennal_normalize(&g_ant, plume_ref, ref_norm);
        ns_kenyon_hash(&g_ken, ref_norm, tag_ref);

        /* Simulate 300 ticks of slow upward thermal baseline ramp (+900 Q16/tick => ~4.1 units) */
        for (step_idx = 0u; step_idx < 300u; ++step_idx) {
            int32_t drift_val = (int32_t)(step_idx * 900u);
            for (i = 0u; i < 8u; ++i) {
                drifted_sample[i] = drift_val + (int32_t)(i * 512u);
            }
            ns_antennal_step(&g_ant, drifted_sample, step_out);
        }

        /* Inject a 0.5x weak plume atop the large thermal baseline offset */
        for (i = 0u; i < 8u; ++i) {
            drifted_sample[i] = (int32_t)(300u * 900u) + (int32_t)(i * 512u) + (plume_ref[i] >> 1);
        }
        ns_antennal_step(&g_ant, drifted_sample, step_out);
        ns_kenyon_hash(&g_ken, step_out, tag_drifted);
        ov_drift = ns_popcount_overlap(tag_ref, tag_drifted, 16u);

        if (ov_drift >= 45u) {
            printf("[PASS] Test 5: Thermal baseline drift rejection + 0.5x plume recovery (KC_overlap=%" PRIu32 "/51)\n",
                   ov_drift);
            passed++;
        } else {
            printf("[FAIL] Test 5: ov_drift=%" PRIu32 "/51\n", ov_drift);
            return 1;
        }
    }

    printf("------------------------------------------------------------------\n");
    {
        /* Micro-benchmark: 20,000 full pipeline ticks (antennal + kenyon + novelty) */
        int32_t sample_q16[16];
        int32_t norm_q16[16];
        uint64_t words[16];
        uint32_t iter;
        uint32_t sink = 0u;
        clock_t t0;
        clock_t dt;
        uint64_t total_ns;
        uint32_t ns_per_op;

        ns_antennal_init(&g_ant, 16u, 512, 65536, 2u, 655, 1311, 32768, 22938);
        ns_kenyon_init(&g_ken, 16u, 1024u, 6u, 51u, 42u);
        ns_novelty_init(&g_nov, 1024u, 36045, 16);
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
        printf("C99 pipeline throughput (D=16, M=1024): %" PRIu32 " ns/sample (guard=%" PRIu32 ")\n",
               ns_per_op, sink & 1u);
    }
    printf("All %d/%d ANSI C99 NeckSense self-tests PASSED.\n", passed, total);
    return 0;
}
