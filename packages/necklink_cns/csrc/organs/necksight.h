/*
 * necksight.h - Zero-heap, fixed-point integer-only ANSI C99 Optic Lobe engine
 *
 * Implements R1-R6 log photoreceptor adaptation, Lamina L1/L2 ON/OFF split
 * with glare + AEC/AGC hunting cancellation, 4-cardinal 3-arm T4/T5 Elementary
 * Motion Detectors (Hassenstein-Reichardt + Barlow-Levick shunting veto) with
 * connectome-grounded T5:T4 OFF/ON weighting, LPTC wide-field flow, real BANC v888
 * 64-neuron LPLC2 + LPi and 32-neuron LC4 + Li22 connectome looming gates (with
 * NULL-pointer fallback to v2 analytic SAT), 3-DOF BAM gyro derotation, and
 * DNa02 contralateral evasive steering on a fixed 32x32 grid (< 15000 B SRAM).
 *
 * Author: Amandeep Singh Bains
 * License: MIT
 */

#ifndef NECKSIGHT_H
#define NECKSIGHT_H

#include <stddef.h>
#include <stdint.h>
#include "necksight_banc_weights.h"

#ifdef __cplusplus
extern "C" {
#endif

#define NECKSIGHT_VERSION_MAJOR 3
#define NECKSIGHT_VERSION_MINOR 0
#define NECKSIGHT_VERSION_PATCH 0
#define NECKSIGHT_VERSION_STRING "3.0.0"

static inline const necksight_banc_weights_t *necksight_header_weights_ref_(void)
{
    return &k_necksight_banc_default;
}

/* Return singleton default BANC v888 connectome weights table in Flash/RODATA. */
const necksight_banc_weights_t *necksight_default_weights(void);

#define NECKSIGHT_GRID 32
#define NECKSIGHT_PIXELS (NECKSIGHT_GRID * NECKSIGHT_GRID)
#define NECKSIGHT_SAT_STRIDE (NECKSIGHT_GRID + 1)
#define NECKSIGHT_SAT_LEN (NECKSIGHT_SAT_STRIDE * NECKSIGHT_SAT_STRIDE)

/* Default Giant Fiber trigger threshold in Q10 motion units */
#define NECKSIGHT_DEFAULT_THRESHOLD_Q10 55U
/* Default connectome-grounded T5 (OFF) gain in Q8: 346/256 ~ 1.35x */
#define NECKSIGHT_DEFAULT_T5_GAIN_Q8 346U
/* Default LC4/LPLC1 bar/wire channel weight in Q8: 108/256 ~ 0.42x */
#define NECKSIGHT_DEFAULT_LC4_WEIGHT_Q8 108U
#define NECKSIGHT_TTC_IDLE_MS 0xFFFFU

typedef struct {
    int32_t vx_flow_q10;         /* Wide-field horizontal flow (+X right, -X left) */
    int32_t vy_flow_q10;         /* Wide-field vertical flow (+Y down, -Y up)      */
    int32_t yaw_pan_q10;         /* Equatorial HS-weighted horizontal yaw pan      */
    int32_t radial_div_q10;      /* Wide-field centrifugal radial divergence       */
    uint32_t t4_on_energy;       /* Total T4 (ON pathway) cardinal motion energy   */
    uint32_t t5_off_energy;      /* Total T5 (OFF pathway) cardinal motion energy  */
    uint16_t looming_score_q10;  /* Giant Fiber leaky looming score                */
    uint16_t lc4_score_q10;      /* LC4/LPLC1 leaky thin-bar/wire looming score    */
    uint16_t est_ttc_ms;         /* Estimated time-to-collision in ms (0xFFFF idle)*/
    uint16_t ql_out;             /* Winning left-arm outward response              */
    uint16_t qr_out;             /* Winning right-arm outward response             */
    uint16_t qu_out;             /* Winning up-arm outward response                */
    uint16_t qd_out;             /* Winning down-arm outward response              */
    uint8_t focus_x;             /* Estimated focus-of-expansion X in [0..31]      */
    uint8_t focus_y;             /* Estimated focus-of-expansion Y in [0..31]      */
    uint8_t triggered;           /* 1 if looming_score_q10 >= threshold_q10 else 0 */
    int8_t avoid_x_q7;           /* Contralateral horizontal dodge in [-127..+127] */
    int8_t avoid_y_q7;           /* Contralateral vertical dodge in [-127..+127]   */
} necksight_result_t;

typedef struct {
    /* Persistent photoreceptor & medulla delay states (6 KB) */
    uint16_t baseline_q8[NECKSIGHT_PIXELS];
    uint16_t d_on[NECKSIGHT_PIXELS];
    uint16_t d_off[NECKSIGHT_PIXELS];

    /* Reusable scratch buffers inside state to keep MCU stack compact */
    uint16_t f_on[NECKSIGHT_PIXELS];
    uint16_t f_off[NECKSIGHT_PIXELS];
    int32_t sat[NECKSIGHT_SAT_LEN];

    /* Connectome synaptic table in Flash/RODATA (&k_necksight_banc_default; NULL = v2 analytic) */
    const necksight_banc_weights_t *weights;

    /* Giant Fiber, LC4 bar/wire & optical-tau scalar tracking */
    uint32_t gf_score_q10;
    uint32_t lc4_score_q10;
    uint16_t threshold_q10;
    uint16_t prev_rim_r_q4;
    uint16_t dr_dt_ema_q4;
    uint16_t t5_gain_q8;
    uint16_t lc4_weight_q8;
    uint32_t frame_count;
    uint8_t prev_fx;
    uint8_t prev_fy;
    uint8_t enable_lc4_bar;
    uint8_t initialized;
} necksight_state_t;

/* Compile-time guarantee: static state struct strictly < 15000 bytes SRAM */
typedef char necksight_state_under_15000b_check[
    (sizeof(necksight_state_t) < 15000U) ? 1 : -1
];

/* Initialize state with default threshold, BANC v888 connectome weights, and LC4 enabled. */
void necksight_init(necksight_state_t *st);

/* Initialize state with a custom Q10 looming trigger threshold. */
void necksight_init_custom(necksight_state_t *st, uint16_t threshold_q10);

/* Set connectome weights pointer (&k_necksight_banc_default default; NULL = legacy v2 SAT). */
void necksight_set_weights(necksight_state_t *st, const necksight_banc_weights_t *weights);

/* Reset temporal adaptation and motion history while keeping configuration. */
void necksight_reset(necksight_state_t *st);

/*
 * Integer box-area downsampler from arbitrary HxW 8-bit grayscale buffer
 * (e.g., ESP32-CAM OV2640 QVGA 240x320) to 32x32 Q4 uint16_t grid [0..4080].
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

/*
 * 2-DOF ego-motion derotation (call once per frame, BEFORE the step function).
 * Shifts photoreceptor baseline and T4/T5 delay lines by (dx_q4, dy_q4) in 1/16 grid px.
 */
void necksight_shift_state(necksight_state_t *st, int16_t dx_q4, int16_t dy_q4);

/*
 * 3-DOF ego-motion derotation (yaw, pitch, roll) around grid center (15.5, 15.5).
 * Pure integer BAM sin/cos (65536 BAM = 360 deg = 2*pi rad; +droll_bam = clockwise
 * scene rotation in image coordinates, matching counter-clockwise camera roll).
 */
void necksight_derotate_3dof(
    necksight_state_t *st,
    int16_t dx_q4,
    int16_t dy_q4,
    int16_t droll_bam
);

/* Gyro rate (mrad/s) x frame period (ms) x focal length (Q4 grid px) -> image shift in Q4 px.
 * For the 32-px grid, focal_px_q4 = 16 * 16 / tan(hfov / 2), e.g. 256 for a 90 deg lens. */
int16_t necksight_gyro_to_shift_q4(int32_t rate_mrad_s, uint16_t dt_ms, uint16_t focal_px_q4);

/* Roll rate (mrad/s) x frame period (ms) -> 16-bit Binary Angular Measurement (BAM). */
int16_t necksight_gyro_to_bam(int32_t roll_rate_mrad_s, uint16_t dt_ms);

uint16_t necksight_ilog2_q8(uint32_t v);
uint32_t necksight_isqrt_u32(uint32_t n);

#ifdef __cplusplus
} /* extern "C" */

