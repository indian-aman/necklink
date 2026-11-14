/*
 * necklink_cns_selftest.c - Comprehensive C99 Self-Test & Timing Benchmark for NeckLink CNS v3.0.0
 */

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "necklink_cns.h"

static nl_cns_t g_cns;

static int test_sram_budget(void)
{
    size_t sz = sizeof(nl_cns_t);
    printf("[1/7] Whole-CNS static SRAM footprint: %lu bytes (%.2f KB)\n",
           (unsigned long)sz, (unsigned long)sz / 1024.0);
    if (sz >= 25600u) {
        printf("FAIL: sizeof(nl_cns_t) exceeded 25 KB budget!\n");
        return 1;
    }
    return 0;
}

static int test_mavlink_v2_roundtrip(void)
{
    uint8_t seq = 0u;
    uint8_t wire[NL_MAV2_MAX_FRAME_LEN];
    nl_mav_parser_t parser;
    nl_mav_frame_t frame;
    nl_mav_heartbeat_t hb_in, hb_out;
    nl_mav_manual_control_t mc_in, mc_out;
    nl_mav_scaled_imu_t imu_in, imu_out;
    nl_mav_statustext_t st_out;
    size_t n, i;
    int got = 0;

    nl_mav_parser_init(&parser);

    memset(&hb_in, 0, sizeof(hb_in));
    hb_in.type = NL_MAV_TYPE_ONBOARD_CONTROLLER;
    hb_in.autopilot = NL_MAV_AUTOPILOT_INVALID;
    hb_in.system_status = NL_MAV_STATE_ACTIVE;
    hb_in.mavlink_version = 3u;

    n = nl_mav_encode_heartbeat(42u, 191u, &seq, &hb_in, wire);
    if (n < 12u || wire[0] != 0xFDu) {
        return 1;
    }
    for (i = 0u; i < n; ++i) {
        if (nl_mav_parse_byte(&parser, wire[i], &frame)) {
            got = 1;
        }
    }
    if (!got || nl_mav_decode_heartbeat(&frame, &hb_out) != 0 || hb_out.type != NL_MAV_TYPE_ONBOARD_CONTROLLER) {
        return 1;
    }

    mc_in.x = 650;
    mc_in.y = -220;
    mc_in.z = 500;
    mc_in.r = 410;
    mc_in.buttons = 3u;
    mc_in.target = 0u; /* tests trailing-zero trimming! */
    n = nl_mav_encode_manual_control(42u, 191u, &seq, &mc_in, wire);
    got = 0;
    for (i = 0u; i < n; ++i) {
        if (nl_mav_parse_byte(&parser, wire[i], &frame)) {
            got = 1;
        }
    }
    if (!got || nl_mav_decode_manual_control(&frame, &mc_out) != 0 || mc_out.x != 650 || mc_out.y != -220 || mc_out.r != 410) {
        return 1;
    }

    imu_in.time_boot_ms = 12345u;
    imu_in.xacc_mg = 120;
    imu_in.yacc_mg = -85;
    imu_in.zacc_mg = 985;
    imu_in.xgyro_mrad_s = 15;
    imu_in.ygyro_mrad_s = -20;
    imu_in.zgyro_mrad_s = 110;
    imu_in.xmag_mgauss = 210;
    imu_in.ymag_mgauss = -45;
    imu_in.zmag_mgauss = 430;
    n = nl_mav_encode_scaled_imu(1u, 1u, &seq, &imu_in, wire);
    got = 0;
    for (i = 0u; i < n; ++i) {
        if (nl_mav_parse_byte(&parser, wire[i], &frame)) {
            got = 1;
        }
    }
    if (!got || nl_mav_decode_scaled_imu(&frame, &imu_out) != 0 || imu_out.zacc_mg != 985 || imu_out.zmag_mgauss != 430) {
        return 1;
    }

    n = nl_mav_encode_statustext(42u, 191u, &seq, NL_MAV_SEVERITY_CRITICAL, "NECKLINK LOOM DODGE", wire);
    got = 0;
    for (i = 0u; i < n; ++i) {
        if (nl_mav_parse_byte(&parser, wire[i], &frame)) {
            got = 1;
        }
    }
    if (!got || nl_mav_decode_statustext(&frame, &st_out) != 0 || strncmp(st_out.text, "NECKLINK LOOM DODGE", 19) != 0) {
        return 1;
    }

    printf("[2/7] MAVLink v2 wire codec (0xFD + X.25 CRC + zero-trim): PASS (%u frames verified)\n",
           (unsigned)parser.rx_ok_count);
    return 0;
}

