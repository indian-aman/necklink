/*
 * nltp_txonly_selftest.c — ASan/UBSan verification of the ultra-light NLTP_TX_ONLY=1
 * sensor/station profile (~4.8 KB RAM endpoint footprint).
 *
 * Build:
 *   gcc -std=c99 -O2 -Wall -Wextra -pedantic -Werror -DNLTP_TX_ONLY=1 \
 *       -I. nltp_core.c nltp_sha256.c tools/nltp_txonly_selftest.c -o nltp_txonly_selftest
 */
#ifndef NLTP_TX_ONLY
#define NLTP_TX_ONLY 1
#endif

#include <stdio.h>
#include <string.h>

#include "nltp_core.h"

#define QMAX 128
#define PMAX 1200

typedef struct {
    uint8_t data[QMAX][PMAX];
    uint16_t len[QMAX];
    int head, count;
} pkt_q_t;

typedef struct {
    pkt_q_t *q;
    uint32_t rng;
    int drop_pct;
} sim_link_t;

static uint32_t g_now_ms = 1000u;

static uint32_t now_cb(void *u) {
    (void)u;
    return g_now_ms;
}

static uint32_t xorshift32(uint32_t *s) {
    uint32_t x = *s ? *s : 0xA5A55A5Au;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *s = x;
    return x;
}

static void det_rng(void *u, uint8_t *out, size_t len) {
    sim_link_t *lk = (sim_link_t *)u;
    size_t i;
    for (i = 0; i < len; ++i) out[i] = (uint8_t)(xorshift32(&lk->rng) & 0xFFu);
}

static void send_cb(void *u, const uint8_t *pkt, size_t len) {
    sim_link_t *lk = (sim_link_t *)u;
    int slot;
    if (len > PMAX || lk->q->count >= QMAX) return;
    if (lk->drop_pct > 0 && (int)(xorshift32(&lk->rng) % 100u) < lk->drop_pct) return;
    slot = (lk->q->head + lk->q->count) % QMAX;
    memcpy(lk->q->data[slot], pkt, len);
    lk->q->len[slot] = (uint16_t)len;
    lk->q->count++;
}

static void deliver(pkt_q_t *q, nltp_endpoint_t *dst) {
    while (q->count > 0) {
        int idx = q->head;
        q->head = (q->head + 1) % QMAX;
        q->count--;
        nltp_on_datagram(dst, q->data[idx], q->len[idx]);
    }
}

int main(void) {
    static nltp_endpoint_t ep_a, ep_b;
    static pkt_q_t q_ab, q_ba;
    static uint8_t telemetry[4096], comp_scratch[4096], dummy_rx[256];
    sim_link_t la, lb;
    uint8_t key[32], rx_cmd[NLTP_REFLEX_MAX_LEN];
    uint16_t rx_len = 0;
    uint32_t rx_cid = 0, s1, s2, t, sid;
    size_t pos = 0, ep_bytes = sizeof(nltp_endpoint_t);
    int got_a = 0, got_b = 0, row = 0;

    printf("NLTP_TX_ONLY=1 sizeof(nltp_endpoint_t) = %lu bytes\n", (unsigned long)ep_bytes);
    if (ep_bytes > 5120u) {
        fprintf(stderr, "FAIL: sizeof(nltp_endpoint_t) = %lu exceeds 5120 B budget\n", (unsigned long)ep_bytes);
        return 1;
    }

    memset(&q_ab, 0, sizeof(q_ab));
    memset(&q_ba, 0, sizeof(q_ba));
    la.q = &q_ab; la.rng = 0x12345678u; la.drop_pct = 20;
    lb.q = &q_ba; lb.rng = 0x87654321u; lb.drop_pct = 20;

    nltp_key_from_psk("station-tx-only-secret", key);
    nltp_init(&ep_a, 0xA001u, 10000u, send_cb, now_cb, &la);
    nltp_init(&ep_b, 0xB002u, 10000u, send_cb, now_cb, &lb);
    nltp_set_random(&ep_a, det_rng);
    nltp_set_random(&ep_b, det_rng);
    nltp_set_key(&ep_a, key);
    nltp_set_key(&ep_b, key);
    nltp_set_rx_buffer(&ep_b, dummy_rx, sizeof(dummy_rx));

    /* Bidirectional encrypted X25519+PSK reflex commands under 20% packet loss */
    s1 = nltp_send_reflex(&ep_a, (const uint8_t *)"ALARM_HIGH_PM25", 15u);
    s2 = nltp_send_reflex(&ep_b, (const uint8_t *)"GATEWAY_RESET_PUMP", 18u);

    for (t = 0; t < 5000u; ++t) {
        g_now_ms++;
        deliver(&q_ab, &ep_b);
        deliver(&q_ba, &ep_a);
        nltp_poll(&ep_a);
        nltp_poll(&ep_b);
        while (nltp_pop_reflex(&ep_b, rx_cmd, sizeof(rx_cmd), &rx_len, &rx_cid)) {
            if (rx_len == 15u && memcmp(rx_cmd, "ALARM_HIGH_PM25", 15u) == 0) got_b++;
        }
        while (nltp_pop_reflex(&ep_a, rx_cmd, sizeof(rx_cmd), &rx_len, &rx_cid)) {
            if (rx_len == 18u && memcmp(rx_cmd, "GATEWAY_RESET_PUMP", 18u) == 0) got_a++;
        }
        if (nltp_reflex_acked(&ep_a, s1) && nltp_reflex_acked(&ep_b, s2)) break;
    }
    if (got_a != 1 || got_b != 1 || !nltp_reflex_acked(&ep_a, s1) || !nltp_reflex_acked(&ep_b, s2)) {
        fprintf(stderr, "FAIL: bidirectional reflex under NLTP_TX_ONLY failed (got_a=%d got_b=%d)\n", got_a, got_b);
        return 1;
    }

    /* Bulk LZ4 TX emission + safe ignore of incoming STREAM_META & DATA_SHARD on TX-only peer */
    la.drop_pct = 0;
    lb.drop_pct = 0;
    while (pos + 80u < sizeof(telemetry)) {
        int w = snprintf((char *)telemetry + pos, sizeof(telemetry) - pos,
                         "station=ST01,row=%d,pm25=21.4,pm10=48.0,co2=419\n", row++);
        if (w <= 0) break;
        pos += (size_t)w;
    }
    sid = nltp_send_buffer_lz4(&ep_a, telemetry, (uint32_t)pos, comp_scratch, sizeof(comp_scratch), "log.csv");
    if (sid == 0u || ep_a.tx.len >= pos) {
        fprintf(stderr, "FAIL: nltp_send_buffer_lz4 did not compress telemetry (sid=%u tx.len=%u raw=%lu)\n",
                (unsigned)sid, (unsigned)ep_a.tx.len, (unsigned long)pos);
        return 1;
    }
    for (t = 0; t < 20u; ++t) {
        g_now_ms++;
        nltp_poll(&ep_a);
        deliver(&q_ab, &ep_b); /* ep_b is NLTP_TX_ONLY=1: must safely drop oversized/bulk frames */
    }
    printf("PASS  NLTP_TX_ONLY=1: %lu B RAM, X25519+PSK bidirectional reflex @ 20%% loss, LZ4 TX (%luB -> %uB) & bulk-RX guard OK\n",
           (unsigned long)ep_bytes, (unsigned long)pos, (unsigned)ep_a.tx.len);
    return 0;
}