/*
 * Clean zero-heap, zero-float C++ wrapper for Arduino / ESP32-CAM / C++11+.
 */
class NeckSight {
public:
    NeckSight() {
        necksight_init(&state_);
        last_ = emptyResult();
    }

    void begin(uint16_t threshold_q10 = NECKSIGHT_DEFAULT_THRESHOLD_Q10) {
        necksight_init_custom(&state_, threshold_q10);
        last_ = emptyResult();
    }

    void reset() {
        necksight_reset(&state_);
        last_ = emptyResult();
    }

    void setLC4BarChannel(bool enabled, uint16_t weight_q8 = NECKSIGHT_DEFAULT_LC4_WEIGHT_Q8) {
        state_.enable_lc4_bar = enabled ? 1U : 0U;
        state_.lc4_weight_q8 = weight_q8;
    }

    void setT5OffGainQ8(uint16_t t5_gain_q8) {
        state_.t5_gain_q8 = t5_gain_q8;
    }

    void setWeights(const necksight_banc_weights_t *weights) {
        necksight_set_weights(&state_, weights);
    }

    void derotateShiftQ4(int16_t dx_q4, int16_t dy_q4) {
        necksight_shift_state(&state_, dx_q4, dy_q4);
    }

    void derotate3DOF(int16_t dx_q4, int16_t dy_q4, int16_t droll_bam) {
        necksight_derotate_3dof(&state_, dx_q4, dy_q4, droll_bam);
    }

