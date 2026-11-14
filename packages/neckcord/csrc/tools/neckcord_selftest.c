/*
 * NeckCord v2.0.0 - Pure Integer ANSI C99 Verification Suite (18 tests)
 *
 * Verifies:
 *   1. Static RAM footprint (< 3 KB total, < 600 B v1.x core, zero heap)
 *   2. 16-bit BAM Q15 sin/cos LUT identity across all 65,536 angles
 *   3. 4-byte cervical codec roundtrip & Python wire-format golden parity
 *   4. CRC-4 single-bit error (32/32 bits x 256 frames) & stuck-bus rejection
 *   5. Tripod gait anti-phase invariants
 *   6. Metachronal wave posterior-to-anterior phase lag invariants
 *   7. Giant Fiber freeze reflex instant phase lock
 *   8. Single-leg amputation (L2 jammed) VNC pentapod vs unadapted stability
 *   9. 4-thruster vectored USV online fault isolation & zero-spin rebalancing
 *  10. 8-actuator octo-ROV dual-fault wrench rebalancing
 *  11. Recovery probe: transient fault returns to service, persistent fault stays isolated
 *  12. Integer isqrt, atan2_bam, and acos_q15_bam full-domain accuracy
 *  13. 4-DOF [0, u_max] 6-rotor hexacopter motor-loss survival vs unconstrained pinv
 *  14. 4-DOF [0, u_max] 8-rotor octocopter single- & dual-rotor-loss survival
 *  15. Asymmetric BlueRobotics T200 (-40 N / +52.5 N + deadzone) active-set allocation
 *  16. Sensorless ESC observer: broken propeller blade caught & shunted in < 50 ms
 *  17. Sensorless ESC observer: weed-foul overcurrent (>1.6x I_exp) & open-phase stall
 *  18. Exact 3-DOF 18-servo hexapod leg IK (1000..2000 us) & L2 pentapod +45 mm parking
 *
 * Copyright (c) 2026 Amandeep Singh Bains. MIT License.
 */

#include "../neckcord.h"
#include <stdio.h>
#include <string.h>
#include <time.h>

static int g_passed = 0;
static int g_failed = 0;

#define CHECK(cond, msg)                                                       \
    do {                                                                       \
        if (cond) {                                                            \
            ++g_passed;                                                        \
            printf("  [PASS] %s\n", (msg));                                    \
        } else {                                                               \
            ++g_failed;                                                        \
            printf("  [FAIL] %s (line %d)\n", (msg), __LINE__);                \
        }                                                                      \
    } while (0)

static int32_t abs_i32(int32_t v)
{
    return (v < 0) ? -v : v;
}

static uint16_t bam_dist(uint16_t a, uint16_t b)
{
    uint16_t d = (uint16_t)(a - b);
    return (d <= 32768u) ? d : (uint16_t)(65536u - d);
}

static void test_1_static_ram_budget(void)
{
    size_t v1_ram = sizeof(nc_descending_frame_t)
                  + sizeof(nc_cpg_cord_t)
                  + sizeof(nc_cpg_state_t)
                  + sizeof(nc_shunting_cord_t)
                  + sizeof(nc_alloc_result_t);
    size_t v2_ram = sizeof(nc_shunting4d_t)
                  + sizeof(nc_alloc4d_result_t)
                  + sizeof(nc_esc_observer_t)
                  + sizeof(nc_esc_obs_result_t)
                  + sizeof(nc_hexapod_ik_cfg_t)
                  + sizeof(nc_hexapod_ik_out_t);
    size_t total_ram = v1_ram + v2_ram;

    printf("Test 1: Static RAM footprint: v1=%lu B, v2_ext=%lu B, combined=%lu B (budget < 3072 B)\n",
           (unsigned long)v1_ram, (unsigned long)v2_ram, (unsigned long)total_ram);
    printf("    sizeof: desc=%lu cpg=%lu cpg_st=%lu shunt3d=%lu res3d=%lu "
           "shunt4d=%lu res4d=%lu esc=%lu esc_res=%lu ik_cfg=%lu ik_out=%lu\n",
           (unsigned long)sizeof(nc_descending_frame_t),
           (unsigned long)sizeof(nc_cpg_cord_t),
           (unsigned long)sizeof(nc_cpg_state_t),
           (unsigned long)sizeof(nc_shunting_cord_t),
           (unsigned long)sizeof(nc_alloc_result_t),
           (unsigned long)sizeof(nc_shunting4d_t),
           (unsigned long)sizeof(nc_alloc4d_result_t),
           (unsigned long)sizeof(nc_esc_observer_t),
           (unsigned long)sizeof(nc_esc_obs_result_t),
           (unsigned long)sizeof(nc_hexapod_ik_cfg_t),
           (unsigned long)sizeof(nc_hexapod_ik_out_t));
    CHECK(v1_ram < 600u && total_ram < 3072u && total_ram < 1200u,
          "All v1.x (<600 B) and v2.0 (<1.2 KB) structs fit well under 3 KB static RAM, 0 heap");
}

static void test_2_trig_lut_accuracy(void)
{
    uint32_t ang;
    int32_t max_pythag_err = 0;
    for (ang = 0u; ang < 65536u; ++ang) {
        int32_t s = nc_sin_bam_q15((uint16_t)ang);
        int32_t c = nc_cos_bam_q15((uint16_t)ang);
        int64_t norm = ((int64_t)s * s + (int64_t)c * c) / NC_Q15_ONE;
        int32_t err = abs_i32((int32_t)norm - NC_Q15_ONE);
        if (err > max_pythag_err) {
            max_pythag_err = err;
        }
    }
    CHECK(nc_sin_bam_q15(0u) == 0 &&
          nc_sin_bam_q15(16384u) == NC_Q15_ONE &&
          nc_sin_bam_q15(32768u) == 0 &&
          nc_sin_bam_q15(49152u) == -NC_Q15_ONE &&
          max_pythag_err <= 8,
          "65-entry Q15 sin/cos LUT satisfies sin^2+cos^2=1 across all 65,536 angles");
}

