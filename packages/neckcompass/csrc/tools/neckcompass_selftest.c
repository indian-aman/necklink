/*
 * NeckCompass v3.0.0 ANSI C99 Self-Test Suite (Zero-Heap, Zero-FPU)
 *
 * Verifies (14/14):
 *   1. Static RAM footprint & exact v3.0 struct byte sizes (+ BANC v888 ROM table)
 *   2. LUT sin/cos/atan2 accuracy & -180 <-> +180 deg wrap-around continuity
 *   3. v2_analytic (weights = NULL) 360-deg Q15 E-PG <-> Delta7 integration (< 3 deg)
 *   4. BANC v888 120-deg ferromagnetic spike gating, coherence & MEMS gyro bias homeostasis
 *   5. BANC v888 multi-leg GPS-denied outbound + 25 PFL3 / 12 PFL2 -> DNa02 return (< 5% path)
 *   6. Execution latency (< 25 us BANC v888 matrix step, < 10 us v2_analytic step)
 *   7. BANC v888 re-anchoring after a real heading upset (and no re-anchor on a 2 s spike)
 *   8. 3D +/-25 deg roll / +/-20 deg pitch tilt compensation under 60-65 deg magnetic dip (< 1 deg)
 *   9. 9-DOF gravity gate rejecting +0.85g wave-slam linear acceleration bursts
 *  10. C99 nc_plastic_ring_t self-supervised 31 deg steel-harbor warp learning (< 3.5 deg mean error)
 *  11. C99 nc_plastic_ring_t supervised sun/GNSS reference lock (< 2.0 deg mean error)
 *  12. v2_analytic PFL2 symmetric 179-180 deg turn-around & U-turn homing
 *  13. BANC v888 25 PFL3 + 12 PFL2 -> DNa02 anti-goal U-turn homing & firing rate readout
 *  14. BANC v888 real connectome vs shuffled connectome superiority (fixed 10.2 velocity gain)
 *
 * SPDX-License-Identifier: MIT
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

#include "neckcompass.h"

static int32_t abs_i32(int32_t v) {
    return (v < 0) ? (int32_t)(-(int64_t)v) : v;
}

/* Synthesize exact body-frame 3D gravity (mg) and dipped magnetic vector (mG*10) in pure integer Q15.
 * World frame: +X North, +Y West/Left, +Z Up; gravity specific force = [0, 0, +1000 mg];
 * Magnetic vector with downward dip_bam: [B*cos(dip), 0, -B*sin(dip)] where B = 10000. */
static void synth_body_9dof(
    nc_bam_t yaw_bam,
    nc_bam_t pitch_bam,
    nc_bam_t roll_bam,
    nc_bam_t dip_bam,
    int32_t *out_ax_mg,
    int32_t *out_ay_mg,
    int32_t *out_az_mg,
    int32_t *out_mx,
    int32_t *out_my,
    int32_t *out_mz
) {
    int64_t cy = nc_cos_q15(yaw_bam), sy = nc_sin_q15(yaw_bam);
    int64_t cp = nc_cos_q15(pitch_bam), sp = nc_sin_q15(pitch_bam);
    int64_t cr = nc_cos_q15(roll_bam), sr = nc_sin_q15(roll_bam);
    int64_t bh = (10000LL * (int64_t)nc_cos_q15(dip_bam)) / NC_Q15_ONE;
    int64_t bz = -(10000LL * (int64_t)nc_sin_q15(dip_bam)) / NC_Q15_ONE;

    /* R_z(-yaw) * m_world */
    int64_t hx = (bh * cy) / NC_Q15_ONE;
    int64_t hy = -(bh * sy) / NC_Q15_ONE;
    int64_t hz = bz;

    /* R_y(-pitch) * [hx, hy, hz] */
    int64_t px = (hx * cp - hz * sp) / NC_Q15_ONE;
    int64_t py = hy;
    int64_t pz = (hx * sp + hz * cp) / NC_Q15_ONE;

    /* R_x(-roll) * [px, py, pz] */
    *out_mx = (int32_t)px;
    *out_my = (int32_t)((py * cr + pz * sr) / NC_Q15_ONE);
    *out_mz = (int32_t)((-py * sr + pz * cr) / NC_Q15_ONE);

    /* Body accelerometer specific force = R_x(-roll) * R_y(-pitch) * [0, 0, +1000 mg] */
    *out_ax_mg = (int32_t)((-1000LL * sp) / NC_Q15_ONE);
    *out_ay_mg = (int32_t)((1000LL * sr * cp) / ((int64_t)NC_Q15_ONE * NC_Q15_ONE));
    *out_az_mg = (int32_t)((1000LL * cr * cp) / ((int64_t)NC_Q15_ONE * NC_Q15_ONE));
}

/* 31.1-deg peak steel-harbor soft-iron warp: psi + 20 deg * sin(psi) + 16 deg * sin(2*psi) */
static nc_bam_t harbor_warp_bam(nc_bam_t true_bam, int32_t const_offset_cdeg) {
    int64_t s1 = (int64_t)nc_sin_q15(true_bam);
    int64_t s2 = (int64_t)nc_sin_q15(nc_bam_add(true_bam, true_bam));
    int32_t w_cdeg = const_offset_cdeg
                   + (int32_t)((2000LL * s1) / NC_Q15_ONE)
                   + (int32_t)((1600LL * s2) / NC_Q15_ONE);
    return nc_bam_add(true_bam, nc_cdeg_to_bam(w_cdeg));
}

