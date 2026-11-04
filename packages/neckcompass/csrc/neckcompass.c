/*
 * NeckCompass ANSI C99 Embedded Core Implementation
 * Zero heap allocation, zero FPU instructions, pure Q15 / int32_t / int64_t arithmetic.
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

static nc_bam_t nc_wedge_theta(uint32_t idx) {
    uint16_t u = (uint16_t)(32768u + (idx & 15u) * 4096u);
    return (nc_bam_t)u;
}

nc_bam_t nc_bam_add(nc_bam_t a, nc_bam_t b) {
    return (nc_bam_t)((uint16_t)a + (uint16_t)b);
}

nc_bam_t nc_bam_sub(nc_bam_t a, nc_bam_t b) {
    return (nc_bam_t)((uint16_t)a - (uint16_t)b);
}

nc_bam_t nc_cdeg_to_bam(int32_t centideg) {
    /* 36000 centidegrees == 65536 BAM counts */
    int64_t scaled = (int64_t)centideg * 65536LL;
    int64_t q = (scaled >= 0) ? ((scaled + 18000LL) / 36000LL)
                              : ((scaled - 18000LL) / 36000LL);
    return (nc_bam_t)((uint16_t)((int32_t)q));
}

int32_t nc_bam_to_cdeg(nc_bam_t bam) {
    int64_t scaled = (int64_t)bam * 36000LL;
    if (scaled >= 0) {
        return (int32_t)((scaled + 32768LL) >> 16);
    }
    return -(int32_t)(((-scaled) + 32768LL) >> 16);
}