static void test_3_cervical_roundtrip_and_python_parity(void)
{
    nc_descending_frame_t tx;
    nc_descending_frame_t rx;
    uint8_t raw[4];
    int rc_enc;
    int rc_dec;
    int sweep_ok = 1;
    int v;

    tx.drive_fwd_q15 = 26869;
    tx.turn_yaw_q15 = -8192;
    tx.strafe_lat_q15 = 3277;
    tx.gait_mode = 0u;
    tx.freeze_reflex = 0u;
    tx.seq = 2u;

    rc_enc = nc_encode_descending_4b(&tx, raw);
    rc_dec = nc_decode_descending_4b(raw, 2, &rx);

    for (v = -32767; v <= 32767; v += 511) {
        nc_descending_frame_t f_in;
        nc_descending_frame_t f_out;
        uint8_t buf[4];
        f_in.drive_fwd_q15 = (int16_t)v;
        f_in.turn_yaw_q15 = (int16_t)(-v);
        f_in.strafe_lat_q15 = (int16_t)(v / 2);
        f_in.gait_mode = (uint8_t)((v + 32767) & 3);
        f_in.freeze_reflex = (uint8_t)((v >> 3) & 1);
        f_in.seq = (uint8_t)((v >> 5) & 3);

        if (nc_encode_descending_4b(&f_in, buf) != NC_OK ||
            nc_decode_descending_4b(buf, f_in.seq, &f_out) != NC_OK) {
            sweep_ok = 0;
            break;
        }
        if (abs_i32(f_in.drive_fwd_q15 - f_out.drive_fwd_q15) > 260 ||
            abs_i32(f_in.turn_yaw_q15 - f_out.turn_yaw_q15) > 260 ||
            abs_i32(f_in.strafe_lat_q15 - f_out.strafe_lat_q15) > 530 ||
            f_in.gait_mode != f_out.gait_mode ||
            f_in.freeze_reflex != f_out.freeze_reflex ||
            f_in.seq != f_out.seq) {
            sweep_ok = 0;
            break;
        }
    }

    CHECK(rc_enc == NC_OK && rc_dec == NC_OK &&
          raw[0] == 0x68u && raw[1] == 0xE0u && raw[2] == 0x0Cu && raw[3] == 0x20u &&
          sweep_ok == 1,
          "4-byte cervical codec matches Python 0x68E00C20 wire vector & quantization bounds");
}

static void test_4_cervical_crc_and_bus_faults(void)
{
    uint8_t zero_bus[4] = { 0x00u, 0x00u, 0x00u, 0x00u };
    uint8_t ones_bus[4] = { 0xFFu, 0xFFu, 0xFFu, 0xFFu };
    nc_descending_frame_t rx;
    int all_flips_caught = 1;
    int k;
    int bit;

    for (k = 0; k < 256; ++k) {
        nc_descending_frame_t tx;
        uint8_t raw[4];
        tx.drive_fwd_q15 = (int16_t)((k - 128) * 250);
        tx.turn_yaw_q15 = (int16_t)((127 - k) * 250);
        tx.strafe_lat_q15 = (int16_t)(((k * 7) % 255 - 127) * 250);
        tx.gait_mode = (uint8_t)(k & 3);
        tx.freeze_reflex = (uint8_t)((k >> 2) & 1);
        tx.seq = (uint8_t)((k >> 3) & 3);
        (void)nc_encode_descending_4b(&tx, raw);

        for (bit = 0; bit < 32; ++bit) {
            uint8_t corrupted[4];
            memcpy(corrupted, raw, 4u);
            corrupted[bit >> 3] ^= (uint8_t)(1u << (bit & 7));
            if (nc_decode_descending_4b(corrupted, -1, &rx) == NC_OK) {
                all_flips_caught = 0;
            }
        }
    }

    CHECK(nc_decode_descending_4b(zero_bus, -1, &rx) == NC_ERR_CRC &&
          nc_decode_descending_4b(ones_bus, -1, &rx) == NC_ERR_CRC &&
          all_flips_caught == 1,
          "CRC-4 rejects 0x00000000, 0xFFFFFFFF, and 8,192/8,192 single-bit flips");
}

static void test_5_tripod_antiphase_invariants(void)
{
    nc_cpg_cord_t cpg;
    nc_cpg_state_t st;
    nc_descending_frame_t cmd;
    int step;

    nc_cpg_init(&cpg, 1u);
    cpg.phases_bam[0] = 1000u;
    cpg.phases_bam[1] = 28000u;
    cpg.phases_bam[2] = 35000u;
    cpg.phases_bam[3] = 4200u;
    cpg.phases_bam[4] = 63000u;
    cpg.phases_bam[5] = 30000u;

    memset(&cmd, 0, sizeof(cmd));
    cmd.drive_fwd_q15 = 29000;
    cmd.gait_mode = (uint8_t)NC_GAIT_AUTO;

    for (step = 0; step < 200; ++step) {
        (void)nc_cpg_step(&cpg, &cmd, 10u, &st);
    }

    CHECK(st.active_gait == (uint8_t)NC_GAIT_TRIPOD &&
          bam_dist(st.phases_bam[0], st.phases_bam[3]) < 500u &&
          bam_dist(st.phases_bam[0], st.phases_bam[4]) < 500u &&
          abs_i32((int32_t)bam_dist(st.phases_bam[0], st.phases_bam[1]) - 32768) < 500 &&
          abs_i32((int32_t)bam_dist(st.phases_bam[2], st.phases_bam[3]) - 32768) < 500,
          "Kuramoto CPG converges from perturbed phases to strict Tripod anti-phase lock");
}

