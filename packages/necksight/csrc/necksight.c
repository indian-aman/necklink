/*
 * necksight.c - Zero-heap, fixed-point integer-only ANSI C99 Optic Lobe engine
 *
 * Pure integer pipeline (int32_t / uint16_t / uint8_t) on a fixed 32x32 grid.
 * Author: Amandeep Singh Bains
 * License: MIT
 */

#include "necksight.h"
#include <string.h>

/* 16-entry Q8 fractional log2 table: round(256 * log2(1 + k/16)) */
static const uint8_t LOG2_FRAC_Q8[16] = {
    0, 18, 35, 51, 66, 81, 95, 108,
    121, 134, 146, 157, 169, 180, 190, 201
};

/* Equatorial HS tangential cell vertical weights in Q7 (mean ~ 128) */
static const uint8_t HS_WEIGHT_Y_Q7[NECKSIGHT_GRID] = {
    38, 46, 56, 66, 78, 91, 104, 118,
    132, 146, 159, 171, 181, 190, 196, 200,
    200, 196, 190, 181, 171, 159, 146, 132,
    118, 104, 91, 78, 66, 56, 46, 38
};

#define LPLC2_GRID_DIM 5
#define LPLC2_NUM_SCALES 3
#define LPLC2_UNITS (LPLC2_GRID_DIM * LPLC2_GRID_DIM * LPLC2_NUM_SCALES)

static const uint8_t LPLC2_CENTERS[LPLC2_GRID_DIM] = { 6, 11, 16, 21, 25 };
static const uint8_t LPLC2_RADII[LPLC2_NUM_SCALES] = { 3, 6, 10 };

uint16_t necksight_ilog2_q8(uint32_t v)
{
    uint32_t msb = 0;
    uint32_t tmp = v;
    uint32_t frac;

    if (v == 0U) {
        return 0U;
    }
    if (tmp >= 65536U) { tmp >>= 16; msb += 16U; }
    if (tmp >= 256U)   { tmp >>= 8;  msb += 8U;  }
    if (tmp >= 16U)    { tmp >>= 4;  msb += 4U;  }
    if (tmp >= 4U)     { tmp >>= 2;  msb += 2U;  }
    if (tmp >= 2U)     { msb += 1U; }

    if (msb >= 4U) {
        frac = (v >> (msb - 4U)) & 0x0FU;
    } else {
        frac = (v << (4U - msb)) & 0x0FU;
    }
    return (uint16_t)((msb << 8) | (uint16_t)LOG2_FRAC_Q8[frac]);
}

uint32_t necksight_isqrt_u32(uint32_t n)
{
    uint32_t res = 0U;
    uint32_t bit = 1UL << 30;

    while (bit > n) {
        bit >>= 2;
    }
    while (bit != 0U) {
        if (n >= res + bit) {
            n -= res + bit;
            res = (res >> 1) + bit;
        } else {
            res >>= 1;
        }
        bit >>= 2;
    }
    return res;
}

void necksight_init_custom(necksight_state_t *st, uint16_t threshold_q10)
{
    if (st == NULL) {
        return;
    }
    memset(st, 0, sizeof(*st));
    st->threshold_q10 = threshold_q10;
}

void necksight_init(necksight_state_t *st)
{
    necksight_init_custom(st, (uint16_t)NECKSIGHT_DEFAULT_THRESHOLD_Q10);
}

void necksight_reset(necksight_state_t *st)
{
    uint16_t thr;
    if (st == NULL) {
        return;
    }
    thr = st->threshold_q10;
    memset(st, 0, sizeof(*st));
    st->threshold_q10 = thr;
}

