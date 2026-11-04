/*
 * NeckCompass ANSI C99 Self-Test Suite (Zero-Heap, Zero-FPU)
 *
 * Verifies:
 *   1. Static memory footprint (< 2 KB)
 *   2. LUT sin/cos/atan2 accuracy & -180 <-> +180 deg wrap-around continuity
 *   3. Q15 16-wedge E-PG angular velocity integration (< 3 deg error over full 360 deg)
 *   4. 120 deg ferromagnetic spike rejection & MEMS gyro bias anchoring
 *   5. Multi-leg GPS-denied outbound + PFL3 homebound return (< 5% of path length)
 *   6. Execution latency (< 10 us per combined compass + navigator + steering step)
 *   7. Re-anchoring after a real heading upset (and no re-anchor on a 2 s spike)
 *
 * SPDX-License-Identifier: MIT
 */

#include <stdio.h>
#include <stdint.h>
#include <time.h>

#include "neckcompass.h"

static int32_t abs_i32(int32_t v) {
    return (v < 0) ? (-v) : v;
}

static int test_memory_footprint(void) {
    size_t comp_bytes = sizeof(nc_compass_t);
    size_t nav_bytes = sizeof(nc_navigator_t);
    size_t total_ram = comp_bytes + nav_bytes;
    printf("[1/7] Memory footprint: nc_compass_t=%lu B, nc_navigator_t=%lu B, total=%lu B (< 2048 B) ... ",
           (unsigned long)comp_bytes, (unsigned long)nav_bytes, (unsigned long)total_ram);
    if (total_ram >= 2048u) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_lut_trig_and_wrap(void) {
    int32_t max_err_cdeg = 0;
    int32_t deg;
    nc_bam_t near_pos, near_neg, wrapped_step;

    for (deg = -179; deg <= 179; ++deg) {
        nc_bam_t b = nc_cdeg_to_bam(deg * 100);
        int32_t s = nc_sin_q15(b);
        int32_t c = nc_cos_q15(b);
        nc_bam_t rec = nc_atan2_bam(s, c);
        int32_t err_cdeg = abs_i32(nc_bam_to_cdeg(nc_bam_sub(rec, b)));
        if (err_cdeg > max_err_cdeg) {
            max_err_cdeg = err_cdeg;
        }
    }

    /* Check seamless wrap across +179.00 deg + 2.00 deg -> -179.00 deg */
    near_pos = nc_cdeg_to_bam(17900);
    near_neg = nc_cdeg_to_bam(-17900);
    wrapped_step = nc_bam_add(near_pos, nc_cdeg_to_bam(200));
    if (abs_i32(nc_bam_to_cdeg(nc_bam_sub(wrapped_step, near_neg))) > 5) {
        printf("[2/7] LUT trig & wrap-around ... FAIL (wrap mismatch)\n");
        return 1;
    }

    printf("[2/7] LUT sin/cos/atan2 & +/-180 deg wrap: max_err=%d.%02d deg ... ",
           (int)(max_err_cdeg / 100), (int)(max_err_cdeg % 100));
    if (max_err_cdeg > 30) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_full_360_integration(void) {
    nc_compass_t comp;
    nc_bam_t start_bam = nc_cdeg_to_bam(-14500); /* Start at -145.00 deg so 360 turn crosses +/-180! */
    /* Exactly 65536 BAM in 10.00 s -> 6554 BAM/s over 500 steps of 20 ms -> let's use 8192 BAM/s for 8.00 s (400 steps)
     * AND also test non-power-of-two 36.00 deg/s (6554 BAM/s) over 500 steps! */
    int32_t rate_bam_s = nc_cdeg_to_bam(3600); /* 36.00 deg/s */
    uint32_t step;
    int32_t err_ccw_cdeg, err_cw_cdeg;

    nc_compass_init(&comp);
    nc_compass_reset(&comp, start_bam);
    for (step = 0; step < 500u; ++step) {
        nc_compass_update(&comp, rate_bam_s, 20u, 0, 0u, 0, 0, 0u, 0);
    }
    err_ccw_cdeg = abs_i32(nc_bam_to_cdeg(nc_bam_sub(nc_compass_heading_bam(&comp), start_bam)));

    nc_compass_reset(&comp, start_bam);
    for (step = 0; step < 500u; ++step) {
        nc_compass_update(&comp, -rate_bam_s, 20u, 0, 0u, 0, 0, 0u, 0);
    }
    err_cw_cdeg = abs_i32(nc_bam_to_cdeg(nc_bam_sub(nc_compass_heading_bam(&comp), start_bam)));

    printf("[3/7] 360-deg Q15 integration (across +/-180 seam): CCW_err=%d.%02d deg, CW_err=%d.%02d deg (< 3.00 deg) ... ",
           (int)(err_ccw_cdeg / 100), (int)(err_ccw_cdeg % 100),
           (int)(err_cw_cdeg / 100), (int)(err_cw_cdeg % 100));
    if (err_ccw_cdeg >= 300 || err_cw_cdeg >= 300) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_spike_rejection_and_bias_anchoring(void) {
    nc_compass_t comp;
    nc_bam_t true_bam = nc_cdeg_to_bam(3000);   /* +30.00 deg */
    nc_bam_t spike_bam = nc_cdeg_to_bam(15000); /* +150.00 deg (+120 deg ferromagnetic spike!) */
    int32_t bias_bam_s = nc_cdeg_to_bam(150);   /* +1.50 deg/s steady MEMS gyro bias */
    int32_t max_spike_err_cdeg = 0;
    int32_t anchored_err_cdeg;
    uint32_t step;

    nc_compass_init(&comp);
    nc_compass_reset(&comp, true_bam);

    /* Apply 1.5 s (75 steps @ 20 ms) of a 120-deg ferromagnetic anomaly */
    for (step = 0; step < 75u; ++step) {
        nc_bam_t h = nc_compass_update(&comp, 0, 20u, spike_bam, 1u, NC_Q15_ONE, 0, 0u, 0);
        int32_t err = abs_i32(nc_bam_to_cdeg(nc_bam_sub(h, true_bam)));
        if (err > max_spike_err_cdeg) {
            max_spike_err_cdeg = err;
        }
    }

    /* Apply 20.0 s (1000 steps @ 20 ms) of +1.50 deg/s gyro bias (would drift 30 deg unanchored) */
    for (step = 0; step < 1000u; ++step) {
        nc_compass_update(&comp, bias_bam_s, 20u, true_bam, 1u, NC_Q15_ONE, 0, 0u, 0);
    }
    anchored_err_cdeg = abs_i32(nc_bam_to_cdeg(nc_bam_sub(nc_compass_heading_bam(&comp), true_bam)));

    printf("[4/7] 120-deg spike rejection & bias anchor: spike_err=%d.%02d deg, 20s_bias_err=%d.%02d deg ... ",
           (int)(max_spike_err_cdeg / 100), (int)(max_spike_err_cdeg % 100),
           (int)(anchored_err_cdeg / 100), (int)(anchored_err_cdeg % 100));
    if (max_spike_err_cdeg > 100 || anchored_err_cdeg > 200) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_gps_denied_return_to_origin(void) {
    nc_compass_t comp;
    nc_navigator_t nav;
    int64_t true_x_um = 0; /* micrometers for exact ground-truth integration */
    int64_t true_y_um = 0;
    uint64_t total_path_mm = 0;
    nc_bam_t true_bam = nc_cdeg_to_bam(2000); /* +20.00 deg */
    int32_t speed_mm_s = 1500;                /* 1.50 m/s */
    int32_t gyro_bias_bam_s = nc_cdeg_to_bam(80); /* +0.80 deg/s gyro bias */
    uint32_t dt_ms = 20u;
    uint32_t step;
    uint32_t final_miss_mm, pct_x100;

    nc_compass_init(&comp);
    nc_compass_reset(&comp, true_bam);
    nc_navigator_init(&nav, 1500);

    /* Outbound multi-leg survey: 3000 steps = 60.0 s (90.0 m outbound) with 120-deg spikes */
    for (step = 0; step < 3000u; ++step) {
        int32_t omega_true_bam_s = 0;
        nc_bam_t mag_bam, est_bam;

        if (step >= 900u && step < 1150u) {
            omega_true_bam_s = nc_cdeg_to_bam(1300); /* Turn +65 deg over 5 s */
        } else if (step >= 1900u && step < 2200u) {
            omega_true_bam_s = -nc_cdeg_to_bam(2000); /* Turn -120 deg over 6 s */
        }

        true_bam = nc_bam_add(true_bam, (nc_bam_t)((omega_true_bam_s * (int32_t)dt_ms) / 1000));
        /* Advance true ground position: speed_mm_s * dt_ms = 30,000 um per step */
        true_x_um += ((int64_t)30000 * (int64_t)nc_cos_q15(true_bam)) / NC_Q15_ONE;
        true_y_um += ((int64_t)30000 * (int64_t)nc_sin_q15(true_bam)) / NC_Q15_ONE;
        total_path_mm += 30u;

        /* Inject two 120-deg ferromagnetic spikes during outbound travel */
        if ((step >= 500u && step < 575u) || (step >= 2400u && step < 2480u)) {
            mag_bam = nc_bam_add(true_bam, nc_cdeg_to_bam(12000));
        } else {
            mag_bam = true_bam;
        }

        est_bam = nc_compass_update(
            &comp, omega_true_bam_s + gyro_bias_bam_s, dt_ms,
            mag_bam, 1u, NC_Q15_ONE, 0, 0u, 0
        );
        nc_navigator_update(&nav, speed_mm_s, dt_ms, est_bam);
    }

    /* Engage blind GPS-denied CPU4/PFL3 return-to-home */
    {
        int32_t max_turn = nc_cdeg_to_bam(4500); /* 45 deg/s max yaw rate */
        for (step = 0; step < 4500u; ++step) {
            uint32_t est_dist_mm = 0;
            nc_bam_t est_home_bam = 0;
            nc_bam_t cur_est_bam = nc_compass_heading_bam(&comp);
            int32_t cmd_bam_s, cmd_speed_mm_s;
            nc_bam_t mag_bam, new_est_bam;
            int64_t ds_um;

            nc_navigator_home_vector(&nav, &est_dist_mm, &est_home_bam);
            if (est_dist_mm < 250u) {
                break;
            }
            cmd_bam_s = nc_navigator_steering_cmd(&nav, cur_est_bam, max_turn);
            cmd_speed_mm_s = (est_dist_mm < 2000u) ? (int32_t)(300u + (est_dist_mm * 1200u) / 2000u) : speed_mm_s;

            true_bam = nc_bam_add(true_bam, (nc_bam_t)((cmd_bam_s * (int32_t)dt_ms) / 1000));
            ds_um = (int64_t)cmd_speed_mm_s * (int64_t)dt_ms; /* mm * ms == um */
            true_x_um += (ds_um * (int64_t)nc_cos_q15(true_bam)) / NC_Q15_ONE;
            true_y_um += (ds_um * (int64_t)nc_sin_q15(true_bam)) / NC_Q15_ONE;
            total_path_mm += (uint64_t)(ds_um / 1000LL);

            /* One more 125-deg magnetic spike mid-return! */
            if (step >= 800u && step < 875u) {
                mag_bam = nc_bam_sub(true_bam, nc_cdeg_to_bam(12500));
            } else {
                mag_bam = true_bam;
            }
            new_est_bam = nc_compass_update(
                &comp, cmd_bam_s + gyro_bias_bam_s, dt_ms,
                mag_bam, 1u, NC_Q15_ONE, 0, 0u, 0
            );
            nc_navigator_update(&nav, cmd_speed_mm_s, dt_ms, new_est_bam);
        }
    }

    {
        int64_t fx_mm = true_x_um / 1000LL;
        int64_t fy_mm = true_y_um / 1000LL;
        final_miss_mm = nc_isqrt64((uint64_t)(fx_mm * fx_mm + fy_mm * fy_mm));
        pct_x100 = (uint32_t)(((uint64_t)final_miss_mm * 10000ULL) / (total_path_mm > 0 ? total_path_mm : 1ULL));
    }

    printf("[5/7] Outbound + blind CPU4/PFL3 return: miss=%u.%03u m / %lu.%01lu m path (%u.%02u%% < 5.00%%) ... ",
           (unsigned)(final_miss_mm / 1000u), (unsigned)(final_miss_mm % 1000u),
           (unsigned long)(total_path_mm / 1000ULL), (unsigned long)((total_path_mm % 1000ULL) / 100ULL),
           (unsigned)(pct_x100 / 100u), (unsigned)(pct_x100 % 100u));
    if (pct_x100 >= 500u) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_update_latency(void) {
    nc_compass_t comp;
    nc_navigator_t nav;
    uint32_t i;
    clock_t t0, t1;
    uint64_t elapsed_ns, ns_per_update;
    volatile int32_t sink = 0;

    nc_compass_init(&comp);
    nc_compass_reset(&comp, 1024);
    nc_navigator_init(&nav, 1500);

    t0 = clock();
    for (i = 0; i < 100000u; ++i) {
        nc_bam_t h = nc_compass_update(&comp, 512, 20u, (nc_bam_t)(i * 32u), 1u, NC_Q15_ONE, 0, 0u, 0);
        nc_navigator_update(&nav, 1500, 20u, h);
        sink += nc_navigator_steering_cmd(&nav, h, 8192);
    }
    t1 = clock();
    (void)sink;

    elapsed_ns = ((uint64_t)(t1 - t0) * 1000000000ULL) / (uint64_t)CLOCKS_PER_SEC;
    ns_per_update = elapsed_ns / 100000ULL;

    printf("[6/7] Combined E-PG + CPU4 + PFL3 step latency: %lu.%03lu us (< 10.000 us) ... ",
           (unsigned long)(ns_per_update / 1000ULL), (unsigned long)(ns_per_update % 1000ULL));
    if (ns_per_update >= 10000ULL) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

/* Deterministic +/-3 deg triangle-ish pseudo-noise (no RNG, no floats). */
static int32_t pseudo_noise_cdeg(uint32_t k) {
    uint32_t h = k * 2654435761u;
    return (int32_t)((h >> 16) % 601u) - 300;
}

static int test_reanchor_after_upset(void) {
    nc_compass_t comp;
    uint32_t k;
    int32_t err_after_spike, err_after_upset;

    /* (a) A 2 s, 110-deg off-basin spike must NOT re-anchor the compass. */
    nc_compass_init(&comp);
    nc_compass_reset(&comp, nc_cdeg_to_bam(0));
    for (k = 0; k < 100u; ++k) { /* 2 s at 50 Hz */
        nc_compass_update(&comp, 0, 20u, nc_cdeg_to_bam(11000 + pseudo_noise_cdeg(k)), 1u, NC_Q15_ONE, 0, 0u, 0);
    }
    for (k = 0; k < 100u; ++k) {
        nc_compass_update(&comp, 0, 20u, nc_cdeg_to_bam(pseudo_noise_cdeg(k + 100u)), 1u, NC_Q15_ONE, 0, 0u, 0);
    }
    err_after_spike = abs_i32(nc_bam_to_cdeg(nc_compass_heading_bam(&comp)));

    /* (b) A consistent 110-deg offset for 11 s (noisy) is a real upset: re-anchor. */
    nc_compass_init(&comp);
    nc_compass_reset(&comp, nc_cdeg_to_bam(0));
    for (k = 0; k < 550u; ++k) {
        nc_compass_update(&comp, 0, 20u, nc_cdeg_to_bam(11000 + pseudo_noise_cdeg(k)), 1u, NC_Q15_ONE, 0, 0u, 0);
    }
    err_after_upset = abs_i32(nc_bam_to_cdeg(nc_bam_sub(nc_compass_heading_bam(&comp), nc_cdeg_to_bam(11000))));

    printf("[7/7] Re-anchor: after 2 s spike err=%d.%02d deg (< 1), after 11 s upset err=%d.%02d deg (< 2) ... ",
           (int)(err_after_spike / 100), (int)(err_after_spike % 100),
           (int)(err_after_upset / 100), (int)(err_after_upset % 100));
    if (err_after_spike >= 100 || err_after_upset >= 200) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

int main(void) {
    int failures = 0;
    printf("=== NeckCompass C99 Fixed-Point (Q15 / Zero-Heap / Zero-FPU) Self-Test ===\n");
    failures += test_memory_footprint();
    failures += test_lut_trig_and_wrap();
    failures += test_full_360_integration();
    failures += test_spike_rejection_and_bias_anchoring();
    failures += test_gps_denied_return_to_origin();
    failures += test_update_latency();
    failures += test_reanchor_after_upset();

    if (failures == 0) {
        printf("=== ALL 7/7 C99 SELF-TESTS PASSED ===\n");
        return 0;
    }
    printf("=== %d C99 SELF-TEST(S) FAILED ===\n", failures);
    return 1;
}
