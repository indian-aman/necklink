/*
 * nltp_core.c — portable C99 NLTP v1 endpoint. See nltp_core.h.
 * Mirrors necklink/nltp.py (NLTPEndpoint) so C and Python peers interoperate; integer arithmetic only.
 */
#include "nltp_core.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "nltp_sha256.h"

#if defined(ESP_PLATFORM)
#include "esp_random.h"
#endif

#define U32_INF 0xFFFFFFFFu
#define ACK_EVERY_SHARDS 8u
#define ACK_MAX_DELAY_MS 25u

/* ---------------------------------------------------------------------------
 * GF(2^8) over 0x11D with log/exp tables (768 B, built once)
 * ------------------------------------------------------------------------- */

static uint8_t gf_exp[512];
static uint8_t gf_log[256];
static uint8_t gf_ready = 0;

static void gf_init(void) {
    unsigned x = 1, i;
    if (gf_ready) return;
    for (i = 0; i < 255; ++i) {
        gf_exp[i] = (uint8_t)x;
        gf_log[x] = (uint8_t)i;
        x <<= 1;
        if (x & 0x100u) x ^= 0x11Du;
    }
    for (i = 255; i < 512; ++i) gf_exp[i] = gf_exp[i - 255];
    gf_ready = 1;
}

#if !NLTP_TX_ONLY
static inline uint8_t gf_mul(uint8_t a, uint8_t b) {
    return (a && b) ? gf_exp[(unsigned)gf_log[a] + gf_log[b]] : 0u;
}
#endif

static inline uint8_t gf_inv(uint8_t a) { return gf_exp[255u - gf_log[a]]; }

/* dst ^= f * src over n bytes (branchless 256-entry row lookup, 4x unrolled) */
static void gf_axpy(uint8_t *dst, const uint8_t *src, uint8_t f, size_t n) {
    uint8_t mul_row[256];
    size_t i = 0;
    unsigned lf, x;
    if (f == 0) return;
    if (f == 1) {
        for (; i + 4u <= n; i += 4u) {
            dst[i] ^= src[i];
            dst[i + 1u] ^= src[i + 1u];
            dst[i + 2u] ^= src[i + 2u];
            dst[i + 3u] ^= src[i + 3u];
        }
        for (; i < n; ++i) dst[i] ^= src[i];
        return;
    }
    lf = gf_log[f];
    mul_row[0] = 0u;
    for (x = 1u; x < 256u; ++x) {
        mul_row[x] = gf_exp[lf + gf_log[x]];
    }
    for (; i + 4u <= n; i += 4u) {
        dst[i] ^= mul_row[src[i]];
        dst[i + 1u] ^= mul_row[src[i + 1u]];
        dst[i + 2u] ^= mul_row[src[i + 2u]];
        dst[i + 3u] ^= mul_row[src[i + 3u]];
    }
    for (; i < n; ++i) {
        dst[i] ^= mul_row[src[i]];
    }
}

#if !NLTP_TX_ONLY
static void gf_scale(uint8_t *buf, uint8_t f, size_t n) {
    uint8_t mul_row[256];
    size_t i = 0;
    unsigned lf, x;
    if (f == 1) return;
    if (f == 0) {
        memset(buf, 0, n);
        return;
    }
    lf = gf_log[f];
    mul_row[0] = 0u;
    for (x = 1u; x < 256u; ++x) {
        mul_row[x] = gf_exp[lf + gf_log[x]];
    }
    for (; i + 4u <= n; i += 4u) {
        buf[i] = mul_row[buf[i]];
        buf[i + 1u] = mul_row[buf[i + 1u]];
        buf[i + 2u] = mul_row[buf[i + 2u]];
        buf[i + 3u] = mul_row[buf[i + 3u]];
    }
    for (; i < n; ++i) {
        buf[i] = mul_row[buf[i]];
    }
}
#endif

static void cauchy_row(uint8_t parity_offset, uint8_t k, uint8_t *out) {
    uint8_t x_r = (uint8_t)(64u + (parity_offset % 192u));
    uint8_t c;
    for (c = 0; c < k; ++c) out[c] = gf_inv((uint8_t)(x_r ^ (uint8_t)(c & 0x3Fu)));
}

/* ---------------------------------------------------------------------------
 * Small helpers
 * ------------------------------------------------------------------------- */

static inline uint32_t umin32(uint32_t a, uint32_t b) { return a < b ? a : b; }
static inline uint32_t umax32(uint32_t a, uint32_t b) { return a > b ? a : b; }
static inline uint32_t now(nltp_endpoint_t *ep) { return ep->now_ms(ep->user); }

/* Bytes a frame adds around its payload: CRC header, or AEAD header + Poly1305 tag. */
static uint32_t frame_overhead(const nltp_endpoint_t *ep) {
    return ep->encrypted ? NLTP_ENC_HEADER_BYTES + NLTP_AEAD_TAG_BYTES : NLTP_HEADER_BYTES;
}

static uint8_t *payload_buf(nltp_endpoint_t *ep) {
    return ep->pkt + (ep->encrypted ? NLTP_ENC_HEADER_BYTES : NLTP_HEADER_BYTES);
}

/* ---------------------------------------------------------------------------
 * Encrypted sessions (NLTP 1.5, wire version 2) — same protocol as necklink/session.py:
 *   HELLO     { n_i (16 B) || pk_i (32 B) }               sealed with the long-term PSK K (84 B wire)
 *   HELLO_ACK { n_i (16 B) || n_r (16 B) || pk_r (32 B) } sealed with the long-term PSK K (100 B wire)
 *   dh    = X25519(sk_i, pk_r) == X25519(sk_r, pk_i)      (rejected if all-zero per RFC 7748 s6.1)
 *   prk   = HMAC-SHA256(K, "NLTP-1.5-X25519" || dh || n_i || n_r || LE32(cid_i) || LE32(cid_r))
 *   k_i2r = HMAC-SHA256(prk, "i2r"), k_r2i = HMAC-SHA256(prk, "r2i").
 * Ephemeral scalars (sk_i, sk_r) and dh are zeroized immediately on session derivation for full forward secrecy.
 * ------------------------------------------------------------------------- */

#define HS_FIRST_RETX_MS 60u
#define HS_MAX_RETX_MS 200u
#define HS_GIVE_UP_MS 10000u
#define SESS_TAKEOVER_MS 1000u   /* a new HELLO replaces the live session only after this much silence */
#define SESS_REKEY_MS 3000u      /* re-handshake when the peer of an unfinished send has been silent this long */
#define HINT_INTERVAL_MS 250u
#define HS_FRAME_MAX (NLTP_ENC_HEADER_BYTES + NLTP_HELLO_ACK_PT_BYTES + NLTP_AEAD_TAG_BYTES)

static void reflex_poll(nltp_endpoint_t *ep);
static void tx_poll(nltp_endpoint_t *ep);

#if defined(ESP_PLATFORM)
static void default_random(void *user, uint8_t *out, size_t len) {
    (void)user;
    esp_fill_random(out, len);
}
#elif defined(ESP8266)
/* ESP8266 hardware RNG register (RANDOM_REG32 in the Arduino core, read by ESP.random()). */
static void default_random(void *user, uint8_t *out, size_t len) {
    size_t i;
    (void)user;
    for (i = 0; i < len; ++i) out[i] = (uint8_t)(*(volatile uint32_t *)0x3FF20E44u);
}
#elif defined(__unix__) || defined(__linux__) || defined(__APPLE__)
static void default_random(void *user, uint8_t *out, size_t len) {
    FILE *f = fopen("/dev/urandom", "rb");
    size_t got = f ? fread(out, 1, len, f) : 0u;
    (void)user;
    if (f) fclose(f);
    if (got != len) abort();   /* no secure randomness: refuse to continue rather than reuse nonces */
}
#else
#define NLTP_NO_DEFAULT_RANDOM 1
#endif

static int random_bytes(nltp_endpoint_t *ep, uint8_t *out, size_t len) {
    if (!ep->rng) return 0;
    ep->rng(ep->user, out, len);
    return 1;
}

static void sess_derive(nltp_endpoint_t *ep, nltp_session_t *s, const uint8_t dh[32],
                        const uint8_t n_i[16], const uint8_t n_r[16], uint32_t cid_i, uint32_t cid_r) {
    uint8_t msg[15u + 32u + 16u + 16u + 8u], prk[32], k_i2r[32], k_r2i[32];
    memcpy(msg, "NLTP-1.5-X25519", 15u);
    memcpy(msg + 15, dh, 32u);
    memcpy(msg + 47, n_i, 16u);
    memcpy(msg + 63, n_r, 16u);
    nltp_write_u32_le(msg + 79, cid_i);
    nltp_write_u32_le(msg + 83, cid_r);
    nltp_hmac_sha256(ep->key, 32u, msg, sizeof(msg), prk);
    nltp_hmac_sha256(prk, 32u, (const uint8_t *)"i2r", 3u, k_i2r);
    nltp_hmac_sha256(prk, 32u, (const uint8_t *)"r2i", 3u, k_r2i);
    memcpy(s->k_tx, s->role == 'i' ? k_i2r : k_r2i, 32u);
    memcpy(s->k_rx, s->role == 'i' ? k_r2i : k_i2r, 32u);
    nltp_wipe_bytes(msg, sizeof(msg));
    nltp_wipe_bytes(prk, sizeof(prk));
    nltp_wipe_bytes(k_i2r, sizeof(k_i2r));
    nltp_wipe_bytes(k_r2i, sizeof(k_r2i));
    s->used = 1;
    s->tx_seq = 0;
    nltp_replay_init(&s->replay);
    s->est_ms = s->last_rx_ms = now(ep);
}

/* Seals a handshake frame with the long-term key and a random 64-bit counter; returns its length. */
static size_t hs_pack(nltp_endpoint_t *ep, uint8_t *out, uint8_t type, const uint8_t *body, size_t blen) {
    uint8_t r[8];
    uint64_t seq;
    if (!random_bytes(ep, r, 8u)) return 0;
    seq = nltp_read_u64_le(r) | 1u;
    return nltp_pack_encrypted_frame_v(out, ep->key, NLTP_SESSION_VERSION, type, 0, ep->conn_id, seq, now(ep), body, blen);
}

static void raw_send(nltp_endpoint_t *ep, const uint8_t *pkt, size_t n) {
    ep->send(ep->user, pkt, n);
    ep->pkts_tx++;
    ep->bytes_tx += (uint32_t)n;
    ep->last_tx_ms = now(ep);
}

static void hs_send_hello(nltp_endpoint_t *ep) {
    uint8_t body[NLTP_HELLO_PT_BYTES];
    uint8_t f[HS_FRAME_MAX];
    size_t n;
    memcpy(body, ep->pend_n_i, 16u);
    memcpy(body + 16, ep->pend_pk_i, 32u);
    n = hs_pack(ep, f, NLTP_PT_HELLO, body, NLTP_HELLO_PT_BYTES);
    if (n) {
        raw_send(ep, f, n);
        ep->hellos_tx++;
    }
}

static void hs_start(nltp_endpoint_t *ep) {
    uint32_t t = now(ep);
    if (!random_bytes(ep, ep->pend_n_i, 16u)) return;   /* no random source: fail closed */
    if (!random_bytes(ep, ep->pend_sk_i, 32u)) return;
    nltp_x25519_public_key(ep->pend_pk_i, ep->pend_sk_i);
    ep->pend = 1;
    ep->pend_interval_ms = HS_FIRST_RETX_MS;
    ep->pend_next_ms = t + HS_FIRST_RETX_MS;
    ep->pend_deadline_ms = t + HS_GIVE_UP_MS;
    hs_send_hello(ep);
}

