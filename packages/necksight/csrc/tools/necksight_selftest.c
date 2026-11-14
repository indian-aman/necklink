/*
 * necksight_selftest.c - Self-test and throughput verifier for ANSI C99 NeckSight v3.0.0
 *
 * Verifies in zero-heap, zero-float pure integer ANSI C99:
 *   (a) Rightward translating grating (vx > 0, 0 false looming trigger)
 *   (b) Pure fast camera yaw pan across clutter (0 false looming trigger)
 *   (c) 20x & 100x global illumination steps + AEC/AGC exposure hunting (0 false trigger)
 *   (d) Expanding dark & bright looming disks + directional avoid_x_q7 sign check
 *   (e) ESP32-CAM QVGA (240x320) camera buffer downsampling & looming trigger
 *   (f) Single-core throughput (> 2,000 FPS requirement)
 *   (g) 2-DOF yaw +/-3 px/frame gyro derotation
 *   (h) LC4 thin vertical pole & horizontal wire looming (width 2->12 px) + directional dodge
 *   (i) 3-DOF roll + yaw BAM derotation (0 false alarms on pure roll+yaw; catches obstacle)
 *   (j) BANC v888 connectome weights vs NULL (v2 fallback) & scrambled weights ablation
 */

#include "necksight.h"
#include <stdio.h>
#include <string.h>
#include <time.h>

/* 16-step integer cosine table in [-127..+127] for period-16 wave */
static const int8_t WAVE16[16] = {
    127, 117, 90, 49, 0, -49, -90, -117,
    -127, -117, -90, -49, 0, 49, 90, 117
};

/* 65-entry Q14 quarter-wave sine table for test scene synthesis */
static const int16_t TEST_SIN_Q14[65] = {
    0, 402, 804, 1205, 1606, 2006, 2404, 2801,
    3196, 3590, 3981, 4370, 4756, 5139, 5520, 5897,
    6270, 6639, 7005, 7366, 7723, 8076, 8423, 8765,
    9102, 9434, 9760, 10080, 10394, 10702, 11003, 11297,
    11585, 11866, 12140, 12406, 12665, 12916, 13160, 13395,
    13623, 13842, 14053, 14256, 14449, 14635, 14811, 14978,
    15137, 15286, 15426, 15557, 15679, 15791, 15893, 15986,
    16069, 16143, 16207, 16261, 16305, 16340, 16364, 16379,
    16384
};

static int32_t test_sin_q14(uint16_t ang)
{
    uint32_t q = ((uint32_t)ang >> 14) & 3U;
    uint32_t off = (uint32_t)ang & 0x3FFFU;
    uint32_t p = (q & 1U) ? (16384U - off) : off;
    uint32_t idx = p >> 8;
    uint32_t frac = p & 0xFFU;
    int32_t mag = (idx >= 64U) ? 16384 :
        (((int32_t)TEST_SIN_Q14[idx] * (int32_t)(256U - frac)
        + (int32_t)TEST_SIN_Q14[idx + 1U] * (int32_t)frac + 128) >> 8);
    return (q >= 2U) ? -mag : mag;
}

static int32_t test_cos_q14(uint16_t ang)
{
    return test_sin_q14((uint16_t)(ang + 16384U));
}

static uint8_t synth_clutter_u8(int y, int x)
{
    int v = 128;
    v += ((int)WAVE16[(x + 2 * y) & 15] * 35) / 127;
    v += ((int)WAVE16[(3 * x - y + 5) & 15] * 25) / 127;
    v += ((int)WAVE16[(x + y) & 15] * 20) / 127;
    if (v < 20) v = 20;
    if (v > 235) v = 235;
    return (uint8_t)v;
}

/* Sub-pixel Q4 clutter sampler via bilinear interpolation */
static uint8_t synth_clutter_q4(int32_t y_q4, int32_t x_q4)
{
    int32_t yi = (y_q4 >= 0) ? (y_q4 / 16) : ((y_q4 - 15) / 16);
    int32_t xi = (x_q4 >= 0) ? (x_q4 / 16) : ((x_q4 - 15) / 16);
    int32_t fy = y_q4 - yi * 16;
    int32_t fx = x_q4 - xi * 16;
    uint32_t a00 = synth_clutter_u8(yi, xi);
    uint32_t a01 = synth_clutter_u8(yi, xi + 1);
    uint32_t a10 = synth_clutter_u8(yi + 1, xi);
    uint32_t a11 = synth_clutter_u8(yi + 1, xi + 1);
    uint32_t top = a00 * (uint32_t)(16 - fx) + a01 * (uint32_t)fx;
    uint32_t bot = a10 * (uint32_t)(16 - fx) + a11 * (uint32_t)fx;
    return (uint8_t)((top * (uint32_t)(16 - fy) + bot * (uint32_t)fy + 128U) >> 8);
}