void necksight_downsample_u8(
    const uint8_t *src,
    uint16_t src_h,
    uint16_t src_w,
    uint16_t src_stride,
    uint16_t dst_q4[NECKSIGHT_PIXELS]
)
{
    uint16_t gy, gx;
    if (src == NULL || dst_q4 == NULL || src_h == 0U || src_w == 0U) {
        return;
    }
    if (src_stride < src_w) {
        src_stride = src_w;
    }

    for (gy = 0U; gy < (uint16_t)NECKSIGHT_GRID; gy++) {
        uint32_t y0 = ((uint32_t)gy * (uint32_t)src_h) / (uint32_t)NECKSIGHT_GRID;
        uint32_t y1 = (((uint32_t)gy + 1U) * (uint32_t)src_h) / (uint32_t)NECKSIGHT_GRID;
        if (y1 <= y0) {
            y1 = y0 + 1U;
        }
        if (y1 > (uint32_t)src_h) {
            y1 = (uint32_t)src_h;
        }

        for (gx = 0U; gx < (uint16_t)NECKSIGHT_GRID; gx++) {
            uint32_t x0 = ((uint32_t)gx * (uint32_t)src_w) / (uint32_t)NECKSIGHT_GRID;
            uint32_t x1 = (((uint32_t)gx + 1U) * (uint32_t)src_w) / (uint32_t)NECKSIGHT_GRID;
            uint32_t sum = 0U;
            uint32_t count;
            uint32_t y, x;

            if (x1 <= x0) {
                x1 = x0 + 1U;
            }
            if (x1 > (uint32_t)src_w) {
                x1 = (uint32_t)src_w;
            }

            for (y = y0; y < y1; y++) {
                const uint8_t *row = src + y * (uint32_t)src_stride;
                for (x = x0; x < x1; x++) {
                    sum += (uint32_t)row[x];
                }
            }
            count = (y1 - y0) * (x1 - x0);
            dst_q4[gy * NECKSIGHT_GRID + gx] = (uint16_t)(((sum << 4) + (count >> 1)) / count);
        }
    }
}

static void insertion_sort_i16(int16_t *arr, int n)
{
    int i, j;
    for (i = 1; i < n; i++) {
        int16_t key = arr[i];
        j = i - 1;
        while (j >= 0 && arr[j] > key) {
            arr[j + 1] = arr[j];
            j--;
        }
        arr[j + 1] = key;
    }
}

static uint32_t emd_3arm_pair(
    uint16_t f_c,
    uint16_t d_c,
    uint16_t f_back,
    uint16_t d_back,
    uint16_t d_front
)
{
    int32_t hr = (int32_t)d_back * (int32_t)f_c - (((int32_t)d_c * (int32_t)f_back * 11) >> 3);
    int32_t denom;
    if (hr <= 0) {
        return 0U;
    }
    denom = 2048 + 12 * (int32_t)d_front;
    return (uint32_t)(((uint32_t)hr * 16U) / (uint32_t)denom);
}

static void eval_pixel_x(
    const uint16_t *f_on,
    const uint16_t *d_on,
    const uint16_t *f_off,
    const uint16_t *d_off,
    int y,
    int x,
    uint32_t *out_px,
    uint32_t *out_nx,
    uint32_t *out_t4,
    uint32_t *out_t5
)
{
    int idx = y * NECKSIGHT_GRID + x;
    uint32_t px_on = 0U, nx_on = 0U, px_off = 0U, nx_off = 0U;

    if (x >= 1 && x < NECKSIGHT_GRID - 1) {
        px_on  += emd_3arm_pair(f_on[idx],  d_on[idx],  f_on[idx - 1],  d_on[idx - 1],  d_on[idx + 1]) * 2U;
        nx_on  += emd_3arm_pair(f_on[idx],  d_on[idx],  f_on[idx + 1],  d_on[idx + 1],  d_on[idx - 1]) * 2U;
        px_off += emd_3arm_pair(f_off[idx], d_off[idx], f_off[idx - 1], d_off[idx - 1], d_off[idx + 1]) * 2U;
        nx_off += emd_3arm_pair(f_off[idx], d_off[idx], f_off[idx + 1], d_off[idx + 1], d_off[idx - 1]) * 2U;
    }
    if (x >= 2 && x < NECKSIGHT_GRID - 2) {
        px_on  += emd_3arm_pair(f_on[idx],  d_on[idx],  f_on[idx - 2],  d_on[idx - 2],  d_on[idx + 2]);
        nx_on  += emd_3arm_pair(f_on[idx],  d_on[idx],  f_on[idx + 2],  d_on[idx + 2],  d_on[idx - 2]);
        px_off += emd_3arm_pair(f_off[idx], d_off[idx], f_off[idx - 2], d_off[idx - 2], d_off[idx + 2]);
        nx_off += emd_3arm_pair(f_off[idx], d_off[idx], f_off[idx + 2], d_off[idx + 2], d_off[idx - 2]);
    }

    px_on  /= 3U;
    nx_on  /= 3U;
    px_off /= 3U;
    nx_off /= 3U;

    *out_px = px_on + px_off;
    *out_nx = nx_on + nx_off;
    *out_t4 = px_on + nx_on;
    *out_t5 = px_off + nx_off;
}