/* 1 when frames can be sealed for the peer; otherwise starts a handshake (if none is running). */
static int sess_ready(nltp_endpoint_t *ep) {
    if (!ep->encrypted || ep->sess[0].used) return 1;
    if (!ep->pend) hs_start(ep);
    return 0;
}

static void sess_install(nltp_endpoint_t *ep, const nltp_session_t *ns, int live) {
    if (live) {
        ep->sess[1] = ep->sess[0];
        ep->sess[0] = *ns;
    } else {
        ep->sess[1] = *ns;
    }
    ep->sessions_up++;
}

static void sess_on_hello(nltp_endpoint_t *ep, uint32_t cid_i, const uint8_t n_i[16], const uint8_t pk_i[32]) {
    nltp_session_t ns;
    uint8_t body[NLTP_HELLO_ACK_PT_BYTES], n_r[16], sk_r[32], pk_r[32], dh[32];
    size_t n;
    int i, live, ok_dh;
    for (i = 0; i < 2; ++i) {
        nltp_session_t *s = &ep->sess[i];
        if (s->used && s->role == 'r' && s->peer_cid == cid_i && s->has_ack &&
            memcmp(s->n_i, n_i, 16u) == 0 && memcmp(s->pk_i, pk_i, 32u) == 0) {
            raw_send(ep, s->ack, sizeof(s->ack));   /* retransmitted HELLO: same answer */
            return;
        }
    }
    if (!random_bytes(ep, sk_r, 32u) || !random_bytes(ep, n_r, 16u)) {
        nltp_wipe_bytes(sk_r, sizeof(sk_r));
        return;
    }
    nltp_x25519_public_key(pk_r, sk_r);
    ok_dh = nltp_x25519_scalarmult(dh, sk_r, pk_i);
    nltp_wipe_bytes(sk_r, sizeof(sk_r));
    if (!ok_dh) {
        nltp_wipe_bytes(dh, sizeof(dh));
        return;
    }
    if (ep->pend) {
        if (memcmp(ep->pend_n_i, n_i, 16u) < 0) {   /* simultaneous open: our HELLO wins */
            nltp_wipe_bytes(dh, sizeof(dh));
            return;
        }
        ep->pend = 0;
        nltp_wipe_bytes(ep->pend_sk_i, sizeof(ep->pend_sk_i));
    }
    memset(&ns, 0, sizeof(ns));
    ns.role = 'r';
    ns.peer_cid = cid_i;
    memcpy(ns.n_i, n_i, 16u);
    memcpy(ns.pk_i, pk_i, 32u);
    sess_derive(ep, &ns, dh, n_i, n_r, cid_i, ep->conn_id);
    nltp_wipe_bytes(dh, sizeof(dh));
    memcpy(body, n_i, 16u);
    memcpy(body + 16, n_r, 16u);
    memcpy(body + 32, pk_r, 32u);
    n = hs_pack(ep, ns.ack, NLTP_PT_HELLO_ACK, body, NLTP_HELLO_ACK_PT_BYTES);
    if (n != sizeof(ns.ack)) return;
    ns.has_ack = 1;
    live = !ep->sess[0].used || (now(ep) - ep->sess[0].last_rx_ms) >= SESS_TAKEOVER_MS;
    sess_install(ep, &ns, live);
    raw_send(ep, ns.ack, sizeof(ns.ack));
    if (live) {
        reflex_poll(ep);
        tx_poll(ep);
    }
}

static void sess_on_ack(nltp_endpoint_t *ep, uint32_t cid_r, const uint8_t n_i[16], const uint8_t n_r[16],
                        const uint8_t pk_r[32]) {
    nltp_session_t ns;
    uint8_t dh[32];
    int ok_dh;
    if (!ep->pend || memcmp(ep->pend_n_i, n_i, 16u) != 0) return;   /* stale or replayed ACK */
    ok_dh = nltp_x25519_scalarmult(dh, ep->pend_sk_i, pk_r);
    if (!ok_dh) {
        nltp_wipe_bytes(dh, sizeof(dh));
        return;
    }
    nltp_wipe_bytes(ep->pend_sk_i, sizeof(ep->pend_sk_i));
    ep->pend = 0;
    memset(&ns, 0, sizeof(ns));
    ns.role = 'i';
    ns.peer_cid = cid_r;
    memcpy(ns.n_i, n_i, 16u);
    sess_derive(ep, &ns, dh, n_i, n_r, ep->conn_id, cid_r);
    nltp_wipe_bytes(dh, sizeof(dh));
    sess_install(ep, &ns, 1);
    reflex_poll(ep);
    tx_poll(ep);
}

/* A peer seals frames for a session we do not have (we restarted): say so, so it re-keys in one round
   trip. Unauthenticated by necessity; it echoes the frame's conn_id and counter so the peer only reacts to
   frames it really sent, and a forged hint costs at most one extra handshake. */
static void sess_send_no_session(nltp_endpoint_t *ep, uint32_t cid, uint64_t seq) {
    uint8_t f[NLTP_ENC_HEADER_BYTES + 16u];
    uint32_t t = now(ep);
    if (ep->hint_tx_ms != 0u && t - ep->hint_tx_ms < HINT_INTERVAL_MS) return;
    ep->hint_tx_ms = t | 1u;
    f[0] = NLTP_MAGIC_0;
    f[1] = NLTP_MAGIC_1;
    f[2] = (uint8_t)((NLTP_SESSION_VERSION << 4) | NLTP_PT_NO_SESSION);
    f[3] = NLTP_FLAG_ENCRYPTED;
    nltp_write_u32_le(f + 4, ep->conn_id);
    nltp_write_u64_le(f + 8, 0u);
    nltp_write_u32_le(f + 16, t);
    nltp_write_u32_le(f + 20, cid);
    nltp_write_u64_le(f + 24, seq);
    nltp_write_u32_le(f + 32, 0u);
    raw_send(ep, f, sizeof(f));
}

static void sess_on_no_session(nltp_endpoint_t *ep, const uint8_t *raw, size_t len) {
    uint32_t t = now(ep);
    uint64_t echo_seq;
    if (len < NLTP_ENC_HEADER_BYTES + 12u || !ep->sess[0].used || ep->pend) return;
    echo_seq = nltp_read_u64_le(raw + NLTP_ENC_HEADER_BYTES + 4);
    if (nltp_read_u32_le(raw + NLTP_ENC_HEADER_BYTES) != ep->conn_id || echo_seq == 0u || echo_seq > ep->sess[0].tx_seq) return;
    if (ep->hint_rx_ms != 0u && t - ep->hint_rx_ms < HINT_INTERVAL_MS) return;
    ep->hint_rx_ms = t | 1u;
    ep->rekeys_from_hint++;
    hs_start(ep);
}

static void sess_poll(nltp_endpoint_t *ep) {
    uint32_t t = now(ep);
    if (!ep->encrypted) return;
    if (ep->pend) {
        if ((int32_t)(t - ep->pend_deadline_ms) >= 0) {
            ep->pend = 0;
            nltp_wipe_bytes(ep->pend_sk_i, sizeof(ep->pend_sk_i));
        } else if ((int32_t)(t - ep->pend_next_ms) >= 0) {
            hs_send_hello(ep);
            ep->pend_interval_ms = umin32(HS_MAX_RETX_MS, ep->pend_interval_ms * 3u / 2u);
            ep->pend_next_ms = t + ep->pend_interval_ms;
        }
    } else if (ep->sess[0].used &&
               ((ep->tx.active && !ep->tx.completed) || nltp_reflex_pending(ep) > 0u) &&
               t - ep->sess[0].last_rx_ms > SESS_REKEY_MS && t - ep->sess[0].est_ms > SESS_REKEY_MS) {
        hs_start(ep);
    }
}

static void emit_flags(nltp_endpoint_t *ep, uint8_t type, uint8_t flags, size_t payload_len) {
    /* the payload is already in place at payload_buf(ep); encryption works in place */
    size_t n;
    if (ep->encrypted) {
        nltp_session_t *s = &ep->sess[0];
        if (!sess_ready(ep)) return;   /* handshake running; senders retry */
        n = nltp_pack_encrypted_frame_v(ep->pkt, s->k_tx, NLTP_SESSION_VERSION, type, flags, ep->conn_id, ++s->tx_seq,
                                        now(ep), ep->pkt + NLTP_ENC_HEADER_BYTES, payload_len);
    } else {
        nltp_pack_header(ep->pkt, type, flags, ep->conn_id, now(ep), ep->pkt + NLTP_HEADER_BYTES, payload_len);
        n = NLTP_HEADER_BYTES + payload_len;
    }
    ep->send(ep->user, ep->pkt, n);
    ep->pkts_tx++;
    ep->bytes_tx += (uint32_t)n;
    ep->last_tx_ms = now(ep);
}

static void emit(nltp_endpoint_t *ep, uint8_t type, size_t payload_len) {
    emit_flags(ep, type, 0u, payload_len);
}

/* Generation geometry, identical to Python _TxGeneration chunking (supports runtime shard_bytes). */
static uint32_t total_gens_for(uint32_t len, uint16_t sb) {
    uint32_t gen_bytes = (uint32_t)NLTP_GEN_K * (uint32_t)sb;
    return len == 0 ? 1u : (len + gen_bytes - 1u) / gen_bytes;
}

static void gen_geometry(uint32_t len, uint16_t sb, uint32_t g, uint8_t *k_out, uint16_t *rw_out) {
    uint32_t start = g * (uint32_t)NLTP_GEN_K * (uint32_t)sb;
    uint32_t rem = len > start ? len - start : 0u;
    uint32_t k = rem == 0 ? 1u : (rem + (uint32_t)sb - 1u) / (uint32_t)sb;
    if (k > NLTP_GEN_K) k = NLTP_GEN_K;
    *k_out = (uint8_t)k;
    *rw_out = (uint16_t)(2u + (k >= 2u ? (uint32_t)sb : rem));
}

/* ---------------------------------------------------------------------------
 * Pacer + rate controller
 * ------------------------------------------------------------------------- */

static int32_t auto_burst(uint32_t rate_kbps) {
    int32_t b = (int32_t)(rate_kbps * 6u / 8u); /* ~6 ms of data, in bytes */
    if (b < 4096) b = 4096;
    if (b > 16384) b = 16384;
    return b;
}

static void pacer_set_rate(nltp_pacer_t *p, uint32_t kbps) {
    p->rate_kbps = kbps;
    p->max_burst = auto_burst(kbps);
    if (p->tokens > p->max_burst) p->tokens = p->max_burst;
}

static int pacer_consume(nltp_endpoint_t *ep, uint32_t n) {
    nltp_pacer_t *p = &ep->pacer;
    uint32_t t;
    if (!ep->paced) return 1;
    if (p->bonus >= n) {
        p->bonus -= n;
        ep->rate.sent_bytes += n;
        return 1;
    }
    t = now(ep);
    if (!p->started) {
        p->started = 1;
        p->last_ms = t;
    } else {
        uint32_t dt = t - p->last_ms;
        p->last_ms = t;
        if (dt > 1000u) dt = 1000u;
        p->tokens += (int32_t)((uint64_t)dt * p->rate_kbps / 8u);
        if (p->tokens > p->max_burst) p->tokens = p->max_burst;
    }
    if (p->tokens >= (int32_t)n) {
        p->tokens -= (int32_t)n;
        ep->rate.sent_bytes += n;
        return 1;
    }
    return 0;
}

