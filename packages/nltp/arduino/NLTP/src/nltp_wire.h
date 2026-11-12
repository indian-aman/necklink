/*
 * nltp_wire.h — Standalone ANSI C99 Wire Framing, GF(2^8) Epistatic Cauchy FEC,
 *               RFC 8439 ChaCha20-Poly1305 AEAD & RFC 6479 Anti-Replay Spec for NLTP v1
 *
 * Byte-for-byte compatible with Python `NLTPServer` / `NLTPClient` (`necklink/nltp.py`).
 * Standard: Pure ANSI C99 (0 floating-point instructions, 0 dynamic heap allocations,
 *           constant-time AEAD tag verification, < 3 KB Flash on ESP32 / STM32 / ARM Cortex-M).
 */
#ifndef NLTP_WIRE_H
#define NLTP_WIRE_H

#include <stdint.h>
#include <stddef.h>

#define NLTP_MAGIC_0            0x4Eu  /* 'N' */
#define NLTP_MAGIC_1            0x4Cu  /* 'L' */
#define NLTP_VERSION            1u
#define NLTP_FLAG_ENCRYPTED     0x80u

#define NLTP_HEADER_BYTES       16u    /* Unencrypted CRC32 header */
#define NLTP_ENC_HEADER_BYTES   20u    /* Encrypted AAD header: magic(2)+vt(1)+fl(1)+cid(4)+seq64(8)+ts(4) */
#define NLTP_AEAD_TAG_BYTES     16u    /* 128-bit Poly1305 authentication tag */
#define NLTP_SHARD_HDR_BYTES    12u
#define NLTP_DEFAULT_SHARD_LEN  960u   /* <= 1,029 B UDP packet even with AEAD + Relay -> 0 IP fragmentation */
#define NLTP_DEFAULT_GEN_K      8u

/* Packet types (lower nibble of version_type) */
#define NLTP_PT_DATA_SHARD          0x01u
#define NLTP_PT_CRYPTIC_PARITY      0x02u
#define NLTP_PT_PROPRIOCEPTIVE_ACK  0x03u
#define NLTP_PT_STREAM_META         0x04u
#define NLTP_PT_STREAM_FIN_ACK      0x05u
#define NLTP_PT_REFLEX_MSG          0x06u
#define NLTP_PT_NEURAL_STATE        0x07u
#define NLTP_PT_PING                0x08u
#define NLTP_PT_PONG                0x09u
#define NLTP_PT_REFLEX_ACK          0x0Au  /* reliable reflex lane acknowledgement (v1.3.2+) */
#define NLTP_PT_HELLO               0x0Bu  /* NLTP 1.5 X25519+PSK session handshake (wire version 2) */
#define NLTP_PT_HELLO_ACK           0x0Cu
#define NLTP_PT_NO_SESSION          0x0Du  /* unauthenticated restart hint, see nltp_core.c */
#define NLTP_SESSION_VERSION        2u     /* version nibble of every encrypted frame since NLTP 1.4 */
#define NLTP_HELLO_PT_BYTES         48u    /* n_i (16 B) || pk_i (32 B) */
#define NLTP_HELLO_ACK_PT_BYTES     64u    /* n_i (16 B) || n_r (16 B) || pk_r (32 B) */

/* -------------------------------------------------------------------------
 * Little-Endian Unaligned-Safe Load/Store Helpers
 * ------------------------------------------------------------------------- */

static inline uint32_t nltp_read_u32_le(const uint8_t *p) {
    return ((uint32_t)p[0])
         | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16)
         | ((uint32_t)p[3] << 24);
}

static inline uint64_t nltp_read_u64_le(const uint8_t *p) {
    return ((uint64_t)nltp_read_u32_le(p)) | (((uint64_t)nltp_read_u32_le(p + 4)) << 32);
}

static inline void nltp_write_u16_le(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
}