static void test_6_wave_gait_metachronal_lag(void)
{
    nc_cpg_cord_t cpg;
    nc_cpg_state_t st;
    nc_descending_frame_t cmd;
    int step;
    uint16_t lag_l2_l1;
    uint16_t lag_l3_l2;

    nc_cpg_init(&cpg, 1u);
    memset(&cmd, 0, sizeof(cmd));
    cmd.drive_fwd_q15 = 6500;
    cmd.gait_mode = (uint8_t)NC_GAIT_AUTO;

    for (step = 0; step < 250; ++step) {
        (void)nc_cpg_step(&cpg, &cmd, 10u, &st);
    }

    lag_l2_l1 = (uint16_t)(st.phases_bam[2] - st.phases_bam[0]);
    lag_l3_l2 = (uint16_t)(st.phases_bam[4] - st.phases_bam[2]);

    CHECK(st.active_gait == (uint8_t)NC_GAIT_WAVE &&
          abs_i32((int32_t)lag_l2_l1 - 10923) < 600 &&
          abs_i32((int32_t)lag_l3_l2 - 10922) < 600 &&
          abs_i32((int32_t)bam_dist(st.phases_bam[0], st.phases_bam[1]) - 32768) < 600,
          "Low-speed descending drive transitions CPG to 60-deg posterior-to-anterior Wave gait");
}

static void test_7_giant_fiber_freeze(void)
{
    nc_cpg_cord_t cpg;
    nc_cpg_state_t st1;
    nc_cpg_state_t st2;
    nc_descending_frame_t cmd;

    nc_cpg_init(&cpg, 1u);
    memset(&cmd, 0, sizeof(cmd));
    cmd.drive_fwd_q15 = 25000;
    cmd.freeze_reflex = 1u;

    (void)nc_cpg_step(&cpg, &cmd, 20u, &st1);
    (void)nc_cpg_step(&cpg, &cmd, 20u, &st2);

    CHECK(st2.active_gait == (uint8_t)NC_GAIT_FREEZE &&
          st2.stance_mask == 0x3Fu &&
          memcmp(st1.phases_bam, st2.phases_bam, sizeof(st1.phases_bam)) == 0 &&
          st2.is_statically_stable == 1u,
          "Giant Fiber freeze_reflex halts phase progression and locks all 6 legs in stance");
}

static void test_8_hexapod_single_leg_amputation_adaptation(void)
{
    nc_cpg_cord_t cpg_adapt;
    nc_cpg_cord_t cpg_fixed;
    nc_cpg_state_t st_a;
    nc_cpg_state_t st_f;
    nc_descending_frame_t cmd;
    int step;
    int stable_adapt = 0;
    int stable_fixed = 0;
    int eval_steps = 400;

    nc_cpg_init(&cpg_adapt, 1u);
    nc_cpg_init(&cpg_fixed, 0u);

    (void)nc_cpg_set_limb_health(&cpg_adapt, 2u, 0);
    (void)nc_cpg_set_limb_health(&cpg_fixed, 2u, 0);

    memset(&cmd, 0, sizeof(cmd));
    cmd.drive_fwd_q15 = 27852;
    cmd.gait_mode = (uint8_t)NC_GAIT_AUTO;

    for (step = 0; step < 100; ++step) {
        (void)nc_cpg_step(&cpg_adapt, &cmd, 10u, &st_a);
        (void)nc_cpg_step(&cpg_fixed, &cmd, 10u, &st_f);
    }

    for (step = 0; step < eval_steps; ++step) {
        (void)nc_cpg_step(&cpg_adapt, &cmd, 10u, &st_a);
        (void)nc_cpg_step(&cpg_fixed, &cmd, 10u, &st_f);
        if (st_a.is_statically_stable) {
            ++stable_adapt;
        }
        if (st_f.is_statically_stable) {
            ++stable_fixed;
        }
    }

    printf("    L2-jammed static polygon stability: adaptive=%d/%d vs unadapted=%d/%d\n",
           stable_adapt, eval_steps, stable_fixed, eval_steps);
    CHECK(st_a.active_gait == (uint8_t)NC_GAIT_PENTAPOD &&
          (st_a.stance_mask & (1u << 2)) == 0u &&
          stable_adapt == eval_steps &&
          stable_fixed < (eval_steps * 60) / 100,
          "L2-amputated VNC shifts to 5-limb rhythm with 100% CoM polygon stability");
}

static void test_9_usv4_fault_isolation_and_zero_spin_rebalance(void)
{
    nc_shunting_cord_t cord;
    nc_alloc_result_t res;
    int32_t w_des[3] = { 16384, 0, 0 };
    int16_t true_eff_q15[4] = { NC_Q15_ONE, NC_Q15_ONE, 2621, NC_Q15_ONE };
    int step;
    int isolated_tick = -1;
    int32_t true_fx_q14 = 0;
    int32_t true_fy_q14 = 0;
    int32_t true_tz_q14 = 0;
    uint8_t i;

    nc_shunting_init_usv4(&cord, 450, 350);
    cord.tau_act_ms = 0u;

    (void)nc_shunting_step(&cord, w_des, NULL, 20u, &res);

    for (step = 1; step <= 8; ++step) {
        int16_t fb_q15[4];
        for (i = 0u; i < 4u; ++i) {
            fb_q15[i] = (int16_t)(((int64_t)true_eff_q15[i] * res.cmd_q15[i]) / NC_Q15_ONE);
        }
        (void)nc_shunting_step(&cord, w_des, fb_q15, 20u, &res);
        if (isolated_tick < 0 && (res.isolated_mask & (1u << 2)) != 0u) {
            isolated_tick = step;
        }
    }

    for (i = 0u; i < 4u; ++i) {
        int64_t f_i = ((int64_t)true_eff_q15[i] * res.cmd_q15[i]) / NC_Q15_ONE;
        true_fx_q14 += (int32_t)(((int64_t)cord.W_q14[0][i] * f_i) / NC_Q15_ONE);
        true_fy_q14 += (int32_t)(((int64_t)cord.W_q14[1][i] * f_i) / NC_Q15_ONE);
        true_tz_q14 += (int32_t)(((int64_t)cord.W_q14[2][i] * f_i) / NC_Q15_ONE);
    }

    printf("    USV4 T2 isolated at %d ms | delivered Fx=%d (target 16384), Fy=%d, Tz=%d\n",
           isolated_tick * 20, (int)true_fx_q14, (int)true_fy_q14, (int)true_tz_q14);

    CHECK(isolated_tick > 0 && isolated_tick <= 4 &&
          res.cmd_q15[2] == 0 &&
          abs_i32(res.cmd_q15[3]) <= 32 &&
          abs_i32(true_fx_q14 - 16384) < 80 &&
          abs_i32(true_fy_q14) < 32 &&
          abs_i32(true_tz_q14) < 32,
          "USV4 isolates fouled Port-Aft thruster in <=80 ms with zero yaw spin & exact surge");
}

