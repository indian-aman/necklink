/*
 * NeckCompass v2.0.0 ANSI C99 Embedded Core Implementation
 * Zero heap allocation, zero FPU instructions, pure Q15 / Q12 / int32_t / int64_t arithmetic.
 *
 * SPDX-License-Identifier: MIT
 */

#include "neckcompass.h"

/* Quarter-wave Q15 sine lookup table: sin((pi/2) * i / 64) * 32767 for i = 0..64 (130 bytes) */
static const int16_t NC_SIN_Q15_LUT[65] = {
        0,   804,  1608,  2410,  3212,  4011,  4808,  5602,
     6393,  7179,  7962,  8739,  9512, 10278, 11039, 11793,
    12539, 13279, 14010, 14732, 15446, 16151, 16846, 17530,
    18204, 18868, 19519, 20159, 20787, 21403, 22005, 22594,
    23170, 23731, 24279, 24811, 25329, 25832, 26319, 26790,
    27245, 27683, 28105, 28510, 28898, 29268, 29621, 29956,
    30273, 30571, 30852, 31113, 31356, 31580, 31785, 31971,
    32137, 32285, 32412, 32521, 32609, 32678, 32728, 32757,
    32767
};

/* Octant arctangent LUT in BAM: atan(i / 64) * 8192 / (pi/4) for i = 0..64 (130 bytes) */
static const uint16_t NC_ATAN_BAM_LUT[65] = {
       0,  163,  326,  489,  651,  813,  975, 1136,
    1297, 1457, 1617, 1775, 1933, 2090, 2246, 2401,
    2555, 2708, 2860, 3010, 3159, 3307, 3453, 3599,
    3742, 3884, 4025, 4164, 4302, 4438, 4572, 4705,
    4836, 4966, 5094, 5220, 5344, 5467, 5589, 5708,
    5826, 5943, 6058, 6171, 6282, 6392, 6500, 6607,
    6712, 6815, 6917, 7018, 7117, 7214, 7310, 7405,
    7498, 7589, 7679, 7768, 7856, 7942, 8026, 8110,
    8192
};

/* UB-safe symmetric nearest-integer division by 2^shift for signed int64_t */
static int64_t nc_div_pow2_round(int64_t val, unsigned int shift) {
    uint64_t half;
    if (shift == 0u) {
        return val;
    }
    half = (uint64_t)1u << (shift - 1u);
    if (val >= 0) {
        return (int64_t)(((uint64_t)val + half) >> shift);
    }
    return -(int64_t)(((uint64_t)(-(val + 1LL)) + 1ULL + half) >> shift);
}

static int64_t nc_div_pow2_trunc(int64_t val, unsigned int shift) {
    if (shift == 0u) {
        return val;
    }
    if (val >= 0) {
        return (int64_t)(((uint64_t)val) >> shift);
    }
    return -(int64_t)(((uint64_t)(-(val + 1LL)) + 1ULL) >> shift);
}

static nc_bam_t nc_wedge_theta(uint32_t idx) {
    uint16_t u = (uint16_t)(32768u + (idx & 15u) * 4096u);
    return (nc_bam_t)u;
}

static nc_bam_t nc_r_neuron_theta(uint32_t idx) {
    uint16_t u = (uint16_t)(32768u + (idx & 31u) * 2048u);
    return (nc_bam_t)u;
}

nc_bam_t nc_bam_add(nc_bam_t a, nc_bam_t b) {
    return (nc_bam_t)((uint16_t)a + (uint16_t)b);
}

nc_bam_t nc_bam_sub(nc_bam_t a, nc_bam_t b) {
    return (nc_bam_t)((uint16_t)a - (uint16_t)b);
}

int32_t nc_cdeg_s_to_bam_s(int32_t cdeg_s) {
    /* 36000 centidegrees/s == 65536 BAM/s (unwrapped int32_t for rates > +/-180 deg/s) */
    int64_t scaled = (int64_t)cdeg_s * 65536LL;
    int64_t q = (scaled >= 0) ? ((scaled + 18000LL) / 36000LL)
                              : ((scaled - 18000LL) / 36000LL);
    return (int32_t)q;
}

int32_t nc_bam_s_to_cdeg_s(int32_t bam_s) {
    int64_t scaled = (int64_t)bam_s * 36000LL;
    return (int32_t)nc_div_pow2_round(scaled, 16u);
}

int32_t nc_mrad_s_to_bam_s(int32_t mrad_s) {
    /* 2 * pi * 1000 ~= 6283.1853 mrad/s -> 6283185 urad/s == 65536 BAM/s */
    int64_t scaled = (int64_t)mrad_s * 65536000LL;
    int64_t q = (scaled >= 0) ? ((scaled + 3141593LL) / 6283185LL)
                              : ((scaled - 3141593LL) / 6283185LL);
    return (int32_t)q;
}

nc_bam_t nc_cdeg_to_bam(int32_t centideg) {
    int32_t q = nc_cdeg_s_to_bam_s(centideg);
    return (nc_bam_t)((uint16_t)q);
}

int32_t nc_bam_to_cdeg(nc_bam_t bam) {
    return nc_bam_s_to_cdeg_s((int32_t)bam);
}

nc_bam_t nc_mrad_to_bam(int32_t mrad) {
    int32_t q = nc_mrad_s_to_bam_s(mrad);
    return (nc_bam_t)((uint16_t)q);
}

int16_t nc_sin_q15(nc_bam_t bam) {
    uint16_t u = (uint16_t)bam;
    uint16_t quad = (uint16_t)((u >> 14) & 3u);
    uint16_t phase = (uint16_t)(u & 0x3FFFu); /* 0..16383 within quadrant */
    uint16_t mirrored = (quad & 1u) ? (uint16_t)(16384u - phase) : phase;
    uint32_t idx = (uint32_t)(mirrored >> 8); /* 0..64 */
    uint32_t frac = (uint32_t)(mirrored & 0xFFu);
    int32_t v0 = NC_SIN_Q15_LUT[idx];
    int32_t v1 = (idx < 64u) ? NC_SIN_Q15_LUT[idx + 1u] : NC_Q15_ONE;
    int32_t interp = v0 + (int32_t)nc_div_pow2_round((int64_t)(v1 - v0) * (int64_t)frac, 8u);
    return (quad & 2u) ? (int16_t)(-interp) : (int16_t)interp;
}

int16_t nc_cos_q15(nc_bam_t bam) {
    return nc_sin_q15(nc_bam_add(bam, (nc_bam_t)16384));
}

nc_bam_t nc_atan2_bam(int32_t y, int32_t x) {
    uint64_t ax, ay, mx, mn;
    uint32_t ratio_q14, idx, frac;
    uint16_t base_ang, oct_ang, full_u;

    if (x == 0 && y == 0) {
        return 0;
    }
    ax = (x < 0) ? (uint64_t)(-(int64_t)x) : (uint64_t)x;
    ay = (y < 0) ? (uint64_t)(-(int64_t)y) : (uint64_t)y;

    if (ax >= ay) {
        mx = ax;
        mn = ay;
    } else {
        mx = ay;
        mn = ax;
    }

    ratio_q14 = (uint32_t)((mn << 14) / mx); /* 0..16384 */
    idx = ratio_q14 >> 8;                    /* 0..64    */
    frac = ratio_q14 & 0xFFu;
    if (idx >= 64u) {
        base_ang = 8192u;
    } else {
        uint32_t a0 = NC_ATAN_BAM_LUT[idx];
        uint32_t a1 = NC_ATAN_BAM_LUT[idx + 1u];
        base_ang = (uint16_t)(a0 + (((a1 - a0) * frac + 128u) >> 8));
    }

    oct_ang = (ax >= ay) ? base_ang : (uint16_t)(16384u - base_ang);
    if (x >= 0) {
        full_u = (y >= 0) ? oct_ang : (uint16_t)(0u - oct_ang);
    } else {
        full_u = (y >= 0) ? (uint16_t)(32768u - oct_ang) : (uint16_t)(32768u + oct_ang);
    }
    return (nc_bam_t)full_u;
}

