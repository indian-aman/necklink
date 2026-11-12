/*
 * nltp_selftest.c — in-process tests for nltp_core: two endpoints over a simulated link that can drop,
 * tamper with, replay or mis-key packets. Exit code 0 = all tests passed.
 *
 *   gcc -O2 -std=c99 -I csrc -o nltp_selftest csrc/tools/nltp_selftest.c csrc/nltp_core.c csrc/nltp_sha256.c
 *
 * Expected keys come from Python necklink.internet.derive_aead_key for the same inputs.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "nltp_core.h"

#define QCAP 4096
#define PMAX (NLTP_PKT_MAX + 64)

typedef struct {
    uint8_t data[QCAP][PMAX];
    uint16_t len[QCAP];
    uint32_t due[QCAP];
    int head, tail;
} queue_t;

typedef struct {
    queue_t *q;           /* where this endpoint's packets go */
    int drop_pct, tamper_pct, replay_every;
    uint32_t sent;
    uint16_t max_len;
} link_t;

static uint32_t g_clock = 1000;
static queue_t q_ab, q_ba;
static nltp_endpoint_t ep_a, ep_b;
static link_t link_a, link_b;
static uint8_t file_tx[200 * 1024];
static uint8_t file_rx[256 * 1024];
static int failures = 0;

/* What an eavesdropper on the A -> B path records (tests 15-16). */
#define REC_MAX 600
static uint8_t rec[REC_MAX][PMAX];
static uint16_t rec_len[REC_MAX];
static int rec_n, rec_on;

static uint32_t clock_cb(void *user) {
    (void)user;
    return g_clock;
}

static void push(queue_t *q, const uint8_t *pkt, size_t len, uint32_t delay) {
    int next = (q->tail + 1) % QCAP;
    if (next == q->head || len > PMAX) return;  /* full: behaves like a dropped packet */
    memcpy(q->data[q->tail], pkt, len);
    q->len[q->tail] = (uint16_t)len;
    q->due[q->tail] = g_clock + delay;
    q->tail = next;
}

static void send_cb(void *user, const uint8_t *pkt, size_t len) {
    link_t *l = (link_t *)user;
    uint8_t copy[PMAX];
    l->sent++;
    if (len > l->max_len) l->max_len = (uint16_t)len;
    if (rec_on && l == &link_a && rec_n < REC_MAX && len <= PMAX) {
        memcpy(rec[rec_n], pkt, len);
        rec_len[rec_n++] = (uint16_t)len;
    }
    if (l->drop_pct && rand() % 100 < l->drop_pct) return;
    memcpy(copy, pkt, len);
    if (l->tamper_pct && rand() % 100 < l->tamper_pct) copy[len / 2] ^= 0x01;  /* one flipped bit */
    push(l->q, copy, len, 10);
    if (l->replay_every && l->sent % (uint32_t)l->replay_every == 0) push(l->q, pkt, len, 30);  /* replay */
}

static void deliver(queue_t *q, nltp_endpoint_t *to) {
    while (q->head != q->tail && (int32_t)(g_clock - q->due[q->head]) >= 0) {
        nltp_on_datagram(to, q->data[q->head], q->len[q->head]);
        q->head = (q->head + 1) % QCAP;
    }
}

/* Runs one A -> B transfer; returns 1 if B verified the file within `max_ms` simulated ms. */
static int run_transfer(const uint8_t *key_a, const uint8_t *key_b, int drop, int tamper, int replay, uint32_t max_ms) {
    uint32_t t;
    memset(&q_ab, 0, sizeof(q_ab));
    memset(&q_ba, 0, sizeof(q_ba));
    memset(&link_a, 0, sizeof(link_a));
    memset(&link_b, 0, sizeof(link_b));
    link_a.q = &q_ab;
    link_b.q = &q_ba;
    link_a.drop_pct = drop;
    link_a.tamper_pct = tamper;
    link_a.replay_every = replay;
    nltp_init(&ep_a, 0x1111, 20000, send_cb, clock_cb, &link_a);
    nltp_init(&ep_b, 0x2222, 20000, send_cb, clock_cb, &link_b);
    nltp_set_key(&ep_a, key_a);
    nltp_set_key(&ep_b, key_b);
    nltp_set_rx_buffer(&ep_b, file_rx, sizeof(file_rx));
    nltp_send_buffer(&ep_a, file_tx, sizeof(file_tx), "selftest.bin");
    for (t = 0; t < max_ms; ++t) {
        g_clock++;
        deliver(&q_ab, &ep_b);
        deliver(&q_ba, &ep_a);
        nltp_poll(&ep_a);
        nltp_poll(&ep_b);
        if (nltp_rx_done(&ep_b) && nltp_tx_done(&ep_a)) break;
    }
    return nltp_rx_done(&ep_b) && nltp_rx_sha_ok(&ep_b) && nltp_tx_verified(&ep_a) &&
           memcmp(file_rx, file_tx, sizeof(file_tx)) == 0;
}

static void check(int cond, const char *name, const char *detail) {
    printf("%-58s %s  %s\n", name, cond ? "PASS" : "FAIL", detail);
    if (!cond) failures++;
}