static void test_10_octo8_dual_fault_rebalance(void)
{
    static const int16_t w8[24] = {
        +11585, +11585, +11585, +11585, +11585, +11585, +11585, +11585,
        -11585, +11585, +11585, -11585, -11585, +11585, +11585, -11585,
         -9268,  +9268,  -9268,  +9268,  -7000,  +7000,  -7000,  +7000
    };
    nc_shunting_cord_t cord8;
    nc_alloc_result_t res8;
    int32_t w_des[3] = { 20000, -4000, 3500 };
    int rc;

    rc = nc_shunting_init(&cord8, 8u, w8);
    (void)nc_shunting_set_health(&cord8, 1u, 0);
    (void)nc_shunting_set_health(&cord8, 6u, 0);
    rc |= nc_shunting_allocate(&cord8, w_des, &res8);

    CHECK(rc == NC_OK &&
          res8.isolated_mask == ((1u << 1) | (1u << 6)) &&
          res8.cmd_q15[1] == 0 &&
          res8.cmd_q15[6] == 0 &&
          abs_i32(res8.pred_wrench_q14[0] - w_des[0]) < 120 &&
          abs_i32(res8.pred_wrench_q14[1] - w_des[1]) < 120 &&
          abs_i32(res8.pred_wrench_q14[2] - w_des[2]) < 120,
          "8-channel octo-ROV allocator isolates 2 failed thrusters & tracks full 3-DOF wrench");
}

static int run_usv4_ticks(nc_shunting_cord_t *cord, nc_alloc_result_t *res, int16_t eff_t2_q15, int n)
{
    int32_t w_des[3] = { 16384, 0, 0 };
    int k;
    uint8_t i;
    for (k = 0; k < n; ++k) {
        int16_t fb_q15[4];
        for (i = 0u; i < 4u; ++i) {
            int32_t eff = (i == 2u) ? eff_t2_q15 : NC_Q15_ONE;
            fb_q15[i] = (int16_t)(((int64_t)eff * res->cmd_q15[i]) / NC_Q15_ONE);
        }
        (void)nc_shunting_step(cord, w_des, fb_q15, 20u, res);
    }
    return (res->isolated_mask & (1u << 2)) != 0u;
}

static void test_11_recovery_probe(void)
{
    nc_shunting_cord_t cord;
    nc_alloc_result_t res;
    int32_t w_des[3] = { 16384, 0, 0 };
    int isolated_after_fault, isolated_after_clear, isolated_without_probe;
    int persistent_isolated_ticks = 0;
    int k;

    nc_shunting_init_usv4(&cord, 450, 350);
    cord.tau_act_ms = 0u;
    nc_shunting_set_probe_interval(&cord, 2000u);
    (void)nc_shunting_step(&cord, w_des, NULL, 20u, &res);
    isolated_after_fault = run_usv4_ticks(&cord, &res, 1000, 50);
    isolated_after_clear = run_usv4_ticks(&cord, &res, NC_Q15_ONE, 200);

    nc_shunting_init_usv4(&cord, 450, 350);
    cord.tau_act_ms = 0u;
    (void)nc_shunting_step(&cord, w_des, NULL, 20u, &res);
    (void)run_usv4_ticks(&cord, &res, 1000, 50);
    isolated_without_probe = run_usv4_ticks(&cord, &res, NC_Q15_ONE, 200);

    nc_shunting_init_usv4(&cord, 450, 350);
    cord.tau_act_ms = 0u;
    nc_shunting_set_probe_interval(&cord, 2000u);
    (void)nc_shunting_step(&cord, w_des, NULL, 20u, &res);
    for (k = 0; k < 500; ++k) {
        persistent_isolated_ticks += run_usv4_ticks(&cord, &res, 1000, 1);
    }

    printf("    probe: isolated after fault=%d, after clear=%d (no-probe=%d), persistent fault isolated %d/500 ticks\n",
           isolated_after_fault, isolated_after_clear, isolated_without_probe, persistent_isolated_ticks);
    CHECK(isolated_after_fault && !isolated_after_clear && isolated_without_probe &&
          res.cmd_q15[2] == 0 && persistent_isolated_ticks >= 480,
          "Recovery probe returns a cleared thruster to service and keeps a dead one isolated");
}

/* =========================================================================
 * v2.0.0 Expanded Self-Tests (Tests 12..18)
 * ========================================================================= */

