/*
 * necklink_cns.h - Unified Adult Drosophila Whole-CNS Edge Autonomy Controller (v2.0.0)
 *
 * Integrates all four BANC v888-grounded sensory-motor organs plus official MAVLink v2
 * framing inside a single < 24 KB static SRAM footprint (zero dynamic heap allocation,
 * strictly integer fixed-point arithmetic):
 *
 *   1. NeckSight v2.0.0   - Optic Lobe (R1-R6, T4/T5, LPLC2 + LC4 pole/wire, 3-DOF derotation)
 *   2. NeckCompass v2.0.0 - Central Complex (9-DOF tilt + wave gate, E-PG/Delta7/Plastic R, CPU4, PFL3/PFL2)
 *   3. NeckSense v2.0.0   - Antennal Lobe, Chordotonal Vibration & 2-Compartment KCg/KCab Mushroom Body
 *   4. NeckCord v2.0.0    - 4-Byte Cervical Frame, ESC Fault Observer, 4-DOF Active-Set & 18-DOF Leg IK
 *   5. nl_mavlink         - Wire-valid MAVLink v2 (0xFD, X.25 CRC-16 + CRC_EXTRA) autopilot codec
 *
 * Copyright (c) 2026 Amandeep Singh Bains. SPDX-License-Identifier: MIT
 */

#ifndef NECKLINK_CNS_H
#define NECKLINK_CNS_H

#ifndef NL_CNS_TINY
#define NL_CNS_TINY 1
#endif

#if NL_CNS_TINY && !defined(NS_TINY)
#define NS_TINY 1
#endif

#include <stddef.h>
#include <stdint.h>

#include "necksight.h"
#include "neckcompass.h"
#include "neckcord.h"
#include "necksense.h"
#include "nl_mavlink.h"