uint32_t nc_isqrt64(uint64_t val) {
    uint64_t res = 0;
    uint64_t bit = (uint64_t)1u << 62;
    while (bit > val) {
        bit >>= 2;
    }
    while (bit != 0) {
        if (val >= res + bit) {
            val -= res + bit;
            res = (res >> 1) + bit;
        } else {
            res >>= 1;
        }
        bit >>= 2;
    }
    return (uint32_t)res;
}

/* =========================================================================
 * 9-Axis 3D Tilt-Compensated IMU + Magnetometer Front-End
 * ========================================================================= */

/* Pitch clamp: +/-85.00 deg == +/-15473 BAM == +/-3,961,088 in Q8 sub-BAM */
#define NC_MAX_PITCH_SUB_BAM 3961088
#define NC_MIN_COS_PITCH_Q15 2856 /* cos(85 deg) * 32767 */

static int32_t nc_clamp_pitch_sub_bam(int64_t p_sub) {
    if (p_sub > (int64_t)NC_MAX_PITCH_SUB_BAM) {
        return NC_MAX_PITCH_SUB_BAM;
    }
    if (p_sub < -(int64_t)NC_MAX_PITCH_SUB_BAM) {
        return -NC_MAX_PITCH_SUB_BAM;
    }
    return (int32_t)p_sub;
}

void nc_tilt_init(nc_tilt_t *tilt) {
    if (tilt == NULL) {
        return;
    }
    tilt->roll_sub_bam = 0;
    tilt->pitch_sub_bam = 0;
    tilt->tau_ms = 500u;
    tilt->gate_tol_mg = 250u; /* |norm(a) - 1000 mg| > 250 mg (0.25g) rejects wave slams */
    tilt->accel_gated = 0u;
    tilt->initialized = 0u;
}

void nc_tilt_reset(nc_tilt_t *tilt, nc_bam_t roll_bam, nc_bam_t pitch_bam) {
    int64_t p_sub;
    if (tilt == NULL) {
        return;
    }
    tilt->roll_sub_bam = (int32_t)((uint32_t)(uint16_t)roll_bam * 256u);
    p_sub = (int64_t)pitch_bam * 256LL;
    tilt->pitch_sub_bam = nc_clamp_pitch_sub_bam(p_sub);
    tilt->accel_gated = 0u;
    tilt->initialized = 1u;
}

nc_bam_t nc_tilt_roll_bam(const nc_tilt_t *tilt) {
    if (tilt == NULL || !tilt->initialized) {
        return 0;
    }
    return (nc_bam_t)((uint16_t)((((uint32_t)tilt->roll_sub_bam + 128u) >> 8) & 0xFFFFu));
}

nc_bam_t nc_tilt_pitch_bam(const nc_tilt_t *tilt) {
    if (tilt == NULL || !tilt->initialized) {
        return 0;
    }
    return (nc_bam_t)nc_div_pow2_round((int64_t)tilt->pitch_sub_bam, 8u);
}

nc_bam_t nc_tilt_compensate_mag_bam(
    nc_bam_t roll_bam,
    nc_bam_t pitch_bam,
    int32_t mx,
    int32_t my,
    int32_t mz
) {
    int64_t cr = (int64_t)nc_cos_q15(roll_bam);
    int64_t sr = (int64_t)nc_sin_q15(roll_bam);
    int64_t cp = (int64_t)nc_cos_q15(pitch_bam);
    int64_t sp = (int64_t)nc_sin_q15(pitch_bam);
    int64_t mx_q30 = (int64_t)mx * cp * (int64_t)NC_Q15_ONE
                   + (int64_t)my * sr * sp
                   + (int64_t)mz * cr * sp;
    int64_t my_q30 = ((int64_t)my * cr - (int64_t)mz * sr) * (int64_t)NC_Q15_ONE;
    int64_t neg_my_q30 = -my_q30;
    uint64_t ax = (mx_q30 < 0) ? (uint64_t)(-mx_q30) : (uint64_t)mx_q30;
    uint64_t ay = (neg_my_q30 < 0) ? (uint64_t)(-neg_my_q30) : (uint64_t)neg_my_q30;
    uint64_t peak = (ax > ay) ? ax : ay;
    unsigned int sh = 0u;

    if (peak == 0ULL) {
        return 0;
    }
    while ((peak >> sh) > 0x3FFFFFFFULL) {
        sh += 1u;
    }
    return nc_atan2_bam(
        (int32_t)nc_div_pow2_round(neg_my_q30, sh),
        (int32_t)nc_div_pow2_round(mx_q30, sh)
    );
}