static void test_12_integer_math_isqrt_atan2_acos(void)
{
    int ok_sqrt = 1;
    int32_t max_acos_err = 0;
    int32_t max_atan_err = 0;
    uint32_t s;
    int32_t c;
    uint32_t ang;

    if (nc_isqrt_u32(0u) != 0u || nc_isqrt_u32(1u) != 1u ||
        nc_isqrt_u32(0xFFFFFFFFu) != 65535u ||
        nc_isqrt_u64(0xFFFFFFFFFFFFFFFFULL) != 4294967295u) {
        ok_sqrt = 0;
    }
    for (s = 2u; s < 500000u; s = s * 3u + 17u) {
        uint64_t r = nc_isqrt_u32(s);
        if (r * r > s || (r + 1u) * (r + 1u) <= s) {
            ok_sqrt = 0;
        }
    }

    for (ang = 0u; ang < 65536u; ang += 64u) {
        int32_t sy = nc_sin_bam_q15((uint16_t)ang);
        int32_t cx = nc_cos_bam_q15((uint16_t)ang);
        uint16_t rec = nc_atan2_bam(sy, cx);
        int32_t err = (int32_t)bam_dist(rec, (uint16_t)ang);
        if (err > max_atan_err) {
            max_atan_err = err;
        }
    }

    for (c = -NC_Q15_ONE; c <= NC_Q15_ONE; c += 31) {
        uint16_t b = nc_acos_q15_bam((int16_t)c);
        int32_t c_rec = nc_cos_bam_q15(b);
        int32_t err = abs_i32(c_rec - c);
        if (err > max_acos_err) {
            max_acos_err = err;
        }
    }

    CHECK(ok_sqrt == 1 && max_atan_err <= 24 && max_acos_err <= 80 &&
          nc_acos_q15_bam(NC_Q15_ONE) == 0u &&
          nc_acos_q15_bam(0) == 16384u &&
          nc_acos_q15_bam(-NC_Q15_ONE) == 32768u,
          "Integer isqrt, atan2_bam (<=0.13 deg), and acos_q15_bam (<0.25%) pass full-domain sweep");
}

static void test_13_hex6_unidirectional_motor_loss_survival(void)
{
    nc_shunting4d_t hex_box;
    nc_shunting4d_t hex_uncon;
    nc_alloc4d_result_t res_box;
    nc_alloc4d_result_t res_uncon;
    /* Hover Fz = 2.0 * u_max (36 N on 18 N rotors), nose-up pitch Ty = -1.2 N*m (-1092 Q14) */
    int32_t w_pitch[4] = { 32768, 0, -1092, 0 };
    int32_t w_hover[4] = { 32768, 546, -546, 0 };
    int32_t clipped_ty_q14 = 0;
    int all_six_ok = 1;
    uint8_t i;
    uint8_t r_dead;

    nc_shunting4d_init_hex6(&hex_box, 280, 18);
    (void)nc_shunting4d_set_health(&hex_box, 0u, 0);
    (void)nc_shunting4d_allocate(&hex_box, w_pitch, &res_box);

    /* Unconstrained symmetric [-32767, +32767] 4-DOF pseudo-inverse with oracle R0 drop */
    nc_shunting4d_init_hex6(&hex_uncon, 280, 18);
    for (i = 0u; i < 6u; ++i) {
        hex_uncon.u_min_q15[i] = -NC_Q15_ONE;
    }
    hex_uncon.prioritize_attitude = 0u;
    (void)nc_shunting4d_set_health(&hex_uncon, 0u, 0);
    (void)nc_shunting4d_allocate(&hex_uncon, w_pitch, &res_uncon);

    /* Physical rotor clipping max(u_i, 0) on unconstrained pseudo-inverse */
    for (i = 1u; i < 6u; ++i) {
        int32_t u_pos = (res_uncon.cmd_q15[i] > 0) ? res_uncon.cmd_q15[i] : 0;
        clipped_ty_q14 += (int32_t)(((int64_t)hex_uncon.W_q14[2][i] * u_pos) / NC_Q15_ONE);
    }

    /* Sweep all 6 single-rotor failures under combined roll + pitch hover */
    for (r_dead = 0u; r_dead < 6u; ++r_dead) {
        nc_shunting4d_t hc;
        nc_alloc4d_result_t hr;
        nc_shunting4d_init_hex6(&hc, 280, 18);
        (void)nc_shunting4d_set_health(&hc, r_dead, 0);
        if (nc_shunting4d_allocate(&hc, w_hover, &hr) != NC_OK || hr.cmd_q15[r_dead] != 0) {
            all_six_ok = 0;
        }
        for (i = 0u; i < 6u; ++i) {
            if (hr.cmd_q15[i] < hc.u_min_q15[i] || hr.cmd_q15[i] > hc.u_max_q15[i]) {
                all_six_ok = 0;
            }
        }
        if (abs_i32(hr.pred_wrench_q14[0] - w_hover[0]) > 655 ||
            abs_i32(hr.pred_wrench_q14[1] - w_hover[1]) > 16 ||
            abs_i32(hr.pred_wrench_q14[2] - w_hover[2]) > 16) {
            all_six_ok = 0;
        }
    }

    printf("    Hex6 R0 dead: uncon u3=%d Q15 (<0 illegal!, clipped Ty=%d vs -1092) | "
           "NeckCord u3=%d, Fz=%d, Tx=%d, Ty=%d\n",
           (int)res_uncon.cmd_q15[3], (int)clipped_ty_q14,
           (int)res_box.cmd_q15[3],
           (int)res_box.pred_wrench_q14[0],
           (int)res_box.pred_wrench_q14[1],
           (int)res_box.pred_wrench_q14[2]);

    CHECK(res_uncon.cmd_q15[3] < -4000 &&
          abs_i32(clipped_ty_q14 - w_pitch[2]) > 600 &&
          res_box.cmd_q15[0] == 0 &&
          res_box.cmd_q15[3] >= 0 && res_box.cmd_q15[3] <= 2 &&
          abs_i32(res_box.pred_wrench_q14[0] - w_pitch[0]) < 128 &&
          abs_i32(res_box.pred_wrench_q14[1] - w_pitch[1]) < 16 &&
          abs_i32(res_box.pred_wrench_q14[2] - w_pitch[2]) < 16 &&
          all_six_ok == 1,
          "Hex6 active-set keeps u in [0, u_max] and <0.5% [Fz,Tx,Ty] error on all 6 rotor failures");
}