static void rate_init(nltp_rate_t *r, uint32_t cap_kbps) {
    memset(r, 0, sizeof(*r));
    r->cap_kbps = cap_kbps;
    r->rate_kbps = umin32(2000u, cap_kbps);
    r->min_rtt_ms = U32_INF;
    r->min_mean_rtt_ms = U32_INF;
    r->round_min_rtt_ms = U32_INF;
}

static uint32_t rate_btlbw(const nltp_rate_t *r) {
    uint32_t m = r->round_arrival_max;
    uint8_t i;
    for (i = 0; i < r->n_btlbw; ++i) m = umax32(m, r->btlbw_rounds[i]);
    return m;
}

static uint32_t rate_base_rtt(const nltp_rate_t *r) {
    if (r->min_mean_rtt_ms != U32_INF) return r->min_mean_rtt_ms;
    if (r->n_rtt_samples >= 2u && r->srtt_ms > 0u) return r->srtt_ms;
    return r->min_rtt_ms;
}

static uint32_t rate_capacity(const nltp_rate_t *r, uint32_t deliv_kbps) {
    uint32_t denom = r->random_loss_pm > 700u ? 300u : 1000u - r->random_loss_pm;
    return (uint32_t)((uint64_t)deliv_kbps * 1000u / denom);
}

static void rate_on_rtt(nltp_rate_t *r, uint32_t rtt) {
    r->min_rtt_ms = umin32(r->min_rtt_ms, rtt);
    r->round_min_rtt_ms = umin32(r->round_min_rtt_ms, rtt);
    r->round_rtt_sum += rtt;
    r->round_rtt_cnt++;
    r->n_rtt_samples++;
    if (r->n_rtt_samples == 1u) {
        r->srtt_ms = rtt;
        r->rttvar_ms = rtt / 4u;
    } else {
        uint32_t dev = rtt > r->srtt_ms ? rtt - r->srtt_ms : r->srtt_ms - rtt;
        r->rttvar_ms = (3u * r->rttvar_ms + dev) / 4u;
        r->srtt_ms = (7u * r->srtt_ms + rtt) / 8u;
    }
    if (r->mode == 0 && r->n_rtt_samples >= 4u) {
        uint32_t base = rate_base_rtt(r);
        if (base != U32_INF && rtt > base + umax32(umax32(6u, base / 6u), r->rttvar_ms * 5u / 2u)) {
            r->startup_exit_pending = 1;
        }
    }
}

/* Delay variation below ~2x the path's normal jitter is not treated as a standing queue. */
static uint32_t rate_queue_target(const nltp_rate_t *r) {
    uint32_t base = rate_base_rtt(r);
    if (base == U32_INF) return 4u;
    return umax32(umax32(4u, base / 20u), 2u * r->rttvar_ms);
}

/* Cumulative bytes sent as of time t (latest recorded ACK not after t). */
static uint32_t rate_sent_at(const nltp_rate_t *r, uint32_t t) {
    uint8_t i;
    for (i = 0; i < r->hist_len; ++i) {
        uint8_t idx = (uint8_t)((r->hist_head + 32u - 1u - i) % 32u);
        if ((int32_t)(t - r->hist_t[idx]) >= 0) return r->hist_sent[idx];
    }
    return r->hist_len ? r->hist_sent[(r->hist_head + 32u - r->hist_len) % 32u] : 0u;
}

static void rate_start_stream(nltp_rate_t *r, uint32_t sid, uint32_t t) {
    r->tracked_sid = sid;
    r->train_probe_done = (uint8_t)(r->mode == 1 && r->n_btlbw > 0);
    r->round_started = 1;
    r->round_t0_ms = t;
    r->round_rx0 = 0;
    r->round_sent0 = r->sent_bytes;
    r->prev_round_sent = 0;
    r->round_min_rtt_ms = U32_INF;
    r->round_rtt_sum = 0;
    r->round_rtt_cnt = 0;
    r->rounds_done = 0;
}

static uint32_t clamp_rate(const nltp_rate_t *r, uint32_t v) {
    if (v < 64u) v = 64u;
    return umin32(v, r->cap_kbps);
}

/* Returns 1 when the pacing rate changed. */
static int rate_on_ack(nltp_rate_t *r, uint32_t t, uint32_t sid, uint32_t rx_bytes, uint32_t srtt, uint32_t arrival_kbps) {
    uint32_t old = r->rate_kbps, base_rtt, dt, delivered, deliv_kbps, round_sent, queue, target;
    int early;
    if (sid != r->tracked_sid || !r->round_started) return 0;
    r->hist_t[r->hist_head] = t;
    r->hist_sent[r->hist_head] = r->sent_bytes;
    r->hist_head = (uint8_t)((r->hist_head + 1u) % 32u);
    if (r->hist_len < 32u) r->hist_len++;
    if (arrival_kbps > 0) {
        r->round_arrival_max = umax32(r->round_arrival_max, arrival_kbps);
        if (r->mode == 0 && !r->train_probe_done) {
            /* The cold-start burst reaches the receiver at the bottleneck rate: jump straight to it. */
            r->train_probe_done = 1;
            r->mode = 1;
            r->rate_kbps = clamp_rate(r, umax32(r->rate_kbps, (uint32_t)((uint64_t)arrival_kbps * 105u / 100u)));
            return r->rate_kbps != old;
        } else if (r->rounds_done <= 1u && r->last_queue_ms <= rate_queue_target(r)) {
            /* Second half of the initial burst: ratchet upward if early jitter understated the first train. */
            uint32_t cand = clamp_rate(r, (uint32_t)((uint64_t)arrival_kbps * 105u / 100u));
            if (cand > r->rate_kbps) {
                r->rate_kbps = cand;
                return 1;
            }
        }
    }
    base_rtt = rate_base_rtt(r);
    if (base_rtt == U32_INF) base_rtt = srtt;
    dt = t - r->round_t0_ms;
    early = r->mode == 0 && r->startup_exit_pending && dt >= 5u;
    if (dt < umax32(20u, base_rtt) && !early) return 0;
    if (dt == 0) dt = 1;

    delivered = rx_bytes - r->round_rx0;
    deliv_kbps = (uint32_t)((uint64_t)delivered * 8u / dt);
    round_sent = r->sent_bytes - r->round_sent0;
    if (r->round_rtt_cnt >= 3u) {
        uint32_t round_mean = r->round_rtt_sum / r->round_rtt_cnt;
        r->min_mean_rtt_ms = umin32(r->min_mean_rtt_ms, round_mean);
        queue = round_mean > r->min_mean_rtt_ms ? round_mean - r->min_mean_rtt_ms : 0u;
    } else if (r->round_min_rtt_ms != U32_INF && r->min_rtt_ms != U32_INF) {
        uint32_t raw_q = r->round_min_rtt_ms > r->min_rtt_ms ? r->round_min_rtt_ms - r->min_rtt_ms : 0u;
        uint32_t j_half = r->rttvar_ms / 2u;
        queue = raw_q > j_half ? raw_q - j_half : 0u;
    } else {
        queue = 0u;
    }
    target = rate_queue_target(r);

    /* Random-loss sample: deliveries vs what was sent one smoothed RTT earlier (so in-flight or reordered
       packets are not counted as lost); skip the first rounds while the pipe fills, and queued rounds. */
    {
        uint32_t lag = r->srtt_ms ? r->srtt_ms : base_rtt;
        uint32_t sent_window = rate_sent_at(r, t - lag) - rate_sent_at(r, r->round_t0_ms - lag);
        r->rounds_done++;
        r->prev_round_sent = sent_window;
    }
    if (r->rounds_done > 2u && r->prev_round_sent > 8192u && queue < target && r->prev_queue_ms < target) {
        uint32_t ratio = (uint32_t)((uint64_t)delivered * 1000u / r->prev_round_sent);
        uint32_t sample = ratio >= 1000u ? 0u : 1000u - ratio;
        if (sample > 900u) sample = 900u;
        r->random_loss_pm = r->random_loss_samples == 0 ? (uint16_t)sample
                                                        : (uint16_t)((8u * r->random_loss_pm + 2u * sample) / 10u);
        r->random_loss_samples++;
    }

    if (r->mode == 0) {
        if ((uint64_t)deliv_kbps * 100u >= (uint64_t)r->best_deliv_kbps * 125u) {
            r->best_deliv_kbps = deliv_kbps;
            r->plateau_rounds = 0;
        } else {
            r->plateau_rounds++;
        }
        if (early || queue > target) {
            uint32_t cap = rate_capacity(r, umax32(deliv_kbps, r->best_deliv_kbps));
            r->mode = 1;
            r->rate_kbps = cap > 0 ? umax32((uint32_t)((uint64_t)cap * 95u / 100u), r->rate_kbps / 2u) : r->rate_kbps / 2u;
        } else if (r->plateau_rounds >= 3u) {
            r->mode = 1;
        } else if (deliv_kbps * 2u >= r->rate_kbps) {
            r->rate_kbps = umax32(2u * r->rate_kbps, 2u * deliv_kbps);
        }
    } else {
        uint32_t deliv_cap = rate_capacity(r, deliv_kbps);
        uint32_t cap = umax32(deliv_cap, rate_btlbw(r));
        if (queue > 2u * target) {
            uint32_t floor_kbps = deliv_cap > 0u ? (uint32_t)((uint64_t)deliv_cap * 55u / 100u) : 64u;
            uint32_t cut = cap > 0u ? umin32((uint32_t)((uint64_t)r->rate_kbps * 90u / 100u),
                                             (uint32_t)((uint64_t)cap * 95u / 100u))
                                    : (uint32_t)((uint64_t)r->rate_kbps * 85u / 100u);
            r->rate_kbps = umax32(floor_kbps, cut);
        } else if (queue < target) {
            int clean_pipe = (r->rounds_done >= 2u) && (r->random_loss_pm < 10u);
            int fast_probe = !clean_pipe && (queue * 2u < target) &&
                             ((uint64_t)deliv_cap * 100u >= (uint64_t)r->rate_kbps * 80u);
            uint32_t gain_pct = fast_probe ? 130u : (clean_pipe ? 108u : 112u);
            uint32_t ceil_pct = fast_probe ? 135u : (clean_pipe ? 112u : 125u);
            uint32_t grown = (uint32_t)((uint64_t)r->rate_kbps * gain_pct / 100u);
            uint32_t ceiling = cap > 0u ? (uint32_t)((uint64_t)cap * ceil_pct / 100u) : grown;
            r->rate_kbps = umin32(grown, umax32(r->rate_kbps, ceiling));
        }
    }
    r->rate_kbps = clamp_rate(r, r->rate_kbps);
    r->last_deliv_kbps = deliv_kbps;
    r->last_queue_ms = queue;
    (void)round_sent;
    r->prev_queue_ms = queue;
    r->round_t0_ms = t;
    r->round_rx0 = rx_bytes;
    r->round_sent0 = r->sent_bytes;
    r->round_min_rtt_ms = U32_INF;
    r->round_rtt_sum = 0;
    r->round_rtt_cnt = 0;
    r->startup_exit_pending = 0;
    if (r->round_arrival_max > 0u &&
        (r->n_btlbw == 0u || (uint64_t)r->round_arrival_max * 100u >= (uint64_t)r->rate_kbps * 85u)) {
        if (r->n_btlbw == 10u) {
            memmove(r->btlbw_rounds, r->btlbw_rounds + 1, 9u * sizeof(uint32_t));
            r->n_btlbw = 9u;
        }
        r->btlbw_rounds[r->n_btlbw++] = r->round_arrival_max;
    }
    r->round_arrival_max = 0;
    return r->rate_kbps != old;
}