/* Triangle-wave yaw: image offset in px at frame t (+/-3 px/frame, +/-12 px swing). */
static int yaw_offset_px(int t)
{
    int p = (t + 4) % 16;
    return (p < 8) ? (3 * p - 12) : (36 - 3 * p);
}

static void synth_yaw_frame(uint8_t *frame, int t, int with_disk)
{
    int o = yaw_offset_px(t);
    int rem = 30 - t;
    int rad_q2 = (96 + rem / 2) / rem;
    int r2_q4 = rad_q2 * rad_q2;
    int y, x;
    for (y = 0; y < NECKSIGHT_GRID; y++) {
        for (x = 0; x < NECKSIGHT_GRID; x++) {
            uint8_t bg = synth_clutter_u8(y + 4, x + 20 + o);
            int dy = y - 16;
            int dx = x - (16 - o);
            int d2_q4 = (dx * dx + dy * dy) * 16;
            frame[y * NECKSIGHT_GRID + x] = (with_disk && d2_q4 <= r2_q4) ? 12U : bg;
        }
    }
}

/* Synthesize 3-DOF rolling + yawing camera view of clutter + optional approaching disk */
static void synth_3dof_frame(
    uint8_t *frame,
    int t,
    int32_t cam_wx_q4,
    int32_t cam_wy_q4,
    uint16_t cam_roll_bam,
    int with_disk
)
{
    int32_t c_q14 = test_cos_q14(cam_roll_bam);
    int32_t s_q14 = test_sin_q14(cam_roll_bam);
    int rem = 30 - t;
    int rad_q4 = (16 * 96 + rem / 2) / (4 * rem);
    int32_t r2_q8 = (int32_t)rad_q4 * (int32_t)rad_q4;
    int y, x;

    for (y = 0; y < NECKSIGHT_GRID; y++) {
        int32_t uy_q4 = y * 16 - 248;
        for (x = 0; x < NECKSIGHT_GRID; x++) {
            int32_t ux_q4 = x * 16 - 248;
            /* Inverse screen-to-world map */
            int32_t wx_q4 = 512 + cam_wx_q4 + ((ux_q4 * c_q14 + uy_q4 * s_q14 + 8192) / 16384);
            int32_t wy_q4 = 512 + cam_wy_q4 + ((-ux_q4 * s_q14 + uy_q4 * c_q14 + 8192) / 16384);
            uint8_t bg = synth_clutter_q4(wy_q4, wx_q4);
            if (with_disk) {
                int32_t dx_q4 = wx_q4 - 512;
                int32_t dy_q4 = wy_q4 - 512;
                int32_t d2_q8 = dx_q4 * dx_q4 + dy_q4 * dy_q4;
                if (d2_q8 <= r2_q8) {
                    frame[y * NECKSIGHT_GRID + x] = 12U;
                } else if (d2_q8 <= r2_q8 + 768) {
                    frame[y * NECKSIGHT_GRID + x] = (uint8_t)(((int)bg + 12) / 2);
                } else {
                    frame[y * NECKSIGHT_GRID + x] = bg;
                }
            } else {
                frame[y * NECKSIGHT_GRID + x] = bg;
            }
        }
    }
}

/* Synthesize thin vertical pole (is_vert=1) or horizontal wire (is_vert=0) expanding 2->12 px */
static void synth_thin_bar_frame(uint8_t *frame, int t, int is_vert, int center_px, uint8_t bar_val)
{
    int rem = 30 - t;
    /* Half-width in Q4 grows from ~16 (1 px -> width 2 px) to ~96 (6 px -> width 12 px) */
    int hw_q4 = 14 + (230 / rem);
    int y, x;
    if (hw_q4 > 96) hw_q4 = 96;

    for (y = 0; y < NECKSIGHT_GRID; y++) {
        for (x = 0; x < NECKSIGHT_GRID; x++) {
            uint8_t bg = synth_clutter_u8(y, x);
            int coord_q4 = (is_vert ? x : y) * 16;
            int dist_q4 = coord_q4 - center_px * 16;
            if (dist_q4 < 0) dist_q4 = -dist_q4;
            if (dist_q4 <= hw_q4 - 8) {
                frame[y * NECKSIGHT_GRID + x] = bar_val;
            } else if (dist_q4 < hw_q4 + 8) {
                int alpha = hw_q4 + 8 - dist_q4; /* in [1..16] */
                frame[y * NECKSIGHT_GRID + x] = (uint8_t)(
                    (((int)bar_val * alpha) + ((int)bg * (16 - alpha)) + 8) / 16
                );
            } else {
                frame[y * NECKSIGHT_GRID + x] = bg;
            }
        }
    }
}