/* Deterministic zero-mean Murmur3-mixed pseudo-noise in [-amp_cdeg .. +amp_cdeg] (no RNG, no floats). */
static int32_t det_noise_cdeg(uint32_t k, uint32_t salt, uint32_t amp_cdeg) {
    uint32_t h = k + salt * 0x9E3779B9u + 1u;
    uint32_t span = 2u * amp_cdeg + 1u;
    h ^= h >> 16;
    h *= 0x85EBCA6Bu;
    h ^= h >> 13;
    h *= 0xC2B2AE35u;
    h ^= h >> 16;
    return (int32_t)(h % span) - (int32_t)amp_cdeg;
}

static uint32_t det_u32(uint32_t *state) {
    uint32_t x = *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *state = x;
    return x;
}

static int test_memory_footprint(void) {
    nc_plastic_ring_t dummy_pr;
    size_t comp_bytes = sizeof(nc_compass_t);
    size_t nav_bytes = sizeof(nc_navigator_t);
    size_t total_ram = comp_bytes + nav_bytes;
    size_t tilt_bytes = sizeof(nc_tilt_t);
    size_t imu_bytes = sizeof(nc_imu9_t);
    size_t pr_bytes = sizeof(nc_plastic_ring_t);
    size_t w_bytes = sizeof(dummy_pr.weights_q12);
    size_t banc_rom_bytes = sizeof(nc_banc_cx_weights_t);

    printf("[ 1/14] Memory footprint: nc_compass_t=%lu B, nc_navigator_t=%lu B, total=%lu B (< 2048 B) ... ",
           (unsigned long)comp_bytes, (unsigned long)nav_bytes, (unsigned long)total_ram);
    if (total_ram >= 2048u || tilt_bytes > 32u || w_bytes != 1024u || pr_bytes >= 1200u) {
        printf("FAIL (nc_plastic_ring_t=%lu B)\n", (unsigned long)pr_bytes);
        return 1;
    }
    printf("PASS\n");
    printf("        v3.0 structs: nc_tilt_t=%lu B, nc_imu9_t=%lu B, nc_plastic_ring_t=%lu B (< 1200 B), BANC_ROM=%lu B\n",
           (unsigned long)tilt_bytes, (unsigned long)imu_bytes,
           (unsigned long)pr_bytes, (unsigned long)banc_rom_bytes);
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

    near_pos = nc_cdeg_to_bam(17900);
    near_neg = nc_cdeg_to_bam(-17900);
    wrapped_step = nc_bam_add(near_pos, nc_cdeg_to_bam(200));
    if (abs_i32(nc_bam_to_cdeg(nc_bam_sub(wrapped_step, near_neg))) > 5) {
        printf("[ 2/14] LUT trig & wrap-around ... FAIL (wrap mismatch)\n");
        return 1;
    }

    printf("[ 2/14] LUT sin/cos/atan2 & +/-180 deg wrap: max_err=%d.%02d deg ... ",
           (int)(max_err_cdeg / 100), (int)(max_err_cdeg % 100));
    if (max_err_cdeg > 30) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_full_360_integration_analytic(void) {
    nc_compass_t comp;
    int16_t delta7[NC_NUM_WEDGES];
    nc_bam_t start_bam = nc_cdeg_to_bam(-14500);
    int32_t rate_bam_s = nc_cdeg_s_to_bam_s(3600);
    uint32_t step;
    int32_t err_ccw_cdeg, err_cw_cdeg;

    nc_compass_init(&comp);
    nc_compass_set_weights(&comp, NULL);
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

    /* Verify Delta7 anti-phase glutamatergic bridge readout at 0 deg (wedge 8 = peak, wedge 0 = anti-peak) */
    nc_compass_reset(&comp, 0);
    nc_compass_delta7_q15(&comp, delta7);

    printf("[ 3/14] v2_analytic 360-deg Q15 integration: CCW_err=%d.%02d deg, CW_err=%d.%02d deg ... ",
           (int)(err_ccw_cdeg / 100), (int)(err_ccw_cdeg % 100),
           (int)(err_cw_cdeg / 100), (int)(err_cw_cdeg % 100));
    if (err_ccw_cdeg >= 300 || err_cw_cdeg >= 300 || delta7[8] != 0 || delta7[0] < 32760) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_banc_spike_rejection_and_bias_anchoring(void) {
    nc_compass_t comp;
    int16_t delta7[NC_NUM_WEDGES];
    nc_bam_t true_bam = nc_cdeg_to_bam(3000);
    nc_bam_t spike_bam = nc_cdeg_to_bam(15000);
    int32_t bias_bam_s = nc_cdeg_s_to_bam_s(150);
    int32_t max_gate_during_spike = 0;
    int32_t anchored_err_cdeg, learned_bias_cdeg_s;
    int16_t coh_q15;
    uint32_t step;

    nc_compass_init(&comp);
    nc_compass_reset(&comp, true_bam);

    /* Stabilize bump onto BANC v888 attractor with clean magnetic cue for 0.5 s */
    for (step = 0; step < 25u; ++step) {
        nc_compass_update(&comp, 0, 20u, true_bam, 1u, NC_Q15_ONE, 0, 0u, 0);
    }

    for (step = 0; step < 75u; ++step) {
        nc_compass_update(&comp, 0, 20u, spike_bam, 1u, NC_Q15_ONE, 0, 0u, 0);
        if (comp.last_mag_gate_q15 > max_gate_during_spike) {
            max_gate_during_spike = comp.last_mag_gate_q15;
        }
    }

    for (step = 0; step < 1000u; ++step) {
        nc_compass_update(&comp, bias_bam_s, 20u, true_bam, 1u, NC_Q15_ONE, 0, 0u, 0);
    }
    anchored_err_cdeg = abs_i32(nc_bam_to_cdeg(nc_bam_sub(nc_compass_heading_bam(&comp), true_bam)));
    learned_bias_cdeg_s = nc_bam_s_to_cdeg_s(comp.pen_bias_bam_s);
    coh_q15 = nc_compass_coherence_q15(&comp);
    nc_compass_delta7_q15(&comp, delta7);

    printf("[ 4/14] BANC v888 spike gate & bias: spike_gate=%d, bias=%d.%02d deg/s, coh=%d/32767, err=%d.%02d deg ... ",
           (int)max_gate_during_spike,
           (int)(learned_bias_cdeg_s / 100), (int)abs_i32(learned_bias_cdeg_s % 100),
           (int)coh_q15,
           (int)(anchored_err_cdeg / 100), (int)(anchored_err_cdeg % 100));
    if (max_gate_during_spike != 0 ||
        coh_q15 < 21000 ||
        abs_i32(learned_bias_cdeg_s - 150) > 40 ||
        anchored_err_cdeg > 300 ||
        delta7[1] <= delta7[9]) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_banc_gps_denied_return_to_origin(void) {
    nc_compass_t comp;
    nc_navigator_t nav;
    int64_t true_x_um = 0;
    int64_t true_y_um = 0;
    uint64_t total_path_mm = 0;
    nc_bam_t true_bam = nc_cdeg_to_bam(2000);
    int32_t speed_mm_s = 1500;
    int32_t gyro_bias_bam_s = nc_cdeg_s_to_bam_s(80);
    uint32_t dt_ms = 20u;
    uint32_t step;
    uint32_t final_miss_mm, pct_x100;

    nc_compass_init(&comp);
    nc_compass_reset(&comp, true_bam);
    nc_navigator_init(&nav, 1500);

    for (step = 0; step < 3000u; ++step) {
        int32_t omega_true_bam_s = 0;
        nc_bam_t mag_bam, est_bam;

        if (step >= 900u && step < 1150u) {
            omega_true_bam_s = nc_cdeg_s_to_bam_s(1300);
        } else if (step >= 1900u && step < 2200u) {
            omega_true_bam_s = -nc_cdeg_s_to_bam_s(1000);
        }

        true_bam = nc_bam_add(true_bam, (nc_bam_t)((omega_true_bam_s * (int32_t)dt_ms) / 1000));
        true_x_um += ((int64_t)30000 * (int64_t)nc_cos_q15(true_bam)) / NC_Q15_ONE;
        true_y_um += ((int64_t)30000 * (int64_t)nc_sin_q15(true_bam)) / NC_Q15_ONE;
        total_path_mm += 30u;

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

    {
        int32_t max_turn = nc_cdeg_s_to_bam_s(4500);
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
            cmd_bam_s = nc_navigator_steering_cmd_with_compass(&nav, &comp, cur_est_bam, max_turn);
            cmd_speed_mm_s = (est_dist_mm < 2000u) ? (int32_t)(300u + (est_dist_mm * 1200u) / 2000u) : speed_mm_s;

            true_bam = nc_bam_add(true_bam, (nc_bam_t)((cmd_bam_s * (int32_t)dt_ms) / 1000));
            ds_um = (int64_t)cmd_speed_mm_s * (int64_t)dt_ms;
            true_x_um += (ds_um * (int64_t)nc_cos_q15(true_bam)) / NC_Q15_ONE;
            true_y_um += (ds_um * (int64_t)nc_sin_q15(true_bam)) / NC_Q15_ONE;
            total_path_mm += (uint64_t)(ds_um / 1000LL);

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

    printf("[ 5/14] BANC v888 outbound + PFL3/PFL2->DNa02 return: miss=%u.%03u m / %lu.%01lu m (%u.%02u%% < 5.00%%) ... ",
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
    uint64_t ns_banc, ns_v2;
    volatile int32_t sink = 0;

    nc_compass_init(&comp);
    nc_compass_reset(&comp, 1024);
    nc_navigator_init(&nav, 1500);

    t0 = clock();
    for (i = 0; i < 40000u; ++i) {
        nc_bam_t h = nc_compass_update(&comp, 512, 10u, (nc_bam_t)(i * 32u), 1u, NC_Q15_ONE, 0, 0u, 0);
        nc_navigator_update(&nav, 1500, 10u, h);
        sink += nc_navigator_steering_cmd_with_compass(&nav, &comp, h, 8192);
    }
    t1 = clock();
    ns_banc = (((uint64_t)(t1 - t0) * 1000000000ULL) / (uint64_t)CLOCKS_PER_SEC) / 40000ULL;

    nc_compass_set_weights(&comp, NULL);
    nc_navigator_set_weights(&nav, NULL);
    t0 = clock();
    for (i = 0; i < 100000u; ++i) {
        nc_bam_t h = nc_compass_update(&comp, 512, 20u, (nc_bam_t)(i * 32u), 1u, NC_Q15_ONE, 0, 0u, 0);
        nc_navigator_update(&nav, 1500, 20u, h);
        sink += nc_navigator_steering_cmd(&nav, h, 8192);
    }
    t1 = clock();
    ns_v2 = (((uint64_t)(t1 - t0) * 1000000000ULL) / (uint64_t)CLOCKS_PER_SEC) / 100000ULL;
    (void)sink;

    {
#if defined(__SANITIZE_ADDRESS__)
        uint64_t banc_limit_ns = 120000ULL;
        uint64_t v2_limit_ns = 30000ULL;
#elif defined(__has_feature)
#  if __has_feature(address_sanitizer)
        uint64_t banc_limit_ns = 120000ULL;
        uint64_t v2_limit_ns = 30000ULL;
#  else
        uint64_t banc_limit_ns = 25000ULL;
        uint64_t v2_limit_ns = 10000ULL;
#  endif
#else
        uint64_t banc_limit_ns = 25000ULL;
        uint64_t v2_limit_ns = 10000ULL;
#endif
        printf("[ 6/14] Step latency: BANC_v888=%lu.%03lu us (< %lu us), v2_analytic=%lu.%03lu us (< %lu us) ... ",
               (unsigned long)(ns_banc / 1000ULL), (unsigned long)(ns_banc % 1000ULL),
               (unsigned long)(banc_limit_ns / 1000ULL),
               (unsigned long)(ns_v2 / 1000ULL), (unsigned long)(ns_v2 % 1000ULL),
               (unsigned long)(v2_limit_ns / 1000ULL));
        if (ns_banc >= banc_limit_ns || ns_v2 >= v2_limit_ns) {
            printf("FAIL\n");
            return 1;
        }
        printf("PASS\n");
    }
    return 0;
}

static int test_reanchor_after_upset(void) {
    nc_compass_t comp;
    uint32_t k;
    int32_t err_after_spike, err_after_upset;

    nc_compass_init(&comp);
    nc_compass_reset(&comp, nc_cdeg_to_bam(0));
    for (k = 0; k < 100u; ++k) {
        nc_compass_update(&comp, 0, 20u, nc_cdeg_to_bam(11000 + det_noise_cdeg(k, 1u, 300u)), 1u, NC_Q15_ONE, 0, 0u, 0);
    }
    for (k = 0; k < 100u; ++k) {
        nc_compass_update(&comp, 0, 20u, nc_cdeg_to_bam(det_noise_cdeg(k + 100u, 1u, 300u)), 1u, NC_Q15_ONE, 0, 0u, 0);
    }
    err_after_spike = abs_i32(nc_bam_to_cdeg(nc_compass_heading_bam(&comp)));

    nc_compass_init(&comp);
    nc_compass_reset(&comp, nc_cdeg_to_bam(0));
    for (k = 0; k < 550u; ++k) {
        nc_compass_update(&comp, 0, 20u, nc_cdeg_to_bam(11000 + det_noise_cdeg(k, 1u, 300u)), 1u, NC_Q15_ONE, 0, 0u, 0);
    }
    err_after_upset = abs_i32(nc_bam_to_cdeg(nc_bam_sub(nc_compass_heading_bam(&comp), nc_cdeg_to_bam(11000))));

    printf("[ 7/14] BANC v888 re-anchor: 2 s spike err=%d.%02d deg (< 2.5), 11 s upset err=%d.%02d deg (< 3.0) ... ",
           (int)(err_after_spike / 100), (int)(err_after_spike % 100),
           (int)(err_after_upset / 100), (int)(err_after_upset % 100));
    if (err_after_spike >= 250 || err_after_upset >= 300) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_3d_tilt_dip_compensation(void) {
    nc_bam_t roll_pos = nc_cdeg_to_bam(2500);   /* +25.00 deg roll  */
    nc_bam_t pitch_pos = nc_cdeg_to_bam(2000);  /* +20.00 deg pitch */
    nc_bam_t dip_60 = nc_cdeg_to_bam(6000);     /* 60.00 deg magnetic dip */
    nc_bam_t roll_neg = nc_cdeg_to_bam(-2500);  /* -25.00 deg roll  */
    nc_bam_t pitch_neg = nc_cdeg_to_bam(-2000); /* -20.00 deg pitch */
    nc_bam_t dip_65 = nc_cdeg_to_bam(6500);     /* 65.00 deg magnetic dip */
    int32_t max_comp_err_cdeg = 0;
    int32_t max_flat_err_cdeg = 0;
    int32_t k;

    for (k = 0; k < 36; ++k) {
        nc_bam_t yaw_true = nc_cdeg_to_bam(-18000 + k * 1000);
        int32_t ax, ay, az, mx, my, mz;
        nc_tilt_t tilt;
        nc_bam_t comp_mag, flat_mag;
        int32_t err_c, err_f;

        synth_body_9dof(yaw_true, pitch_pos, roll_pos, dip_60, &ax, &ay, &az, &mx, &my, &mz);
        nc_tilt_init(&tilt);
        comp_mag = nc_tilt_update_9dof(&tilt, 0, 0, 0, ax, ay, az, mx, my, mz, 20u, NULL);
        flat_mag = nc_atan2_bam(-my, mx);
        err_c = abs_i32(nc_bam_to_cdeg(nc_bam_sub(comp_mag, yaw_true)));
        err_f = abs_i32(nc_bam_to_cdeg(nc_bam_sub(flat_mag, yaw_true)));
        if (err_c > max_comp_err_cdeg) max_comp_err_cdeg = err_c;
        if (err_f > max_flat_err_cdeg) max_flat_err_cdeg = err_f;

        synth_body_9dof(yaw_true, pitch_neg, roll_neg, dip_65, &ax, &ay, &az, &mx, &my, &mz);
        nc_tilt_init(&tilt);
        comp_mag = nc_tilt_update_9dof(&tilt, 0, 0, 0, ax, ay, az, mx, my, mz, 20u, NULL);
        err_c = abs_i32(nc_bam_to_cdeg(nc_bam_sub(comp_mag, yaw_true)));
        if (err_c > max_comp_err_cdeg) max_comp_err_cdeg = err_c;
    }

    printf("[ 8/14] 3D +/-25 deg roll, +/-20 deg pitch, 60-65 deg dip: flat_err=%d.%02d deg -> comp_err=%d.%02d deg (< 1.00) ... ",
           (int)(max_flat_err_cdeg / 100), (int)(max_flat_err_cdeg % 100),
           (int)(max_comp_err_cdeg / 100), (int)(max_comp_err_cdeg % 100));
    if (max_flat_err_cdeg < 2000 || max_comp_err_cdeg >= 100) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_9dof_wave_slam_gravity_gating(void) {
    nc_tilt_t tilt;
    nc_compass_t comp;
    nc_bam_t yaw_true = nc_cdeg_to_bam(4200);   /* +42.00 deg true course */
    nc_bam_t roll_true = nc_cdeg_to_bam(2200);  /* +22.00 deg steady heel */
    nc_bam_t pitch_true = nc_cdeg_to_bam(-1500);/* -15.00 deg bow pitch   */
    nc_bam_t dip_65 = nc_cdeg_to_bam(6500);
    int32_t ax, ay, az, mx, my, mz;
    uint32_t k, gated_count = 0u;
    int32_t max_slam_err_cdeg = 0;

    synth_body_9dof(yaw_true, pitch_true, roll_true, dip_65, &ax, &ay, &az, &mx, &my, &mz);
    nc_tilt_init(&tilt);
    nc_compass_init(&comp);

    for (k = 0; k < 20u; ++k) {
        nc_imu9_t imu = {0, 0, 0, ax, ay, az, mx, my, mz};
        nc_compass_update_9dof(&comp, &tilt, &imu, 20u, 1u, NC_Q15_ONE, 0, 0u, 0);
    }

    /* Hit the boat with +950 mg lateral + +500 mg vertical wave-slam impact (|a| > 1.25g)! */
    for (k = 0; k < 50u; ++k) {
        nc_imu9_t imu = {0, 0, 0, ax + 600, ay + 950, az + 500, mx, my, mz};
        nc_bam_t h = nc_compass_update_9dof(&comp, &tilt, &imu, 20u, 1u, NC_Q15_ONE, 0, 0u, 0);
        int32_t err = abs_i32(nc_bam_to_cdeg(nc_bam_sub(h, yaw_true)));
        if (tilt.accel_gated) {
            gated_count += 1u;
        }
        if (err > max_slam_err_cdeg) {
            max_slam_err_cdeg = err;
        }
    }

    printf("[ 9/14] 9-DOF wave-slam gravity gate (|a-g|>0.25g): gated=%u/50, max_heading_err=%d.%02d deg (< 2.50) ... ",
           (unsigned)gated_count, (int)(max_slam_err_cdeg / 100), (int)(max_slam_err_cdeg % 100));
    if (gated_count != 50u || max_slam_err_cdeg >= 250) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_plastic_ring_self_supervised_harbor(void) {
    nc_plastic_ring_t pr;
    uint32_t k;
    uint32_t psi_sub_q8 = 0u;
    int32_t gyro_bias_bam_s = nc_cdeg_s_to_bam_s(90); /* +0.90 deg/s steady MEMS gyro drift */
    int64_t raw_err_sum_cdeg = 0;
    int64_t learned_err_sum_cdeg = 0;
    int32_t raw_mean_cdeg, learned_mean_cdeg;

    nc_plastic_ring_init(&pr);

    /* 240 s @ 50 Hz (12,000 steps) of varying harbor turns with biased noisy gyro + noisy warped mag */
    for (k = 0; k < 12000u; ++k) {
        nc_bam_t mod_phase = (nc_bam_t)(((k * 10430u) / 1000u) & 0xFFFFu);
        int32_t omega_cdeg_s = 3438 + (int32_t)((1719LL * (int64_t)nc_sin_q15(mod_phase)) / NC_Q15_ONE);
        int32_t omega_true_bam_s = nc_cdeg_s_to_bam_s(omega_cdeg_s);
        int32_t gyro_noise_bam_s = nc_cdeg_s_to_bam_s(det_noise_cdeg(k, 11u, 180u));
        nc_bam_t mag_noise_bam = nc_cdeg_to_bam(det_noise_cdeg(k, 29u, 240u));
        int32_t d_sub_q8 = (int32_t)(((int64_t)omega_true_bam_s * 20LL * 256LL) / 1000LL);
        nc_bam_t psi_bam, raw_mag;

        psi_sub_q8 = (psi_sub_q8 + (uint32_t)d_sub_q8) & 0x00FFFFFFu;
        psi_bam = (nc_bam_t)((uint16_t)(((psi_sub_q8 + 128u) >> 8) & 0xFFFFu));
        raw_mag = nc_bam_add(harbor_warp_bam(psi_bam, 0), mag_noise_bam);

        nc_plastic_ring_update(
            &pr,
            omega_true_bam_s + gyro_bias_bam_s + gyro_noise_bam_s,
            20u,
            raw_mag,
            0, 0u, 1u
        );
    }

    for (k = 0; k < 72u; ++k) {
        nc_bam_t a_bam = nc_cdeg_to_bam(-18000 + (int32_t)k * 500);
        nc_bam_t w_bam = harbor_warp_bam(a_bam, 0);
        nc_bam_t corr_bam = 0;
        int16_t conf_q15 = 0;
        nc_plastic_ring_map(&pr, w_bam, &corr_bam, &conf_q15);
        raw_err_sum_cdeg += abs_i32(nc_bam_to_cdeg(nc_bam_sub(w_bam, a_bam)));
        learned_err_sum_cdeg += abs_i32(nc_bam_to_cdeg(nc_bam_sub(corr_bam, a_bam)));
    }

    raw_mean_cdeg = (int32_t)(raw_err_sum_cdeg / 72LL);
    learned_mean_cdeg = (int32_t)(learned_err_sum_cdeg / 72LL);

    printf("[10/14] C99 nc_plastic_ring_t self-supervised 31-deg harbor warp: raw=%d.%02d deg -> learned=%d.%02d deg (< 3.50) ... ",
           (int)(raw_mean_cdeg / 100), (int)(raw_mean_cdeg % 100),
           (int)(learned_mean_cdeg / 100), (int)(learned_mean_cdeg % 100));
    if (raw_mean_cdeg < 1400 || learned_mean_cdeg >= 350) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_plastic_ring_supervised_lock(void) {
    nc_plastic_ring_t pr;
    uint32_t k;
    nc_bam_t psi_bam = nc_cdeg_to_bam(-18000);
    int32_t omega_bam_s = nc_cdeg_s_to_bam_s(4500); /* 45 deg/s -> 8 s per revolution */
    int64_t learned_err_sum_cdeg = 0;
    int32_t learned_mean_cdeg;

    nc_plastic_ring_init(&pr);
    for (k = 0; k < 1000u; ++k) {
        nc_bam_t raw_mag;
        psi_bam = nc_bam_add(psi_bam, (nc_bam_t)((omega_bam_s * 20) / 1000));
        raw_mag = harbor_warp_bam(psi_bam, 1800);
        nc_plastic_ring_update(&pr, omega_bam_s, 20u, raw_mag, psi_bam, 1u, 1u);
    }

    for (k = 0; k < 72u; ++k) {
        nc_bam_t a_bam = nc_cdeg_to_bam(-18000 + (int32_t)k * 500);
        nc_bam_t w_bam = harbor_warp_bam(a_bam, 1800);
        nc_bam_t corr_bam = 0;
        int16_t conf_q15 = 0;
        nc_plastic_ring_map(&pr, w_bam, &corr_bam, &conf_q15);
        if (conf_q15 < 16000) {
            printf("[11/14] C99 nc_plastic_ring_t supervised lock ... FAIL (low map confidence)\n");
            return 1;
        }
        learned_err_sum_cdeg += abs_i32(nc_bam_to_cdeg(nc_bam_sub(corr_bam, a_bam)));
    }
    learned_mean_cdeg = (int32_t)(learned_err_sum_cdeg / 72LL);

    printf("[11/14] C99 nc_plastic_ring_t supervised sun/GNSS lock: mean_err=%d.%02d deg (< 2.00) ... ",
           (int)(learned_mean_cdeg / 100), (int)(learned_mean_cdeg % 100));
    if (!pr.has_abs_ref || learned_mean_cdeg >= 200) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_pfl2_180_antigoal_analytic(void) {
    nc_navigator_t nav;
    int32_t max_turn = nc_cdeg_s_to_bam_s(6000);
    int32_t cmd_179_ccw, cmd_179_cw, cmd_180;
    nc_bam_t heading;
    uint32_t step, rem_dist_mm = 0;
    nc_bam_t home_bam = 0;

    nc_navigator_init(&nav, 1500);
    nc_navigator_set_weights(&nav, NULL);
    nc_navigator_update(&nav, 20000, 1000u, 0);

    cmd_179_ccw = nc_navigator_steering_cmd(&nav, nc_cdeg_to_bam(100), max_turn);
    cmd_179_cw = nc_navigator_steering_cmd(&nav, nc_cdeg_to_bam(-100), max_turn);
    cmd_180 = nc_navigator_steering_cmd(&nav, 0, max_turn);

    heading = 0;
    for (step = 0; step < 1500u; ++step) {
        int32_t cmd, spd;
        nc_navigator_home_vector(&nav, &rem_dist_mm, &home_bam);
        if (rem_dist_mm < 200u) {
            break;
        }
        cmd = nc_navigator_steering_cmd(&nav, heading, max_turn);
        spd = (rem_dist_mm < 2000u) ? 400 : 1800;
        heading = nc_bam_add(heading, (nc_bam_t)((cmd * 20) / 1000));
        nc_navigator_update(&nav, spd, 20u, heading);
    }
    nc_navigator_home_vector(&nav, &rem_dist_mm, &home_bam);

    printf("[12/14] v2_analytic PFL2 179-180 deg turn-boost: cmd(179)=%+d, cmd(-179)=%+d, cmd(180)=%+d, miss=%u mm ... ",
           (int)nc_bam_s_to_cdeg_s(cmd_179_ccw),
           (int)nc_bam_s_to_cdeg_s(cmd_179_cw),
           (int)nc_bam_s_to_cdeg_s(cmd_180),
           (unsigned)rem_dist_mm);
    if (cmd_179_ccw < (max_turn * 3) / 4 ||
        cmd_179_cw > -(max_turn * 3) / 4 ||
        abs_i32(cmd_180) < (max_turn * 3) / 4 ||
        rem_dist_mm >= 300u) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_banc_pfl3_pfl2_dna02_uturn(void) {
    nc_compass_t comp;
    nc_navigator_t nav;
    int32_t max_turn = nc_cdeg_s_to_bam_s(6000);
    int32_t cmd_plus60, cmd_minus60, cmd_180;
    int16_t pfl2_at_180_q12;
    nc_bam_t heading;
    uint32_t step, rem_dist_mm = 0;
    nc_bam_t home_bam = 0;

    nc_compass_init(&comp);
    nc_navigator_init(&nav, 1500);

    /* Walk +20 m East (heading = 0) -> home is due West (+/-180 deg) */
    nc_navigator_update(&nav, 20000, 1000u, 0);

    /* Heading +120 deg -> hdg_err = +60 deg CCW left -> positive turn command */
    nc_compass_reset(&comp, nc_cdeg_to_bam(12000));
    cmd_plus60 = nc_navigator_steering_cmd_with_compass(&nav, &comp, nc_cdeg_to_bam(12000), max_turn);

    /* Heading -120 deg -> hdg_err = -60 deg CW right -> negative turn command */
    nc_compass_reset(&comp, nc_cdeg_to_bam(-12000));
    cmd_minus60 = nc_navigator_steering_cmd_with_compass(&nav, &comp, nc_cdeg_to_bam(-12000), max_turn);

    /* Exact 180-deg anti-home (heading = 0 deg) */
    nc_compass_reset(&comp, 0);
    cmd_180 = nc_navigator_steering_cmd_with_compass(&nav, &comp, 0, max_turn);
    pfl2_at_180_q12 = nav.last_pfl2_q12;

    heading = 0;
    for (step = 0; step < 1500u; ++step) {
        int32_t cmd, spd;
        nc_navigator_home_vector(&nav, &rem_dist_mm, &home_bam);
        if (rem_dist_mm < 200u) {
            break;
        }
        nc_compass_reset(&comp, heading);
        cmd = nc_navigator_steering_cmd_with_compass(&nav, &comp, heading, max_turn);
        spd = (rem_dist_mm < 2000u) ? 400 : 1800;
        heading = nc_bam_add(heading, (nc_bam_t)((cmd * 20) / 1000));
        nc_navigator_update(&nav, spd, 20u, heading);
    }
    nc_navigator_home_vector(&nav, &rem_dist_mm, &home_bam);

    printf("[13/14] BANC v888 PFL3+PFL2->DNa02: cmd(+60)=%+d, cmd(-60)=%+d, cmd(180)=%+d, pfl2_q12=%d, miss=%u mm ... ",
           (int)nc_bam_s_to_cdeg_s(cmd_plus60),
           (int)nc_bam_s_to_cdeg_s(cmd_minus60),
           (int)nc_bam_s_to_cdeg_s(cmd_180),
           (int)pfl2_at_180_q12,
           (unsigned)rem_dist_mm);
    if (cmd_plus60 <= 0 ||
        cmd_minus60 >= 0 ||
        abs_i32(cmd_180) < (max_turn * 3) / 4 ||
        pfl2_at_180_q12 < 1024 ||
        rem_dist_mm >= 500u) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

static int test_banc_connectome_vs_shuffled_weights(void) {
    nc_banc_cx_weights_t shuf;
    nc_compass_t comp_real, comp_shuf;
    uint32_t rng = 0xBADC0FFEu;
    uint32_t i, j, k;
    nc_bam_t psi_bam = nc_cdeg_to_bam(-9000);
    int32_t omega_bam_s = nc_cdeg_s_to_bam_s(3200);
    int64_t real_err_sum = 0, shuf_err_sum = 0;
    int32_t real_mean_cdeg, shuf_mean_cdeg;
    int16_t coh_real, coh_shuf;

    memcpy(&shuf, &k_nc_banc_cx_default, sizeof(shuf));
    /* Fisher-Yates shuffle of each synaptic matrix preserving exact weight distribution */
    for (i = 255u; i > 0u; --i) {
        j = det_u32(&rng) % (i + 1u);
        {
            uint16_t t = shuf.M_exc_q12[i];
            shuf.M_exc_q12[i] = shuf.M_exc_q12[j];
            shuf.M_exc_q12[j] = t;
        }
        {
            int16_t tp = shuf.M_pa_diff_q12[i];
            shuf.M_pa_diff_q12[i] = shuf.M_pa_diff_q12[j];
            shuf.M_pa_diff_q12[j] = tp;
        }
    }
    for (i = 639u; i > 0u; --i) {
        j = det_u32(&rng) % (i + 1u);
        {
            uint16_t t = shuf.M_ed7_q12[i];
            shuf.M_ed7_q12[i] = shuf.M_ed7_q12[j];
            shuf.M_ed7_q12[j] = t;
        }
        j = det_u32(&rng) % (i + 1u);
        {
            uint16_t t = shuf.M_d7e_q12[i];
            shuf.M_d7e_q12[i] = shuf.M_d7e_q12[j];
            shuf.M_d7e_q12[j] = t;
        }
    }

    nc_compass_init(&comp_real);
    nc_compass_reset(&comp_real, psi_bam);

    nc_compass_init(&comp_shuf);
    nc_compass_set_weights(&comp_shuf, &shuf);
    nc_compass_reset(&comp_shuf, psi_bam);

    for (k = 0; k < 600u; ++k) {
        nc_bam_t mag_bam, hr, hs;
        psi_bam = nc_bam_add(psi_bam, (nc_bam_t)((omega_bam_s * 20) / 1000));
        if (k >= 200u && k < 260u) {
            mag_bam = nc_bam_add(psi_bam, nc_cdeg_to_bam(13500));
        } else {
            mag_bam = nc_bam_add(psi_bam, nc_cdeg_to_bam(det_noise_cdeg(k, 7u, 300u)));
        }
        hr = nc_compass_update(&comp_real, omega_bam_s, 20u, mag_bam, 1u, NC_Q15_ONE, 0, 0u, 0);
        hs = nc_compass_update(&comp_shuf, omega_bam_s, 20u, mag_bam, 1u, NC_Q15_ONE, 0, 0u, 0);
        real_err_sum += abs_i32(nc_bam_to_cdeg(nc_bam_sub(hr, psi_bam)));
        shuf_err_sum += abs_i32(nc_bam_to_cdeg(nc_bam_sub(hs, psi_bam)));
    }

    real_mean_cdeg = (int32_t)(real_err_sum / 600LL);
    shuf_mean_cdeg = (int32_t)(shuf_err_sum / 600LL);
    coh_real = nc_compass_coherence_q15(&comp_real);
    coh_shuf = nc_compass_coherence_q15(&comp_shuf);

    printf("[14/14] BANC v888 vs shuffled CX: real_err=%d.%02d deg (coh=%d) vs shuf_err=%d.%02d deg (coh=%d) ... ",
           (int)(real_mean_cdeg / 100), (int)(real_mean_cdeg % 100), (int)coh_real,
           (int)(shuf_mean_cdeg / 100), (int)(shuf_mean_cdeg % 100), (int)coh_shuf);
    if (real_mean_cdeg >= 450 || shuf_mean_cdeg < real_mean_cdeg * 4 || coh_real <= coh_shuf) {
        printf("FAIL\n");
        return 1;
    }
    printf("PASS\n");
    return 0;
}

int main(void) {
    int failures = 0;
    printf("=== NeckCompass v%s C99 Fixed-Point (BANC v888 Q12/Q15 / Zero-Heap / Zero-FPU) Self-Test ===\n",
           NC_VERSION_STRING);
    failures += test_memory_footprint();
    failures += test_lut_trig_and_wrap();
    failures += test_full_360_integration_analytic();
    failures += test_banc_spike_rejection_and_bias_anchoring();
    failures += test_banc_gps_denied_return_to_origin();
    failures += test_update_latency();
    failures += test_reanchor_after_upset();
    failures += test_3d_tilt_dip_compensation();
    failures += test_9dof_wave_slam_gravity_gating();
    failures += test_plastic_ring_self_supervised_harbor();
    failures += test_plastic_ring_supervised_lock();
    failures += test_pfl2_180_antigoal_analytic();
    failures += test_banc_pfl3_pfl2_dna02_uturn();
    failures += test_banc_connectome_vs_shuffled_weights();

    if (failures == 0) {
        printf("=== ALL 14/14 C99 SELF-TESTS PASSED ===\n");
        return 0;
    }
    printf("=== %d C99 SELF-TEST(S) FAILED ===\n", failures);
    return 1;
}