static void record_rtt(nltp_endpoint_t *ep, uint32_t rtt) {
    if (rtt == 0) rtt = 1;
    ep->min_rtt_ms = umin32(ep->min_rtt_ms, rtt);
    if (!ep->has_rtt) {
        ep->srtt_ms = rtt;
        ep->has_rtt = 1;
    } else {
        ep->srtt_ms = (4u * ep->srtt_ms + rtt) / 5u;
    }
    if (ep->paced) rate_on_rtt(&ep->rate, rtt);
}

/* ---------------------------------------------------------------------------
 * Init
 * ------------------------------------------------------------------------- */

void nltp_init(nltp_endpoint_t *ep, uint32_t conn_id, uint32_t max_kbps,
               nltp_send_fn send, nltp_now_ms_fn now_ms, void *user) {
#if !NLTP_TX_ONLY
    uint32_t i;
#endif
    gf_init();
    memset(ep, 0, sizeof(*ep));
    ep->send = send;
    ep->now_ms = now_ms;
    ep->user = user;
    ep->conn_id = conn_id | 1u;
    ep->srtt_ms = 150u;
    ep->min_rtt_ms = U32_INF;
    ep->tx_shard_bytes = NLTP_SHARD_BYTES;
    ep->next_stream_id = 1;
    ep->last_tx_ms = now_ms(user);
    ep->paced = max_kbps > 0;
    if (ep->paced) {
        rate_init(&ep->rate, max_kbps);
        pacer_set_rate(&ep->pacer, ep->rate.rate_kbps);
        ep->pacer.tokens = ep->pacer.max_burst;
    }
#if !NLTP_TX_ONLY
    for (i = 0; i < NLTP_RX_SLOTS; ++i) ep->rx.slots[i].gen_id = -1;
    ep->rx.max_gen_seen = -1;
    ep->rx.max_sys_seen = -1;
    ep->rx.max_sys_ord = -1;
#endif
#ifndef NLTP_NO_DEFAULT_RANDOM
    ep->rng = default_random;
#endif
}

void nltp_set_random(nltp_endpoint_t *ep, nltp_random_fn rng) {
    ep->rng = rng;
}

void nltp_set_rx_buffer(nltp_endpoint_t *ep, uint8_t *buf, uint32_t cap) {
    ep->rx.out = buf;
    ep->rx.out_cap = cap;
}

void nltp_set_shard_bytes(nltp_endpoint_t *ep, uint16_t shard_bytes) {
    if (shard_bytes < 16u) shard_bytes = 16u;
    if (shard_bytes > NLTP_SHARD_BYTES) shard_bytes = NLTP_SHARD_BYTES;
    ep->tx_shard_bytes = shard_bytes;
}

void nltp_set_key(nltp_endpoint_t *ep, const uint8_t key32[32]) {
    ep->encrypted = key32 != 0;
    if (key32) memcpy(ep->key, key32, 32);
    else nltp_wipe_bytes(ep->key, 32);
    nltp_wipe_bytes(ep->sess, sizeof(ep->sess));   /* a new key starts new sessions */
    nltp_wipe_bytes(ep->pend_sk_i, sizeof(ep->pend_sk_i));
    ep->pend = 0;
    ep->replay_valid = 0;
}

static int b64url_val(char ch) {
    if (ch >= 'A' && ch <= 'Z') return ch - 'A';
    if (ch >= 'a' && ch <= 'z') return ch - 'a' + 26;
    if (ch >= '0' && ch <= '9') return ch - '0' + 52;
    if (ch == '-') return 62;
    if (ch == '_') return 63;
    return -1;
}