nc_bam_t nc_mrad_to_bam(int32_t mrad) {
    /* 2 * pi * 1000 ~= 6283.1853 mrad -> 6283185 micro-rad == 65536 BAM */
    int64_t scaled = (int64_t)mrad * 65536000LL;
    int64_t q = (scaled >= 0) ? ((scaled + 3141593LL) / 6283185LL)
                              : ((scaled - 3141593LL) / 6283185LL);
    return (nc_bam_t)((uint16_t)((int32_t)q));
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
    int32_t interp = v0 + (((v1 - v0) * (int32_t)frac + 128) >> 8);
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
        int32_t r2_q15 = (r_q15 * r_q15) >> 15;
        int32_t one_minus = NC_Q15_ONE - r2_q15;
        return (int16_t)((one_minus * one_minus) >> 15);
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
        int32_t h2_q15 = (h_q15 * h_q15) >> 15;
        int32_t h3_q15 = (h2_q15 * h_q15) >> 15;
        /* Match 1st-harmonic norm of h^3 (3*N/16 = 3) to max(0,cos) (N/4 = 4) -> scale 4/3 */
        int32_t prof_q15 = (h3_q15 * 4) / 3;
        u_ring_q15[i] += (int32_t)(((int64_t)weight_q15 * (int64_t)prof_q15) >> 15);
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
    comp->phase_sub_bam = ((int32_t)(uint16_t)heading_bam) << 8;
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
        comp->offbasin_c_q15 = (int16_t)(comp->offbasin_c_q15 + ((a_q15 * (c - comp->offbasin_c_q15)) >> 15));
        comp->offbasin_s_q15 = (int16_t)(comp->offbasin_s_q15 + ((a_q15 * (s - comp->offbasin_s_q15)) >> 15));
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
    int32_t sum_c = 0;
    int32_t sum_s = 0;
    uint32_t i;
    if (comp == NULL || !comp->initialized) {
        return 0;
    }
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int32_t r = comp->wedges_q15[i];
        sum_c += (r * (int32_t)nc_cos_q15(th)) >> 8;
        sum_s += (r * (int32_t)nc_sin_q15(th)) >> 8;
    }
    return nc_atan2_bam(sum_s, sum_c);
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
    int64_t net_rate_bam_s, d_sub_q8;
    nc_bam_t pred_bam, new_bam;
    int32_t m_conf, s_conf, cue_applied = 0;
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

    /* 1. P-EN asymmetric velocity integration on Q8 sub-BAM phase + 16-wedge E-PG ring */
    net_rate_bam_s = (int64_t)omega_bam_s - (int64_t)comp->pen_bias_bam_s;
    d_sub_q8 = (net_rate_bam_s * (int64_t)dt_ms * 256LL) / 1000LL;
    comp->phase_sub_bam = (int32_t)(((uint32_t)comp->phase_sub_bam + (uint32_t)d_sub_q8) & 0x00FFFFFFu);
    pred_bam = (nc_bam_t)((uint16_t)((comp->phase_sub_bam + 128) >> 8));

    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int16_t c = nc_cos_q15(nc_bam_sub(th, pred_bam));
        u_ring[i] = (c > 0) ? (int32_t)c : 0;
    }

    /* 2. R-neuron multi-cue injection gated by active E-PG attractor basin */
    comp->last_mag_gate_q15 = 0;
    if (sun_valid && s_conf > 0 && dt_ms > 0u) {
        int16_t gate = nc_basin_gate_q15(comp, sun_bam, pred_bam);
        int64_t w_q15 = ((int64_t)comp->sun_gain_q8 * (int64_t)s_conf * (int64_t)gate * (int64_t)dt_ms)
                        / (256LL * 32767LL * 1000LL);
        if (w_q15 > 0) {
            nc_inject_cue_ring(u_ring, sun_bam, (int32_t)w_q15);
            weighted_err_bam += ((int64_t)nc_bam_sub(sun_bam, pred_bam) * (int64_t)gate * (int64_t)s_conf) >> 30;
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
            nc_inject_cue_ring(u_ring, mag_bam, (int32_t)w_q15);
            weighted_err_bam += ((int64_t)nc_bam_sub(mag_bam, pred_bam) * (int64_t)gate * (int64_t)m_conf) >> 30;
            cue_applied = 1;
        }
    }

    /* 3. Delta7 global inhibition + population vector readout across the 16 wedges */
    if (cue_applied) {
        int64_t sum_c = 0;
        int64_t sum_s = 0;
        for (i = 0; i < NC_NUM_WEDGES; ++i) {
            nc_bam_t th = nc_wedge_theta(i);
            sum_c += (int64_t)u_ring[i] * (int64_t)nc_cos_q15(th);
            sum_s += (int64_t)u_ring[i] * (int64_t)nc_sin_q15(th);
        }
        new_bam = nc_atan2_bam((int32_t)(sum_s >> 14), (int32_t)(sum_c >> 14));
        comp->phase_sub_bam = ((int32_t)(uint16_t)new_bam) << 8;

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
    /* (2 / 16) * sum(net_q15mm * cos_q15) >> 30 == (sum >> 33), since Q15 * Q15 = Q30 and 2/16 = 1/8 */
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int64_t net = nav->cpu4_pos_q15mm[i] - nav->cpu4_neg_q15mm[i];
        sum_x += (net >> 7) * (int64_t)nc_cos_q15(th);
        sum_y += (net >> 7) * (int64_t)nc_sin_q15(th);
    }
    /* net>>7 is Q8*mm; times Q15 = Q23*mm; times (2/16 = 1/8) = >> 26 */
    if (out_x_mm) {
        *out_x_mm = (int32_t)(sum_x >> 26);
    }
    if (out_y_mm) {
        *out_y_mm = (int32_t)(sum_y >> 26);
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
        *out_home_bearing_bam = (dist == 0u) ? 0 : nc_atan2_bam(-y_mm, -x_mm);
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
    int64_t asym_q15, boost_q15, drive_q15, cmd;
    uint32_t i;

    if (nav == NULL || max_turn_bam_s <= 0) {
        return 0;
    }
    nc_navigator_home_vector(nav, &dist_mm, &home_bam);
    if (dist_mm < 5u) {
        return 0;
    }

    /* 16-column Fan-Shaped Body PFL3 (+/-67.5 deg = +/-12288 BAM) + PFL2 (180 deg = 32768 BAM) */
    for (i = 0; i < NC_NUM_WEDGES; ++i) {
        nc_bam_t th = nc_wedge_theta(i);
        int32_t hdelta_q15 = (int32_t)nc_cos_q15(nc_bam_sub(th, home_bam));
        int32_t epg_l = (int32_t)nc_cos_q15(nc_bam_sub(th, nc_bam_sub(current_heading_bam, 12288)));
        int32_t epg_r = (int32_t)nc_cos_q15(nc_bam_sub(th, nc_bam_add(current_heading_bam, 12288)));
        int32_t epg_a = (int32_t)nc_cos_q15(nc_bam_sub(th, nc_bam_add(current_heading_bam, (nc_bam_t)-32768)));

        int32_t sum_l = (hdelta_q15 + epg_l) >> 1;
        int32_t sum_r = (hdelta_q15 + epg_r) >> 1;
        int32_t sum_a = (hdelta_q15 + epg_a) >> 1;

        int64_t act_l = (sum_l > 0) ? (((int64_t)sum_l * (int64_t)sum_l) >> 15) : 0;
        int64_t act_r = (sum_r > 0) ? (((int64_t)sum_r * (int64_t)sum_r) >> 15) : 0;
        int64_t act_a = (sum_a > 0) ? (((int64_t)sum_a * (int64_t)sum_a) >> 15) : 0;

        pfl3_diff += (act_r - act_l);
        pfl2_sum += act_a;
    }

    /* Normalize across 8*sin(67.5 deg) ~= 7.39 -> scale by 1 / 7.39 ~= 111 / 820 */
    asym_q15 = (pfl3_diff * 111LL) / 820LL;
    boost_q15 = pfl2_sum / NC_NUM_WEDGES;

    if (asym_q15 > -64 && asym_q15 < 64 && boost_q15 > 11000) {
        nc_bam_t err = nc_bam_sub(home_bam, current_heading_bam);
        asym_q15 = (err >= 0) ? 8192 : -8192;
    }

    drive_q15 = (asym_q15 * (NC_Q15_ONE + ((boost_q15 * 5LL) >> 1))) >> 15;
    drive_q15 = (drive_q15 * (int64_t)nav->steering_gain_q8) >> 8;

    /* Smooth saturating nonlinearity x / (1 + |x|) in Q15 */
    {
        int64_t abs_d = (drive_q15 < 0) ? (-drive_q15) : drive_q15;
        int64_t sat_q15 = (drive_q15 * (int64_t)NC_Q15_ONE) / (NC_Q15_ONE + abs_d);
        cmd = (sat_q15 * (int64_t)max_turn_bam_s) >> 15;
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
