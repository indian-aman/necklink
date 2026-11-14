/*
 * NeckCompass v2.0.0 ANSI C99 Embedded Core (Zero-Heap, Zero-Floating-Point, Q15/Q12 Fixed-Point)
 *
 * Biophysical Drosophila Central Complex navigation stack grounded in BANC v888 connectome counts:
 *   - 50 EPG compass neurons (45 EPG + 5 EPGt) tiled across 16 Ellipsoid Body wedges
 *   - 42 PEN angular-velocity shifter neurons (24 PEN_b + 18 PEN_a; 7,998 PEN -> EPG synapses)
 *   - 40 Delta7 glutamatergic bridge neurons (6,998 EPG -> Delta7, 4,646 Delta7 -> EPG,
 *     6,066 Delta7 -> PFL3 synapses) enforcing anti-phase sinusoidal bump sharpening
 *   - 32 R2/R4d sensory Ring Neurons with Q12 Hebbian/anti-Hebbian online soft-iron learning
 *   - 16-column opponent CPU4 Fan-Shaped Body path integrator
 *   - 25 PFL3 bilateral steering neurons (259 direct synapses onto DNa02) +
 *     12 PFL2 180-deg anti-goal turn-boost neurons breaking the sin(180 deg) == 0 dead-zone
 *   - 9-DOF 3D gravity-gated complementary tilt compensator for pitching boats & banking drones
 *
 * Designed for 32-bit/16-bit MCUs (ESP32, STM32, RP2040, Teensy, AVR, flight controllers, USVs):
 *   - 0 bytes heap allocation
 *   - 0 hardware/software FPU instructions
 *   - 60 B compass + 16 B 9-DOF tilt + 264 B navigator + 1,104 B plastic soft-iron ring
 *   - 260 B Flash LUT (quarter-wave sin + octant atan)
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

#define NC_VERSION_MAJOR     2
#define NC_VERSION_MINOR     0
#define NC_VERSION_PATCH     0
#define NC_VERSION_STRING    "2.0.0"

#define NC_NUM_WEDGES        16
#define NC_NUM_R_NEURONS     32
#define NC_Q15_ONE           32767
#define NC_Q12_ONE           4096

/* Empirical BANC v888 proofread connectome neuron counts */
#define NC_BANC_EPG_COUNT    50
#define NC_BANC_PEN_COUNT    42
#define NC_BANC_DELTA7_COUNT 40
#define NC_BANC_PFL3_COUNT   25
#define NC_BANC_PFL2_COUNT   12

/*
 * 16-bit Binary Angular Measurement (BAM):
 *   Full circle (360 deg) = 65536 counts (wraps naturally modulo 2^16).
 *   [-32768 .. +32767] maps to [-180.00 deg .. +179.9945 deg).
 *   +16384 = +90 deg, -16384 = -90 deg, -32768 = +/-180 deg.
 */
typedef int16_t nc_bam_t;

/*
 * 9-axis strapdown IMU + 3-axis magnetometer sample (zero-float integer units).
 *
 * Right-handed body frame (standard FLU / NWU: +X forward, +Y left, +Z up; at flat level
 * rest ax_mg = 0, ay_mg = 0, az_mg = +1000, and +gz_bam_s is CCW yaw rate matching RingCompass;
 * algebraically identical in NED when feeding body gravity vector +1000 mg along +Z down):
 *   - Roll (phi) around +X, Pitch (theta) around +Y, Yaw (psi) around +Z (intrinsic Z-Y-X).
 *   - Tilt-compensated horizontal field:
 *       mx_h = mx*cos(pitch) + my*sin(roll)*sin(pitch) + mz*cos(roll)*sin(pitch)
 *       my_h = my*cos(roll) - mz*sin(roll)
 *       mag_heading_bam = atan2(-my_h, mx_h)
 */
typedef struct {
    int32_t gx_bam_s; /* Body roll rate p in BAM/s (see nc_cdeg_s_to_bam_s / nc_mrad_s_to_bam_s) */
    int32_t gy_bam_s; /* Body pitch rate q in BAM/s                                              */
    int32_t gz_bam_s; /* Body yaw rate r in BAM/s                                                */
    int32_t ax_mg;    /* Body X accelerometer reading in milli-g (0 at level rest)               */
    int32_t ay_mg;    /* Body Y accelerometer reading in milli-g (0 at level rest)               */
    int32_t az_mg;    /* Body Z accelerometer reading in milli-g (+1000 mg at level rest)        */
    int32_t mx;       /* Body X magnetometer reading (arbitrary integer units: mG, nT, LSB)      */
    int32_t my;       /* Body Y magnetometer reading                                             */
    int32_t mz;       /* Body Z magnetometer reading                                             */
} nc_imu9_t;

/*
 * Complementary 3D gravity roll & pitch estimator with wave-slam / linear-accel gating (16 B).
 * Rejects accelerometer updates whenever |norm(a) - 1000 mg| > gate_tol_mg (default 250 mg = 0.25g).
 */
typedef struct {
    int32_t  roll_sub_bam;  /* Unsigned 24-bit Q8 sub-BAM roll phase (wraps cleanly modulo 360 deg) */
    int32_t  pitch_sub_bam; /* Signed Q8 sub-BAM pitch angle clamped to +/-85.00 deg                */
    uint16_t tau_ms;        /* Complementary gravity filter time constant in ms (default 500 ms)    */
    uint16_t gate_tol_mg;   /* Gravity norm gate tolerance around 1000 mg (default 250 mg = 0.25g)  */
    uint8_t  accel_gated;   /* 1 if latest sample gated out accel due to wave slam / linear accel   */
    uint8_t  initialized;   /* 1 once roll & pitch have been seeded                                 */
} nc_tilt_t;

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