nc_bam_t nc_tilt_update_9dof(
    nc_tilt_t *tilt,
    int32_t gx_bam_s,
    int32_t gy_bam_s,
    int32_t gz_bam_s,
    int32_t ax_mg,
    int32_t ay_mg,
    int32_t az_mg,
    int32_t mx,
    int32_t my,
    int32_t mz,
    uint32_t dt_ms,
    int32_t *out_yaw_rate_bam_s
) {
    uint64_t norm_sq, yz_sq;
    uint32_t acc_norm_mg, diff_mg, gate_tol;
    uint8_t gated;
    nc_bam_t roll_bam, pitch_bam;
    int64_t sr, cr, cp, cp_safe;
    int32_t omega_yaw_bam_s;

    if (tilt == NULL) {
        if (out_yaw_rate_bam_s != NULL) {
            *out_yaw_rate_bam_s = gz_bam_s;
        }
        return nc_tilt_compensate_mag_bam(0, 0, mx, my, mz);
    }

    norm_sq = (uint64_t)((int64_t)ax_mg * (int64_t)ax_mg)
            + (uint64_t)((int64_t)ay_mg * (int64_t)ay_mg)
            + (uint64_t)((int64_t)az_mg * (int64_t)az_mg);
    acc_norm_mg = nc_isqrt64(norm_sq);
    diff_mg = (acc_norm_mg >= 1000u) ? (acc_norm_mg - 1000u) : (1000u - acc_norm_mg);
    gate_tol = (tilt->gate_tol_mg > 0u) ? (uint32_t)tilt->gate_tol_mg : 250u;
    gated = (acc_norm_mg == 0u || diff_mg > gate_tol) ? 1u : 0u;
    tilt->accel_gated = gated;

    if (!tilt->initialized) {
        if (!gated) {
            yz_sq = (uint64_t)((int64_t)ay_mg * (int64_t)ay_mg)
                  + (uint64_t)((int64_t)az_mg * (int64_t)az_mg);
            roll_bam = nc_atan2_bam(ay_mg, az_mg);
            pitch_bam = nc_atan2_bam((int32_t)(-(int64_t)ax_mg), (int32_t)nc_isqrt64(yz_sq));
            nc_tilt_reset(tilt, roll_bam, pitch_bam);
        } else {
            nc_tilt_reset(tilt, 0, 0);
        }
        tilt->accel_gated = gated;
    } else if (dt_ms > 0u) {
        int64_t sp, q_sin_r_cos, yaw_rate_tmp, roll_dot_bam_s, pitch_dot_bam_s;
        int64_t d_roll_sub, d_pitch_sub;

        roll_bam = nc_tilt_roll_bam(tilt);
        pitch_bam = nc_tilt_pitch_bam(tilt);
        sr = (int64_t)nc_sin_q15(roll_bam);
        cr = (int64_t)nc_cos_q15(roll_bam);
        sp = (int64_t)nc_sin_q15(pitch_bam);
        cp = (int64_t)nc_cos_q15(pitch_bam);
        cp_safe = (cp < (int64_t)NC_MIN_COS_PITCH_Q15) ? (int64_t)NC_MIN_COS_PITCH_Q15 : cp;

        /* 3D Euler angle kinematics from body gyro (gx, gy, gz) */
        q_sin_r_cos = (int64_t)gy_bam_s * sr + (int64_t)gz_bam_s * cr;
        yaw_rate_tmp = q_sin_r_cos / cp_safe;
        roll_dot_bam_s = (int64_t)gx_bam_s + (yaw_rate_tmp * sp) / (int64_t)NC_Q15_ONE;
        pitch_dot_bam_s = ((int64_t)gy_bam_s * cr - (int64_t)gz_bam_s * sr) / (int64_t)NC_Q15_ONE;

        d_roll_sub = (roll_dot_bam_s * (int64_t)dt_ms * 256LL) / 1000LL;
        d_pitch_sub = (pitch_dot_bam_s * (int64_t)dt_ms * 256LL) / 1000LL;

        tilt->roll_sub_bam = (int32_t)(((uint32_t)tilt->roll_sub_bam + (uint32_t)((int32_t)d_roll_sub)) & 0x00FFFFFFu);
        tilt->pitch_sub_bam = nc_clamp_pitch_sub_bam((int64_t)tilt->pitch_sub_bam + d_pitch_sub);

        /* Complementary gravity fusion ONLY when |norm(a) - 1g| <= 0.25g */
        if (!gated) {
            uint32_t tau = (tilt->tau_ms > 0u) ? (uint32_t)tilt->tau_ms : 500u;
            int64_t alpha_q15 = ((int64_t)dt_ms * (int64_t)NC_Q15_ONE) / (int64_t)(tau + dt_ms);
            nc_bam_t roll_pred = nc_tilt_roll_bam(tilt);
            nc_bam_t pitch_pred = nc_tilt_pitch_bam(tilt);
            nc_bam_t roll_acc, pitch_acc, err_r, err_p;
            int32_t corr_r_sub, corr_p_sub;

            yz_sq = (uint64_t)((int64_t)ay_mg * (int64_t)ay_mg)
                  + (uint64_t)((int64_t)az_mg * (int64_t)az_mg);
            roll_acc = nc_atan2_bam(ay_mg, az_mg);
            pitch_acc = nc_atan2_bam((int32_t)(-(int64_t)ax_mg), (int32_t)nc_isqrt64(yz_sq));

            err_r = nc_bam_sub(roll_acc, roll_pred);
            err_p = nc_bam_sub(pitch_acc, pitch_pred);

            corr_r_sub = (int32_t)((alpha_q15 * (int64_t)err_r * 256LL) / (int64_t)NC_Q15_ONE);
            corr_p_sub = (int32_t)((alpha_q15 * (int64_t)err_p * 256LL) / (int64_t)NC_Q15_ONE);

            tilt->roll_sub_bam = (int32_t)(((uint32_t)tilt->roll_sub_bam + (uint32_t)corr_r_sub) & 0x00FFFFFFu);
            tilt->pitch_sub_bam = nc_clamp_pitch_sub_bam((int64_t)tilt->pitch_sub_bam + (int64_t)corr_p_sub);
        }
    }

    roll_bam = nc_tilt_roll_bam(tilt);
    pitch_bam = nc_tilt_pitch_bam(tilt);
    sr = (int64_t)nc_sin_q15(roll_bam);
    cr = (int64_t)nc_cos_q15(roll_bam);
    cp = (int64_t)nc_cos_q15(pitch_bam);
    cp_safe = (cp < (int64_t)NC_MIN_COS_PITCH_Q15) ? (int64_t)NC_MIN_COS_PITCH_Q15 : cp;
    omega_yaw_bam_s = (int32_t)(((int64_t)gy_bam_s * sr + (int64_t)gz_bam_s * cr) / cp_safe);

    if (out_yaw_rate_bam_s != NULL) {
        *out_yaw_rate_bam_s = omega_yaw_bam_s;
    }
    return nc_tilt_compensate_mag_bam(roll_bam, pitch_bam, mx, my, mz);
}

/* =========================================================================
 * 16-Wedge E-PG <-> Delta7 Ring Attractor Compass
 * ========================================================================= */

static void nc_shape_epg_wedges(nc_compass_t *comp, nc_bam_t center_bam) {
    uint32_t i;
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int16_t c = nc_cos_q15(nc_bam_sub(th, center_bam));
        comp->wedges_q15[i] = (c > 0) ? c : 0;
    }
}

/* Cooperative dendritic coincidence gate in Q15 [0..32767]: (1 - r^2)^2 with
 * r = (1 - cos d) / gate_dist, so the gate is 0.5 at r = 0.541 and 0 at r >= 1. */
static int16_t nc_basin_gate_q15(const nc_compass_t *comp, nc_bam_t cue_bam, nc_bam_t pred_bam) {
    int32_t cos_d = (int32_t)nc_cos_q15(nc_bam_sub(cue_bam, pred_bam));
    int32_t dist = NC_Q15_ONE - cos_d;
    int32_t edge = (comp->gate_dist_q15 > 0) ? (int32_t)comp->gate_dist_q15 : 1;
    if (dist >= edge) {
        return 0;
    }
    if (dist <= 0) {
        return NC_Q15_ONE;
    }
    {
        int32_t r_q15 = (dist * NC_Q15_ONE) / edge;
        int32_t r2_q15 = (int32_t)(((int64_t)r_q15 * (int64_t)r_q15) >> 15);
        int32_t one_minus = NC_Q15_ONE - r2_q15;
        return (int16_t)(((int64_t)one_minus * (int64_t)one_minus) >> 15);
    }
}

/* Degree-3 raised-cosine R-neuron profile matched to 16-wedge E-PG 1st harmonic */
static void nc_inject_cue_ring(
    int32_t u_ring_q15[NC_NUM_WEDGES],
    nc_bam_t cue_bam,
    int32_t weight_q15
) {
    uint32_t i;
    if (weight_q15 <= 0) {
        return;
    }
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int32_t c_q15 = (int32_t)nc_cos_q15(nc_bam_sub(th, cue_bam));
        /* h = (1 + cos)/2 in Q15 [0..32767] */
        int32_t h_q15 = (NC_Q15_ONE + c_q15) >> 1;
        int32_t h2_q15 = (int32_t)(((int64_t)h_q15 * (int64_t)h_q15) >> 15);
        int32_t h3_q15 = (int32_t)(((int64_t)h2_q15 * (int64_t)h_q15) >> 15);
        /* Match 1st-harmonic norm of h^3 (3*N/16 = 3) to max(0,cos) (N/4 = 4) -> scale 4/3 */
        int32_t prof_q15 = (h3_q15 * 4) / 3;
        u_ring_q15[i] += (int32_t)nc_div_pow2_trunc((int64_t)weight_q15 * (int64_t)prof_q15, 15u);
    }
}

void nc_compass_init(nc_compass_t *comp) {
    uint32_t i;
    if (comp == NULL) {
        return;
    }
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        comp->wedges_q15[i] = 0;
    }
    comp->phase_sub_bam = 0;
    comp->pen_bias_bam_s = 0;
    comp->mag_gain_q8 = 410;   /* ~1.60 s^-1 */
    comp->sun_gain_q8 = 819;   /* ~3.20 s^-1 */
    comp->bias_rate_q8 = 140;  /* ~0.55 s^-2 */
    comp->last_mag_gate_q15 = 0;
    comp->offbasin_c_q15 = 0;
    comp->offbasin_s_q15 = 0;
    comp->offbasin_ms = 0;
    comp->reanchor_ms = 10000u;
    comp->initialized = 0;
    nc_compass_set_cue_noise(comp, 300);
}

void nc_compass_set_cue_noise(nc_compass_t *comp, int32_t noise_cdeg) {
    int32_t half_cdeg, dist_half, edge;
    if (comp == NULL) {
        return;
    }
    if (noise_cdeg < 1) {
        noise_cdeg = 1;
    }
    half_cdeg = 400 + noise_cdeg;
    if (half_cdeg > 6000) {
        half_cdeg = 6000; /* keep the basin well inside +/-90 deg */
    }
    dist_half = NC_Q15_ONE - (int32_t)nc_cos_q15(nc_cdeg_to_bam(half_cdeg));
    /* gate = 0.5 where (1 - r^2)^2 = 0.5 -> r = 0.5412 */
    edge = (dist_half * 10000) / 5412;
    if (edge < 8) {
        edge = 8;
    }
    if (edge > NC_Q15_ONE) {
        edge = NC_Q15_ONE;
    }
    comp->gate_dist_q15 = (int16_t)edge;
}