static void eval_pixel_y(
    const uint16_t *f_on,
    const uint16_t *d_on,
    const uint16_t *f_off,
    const uint16_t *d_off,
    int y,
    int x,
    uint32_t *out_py,
    uint32_t *out_ny,
    uint32_t *out_t4,
    uint32_t *out_t5
)
{
    int idx = y * NECKSIGHT_GRID + x;
    int s1 = NECKSIGHT_GRID;
    int s2 = 2 * NECKSIGHT_GRID;
    uint32_t py_on = 0U, ny_on = 0U, py_off = 0U, ny_off = 0U;

    if (y >= 1 && y < NECKSIGHT_GRID - 1) {
        py_on  += emd_3arm_pair(f_on[idx],  d_on[idx],  f_on[idx - s1],  d_on[idx - s1],  d_on[idx + s1]) * 2U;
        ny_on  += emd_3arm_pair(f_on[idx],  d_on[idx],  f_on[idx + s1],  d_on[idx + s1],  d_on[idx - s1]) * 2U;
        py_off += emd_3arm_pair(f_off[idx], d_off[idx], f_off[idx - s1], d_off[idx - s1], d_off[idx + s1]) * 2U;
        ny_off += emd_3arm_pair(f_off[idx], d_off[idx], f_off[idx + s1], d_off[idx + s1], d_off[idx - s1]) * 2U;
    }
    if (y >= 2 && y < NECKSIGHT_GRID - 2) {
        py_on  += emd_3arm_pair(f_on[idx],  d_on[idx],  f_on[idx - s2],  d_on[idx - s2],  d_on[idx + s2]);
        ny_on  += emd_3arm_pair(f_on[idx],  d_on[idx],  f_on[idx + s2],  d_on[idx + s2],  d_on[idx - s2]);
        py_off += emd_3arm_pair(f_off[idx], d_off[idx], f_off[idx - s2], d_off[idx - s2], d_off[idx + s2]);
        ny_off += emd_3arm_pair(f_off[idx], d_off[idx], f_off[idx + s2], d_off[idx + s2], d_off[idx - s2]);
    }

    py_on  /= 3U;
    ny_on  /= 3U;
    py_off /= 3U;
    ny_off /= 3U;

    *out_py = py_on + py_off;
    *out_ny = ny_on + ny_off;
    *out_t4 = py_on + ny_on;
    *out_t5 = py_off + ny_off;
}

static int32_t query_sat_mean_i32(
    const int32_t sat[NECKSIGHT_SAT_LEN],
    int y0,
    int y1,
    int x0,
    int x1
)
{
    int32_t sum;
    int32_t area;

    if (y0 < 0) y0 = 0;
    if (x0 < 0) x0 = 0;
    if (y1 > NECKSIGHT_GRID) y1 = NECKSIGHT_GRID;
    if (x1 > NECKSIGHT_GRID) x1 = NECKSIGHT_GRID;
    if (y1 <= y0 || x1 <= x0) {
        return 0;
    }

    sum = sat[y1 * NECKSIGHT_SAT_STRIDE + x1]
        - sat[y0 * NECKSIGHT_SAT_STRIDE + x1]
        - sat[y1 * NECKSIGHT_SAT_STRIDE + x0]
        + sat[y0 * NECKSIGHT_SAT_STRIDE + x0];
    area = (int32_t)((y1 - y0) * (x1 - x0));
    return sum / area;
}

