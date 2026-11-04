/*
 * NeckCord v1.1.0 - Zero-Heap, Pure Fixed-Point ANSI C99 Embedded Core
 *
 * Implements:
 *   1. 4-byte Descending Cervical Command Frame codec + CRC-4 verification
 *   2. 6-limb phase-coupled Kuramoto VNC CPG with single-leg-loss pentapod adaptation
 *   3. 4..8 channel presynaptic-shunting fault-tolerant 3-DOF wrench allocator
 *
 * Memory guarantees:
 *   - Zero dynamic allocation (no stdlib heap calls)
 *   - Pure integer Q15 / Q14 / 16-bit BAM arithmetic
 *   - Total runtime state < 512 bytes (< 3 KB static RAM budget)
 *
 * Copyright (c) 2026 Amandeep Singh Bains. MIT License.
 */

#ifndef NECKCORD_H
#define NECKCORD_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define NC_Q15_ONE       32767
#define NC_Q14_ONE       16384
#define NC_NUM_LIMBS     6
#define NC_MAX_ACTUATORS 8
#define NC_MIN_ACTUATORS 4

/* Error / status codes */
#define NC_OK            0
#define NC_ERR_ARG      -1
#define NC_ERR_CRC      -2
#define NC_ERR_RANGE    -3
#define NC_ERR_SEQ      -4
#define NC_ERR_SINGULAR -5

/* Descending gait mode codes (2-bit wire field) */
typedef enum {
    NC_GAIT_AUTO     = 0,
    NC_GAIT_TRIPOD   = 1,
    NC_GAIT_TETRAPOD = 2,
    NC_GAIT_WAVE     = 3,
    NC_GAIT_PENTAPOD = 4,
    NC_GAIT_FREEZE   = 5
} nc_gait_mode_t;

/* 1. 4-byte Descending Cervical Command Frame */
typedef struct {
    int16_t drive_fwd_q15;   /* [-32767, +32767] */
    int16_t turn_yaw_q15;    /* [-32767, +32767] */
    int16_t strafe_lat_q15;  /* [-32767, +32767] */
    uint8_t gait_mode;       /* 0..3 */
    uint8_t freeze_reflex;   /* 0 or 1 */
    uint8_t seq;             /* 0..3 rolling sequence */
} nc_descending_frame_t;

uint8_t nc_crc4_upper28(uint32_t upper28);

int nc_encode_descending_4b(const nc_descending_frame_t *frame, uint8_t out_raw[4]);

int nc_decode_descending_4b(
    const uint8_t in_raw[4],
    int expected_seq,
    nc_descending_frame_t *out_frame
);

/* 2. 6-Limb Segmental VNC CPG Cord */
typedef struct {
    uint16_t phases_bam[NC_NUM_LIMBS];      /* 0..65535 -> [0, 2*pi) */
    int16_t  limb_health_q15[NC_NUM_LIMBS]; /* 0..32767 */
    uint16_t base_freq_milli_hz;            /* e.g. 800 = 0.80 Hz */
    uint16_t max_freq_milli_hz;             /* e.g. 2800 = 2.80 Hz */
    int16_t  stride_mm;                     /* e.g. 80 mm */
    int16_t  fault_thresh_q15;              /* e.g. 11468 (~0.35) */
    uint8_t  adapt_to_damage;               /* 1 = enable pentapod + gap closure */
} nc_cpg_cord_t;

typedef struct {
    uint16_t phases_bam[NC_NUM_LIMBS];
    uint8_t  stance_mask;                    /* bit i = 1 if limb i grounded */
    int16_t  coxa_q15[NC_NUM_LIMBS];         /* protraction/retraction [-32767, +32767] */
    int16_t  femur_lift_q15[NC_NUM_LIMBS];   /* swing elevation [0, +32767] */
    int16_t  foot_x_mm[NC_NUM_LIMBS];        /* body-frame x [mm] */
    int16_t  foot_y_mm[NC_NUM_LIMBS];        /* body-frame y [mm] */
    uint16_t duty_q15;                       /* stance duty in Q15 */
    uint8_t  active_gait;                    /* nc_gait_mode_t */
    uint8_t  is_statically_stable;           /* 1 if >=3 grounded limbs strictly enclose CoM */
} nc_cpg_state_t;

int16_t nc_sin_bam_q15(uint16_t angle_bam);
int16_t nc_cos_bam_q15(uint16_t angle_bam);