void nc_compass_set_reanchor_ms(nc_compass_t *comp, uint16_t reanchor_ms) {
    if (comp == NULL) {
        return;
    }
    comp->reanchor_ms = reanchor_ms;
    comp->offbasin_ms = 0;
}

static void nc_compass_seed(nc_compass_t *comp, nc_bam_t heading_bam) {
    nc_shape_epg_wedges(comp, heading_bam);
    comp->phase_sub_bam = (int32_t)((uint32_t)(uint16_t)heading_bam * 256u);
    comp->last_mag_gate_q15 = 0;
    comp->offbasin_c_q15 = 0;
    comp->offbasin_s_q15 = 0;
    comp->offbasin_ms = 0;
    comp->initialized = 1;
}

void nc_compass_reset(nc_compass_t *comp, nc_bam_t heading_bam) {
    if (comp == NULL) {
        return;
    }
    nc_compass_seed(comp, heading_bam);
    comp->pen_bias_bam_s = 0;
}

/* Track a consistent off-basin cue; returns 1 (and re-seeds the bump) once it has
 * persisted for reanchor_ms. Short ferromagnetic spikes never get that far. */
static int nc_compass_track_offbasin(nc_compass_t *comp, nc_bam_t cue_bam, nc_bam_t pred_bam, uint32_t dt_ms) {
    nc_bam_t innov = nc_bam_sub(cue_bam, pred_bam);
    int32_t c = (int32_t)nc_cos_q15(innov);
    int32_t s = (int32_t)nc_sin_q15(innov);
    int64_t r2;
    if (comp->offbasin_ms == 0u) {
        comp->offbasin_c_q15 = (int16_t)c;
        comp->offbasin_s_q15 = (int16_t)s;
    } else {
        /* first-order low-pass, tau = 1 s */
        int32_t a_q15 = (dt_ms >= 1000u) ? NC_Q15_ONE : (int32_t)((dt_ms * 32767u) / 1000u);
        comp->offbasin_c_q15 = (int16_t)(
            comp->offbasin_c_q15 + (int32_t)nc_div_pow2_trunc((int64_t)a_q15 * (int64_t)(c - comp->offbasin_c_q15), 15u)
        );
        comp->offbasin_s_q15 = (int16_t)(
            comp->offbasin_s_q15 + (int32_t)nc_div_pow2_trunc((int64_t)a_q15 * (int64_t)(s - comp->offbasin_s_q15), 15u)
        );
    }
    r2 = (int64_t)comp->offbasin_c_q15 * comp->offbasin_c_q15 + (int64_t)comp->offbasin_s_q15 * comp->offbasin_s_q15;
    /* coherence >= 0.8  <=>  r2 >= 0.64 * 2^30 */
    if (r2 < 687194767LL) {
        comp->offbasin_ms = 0;
        return 0;
    }
    {
        uint32_t t = (uint32_t)comp->offbasin_ms + dt_ms;
        comp->offbasin_ms = (uint16_t)((t > 65535u) ? 65535u : t);
    }
    if (comp->offbasin_ms >= comp->reanchor_ms) {
        nc_bam_t mean_innov = nc_atan2_bam(comp->offbasin_s_q15, comp->offbasin_c_q15);
        nc_compass_seed(comp, nc_bam_add(pred_bam, mean_innov));
        return 1;
    }
    return 0;
}

nc_bam_t nc_compass_heading_bam(const nc_compass_t *comp) {
    int64_t sum_c = 0;
    int64_t sum_s = 0;
    uint32_t i;
    if (comp == NULL || !comp->initialized) {
        return 0;
    }
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int64_t r = (int64_t)comp->wedges_q15[i];
        sum_c += nc_div_pow2_trunc(r * (int64_t)nc_cos_q15(th), 8u);
        sum_s += nc_div_pow2_trunc(r * (int64_t)nc_sin_q15(th), 8u);
    }
    return nc_atan2_bam((int32_t)sum_s, (int32_t)sum_c);
}

void nc_compass_delta7_q15(const nc_compass_t *comp, int16_t out_delta7_q15[NC_NUM_WEDGES]) {
    uint32_t i;
    nc_bam_t heading;
    if (out_delta7_q15 == NULL) {
        return;
    }
    if (comp == NULL || !comp->initialized) {
        for (i = 0; i < NC_NUM_WEDGES; ++i) {
            out_delta7_q15[i] = 0;
        }
        return;
    }
    /* BANC v888: 40 Delta7 glutamatergic bridge neurons form an anti-phase cosine inhibitory bump */
    heading = nc_compass_heading_bam(comp);
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int32_t c_q15 = (int32_t)nc_cos_q15(nc_bam_sub(th, heading));
        out_delta7_q15[i] = (int16_t)((NC_Q15_ONE - c_q15) >> 1);
    }
}