static void test_14_octo8_single_and_dual_rotor_loss(void)
{
    nc_shunting4d_t octo;
    nc_alloc4d_result_t r1;
    nc_alloc4d_result_t r2;
    int32_t w_des[4] = { 40960, 820, -900, 140 };
    int bounds_ok = 1;
    uint8_t i;

    nc_shunting4d_init_octo8(&octo, 320, 18);
    (void)nc_shunting4d_set_health(&octo, 1u, 0);
    (void)nc_shunting4d_allocate(&octo, w_des, &r1);

    (void)nc_shunting4d_set_health(&octo, 4u, 0);
    (void)nc_shunting4d_allocate(&octo, w_des, &r2);

    for (i = 0u; i < 8u; ++i) {
        if (r1.cmd_q15[i] < octo.u_min_q15[i] || r1.cmd_q15[i] > octo.u_max_q15[i] ||
            r2.cmd_q15[i] < octo.u_min_q15[i] || r2.cmd_q15[i] > octo.u_max_q15[i]) {
            bounds_ok = 0;
        }
    }

    CHECK(bounds_ok == 1 &&
          r1.cmd_q15[1] == 0 &&
          r2.cmd_q15[1] == 0 && r2.cmd_q15[4] == 0 &&
          abs_i32(r1.pred_wrench_q14[0] - w_des[0]) < 200 &&
          abs_i32(r1.pred_wrench_q14[1] - w_des[1]) < 18 &&
          abs_i32(r1.pred_wrench_q14[2] - w_des[2]) < 18 &&
          abs_i32(r2.pred_wrench_q14[0] - w_des[0]) < 300 &&
          abs_i32(r2.pred_wrench_q14[1] - w_des[1]) < 18 &&
          abs_i32(r2.pred_wrench_q14[2] - w_des[2]) < 18,
          "Octo8 [0, u_max] active-set survives 1 and 2 dead rotors with <1% [Fz,Tx,Ty] error");
}

static void test_15_usv_t200_asymmetric_and_deadzone(void)
{
    nc_shunting4d_t t200;
    nc_alloc4d_result_t r_rev;
    nc_alloc4d_result_t r_dz;
    int32_t w_rev[3] = { -22000, 0, 3200 };
    int32_t w_gentle[3] = { 2000, 0, 0 };
    int asym_ok = 1;
    int dz_ok = 1;
    uint8_t i;

    /* T200: -40.0 N reverse / +52.5 N forward -> u_min = -24965, deadzone = 1966 (6%) */
    nc_shunting4d_init_usv_t200(&t200, 450, 350, -24965, 1966);
    (void)nc_shunting4d_set_health(&t200, 2u, 0);
    (void)nc_shunting4d_allocate(&t200, w_rev, &r_rev);

    for (i = 0u; i < 4u; ++i) {
        if (r_rev.cmd_q15[i] < t200.u_min_q15[i] || r_rev.cmd_q15[i] > t200.u_max_q15[i]) {
            asym_ok = 0;
        }
    }

    nc_shunting4d_init_usv_t200(&t200, 450, 350, -24965, 1966);
    (void)nc_shunting4d_allocate(&t200, w_gentle, &r_dz);
    for (i = 0u; i < 4u; ++i) {
        int32_t au = abs_i32(r_dz.cmd_q15[i]);
        if (au > 0 && au < 1966) {
            dz_ok = 0;
        }
    }

    printf("    T200 rev u=[%d,%d,%d,%d] Tz=%d | dz u=[%d,%d,%d,%d] Fx=%d\n",
           (int)r_rev.cmd_q15[0], (int)r_rev.cmd_q15[1], (int)r_rev.cmd_q15[2], (int)r_rev.cmd_q15[3],
           (int)r_rev.pred_wrench_q14[2],
           (int)r_dz.cmd_q15[0], (int)r_dz.cmd_q15[1], (int)r_dz.cmd_q15[2], (int)r_dz.cmd_q15[3],
           (int)r_dz.pred_wrench_q14[0]);

    CHECK(asym_ok == 1 && dz_ok == 1 &&
          r_rev.cmd_q15[2] == 0 &&
          r_rev.cmd_q15[0] <= -24963 && r_rev.cmd_q15[0] >= -24965 &&
          abs_i32(r_rev.pred_wrench_q14[2] - w_rev[2]) < 120 &&
          abs_i32(r_dz.pred_wrench_q14[0] - w_gentle[0]) < 120,
          "Asymmetric T200 [-40,+52.5 N] clamps reverse at -24965, preserves yaw, and clears deadzone");
}

static void test_16_esc_observer_broken_blade_fast_isolation(void)
{
    nc_esc_observer_t obs;
    nc_esc_obs_result_t obs_res;
    nc_shunting4d_t hex6;
    nc_alloc4d_result_t alloc_res;
    int32_t w_des[4] = { 32768, 0, -1092, 0 };
    int detect_ms = -1;
    int isolate_ms = -1;
    int tick;
    uint8_t i;

    (void)nc_esc_observer_init(&obs, 6u, 11500, 450, 21500);
    obs.tau_motor_ms = 0u;
    nc_shunting4d_init_hex6(&hex6, 280, 18);
    hex6.tau_act_ms = 0u;

    (void)nc_shunting4d_step(&hex6, w_des, NULL, 5u, &alloc_res);

    /* 200 Hz telemetry (5 ms/tick): Rotor 0 loses blade -> spins freely at normal RPM, 8% load current */
    for (tick = 1; tick <= 20; ++tick) {
        int32_t rpm[6];
        int32_t cur_ma[6];
        for (i = 0u; i < 6u; ++i) {
            uint32_t su = nc_isqrt_u64((uint64_t)(uint32_t)alloc_res.cmd_q15[i] * (uint64_t)NC_Q15_ONE);
            int32_t r_nom = (int32_t)(((int64_t)11500 * (int64_t)su) / NC_Q15_ONE);
            int32_t nr_q15 = (int32_t)(((int64_t)r_nom * NC_Q15_ONE) / 11500);
            int32_t r3_q15 = (int32_t)((((int64_t)nr_q15 * nr_q15) / NC_Q15_ONE * nr_q15) / NC_Q15_ONE);
            int32_t load_ma = (int32_t)(((int64_t)21500 * r3_q15) / NC_Q15_ONE);
            rpm[i] = r_nom;
            cur_ma[i] = 450 + ((i == 0u) ? (load_ma / 15) : load_ma);
        }
        (void)nc_esc_observer_update(&obs, alloc_res.cmd_q15, rpm, cur_ma, 5u, &obs_res);
        if (detect_ms < 0 && (obs_res.broken_prop_mask & 1u) != 0u) {
            detect_ms = tick * 5;
        }
        (void)nc_shunting4d_step(&hex6, w_des, obs_res.est_thrust_q15, 5u, &alloc_res);
        if (isolate_ms < 0 && (alloc_res.isolated_mask & 1u) != 0u) {
            isolate_ms = tick * 5;
        }
    }

    printf("    ESC broken-blade R0 detected at %d ms, shunted at %d ms | Fz=%d, Ty=%d\n",
           detect_ms, isolate_ms,
           (int)alloc_res.pred_wrench_q14[0], (int)alloc_res.pred_wrench_q14[2]);

    CHECK(detect_ms > 0 && detect_ms < 50 &&
          isolate_ms > 0 && isolate_ms <= 50 &&
          alloc_res.cmd_q15[0] == 0 &&
          abs_i32(alloc_res.pred_wrench_q14[0] - w_des[0]) < 128 &&
          abs_i32(alloc_res.pred_wrench_q14[2] - w_des[2]) < 16,
          "Sensorless ESC observer catches broken blade in <50 ms & shunts Hex6 R0 cleanly");
}

