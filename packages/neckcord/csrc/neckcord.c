/*
 * NeckCord v2.0.0 - Zero-Heap, Pure Fixed-Point ANSI C99 Implementation
 *
 * Copyright (c) 2026 Amandeep Singh Bains. MIT License.
 */

#include "neckcord.h"
#include <string.h>

#define NC_Q20_ONE ((int64_t)1048576)

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

/* Octant 16-bit BAM arctangent table: atan(i / 64) * 65536 / (2*pi) for i = 0..64 */
static const uint16_t k_atan_bam_lut[65] = {
       0u,  163u,  326u,  489u,  651u,  813u,  975u, 1136u,
    1297u, 1457u, 1617u, 1775u, 1933u, 2090u, 2246u, 2401u,
    2555u, 2708u, 2860u, 3010u, 3159u, 3307u, 3453u, 3599u,
    3742u, 3884u, 4025u, 4164u, 4302u, 4438u, 4572u, 4705u,
    4836u, 4966u, 5094u, 5220u, 5344u, 5467u, 5589u, 5708u,
    5826u, 5943u, 6058u, 6171u, 6282u, 6392u, 6500u, 6607u,
    6712u, 6815u, 6917u, 7018u, 7117u, 7214u, 7310u, 7405u,
    7498u, 7589u, 7679u, 7768u, 7856u, 7942u, 8026u, 8110u,
    8192u
};

/* Quadrant 16-bit BAM arccosine LUT: acos(i / 64) * 65536 / (2*pi) for i = 0..64 */
static const uint16_t k_acos_bam_lut[65] = {
    16384u, 16221u, 16058u, 15895u, 15732u, 15568u, 15405u, 15241u,
    15077u, 14912u, 14748u, 14582u, 14417u, 14250u, 14084u, 13916u,
    13748u, 13580u, 13410u, 13240u, 13069u, 12897u, 12724u, 12550u,
    12375u, 12198u, 12020u, 11841u, 11661u, 11479u, 11295u, 11110u,
    10923u, 10733u, 10542u, 10349u, 10153u,  9954u,  9753u,  9549u,
     9342u,  9132u,  8918u,  8700u,  8477u,  8251u,  8019u,  7782u,
     7538u,  7289u,  7031u,  6766u,  6492u,  6207u,  5909u,  5599u,
     5271u,  4924u,  4553u,  4150u,  3707u,  3206u,  2614u,  1846u,
        0u
};

/* Nominal neutral hexapod footholds in millimeters: L1, R1, L2, R2, L3, R3 */
static const int16_t k_nom_foot_x_mm[NC_NUM_LIMBS] = { +200, +200,    0,    0, -200, -200 };
static const int16_t k_nom_foot_y_mm[NC_NUM_LIMBS] = { +130, -130, +160, -160, +130, -130 };

/* Default thoracic coxa hip mounts [mm], outward mount yaw [BAM], and retracted park XY [mm] */
static const int16_t k_hip_x_mm[NC_NUM_LIMBS]    = { +120, +120,    0,    0, -120, -120 };
static const int16_t k_hip_y_mm[NC_NUM_LIMBS]    = {  +55,  -55,  +65,  -65,  +55,  -55 };
static const uint16_t k_mount_bam[NC_NUM_LIMBS]  = {
     8192u, /* +45 deg */
    57344u, /* -45 deg */
    16384u, /* +90 deg */
    49152u, /* -90 deg */
    24576u, /* +135 deg */
    40960u  /* -135 deg */
};
static const int16_t k_park_x_mm[NC_NUM_LIMBS]   = { +190, +190,    0,    0, -190, -190 };
static const int16_t k_park_y_mm[NC_NUM_LIMBS]   = { +120, -120, +150, -150, +120, -120 };

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

static int64_t nc_clamp_i64(int64_t v, int64_t lo, int64_t hi)
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
    if (v == INT32_MIN) {
        return INT32_MAX;
    }
    return (v < 0) ? -v : v;
}

/* =========================================================================
 * Integer Trigonometric & Root Primitives (Zero FP)
 * ========================================================================= */

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

uint32_t nc_isqrt_u32(uint32_t val)
{
    uint32_t op = val;
    uint32_t res = 0u;
    uint32_t one = (uint32_t)1u << 30;

    while (one > op) {
        one >>= 2;
    }
    while (one != 0u) {
        if (op >= res + one) {
            op -= res + one;
            res = (res >> 1) + one;
        } else {
            res >>= 1;
        }
        one >>= 2;
    }
    return res;
}

uint32_t nc_isqrt_u64(uint64_t val)
{
    uint64_t op = val;
    uint64_t res = 0u;
    uint64_t one = (uint64_t)1u << 62;

    while (one > op) {
        one >>= 2;
    }
    while (one != 0u) {
        if (op >= res + one) {
            op -= res + one;
            res = (res >> 1) + one;
        } else {
            res >>= 1;
        }
        one >>= 2;
    }
    return (uint32_t)res;
}

uint16_t nc_cord_atan2_bam(int32_t y, int32_t x)
{
    uint32_t ax;
    uint32_t ay;
    uint32_t num;
    uint32_t den;
    uint32_t ratio_q14;
    uint32_t idx;
    uint32_t frac;
    uint16_t base_ang;
    uint16_t q1_ang;

    if (x == 0 && y == 0) {
        return 0u;
    }

    ax = (uint32_t)nc_abs_i32(x);
    ay = (uint32_t)nc_abs_i32(y);

    if (ax >= ay) {
        num = ay;
        den = ax;
    } else {
        num = ax;
        den = ay;
    }

    /* ratio in Q14: [0, 16384] -> LUT index 0..64 with 8-bit fractional interpolation */
    ratio_q14 = (uint32_t)(((uint64_t)num * 16384u + (den >> 1)) / den);
    if (ratio_q14 >= 16384u) {
        base_ang = k_atan_bam_lut[64];
    } else {
        idx = ratio_q14 >> 8;
        frac = ratio_q14 & 0xFFu;
        base_ang = (uint16_t)(
            k_atan_bam_lut[idx]
            + ((((uint32_t)k_atan_bam_lut[idx + 1u] - (uint32_t)k_atan_bam_lut[idx]) * frac + 128u) >> 8)
        );
    }

    q1_ang = (ax >= ay) ? base_ang : (uint16_t)(16384u - base_ang);

    if (x >= 0 && y >= 0) {
        return q1_ang;
    }
    if (x < 0 && y >= 0) {
        return (uint16_t)(32768u - q1_ang);
    }
    if (x < 0 && y < 0) {
        return (uint16_t)(32768u + q1_ang);
    }
    return (uint16_t)(0u - q1_ang);
}