nc_bam_t nc_compass_update(
    nc_compass_t *comp,
    int32_t omega_bam_s,
    uint32_t dt_ms,
    nc_bam_t mag_bam,
    uint8_t mag_valid,
    int16_t mag_conf_q15,
    nc_bam_t sun_bam,
    uint8_t sun_valid,
    int16_t sun_conf_q15
) {
    int32_t u_ring[NC_NUM_WEDGES];
    int16_t delta7_inh[NC_NUM_WEDGES];
    int64_t net_rate_bam_s, d_sub_q8;
    nc_bam_t ring_bam, pred_bam, new_bam;
    int32_t m_conf, s_conf, cue_applied = 0;
    int64_t sub_pull_q8 = 0;
    int64_t weighted_err_bam = 0;
    uint32_t i;

    if (comp == NULL) {
        return 0;
    }

    m_conf = (mag_conf_q15 < 0) ? 0 : (int32_t)mag_conf_q15;
    s_conf = (sun_conf_q15 < 0) ? 0 : (int32_t)sun_conf_q15;

    if (!comp->initialized) {
        if (sun_valid && s_conf > 0) {
            nc_compass_reset(comp, sun_bam);
        } else if (mag_valid && m_conf > 0) {
            nc_compass_reset(comp, mag_bam);
        } else {
            nc_compass_reset(comp, 0);
        }
        if (dt_ms == 0u) {
            return nc_compass_heading_bam(comp);
        }
    }

    /* 1. Recurrent EPG <-> Delta7 state + P-EN asymmetric velocity shift on Q8 sub-BAM phase */
    ring_bam = nc_compass_heading_bam(comp);
    {
        nc_bam_t phase_bam = (nc_bam_t)((uint16_t)((((uint32_t)comp->phase_sub_bam + 128u) >> 8) & 0xFFFFu));
        nc_bam_t d_ring = nc_bam_sub(ring_bam, phase_bam);
        if (d_ring < -8 || d_ring > 8) {
            /* Resync sub-BAM phase if caller modified wedges_q15 directly */
            comp->phase_sub_bam = (int32_t)((uint32_t)(uint16_t)ring_bam * 256u);
        }
    }

    net_rate_bam_s = (int64_t)omega_bam_s - (int64_t)comp->pen_bias_bam_s;
    d_sub_q8 = (net_rate_bam_s * (int64_t)dt_ms * 256LL) / 1000LL;
    comp->phase_sub_bam = (int32_t)(((uint32_t)comp->phase_sub_bam + (uint32_t)((int32_t)d_sub_q8)) & 0x00FFFFFFu);
    pred_bam = (nc_bam_t)((uint16_t)((((uint32_t)comp->phase_sub_bam + 128u) >> 8) & 0xFFFFu));

    /* Delta7 anti-phase glutamatergic bridge inhibition sculpts the shifted E-PG bump */
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int32_t c = (int32_t)nc_cos_q15(nc_bam_sub(th, pred_bam));
        delta7_inh[i] = (int16_t)((NC_Q15_ONE - c) >> 1);
        /* EPG excitation minus anti-phase Delta7 bridge inhibition = half-wave cosine bump */
        u_ring[i] = (c > 0) ? (c - ((int32_t)delta7_inh[i] - ((NC_Q15_ONE - c) >> 1))) : 0;
    }

    /* 2. R-neuron multi-cue injection gated by active E-PG attractor basin */
    comp->last_mag_gate_q15 = 0;
    if (sun_valid && s_conf > 0 && dt_ms > 0u) {
        int16_t gate = nc_basin_gate_q15(comp, sun_bam, pred_bam);
        int64_t w_q15 = ((int64_t)comp->sun_gain_q8 * (int64_t)s_conf * (int64_t)gate * (int64_t)dt_ms)
                        / (256LL * 32767LL * 1000LL);
        if (w_q15 > 0) {
            nc_bam_t err_s = nc_bam_sub(sun_bam, pred_bam);
            nc_inject_cue_ring(u_ring, sun_bam, (int32_t)w_q15);
            sub_pull_q8 += (w_q15 * (int64_t)err_s * 256LL) / ((int64_t)NC_Q15_ONE + w_q15);
            weighted_err_bam += nc_div_pow2_round((int64_t)err_s * (int64_t)gate * (int64_t)s_conf, 30u);
            cue_applied = 1;
        }
    }

    if (mag_valid && m_conf > 0 && dt_ms > 0u) {
        int16_t gate = nc_basin_gate_q15(comp, mag_bam, pred_bam);
        int64_t w_q15;
        comp->last_mag_gate_q15 = gate;
        if (comp->reanchor_ms > 0u && gate < 1638 && !(sun_valid && s_conf > 0)) {
            if (nc_compass_track_offbasin(comp, mag_bam, pred_bam, dt_ms)) {
                return nc_compass_heading_bam(comp);
            }
        } else {
            comp->offbasin_ms = 0;
        }
        w_q15 = ((int64_t)comp->mag_gain_q8 * (int64_t)m_conf * (int64_t)gate * (int64_t)dt_ms)
                / (256LL * 32767LL * 1000LL);
        if (w_q15 > 0) {
            nc_bam_t err_m = nc_bam_sub(mag_bam, pred_bam);
            nc_inject_cue_ring(u_ring, mag_bam, (int32_t)w_q15);
            sub_pull_q8 += (w_q15 * (int64_t)err_m * 256LL) / ((int64_t)NC_Q15_ONE + w_q15);
            weighted_err_bam += nc_div_pow2_round((int64_t)err_m * (int64_t)gate * (int64_t)m_conf, 30u);
            cue_applied = 1;
        }
    }

    /* 3. Delta7 global inhibition + population vector readout across the 16 wedges */
    if (cue_applied) {
        int64_t sum_c = 0;
        int64_t sum_s = 0;
        nc_bam_t ring_shift_bam;
        for (i = 0; i < NC_NUM_WEDGES; ++i) {
            nc_bam_t th = nc_wedge_theta(i);
            sum_c += (int64_t)u_ring[i] * (int64_t)nc_cos_q15(th);
            sum_s += (int64_t)u_ring[i] * (int64_t)nc_sin_q15(th);
        }
        new_bam = nc_atan2_bam(
            (int32_t)nc_div_pow2_round(sum_s, 14u),
            (int32_t)nc_div_pow2_round(sum_c, 14u)
        );
        ring_shift_bam = nc_bam_sub(new_bam, pred_bam);
        if (ring_shift_bam >= -1 && ring_shift_bam <= 1) {
            /* Preserve sub-BAM gyro integration continuity during gentle cue pulls */
            comp->phase_sub_bam = (int32_t)(((uint32_t)comp->phase_sub_bam + (uint32_t)((int32_t)sub_pull_q8)) & 0x00FFFFFFu);
            new_bam = (nc_bam_t)((uint16_t)((((uint32_t)comp->phase_sub_bam + 128u) >> 8) & 0xFFFFu));
        } else {
            comp->phase_sub_bam = (int32_t)((uint32_t)(uint16_t)new_bam * 256u);
        }

        /* 4. Slow homeostatic P-EN bias trim (clamped to +/- 20 deg/s ~= +/-3640 BAM/s) */
        comp->pen_bias_bam_s -= (int32_t)((weighted_err_bam * (int64_t)comp->bias_rate_q8 * (int64_t)dt_ms)
                                          / (256LL * 1000LL));
        if (comp->pen_bias_bam_s > 3640) {
            comp->pen_bias_bam_s = 3640;
        } else if (comp->pen_bias_bam_s < -3640) {
            comp->pen_bias_bam_s = -3640;
        }
    } else {
        new_bam = pred_bam;
    }

    nc_shape_epg_wedges(comp, new_bam);
    return new_bam;
}

nc_bam_t nc_compass_update_9dof(
    nc_compass_t *comp,
    nc_tilt_t *tilt,
    const nc_imu9_t *imu,
    uint32_t dt_ms,
    uint8_t mag_valid,
    int16_t mag_conf_q15,
    nc_bam_t sun_bam,
    uint8_t sun_valid,
    int16_t sun_conf_q15
) {
    int32_t yaw_rate_bam_s = 0;
    nc_bam_t mag_bam;
    if (comp == NULL || imu == NULL) {
        return 0;
    }
    mag_bam = nc_tilt_update_9dof(
        tilt,
        imu->gx_bam_s, imu->gy_bam_s, imu->gz_bam_s,
        imu->ax_mg, imu->ay_mg, imu->az_mg,
        imu->mx, imu->my, imu->mz,
        dt_ms,
        &yaw_rate_bam_s
    );
    return nc_compass_update(
        comp, yaw_rate_bam_s, dt_ms,
        mag_bam, mag_valid, mag_conf_q15,
        sun_bam, sun_valid, sun_conf_q15
    );
}

/* =========================================================================
 * Plastic R-Neuron -> E-PG Soft-Iron / Harbor Self-Calibration (Zero-Float)
 * ========================================================================= */

void nc_plastic_ring_encode_r(nc_bam_t raw_mag_bam, int16_t out_r_q15[NC_NUM_R_NEURONS]) {
    uint32_t raw_act[NC_NUM_R_NEURONS];
    uint64_t sum_sq = 0ULL;
    uint32_t norm;
    uint32_t j;

    if (out_r_q15 == NULL) {
        return;
    }

    /*
     * Approximate von Mises exp(56 * (cos(d) - 1)) in pure Q15 integer arithmetic:
     *   (1 - (7/8) * (1 - cos(d)))^64  via 6 squarings, since (56 / 64) == 7 / 8!
     * Matches exp(-56*(1 - cos d)) to < 0.8% across active support with 0 LUT bytes & 0 floats.
     */
    for (j = 0; j < NC_NUM_R_NEURONS; ++j) {
        nc_bam_t th_r = nc_r_neuron_theta(j);
        int32_t c_q15 = (int32_t)nc_cos_q15(nc_bam_sub(th_r, raw_mag_bam));
        int32_t d_q15 = NC_Q15_ONE - c_q15; /* 1 - cos(d) in [0..65534] */
        int32_t u_q15 = (d_q15 * 7) >> 3;
        if (u_q15 >= NC_Q15_ONE) {
            raw_act[j] = 0u;
        } else {
            uint64_t p = (uint64_t)(NC_Q15_ONE - u_q15);
            unsigned int sq;
            for (sq = 0u; sq < 6u; ++sq) {
                p = (p * p + 16384ULL) >> 15;
            }
            raw_act[j] = (uint32_t)p;
            sum_sq += p * p;
        }
    }

    norm = nc_isqrt64(sum_sq);
    if (norm == 0u) {
        for (j = 0; j < NC_NUM_R_NEURONS; ++j) {
            out_r_q15[j] = 0;
        }
        return;
    }
    for (j = 0; j < NC_NUM_R_NEURONS; ++j) {
        uint64_t v = ((uint64_t)raw_act[j] * (uint64_t)NC_Q15_ONE + (uint64_t)(norm >> 1)) / (uint64_t)norm;
        out_r_q15[j] = (v > (uint64_t)NC_Q15_ONE) ? (int16_t)NC_Q15_ONE : (int16_t)v;
    }
}