void necksight_step_u16(
    necksight_state_t *st,
    const uint16_t frame_32x32[NECKSIGHT_PIXELS],
    uint16_t dt_ms,
    necksight_result_t *out
)
{
    int y, x, i;
    int16_t med_samples[64];
    int32_t g_step;

    int32_t sum_vx = 0;
    int32_t sum_vy = 0;
    int32_t sum_yaw_q7 = 0;
    int32_t sum_rad_q5 = 0;
    uint32_t total_motion = 0U;
    uint32_t t4_total = 0U;
    uint32_t t5_total = 0U;

    uint16_t ql_arr[LPLC2_UNITS];
    uint16_t qr_arr[LPLC2_UNITS];
    uint16_t qu_arr[LPLC2_UNITS];
    uint16_t qd_arr[LPLC2_UNITS];

    uint32_t best_sym = 0U;
    int best_idx = 0;
    uint8_t best_cx = 16U;
    uint8_t best_cy = 16U;
    uint8_t best_r = LPLC2_RADII[0];

    uint32_t pan_mag;
    uint32_t gated_score;

    if (st == NULL || frame_32x32 == NULL || out == NULL) {
        return;
    }
    if (dt_ms == 0U) {
        dt_ms = 16U;
    }

    memset(out, 0, sizeof(*out));
    out->est_ttc_ms = (uint16_t)NECKSIGHT_TTC_IDLE_MS;
    out->focus_x = 16U;
    out->focus_y = 16U;

    /* 1. Separable [1, 2, 1] / 4 optical acceptance blur using f_on/f_off scratch */
    for (y = 0; y < NECKSIGHT_GRID; y++) {
        const uint16_t *row = frame_32x32 + y * NECKSIGHT_GRID;
        uint16_t *tmp_row = st->f_on + y * NECKSIGHT_GRID;
        tmp_row[0] = (uint16_t)((3U * (uint32_t)row[0] + (uint32_t)row[1] + 2U) >> 2);
        for (x = 1; x < NECKSIGHT_GRID - 1; x++) {
            tmp_row[x] = (uint16_t)(
                ((uint32_t)row[x - 1] + 2U * (uint32_t)row[x] + (uint32_t)row[x + 1] + 2U) >> 2
            );
        }
        tmp_row[NECKSIGHT_GRID - 1] = (uint16_t)(
            ((uint32_t)row[NECKSIGHT_GRID - 2] + 3U * (uint32_t)row[NECKSIGHT_GRID - 1] + 2U) >> 2
        );
    }

    for (x = 0; x < NECKSIGHT_GRID; x++) {
        st->f_off[x] = (uint16_t)(
            (3U * (uint32_t)st->f_on[x] + (uint32_t)st->f_on[NECKSIGHT_GRID + x] + 2U) >> 2
        );
        for (y = 1; y < NECKSIGHT_GRID - 1; y++) {
            st->f_off[y * NECKSIGHT_GRID + x] = (uint16_t)(
                ((uint32_t)st->f_on[(y - 1) * NECKSIGHT_GRID + x]
               + 2U * (uint32_t)st->f_on[y * NECKSIGHT_GRID + x]
               + (uint32_t)st->f_on[(y + 1) * NECKSIGHT_GRID + x] + 2U) >> 2
            );
        }
        st->f_off[(NECKSIGHT_GRID - 1) * NECKSIGHT_GRID + x] = (uint16_t)(
            ((uint32_t)st->f_on[(NECKSIGHT_GRID - 2) * NECKSIGHT_GRID + x]
           + 3U * (uint32_t)st->f_on[(NECKSIGHT_GRID - 1) * NECKSIGHT_GRID + x] + 2U) >> 2
        );
    }

    /* 2. R1-R6 photoreceptor integer log2 compression (with dark offset +16) */
    for (i = 0; i < NECKSIGHT_PIXELS; i++) {
        st->f_on[i] = necksight_ilog2_q8((uint32_t)st->f_off[i] + 16U);
    }

    if (st->initialized == 0U) {
        memcpy(st->baseline_q8, st->f_on, sizeof(st->baseline_q8));
        memset(st->d_on, 0, sizeof(st->d_on));
        memset(st->d_off, 0, sizeof(st->d_off));
        st->initialized = 1U;
        st->frame_count = 1U;
        return;
    }

    /* 3. Wide-field glare step cancellation via 8x8 lattice median */
    {
        int k = 0;
        int ry, rx;
        for (ry = 0; ry < 8; ry++) {
            for (rx = 0; rx < 8; rx++) {
                int idx = (2 + 4 * ry) * NECKSIGHT_GRID + (2 + 4 * rx);
                int32_t diff = (int32_t)st->f_on[idx] - (int32_t)st->baseline_q8[idx];
                if (diff > 32000) diff = 32000;
                if (diff < -32000) diff = -32000;
                med_samples[k++] = (int16_t)diff;
            }
        }
        insertion_sort_i16(med_samples, 64);
        g_step = ((int32_t)med_samples[31] + (int32_t)med_samples[32]) / 2;
    }

    /* 4. Lamina L1 (ON) & L2 (OFF) Naka-Rushton half-wave rectification */
    for (i = 0; i < NECKSIGHT_PIXELS; i++) {
        uint16_t log_v = st->f_on[i];
        int32_t base_s = (int32_t)st->baseline_q8[i] + g_step;
        int32_t dl;

        if (base_s < 0) base_s = 0;
        if (base_s > 65535) base_s = 65535;
        dl = (int32_t)log_v - base_s;

        /* Monotonic adaptation (~0.82 per step: 210 / 256) */
        st->baseline_q8[i] = (uint16_t)(((base_s * 46) + ((int32_t)log_v * 210) + 128) >> 8);

        if (dl > 2) {
            uint32_t u = (uint32_t)(dl - 2);
            st->f_on[i] = (uint16_t)((u * 2047U) / (u + 90U));
            st->f_off[i] = 0U;
        } else if (dl < -2) {
            uint32_t u = (uint32_t)(-dl - 2);
            st->f_on[i] = 0U;
            st->f_off[i] = (uint16_t)((u * 2047U) / (u + 90U));
        } else {
            st->f_on[i] = 0U;
            st->f_off[i] = 0U;
        }
    }

    /* 5. Pass X: build SAT for Left arm (nx - 1.25*px), accumulate LPTC X stats */
    memset(st->sat, 0, NECKSIGHT_SAT_STRIDE * sizeof(int32_t));
    for (y = 0; y < NECKSIGHT_GRID; y++) {
        int32_t row_acc = 0;
        int sat_row = (y + 1) * NECKSIGHT_SAT_STRIDE;
        int prev_row = y * NECKSIGHT_SAT_STRIDE;
        st->sat[sat_row] = 0;

        for (x = 0; x < NECKSIGHT_GRID; x++) {
            uint32_t px, nx, t4e, t5e;
            int32_t vx;
            int32_t drive_l;

            eval_pixel_x(st->f_on, st->d_on, st->f_off, st->d_off, y, x, &px, &nx, &t4e, &t5e);
            vx = (int32_t)px - (int32_t)nx;
            sum_vx += vx;
            sum_yaw_q7 += vx * (int32_t)HS_WEIGHT_Y_Q7[y];
            sum_rad_q5 += vx * (2 * x - (NECKSIGHT_GRID - 1));
            total_motion += (px + nx);
            t4_total += t4e;
            t5_total += t5e;

            drive_l = (int32_t)nx - ((5 * (int32_t)px) >> 2);
            row_acc += drive_l;
            st->sat[sat_row + x + 1] = st->sat[prev_row + x + 1] + row_acc;
        }
    }

    {
        int u = 0, iy, ix, is;
        for (iy = 0; iy < LPLC2_GRID_DIM; iy++) {
            int cy = (int)LPLC2_CENTERS[iy];
            for (ix = 0; ix < LPLC2_GRID_DIM; ix++) {
                int cx = (int)LPLC2_CENTERS[ix];
                for (is = 0; is < LPLC2_NUM_SCALES; is++) {
                    int r = (int)LPLC2_RADII[is];
                    int hw = (2 * r + 1) / 3;
                    int32_t m;
                    if (hw < 2) hw = 2;
                    m = query_sat_mean_i32(st->sat, cy - hw, cy + hw + 1, cx - r, cx - 1);
                    ql_arr[u++] = (m > 0) ? (uint16_t)(m > 65535 ? 65535 : m) : 0U;
                }
            }
        }
    }

    /* Pass X (Right arm): build SAT for (px - 1.25*nx) */
    for (y = 0; y < NECKSIGHT_GRID; y++) {
        int32_t row_acc = 0;
        int sat_row = (y + 1) * NECKSIGHT_SAT_STRIDE;
        int prev_row = y * NECKSIGHT_SAT_STRIDE;
        st->sat[sat_row] = 0;

        for (x = 0; x < NECKSIGHT_GRID; x++) {
            uint32_t px, nx, t4e, t5e;
            int32_t drive_r;
            eval_pixel_x(st->f_on, st->d_on, st->f_off, st->d_off, y, x, &px, &nx, &t4e, &t5e);
            drive_r = (int32_t)px - ((5 * (int32_t)nx) >> 2);
            row_acc += drive_r;
            st->sat[sat_row + x + 1] = st->sat[prev_row + x + 1] + row_acc;
        }
    }

    {
        int u = 0, iy, ix, is;
        for (iy = 0; iy < LPLC2_GRID_DIM; iy++) {
            int cy = (int)LPLC2_CENTERS[iy];
            for (ix = 0; ix < LPLC2_GRID_DIM; ix++) {
                int cx = (int)LPLC2_CENTERS[ix];
                for (is = 0; is < LPLC2_NUM_SCALES; is++) {
                    int r = (int)LPLC2_RADII[is];
                    int hw = (2 * r + 1) / 3;
                    int32_t m;
                    if (hw < 2) hw = 2;
                    m = query_sat_mean_i32(st->sat, cy - hw, cy + hw + 1, cx + 2, cx + r + 1);
                    qr_arr[u++] = (m > 0) ? (uint16_t)(m > 65535 ? 65535 : m) : 0U;
                }
            }
        }
    }

    /* 6. Pass Y: build SAT for Up arm (ny - 1.25*py), accumulate LPTC Y stats */
    for (y = 0; y < NECKSIGHT_GRID; y++) {
        int32_t row_acc = 0;
        int sat_row = (y + 1) * NECKSIGHT_SAT_STRIDE;
        int prev_row = y * NECKSIGHT_SAT_STRIDE;
        st->sat[sat_row] = 0;

        for (x = 0; x < NECKSIGHT_GRID; x++) {
            uint32_t py, ny, t4e, t5e;
            int32_t vy;
            int32_t drive_u;

            eval_pixel_y(st->f_on, st->d_on, st->f_off, st->d_off, y, x, &py, &ny, &t4e, &t5e);
            vy = (int32_t)py - (int32_t)ny;
            sum_vy += vy;
            sum_rad_q5 += vy * (2 * y - (NECKSIGHT_GRID - 1));
            total_motion += (py + ny);
            t4_total += t4e;
            t5_total += t5e;

            drive_u = (int32_t)ny - ((5 * (int32_t)py) >> 2);
            row_acc += drive_u;
            st->sat[sat_row + x + 1] = st->sat[prev_row + x + 1] + row_acc;
        }
    }

    {
        int u = 0, iy, ix, is;
        for (iy = 0; iy < LPLC2_GRID_DIM; iy++) {
            int cy = (int)LPLC2_CENTERS[iy];
            for (ix = 0; ix < LPLC2_GRID_DIM; ix++) {
                int cx = (int)LPLC2_CENTERS[ix];
                for (is = 0; is < LPLC2_NUM_SCALES; is++) {
                    int r = (int)LPLC2_RADII[is];
                    int hw = (2 * r + 1) / 3;
                    int32_t m;
                    if (hw < 2) hw = 2;
                    m = query_sat_mean_i32(st->sat, cy - r, cy - 1, cx - hw, cx + hw + 1);
                    qu_arr[u++] = (m > 0) ? (uint16_t)(m > 65535 ? 65535 : m) : 0U;
                }
            }
        }
    }

    /* Pass Y (Down arm): build SAT for (py - 1.25*ny) */
    for (y = 0; y < NECKSIGHT_GRID; y++) {
        int32_t row_acc = 0;
        int sat_row = (y + 1) * NECKSIGHT_SAT_STRIDE;
        int prev_row = y * NECKSIGHT_SAT_STRIDE;
        st->sat[sat_row] = 0;

        for (x = 0; x < NECKSIGHT_GRID; x++) {
            uint32_t py, ny, t4e, t5e;
            int32_t drive_d;
            eval_pixel_y(st->f_on, st->d_on, st->f_off, st->d_off, y, x, &py, &ny, &t4e, &t5e);
            drive_d = (int32_t)py - ((5 * (int32_t)ny) >> 2);
            row_acc += drive_d;
            st->sat[sat_row + x + 1] = st->sat[prev_row + x + 1] + row_acc;
        }
    }

    {
        int u = 0, iy, ix, is;
        for (iy = 0; iy < LPLC2_GRID_DIM; iy++) {
            int cy = (int)LPLC2_CENTERS[iy];
            for (ix = 0; ix < LPLC2_GRID_DIM; ix++) {
                int cx = (int)LPLC2_CENTERS[ix];
                for (is = 0; is < LPLC2_NUM_SCALES; is++) {
                    int r = (int)LPLC2_RADII[is];
                    int hw = (2 * r + 1) / 3;
                    int32_t m;
                    uint32_t ql, qr, qu, qd, sym;
                    if (hw < 2) hw = 2;
                    m = query_sat_mean_i32(st->sat, cy + 2, cy + r + 1, cx - hw, cx + hw + 1);
                    qd_arr[u] = (m > 0) ? (uint16_t)(m > 65535 ? 65535 : m) : 0U;

                    ql = (uint32_t)ql_arr[u];
                    qr = (uint32_t)qr_arr[u];
                    qu = (uint32_t)qu_arr[u];
                    qd = (uint32_t)qd_arr[u];
                    if (ql > 0U && qr > 0U && qu > 0U && qd > 0U) {
                        uint32_t lr = necksight_isqrt_u32(ql * qr);
                        uint32_t ud = necksight_isqrt_u32(qu * qd);
                        sym = necksight_isqrt_u32(lr * ud);
                        if (sym > best_sym) {
                            best_sym = sym;
                            best_idx = u;
                            best_cx = (uint8_t)cx;
                            best_cy = (uint8_t)cy;
                            best_r = (uint8_t)r;
                        }
                    }
                    u++;
                }
            }
        }
    }

    /* 7. Update delayed Medulla low-pass states (~0.55 alpha: 141 / 256) */
    for (i = 0; i < NECKSIGHT_PIXELS; i++) {
        st->d_on[i] = (uint16_t)(
            (((uint32_t)st->d_on[i] * 115U) + ((uint32_t)st->f_on[i] * 141U) + 128U) >> 8
        );
        st->d_off[i] = (uint16_t)(
            (((uint32_t)st->d_off[i] * 115U) + ((uint32_t)st->f_off[i] * 141U) + 128U) >> 8
        );
    }

    /* 8. Populate LPTC wide-field averages */
    out->vx_flow_q10 = sum_vx / NECKSIGHT_PIXELS;
    out->vy_flow_q10 = sum_vy / NECKSIGHT_PIXELS;
    out->yaw_pan_q10 = (sum_yaw_q7 >> 7) / NECKSIGHT_PIXELS;
    out->radial_div_q10 = (sum_rad_q5 / 32) / NECKSIGHT_PIXELS;
    out->t4_on_energy = t4_total;
    out->t5_off_energy = t5_total;

    /* 9. Wide-field LPTC tangential pan suppression on LPLC2 */
    {
        uint32_t ax = (out->vx_flow_q10 >= 0) ? (uint32_t)out->vx_flow_q10 : (uint32_t)(-out->vx_flow_q10);
        uint32_t ay = (out->vy_flow_q10 >= 0) ? (uint32_t)out->vy_flow_q10 : (uint32_t)(-out->vy_flow_q10);
        pan_mag = necksight_isqrt_u32(ax * ax + ay * ay);
    }

    if (best_sym > (pan_mag * 2U) / 3U) {
        uint32_t num = (best_sym - (pan_mag * 2U) / 3U) * 1024U;
        uint32_t denom = 1024U + 12U * pan_mag;
        uint32_t avg_mot = (total_motion / NECKSIGHT_PIXELS) + 1U;
        uint32_t coh_q8 = (pan_mag * 256U) / avg_mot;
        if (coh_q8 > 90U) {
            uint32_t dc = coh_q8 - 90U;
            denom += (dc * dc) / 2U;
        }
        gated_score = num / denom;
    } else {
        gated_score = 0U;
    }

    /* 10. Giant Fiber leaky membrane potential (alpha ~ 0.65: 166 / 256) */
    if (gated_score > 0U) {
        st->gf_score_q10 = ((st->gf_score_q10 * 90U) + (gated_score * 166U) + 128U) >> 8;
    } else {
        st->gf_score_q10 = (st->gf_score_q10 * 32U) >> 8;
    }

    if (st->gf_score_q10 > 65535U) {
        st->gf_score_q10 = 65535U;
    }

    out->looming_score_q10 = (uint16_t)st->gf_score_q10;
    out->triggered = (out->looming_score_q10 >= st->threshold_q10) ? 1U : 0U;

    if (best_sym > 0U) {
        uint16_t r_curr_q4 = (uint16_t)(((uint32_t)best_r << 4));
        uint32_t rate_q4_per_step;

        out->focus_x = best_cx;
        out->focus_y = best_cy;
        out->ql_out = ql_arr[best_idx];
        out->qr_out = qr_arr[best_idx];
        out->qu_out = qu_arr[best_idx];
        out->qd_out = qd_arr[best_idx];

        if (st->prev_rim_r_q4 > 0U && r_curr_q4 > st->prev_rim_r_q4) {
            uint16_t step_dr = (uint16_t)(r_curr_q4 - st->prev_rim_r_q4);
            st->dr_dt_ema_q4 = (st->dr_dt_ema_q4 == 0U)
                ? step_dr
                : (uint16_t)(((uint32_t)st->dr_dt_ema_q4 + (uint32_t)step_dr + 1U) >> 1);
        }
        st->prev_rim_r_q4 = r_curr_q4;
        st->prev_fx = best_cx;
        st->prev_fy = best_cy;

        rate_q4_per_step = st->dr_dt_ema_q4;
        if (rate_q4_per_step < 4U) {
            rate_q4_per_step = (best_sym / 24U) + 4U;
        }
        {
            uint32_t ttc = ((uint32_t)r_curr_q4 * (uint32_t)dt_ms) / rate_q4_per_step;
            if (ttc < 5U) ttc = 5U;
            if (ttc > 30000U) ttc = 30000U;
            out->est_ttc_ms = (uint16_t)ttc;
        }
    } else {
        st->prev_rim_r_q4 = 0U;
        st->dr_dt_ema_q4 = 0U;
    }

    st->frame_count++;
}

