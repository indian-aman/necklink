/*
 * necksight_selftest.c - Self-test and throughput verifier for ANSI C99 NeckSight
 *
 * Synthesizes 4 canonical optic-lobe sequences in pure integer C99:
 *   (a) Rightward translating grating (vx > 0, 0 false looming trigger)
 *   (b) Pure fast camera yaw pan across clutter (0 false looming trigger)
 *   (c) 20x sudden global illumination flash (0 false looming trigger)
 *   (d) Expanding dark & bright looming disks (LPLC2 triggers cleanly before impact)
 * Plus QVGA (240x320) ESP32-S3 camera buffer downsampling & >2,000 FPS check, and
 *   (g) gyro derotation: looming obstacle while the camera yaws back and forth at 3 px/frame
 */

#include "necksight.h"
#include <stdio.h>
#include <string.h>
#include <time.h>

/* Quarter-wave 16-step integer cosine table in [-127..+127] for period-16 wave */
static const int8_t WAVE16[16] = {
    127, 117, 90, 49, 0, -49, -90, -117,
    -127, -117, -90, -49, 0, 49, 90, 117
};

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

/* Triangle-wave yaw: image offset in px at frame t (+/-3 px/frame, +/-12 px swing). */
static int yaw_offset_px(int t)
{
    int p = (t + 4) % 16; /* period 16 frames */
    return (p < 8) ? (3 * p - 12) : (36 - 3 * p);
}

/* One yawing frame: clutter shifted by the yaw offset, plus (optionally) a dark disk that
 * moves with the scene and expands toward impact at t = 30. */
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

    clock_t c0, c1;
    uint32_t bench_frames = 25000U;
    uint32_t fps = 0U;
    size_t state_bytes = sizeof(necksight_state_t);

    /* Verify static state size < 16 KB */
    if (state_bytes >= 16384U) {
        fprintf(stderr, "FAIL: sizeof(necksight_state_t) = %lu >= 16384\n",
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

    /* (b) Pure fast camera yaw pan across cluttered panorama (3 px/frame) */
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
    if (pan_vx_sum >= 0 || pan_triggers != 0) {
        fprintf(stderr, "FAIL (b) yaw pan: vx_sum=%ld max_loom=%u triggers=%d\n",
                (long)pan_vx_sum, (unsigned)pan_max_loom, pan_triggers);
        failures++;
    }

    /* (c) 20x sudden global illumination flash on textured scene */
    necksight_init(&state);
    for (t = 0; t < 30; t++) {
        uint16_t mult = (t >= 15) ? 20U : 1U;
        for (y = 0; y < NECKSIGHT_GRID; y++) {
            for (x = 0; x < NECKSIGHT_GRID; x++) {
                uint16_t base = (uint16_t)synth_clutter_u8(y, x);
                /* base in [20..235]; * 20 -> [400..4700] in 16-bit HDR space */
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
    /* Also verify 8-bit 20x step (e.g. dark scene [6..12] -> [120..240]) */
    necksight_reset(&state);
    for (t = 0; t < 24; t++) {
        uint8_t mult = (t >= 12) ? 20U : 1U;
        for (y = 0; y < NECKSIGHT_GRID; y++) {
            for (x = 0; x < NECKSIGHT_GRID; x++) {
                uint8_t base = (uint8_t)(6U + (synth_clutter_u8(y, x) >> 5));
                frame_u8[y * NECKSIGHT_GRID + x] = (uint8_t)(base * mult);
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
        fprintf(stderr, "FAIL (c) 20x flash: max_loom=%u triggers=%d\n",
                (unsigned)flash_max_loom, flash_triggers);
        failures++;
    }

    /* (d.1) Expanding dark looming disk on cluttered bright background */
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        /* Radius grows hyperbolically before impact at t=28 */
        int rem = 30 - t;
        int rad_q2 = (96 + rem / 2) / rem; /* grows from ~3 px to ~14 px in Q0 (rad_q2 = 4*r) */
        int r2_q4 = rad_q2 * rad_q2;       /* 16 * r^2 */

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

    /* (e) ESP32-S3 QVGA (240x320) full-frame downsampler + looming check */
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

    /* (g) Gyro derotation: looming while the camera yaws +/-3 px/frame */
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        synth_yaw_frame(frame_u8, t, 1);
        necksight_step_u8(&state, frame_u8, 16U, &res);
        if (res.triggered) yaw_loom_plain = 1;
    }
    necksight_init(&state);
    for (t = 0; t < 28; t++) {
        /* scene content moves by -(o(t) - o(t-1)) px; a gyro would report this */
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
        fprintf(stderr, "FAIL (g) derotation: loom_derot=%d pan_triggers=%d\n",
                yaw_loom_derot, yaw_pan_derot_triggers);
        failures++;
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
    if (fps < 2000U) {
        fprintf(stderr, "FAIL (f) throughput: %u FPS < 2000 FPS\n", (unsigned)fps);
        failures++;
    }

    if (json_mode) {
        printf("{\n");
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
        printf("  \"failures\": %d\n", failures);
        printf("}\n");
    } else {
        printf("NeckSight ANSI C99 Self-Test (32x32 zero-heap, zero-fixed-point-only)\n");
        printf("  sizeof(necksight_state_t) : %lu bytes (< 16384 B limit)\n",
               (unsigned long)state_bytes);
        printf("  [a] Rightward grating     : vx_sum=%+ld | max_loom=%u | triggers=%d\n",
               (long)grating_vx_sum, (unsigned)grating_max_loom, grating_triggers);
        printf("  [b] Fast camera yaw pan   : vx_sum=%+ld | max_loom=%u | triggers=%d\n",
               (long)pan_vx_sum, (unsigned)pan_max_loom, pan_triggers);
        printf("  [c] 20x illumination step : max_loom=%u | triggers=%d\n",
               (unsigned)flash_max_loom, flash_triggers);
        printf("  [d] Looming disk (dark)   : peak=%u | trig_frame=%d/28 | ttc=%u ms\n",
               (unsigned)dark_loom_peak, dark_loom_trig_frame, (unsigned)dark_loom_ttc_ms);
        printf("  [d] Looming disk (bright) : peak=%u | trig_frame=%d/28\n",
               (unsigned)bright_loom_peak, bright_loom_trig_frame);
        printf("  [e] ESP32 QVGA 240x320    : triggered=%d\n", qvga_triggered);
        printf("  [f] Single-core speed     : %u FPS\n", (unsigned)fps);
        printf("  [g] Yaw +/-3 px/f + loom  : plain=%d | gyro-derotated=%d | pan-only false triggers=%d\n",
               yaw_loom_plain, yaw_loom_derot, yaw_pan_derot_triggers);
        printf("  RESULT                    : %s\n", (failures == 0) ? "ALL PASSED" : "FAILED");
    }

    return (failures == 0) ? 0 : 1;
}