static int test_closed_loop_pole_dodge_and_homing(void)
{
    nl_cns_inputs_t in;
    nl_cns_outputs_t out;
    uint8_t frame[NECKSIGHT_PIXELS];
    int step, r, c;
    int triggered_dodge = 0;

    nl_cns_init(&g_cns, NL_VEHICLE_USV_T200);
    memset(&in, 0, sizeof(in));
    in.dt_ms = 20u;
    in.speed_mm_s = 1500;
    in.cruise_drive_q15 = 22000;
    in.return_home = 1u;
    in.az_mg = 1000;
    in.mx = 1000;
    in.my = 0;
    in.mz = 1800;
    in.mag_valid = 1u;

    /* Outbound run to build CPU4 path-integrator memory, then expanding vertical pole on left */
    for (step = 0; step < 28; ++step) {
        int half_w = 1 + (step / 4);
        for (r = 0; r < 32; ++r) {
            for (c = 0; c < 32; ++c) {
                uint8_t bg = (uint8_t)(160 + ((r ^ c) & 15));
                if (step >= 6 && c >= (10 - half_w) && c <= (10 + half_w)) {
                    frame[r * 32 + c] = 12u; /* Approaching dark vertical pole on LEFT (x=10) */
                } else {
                    frame[r * 32 + c] = bg;
                }
            }
        }
        in.cam_32x32 = frame;
        if (nl_cns_step(&g_cns, &in, &out) != 0) {
            return 1;
        }
        if (out.behavior_mode == NL_MODE_DODGE_LOOM || out.behavior_mode == NL_MODE_MOONWALK_REVERSE) {
            triggered_dodge = 1;
            /* Pole is on LEFT (x=10 < 16), so robot must dodge RIGHT (turn_yaw_q15 < 0) */
            if (out.desc_cmd.turn_yaw_q15 >= 0) {
                printf("FAIL: wrong dodge sign %d for left pole!\n", out.desc_cmd.turn_yaw_q15);
                return 1;
            }
        }
    }

    if (!triggered_dodge) {
        printf("FAIL: expanding thin vertical pole did not trigger CNS dodge!\n");
        return 1;
    }
    printf("[3/7] Closed-loop thin-pole LC4 detection + contralateral DNa02 dodge: PASS (yaw=%d, lc4=%u)\n",
           (int)out.desc_cmd.turn_yaw_q15, (unsigned)out.lc4_bar_score_q10);
    return 0;
}