/*
 * Plastic R-neuron -> E-PG associative soft-iron / harbor warp self-calibrator (< 1,200 B RAM).
 * Learns 30+ deg non-ellipsoidal magnetic distortions online (self-supervised from gyro turns or
 * supervised from intermittent sun/GNSS course cues) with zero floats and zero heap allocation.
 */
typedef struct {
    int16_t      weights_q12[NC_NUM_WEDGES][NC_NUM_R_NEURONS]; /* 16x32 weights in Q12 [-8..+8) (1024 B) */
    nc_compass_t comp;              /* Embedded 16-wedge E-PG ring attractor compass (60 B)          */
    int32_t      bias_rem_accum;    /* Sub-BAM/s remainder accumulator for smooth bias tracking      */
    uint32_t     learn_steps;       /* Plasticity step count for periodic zero-mean recentering      */
    int16_t      lr_q8;             /* Hebbian learning rate in Q8 (default 1024 = 4.0)              */
    int16_t      anchor_scale_q15;  /* Self-supervised magnetic pull scale in Q15 (default 8192=0.25)*/
    nc_bam_t     map_offset_bam;    /* Circular zero-mean offset in self-supervised mode             */
    nc_bam_t     prev_raw_bam;      /* Previous raw magnetic heading for movement-gated learning     */
    uint8_t      has_prev_raw;      /* 1 once prev_raw_bam has been captured                         */
    uint8_t      has_abs_ref;       /* 1 once a supervised reference cue has locked absolute azimuth */
} nc_plastic_ring_t;

typedef struct {
    int64_t  cpu4_pos_q15mm[NC_NUM_WEDGES]; /* Excitatory Fan-Shaped Body CPU4 columns      */
    int64_t  cpu4_neg_q15mm[NC_NUM_WEDGES]; /* Opponent 180-deg hDelta CPU4 columns         */
    int32_t  arrival_radius_mm;             /* Smooth deceleration/taper radius (mm)        */
    nc_bam_t last_heading_bam;              /* Latest E-PG heading in BAM                   */
    int16_t  steering_gain_q8;              /* PFL3/PFL2 motor gain in Q8                   */
} nc_navigator_t;

/* Fixed-point angle & angular-rate conversions + LUT trigonometry (zero FPU) */
nc_bam_t nc_bam_add(nc_bam_t a, nc_bam_t b);
nc_bam_t nc_bam_sub(nc_bam_t a, nc_bam_t b);
nc_bam_t nc_cdeg_to_bam(int32_t centideg);
int32_t  nc_bam_to_cdeg(nc_bam_t bam);
nc_bam_t nc_mrad_to_bam(int32_t mrad);
int32_t  nc_cdeg_s_to_bam_s(int32_t cdeg_s);
int32_t  nc_bam_s_to_cdeg_s(int32_t bam_s);
int32_t  nc_mrad_s_to_bam_s(int32_t mrad_s);

int16_t  nc_sin_q15(nc_bam_t bam);
int16_t  nc_cos_q15(nc_bam_t bam);
nc_bam_t nc_atan2_bam(int32_t y, int32_t x);
uint32_t nc_isqrt64(uint64_t val);

/* 9-Axis 3D Tilt-Compensated IMU + Magnetometer Front-End API */
void     nc_tilt_init(nc_tilt_t *tilt);
void     nc_tilt_reset(nc_tilt_t *tilt, nc_bam_t roll_bam, nc_bam_t pitch_bam);
nc_bam_t nc_tilt_roll_bam(const nc_tilt_t *tilt);
nc_bam_t nc_tilt_pitch_bam(const nc_tilt_t *tilt);

nc_bam_t nc_tilt_compensate_mag_bam(
    nc_bam_t roll_bam,
    nc_bam_t pitch_bam,
    int32_t mx,
    int32_t my,
    int32_t mz
);

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
);

/* 16-wedge E-PG <-> Delta7 Ring Attractor Compass API */
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
void     nc_compass_delta7_q15(const nc_compass_t *comp, int16_t out_delta7_q15[NC_NUM_WEDGES]);

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
);

/* Plastic R-Neuron -> E-PG Online Soft-Iron Self-Calibration API */
void     nc_plastic_ring_init(nc_plastic_ring_t *pr);
void     nc_plastic_ring_encode_r(nc_bam_t raw_mag_bam, int16_t out_r_q15[NC_NUM_R_NEURONS]);

void     nc_plastic_ring_map(
    const nc_plastic_ring_t *pr,
    nc_bam_t raw_mag_bam,
    nc_bam_t *out_corrected_bam,
    int16_t *out_conf_q15
);

nc_bam_t nc_plastic_ring_heading_bam(const nc_plastic_ring_t *pr);

nc_bam_t nc_plastic_ring_update(
    nc_plastic_ring_t *pr,
    int32_t omega_bam_s,
    uint32_t dt_ms,
    nc_bam_t raw_mag_bam,
    nc_bam_t ref_bam,
    uint8_t ref_valid,
    uint8_t learn
);

nc_bam_t nc_plastic_ring_update_9dof(
    nc_plastic_ring_t *pr,
    nc_tilt_t *tilt,
    const nc_imu9_t *imu,
    uint32_t dt_ms,
    nc_bam_t ref_bam,
    uint8_t ref_valid,
    uint8_t learn
);

/* 16-column Fan-Shaped Body CPU4 Path Integrator & PFL3/PFL2 Steering API */
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