static void hex_of(const uint8_t *k, char *out) {
    int i;
    for (i = 0; i < 32; ++i) sprintf(out + 2 * i, "%02x", k[i]);
}

int main(void) {
    uint8_t key[32], other[32], derived[32];
    char hex[65], detail[160];
    uint32_t i;
    int ok;

    srand(1234);
    for (i = 0; i < sizeof(file_tx); ++i) file_tx[i] = (uint8_t)(rand() & 0xFF);
    nltp_key_from_psk("correct horse battery staple", key);
    nltp_key_from_psk("a different passphrase", other);

    /* key derivation must match Python derive_aead_key */
    nltp_key_from_psk("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", derived);
    hex_of(derived, hex);
    check(strcmp(hex, "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f") == 0,
          "key: base64 key (generate_nltp_key format) used as-is", hex);
    nltp_key_from_psk("  boat radio test \n", derived);
    hex_of(derived, hex);
    check(strcmp(hex, "0d978f36f81f07b3fedabbaf5fd5fafdf6b9a31f78fda65a04d534ceb540e6d0") == 0,
          "key: passphrase trimmed and hashed like Python", hex);

    ok = run_transfer(NULL, NULL, 0, 0, 0, 20000);
    check(ok, "plaintext transfer, 200 KB", "");

    ok = run_transfer(key, key, 0, 0, 0, 20000);
    snprintf(detail, sizeof(detail), "auth_drops=%u", (unsigned)ep_b.auth_drops);
    check(ok && ep_b.auth_drops == 0, "encrypted transfer, 200 KB", detail);

    ok = run_transfer(key, key, 10, 0, 0, 60000);
    check(ok, "encrypted transfer with 10% loss", "");

    ok = run_transfer(key, key, 0, 5, 0, 60000);
    snprintf(detail, sizeof(detail), "tampered packets rejected=%u", (unsigned)ep_b.auth_drops);
    check(ok && ep_b.auth_drops > 0, "tampering: 5% bit-flipped packets rejected, file intact", detail);

    ok = run_transfer(key, key, 0, 0, 7, 60000);
    snprintf(detail, sizeof(detail), "replays rejected=%u", (unsigned)ep_b.auth_drops);
    check(ok && ep_b.auth_drops > 0, "replay: every 7th packet re-sent, all rejected", detail);

    ok = run_transfer(key, other, 0, 0, 0, 5000);
    snprintf(detail, sizeof(detail), "accepted=0 rejected=%u", (unsigned)ep_b.auth_drops);
    check(!ok && !nltp_rx_done(&ep_b) && ep_b.auth_drops > 0 && ep_b.rx.shards_rcvd == 0,
          "wrong key: nothing accepted, transfer does not complete", detail);

    ok = run_transfer(NULL, key, 0, 0, 0, 5000);
    snprintf(detail, sizeof(detail), "rejected=%u", (unsigned)ep_b.auth_drops);
    check(!ok && ep_b.rx.shards_rcvd == 0 && ep_b.auth_drops > 0,
          "downgrade: unencrypted sender refused by encrypted receiver", detail);

    /* regression: cancelled call (sid=1 META lost, DATA_SHARD arrived -> rx incomplete) replaced by sid=2 */
    {
        uint8_t call1[4] = {0xCA, 0x11, 0x00, 0x01};
        uint8_t call2[4] = {0xCA, 0x11, 0x00, 0x02};
        uint8_t stale_pkt[PMAX];
        uint16_t stale_len = 0;
        uint32_t t;
        memset(&q_ab, 0, sizeof(q_ab));
        memset(&q_ba, 0, sizeof(q_ba));
        memset(&link_a, 0, sizeof(link_a));
        memset(&link_b, 0, sizeof(link_b));
        link_a.q = &q_ab;
        link_b.q = &q_ba;
        nltp_init(&ep_a, 0x3333, 2000, send_cb, clock_cb, &link_a);
        nltp_init(&ep_b, 0x4444, 2000, send_cb, clock_cb, &link_b);
        nltp_set_key(&ep_a, key);
        nltp_set_key(&ep_b, key);
        nltp_set_rx_buffer(&ep_b, file_rx, sizeof(file_rx));
        memset(file_rx, 0, 16);

        /* sid=1 emits [0]=STREAM_META (~215B, dropped) and [1]=DATA_SHARD (54B, delivered) */
        nltp_send_buffer(&ep_a, call1, sizeof(call1), "call");
        nltp_on_datagram(&ep_b, q_ab.data[1], q_ab.len[1]);
        /* capture a fresh-seq sid=1 retry packet to test delayed stale-stream rejection later */
        q_ab.head = q_ab.tail = 0;
        g_clock += 400;
        nltp_poll(&ep_a);
        if (q_ab.tail > 1) {
            stale_len = q_ab.len[1];
            memcpy(stale_pkt, q_ab.data[1], stale_len);
        }
        q_ab.head = q_ab.tail = 0;

        /* cancel sid=1 and immediately send sid=2 while ep_b.rx.active==1 && ep_b.rx.completed==0 */
        ep_a.tx.completed = 1;
        ep_a.tx.verified = 0;
        nltp_send_buffer(&ep_a, call2, sizeof(call2), "call");
        /* deliver sid=2 DATA_SHARD first, then inject delayed sid=1 shard mid-stream, then sid=2 META */
        nltp_on_datagram(&ep_b, q_ab.data[1], q_ab.len[1]);
        if (stale_len) nltp_on_datagram(&ep_b, stale_pkt, stale_len);
        nltp_on_datagram(&ep_b, q_ab.data[0], q_ab.len[0]);
        q_ab.head = q_ab.tail = 0;
        for (t = 0; t < 2000; ++t) {
            g_clock++;
            deliver(&q_ab, &ep_b);
            deliver(&q_ba, &ep_a);
            nltp_poll(&ep_a);
            nltp_poll(&ep_b);
            if (nltp_rx_done(&ep_b) && nltp_tx_done(&ep_a)) break;
        }
        ok = nltp_rx_done(&ep_b) && nltp_rx_sha_ok(&ep_b) && nltp_tx_verified(&ep_a) &&
             memcmp(file_rx, call2, sizeof(call2)) == 0;
        snprintf(detail, sizeof(detail), "sid=%u verified=%d", (unsigned)ep_b.rx.stream_id, nltp_tx_verified(&ep_a));
        check(ok, "cancelled stream: sid=2 resets incomplete sid=1 cleanly", detail);
    }

    /* regression: deep-sleep wake with new conn_id while bridge rx slot is incomplete */
    {
        uint8_t call_w1[4] = {0x11, 0x22, 0x33, 0x44};
        uint8_t call_w2[4] = {0xDE, 0xAD, 0xBE, 0xEF};
        uint32_t t;
        memset(&q_ab, 0, sizeof(q_ab));
        memset(&q_ba, 0, sizeof(q_ba));
        memset(&link_a, 0, sizeof(link_a));
        memset(&link_b, 0, sizeof(link_b));
        link_a.q = &q_ab;
        link_b.q = &q_ba;
        nltp_init(&ep_a, 0x5555, 2000, send_cb, clock_cb, &link_a);
        nltp_init(&ep_b, 0x6666, 2000, send_cb, clock_cb, &link_b);
        nltp_set_key(&ep_a, key);
        nltp_set_key(&ep_b, key);
        nltp_set_rx_buffer(&ep_b, file_rx, sizeof(file_rx));
        memset(file_rx, 0, 16);

        /* wake 1: only 54B DATA_SHARD reaches bridge; STREAM_META lost before sleep timeout */
        nltp_send_buffer(&ep_a, call_w1, sizeof(call_w1), "wake1");
        nltp_on_datagram(&ep_b, q_ab.data[1], q_ab.len[1]);
        q_ab.head = q_ab.tail = 0;
        q_ba.head = q_ba.tail = 0;

        /* wake 2 within 45s: fresh nltp_init() with new random conn_id, sid resets to 1, tx_seq resets to 0 */
        g_clock += 10000;
        nltp_init(&ep_a, 0x7777, 2000, send_cb, clock_cb, &link_a);
        nltp_set_key(&ep_a, key);
        nltp_send_buffer(&ep_a, call_w2, sizeof(call_w2), "wake2");
        for (t = 0; t < 2000; ++t) {
            g_clock++;
            deliver(&q_ab, &ep_b);
            deliver(&q_ba, &ep_a);
            nltp_poll(&ep_a);
            nltp_poll(&ep_b);
            if (nltp_rx_done(&ep_b) && nltp_tx_done(&ep_a)) break;
        }
        ok = nltp_rx_done(&ep_b) && nltp_rx_sha_ok(&ep_b) && nltp_tx_verified(&ep_a) &&
             memcmp(file_rx, call_w2, sizeof(call_w2)) == 0;
        snprintf(detail, sizeof(detail), "peer_cid=0x%x verified=%d", (unsigned)ep_b.rx.peer_cid, nltp_tx_verified(&ep_a));
        check(ok, "deep-sleep re-wake: new conn_id resets incomplete bridge rx", detail);
    }

    /* 12. ESP-NOW v1 (192B shard -> <=250B frames) to ESP-NOW v2 (960B) receiver under 15% loss */
    {
        uint32_t v1_len = 32u * 1024u + 73u;  /* 22 generations with partial final generation */
        int t, ok;
        memset(&link_a, 0, sizeof(link_a));
        memset(&link_b, 0, sizeof(link_b));
        link_a.q = &q_ab;
        link_b.q = &q_ba;
        link_a.drop_pct = 15;
        q_ab.head = q_ab.tail = 0;
        q_ba.head = q_ba.tail = 0;

        nltp_init(&ep_a, 0xA101, 20000, send_cb, clock_cb, &link_a);
        nltp_set_shard_bytes(&ep_a, 192);  /* ESP-NOW v1 mode: 192B shard -> 242B encrypted L2 frame */
        nltp_set_key(&ep_a, key);

        nltp_init(&ep_b, 0xB202, 20000, send_cb, clock_cb, &link_b);
        /* ep_b stays at default NLTP_SHARD_BYTES (960B = ESP-NOW v2) and auto-infers 192B! */
        nltp_set_key(&ep_b, key);
        nltp_set_rx_buffer(&ep_b, file_rx, sizeof(file_rx));
        memset(file_rx, 0, v1_len);

        nltp_send_buffer(&ep_a, file_tx, v1_len, "v1_to_v2.bin");
        for (t = 0; t < 25000; ++t) {
            g_clock++;
            deliver(&q_ab, &ep_b);
            deliver(&q_ba, &ep_a);
            nltp_poll(&ep_a);
            nltp_poll(&ep_b);
            if (nltp_rx_done(&ep_b) && nltp_tx_done(&ep_a)) break;
        }
        ok = nltp_rx_done(&ep_b) && nltp_rx_sha_ok(&ep_b) && nltp_tx_verified(&ep_a) &&
             link_a.max_len <= 250u && memcmp(file_rx, file_tx, v1_len) == 0;
        snprintf(detail, sizeof(detail), "max_pkt=%uB (<=250B) gens=%u sha_ok=%d",
                 (unsigned)link_a.max_len, (unsigned)ep_a.tx.total_gens, nltp_rx_sha_ok(&ep_b));
        check(ok, "esp-now v1 (192B shard <=250B) -> v2 (960B) rx under 15% loss", detail);
    }

    /* 13. v1.2.4 regression: ONE forged plaintext packet with another conn_id must not kill a transfer.
     *     (v1.2.1-v1.2.3 reset the receive stream on any new conn_id; the sender could never repair it.) */
    {
        uint32_t t;
        int ok, injected = 0;
        memset(&q_ab, 0, sizeof(q_ab));
        memset(&q_ba, 0, sizeof(q_ba));
        memset(&link_a, 0, sizeof(link_a));
        memset(&link_b, 0, sizeof(link_b));
        link_a.q = &q_ab;
        link_b.q = &q_ba;
        nltp_init(&ep_a, 0x1313, 20000, send_cb, clock_cb, &link_a);
        nltp_init(&ep_b, 0x3131, 20000, send_cb, clock_cb, &link_b);
        nltp_set_rx_buffer(&ep_b, file_rx, sizeof(file_rx));
        memset(file_rx, 0, sizeof(file_tx));
        nltp_send_buffer(&ep_a, file_tx, sizeof(file_tx), "victim.bin");
        for (t = 0; t < 20000; ++t) {
            g_clock++;
            if (!injected && q_ab.head != q_ab.tail && ep_b.rx.n_solved >= 2u) {
                uint8_t pkt[PMAX];
                uint16_t n = q_ab.len[q_ab.head];
                uint32_t crc;
                memcpy(pkt, q_ab.data[q_ab.head], n);
                nltp_write_u32_le(pkt + 4, 0xBAD0BAD1u);           /* attacker's conn_id */
                nltp_write_u32_le(pkt + 8, 0u);
                crc = nltp_crc32_ieee(pkt, NLTP_HEADER_BYTES, 0u);  /* plaintext CRC is unkeyed */
                crc = nltp_crc32_ieee(pkt + NLTP_HEADER_BYTES, n - NLTP_HEADER_BYTES, crc);
                nltp_write_u32_le(pkt + 8, crc);
                nltp_on_datagram(&ep_b, pkt, n);
                injected = 1;
            }
            deliver(&q_ab, &ep_b);
            deliver(&q_ba, &ep_a);
            nltp_poll(&ep_a);
            nltp_poll(&ep_b);
            if (nltp_rx_done(&ep_b) && nltp_tx_done(&ep_a)) break;
        }
        ok = injected && nltp_rx_done(&ep_b) && nltp_rx_sha_ok(&ep_b) && ep_b.rx.peer_cid == ep_a.conn_id &&
             memcmp(file_rx, file_tx, sizeof(file_tx)) == 0;
        snprintf(detail, sizeof(detail), "forged=%d peer_cid=0x%x sha_ok=%d", injected,
                 (unsigned)ep_b.rx.peer_cid, nltp_rx_sha_ok(&ep_b));
        check(ok, "forged plaintext packet mid-transfer is ignored", detail);
    }

    /* 14. v1.2.4 regression: ONE delayed (or replayed) packet from the sender's previous session -- old
     *     conn_id, valid AEAD tag -- arriving mid-transfer must not kill the new transfer. */
    {
        uint32_t t;
        int ok, injected = 0;
        uint8_t old_pkt[PMAX];
        uint16_t old_n;
        memset(&q_ab, 0, sizeof(q_ab));
        memset(&q_ba, 0, sizeof(q_ba));
        memset(&link_a, 0, sizeof(link_a));
        memset(&link_b, 0, sizeof(link_b));
        link_a.q = &q_ab;
        link_b.q = &q_ba;
        nltp_init(&ep_a, 0x4141, 20000, send_cb, clock_cb, &link_a);
        nltp_init(&ep_b, 0x1414, 20000, send_cb, clock_cb, &link_b);
        nltp_set_key(&ep_a, key);
        nltp_set_key(&ep_b, key);
        nltp_set_rx_buffer(&ep_b, file_rx, sizeof(file_rx));
        nltp_send_buffer(&ep_a, file_tx, sizeof(file_tx), "session1.bin");
        old_n = q_ab.len[q_ab.head];                       /* keep a copy of session 1's first packet */
        memcpy(old_pkt, q_ab.data[q_ab.head], old_n);
        deliver(&q_ab, &ep_b);
        memset(&q_ab, 0, sizeof(q_ab));
        memset(&q_ba, 0, sizeof(q_ba));

        g_clock += 5000;                                   /* device sleeps, wakes with a new conn_id */
        nltp_init(&ep_a, 0x4242, 20000, send_cb, clock_cb, &link_a);
        nltp_set_key(&ep_a, key);
        memset(file_rx, 0, sizeof(file_tx));
        nltp_send_buffer(&ep_a, file_tx, sizeof(file_tx), "session2.bin");
        for (t = 0; t < 20000; ++t) {
            g_clock++;
            if (t == 30) {
                nltp_on_datagram(&ep_b, old_pkt, old_n);
                injected = 1;
            }
            deliver(&q_ab, &ep_b);
            deliver(&q_ba, &ep_a);
            nltp_poll(&ep_a);
            nltp_poll(&ep_b);
            if (nltp_rx_done(&ep_b) && nltp_tx_done(&ep_a)) break;
        }
        ok = injected && nltp_rx_done(&ep_b) && nltp_rx_sha_ok(&ep_b) && nltp_tx_verified(&ep_a) &&
             ep_b.rx.peer_cid == ep_a.conn_id && memcmp(file_rx, file_tx, sizeof(file_tx)) == 0;
        snprintf(detail, sizeof(detail), "peer_cid=0x%x sha_ok=%d", (unsigned)ep_b.rx.peer_cid,
                 nltp_rx_sha_ok(&ep_b));
        check(ok, "stale packet from previous session (valid AEAD) ignored", detail);
    }

    /* 15. NLTP 1.4: a recorded session replayed into a restarted receiver delivers nothing. In 1.3.x the
       receiver forgot its replay window on restart, so the same recording was accepted a second time. */
    {
        int r, done;
        rec_n = 0;
        rec_on = 1;
        done = run_transfer(key, key, 0, 0, 0, 20000);
        rec_on = 0;
        nltp_init(&ep_b, 0x2222, 20000, send_cb, clock_cb, &link_b);   /* receiver restarts */
        nltp_set_key(&ep_b, key);
        nltp_set_rx_buffer(&ep_b, file_rx, sizeof(file_rx));
        memset(file_rx, 0, sizeof(file_tx));
        for (r = 0; r < rec_n; ++r) {
            g_clock++;
            nltp_on_datagram(&ep_b, rec[r], rec_len[r]);
            nltp_poll(&ep_b);
        }
        ok = done && rec_n > 100 && ep_b.rx.shards_rcvd == 0u && !nltp_rx_done(&ep_b);
        snprintf(detail, sizeof(detail), "recorded=%d accepted_shards=%u", rec_n, (unsigned)ep_b.rx.shards_rcvd);
        check(ok, "1.4: recorded session replayed after restart: rejected", detail);
    }

    /* 16. NLTP 1.4: two sessions with the same conn_id and counter (a reboot) use different keys. In 1.3.x
       both sealed with the long-term key and nonce conn_id||counter, so their keystreams were identical. */
    {
        uint8_t first[2][PMAX];
        uint16_t first_len[2] = {0, 0};
        int run, r;
        for (run = 0; run < 2; ++run) {
            rec_n = 0;
            rec_on = 1;
            ok = run_transfer(key, key, 0, 0, 0, 20000);
            rec_on = 0;
            for (r = 0; r < rec_n; ++r) {
                if ((rec[r][2] & 0x0Fu) == NLTP_PT_DATA_SHARD) {
                    memcpy(first[run], rec[r], rec_len[r]);
                    first_len[run] = rec_len[r];
                    break;
                }
            }
        }
        ok = ok && first_len[0] > 40u && first_len[0] == first_len[1] &&
             memcmp(first[0], first[1], NLTP_ENC_HEADER_BYTES - 4u) == 0 &&             /* same conn_id + counter */
             memcmp(first[0] + NLTP_ENC_HEADER_BYTES, first[1] + NLTP_ENC_HEADER_BYTES, 32u) != 0;
        check(ok, "1.5: same conn_id + counter after reboot: new keys", "first data frames differ");
    }

    /* 17. NLTP 1.5: 512-packet anti-replay window accepts out-of-order up to 511 packets back and rejects 512+ */
    {
        nltp_replay_window_t rw;
        nltp_replay_init(&rw);
        ok = nltp_replay_accept(&rw, 513u) == 1 &&
             nltp_replay_accept(&rw, 2u) == 1 &&       /* 513 - 2 = 511 < 512: valid reordered packet */
             nltp_replay_accept(&rw, 2u) == 0 &&       /* duplicate of seq=2 rejected */
             nltp_replay_accept(&rw, 1u) == 0 &&       /* 513 - 1 = 512 >= 512: expired below window */
             nltp_replay_accept(&rw, 300u) == 1 &&
             nltp_replay_accept(&rw, 300u) == 0;
        check(ok, "1.5: 512-packet anti-replay window (reordering <=511 ok)", "513->2 ok, 513->1 rejected");
    }

    /* 18. NLTP 1.5: RFC 7748 s6.1 X25519 test vector + low-order all-zero shared secret rejection */
    {
        static const uint8_t sk_a[32] = {
            0x77, 0x07, 0x6d, 0x0a, 0x73, 0x18, 0xa5, 0x7d, 0x3c, 0x16, 0xc1, 0x72, 0x51, 0xb2, 0x66, 0x45,
            0xdf, 0x4c, 0x2f, 0x87, 0xeb, 0xc0, 0x99, 0x2a, 0xb1, 0x77, 0xfb, 0xa5, 0x1d, 0xb9, 0x2c, 0x2a
        };
        static const uint8_t sk_b[32] = {
            0x5d, 0xab, 0x08, 0x7e, 0x62, 0x4a, 0x8a, 0x4b, 0x79, 0xe1, 0x7f, 0x8b, 0x83, 0x80, 0x0e, 0xe6,
            0x6f, 0x3b, 0xb1, 0x29, 0x26, 0x18, 0xb6, 0xfd, 0x1c, 0x2f, 0x8b, 0x27, 0xff, 0x88, 0xe0, 0xeb
        };
        static const uint8_t want_ss[32] = {
            0x4a, 0x5d, 0x9d, 0x5b, 0xa4, 0xce, 0x2d, 0xe1, 0x72, 0x8e, 0x3b, 0xf4, 0x80, 0x35, 0x0f, 0x25,
            0xe0, 0x7e, 0x21, 0xc9, 0x47, 0xd1, 0x9e, 0x33, 0x76, 0xf0, 0x9b, 0x3c, 0x1e, 0x16, 0x17, 0x42
        };
        uint8_t pk_a[32], pk_b[32], ss_ab[32], ss_ba[32], zero_pt[32], bad_out[32];
        int ok_ab, ok_ba, ok_zero;
        memset(zero_pt, 0, sizeof(zero_pt));
        nltp_x25519_public_key(pk_a, sk_a);
        nltp_x25519_public_key(pk_b, sk_b);
        ok_ab = nltp_x25519_scalarmult(ss_ab, sk_a, pk_b);
        ok_ba = nltp_x25519_scalarmult(ss_ba, sk_b, pk_a);
        ok_zero = nltp_x25519_scalarmult(bad_out, sk_a, zero_pt);
        hex_of(ss_ab, hex);
        ok = ok_ab == 1 && ok_ba == 1 && ok_zero == 0 &&
             memcmp(ss_ab, want_ss, 32) == 0 && memcmp(ss_ba, want_ss, 32) == 0;
        check(ok, "1.5: RFC 7748 X25519 DH + low-order point rejection", hex);
    }

    /* 19. NLTP 1.5 Forward Secrecy: ephemeral private key wiped on handshake; compromised PSK cannot decrypt */
    {
        uint8_t zero32[32], pt_hello[128], pt_data[PMAX];
        size_t pt_hlen = 0, pt_dlen = 0;
        uint8_t t_out, f_out;
        uint32_t c_out, ts_out;
        int r, got_hello = 0, got_data = 0, psk_opened_data = 0;
        memset(zero32, 0, sizeof(zero32));
        rec_n = 0;
        rec_on = 1;
        ok = run_transfer(key, key, 0, 0, 0, 20000);
        rec_on = 0;
        for (r = 0; r < rec_n; ++r) {
            uint8_t ptype = rec[r][2] & 0x0Fu;
            if (!got_hello && ptype == NLTP_PT_HELLO) {
                got_hello = nltp_unpack_encrypted_frame_v(rec[r], rec_len[r], key, NLTP_SESSION_VERSION, 0,
                                                          &t_out, &f_out, &c_out, &ts_out, pt_hello, &pt_hlen) &&
                            pt_hlen == NLTP_HELLO_PT_BYTES;
            } else if (!got_data && ptype == NLTP_PT_DATA_SHARD) {
                got_data = 1;
                psk_opened_data = nltp_unpack_encrypted_frame_v(rec[r], rec_len[r], key, NLTP_SESSION_VERSION, 0,
                                                                &t_out, &f_out, &c_out, &ts_out, pt_data, &pt_dlen);
            }
        }
        ok = ok && got_hello && got_data && !psk_opened_data &&
             memcmp(ep_a.pend_sk_i, zero32, 32) == 0;
        snprintf(detail, sizeof(detail), "hello_pt=%uB sk_i_wiped=%d psk_open=%d",
                 (unsigned)pt_hlen, memcmp(ep_a.pend_sk_i, zero32, 32) == 0, psk_opened_data);
        check(ok, "1.5: PFS wipes ephemeral sk_i; compromised PSK fails on data", detail);
    }

    /* 20. NLTP 1.5 Reliable Reflex Lane + nltp_tx_idle: cold encrypted start under 20% loss + deduplication */
    {
        static const char *cmd1 = "ESTOP_STATION_01";
        static const char *cmd2 = "CALIBRATE_ZERO_SPAN";
        uint8_t out_buf[NLTP_REFLEX_MAX_LEN];
        uint16_t out_len = 0;
        uint32_t out_cid = 0, seq1, seq2, t;
        int got_cnt = 0, idle_before, idle_during, idle_after;
        memset(&q_ab, 0, sizeof(q_ab));
        memset(&q_ba, 0, sizeof(q_ba));
        memset(&link_a, 0, sizeof(link_a));
        memset(&link_b, 0, sizeof(link_b));
        link_a.q = &q_ab;
        link_b.q = &q_ba;
        link_a.drop_pct = 20;
        link_b.drop_pct = 20;
        nltp_init(&ep_a, 0x9001, 20000, send_cb, clock_cb, &link_a);
        nltp_init(&ep_b, 0x9002, 20000, send_cb, clock_cb, &link_b);
        nltp_set_key(&ep_a, key);
        nltp_set_key(&ep_b, key);
        nltp_set_rx_buffer(&ep_b, file_rx, sizeof(file_rx));

        idle_before = nltp_tx_idle(&ep_a) && !nltp_tx_done(&ep_a);
        seq1 = nltp_send_reflex(&ep_a, (const uint8_t *)cmd1, (uint16_t)strlen(cmd1));
        seq2 = nltp_send_reflex(&ep_a, (const uint8_t *)cmd2, (uint16_t)strlen(cmd2));
        nltp_send_buffer(&ep_a, file_tx, 4096u, "concurrent.bin");
        idle_during = !nltp_tx_idle(&ep_a);

        ok = 1;
        for (t = 0; t < 15000; ++t) {
            g_clock++;
            deliver(&q_ab, &ep_b);
            deliver(&q_ba, &ep_a);
            nltp_poll(&ep_a);
            nltp_poll(&ep_b);
            while (nltp_pop_reflex(&ep_b, out_buf, sizeof(out_buf), &out_len, &out_cid)) {
                got_cnt++;
                if (got_cnt == 1) ok = ok && out_len == strlen(cmd1) && memcmp(out_buf, cmd1, out_len) == 0;
                if (got_cnt == 2) ok = ok && out_len == strlen(cmd2) && memcmp(out_buf, cmd2, out_len) == 0;
            }
            if (nltp_reflex_acked(&ep_a, seq1) && nltp_reflex_acked(&ep_a, seq2) &&
                nltp_tx_done(&ep_a) && nltp_rx_done(&ep_b)) break;
        }
        idle_after = nltp_tx_idle(&ep_a) && nltp_tx_done(&ep_a);
        ok = ok && got_cnt == 2 && nltp_reflex_acked(&ep_a, seq1) && nltp_reflex_acked(&ep_a, seq2) &&
             nltp_reflex_pending(&ep_a) == 0u && idle_before && idle_during && idle_after;
        snprintf(detail, sizeof(detail), "got=%d acks=%u idle=%d/%d/%d",
                 got_cnt, (unsigned)ep_a.reflex_acks_rx, idle_before, !idle_during, idle_after);
        check(ok, "1.5: reliable reflex under 20% loss + nltp_tx_idle states", detail);
    }

    /* 21. NLTP 1.6 Zero-Dependency LZ4 Block Codec: round-trip, zero-heap inplace, incompressible & fuzz bounds */
    {
        static uint8_t sensor_csv[32768], comp_buf[32768], dec_buf[33280];
        uint32_t htab[1024];
        size_t pos = 0, clen, incomp_len;
        int row = 0, ok_dec, ok_inp, ok_bad1, ok_bad2;
        while (pos + 128 < sizeof(sensor_csv)) {
            int w = snprintf((char *)sensor_csv + pos, sizeof(sensor_csv) - pos,
                             "2026-11-12T03:%02d:%02dZ,AQMS-ST01,PM25=%.1f,PM10=%.1f,NO2=%.2f,SO2=%.2f,CO=%.2f,STATUS=OK\n",
                             (row / 60) % 60, row % 60, 24.3 + (row % 7) * 0.4, 51.2 + (row % 11) * 0.5,
                             18.25, 6.10, 0.72);
            if (w <= 0) break;
            pos += (size_t)w;
            row++;
        }
        clen = nltp_lz4_compress(sensor_csv, pos, comp_buf, sizeof(comp_buf), htab, 10u);
        ok_dec = clen > 0u && clen < pos / 3u && nltp_lz4_decompress(comp_buf, clen, dec_buf, pos) &&
                 memcmp(dec_buf, sensor_csv, pos) == 0;
        memcpy(dec_buf, comp_buf, clen);
        ok_inp = nltp_lz4_decompress_inplace(dec_buf, sizeof(dec_buf), clen, pos) &&
                 memcmp(dec_buf, sensor_csv, pos) == 0;
        /* Incompressible pseudo-random input (`file_tx`) must return 0 (auto-bypass) */
        incomp_len = nltp_lz4_compress(file_tx, sizeof(file_tx), comp_buf, sizeof(comp_buf), htab, 10u);
        /* Corrupted offset (0) and truncated length must fail safely */
        comp_buf[clen / 2u] ^= 0xFFu;
        ok_bad1 = !nltp_lz4_decompress(comp_buf, clen - 3u, dec_buf, pos);
        dec_buf[0] = 0x1Fu; dec_buf[1] = 'A'; dec_buf[2] = 0u; dec_buf[3] = 0u; /* invalid zero offset */
        ok_bad2 = !nltp_lz4_decompress(dec_buf, 4u, comp_buf, 20u);
        ok = ok_dec && ok_inp && incomp_len == 0u && ok_bad1 && ok_bad2;
        snprintf(detail, sizeof(detail), "csv=%uB->%uB (%.2fx) rand_bypass=%d fuzz_safe=%d",
                 (unsigned)pos, (unsigned)clen, clen ? (double)pos / (double)clen : 0.0,
                 incomp_len == 0u, ok_bad1 && ok_bad2);
        check(ok, "1.6: LZ4 block compress/decompress, zero-heap inplace & fuzz guards", detail);
    }

    /* 22. NLTP 1.6 Encrypted X25519 + LZ4 E2E Transfer under 15% Loss (compressible + incompressible fallback) */
    {
        static uint8_t log_tx[28672], comp_scratch[28672];
        size_t pos = 0;
        int row = 0;
        uint32_t t, wire_lz4, wire_raw;
        while (pos + 96 < sizeof(log_tx)) {
            int w = snprintf((char *)log_tx + pos, sizeof(log_tx) - pos,
                             "{\"station\":\"RUT200-AQMS\",\"seq\":%d,\"pm25\":28.4,\"pm10\":61.0,\"o3\":31.2}\n", row++);
            if (w <= 0) break;
            pos += (size_t)w;
        }
        memset(&q_ab, 0, sizeof(q_ab));
        memset(&q_ba, 0, sizeof(q_ba));
        memset(&link_a, 0, sizeof(link_a));
        memset(&link_b, 0, sizeof(link_b));
        link_a.q = &q_ab; link_b.q = &q_ba;
        link_a.drop_pct = 15; link_b.drop_pct = 15;
        nltp_init(&ep_a, 0xA001, 20000, send_cb, clock_cb, &link_a);
        nltp_init(&ep_b, 0xA002, 20000, send_cb, clock_cb, &link_b);
        nltp_set_key(&ep_a, key);
        nltp_set_key(&ep_b, key);
        memset(file_rx, 0, sizeof(file_rx));
        nltp_set_rx_buffer(&ep_b, file_rx, sizeof(file_rx));
        nltp_send_buffer_lz4(&ep_a, log_tx, (uint32_t)pos, comp_scratch, sizeof(comp_scratch), "aqms.jsonl");
        for (t = 0; t < 20000 && !(nltp_tx_done(&ep_a) && nltp_rx_done(&ep_b)); ++t) {
            g_clock++;
            deliver(&q_ab, &ep_b);
            deliver(&q_ba, &ep_a);
            nltp_poll(&ep_a);
            nltp_poll(&ep_b);
        }
        wire_lz4 = ep_a.bytes_tx;
        ok = nltp_tx_verified(&ep_a) && nltp_rx_sha_ok(&ep_b) && nltp_rx_len(&ep_b) == (uint32_t)pos &&
             memcmp(file_rx, log_tx, pos) == 0 && wire_lz4 < (uint32_t)(pos / 2u);

        /* Also verify incompressible `file_tx` sent via `nltp_send_buffer_lz4` auto-bypasses cleanly */
        nltp_send_buffer_lz4(&ep_a, file_tx, 8192u, comp_scratch, sizeof(comp_scratch), "rand.bin");
        for (t = 0; t < 20000 && !(nltp_tx_done(&ep_a) && ep_b.rx.completed && ep_b.rx.stream_id == 2u); ++t) {
            g_clock++;
            deliver(&q_ab, &ep_b);
            deliver(&q_ba, &ep_a);
            nltp_poll(&ep_a);
            nltp_poll(&ep_b);
        }
        wire_raw = ep_a.bytes_tx - wire_lz4;
        ok = ok && nltp_tx_verified(&ep_a) && nltp_rx_sha_ok(&ep_b) && nltp_rx_len(&ep_b) == 8192u &&
             memcmp(file_rx, file_tx, 8192u) == 0;
        snprintf(detail, sizeof(detail), "jsonl=%uB->wire=%uB rand_wire=%uB sha_ok=1",
                 (unsigned)pos, (unsigned)wire_lz4, (unsigned)wire_raw);
        check(ok, "1.6: encrypted X25519 + LZ4 E2E under 15% loss + raw auto-bypass", detail);
    }

    printf("\n%s (%d failure%s)\n", failures ? "FAILED" : "ALL PASSED", failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
