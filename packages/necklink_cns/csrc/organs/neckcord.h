/*
 * NeckCord v2.0.0 - Zero-Heap, Pure Fixed-Point ANSI C99 Embedded Core
 *
 * Implements:
 *   1. 4-byte Descending Cervical Command Frame codec + CRC-4 verification
 *   2. 6-limb phase-coupled Kuramoto VNC CPG with single-leg-loss pentapod adaptation
 *   3. 4..8 channel presynaptic-shunting 3-DOF & 4-DOF box-constrained active-set allocator
 *      (unidirectional [0, u_max] multicopters + asymmetric T200 marine thrusters)
 *   4. Sensorless ESC RPM + Current proprioceptive fault observer (stall, broken blade, foul)
 *   5. Exact 3-DOF 18-servo hexapod leg inverse kinematics (integer atan2, isqrt, acos LUT)
 *
 * Memory guarantees:
 *   - Zero dynamic allocation (no stdlib heap calls)
 *   - Pure integer Q20 / Q15 / Q14 / 16-bit BAM arithmetic (zero FP instructions)
 *   - Total runtime state < 1 KB (< 3 KB static RAM budget)
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

#ifndef NC_VERSION_MAJOR
#define NC_VERSION_MAJOR 2
#define NC_VERSION_MINOR 0
#define NC_VERSION_PATCH 0
#endif
#define NC_VERSION_STR   "2.0.0"

#ifndef NC_Q15_ONE
#define NC_Q15_ONE       32767
#endif
#ifndef NC_Q14_ONE
#define NC_Q14_ONE       16384
#endif
#define NC_NUM_LIMBS     6
#define NC_MAX_ACTUATORS 8
#define NC_MIN_ACTUATORS 4
#define NC_MAX_DOF       4

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

/* Integer trigonometric & root primitives (zero FP) */
int16_t  nc_sin_bam_q15(uint16_t angle_bam);
int16_t  nc_cos_bam_q15(uint16_t angle_bam);
uint32_t nc_isqrt_u32(uint32_t val);
uint32_t nc_isqrt_u64(uint64_t val);
uint16_t nc_cord_atan2_bam(int32_t y, int32_t x);
#if !defined(NECKCOMPASS_H) && !defined(nc_atan2_bam)
#define nc_atan2_bam nc_cord_atan2_bam
#endif
uint16_t nc_acos_q15_bam(int16_t cos_q15);

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