void nc_cpg_init(nc_cpg_cord_t *cord, uint8_t adapt_to_damage);
int  nc_cpg_set_limb_health(nc_cpg_cord_t *cord, uint8_t limb_idx, int16_t health_q15);
int  nc_cpg_step(
    nc_cpg_cord_t *cord,
    const nc_descending_frame_t *cmd,
    uint16_t dt_ms,
    nc_cpg_state_t *out_state
);
uint8_t nc_check_com_enclosed_mm(
    const int16_t foot_x_mm[NC_NUM_LIMBS],
    const int16_t foot_y_mm[NC_NUM_LIMBS],
    uint8_t stance_mask,
    int16_t min_margin_mm
);

/* 3. 4..8 Channel Presynaptic-Shunting Thruster / Motor Allocator */
typedef struct {
    uint8_t n_actuators;                       /* 4..8 */
    int16_t W_q14[3][NC_MAX_ACTUATORS];        /* 3xN wrench matrix in Q14 (16384 = 1.0) */
    int16_t health_q15[NC_MAX_ACTUATORS];      /* estimated health 0..32767 */
    int16_t shunt_q15[NC_MAX_ACTUATORS];       /* presynaptic shunting gate 0..32767 */
    int16_t last_cmd_q15[NC_MAX_ACTUATORS];    /* previous command [-32767, +32767] */
    int16_t exp_effort_q15[NC_MAX_ACTUATORS];  /* lagged expected effort [-32767, +32767] */
    int16_t lambda_q14;                        /* damping term in Q14 (e.g. 8) */
    int16_t shunt_floor_q15;                   /* e.g. 6553 (~0.20) */
    int16_t shunt_knee_q15;                    /* e.g. 14745 (~0.45) */
    int16_t deadband_q15;                      /* e.g. 1966 (~0.06) */
    uint16_t tau_act_ms;                       /* actuator lag [ms], e.g. 40 */
    uint16_t tau_fault_ms;                     /* fast fault onset [ms], e.g. 28 */
    uint16_t tau_recover_ms;                   /* slow recovery [ms], e.g. 850 */
    uint16_t probe_interval_ms;                /* re-test isolated actuators (0 = never)  */
    uint16_t isolated_ms[NC_MAX_ACTUATORS];    /* time each actuator has been isolated    */
    uint8_t prioritize_yaw_sway;               /* 1 = protect Fy/Tz before Fx saturation */
    uint8_t initialized;
} nc_shunting_cord_t;

typedef struct {
    int16_t cmd_q15[NC_MAX_ACTUATORS];         /* allocated commands [-32767, +32767] */
    int16_t health_q15[NC_MAX_ACTUATORS];      /* updated health [0, 32767] */
    int16_t shunt_q15[NC_MAX_ACTUATORS];       /* updated shunting gate [0, 32767] */
    int32_t pred_wrench_q14[3];                /* predicted wrench W * diag(h) * u in Q14 */
    uint8_t isolated_mask;                     /* bit i = 1 if actuator i shunted */
} nc_alloc_result_t;

/* W_q14 is row-major 3 x n_actuators: W_q14[r * n_actuators + i] */
int nc_shunting_init(
    nc_shunting_cord_t *cord,
    uint8_t n_actuators,
    const int16_t *W_q14
);

/* Standard X-vectored 4-thruster USV/ROV (lx_mm=450, ly_mm=350, 45-deg cant) */
void nc_shunting_init_usv4(nc_shunting_cord_t *cord, int16_t lx_mm, int16_t ly_mm);

int nc_shunting_set_health(nc_shunting_cord_t *cord, uint8_t idx, int16_t health_q15);

/* Return an isolated actuator to service (health = shunt knee); feedback then confirms
 * or re-isolates it. Matches ShuntingThrusterCord.reinstate_actuator() in Python. */
int nc_shunting_reinstate(nc_shunting_cord_t *cord, uint8_t idx);

/* Automatically reinstate isolated actuators every probe_interval_ms (0 disables, default). */
void nc_shunting_set_probe_interval(nc_shunting_cord_t *cord, uint16_t probe_interval_ms);

int nc_shunting_allocate(
    const nc_shunting_cord_t *cord,
    const int32_t wrench_des_q14[3],
    nc_alloc_result_t *out_res
);

int nc_shunting_step(
    nc_shunting_cord_t *cord,
    const int32_t wrench_des_q14[3],
    const int16_t *feedback_q15,
    uint16_t dt_ms,
    nc_alloc_result_t *out_res
);

#ifdef __cplusplus
}
#endif

#endif /* NECKCORD_H */