#ifdef __cplusplus
extern "C" {
#endif

#define NL_CNS_VERSION_MAJOR  2
#define NL_CNS_VERSION_MINOR  0
#define NL_CNS_VERSION_PATCH  0
#define NL_CNS_VERSION_STRING "2.0.0"

/* Empirical BANC v888 whole-CNS structural constants (157,789 proofread neurons) */
#define NL_BANC_TOTAL_NEURONS      157789u
#define NL_BANC_DESCENDING_COUNT   1316u
#define NL_BANC_ASCENDING_COUNT    1849u
#define NL_BANC_MOTOR_COUNT        805u
#define NL_BANC_LPLC2_DN_SYN       2896u
#define NL_BANC_LC4_LPLC1_DN_SYN   2053u  /* 1108 LC4 + 945 LPLC1 */

typedef enum {
    NL_VEHICLE_USV_T200      = 0,
    NL_VEHICLE_HEXACOPTER    = 1,
    NL_VEHICLE_OCTOCOPTER    = 2,
    NL_VEHICLE_HEXAPOD_18DOF = 3
} nl_vehicle_mode_t;

typedef enum {
    NL_MODE_CRUISE_HOME      = 0,
    NL_MODE_DODGE_LOOM       = 1,
    NL_MODE_MOONWALK_REVERSE = 2,
    NL_MODE_PLUME_CAST       = 3,
    NL_MODE_LIMP_HOME        = 4
} nl_behavior_mode_t;

typedef struct {
    uint16_t dt_ms;
    int32_t  speed_mm_s;          /* Forward ground/water speed [mm/s] */
    int16_t  cruise_drive_q15;    /* Nominal forward cruise command [0..32767] */
    uint8_t  return_home;         /* 1 = steer toward home vector via PFL3/PFL2, 0 = hold course */
    uint8_t  enable_plastic_cal;  /* 1 = run online R->EPG soft-iron learning */

    /* 9-Axis IMU + Magnetometer (mrad/s, milli-g, arbitrary mag LSB/mGauss) */
    int32_t  gx_mrad_s;
    int32_t  gy_mrad_s;
    int32_t  gz_mrad_s;
    int32_t  ax_mg;
    int32_t  ay_mg;
    int32_t  az_mg;
    int32_t  mx;
    int32_t  my;
    int32_t  mz;
    uint8_t  mag_valid;

    /* Optional camera input: either arbitrary HxW 8-bit frame (Q4 box-downsampled) or 32x32 grid */
    const uint8_t *cam_raw_u8;
    uint16_t       cam_h;
    uint16_t       cam_w;
    const uint8_t *cam_32x32;

    /* Optional 8-channel chemical / gas array in Q16.16 (NULL if absent) */
    const int32_t *chem8_q16;

    /* Optional raw int16 accelerometer/microphone vibration window (NULL if absent) */
    const int16_t *vib_samples_i16;
    uint16_t       vib_n_samples;

    /* Optional actuator feedback: either direct Q15 thrust feedback OR sensorless ESC RPM + mA */
    const int16_t *thrust_fb_q15;
    const int32_t *esc_rpm;
    const int32_t *esc_current_ma;
} nl_cns_inputs_t;

typedef struct {
    /* Central Complex readout */
    nc_bam_t heading_bam;
    int32_t  heading_cdeg;
    uint32_t home_dist_mm;
    nc_bam_t home_bearing_bam;

    /* Optic Lobe readout */
    uint16_t looming_score_q10;
    uint16_t lc4_bar_score_q10;
    uint16_t est_ttc_ms;
    uint8_t  loom_triggered;
    int8_t   avoid_x_q7;
    int8_t   avoid_y_q7;

    /* Mushroom Body & Chordotonal readout */
    int32_t  chem_novelty_q16;
    int32_t  whiff_rate_q16;
    int32_t  vib_novelty_q16;
    uint32_t predicted_class;

    /* BANC v888 Descending Neck Bottleneck state */
    int16_t  dnp01_brake_q15;
    int16_t  mdn_reverse_q15;
    int16_t  dna02_left_q15;
    int16_t  dna02_right_q15;
    uint8_t  behavior_mode;       /* nl_behavior_mode_t */
    uint8_t  cervical_wire_4b[4]; /* 4-byte CRC-4 descending command packet */
    nc_descending_frame_t desc_cmd;

    /* Ventral Nerve Cord motor outputs */
    int16_t  actuator_cmd_q15[NC_MAX_ACTUATORS];
    int16_t  actuator_health_q15[NC_MAX_ACTUATORS];
    int16_t  min_health_q15;
    uint8_t  isolated_mask;
    uint8_t  esc_broken_mask;
    uint16_t hexapod_pwm_us[NC_NUM_LIMBS][3];
    uint8_t  hexapod_parked_mask;
} nl_cns_outputs_t;

typedef struct {
    uint8_t  vehicle_mode;
    uint8_t  mav_sysid;
    uint8_t  mav_compid;
    uint8_t  mav_tx_seq;
    uint8_t  desc_seq;
    uint16_t cast_phase_bam;
    uint16_t loom_reverse_q10;    /* Moonwalker backward escape threshold (default 150 Q10) */
    int32_t  nov_alert_q16;       /* Mushroom Body novelty cast threshold (default 36045 ~ 0.55) */
    int32_t  vib_fault_thresh_q16;/* Vibration anomaly limp-home threshold (default 39322 ~ 0.60) */
    uint32_t tick_count;

    /* 1. Optic Lobe (< 14.7 KB) */
    necksight_state_t  sight;
    necksight_result_t last_sight;

    /* 2. Central Complex (~1.4 KB) */
    nc_tilt_t          tilt;
    nc_plastic_ring_t  plastic_ring;
    nc_navigator_t     nav;

    /* 3. Antennal Lobe, Chordotonal & Mushroom Body (~5.9 KB in NS_TINY=1) */
    ns_vib_fe_t        vib_fe;
    ns_antennal_t      chem_ant;
    ns_kenyon_t        chem_kc;
    ns_novelty_t       chem_nov;
    ns_antennal_t      vib_ant;
    ns_kenyon_t        vib_kc;
    ns_novelty_t       vib_nov;
    ns_valence_t       valence_clf;

    /* 4. Ventral Nerve Cord (< 0.4 KB) */
    nc_esc_observer_t    esc_obs;
    nc_shunting4d_t      thruster_cord;
    nc_cpg_cord_t        cpg_cord;
    nc_hexapod_ik_cfg_t  ik_cfg;

    /* 5. MAVLink v2 Streaming Parser (~0.28 KB) */
    nl_mav_parser_t      mav_rx;
} nl_cns_t;

#if NL_CNS_TINY
/* Compile-time guarantee: ENTIRE 4-organ + MAVLink whole-CNS controller < 25 KB static RAM */
typedef char nl_cns_static_sram_budget_check[
    (sizeof(nl_cns_t) < 25600u) ? 1 : -1
];
#endif

void nl_cns_init(nl_cns_t *cns, nl_vehicle_mode_t vehicle_mode);
void nl_cns_reset(nl_cns_t *cns);

/* Teach a 1-shot chemical or vibration prototype into the 2-compartment KCg+KCab classifier */
int nl_cns_learn_chemical(
    nl_cns_t *cns,
    const int32_t chem8_q16[8],
    uint32_t class_id,
    int32_t reward_q16,
    uint8_t consolidate_ltm
);

/* Execute one full closed-loop Whole-CNS sensorimotor tick */
int nl_cns_step(
    nl_cns_t *cns,
    const nl_cns_inputs_t *in,
    nl_cns_outputs_t *out
);

/* Encode latest CNS motor decision into a wire-valid MAVLink v2 MANUAL_CONTROL packet */
size_t nl_cns_encode_mavlink_control(
    nl_cns_t *cns,
    const nl_cns_outputs_t *out,
    uint8_t target_sysid,
    uint8_t out_pkt[NL_MAV2_MAX_FRAME_LEN]
);

/* WebAssembly / FFI flat integer telemetry & webcam entrypoints */
nl_cns_t *nl_wasm_get_cns(void);
uint32_t  nl_wasm_sizeof_cns(void);
void      nl_wasm_init(int32_t vehicle_mode);
uint8_t  *nl_wasm_frame_buf(void);
int32_t   nl_wasm_step_webcam_and_imu(
    uint16_t dt_ms,
    int32_t speed_mm_s,
    int32_t gx_mrad_s,
    int32_t gy_mrad_s,
    int32_t gz_mrad_s,
    int32_t ax_mg,
    int32_t ay_mg,
    int32_t mz_spike_cdeg,
    int32_t thruster1_health_q15,
    uint8_t has_camera
);
const int32_t *nl_wasm_get_telemetry(void);

#ifdef __cplusplus
}
#endif

#endif /* NECKLINK_CNS_H */