/* 3. Symmetric 3-DOF Presynaptic-Shunting Allocator (v1.x compatible) */
typedef struct {
    uint8_t n_actuators;                       /* 4..8 */
    int16_t W_q14[3][NC_MAX_ACTUATORS];        /* 3xN wrench matrix in Q14 (16384 = 1.0) */
    int16_t health_q15[NC_MAX_ACTUATORS];      /* estimated health 0..32767 */
    int16_t shunt_q15[NC_MAX_ACTUATORS];       /* presynaptic shunting gate 0..32767 */
    int16_t last_cmd_q15[NC_MAX_ACTUATORS];    /* previous command [-32767, +32767] */
    int16_t exp_effort_q15[NC_MAX_ACTUATORS];  /* lagged expected effort [-32767, +32767] */
    int16_t lambda_q14;                        /* damping term in Q14 (e.g. 8) */
    int16_t shunt_floor_q15;                   /* e.g. 7209 (~0.22) */
    int16_t shunt_knee_q15;                    /* e.g. 15728 (~0.48) */
    int16_t deadband_q15;                      /* e.g. 1638 (~0.05) */
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
int nc_shunting_reinstate(nc_shunting_cord_t *cord, uint8_t idx);
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

/* 4. v2.0 Box-Constrained Asymmetric 3-DOF & 4-DOF Active-Set Allocator */
typedef struct {
    uint8_t  n_dof;                                /* 3 ([Fx,Fy,Tz]) or 4 ([Fz,Tx,Ty,Tz]) */
    uint8_t  n_actuators;                          /* 4..8 */
    uint8_t  prioritize_attitude;                  /* 1 = protect [Fz,Tx,Ty] or [Fy,Tz] */
    uint8_t  initialized;
    int16_t  W_q14[NC_MAX_DOF][NC_MAX_ACTUATORS];  /* wrench matrix in Q14 */
    int16_t  u_min_q15[NC_MAX_ACTUATORS];          /* per-actuator lower bound [-32767, 0] */
    int16_t  u_max_q15[NC_MAX_ACTUATORS];          /* per-actuator upper bound [1, +32767] */
    int16_t  deadzone_q15[NC_MAX_ACTUATORS];       /* ESC deadzone jump [0, 16384] */
    int16_t  health_q15[NC_MAX_ACTUATORS];         /* 0..32767 */
    int16_t  shunt_q15[NC_MAX_ACTUATORS];          /* 0..32767 */
    int16_t  last_cmd_q15[NC_MAX_ACTUATORS];       /* [-32767, +32767] */
    int16_t  exp_effort_q15[NC_MAX_ACTUATORS];     /* [-32767, +32767] */
    uint16_t isolated_ms[NC_MAX_ACTUATORS];        /* recovery probe elapsed ms */
    int16_t  lambda_q20;                           /* Gram regularization in Q20 (default 16) */
    int16_t  shunt_floor_q15;                      /* default 7209 (~0.22) */
    int16_t  shunt_knee_q15;                       /* default 15728 (~0.48) */
    int16_t  deadband_q15;                         /* default 1638 (~0.05) */
    uint16_t tau_act_ms;                           /* default 40 */
    uint16_t tau_fault_ms;                         /* default 28 */
    uint16_t tau_recover_ms;                       /* default 850 */
    uint16_t probe_interval_ms;                    /* 0 = never */
} nc_shunting4d_t;

typedef struct {
    int16_t cmd_q15[NC_MAX_ACTUATORS];             /* box-constrained commands [u_min, u_max] */
    int16_t health_q15[NC_MAX_ACTUATORS];          /* 0..32767 */
    int16_t shunt_q15[NC_MAX_ACTUATORS];           /* 0..32767 */
    int32_t pred_wrench_q14[NC_MAX_DOF];           /* predicted wrench in Q14 */
    uint8_t isolated_mask;                         /* bit i = 1 if actuator shunted */
    uint8_t saturated_mask;                        /* bit i = 1 if actuator hit u_min/u_max */
    uint8_t active_iters;                          /* active-set iterations executed */
} nc_alloc4d_result_t;

int nc_shunting4d_init(
    nc_shunting4d_t *cord,
    uint8_t n_dof,
    uint8_t n_actuators,
    const int16_t *W_q14,
    const int16_t *u_min_q15,
    const int16_t *u_max_q15
);

/* Unidirectional [0, +32767] 6-rotor hexacopter (e.g. arm_mm=280, k_yaw_milli=18) */
void nc_shunting4d_init_hex6(nc_shunting4d_t *cord, int16_t arm_mm, int16_t k_yaw_milli);

/* Unidirectional [0, +32767] 8-rotor octocopter (e.g. arm_mm=320, k_yaw_milli=18) */
void nc_shunting4d_init_octo8(nc_shunting4d_t *cord, int16_t arm_mm, int16_t k_yaw_milli);

/* Asymmetric BlueRobotics T200 4-thruster USV (-40.0 N / +52.5 N -> u_min_q15 = -24965) */
void nc_shunting4d_init_usv_t200(
    nc_shunting4d_t *cord,
    int16_t lx_mm,
    int16_t ly_mm,
    int16_t u_min_q15,
    int16_t deadzone_q15
);

int nc_shunting4d_set_health(nc_shunting4d_t *cord, uint8_t idx, int16_t health_q15);
int nc_shunting4d_reinstate(nc_shunting4d_t *cord, uint8_t idx);

int nc_shunting4d_allocate(
    const nc_shunting4d_t *cord,
    const int32_t *wrench_des_q14,
    nc_alloc4d_result_t *out_res
);

int nc_shunting4d_step(
    nc_shunting4d_t *cord,
    const int32_t *wrench_des_q14,
    const int16_t *feedback_q15,
    uint16_t dt_ms,
    nc_alloc4d_result_t *out_res
);

/* 5. Sensorless ESC RPM + Current Proprioceptive Fault Observer */
typedef enum {
    NC_ESC_OK               = 0,
    NC_ESC_FAULT_STALL_OPEN = 1,
    NC_ESC_FAULT_BROKEN     = 2,
    NC_ESC_FAULT_OVERCURR   = 3
} nc_esc_fault_t;

typedef struct {
    uint8_t  n_actuators;                          /* 1..8 */
    uint8_t  initialized;
    uint16_t overcurrent_ratio_q8;                 /* default 410 (~1.60 * 256) */
    int32_t  rpm_max;                              /* full-scale speed [RPM] */
    int32_t  i_idle_ma;                            /* idle current [mA] */
    int32_t  k_load_ma;                            /* cubic load current at rpm_max [mA] */
    int16_t  broken_ratio_q15;                     /* default 14745 (~0.45) */
    int16_t  stall_ratio_q15;                      /* default 16384 (~0.50) */
    int16_t  deadband_q15;                         /* default 1638 (~0.05) */
    uint16_t tau_motor_ms;                         /* default 15 ms */
    uint16_t tau_fault_ms;                         /* default 16 ms */
    uint16_t tau_recover_ms;                       /* default 600 ms */
    int16_t  lagged_cmd_q15[NC_MAX_ACTUATORS];
    int16_t  health_q15[NC_MAX_ACTUATORS];
    int16_t  overcurr_score_q15[NC_MAX_ACTUATORS];
    int16_t  broken_score_q15[NC_MAX_ACTUATORS];
    int16_t  stall_score_q15[NC_MAX_ACTUATORS];
} nc_esc_observer_t;

typedef struct {
    int16_t est_thrust_q15[NC_MAX_ACTUATORS];      /* signed Q15 effective thrust */
    int16_t health_q15[NC_MAX_ACTUATORS];          /* 0..32767 */
    int32_t exp_rpm[NC_MAX_ACTUATORS];
    int32_t exp_current_ma[NC_MAX_ACTUATORS];
    uint8_t fault_code[NC_MAX_ACTUATORS];          /* nc_esc_fault_t */
    uint8_t stall_mask;
    uint8_t broken_prop_mask;
    uint8_t overcurrent_mask;
} nc_esc_obs_result_t;

int nc_esc_observer_init(
    nc_esc_observer_t *obs,
    uint8_t n_actuators,
    int32_t rpm_max,
    int32_t i_idle_ma,
    int32_t k_load_ma
);

int nc_esc_observer_update(
    nc_esc_observer_t *obs,
    const int16_t *cmd_q15,
    const int32_t *rpm_meas,
    const int32_t *current_ma,
    uint16_t dt_ms,
    nc_esc_obs_result_t *out_res
);

/* 6. Exact 3-DOF 18-Servo Hexapod Leg Inverse Kinematics */
typedef struct {
    int16_t l_coxa_mm;       /* default 38 mm */
    int16_t l_femur_mm;      /* default 95 mm */
    int16_t l_tibia_mm;      /* default 135 mm */
    int16_t body_z_mm;       /* default 85 mm */
    int16_t swing_height_mm; /* default 30 mm */
    int16_t park_z_mm;       /* default +45 mm ground clearance */
} nc_hexapod_ik_cfg_t;

typedef struct {
    int16_t  coxa_cdeg[NC_NUM_LIMBS];              /* centi-degrees (-9000..+9000) */
    int16_t  femur_cdeg[NC_NUM_LIMBS];             /* centi-degrees (-9000..+9000) */
    int16_t  tibia_cdeg[NC_NUM_LIMBS];             /* centi-degrees (-9000..+9000) */
    uint16_t pwm_us[NC_NUM_LIMBS][3];              /* 18 servo pulses [1000..2000 us] */
    int16_t  foot_z_mm[NC_NUM_LIMBS];              /* target foot height [mm] */
    uint8_t  parked_mask;                          /* bit i = 1 if damaged leg parked at +45 mm */
    uint8_t  reachable_mask;                       /* bit i = 1 if strictly inside reach */
} nc_hexapod_ik_out_t;

void nc_hexapod_ik_default_cfg(nc_hexapod_ik_cfg_t *cfg);

int nc_hexapod_ik_3dof_single(
    const nc_hexapod_ik_cfg_t *cfg,
    uint8_t limb_idx,
    int16_t foot_x_mm,
    int16_t foot_y_mm,
    int16_t foot_z_mm,
    int16_t out_cdeg[3],
    uint16_t out_pwm_us[3],
    uint8_t *out_reachable
);

int nc_hexapod_ik_3dof(
    const nc_hexapod_ik_cfg_t *cfg,
    const nc_cpg_cord_t *cord,
    const nc_cpg_state_t *cpg_state,
    nc_hexapod_ik_out_t *out_ik
);

#ifdef __cplusplus
}
#endif

#endif /* NECKCORD_H */