static int test_hexacopter_esc_broken_blade_survival(void)
{
    nl_cns_inputs_t in;
    nl_cns_outputs_t out;
    int32_t rpm[6];
    int32_t curr_ma[6];
    int step, i;

    nl_cns_init(&g_cns, NL_VEHICLE_HEXACOPTER);
    memset(&in, 0, sizeof(in));
    in.dt_ms = 20u;
    in.speed_mm_s = 0;
    in.cruise_drive_q15 = 8192;
    in.az_mg = 1000;
    in.mx = 1000;
    in.mag_valid = 1u;

    for (step = 0; step < 18; ++step) {
        for (i = 0; i < 6; ++i) {
            int32_t u = g_cns.thruster_cord.last_cmd_q15[i];
            if (u < 0) {
                u = 0;
            }
            rpm[i] = (u * 4000) / 32767;
            /* Cubic load current + idle, EXCEPT rotor #2 has a snapped blade (near-idle current!) */
            if (i == 2 && step >= 3) {
                curr_ma[i] = 520;
            } else {
                int64_t r_norm = u;
                int64_t load = (18000LL * r_norm * r_norm) / (32767LL * 32767LL);
                load = (load * r_norm) / 32767LL;
                curr_ma[i] = (int32_t)(450 + load);
            }
        }
        in.esc_rpm = rpm;
        in.esc_current_ma = curr_ma;
        (void)nl_cns_step(&g_cns, &in, &out);

        for (i = 0; i < 6; ++i) {
            if (out.actuator_cmd_q15[i] < 0) {
                printf("FAIL: unidirectional hexacopter commanded negative thrust %d!\n",
                       out.actuator_cmd_q15[i]);
                return 1;
            }
        }
    }

    if ((out.esc_broken_mask & (1u << 2)) == 0u || out.actuator_health_q15[2] > 16000) {
        printf("FAIL: ESC broken blade on rotor 2 not isolated (mask=0x%02X, h2=%d)\n",
               out.esc_broken_mask, out.actuator_health_q15[2]);
        return 1;
    }
    printf("[4/7] Hexacopter [0,u_max] broken-blade sensorless ESC isolation & Limp-Home: PASS (h2=%d, mode=%u)\n",
           (int)out.actuator_health_q15[2], (unsigned)out.behavior_mode);
    return 0;
}

static int test_hexapod_18dof_and_vibration_continual_memory(void)
{
    nl_cns_inputs_t in;
    nl_cns_outputs_t out;
    int16_t vib_healthy[128];
    int16_t vib_fault[128];
    int32_t plume_a[8] = { 65536, 12000, 8000, 90000, 10000, 5000, 72000, 6000 };
    int32_t plume_b[8] = { 5000, 88000, 76000, 4000, 92000, 70000, 3000, 85000 };
    int i, step;

    nl_cns_init(&g_cns, NL_VEHICLE_HEXAPOD_18DOF);
    (void)nc_cpg_set_limb_health(&g_cns.cpg_cord, 2u, 0); /* Amputate middle-left leg L2 */

    for (i = 0; i < 128; ++i) {
        vib_healthy[i] = (int16_t)(((i & 7) < 4) ? 180 : -180);
        vib_fault[i] = (int16_t)(((i & 1) ? 260 : -260) + ((i % 16 == 0) ? 2600 : 0));
    }

    /* Teach Class 1 (Plume A) + consolidate into KCab LTM, then Class 2 (Plume B) */
    if (nl_cns_learn_chemical(&g_cns, plume_a, 1u, 65536, 1u) != 0) {
        return 1;
    }
    for (step = 0; step < 10; ++step) {
        (void)nl_cns_learn_chemical(&g_cns, plume_b, 2u, 65536, 0u);
    }

    memset(&in, 0, sizeof(in));
    in.dt_ms = 20u;
    in.speed_mm_s = 400;
    in.cruise_drive_q15 = 20000;
    in.az_mg = 1000;
    in.mx = 1000;
    in.mag_valid = 1u;
    in.vib_samples_i16 = vib_healthy;
    in.vib_n_samples = 128u;
    in.chem8_q16 = plume_a;

    for (step = 0; step < 12; ++step) {
        (void)nl_cns_step(&g_cns, &in, &out);
    }

    /* Verify Plume A was NOT forgotten after 10x Class 2 updates, and L2 leg is parked! */
    if (out.predicted_class != 1u || (out.hexapod_parked_mask & (1u << 2)) == 0u) {
        printf("FAIL: continual memory class=%u (expected 1), parked=0x%02X\n",
               (unsigned)out.predicted_class, (unsigned)out.hexapod_parked_mask);
        return 1;
    }

    /* Inject bearing-impact vibration fault */
    in.vib_samples_i16 = vib_fault;
    (void)nl_cns_step(&g_cns, &in, &out);
    if (out.vib_novelty_q16 < 30000) {
        printf("FAIL: bearing fault vibration novelty too low (%d)\n", (int)out.vib_novelty_q16);
        return 1;
    }

    printf("[5/7] 18-DOF Pentapod IK + Chordotonal Bearing Fault + KCab Zero-Forgetting: PASS (vib_nov=%.3f)\n",
           out.vib_novelty_q16 / 65536.0);
    return 0;
}