static int is_space(char ch) { return ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r'; }

void nltp_key_from_psk(const char *psk, uint8_t out_key32[32]) {
    static const char prefix[] = "NLTP-v1-PASSPHRASE:";
    const char *b = psk, *e;
    size_t len;
    nltp_sha256_t ctx;
    while (is_space(*b)) ++b;
    e = b + strlen(b);
    while (e > b && is_space(e[-1])) --e;
    len = (size_t)(e - b);
    if (len == 43u || len == 44u) {  /* generate_nltp_key(): URL-safe base64 of 32 raw bytes */
        uint8_t raw[33];
        uint32_t acc = 0;
        size_t i, nbits = 0, nout = 0;
        int ok = 1;
        for (i = 0; i < len; ++i) {
            int v;
            if (b[i] == '=') break;
            v = b64url_val(b[i]);
            if (v < 0) {
                ok = 0;
                break;
            }
            acc = (acc << 6) | (uint32_t)v;
            nbits += 6;
            if (nbits >= 8) {
                nbits -= 8;
                if (nout < sizeof(raw)) raw[nout++] = (uint8_t)(acc >> nbits);
            }
        }
        if (ok && nout == 32u) {
            memcpy(out_key32, raw, 32);
            return;
        }
    }
    nltp_sha256_init(&ctx);
    nltp_sha256_update(&ctx, (const uint8_t *)prefix, sizeof(prefix) - 1u);
    nltp_sha256_update(&ctx, (const uint8_t *)b, len);
    nltp_sha256_final(&ctx, out_key32);
}

/* ---------------------------------------------------------------------------
 * Sender
 * ------------------------------------------------------------------------- */

static nltp_txgen_t *tx_slot(nltp_tx_t *tx, uint32_t g) {
    nltp_txgen_t *s = &tx->win[g % NLTP_TX_WINDOW];
    if (!s->valid || s->gen_id != g) {
        uint16_t sb = tx->shard_bytes ? tx->shard_bytes : (uint16_t)NLTP_SHARD_BYTES;
        memset(s, 0, sizeof(*s));
        s->valid = 1;
        s->gen_id = g;
        s->proactive_target = -1;
        gen_geometry(tx->len, sb, g, &s->k, &s->row_width);
    }
    return s;
}

static void tx_fill_row(const nltp_tx_t *tx, uint32_t g, uint8_t i, uint16_t row_width, uint8_t *out) {
    uint32_t sb = tx->shard_bytes ? (uint32_t)tx->shard_bytes : (uint32_t)NLTP_SHARD_BYTES;
    uint32_t off = g * (uint32_t)NLTP_GEN_K * sb + (uint32_t)i * sb;
    uint32_t clen = 0;
    if (off < tx->len) clen = umin32(sb, tx->len - off);
    out[0] = (uint8_t)(clen & 0xFFu);
    out[1] = (uint8_t)(clen >> 8);
    if (clen) memcpy(out + 2, tx->data + off, clen);
    if (2u + clen < row_width) memset(out + 2 + clen, 0, row_width - 2u - clen);
}

static size_t put_shard_hdr(uint8_t *p, uint32_t sid, uint32_t g, uint8_t k, uint8_t idx, uint16_t rw) {
    nltp_write_u32_le(p, sid);
    nltp_write_u32_le(p + 4, g);
    p[8] = k;
    p[9] = idx;
    nltp_write_u16_le(p + 10, rw);
    return NLTP_SHARD_HDR_BYTES;
}

static int tx_send_sys(nltp_endpoint_t *ep, nltp_txgen_t *s) {
    uint32_t n = frame_overhead(ep) + NLTP_SHARD_HDR_BYTES + s->row_width;
    uint8_t *p = payload_buf(ep);
    if (!pacer_consume(ep, n)) return 0;
    put_shard_hdr(p, ep->tx.stream_id, s->gen_id, s->k, s->next_sys, s->row_width);
    tx_fill_row(&ep->tx, s->gen_id, s->next_sys, s->row_width, p + NLTP_SHARD_HDR_BYTES);
    emit(ep, NLTP_PT_DATA_SHARD, NLTP_SHARD_HDR_BYTES + s->row_width);
    s->next_sys++;
    return 1;
}

static int tx_send_parity(nltp_endpoint_t *ep, nltp_txgen_t *s) {
    static uint8_t row[NLTP_ROW_MAX];
    uint32_t n = frame_overhead(ep) + NLTP_SHARD_HDR_BYTES + s->row_width;
    uint8_t *p = payload_buf(ep);
    uint8_t *coded = p + NLTP_SHARD_HDR_BYTES;
    uint8_t p_off, c;
    if (!pacer_consume(ep, n)) return 0;
    p_off = s->next_parity;
    s->next_parity = (uint8_t)((s->next_parity + 1u) % 192u);
    put_shard_hdr(p, ep->tx.stream_id, s->gen_id, s->k, (uint8_t)(s->k + p_off), s->row_width);
    if (s->k == 1u) {
        tx_fill_row(&ep->tx, s->gen_id, 0, s->row_width, coded);  /* matches Python: k == 1 sends the row itself */
    } else {
        uint8_t coeff[NLTP_GEN_K];
        cauchy_row(p_off, s->k, coeff);
        memset(coded, 0, s->row_width);
        for (c = 0; c < s->k; ++c) {
            tx_fill_row(&ep->tx, s->gen_id, c, s->row_width, row);
            gf_axpy(coded, row, coeff[c], s->row_width);
        }
    }
    emit(ep, NLTP_PT_CRYPTIC_PARITY, NLTP_SHARD_HDR_BYTES + s->row_width);
    return 1;
}

static uint32_t fec_loss_pm(const nltp_endpoint_t *ep) {
    if (ep->paced && ep->rate.random_loss_samples >= 2u) return ep->rate.random_loss_pm;
    return ep->peer_loss_pm;
}

static int16_t proactive_shards(const nltp_endpoint_t *ep, const nltp_txgen_t *s) {
    uint32_t p = fec_loss_pm(ep);
    uint32_t n = p >= 30u ? ((uint32_t)s->k * p * 11u + 9999u) / 10000u : 0u;
    if (p >= 5u && ep->paced) {
        uint32_t sb = ep->tx.shard_bytes ? (uint32_t)ep->tx.shard_bytes : (uint32_t)NLTP_SHARD_BYTES;
        uint32_t gen_bytes = (uint32_t)s->k * sb;
        uint32_t bdp = (uint32_t)((uint64_t)ep->rate.rate_kbps * ep->srtt_ms / 8u);
        uint32_t tail_gens = umax32(1u, (bdp + gen_bytes - 1u) / gen_bytes);
        if (s->gen_id + tail_gens >= ep->tx.total_gens) n = umax32(n + 1u, p >= 100u ? 2u : 1u);
    }
    /* Tail insurance: nothing after the final generation reveals a lost last packet */
    if (s->gen_id == ep->tx.total_gens - 1u && s->k > 1u) n = umax32(n, 1u);
    /* Never more proactive repair than half the data: a mis-estimated loss must not flood the link */
    return (int16_t)umin32(n, umax32(1u, s->k / 2u));
}

static void safe_name(const char *in, char *out, size_t cap) {
    size_t i = 0;
    if (!in) in = "";
    for (; *in && i + 1 < cap; ++in) {
        char ch = *in;
        int ok = (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9') ||
                 ch == '.' || ch == '_' || ch == '-';
        out[i++] = ok ? ch : '_';
    }
    out[i] = '\0';
}

static uint32_t tx_start_common(nltp_endpoint_t *ep, const uint8_t *wire_data, uint32_t wire_len,
                                const char *sha_hex, uint32_t raw_len, int is_lz4, const char *filename) {
    nltp_tx_t *tx = &ep->tx;
    char name[48];
    int n;
    uint32_t t = now(ep);
    memset(tx, 0, sizeof(*tx));
    tx->data = wire_data;
    tx->len = wire_len;
    tx->shard_bytes = ep->tx_shard_bytes ? ep->tx_shard_bytes : (uint16_t)NLTP_SHARD_BYTES;
    tx->stream_id = ep->next_stream_id++;
    tx->total_gens = total_gens_for(wire_len, tx->shard_bytes);
    tx->active = 1;
    tx->started_ms = t;
    safe_name(filename, name, sizeof(name));
    if (is_lz4) {
        n = snprintf((char *)tx->meta, sizeof(tx->meta),
                     "{\"stream_id\":%lu,\"kind\":\"file\",\"filename\":\"%s\",\"total_bytes\":%lu,"
                     "\"total_gens\":%lu,\"sha256\":\"%s\",\"codec\":\"lz4\",\"raw_bytes\":%lu}",
                     (unsigned long)tx->stream_id, name, (unsigned long)wire_len,
                     (unsigned long)tx->total_gens, sha_hex, (unsigned long)raw_len);
    } else {
        n = snprintf((char *)tx->meta, sizeof(tx->meta),
                     "{\"stream_id\":%lu,\"kind\":\"file\",\"filename\":\"%s\",\"total_bytes\":%lu,"
                     "\"total_gens\":%lu,\"sha256\":\"%s\"}",
                     (unsigned long)tx->stream_id, name, (unsigned long)wire_len,
                     (unsigned long)tx->total_gens, sha_hex);
    }
    tx->meta_len = (uint16_t)(n > 0 && (size_t)n < sizeof(tx->meta) ? n : 0);
    if (ep->paced) {
        rate_start_stream(&ep->rate, tx->stream_id, t);
        if (ep->rate.mode == 0) {  /* cold start: one burst, then pace; warm: pace at the learned rate */
            uint32_t burst = umin32(NLTP_COLD_BURST_BYTES, wire_len + 2u * (uint32_t)tx->shard_bytes);
            ep->pacer.bonus = umax32(ep->pacer.bonus, burst);
            ep->pacer.tokens = 0;
        }
    }
    nltp_poll(ep);
    return tx->stream_id;
}

uint32_t nltp_send_buffer(nltp_endpoint_t *ep, const uint8_t *data, uint32_t len, const char *filename) {
    char sha[65];
    if (ep->tx.active && !ep->tx.completed) return 0;
    nltp_sha256_hex(data, len, sha);
    return tx_start_common(ep, data, len, sha, len, 0, filename);
}

uint32_t nltp_send_buffer_lz4(nltp_endpoint_t *ep, const uint8_t *data, uint32_t len,
                              uint8_t *comp_scratch, uint32_t comp_cap, const char *filename) {
#if NLTP_TX_ONLY || defined(ESP8266)
    uint32_t htab[512];
    const unsigned hbits = 9u;
#else
    uint32_t htab[1024];
    const unsigned hbits = 10u;
#endif
    char sha[65];
    size_t comp_len = 0u;
    if (ep->tx.active && !ep->tx.completed) return 0;
    nltp_sha256_hex(data, len, sha);
    if (comp_scratch && comp_cap > 0u && len >= 32u) {
        comp_len = nltp_lz4_compress(data, (size_t)len, comp_scratch, (size_t)comp_cap, htab, hbits);
    }
    if (comp_len > 0u && comp_len < (size_t)len) {
        return tx_start_common(ep, comp_scratch, (uint32_t)comp_len, sha, len, 1, filename);
    }
    return tx_start_common(ep, data, len, sha, len, 0, filename);
}

static void tx_poll(nltp_endpoint_t *ep) {
    nltp_tx_t *tx = &ep->tx;
    uint32_t t = now(ep), g, end, eff_win, rto, guard, p;
    int exhausted = 0;
    if (!tx->active || tx->completed) return;
    if (!sess_ready(ep)) return;   /* encrypted: wait for the session handshake before sending */

    rto = umin32(2000u, umax32(55u, ep->srtt_ms * 185u / 100u));
    guard = umax32(35u, ep->srtt_ms * 140u / 100u);
    p = fec_loss_pm(ep);

    if ((!tx->meta_acked || tx->base >= tx->total_gens) && (!tx->meta_sent || t - tx->last_meta_ms >= rto)) {
        memcpy(payload_buf(ep), tx->meta, tx->meta_len);
        emit(ep, NLTP_PT_STREAM_META, tx->meta_len);
        tx->meta_sent = 1;
        tx->last_meta_ms = t;
    }

    eff_win = NLTP_TX_WINDOW;
    if (ep->paced && ep->has_rtt && ep->rate.mode == 1u) {
        uint32_t sb = tx->shard_bytes ? (uint32_t)tx->shard_bytes : (uint32_t)NLTP_SHARD_BYTES;
        uint32_t gen_bytes = (uint32_t)NLTP_GEN_K * sb;
        uint64_t bdp = (uint64_t)ep->rate.rate_kbps * (uint64_t)ep->srtt_ms / 8u;
        uint32_t bdp_gens = (uint32_t)((bdp * 22u / 10u + gen_bytes - 1u) / gen_bytes) + 2u;
        eff_win = umin32(eff_win, umax32(4u, bdp_gens));
    }
    {
        uint32_t base_rtt = ep->rate.min_mean_rtt_ms != U32_INF ? ep->rate.min_mean_rtt_ms : ep->min_rtt_ms;
        uint32_t j_guard = 35u + ep->rate.rttvar_ms * 5u / 2u;
        if (ep->has_rtt && base_rtt != U32_INF && ep->srtt_ms > umax32(base_rtt * 2u, base_rtt + j_guard)) {
            eff_win = umax32(3u, NLTP_TX_WINDOW / 4u);
        }
    }

    /* 1. Repairs the receiver asked for */
    end = umin32(tx->total_gens, tx->base + NLTP_TX_WINDOW);
    for (g = tx->base; g < end && !exhausted; ++g) {
        nltp_txgen_t *s = tx_slot(tx, g);
        uint32_t add, extra, n_send, sent = 0, denom;
        if (!s->pending_need) continue;
        if (t - s->last_repair_ms >= guard) s->repair_in_flight = 0;
        if (s->pending_need <= s->repair_in_flight) {
            s->pending_need = 0;
            continue;
        }
        add = (uint32_t)s->pending_need - s->repair_in_flight;
        denom = umax32(450u, 1000u - umin32(1000u, p * 7u / 10u));
        extra = (add * 1000u + denom - 1u) / denom;
        n_send = umin32(extra, (uint32_t)s->k + 2u);
        while (sent < n_send) {
            if (!tx_send_parity(ep, s)) {
                exhausted = 1;
                break;
            }
            sent++;
        }
        if (sent) {
            s->repair_in_flight = (uint8_t)umin32(255u, s->repair_in_flight + sent);
            s->last_repair_ms = t;
            s->last_tx_ms = t;
        }
        if (sent >= add) s->pending_need = 0;
    }

    /* 2. New data, proactive repairs, and tail probes for the sliding window */
    end = umin32(tx->total_gens, tx->base + eff_win);
    for (g = tx->base; g < end && !exhausted; ++g) {
        nltp_txgen_t *s = tx_slot(tx, g);
        if (s->next_sys < s->k || !s->proactive_done) {
            while (s->next_sys < s->k) {
                if (!tx_send_sys(ep, s)) {
                    exhausted = 1;
                    break;
                }
            }
            if (exhausted) break;
            if (s->proactive_target < 0) s->proactive_target = proactive_shards(ep, s);
            while (s->proactive_sent < s->proactive_target) {
                if (!tx_send_parity(ep, s)) {
                    exhausted = 1;
                    break;
                }
                s->proactive_sent++;
            }
            if (exhausted) break;
            s->proactive_done = 1;
            s->last_tx_ms = t;
        } else if (t - s->last_tx_ms >= rto) {
            uint32_t n_tail = p < 220u ? 1u : 2u, sent = 0;
            while (sent < umin32(n_tail, s->k)) {
                if (!tx_send_parity(ep, s)) {
                    exhausted = 1;
                    break;
                }
                sent++;
            }
            if (sent) {
                s->repair_in_flight = 0;
                s->last_tx_ms = t;
            }
        }
    }
}

static void tx_on_ack(nltp_endpoint_t *ep, uint32_t peer_cid, uint8_t flags, const uint8_t *pl, size_t len) {
    nltp_tx_t *tx = &ep->tx;
    uint32_t sid = nltp_read_u32_le(pl), base_gen = nltp_read_u32_le(pl + 4), echo = nltp_read_u32_le(pl + 8);
    uint8_t loss_q8 = pl[12], n_rep = pl[13], i;
    uint32_t t = now(ep), rtt, rep_pm;
    size_t pos = 14;
    if (!tx->active || tx->completed || sid != tx->stream_id) return;
    if (!tx->have_peer_cid) {
        tx->peer_conn_id = peer_cid;
        tx->have_peer_cid = 1;
    } else if (tx->peer_conn_id != peer_cid) {
        return;
    }
    if ((flags & 0x02u) == 0u || (flags & 0x01u) != 0u) tx->meta_acked = 1;
    rtt = t - echo;
    if (rtt < 30000u) record_rtt(ep, rtt);
    rep_pm = (uint32_t)loss_q8 * 1000u / 255u;
    ep->peer_loss_pm = (uint16_t)((65u * ep->peer_loss_pm + 35u * rep_pm) / 100u);
    if (base_gen > tx->base) tx->base = umin32(base_gen, tx->total_gens);
    for (i = 0; i < n_rep && pos + 4u <= len; ++i, pos += 4u) {
        uint32_t g = base_gen + (uint32_t)(pl[pos] | ((uint16_t)pl[pos + 1] << 8));
        uint8_t need = pl[pos + 3];
        if (need > 0 && g >= tx->base && g < tx->total_gens && g < tx->base + NLTP_TX_WINDOW) {
            tx_slot(tx, g)->pending_need = need;
        }
    }
    if (ep->paced && pos + 4u <= len) {
        uint32_t rx_bytes = nltp_read_u32_le(pl + pos);
        uint32_t arrival = pos + 8u <= len ? nltp_read_u32_le(pl + pos + 4) : 0u;
        if (rate_on_ack(&ep->rate, t, sid, rx_bytes, ep->srtt_ms, arrival)) pacer_set_rate(&ep->pacer, ep->rate.rate_kbps);
    }
    tx_poll(ep);
}

static void tx_on_fin(nltp_endpoint_t *ep, uint32_t peer_cid, const uint8_t *pl) {
    nltp_tx_t *tx = &ep->tx;
    uint32_t sid = nltp_read_u32_le(pl);
    if (!tx->active || tx->completed || sid != tx->stream_id) return;
    if (tx->have_peer_cid && tx->peer_conn_id != peer_cid) return;
    tx->meta_acked = 1;
    tx->completed = 1;
    tx->verified = pl[4] == 1u;
    tx->completed_ms = now(ep);
}

/* ---------------------------------------------------------------------------
 * Receiver
 * ------------------------------------------------------------------------- */

#if !NLTP_TX_ONLY
static int json_find(const char *js, size_t len, const char *key, const char **val) {
    char pat[40];
    size_t pl, i;
    int n = snprintf(pat, sizeof(pat), "\"%s\"", key);
    if (n <= 0) return 0;
    pl = (size_t)n;
    for (i = 0; i + pl <= len; ++i) {
        if (memcmp(js + i, pat, pl) == 0) {
            size_t j = i + pl;
            while (j < len && (js[j] == ' ' || js[j] == ':')) ++j;
            *val = js + j;
            return 1;
        }
    }
    return 0;
}

static uint32_t json_u32(const char *js, size_t len, const char *key) {
    const char *v;
    uint32_t x = 0;
    if (!json_find(js, len, key, &v)) return 0;
    while (v < js + len && *v >= '0' && *v <= '9') x = x * 10u + (uint32_t)(*v++ - '0');
    return x;
}

static void json_str(const char *js, size_t len, const char *key, char *out, size_t cap) {
    const char *v;
    size_t i = 0;
    out[0] = '\0';
    if (!json_find(js, len, key, &v) || v >= js + len || *v != '"') return;
    ++v;
    while (v < js + len && *v != '"' && i + 1 < cap) out[i++] = *v++;
    out[i] = '\0';
}

static int bit_get(const uint8_t *b, uint32_t i) { return (b[i >> 3] >> (i & 7u)) & 1u; }
static void bit_set(uint8_t *b, uint32_t i) { b[i >> 3] = (uint8_t)(b[i >> 3] | (1u << (i & 7u))); }

static void rx_reset_stream(nltp_rx_t *rx, uint32_t peer_cid, uint32_t sid) {
    uint8_t *out = rx->out;
    uint32_t cap = rx->out_cap, i;
    memset(rx, 0, offsetof(nltp_rx_t, slots));
    rx->out = out;
    rx->out_cap = cap;
    rx->active = 1;
    rx->peer_cid = peer_cid;
    rx->stream_id = sid;
    rx->max_gen_seen = -1;
    rx->max_sys_seen = -1;
    rx->max_sys_ord = -1;
    for (i = 0; i < NLTP_RX_SLOTS; ++i) rx->slots[i].gen_id = -1;
}

static void send_fin(nltp_endpoint_t *ep) {
    uint8_t *p = payload_buf(ep);
    nltp_write_u32_le(p, ep->rx.stream_id);
    p[4] = ep->rx.sha_ok ? 1u : 0u;
    emit(ep, NLTP_PT_STREAM_FIN_ACK, 5);
}

static void rx_try_finalize(nltp_endpoint_t *ep) {
    nltp_rx_t *rx = &ep->rx;
    char got[65];
    if (rx->completed || !rx->meta_received || rx->n_solved < rx->total_gens) return;
    if (rx->out && rx->total_bytes <= rx->out_cap) {
        if (rx->is_lz4) {
            int ok_lz4 = 0;
            if (rx->raw_bytes <= rx->out_cap) {
                if (NLTP_LZ4_INPLACE_MARGIN(rx->total_bytes) <= (size_t)(rx->out_cap - rx->raw_bytes)) {
                    ok_lz4 = nltp_lz4_decompress_inplace(rx->out, rx->out_cap, rx->total_bytes, rx->raw_bytes);
                }
#if !defined(NLTP_NO_MALLOC)
                else if (rx->total_bytes > 0u) {
                    uint8_t *tmp = (uint8_t *)malloc(rx->total_bytes);
                    if (tmp) {
                        memcpy(tmp, rx->out, rx->total_bytes);
                        ok_lz4 = nltp_lz4_decompress(tmp, rx->total_bytes, rx->out, rx->raw_bytes);
                        free(tmp);
                    }
                } else if (rx->raw_bytes == 0u) {
                    ok_lz4 = 1;
                }
#endif
            }
            if (ok_lz4) {
                rx->total_bytes = rx->raw_bytes;
                nltp_sha256_hex(rx->out, rx->total_bytes, got);
                rx->sha_ok = strcmp(got, rx->want_sha) == 0;
            } else {
                rx->sha_ok = 0;
            }
        } else {
            nltp_sha256_hex(rx->out, rx->total_bytes, got);
            rx->sha_ok = strcmp(got, rx->want_sha) == 0;
        }
    }
    rx->completed = 1;
    send_fin(ep);
    send_fin(ep);
}

static void rx_send_ack(nltp_endpoint_t *ep) {
    nltp_rx_t *rx = &ep->rx;
    uint8_t *p = payload_buf(ep);
    uint32_t expected, loss, g, max_g, r_guard = 0u, max_g_lead = 0u;
    uint8_t n_rep = 0;
    size_t pos = 14;
    if (rx->max_gen_seen >= 0) {
        uint32_t raw_expected;
        int is_final_gen = rx->meta_received && ((uint32_t)rx->max_gen_seen + 1u >= rx->total_gens);
        max_g = (uint32_t)rx->max_gen_seen;
        r_guard = is_final_gen ? 0u : umin32(rx->reorder_wnd, 6u);
        max_g_lead = (uint32_t)(rx->max_sys_seen + 1);
        raw_expected = max_g * NLTP_GEN_K + max_g_lead;
        expected = raw_expected > r_guard ? raw_expected - r_guard : 0u;
        if (expected < rx->sys_shards_rcvd) expected = rx->sys_shards_rcvd;
    } else {
        max_g = 0;
        expected = 1;
    }
    if (expected == 0) expected = 1;
    loss = rx->sys_shards_rcvd >= expected ? 0u : 1000u - rx->sys_shards_rcvd * 1000u / expected;
    if (loss > 920u) loss = 920u;
    if (rx->max_gen_seen >= 0) {
        for (g = rx->base_gen; g <= max_g && n_rep < 16u; ++g) {
            const nltp_rxslot_t *s = 0;
            uint32_t i, have, need;
            if (bit_get(rx->solved, g)) continue;
            for (i = 0; i < NLTP_RX_SLOTS; ++i) {
                if (rx->slots[i].gen_id == (int32_t)g) {
                    s = &rx->slots[i];
                    break;
                }
            }
            have = s ? s->rank : 0u;
            need = s ? (uint32_t)s->k - s->rank : NLTP_GEN_K;
            if (g < max_g && need > 0) {
                if (r_guard > 0u && max_g < g + 2u && max_g_lead < r_guard) continue;
            } else if (g == max_g && s && s->max_seen_idx >= 0) {
                uint32_t max_seen = umin32(s->k, (uint32_t)s->max_seen_idx + 1u);
                uint32_t g_guard = umin32(r_guard, 4u);
                uint32_t eff_seen = max_seen > g_guard ? max_seen - g_guard : 0u;
                uint32_t confirmed = eff_seen > have ? umin32(need, eff_seen - have) : 0u;
                if (!confirmed) continue;
                need = confirmed;
            } else {
                continue;
            }
            nltp_write_u16_le(p + pos, (uint16_t)(g - rx->base_gen));
            p[pos + 2] = (uint8_t)have;
            p[pos + 3] = (uint8_t)need;
            pos += 4;
            n_rep++;
        }
    }
    nltp_write_u32_le(p, rx->stream_id);
    nltp_write_u32_le(p + 4, rx->base_gen);
    nltp_write_u32_le(p + 8, rx->ack_echo_ts);
    p[12] = (uint8_t)((loss * 255u + 500u) / 1000u);
    p[13] = n_rep;
    nltp_write_u32_le(p + pos, rx->bytes_rcvd);
    nltp_write_u32_le(p + pos + 4, rx->arrival_kbps);
    pos += 8;
    emit_flags(ep, NLTP_PT_PROPRIOCEPTIVE_ACK, rx->meta_received ? 0x03u : 0x02u, pos);
    rx->last_ack_ms = now(ep);
    rx->ack_pending = 0;
}

static void rx_deliver(nltp_rx_t *rx, nltp_rxslot_t *s) {
    uint8_t i;
    uint32_t sb = rx->peer_shard_bytes ? (uint32_t)rx->peer_shard_bytes : (uint32_t)NLTP_SHARD_BYTES;
    uint32_t gk = rx->peer_gen_k ? (uint32_t)rx->peer_gen_k : (uint32_t)NLTP_GEN_K;
    for (i = 0; i < s->k; ++i) {
        const uint8_t *row = s->rows[s->pivot_of_col[i]];
        uint32_t clen = (uint32_t)row[0] | ((uint32_t)row[1] << 8);
        uint32_t off = (s->gen_id == 0 && s->k == 1u)
                           ? 0u
                           : ((uint32_t)s->gen_id * gk * sb + (uint32_t)i * sb);
        if (clen > (uint32_t)s->row_width - 2u) clen = (uint32_t)s->row_width - 2u;
        if (rx->out && off + clen <= rx->out_cap) memcpy(rx->out + off, row + 2, clen);
    }
}

/* Incremental Gauss-Jordan: returns 1 when the generation becomes solvable. */
static int rx_add_row(nltp_rxslot_t *s, const uint8_t *coeff_in, const uint8_t *data) {
    uint8_t *coeff = s->coeff[s->rank];
    uint8_t *row = s->rows[s->rank];
    uint8_t c, r, pivot = 0xFFu;
    memcpy(coeff, coeff_in, s->k);
    memcpy(row, data, s->row_width);
    for (c = 0; c < s->k; ++c) {
        uint8_t pr = s->pivot_of_col[c];
        if (pr != 0xFFu && coeff[c]) {
            uint8_t f = coeff[c];
            gf_axpy(coeff, s->coeff[pr], f, s->k);
            gf_axpy(row, s->rows[pr], f, s->row_width);
        }
    }
    for (c = 0; c < s->k; ++c) {
        if (coeff[c]) {
            pivot = c;
            break;
        }
    }
    if (pivot == 0xFFu) return 0;  /* linearly dependent: carries no new information */
    {
        uint8_t inv = gf_inv(coeff[pivot]);
        gf_scale(coeff, inv, s->k);
        gf_scale(row, inv, s->row_width);
    }
    for (r = 0; r < s->rank; ++r) {
        uint8_t f = s->coeff[r][pivot];
        if (f) {
            gf_axpy(s->coeff[r], coeff, f, s->k);
            gf_axpy(s->rows[r], row, f, s->row_width);
        }
    }
    s->pivot_of_col[pivot] = s->rank;
    s->rank++;
    return s->rank == s->k;
}

/* When may an incoming packet replace the current receive stream?
 * - same sender (conn_id): a newer stream_id, or any other stream once the current one is complete;
 * - different conn_id (e.g. a device that woke from deep sleep with a fresh conn_id): only once the
 *   current stream is complete or has been silent for NLTP_RX_TAKEOVER_MS. Taking over immediately
 *   (v1.2.1) let a single forged plaintext packet, or one delayed/replayed packet from the sender's
 *   previous session, wipe a transfer in progress that the sender could then never repair. */
static int rx_should_reset(const nltp_rx_t *rx, uint32_t peer_cid, uint32_t sid, uint32_t t) {
    if (!rx->active) return 1;
    if (peer_cid != rx->peer_cid) return rx->completed || (uint32_t)(t - rx->last_rx_ms) >= NLTP_RX_TAKEOVER_MS;
    return sid > rx->stream_id || (rx->completed && sid != rx->stream_id);
}

static void rx_on_shard(nltp_endpoint_t *ep, uint32_t peer_cid, uint8_t type, uint32_t pkt_ts,
                        const uint8_t *pl, size_t len) {
    nltp_rx_t *rx = &ep->rx;
    uint32_t sid = nltp_read_u32_le(pl), g = nltp_read_u32_le(pl + 4);
    uint8_t k = pl[8], idx = pl[9];
    uint16_t rw = (uint16_t)(pl[10] | (pl[11] << 8));
    const uint8_t *shard = pl + NLTP_SHARD_HDR_BYTES;
    uint32_t t = now(ep), i;
    nltp_rxslot_t *s = 0;
    int newly_solved = 0;

    if (len < NLTP_SHARD_HDR_BYTES + (size_t)rw || k == 0 || k > NLTP_GEN_K || rw > NLTP_ROW_MAX || rw < 2) return;
    /* No receive buffer yet: ignore (unacknowledged, so the sender repairs it later) rather than mark
       generations solved whose data could not be stored */
    if (!rx->out) return;
    if (rx_should_reset(rx, peer_cid, sid, t)) {
        rx_reset_stream(rx, peer_cid, sid);
    } else if (peer_cid != rx->peer_cid || sid != rx->stream_id) {
        return;  /* late packet from another sender / an earlier session, or a cancelled stream */
    }
    rx->last_rx_ms = t;
    if (rx->completed) {
        send_fin(ep);
        return;
    }
    rx->shards_rcvd++;
    rx->bytes_rcvd += (uint32_t)len;
    if (!rx->arr_started) {
        rx->arr_started = 1;
        rx->arr_t0 = t;
        rx->arr_b0 = rx->bytes_rcvd;
        rx->arr_n0 = rx->shards_rcvd;
    } else if (rx->shards_rcvd - rx->arr_n0 >= 16u && t - rx->arr_t0 >= 5u) {
        rx->arrival_kbps = (uint32_t)((uint64_t)(rx->bytes_rcvd - rx->arr_b0) * 8u / (t - rx->arr_t0));
        rx->arr_t0 = t;
        rx->arr_b0 = rx->bytes_rcvd;
        rx->arr_n0 = rx->shards_rcvd;
    }
    if (g >= NLTP_RX_MAX_GENS) return;
    if (k >= 2u) {
        rx->peer_shard_bytes = (uint16_t)(rw - 2u);
        if (g == 0u || k > rx->peer_gen_k) rx->peer_gen_k = k;
    }

    if (!bit_get(rx->solved, g)) {
        for (i = 0; i < NLTP_RX_SLOTS; ++i) {
            if (rx->slots[i].gen_id == (int32_t)g) {
                s = &rx->slots[i];
                break;
            }
        }
        if (!s) {
            for (i = 0; i < NLTP_RX_SLOTS; ++i) {
                if (rx->slots[i].gen_id < 0) {
                    s = &rx->slots[i];
                    s->gen_id = (int32_t)g;
                    s->k = k;
                    s->rank = 0;
                    s->sys_only = 1;
                    s->row_width = rw;
                    s->max_seen_idx = -1;
                    memset(s->seen, 0, sizeof(s->seen));
                    memset(s->pivot_of_col, 0xFF, sizeof(s->pivot_of_col));
                    break;
                }
            }
        }
    }
    if (type == NLTP_PT_DATA_SHARD && idx < k) {
        int dup = bit_get(rx->solved, g) || (s && bit_get(s->seen, idx));
        if (!dup) {
            uint32_t gk = rx->peer_gen_k ? (uint32_t)rx->peer_gen_k : (uint32_t)NLTP_GEN_K;
            int32_t sys_ord = (int32_t)(g * gk + (uint32_t)idx);
            rx->sys_shards_rcvd++;
            if (sys_ord < rx->max_sys_ord) {
                uint32_t dist = (uint32_t)(rx->max_sys_ord - sys_ord);
                if (dist <= 32u && dist > (uint32_t)rx->reorder_wnd) {
                    rx->reorder_wnd = (uint8_t)umin32(16u, dist);
                }
            } else {
                rx->max_sys_ord = sys_ord;
            }
        }
    }
    if ((int32_t)g > rx->max_gen_seen) {
        rx->max_gen_seen = (int32_t)g;
        rx->max_sys_seen = -1;
    }
    if ((int32_t)g == rx->max_gen_seen) {
        int16_t m = (type == NLTP_PT_CRYPTIC_PARITY) ? (int16_t)(k - 1) : (int16_t)idx;
        if (m > rx->max_sys_seen) rx->max_sys_seen = m;
    }

    if (s && s->k == k && s->row_width == rw && idx < 208u && !bit_get(s->seen, idx)) {
        bit_set(s->seen, idx);
        if ((int16_t)idx > s->max_seen_idx) s->max_seen_idx = (int16_t)idx;
        if (idx < k && s->sys_only) {
            /* Zero-GF(2^8) fast path: unit row is already orthogonal to all prior unit rows */
            memset(s->coeff[s->rank], 0, k);
            s->coeff[s->rank][idx] = 1u;
            memcpy(s->rows[s->rank], shard, rw);
            s->pivot_of_col[idx] = s->rank;
            s->rank++;
        } else {
            uint8_t coeff[NLTP_GEN_K];
            s->sys_only = 0;
            if (idx < k) {
                memset(coeff, 0, k);
                coeff[idx] = 1;
            } else if (k == 1u) {
                coeff[0] = 1;  /* Python sends the bare row for k == 1 */
            } else {
                cauchy_row((uint8_t)(idx - k), k, coeff);
            }
            (void)rx_add_row(s, coeff, shard);
        }
    }
    for (i = 0; i < NLTP_RX_SLOTS; ++i) {
        nltp_rxslot_t *cs = &rx->slots[i];
        if (cs->gen_id >= 0 && cs->rank == cs->k &&
            ((cs->gen_id == 0 && cs->k == 1u) || rx->peer_shard_bytes != 0u)) {
            uint32_t cg = (uint32_t)cs->gen_id;
            rx_deliver(rx, cs);
            bit_set(rx->solved, cg);
            rx->n_solved++;
            cs->gen_id = -1;
            newly_solved = 1;
        }
    }
    if (newly_solved) {
        while (rx->base_gen < NLTP_RX_MAX_GENS && bit_get(rx->solved, rx->base_gen)) rx->base_gen++;
    }

    rx->ack_echo_ts = pkt_ts;
    if (newly_solved && rx->meta_received && rx->n_solved >= rx->total_gens) {
        rx_try_finalize(ep);
        return;
    }
    if (newly_solved || rx->shards_rcvd % ACK_EVERY_SHARDS == 0u || t - rx->last_ack_ms >= ACK_MAX_DELAY_MS) {
        rx->ack_pending = 1;
    }
}

static void rx_on_meta(nltp_endpoint_t *ep, uint32_t peer_cid, uint32_t pkt_ts, const uint8_t *pl, size_t len) {
    nltp_rx_t *rx = &ep->rx;
    const char *js = (const char *)pl;
    char codec[12];
    uint32_t sid = json_u32(js, len, "stream_id");
    if (rx_should_reset(rx, peer_cid, sid, now(ep))) {
        rx_reset_stream(rx, peer_cid, sid);
    } else if (peer_cid != rx->peer_cid || sid != rx->stream_id) {
        return;
    }
    rx->last_rx_ms = now(ep);
    if (rx->completed) {
        send_fin(ep);
        return;
    }
    rx->total_bytes = json_u32(js, len, "total_bytes");
    rx->total_gens = json_u32(js, len, "total_gens");
    json_str(js, len, "sha256", rx->want_sha, sizeof(rx->want_sha));
    json_str(js, len, "filename", rx->filename, sizeof(rx->filename));
    json_str(js, len, "codec", codec, sizeof(codec));
    if (strcmp(codec, "lz4") == 0) {
        rx->is_lz4 = 1u;
        rx->raw_bytes = json_u32(js, len, "raw_bytes");
    } else {
        rx->is_lz4 = 0u;
        rx->raw_bytes = rx->total_bytes;
    }
    rx->meta_received = 1;
    rx->ack_echo_ts = pkt_ts;
    if (rx->n_solved >= rx->total_gens) {
        rx_try_finalize(ep);
        return;
    }
    rx_send_ack(ep);
}
#endif /* !NLTP_TX_ONLY */

/* ---------------------------------------------------------------------------
 * Reliable out-of-line reflex lane (Giant-Fiber emergency / command messages)
 * ------------------------------------------------------------------------- */

#define REFLEX_RETX_MIN_MS 45u
#define REFLEX_RETX_FIRST_MAX_MS 150u
#define REFLEX_RETX_MAX_MS 250u
#define REFLEX_ACK_TIMEOUT_MS 10000u

static void reflex_emit_one(nltp_endpoint_t *ep, const nltp_reflex_out_t *slot) {
    uint8_t *p = payload_buf(ep);
    nltp_write_u32_le(p, slot->seq);
    nltp_write_u16_le(p + 4, slot->len);
    if (slot->len > 0u) memcpy(p + 6, slot->data, slot->len);
    emit_flags(ep, NLTP_PT_REFLEX_MSG, 0x02u, 6u + (size_t)slot->len);
}

uint32_t nltp_send_reflex(nltp_endpoint_t *ep, const uint8_t *msg, uint16_t len) {
    uint32_t i, t = now(ep), interval;
    nltp_reflex_out_t *slot = 0;
    if (len > NLTP_REFLEX_MAX_LEN || (len > 0u && !msg)) return 0u;
    for (i = 0; i < NLTP_REFLEX_OUT_SLOTS; ++i) {
        if (!ep->reflex_out[i].used) {
            slot = &ep->reflex_out[i];
            break;
        }
    }
    if (!slot) return 0u;
    ep->reflex_tx_seq++;
    if (ep->reflex_tx_seq == 0u) ep->reflex_tx_seq = 1u;
    interval = umin32(REFLEX_RETX_FIRST_MAX_MS, umax32(REFLEX_RETX_MIN_MS, ep->srtt_ms * 125u / 100u));
    slot->used = 1u;
    slot->sent_initial = 0u;
    slot->len = len;
    slot->seq = ep->reflex_tx_seq;
    slot->interval_ms = interval;
    slot->deadline_ms = t + REFLEX_ACK_TIMEOUT_MS;
    if (len > 0u) memcpy(slot->data, msg, len);
    if (sess_ready(ep)) {
        reflex_emit_one(ep, slot);
        reflex_emit_one(ep, slot);
        slot->sent_initial = 1u;
        slot->next_tx_ms = t + interval;
    } else {
        slot->next_tx_ms = t;
    }
    return slot->seq;
}

int nltp_reflex_acked(const nltp_endpoint_t *ep, uint32_t seq) {
    uint8_t i;
    if (seq == 0u) return 0;
    for (i = 0; i < ep->reflex_acked_len; ++i) {
        uint8_t idx = (uint8_t)((ep->reflex_acked_head + NLTP_REFLEX_SEEN_MAX - 1u - i) % NLTP_REFLEX_SEEN_MAX);
        if (ep->reflex_acked_seq[idx] == seq) return 1;
    }
    return 0;
}

uint32_t nltp_reflex_pending(const nltp_endpoint_t *ep) {
    uint32_t i, n = 0;
    for (i = 0; i < NLTP_REFLEX_OUT_SLOTS; ++i) {
        if (ep->reflex_out[i].used) n++;
    }
    return n;
}

int nltp_pop_reflex(nltp_endpoint_t *ep, uint8_t *out_buf, uint16_t cap, uint16_t *out_len, uint32_t *out_peer_cid) {
    const nltp_reflex_in_t *in;
    uint16_t copy_len;
    if (ep->reflex_in_count == 0u) return 0;
    in = &ep->reflex_in[ep->reflex_in_head];
    if (out_len) *out_len = in->len;
    if (out_peer_cid) *out_peer_cid = in->peer_cid;
    if (out_buf && cap > 0u && in->len > 0u) {
        copy_len = in->len < cap ? in->len : cap;
        memcpy(out_buf, in->data, copy_len);
    }
    ep->reflex_in_head = (uint8_t)((ep->reflex_in_head + 1u) % NLTP_REFLEX_IN_SLOTS);
    ep->reflex_in_count--;
    return 1;
}

static void reflex_poll(nltp_endpoint_t *ep) {
    uint32_t i, t = now(ep);
    for (i = 0; i < NLTP_REFLEX_OUT_SLOTS; ++i) {
        nltp_reflex_out_t *slot = &ep->reflex_out[i];
        if (!slot->used) continue;
        if ((int32_t)(t - slot->deadline_ms) >= 0) {
            slot->used = 0u;
            ep->reflex_expired++;
            continue;
        }
        if ((int32_t)(t - slot->next_tx_ms) >= 0) {
            if (!sess_ready(ep)) {
                slot->next_tx_ms = t + slot->interval_ms;
                continue;
            }
            if (!slot->sent_initial) {
                reflex_emit_one(ep, slot);
                reflex_emit_one(ep, slot);
                slot->sent_initial = 1u;
                slot->next_tx_ms = t + slot->interval_ms;
            } else {
                reflex_emit_one(ep, slot);
                ep->reflex_retransmits++;
                slot->interval_ms = umin32(REFLEX_RETX_MAX_MS, slot->interval_ms * 3u / 2u);
                slot->next_tx_ms = t + slot->interval_ms;
            }
        }
    }
}

static void rx_on_reflex(nltp_endpoint_t *ep, uint32_t peer_cid, const uint8_t *pl, size_t len) {
    uint32_t r_seq;
    uint16_t m_len;
    uint8_t i, seen = 0u;
    if (len < 6u) return;
    r_seq = nltp_read_u32_le(pl);
    m_len = (uint16_t)(pl[4] | ((uint16_t)pl[5] << 8));
    if (6u + (size_t)m_len > len || m_len > NLTP_REFLEX_MAX_LEN) return;
    for (i = 0; i < ep->reflex_seen_len; ++i) {
        uint8_t idx = (uint8_t)((ep->reflex_seen_head + NLTP_REFLEX_SEEN_MAX - 1u - i) % NLTP_REFLEX_SEEN_MAX);
        if (ep->reflex_seen_cid[idx] == peer_cid && ep->reflex_seen_seq[idx] == r_seq) {
            seen = 1u;
            break;
        }
    }
    if (!seen) {
        ep->reflex_seen_cid[ep->reflex_seen_head] = peer_cid;
        ep->reflex_seen_seq[ep->reflex_seen_head] = r_seq;
        ep->reflex_seen_head = (uint8_t)((ep->reflex_seen_head + 1u) % NLTP_REFLEX_SEEN_MAX);
        if (ep->reflex_seen_len < NLTP_REFLEX_SEEN_MAX) ep->reflex_seen_len++;
        if (ep->reflex_in_count < NLTP_REFLEX_IN_SLOTS) {
            uint8_t tail = (uint8_t)((ep->reflex_in_head + ep->reflex_in_count) % NLTP_REFLEX_IN_SLOTS);
            ep->reflex_in[tail].peer_cid = peer_cid;
            ep->reflex_in[tail].len = m_len;
            if (m_len > 0u) memcpy(ep->reflex_in[tail].data, pl + 6, m_len);
            ep->reflex_in_count++;
        }
    }
    nltp_write_u32_le(payload_buf(ep), r_seq);
    emit_flags(ep, NLTP_PT_REFLEX_ACK, 0x02u, 4u);
}

static void tx_on_reflex_ack(nltp_endpoint_t *ep, const uint8_t *pl, size_t len) {
    uint32_t a_seq, i;
    if (len < 4u) return;
    a_seq = nltp_read_u32_le(pl);
    for (i = 0; i < NLTP_REFLEX_OUT_SLOTS; ++i) {
        if (ep->reflex_out[i].used && ep->reflex_out[i].seq == a_seq) {
            ep->reflex_out[i].used = 0u;
            ep->reflex_acks_rx++;
            ep->reflex_acked_seq[ep->reflex_acked_head] = a_seq;
            ep->reflex_acked_head = (uint8_t)((ep->reflex_acked_head + 1u) % NLTP_REFLEX_SEEN_MAX);
            if (ep->reflex_acked_len < NLTP_REFLEX_SEEN_MAX) ep->reflex_acked_len++;
            break;
        }
    }
}

/* ---------------------------------------------------------------------------
 * Datagram dispatch + poll
 * ------------------------------------------------------------------------- */

void nltp_on_datagram(nltp_endpoint_t *ep, const uint8_t *raw, size_t len) {
    uint32_t cid, ts;
    uint8_t type, flags = 0u;
    const uint8_t *pl;
    size_t plen;
    ep->pkts_rx++;
    ep->bytes_rx += (uint32_t)len;
    if (len < NLTP_HEADER_BYTES || raw[0] != NLTP_MAGIC_0 || raw[1] != NLTP_MAGIC_1) return;
    if (!ep->encrypted && (raw[2] >> 4) != NLTP_VERSION) return;   /* encrypted frames are version 2 */

    if (ep->encrypted) {
        /* Strict like Python: with a key set, only authentic, fresh frames of a session are accepted. */
        uint8_t ptype = raw[2] & 0x0Fu;
        uint32_t peer_cid;
        uint64_t seq;
        int i, hit = -1, known = 0;
        if (!(raw[3] & NLTP_FLAG_ENCRYPTED) || len < NLTP_ENC_HEADER_BYTES + NLTP_AEAD_TAG_BYTES) {
            ep->auth_drops++;
            return;
        }
        if ((raw[2] >> 4) != NLTP_SESSION_VERSION) {   /* 1.3.x frame sealed with the long-term key */
            ep->legacy_drops++;
            ep->auth_drops++;
            return;
        }
        peer_cid = nltp_read_u32_le(raw + 4);
        seq = nltp_read_u64_le(raw + 8);
        if (ptype == NLTP_PT_NO_SESSION) {
            sess_on_no_session(ep, raw, len);
            return;
        }
        if (len > NLTP_ENC_HEADER_BYTES + NLTP_AEAD_TAG_BYTES + sizeof(ep->dec)) {
            ep->auth_drops++;
            return;
        }
        if (ptype == NLTP_PT_HELLO || ptype == NLTP_PT_HELLO_ACK) {
            if (!nltp_unpack_encrypted_frame_v(raw, len, ep->key, NLTP_SESSION_VERSION, 0, &type, &flags, &cid, &ts,
                                               ep->dec, &plen)) {
                ep->auth_drops++;
                return;
            }
            ep->valid_rx++;   /* authenticated with the long-term key: the app may follow this address */
            if (ptype == NLTP_PT_HELLO && plen >= NLTP_HELLO_PT_BYTES) {
                sess_on_hello(ep, cid, ep->dec, ep->dec + 16);
            }
            if (ptype == NLTP_PT_HELLO_ACK && plen >= NLTP_HELLO_ACK_PT_BYTES) {
                sess_on_ack(ep, cid, ep->dec, ep->dec + 16, ep->dec + 32);
            }
            return;
        }
        for (i = 0; i < 2; ++i) {
            nltp_session_t *s = &ep->sess[i];
            if (!s->used || s->peer_cid != peer_cid) continue;
            known = 1;
            if (nltp_unpack_encrypted_frame_v(raw, len, s->k_rx, NLTP_SESSION_VERSION, &s->replay, &type, &flags, &cid,
                                              &ts, ep->dec, &plen)) {
                hit = i;
                break;
            }
        }
        if (hit < 0) {
            if (!known) sess_send_no_session(ep, peer_cid, seq);
            ep->auth_drops++;
            return;
        }
        ep->sess[hit].last_rx_ms = now(ep);
        if (hit == 1) {   /* authenticated traffic proves the candidate: it becomes the live session */
            nltp_session_t tmp = ep->sess[0];
            ep->sess[0] = ep->sess[1];
            ep->sess[1] = tmp;
        }
        pl = ep->dec;
    } else {
        uint8_t hdr0[NLTP_HEADER_BYTES];
        uint32_t crc;
        if (raw[3] & NLTP_FLAG_ENCRYPTED) {
            ep->auth_drops++;
            return;
        }
        memcpy(hdr0, raw, NLTP_HEADER_BYTES);
        nltp_write_u32_le(hdr0 + 8, 0u);
        crc = nltp_crc32_ieee(hdr0, NLTP_HEADER_BYTES, 0u);
        pl = raw + NLTP_HEADER_BYTES;
        plen = len - NLTP_HEADER_BYTES;
        if (plen) crc = nltp_crc32_ieee(pl, plen, crc);
        if (crc != nltp_read_u32_le(raw + 8)) {
            ep->crc_drops++;
            return;
        }
        type = raw[2] & 0x0Fu;
        flags = raw[3] & 0x7Fu;
        cid = nltp_read_u32_le(raw + 4);
        ts = nltp_read_u32_le(raw + 12);
    }
    ep->valid_rx++;

    switch (type) {
    case NLTP_PT_DATA_SHARD:
    case NLTP_PT_CRYPTIC_PARITY:
#if !NLTP_TX_ONLY
        if (plen >= NLTP_SHARD_HDR_BYTES) rx_on_shard(ep, cid, type, ts, pl, plen);
#endif
        break;
    case NLTP_PT_STREAM_META:
#if !NLTP_TX_ONLY
        rx_on_meta(ep, cid, ts, pl, plen);
#endif
        break;
    case NLTP_PT_PROPRIOCEPTIVE_ACK:
        if (plen >= 14u) tx_on_ack(ep, cid, flags, pl, plen);
        break;
    case NLTP_PT_STREAM_FIN_ACK:
        if (plen >= 5u) tx_on_fin(ep, cid, pl);
        break;
    case NLTP_PT_REFLEX_MSG:
        rx_on_reflex(ep, cid, pl, plen);
        break;
    case NLTP_PT_REFLEX_ACK:
        tx_on_reflex_ack(ep, pl, plen);
        break;
    case NLTP_PT_PING:
        if (plen >= 4u) {
            memcpy(payload_buf(ep), pl, 4);
            emit(ep, NLTP_PT_PONG, 4);
        }
        break;
    case NLTP_PT_PONG:
        if (plen >= 4u) {
            uint32_t rtt = now(ep) - nltp_read_u32_le(pl);
            if (rtt < 30000u) record_rtt(ep, rtt);
            ep->pongs_rx++;
        }
        break;
    default:
        break;
    }
}

void nltp_send_ping(nltp_endpoint_t *ep) {
    nltp_write_u32_le(payload_buf(ep), now(ep));
    emit(ep, NLTP_PT_PING, 4);
}

void nltp_poll(nltp_endpoint_t *ep) {
    sess_poll(ep);
    reflex_poll(ep);
#if !NLTP_TX_ONLY
    if (ep->rx.ack_pending && !ep->rx.completed) rx_send_ack(ep);
#endif
    tx_poll(ep);
    if (ep->keepalive_ms && now(ep) - ep->last_tx_ms >= ep->keepalive_ms) nltp_send_ping(ep);
}