uint16_t nc_acos_q15_bam(int16_t cos_q15)
{
    int32_t c = nc_clamp_i32((int32_t)cos_q15, -NC_Q15_ONE, NC_Q15_ONE);
    uint32_t ac = (uint32_t)nc_abs_i32(c);
    uint16_t q1_bam;

    if (ac <= 28672u) {
        /* Smooth interior [0, 0.875]: linearly interpolate 65-entry acos BAM LUT */
        uint32_t scaled = (ac * 16384u + 16383u) / (uint32_t)NC_Q15_ONE;
        uint32_t idx = scaled >> 8;
        uint32_t frac = scaled & 0xFFu;
        if (idx >= 64u) {
            q1_bam = 0u;
        } else {
            int32_t a0 = (int32_t)k_acos_bam_lut[idx];
            int32_t a1 = (int32_t)k_acos_bam_lut[idx + 1u];
            q1_bam = (uint16_t)(a0 + (((a1 - a0) * (int32_t)frac + 128) >> 8));
        }
    } else {
        /* Steep boundary (0.875, 1.0]: exact atan2(isqrt(1 - c^2), |c|) eliminates slope error */
        uint64_t r2 = (uint64_t)NC_Q15_ONE * (uint64_t)NC_Q15_ONE;
        uint64_t c2 = (uint64_t)ac * (uint64_t)ac;
        uint32_t sin_part = (c2 >= r2) ? 0u : nc_isqrt_u64(r2 - c2);
        q1_bam = nc_cord_atan2_bam((int32_t)sin_part, (int32_t)ac);
    }

    return (c >= 0) ? q1_bam : (uint16_t)(32768u - q1_bam);
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
            (w_wave_q14 * 53740 + w_tetra_q14 * 44564 + w_tri_q14 * 35389) / NC_Q14_ONE
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
                if ((i ^ 1u) == j) {
                    k_gain = 156450;
                } else if ((i & 1u) == (j & 1u)) {
                    uint8_t dist = (i > j) ? (uint8_t)(i - j) : (uint8_t)(j - i);
                    k_gain = (dist == 2u) ? 135000 : 72000;
                } else {
                    k_gain = 67800;
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
                    sin_q15 = (w_wave_q14 * s_wave + w_tetra_q14 * s_tetra + w_tri_q14 * s_tri) / NC_Q14_ONE;
                }

                coup_acc += ((int64_t)k_gain * (int64_t)hj_q15 * (int64_t)sin_q15) / ((int64_t)1 << 30);
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

    out_state->stance_mask = 0u;
    out_state->duty_q15 = (uint16_t)((duty_bam * NC_Q15_ONE) >> 16);
    out_state->active_gait = active_gait;

    {
        int32_t left_q15 = nc_clamp_i32(
            (int32_t)cmd->drive_fwd_q15 - (((int32_t)cmd->turn_yaw_q15 * 21299) / NC_Q15_ONE),
            -39320,
            45874
        );
        int32_t right_q15 = nc_clamp_i32(
            (int32_t)cmd->drive_fwd_q15 + (((int32_t)cmd->turn_yaw_q15 * 21299) / NC_Q15_ONE),
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
 * 3. Symmetric 3-DOF Presynaptic-Shunting Allocator (v1.x compatible)
 * ========================================================================= */

static int16_t nc_hermite_shunt_q15(int32_t h, int32_t floor_q15, int32_t knee_q15)
{
    int32_t span = knee_q15 - floor_q15;
    if (span < 1) {
        span = 1;
    }
    if (h <= floor_q15) {
        return 0;
    }
    if (h >= knee_q15) {
        return NC_Q15_ONE;
    }
    {
        int32_t t_q14 = ((h - floor_q15) * NC_Q14_ONE) / span;
        int32_t t2_q14 = (t_q14 * t_q14) / NC_Q14_ONE;
        int32_t hermite_q14 = (t2_q14 * (3 * NC_Q14_ONE - 2 * t_q14)) / NC_Q14_ONE;
        int32_t gated_q14 = 5734 + ((10650 * hermite_q14) / NC_Q14_ONE);
        return (int16_t)nc_clamp_i32((gated_q14 * NC_Q15_ONE) / NC_Q14_ONE, 0, NC_Q15_ONE);
    }
}

static void nc_recompute_shunt_q15(nc_shunting_cord_t *cord)
{
    uint8_t i;
    for (i = 0u; i < cord->n_actuators; ++i) {
        cord->shunt_q15[i] = nc_hermite_shunt_q15(
            cord->health_q15[i], cord->shunt_floor_q15, cord->shunt_knee_q15
        );
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
    cord->shunt_floor_q15 = 7209;
    cord->shunt_knee_q15 = 15728;
    cord->deadband_q15 = 983;
    cord->tau_act_ms = 40u;
    cord->tau_fault_ms = 28u;
    cord->tau_recover_ms = 850u;
    cord->prioritize_yaw_sway = 1u;
    cord->initialized = 0u;
    return NC_OK;
}

void nc_shunting_init_usv4(nc_shunting_cord_t *cord, int16_t lx_mm, int16_t ly_mm)
{
    int32_t c_q14 = 11585;
    int32_t arm_q14 = ((int32_t)(lx_mm + ly_mm) * c_q14) / 1000;
    int16_t m_q14 = (int16_t)nc_clamp_i32(arm_q14, 1, 32767);
    int16_t w[12];

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
        out_x_q14[r] = (int32_t)nc_clamp_i64(x_val, -((int64_t)1 << 24), ((int64_t)1 << 24));
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
        int32_t sh14 = (s14 * h14) / NC_Q14_ONE;
        int64_t wt_x = (int64_t)cord->W_q14[0][i] * x_q14[0]
                     + (int64_t)cord->W_q14[1][i] * x_q14[1]
                     + (int64_t)cord->W_q14[2][i] * x_q14[2];
        int32_t y_q14 = (int32_t)(wt_x / NC_Q14_ONE);
        int32_t u_q14 = (int32_t)(((int64_t)sh14 * y_q14) / NC_Q14_ONE);
        out_u_q15[i] = (int32_t)(((int64_t)u_q14 * NC_Q15_ONE) / NC_Q14_ONE);
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

    for (r = 0u; r < 3u; ++r) {
        for (c = 0u; c < 3u; ++c) {
            int64_t acc = 0;
            for (i = 0u; i < cord->n_actuators; ++i) {
                int32_t h14 = ((int32_t)cord->health_q15[i] + 1) >> 1;
                int32_t s14 = ((int32_t)cord->shunt_q15[i] + 1) >> 1;
                int32_t h2_14 = (h14 * h14) / NC_Q14_ONE;
                int32_t heff_14 = (h2_14 * s14) / NC_Q14_ONE;
                int64_t w_heff = ((int64_t)cord->W_q14[r][i] * heff_14) / NC_Q14_ONE;
                acc += (w_heff * cord->W_q14[c][i]) / NC_Q14_ONE;
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

            {
                int32_t e_i = cord->exp_effort_q15[i];
                int32_t c_i = cord->last_cmd_q15[i];
                if ((e_i >= 0 && c_i <= 0) || (e_i <= 0 && c_i >= 0)) {
                    u_ref = 0;
                } else {
                    u_ref = (nc_abs_i32(c_i) < nc_abs_i32(e_i)) ? c_i : e_i;
                }
                if (nc_abs_i32(e_i) <= cord->deadband_q15) {
                    u_ref = 0;
                }
            }
            if (nc_abs_i32(u_ref) > (cord->deadband_q15 >> 2)) {
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

/* =========================================================================
 * 4. v2.0 Box-Constrained Asymmetric 3-DOF & 4-DOF Active-Set Allocator
 * ========================================================================= */

static void nc_recompute_shunt4d_q15(nc_shunting4d_t *cord)
{
    uint8_t i;
    for (i = 0u; i < cord->n_actuators; ++i) {
        cord->shunt_q15[i] = nc_hermite_shunt_q15(
            cord->health_q15[i], cord->shunt_floor_q15, cord->shunt_knee_q15
        );
    }
}

int nc_shunting4d_init(
    nc_shunting4d_t *cord,
    uint8_t n_dof,
    uint8_t n_actuators,
    const int16_t *W_q14,
    const int16_t *u_min_q15,
    const int16_t *u_max_q15
)
{
    uint8_t r;
    uint8_t i;

    if (cord == NULL || W_q14 == NULL ||
        (n_dof != 3u && n_dof != 4u) ||
        n_actuators < NC_MIN_ACTUATORS || n_actuators > NC_MAX_ACTUATORS) {
        return NC_ERR_ARG;
    }

    memset(cord, 0, sizeof(*cord));
    cord->n_dof = n_dof;
    cord->n_actuators = n_actuators;
    cord->prioritize_attitude = 1u;

    for (r = 0u; r < n_dof; ++r) {
        for (i = 0u; i < n_actuators; ++i) {
            cord->W_q14[r][i] = W_q14[(uint8_t)(r * n_actuators + i)];
        }
    }
    for (i = 0u; i < n_actuators; ++i) {
        int16_t hi = (u_max_q15 != NULL) ? u_max_q15[i] : (int16_t)NC_Q15_ONE;
        int16_t lo = (u_min_q15 != NULL) ? u_min_q15[i] : (int16_t)(-hi);
        if (hi <= 0 || lo > 0 || lo >= hi) {
            return NC_ERR_RANGE;
        }
        cord->u_min_q15[i] = lo;
        cord->u_max_q15[i] = hi;
        cord->health_q15[i] = NC_Q15_ONE;
        cord->shunt_q15[i] = NC_Q15_ONE;
    }

    cord->lambda_q20 = 16;
    cord->shunt_floor_q15 = 7209;
    cord->shunt_knee_q15 = 15728;
    cord->deadband_q15 = 983;
    cord->tau_act_ms = 40u;
    cord->tau_fault_ms = 28u;
    cord->tau_recover_ms = 850u;
    return NC_OK;
}

void nc_shunting4d_init_hex6(nc_shunting4d_t *cord, int16_t arm_mm, int16_t k_yaw_milli)
{
    /* 6-rotor FLU hexacopter at 0, 60, 120, 180, 240, 300 deg:
     * cos = {1, +1/2, -1/2, -1, -1/2, +1/2}, sin = {0, +sqrt(3)/2, +sqrt(3)/2, 0, -sqrt(3)/2, -sqrt(3)/2} */
    static const int16_t k_cos6_q14[6] = { +16384,  +8192,  -8192, -16384,  -8192,  +8192 };
    static const int16_t k_sin6_q14[6] = {      0, +14189, +14189,      0, -14189, -14189 };
    int16_t w[24];
    int16_t u_min[6];
    int16_t u_max[6];
    int16_t ky_q14 = (int16_t)nc_clamp_i32(((int32_t)k_yaw_milli * NC_Q14_ONE + 500) / 1000, 1, 16384);
    uint8_t i;

    for (i = 0u; i < 6u; ++i) {
        int32_t px_q14 = ((int32_t)arm_mm * (int32_t)k_cos6_q14[i]) / 1000;
        int32_t py_q14 = ((int32_t)arm_mm * (int32_t)k_sin6_q14[i]) / 1000;
        int16_t spin = ((i & 1u) == 0u) ? +1 : -1;

        w[0u * 6u + i] = NC_Q14_ONE;                        /* Fz */
        w[1u * 6u + i] = (int16_t)nc_clamp_i32(py_q14, -32767, 32767);   /* Tx = +py */
        w[2u * 6u + i] = (int16_t)nc_clamp_i32(-px_q14, -32767, 32767);  /* Ty = -px */
        w[3u * 6u + i] = (int16_t)(spin * ky_q14);          /* Tz */
        u_min[i] = 0;
        u_max[i] = NC_Q15_ONE;
    }
    (void)nc_shunting4d_init(cord, 4u, 6u, w, u_min, u_max);
    if (cord != NULL) {
        cord->tau_act_ms = 18u;
        cord->tau_fault_ms = 20u;
    }
}

void nc_shunting4d_init_octo8(nc_shunting4d_t *cord, int16_t arm_mm, int16_t k_yaw_milli)
{
    static const int16_t k_cos8_q14[8] = {
        +16384, +11585,      0, -11585, -16384, -11585,      0, +11585
    };
    static const int16_t k_sin8_q14[8] = {
             0, +11585, +16384, +11585,      0, -11585, -16384, -11585
    };
    int16_t w[32];
    int16_t u_min[8];
    int16_t u_max[8];
    int16_t ky_q14 = (int16_t)nc_clamp_i32(((int32_t)k_yaw_milli * NC_Q14_ONE + 500) / 1000, 1, 16384);
    uint8_t i;

    for (i = 0u; i < 8u; ++i) {
        int32_t px_q14 = ((int32_t)arm_mm * (int32_t)k_cos8_q14[i]) / 1000;
        int32_t py_q14 = ((int32_t)arm_mm * (int32_t)k_sin8_q14[i]) / 1000;
        int16_t spin = ((i & 1u) == 0u) ? +1 : -1;

        w[0u * 8u + i] = NC_Q14_ONE;
        w[1u * 8u + i] = (int16_t)nc_clamp_i32(py_q14, -32767, 32767);
        w[2u * 8u + i] = (int16_t)nc_clamp_i32(-px_q14, -32767, 32767);
        w[3u * 8u + i] = (int16_t)(spin * ky_q14);
        u_min[i] = 0;
        u_max[i] = NC_Q15_ONE;
    }
    (void)nc_shunting4d_init(cord, 4u, 8u, w, u_min, u_max);
    if (cord != NULL) {
        cord->tau_act_ms = 18u;
        cord->tau_fault_ms = 20u;
    }
}

void nc_shunting4d_init_usv_t200(
    nc_shunting4d_t *cord,
    int16_t lx_mm,
    int16_t ly_mm,
    int16_t u_min_q15,
    int16_t deadzone_q15
)
{
    int32_t c_q14 = 11585;
    int32_t arm_q14 = ((int32_t)(lx_mm + ly_mm) * c_q14) / 1000;
    int16_t m_q14 = (int16_t)nc_clamp_i32(arm_q14, 1, 32767);
    int16_t w[12];
    int16_t u_min[4];
    int16_t u_max[4];
    int16_t eff_min = (u_min_q15 < 0) ? u_min_q15 : (int16_t)(-24965);
    int16_t eff_dz = (int16_t)nc_clamp_i32(deadzone_q15, 0, 16384);
    uint8_t i;

    w[0] = +11585; w[1] = +11585; w[2] = +11585; w[3] = +11585;
    w[4] = -11585; w[5] = +11585; w[6] = +11585; w[7] = -11585;
    w[8]  = (int16_t)(-m_q14);
    w[9]  = (int16_t)(+m_q14);
    w[10] = (int16_t)(-m_q14);
    w[11] = (int16_t)(+m_q14);

    for (i = 0u; i < 4u; ++i) {
        u_min[i] = eff_min;
        u_max[i] = NC_Q15_ONE;
    }
    (void)nc_shunting4d_init(cord, 3u, 4u, w, u_min, u_max);
    if (cord != NULL) {
        for (i = 0u; i < 4u; ++i) {
            cord->deadzone_q15[i] = eff_dz;
        }
    }
}

int nc_shunting4d_set_health(nc_shunting4d_t *cord, uint8_t idx, int16_t health_q15)
{
    if (cord == NULL || idx >= cord->n_actuators) {
        return NC_ERR_ARG;
    }
    cord->health_q15[idx] = (int16_t)nc_clamp_i32((int32_t)health_q15, 0, NC_Q15_ONE);
    nc_recompute_shunt4d_q15(cord);
    return NC_OK;
}

int nc_shunting4d_reinstate(nc_shunting4d_t *cord, uint8_t idx)
{
    if (cord == NULL || idx >= cord->n_actuators) {
        return NC_ERR_ARG;
    }
    cord->isolated_ms[idx] = 0u;
    return nc_shunting4d_set_health(cord, idx, cord->shunt_knee_q15);
}

/* Exact Q20 LDL^T symmetric positive-definite solver for dim in {3, 4} */
static int nc_solve_ldlt_q20(
    uint8_t dim,
    const int64_t g_q20[NC_MAX_DOF * NC_MAX_DOF],
    int64_t min_pivot_q20,
    const int32_t *rhs_q14,
    int64_t *out_x_q20
)
{
    int64_t l_q20[NC_MAX_DOF][NC_MAX_DOF];
    int64_t d_q20[NC_MAX_DOF];
    int64_t z_q20[NC_MAX_DOF];
    int64_t y_q20[NC_MAX_DOF];
    uint8_t i;
    uint8_t j;
    uint8_t k;

    memset(l_q20, 0, sizeof(l_q20));
    for (j = 0u; j < dim; ++j) {
        int64_t dj = g_q20[j * NC_MAX_DOF + j];
        for (k = 0u; k < j; ++k) {
            int64_t ld = (l_q20[j][k] * d_q20[k]) / NC_Q20_ONE;
            dj -= (l_q20[j][k] * ld) / NC_Q20_ONE;
        }
        if (dj < min_pivot_q20) {
            if (min_pivot_q20 > 0) {
                dj = min_pivot_q20;
            } else if (dj <= 0) {
                return NC_ERR_SINGULAR;
            }
        }
        d_q20[j] = dj;
        l_q20[j][j] = NC_Q20_ONE;

        for (i = (uint8_t)(j + 1u); i < dim; ++i) {
            int64_t sum = g_q20[i * NC_MAX_DOF + j];
            for (k = 0u; k < j; ++k) {
                int64_t ld = (l_q20[j][k] * d_q20[k]) / NC_Q20_ONE;
                sum -= (l_q20[i][k] * ld) / NC_Q20_ONE;
            }
            sum = nc_clamp_i64(sum, -((int64_t)1 << 40), ((int64_t)1 << 40));
            l_q20[i][j] = nc_clamp_i64(
                (sum * NC_Q20_ONE) / dj,
                -((int64_t)1 << 27),
                ((int64_t)1 << 27)
            );
        }
    }

    for (i = 0u; i < dim; ++i) {
        int64_t v = (int64_t)rhs_q14[i] * 64;
        for (k = 0u; k < i; ++k) {
            v -= (l_q20[i][k] * z_q20[k]) / NC_Q20_ONE;
        }
        z_q20[i] = nc_clamp_i64(v, -((int64_t)1 << 34), ((int64_t)1 << 34));
    }

    for (i = 0u; i < dim; ++i) {
        y_q20[i] = nc_clamp_i64(
            (z_q20[i] * NC_Q20_ONE) / d_q20[i],
            -((int64_t)1 << 34),
            ((int64_t)1 << 34)
        );
    }

    for (i = dim; i > 0u; --i) {
        uint8_t r = (uint8_t)(i - 1u);
        int64_t v = y_q20[r];
        for (k = (uint8_t)(r + 1u); k < dim; ++k) {
            v -= (l_q20[k][r] * out_x_q20[k]) / NC_Q20_ONE;
        }
        out_x_q20[r] = nc_clamp_i64(v, -((int64_t)1 << 34), ((int64_t)1 << 34));
    }
    return NC_OK;
}

static void nc_eval_wrench_sub_q14(
    const nc_shunting4d_t *cord,
    uint8_t dim,
    const int32_t u_q15[NC_MAX_ACTUATORS],
    int32_t out_w_q14[NC_MAX_DOF]
)
{
    uint8_t r;
    uint8_t i;
    for (r = 0u; r < dim; ++r) {
        int64_t acc = 0;
        for (i = 0u; i < cord->n_actuators; ++i) {
            int64_t hu_q15 = ((int64_t)cord->health_q15[i] * u_q15[i]) / NC_Q15_ONE;
            acc += ((int64_t)cord->W_q14[r][i] * hu_q15) / NC_Q15_ONE;
        }
        out_w_q14[r] = (int32_t)acc;
    }
}

static int nc_active_set_solve4d(
    const nc_shunting4d_t *cord,
    uint8_t dim,
    const int32_t target_w_q14[NC_MAX_DOF],
    uint8_t fixed_in_mask,
    const int32_t u_fixed_in[NC_MAX_ACTUATORS],
    int32_t out_u_q15[NC_MAX_ACTUATORS],
    uint8_t *out_iters
)
{
    uint8_t unclamped_mask = 0u;
    int8_t clamp_dir[NC_MAX_ACTUATORS];
    uint8_t releases = 0u;
    uint8_t iter;
    uint8_t i;
    uint8_t r;
    uint8_t c;
    int32_t reg_q20 = (cord->lambda_q20 > 0) ? cord->lambda_q20 : 16;

    memset(clamp_dir, 0, sizeof(clamp_dir));
    for (i = 0u; i < cord->n_actuators; ++i) {
        out_u_q15[i] = (u_fixed_in != NULL) ? u_fixed_in[i] : 0;
        if (cord->shunt_q15[i] <= 4 || cord->health_q15[i] <= 4) {
            out_u_q15[i] = 0;
        } else if ((fixed_in_mask & (uint8_t)(1u << i)) == 0u) {
            unclamped_mask |= (uint8_t)(1u << i);
        }
    }

    for (iter = 0u; iter < (uint8_t)(2u * cord->n_actuators + 2u); ++iter) {
        int64_t g_q20[NC_MAX_DOF * NC_MAX_DOF];
        int32_t w_rem_q14[NC_MAX_DOF];
        int64_t x_q20[NC_MAX_DOF];
        int32_t u_cand[NC_MAX_ACTUATORS];
        int32_t worst_viol = 2;
        int worst_idx = -1;
        int8_t worst_dir = 0;
        int rc;

        if (unclamped_mask == 0u) {
            break;
        }

        memset(g_q20, 0, sizeof(g_q20));
        for (r = 0u; r < dim; ++r) {
            int64_t fixed_w = 0;
            for (i = 0u; i < cord->n_actuators; ++i) {
                if ((unclamped_mask & (uint8_t)(1u << i)) == 0u) {
                    int64_t hu = ((int64_t)cord->health_q15[i] * out_u_q15[i]) / NC_Q15_ONE;
                    fixed_w += ((int64_t)cord->W_q14[r][i] * hu) / NC_Q15_ONE;
                }
            }
            w_rem_q14[r] = target_w_q14[r] - (int32_t)fixed_w;
        }

        for (r = 0u; r < dim; ++r) {
            for (c = 0u; c < dim; ++c) {
                int64_t acc_q42 = 0;
                int64_t acc_q20;
                for (i = 0u; i < cord->n_actuators; ++i) {
                    if ((unclamped_mask & (uint8_t)(1u << i)) != 0u) {
                        int64_t h14 = ((int64_t)cord->health_q15[i] + 1) >> 1;
                        int64_t s14 = ((int64_t)cord->shunt_q15[i] + 1) >> 1;
                        int64_t heff_14 = ((h14 * h14) / NC_Q14_ONE * s14) / NC_Q14_ONE;
                        int64_t ww_q28 = (int64_t)cord->W_q14[r][i] * (int64_t)cord->W_q14[c][i];
                        acc_q42 += ww_q28 * heff_14;
                    }
                }
                acc_q20 = acc_q42 / ((int64_t)1 << 22);
                if (r == c) {
                    acc_q20 += reg_q20;
                }
                g_q20[r * NC_MAX_DOF + c] = acc_q20;
            }
        }

        rc = nc_solve_ldlt_q20(dim, g_q20, (reg_q20 >= 8) ? (reg_q20 / 2) : 4, w_rem_q14, x_q20);
        if (rc != NC_OK) {
            return rc;
        }

        for (i = 0u; i < cord->n_actuators; ++i) {
            if ((unclamped_mask & (uint8_t)(1u << i)) != 0u) {
                int64_t h14 = ((int64_t)cord->health_q15[i] + 1) >> 1;
                int64_t s14 = ((int64_t)cord->shunt_q15[i] + 1) >> 1;
                int64_t sh14 = (s14 * h14) / NC_Q14_ONE;
                int64_t wt_x = 0;
                int64_t y_q14;
                int64_t u_q14;
                for (r = 0u; r < dim; ++r) {
                    wt_x += (int64_t)cord->W_q14[r][i] * x_q20[r];
                }
                y_q14 = wt_x / NC_Q20_ONE;
                u_q14 = (sh14 * y_q14) / NC_Q14_ONE;
                u_cand[i] = (int32_t)nc_clamp_i64(
                    (u_q14 * NC_Q15_ONE) / NC_Q14_ONE,
                    -((int64_t)1 << 24),
                    ((int64_t)1 << 24)
                );

                if (u_cand[i] < (int32_t)cord->u_min_q15[i] - 2) {
                    int32_t viol = (int32_t)cord->u_min_q15[i] - u_cand[i];
                    if (viol > worst_viol) {
                        worst_viol = viol;
                        worst_idx = (int)i;
                        worst_dir = -1;
                    }
                } else if (u_cand[i] > (int32_t)cord->u_max_q15[i] + 2) {
                    int32_t viol = u_cand[i] - (int32_t)cord->u_max_q15[i];
                    if (viol > worst_viol) {
                        worst_viol = viol;
                        worst_idx = (int)i;
                        worst_dir = +1;
                    }
                }
            }
        }

        if (worst_idx >= 0) {
            uint8_t wi = (uint8_t)worst_idx;
            out_u_q15[wi] = (worst_dir < 0) ? cord->u_min_q15[wi] : cord->u_max_q15[wi];
            unclamped_mask = (uint8_t)(unclamped_mask & (uint8_t)(~(1u << wi)));
            clamp_dir[wi] = worst_dir;
            continue;
        }

        for (i = 0u; i < cord->n_actuators; ++i) {
            if ((unclamped_mask & (uint8_t)(1u << i)) != 0u) {
                out_u_q15[i] = nc_clamp_i32(u_cand[i], cord->u_min_q15[i], cord->u_max_q15[i]);
            }
        }

        /* KKT multiplier check on clamped bounds when residual wrench is non-zero */
        if (releases < cord->n_actuators) {
            int32_t w_cur[NC_MAX_DOF];
            int64_t best_gain = ((int64_t)1 << 20);
            int best_rel = -1;

            nc_eval_wrench_sub_q14(cord, dim, out_u_q15, w_cur);
            for (i = 0u; i < cord->n_actuators; ++i) {
                int64_t grad_acc = 0;
                if (clamp_dir[i] == 0) {
                    continue;
                }
                for (r = 0u; r < dim; ++r) {
                    int64_t res_r = (int64_t)target_w_q14[r] - (int64_t)w_cur[r];
                    grad_acc += (int64_t)cord->W_q14[r][i] * res_r;
                }
                grad_acc = (grad_acc * cord->health_q15[i]) / NC_Q15_ONE;
                if (clamp_dir[i] < 0 && grad_acc > best_gain) {
                    best_gain = grad_acc;
                    best_rel = (int)i;
                } else if (clamp_dir[i] > 0 && (-grad_acc) > best_gain) {
                    best_gain = -grad_acc;
                    best_rel = (int)i;
                }
            }
            if (best_rel >= 0) {
                uint8_t ri = (uint8_t)best_rel;
                unclamped_mask |= (uint8_t)(1u << ri);
                clamp_dir[ri] = 0;
                ++releases;
                continue;
            }
        }
        break;
    }

    if (out_iters != NULL) {
        *out_iters = (uint8_t)(iter + 1u);
    }
    return NC_OK;
}

static void nc_apply_deadzone4d(
    const nc_shunting4d_t *cord,
    uint8_t dim,
    const int32_t target_w_q14[NC_MAX_DOF],
    int32_t u_q15[NC_MAX_ACTUATORS]
)
{
    uint8_t has_dz = 0u;
    int64_t w_norm = 0;
    uint8_t fixed_mask = 0u;
    uint8_t pass;
    uint8_t i;

    for (i = 0u; i < cord->n_actuators; ++i) {
        if (cord->deadzone_q15[i] > 0) {
            has_dz = 1u;
        }
    }
    for (i = 0u; i < dim; ++i) {
        w_norm += nc_abs_i32(target_w_q14[i]);
    }
    if (!has_dz || w_norm == 0) {
        return;
    }

    for (pass = 0u; pass < cord->n_actuators; ++pass) {
        int worst_i = -1;
        int32_t smallest_au = NC_Q15_ONE + 1;
        int32_t u_try0[NC_MAX_ACTUATORS];
        int32_t u_trydz[NC_MAX_ACTUATORS];
        int32_t u_res0[NC_MAX_ACTUATORS];
        int32_t u_resdz[NC_MAX_ACTUATORS];
        int32_t w0[NC_MAX_DOF];
        int32_t wdz[NC_MAX_DOF];
        int64_t err0 = 0;
        int64_t errdz = 0;
        uint8_t next_mask;
        uint8_t r;

        for (i = 0u; i < cord->n_actuators; ++i) {
            int32_t dz = cord->deadzone_q15[i];
            int32_t au = nc_abs_i32(u_q15[i]);
            if ((fixed_mask & (uint8_t)(1u << i)) != 0u || cord->shunt_q15[i] <= 4) {
                continue;
            }
            if (dz > 0 && au > 0 && au < dz && au < smallest_au) {
                smallest_au = au;
                worst_i = (int)i;
            }
        }
        if (worst_i < 0) {
            break;
        }

        next_mask = (uint8_t)(fixed_mask | (uint8_t)(1u << (uint8_t)worst_i));
        for (i = 0u; i < cord->n_actuators; ++i) {
            u_try0[i] = u_q15[i];
            u_trydz[i] = u_q15[i];
        }
        u_try0[worst_i] = 0;
        u_trydz[worst_i] = (u_q15[worst_i] > 0)
            ? nc_clamp_i32(cord->deadzone_q15[worst_i], cord->u_min_q15[worst_i], cord->u_max_q15[worst_i])
            : nc_clamp_i32(-cord->deadzone_q15[worst_i], cord->u_min_q15[worst_i], cord->u_max_q15[worst_i]);

        if (nc_active_set_solve4d(cord, dim, target_w_q14, next_mask, u_try0, u_res0, NULL) != NC_OK) {
            for (i = 0u; i < cord->n_actuators; ++i) {
                u_res0[i] = u_try0[i];
            }
        }
        if (nc_active_set_solve4d(cord, dim, target_w_q14, next_mask, u_trydz, u_resdz, NULL) != NC_OK) {
            for (i = 0u; i < cord->n_actuators; ++i) {
                u_resdz[i] = u_trydz[i];
            }
        }

        nc_eval_wrench_sub_q14(cord, dim, u_res0, w0);
        nc_eval_wrench_sub_q14(cord, dim, u_resdz, wdz);
        for (r = 0u; r < dim; ++r) {
            err0 += nc_abs_i32(w0[r] - target_w_q14[r]);
            errdz += nc_abs_i32(wdz[r] - target_w_q14[r]);
        }

        fixed_mask = next_mask;
        for (i = 0u; i < cord->n_actuators; ++i) {
            u_q15[i] = (err0 <= errdz + 16) ? u_res0[i] : u_resdz[i];
        }
    }

    for (i = 0u; i < cord->n_actuators; ++i) {
        int32_t dz = cord->deadzone_q15[i];
        int32_t au = nc_abs_i32(u_q15[i]);
        if (dz > 0 && au > 0 && au < dz) {
            if (au < (dz / 2)) {
                u_q15[i] = 0;
            } else if (u_q15[i] > 0) {
                u_q15[i] = nc_clamp_i32(dz, cord->u_min_q15[i], cord->u_max_q15[i]);
            } else {
                u_q15[i] = nc_clamp_i32(-dz, cord->u_min_q15[i], cord->u_max_q15[i]);
            }
        }
    }
}

int nc_shunting4d_allocate(
    const nc_shunting4d_t *cord,
    const int32_t *wrench_des_q14,
    nc_alloc4d_result_t *out_res
)
{
    int32_t u_cmd[NC_MAX_ACTUATORS];
    uint8_t iters = 0u;
    uint8_t i;
    uint8_t r;
    int rc;

    if (cord == NULL || wrench_des_q14 == NULL || out_res == NULL) {
        return NC_ERR_ARG;
    }

    memset(out_res, 0, sizeof(*out_res));

    if (cord->n_dof == 3u) {
        int32_t w_cur[NC_MAX_DOF];
        int64_t err_l1 = 0;
        int64_t des_l1 = 0;

        rc = nc_active_set_solve4d(cord, 3u, wrench_des_q14, 0u, NULL, u_cmd, &iters);
        if (rc != NC_OK) {
            return rc;
        }
        nc_eval_wrench_sub_q14(cord, 3u, u_cmd, w_cur);
        for (r = 0u; r < 3u; ++r) {
            err_l1 += nc_abs_i32(w_cur[r] - wrench_des_q14[r]);
            des_l1 += nc_abs_i32(wrench_des_q14[r]);
        }

        /* If surge exceeds asymmetric envelope and priority is enabled, protect [Fy, Tz] */
        if (cord->prioritize_attitude && err_l1 > (64 + (des_l1 / 128))) {
            int32_t w_lat[NC_MAX_DOF] = { 0, 0, 0, 0 };
            int32_t w_srg[NC_MAX_DOF] = { 0, 0, 0, 0 };
            int32_t u_lat[NC_MAX_ACTUATORS];
            int32_t u_srg[NC_MAX_ACTUATORS];
            int64_t gs_q20[NC_MAX_DOF * NC_MAX_DOF];
            int64_t xs_q20[NC_MAX_DOF] = { 0, 0, 0, 0 };
            int32_t regs = (cord->lambda_q20 > 0) ? cord->lambda_q20 : 16;
            uint8_t c;

            w_lat[1] = wrench_des_q14[1];
            w_lat[2] = wrench_des_q14[2];
            w_srg[0] = wrench_des_q14[0];

            memset(gs_q20, 0, sizeof(gs_q20));
            for (r = 0u; r < 3u; ++r) {
                for (c = 0u; c < 3u; ++c) {
                    int64_t acc42 = 0;
                    for (i = 0u; i < cord->n_actuators; ++i) {
                        if (cord->shunt_q15[i] > 4 && cord->health_q15[i] > 4) {
                            int64_t h14 = ((int64_t)cord->health_q15[i] + 1) >> 1;
                            int64_t s14 = ((int64_t)cord->shunt_q15[i] + 1) >> 1;
                            int64_t heff_14 = ((h14 * h14) / NC_Q14_ONE * s14) / NC_Q14_ONE;
                            acc42 += (int64_t)cord->W_q14[r][i] * cord->W_q14[c][i] * heff_14;
                        }
                    }
                    gs_q20[r * NC_MAX_DOF + c] = (acc42 / ((int64_t)1 << 22)) + ((r == c) ? regs : 0);
                }
            }

            if (nc_active_set_solve4d(cord, 3u, w_lat, 0u, NULL, u_lat, NULL) == NC_OK &&
                nc_solve_ldlt_q20(3u, gs_q20, (regs >= 8) ? (regs / 2) : 4, w_srg, xs_q20) == NC_OK) {
                int32_t alpha_q15 = NC_Q15_ONE;
                for (i = 0u; i < cord->n_actuators; ++i) {
                    if (cord->shunt_q15[i] <= 4 || cord->health_q15[i] <= 4) {
                        u_srg[i] = 0;
                    } else {
                        int64_t h14 = ((int64_t)cord->health_q15[i] + 1) >> 1;
                        int64_t s14 = ((int64_t)cord->shunt_q15[i] + 1) >> 1;
                        int64_t sh14 = (s14 * h14) / NC_Q14_ONE;
                        int64_t wt_x = (int64_t)cord->W_q14[0][i] * xs_q20[0]
                                     + (int64_t)cord->W_q14[1][i] * xs_q20[1]
                                     + (int64_t)cord->W_q14[2][i] * xs_q20[2];
                        int64_t y_q14 = wt_x / NC_Q20_ONE;
                        int64_t u_q14 = (sh14 * y_q14) / NC_Q14_ONE;
                        u_srg[i] = (int32_t)nc_clamp_i64(
                            (u_q14 * NC_Q15_ONE) / NC_Q14_ONE,
                            -((int64_t)1 << 24),
                            ((int64_t)1 << 24)
                        );
                    }
                }
                for (i = 0u; i < cord->n_actuators; ++i) {
                    int32_t ds = u_srg[i];
                    int32_t ads = nc_abs_i32(ds);
                    if (cord->shunt_q15[i] > 4 && ads > 0) {
                        int32_t rem = (ds > 0)
                            ? (cord->u_max_q15[i] - u_lat[i])
                            : (u_lat[i] - cord->u_min_q15[i]);
                        int32_t cand;
                        if (rem < 0) {
                            rem = 0;
                        }
                        cand = (int32_t)(((int64_t)rem * NC_Q15_ONE) / ads);
                        if (cand < alpha_q15) {
                            alpha_q15 = cand;
                        }
                    }
                }
                for (i = 0u; i < cord->n_actuators; ++i) {
                    int32_t comb = u_lat[i] + (int32_t)(((int64_t)alpha_q15 * u_srg[i]) / NC_Q15_ONE);
                    u_cmd[i] = nc_clamp_i32(comb, cord->u_min_q15[i], cord->u_max_q15[i]);
                }
            }
        }
        nc_apply_deadzone4d(cord, 3u, wrench_des_q14, u_cmd);
    } else {
        /* 4-DOF [Fz, Tx, Ty, Tz] active-set with [Fz, Tx, Ty] attitude priority over Tz */
        int32_t w_cur4[NC_MAX_DOF];
        int32_t fz_err;
        int32_t fz_tol;
        int32_t att_err;
        int32_t att_mag;
        int32_t att_tol;

        rc = nc_active_set_solve4d(cord, 4u, wrench_des_q14, 0u, NULL, u_cmd, &iters);
        if (rc != NC_OK) {
            return rc;
        }
        nc_eval_wrench_sub_q14(cord, 4u, u_cmd, w_cur4);

        fz_err = nc_abs_i32(w_cur4[0] - wrench_des_q14[0]);
        fz_tol = 16 + (nc_abs_i32(wrench_des_q14[0]) / 512);
        att_err = nc_abs_i32(w_cur4[1] - wrench_des_q14[1]) + nc_abs_i32(w_cur4[2] - wrench_des_q14[2]);
        att_mag = nc_abs_i32(wrench_des_q14[1]) + nc_abs_i32(wrench_des_q14[2]);
        att_tol = 4 + (att_mag / 512);

        if (cord->prioritize_attitude && (fz_err > fz_tol || att_err > att_tol)) {
            int32_t u_att[NC_MAX_ACTUATORS];
            uint8_t att_iters = 0u;
            rc = nc_active_set_solve4d(cord, 3u, wrench_des_q14, 0u, NULL, u_att, &att_iters);
            if (rc == NC_OK) {
                int32_t w_after_att[NC_MAX_DOF];
                int32_t e_yaw_q14;
                uint8_t mov_mask = 0u;
                uint8_t mov_cnt = 0u;

                iters = (uint8_t)(iters + att_iters);
                nc_eval_wrench_sub_q14(cord, 4u, u_att, w_after_att);
                e_yaw_q14 = wrench_des_q14[3] - w_after_att[3];

                for (i = 0u; i < cord->n_actuators; ++i) {
                    if (cord->shunt_q15[i] > 4 &&
                        cord->health_q15[i] > 4 &&
                        u_att[i] > (int32_t)cord->u_min_q15[i] + 16 &&
                        u_att[i] < (int32_t)cord->u_max_q15[i] - 16) {
                        mov_mask |= (uint8_t)(1u << i);
                        ++mov_cnt;
                    }
                }

                /* Nullspace yaw recovery using interior rotors without disturbing [Fz, Tx, Ty] */
                if (nc_abs_i32(e_yaw_q14) > 8 && mov_cnt >= 4u) {
                    int64_t g3_q20[NC_MAX_DOF * NC_MAX_DOF];
                    int32_t rhs3_q14[NC_MAX_DOF] = { 0, 0, 0, 0 };
                    int64_t q_q20[NC_MAX_DOF] = { 0, 0, 0, 0 };
                    int32_t reg3 = (cord->lambda_q20 > 0) ? cord->lambda_q20 : 16;
                    uint8_t c;

                    memset(g3_q20, 0, sizeof(g3_q20));
                    for (r = 0u; r < 3u; ++r) {
                        int64_t rhs_acc = 0;
                        for (c = 0u; c < 3u; ++c) {
                            int64_t g_acc42 = 0;
                            int64_t g_acc;
                            for (i = 0u; i < cord->n_actuators; ++i) {
                                if ((mov_mask & (uint8_t)(1u << i)) != 0u) {
                                    int64_t h14 = ((int64_t)cord->health_q15[i] + 1) >> 1;
                                    int64_t s14 = ((int64_t)cord->shunt_q15[i] + 1) >> 1;
                                    int64_t heff_14 = ((h14 * h14) / NC_Q14_ONE * s14) / NC_Q14_ONE;
                                    int64_t ww = (int64_t)cord->W_q14[r][i] * cord->W_q14[c][i];
                                    g_acc42 += ww * heff_14;
                                }
                            }
                            g_acc = g_acc42 / ((int64_t)1 << 22);
                            if (r == c) {
                                g_acc += reg3;
                            }
                            g3_q20[r * NC_MAX_DOF + c] = g_acc;
                        }
                        for (i = 0u; i < cord->n_actuators; ++i) {
                            if ((mov_mask & (uint8_t)(1u << i)) != 0u) {
                                int64_t h14 = ((int64_t)cord->health_q15[i] + 1) >> 1;
                                int64_t s14 = ((int64_t)cord->shunt_q15[i] + 1) >> 1;
                                int64_t heff_14 = ((h14 * h14) / NC_Q14_ONE * s14) / NC_Q14_ONE;
                                int64_t wz = ((int64_t)cord->W_q14[r][i] * cord->W_q14[3][i]) / NC_Q14_ONE;
                                rhs_acc += (wz * heff_14) / NC_Q14_ONE;
                            }
                        }
                        rhs3_q14[r] = (int32_t)rhs_acc;
                    }

                    if (nc_solve_ldlt_q20(3u, g3_q20, (reg3 >= 8) ? (reg3 / 2) : 4, rhs3_q14, q_q20) == NC_OK) {
                        int32_t du_q15[NC_MAX_ACTUATORS];
                        int64_t yaw_gain_q14 = 0;
                        memset(du_q15, 0, sizeof(du_q15));
                        for (i = 0u; i < cord->n_actuators; ++i) {
                            if ((mov_mask & (uint8_t)(1u << i)) != 0u) {
                                int64_t h14 = ((int64_t)cord->health_q15[i] + 1) >> 1;
                                int64_t s14 = ((int64_t)cord->shunt_q15[i] + 1) >> 1;
                                int64_t sh14 = (s14 * h14) / NC_Q14_ONE;
                                int64_t proj_q14 = (
                                    (int64_t)cord->W_q14[0][i] * q_q20[0]
                                    + (int64_t)cord->W_q14[1][i] * q_q20[1]
                                    + (int64_t)cord->W_q14[2][i] * q_q20[2]
                                ) / NC_Q20_ONE;
                                int64_t diff_q14 = (int64_t)cord->W_q14[3][i] - proj_q14;
                                du_q15[i] = (int32_t)(((sh14 * diff_q14) / NC_Q14_ONE * NC_Q15_ONE) / NC_Q14_ONE);
                                yaw_gain_q14 += (
                                    ((int64_t)cord->health_q15[i] * du_q15[i]) / NC_Q15_ONE
                                    * cord->W_q14[3][i]
                                ) / NC_Q15_ONE;
                            }
                        }
                        if (yaw_gain_q14 > 4) {
                            int64_t beta_q14 = ((int64_t)e_yaw_q14 * NC_Q14_ONE) / yaw_gain_q14;
                            for (i = 0u; i < cord->n_actuators; ++i) {
                                if ((mov_mask & (uint8_t)(1u << i)) != 0u && du_q15[i] != 0) {
                                    int64_t d_i = du_q15[i];
                                    if (beta_q14 > 0) {
                                        int64_t rem = (d_i > 0)
                                            ? (cord->u_max_q15[i] - u_att[i])
                                            : (u_att[i] - cord->u_min_q15[i]);
                                        int64_t lim = (rem * NC_Q14_ONE) / ((d_i > 0) ? d_i : -d_i);
                                        if (lim < beta_q14) {
                                            beta_q14 = lim;
                                        }
                                    } else if (beta_q14 < 0) {
                                        int64_t rem = (d_i > 0)
                                            ? (u_att[i] - cord->u_min_q15[i])
                                            : (cord->u_max_q15[i] - u_att[i]);
                                        int64_t lim = -(rem * NC_Q14_ONE) / ((d_i > 0) ? d_i : -d_i);
                                        if (lim > beta_q14) {
                                            beta_q14 = lim;
                                        }
                                    }
                                }
                            }
                            for (i = 0u; i < cord->n_actuators; ++i) {
                                if ((mov_mask & (uint8_t)(1u << i)) != 0u) {
                                    int32_t adj = u_att[i] + (int32_t)((beta_q14 * du_q15[i]) / NC_Q14_ONE);
                                    u_att[i] = nc_clamp_i32(adj, cord->u_min_q15[i], cord->u_max_q15[i]);
                                }
                            }
                        }
                    }
                }
                for (i = 0u; i < cord->n_actuators; ++i) {
                    u_cmd[i] = u_att[i];
                }
            }
        }
        nc_apply_deadzone4d(cord, cord->prioritize_attitude ? 3u : 4u, wrench_des_q14, u_cmd);
    }

    out_res->active_iters = iters;
    for (i = 0u; i < cord->n_actuators; ++i) {
        out_res->health_q15[i] = cord->health_q15[i];
        out_res->shunt_q15[i] = cord->shunt_q15[i];
        if (cord->shunt_q15[i] <= 4) {
            out_res->cmd_q15[i] = 0;
            out_res->isolated_mask |= (uint8_t)(1u << i);
        } else {
            int16_t cl = (int16_t)nc_clamp_i32(u_cmd[i], cord->u_min_q15[i], cord->u_max_q15[i]);
            out_res->cmd_q15[i] = cl;
            if (cl <= cord->u_min_q15[i] + 2 || cl >= cord->u_max_q15[i] - 2) {
                out_res->saturated_mask |= (uint8_t)(1u << i);
            }
        }
    }

    for (r = 0u; r < cord->n_dof; ++r) {
        int64_t w_acc = 0;
        for (i = 0u; i < cord->n_actuators; ++i) {
            int64_t hu_q15 = ((int64_t)cord->health_q15[i] * out_res->cmd_q15[i]) / NC_Q15_ONE;
            w_acc += ((int64_t)cord->W_q14[r][i] * hu_q15) / NC_Q15_ONE;
        }
        out_res->pred_wrench_q14[r] = (int32_t)w_acc;
    }

    return NC_OK;
}

int nc_shunting4d_step(
    nc_shunting4d_t *cord,
    const int32_t *wrench_des_q14,
    const int16_t *feedback_q15,
    uint16_t dt_ms,
    nc_alloc4d_result_t *out_res
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
                int32_t alpha_a = ((int32_t)eff_dt * NC_Q15_ONE)
                                / ((int32_t)cord->tau_act_ms + (int32_t)eff_dt);
                int32_t prev_e = cord->exp_effort_q15[i];
                cord->exp_effort_q15[i] = (int16_t)(
                    prev_e + (((int32_t)cord->last_cmd_q15[i] - prev_e) * alpha_a) / NC_Q15_ONE
                );
            } else {
                cord->exp_effort_q15[i] = cord->last_cmd_q15[i];
            }

            {
                int32_t e_i = cord->exp_effort_q15[i];
                int32_t c_i = cord->last_cmd_q15[i];
                if ((e_i >= 0 && c_i <= 0) || (e_i <= 0 && c_i >= 0)) {
                    u_ref = 0;
                } else {
                    u_ref = (nc_abs_i32(c_i) < nc_abs_i32(e_i)) ? c_i : e_i;
                }
                if (nc_abs_i32(e_i) <= cord->deadband_q15) {
                    u_ref = 0;
                }
            }
            if (nc_abs_i32(u_ref) > (cord->deadband_q15 >> 2)) {
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
        nc_recompute_shunt4d_q15(cord);
    }

    if (cord->probe_interval_ms > 0u) {
        for (i = 0u; i < cord->n_actuators; ++i) {
            if (cord->shunt_q15[i] <= 4) {
                uint32_t t = (uint32_t)cord->isolated_ms[i] + (uint32_t)dt_ms;
                cord->isolated_ms[i] = (uint16_t)((t > 65535u) ? 65535u : t);
                if (cord->isolated_ms[i] >= cord->probe_interval_ms) {
                    (void)nc_shunting4d_reinstate(cord, i);
                }
            } else {
                cord->isolated_ms[i] = 0u;
            }
        }
    }

    rc = nc_shunting4d_allocate(cord, wrench_des_q14, out_res);
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

/* =========================================================================
 * 5. Sensorless ESC RPM + Current Proprioceptive Fault Observer
 * ========================================================================= */

int nc_esc_observer_init(
    nc_esc_observer_t *obs,
    uint8_t n_actuators,
    int32_t rpm_max,
    int32_t i_idle_ma,
    int32_t k_load_ma
)
{
    uint8_t i;
    if (obs == NULL || n_actuators == 0u || n_actuators > NC_MAX_ACTUATORS ||
        rpm_max <= 0 || i_idle_ma < 0 || k_load_ma <= 0) {
        return NC_ERR_ARG;
    }
    memset(obs, 0, sizeof(*obs));
    obs->n_actuators = n_actuators;
    obs->rpm_max = rpm_max;
    obs->i_idle_ma = i_idle_ma;
    obs->k_load_ma = k_load_ma;
    obs->overcurrent_ratio_q8 = 410u; /* 1.60 * 256 */
    obs->broken_ratio_q15 = 14745;    /* 0.45 */
    obs->stall_ratio_q15 = 16384;     /* 0.50 */
    obs->deadband_q15 = 1638;         /* 0.05 */
    obs->tau_motor_ms = 15u;
    obs->tau_fault_ms = 16u;
    obs->tau_recover_ms = 600u;
    for (i = 0u; i < n_actuators; ++i) {
        obs->health_q15[i] = NC_Q15_ONE;
    }
    return NC_OK;
}

int nc_esc_observer_update(
    nc_esc_observer_t *obs,
    const int16_t *cmd_q15,
    const int32_t *rpm_meas,
    const int32_t *current_ma,
    uint16_t dt_ms,
    nc_esc_obs_result_t *out_res
)
{
    uint16_t eff_dt;
    int32_t alpha_fast_q15;
    int32_t alpha_slow_q15;
    int32_t min_dyn_ma;
    uint8_t i;

    if (obs == NULL || cmd_q15 == NULL || rpm_meas == NULL || current_ma == NULL || out_res == NULL) {
        return NC_ERR_ARG;
    }

    memset(out_res, 0, sizeof(*out_res));
    eff_dt = (dt_ms == 0u) ? 1u : dt_ms;
    alpha_fast_q15 = ((int32_t)eff_dt * NC_Q15_ONE)
                   / ((int32_t)(obs->tau_fault_ms ? obs->tau_fault_ms : 1u) + (int32_t)eff_dt);
    alpha_slow_q15 = ((int32_t)eff_dt * NC_Q15_ONE)
                   / ((int32_t)(obs->tau_recover_ms ? obs->tau_recover_ms : 1u) + (int32_t)eff_dt);
    min_dyn_ma = obs->k_load_ma / 50;
    if (min_dyn_ma < 150) {
        min_dyn_ma = 150;
    }

    for (i = 0u; i < obs->n_actuators; ++i) {
        int32_t u_ref;
        int32_t abs_u;
        int32_t sign_u;
        uint32_t sqrt_u_q15;
        int32_t w_exp;
        int32_t abs_rpm;
        int32_t norm_rpm_q15;
        int32_t r2_q15;
        int32_t r3_q15;
        int32_t i_dyn_exp_ma;
        int32_t i_exp_ma;
        int32_t t_rpm_q15;

        if (!obs->initialized || obs->tau_motor_ms == 0u) {
            obs->lagged_cmd_q15[i] = cmd_q15[i];
        } else {
            int32_t alpha_m = ((int32_t)eff_dt * NC_Q15_ONE)
                            / ((int32_t)obs->tau_motor_ms + (int32_t)eff_dt);
            int32_t prev = obs->lagged_cmd_q15[i];
            obs->lagged_cmd_q15[i] = (int16_t)(
                prev + (((int32_t)cmd_q15[i] - prev) * alpha_m) / NC_Q15_ONE
            );
        }

        u_ref = obs->lagged_cmd_q15[i];
        abs_u = nc_abs_i32(u_ref);
        sign_u = (u_ref < 0) ? -1 : ((u_ref > 0) ? +1 : 0);

        /* Expected RPM = rpm_max * sqrt(|u_ref| / 32767) */
        sqrt_u_q15 = nc_isqrt_u64((uint64_t)(uint32_t)abs_u * (uint64_t)NC_Q15_ONE);
        w_exp = (int32_t)(((int64_t)obs->rpm_max * (int64_t)sqrt_u_q15) / NC_Q15_ONE);
        out_res->exp_rpm[i] = sign_u * w_exp;

        abs_rpm = nc_abs_i32(rpm_meas[i]);
        norm_rpm_q15 = (int32_t)nc_clamp_i64(
            ((int64_t)abs_rpm * NC_Q15_ONE) / obs->rpm_max, 0, 2 * NC_Q15_ONE
        );
        r2_q15 = (int32_t)(((int64_t)norm_rpm_q15 * norm_rpm_q15) / NC_Q15_ONE);
        r3_q15 = (int32_t)(((int64_t)r2_q15 * norm_rpm_q15) / NC_Q15_ONE);

        i_dyn_exp_ma = (int32_t)(((int64_t)obs->k_load_ma * r3_q15) / NC_Q15_ONE);
        i_exp_ma = obs->i_idle_ma + i_dyn_exp_ma;
        out_res->exp_current_ma[i] = i_exp_ma;

        if ((rpm_meas[i] > (obs->rpm_max / 500) && u_ref < 0) ||
            (rpm_meas[i] < -(obs->rpm_max / 500) && u_ref > 0)) {
            t_rpm_q15 = 0;
        } else {
            t_rpm_q15 = nc_clamp_i32(r2_q15, 0, 2 * NC_Q15_ONE);
        }

        if (abs_u <= obs->deadband_q15) {
            out_res->est_thrust_q15[i] = (int16_t)nc_clamp_i32(
                sign_u * t_rpm_q15, -NC_Q15_ONE, NC_Q15_ONE
            );
        } else {
            int32_t denom_w = (w_exp > 1) ? w_exp : 1;
            int32_t r_omega_q15 = (int32_t)nc_clamp_i64(
                ((int64_t)abs_rpm * NC_Q15_ONE) / denom_w, 0, 2 * NC_Q15_ONE
            );
            int32_t curr_ma = (current_ma[i] > 0) ? current_ma[i] : 0;
            int32_t cmd_r2_q15 = abs_u;
            int32_t cmd_r3_q15 = (int32_t)(((int64_t)cmd_r2_q15 * (int64_t)sqrt_u_q15) / NC_Q15_ONE);
            int32_t i_cmd_exp_ma = obs->i_idle_ma
                                 + (int32_t)(((int64_t)obs->k_load_ma * cmd_r3_q15) / NC_Q15_ONE);

            int64_t oc_lim_actual = ((int64_t)i_exp_ma * obs->overcurrent_ratio_q8) / 256;
            int64_t oc_lim_cmd = ((int64_t)i_cmd_exp_ma * obs->overcurrent_ratio_q8) / 256;
            uint8_t is_oc = ((curr_ma > oc_lim_actual && r_omega_q15 < 31128) || (curr_ma > oc_lim_cmd)) ? 1u : 0u;
            uint8_t is_broken = 0u;
            uint8_t is_stall = 0u;
            int32_t coupling_q15 = NC_Q15_ONE;

            if (is_oc) {
                int32_t safe_exp = (i_exp_ma > 10) ? i_exp_ma : 10;
                int32_t excess_q10 = (int32_t)nc_clamp_i64(
                    (((int64_t)(curr_ma - safe_exp)) * 1024) / safe_exp, 0, 16384
                );
                int64_t pen_q10 = 1024 + (((int64_t)5 * excess_q10 * excess_q10) / 2048);
                coupling_q15 = (int32_t)nc_clamp_i64(((int64_t)NC_Q15_ONE * 1024) / pen_q10, 1310, NC_Q15_ONE);
            } else if (r_omega_q15 < obs->stall_ratio_q15) {
                is_stall = 1u;
                coupling_q15 = NC_Q15_ONE;
            } else if (i_dyn_exp_ma >= min_dyn_ma) {
                int32_t net_curr = (curr_ma > obs->i_idle_ma) ? (curr_ma - obs->i_idle_ma) : 0;
                int32_t eta_q15 = (int32_t)nc_clamp_i64(
                    ((int64_t)net_curr * NC_Q15_ONE) / i_dyn_exp_ma, 0, 2 * NC_Q15_ONE
                );
                if (eta_q15 < obs->broken_ratio_q15) {
                    int32_t knee = (obs->broken_ratio_q15 > 19660) ? obs->broken_ratio_q15 : 19660;
                    is_broken = 1u;
                    coupling_q15 = (int32_t)(((int64_t)eta_q15 * NC_Q15_ONE) / knee);
                } else if (eta_q15 < 21299) {
                    coupling_q15 = (int32_t)(((int64_t)eta_q15 * NC_Q15_ONE) / 21299);
                }
            }

            {
                int32_t eff_mag_q15 = (int32_t)(((int64_t)t_rpm_q15 * coupling_q15) / NC_Q15_ONE);
                int32_t inst_h_q15 = (int32_t)nc_clamp_i64(
                    ((int64_t)eff_mag_q15 * NC_Q15_ONE) / abs_u, 0, NC_Q15_ONE
                );
                int32_t ah = (inst_h_q15 < obs->health_q15[i]) ? alpha_fast_q15 : alpha_slow_q15;

                out_res->est_thrust_q15[i] = (int16_t)nc_clamp_i32(
                    sign_u * eff_mag_q15, -NC_Q15_ONE, NC_Q15_ONE
                );
                obs->health_q15[i] = (int16_t)nc_clamp_i32(
                    obs->health_q15[i] + (((inst_h_q15 - obs->health_q15[i]) * ah) / NC_Q15_ONE),
                    0,
                    NC_Q15_ONE
                );

                obs->overcurr_score_q15[i] = (int16_t)nc_clamp_i32(
                    obs->overcurr_score_q15[i]
                    + ((((is_oc ? NC_Q15_ONE : 0) - obs->overcurr_score_q15[i])
                        * (is_oc ? alpha_fast_q15 : alpha_slow_q15)) / NC_Q15_ONE),
                    0,
                    NC_Q15_ONE
                );
                obs->broken_score_q15[i] = (int16_t)nc_clamp_i32(
                    obs->broken_score_q15[i]
                    + ((((is_broken ? NC_Q15_ONE : 0) - obs->broken_score_q15[i])
                        * (is_broken ? alpha_fast_q15 : alpha_slow_q15)) / NC_Q15_ONE),
                    0,
                    NC_Q15_ONE
                );
                obs->stall_score_q15[i] = (int16_t)nc_clamp_i32(
                    obs->stall_score_q15[i]
                    + ((((is_stall ? NC_Q15_ONE : 0) - obs->stall_score_q15[i])
                        * (is_stall ? alpha_fast_q15 : alpha_slow_q15)) / NC_Q15_ONE),
                    0,
                    NC_Q15_ONE
                );
            }
        }

        out_res->health_q15[i] = obs->health_q15[i];
        if (obs->overcurr_score_q15[i] >= 14745) {
            out_res->overcurrent_mask |= (uint8_t)(1u << i);
            out_res->fault_code[i] = (uint8_t)NC_ESC_FAULT_OVERCURR;
        } else if (obs->broken_score_q15[i] >= 14745) {
            out_res->broken_prop_mask |= (uint8_t)(1u << i);
            out_res->fault_code[i] = (uint8_t)NC_ESC_FAULT_BROKEN;
        } else if (obs->stall_score_q15[i] >= 14745 || obs->health_q15[i] < 14745) {
            out_res->stall_mask |= (uint8_t)(1u << i);
            out_res->fault_code[i] = (uint8_t)NC_ESC_FAULT_STALL_OPEN;
        } else {
            out_res->fault_code[i] = (uint8_t)NC_ESC_OK;
        }
    }

    obs->initialized = 1u;
    return NC_OK;
}

/* =========================================================================
 * 6. Exact 3-DOF 18-Servo Hexapod Leg Inverse Kinematics
 * ========================================================================= */

void nc_hexapod_ik_default_cfg(nc_hexapod_ik_cfg_t *cfg)
{
    if (cfg == NULL) {
        return;
    }
    cfg->l_coxa_mm = 38;
    cfg->l_femur_mm = 95;
    cfg->l_tibia_mm = 135;
    cfg->body_z_mm = 85;
    cfg->swing_height_mm = 30;
    cfg->park_z_mm = 45;
}

static int16_t nc_bam_signed_to_cdeg(int16_t signed_bam)
{
    int32_t b = (int32_t)signed_bam;
    if (b >= 0) {
        return (int16_t)((b * 18000 + 16384) / 32768);
    }
    return (int16_t)(-(((-b) * 18000 + 16384) / 32768));
}

static uint16_t nc_cdeg_to_pwm_us(int16_t cdeg)
{
    int32_t c = (int32_t)cdeg;
    int32_t delta_us;
    if (c >= 0) {
        delta_us = (c * 500 + 4500) / 9000;
    } else {
        delta_us = -(((-c) * 500 + 4500) / 9000);
    }
    return (uint16_t)nc_clamp_i32(1500 + delta_us, 1000, 2000);
}

int nc_hexapod_ik_3dof_single(
    const nc_hexapod_ik_cfg_t *cfg,
    uint8_t limb_idx,
    int16_t foot_x_mm,
    int16_t foot_y_mm,
    int16_t foot_z_mm,
    int16_t out_cdeg[3],
    uint16_t out_pwm_us[3],
    uint8_t *out_reachable
)
{
    int32_t dx10;
    int32_t dy10;
    int32_t dz10;
    int32_t lc10;
    int32_t lf10;
    int32_t lt10;
    uint16_t az_bam;
    int16_t coxa_bam;
    uint32_t r_horiz10;
    int32_t r_fem10;
    uint32_t d_raw10;
    int32_t d_min10;
    int32_t d_max10;
    int32_t d10;
    uint8_t reachable = 1u;
    uint16_t gamma_bam;
    int64_t num_b1;
    int64_t den_b1;
    int16_t cos_b1_q15;
    uint16_t beta1_bam;
    int16_t femur_bam;
    int64_t num_b2;
    int64_t den_b2;
    int16_t cos_b2_q15;
    uint16_t beta2_bam;
    int16_t tibia_bam;
    uint8_t k;

    if (cfg == NULL || out_cdeg == NULL || out_pwm_us == NULL || limb_idx >= NC_NUM_LIMBS) {
        return NC_ERR_ARG;
    }
    if (cfg->l_coxa_mm <= 0 || cfg->l_femur_mm <= 0 || cfg->l_tibia_mm <= 0 || cfg->body_z_mm <= 0) {
        return NC_ERR_RANGE;
    }

    /* Compute in 0.1 mm internal units for 0.08 deg fixed-point accuracy */
    dx10 = ((int32_t)foot_x_mm - (int32_t)k_hip_x_mm[limb_idx]) * 10;
    dy10 = ((int32_t)foot_y_mm - (int32_t)k_hip_y_mm[limb_idx]) * 10;
    dz10 = ((int32_t)foot_z_mm - (int32_t)cfg->body_z_mm) * 10;

    lc10 = (int32_t)cfg->l_coxa_mm * 10;
    lf10 = (int32_t)cfg->l_femur_mm * 10;
    lt10 = (int32_t)cfg->l_tibia_mm * 10;

    az_bam = nc_cord_atan2_bam(dy10, dx10);
    coxa_bam = (int16_t)(uint16_t)(az_bam - k_mount_bam[limb_idx]);

    r_horiz10 = nc_isqrt_u64((uint64_t)((int64_t)dx10 * dx10 + (int64_t)dy10 * dy10));
    r_fem10 = (int32_t)r_horiz10 - lc10;
    if (r_fem10 < 10) {
        r_fem10 = 10;
        reachable = 0u;
    }

    d_raw10 = nc_isqrt_u64((uint64_t)((int64_t)r_fem10 * r_fem10 + (int64_t)dz10 * dz10));
    d_min10 = nc_abs_i32(lf10 - lt10) + 5;
    d_max10 = (lf10 + lt10) - 5;
    if ((int32_t)d_raw10 < d_min10 || (int32_t)d_raw10 > d_max10) {
        reachable = 0u;
    }
    d10 = nc_clamp_i32((int32_t)d_raw10, d_min10, d_max10);

    gamma_bam = nc_cord_atan2_bam(dz10, r_fem10);

    num_b1 = (int64_t)lf10 * lf10 + (int64_t)d10 * d10 - (int64_t)lt10 * lt10;
    den_b1 = (int64_t)2 * lf10 * d10;
    cos_b1_q15 = (int16_t)nc_clamp_i64((num_b1 * NC_Q15_ONE) / den_b1, -NC_Q15_ONE, NC_Q15_ONE);
    beta1_bam = nc_acos_q15_bam(cos_b1_q15);
    femur_bam = (int16_t)(uint16_t)(gamma_bam + beta1_bam);

    num_b2 = (int64_t)lf10 * lf10 + (int64_t)lt10 * lt10 - (int64_t)d10 * d10;
    den_b2 = (int64_t)2 * lf10 * lt10;
    cos_b2_q15 = (int16_t)nc_clamp_i64((num_b2 * NC_Q15_ONE) / den_b2, -NC_Q15_ONE, NC_Q15_ONE);
    beta2_bam = nc_acos_q15_bam(cos_b2_q15);
    tibia_bam = (int16_t)(uint16_t)(beta2_bam - 16384u); /* 0 deg = 90 deg perpendicular knee */

    out_cdeg[0] = nc_bam_signed_to_cdeg(coxa_bam);
    out_cdeg[1] = nc_bam_signed_to_cdeg(femur_bam);
    out_cdeg[2] = nc_bam_signed_to_cdeg(tibia_bam);

    for (k = 0u; k < 3u; ++k) {
        out_pwm_us[k] = nc_cdeg_to_pwm_us(out_cdeg[k]);
    }
    if (out_reachable != NULL) {
        *out_reachable = reachable;
    }
    return NC_OK;
}

int nc_hexapod_ik_3dof(
    const nc_hexapod_ik_cfg_t *cfg,
    const nc_cpg_cord_t *cord,
    const nc_cpg_state_t *cpg_state,
    nc_hexapod_ik_out_t *out_ik
)
{
    nc_hexapod_ik_cfg_t def_cfg;
    const nc_hexapod_ik_cfg_t *eff_cfg;
    uint8_t i;

    if (cord == NULL || cpg_state == NULL || out_ik == NULL) {
        return NC_ERR_ARG;
    }

    if (cfg == NULL) {
        nc_hexapod_ik_default_cfg(&def_cfg);
        eff_cfg = &def_cfg;
    } else {
        eff_cfg = cfg;
    }

    memset(out_ik, 0, sizeof(*out_ik));
    for (i = 0u; i < NC_NUM_LIMBS; ++i) {
        int16_t fx;
        int16_t fy;
        int16_t fz;
        int16_t cdeg[3];
        uint16_t pwm[3];
        uint8_t ok = 0u;
        int rc;

        if (cord->limb_health_q15[i] < cord->fault_thresh_q15) {
            fx = k_park_x_mm[i];
            fy = k_park_y_mm[i];
            fz = eff_cfg->park_z_mm;
            out_ik->parked_mask |= (uint8_t)(1u << i);
        } else if ((cpg_state->stance_mask & (uint8_t)(1u << i)) != 0u) {
            fx = cpg_state->foot_x_mm[i];
            fy = cpg_state->foot_y_mm[i];
            fz = 0;
        } else {
            fx = cpg_state->foot_x_mm[i];
            fy = cpg_state->foot_y_mm[i];
            fz = (int16_t)(
                ((int32_t)eff_cfg->swing_height_mm * (int32_t)cpg_state->femur_lift_q15[i]
                 + (NC_Q15_ONE / 2)) / NC_Q15_ONE
            );
        }

        rc = nc_hexapod_ik_3dof_single(eff_cfg, i, fx, fy, fz, cdeg, pwm, &ok);
        if (rc != NC_OK) {
            return rc;
        }
        out_ik->coxa_cdeg[i] = cdeg[0];
        out_ik->femur_cdeg[i] = cdeg[1];
        out_ik->tibia_cdeg[i] = cdeg[2];
        out_ik->pwm_us[i][0] = pwm[0];
        out_ik->pwm_us[i][1] = pwm[1];
        out_ik->pwm_us[i][2] = pwm[2];
        out_ik->foot_z_mm[i] = fz;
        if (ok) {
            out_ik->reachable_mask |= (uint8_t)(1u << i);
        }
    }

    return NC_OK;
}
