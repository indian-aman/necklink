/*
 * necklink_cns.c - Unified Adult Drosophila Whole-CNS Edge Autonomy Controller (v3.0.0)
 *
 * Zero dynamic heap allocation. Strictly integer fixed-point arithmetic.
 * Copyright (c) 2026 Amandeep Singh Bains. SPDX-License-Identifier: MIT
 */

#include "necklink_cns.h"
#include <string.h>

const nl_banc_dn_weights_t *nl_banc_dn_default_weights(void)
{
    return &k_nl_banc_dn_default;
}

static int32_t nl_clamp_i32(int32_t v, int32_t lo, int32_t hi)
{
    if (v < lo) {
        return lo;
    }
    if (v > hi) {
        return hi;
    }
    return v;
}

static int16_t nl_clamp_q15(int32_t v)
{
    return (int16_t)nl_clamp_i32(v, -32767, 32767);
}

void nl_cns_init(nl_cns_t *cns, nl_vehicle_mode_t vehicle_mode)
{
    uint32_t n_kc = (NS_MAX_KC >= 512u) ? 512u : NS_MAX_KC;
    uint32_t k_win = (n_kc * 5u) / 100u; /* 5% APL winner-take-all sparsity */
    uint8_t n_act = 4u;

    if (cns == NULL) {
        return;
    }
    memset(cns, 0, sizeof(*cns));
    if (k_win < 4u) {
        k_win = 4u;
    }

    cns->dn_weights = &k_nl_banc_dn_default;
    cns->vehicle_mode = (uint8_t)vehicle_mode;
    cns->mav_sysid = 42u;
    cns->mav_compid = 191u;
    cns->loom_reverse_q10 = 150u;
    cns->nov_alert_q16 = 36045;       /* ~0.55 in Q16.16 */
    cns->vib_fault_thresh_q16 = 39322;/* ~0.60 in Q16.16 */

    /* 1. Optic Lobe */
    necksight_init(&cns->sight);

    /* 2. Central Complex */
    nc_tilt_init(&cns->tilt);
    nc_plastic_ring_init(&cns->plastic_ring);
    nc_compass_set_weights(&cns->plastic_ring.comp, nc_banc_default_weights());
    nc_navigator_init(&cns->nav, 1500); /* 1.5 m arrival taper radius */

    /* 3. Antennal Lobe, Chordotonal Organ & Mushroom Body */
    (void)ns_vib_init(&cns->vib_fe, 0u, 6u);
    (void)ns_antennal_init(&cns->chem_ant, 8u, 9830, 65536, 1u, 1311, 3277, 98304, 32768);
    (void)ns_kenyon_init(&cns->chem_kc, 8u, n_kc, 4u, k_win, 20261114u);
    (void)ns_novelty_init(&cns->chem_nov, n_kc, 49152, 65);

    (void)ns_antennal_init(&cns->vib_ant, NS_VIB_CHANNELS, 9830, 65536, 1u, 655, 3277, 65536, 49152);
    (void)ns_kenyon_init(&cns->vib_kc, NS_VIB_CHANNELS, n_kc, 6u, k_win, 20261115u);
    (void)ns_novelty_init(&cns->vib_nov, n_kc, 49152, 32);

    (void)ns_valence_init(&cns->valence_clf, n_kc, NS_MAX_CLASSES, 6554, 16384);

    /* 4. Ventral Nerve Cord */
    if (vehicle_mode == NL_VEHICLE_HEXACOPTER) {
        nc_shunting4d_init_hex6(&cns->thruster_cord, 280, 18);
        n_act = 6u;
    } else if (vehicle_mode == NL_VEHICLE_OCTOCOPTER) {
        nc_shunting4d_init_octo8(&cns->thruster_cord, 320, 18);
        n_act = 8u;
    } else {
        /* Default 4-thruster asymmetric BlueRobotics T200 USV (-40.0 N / +52.5 N -> -24965 Q15) */
        nc_shunting4d_init_usv_t200(&cns->thruster_cord, 450, 350, -24965, 1638);
        n_act = 4u;
    }
    (void)nc_esc_observer_init(&cns->esc_obs, n_act, 4000, 450, 18000);
    nc_cpg_init(&cns->cpg_cord, 1u);
    nc_hexapod_ik_default_cfg(&cns->ik_cfg);

    /* 5. MAVLink v2 Parser */
    nl_mav_parser_init(&cns->mav_rx);
}

void nl_cns_set_dn_weights(nl_cns_t *cns, const nl_banc_dn_weights_t *dn_weights)
{
    if (cns == NULL) {
        return;
    }
    cns->dn_weights = dn_weights;
    nc_compass_set_weights(
        &cns->plastic_ring.comp,
        (dn_weights != NULL) ? nc_banc_default_weights() : NULL
    );
}

void nl_cns_reset(nl_cns_t *cns)
{
    const nl_banc_dn_weights_t *saved_dn;
    if (cns == NULL) {
        return;
    }
    saved_dn = cns->dn_weights;
    nl_cns_init(cns, (nl_vehicle_mode_t)cns->vehicle_mode);
    nl_cns_set_dn_weights(cns, saved_dn);
}

