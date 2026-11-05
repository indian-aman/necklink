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
#define NLTP_PT_HELLO               0x0Bu  /* NLTP 1.4 session handshake (wire version 2) */
#define NLTP_PT_HELLO_ACK           0x0Cu
#define NLTP_PT_NO_SESSION          0x0Du  /* unauthenticated restart hint, see nltp_core.c */
#define NLTP_SESSION_VERSION        2u     /* version nibble of every encrypted frame since NLTP 1.4 */

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
 * RFC 6479 64-Packet Sliding Anti-Replay Window (Matches `ReplayWindow64`)
 * ------------------------------------------------------------------------- */

typedef struct {
    uint64_t highest_seq;
    uint64_t bitmap;
} nltp_replay_window_t;

static inline void nltp_replay_init(nltp_replay_window_t *w) {
    w->highest_seq = 0u;
    w->bitmap = 0u;
}

static inline int nltp_replay_would_accept(const nltp_replay_window_t *w, uint64_t seq) {
    if (seq == 0u) return 0;
    if (seq > w->highest_seq) return 1;
    uint64_t diff = w->highest_seq - seq;
    if (diff >= 64u) return 0;
    return ((w->bitmap & ((uint64_t)1u << diff)) == 0u) ? 1 : 0;
}

static inline int nltp_replay_accept(nltp_replay_window_t *w, uint64_t seq) {
    if (!nltp_replay_would_accept(w, seq)) return 0;
    if (seq > w->highest_seq) {
        uint64_t shift = seq - w->highest_seq;
        if (shift >= 64u) {
            w->bitmap = 1u;
        } else {
            w->bitmap = (w->bitmap << shift) | 1u;
        }
        w->highest_seq = seq;
    } else {
        uint64_t diff = w->highest_seq - seq;
        w->bitmap |= ((uint64_t)1u << diff);
    }
    return 1;
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

#endif /* NLTP_WIRE_H */
