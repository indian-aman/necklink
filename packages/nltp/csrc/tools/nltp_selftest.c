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

    printf("\n%s (%d failure%s)\n", failures ? "FAILED" : "ALL PASSED", failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
