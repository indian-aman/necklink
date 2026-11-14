/*
 * nl_mavlink.c - Zero-heap, zero-dependency ANSI C99 MAVLink v2 wire codec
 *
 * Copyright (c) 2026 Amandeep Singh Bains. SPDX-License-Identifier: MIT
 */

#include "nl_mavlink.h"
#include <string.h>

enum {
    MAV_PARSE_IDLE    = 0,
    MAV_PARSE_HEADER  = 1,
    MAV_PARSE_PAYLOAD = 2,
    MAV_PARSE_CRC1    = 3,
    MAV_PARSE_CRC2    = 4
};

static void put_u16_le(uint8_t *dst, uint16_t v)
{
    dst[0] = (uint8_t)(v & 0xFFu);
    dst[1] = (uint8_t)((v >> 8) & 0xFFu);
}

static void put_i16_le(uint8_t *dst, int16_t v)
{
    put_u16_le(dst, (uint16_t)v);
}

static void put_u32_le(uint8_t *dst, uint32_t v)
{
    dst[0] = (uint8_t)(v & 0xFFu);
    dst[1] = (uint8_t)((v >> 8) & 0xFFu);
    dst[2] = (uint8_t)((v >> 16) & 0xFFu);
    dst[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static void put_u64_le(uint8_t *dst, uint64_t v)
{
    put_u32_le(dst, (uint32_t)(v & 0xFFFFFFFFu));
    put_u32_le(dst + 4, (uint32_t)((v >> 32) & 0xFFFFFFFFu));
}

static uint16_t get_u16_le(const uint8_t *src)
{
    return (uint16_t)((uint16_t)src[0] | ((uint16_t)src[1] << 8));
}

static int16_t get_i16_le(const uint8_t *src)
{
    return (int16_t)get_u16_le(src);
}

static uint32_t get_u32_le(const uint8_t *src)
{
    return (uint32_t)src[0]
         | ((uint32_t)src[1] << 8)
         | ((uint32_t)src[2] << 16)
         | ((uint32_t)src[3] << 24);
}

static uint64_t get_u64_le(const uint8_t *src)
{
    return (uint64_t)get_u32_le(src) | ((uint64_t)get_u32_le(src + 4) << 32);
}

uint16_t nl_mav_crc_init(void)
{
    return 0xFFFFu;
}

uint16_t nl_mav_crc_accumulate(uint8_t byte_val, uint16_t crc)
{
    uint8_t tmp = (uint8_t)(byte_val ^ (uint8_t)(crc & 0xFFu));
    tmp = (uint8_t)(tmp ^ (uint8_t)(tmp << 4));
    return (uint16_t)((crc >> 8) ^ ((uint16_t)tmp << 8) ^ ((uint16_t)tmp << 3) ^ ((uint16_t)tmp >> 4));
}

uint16_t nl_mav_crc_buffer(const uint8_t *buf, size_t len, uint16_t crc)
{
    size_t i;
    for (i = 0u; i < len; ++i) {
        crc = nl_mav_crc_accumulate(buf[i], crc);
    }
    return crc;
}

int nl_mav_crc_extra_for_msgid(uint32_t msgid)
{
    switch (msgid) {
        case NL_MAV_MSG_HEARTBEAT:         return (int)NL_MAV_CRC_HEARTBEAT;
        case NL_MAV_MSG_SCALED_IMU:        return (int)NL_MAV_CRC_SCALED_IMU;
        case NL_MAV_MSG_MANUAL_CONTROL:    return (int)NL_MAV_CRC_MANUAL_CONTROL;
        case NL_MAV_MSG_STATUSTEXT:        return (int)NL_MAV_CRC_STATUSTEXT;
        case NL_MAV_MSG_OBSTACLE_DISTANCE: return (int)NL_MAV_CRC_OBSTACLE_DISTANCE;
        default:                           return -1;
    }
}

size_t nl_mav_pack_raw(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    uint32_t msgid,
    const uint8_t *payload,
    uint8_t full_payload_len,
    uint8_t crc_extra,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
)
{
    uint8_t trimmed_len = full_payload_len;
    uint8_t seq = 0u;
    uint16_t crc;

    if (out_buf == NULL || (full_payload_len > 0u && payload == NULL) || msgid > 0xFFFFFFu) {
        return 0u;
    }

    /* MAVLink v2 trims trailing zero bytes (keeping at least 1 byte if full_payload_len > 0) */
    while (trimmed_len > 1u && payload[trimmed_len - 1u] == 0u) {
        --trimmed_len;
    }

    if (inout_seq != NULL) {
        seq = *inout_seq;
        *inout_seq = (uint8_t)(seq + 1u);
    }

    out_buf[0] = NL_MAV2_STX;
    out_buf[1] = trimmed_len;
    out_buf[2] = 0u; /* incompat_flags */
    out_buf[3] = 0u; /* compat_flags */
    out_buf[4] = seq;
    out_buf[5] = sysid;
    out_buf[6] = compid;
    out_buf[7] = (uint8_t)(msgid & 0xFFu);
    out_buf[8] = (uint8_t)((msgid >> 8) & 0xFFu);
    out_buf[9] = (uint8_t)((msgid >> 16) & 0xFFu);

    if (trimmed_len > 0u) {
        memcpy(out_buf + NL_MAV2_HEADER_LEN, payload, trimmed_len);
    }

    crc = nl_mav_crc_init();
    crc = nl_mav_crc_buffer(out_buf + 1, 9u + (size_t)trimmed_len, crc);
    crc = nl_mav_crc_accumulate(crc_extra, crc);

    put_u16_le(out_buf + NL_MAV2_HEADER_LEN + trimmed_len, crc);
    return (size_t)NL_MAV2_HEADER_LEN + (size_t)trimmed_len + 2u;
}

size_t nl_mav_encode_heartbeat(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    const nl_mav_heartbeat_t *msg,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
)
{
    uint8_t p[9];
    if (msg == NULL) {
        return 0u;
    }
    put_u32_le(p + 0, msg->custom_mode);
    p[4] = msg->type;
    p[5] = msg->autopilot;
    p[6] = msg->base_mode;
    p[7] = msg->system_status;
    p[8] = (msg->mavlink_version == 0u) ? 3u : msg->mavlink_version;
    return nl_mav_pack_raw(sysid, compid, inout_seq, NL_MAV_MSG_HEARTBEAT, p, 9u, NL_MAV_CRC_HEARTBEAT, out_buf);
}

size_t nl_mav_encode_scaled_imu(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    const nl_mav_scaled_imu_t *msg,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
)
{
    uint8_t p[22];
    if (msg == NULL) {
        return 0u;
    }
    put_u32_le(p + 0, msg->time_boot_ms);
    put_i16_le(p + 4, msg->xacc_mg);
    put_i16_le(p + 6, msg->yacc_mg);
    put_i16_le(p + 8, msg->zacc_mg);
    put_i16_le(p + 10, msg->xgyro_mrad_s);
    put_i16_le(p + 12, msg->ygyro_mrad_s);
    put_i16_le(p + 14, msg->zgyro_mrad_s);
    put_i16_le(p + 16, msg->xmag_mgauss);
    put_i16_le(p + 18, msg->ymag_mgauss);
    put_i16_le(p + 20, msg->zmag_mgauss);
    return nl_mav_pack_raw(sysid, compid, inout_seq, NL_MAV_MSG_SCALED_IMU, p, 22u, NL_MAV_CRC_SCALED_IMU, out_buf);
}

size_t nl_mav_encode_manual_control(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    const nl_mav_manual_control_t *msg,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
)
{
    uint8_t p[11];
    if (msg == NULL) {
        return 0u;
    }
    put_i16_le(p + 0, msg->x);
    put_i16_le(p + 2, msg->y);
    put_i16_le(p + 4, msg->z);
    put_i16_le(p + 6, msg->r);
    put_u16_le(p + 8, msg->buttons);
    p[10] = msg->target;
    return nl_mav_pack_raw(sysid, compid, inout_seq, NL_MAV_MSG_MANUAL_CONTROL, p, 11u, NL_MAV_CRC_MANUAL_CONTROL, out_buf);
}

size_t nl_mav_encode_statustext(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    uint8_t severity,
    const char *text,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
)
{
    uint8_t p[51];
    size_t i;
    memset(p, 0, sizeof(p));
    p[0] = severity;
    if (text != NULL) {
        for (i = 0u; i < 50u && text[i] != '\0'; ++i) {
            p[1u + i] = (uint8_t)text[i];
        }
    }
    return nl_mav_pack_raw(sysid, compid, inout_seq, NL_MAV_MSG_STATUSTEXT, p, 51u, NL_MAV_CRC_STATUSTEXT, out_buf);
}

size_t nl_mav_encode_obstacle_distance(
    uint8_t sysid,
    uint8_t compid,
    uint8_t *inout_seq,
    const nl_mav_obstacle_distance_t *msg,
    uint8_t out_buf[NL_MAV2_MAX_FRAME_LEN]
)
{
    uint8_t p[158];
    size_t i;
    if (msg == NULL) {
        return 0u;
    }
    put_u64_le(p + 0, msg->time_usec);
    for (i = 0u; i < 72u; ++i) {
        put_u16_le(p + 8u + 2u * i, msg->distances_cm[i]);
    }
    put_u16_le(p + 152, msg->min_distance_cm);
    put_u16_le(p + 154, msg->max_distance_cm);
    p[156] = msg->sensor_type;
    p[157] = msg->increment_deg;
    return nl_mav_pack_raw(sysid, compid, inout_seq, NL_MAV_MSG_OBSTACLE_DISTANCE, p, 158u, NL_MAV_CRC_OBSTACLE_DISTANCE, out_buf);
}

void nl_mav_parser_init(nl_mav_parser_t *p)
{
    if (p != NULL) {
        memset(p, 0, sizeof(*p));
    }
}

int nl_mav_parse_byte(nl_mav_parser_t *p, uint8_t byte_val, nl_mav_frame_t *out_frame)
{
    if (p == NULL || out_frame == NULL) {
        return 0;
    }

    switch (p->state) {
        case MAV_PARSE_IDLE:
            if (byte_val == NL_MAV2_STX) {
                p->state = MAV_PARSE_HEADER;
                p->idx = 0u;
            }
            break;

        case MAV_PARSE_HEADER:
            p->header[p->idx++] = byte_val;
            if (p->idx == 9u) {
                p->expected_len = p->header[0];
                p->idx = 0u;
                p->state = (p->expected_len > 0u) ? MAV_PARSE_PAYLOAD : MAV_PARSE_CRC1;
            }
            break;

        case MAV_PARSE_PAYLOAD:
            p->payload[p->idx++] = byte_val;
            if (p->idx == p->expected_len) {
                p->state = MAV_PARSE_CRC1;
            }
            break;

        case MAV_PARSE_CRC1:
            p->crc_lo = byte_val;
            p->state = MAV_PARSE_CRC2;
            break;

        case MAV_PARSE_CRC2: {
            uint16_t rx_crc = (uint16_t)((uint16_t)p->crc_lo | ((uint16_t)byte_val << 8));
            uint32_t msgid = (uint32_t)p->header[6]
                           | ((uint32_t)p->header[7] << 8)
                           | ((uint32_t)p->header[8] << 16);
            int extra = nl_mav_crc_extra_for_msgid(msgid);
            p->state = MAV_PARSE_IDLE;

            if (extra >= 0) {
                uint16_t calc = nl_mav_crc_init();
                calc = nl_mav_crc_buffer(p->header, 9u, calc);
                calc = nl_mav_crc_buffer(p->payload, p->expected_len, calc);
                calc = nl_mav_crc_accumulate((uint8_t)extra, calc);
                if (calc == rx_crc) {
                    out_frame->msgid = msgid;
                    out_frame->seq = p->header[3];
                    out_frame->sysid = p->header[4];
                    out_frame->compid = p->header[5];
                    out_frame->payload_len = p->expected_len;
                    memset(out_frame->payload, 0, sizeof(out_frame->payload));
                    if (p->expected_len > 0u) {
                        memcpy(out_frame->payload, p->payload, p->expected_len);
                    }
                    ++p->rx_ok_count;
                    return 1;
                }
                ++p->rx_crc_errors;
            }
            break;
        }

        default:
            p->state = MAV_PARSE_IDLE;
            break;
    }
    return 0;
}

static void copy_zero_padded(const nl_mav_frame_t *f, uint8_t *dst, size_t full_len)
{
    size_t n = (f->payload_len < full_len) ? f->payload_len : full_len;
    memset(dst, 0, full_len);
    if (n > 0u) {
        memcpy(dst, f->payload, n);
    }
}

int nl_mav_decode_heartbeat(const nl_mav_frame_t *f, nl_mav_heartbeat_t *out)
{
    uint8_t p[9];
    if (f == NULL || out == NULL || f->msgid != NL_MAV_MSG_HEARTBEAT) {
        return -1;
    }
    copy_zero_padded(f, p, sizeof(p));
    out->custom_mode = get_u32_le(p + 0);
    out->type = p[4];
    out->autopilot = p[5];
    out->base_mode = p[6];
    out->system_status = p[7];
    out->mavlink_version = p[8];
    return 0;
}

int nl_mav_decode_scaled_imu(const nl_mav_frame_t *f, nl_mav_scaled_imu_t *out)
{
    uint8_t p[22];
    if (f == NULL || out == NULL || f->msgid != NL_MAV_MSG_SCALED_IMU) {
        return -1;
    }
    copy_zero_padded(f, p, sizeof(p));
    out->time_boot_ms = get_u32_le(p + 0);
    out->xacc_mg = get_i16_le(p + 4);
    out->yacc_mg = get_i16_le(p + 6);
    out->zacc_mg = get_i16_le(p + 8);
    out->xgyro_mrad_s = get_i16_le(p + 10);
    out->ygyro_mrad_s = get_i16_le(p + 12);
    out->zgyro_mrad_s = get_i16_le(p + 14);
    out->xmag_mgauss = get_i16_le(p + 16);
    out->ymag_mgauss = get_i16_le(p + 18);
    out->zmag_mgauss = get_i16_le(p + 20);
    return 0;
}

int nl_mav_decode_manual_control(const nl_mav_frame_t *f, nl_mav_manual_control_t *out)
{
    uint8_t p[11];
    if (f == NULL || out == NULL || f->msgid != NL_MAV_MSG_MANUAL_CONTROL) {
        return -1;
    }
    copy_zero_padded(f, p, sizeof(p));
    out->x = get_i16_le(p + 0);
    out->y = get_i16_le(p + 2);
    out->z = get_i16_le(p + 4);
    out->r = get_i16_le(p + 6);
    out->buttons = get_u16_le(p + 8);
    out->target = p[10];
    return 0;
}

int nl_mav_decode_statustext(const nl_mav_frame_t *f, nl_mav_statustext_t *out)
{
    uint8_t p[51];
    if (f == NULL || out == NULL || f->msgid != NL_MAV_MSG_STATUSTEXT) {
        return -1;
    }
    copy_zero_padded(f, p, sizeof(p));
    out->severity = p[0];
    memcpy(out->text, p + 1, 50u);
    return 0;
}

int nl_mav_decode_obstacle_distance(const nl_mav_frame_t *f, nl_mav_obstacle_distance_t *out)
{
    uint8_t p[158];
    size_t i;
    if (f == NULL || out == NULL || f->msgid != NL_MAV_MSG_OBSTACLE_DISTANCE) {
        return -1;
    }
    copy_zero_padded(f, p, sizeof(p));
    out->time_usec = get_u64_le(p + 0);
    for (i = 0u; i < 72u; ++i) {
        out->distances_cm[i] = get_u16_le(p + 8u + 2u * i);
    }
    out->min_distance_cm = get_u16_le(p + 152);
    out->max_distance_cm = get_u16_le(p + 154);
    out->sensor_type = p[156];
    out->increment_deg = p[157];
    return 0;
}