static int test_full_cns_throughput(void)
{
    nl_cns_inputs_t in;
    nl_cns_outputs_t out;
    uint8_t cam[NECKSIGHT_PIXELS];
    int32_t chem[8] = { 40000, 15000, 22000, 60000, 12000, 18000, 50000, 9000 };
    int16_t vib[64];
    uint8_t mav_pkt[NL_MAV2_MAX_FRAME_LEN];
    clock_t t0, t1;
    int iters = 2000;
    int i;
    double elapsed_us;

    nl_cns_init(&g_cns, NL_VEHICLE_USV_T200);
    for (i = 0; i < NECKSIGHT_PIXELS; ++i) {
        cam[i] = (uint8_t)((i * 29) & 0xFF);
    }
    for (i = 0; i < 64; ++i) {
        vib[i] = (int16_t)((i & 3) ? 140 : -140);
    }

    memset(&in, 0, sizeof(in));
    in.dt_ms = 20u;
    in.speed_mm_s = 1200;
    in.cruise_drive_q15 = 20000;
    in.return_home = 1u;
    in.gx_mrad_s = 40;
    in.gy_mrad_s = -30;
    in.gz_mrad_s = 120;
    in.ax_mg = 80;
    in.ay_mg = -60;
    in.az_mg = 995;
    in.mx = 420;
    in.my = -110;
    in.mz = 880;
    in.mag_valid = 1u;
    in.cam_32x32 = cam;
    in.chem8_q16 = chem;
    in.vib_samples_i16 = vib;
    in.vib_n_samples = 64u;

    t0 = clock();
    for (i = 0; i < iters; ++i) {
        cam[i & 1023] ^= 0x3Cu;
        (void)nl_cns_step(&g_cns, &in, &out);
        (void)nl_cns_encode_mavlink_control(&g_cns, &out, 1u, mav_pkt);
    }
    t1 = clock();
    elapsed_us = ((double)(t1 - t0) * 1e6) / (double)CLOCKS_PER_SEC;
    printf("[6/7] Full 4-Organ + Camera + Vibration + MAVLink v2 Tick Latency: %.2f us/tick (%.0f Hz)\n",
           elapsed_us / iters, (iters * 1e6) / (elapsed_us > 1.0 ? elapsed_us : 1.0));
    return 0;
}