static inline void nltp_write_u32_le(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static inline void nltp_write_u64_le(uint8_t *p, uint64_t v) {
    nltp_write_u32_le(p, (uint32_t)(v & 0xFFFFFFFFu));
    nltp_write_u32_le(p + 4, (uint32_t)(v >> 32));
}

static inline uint32_t nltp_rotl32(uint32_t x, int n) {
    return (x << n) | (x >> (32 - n));
}

/* -------------------------------------------------------------------------
 * IEEE 802.3 CRC32 (Table-Free Bitwise for Tiny MCU Flash)
 * ------------------------------------------------------------------------- */

static inline uint32_t nltp_crc32_ieee(const uint8_t *buf, size_t len, uint32_t init_crc) {
    uint32_t crc = ~init_crc;
    size_t i;
    int bit;
    for (i = 0; i < len; ++i) {
        crc ^= (uint32_t)buf[i];
        for (bit = 0; bit < 8; ++bit) {
            uint32_t mask = (uint32_t)-(int32_t)(crc & 1u);
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return ~crc;
}

/* -------------------------------------------------------------------------
 * GF(2^8) Algebraic Cauchy FEC over x^8 + x^4 + x^3 + x^2 + 1 (0x11D)
 * ------------------------------------------------------------------------- */

static inline uint8_t nltp_gf256_mul(uint8_t a, uint8_t b) {
    uint8_t p = 0;
    int i;
    for (i = 0; i < 8; ++i) {
        if (b & 1u) p ^= a;
        uint8_t hi = (uint8_t)(a & 0x80u);
        a = (uint8_t)(a << 1);
        if (hi) a ^= 0x1Du;
        b >>= 1;
    }
    return p;
}

static inline uint8_t nltp_gf256_inv(uint8_t a) {
    uint8_t res = 1u;
    uint8_t base = a;
    uint8_t exp = 254u;
    while (exp > 0u) {
        if (exp & 1u) res = nltp_gf256_mul(res, base);
        base = nltp_gf256_mul(base, base);
        exp >>= 1;
    }
    return res;
}

static inline void nltp_cauchy_row(uint8_t parity_offset, uint8_t k_shards, uint8_t *out_coeff) {
    /* Disjoint sets: x_r in [64..255], y_c in [0..63] -> (x_r ^ y_c) >= 64 > 0 always */
    uint8_t x_r = (uint8_t)(64u + (parity_offset % 192u));
    uint8_t c;
    for (c = 0; c < k_shards; ++c) {
        out_coeff[c] = nltp_gf256_inv((uint8_t)(x_r ^ (uint8_t)(c & 0x3Fu)));
    }
}

/* -------------------------------------------------------------------------
 * RFC 6479 512-Packet Sliding Anti-Replay Window (Matches `ReplayWindow64`)
 * ------------------------------------------------------------------------- */

#define NLTP_REPLAY_WINDOW_BITS 512u
#define NLTP_REPLAY_WORDS       8u

typedef struct {
    uint64_t highest_seq;
    uint64_t bitmap[NLTP_REPLAY_WORDS];
} nltp_replay_window_t;

static inline void nltp_replay_init(nltp_replay_window_t *w) {
    unsigned i;
    w->highest_seq = 0u;
    for (i = 0u; i < NLTP_REPLAY_WORDS; ++i) w->bitmap[i] = 0u;
}

static inline int nltp_replay_would_accept(const nltp_replay_window_t *w, uint64_t seq) {
    uint64_t diff;
    if (seq == 0u) return 0;
    if (seq > w->highest_seq) return 1;
    diff = w->highest_seq - seq;
    if (diff >= NLTP_REPLAY_WINDOW_BITS) return 0;
    return ((w->bitmap[diff >> 6] & ((uint64_t)1u << (diff & 63u))) == 0u) ? 1 : 0;
}

static inline int nltp_replay_accept(nltp_replay_window_t *w, uint64_t seq) {
    if (!nltp_replay_would_accept(w, seq)) return 0;
    if (seq > w->highest_seq) {
        uint64_t shift = seq - w->highest_seq;
        if (shift >= NLTP_REPLAY_WINDOW_BITS) {
            unsigned i;
            for (i = 1u; i < NLTP_REPLAY_WORDS; ++i) w->bitmap[i] = 0u;
            w->bitmap[0] = 1u;
        } else {
            unsigned wshift = (unsigned)(shift >> 6);
            unsigned bshift = (unsigned)(shift & 63u);
            int i;
            if (wshift > 0u) {
                for (i = (int)NLTP_REPLAY_WORDS - 1; i >= (int)wshift; --i) {
                    w->bitmap[i] = w->bitmap[i - (int)wshift];
                }
                for (i = (int)wshift - 1; i >= 0; --i) {
                    w->bitmap[i] = 0u;
                }
            }
            if (bshift > 0u) {
                for (i = (int)NLTP_REPLAY_WORDS - 1; i > 0; --i) {
                    w->bitmap[i] = (w->bitmap[i] << bshift) | (w->bitmap[i - 1] >> (64u - bshift));
                }
                w->bitmap[0] <<= bshift;
            }
            w->bitmap[0] |= 1u;
        }
        w->highest_seq = seq;
    } else {
        uint64_t diff = w->highest_seq - seq;
        w->bitmap[diff >> 6] |= ((uint64_t)1u << (diff & 63u));
    }
    return 1;
}

static inline void nltp_wipe_bytes(void *buf, size_t n) {
    volatile uint8_t *p = (volatile uint8_t *)buf;
    while (n--) *p++ = 0u;
}

/* -------------------------------------------------------------------------
 * RFC 7748 X25519 Ephemeral Diffie-Hellman (Zero-Malloc, Constant-Time)
 * ------------------------------------------------------------------------- */

typedef int64_t nltp_fe25519_t[16];

static inline void nltp_fe25519_car(nltp_fe25519_t o) {
    int i;
    for (i = 0; i < 16; ++i) {
        int64_t r = (int64_t)((uint64_t)o[i] & 0xFFFFu);
        int64_t c = (o[i] - r) / 65536;
        o[i] = r;
        if (i < 15) o[i + 1] += c;
        else o[0] += 38 * c;
    }
}

static inline void nltp_fe25519_add(nltp_fe25519_t o, const nltp_fe25519_t a, const nltp_fe25519_t b) {
    int i;
    for (i = 0; i < 16; ++i) o[i] = a[i] + b[i];
}

static inline void nltp_fe25519_sub(nltp_fe25519_t o, const nltp_fe25519_t a, const nltp_fe25519_t b) {
    int i;
    for (i = 0; i < 16; ++i) o[i] = a[i] - b[i];
}

static inline void nltp_fe25519_cswap(nltp_fe25519_t p, nltp_fe25519_t q, int b) {
    int64_t mask = -(int64_t)b;
    int i;
    for (i = 0; i < 16; ++i) {
        int64_t t = mask & (p[i] ^ q[i]);
        p[i] ^= t;
        q[i] ^= t;
    }
}

static inline void nltp_fe25519_mul(nltp_fe25519_t o, const nltp_fe25519_t a, const nltp_fe25519_t b) {
    int64_t t[31];
    int i, j;
    for (i = 0; i < 31; ++i) t[i] = 0;
    for (i = 0; i < 16; ++i) {
        for (j = 0; j < 16; ++j) {
            t[i + j] += a[i] * b[j];
        }
    }
    for (i = 0; i < 15; ++i) t[i] += 38 * t[i + 16];
    for (i = 0; i < 16; ++i) o[i] = t[i];
    nltp_fe25519_car(o);
    nltp_fe25519_car(o);
}

static inline void nltp_fe25519_inv(nltp_fe25519_t o, const nltp_fe25519_t in_fe) {
    nltp_fe25519_t c;
    int a;
    for (a = 0; a < 16; ++a) c[a] = in_fe[a];
    for (a = 253; a >= 0; --a) {
        nltp_fe25519_mul(c, c, c);
        if (a != 2 && a != 4) nltp_fe25519_mul(c, c, in_fe);
    }
    for (a = 0; a < 16; ++a) o[a] = c[a];
}

static inline void nltp_fe25519_pack(uint8_t out32[32], const nltp_fe25519_t n) {
    nltp_fe25519_t t, m;
    int i, j, b;
    for (i = 0; i < 16; ++i) t[i] = n[i];
    nltp_fe25519_car(t);
    nltp_fe25519_car(t);
    nltp_fe25519_car(t);
    for (j = 0; j < 2; ++j) {
        m[0] = t[0] - 0xFFED;
        for (i = 1; i < 15; ++i) {
            int64_t borrow = (int64_t)((uint64_t)m[i - 1] >> 63);
            m[i] = t[i] - 0xFFFF - borrow;
            m[i - 1] = (int64_t)((uint64_t)m[i - 1] & 0xFFFFu);
        }
        {
            int64_t borrow = (int64_t)((uint64_t)m[14] >> 63);
            m[15] = t[15] - 0x7FFF - borrow;
            b = (int)((uint64_t)m[15] >> 63);
            m[14] = (int64_t)((uint64_t)m[14] & 0xFFFFu);
            nltp_fe25519_cswap(t, m, 1 - b);
        }
    }
    for (i = 0; i < 16; ++i) {
        out32[2 * i]     = (uint8_t)((uint64_t)t[i] & 0xFFu);
        out32[2 * i + 1] = (uint8_t)(((uint64_t)t[i] >> 8) & 0xFFu);
    }
}

/*
 * RFC 7748 X25519 scalar multiplication: computes `out32 = X25519(scalar32, u32)`.
 * Returns 1 if the shared secret is non-zero, or 0 if `u32` is a low-order point (RFC 7748 s6.1).
 */
static inline int nltp_x25519_scalarmult(
    uint8_t out32[32],
    const uint8_t scalar32[32],
    const uint8_t u32[32]
) {
    uint8_t z_clamp[32], u_masked[32];
    nltp_fe25519_t x_1, x_2, z_2, x_3, z_3, a24_fe;
    nltp_fe25519_t A, AA, B, BB, E, C, D, DA, CB, t0, t1;
    int i, t, swap = 0;
    uint8_t nz = 0u;

    for (i = 0; i < 32; ++i) {
        z_clamp[i] = scalar32[i];
        u_masked[i] = u32[i];
    }
    z_clamp[0] &= 248u;
    z_clamp[31] &= 127u;
    z_clamp[31] |= 64u;
    u_masked[31] &= 127u;

    for (i = 0; i < 16; ++i) {
        x_1[i] = (int64_t)u_masked[2 * i] | ((int64_t)u_masked[2 * i + 1] << 8);
        x_2[i] = 0;
        z_2[i] = 0;
        x_3[i] = x_1[i];
        z_3[i] = 0;
        a24_fe[i] = 0;
    }
    x_2[0] = 1;
    z_3[0] = 1;
    a24_fe[0] = 0xDB41;
    a24_fe[1] = 1;

    for (t = 254; t >= 0; --t) {
        int k_t = (int)((z_clamp[t >> 3] >> (t & 7)) & 1u);
        swap ^= k_t;
        nltp_fe25519_cswap(x_2, x_3, swap);
        nltp_fe25519_cswap(z_2, z_3, swap);
        swap = k_t;

        nltp_fe25519_add(A, x_2, z_2);
        nltp_fe25519_mul(AA, A, A);
        nltp_fe25519_sub(B, x_2, z_2);
        nltp_fe25519_mul(BB, B, B);
        nltp_fe25519_sub(E, AA, BB);
        nltp_fe25519_add(C, x_3, z_3);
        nltp_fe25519_sub(D, x_3, z_3);
        nltp_fe25519_mul(DA, D, A);
        nltp_fe25519_mul(CB, C, B);

        nltp_fe25519_add(t0, DA, CB);
        nltp_fe25519_mul(x_3, t0, t0);
        nltp_fe25519_sub(t1, DA, CB);
        nltp_fe25519_mul(t1, t1, t1);
        nltp_fe25519_mul(z_3, x_1, t1);

        nltp_fe25519_mul(x_2, AA, BB);
        nltp_fe25519_mul(t0, a24_fe, E);
        nltp_fe25519_add(t0, AA, t0);
        nltp_fe25519_mul(z_2, E, t0);
    }

    nltp_fe25519_cswap(x_2, x_3, swap);
    nltp_fe25519_cswap(z_2, z_3, swap);
    nltp_fe25519_inv(z_2, z_2);
    nltp_fe25519_mul(x_2, x_2, z_2);
    nltp_fe25519_pack(out32, x_2);
    nltp_wipe_bytes(z_clamp, sizeof(z_clamp));

    for (i = 0; i < 32; ++i) nz |= out32[i];
    return nz != 0u ? 1 : 0;
}

static inline void nltp_x25519_public_key(uint8_t out32[32], const uint8_t scalar32[32]) {
    uint8_t base9[32];
    int i;
    base9[0] = 9u;
    for (i = 1; i < 32; ++i) base9[i] = 0u;
    (void)nltp_x25519_scalarmult(out32, scalar32, base9);
}

/* -------------------------------------------------------------------------
 * RFC 8439 IETF ChaCha20-Poly1305 AEAD (Zero-Malloc, Constant-Time)
 * ------------------------------------------------------------------------- */

#define NLTP_CHACHA_QR(a, b, c, d) do { \
    a += b; d ^= a; d = nltp_rotl32(d, 16); \
    c += d; b ^= c; b = nltp_rotl32(b, 12); \
    a += b; d ^= a; d = nltp_rotl32(d, 8);  \
    c += d; b ^= c; b = nltp_rotl32(b, 7);  \
} while (0)

static inline void nltp_chacha20_block(
    const uint8_t key32[32],
    uint32_t counter,
    const uint8_t nonce12[12],
    uint8_t out64[64]
) {
    uint32_t st[16], x[16];
    int i;
    st[0] = 0x61707865u;
    st[1] = 0x3320646eu;
    st[2] = 0x79622d32u;
    st[3] = 0x6b206574u;
    for (i = 0; i < 8; ++i) st[4 + i] = nltp_read_u32_le(key32 + 4 * i);
    st[12] = counter;
    st[13] = nltp_read_u32_le(nonce12 + 0);
    st[14] = nltp_read_u32_le(nonce12 + 4);
    st[15] = nltp_read_u32_le(nonce12 + 8);

    for (i = 0; i < 16; ++i) x[i] = st[i];
    for (i = 0; i < 10; ++i) {
        NLTP_CHACHA_QR(x[0], x[4], x[ 8], x[12]);
        NLTP_CHACHA_QR(x[1], x[5], x[ 9], x[13]);
        NLTP_CHACHA_QR(x[2], x[6], x[10], x[14]);
        NLTP_CHACHA_QR(x[3], x[7], x[11], x[15]);
        NLTP_CHACHA_QR(x[0], x[5], x[10], x[15]);
        NLTP_CHACHA_QR(x[1], x[6], x[11], x[12]);
        NLTP_CHACHA_QR(x[2], x[7], x[ 8], x[13]);
        NLTP_CHACHA_QR(x[3], x[4], x[ 9], x[14]);
    }
    for (i = 0; i < 16; ++i) {
        nltp_write_u32_le(out64 + 4 * i, x[i] + st[i]);
    }
}

static inline void nltp_chacha20_xor(
    const uint8_t key32[32],
    uint32_t init_counter,
    const uint8_t nonce12[12],
    const uint8_t *in_buf,
    uint8_t *out_buf,
    size_t len
) {
    uint8_t ks[64];
    uint32_t ctr = init_counter;
    size_t off = 0, i;
    while (off < len) {
        size_t take = (len - off < 64u) ? (len - off) : 64u;
        nltp_chacha20_block(key32, ctr, nonce12, ks);
        for (i = 0; i < take; ++i) {
            out_buf[off + i] = (uint8_t)(in_buf[off + i] ^ ks[i]);
        }
        off += take;
        ctr += 1u;
    }
}

typedef struct {
    uint32_t r[5];
    uint32_t s[4];
    uint32_t h[5];
} nltp_poly1305_t;

static inline void nltp_poly1305_init(nltp_poly1305_t *ctx, const uint8_t otk32[32]) {
    uint8_t r_clamped[16];
    int i;
    for (i = 0; i < 16; ++i) r_clamped[i] = otk32[i];
    r_clamped[ 3] &= 15u;
    r_clamped[ 7] &= 15u;
    r_clamped[11] &= 15u;
    r_clamped[15] &= 15u;
    r_clamped[ 4] &= 252u;
    r_clamped[ 8] &= 252u;
    r_clamped[12] &= 252u;

    ctx->r[0] = (nltp_read_u32_le(r_clamped +  0)     ) & 0x3FFFFFFu;
    ctx->r[1] = (nltp_read_u32_le(r_clamped +  3) >> 2) & 0x3FFFFFFu;
    ctx->r[2] = (nltp_read_u32_le(r_clamped +  6) >> 4) & 0x3FFFFFFu;
    ctx->r[3] = (nltp_read_u32_le(r_clamped +  9) >> 6) & 0x3FFFFFFu;
    ctx->r[4] = (nltp_read_u32_le(r_clamped + 12) >> 8) & 0x3FFFFFFu;

    for (i = 0; i < 4; ++i) ctx->s[i] = nltp_read_u32_le(otk32 + 16 + 4 * i);
    for (i = 0; i < 5; ++i) ctx->h[i] = 0u;
}

static inline void nltp_poly1305_block16(nltp_poly1305_t *ctx, const uint8_t b[16]) {
    uint32_t r0 = ctx->r[0], r1 = ctx->r[1], r2 = ctx->r[2], r3 = ctx->r[3], r4 = ctx->r[4];
    uint32_t s1 = r1 * 5u, s2 = r2 * 5u, s3 = r3 * 5u, s4 = r4 * 5u;
    uint32_t h0 = ctx->h[0] + ((nltp_read_u32_le(b +  0)     ) & 0x3FFFFFFu);
    uint32_t h1 = ctx->h[1] + ((nltp_read_u32_le(b +  3) >> 2) & 0x3FFFFFFu);
    uint32_t h2 = ctx->h[2] + ((nltp_read_u32_le(b +  6) >> 4) & 0x3FFFFFFu);
    uint32_t h3 = ctx->h[3] + ((nltp_read_u32_le(b +  9) >> 6) & 0x3FFFFFFu);
    uint32_t h4 = ctx->h[4] + ((nltp_read_u32_le(b + 12) >> 8) | (1u << 24));

    uint64_t d0 = (uint64_t)h0*r0 + (uint64_t)h1*s4 + (uint64_t)h2*s3 + (uint64_t)h3*s2 + (uint64_t)h4*s1;
    uint64_t d1 = (uint64_t)h0*r1 + (uint64_t)h1*r0 + (uint64_t)h2*s4 + (uint64_t)h3*s3 + (uint64_t)h4*s2;
    uint64_t d2 = (uint64_t)h0*r2 + (uint64_t)h1*r1 + (uint64_t)h2*r0 + (uint64_t)h3*s4 + (uint64_t)h4*s3;
    uint64_t d3 = (uint64_t)h0*r3 + (uint64_t)h1*r2 + (uint64_t)h2*r1 + (uint64_t)h3*r0 + (uint64_t)h4*s4;
    uint64_t d4 = (uint64_t)h0*r4 + (uint64_t)h1*r3 + (uint64_t)h2*r2 + (uint64_t)h3*r1 + (uint64_t)h4*r0;

    uint32_t c;
    c = (uint32_t)(d0 >> 26); h0 = (uint32_t)d0 & 0x3FFFFFFu; d1 += c;
    c = (uint32_t)(d1 >> 26); h1 = (uint32_t)d1 & 0x3FFFFFFu; d2 += c;
    c = (uint32_t)(d2 >> 26); h2 = (uint32_t)d2 & 0x3FFFFFFu; d3 += c;
    c = (uint32_t)(d3 >> 26); h3 = (uint32_t)d3 & 0x3FFFFFFu; d4 += c;
    c = (uint32_t)(d4 >> 26); h4 = (uint32_t)d4 & 0x3FFFFFFu; h0 += c * 5u;
    c = h0 >> 26;             h0 &= 0x3FFFFFFu;               h1 += c;

    ctx->h[0] = h0; ctx->h[1] = h1; ctx->h[2] = h2; ctx->h[3] = h3; ctx->h[4] = h4;
}

static inline void nltp_poly1305_feed_padded16(nltp_poly1305_t *ctx, const uint8_t *buf, size_t len) {
    size_t off = 0;
    while (off + 16u <= len) {
        nltp_poly1305_block16(ctx, buf + off);
        off += 16u;
    }
    if (off < len) {
        uint8_t tail[16];
        size_t i, rem = len - off;
        for (i = 0; i < rem; ++i) tail[i] = buf[off + i];
        for (; i < 16u; ++i) tail[i] = 0u;
        nltp_poly1305_block16(ctx, tail);
    }
}

static inline void nltp_poly1305_finish(nltp_poly1305_t *ctx, uint8_t tag16[16]) {
    uint32_t h0 = ctx->h[0], h1 = ctx->h[1], h2 = ctx->h[2], h3 = ctx->h[3], h4 = ctx->h[4];
    uint32_t c;
    c = h1 >> 26; h1 &= 0x3FFFFFFu; h2 += c;
    c = h2 >> 26; h2 &= 0x3FFFFFFu; h3 += c;
    c = h3 >> 26; h3 &= 0x3FFFFFFu; h4 += c;
    c = h4 >> 26; h4 &= 0x3FFFFFFu; h0 += c * 5u;
    c = h0 >> 26; h0 &= 0x3FFFFFFu; h1 += c;

    uint32_t g0 = h0 + 5u; c = g0 >> 26; g0 &= 0x3FFFFFFu;
    uint32_t g1 = h1 + c;  c = g1 >> 26; g1 &= 0x3FFFFFFu;
    uint32_t g2 = h2 + c;  c = g2 >> 26; g2 &= 0x3FFFFFFu;
    uint32_t g3 = h3 + c;  c = g3 >> 26; g3 &= 0x3FFFFFFu;
    uint32_t g4 = h4 + c - (1u << 26);

    uint32_t mask = (g4 >> 31) - 1u;
    g0 &= mask; g1 &= mask; g2 &= mask; g3 &= mask; g4 &= mask;
    mask = ~mask;
    h0 = (h0 & mask) | g0;
    h1 = (h1 & mask) | g1;
    h2 = (h2 & mask) | g2;
    h3 = (h3 & mask) | g3;
    h4 = (h4 & mask) | g4;

    uint32_t w0 = (h0      ) | (h1 << 26);
    uint32_t w1 = (h1 >>  6) | (h2 << 20);
    uint32_t w2 = (h2 >> 12) | (h3 << 14);
    uint32_t w3 = (h3 >> 18) | (h4 <<  8);

    uint64_t f;
    f = (uint64_t)w0 + ctx->s[0];              w0 = (uint32_t)f;
    f = (uint64_t)w1 + ctx->s[1] + (f >> 32);  w1 = (uint32_t)f;
    f = (uint64_t)w2 + ctx->s[2] + (f >> 32);  w2 = (uint32_t)f;
    f = (uint64_t)w3 + ctx->s[3] + (f >> 32);  w3 = (uint32_t)f;

    nltp_write_u32_le(tag16 +  0, w0);
    nltp_write_u32_le(tag16 +  4, w1);
    nltp_write_u32_le(tag16 +  8, w2);
    nltp_write_u32_le(tag16 + 12, w3);
}

static inline void nltp_aead_compute_tag(
    const uint8_t key32[32],
    const uint8_t nonce12[12],
    const uint8_t *aad,
    size_t aad_len,
    const uint8_t *ct,
    size_t ct_len,
    uint8_t out_tag16[16]
) {
    uint8_t block0[64];
    uint8_t lengths16[16];
    nltp_poly1305_t poly;

    nltp_chacha20_block(key32, 0u, nonce12, block0);
    nltp_poly1305_init(&poly, block0);
    nltp_poly1305_feed_padded16(&poly, aad, aad_len);
    nltp_poly1305_feed_padded16(&poly, ct, ct_len);
    nltp_write_u64_le(lengths16 + 0, (uint64_t)aad_len);
    nltp_write_u64_le(lengths16 + 8, (uint64_t)ct_len);
    nltp_poly1305_block16(&poly, lengths16);
    nltp_poly1305_finish(&poly, out_tag16);
}

/* -------------------------------------------------------------------------
 * NLTP v1 Plaintext & Encrypted Frame Pack / Unpack API
 * ------------------------------------------------------------------------- */

static inline size_t nltp_pack_header(
    uint8_t *out_hdr16,
    uint8_t pkt_type,
    uint8_t flags,
    uint32_t conn_id,
    uint32_t ts_ms,
    const uint8_t *payload,
    size_t payload_len
) {
    out_hdr16[0] = NLTP_MAGIC_0;
    out_hdr16[1] = NLTP_MAGIC_1;
    out_hdr16[2] = (uint8_t)(((NLTP_VERSION & 0x0Fu) << 4) | (pkt_type & 0x0Fu));
    out_hdr16[3] = (uint8_t)(flags & 0x7Fu);
    nltp_write_u32_le(out_hdr16 + 4, conn_id);
    nltp_write_u32_le(out_hdr16 + 8, 0u);
    nltp_write_u32_le(out_hdr16 + 12, ts_ms);

    uint32_t crc = nltp_crc32_ieee(out_hdr16, NLTP_HEADER_BYTES, 0u);
    if (payload_len > 0u && payload != 0) {
        crc = nltp_crc32_ieee(payload, payload_len, crc);
    }
    nltp_write_u32_le(out_hdr16 + 8, crc);
    return NLTP_HEADER_BYTES;
}

/*
 * Packs a full Internet-ready ChaCha20-Poly1305 encrypted NLTP v1 packet into `out_pkt`
 * (`out_pkt` must have capacity >= `NLTP_ENC_HEADER_BYTES + payload_len + NLTP_AEAD_TAG_BYTES`).
 * Matches Python `pack_nltp_frame(..., aead=AEADCipherContext(key32))` byte-for-byte!
 */
static inline size_t nltp_pack_encrypted_frame_v(
    uint8_t *out_pkt,
    const uint8_t key32[32],
    uint8_t version,
    uint8_t pkt_type,
    uint8_t flags,
    uint32_t conn_id,
    uint64_t seq64,
    uint32_t ts_ms,
    const uint8_t *plaintext,
    size_t plaintext_len
) {
    uint8_t nonce12[12];
    out_pkt[0] = NLTP_MAGIC_0;
    out_pkt[1] = NLTP_MAGIC_1;
    out_pkt[2] = (uint8_t)(((version & 0x0Fu) << 4) | (pkt_type & 0x0Fu));
    out_pkt[3] = (uint8_t)((flags & 0x7Fu) | NLTP_FLAG_ENCRYPTED);
    nltp_write_u32_le(out_pkt + 4, conn_id);
    nltp_write_u64_le(out_pkt + 8, seq64);
    nltp_write_u32_le(out_pkt + 16, ts_ms);

    nltp_write_u32_le(nonce12 + 0, conn_id);
    nltp_write_u64_le(nonce12 + 4, seq64);

    if (plaintext_len > 0u && plaintext != 0) {
        nltp_chacha20_xor(key32, 1u, nonce12, plaintext, out_pkt + NLTP_ENC_HEADER_BYTES, plaintext_len);
    }
    nltp_aead_compute_tag(
        key32,
        nonce12,
        out_pkt,
        NLTP_ENC_HEADER_BYTES,
        out_pkt + NLTP_ENC_HEADER_BYTES,
        plaintext_len,
        out_pkt + NLTP_ENC_HEADER_BYTES + plaintext_len
    );
    return NLTP_ENC_HEADER_BYTES + plaintext_len + NLTP_AEAD_TAG_BYTES;
}

/* NLTP 1.3.x frame sealed directly with a key (wire version 1). NLTP 1.4 endpoints seal data frames with
   per-session keys at version NLTP_SESSION_VERSION and reject version-1 encrypted frames. */
static inline size_t nltp_pack_encrypted_frame(
    uint8_t *out_pkt,
    const uint8_t key32[32],
    uint8_t pkt_type,
    uint8_t flags,
    uint32_t conn_id,
    uint64_t seq64,
    uint32_t ts_ms,
    const uint8_t *plaintext,
    size_t plaintext_len
) {
    return nltp_pack_encrypted_frame_v(out_pkt, key32, NLTP_VERSION, pkt_type, flags, conn_id, seq64, ts_ms,
                                       plaintext, plaintext_len);
}

/*
 * Verifies Poly1305 MAC + RFC 6479 64-bit replay window in constant time BEFORE touching `out_pt`.
 * Returns 1 on authentic fresh packet (`*out_pt_len` set), or 0 if forged/tampered/replayed/downgraded.
 */
static inline int nltp_unpack_encrypted_frame_v(
    const uint8_t *raw_pkt,
    size_t raw_len,
    const uint8_t key32[32],
    uint8_t version,
    nltp_replay_window_t *replay_win,
    uint8_t *out_pkt_type,
    uint8_t *out_flags,
    uint32_t *out_conn_id,
    uint32_t *out_ts_ms,
    uint8_t *out_pt,
    size_t *out_pt_len
) {
    if (raw_len < (NLTP_ENC_HEADER_BYTES + NLTP_AEAD_TAG_BYTES)) return 0;
    if (raw_pkt[0] != NLTP_MAGIC_0 || raw_pkt[1] != NLTP_MAGIC_1) return 0;
    uint8_t ver_type = raw_pkt[2];
    uint8_t flags = raw_pkt[3];
    if ((ver_type >> 4) != version) return 0;
    if ((flags & NLTP_FLAG_ENCRYPTED) == 0u) return 0;

    uint32_t cid = nltp_read_u32_le(raw_pkt + 4);
    uint64_t seq64 = nltp_read_u64_le(raw_pkt + 8);
    uint32_t ts_ms = nltp_read_u32_le(raw_pkt + 16);

    if (replay_win != 0 && !nltp_replay_would_accept(replay_win, seq64)) {
        return 0;
    }

    size_t ct_len = raw_len - NLTP_ENC_HEADER_BYTES - NLTP_AEAD_TAG_BYTES;
    const uint8_t *ct_ptr = raw_pkt + NLTP_ENC_HEADER_BYTES;
    const uint8_t *rx_tag = ct_ptr + ct_len;

    uint8_t nonce12[12];
    uint8_t calc_tag[16];
    nltp_write_u32_le(nonce12 + 0, cid);
    nltp_write_u64_le(nonce12 + 4, seq64);
    nltp_aead_compute_tag(key32, nonce12, raw_pkt, NLTP_ENC_HEADER_BYTES, ct_ptr, ct_len, calc_tag);

    uint8_t diff = 0u;
    size_t i;
    for (i = 0; i < 16u; ++i) {
        diff |= (uint8_t)(calc_tag[i] ^ rx_tag[i]);
    }
    if (diff != 0u) return 0;

    if (replay_win != 0 && !nltp_replay_accept(replay_win, seq64)) {
        return 0;
    }

    if (ct_len > 0u && out_pt != 0) {
        nltp_chacha20_xor(key32, 1u, nonce12, ct_ptr, out_pt, ct_len);
    }
    *out_pkt_type = (uint8_t)(ver_type & 0x0Fu);
    *out_flags = (uint8_t)(flags & 0x7Fu);
    *out_conn_id = cid;
    *out_ts_ms = ts_ms;
    *out_pt_len = ct_len;
    return 1;
}

static inline int nltp_unpack_encrypted_frame(
    const uint8_t *raw_pkt,
    size_t raw_len,
    const uint8_t key32[32],
    nltp_replay_window_t *replay_win,
    uint8_t *out_pkt_type,
    uint8_t *out_flags,
    uint32_t *out_conn_id,
    uint32_t *out_ts_ms,
    uint8_t *out_pt,
    size_t *out_pt_len
) {
    return nltp_unpack_encrypted_frame_v(raw_pkt, raw_len, key32, NLTP_VERSION, replay_win, out_pkt_type, out_flags,
                                         out_conn_id, out_ts_ms, out_pt, out_pt_len);
}

/* -------------------------------------------------------------------------
 * Zero-Dependency Standard LZ4 Block Codec (Zero Heap, Bounds-Checked)
 *
 * Produces and consumes standard Yann Collet LZ4 raw blocks, byte-for-byte
 * interoperable with Python, JS, Java, and external `lz4.block` libraries.
 * ------------------------------------------------------------------------- */

#define NLTP_LZ4_INPLACE_MARGIN(comp_len) (((size_t)(comp_len) >> 8) + 32u)

static inline size_t nltp_lz4_compress_bound(size_t src_len) {
    return src_len + (src_len / 255u) + 16u;
}

/*
 * Compresses `src[0..src_len-1]` into `dst[0..dst_cap-1]` using caller-provided
 * `htab` of `(1u << hbits)` uint32_t slots (e.g. hbits=10 -> 4 KB, hbits=12 -> 16 KB).
 * Returns compressed byte count (`1..src_len-1`) ONLY when compression shrinks the
 * input; returns 0 if incompressible or `dst_cap` is insufficient (caller sends raw).
 */
static inline size_t nltp_lz4_compress(
    const uint8_t *src,
    size_t src_len,
    uint8_t *dst,
    size_t dst_cap,
    uint32_t *htab,
    unsigned hbits
) {
    const size_t min_match = 4u;
    const size_t last_literals = 5u;
    const size_t mf_limit = 12u;
    size_t hsize, i, ip, anchor, match_limit, step_limit, dp = 0u;
    size_t max_out;

    if (!src || !dst || !htab || hbits < 6u || hbits > 16u) return 0u;
    if (src_len < 32u) return 0u;
    max_out = dst_cap < src_len - 1u ? dst_cap : src_len - 1u;

    hsize = (size_t)1u << hbits;
    for (i = 0u; i < hsize; ++i) htab[i] = 0u;

    ip = 0u;
    anchor = 0u;
    match_limit = src_len - last_literals;
    step_limit = src_len > mf_limit ? src_len - mf_limit : 0u;

    while (ip <= step_limit) {
        uint32_t h = (uint32_t)(nltp_read_u32_le(src + ip) * 2654435761u) >> (32u - hbits);
        uint32_t ref_plus1 = htab[h];
        htab[h] = (uint32_t)(ip + 1u);

        if (ref_plus1 > 0u) {
            size_t ref = (size_t)(ref_plus1 - 1u);
            size_t offset = ip - ref;
            if (offset > 0u && offset <= 65535u &&
                nltp_read_u32_le(src + ref) == nltp_read_u32_le(src + ip)) {
                size_t lit_len, ml, rem;
                uint8_t token;

                while (ip > anchor && ref > 0u && src[ip - 1u] == src[ref - 1u]) {
                    --ip;
                    --ref;
                }
                lit_len = ip - anchor;
                ml = min_match;
                while (ip + ml < match_limit && src[ip + ml] == src[ref + ml]) {
                    ++ml;
                }

                /* Emit token + literal length + literals + 16-bit offset + match length */
                if (dp + 1u + (lit_len / 255u) + 1u + lit_len + 2u + ((ml - 4u) / 255u) + 1u > max_out) {
                    return 0u;
                }
                token = (uint8_t)((lit_len >= 15u ? 15u : lit_len) << 4);
                token |= (uint8_t)((ml - 4u) >= 15u ? 15u : (ml - 4u));
                dst[dp++] = token;

                if (lit_len >= 15u) {
                    rem = lit_len - 15u;
                    while (rem >= 255u) { dst[dp++] = 255u; rem -= 255u; }
                    dst[dp++] = (uint8_t)rem;
                }
                for (i = 0u; i < lit_len; ++i) dst[dp++] = src[anchor + i];

                nltp_write_u16_le(dst + dp, (uint16_t)offset);
                dp += 2u;

                if (ml - 4u >= 15u) {
                    rem = (ml - 4u) - 15u;
                    while (rem >= 255u) { dst[dp++] = 255u; rem -= 255u; }
                    dst[dp++] = (uint8_t)rem;
                }

                ip += ml;
                anchor = ip;
                if (ip <= step_limit && ip >= 2u) {
                    uint32_t h2 = (uint32_t)(nltp_read_u32_le(src + ip - 2u) * 2654435761u) >> (32u - hbits);
                    htab[h2] = (uint32_t)(ip - 2u + 1u);
                }
                continue;
            }
        }
        ip += 1u + ((ip - anchor) >> 5);
    }

    /* Final literal-only sequence */
    {
        size_t last_lit = src_len - anchor;
        size_t rem;
        if (dp + 1u + (last_lit / 255u) + 1u + last_lit > max_out) {
            /* Exact check without conservative +1 slack */
            size_t exact_extra = last_lit >= 15u ? (1u + (last_lit - 15u) / 255u) : 0u;
            if (dp + 1u + exact_extra + last_lit > max_out) return 0u;
        }
        dst[dp++] = (uint8_t)((last_lit >= 15u ? 15u : last_lit) << 4);
        if (last_lit >= 15u) {
            rem = last_lit - 15u;
            while (rem >= 255u) { dst[dp++] = 255u; rem -= 255u; }
            dst[dp++] = (uint8_t)rem;
        }
        for (i = 0u; i < last_lit; ++i) dst[dp++] = src[anchor + i];
    }
    return dp < src_len ? dp : 0u;
}

/*
 * Decompresses an LZ4 raw block `src[0..src_len-1]` into `dst[0..expected_raw_len-1]`.
 * Returns 1 iff the block is valid and expands to `expected_raw_len` bytes exactly; 0 otherwise.
 */
static inline int nltp_lz4_decompress(
    const uint8_t *src,
    size_t src_len,
    uint8_t *dst,
    size_t expected_raw_len
) {
    size_t sp = 0u, dp = 0u, i;
    if (expected_raw_len == 0u) return src_len == 0u ? 1 : 0;
    if (!src || !dst || src_len == 0u) return 0;

    while (sp < src_len) {
        uint8_t token = src[sp++];
        size_t lit_len = (size_t)(token >> 4);
        size_t offset, match_len, ref;

        if (lit_len == 15u) {
            uint8_t s;
            do {
                if (sp >= src_len) return 0;
                s = src[sp++];
                if (s > expected_raw_len - dp || lit_len > expected_raw_len - dp - s) return 0;
                lit_len += s;
            } while (s == 255u);
        }
        if (lit_len > src_len - sp || lit_len > expected_raw_len - dp) return 0;
        for (i = 0u; i < lit_len; ++i) dst[dp + i] = src[sp + i];
        sp += lit_len;
        dp += lit_len;

        if (sp == src_len) break;

        if (src_len - sp < 2u) return 0;
        offset = (size_t)src[sp] | ((size_t)src[sp + 1u] << 8);
        sp += 2u;
        if (offset == 0u || offset > dp) return 0;

        match_len = (size_t)(token & 0x0Fu);
        if (match_len == 15u) {
            uint8_t s;
            do {
                if (sp >= src_len) return 0;
                s = src[sp++];
                if (expected_raw_len - dp < 4u || match_len > expected_raw_len - dp - 4u ||
                    s > expected_raw_len - dp - 4u - match_len) {
                    return 0;
                }
                match_len += s;
            } while (s == 255u);
        }
        match_len += 4u;
        if (match_len > expected_raw_len - dp) return 0;

        ref = dp - offset;
        for (i = 0u; i < match_len; ++i) dst[dp + i] = dst[ref + i];
        dp += match_len;
    }
    return dp == expected_raw_len ? 1 : 0;
}

/*
 * Zero-heap in-place LZ4 decompression inside `buf[0..buf_cap-1]`, where the
 * compressed stream initially occupies `buf[0..comp_len-1]` and expands to
 * `buf[0..expected_raw_len-1]`. Requires `expected_raw_len + NLTP_LZ4_INPLACE_MARGIN(comp_len) <= buf_cap`.
 * Safe even on corrupted/malicious inputs (never overwrites unread source bytes).
 */
static inline int nltp_lz4_decompress_inplace(
    uint8_t *buf,
    size_t buf_cap,
    size_t comp_len,
    size_t expected_raw_len
) {
    size_t sp, dp = 0u, i;
    if (expected_raw_len == 0u) return comp_len == 0u ? 1 : 0;
    if (!buf || comp_len == 0u || comp_len > buf_cap || expected_raw_len > buf_cap) return 0;
    if (NLTP_LZ4_INPLACE_MARGIN(comp_len) > buf_cap - expected_raw_len) return 0;

    /* Shift compressed bytes to the tail of `buf` using overlap-safe backward copy */
    sp = buf_cap - comp_len;
    if (sp > 0u) {
        for (i = comp_len; i > 0u; --i) {
            buf[sp + i - 1u] = buf[i - 1u];
        }
    }

    while (sp < buf_cap) {
        uint8_t token = buf[sp++];
        size_t lit_len = (size_t)(token >> 4);
        size_t offset, match_len, ref;

        if (lit_len == 15u) {
            uint8_t s;
            do {
                if (sp >= buf_cap) return 0;
                s = buf[sp++];
                if (s > expected_raw_len - dp || lit_len > expected_raw_len - dp - s) return 0;
                lit_len += s;
            } while (s == 255u);
        }
        if (lit_len > buf_cap - sp || lit_len > expected_raw_len - dp) return 0;
        if (dp > sp) return 0;  /* output cursor must never overtake source cursor */
        for (i = 0u; i < lit_len; ++i) buf[dp + i] = buf[sp + i];
        sp += lit_len;
        dp += lit_len;

        if (sp == buf_cap) break;

        if (buf_cap - sp < 2u) return 0;
        offset = (size_t)buf[sp] | ((size_t)buf[sp + 1u] << 8);
        sp += 2u;
        if (offset == 0u || offset > dp) return 0;

        match_len = (size_t)(token & 0x0Fu);
        if (match_len == 15u) {
            uint8_t s;
            do {
                if (sp >= buf_cap) return 0;
                s = buf[sp++];
                if (expected_raw_len - dp < 4u || match_len > expected_raw_len - dp - 4u ||
                    s > expected_raw_len - dp - 4u - match_len) {
                    return 0;
                }
                match_len += s;
            } while (s == 255u);
        }
        match_len += 4u;
        if (match_len > expected_raw_len - dp) return 0;
        if (dp + match_len > sp) return 0;  /* corrupted expansion guard before writing */

        ref = dp - offset;
        for (i = 0u; i < match_len; ++i) buf[dp + i] = buf[ref + i];
        dp += match_len;
    }
    return dp == expected_raw_len ? 1 : 0;
}

#endif /* NLTP_WIRE_H */
