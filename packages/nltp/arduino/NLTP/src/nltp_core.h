/*
 * nltp_core.h — portable C99 NLTP v1 endpoint (sender + receiver) for microcontrollers and hosts.
 *
 * Wire-compatible with the Python NLTPServer / NLTPClient (necklink/nltp.py) for unencrypted transfers:
 * same frames, shard layout, GF(2^8) Cauchy repair rows, proprioceptive ACKs (incl. the bytes/arrival-rate
 * trailer), STREAM_META JSON and FIN_ACK. No heap allocation and no floating point; all memory lives in
 * nltp_endpoint_t (size it with the NLTP_* macros below — the receive slots dominate).
 *
 * Optional end-to-end ChaCha20-Poly1305 encryption with X25519+PSK forward secrecy and 512-packet anti-replay
 * (nltp_set_key), matching NLTPServer/NLTPClient(psk=...). With a key set, unencrypted frames are rejected.
 *
 * Relay framing and STUN live in nltp_net.h; PING/PONG, NAT keepalive, and the out-of-line reliable reflex
 * lane (nltp_send_reflex / nltp_pop_reflex) are here. Not in this version: telemetry lane, and more than one
 * bulk stream per direction.
 *
 * Usage (one peer per endpoint; the app owns the socket and the peer address):
 *   nltp_init(&ep, conn_id, max_kbps, send_cb, now_ms_cb, user);
 *   nltp_set_rx_buffer(&ep, buf, cap);          // where received files are written
 *   nltp_send_buffer(&ep, data, len, "fw.bin"); // start sending (data must stay valid until done)
 *   loop: for each datagram: nltp_on_datagram(&ep, pkt, len);  then nltp_poll(&ep) at least every ~1 ms
 */
#ifndef NLTP_CORE_H
#define NLTP_CORE_H

#include <stddef.h>
#include <stdint.h>

#include "nltp_wire.h"