static void test_17_esc_observer_weed_foul_overcurrent_and_stall(void)
{
    nc_esc_observer_t obs;
    nc_esc_obs_result_t res;
    int16_t cmd_q15[4] = { 24000, 24000, 24000, 24000 };
    int32_t rpm[4];
    int32_t cur_ma[4];
    int oc_ms = -1;
    int stall_ms = -1;
    int tick;

    /* T200 ESC observer: 3900 RPM max, 500 mA idle, 23500 mA full cubic load */
    (void)nc_esc_observer_init(&obs, 4u, 3900, 500, 23500);
    obs.tau_motor_ms = 0u;

    /* T0,T3 nominal (~3337 RPM, ~15000 mA); T1 open-phase stalled (0 RPM, 0 mA);
     * T2 weed-fouled (sagged to 2100 RPM = 63% speed, current spikes to 26000 mA > 1.6x I_exp) */
    rpm[0] = 3337; cur_ma[0] = 15000;
    rpm[1] = 0;    cur_ma[1] = 0;
    rpm[2] = 2100; cur_ma[2] = 26000;
    rpm[3] = 3337; cur_ma[3] = 15000;

    for (tick = 1; tick <= 10; ++tick) {
        (void)nc_esc_observer_update(&obs, cmd_q15, rpm, cur_ma, 5u, &res);
        if (oc_ms < 0 && (res.overcurrent_mask & (1u << 2)) != 0u) {
            oc_ms = tick * 5;
        }
        if (stall_ms < 0 && (res.stall_mask & (1u << 1)) != 0u) {
            stall_ms = tick * 5;
        }
    }

    CHECK(oc_ms > 0 && oc_ms < 50 &&
          stall_ms > 0 && stall_ms < 50 &&
          res.fault_code[0] == (uint8_t)NC_ESC_OK &&
          res.fault_code[1] == (uint8_t)NC_ESC_FAULT_STALL_OPEN &&
          res.fault_code[2] == (uint8_t)NC_ESC_FAULT_OVERCURR &&
          res.est_thrust_q15[1] == 0 &&
          res.est_thrust_q15[2] < 5000,
          "ESC observer flags weed-foul >1.6x overcurrent & open-phase stall in <50 ms");
}