static int test_banc_dn_weights_override_and_fallback(void)
{
    nl_cns_inputs_t in;
    nl_cns_outputs_t out_real, out_mirror, out_v2;
    nl_banc_dn_weights_t mirrored_w;
    uint8_t frame[NECKSIGHT_PIXELS];
    int step, r, c;

    memcpy(&mirrored_w, nl_banc_dn_default_weights(), sizeof(mirrored_w));
    /* Swap left/right leg MN rows in W_dn_leg_q12 (0<->1, 2<->3, 4<->5) to test runtime causality */
    for (r = 0; r < 6; r += 2) {
        for (c = 0; c < 10; ++c) {
            int16_t tmp = mirrored_w.W_dn_leg_q12[r * 10 + c];
            mirrored_w.W_dn_leg_q12[r * 10 + c] = mirrored_w.W_dn_leg_q12[(r + 1) * 10 + c];
            mirrored_w.W_dn_leg_q12[(r + 1) * 10 + c] = tmp;
        }
    }

    memset(&in, 0, sizeof(in));
    in.dt_ms = 20u;
    in.speed_mm_s = 1200;
    in.cruise_drive_q15 = 20000;
    in.az_mg = 1000;
    in.mx = 1000;
    in.mag_valid = 1u;

    int32_t min_real = 0, max_mirror = 0, min_v2 = 0;

    /* Run real BANC v888 weights on expanding left obstacle */
    nl_cns_init(&g_cns, NL_VEHICLE_USV_T200);
    if (g_cns.plastic_ring.comp.weights == NULL) {
        printf("FAIL: nl_cns_init left CX ring compass in NULL (v2_analytic) mode instead of BANC v888!\n");
        return 1;
    }
    for (step = 0; step < 24; ++step) {
        int hw = (step >= 6) ? (1 + ((step - 6) / 2)) : 1;
        for (r = 0; r < 32; ++r) {
            for (c = 0; c < 32; ++c) {
                frame[r * 32 + c] = (step >= 6 && c >= (9 - hw) && c <= (9 + hw))
                                  ? 10u
                                  : (uint8_t)(160 + ((r ^ c) & 15));
            }
        }
        in.cam_32x32 = frame;
        (void)nl_cns_step(&g_cns, &in, &out_real);
        if (out_real.desc_cmd.turn_yaw_q15 < min_real) {
            min_real = out_real.desc_cmd.turn_yaw_q15;
        }
    }

    /* Run mirrored W_dn_leg_q12 on identical frames -> must invert turn_yaw_q15 sign! */
    nl_cns_init(&g_cns, NL_VEHICLE_USV_T200);
    nl_cns_set_dn_weights(&g_cns, &mirrored_w);
    for (step = 0; step < 24; ++step) {
        int hw = (step >= 6) ? (1 + ((step - 6) / 2)) : 1;
        for (r = 0; r < 32; ++r) {
            for (c = 0; c < 32; ++c) {
                frame[r * 32 + c] = (step >= 6 && c >= (9 - hw) && c <= (9 + hw))
                                  ? 10u
                                  : (uint8_t)(160 + ((r ^ c) & 15));
            }
        }
        in.cam_32x32 = frame;
        (void)nl_cns_step(&g_cns, &in, &out_mirror);
        if (out_mirror.desc_cmd.turn_yaw_q15 > max_mirror) {
            max_mirror = out_mirror.desc_cmd.turn_yaw_q15;
        }
    }

    /* Run NULL dn_weights -> v2_analytic fallback */
    nl_cns_init(&g_cns, NL_VEHICLE_USV_T200);
    nl_cns_set_dn_weights(&g_cns, NULL);
    if (g_cns.plastic_ring.comp.weights != NULL) {
        printf("FAIL: nl_cns_set_dn_weights(NULL) did not switch CX ring compass to v2_analytic!\n");
        return 1;
    }
    for (step = 0; step < 24; ++step) {
        int hw = (step >= 6) ? (1 + ((step - 6) / 2)) : 1;
        for (r = 0; r < 32; ++r) {
            for (c = 0; c < 32; ++c) {
                frame[r * 32 + c] = (step >= 6 && c >= (9 - hw) && c <= (9 + hw))
                                  ? 10u
                                  : (uint8_t)(160 + ((r ^ c) & 15));
            }
        }
        in.cam_32x32 = frame;
        (void)nl_cns_step(&g_cns, &in, &out_v2);
        if (out_v2.desc_cmd.turn_yaw_q15 < min_v2) {
            min_v2 = out_v2.desc_cmd.turn_yaw_q15;
        }
    }

    if (min_real >= 0 || max_mirror <= 0 || min_v2 >= 0) {
        printf("FAIL: BANC DN causal override failed (real=%d, mirror=%d, v2=%d)\n",
               (int)min_real, (int)max_mirror, (int)min_v2);
        return 1;
    }

    printf("[7/7] BANC v888 DN runtime matrix causality & v2 fallback: PASS (real=%d, mirror=%d, v2=%d)\n",
           (int)min_real, (int)max_mirror, (int)min_v2);
    return 0;
}

int main(void)
{
    if (test_sram_budget() != 0) return 1;
    if (test_mavlink_v2_roundtrip() != 0) return 1;
    if (test_closed_loop_pole_dodge_and_homing() != 0) return 1;
    if (test_hexacopter_esc_broken_blade_survival() != 0) return 1;
    if (test_hexapod_18dof_and_vibration_continual_memory() != 0) return 1;
    if (test_full_cns_throughput() != 0) return 1;
    if (test_banc_dn_weights_override_and_fallback() != 0) return 1;
    printf("ALL 7 NECKLINK WHOLE-CNS v3.0.0 C99 SELF-TESTS PASSED!\n");
    return 0;
}
