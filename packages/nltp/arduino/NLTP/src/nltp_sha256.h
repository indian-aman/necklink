/*
 * nltp_sha256.h — FIPS 180-4 SHA-256 for NLTP file verification (C99, no heap, no floats).
 */
#ifndef NLTP_SHA256_H
#define NLTP_SHA256_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t state[8];
    uint64_t total_len;
    uint8_t buf[64];
    size_t buf_len;
} nltp_sha256_t;

void nltp_sha256_init(nltp_sha256_t *ctx);
void nltp_sha256_update(nltp_sha256_t *ctx, const uint8_t *data, size_t len);
void nltp_sha256_final(nltp_sha256_t *ctx, uint8_t out32[32]);
/* One-shot digest as 64 lowercase hex chars + NUL. */
void nltp_sha256_hex(const uint8_t *data, size_t len, char out_hex65[65]);
/* RFC 2104 HMAC-SHA256 (used for NLTP 1.4 session key derivation). */
void nltp_hmac_sha256(const uint8_t *key, size_t key_len, const uint8_t *msg, size_t msg_len, uint8_t out32[32]);

#ifdef __cplusplus
}
#endif

#endif /* NLTP_SHA256_H */