void necksight_step_u8(
    necksight_state_t *st,
    const uint8_t frame_32x32[NECKSIGHT_PIXELS],
    uint16_t dt_ms,
    necksight_result_t *out
)
{
    uint16_t buf_q4[NECKSIGHT_PIXELS];
    int i;
    if (frame_32x32 == NULL) {
        return;
    }
    for (i = 0; i < NECKSIGHT_PIXELS; i++) {
        buf_q4[i] = (uint16_t)((uint16_t)frame_32x32[i] << 4);
    }
    necksight_step_u16(st, buf_q4, dt_ms, out);
}

void necksight_step_frame_u8(
    necksight_state_t *st,
    const uint8_t *src,
    uint16_t src_h,
    uint16_t src_w,
    uint16_t src_stride,
    uint16_t dt_ms,
    necksight_result_t *out
)
{
    uint16_t grid_q4[NECKSIGHT_PIXELS];
    if (src == NULL) {
        return;
    }
    necksight_downsample_u8(src, src_h, src_w, src_stride, grid_q4);
    necksight_step_u16(st, grid_q4, dt_ms, out);
}


/* Bilinear Q4 shift of one 32x32 uint16 plane: out(y, x) = in(y - dy, x - dx), edges clamped. */
static void necksight_shift_plane(uint16_t *plane, uint16_t *scratch, int32_t dx_q4, int32_t dy_q4)
{
    const int32_t g = (int32_t)NECKSIGHT_GRID;
    const int32_t max_q4 = (g - 1) * 16;
    int32_t y, x;
    for (y = 0; y < g; y++) {
        int32_t sy = y * 16 - dy_q4;
        int32_t y0, fy;
        if (sy < 0) sy = 0;
        if (sy > max_q4) sy = max_q4;
        y0 = sy >> 4;
        fy = sy & 15;
        if (y0 >= g - 1) { y0 = g - 2; fy = 16; }
        for (x = 0; x < g; x++) {
            int32_t sx = x * 16 - dx_q4;
            int32_t x0, fx;
            uint32_t a00, a01, a10, a11, top, bot;
            if (sx < 0) sx = 0;
            if (sx > max_q4) sx = max_q4;
            x0 = sx >> 4;
            fx = sx & 15;
            if (x0 >= g - 1) { x0 = g - 2; fx = 16; }
            a00 = plane[y0 * g + x0];
            a01 = plane[y0 * g + x0 + 1];
            a10 = plane[(y0 + 1) * g + x0];
            a11 = plane[(y0 + 1) * g + x0 + 1];
            top = a00 * (uint32_t)(16 - fx) + a01 * (uint32_t)fx;
            bot = a10 * (uint32_t)(16 - fx) + a11 * (uint32_t)fx;
            scratch[y * g + x] = (uint16_t)((top * (uint32_t)(16 - fy) + bot * (uint32_t)fy + 128U) >> 8);
        }
    }
    memcpy(plane, scratch, sizeof(uint16_t) * (size_t)NECKSIGHT_PIXELS);
}

void necksight_shift_state(necksight_state_t *st, int16_t dx_q4, int16_t dy_q4)
{
    if (st == NULL || !st->initialized || (dx_q4 == 0 && dy_q4 == 0)) {
        return;
    }
    /* f_on is per-frame scratch, rewritten at the start of every step */
    necksight_shift_plane(st->baseline_q8, st->f_on, dx_q4, dy_q4);
    necksight_shift_plane(st->d_on, st->f_on, dx_q4, dy_q4);
    necksight_shift_plane(st->d_off, st->f_on, dx_q4, dy_q4);
}

int16_t necksight_gyro_to_shift_q4(int32_t rate_mrad_s, uint16_t dt_ms, uint16_t focal_px_q4)
{
    int64_t num = (int64_t)rate_mrad_s * (int64_t)dt_ms * (int64_t)focal_px_q4;
    int64_t q = (num >= 0) ? (num + 500000) / 1000000 : -((-num + 500000) / 1000000);
    if (q > 32767) q = 32767;
    if (q < -32767) q = -32767;
    return (int16_t)q;
}