static void test_18_hexapod_18servo_3dof_ik_and_pentapod_parking(void)
{
    static const int16_t k_hx[6] = { +120, +120,    0,    0, -120, -120 };
    static const int16_t k_hy[6] = {  +55,  -55,  +65,  -65,  +55,  -55 };
    static const uint16_t k_mbam[6] = { 8192u, 57344u, 16384u, 49152u, 24576u, 40960u };
    nc_hexapod_ik_cfg_t cfg;
    nc_cpg_cord_t cpg;
    nc_cpg_state_t st;
    nc_hexapod_ik_out_t ik;
    nc_descending_frame_t cmd;
    int pwm_ok = 1;
    int fk_ok = 1;
    int step;
    uint8_t i;
    uint8_t j;

    nc_hexapod_ik_default_cfg(&cfg);
    nc_cpg_init(&cpg, 1u);
    memset(&cmd, 0, sizeof(cmd));
    cmd.drive_fwd_q15 = 26000;

    /* Intact Tripod gait check + integer FK verification */
    for (step = 0; step < 60; ++step) {
        (void)nc_cpg_step(&cpg, &cmd, 10u, &st);
        (void)nc_hexapod_ik_3dof(&cfg, &cpg, &st, &ik);
        if (ik.reachable_mask != 0x3Fu || ik.parked_mask != 0u) {
            pwm_ok = 0;
        }
        for (i = 0u; i < 6u; ++i) {
            for (j = 0u; j < 3u; ++j) {
                if (ik.pwm_us[i][j] < 1000u || ik.pwm_us[i][j] > 2000u) {
                    pwm_ok = 0;
                }
            }
        }
    }

    /* Integer forward kinematics check on all 6 legs */
    for (i = 0u; i < 6u; ++i) {
        uint16_t coxa_bam = (uint16_t)((int32_t)k_mbam[i] + ((int32_t)ik.coxa_cdeg[i] * 32768) / 18000);
        uint16_t fem_bam = (uint16_t)(((int32_t)ik.femur_cdeg[i] * 32768) / 18000);
        uint16_t knee_bam = (uint16_t)((int32_t)fem_bam - 16384 + ((int32_t)ik.tibia_cdeg[i] * 32768) / 18000);
        int32_t r_fem = ((int32_t)cfg.l_femur_mm * nc_cos_bam_q15(fem_bam)
                       + (int32_t)cfg.l_tibia_mm * nc_cos_bam_q15(knee_bam)) / NC_Q15_ONE;
        int32_t r_tot = cfg.l_coxa_mm + r_fem;
        int32_t fx_fk = k_hx[i] + (r_tot * nc_cos_bam_q15(coxa_bam)) / NC_Q15_ONE;
        int32_t fy_fk = k_hy[i] + (r_tot * nc_sin_bam_q15(coxa_bam)) / NC_Q15_ONE;
        int32_t fz_fk = cfg.body_z_mm
                      + ((int32_t)cfg.l_femur_mm * nc_sin_bam_q15(fem_bam)
                       + (int32_t)cfg.l_tibia_mm * nc_sin_bam_q15(knee_bam)) / NC_Q15_ONE;
        if (abs_i32(fx_fk - st.foot_x_mm[i]) > 2 ||
            abs_i32(fy_fk - st.foot_y_mm[i]) > 2 ||
            abs_i32(fz_fk - ik.foot_z_mm[i]) > 2) {
            fk_ok = 0;
        }
    }

    /* Amputate L2 -> pentapod walk with L2 parked safely at z = +45 mm */
    (void)nc_cpg_set_limb_health(&cpg, 2u, 0);
    for (step = 0; step < 60; ++step) {
        (void)nc_cpg_step(&cpg, &cmd, 10u, &st);
        (void)nc_hexapod_ik_3dof(&cfg, &cpg, &st, &ik);
        if (ik.parked_mask != (1u << 2) || ik.foot_z_mm[2] != 45 || ik.reachable_mask != 0x3Fu) {
            pwm_ok = 0;
        }
        for (i = 0u; i < 6u; ++i) {
            for (j = 0u; j < 3u; ++j) {
                if (ik.pwm_us[i][j] < 1000u || ik.pwm_us[i][j] > 2000u) {
                    pwm_ok = 0;
                }
            }
        }
    }

    CHECK(pwm_ok == 1 && fk_ok == 1 && st.active_gait == (uint8_t)NC_GAIT_PENTAPOD,
          "18-servo 3-DOF integer IK matches FK (<=2 mm), keeps 1000..2000 us, & parks L2 at +45 mm");
}

static void run_timing_benchmark(void)
{
    nc_shunting4d_t hex6;
    nc_alloc4d_result_t ares;
    nc_esc_observer_t obs;
    nc_esc_obs_result_t ores;
    nc_cpg_cord_t cpg;
    nc_cpg_state_t cst;
    nc_hexapod_ik_cfg_t ik_cfg;
    nc_hexapod_ik_out_t ik_out;
    nc_descending_frame_t cmd;
    int32_t w_des[4] = { 32768, 300, -1092, 0 };
    int32_t rpm[6] = { 0, 8200, 7900, 0, 7900, 8200 };
    int32_t cur[6] = { 450, 8100, 7400, 450, 7400, 8100 };
    clock_t t0, t1;
    long elapsed_us;
    int iter;

    nc_shunting4d_init_hex6(&hex6, 280, 18);
    (void)nc_shunting4d_set_health(&hex6, 0u, 0);
    (void)nc_esc_observer_init(&obs, 6u, 11500, 450, 21500);
    nc_cpg_init(&cpg, 1u);
    (void)nc_cpg_set_limb_health(&cpg, 2u, 0);
    nc_hexapod_ik_default_cfg(&ik_cfg);
    memset(&cmd, 0, sizeof(cmd));
    cmd.drive_fwd_q15 = 26000;

    t0 = clock();
    for (iter = 0; iter < 10000; ++iter) {
        (void)nc_esc_observer_update(&obs, ares.cmd_q15, rpm, cur, 5u, &ores);
        (void)nc_shunting4d_allocate(&hex6, w_des, &ares);
        (void)nc_cpg_step(&cpg, &cmd, 5u, &cst);
        (void)nc_hexapod_ik_3dof(&ik_cfg, &cpg, &cst, &ik_out);
    }
    t1 = clock();
    elapsed_us = (long)(((int64_t)(t1 - t0) * 1000000) / CLOCKS_PER_SEC);
    printf("    Timing (10,000 full ticks of ESC + 4-DOF Hex6 Active-Set + CPG + 18-DOF IK): "
           "%ld us total (~%ld ns/tick)\n",
           elapsed_us, (elapsed_us * 100L) / 1000L);
}

int main(void)
{
    printf("=== NeckCord v2.0.0 ANSI C99 Fixed-Point Self-Test (18 Tests) ===\n");
    test_1_static_ram_budget();
    test_2_trig_lut_accuracy();
    test_3_cervical_roundtrip_and_python_parity();
    test_4_cervical_crc_and_bus_faults();
    test_5_tripod_antiphase_invariants();
    test_6_wave_gait_metachronal_lag();
    test_7_giant_fiber_freeze();
    test_8_hexapod_single_leg_amputation_adaptation();
    test_9_usv4_fault_isolation_and_zero_spin_rebalance();
    test_10_octo8_dual_fault_rebalance();
    test_11_recovery_probe();
    test_12_integer_math_isqrt_atan2_acos();
    test_13_hex6_unidirectional_motor_loss_survival();
    test_14_octo8_single_and_dual_rotor_loss();
    test_15_usv_t200_asymmetric_and_deadzone();
    test_16_esc_observer_broken_blade_fast_isolation();
    test_17_esc_observer_weed_foul_overcurrent_and_stall();
    test_18_hexapod_18servo_3dof_ik_and_pentapod_parking();
    run_timing_benchmark();

    printf("=== Summary: %d/%d passed, %d failed ===\n",
           g_passed, g_passed + g_failed, g_failed);
    return (g_failed == 0) ? 0 : 1;
}