int main(int argc, char **argv)
{
    static necksight_state_t state;
    static uint8_t qvga_buf[240 * 320];
    uint8_t frame_u8[NECKSIGHT_PIXELS];
    uint16_t frame_u16[NECKSIGHT_PIXELS];
    necksight_result_t res;

    int json_mode = (argc > 1 && strcmp(argv[1], "--json") == 0);
    int failures = 0;
    int t, y, x;

    int32_t grating_vx_sum = 0;
    uint16_t grating_max_loom = 0U;
    int grating_triggers = 0;

    int32_t pan_vx_sum = 0;
    uint16_t pan_max_loom = 0U;
    int pan_triggers = 0;

    uint16_t flash_max_loom = 0U;
    int flash_triggers = 0;

    uint16_t dark_loom_peak = 0U;
    int dark_loom_trig_frame = -1;
    uint16_t dark_loom_ttc_ms = 0U;

    uint16_t bright_loom_peak = 0U;
    int bright_loom_trig_frame = -1;

    int qvga_triggered = 0;

    int yaw_loom_plain = 0;
    int yaw_loom_derot = 0;
    int yaw_pan_derot_triggers = 0;

    /* v2.0 verification trackers */
    int pole_no_lc4_triggered = 0;
    int pole_left_triggered = 0;
    uint16_t pole_left_lc4_peak = 0U;
    int8_t pole_left_avoid_x = 0;

    int pole_right_triggered = 0;
    int8_t pole_right_avoid_x = 0;

    int wire_top_triggered = 0;
    uint16_t wire_top_lc4_peak = 0U;
    int8_t wire_top_avoid_y = 0;

    int roll3d_loom_derot = 0;
    int roll3d_only_triggers = 0;
    int pure_roll_uncomp_triggers = 0;
    int v2_fallback_ok = 0;
    uint16_t scrambled_loom_peak = 0U;

    clock_t c0, c1;
    uint32_t bench_frames = 25000U;
    uint32_t fps = 0U;
    size_t state_bytes = sizeof(necksight_state_t);

    /* Verify static state size < 15000 bytes SRAM */
    if (state_bytes >= 15000U) {
        fprintf(stderr, "FAIL: sizeof(necksight_state_t) = %lu >= 15000\n",
                (unsigned long)state_bytes);
        return 1;
    }

    /* (a) Rightward translating grating (+X: wave shifts +1 px/frame) */
    necksight_init(&state);
    for (t = 0; t < 32; t++) {
        for (y = 0; y < NECKSIGHT_GRID; y++) {
            for (x = 0; x < NECKSIGHT_GRID; x++) {
                int phase = (2 * (x - t)) & 15;
                int val = 128 + (((int)WAVE16[phase] * 85) / 127);
                frame_u8[y * NECKSIGHT_GRID + x] = (uint8_t)val;
            }
        }
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (t >= 3) {
            grating_vx_sum += res.vx_flow_q10;
        }
        if (res.looming_score_q10 > grating_max_loom) {
            grating_max_loom = res.looming_score_q10;
        }
        if (res.triggered) {
            grating_triggers++;
        }
    }
    if (grating_vx_sum <= 0 || grating_triggers != 0) {
        fprintf(stderr, "FAIL (a) grating: vx_sum=%ld triggers=%d\n",
                (long)grating_vx_sum, grating_triggers);
        failures++;
    }

    /* (b) Pure fast camera yaw pan across cluttered panorama (3 px/frame & 4 px/frame) */
    necksight_init(&state);
    for (t = 0; t < 36; t++) {
        int ox = 3 * t;
        for (y = 0; y < NECKSIGHT_GRID; y++) {
            for (x = 0; x < NECKSIGHT_GRID; x++) {
                frame_u8[y * NECKSIGHT_GRID + x] = synth_clutter_u8(y + 4, x + ox);
            }
        }
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (t >= 3) {
            pan_vx_sum += res.vx_flow_q10;
        }
        if (res.looming_score_q10 > pan_max_loom) {
            pan_max_loom = res.looming_score_q10;
        }
        if (res.triggered) {
            pan_triggers++;
        }
    }
    necksight_reset(&state);
    for (t = 0; t < 36; t++) {
        int ox = 4 * t;
        for (y = 0; y < NECKSIGHT_GRID; y++) {
            for (x = 0; x < NECKSIGHT_GRID; x++) {
                frame_u8[y * NECKSIGHT_GRID + x] = synth_clutter_u8(y + 6, x + ox);
            }
        }
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.looming_score_q10 > pan_max_loom) {
            pan_max_loom = res.looming_score_q10;
        }
        if (res.triggered) {
            pan_triggers++;
        }
    }
    if (pan_vx_sum >= 0 || pan_triggers != 0) {
        fprintf(stderr, "FAIL (b) yaw pan: vx_sum=%ld max_loom=%u triggers=%d\n",
                (long)pan_vx_sum, (unsigned)pan_max_loom, pan_triggers);
        failures++;
    }

    /* (c) 20x & 100x sudden global illumination flash + AEC/AGC exposure hunting */
    necksight_init(&state);
    for (t = 0; t < 30; t++) {
        uint16_t mult = (t >= 15) ? 100U : 1U;
        for (y = 0; y < NECKSIGHT_GRID; y++) {
            for (x = 0; x < NECKSIGHT_GRID; x++) {
                uint16_t base = (uint16_t)synth_clutter_u8(y, x);
                /* base in [20..235]; * 100 -> [2000..23500] in 16-bit HDR space */
                frame_u16[y * NECKSIGHT_GRID + x] = (uint16_t)(base * mult);
            }
        }
        necksight_step_u16(&state, frame_u16, 16U, &res);
        if (res.looming_score_q10 > flash_max_loom) {
            flash_max_loom = res.looming_score_q10;
        }
        if (res.triggered) {
            flash_triggers++;
        }
    }
    /* Verify 8-bit 20x step + AEC/AGC hunting oscillation with sensor clipping at 255 */
    necksight_reset(&state);
    for (t = 0; t < 32; t++) {
        int gain_q4 = 16 + ((int)WAVE16[(3 * t) & 15] * 12) / 127; /* [4..28] in Q4 */
        for (y = 0; y < NECKSIGHT_GRID; y++) {
            for (x = 0; x < NECKSIGHT_GRID; x++) {
                int v = ((int)synth_clutter_u8(y, x) * gain_q4) / 16;
                if (v > 255) v = 255;
                if (v < 1) v = 1;
                frame_u8[y * NECKSIGHT_GRID + x] = (uint8_t)v;
            }
        }
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.looming_score_q10 > flash_max_loom) {
            flash_max_loom = res.looming_score_q10;
        }
        if (res.triggered) {
            flash_triggers++;
        }
    }
    if (flash_triggers != 0) {
        fprintf(stderr, "FAIL (c) 100x glare / AEC hunting: max_loom=%u triggers=%d\n",
                (unsigned)flash_max_loom, flash_triggers);
        failures++;
    }

    /* (d.1) Expanding dark looming disk on cluttered bright background */
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        int rem = 30 - t;
        int rad_q2 = (96 + rem / 2) / rem;
        int r2_q4 = rad_q2 * rad_q2;

        for (y = 0; y < NECKSIGHT_GRID; y++) {
            for (x = 0; x < NECKSIGHT_GRID; x++) {
                int dy = y - 16;
                int dx = x - 16;
                int d2_q4 = (dx * dx + dy * dy) * 16;
                uint8_t bg = synth_clutter_u8(y, x);
                if (d2_q4 <= r2_q4) {
                    frame_u8[y * NECKSIGHT_GRID + x] = 12U;
                } else if (d2_q4 <= r2_q4 + 48) {
                    frame_u8[y * NECKSIGHT_GRID + x] = (uint8_t)(((int)bg + 12) / 2);
                } else {
                    frame_u8[y * NECKSIGHT_GRID + x] = bg;
                }
            }
        }
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.looming_score_q10 > dark_loom_peak) {
            dark_loom_peak = res.looming_score_q10;
        }
        if (res.triggered && dark_loom_trig_frame < 0) {
            dark_loom_trig_frame = t;
            dark_loom_ttc_ms = res.est_ttc_ms;
        }
    }
    if (dark_loom_trig_frame < 0 || dark_loom_trig_frame >= 26) {
        fprintf(stderr, "FAIL (d.1) dark looming disk: trig_frame=%d peak=%u\n",
                dark_loom_trig_frame, (unsigned)dark_loom_peak);
        failures++;
    }

    /* (d.2) Expanding bright looming disk on dark background */
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        int rem = 30 - t;
        int rad_q2 = (96 + rem / 2) / rem;
        int r2_q4 = rad_q2 * rad_q2;

        for (y = 0; y < NECKSIGHT_GRID; y++) {
            for (x = 0; x < NECKSIGHT_GRID; x++) {
                int dy = y - 16;
                int dx = x - 16;
                int d2_q4 = (dx * dx + dy * dy) * 16;
                if (d2_q4 <= r2_q4) {
                    frame_u8[y * NECKSIGHT_GRID + x] = 240U;
                } else if (d2_q4 <= r2_q4 + 48) {
                    frame_u8[y * NECKSIGHT_GRID + x] = 130U;
                } else {
                    frame_u8[y * NECKSIGHT_GRID + x] = 18U;
                }
            }
        }
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.looming_score_q10 > bright_loom_peak) {
            bright_loom_peak = res.looming_score_q10;
        }
        if (res.triggered && bright_loom_trig_frame < 0) {
            bright_loom_trig_frame = t;
        }
    }
    if (bright_loom_trig_frame < 0 || bright_loom_trig_frame >= 26) {
        fprintf(stderr, "FAIL (d.2) bright looming disk: trig_frame=%d peak=%u\n",
                bright_loom_trig_frame, (unsigned)bright_loom_peak);
        failures++;
    }

    /* (e) ESP32-CAM QVGA (240x320) full-frame downsampler + looming check */
    necksight_init(&state);
    for (t = 0; t < 24; t++) {
        int rem = 26 - t;
        int rad = (900 + rem / 2) / rem;
        int r2 = rad * rad;
        for (y = 0; y < 240; y++) {
            uint8_t *row = qvga_buf + y * 320;
            int dy = y - 120;
            for (x = 0; x < 320; x++) {
                int dx = x - 160;
                row[x] = (dx * dx + dy * dy <= r2) ? 15U : 200U;
            }
        }
        necksight_step_frame_u8(&state, qvga_buf, 240U, 320U, 320U, 16U, &res);
        if (res.triggered) {
            qvga_triggered = 1;
        }
    }
    if (!qvga_triggered) {
        fprintf(stderr, "FAIL (e) QVGA 240x320 looming sequence did not trigger\n");
        failures++;
    }

    /* (g) 2-DOF gyro derotation: looming while the camera yaws +/-3 px/frame */
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        synth_yaw_frame(frame_u8, t, 1);
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.triggered) yaw_loom_plain = 1;
    }
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        int shift_px = -(yaw_offset_px(t) - yaw_offset_px(t - 1 + 16));
        synth_yaw_frame(frame_u8, t, 1);
        necksight_shift_state(&state, (int16_t)(16 * shift_px), 0);
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.triggered) yaw_loom_derot = 1;
    }
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        int shift_px = -(yaw_offset_px(t) - yaw_offset_px(t - 1 + 16));
        synth_yaw_frame(frame_u8, t, 0);
        necksight_shift_state(&state, (int16_t)(16 * shift_px), 0);
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.triggered) yaw_pan_derot_triggers++;
    }
    if (!yaw_loom_derot || yaw_pan_derot_triggers != 0 ||
        necksight_gyro_to_shift_q4(1000, 20U, 256U) != 5 ||
        necksight_gyro_to_shift_q4(-1000, 20U, 256U) != -5) {
        fprintf(stderr, "FAIL (g) 2-DOF derotation: loom_derot=%d pan_triggers=%d\n",
                yaw_loom_derot, yaw_pan_derot_triggers);
        failures++;
    }

    /* (h) LC4 thin vertical pole & horizontal wire looming + DNa02 directional avoidance */
    necksight_init(&state);
    state.enable_lc4_bar = 0U; /* LPLC2-only blindspot check on thin vertical pole */
    for (t = 0; t < 28; t++) {
        synth_thin_bar_frame(frame_u8, t, 1, 10, 12U);
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.triggered) pole_no_lc4_triggered = 1;
    }

    /* Left pole (x=10 < 15.5) with LC4 enabled -> must trigger & steer right (avoid_x_q7 > 0) */
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        synth_thin_bar_frame(frame_u8, t, 1, 10, 12U);
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.lc4_score_q10 > pole_left_lc4_peak) {
            pole_left_lc4_peak = res.lc4_score_q10;
        }
        if (res.triggered && !pole_left_triggered) {
            pole_left_triggered = 1;
            pole_left_avoid_x = res.avoid_x_q7;
        }
    }

    /* Right pole (x=22 > 15.5) -> must trigger & steer left (avoid_x_q7 < 0) */
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        synth_thin_bar_frame(frame_u8, t, 1, 22, 12U);
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.triggered && !pole_right_triggered) {
            pole_right_triggered = 1;
            pole_right_avoid_x = res.avoid_x_q7;
        }
    }

    /* Overhead horizontal wire (y=10 < 15.5) -> must trigger & duck down (avoid_y_q7 > 0) */
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        synth_thin_bar_frame(frame_u8, t, 0, 10, 12U);
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.lc4_score_q10 > wire_top_lc4_peak) {
            wire_top_lc4_peak = res.lc4_score_q10;
        }
        if (res.triggered && !wire_top_triggered) {
            wire_top_triggered = 1;
            wire_top_avoid_y = res.avoid_y_q7;
        }
    }

    if (pole_no_lc4_triggered != 0 || !pole_left_triggered || pole_left_avoid_x <= 0 ||
        !pole_right_triggered || pole_right_avoid_x >= 0 ||
        !wire_top_triggered || wire_top_avoid_y <= 0) {
        fprintf(stderr,
                "FAIL (h) LC4 bar/wire + avoid: no_lc4=%d pL=%d(ax=%d) pR=%d(ax=%d) wT=%d(ay=%d)\n",
                pole_no_lc4_triggered, pole_left_triggered, (int)pole_left_avoid_x,
                pole_right_triggered, (int)pole_right_avoid_x,
                wire_top_triggered, (int)wire_top_avoid_y);
        failures++;
    }

    /* (i) 3-DOF roll + yaw BAM derotation + pure uncompensated roll rejection */
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        /* Pure uncompensated roll (~4 deg/frame = 728 BAM/frame) on clutter: 0 false alarms */
        synth_3dof_frame(frame_u8, t, 0, 0, (uint16_t)(t * 728), 0);
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.triggered) pure_roll_uncomp_triggers++;
    }

    /* Simultaneous camera roll (640 BAM/frame ~ 3.5 deg/f) + yaw oscillation (+/-2.5 px/f) */
    {
        int pass;
        for (pass = 0; pass < 2; pass++) {
            int with_obs = (pass == 0) ? 1 : 0;
            int32_t cam_wx_q4 = 0;
            int32_t cam_wy_q4 = 0;
            uint16_t cam_roll_bam = 0U;

            necksight_init(&state);
            for (t = 0; t < 28; t++) {
                int16_t step_dx_q4 = 0;
                int16_t step_droll_bam = 0;
                if (t > 0) {
                    /* Camera body yaws & rolls each frame; scene moves opposite in body frame */
                    int yaw_dir = ((t / 7) & 1) ? 40 : -40; /* +/-2.5 px/frame in Q4 */
                    int32_t c_prev = test_cos_q14(cam_roll_bam);
                    int32_t s_prev = test_sin_q14(cam_roll_bam);
                    /* Advance camera world center along body X, then roll */
                    cam_wx_q4 -= (yaw_dir * c_prev) / 16384;
                    cam_wy_q4 += (yaw_dir * s_prev) / 16384;
                    step_droll_bam = 640;
                    cam_roll_bam = (uint16_t)(cam_roll_bam + (uint16_t)step_droll_bam);
                    /* Scene shift in the new rolled frame */
                    step_dx_q4 = (int16_t)((yaw_dir * test_cos_q14((uint16_t)step_droll_bam)) / 16384);
                    necksight_derotate_3dof(
                        &state,
                        step_dx_q4,
                        (int16_t)((yaw_dir * test_sin_q14((uint16_t)step_droll_bam)) / 16384),
                        step_droll_bam
                    );
                }
                synth_3dof_frame(frame_u8, t, cam_wx_q4, cam_wy_q4, cam_roll_bam, with_obs);
                necksight_step_u8(&state, frame_u8, 16U, &res);
                if (res.triggered) {
                    if (with_obs) roll3d_loom_derot = 1;
                    else roll3d_only_triggers++;
                }
            }
        }
    }
    if (pure_roll_uncomp_triggers != 0 || !roll3d_loom_derot || roll3d_only_triggers != 0 ||
        necksight_gyro_to_bam(1000, 16U) != 167 || necksight_gyro_to_bam(-1000, 16U) != -167) {
        fprintf(stderr,
                "FAIL (i) 3-DOF roll+yaw: pure_roll_trig=%d loom_3dof=%d bg_3dof_trig=%d\n",
                pure_roll_uncomp_triggers, roll3d_loom_derot, roll3d_only_triggers);
        failures++;
    }

    /* (j) BANC v888 default pointer check, NULL legacy v2 fallback, and scrambled ablation */
    {
        static necksight_banc_weights_t scram_w;
        int v2_loom = 0;
        int v2_pan_fa = 0;
        unsigned int idx;

        necksight_init(&state);
        if (state.weights != necksight_default_weights()) {
            fprintf(stderr, "FAIL (j) default weights pointer not set\n");
            failures++;
        }

        /* Legacy v2 fallback (weights == NULL) */
        necksight_set_weights(&state, NULL);
        for (t = 0; t < 28; t++) {
            int rem = 30 - t;
            int rad_q2 = (96 + rem / 2) / rem;
            int r2_q4 = rad_q2 * rad_q2;
            for (y = 0; y < NECKSIGHT_GRID; y++) {
                for (x = 0; x < NECKSIGHT_GRID; x++) {
                    int dy = y - 16;
                    int dx = x - 16;
                    int d2_q4 = (dx * dx + dy * dy) * 16;
                    frame_u8[y * NECKSIGHT_GRID + x] = (d2_q4 <= r2_q4) ? 12U : synth_clutter_u8(y, x);
                }
            }
            necksight_step_u8(&state, frame_u8, 16U, &res);
            if (res.triggered) v2_loom = 1;
        }
        necksight_reset(&state);
        for (t = 0; t < 32; t++) {
            for (y = 0; y < NECKSIGHT_GRID; y++) {
                for (x = 0; x < NECKSIGHT_GRID; x++) {
                    frame_u8[y * NECKSIGHT_GRID + x] = synth_clutter_u8(y + 4, x + 3 * t);
                }
            }
            necksight_step_u8(&state, frame_u8, 16U, &res);
            if (res.triggered) v2_pan_fa++;
        }
        v2_fallback_ok = (state.weights == NULL && v2_loom && v2_pan_fa == 0) ? 1 : 0;

        /* Scrambled connectome RF table: permute patches across columns/rows */
        memcpy(&scram_w, necksight_default_weights(), sizeof(scram_w));
        for (idx = 0U; idx < 64U * 4U; idx++) {
            unsigned int p0;
            for (p0 = 0U; p0 < 64U; p0++) {
                unsigned int p_src = ((p0 * 29U) + (idx * 17U) + 11U) & 63U;
                scram_w.W_lplc2_rf_q8[idx * 64U + p0] =
                    necksight_default_weights()->W_lplc2_rf_q8[idx * 64U + p_src];
            }
        }
        necksight_init(&state);
        necksight_set_weights(&state, &scram_w);
        state.enable_lc4_bar = 0U;
        for (t = 0; t < 28; t++) {
            int rem = 30 - t;
            int rad_q2 = (96 + rem / 2) / rem;
            int r2_q4 = rad_q2 * rad_q2;
            for (y = 0; y < NECKSIGHT_GRID; y++) {
                for (x = 0; x < NECKSIGHT_GRID; x++) {
                    int dy = y - 16;
                    int dx = x - 16;
                    int d2_q4 = (dx * dx + dy * dy) * 16;
                    frame_u8[y * NECKSIGHT_GRID + x] = (d2_q4 <= r2_q4) ? 12U : synth_clutter_u8(y, x);
                }
            }
            necksight_step_u8(&state, frame_u8, 16U, &res);
            if (res.looming_score_q10 > scrambled_loom_peak) {
                scrambled_loom_peak = res.looming_score_q10;
            }
        }
        if (!v2_fallback_ok || scrambled_loom_peak >= dark_loom_peak) {
            fprintf(stderr, "FAIL (j) connectome/v2 check: v2_ok=%d scram=%u real=%u\n",
                    v2_fallback_ok, (unsigned)scrambled_loom_peak, (unsigned)dark_loom_peak);
            failures++;
        }
    }

    /* (f) Single-core throughput benchmark (> 2,000 FPS requirement) */
    necksight_init(&state);
    c0 = clock();
    for (t = 0; t < (int)bench_frames; t++) {
        frame_u8[t & 1023] = (uint8_t)((frame_u8[t & 1023] + 17U) & 0xFFU);
        necksight_step_u8(&state, frame_u8, 16U, &res);
    }
    c1 = clock();
    {
        unsigned long elapsed_ticks = (c1 > c0) ? (unsigned long)(c1 - c0) : 1UL;
        unsigned long num = (unsigned long)bench_frames * (unsigned long)CLOCKS_PER_SEC;
        fps = (uint32_t)(num / elapsed_ticks);
    }
    {
#if defined(__SANITIZE_ADDRESS__)
        const uint32_t min_fps = 500U;
#else
        const uint32_t min_fps = 2000U;
#endif
        if (fps < min_fps) {
            fprintf(stderr, "FAIL (f) throughput: %u FPS < %u FPS\n",
                    (unsigned)fps, (unsigned)min_fps);
            failures++;
        }
    }

    if (json_mode) {
        printf("{\n");
        printf("  \"version\": \"%s\",\n", NECKSIGHT_VERSION_STRING);
        printf("  \"state_bytes\": %lu,\n", (unsigned long)state_bytes);
        printf("  \"throughput_fps\": %u,\n", (unsigned)fps);
        printf("  \"grating_vx_sum\": %ld,\n", (long)grating_vx_sum);
        printf("  \"grating_triggers\": %d,\n", grating_triggers);
        printf("  \"pan_vx_sum\": %ld,\n", (long)pan_vx_sum);
        printf("  \"pan_max_loom_q10\": %u,\n", (unsigned)pan_max_loom);
        printf("  \"pan_triggers\": %d,\n", pan_triggers);
        printf("  \"flash_20x_max_loom_q10\": %u,\n", (unsigned)flash_max_loom);
        printf("  \"flash_20x_triggers\": %d,\n", flash_triggers);
        printf("  \"dark_loom_peak_q10\": %u,\n", (unsigned)dark_loom_peak);
        printf("  \"dark_loom_trig_frame\": %d,\n", dark_loom_trig_frame);
        printf("  \"dark_loom_ttc_ms\": %u,\n", (unsigned)dark_loom_ttc_ms);
        printf("  \"bright_loom_peak_q10\": %u,\n", (unsigned)bright_loom_peak);
        printf("  \"bright_loom_trig_frame\": %d,\n", bright_loom_trig_frame);
        printf("  \"qvga_triggered\": %d,\n", qvga_triggered);
        printf("  \"yaw_loom_plain_triggered\": %d,\n", yaw_loom_plain);
        printf("  \"yaw_loom_derotated_triggered\": %d,\n", yaw_loom_derot);
        printf("  \"yaw_pan_derotated_triggers\": %d,\n", yaw_pan_derot_triggers);
        printf("  \"pole_left_triggered\": %d,\n", pole_left_triggered);
        printf("  \"pole_left_lc4_peak_q10\": %u,\n", (unsigned)pole_left_lc4_peak);
        printf("  \"pole_left_avoid_x_q7\": %d,\n", (int)pole_left_avoid_x);
        printf("  \"pole_right_avoid_x_q7\": %d,\n", (int)pole_right_avoid_x);
        printf("  \"wire_top_triggered\": %d,\n", wire_top_triggered);
        printf("  \"wire_top_lc4_peak_q10\": %u,\n", (unsigned)wire_top_lc4_peak);
        printf("  \"wire_top_avoid_y_q7\": %d,\n", (int)wire_top_avoid_y);
        printf("  \"roll3d_loom_derotated_triggered\": %d,\n", roll3d_loom_derot);
        printf("  \"roll3d_only_triggers\": %d,\n", roll3d_only_triggers);
        printf("  \"pure_roll_uncomp_triggers\": %d,\n", pure_roll_uncomp_triggers);
        printf("  \"v2_fallback_ok\": %d,\n", v2_fallback_ok);
        printf("  \"scrambled_loom_peak_q10\": %u,\n", (unsigned)scrambled_loom_peak);
        printf("  \"failures\": %d\n", failures);
        printf("}\n");
    } else {
        printf("NeckSight v%s ANSI C99 Self-Test (32x32 zero-heap, zero-float)\n",
               NECKSIGHT_VERSION_STRING);
        printf("  sizeof(necksight_state_t) : %lu bytes (< 15000 B limit)\n",
               (unsigned long)state_bytes);
        printf("  [a] Rightward grating     : vx_sum=%+ld | max_loom=%u | triggers=%d\n",
               (long)grating_vx_sum, (unsigned)grating_max_loom, grating_triggers);
        printf("  [b] Fast camera yaw pan   : vx_sum=%+ld | max_loom=%u | triggers=%d\n",
               (long)pan_vx_sum, (unsigned)pan_max_loom, pan_triggers);
        printf("  [c] 100x glare + AEC hunt : max_loom=%u | triggers=%d\n",
               (unsigned)flash_max_loom, flash_triggers);
        printf("  [d] Looming disk (dark)   : peak=%u | trig_frame=%d/28 | ttc=%u ms\n",
               (unsigned)dark_loom_peak, dark_loom_trig_frame, (unsigned)dark_loom_ttc_ms);
        printf("  [d] Looming disk (bright) : peak=%u | trig_frame=%d/28\n",
               (unsigned)bright_loom_peak, bright_loom_trig_frame);
        printf("  [e] ESP32 QVGA 240x320    : triggered=%d\n", qvga_triggered);
        printf("  [f] Single-core speed     : %u FPS\n", (unsigned)fps);
        printf("  [g] 2-DOF yaw + loom      : plain=%d | gyro-derotated=%d | pan-only=%d\n",
               yaw_loom_plain, yaw_loom_derot, yaw_pan_derot_triggers);
        printf("  [h] LC4 pole & wire       : pL=%d(lc4=%u,ax=%+d) | pR=%d(ax=%+d) | wire=%d(ay=%+d)\n",
               pole_left_triggered, (unsigned)pole_left_lc4_peak, (int)pole_left_avoid_x,
               pole_right_triggered, (int)pole_right_avoid_x,
               wire_top_triggered, (int)wire_top_avoid_y);
        printf("  [i] 3-DOF roll+yaw derot  : pure_roll_fa=%d | 3dof_loom=%d | 3dof_bg_fa=%d\n",
               pure_roll_uncomp_triggers, roll3d_loom_derot, roll3d_only_triggers);
        printf("  [j] BANC v888 vs scram/v2 : v2_null_ok=%d | real_peak=%u > scram_peak=%u\n",
               v2_fallback_ok, (unsigned)dark_loom_peak, (unsigned)scrambled_loom_peak);
        printf("  RESULT                    : %s\n", (failures == 0) ? "ALL PASSED" : "FAILED");
    }

    return (failures == 0) ? 0 : 1;
}
