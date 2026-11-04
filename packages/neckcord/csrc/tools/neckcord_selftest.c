/*
 * NeckCord v1.0.0 - Pure Integer ANSI C99 Verification Suite (10/10 tests)
 *
 * Verifies:
 *   1. Static RAM footprint (< 3 KB, zero heap)
 *   2. 16-bit BAM Q15 sin/cos LUT identity across all 65,536 angles
 *   3. 4-byte cervical codec roundtrip & Python wire-format golden parity
 *   4. CRC-4 single-bit error (32/32 bits x 256 frames) & stuck-bus rejection
 *   5. Tripod gait anti-phase invariants
 *   6. Metachronal wave posterior-to-anterior phase lag invariants
 *   7. Giant Fiber freeze reflex instant phase lock
 *   8. Single-leg amputation (L2 jammed) VNC pentapod vs unadapted stability
 *   9. 4-thruster vectored USV online fault isolation & zero-spin rebalancing
 *  10. 8-actuator octo-ROV dual-fault wrench rebalancing
 *
 * Copyright (c) 2026 Amandeep Singh Bains. MIT License.
 */

#include "../neckcord.h"
#include <stdio.h>
#include <string.h>

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
    size_t total_ram = sizeof(nc_descending_frame_t)
                     + sizeof(nc_cpg_cord_t)
                     + sizeof(nc_cpg_state_t)
                     + sizeof(nc_shunting_cord_t)
                     + sizeof(nc_alloc_result_t);
    printf("Test 1: Static RAM footprint = %lu bytes (budget < 3072 bytes)\n",
           (unsigned long)total_ram);
    CHECK(total_ram < 3072u && total_ram < 600u,
          "Combined runtime structs fit under 600 B (< 3 KB static RAM, 0 heap)");
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
    /* Golden Python wire vector from `neckcord demo`:
     * drive_fwd=0.82 -> 26869 Q15, turn_yaw=-0.25 -> -8192 Q15,
     * strafe_lat=0.10 -> 3277 Q15, gait=0, freeze=0, seq=2 => 0x68E00C20 */
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
    /* Perturb initial phases away from equilibrium to verify Kuramoto convergence */
    cpg.phases_bam[0] = 1000u;
    cpg.phases_bam[1] = 28000u;
    cpg.phases_bam[2] = 35000u;
    cpg.phases_bam[3] = 4200u;
    cpg.phases_bam[4] = 63000u;
    cpg.phases_bam[5] = 30000u;

    memset(&cmd, 0, sizeof(cmd));
    cmd.drive_fwd_q15 = 29000; /* Fast walk -> Tripod */
    cmd.gait_mode = (uint8_t)NC_GAIT_AUTO;

    for (step = 0; step < 200; ++step) {
        (void)nc_cpg_step(&cpg, &cmd, 10u, &st);
    }

    /* Tripod A: {L1=0, R2=3, L3=4} in-phase; Tripod B: {R1=1, L2=2, R3=5} anti-phase (32768 BAM) */
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
    cmd.drive_fwd_q15 = 6500; /* Slow heavy-load walk -> Metachronal Wave */
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

    /* Amputate / jam L2 (mesothoracic left, index 2) */
    (void)nc_cpg_set_limb_health(&cpg_adapt, 2u, 0);
    (void)nc_cpg_set_limb_health(&cpg_fixed, 2u, 0);

    memset(&cmd, 0, sizeof(cmd));
    cmd.drive_fwd_q15 = 27852; /* 0.85 fast forward command */
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
    int32_t w_des[3] = { 16384, 0, 0 }; /* Pure forward surge Fx = 1.0 * u_max (Q14) */
    int16_t true_eff_q15[4] = { NC_Q15_ONE, NC_Q15_ONE, 2621, NC_Q15_ONE }; /* T2 = 8% */
    int step;
    int isolated_tick = -1;
    int32_t true_fx_q14 = 0;
    int32_t true_fy_q14 = 0;
    int32_t true_tz_q14 = 0;
    uint8_t i;

    nc_shunting_init_usv4(&cord, 450, 350);
    cord.tau_act_ms = 0u; /* Direct actuator feedback for crisp deterministic latency check */

    /* Initial nominal step */
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

int main(void)
{
    printf("=== NeckCord v1.0.0 ANSI C99 Fixed-Point Self-Test ===\n");
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

    printf("=== Summary: %d/%d passed, %d failed ===\n",
           g_passed, g_passed + g_failed, g_failed);
    return (g_failed == 0) ? 0 : 1;
}