int nl_cns_learn_chemical(
    nl_cns_t *cns,
    const int32_t chem8_q16[8],
    uint32_t class_id,
    int32_t reward_q16,
    uint8_t consolidate_ltm
)
{
    int32_t norm_q16[NS_MAX_CHANNELS];
    uint64_t kc_words[NS_MAX_WORDS];
    int rc;

    if (cns == NULL || chem8_q16 == NULL) {
        return -1;
    }
    rc = ns_antennal_normalize(&cns->chem_ant, chem8_q16, norm_q16);
    if (rc != NS_OK) {
        return rc;
    }
    rc = ns_kenyon_hash(&cns->chem_kc, norm_q16, kc_words);
    if (rc != NS_OK) {
        return rc;
    }
    rc = ns_valence_learn(&cns->valence_clf, kc_words, class_id, reward_q16);
    if (rc != NS_OK) {
        return rc;
    }
    if (consolidate_ltm) {
        (void)ns_valence_consolidate(&cns->valence_clf, 26214); /* ~0.40 threshold */
    }
    return 0;
}

int nl_cns_step(
    nl_cns_t *cns,
    const nl_cns_inputs_t *in,
    nl_cns_outputs_t *out
)
{
    uint16_t dt_ms;
    nc_imu9_t imu9;
    nc_bam_t hdg_bam;
    int32_t max_turn_bam_s = 16384; /* 90 deg/s full-scale turn reference */
    int32_t cx_turn_bam_s = 0;
    int32_t cx_yaw_q15 = 0;
    uint16_t eff_loom_q10 = 0u;
    uint16_t thresh_q10;
    int32_t dnp01_q15 = 0;
    int32_t mdn_q15 = 0;
    int32_t evade_yaw_q15 = 0;
    int32_t strafe_q15 = 0;
    int32_t drive_q15 = 0;
    int32_t yaw_q15 = 0;
    int32_t best_chem_sc_q16 = 0;
    uint8_t mode = (uint8_t)NL_MODE_CRUISE_HOME;
    int16_t min_health = NC_Q15_ONE;
    uint8_t i;

    if (cns == NULL || in == NULL || out == NULL) {
        return -1;
    }
    memset(out, 0, sizeof(*out));
    dt_ms = (in->dt_ms == 0u) ? 20u : in->dt_ms;
    ++cns->tick_count;

    /* ---------------------------------------------------------------------
     * 1. Central Complex: 9-DOF Tilt-Compensated Ring Attractor + Path Integrator
     * --------------------------------------------------------------------- */
    imu9.gx_bam_s = nc_mrad_s_to_bam_s(in->gx_mrad_s);
    imu9.gy_bam_s = nc_mrad_s_to_bam_s(in->gy_mrad_s);
    imu9.gz_bam_s = nc_mrad_s_to_bam_s(in->gz_mrad_s);
    imu9.ax_mg = in->ax_mg;
    imu9.ay_mg = in->ay_mg;
    imu9.az_mg = (in->ax_mg == 0 && in->ay_mg == 0 && in->az_mg == 0) ? 1000 : in->az_mg;
    imu9.mx = in->mx;
    imu9.my = in->my;
    imu9.mz = in->mz;

    if (in->enable_plastic_cal && in->mag_valid) {
        nc_compass_set_weights(&cns->plastic_ring.comp, NULL);
        hdg_bam = nc_plastic_ring_update_9dof(
            &cns->plastic_ring, &cns->tilt, &imu9, dt_ms, 0, 0u, 1u
        );
    } else {
        nc_compass_set_weights(
            &cns->plastic_ring.comp,
            (cns->dn_weights != NULL) ? nc_banc_default_weights() : NULL
        );
        hdg_bam = nc_compass_update_9dof(
            &cns->plastic_ring.comp,
            &cns->tilt,
            &imu9,
            dt_ms,
            in->mag_valid,
            NC_Q15_ONE,
            0,
            0u,
            0
        );
    }

    nc_navigator_update(&cns->nav, in->speed_mm_s, dt_ms, hdg_bam);
    nc_navigator_home_vector(&cns->nav, &out->home_dist_mm, &out->home_bearing_bam);
    out->heading_bam = hdg_bam;
    out->heading_cdeg = nc_bam_to_cdeg(hdg_bam);

    if (in->return_home) {
        cx_turn_bam_s = nc_navigator_steering_cmd_with_compass(
            &cns->nav, &cns->plastic_ring.comp, hdg_bam, max_turn_bam_s
        );
        cx_yaw_q15 = nl_clamp_i32((cx_turn_bam_s * NC_Q15_ONE) / max_turn_bam_s, -NC_Q15_ONE, NC_Q15_ONE);
    }

    /* ---------------------------------------------------------------------
     * 2. Optic Lobe: 3-DOF Derotation + LPLC2 (4-arm) & LC4 (2-arm pole/wire)
     * --------------------------------------------------------------------- */
    if ((in->cam_raw_u8 != NULL && in->cam_h > 0u && in->cam_w > 0u) || in->cam_32x32 != NULL) {
        int16_t dx_q4 = necksight_gyro_to_shift_q4(in->gz_mrad_s, dt_ms, 256u);
        int16_t dy_q4 = (int16_t)(-necksight_gyro_to_shift_q4(in->gy_mrad_s, dt_ms, 256u));
        int16_t droll_bam = necksight_gyro_to_bam(in->gx_mrad_s, dt_ms);
        necksight_derotate_3dof(&cns->sight, dx_q4, dy_q4, droll_bam);
        if (in->cam_raw_u8 != NULL && in->cam_h > 0u && in->cam_w > 0u) {
            necksight_step_frame_u8(
                &cns->sight, in->cam_raw_u8, in->cam_h, in->cam_w, in->cam_w, dt_ms, &cns->last_sight
            );
        } else {
            necksight_step_u8(&cns->sight, in->cam_32x32, dt_ms, &cns->last_sight);
        }
    }
    out->looming_score_q10 = cns->last_sight.looming_score_q10;
    out->lc4_bar_score_q10 = cns->last_sight.lc4_score_q10;
    out->est_ttc_ms = cns->last_sight.est_ttc_ms;
    out->loom_triggered = cns->last_sight.triggered;
    out->avoid_x_q7 = cns->last_sight.avoid_x_q7;
    out->avoid_y_q7 = cns->last_sight.avoid_y_q7;

    /* ---------------------------------------------------------------------
     * 3. Antennal Lobe, Chordotonal Vibration & Mushroom Body
     * --------------------------------------------------------------------- */
    if (in->chem8_q16 != NULL) {
        int32_t norm_q16[NS_MAX_CHANNELS];
        uint64_t kc_words[NS_MAX_WORDS];
        if (ns_antennal_step_whiff(&cns->chem_ant, in->chem8_q16, norm_q16, &out->whiff_rate_q16) == NS_OK) {
            if (ns_kenyon_hash(&cns->chem_kc, norm_q16, kc_words) == NS_OK) {
                out->chem_novelty_q16 = ns_novelty_update_and_score(&cns->chem_nov, kc_words);
                (void)ns_valence_predict(&cns->valence_clf, kc_words, &out->predicted_class, &best_chem_sc_q16);
            }
        }
    }

    if (in->vib_samples_i16 != NULL && in->vib_n_samples >= 16u) {
        int32_t vib_raw_q16[NS_VIB_CHANNELS];
        int32_t vib_norm_q16[NS_MAX_CHANNELS];
        uint64_t vib_words[NS_MAX_WORDS];
        if (ns_vib_extract_i16(&cns->vib_fe, in->vib_samples_i16, in->vib_n_samples, vib_raw_q16) == NS_OK) {
            if (ns_antennal_normalize(&cns->vib_ant, vib_raw_q16, vib_norm_q16) == NS_OK) {
                if (ns_kenyon_hash(&cns->vib_kc, vib_norm_q16, vib_words) == NS_OK) {
                    out->vib_novelty_q16 = ns_novelty_update_and_score(&cns->vib_nov, vib_words);
                }
            }
        }
    }

    /* Inspect minimum VNC actuator health from previous tick */
    for (i = 0u; i < cns->thruster_cord.n_actuators; ++i) {
        if (cns->thruster_cord.health_q15[i] < min_health) {
            min_health = cns->thruster_cord.health_q15[i];
        }
    }
    cns->cast_phase_bam = (uint16_t)(cns->cast_phase_bam + (uint16_t)(82u * dt_ms));
    thresh_q10 = (cns->sight.threshold_q10 > 0u) ? cns->sight.threshold_q10 : NECKSIGHT_DEFAULT_THRESHOLD_Q10;

    if (cns->dn_weights != NULL) {
        /* -----------------------------------------------------------------
         * 4A. v3.0.0 BANC v888 Synaptic Competition (11 Brain -> 10 DN -> 6 Leg MN)
         *     Strictly integer Q12 fixed-point (4096 = 1.0), zero floats.
         * ----------------------------------------------------------------- */
        const int16_t *Wb = cns->dn_weights->W_brain_dn_q12;
        const int16_t *Wl = cns->dn_weights->W_dn_leg_q12;
        int32_t u_brain_q12[11];
        int32_t r_dn_q12[10];
        int32_t r_eff_q12[10];
        int32_t jump_leg_q12[6];
        int32_t rev_leg_q12[6];
        int32_t steer_leg_q12[6];
        int32_t fx_q12, abs_fx_q12, w_left_q12, w_right_q12;
        int32_t half_th_q10, rev_q10, loom_sup_q12, lc4_sup_q12, loom_extra_q12;
        int32_t lplc2_drv_q12, lc4_drv_q12, lplc1_drv_q12, cruise_q12, taper_q12;
        int32_t nov_q12, nov_th_q12, nov_exc_q12, vib_q12, vib_exc_q12;
        int32_t mbon_ach_q12, mbon_inh_q12, health_gate_q12;
        int32_t jump_asym_q12, steer_asym_q12, rev_pos_sum_q12;
        int32_t dnp01_q12, mdn_q12, dnp09_q12, fwd_surv_q12, drive_q12, yaw_q12, strafe_q12;
        uint8_t d, b_idx;

        if (out->avoid_x_q7 != 0) {
            fx_q12 = nl_clamp_i32(-((int32_t)out->avoid_x_q7 * 4096) / 127, -4096, 4096);
        } else {
            fx_q12 = (((int32_t)cns->last_sight.focus_x * 8192) / 31) - 4096;
        }
        abs_fx_q12 = (fx_q12 < 0) ? -fx_q12 : fx_q12;

        /* Compound-eye hemifields with +-15% frontal binocular overlap */
        w_left_q12  = nl_clamp_i32(((655 - fx_q12) * 4096) / 2662, 0, 4096);
        w_right_q12 = nl_clamp_i32(((573 + fx_q12) * 4096) / 2662, 0, 4096);

        half_th_q10 = (int32_t)(thresh_q10 >> 1);
        rev_q10 = (cns->loom_reverse_q10 > 16u) ? (int32_t)cns->loom_reverse_q10 : 143;
        loom_sup_q12 = ((int32_t)out->looming_score_q10 > half_th_q10)
                     ? ((((int32_t)out->looming_score_q10 - half_th_q10) * 4096) / rev_q10)
                     : 0;
        lc4_sup_q12  = ((int32_t)out->lc4_bar_score_q10 > half_th_q10)
                     ? ((((int32_t)out->lc4_bar_score_q10 - half_th_q10) * 4096) / rev_q10)
                     : 0;
        lplc2_drv_q12 = nl_clamp_i32(loom_sup_q12, 0, 10240);
        loom_extra_q12 = (loom_sup_q12 > 2048) ? (((loom_sup_q12 - 2048) * 2458) >> 12) : 0;
        lc4_drv_q12 = nl_clamp_i32(lc4_sup_q12 + loom_extra_q12, 0, 10240);
        lplc1_drv_q12 = (lplc2_drv_q12 + lc4_drv_q12) >> 1;

        cruise_q12 = (nl_clamp_i32(in->cruise_drive_q15, 0, NC_Q15_ONE) * 4096) / NC_Q15_ONE;

        u_brain_q12[0] = (lplc2_drv_q12 * w_left_q12) >> 12;   /* LPLC2_L */
        u_brain_q12[1] = (lplc2_drv_q12 * w_right_q12) >> 12;  /* LPLC2_R */
        u_brain_q12[2] = (lc4_drv_q12 * w_left_q12) >> 12;     /* LC4_L   */
        u_brain_q12[3] = (lc4_drv_q12 * w_right_q12) >> 12;    /* LC4_R   */
        u_brain_q12[4] = (lplc1_drv_q12 * w_left_q12) >> 12;   /* LPLC1_L */
        u_brain_q12[5] = (lplc1_drv_q12 * w_right_q12) >> 12;  /* LPLC1_R */

        if (in->return_home) {
            taper_q12 = (cns->nav.arrival_radius_mm > 0 && out->home_dist_mm < (uint32_t)cns->nav.arrival_radius_mm)
                      ? (int32_t)((out->home_dist_mm * 4096u) / (uint32_t)cns->nav.arrival_radius_mm)
                      : 4096;
            if (cns->nav.weights != NULL) {
                u_brain_q12[6] = nl_clamp_i32(((((int32_t)cns->nav.last_pfl3_left_q12 * 6554) >> 12) * taper_q12) >> 12, 0, 10240);
                u_brain_q12[7] = nl_clamp_i32(((((int32_t)cns->nav.last_pfl3_right_q12 * 6554) >> 12) * taper_q12) >> 12, 0, 10240);
                u_brain_q12[8] = nl_clamp_i32(((((int32_t)cns->nav.last_pfl2_q12 * 6554) >> 12) * taper_q12) >> 12, 0, 8192);
            } else {
                u_brain_q12[6] = (cx_yaw_q15 < 0) ? nl_clamp_i32((-cx_yaw_q15) >> 3, 0, 6144) : 0;
                u_brain_q12[7] = (cx_yaw_q15 > 0) ? nl_clamp_i32(cx_yaw_q15 >> 3, 0, 6144) : 0;
                u_brain_q12[8] = 0;
            }
        } else {
            u_brain_q12[6] = 0;
            u_brain_q12[7] = 0;
            u_brain_q12[8] = 0;
        }

        nov_q12 = nl_clamp_i32(out->chem_novelty_q16 >> 4, 0, 4096);
        nov_th_q12 = nl_clamp_i32(cns->nov_alert_q16 >> 4, 256, 3840);
        nov_exc_q12 = (nov_q12 > nov_th_q12) ? (((nov_q12 - nov_th_q12) * 4096) / (4096 - nov_th_q12)) : 0;
        vib_q12 = nl_clamp_i32(out->vib_novelty_q16 >> 4, 0, 4096);
        vib_exc_q12 = (vib_q12 > 2253) ? (((vib_q12 - 2253) * 4096) / 1843) : 0;

        mbon_ach_q12 = nl_clamp_i32(((nov_exc_q12 * 1843) + (vib_exc_q12 * 2662)) >> 12, 0, 8192);
        mbon_inh_q12 = (in->chem8_q16 != NULL && best_chem_sc_q16 > 0)
                     ? nl_clamp_i32(best_chem_sc_q16 >> 4, 0, 6144)
                     : 0;
        u_brain_q12[9]  = mbon_ach_q12;
        u_brain_q12[10] = mbon_inh_q12;

        health_gate_q12 = ((out->vib_novelty_q16 > cns->vib_fault_thresh_q16) || (min_health < 14745))
                        ? 2253 /* 0.55 in Q12 */
                        : 4096;

        for (d = 0u; d < 10u; ++d) {
            int64_t acc = 0;
            int32_t tonic_q12 = 0;
            for (b_idx = 0u; b_idx < 11u; ++b_idx) {
                acc += (int64_t)Wb[d * 11u + b_idx] * (int64_t)u_brain_q12[b_idx];
            }
            if (d == 2u || d == 3u) {
                /* Reciprocal forward-walking inhibition onto MDN (Bidaye et al., Nature 2020) */
                tonic_q12 = -((cruise_q12 * 2662) >> 12);
            } else if (d == 8u || d == 9u) {
                tonic_q12 = (cruise_q12 * health_gate_q12) >> 12;
            } else if ((d == 4u || d == 5u) && nov_exc_q12 > 205) {
                int32_t cast_sin = (int32_t)nc_sin_q15((nc_bam_t)cns->cast_phase_bam);
                int32_t cast_q12 = ((nov_exc_q12 * cast_sin) >> 15) >> 2;
                if (d == 4u && cast_q12 > 0) {
                    tonic_q12 = cast_q12;
                } else if (d == 5u && cast_q12 < 0) {
                    tonic_q12 = -cast_q12;
                }
            }
            acc = (acc >> 12) + (int64_t)tonic_q12;
            if ((d == 2u || d == 3u) && loom_sup_q12 > 3891 && abs_fx_q12 < 3072) {
                int32_t mdn_boost_q12 = (((loom_sup_q12 - 3891) * (4096 - abs_fx_q12)) >> 12) * 9011 >> 12;
                int32_t col_sum_q12 = (d == 2u)
                    ? ((int32_t)Wb[2u * 11u + 0u] + (int32_t)Wb[2u * 11u + 2u] + (int32_t)Wb[2u * 11u + 9u])
                    : ((int32_t)Wb[3u * 11u + 1u] + (int32_t)Wb[3u * 11u + 3u] + (int32_t)Wb[3u * 11u + 9u]);
                if (col_sum_q12 > 0) {
                    acc += ((int64_t)mdn_boost_q12 * (int64_t)col_sum_q12) >> 12;
                }
            }
            r_dn_q12[d] = nl_clamp_i32((int32_t)acc, 0, 8192);
            if (d == 4u || d == 5u) {
                /* Supralinear DNa02 input-output curve (Westeinde et al., Nature 2024): r * (0.5 + 1.5 * r) */
                int64_t gain_q12 = 2048LL + (((int64_t)r_dn_q12[d] * 6144LL) >> 12);
                r_eff_q12[d] = (int32_t)(((int64_t)r_dn_q12[d] * gain_q12) >> 12);
            } else {
                r_eff_q12[d] = r_dn_q12[d];
            }
        }

        for (i = 0u; i < 6u; ++i) {
            int64_t j_acc = (int64_t)Wl[i * 10u + 0u] * r_eff_q12[0] + (int64_t)Wl[i * 10u + 1u] * r_eff_q12[1];
            int64_t r_acc = (int64_t)Wl[i * 10u + 2u] * r_eff_q12[2] + (int64_t)Wl[i * 10u + 3u] * r_eff_q12[3];
            int64_t s_acc = (int64_t)Wl[i * 10u + 4u] * r_eff_q12[4] + (int64_t)Wl[i * 10u + 5u] * r_eff_q12[5]
                          + (int64_t)Wl[i * 10u + 6u] * r_eff_q12[6] + (int64_t)Wl[i * 10u + 7u] * r_eff_q12[7];
            jump_leg_q12[i]  = (int32_t)(j_acc >> 12);
            rev_leg_q12[i]   = (int32_t)(r_acc >> 12);
            steer_leg_q12[i] = (int32_t)(s_acc >> 12);
        }

        jump_asym_q12  = (jump_leg_q12[0] + jump_leg_q12[2] + jump_leg_q12[4])
                       - (jump_leg_q12[1] + jump_leg_q12[3] + jump_leg_q12[5]);
        steer_asym_q12 = (steer_leg_q12[0] + steer_leg_q12[2] + steer_leg_q12[4])
                       - (steer_leg_q12[1] + steer_leg_q12[3] + steer_leg_q12[5]);

        rev_pos_sum_q12 = 0;
        for (i = 0u; i < 6u; ++i) {
            if (rev_leg_q12[i] > 0) {
                rev_pos_sum_q12 += rev_leg_q12[i];
            }
        }

        dnp01_q12 = nl_clamp_i32((r_dn_q12[0] + r_dn_q12[1]) >> 1, 0, 4096);
        mdn_q12   = nl_clamp_i32((rev_pos_sum_q12 * 2662) >> 12, 0, 4096);
        dnp09_q12 = nl_clamp_i32((r_dn_q12[8] + r_dn_q12[9]) >> 1, 0, 4096);

        fwd_surv_q12 = (dnp09_q12 * nl_clamp_i32(4096 - ((dnp01_q12 * 2867) >> 12), 0, 4096)) >> 12;
        drive_q12  = nl_clamp_i32(fwd_surv_q12 - ((mdn_q12 * 3482) >> 12), -4096, 4096);
        yaw_q12    = nl_clamp_i32(((steer_asym_q12 * 4506) - (jump_asym_q12 * 5530)) >> 12, -4096, 4096);
        strafe_q12 = nl_clamp_i32((jump_asym_q12 * 5734) >> 12, -3482, 3482);

        dnp01_q15  = (dnp01_q12 * NC_Q15_ONE) >> 12;
        mdn_q15    = (mdn_q12 * NC_Q15_ONE) >> 12;
        drive_q15  = (drive_q12 * NC_Q15_ONE) >> 12;
        yaw_q15    = (yaw_q12 * NC_Q15_ONE) >> 12;
        strafe_q15 = (strafe_q12 * NC_Q15_ONE) >> 12;

        if (drive_q12 < -205) {
            mode = (uint8_t)NL_MODE_MOONWALK_REVERSE;
        } else if (dnp01_q12 > 492 || jump_asym_q12 > 328 || jump_asym_q12 < -328) {
            mode = (uint8_t)NL_MODE_DODGE_LOOM;
        } else if (health_gate_q12 < 4096) {
            mode = (uint8_t)NL_MODE_LIMP_HOME;
        } else if (nov_exc_q12 > 205) {
            mode = (uint8_t)NL_MODE_PLUME_CAST;
        } else {
            mode = (uint8_t)NL_MODE_CRUISE_HOME;
        }

        out->dnp01_brake_q15 = nl_clamp_q15(dnp01_q15);
        out->mdn_reverse_q15 = nl_clamp_q15(mdn_q15);
        out->dna02_left_q15  = nl_clamp_q15((nl_clamp_i32(r_dn_q12[4], 0, 4096) * NC_Q15_ONE) >> 12);
        out->dna02_right_q15 = nl_clamp_q15((nl_clamp_i32(r_dn_q12[5], 0, 4096) * NC_Q15_ONE) >> 12);
        out->behavior_mode = mode;
    } else {
        /* -----------------------------------------------------------------
         * 4B. Legacy v2_analytic Fallback (when dn_weights == NULL)
         * ----------------------------------------------------------------- */
        uint32_t weighted_q10 = ((uint32_t)out->looming_score_q10 * 150u
                               + (uint32_t)out->lc4_bar_score_q10 * 106u) >> 8;
        eff_loom_q10 = (out->looming_score_q10 > weighted_q10)
                     ? out->looming_score_q10
                     : (uint16_t)weighted_q10;

        if (eff_loom_q10 > thresh_q10) {
            uint32_t span = (cns->loom_reverse_q10 > thresh_q10)
                          ? (uint32_t)(cns->loom_reverse_q10 - thresh_q10)
                          : 64u;
            dnp01_q15 = nl_clamp_i32(
                (int32_t)(((uint32_t)(eff_loom_q10 - thresh_q10) * (uint32_t)NC_Q15_ONE) / span),
                0,
                NC_Q15_ONE
            );
        }

        if (eff_loom_q10 > cns->loom_reverse_q10) {
            int32_t ax_abs = (out->avoid_x_q7 < 0) ? -(int32_t)out->avoid_x_q7 : (int32_t)out->avoid_x_q7;
            if (ax_abs < 96) {
                mdn_q15 = nl_clamp_i32(
                    (int32_t)(((uint32_t)(eff_loom_q10 - cns->loom_reverse_q10) * (uint32_t)NC_Q15_ONE) / 80u),
                    8192,
                    NC_Q15_ONE
                );
            }
        }

        if (eff_loom_q10 >= thresh_q10 || out->loom_triggered) {
            int32_t dodge_dir = (out->avoid_x_q7 >= 0) ? -1 : +1;
            int32_t mag_q7 = (out->avoid_x_q7 < 0) ? -(int32_t)out->avoid_x_q7 : (int32_t)out->avoid_x_q7;
            if (mag_q7 < 48) {
                mag_q7 = 96;
            }
            evade_yaw_q15 = dodge_dir * nl_clamp_i32((mag_q7 * NC_Q15_ONE) / 127, 0, NC_Q15_ONE);
            strafe_q15 = (-dodge_dir) * nl_clamp_i32((mag_q7 * 24576) / 127, 0, 24576);
        }

        {
            uint8_t severe_fault = (out->vib_novelty_q16 > cns->vib_fault_thresh_q16) || (min_health < 14745);
            uint8_t plume_cast = (out->chem_novelty_q16 > cns->nov_alert_q16);
            int32_t base_drive = (int32_t)nl_clamp_i32(in->cruise_drive_q15, 0, NC_Q15_ONE);
            if (severe_fault) {
                base_drive = (base_drive * 18022) >> 15;
            }

            if (mdn_q15 > 0) {
                drive_q15 = -((mdn_q15 * 21299) >> 15);
                yaw_q15 = evade_yaw_q15;
                mode = (uint8_t)NL_MODE_MOONWALK_REVERSE;
            } else if (eff_loom_q10 >= thresh_q10 || out->loom_triggered) {
                int32_t brake_keep = NC_Q15_ONE - ((dnp01_q15 * 22937) >> 15);
                drive_q15 = nl_clamp_i32((base_drive * brake_keep) >> 15, 2048, NC_Q15_ONE);
                yaw_q15 = nl_clamp_i32(((evade_yaw_q15 * 26214) + (cx_yaw_q15 * 6554)) >> 15, -NC_Q15_ONE, NC_Q15_ONE);
                mode = (uint8_t)NL_MODE_DODGE_LOOM;
            } else if (severe_fault) {
                drive_q15 = base_drive;
                yaw_q15 = cx_yaw_q15;
                mode = (uint8_t)NL_MODE_LIMP_HOME;
            } else if (plume_cast) {
                int32_t cast_sin = nc_sin_q15((nc_bam_t)cns->cast_phase_bam);
                drive_q15 = (base_drive * 26214) >> 15;
                yaw_q15 = nl_clamp_i32(cx_yaw_q15 + ((cast_sin * 9175) >> 15), -NC_Q15_ONE, NC_Q15_ONE);
                mode = (uint8_t)NL_MODE_PLUME_CAST;
            } else {
                drive_q15 = base_drive;
                yaw_q15 = cx_yaw_q15;
                mode = (uint8_t)NL_MODE_CRUISE_HOME;
            }
        }

        out->dnp01_brake_q15 = nl_clamp_q15(dnp01_q15);
        out->mdn_reverse_q15 = nl_clamp_q15(mdn_q15);
        out->dna02_left_q15 = (yaw_q15 > 0) ? nl_clamp_q15(yaw_q15) : 0;
        out->dna02_right_q15 = (yaw_q15 < 0) ? nl_clamp_q15(-yaw_q15) : 0;
        out->behavior_mode = mode;
    }

    out->desc_cmd.drive_fwd_q15 = nl_clamp_q15(drive_q15);
    out->desc_cmd.turn_yaw_q15 = nl_clamp_q15(yaw_q15);
    out->desc_cmd.strafe_lat_q15 = nl_clamp_q15(strafe_q15);
    out->desc_cmd.gait_mode = (uint8_t)NC_GAIT_AUTO;
    out->desc_cmd.freeze_reflex = (dnp01_q15 > 30146 && mdn_q15 == 0) ? 1u : 0u;
    out->desc_cmd.seq = (uint8_t)(cns->desc_seq & 0x03u);
    cns->desc_seq = (uint8_t)((cns->desc_seq + 1u) & 0x03u);

    (void)nc_encode_descending_4b(&out->desc_cmd, out->cervical_wire_4b);

    /* ---------------------------------------------------------------------
     * 5. Ventral Nerve Cord: ESC Observer + 4-DOF Shunting Allocator + 18-DOF IK
     * --------------------------------------------------------------------- */
    if (cns->vehicle_mode == (uint8_t)NL_VEHICLE_HEXAPOD_18DOF) {
        nc_cpg_state_t cpg_st;
        nc_hexapod_ik_out_t ik_out;
        (void)nc_cpg_step(&cns->cpg_cord, &out->desc_cmd, dt_ms, &cpg_st);
        (void)nc_hexapod_ik_3dof(&cns->ik_cfg, &cns->cpg_cord, &cpg_st, &ik_out);
        memcpy(out->hexapod_pwm_us, ik_out.pwm_us, sizeof(out->hexapod_pwm_us));
        out->hexapod_parked_mask = ik_out.parked_mask;
        min_health = NC_Q15_ONE;
        for (i = 0u; i < NC_NUM_LIMBS; ++i) {
            if (cns->cpg_cord.limb_health_q15[i] < min_health) {
                min_health = cns->cpg_cord.limb_health_q15[i];
            }
        }
        out->min_health_q15 = min_health;
    } else {
        int32_t wrench_q14[4] = { 0, 0, 0, 0 };
        int16_t fb_buf[NC_MAX_ACTUATORS];
        const int16_t *fb_ptr = in->thrust_fb_q15;
        nc_alloc4d_result_t ares;

        if (in->esc_rpm != NULL && in->esc_current_ma != NULL) {
            nc_esc_obs_result_t eres;
            if (nc_esc_observer_update(
                    &cns->esc_obs,
                    cns->thruster_cord.last_cmd_q15,
                    in->esc_rpm,
                    in->esc_current_ma,
                    dt_ms,
                    &eres
                ) == NC_OK) {
                memcpy(fb_buf, eres.est_thrust_q15, sizeof(fb_buf));
                fb_ptr = fb_buf;
                out->esc_broken_mask = eres.broken_prop_mask;
            }
        }

        if (cns->thruster_cord.n_dof == 4u) {
            /* 4-DOF Multicopter: [Fz hover + surge_climb, Tx roll, Ty pitch, Tz yaw] in Q14 */
            wrench_q14[0] = 32768 + ((int32_t)out->desc_cmd.drive_fwd_q15 / 4);
            wrench_q14[1] = ((int32_t)out->desc_cmd.strafe_lat_q15 * 4096) / NC_Q15_ONE;
            wrench_q14[2] = ((int32_t)out->desc_cmd.drive_fwd_q15 * 4096) / NC_Q15_ONE;
            wrench_q14[3] = ((int32_t)out->desc_cmd.turn_yaw_q15 * 2048) / NC_Q15_ONE;
        } else {
            /* 3-DOF Marine USV / Omni Rover: [Fx surge, Fy sway, Tz yaw] in Q14 */
            wrench_q14[0] = ((int32_t)out->desc_cmd.drive_fwd_q15 * NC_Q14_ONE) / NC_Q15_ONE;
            wrench_q14[1] = ((int32_t)out->desc_cmd.strafe_lat_q15 * NC_Q14_ONE) / NC_Q15_ONE;
            wrench_q14[2] = ((int32_t)out->desc_cmd.turn_yaw_q15 * (NC_Q14_ONE / 2)) / NC_Q15_ONE;
        }

        if (fb_ptr != NULL) {
            (void)nc_shunting4d_step(&cns->thruster_cord, wrench_q14, fb_ptr, dt_ms, &ares);
        } else {
            (void)nc_shunting4d_allocate(&cns->thruster_cord, wrench_q14, &ares);
        }

        min_health = NC_Q15_ONE;
        for (i = 0u; i < cns->thruster_cord.n_actuators; ++i) {
            out->actuator_cmd_q15[i] = ares.cmd_q15[i];
            out->actuator_health_q15[i] = ares.health_q15[i];
            if (ares.health_q15[i] < min_health) {
                min_health = ares.health_q15[i];
            }
        }
        out->min_health_q15 = min_health;
        out->isolated_mask = ares.isolated_mask;
    }

    return 0;
}

