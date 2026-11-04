/*
 * NeckCord v1.1.0 - Zero-Heap, Pure Fixed-Point ANSI C99 Implementation
 *
 * Copyright (c) 2026 Amandeep Singh Bains. MIT License.
 */

#include "neckcord.h"
#include <string.h>

/* Quarter-wave Q15 sine lookup table: sin(i * pi / 128) * 32767 for i = 0..64 */
static const int16_t k_sin_q15_lut[65] = {
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

/* Nominal neutral hexapod footholds in millimeters: L1, R1, L2, R2, L3, R3 */
static const int16_t k_nom_foot_x_mm[NC_NUM_LIMBS] = { +200, +200,    0,    0, -200, -200 };
static const int16_t k_nom_foot_y_mm[NC_NUM_LIMBS] = { +130, -130, +160, -160, +130, -130 };

/* Canonical target phase offsets in 16-bit BAM (65536 = 2*pi, L1 anchored at 0) */
static const uint16_t k_offsets_tripod_bam[NC_NUM_LIMBS] = {
    0u, 32768u, 32768u, 0u, 0u, 32768u
};
static const uint16_t k_offsets_tetrapod_bam[NC_NUM_LIMBS] = {
    0u, 21845u, 21845u, 43691u, 43691u, 0u
};
static const uint16_t k_offsets_wave_bam[NC_NUM_LIMBS] = {
    0u, 32768u, 10923u, 43691u, 21845u, 54613u
};

static int32_t nc_clamp_i32(int32_t v, int32_t lo, int32_t hi)
{
    if (v < lo) {
        return lo;
    }
    if (v > hi) {
        return hi;
    }
    return v;
}

static int32_t nc_abs_i32(int32_t v)
{
    return (v < 0) ? -v : v;
}

int16_t nc_sin_bam_q15(uint16_t angle_bam)
{
    uint16_t quad = (uint16_t)(angle_bam >> 14);
    uint16_t rem14 = (uint16_t)(angle_bam & 0x3FFFu);
    uint16_t idx;
    uint16_t frac8;
    int32_t y0;
    int32_t y1;
    int32_t interp;

    if ((quad & 1u) != 0u) {
        rem14 = (uint16_t)(0x4000u - rem14);
    }

    idx = (uint16_t)(rem14 >> 8);
    frac8 = (uint16_t)(rem14 & 0xFFu);

    if (idx >= 64u) {
        interp = k_sin_q15_lut[64];
    } else {
        y0 = k_sin_q15_lut[idx];
        y1 = k_sin_q15_lut[idx + 1u];
        interp = y0 + (((y1 - y0) * (int32_t)frac8 + 128) >> 8);
    }

    if ((quad & 2u) != 0u) {
        interp = -interp;
    }
    return (int16_t)interp;
}

int16_t nc_cos_bam_q15(uint16_t angle_bam)
{
    return nc_sin_bam_q15((uint16_t)(angle_bam + 16384u));
}

/* =========================================================================
 * 1. 4-Byte Descending Cervical Command Frame Codec
 * ========================================================================= */

uint8_t nc_crc4_upper28(uint32_t upper28)
{
    uint8_t crc = 0x9u;
    uint32_t val = upper28 & 0x0FFFFFFFu;
    int shift;

    for (shift = 27; shift >= 0; --shift) {
        uint8_t fb = (uint8_t)(((val >> shift) ^ (uint32_t)(crc >> 3)) & 0x1u);
        crc = (uint8_t)(((crc << 1) & 0xFu) ^ (fb ? 0x3u : 0x0u));
    }
    return (uint8_t)((crc ^ 0x5u) & 0xFu);
}

static int32_t nc_q15_to_sym(int16_t val_q15, int32_t max_int)
{
    int32_t v = nc_clamp_i32((int32_t)val_q15, -NC_Q15_ONE, NC_Q15_ONE);
    if (v >= 0) {
        return (v * max_int + (NC_Q15_ONE / 2)) / NC_Q15_ONE;
    }
    return -(((-v) * max_int + (NC_Q15_ONE / 2)) / NC_Q15_ONE);
}

static int16_t nc_sym_to_q15(int32_t q_val, int32_t max_int)
{
    int32_t res;
    if (q_val >= 0) {
        res = (q_val * NC_Q15_ONE + (max_int / 2)) / max_int;
    } else {
        res = -(((-q_val) * NC_Q15_ONE + (max_int / 2)) / max_int);
    }
    return (int16_t)nc_clamp_i32(res, -NC_Q15_ONE, NC_Q15_ONE);
}

int nc_encode_descending_4b(const nc_descending_frame_t *frame, uint8_t out_raw[4])
{
    int32_t q_fwd;
    int32_t q_yaw;
    int32_t q_lat;
    uint32_t u_fwd;
    uint32_t u_yaw;
    uint32_t u_lat;
    uint32_t upper28;
    uint32_t word32;
    uint8_t crc;

    if (frame == NULL || out_raw == NULL) {
        return NC_ERR_ARG;
    }
    if (frame->gait_mode > 3u) {
        return NC_ERR_RANGE;
    }

    q_fwd = nc_q15_to_sym(frame->drive_fwd_q15, 127);
    q_yaw = nc_q15_to_sym(frame->turn_yaw_q15, 127);
    q_lat = nc_q15_to_sym(frame->strafe_lat_q15, 63);

    u_fwd = (uint32_t)(q_fwd & 0xFF);
    u_yaw = (uint32_t)(q_yaw & 0xFF);
    u_lat = (uint32_t)(q_lat & 0x7F);

    upper28 = (u_fwd << 20)
            | (u_yaw << 12)
            | (u_lat << 5)
            | ((uint32_t)(frame->gait_mode & 0x3u) << 3)
            | ((uint32_t)(frame->freeze_reflex ? 1u : 0u) << 2)
            | ((uint32_t)(frame->seq & 0x3u));

    crc = nc_crc4_upper28(upper28);
    word32 = (upper28 << 4) | (uint32_t)crc;

    out_raw[0] = (uint8_t)((word32 >> 24) & 0xFFu);
    out_raw[1] = (uint8_t)((word32 >> 16) & 0xFFu);
    out_raw[2] = (uint8_t)((word32 >> 8) & 0xFFu);
    out_raw[3] = (uint8_t)(word32 & 0xFFu);
    return NC_OK;
}

int nc_decode_descending_4b(
    const uint8_t in_raw[4],
    int expected_seq,
    nc_descending_frame_t *out_frame
)
{
    uint32_t word32;
    uint32_t upper28;
    uint8_t rx_crc;
    uint8_t calc_crc;
    int32_t u_fwd;
    int32_t u_yaw;
    int32_t u_lat;
    int32_t q_fwd;
    int32_t q_yaw;
    int32_t q_lat;
    uint8_t seq;

    if (in_raw == NULL || out_frame == NULL) {
        return NC_ERR_ARG;
    }

    word32 = ((uint32_t)in_raw[0] << 24)
           | ((uint32_t)in_raw[1] << 16)
           | ((uint32_t)in_raw[2] << 8)
           | (uint32_t)in_raw[3];

    upper28 = (word32 >> 4) & 0x0FFFFFFFu;
    rx_crc = (uint8_t)(word32 & 0xFu);
    calc_crc = nc_crc4_upper28(upper28);
    if (rx_crc != calc_crc) {
        return NC_ERR_CRC;
    }

    u_fwd = (int32_t)((upper28 >> 20) & 0xFFu);
    u_yaw = (int32_t)((upper28 >> 12) & 0xFFu);
    u_lat = (int32_t)((upper28 >> 5) & 0x7Fu);

    q_fwd = (u_fwd >= 128) ? (u_fwd - 256) : u_fwd;
    q_yaw = (u_yaw >= 128) ? (u_yaw - 256) : u_yaw;
    q_lat = (u_lat >= 64) ? (u_lat - 128) : u_lat;

    if (q_fwd == -128 || q_yaw == -128 || q_lat == -64) {
        return NC_ERR_RANGE;
    }

    seq = (uint8_t)(upper28 & 0x3u);
    if (expected_seq >= 0 && seq != (uint8_t)(expected_seq & 0x3)) {
        return NC_ERR_SEQ;
    }

    out_frame->drive_fwd_q15 = nc_sym_to_q15(q_fwd, 127);
    out_frame->turn_yaw_q15 = nc_sym_to_q15(q_yaw, 127);
    out_frame->strafe_lat_q15 = nc_sym_to_q15(q_lat, 63);
    out_frame->gait_mode = (uint8_t)((upper28 >> 3) & 0x3u);
    out_frame->freeze_reflex = (uint8_t)((upper28 >> 2) & 0x1u);
    out_frame->seq = seq;
    return NC_OK;
}

/* =========================================================================
 * 2. 6-Limb Segmental VNC CPG Cord
 * ========================================================================= */

void nc_cpg_init(nc_cpg_cord_t *cord, uint8_t adapt_to_damage)
{
    uint8_t i;
    if (cord == NULL) {
        return;
    }
    memset(cord, 0, sizeof(*cord));
    for (i = 0u; i < NC_NUM_LIMBS; ++i) {
        cord->phases_bam[i] = k_offsets_tripod_bam[i];
        cord->limb_health_q15[i] = NC_Q15_ONE;
    }
    cord->base_freq_milli_hz = 800u;
    cord->max_freq_milli_hz = 2800u;
    cord->stride_mm = 80;
    cord->fault_thresh_q15 = 11468; /* ~0.35 in Q15 */
    cord->adapt_to_damage = adapt_to_damage ? 1u : 0u;
}

int nc_cpg_set_limb_health(nc_cpg_cord_t *cord, uint8_t limb_idx, int16_t health_q15)
{
    if (cord == NULL || limb_idx >= NC_NUM_LIMBS) {
        return NC_ERR_ARG;
    }
    cord->limb_health_q15[limb_idx] = (int16_t)nc_clamp_i32((int32_t)health_q15, 0, NC_Q15_ONE);
    return NC_OK;
}

static int64_t nc_cross_2d(int32_t ox, int32_t oy, int32_t ax, int32_t ay, int32_t bx, int32_t by)
{
    return (int64_t)(ax - ox) * (int64_t)(by - oy) - (int64_t)(ay - oy) * (int64_t)(bx - ox);
}

uint8_t nc_check_com_enclosed_mm(
    const int16_t foot_x_mm[NC_NUM_LIMBS],
    const int16_t foot_y_mm[NC_NUM_LIMBS],
    uint8_t stance_mask,
    int16_t min_margin_mm
)
{
    int32_t px[NC_NUM_LIMBS];
    int32_t py[NC_NUM_LIMBS];
    int32_t hx[2 * NC_NUM_LIMBS];
    int32_t hy[2 * NC_NUM_LIMBS];
    int n = 0;
    int i;
    int j;
    int k = 0;
    int lower_len;
    int hull_n;
    int64_t min_sq = (int64_t)min_margin_mm * (int64_t)min_margin_mm;

    if (foot_x_mm == NULL || foot_y_mm == NULL) {
        return 0u;
    }

    for (i = 0; i < NC_NUM_LIMBS; ++i) {
        if ((stance_mask & (uint8_t)(1u << i)) != 0u) {
            px[n] = foot_x_mm[i];
            py[n] = foot_y_mm[i];
            ++n;
        }
    }
    if (n < 3) {
        return 0u;
    }

    /* Insertion sort grounded points lexicographically by (x, y) */
    for (i = 1; i < n; ++i) {
        int32_t tx = px[i];
        int32_t ty = py[i];
        j = i - 1;
        while (j >= 0 && (px[j] > tx || (px[j] == tx && py[j] > ty))) {
            px[j + 1] = px[j];
            py[j + 1] = py[j];
            --j;
        }
        px[j + 1] = tx;
        py[j + 1] = ty;
    }

    /* Monotone chain CCW convex hull */
    for (i = 0; i < n; ++i) {
        while (k >= 2 && nc_cross_2d(hx[k - 2], hy[k - 2], hx[k - 1], hy[k - 1], px[i], py[i]) <= 0) {
            --k;
        }
        hx[k] = px[i];
        hy[k] = py[i];
        ++k;
    }
    lower_len = k + 1;
    for (i = n - 2; i >= 0; --i) {
        while (k >= lower_len && nc_cross_2d(hx[k - 2], hy[k - 2], hx[k - 1], hy[k - 1], px[i], py[i]) <= 0) {
            --k;
        }
        hx[k] = px[i];
        hy[k] = py[i];
        ++k;
    }
    hull_n = k - 1;
    if (hull_n < 3) {
        return 0u;
    }

    for (i = 0; i < hull_n; ++i) {
        int next = (i + 1) % hull_n;
        int64_t ax = hx[i];
        int64_t ay = hy[i];
        int64_t bx = hx[next];
        int64_t by = hy[next];
        int64_t cross_origin = ax * by - ay * bx;
        int64_t dx = bx - ax;
        int64_t dy = by - ay;
        int64_t edge_len_sq = dx * dx + dy * dy;

        if (edge_len_sq <= 0 || cross_origin <= 0) {
            return 0u;
        }
        if (min_margin_mm > 0 && (cross_origin * cross_origin) <= min_sq * edge_len_sq) {
            return 0u;
        }
    }
    return 1u;
}

static void nc_compute_pentapod_bam(uint8_t damaged_idx, uint16_t out_bam[NC_NUM_LIMBS])
{
    uint8_t dmg_side = (uint8_t)(damaged_idx & 1u);
    uint8_t intact_side = (uint8_t)(1u - dmg_side);
    uint8_t dmg_surv[2];
    uint8_t ring[5];
    uint8_t s_cnt = 0u;
    uint8_t i;
    static const uint16_t k_pent_steps[5] = { 0u, 13107u, 26214u, 39322u, 52429u };

    for (i = 0u; i < 3u; ++i) {
        uint8_t cand = (uint8_t)(dmg_side + 2u * i);
        if (cand != damaged_idx && s_cnt < 2u) {
            dmg_surv[s_cnt++] = cand;
        }
    }
    ring[0] = dmg_surv[0];
    ring[1] = (uint8_t)(intact_side + 4u);
    ring[2] = (uint8_t)(intact_side + 2u);
    ring[3] = dmg_surv[1];
    ring[4] = intact_side;

    for (i = 0u; i < NC_NUM_LIMBS; ++i) {
        out_bam[i] = 0u;
    }
    for (i = 0u; i < 5u; ++i) {
        out_bam[ring[i]] = k_pent_steps[i];
    }
}

static void nc_compute_anchors_mm(
    const nc_cpg_cord_t *cord,
    int16_t anch_x[NC_NUM_LIMBS],
    int16_t anch_y[NC_NUM_LIMBS]
)
{
    uint8_t i;
    uint8_t side;
    for (i = 0u; i < NC_NUM_LIMBS; ++i) {
        anch_x[i] = k_nom_foot_x_mm[i];
        anch_y[i] = k_nom_foot_y_mm[i];
    }
    if (!cord->adapt_to_damage) {
        return;
    }

    for (side = 0u; side < 2u; ++side) {
        int32_t y_sign = (side == 0u) ? 1 : -1;
        uint8_t i_front = side;
        uint8_t i_mid = (uint8_t)(side + 2u);
        uint8_t i_hind = (uint8_t)(side + 4u);
        int32_t loss_front = NC_Q15_ONE - cord->limb_health_q15[i_front];
        int32_t loss_mid = NC_Q15_ONE - cord->limb_health_q15[i_mid];
        int32_t loss_hind = NC_Q15_ONE - cord->limb_health_q15[i_hind];

        if (loss_mid > 6553) {
            anch_x[i_front] = (int16_t)(anch_x[i_front] - ((90 * loss_mid) / NC_Q15_ONE));
            anch_x[i_hind]  = (int16_t)(anch_x[i_hind]  + ((90 * loss_mid) / NC_Q15_ONE));
            anch_y[i_front] = (int16_t)(anch_y[i_front] + ((20 * y_sign * loss_mid) / NC_Q15_ONE));
            anch_y[i_hind]  = (int16_t)(anch_y[i_hind]  + ((20 * y_sign * loss_mid) / NC_Q15_ONE));
        }
        if (loss_front > 6553) {
            anch_x[i_mid]  = (int16_t)(anch_x[i_mid]  + ((100 * loss_front) / NC_Q15_ONE));
            anch_x[i_hind] = (int16_t)(anch_x[i_hind] + ((80 * loss_front) / NC_Q15_ONE));
            anch_y[i_mid]  = (int16_t)(anch_y[i_mid]  + ((15 * y_sign * loss_front) / NC_Q15_ONE));
            anch_y[i_hind] = (int16_t)(anch_y[i_hind] + ((20 * y_sign * loss_front) / NC_Q15_ONE));
        }
        if (loss_hind > 6553) {
            anch_x[i_mid]   = (int16_t)(anch_x[i_mid]   - ((100 * loss_hind) / NC_Q15_ONE));
            anch_x[i_front] = (int16_t)(anch_x[i_front] - ((80 * loss_hind) / NC_Q15_ONE));
            anch_y[i_mid]   = (int16_t)(anch_y[i_mid]   + ((15 * y_sign * loss_hind) / NC_Q15_ONE));
            anch_y[i_front] = (int16_t)(anch_y[i_front] + ((20 * y_sign * loss_hind) / NC_Q15_ONE));
        }
    }
}

int nc_cpg_step(
    nc_cpg_cord_t *cord,
    const nc_descending_frame_t *cmd,
    uint16_t dt_ms,
    nc_cpg_state_t *out_state
)
{
    uint8_t healthy_mask = 0u;
    uint8_t n_healthy = 0u;
    uint8_t min_idx = 0u;
    int16_t min_h = NC_Q15_ONE;
    int16_t anch_x[NC_NUM_LIMBS];
    int16_t anch_y[NC_NUM_LIMBS];
    uint16_t pent_bam[NC_NUM_LIMBS];
    int32_t w_wave_q14 = 0;
    int32_t w_tetra_q14 = 0;
    int32_t w_tri_q14 = 0;
    uint8_t use_pentapod = 0u;
    uint8_t active_gait;
    uint32_t duty_bam;
    int32_t abs_fwd;
    int32_t abs_lat;
    int32_t abs_yaw;
    int32_t speed_q15;
    int32_t freq_mhz;
    int32_t dir_sign;
    uint16_t sub_steps;
    uint16_t s;
    uint8_t i;
    uint8_t j;

    if (cord == NULL || cmd == NULL || out_state == NULL) {
        return NC_ERR_ARG;
    }

    for (i = 0u; i < NC_NUM_LIMBS; ++i) {
        if (cord->limb_health_q15[i] >= cord->fault_thresh_q15) {
            healthy_mask |= (uint8_t)(1u << i);
            ++n_healthy;
        }
        if (cord->limb_health_q15[i] < min_h) {
            min_h = cord->limb_health_q15[i];
            min_idx = i;
        }
    }

    nc_compute_anchors_mm(cord, anch_x, anch_y);

    if (cmd->freeze_reflex) {
        for (i = 0u; i < NC_NUM_LIMBS; ++i) {
            out_state->phases_bam[i] = cord->phases_bam[i];
            out_state->coxa_q15[i] = 0;
            out_state->femur_lift_q15[i] = ((healthy_mask & (uint8_t)(1u << i)) != 0u) ? 0 : NC_Q15_ONE;
            out_state->foot_x_mm[i] = anch_x[i];
            out_state->foot_y_mm[i] = anch_y[i];
        }
        out_state->stance_mask = healthy_mask;
        out_state->duty_q15 = NC_Q15_ONE;
        out_state->active_gait = (uint8_t)NC_GAIT_FREEZE;
        out_state->is_statically_stable = nc_check_com_enclosed_mm(anch_x, anch_y, healthy_mask, 5);
        return NC_OK;
    }

    abs_fwd = nc_abs_i32(cmd->drive_fwd_q15);
    abs_lat = (nc_abs_i32(cmd->strafe_lat_q15) * 3) >> 2;
    abs_yaw = (nc_abs_i32(cmd->turn_yaw_q15) * 9) >> 4;
    speed_q15 = abs_fwd;
    if (abs_lat > speed_q15) {
        speed_q15 = abs_lat;
    }
    if (abs_yaw > speed_q15) {
        speed_q15 = abs_yaw;
    }
    speed_q15 = nc_clamp_i32(speed_q15, 0, NC_Q15_ONE);

    if (cord->adapt_to_damage && n_healthy == 5u) {
        use_pentapod = 1u;
        nc_compute_pentapod_bam(min_idx, pent_bam);
        duty_bam = 53740u; /* 0.82 * 65536 */
        active_gait = (uint8_t)NC_GAIT_PENTAPOD;
    } else if (cord->adapt_to_damage && n_healthy < 5u) {
        w_wave_q14 = NC_Q14_ONE;
        duty_bam = 55706u; /* 0.85 * 65536 */
        active_gait = (uint8_t)NC_GAIT_WAVE;
    } else if (cmd->gait_mode == (uint8_t)NC_GAIT_TRIPOD) {
        w_tri_q14 = NC_Q14_ONE;
        duty_bam = 35389u; /* 0.54 * 65536 */
        active_gait = (uint8_t)NC_GAIT_TRIPOD;
    } else if (cmd->gait_mode == (uint8_t)NC_GAIT_TETRAPOD) {
        w_tetra_q14 = NC_Q14_ONE;
        duty_bam = 44564u; /* 0.68 * 65536 */
        active_gait = (uint8_t)NC_GAIT_TETRAPOD;
    } else if (cmd->gait_mode == (uint8_t)NC_GAIT_WAVE) {
        w_wave_q14 = NC_Q14_ONE;
        duty_bam = 53740u; /* 0.82 * 65536 */
        active_gait = (uint8_t)NC_GAIT_WAVE;
    } else {
        /* Smooth speed-driven blending Wave -> Tetrapod -> Tripod */
        if (speed_q15 <= 9830) {
            w_wave_q14 = NC_Q14_ONE;
            active_gait = (uint8_t)NC_GAIT_WAVE;
        } else if (speed_q15 >= 24575) {
            w_tri_q14 = NC_Q14_ONE;
            active_gait = (uint8_t)NC_GAIT_TRIPOD;
        } else if (speed_q15 < 17202) {
            w_tetra_q14 = ((speed_q15 - 9830) * NC_Q14_ONE) / 7372;
            w_wave_q14 = NC_Q14_ONE - w_tetra_q14;
            active_gait = (w_tetra_q14 < 5734) ? (uint8_t)NC_GAIT_WAVE : (uint8_t)NC_GAIT_TETRAPOD;
        } else {
            w_tri_q14 = ((speed_q15 - 17202) * NC_Q14_ONE) / 7373;
            w_tetra_q14 = NC_Q14_ONE - w_tri_q14;
            active_gait = (w_tri_q14 < 10649) ? (uint8_t)NC_GAIT_TETRAPOD : (uint8_t)NC_GAIT_TRIPOD;
        }
        duty_bam = (uint32_t)(
            (w_wave_q14 * 53740 + w_tetra_q14 * 44564 + w_tri_q14 * 35389) >> 14
        );
    }

    dir_sign = (cmd->drive_fwd_q15 < -32) ? -1 : 1;
    if (speed_q15 < 32) {
        freq_mhz = 0;
    } else {
        freq_mhz = (int32_t)cord->base_freq_milli_hz
                 + (((int32_t)(cord->max_freq_milli_hz - cord->base_freq_milli_hz) * speed_q15) / NC_Q15_ONE);
    }

    sub_steps = (uint16_t)((dt_ms + 4u) / 5u);
    if (sub_steps == 0u) {
        sub_steps = 1u;
    }

    for (s = 0u; s < sub_steps; ++s) {
        uint16_t next_phases[NC_NUM_LIMBS];
        /* Base phase increment in BAM: 65536 * freq_mhz * dt_ms / (1000 * 1000 * sub_steps) */
        int32_t base_inc_bam = (int32_t)(
            ((int64_t)65536 * (int64_t)(freq_mhz * dir_sign) * (int64_t)dt_ms)
            / ((int64_t)1000000 * (int64_t)sub_steps)
        );

        for (i = 0u; i < NC_NUM_LIMBS; ++i) {
            int64_t coup_acc = 0;
            for (j = 0u; j < NC_NUM_LIMBS; ++j) {
                int32_t k_gain;
                int32_t hj_q15;
                int32_t sin_q15;
                if (i == j || (healthy_mask & (uint8_t)(1u << j)) == 0u) {
                    continue;
                }
                hj_q15 = cord->limb_health_q15[j];
                /* Topology weights matching SegmentalCPGCord (rad/s -> BAM/s factor ~10430) */
                if ((i ^ 1u) == j) {
                    k_gain = 156450; /* contralateral (~15 rad/s) */
                } else if ((i & 1u) == (j & 1u)) {
                    uint8_t dist = (i > j) ? (uint8_t)(i - j) : (uint8_t)(j - i);
                    k_gain = (dist == 2u) ? 135000 : 72000; /* nearest vs T1-T3 ipsilateral */
                } else {
                    k_gain = 67800;  /* diagonal cross (~6.5 rad/s) */
                }

                if (use_pentapod) {
                    uint16_t diff = (uint16_t)(
                        cord->phases_bam[j] - cord->phases_bam[i] - (pent_bam[j] - pent_bam[i])
                    );
                    sin_q15 = nc_sin_bam_q15(diff);
                } else {
                    int32_t s_wave = 0;
                    int32_t s_tetra = 0;
                    int32_t s_tri = 0;
                    if (w_wave_q14 > 0) {
                        uint16_t d = (uint16_t)(
                            cord->phases_bam[j] - cord->phases_bam[i]
                            - (k_offsets_wave_bam[j] - k_offsets_wave_bam[i])
                        );
                        s_wave = nc_sin_bam_q15(d);
                    }
                    if (w_tetra_q14 > 0) {
                        uint16_t d = (uint16_t)(
                            cord->phases_bam[j] - cord->phases_bam[i]
                            - (k_offsets_tetrapod_bam[j] - k_offsets_tetrapod_bam[i])
                        );
                        s_tetra = nc_sin_bam_q15(d);
                    }
                    if (w_tri_q14 > 0) {
                        uint16_t d = (uint16_t)(
                            cord->phases_bam[j] - cord->phases_bam[i]
                            - (k_offsets_tripod_bam[j] - k_offsets_tripod_bam[i])
                        );
                        s_tri = nc_sin_bam_q15(d);
                    }
                    sin_q15 = (w_wave_q14 * s_wave + w_tetra_q14 * s_tetra + w_tri_q14 * s_tri) >> 14;
                }

                /* k_gain [BAM/s] * (hj/32767) * (sin/32767) */
                coup_acc += ((int64_t)k_gain * (int64_t)hj_q15 * (int64_t)sin_q15) >> 30;
            }
            {
                int32_t coup_inc = (int32_t)(
                    (coup_acc * (int64_t)dt_ms) / ((int64_t)1000 * (int64_t)sub_steps)
                );
                next_phases[i] = (uint16_t)((int32_t)cord->phases_bam[i] + base_inc_bam + coup_inc);
            }
        }
        for (i = 0u; i < NC_NUM_LIMBS; ++i) {
            cord->phases_bam[i] = next_phases[i];
        }
    }

    /* Populate limb kinematics and support stability */
    out_state->stance_mask = 0u;
    out_state->duty_q15 = (uint16_t)((duty_bam * NC_Q15_ONE) >> 16);
    out_state->active_gait = active_gait;

    {
        int32_t left_q15 = nc_clamp_i32(
            (int32_t)cmd->drive_fwd_q15 - (((int32_t)cmd->turn_yaw_q15 * 21299) >> 15),
            -39320,
            45874
        );
        int32_t right_q15 = nc_clamp_i32(
            (int32_t)cmd->drive_fwd_q15 + (((int32_t)cmd->turn_yaw_q15 * 21299) >> 15),
            -39320,
            45874
        );
        int32_t lat_mm = ((int32_t)cmd->strafe_lat_q15 * 35) / NC_Q15_ONE;
        uint32_t swing_span = (duty_bam < 65500u) ? (65536u - duty_bam) : 36u;

        for (i = 0u; i < NC_NUM_LIMBS; ++i) {
            uint32_t phi = cord->phases_bam[i];
            int32_t side_scale_q15 = ((i & 1u) == 0u) ? left_q15 : right_q15;
            int32_t amp_mm = ((int32_t)(cord->stride_mm / 2) * side_scale_q15) / NC_Q15_ONE;

            out_state->phases_bam[i] = (uint16_t)phi;

            if ((healthy_mask & (uint8_t)(1u << i)) == 0u) {
                out_state->coxa_q15[i] = 0;
                out_state->femur_lift_q15[i] = NC_Q15_ONE;
                out_state->foot_x_mm[i] = anch_x[i];
                out_state->foot_y_mm[i] = anch_y[i];
                continue;
            }

            if (phi < duty_bam) {
                /* Stance phase */
                int32_t u_q15 = (int32_t)((phi * (uint32_t)NC_Q15_ONE) / (duty_bam ? duty_bam : 1u));
                int32_t coxa_norm_q15 = NC_Q15_ONE - 2 * u_q15;
                out_state->stance_mask |= (uint8_t)(1u << i);
                out_state->coxa_q15[i] = (int16_t)nc_clamp_i32(
                    (coxa_norm_q15 * side_scale_q15) / NC_Q15_ONE, -NC_Q15_ONE, NC_Q15_ONE
                );
                out_state->femur_lift_q15[i] = 0;
                out_state->foot_x_mm[i] = (int16_t)(anch_x[i] + ((amp_mm * coxa_norm_q15) / NC_Q15_ONE));
                out_state->foot_y_mm[i] = (int16_t)(anch_y[i] + ((lat_mm * coxa_norm_q15) / NC_Q15_ONE));
            } else {
                /* Swing phase */
                uint32_t sw_num = phi - duty_bam;
                int32_t u_sw_q15 = (int32_t)((sw_num * (uint32_t)NC_Q15_ONE) / swing_span);
                int32_t coxa_norm_q15 = -NC_Q15_ONE + 2 * u_sw_q15;
                uint16_t lift_ang = (uint16_t)((sw_num * 32768u) / swing_span);
                out_state->coxa_q15[i] = (int16_t)nc_clamp_i32(
                    (coxa_norm_q15 * side_scale_q15) / NC_Q15_ONE, -NC_Q15_ONE, NC_Q15_ONE
                );
                out_state->femur_lift_q15[i] = nc_sin_bam_q15(lift_ang);
                out_state->foot_x_mm[i] = (int16_t)(anch_x[i] + ((amp_mm * coxa_norm_q15) / NC_Q15_ONE));
                out_state->foot_y_mm[i] = (int16_t)(anch_y[i] + ((lat_mm * coxa_norm_q15) / NC_Q15_ONE));
            }
        }
    }

    out_state->is_statically_stable = nc_check_com_enclosed_mm(
        out_state->foot_x_mm, out_state->foot_y_mm, out_state->stance_mask, 5
    );
    return NC_OK;
}

/* =========================================================================
 * 3. 4..8 Channel Presynaptic-Shunting Thruster / Motor Allocator
 * ========================================================================= */

static void nc_recompute_shunt_q15(nc_shunting_cord_t *cord)
{
    uint8_t i;
    int32_t span = (int32_t)cord->shunt_knee_q15 - (int32_t)cord->shunt_floor_q15;
    if (span < 1) {
        span = 1;
    }
    for (i = 0u; i < cord->n_actuators; ++i) {
        int32_t h = cord->health_q15[i];
        if (h <= cord->shunt_floor_q15) {
            cord->shunt_q15[i] = 0;
        } else if (h >= cord->shunt_knee_q15) {
            cord->shunt_q15[i] = NC_Q15_ONE;
        } else {
            int32_t t_q14 = ((h - cord->shunt_floor_q15) * NC_Q14_ONE) / span;
            int32_t t2_q14 = (t_q14 * t_q14) >> 14;
            int32_t hermite_q14 = (t2_q14 * (3 * NC_Q14_ONE - 2 * t_q14)) >> 14;
            int32_t gated_q14 = 5734 + ((10650 * hermite_q14) >> 14); /* 0.35..1.00 above floor */
            cord->shunt_q15[i] = (int16_t)nc_clamp_i32(
                (gated_q14 * NC_Q15_ONE) >> 14, 0, NC_Q15_ONE
            );
        }
    }
}

int nc_shunting_init(
    nc_shunting_cord_t *cord,
    uint8_t n_actuators,
    const int16_t *W_q14
)
{
    uint8_t r;
    uint8_t i;
    if (cord == NULL || W_q14 == NULL || n_actuators < NC_MIN_ACTUATORS || n_actuators > NC_MAX_ACTUATORS) {
        return NC_ERR_ARG;
    }
    memset(cord, 0, sizeof(*cord));
    cord->n_actuators = n_actuators;
    for (r = 0u; r < 3u; ++r) {
        for (i = 0u; i < n_actuators; ++i) {
            cord->W_q14[r][i] = W_q14[(uint8_t)(r * n_actuators + i)];
        }
    }
    for (i = 0u; i < n_actuators; ++i) {
        cord->health_q15[i] = NC_Q15_ONE;
        cord->shunt_q15[i] = NC_Q15_ONE;
    }
    cord->lambda_q14 = 8;
    cord->shunt_floor_q15 = 7209;  /* 0.22 */
    cord->shunt_knee_q15 = 15728;  /* 0.48 */
    cord->deadband_q15 = 1638;     /* 0.05 */
    cord->tau_act_ms = 40u;
    cord->tau_fault_ms = 28u;
    cord->tau_recover_ms = 850u;
    cord->prioritize_yaw_sway = 1u;
    cord->initialized = 0u;
    return NC_OK;
}

void nc_shunting_init_usv4(nc_shunting_cord_t *cord, int16_t lx_mm, int16_t ly_mm)
{
    /* cos(45 deg) = sin(45 deg) = 0.70710678 -> 11585 in Q14 (16384 = 1.0) */
    int32_t c_q14 = 11585;
    int32_t arm_q14 = ((int32_t)(lx_mm + ly_mm) * c_q14) / 1000;
    int16_t m_q14 = (int16_t)nc_clamp_i32(arm_q14, 1, 32767);
    int16_t w[12];

    /* T0 Port-Fore (+lx,+ly,-45), T1 Stbd-Fore (+lx,-ly,+45),
     * T2 Port-Aft  (-lx,+ly,+45), T3 Stbd-Aft  (-lx,-ly,-45) */
    w[0] = +11585; w[1] = +11585; w[2] = +11585; w[3] = +11585;
    w[4] = -11585; w[5] = +11585; w[6] = +11585; w[7] = -11585;
    w[8] = (int16_t)(-m_q14);
    w[9] = (int16_t)(+m_q14);
    w[10] = (int16_t)(-m_q14);
    w[11] = (int16_t)(+m_q14);

    (void)nc_shunting_init(cord, 4u, w);
}

int nc_shunting_set_health(nc_shunting_cord_t *cord, uint8_t idx, int16_t health_q15)
{
    if (cord == NULL || idx >= cord->n_actuators) {
        return NC_ERR_ARG;
    }
    cord->health_q15[idx] = (int16_t)nc_clamp_i32((int32_t)health_q15, 0, NC_Q15_ONE);
    nc_recompute_shunt_q15(cord);
    return NC_OK;
}

int nc_shunting_reinstate(nc_shunting_cord_t *cord, uint8_t idx)
{
    if (cord == NULL || idx >= cord->n_actuators) {
        return NC_ERR_ARG;
    }
    cord->isolated_ms[idx] = 0u;
    return nc_shunting_set_health(cord, idx, cord->shunt_knee_q15);
}

void nc_shunting_set_probe_interval(nc_shunting_cord_t *cord, uint16_t probe_interval_ms)
{
    if (cord != NULL) {
        cord->probe_interval_ms = probe_interval_ms;
    }
}

static int nc_solve_gram3x3_q14(
    const int32_t g[9],
    const int32_t rhs_q14[3],
    int32_t out_x_q14[3]
)
{
    int64_t c00 = (int64_t)g[4] * g[8] - (int64_t)g[5] * g[7];
    int64_t c01 = (int64_t)g[5] * g[6] - (int64_t)g[3] * g[8];
    int64_t c02 = (int64_t)g[3] * g[7] - (int64_t)g[4] * g[6];

    int64_t c10 = (int64_t)g[2] * g[7] - (int64_t)g[1] * g[8];
    int64_t c11 = (int64_t)g[0] * g[8] - (int64_t)g[2] * g[6];
    int64_t c12 = (int64_t)g[1] * g[6] - (int64_t)g[0] * g[7];

    int64_t c20 = (int64_t)g[1] * g[5] - (int64_t)g[2] * g[4];
    int64_t c21 = (int64_t)g[2] * g[3] - (int64_t)g[0] * g[5];
    int64_t c22 = (int64_t)g[0] * g[4] - (int64_t)g[1] * g[3];

    int64_t det = (int64_t)g[0] * c00 + (int64_t)g[1] * c01 + (int64_t)g[2] * c02;
    int64_t abs_det = (det < 0) ? -det : det;
    int64_t num[3];
    int r;

    if (abs_det == 0) {
        return NC_ERR_SINGULAR;
    }

    num[0] = c00 * rhs_q14[0] + c10 * rhs_q14[1] + c20 * rhs_q14[2];
    num[1] = c01 * rhs_q14[0] + c11 * rhs_q14[1] + c21 * rhs_q14[2];
    num[2] = c02 * rhs_q14[0] + c12 * rhs_q14[1] + c22 * rhs_q14[2];

    for (r = 0; r < 3; ++r) {
        int64_t x_val;
        if (abs_det >= ((int64_t)1 << 28)) {
            int64_t det_s = det / NC_Q14_ONE;
            x_val = num[r] / det_s;
        } else {
            x_val = (num[r] * NC_Q14_ONE) / det;
        }
        out_x_q14[r] = (int32_t)nc_clamp_i32((int32_t)x_val, -(1 << 24), (1 << 24));
    }
    return NC_OK;
}

static void nc_map_x_to_actuators_q15(
    const nc_shunting_cord_t *cord,
    const int32_t x_q14[3],
    int32_t out_u_q15[NC_MAX_ACTUATORS]
)
{
    uint8_t i;
    for (i = 0u; i < cord->n_actuators; ++i) {
        int32_t h14 = ((int32_t)cord->health_q15[i] + 1) >> 1;
        int32_t s14 = ((int32_t)cord->shunt_q15[i] + 1) >> 1;
        int32_t sh14 = (s14 * h14) >> 14;
        int64_t wt_x = (int64_t)cord->W_q14[0][i] * x_q14[0]
                     + (int64_t)cord->W_q14[1][i] * x_q14[1]
                     + (int64_t)cord->W_q14[2][i] * x_q14[2];
        int32_t y_q14 = (int32_t)(wt_x >> 14);
        int32_t u_q14 = (int32_t)(((int64_t)sh14 * y_q14) >> 14);
        out_u_q15[i] = (int32_t)(((int64_t)u_q14 * NC_Q15_ONE) >> 14);
    }
}

int nc_shunting_allocate(
    const nc_shunting_cord_t *cord,
    const int32_t wrench_des_q14[3],
    nc_alloc_result_t *out_res
)
{
    int32_t g[9];
    int32_t u_final_q15[NC_MAX_ACTUATORS];
    uint8_t r;
    uint8_t c;
    uint8_t i;

    if (cord == NULL || wrench_des_q14 == NULL || out_res == NULL) {
        return NC_ERR_ARG;
    }

    memset(out_res, 0, sizeof(*out_res));

    /* Build Gram matrix G = W * diag(h^2 * s) * W^T + lambda * I_3 in Q14 */
    for (r = 0u; r < 3u; ++r) {
        for (c = 0u; c < 3u; ++c) {
            int64_t acc = 0;
            for (i = 0u; i < cord->n_actuators; ++i) {
                int32_t h14 = ((int32_t)cord->health_q15[i] + 1) >> 1;
                int32_t s14 = ((int32_t)cord->shunt_q15[i] + 1) >> 1;
                int32_t h2_14 = (h14 * h14) >> 14;
                int32_t heff_14 = (h2_14 * s14) >> 14;
                int64_t w_heff = ((int64_t)cord->W_q14[r][i] * heff_14) >> 14;
                acc += (w_heff * cord->W_q14[c][i]) >> 14;
            }
            if (r == c) {
                acc += cord->lambda_q14;
            }
            g[r * 3u + c] = (int32_t)acc;
        }
    }

    if (cord->prioritize_yaw_sway) {
        int32_t w_lat[3];
        int32_t w_surge[3];
        int32_t x_lat[3];
        int32_t x_surge[3];
        int32_t u_lat[NC_MAX_ACTUATORS];
        int32_t u_surge[NC_MAX_ACTUATORS];
        int32_t peak_lat = 0;
        int32_t alpha_q15 = NC_Q15_ONE;
        int rc;

        w_lat[0] = 0;
        w_lat[1] = wrench_des_q14[1];
        w_lat[2] = wrench_des_q14[2];
        w_surge[0] = wrench_des_q14[0];
        w_surge[1] = 0;
        w_surge[2] = 0;

        rc = nc_solve_gram3x3_q14(g, w_lat, x_lat);
        if (rc != NC_OK) {
            return rc;
        }
        rc = nc_solve_gram3x3_q14(g, w_surge, x_surge);
        if (rc != NC_OK) {
            return rc;
        }

        nc_map_x_to_actuators_q15(cord, x_lat, u_lat);
        nc_map_x_to_actuators_q15(cord, x_surge, u_surge);

        for (i = 0u; i < cord->n_actuators; ++i) {
            int32_t a = nc_abs_i32(u_lat[i]);
            if (a > peak_lat) {
                peak_lat = a;
            }
        }
        if (peak_lat > NC_Q15_ONE) {
            for (i = 0u; i < cord->n_actuators; ++i) {
                u_lat[i] = (int32_t)(((int64_t)u_lat[i] * NC_Q15_ONE) / peak_lat);
            }
        }

        for (i = 0u; i < cord->n_actuators; ++i) {
            int32_t ds = u_surge[i];
            int32_t abs_ds = nc_abs_i32(ds);
            if (abs_ds > 0) {
                int32_t rem = (ds > 0) ? (NC_Q15_ONE - u_lat[i]) : (NC_Q15_ONE + u_lat[i]);
                int32_t cand_q15;
                if (rem < 0) {
                    rem = 0;
                }
                cand_q15 = (int32_t)(((int64_t)rem * NC_Q15_ONE) / abs_ds);
                if (cand_q15 < alpha_q15) {
                    alpha_q15 = cand_q15;
                }
            }
        }

        for (i = 0u; i < cord->n_actuators; ++i) {
            int32_t combined = u_lat[i] + (int32_t)(((int64_t)alpha_q15 * u_surge[i]) / NC_Q15_ONE);
            u_final_q15[i] = nc_clamp_i32(combined, -NC_Q15_ONE, NC_Q15_ONE);
        }
    } else {
        int32_t x_all[3];
        int32_t peak = 0;
        int rc = nc_solve_gram3x3_q14(g, wrench_des_q14, x_all);
        if (rc != NC_OK) {
            return rc;
        }
        nc_map_x_to_actuators_q15(cord, x_all, u_final_q15);
        for (i = 0u; i < cord->n_actuators; ++i) {
            int32_t a = nc_abs_i32(u_final_q15[i]);
            if (a > peak) {
                peak = a;
            }
        }
        if (peak > NC_Q15_ONE) {
            for (i = 0u; i < cord->n_actuators; ++i) {
                u_final_q15[i] = (int32_t)(((int64_t)u_final_q15[i] * NC_Q15_ONE) / peak);
            }
        }
    }

    for (i = 0u; i < cord->n_actuators; ++i) {
        out_res->health_q15[i] = cord->health_q15[i];
        out_res->shunt_q15[i] = cord->shunt_q15[i];
        if (cord->shunt_q15[i] <= 4) {
            out_res->cmd_q15[i] = 0;
            out_res->isolated_mask |= (uint8_t)(1u << i);
        } else {
            out_res->cmd_q15[i] = (int16_t)u_final_q15[i];
        }
    }

    for (r = 0u; r < 3u; ++r) {
        int64_t w_acc = 0;
        for (i = 0u; i < cord->n_actuators; ++i) {
            int64_t hu_q15 = ((int64_t)cord->health_q15[i] * out_res->cmd_q15[i]) / NC_Q15_ONE;
            w_acc += ((int64_t)cord->W_q14[r][i] * hu_q15) / NC_Q15_ONE;
        }
        out_res->pred_wrench_q14[r] = (int32_t)w_acc;
    }

    return NC_OK;
}

int nc_shunting_step(
    nc_shunting_cord_t *cord,
    const int32_t wrench_des_q14[3],
    const int16_t *feedback_q15,
    uint16_t dt_ms,
    nc_alloc_result_t *out_res
)
{
    uint8_t i;
    int rc;

    if (cord == NULL || wrench_des_q14 == NULL || out_res == NULL) {
        return NC_ERR_ARG;
    }

    if (cord->initialized && feedback_q15 != NULL) {
        uint16_t eff_dt = (dt_ms == 0u) ? 1u : dt_ms;
        for (i = 0u; i < cord->n_actuators; ++i) {
            int32_t u_ref;
            if (cord->tau_act_ms > 0u) {
                int32_t num_a = (int32_t)eff_dt * NC_Q15_ONE;
                int32_t den_a = (int32_t)cord->tau_act_ms + (int32_t)eff_dt;
                int32_t alpha_a = num_a / den_a;
                int32_t prev_e = cord->exp_effort_q15[i];
                cord->exp_effort_q15[i] = (int16_t)(
                    prev_e + (((int32_t)cord->last_cmd_q15[i] - prev_e) * alpha_a) / NC_Q15_ONE
                );
            } else {
                cord->exp_effort_q15[i] = cord->last_cmd_q15[i];
            }

            u_ref = cord->exp_effort_q15[i];
            if (nc_abs_i32(u_ref) > cord->deadband_q15) {
                int32_t fb = feedback_q15[i];
                int32_t est_q15;
                uint16_t tau_ms;
                int32_t alpha_h;

                if ((fb > 0 && u_ref < 0) || (fb < 0 && u_ref > 0)) {
                    est_q15 = 0;
                } else {
                    est_q15 = (int32_t)(((int64_t)nc_abs_i32(fb) * NC_Q15_ONE) / nc_abs_i32(u_ref));
                    est_q15 = nc_clamp_i32(est_q15, 0, NC_Q15_ONE);
                }

                tau_ms = (est_q15 < cord->health_q15[i]) ? cord->tau_fault_ms : cord->tau_recover_ms;
                if (tau_ms == 0u) {
                    tau_ms = 1u;
                }
                alpha_h = ((int32_t)eff_dt * NC_Q15_ONE) / ((int32_t)tau_ms + (int32_t)eff_dt);
                cord->health_q15[i] = (int16_t)nc_clamp_i32(
                    cord->health_q15[i] + (((est_q15 - cord->health_q15[i]) * alpha_h) / NC_Q15_ONE),
                    0,
                    NC_Q15_ONE
                );
            }
        }
        nc_recompute_shunt_q15(cord);
    }

    if (cord->probe_interval_ms > 0u) {
        for (i = 0u; i < cord->n_actuators; ++i) {
            if (cord->shunt_q15[i] <= 4) {
                uint32_t t = (uint32_t)cord->isolated_ms[i] + (uint32_t)dt_ms;
                cord->isolated_ms[i] = (uint16_t)((t > 65535u) ? 65535u : t);
                if (cord->isolated_ms[i] >= cord->probe_interval_ms) {
                    (void)nc_shunting_reinstate(cord, i);
                }
            } else {
                cord->isolated_ms[i] = 0u;
            }
        }
    }

    rc = nc_shunting_allocate(cord, wrench_des_q14, out_res);
    if (rc != NC_OK) {
        return rc;
    }

    for (i = 0u; i < cord->n_actuators; ++i) {
        cord->last_cmd_q15[i] = out_res->cmd_q15[i];
        if (!cord->initialized && cord->tau_act_ms == 0u) {
            cord->exp_effort_q15[i] = out_res->cmd_q15[i];
        }
    }
    cord->initialized = 1u;
    return NC_OK;
}