#ifdef __cplusplus
extern "C" {
#endif

#ifndef NLTP_TX_ONLY
#define NLTP_TX_ONLY 0                      /* set to 1 for ~4.8 KB RAM sensor/station TX + reflex-RX nodes */
#endif
#ifndef NLTP_GEN_K
#define NLTP_GEN_K 8u                       /* shards per generation (must match the peer: Python uses 8) */
#endif
#ifndef NLTP_SHARD_BYTES
#define NLTP_SHARD_BYTES NLTP_DEFAULT_SHARD_LEN  /* 960: max shard size; receivers infer the sender's actual size */
#endif
#define NLTP_ROW_MAX (2u + NLTP_SHARD_BYTES)
#ifndef NLTP_TX_WINDOW
#  if NLTP_TX_ONLY
#    define NLTP_TX_WINDOW 8u               /* 8 generations (~61 KB in flight) for ultra-light MCU sensor profile */
#  else
#    define NLTP_TX_WINDOW 32u              /* generations in flight while sending (Python default) */
#  endif
#endif
#if defined(ESP8266)
#  ifndef NLTP_RX_SLOTS
#    define NLTP_RX_SLOTS 2u                /* ~20 KB full TX+RX footprint fitting ESP8266's ~45 KB heap */
#  endif
#  ifndef NLTP_RX_MAX_GENS
#    define NLTP_RX_MAX_GENS 128u           /* ~960 KB max file on ESP8266 */
#  endif
#endif
#if defined(ESP32) || defined(ESP_PLATFORM) || defined(ARDUINO)
#  ifndef NLTP_RX_SLOTS
#    define NLTP_RX_SLOTS 8u                /* ~65 KB endpoint footprint on ESP32 alongside Wi-Fi */
#  endif
#  ifndef NLTP_RX_MAX_GENS
#    define NLTP_RX_MAX_GENS 512u           /* max file: ~3.8 MB at 960 B shards, ~768 KB at 192 B (ESP-NOW v1) */
#  endif
#endif
#ifndef NLTP_RX_SLOTS
#define NLTP_RX_SLOTS 16u                   /* incomplete generations buffered while receiving (~7.8 KB each) */
#endif
#ifndef NLTP_RX_MAX_GENS
#define NLTP_RX_MAX_GENS 4096u              /* max generations per stream: ~31 MB at 960 B shards, ~6 MB at 192 B */
#endif
#define NLTP_PKT_MAX (NLTP_HEADER_BYTES + NLTP_SHARD_HDR_BYTES + NLTP_ROW_MAX)
#ifndef NLTP_DEC_MAX
#  if NLTP_TX_ONLY
#    define NLTP_DEC_MAX 240u               /* fits HELLO/HELLO_ACK/ACK/FIN/REFLEX/PING without 1 KB shard buffer */
#  else
#    define NLTP_DEC_MAX (NLTP_PKT_MAX + 64u)
#  endif
#endif
#define NLTP_META_MAX 352u
#ifndef NLTP_RX_TAKEOVER_MS
#define NLTP_RX_TAKEOVER_MS 1000u  /* silence before another conn_id may replace an unfinished receive */
#endif
#define NLTP_COLD_BURST_BYTES (32u * 1024u)
#ifndef NLTP_REFLEX_MAX_LEN
#define NLTP_REFLEX_MAX_LEN 192u   /* <= 234 B encrypted wire frame -> fits ESP-NOW v1 250 B MTU */
#endif
#ifndef NLTP_REFLEX_OUT_SLOTS
#  if NLTP_TX_ONLY
#    define NLTP_REFLEX_OUT_SLOTS 3u
#  else
#    define NLTP_REFLEX_OUT_SLOTS 8u
#  endif
#endif
#ifndef NLTP_REFLEX_IN_SLOTS
#  if NLTP_TX_ONLY
#    define NLTP_REFLEX_IN_SLOTS 3u
#  else
#    define NLTP_REFLEX_IN_SLOTS 8u
#  endif
#endif
#ifndef NLTP_REFLEX_SEEN_MAX
#  if NLTP_TX_ONLY
#    define NLTP_REFLEX_SEEN_MAX 16u
#  else
#    define NLTP_REFLEX_SEEN_MAX 32u
#  endif
#endif

typedef void (*nltp_send_fn)(void *user, const uint8_t *pkt, size_t len);
/* Fills `out` with `len` cryptographically secure random bytes (session nonces, X25519 scalars). */
typedef void (*nltp_random_fn)(void *user, uint8_t *out, size_t len);
typedef uint32_t (*nltp_now_ms_fn)(void *user);  /* monotonic milliseconds, may wrap */

/* --- sender state ------------------------------------------------------- */

typedef struct {
    uint32_t gen_id;
    uint8_t valid;
    uint8_t k;
    uint16_t row_width;
    uint8_t next_sys;
    int16_t proactive_target;   /* -1 until decided */
    uint8_t proactive_sent;
    uint8_t proactive_done;
    uint8_t pending_need;       /* degrees of freedom the receiver asked for */
    uint8_t repair_in_flight;
    uint8_t next_parity;        /* parity offset 0..191 */
    uint32_t last_tx_ms;
    uint32_t last_repair_ms;
} nltp_txgen_t;

typedef struct {
    const uint8_t *data;
    uint32_t len;
    uint32_t stream_id;
    uint32_t total_gens;
    uint32_t base;              /* first generation not yet confirmed by the receiver */
    uint8_t active, meta_acked, completed, verified, meta_sent;
    uint32_t last_meta_ms, started_ms, completed_ms;
    uint32_t peer_conn_id;
    uint8_t have_peer_cid;
    uint16_t meta_len;
    uint16_t shard_bytes;
    uint8_t meta[NLTP_META_MAX];
    nltp_txgen_t win[NLTP_TX_WINDOW];
} nltp_tx_t;

/* --- pacing / rate control (integer port of necklink.nltp._DeliveryRateController) --- */

typedef struct {
    uint32_t rate_kbps;
    int32_t tokens;
    int32_t max_burst;
    uint32_t bonus;
    uint32_t last_ms;
    uint8_t started;
} nltp_pacer_t;

typedef struct {
    uint32_t cap_kbps, rate_kbps;
    uint8_t mode;               /* 0 = startup, 1 = cruise */
    uint32_t min_rtt_ms, min_mean_rtt_ms, round_min_rtt_ms, n_rtt_samples;
    uint32_t round_rtt_sum, round_rtt_cnt;
    uint8_t startup_exit_pending, round_started, train_probe_done;
    uint32_t tracked_sid;
    uint32_t round_t0_ms, round_rx0, sent_bytes, round_sent0, prev_round_sent, prev_queue_ms;
    uint32_t best_deliv_kbps;
    uint8_t plateau_rounds;
    uint16_t random_loss_pm, random_loss_samples;
    uint32_t btlbw_rounds[10];
    uint8_t n_btlbw;
    uint32_t round_arrival_max;
    uint32_t last_deliv_kbps, last_queue_ms;
    uint32_t srtt_ms, rttvar_ms;        /* controller's own RTT smoothing, for jitter-aware queue detection */
    uint32_t rounds_done;
    uint32_t hist_t[32], hist_sent[32]; /* (time, cumulative bytes sent) at recent ACKs */
    uint8_t hist_len, hist_head;
} nltp_rate_t;

/* --- receiver state ------------------------------------------------------ */

#if !NLTP_TX_ONLY
typedef struct {
    int32_t gen_id;             /* -1 = free */
    uint8_t k, rank, sys_only;
    uint16_t row_width;
    int16_t max_seen_idx;
    uint8_t seen[26];           /* shard indices 0..207 already used */
    uint8_t pivot_of_col[NLTP_GEN_K];
    uint8_t coeff[NLTP_GEN_K][NLTP_GEN_K];
    uint8_t rows[NLTP_GEN_K][NLTP_ROW_MAX];
} nltp_rxslot_t;
#endif

typedef struct {
    uint8_t active, meta_received, completed, sha_ok;
    uint32_t peer_cid, stream_id;
    uint32_t total_bytes, total_gens;
    uint32_t raw_bytes;         /* uncompressed size when is_lz4 == 1 */
    uint8_t is_lz4;
    uint8_t ack_pending;
#if NLTP_TX_ONLY
    char filename[16];
    uint8_t *out;
    uint32_t out_cap;
#else
    char want_sha[65];
    char filename[64];
    uint8_t *out;
    uint32_t out_cap;
    uint8_t solved[NLTP_RX_MAX_GENS / 8u];
    uint32_t n_solved, base_gen;
    int32_t max_gen_seen;
    int16_t max_sys_seen;       /* in generation max_gen_seen */
    int32_t max_sys_ord;        /* monotonic systematic ordinal (gen_id * k + idx) for reordering guard */
    uint8_t reorder_wnd;        /* measured packet reordering span (0 on FIFO links) */
    uint32_t shards_rcvd, sys_shards_rcvd, bytes_rcvd;
    uint32_t arr_t0, arr_b0, arr_n0, arrival_kbps;
    uint8_t arr_started;
    uint32_t last_ack_ms, ack_echo_ts;
    uint8_t peer_gen_k;
    uint16_t peer_shard_bytes;
    uint32_t last_rx_ms;        /* time of the last accepted packet of the current stream */
    nltp_rxslot_t slots[NLTP_RX_SLOTS];
#endif
} nltp_rx_t;

/* --- encrypted session (NLTP 1.5 X25519+PSK) ----------------------------- */

/* One end of a session: keys derived from ephemeral X25519 + PSK + fresh 16-byte nonces (see nltp_core.c). */
typedef struct {
    uint8_t used, role, has_ack;    /* role: 'i' (we sent the HELLO) or 'r' */
    uint32_t peer_cid;
    uint8_t n_i[16];
    uint8_t pk_i[32];
    uint8_t k_tx[32], k_rx[32];
    uint64_t tx_seq;
    nltp_replay_window_t replay;
    uint32_t est_ms, last_rx_ms;
    uint8_t ack[NLTP_ENC_HEADER_BYTES + NLTP_HELLO_ACK_PT_BYTES + NLTP_AEAD_TAG_BYTES]; /* responder HELLO_ACK (100 B) */
} nltp_session_t;

/* --- reliable out-of-line reflex lane ------------------------------------ */

typedef struct {
    uint8_t used, sent_initial;
    uint16_t len;
    uint32_t seq;
    uint32_t next_tx_ms, interval_ms, deadline_ms;
    uint8_t data[NLTP_REFLEX_MAX_LEN];
} nltp_reflex_out_t;

typedef struct {
    uint32_t peer_cid;
    uint16_t len;
    uint8_t data[NLTP_REFLEX_MAX_LEN];
} nltp_reflex_in_t;

/* --- endpoint ------------------------------------------------------------ */

typedef struct {
    nltp_send_fn send;
    nltp_now_ms_fn now_ms;
    void *user;
    uint32_t conn_id;
    uint32_t srtt_ms, min_rtt_ms;
    uint8_t has_rtt, paced;
    uint16_t peer_loss_pm;      /* receiver-reported loss, permille EMA */
    uint16_t tx_shard_bytes;    /* runtime TX shard payload bytes (16..NLTP_SHARD_BYTES; 192 fits ESP-NOW v1 250B) */
    uint32_t next_stream_id;
    nltp_pacer_t pacer;
    nltp_rate_t rate;
    nltp_tx_t tx;
    nltp_rx_t rx;
    uint32_t pkts_tx, pkts_rx, bytes_tx, bytes_rx, crc_drops, auth_drops;
    /* NAT keepalive: when keepalive_ms > 0, nltp_poll() sends a PING after that long without sending */
    uint32_t keepalive_ms, last_tx_ms, pongs_rx;
    /* frames that passed CRC / authentication: an app may follow a peer's new address (roaming, NAT
       rebinding) only when this increased for the datagram it just fed in */
    uint32_t valid_rx;
    /* encryption (NLTP 1.5): key = long-term PSK; frames use per-session forward-secret keys from a 1-RTT
       X25519+PSK handshake. sess[0] is the live session (it seals our frames), sess[1] a candidate that
       becomes live once authenticated traffic arrives under it. */
    uint8_t encrypted;
    uint8_t key[32];
    nltp_random_fn rng;
    nltp_session_t sess[2];
    uint8_t pend, pend_n_i[16];      /* our HELLO waiting for its HELLO_ACK */
    uint8_t pend_sk_i[32], pend_pk_i[32];
    uint32_t pend_next_ms, pend_interval_ms, pend_deadline_ms;
    uint32_t hint_tx_ms, hint_rx_ms;
    uint32_t hellos_tx, sessions_up, rekeys_from_hint, legacy_drops;
    uint8_t replay_valid;            /* unused since 1.4 (kept so older firmware still compiles) */
    /* reliable reflex lane */
    uint32_t reflex_tx_seq;
    uint32_t reflex_retransmits, reflex_acks_rx, reflex_expired;
    nltp_reflex_out_t reflex_out[NLTP_REFLEX_OUT_SLOTS];
    nltp_reflex_in_t reflex_in[NLTP_REFLEX_IN_SLOTS];
    uint8_t reflex_in_head, reflex_in_count;
    uint32_t reflex_seen_cid[NLTP_REFLEX_SEEN_MAX];
    uint32_t reflex_seen_seq[NLTP_REFLEX_SEEN_MAX];
    uint8_t reflex_seen_len, reflex_seen_head;
    uint32_t reflex_acked_seq[NLTP_REFLEX_SEEN_MAX];
    uint8_t reflex_acked_len, reflex_acked_head;
    uint8_t pkt[NLTP_PKT_MAX + 64u];
    uint8_t dec[NLTP_DEC_MAX];
} nltp_endpoint_t;

#if NLTP_TX_ONLY
/* Link-time ABI guard: prevents mixing NLTP_TX_ONLY=1 callers with a default-compiled nltp_core.o */
#define nltp_init nltp_init_txonly
#endif

/* max_kbps: pacing ceiling (the controller measures the real rate below it); 0 = unpaced. */
void nltp_init(nltp_endpoint_t *ep, uint32_t conn_id, uint32_t max_kbps,
               nltp_send_fn send, nltp_now_ms_fn now_ms, void *user);
void nltp_set_rx_buffer(nltp_endpoint_t *ep, uint8_t *buf, uint32_t cap);
void nltp_set_shard_bytes(nltp_endpoint_t *ep, uint16_t shard_bytes);

/* Turns on encryption with a 32-byte key (NULL turns it off). Set it before the first packet; both
   peers need the same key. nltp_key_from_psk() derives the key exactly like Python derive_aead_key(str):
   a 43/44-char URL-safe base64 key (generate_nltp_key) is used as-is, any other passphrase is hashed. */
void nltp_set_key(nltp_endpoint_t *ep, const uint8_t key32[32]);
void nltp_key_from_psk(const char *psk, uint8_t out_key32[32]);

/* Secure random source for session nonces & ephemeral X25519 scalars. nltp_init picks one automatically on
   ESP-IDF / Arduino-ESP32 (esp_fill_random), ESP8266 (RANDOM_REG32) and Linux/macOS (/dev/urandom). */
void nltp_set_random(nltp_endpoint_t *ep, nltp_random_fn rng);

/* Starts sending `data` (kept by reference). Returns the stream id, or 0 if a send is still active. */
uint32_t nltp_send_buffer(nltp_endpoint_t *ep, const uint8_t *data, uint32_t len, const char *filename);

/* LZ4-compressed send: compresses `data[0..len-1]` into `comp_scratch[0..comp_cap-1]`. If compression
   shrinks the payload, `comp_scratch` backs the wire transfer (must remain valid until `nltp_tx_idle(ep)`);
   if incompressible or `comp_scratch` is too small, transparently sends raw `data[0..len-1]`. */
uint32_t nltp_send_buffer_lz4(nltp_endpoint_t *ep, const uint8_t *data, uint32_t len,
                              uint8_t *comp_scratch, uint32_t comp_cap, const char *filename);

/* Reliable out-of-line reflex lane: bypasses bulk stream queue & pacer, retransmitted until PT_REFLEX_ACK. */
uint32_t nltp_send_reflex(nltp_endpoint_t *ep, const uint8_t *msg, uint16_t len);
int nltp_reflex_acked(const nltp_endpoint_t *ep, uint32_t seq);
uint32_t nltp_reflex_pending(const nltp_endpoint_t *ep);
int nltp_pop_reflex(nltp_endpoint_t *ep, uint8_t *out_buf, uint16_t cap, uint16_t *out_len, uint32_t *out_peer_cid);

void nltp_on_datagram(nltp_endpoint_t *ep, const uint8_t *raw, size_t len);
void nltp_poll(nltp_endpoint_t *ep);

/* Sends a PING to the current peer; its PONG increments ep->pongs_rx and gives an RTT sample.
   Used for UDP hole punching (both sides ping each other's public address) and keepalives. */
void nltp_send_ping(nltp_endpoint_t *ep);

/* Status */
static inline int nltp_tx_done(const nltp_endpoint_t *ep) { return ep->tx.completed; }
static inline int nltp_tx_idle(const nltp_endpoint_t *ep) { return !ep->tx.active || ep->tx.completed; }
static inline int nltp_tx_verified(const nltp_endpoint_t *ep) { return ep->tx.verified; }
static inline int nltp_rx_done(const nltp_endpoint_t *ep) { return ep->rx.completed; }
static inline int nltp_rx_sha_ok(const nltp_endpoint_t *ep) { return ep->rx.sha_ok; }
static inline uint32_t nltp_rx_len(const nltp_endpoint_t *ep) { return ep->rx.total_bytes; }

#ifdef __cplusplus
}
#endif

#endif /* NLTP_CORE_H */