size_t nl_cns_encode_mavlink_control(
    nl_cns_t *cns,
    const nl_cns_outputs_t *out,
    uint8_t target_sysid,
    uint8_t out_pkt[NL_MAV2_MAX_FRAME_LEN]
)
{
    nl_mav_manual_control_t mc;
    if (cns == NULL || out == NULL || out_pkt == NULL) {
        return 0u;
    }
    mc.x = (int16_t)(((int32_t)out->desc_cmd.drive_fwd_q15 * 1000) / NC_Q15_ONE);
    mc.y = (int16_t)(((int32_t)out->desc_cmd.strafe_lat_q15 * 1000) / NC_Q15_ONE);
    mc.z = 500;
    mc.r = (int16_t)(((int32_t)out->desc_cmd.turn_yaw_q15 * 1000) / NC_Q15_ONE);
    mc.buttons = (uint16_t)((out->loom_triggered ? 1u : 0u) | ((uint16_t)out->behavior_mode << 1));
    mc.target = target_sysid;
    return nl_mav_encode_manual_control(cns->mav_sysid, cns->mav_compid, &cns->mav_tx_seq, &mc, out_pkt);
}

/* =========================================================================
 * WebAssembly / FFI Singleton & Flat Telemetry Interface
 * ========================================================================= */