static void nc_plastic_forward_drive(
    const nc_plastic_ring_t *pr,
    const int16_t r_q15[NC_NUM_R_NEURONS],
    int32_t out_drive_q12[NC_NUM_WEDGES],
    int32_t *out_c1_q15,
    int32_t *out_s1_q15
) {
    int64_t sum_c = 0;
    int64_t sum_s = 0;
    uint32_t i, j;

    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        int64_t acc = 0;
        nc_bam_t th_i = nc_wedge_theta(i);
        int32_t d_q12;
        for (j = 0; j < NC_NUM_R_NEURONS; ++j) {
            acc += (int64_t)pr->weights_q12[i][j] * (int64_t)r_q15[j];
        }
        d_q12 = (int32_t)nc_div_pow2_round(acc, 15u);
        if (out_drive_q12 != NULL) {
            out_drive_q12[i] = d_q12;
        }
        /* sum(d_q12 * cos_q15) across 16 wedges: for unit cosine (4096 * cos), sum = 8 * 4096 * 32767 = 2^30 */
        sum_c += (int64_t)d_q12 * (int64_t)nc_cos_q15(th_i);
        sum_s += (int64_t)d_q12 * (int64_t)nc_sin_q15(th_i);
    }
    if (out_c1_q15 != NULL) {
        *out_c1_q15 = (int32_t)nc_div_pow2_round(sum_c, 15u);
    }
    if (out_s1_q15 != NULL) {
        *out_s1_q15 = (int32_t)nc_div_pow2_round(sum_s, 15u);
    }
}

static void nc_plastic_recenter_zero_mean(nc_plastic_ring_t *pr) {
    int64_t mean_c = 0;
    int64_t mean_s = 0;
    uint32_t k;
    int16_t r_q15[NC_NUM_R_NEURONS];

    /* Sample 64 exact uniform BAM grid angles around [-180, +180) deg (step = 1024 BAM = 5.625 deg) */
    for (k = 0; k < 64u; ++k) {
        nc_bam_t g_bam = (nc_bam_t)((uint16_t)(k * 1024u));
        int32_t c1_q15 = 0;
        int32_t s1_q15 = 0;
        nc_bam_t mapped_bam, diff_bam;
        nc_plastic_ring_encode_r(g_bam, r_q15);
        nc_plastic_forward_drive(pr, r_q15, NULL, &c1_q15, &s1_q15);
        mapped_bam = nc_atan2_bam(s1_q15, c1_q15);
        diff_bam = nc_bam_sub(mapped_bam, g_bam);
        mean_c += (int64_t)nc_cos_q15(diff_bam);
        mean_s += (int64_t)nc_sin_q15(diff_bam);
    }
    pr->map_offset_bam = nc_atan2_bam((int32_t)mean_s, (int32_t)mean_c);
}

void nc_plastic_ring_init(nc_plastic_ring_t *pr) {
    uint32_t i, j;
    if (pr == NULL) {
        return;
    }
    nc_compass_init(&pr->comp);
    /* Match Python RingCompass magnetic bias scaling: mag_gain / max(mag_gain, sun_gain) = 1.6 / 3.2 = 0.5 */
    pr->comp.bias_rate_q8 = 70;
    pr->bias_rem_accum = 0;
    pr->learn_steps = 0u;
    pr->lr_q8 = 1024;            /* learning_rate = 4.0 in Q8 */
    pr->anchor_scale_q15 = 8192; /* anchor_gain_scale = 0.25 in Q15 */
    pr->map_offset_bam = 0;
    pr->prev_raw_bam = 0;
    pr->has_prev_raw = 0u;
    pr->has_abs_ref = 0u;

    /* Weak topographic identity prior: prior_strength = 0.25 -> 0.25 * 4096 = 1024 in Q12 */
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th_i = nc_wedge_theta(i);
        for (j = 0; j < NC_NUM_R_NEURONS; ++j) {
            nc_bam_t th_j = nc_r_neuron_theta(j);
            int32_t c_q15 = (int32_t)nc_cos_q15(nc_bam_sub(th_i, th_j));
            pr->weights_q12[i][j] = (int16_t)nc_div_pow2_round((int64_t)c_q15 * 1024LL, 15u);
        }
    }
}

void nc_plastic_ring_map(
    const nc_plastic_ring_t *pr,
    nc_bam_t raw_mag_bam,
    nc_bam_t *out_corrected_bam,
    int16_t *out_conf_q15
) {
    int16_t r_q15[NC_NUM_R_NEURONS];
    int32_t c1_q15 = 0;
    int32_t s1_q15 = 0;
    uint64_t amp_sq;
    uint32_t amp_q15;

    if (pr == NULL) {
        if (out_corrected_bam != NULL) {
            *out_corrected_bam = raw_mag_bam;
        }
        if (out_conf_q15 != NULL) {
            *out_conf_q15 = 0;
        }
        return;
    }

    nc_plastic_ring_encode_r(raw_mag_bam, r_q15);
    nc_plastic_forward_drive(pr, r_q15, NULL, &c1_q15, &s1_q15);
    amp_sq = (uint64_t)((int64_t)c1_q15 * (int64_t)c1_q15) + (uint64_t)((int64_t)s1_q15 * (int64_t)s1_q15);
    amp_q15 = nc_isqrt64(amp_sq);

    if (amp_q15 <= 4u) {
        if (out_corrected_bam != NULL) {
            *out_corrected_bam = raw_mag_bam;
        }
        if (out_conf_q15 != NULL) {
            *out_conf_q15 = 0;
        }
        return;
    }

    if (out_corrected_bam != NULL) {
        *out_corrected_bam = nc_bam_sub(nc_atan2_bam(s1_q15, c1_q15), pr->map_offset_bam);
    }
    if (out_conf_q15 != NULL) {
        *out_conf_q15 = (amp_q15 > (uint32_t)NC_Q15_ONE) ? (int16_t)NC_Q15_ONE : (int16_t)amp_q15;
    }
}

nc_bam_t nc_plastic_ring_heading_bam(const nc_plastic_ring_t *pr) {
    if (pr == NULL) {
        return 0;
    }
    return nc_compass_heading_bam(&pr->comp);
}

