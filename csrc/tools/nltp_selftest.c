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

    printf("\n%s (%d failure%s)\n", failures ? "FAILED" : "ALL PASSED", failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
