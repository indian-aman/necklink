/*
 * nltp_sha256.c — FIPS 180-4 SHA-256 (C99, no heap, no floats).
 */
#include "nltp_sha256.h"

#include <string.h>

static const uint32_t K256[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
};

#define ROTR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha256_block(nltp_sha256_t *ctx, const uint8_t *p) {
    uint32_t w[64];
    uint32_t a, b, c, d, e, f, g, h;
    int i;
    for (i = 0; i < 16; ++i) {
        w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16) | ((uint32_t)p[4 * i + 2] << 8) | p[4 * i + 3];
    }
    for (i = 16; i < 64; ++i) {
        uint32_t s0 = ROTR(w[i - 15], 7) ^ ROTR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROTR(w[i - 2], 17) ^ ROTR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = ctx->state[0]; b = ctx->state[1]; c = ctx->state[2]; d = ctx->state[3];
    e = ctx->state[4]; f = ctx->state[5]; g = ctx->state[6]; h = ctx->state[7];
    for (i = 0; i < 64; ++i) {
        uint32_t t1 = h + (ROTR(e, 6) ^ ROTR(e, 11) ^ ROTR(e, 25)) + ((e & f) ^ (~e & g)) + K256[i] + w[i];
        uint32_t t2 = (ROTR(a, 2) ^ ROTR(a, 13) ^ ROTR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    ctx->state[0] += a; ctx->state[1] += b; ctx->state[2] += c; ctx->state[3] += d;
    ctx->state[4] += e; ctx->state[5] += f; ctx->state[6] += g; ctx->state[7] += h;
}

void nltp_sha256_init(nltp_sha256_t *ctx) {
    static const uint32_t iv[8] = {0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                                   0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};
    memcpy(ctx->state, iv, sizeof(iv));
    ctx->total_len = 0;
    ctx->buf_len = 0;
}

void nltp_sha256_update(nltp_sha256_t *ctx, const uint8_t *data, size_t len) {
    ctx->total_len += len;
    while (len > 0) {
        size_t take = 64u - ctx->buf_len;
        if (take > len) take = len;
        memcpy(ctx->buf + ctx->buf_len, data, take);
        ctx->buf_len += take;
        data += take;
        len -= take;
        if (ctx->buf_len == 64u) {
            sha256_block(ctx, ctx->buf);
            ctx->buf_len = 0;
        }
    }
}

void nltp_sha256_final(nltp_sha256_t *ctx, uint8_t out32[32]) {
    uint64_t bits = ctx->total_len * 8u;
    uint8_t pad = 0x80u;
    uint8_t zero = 0;
    uint8_t lenbuf[8];
    int i;
    nltp_sha256_update(ctx, &pad, 1);
    while (ctx->buf_len != 56u) nltp_sha256_update(ctx, &zero, 1);
    for (i = 0; i < 8; ++i) lenbuf[i] = (uint8_t)(bits >> (56 - 8 * i));
    nltp_sha256_update(ctx, lenbuf, 8);
    for (i = 0; i < 8; ++i) {
        out32[4 * i] = (uint8_t)(ctx->state[i] >> 24);
        out32[4 * i + 1] = (uint8_t)(ctx->state[i] >> 16);
        out32[4 * i + 2] = (uint8_t)(ctx->state[i] >> 8);
        out32[4 * i + 3] = (uint8_t)ctx->state[i];
    }
}

void nltp_sha256_hex(const uint8_t *data, size_t len, char out_hex65[65]) {
    static const char hexd[] = "0123456789abcdef";
    nltp_sha256_t ctx;
    uint8_t d[32];
    int i;
    nltp_sha256_init(&ctx);
    nltp_sha256_update(&ctx, data, len);
    nltp_sha256_final(&ctx, d);
    for (i = 0; i < 32; ++i) {
        out_hex65[2 * i] = hexd[d[i] >> 4];
        out_hex65[2 * i + 1] = hexd[d[i] & 15u];
    }
    out_hex65[64] = '\0';
}