#if defined(__EMSCRIPTEN__) || defined(__wasm__) || defined(NL_CNS_WASM_SINGLETON)
static nl_cns_t        g_wasm_cns;
static uint8_t         g_wasm_cam_64x64[64 * 64];
static nl_cns_outputs_t g_wasm_last_out;
static int32_t         g_wasm_telem[24];

nl_cns_t *nl_wasm_get_cns(void)
{
    return &g_wasm_cns;
}

uint32_t nl_wasm_sizeof_cns(void)
{
    return (uint32_t)sizeof(nl_cns_t);
}

void nl_wasm_init(int32_t vehicle_mode)
{
    nl_cns_init(&g_wasm_cns, (nl_vehicle_mode_t)vehicle_mode);
}

uint8_t *nl_wasm_frame_buf(void)
{
    return g_wasm_cam_64x64;
}

int32_t nl_wasm_step_webcam_and_imu(
    uint16_t dt_ms,
    int32_t speed_mm_s,
    int32_t gx_mrad_s,
    int32_t gy_mrad_s,
    int32_t gz_mrad_s,
    int32_t ax_mg,
    int32_t ay_mg,
    int32_t mz_spike_cdeg,
    int32_t thruster1_health_q15,
    uint8_t has_camera
)
{
    nl_cns_inputs_t in;
    int16_t fb_q15[NC_MAX_ACTUATORS];
    uint8_t i;
    nc_bam_t true_mag_bam;

    memset(&in, 0, sizeof(in));
    in.dt_ms = dt_ms;
    in.speed_mm_s = speed_mm_s;
    in.cruise_drive_q15 = 21299; /* ~0.65 cruise */
    in.return_home = 1u;
    in.gx_mrad_s = gx_mrad_s;
    in.gy_mrad_s = gy_mrad_s;
    in.gz_mrad_s = gz_mrad_s;
    in.ax_mg = ax_mg;
    in.ay_mg = ay_mg;
    in.az_mg = 1000;

    true_mag_bam = nc_bam_add(
        nc_compass_heading_bam(&g_wasm_cns.plastic_ring.comp),
        nc_cdeg_to_bam(mz_spike_cdeg)
    );
    in.mx = nc_cos_q15(true_mag_bam);
    in.my = -nc_sin_q15(true_mag_bam);
    in.mz = 0;
    in.mag_valid = 1u;
    if (has_camera) {
        in.cam_raw_u8 = g_wasm_cam_64x64;
        in.cam_h = 64u;
        in.cam_w = 64u;
    }

    for (i = 0u; i < NC_MAX_ACTUATORS; ++i) {
        fb_q15[i] = g_wasm_cns.thruster_cord.last_cmd_q15[i];
    }
    fb_q15[1] = (int16_t)(((int32_t)fb_q15[1] * nl_clamp_i32(thruster1_health_q15, 0, NC_Q15_ONE)) >> 15);
    in.thrust_fb_q15 = fb_q15;

    (void)nl_cns_step(&g_wasm_cns, &in, &g_wasm_last_out);

    g_wasm_telem[0]  = g_wasm_last_out.heading_cdeg;
    g_wasm_telem[1]  = (int32_t)g_wasm_last_out.home_dist_mm;
    g_wasm_telem[2]  = (int32_t)g_wasm_last_out.looming_score_q10;
    g_wasm_telem[3]  = (int32_t)g_wasm_last_out.lc4_bar_score_q10;
    g_wasm_telem[4]  = (int32_t)g_wasm_last_out.est_ttc_ms;
    g_wasm_telem[5]  = (int32_t)g_wasm_last_out.loom_triggered;
    g_wasm_telem[6]  = (int32_t)g_wasm_last_out.avoid_x_q7;
    g_wasm_telem[7]  = (int32_t)g_wasm_last_out.avoid_y_q7;
    g_wasm_telem[8]  = (int32_t)g_wasm_last_out.dnp01_brake_q15;
    g_wasm_telem[9]  = (int32_t)g_wasm_last_out.mdn_reverse_q15;
    g_wasm_telem[10] = (int32_t)g_wasm_last_out.desc_cmd.drive_fwd_q15;
    g_wasm_telem[11] = (int32_t)g_wasm_last_out.desc_cmd.turn_yaw_q15;
    g_wasm_telem[12] = (int32_t)g_wasm_last_out.desc_cmd.strafe_lat_q15;
    g_wasm_telem[13] = (int32_t)g_wasm_last_out.behavior_mode;
    g_wasm_telem[14] = (int32_t)g_wasm_last_out.actuator_cmd_q15[0];
    g_wasm_telem[15] = (int32_t)g_wasm_last_out.actuator_cmd_q15[1];
    g_wasm_telem[16] = (int32_t)g_wasm_last_out.actuator_cmd_q15[2];
    g_wasm_telem[17] = (int32_t)g_wasm_last_out.actuator_cmd_q15[3];
    g_wasm_telem[18] = (int32_t)g_wasm_last_out.actuator_health_q15[1];
    g_wasm_telem[19] = (int32_t)sizeof(nl_cns_t);
    g_wasm_telem[20] = (int32_t)g_wasm_cns.last_sight.focus_x;
    g_wasm_telem[21] = (int32_t)g_wasm_cns.last_sight.focus_y;
    g_wasm_telem[22] = ((int32_t)g_wasm_last_out.cervical_wire_4b[0] << 24)
                     | ((int32_t)g_wasm_last_out.cervical_wire_4b[1] << 16)
                     | ((int32_t)g_wasm_last_out.cervical_wire_4b[2] << 8)
                     |  (int32_t)g_wasm_last_out.cervical_wire_4b[3];
    g_wasm_telem[23] = (int32_t)g_wasm_cns.tick_count;
    return (int32_t)g_wasm_last_out.behavior_mode;
}

const int32_t *nl_wasm_get_telemetry(void)
{
    return g_wasm_telem;
}
#endif