nc_bam_t nc_plastic_ring_update(
    nc_plastic_ring_t *pr,
    int32_t omega_bam_s,
    uint32_t dt_ms,
    nc_bam_t raw_mag_bam,
    nc_bam_t ref_bam,
    uint8_t ref_valid,
    uint8_t learn
) {
    nc_bam_t heading_bam;
    if (pr == NULL) {
        return 0;
    }

    if (ref_valid && !pr->has_abs_ref) {
        pr->has_abs_ref = 1u;
        pr->map_offset_bam = 0;
    }

    if (!pr->comp.initialized) {
        nc_compass_reset(&pr->comp, ref_valid ? ref_bam : raw_mag_bam);
        heading_bam = nc_compass_heading_bam(&pr->comp);
    } else {
        nc_bam_t corr_bam = raw_mag_bam;
        int16_t map_conf_q15 = 0;
        int16_t eff_mag_conf_q15 = 0;
        int64_t net_r, d_sub;
        uint32_t pred_sub;
        nc_bam_t true_pred_bam;
        int32_t bias_before;

        nc_plastic_ring_map(pr, raw_mag_bam, &corr_bam, &map_conf_q15);
        if (!ref_valid && pr->anchor_scale_q15 > 0) {
            eff_mag_conf_q15 = (int16_t)(((int32_t)map_conf_q15 * (int32_t)pr->anchor_scale_q15) / NC_Q15_ONE);
        }

        net_r = (int64_t)omega_bam_s - (int64_t)pr->comp.pen_bias_bam_s;
        d_sub = (net_r * (int64_t)dt_ms * 256LL) / 1000LL;
        pred_sub = ((uint32_t)pr->comp.phase_sub_bam + (uint32_t)((int32_t)d_sub)) & 0x00FFFFFFu;
        true_pred_bam = (nc_bam_t)((uint16_t)(((pred_sub + 128u) >> 8) & 0xFFFFu));
        bias_before = pr->comp.pen_bias_bam_s;

        heading_bam = nc_compass_update(
            &pr->comp,
            omega_bam_s,
            dt_ms,
            corr_bam,
            (eff_mag_conf_q15 > 0) ? 1u : 0u,
            eff_mag_conf_q15,
            ref_bam,
            ref_valid ? 1u : 0u,
            ref_valid ? (int16_t)NC_Q15_ONE : (int16_t)0
        );

        /* Sub-BAM/s bias remainder accumulator so gentle self-supervised pulls adapt gyro bias cleanly */
        if (!ref_valid && eff_mag_conf_q15 > 0 && dt_ms > 0u && pr->comp.pen_bias_bam_s == bias_before) {
            int64_t gate = (int64_t)pr->comp.last_mag_gate_q15;
            if (gate > 0) {
                nc_bam_t err_m = nc_bam_sub(corr_bam, true_pred_bam);
                int64_t w_err = nc_div_pow2_round((int64_t)err_m * gate * (int64_t)eff_mag_conf_q15, 30u);
                int64_t num = -(w_err * (int64_t)pr->comp.bias_rate_q8 * (int64_t)dt_ms);
                pr->bias_rem_accum += (int32_t)num;
                if (pr->bias_rem_accum >= 256000 || pr->bias_rem_accum <= -256000) {
                    int32_t step_b = pr->bias_rem_accum / 256000;
                    pr->bias_rem_accum -= step_b * 256000;
                    pr->comp.pen_bias_bam_s += step_b;
                    if (pr->comp.pen_bias_bam_s > 3640) {
                        pr->comp.pen_bias_bam_s = 3640;
                    } else if (pr->comp.pen_bias_bam_s < -3640) {
                        pr->comp.pen_bias_bam_s = -3640;
                    }
                }
            }
        }
    }

    if (learn && dt_ms > 0u) {
        int16_t r_q15[NC_NUM_R_NEURONS];
        int32_t pred_q12[NC_NUM_WEDGES];
        uint64_t abs_omega = (omega_bam_s < 0) ? (uint64_t)(-(int64_t)omega_bam_s) : (uint64_t)omega_bam_s;
        uint64_t d_turn_bam = (abs_omega * (uint64_t)dt_ms) / 1000ULL;
        uint64_t d_raw_bam, min_d_bam, eff_d_bam, step_rate_q15;
        uint32_t i, j;

        nc_plastic_ring_encode_r(raw_mag_bam, r_q15);
        nc_plastic_forward_drive(pr, r_q15, pred_q12, NULL, NULL);

        if (!pr->has_prev_raw) {
            d_raw_bam = d_turn_bam;
            pr->has_prev_raw = 1u;
        } else {
            int32_t diff_b = (int32_t)nc_bam_sub(raw_mag_bam, pr->prev_raw_bam);
            uint64_t abs_diff = (diff_b < 0) ? (uint64_t)(-(int64_t)diff_b) : (uint64_t)diff_b;
            uint64_t cap = 2ULL * d_turn_bam;
            d_raw_bam = (abs_diff < cap) ? abs_diff : cap;
        }
        pr->prev_raw_bam = raw_mag_bam;

        /* 0.05 rad/s * dt == 522 BAM/s * dt_ms / 1000 */
        min_d_bam = (522ULL * (uint64_t)dt_ms) / 1000ULL;
        eff_d_bam = (d_raw_bam > min_d_bam) ? d_raw_bam : min_d_bam;

        /* lr * eff_d_rad in Q15: (lr_q8 / 256) * (eff_d_bam * 2*pi / 65536) * 32767 = (lr_q8 * eff_d_bam * 205881) >> 24 */
        step_rate_q15 = ((uint64_t)(pr->lr_q8 > 0 ? pr->lr_q8 : 0) * eff_d_bam * 205881ULL) >> 24;
        if (step_rate_q15 > 31129ULL) { /* 0.95 * 32767 */
            step_rate_q15 = 31129ULL;
        }

        for (i = 0; i < NC_NUM_WEDGES; ++i) {
            nc_bam_t th_i = nc_wedge_theta(i);
            int32_t target_q12 = (int32_t)nc_div_pow2_round((int64_t)nc_cos_q15(nc_bam_sub(th_i, heading_bam)), 3u);
            int64_t err_q12 = (int64_t)target_q12 - (int64_t)pred_q12[i];
            int64_t sr_err = (int64_t)step_rate_q15 * err_q12;
            for (j = 0; j < NC_NUM_R_NEURONS; ++j) {
                int64_t dw_q12 = nc_div_pow2_round(sr_err * (int64_t)r_q15[j], 30u);
                int64_t w_new = (int64_t)pr->weights_q12[i][j] + dw_q12;
                if (w_new > 32767LL) {
                    w_new = 32767LL;
                } else if (w_new < -32767LL) {
                    w_new = -32767LL;
                }
                pr->weights_q12[i][j] = (int16_t)w_new;
            }
        }

        pr->learn_steps += 1u;
        if (!pr->has_abs_ref && (pr->learn_steps % 25u) == 0u) {
            nc_plastic_recenter_zero_mean(pr);
        }
    }

    return heading_bam;
}

nc_bam_t nc_plastic_ring_update_9dof(
    nc_plastic_ring_t *pr,
    nc_tilt_t *tilt,
    const nc_imu9_t *imu,
    uint32_t dt_ms,
    nc_bam_t ref_bam,
    uint8_t ref_valid,
    uint8_t learn
) {
    int32_t yaw_rate_bam_s = 0;
    nc_bam_t raw_mag_bam;
    if (pr == NULL || imu == NULL) {
        return 0;
    }
    raw_mag_bam = nc_tilt_update_9dof(
        tilt,
        imu->gx_bam_s, imu->gy_bam_s, imu->gz_bam_s,
        imu->ax_mg, imu->ay_mg, imu->az_mg,
        imu->mx, imu->my, imu->mz,
        dt_ms,
        &yaw_rate_bam_s
    );
    return nc_plastic_ring_update(pr, yaw_rate_bam_s, dt_ms, raw_mag_bam, ref_bam, ref_valid, learn);
}

/* =========================================================================
 * 16-Column Fan-Shaped Body CPU4 Path Integrator & PFL3 / PFL2 Steering
 * ========================================================================= */

void nc_navigator_init(nc_navigator_t *nav, int32_t arrival_radius_mm) {
    if (nav == NULL) {
        return;
    }
    nc_navigator_reset(nav);
    nav->arrival_radius_mm = (arrival_radius_mm > 50) ? arrival_radius_mm : 1500;
    nav->last_heading_bam = 0;
    nav->steering_gain_q8 = 563; /* ~2.20 in Q8 */
}

void nc_navigator_reset(nc_navigator_t *nav) {
    uint32_t i;
    if (nav == NULL) {
        return;
    }
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nav->cpu4_pos_q15mm[i] = 0;
        nav->cpu4_neg_q15mm[i] = 0;
    }
}

