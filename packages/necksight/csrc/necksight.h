/*
 * necksight.h - Zero-heap, fixed-point integer-only ANSI C99 Optic Lobe engine
 *
 * Implements R1-R6 log photoreceptor adaptation, Lamina L1/L2 ON/OFF split,
 * 4-cardinal 3-arm T4/T5 Elementary Motion Detectors (Hassenstein-Reichardt +
 * Barlow-Levick shunting veto), LPTC wide-field flow, and LPLC2 4-quadrant
 * radial symmetry looming reflex on a fixed 32x32 retinal grid (< 16 KB RAM).
 *
 * Author: Amandeep Singh Bains
 * License: MIT
 */

#ifndef NECKSIGHT_H
#define NECKSIGHT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define NECKSIGHT_GRID 32
#define NECKSIGHT_PIXELS (NECKSIGHT_GRID * NECKSIGHT_GRID)
#define NECKSIGHT_SAT_STRIDE (NECKSIGHT_GRID + 1)
#define NECKSIGHT_SAT_LEN (NECKSIGHT_SAT_STRIDE * NECKSIGHT_SAT_STRIDE)

/* Default Giant Fiber trigger threshold in Q10 motion units */
#define NECKSIGHT_DEFAULT_THRESHOLD_Q10 55U
#define NECKSIGHT_TTC_IDLE_MS 0xFFFFU

typedef struct {
    int32_t vx_flow_q10;         /* Wide-field horizontal flow (+X right, -X left) */
    int32_t vy_flow_q10;         /* Wide-field vertical flow (+Y down, -Y up)      */
    int32_t yaw_pan_q10;         /* Equatorial HS-weighted horizontal yaw pan      */
    int32_t radial_div_q10;      /* Wide-field centrifugal radial divergence       */
    uint32_t t4_on_energy;       /* Total T4 (ON pathway) cardinal motion energy   */
    uint32_t t5_off_energy;      /* Total T5 (OFF pathway) cardinal motion energy  */
    uint16_t looming_score_q10;  /* Giant Fiber leaky looming score                */
    uint16_t est_ttc_ms;         /* Estimated time-to-collision in ms (0xFFFF idle)*/
    uint16_t ql_out;             /* Winning LPLC2 left-arm outward response        */
    uint16_t qr_out;             /* Winning LPLC2 right-arm outward response       */
    uint16_t qu_out;             /* Winning LPLC2 up-arm outward response          */
    uint16_t qd_out;             /* Winning LPLC2 down-arm outward response        */
    uint8_t focus_x;             /* Estimated focus-of-expansion X in [0..31]      */
    uint8_t focus_y;             /* Estimated focus-of-expansion Y in [0..31]      */
    uint8_t triggered;           /* 1 if looming_score_q10 >= threshold_q10 else 0 */
} necksight_result_t;

typedef struct {
    /* Persistent photoreceptor & medulla delay states (6 KB) */
    uint16_t baseline_q8[NECKSIGHT_PIXELS];
    uint16_t d_on[NECKSIGHT_PIXELS];
    uint16_t d_off[NECKSIGHT_PIXELS];

    /* Reusable scratch buffers inside state to keep MCU stack < 512 B */
    uint16_t f_on[NECKSIGHT_PIXELS];
    uint16_t f_off[NECKSIGHT_PIXELS];
    int32_t sat[NECKSIGHT_SAT_LEN];

    /* Giant Fiber & optical-tau scalar tracking */
    uint32_t gf_score_q10;
    uint16_t threshold_q10;
    uint16_t prev_rim_r_q4;
    uint16_t dr_dt_ema_q4;
    uint32_t frame_count;
    uint8_t prev_fx;
    uint8_t prev_fy;
    uint8_t initialized;
} necksight_state_t;

/* Compile-time guarantee: static state struct strictly < 16 KB (16384 bytes) */
typedef char necksight_state_under_16kb_check[
    (sizeof(necksight_state_t) < 16384U) ? 1 : -1
];

/* Initialize state with default threshold (NECKSIGHT_DEFAULT_THRESHOLD_Q10). */
void necksight_init(necksight_state_t *st);

/* Initialize state with a custom Q10 looming trigger threshold. */
void necksight_init_custom(necksight_state_t *st, uint16_t threshold_q10);

/* Reset temporal adaptation and motion history while keeping threshold. */
void necksight_reset(necksight_state_t *st);

/*
 * Integer box-area downsampler from arbitrary HxW 8-bit grayscale buffer
 * (e.g., ESP32-S3 OV2640 QVGA 240x320) to 32x32 Q4 uint16_t grid [0..4080].
 */
void necksight_downsample_u8(
    const uint8_t *src,
    uint16_t src_h,
    uint16_t src_w,
    uint16_t src_stride,
    uint16_t dst_q4[NECKSIGHT_PIXELS]
);

/* Step pipeline on a 32x32 8-bit grayscale grid. */
void necksight_step_u8(
    necksight_state_t *st,
    const uint8_t frame_32x32[NECKSIGHT_PIXELS],
    uint16_t dt_ms,
    necksight_result_t *out
);

/* Step pipeline on a 32x32 16-bit (or Q4 downsampled / HDR) grayscale grid. */
void necksight_step_u16(
    necksight_state_t *st,
    const uint16_t frame_32x32[NECKSIGHT_PIXELS],
    uint16_t dt_ms,
    necksight_result_t *out
);

/* One-call helper: downsample arbitrary HxW uint8 camera buffer and step. */
void necksight_step_frame_u8(
    necksight_state_t *st,
    const uint8_t *src,
    uint16_t src_h,
    uint16_t src_w,
    uint16_t src_stride,
    uint16_t dt_ms,
    necksight_result_t *out
);

/* Pure integer utilities exposed for embedded testing */
uint16_t necksight_ilog2_q8(uint32_t v);
uint32_t necksight_isqrt_u32(uint32_t n);

#ifdef __cplusplus
}
#endif

#endif /* NECKSIGHT_H */
