/*
 * nltp_net.c — relay framing and STUN for nltp_core. See nltp_net.h.
 */
#include "nltp_net.h"

#include <string.h>

#include "nltp_sha256.h"

#define STUN_MAGIC_COOKIE 0x2112A442u
#define STUN_BINDING_REQUEST 0x0001u
#define STUN_BINDING_SUCCESS 0x0101u
#define STUN_ATTR_MAPPED_ADDRESS 0x0001u
#define STUN_ATTR_XOR_MAPPED_ADDRESS 0x0020u

static int is_space(char ch) { return ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r'; }

void nltp_room_tag(const char *room, uint8_t out_tag16[16]) {
    static const char prefix[] = "NLTP-RELAY-ROOM:";
    const char *b = room, *e;
    nltp_sha256_t ctx;
    uint8_t digest[32];
    while (is_space(*b)) ++b;
    e = b + strlen(b);
    while (e > b && is_space(e[-1])) --e;
    nltp_sha256_init(&ctx);
    nltp_sha256_update(&ctx, (const uint8_t *)prefix, sizeof(prefix) - 1u);
    nltp_sha256_update(&ctx, (const uint8_t *)b, (size_t)(e - b));
    nltp_sha256_final(&ctx, digest);
    memcpy(out_tag16, digest, 16);
}

size_t nltp_relay_wrap(uint8_t *out, size_t cap, const uint8_t tag16[16], uint8_t cmd,
                       const uint8_t *inner, size_t inner_len) {
    if (cap < NLTP_RELAY_HDR_BYTES + inner_len) return 0;
    out[0] = 'N';
    out[1] = 'R';
    out[2] = cmd;
    memcpy(out + 3, tag16, 16);
    if (inner_len) memmove(out + NLTP_RELAY_HDR_BYTES, inner, inner_len);
    return NLTP_RELAY_HDR_BYTES + inner_len;
}

int nltp_relay_unwrap(const uint8_t *raw, size_t len, const uint8_t tag16[16], uint8_t *cmd,
                      const uint8_t **inner, size_t *inner_len) {
    if (len < NLTP_RELAY_HDR_BYTES || raw[0] != 'N' || raw[1] != 'R') return 0;
    if (memcmp(raw + 3, tag16, 16) != 0) return 0;
    *cmd = raw[2];
    *inner = raw + NLTP_RELAY_HDR_BYTES;
    *inner_len = len - NLTP_RELAY_HDR_BYTES;
    return 1;
}

static uint16_t be16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }
static uint32_t be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

void nltp_stun_build_request(uint8_t out20[NLTP_STUN_REQUEST_BYTES], const uint8_t tid12[12]) {
    out20[0] = 0x00;
    out20[1] = 0x01;  /* Binding Request */
    out20[2] = 0x00;
    out20[3] = 0x00;  /* no attributes */
    out20[4] = 0x21;
    out20[5] = 0x12;
    out20[6] = 0xA4;
    out20[7] = 0x42;  /* magic cookie */
    memcpy(out20 + 8, tid12, 12);
}

int nltp_is_stun(const uint8_t *raw, size_t len) {
    return len >= 20u && be32(raw + 4) == STUN_MAGIC_COOKIE && len >= 20u + be16(raw + 2);
}

int nltp_stun_parse_response(const uint8_t *raw, size_t len, const uint8_t tid12[12],
                             uint32_t *ip_be, uint16_t *port_be) {
    size_t pos = 20, end;
    int have_fallback = 0;
    uint32_t fb_ip = 0;
    uint16_t fb_port = 0;
    if (!nltp_is_stun(raw, len) || be16(raw) != STUN_BINDING_SUCCESS) return 0;
    if (memcmp(raw + 8, tid12, 12) != 0) return 0;
    end = 20u + be16(raw + 2);
    if (end > len) end = len;
    while (pos + 4u <= end) {
        uint16_t type = be16(raw + pos), alen = be16(raw + pos + 2);
        const uint8_t *v = raw + pos + 4;
        if (pos + 4u + alen > end) break;
        if (type == STUN_ATTR_XOR_MAPPED_ADDRESS && alen >= 8u && v[1] == 0x01u) {
            uint16_t port = (uint16_t)(be16(v + 2) ^ (STUN_MAGIC_COOKIE >> 16));
            uint32_t ip = be32(v + 4) ^ STUN_MAGIC_COOKIE;
            uint8_t *pp = (uint8_t *)port_be, *ipp = (uint8_t *)ip_be;
            pp[0] = (uint8_t)(port >> 8);
            pp[1] = (uint8_t)port;
            ipp[0] = (uint8_t)(ip >> 24);
            ipp[1] = (uint8_t)(ip >> 16);
            ipp[2] = (uint8_t)(ip >> 8);
            ipp[3] = (uint8_t)ip;
            return 1;
        }
        if (type == STUN_ATTR_MAPPED_ADDRESS && alen >= 8u && v[1] == 0x01u) {
            uint8_t *pp = (uint8_t *)&fb_port, *ipp = (uint8_t *)&fb_ip;
            pp[0] = v[2];
            pp[1] = v[3];
            memcpy(ipp, v + 4, 4);
            have_fallback = 1;
        }
        pos += 4u + (((size_t)alen + 3u) & ~(size_t)3u);
    }
    if (have_fallback) {
        *ip_be = fb_ip;
        *port_be = fb_port;
        return 1;
    }
    return 0;
}