    void derotateIMU(
        int32_t yaw_rate_mrad_s,
        int32_t pitch_rate_mrad_s,
        int32_t roll_rate_mrad_s,
        uint16_t dt_ms,
        uint16_t focal_px_q4 = 256U
    ) {
        int16_t dx_q4 = necksight_gyro_to_shift_q4(yaw_rate_mrad_s, dt_ms, focal_px_q4);
        int16_t dy_q4 = (int16_t)(-necksight_gyro_to_shift_q4(pitch_rate_mrad_s, dt_ms, focal_px_q4));
        int16_t droll_bam = necksight_gyro_to_bam(roll_rate_mrad_s, dt_ms);
        necksight_derotate_3dof(&state_, dx_q4, dy_q4, droll_bam);
    }

    const necksight_result_t &stepGridU8(
        const uint8_t frame_32x32[NECKSIGHT_PIXELS],
        uint16_t dt_ms = 16U
    ) {
        necksight_step_u8(&state_, frame_32x32, dt_ms, &last_);
        return last_;
    }

    const necksight_result_t &stepGridU16(
        const uint16_t frame_32x32[NECKSIGHT_PIXELS],
        uint16_t dt_ms = 16U
    ) {
        necksight_step_u16(&state_, frame_32x32, dt_ms, &last_);
        return last_;
    }

    const necksight_result_t &stepFrameU8(
        const uint8_t *src,
        uint16_t src_h,
        uint16_t src_w,
        uint16_t src_stride = 0U,
        uint16_t dt_ms = 16U
    ) {
        uint16_t stride = (src_stride == 0U) ? src_w : src_stride;
        necksight_step_frame_u8(&state_, src, src_h, src_w, stride, dt_ms, &last_);
        return last_;
    }

    bool triggered() const { return last_.triggered != 0U; }
    uint16_t loomingScoreQ10() const { return last_.looming_score_q10; }
    uint16_t lc4ScoreQ10() const { return last_.lc4_score_q10; }
    uint16_t estTtcMs() const { return last_.est_ttc_ms; }
    uint8_t focusX() const { return last_.focus_x; }
    uint8_t focusY() const { return last_.focus_y; }
    int8_t avoidXQ7() const { return last_.avoid_x_q7; }
    int8_t avoidYQ7() const { return last_.avoid_y_q7; }

    const necksight_result_t &lastResult() const { return last_; }
    necksight_state_t &rawState() { return state_; }
    const necksight_state_t &rawState() const { return state_; }

private:
    necksight_state_t state_;
    necksight_result_t last_;

    static necksight_result_t emptyResult() {
        necksight_result_t r;
        r.vx_flow_q10 = 0;
        r.vy_flow_q10 = 0;
        r.yaw_pan_q10 = 0;
        r.radial_div_q10 = 0;
        r.t4_on_energy = 0U;
        r.t5_off_energy = 0U;
        r.looming_score_q10 = 0U;
        r.lc4_score_q10 = 0U;
        r.est_ttc_ms = NECKSIGHT_TTC_IDLE_MS;
        r.ql_out = 0U;
        r.qr_out = 0U;
        r.qu_out = 0U;
        r.qd_out = 0U;
        r.focus_x = 16U;
        r.focus_y = 16U;
        r.triggered = 0U;
        r.avoid_x_q7 = 0;
        r.avoid_y_q7 = 0;
        return r;
    }
};

#endif

#endif /* NECKSIGHT_H */
