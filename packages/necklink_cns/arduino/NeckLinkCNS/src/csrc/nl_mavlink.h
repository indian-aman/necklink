/*
 * nl_mavlink.h - Zero-heap, zero-dependency ANSI C99 MAVLink v2 wire codec
 *
 * Implements official MAVLink v2 packet framing (STX 0xFD, X.25 CRC-16/MCRF4XX,
 * payload zero-trimming, and official common.xml CRC_EXTRA bytes) for connecting
 * NeckLink CNS microcontrollers directly to ArduPilot / PX4 / QGroundControl
 * over UART (e.g., Serial1 @ 921600 baud) or UDP (port 14550).
 *
 * Supported official MAVLink v2 messages:
 *   - HEARTBEAT         (#0,   CRC_EXTRA = 50)
 *   - SCALED_IMU        (#26,  CRC_EXTRA = 170)
 *   - MANUAL_CONTROL    (#69,  CRC_EXTRA = 243)
 *   - STATUSTEXT        (#253, CRC_EXTRA = 83)
 *   - OBSTACLE_DISTANCE (#330, CRC_EXTRA = 23)
 *
 * Zero dynamic heap allocation. Strictly integer fixed-point arithmetic.
 * Copyright (c) 2026 Amandeep Singh Bains. SPDX-License-Identifier: MIT
 */

#ifndef NL_MAVLINK_H
#define NL_MAVLINK_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define NL_MAV2_STX                  0xFDu
#define NL_MAV2_HEADER_LEN           10u
#define NL_MAV2_MAX_PAYLOAD          255u
#define NL_MAV2_MAX_FRAME_LEN        (NL_MAV2_HEADER_LEN + NL_MAV2_MAX_PAYLOAD + 2u)

#define NL_MAV_MSG_HEARTBEAT         0u
#define NL_MAV_MSG_SCALED_IMU        26u
#define NL_MAV_MSG_MANUAL_CONTROL    69u
#define NL_MAV_MSG_STATUSTEXT        253u
#define NL_MAV_MSG_OBSTACLE_DISTANCE 330u

#define NL_MAV_CRC_HEARTBEAT         50u
#define NL_MAV_CRC_SCALED_IMU        170u
#define NL_MAV_CRC_MANUAL_CONTROL    243u
#define NL_MAV_CRC_STATUSTEXT        83u
#define NL_MAV_CRC_OBSTACLE_DISTANCE 23u

/* MAV_TYPE & MAV_AUTOPILOT constants */
#define NL_MAV_TYPE_ONBOARD_CONTROLLER 18u
#define NL_MAV_AUTOPILOT_INVALID       8u
#define NL_MAV_STATE_ACTIVE            4u
#define NL_MAV_SEVERITY_WARNING        4u
#define NL_MAV_SEVERITY_CRITICAL       2u
#define NL_MAV_DISTANCE_SENSOR_OPTICAL 2u

typedef struct {
    uint32_t custom_mode;
    uint8_t  type;
    uint8_t  autopilot;
    uint8_t  base_mode;
    uint8_t  system_status;
    uint8_t  mavlink_version;
} nl_mav_heartbeat_t;

typedef struct {
    uint32_t time_boot_ms;
    int16_t  xacc_mg;      /* milli-g */
    int16_t  yacc_mg;
    int16_t  zacc_mg;
    int16_t  xgyro_mrad_s; /* mrad/s */
    int16_t  ygyro_mrad_s;
    int16_t  zgyro_mrad_s;
    int16_t  xmag_mgauss;  /* milli-gauss */
    int16_t  ymag_mgauss;
    int16_t  zmag_mgauss;
} nl_mav_scaled_imu_t;

typedef struct {
    int16_t  x;       /* pitch / surge [-1000..+1000] */
    int16_t  y;       /* roll / sway   [-1000..+1000] */
    int16_t  z;       /* thrust        [0..1000] or [-1000..+1000] */
    int16_t  r;       /* yaw           [-1000..+1000] */
    uint16_t buttons;
    uint8_t  target;
} nl_mav_manual_control_t;

typedef struct {
    uint8_t severity;
    char    text[50];
} nl_mav_statustext_t;

typedef struct {
    uint64_t time_usec;
    uint16_t distances_cm[72]; /* 0xFFFF = unknown */
    uint16_t min_distance_cm;
    uint16_t max_distance_cm;
    uint8_t  sensor_type;
    uint8_t  increment_deg;
} nl_mav_obstacle_distance_t;

typedef struct {
    uint32_t msgid;
    uint8_t  sysid;
    uint8_t  compid;
    uint8_t  seq;
    uint8_t  payload_len;
    uint8_t  payload[NL_MAV2_MAX_PAYLOAD];
} nl_mav_frame_t;

typedef struct {
    uint8_t  state;
    uint8_t  expected_len;
    uint8_t  idx;
    uint8_t  header[9]; /* len..msgid[2] (excludes STX) */
    uint8_t  payload[NL_MAV2_MAX_PAYLOAD];
    uint8_t  crc_lo;
    uint32_t rx_ok_count;
    uint32_t rx_crc_errors;
} nl_mav_parser_t;

/* X.25 CRC-16/MCRF4XX helpers */
uint16_t nl_mav_crc_init(void);
uint16_t nl_mav_crc_accumulate(uint8_t byte_val, uint16_t crc);
uint16_t nl_mav_crc_buffer(const uint8_t *buf, size_t len, uint16_t crc);
int      nl_mav_crc_extra_for_msgid(uint32_t msgid);

/* Generic MAVLink v2 frame packer (applies MAVLink v2 trailing-zero trimming).
 * Returns total encoded wire length (12..267 bytes), or 0 on error. */
size_t nl_mav_pack_raw(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    uint32_t msgid,
    const uint8_t *payload,
    uint8_t full_payload_len,
    uint8_t crc_extra,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
);

/* Typed encoders */
size_t nl_mav_encode_heartbeat(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    const nl_mav_heartbeat_t *msg,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
);

size_t nl_mav_encode_scaled_imu(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    const nl_mav_scaled_imu_t *msg,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
);

size_t nl_mav_encode_manual_control(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    const nl_mav_manual_control_t *msg,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
);

size_t nl_mav_encode_statustext(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    uint8_t severity,
    const char *text,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
);

size_t nl_mav_encode_obstacle_distance(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    const nl_mav_obstacle_distance_t *msg,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
);

/* Streaming byte-by-byte parser & typed decoders */
void nl_mav_parser_init(nl_mav_parser_t *p);

/* Feed 1 byte; returns 1 when a valid, CRC-verified frame is written into out_frame, else 0. */
int nl_mav_parse_byte(nl_mav_parser_t *p, uint8_t byte_val, nl_mav_frame_t *out_frame);

int nl_mav_decode_heartbeat(const nl_mav_frame_t *f, nl_mav_heartbeat_t *out);
int nl_mav_decode_scaled_imu(const nl_mav_frame_t *f, nl_mav_scaled_imu_t *out);
int nl_mav_decode_manual_control(const nl_mav_frame_t *f, nl_mav_manual_control_t *out);
int nl_mav_decode_statustext(const nl_mav_frame_t *f, nl_mav_statustext_t *out);
int nl_mav_decode_obstacle_distance(const nl_mav_frame_t *f, nl_mav_obstacle_distance_t *out);

#ifdef __cplusplus
}
#endif

#endif /* NL_MAVLINK_H */
