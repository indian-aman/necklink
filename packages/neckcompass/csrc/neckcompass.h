/*
 * NeckCompass ANSI C99 Embedded Core (Zero-Heap, Zero-Floating-Point, Q15 Fixed-Point)
 *
 * Biophysical Central Complex 16-wedge E-PG ring attractor compass + P-EN velocity
 * shifters + 16-column opponent CPU4 path integrator + PFL3/PFL2 homebound steering.
 *
 * Designed for 32-bit/16-bit MCUs (ESP32, STM32, RP2040, flight controllers, USVs):
 *   - 0 bytes heap allocation
 *   - 0 hardware/software FPU instructions
 *   - ~330 bytes of state RAM (see selftest [1/7]) + 260 B Flash LUT
 *   - ~1 us per update on a desktop x86-64 core (not yet measured on MCU hardware)
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef NECKCOMPASS_H
#define NECKCOMPASS_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define NC_NUM_WEDGES 16
#define NC_Q15_ONE    32767

/*
 * 16-bit Binary Angular Measurement (BAM):
 *   Full circle (360 deg) = 65536 counts (wraps naturally modulo 2^16).
 *   [-32768 .. +32767] maps to [-180.00 deg .. +179.9945 deg).
 *   +16384 = +90 deg, -16384 = -90 deg, -32768 = +/-180 deg.
 */
typedef int16_t nc_bam_t;

typedef struct {
    int16_t  wedges_q15[NC_NUM_WEDGES]; /* Non-negative 16-wedge E-PG activity in [0, 32767] */
    int32_t  phase_sub_bam;             /* Q8 sub-BAM accumulator (1/256 BAM = 0.00002 deg) */
    int32_t  pen_bias_bam_s;            /* Homeostatic P-EN gyro bias estimate (BAM / sec)  */
    int16_t  mag_gain_q8;               /* R2 magnetic pull gain in Q8 (256 = 1.0 s^-1)     */
    int16_t  sun_gain_q8;               /* R4d sun/sky pull gain in Q8 (256 = 1.0 s^-1)     */
    int16_t  bias_rate_q8;              /* P-EN homeostatic bias rate in Q8 (256 = 1.0 s^-2)*/
    int16_t  last_mag_gate_q15;         /* Latest dendritic coincidence gate in [0, 32767]  */
    int16_t  gate_dist_q15;             /* Basin edge: gate = 0 where (1 - cos d) >= this   */
    int16_t  offbasin_c_q15;            /* Low-pass off-basin innovation phasor (cos)       */
    int16_t  offbasin_s_q15;            /* Low-pass off-basin innovation phasor (sin)       */
    uint16_t offbasin_ms;               /* Time the cue has sat consistently off-basin      */
    uint16_t reanchor_ms;               /* Re-anchor after this long off-basin (0 = never)  */
    uint8_t  initialized;               /* 1 once E-PG bump has been seeded                 */
} nc_compass_t;

typedef struct {
    int64_t  cpu4_pos_q15mm[NC_NUM_WEDGES]; /* Excitatory Fan-Shaped Body CPU4 columns      */
    int64_t  cpu4_neg_q15mm[NC_NUM_WEDGES]; /* Opponent 180-deg hDelta CPU4 columns         */
    int32_t  arrival_radius_mm;             /* Smooth deceleration/taper radius (mm)        */
    nc_bam_t last_heading_bam;              /* Latest E-PG heading in BAM                   */
    int16_t  steering_gain_q8;              /* PFL3 motor gain in Q8                        */
} nc_navigator_t;

/* Fixed-point angle conversions & LUT trigonometry (zero FPU) */
nc_bam_t nc_bam_add(nc_bam_t a, nc_bam_t b);
nc_bam_t nc_bam_sub(nc_bam_t a, nc_bam_t b);
nc_bam_t nc_cdeg_to_bam(int32_t centideg);
int32_t  nc_bam_to_cdeg(nc_bam_t bam);
nc_bam_t nc_mrad_to_bam(int32_t mrad);

int16_t  nc_sin_q15(nc_bam_t bam);
int16_t  nc_cos_q15(nc_bam_t bam);
nc_bam_t nc_atan2_bam(int32_t y, int32_t x);
uint32_t nc_isqrt64(uint64_t val);

/* 16-wedge E-PG Ring Attractor Compass API */
void     nc_compass_init(nc_compass_t *comp);

/*
 * Tune the attractor basin to the 1-sigma noise of the heading cue (centidegrees).
 * Basin half-width (gate = 0.5) = 4 deg + noise. nc_compass_init() applies 3.00 deg.
 * Matches RingCompass(cue_noise_deg=...) in Python.
 */
void     nc_compass_set_cue_noise(nc_compass_t *comp, int32_t noise_cdeg);

/* Re-anchor onto the averaged cue after it sits consistently off-basin this long
 * (ms, max 65535; 0 disables). nc_compass_init() applies 10000 ms. */
void     nc_compass_set_reanchor_ms(nc_compass_t *comp, uint16_t reanchor_ms);
void     nc_compass_reset(nc_compass_t *comp, nc_bam_t heading_bam);
nc_bam_t nc_compass_heading_bam(const nc_compass_t *comp);

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
);

/* 16-column Fan-Shaped Body CPU4 Path Integrator & PFL3 Steering API */
void     nc_navigator_init(nc_navigator_t *nav, int32_t arrival_radius_mm);
void     nc_navigator_reset(nc_navigator_t *nav);

void     nc_navigator_update(
    nc_navigator_t *nav,
    int32_t speed_mm_s,
    uint32_t dt_ms,
    nc_bam_t heading_bam
);

void     nc_navigator_displacement_mm(
    const nc_navigator_t *nav,
    int32_t *out_x_mm,
    int32_t *out_y_mm
);

void     nc_navigator_home_vector(
    const nc_navigator_t *nav,
    uint32_t *out_dist_mm,
    nc_bam_t *out_home_bearing_bam
);

int32_t  nc_navigator_steering_cmd(
    const nc_navigator_t *nav,
    nc_bam_t current_heading_bam,
    int32_t max_turn_bam_s
);

#ifdef __cplusplus
}
#endif

#endif /* NECKCOMPASS_H */