void nc_navigator_update(
    nc_navigator_t *nav,
    int32_t speed_mm_s,
    uint32_t dt_ms,
    nc_bam_t heading_bam
) {
    int64_t ds_mm;
    uint32_t i;
    if (nav == NULL) {
        return;
    }
    nav->last_heading_bam = heading_bam;
    if (dt_ms == 0u || speed_mm_s == 0) {
        return;
    }

    ds_mm = ((int64_t)speed_mm_s * (int64_t)dt_ms) / 1000LL;
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int32_t proj_q15 = (int32_t)nc_cos_q15(nc_bam_sub(th, heading_bam));
        int64_t pos_d = (proj_q15 > 0) ? proj_q15 : 0;
        int64_t neg_d = (proj_q15 < 0) ? (-proj_q15) : 0;
        int64_t common;

        if (ds_mm >= 0) {
            nav->cpu4_pos_q15mm[i] += ds_mm * pos_d;
            nav->cpu4_neg_q15mm[i] += ds_mm * neg_d;
        } else {
            nav->cpu4_pos_q15mm[i] += (-ds_mm) * neg_d;
            nav->cpu4_neg_q15mm[i] += (-ds_mm) * pos_d;
        }
        common = (nav->cpu4_pos_q15mm[i] < nav->cpu4_neg_q15mm[i])
                     ? nav->cpu4_pos_q15mm[i]
                     : nav->cpu4_neg_q15mm[i];
        nav->cpu4_pos_q15mm[i] -= common;
        nav->cpu4_neg_q15mm[i] -= common;
    }
}

void nc_navigator_displacement_mm(
    const nc_navigator_t *nav,
    int32_t *out_x_mm,
    int32_t *out_y_mm
) {
    int64_t sum_x = 0;
    int64_t sum_y = 0;
    uint32_t i;
    if (nav == NULL) {
        if (out_x_mm) *out_x_mm = 0;
        if (out_y_mm) *out_y_mm = 0;
        return;
    }
    /* (2 / 16) * sum(net_q15mm * cos_q15) >> 30 == (sum >> 33) */
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int64_t net = nav->cpu4_pos_q15mm[i] - nav->cpu4_neg_q15mm[i];
        int64_t net_q8 = nc_div_pow2_trunc(net, 7u);
        sum_x += net_q8 * (int64_t)nc_cos_q15(th);
        sum_y += net_q8 * (int64_t)nc_sin_q15(th);
    }
    if (out_x_mm) {
        *out_x_mm = (int32_t)nc_div_pow2_round(sum_x, 26u);
    }
    if (out_y_mm) {
        *out_y_mm = (int32_t)nc_div_pow2_round(sum_y, 26u);
    }
}

void nc_navigator_home_vector(
    const nc_navigator_t *nav,
    uint32_t *out_dist_mm,
    nc_bam_t *out_home_bearing_bam
) {
    int32_t x_mm = 0;
    int32_t y_mm = 0;
    uint64_t d2;
    uint32_t dist;

    nc_navigator_displacement_mm(nav, &x_mm, &y_mm);
    d2 = (uint64_t)((int64_t)x_mm * (int64_t)x_mm) + (uint64_t)((int64_t)y_mm * (int64_t)y_mm);
    dist = nc_isqrt64(d2);

    if (out_dist_mm) {
        *out_dist_mm = dist;
    }
    if (out_home_bearing_bam) {
        *out_home_bearing_bam = (dist == 0u) ? 0 : nc_atan2_bam((int32_t)(-(int64_t)y_mm), (int32_t)(-(int64_t)x_mm));
    }
}

int32_t nc_navigator_steering_cmd(
    const nc_navigator_t *nav,
    nc_bam_t current_heading_bam,
    int32_t max_turn_bam_s
) {
    uint32_t dist_mm = 0;
    nc_bam_t home_bam = 0;
    int64_t pfl3_diff = 0;
    int64_t pfl2_sum = 0;
    int64_t asym_q15, boost_q15, boost_sq_q15, pfl2_drive_q15, turn_sign, total_asym_q15, drive_q15, cmd;
    uint32_t i;

    if (nav == NULL || max_turn_bam_s <= 0) {
        return 0;
    }
    nc_navigator_home_vector(nav, &dist_mm, &home_bam);
    if (dist_mm < 5u) {
        return 0;
    }

    /*
     * BANC v888 Fan-Shaped Body motor readout:
     *   - 25 PFL3 bilateral steering neurons (+/-67.5 deg == +/-12288 BAM phase shift -> DNa02)
     *   - 12 PFL2 anti-goal 180-deg turn-boost neurons (32768 BAM shift -> (1 - cos err)/8 drive)
     */
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int32_t hdelta_q15 = (int32_t)nc_cos_q15(nc_bam_sub(th, home_bam));
        int32_t epg_l = (int32_t)nc_cos_q15(nc_bam_sub(th, nc_bam_sub(current_heading_bam, 12288)));
        int32_t epg_r = (int32_t)nc_cos_q15(nc_bam_sub(th, nc_bam_add(current_heading_bam, 12288)));
        int32_t epg_a = (int32_t)nc_cos_q15(nc_bam_sub(th, nc_bam_add(current_heading_bam, (nc_bam_t)-32768)));

        int32_t sum_l = (hdelta_q15 + epg_l) / 2;
        int32_t sum_r = (hdelta_q15 + epg_r) / 2;
        int32_t sum_a = (hdelta_q15 + epg_a) / 2;

        int64_t act_l = (sum_l > 0) ? (((int64_t)sum_l * (int64_t)sum_l) >> 15) : 0;
        int64_t act_r = (sum_r > 0) ? (((int64_t)sum_r * (int64_t)sum_r) >> 15) : 0;
        int64_t act_a = (sum_a > 0) ? (((int64_t)sum_a * (int64_t)sum_a) >> 15) : 0;

        pfl3_diff += (act_r - act_l);
        pfl2_sum += act_a;
    }

    /* Normalize PFL3 across 8*sin(67.5 deg) ~= 7.39 -> 1 / 7.39 ~= 111 / 820 (asym_q15 = 0.5*sin(err)) */
    asym_q15 = (pfl3_diff * 111LL) / 820LL;
    /* Mean PFL2 anti-goal activity in Q15: (1 - cos(err)) / 8, peaking at 8192 (0.25) at 180 deg */
    boost_q15 = pfl2_sum / NC_NUM_WEDGES;

    /* Anti-goal hemisphere turn boost: pfl2_boost_gain (2.5) * 4 * anti_drive^2
     * = 0 at 0 deg, smooth near 90 deg, and +0.625 (20480 in Q15) across 179-180 deg! */
    boost_sq_q15 = (4LL * boost_q15 * boost_q15) >> 15;
    pfl2_drive_q15 = (boost_sq_q15 * 5LL) >> 1;

    if (asym_q15 > 4) {
        turn_sign = 1LL;
    } else if (asym_q15 < -4) {
        turn_sign = -1LL;
    } else {
        nc_bam_t err = nc_bam_sub(home_bam, current_heading_bam);
        turn_sign = (err >= 0 || err == (nc_bam_t)-32768) ? 1LL : -1LL;
    }

    total_asym_q15 = asym_q15 + turn_sign * pfl2_drive_q15;
    drive_q15 = nc_div_pow2_round(total_asym_q15 * (int64_t)nav->steering_gain_q8, 8u);

    /* C1-smooth monotone Padé rational tanh(x) ~= x*(27 + x^2)/(27 + 9*x^2) for |x| < 3, +/-1 for |x| >= 3 */
    {
        int64_t three_q15 = 3LL * (int64_t)NC_Q15_ONE;
        int64_t sat_q15;
        if (drive_q15 >= three_q15) {
            sat_q15 = NC_Q15_ONE;
        } else if (drive_q15 <= -three_q15) {
            sat_q15 = -NC_Q15_ONE;
        } else {
            int64_t x2_q15 = (drive_q15 * drive_q15) / (int64_t)NC_Q15_ONE;
            int64_t num = drive_q15 * (27LL * (int64_t)NC_Q15_ONE + x2_q15);
            int64_t den = 27LL * (int64_t)NC_Q15_ONE + 9LL * x2_q15;
            sat_q15 = num / den;
        }
        cmd = nc_div_pow2_round(sat_q15 * (int64_t)max_turn_bam_s, 15u);
    }

    if ((int32_t)dist_mm < nav->arrival_radius_mm) {
        cmd = (cmd * (int64_t)dist_mm) / (int64_t)nav->arrival_radius_mm;
    }
    if (cmd > max_turn_bam_s) {
        cmd = max_turn_bam_s;
    } else if (cmd < -max_turn_bam_s) {
        cmd = -max_turn_bam_s;
    }
    return (int32_t)cmd;
}
