/*
 * mesh3_v2_main.cpp — 3-Node Pure Off-Wi-Fi Interconnection & Benchmark Firmware (NLTP 1.3.2)
 *
 * Zero Wi-Fi Router / Zero SoftAP / Zero DHCP on ALL 3 ESP32 Modules:
 *   - Node 0 (COM9), Node 1 (COM11), Node 2 (COM12) all boot in connectionless WIFI_STA mode
 *     (`WiFi.disconnect(true, true)`) locked to 2.4 GHz Channel 11.
 *   - Built directly on the updated NLTP 1.3.2 `nltp_espnow.h` (`nltp_espnow_hub_t`,
 *     stateless `nltp_espnow_frame_ok` pre-auth gate, 16-entry LRU peer-table eviction,
 *     and `nltp_core.c` `NLTP_RX_TAKEOVER_MS` anti-stall protection).
 *   - Compares head-to-head on identical hardware & radio conditions:
 *       1) NLTP over ESP-NOW v2 (zero-wrapper "NL" frames, <= 1,010 B AEAD / 990 B CRC32,
 *          GF(256) Cauchy FEC, ChaCha20-Poly1305 + RFC 6479 anti-replay, SHA-256 FIN_ACK)
 *       2) Bare-Metal Raw ESP-NOW v2 (<= 1,470 B vendor action frames, no FEC/AEAD)
 *     across TX power / link-budget sweep, payload sizes (4 B - 32 KB), all 7 PHY modes
 *     (incl. Espressif Long Range 250K/500K), 0-35% packet loss, 2-hop L2 relay,
 *     simultaneous full-duplex collisions, 50-stream soak, stray-conn_id anti-stall,
 *     and forged/spoofed radio fuzz floods.
 */

#include <Arduino.h>
#include <WiFi.h>
#include "esp_wifi.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "esp_random.h"

#include "nltp_espnow.h"

#ifndef MESH3_NODE_ID
#define MESH3_NODE_ID 0
#endif

static const uint8_t MY_NODE_ID = (uint8_t)MESH3_NODE_ID;
static const uint8_t MESH_CHANNEL = 11;
static const uint32_t MAX_TEST_BYTES = 32u * 1024u;
static const uint8_t BCAST_MAC[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

static char g_psk[96] = "necklink-1.3.2-mesh3-key";
static nltp_espnow_rate_t g_rate_mode = NLTP_ESPNOW_RATE_AUTO;
static int8_t g_txpwr_qdbm = 78; /* 78 = +19.5 dBm max */
static volatile uint8_t g_drop_pct = 0;

/* Deterministic PRNG + O(1) payload verifier */
static uint32_t g_rng = 0x9E3779B9u ^ ((uint32_t)MESH3_NODE_ID * 0x85EBCA6Bu);
static inline uint32_t fast_rng(void) {
    uint32_t x = g_rng;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    g_rng = x;
    return x;
}

static inline uint8_t pattern_byte(uint32_t seed, uint32_t idx) {
    uint32_t z = seed + idx * 0x9E3779B9u;
    z = (z ^ (z >> 16)) * 0x85EBCA6Bu;
    z = (z ^ (z >> 13)) * 0xC2B2AE35u;
    return (uint8_t)(z ^ (z >> 16));
}

static uint8_t g_tx_payload_buf[MAX_TEST_BYTES];
static uint8_t *g_tx_payload = g_tx_payload_buf;
static void fill_tx_payload(uint32_t seed, uint32_t len) {
    for (uint32_t i = 0; i < len; ++i) {
        g_tx_payload[i] = pattern_byte(seed, i);
    }
}

/* =========================================================================
 * 1. Pure Off-Wi-Fi L2 Neighbor Discovery Table (Up to 8 Peers)
 * ========================================================================= */
typedef struct __attribute__((packed)) {
    uint8_t m0;      /* 'M' */
    uint8_t m1;      /* '3' */
    uint8_t node_id;
    uint8_t channel;
    int8_t  txpwr_qdbm;
    uint8_t rate_mode;
    uint16_t seq;
} mesh3_beacon_t;

typedef struct {
    bool     known;
    uint8_t  node_id;
    uint8_t  mac[6];
    int8_t   rssi;
    int8_t   noise;
    int8_t   peer_txpwr;
    uint32_t pkts;
    uint32_t last_ms;
} mesh3_peer_t;

static mesh3_peer_t g_neighbors[8] = {};
static uint16_t g_beacon_seq = 0;
static uint32_t g_last_beacon_ms = 0;

static void record_neighbor(uint8_t node_id, const uint8_t mac[6], int8_t rssi, int8_t noise, int8_t txpwr) {
    int slot = -1;
    for (int i = 0; i < 8; ++i) {
        if (g_neighbors[i].known && memcmp(g_neighbors[i].mac, mac, 6) == 0) {
            slot = i;
            break;
        }
        if (!g_neighbors[i].known && slot < 0) slot = i;
    }
    if (slot < 0) slot = 0;
    g_neighbors[slot].known = true;
    g_neighbors[slot].node_id = node_id;
    memcpy(g_neighbors[slot].mac, mac, 6);
    g_neighbors[slot].rssi = rssi;
    g_neighbors[slot].noise = noise;
    g_neighbors[slot].peer_txpwr = txpwr;
    g_neighbors[slot].pkts++;
    g_neighbors[slot].last_ms = millis();
}

static bool parse_mac_or_node(const char *tok, uint8_t out_mac[6]) {
    if (strchr(tok, ':')) {
        unsigned int b[6];
        if (sscanf(tok, "%x:%x:%x:%x:%x:%x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6) return false;
        for (int i = 0; i < 6; ++i) out_mac[i] = (uint8_t)b[i];
        return true;
    }
    int nid = atoi(tok);
    for (int i = 0; i < 8; ++i) {
        if (g_neighbors[i].known && g_neighbors[i].node_id == (uint8_t)nid) {
            memcpy(out_mac, g_neighbors[i].mac, 6);
            return true;
        }
    }
    return false;
}

static void emit_l2_beacon(void) {
    mesh3_beacon_t b;
    b.m0 = 'M';
    b.m1 = '3';
    b.node_id = MY_NODE_ID;
    b.channel = MESH_CHANNEL;
    b.txpwr_qdbm = g_txpwr_qdbm;
    b.rate_mode = (uint8_t)g_rate_mode;
    b.seq = ++g_beacon_seq;
    nltp_espnow_raw_send(BCAST_MAC, (const uint8_t *)&b, sizeof(b), NLTP_ESPNOW_RATE_1M, -55);
}

/* =========================================================================
 * 2. Bare-Metal Raw ESP-NOW v2 Engine (<= 1,470 B, Sync + Async Duplex + 2-Hop Relay)
 * ========================================================================= */
typedef struct __attribute__((packed)) {
    uint8_t  m0;          /* 'R' */
    uint8_t  m1;          /* '2' = data chunk, 'A' = completion echo, 'F' = forged/unauth test */
    uint8_t  hops_left;   /* 0 = direct, 1 = forward via relay */
    uint8_t  flags;       /* 0x01 = FIN chunk; for 'A': intact (0/1) */
    uint16_t run_id;
    uint16_t seq;         /* for 'A': rcvd_pkts */
    uint32_t total_bytes; /* for 'A': rcvd_bytes */
    uint8_t  final_dst[6];/* used when hops_left > 0 so relay forwards to final destination */
    uint8_t  orig_src[6]; /* original sender MAC for 2-hop ACK return */
} raw_v2_hdr_t;           /* 24 bytes -> 1,446 B payload = 1,470 B exact ESP-NOW v2 MTU! */

typedef struct {
    volatile bool rx_active;
    volatile bool rx_report_ready;
    uint8_t       rx_orig_src[6];
    uint8_t       rx_last_hop[6];
    uint8_t       rx_was_relayed;
    int8_t        rx_rssi;
    uint16_t      rx_run_id;
    uint16_t      rx_expected_pkts;
    uint16_t      rx_rcvd_pkts;
    uint16_t      rx_max_l2;
    uint32_t      rx_total_bytes;
    uint32_t      rx_rcvd_bytes;
    uint32_t      rx_corrupt_bytes;
    uint32_t      rx_forged_accepted;
    uint32_t      rx_last_us;
    uint8_t       rx_seen[32];

    volatile bool ack_got;
    uint16_t      ack_run_id;
    uint8_t       ack_ok;
    uint16_t      ack_pkts;
    uint32_t      ack_bytes;

    /* Async Raw TX state for simultaneous full-duplex Raw v2 collision testing */
    bool          async_tx_active;
    uint8_t       async_dst[6];
    uint8_t       async_via[6];
    bool          async_use_relay;
    uint16_t      async_run_id;
    uint16_t      async_next_seq;
    uint16_t      async_total_pkts;
    uint32_t      async_total_len;
    uint32_t      async_t0_us;
} raw_v2_state_t;

static raw_v2_state_t g_raw = {};
static uint16_t g_next_raw_run = 1;
static uint8_t g_my_mac[6] = {0};

/* L2 Cut-Through Relay state for 2-hop NLTP ESP-NOW v2 testing (`Node A -> Relay -> Node B`) */
static bool g_l2_nltp_relay_on = false;
static uint8_t g_relay_a_mac[6] = {0};
static uint8_t g_relay_b_mac[6] = {0};
static uint32_t g_relayed_frames = 0;

/* Deferred relay queue so ISR never calls esp_now_send directly */
typedef struct {
    uint8_t  dst[6];
    uint16_t len;
    uint8_t  buf[NLTP_ESPNOW_MTU];
} relay_item_t;
static QueueHandle_t g_relay_q = nullptr;

static void enqueue_l2_forward(const uint8_t dst[6], const uint8_t *pkt, uint16_t len) {
    if (!g_relay_q || len > NLTP_ESPNOW_MTU) return;
    relay_item_t it;
    memcpy(it.dst, dst, 6);
    it.len = len;
    memcpy(it.buf, pkt, len);
    xQueueSend(g_relay_q, &it, 0);
}

static void on_non_nltp_frame_isr(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    if (!data || len < 8) return;
    int8_t rssi = (info && info->rx_ctrl) ? (int8_t)info->rx_ctrl->rssi : -60;
    int8_t noise = (info && info->rx_ctrl) ? (int8_t)info->rx_ctrl->noise_floor : -96;

    if (len == (int)sizeof(mesh3_beacon_t) && data[0] == 'M' && data[1] == '3') {
        const mesh3_beacon_t *b = (const mesh3_beacon_t *)data;
        if (info && info->src_addr) {
            record_neighbor(b->node_id, info->src_addr, rssi, noise, b->txpwr_qdbm);
        }
        return;
    }

    if (len < (int)sizeof(raw_v2_hdr_t) || data[0] != 'R') return;

    /* Apply simulated wireless RX packet loss to Raw ESP-NOW v2 identically to NLTP */
    if (g_drop_pct > 0 && (fast_rng() % 100u) < g_drop_pct) return;

    const raw_v2_hdr_t *h = (const raw_v2_hdr_t *)data;

    /* Forged / unauthenticated raw ESP-NOW frame test: bare-metal ESP-NOW has no AEAD/PSK */
    if (h->m1 == 'F') {
        g_raw.rx_forged_accepted++;
        return;
    }

    /* Multi-hop L2 Raw ESP-NOW v2 forwarding if we are the intermediate relay */
    if (h->hops_left > 0 && memcmp(h->final_dst, g_my_mac, 6) != 0) {
        uint8_t tmp[NLTP_ESPNOW_MTU];
        if (len <= (int)NLTP_ESPNOW_MTU) {
            memcpy(tmp, data, (size_t)len);
            ((raw_v2_hdr_t *)tmp)->hops_left = (uint8_t)(h->hops_left - 1u);
            enqueue_l2_forward(h->final_dst, tmp, (uint16_t)len);
            g_relayed_frames++;
        }
        return;
    }

    if (h->m1 == 'A') {
        g_raw.ack_run_id = h->run_id;
        g_raw.ack_ok     = h->flags;
        g_raw.ack_pkts   = h->seq;
        g_raw.ack_bytes  = h->total_bytes;
        g_raw.ack_got    = true;
        return;
    }
    if (h->m1 != '2') return;

    uint32_t max_chunk = NLTP_ESPNOW_MTU - sizeof(raw_v2_hdr_t);
    uint32_t exp_pkts = (h->total_bytes == 0) ? 1u : ((h->total_bytes + max_chunk - 1u) / max_chunk);
    if (exp_pkts > 256u) return;

    if (!g_raw.rx_active || g_raw.rx_run_id != h->run_id) {
        g_raw.rx_active = true;
        g_raw.rx_report_ready = false;
        memcpy(g_raw.rx_orig_src, h->orig_src, 6);
        if (info && info->src_addr) memcpy(g_raw.rx_last_hop, info->src_addr, 6);
        g_raw.rx_was_relayed = (info && info->src_addr && memcmp(info->src_addr, h->orig_src, 6) != 0) ? 1u : 0u;
        g_raw.rx_run_id = h->run_id;
        g_raw.rx_expected_pkts = (uint16_t)exp_pkts;
        g_raw.rx_rcvd_pkts = 0;
        g_raw.rx_max_l2 = 0;
        g_raw.rx_total_bytes = h->total_bytes;
        g_raw.rx_rcvd_bytes = 0;
        g_raw.rx_corrupt_bytes = 0;
        memset(g_raw.rx_seen, 0, sizeof(g_raw.rx_seen));
    }

    g_raw.rx_rssi = rssi;
    if ((uint16_t)len > g_raw.rx_max_l2) g_raw.rx_max_l2 = (uint16_t)len;
    g_raw.rx_last_us = (uint32_t)esp_timer_get_time();

    uint16_t seq = h->seq;
    uint32_t chunk_len = (uint32_t)len - (uint32_t)sizeof(raw_v2_hdr_t);
    if (seq < 256u && !(g_raw.rx_seen[seq >> 3] & (1u << (seq & 7u)))) {
        g_raw.rx_seen[seq >> 3] |= (uint8_t)(1u << (seq & 7u));
        g_raw.rx_rcvd_pkts++;
        g_raw.rx_rcvd_bytes += chunk_len;

        uint32_t seed = 0x55000000u + h->total_bytes;
        uint32_t base_off = (uint32_t)seq * max_chunk;
        const uint8_t *pl = data + sizeof(raw_v2_hdr_t);
        for (uint32_t j = 0; j < chunk_len; ++j) {
            if (pl[j] != pattern_byte(seed, base_off + j)) {
                g_raw.rx_corrupt_bytes++;
            }
        }
    }

    if ((h->flags & 0x01u) || g_raw.rx_rcvd_pkts >= g_raw.rx_expected_pkts) {
        g_raw.rx_report_ready = true;
    }
}

static void poll_raw_v2(void) {
    /* 1. Drain deferred L2 relay queue */
    relay_item_t rit;
    while (g_relay_q && xQueueReceive(g_relay_q, &rit, 0) == pdTRUE) {
        nltp_espnow_raw_send(rit.dst, rit.buf, rit.len, g_rate_mode, -55);
    }

    /* 2. Progress async Raw v2 sender (used for simultaneous full-duplex collision test) */
    if (g_raw.async_tx_active) {
        static uint8_t apkt[NLTP_ESPNOW_MTU];
        uint32_t max_chunk = NLTP_ESPNOW_MTU - sizeof(raw_v2_hdr_t);
        int burst = 0;
        while (g_raw.async_next_seq < g_raw.async_total_pkts && burst < 4) {
            uint16_t i = g_raw.async_next_seq++;
            uint32_t off = (uint32_t)i * max_chunk;
            uint32_t clen = (g_raw.async_total_len > off) ? (g_raw.async_total_len - off) : 0u;
            if (clen > max_chunk) clen = max_chunk;

            raw_v2_hdr_t *h = (raw_v2_hdr_t *)apkt;
            h->m0 = 'R';
            h->m1 = '2';
            h->hops_left = g_raw.async_use_relay ? 1u : 0u;
            h->flags = (i + 1u == g_raw.async_total_pkts) ? 0x01u : 0x00u;
            h->run_id = g_raw.async_run_id;
            h->seq = i;
            h->total_bytes = g_raw.async_total_len;
            memcpy(h->final_dst, g_raw.async_dst, 6);
            memcpy(h->orig_src, g_my_mac, 6);

            uint32_t seed = 0x55000000u + g_raw.async_total_len;
            uint8_t *pl = apkt + sizeof(raw_v2_hdr_t);
            for (uint32_t j = 0; j < clen; ++j) pl[j] = pattern_byte(seed, off + j);

            const uint8_t *next_hop = g_raw.async_use_relay ? g_raw.async_via : g_raw.async_dst;
            nltp_espnow_raw_send(next_hop, apkt, sizeof(raw_v2_hdr_t) + clen, g_rate_mode, -55);
            burst++;
        }
        if (g_raw.async_next_seq >= g_raw.async_total_pkts) {
            uint32_t dt_us = micros() - g_raw.async_t0_us;
            if (g_raw.ack_got || dt_us >= 450000u) {
                g_raw.async_tx_active = false;
                uint8_t ok = (g_raw.ack_got && g_raw.ack_run_id == g_raw.async_run_id) ? g_raw.ack_ok : 0u;
                uint32_t rcvd = (g_raw.ack_got && g_raw.ack_run_id == g_raw.async_run_id) ? g_raw.ack_bytes : 0u;
                float mbps = dt_us > 0 ? (g_raw.async_total_len * 8.0f) / (float)dt_us : 0.0f;
                Serial.printf("RAW_TX ok=%u ack=%u rcvd=%lu/%lu ms=%.2f mbps=%.3f max_tx_l2=%u\n",
                              (unsigned)ok, (unsigned)(g_raw.ack_got ? 1 : 0),
                              (unsigned long)rcvd, (unsigned long)g_raw.async_total_len,
                              dt_us / 1000.0f, mbps, (unsigned)g_nltp_espnow_bus.max_tx_frame_len);
            }
        }
    }

    /* 3. Finalize Raw v2 receiver & send completion echo */
    if (!g_raw.rx_active) return;
    uint32_t now_us = (uint32_t)esp_timer_get_time();
    uint32_t tail_us = (g_rate_mode == NLTP_ESPNOW_RATE_LR_250K || g_rate_mode == NLTP_ESPNOW_RATE_LR_500K)
                           ? 200000u
                           : 55000u;
    if (!g_raw.rx_report_ready && (uint32_t)(now_us - g_raw.rx_last_us) < tail_us) return;

    g_raw.rx_active = false;
    g_raw.rx_report_ready = false;
    uint8_t ok = (g_raw.rx_rcvd_pkts == g_raw.rx_expected_pkts &&
                  g_raw.rx_rcvd_bytes == g_raw.rx_total_bytes &&
                  g_raw.rx_corrupt_bytes == 0)
                     ? 1u
                     : 0u;

    raw_v2_hdr_t ack = {};
    ack.m0 = 'R';
    ack.m1 = 'A';
    ack.hops_left = g_raw.rx_was_relayed ? 1u : 0u;
    ack.flags = ok;
    ack.run_id = g_raw.rx_run_id;
    ack.seq = g_raw.rx_rcvd_pkts;
    ack.total_bytes = g_raw.rx_rcvd_bytes;
    memcpy(ack.final_dst, g_raw.rx_orig_src, 6);
    memcpy(ack.orig_src, g_my_mac, 6);
    const uint8_t *reply_hop = g_raw.rx_was_relayed ? g_raw.rx_last_hop : g_raw.rx_orig_src;
    nltp_espnow_raw_send(reply_hop, (const uint8_t *)&ack, sizeof(ack), g_rate_mode, g_raw.rx_rssi);

    Serial.printf("RAW_RX len=%lu/%lu pkts=%u/%u intact=%u relayed=%u max_l2=%u rssi=%d\n",
                  (unsigned long)g_raw.rx_rcvd_bytes,
                  (unsigned long)g_raw.rx_total_bytes,
                  (unsigned)g_raw.rx_rcvd_pkts,
                  (unsigned)g_raw.rx_expected_pkts,
                  (unsigned)ok,
                  (unsigned)g_raw.rx_was_relayed,
                  (unsigned)g_raw.rx_max_l2,
                  (int)g_raw.rx_rssi);
}

static void execute_raw_v2_sync(const uint8_t dst_mac[6], const uint8_t *via_mac, uint32_t total_len) {
    static uint8_t pkt_buf[NLTP_ESPNOW_MTU];
    uint32_t max_chunk = NLTP_ESPNOW_MTU - sizeof(raw_v2_hdr_t);
    uint32_t n_pkts = (total_len == 0) ? 1u : ((total_len + max_chunk - 1u) / max_chunk);
    uint16_t run_id = g_next_raw_run++;
    if (run_id == 0) run_id = g_next_raw_run++;

    g_raw.ack_got = false;
    g_nltp_espnow_bus.max_tx_frame_len = 0;

    uint32_t seed = 0x55000000u + total_len;
    uint32_t t0_us = micros();

    for (uint32_t i = 0; i < n_pkts; ++i) {
        uint32_t off = i * max_chunk;
        uint32_t clen = (total_len > off) ? (total_len - off) : 0u;
        if (clen > max_chunk) clen = max_chunk;

        raw_v2_hdr_t *h = (raw_v2_hdr_t *)pkt_buf;
        h->m0 = 'R';
        h->m1 = '2';
        h->hops_left = via_mac ? 1u : 0u;
        h->flags = (i + 1u == n_pkts) ? 0x01u : 0x00u;
        h->run_id = run_id;
        h->seq = (uint16_t)i;
        h->total_bytes = total_len;
        memcpy(h->final_dst, dst_mac, 6);
        memcpy(h->orig_src, g_my_mac, 6);

        uint8_t *pl = pkt_buf + sizeof(raw_v2_hdr_t);
        for (uint32_t j = 0; j < clen; ++j) pl[j] = pattern_byte(seed, off + j);

        const uint8_t *hop = via_mac ? via_mac : dst_mac;
        nltp_espnow_raw_send(hop, pkt_buf, sizeof(raw_v2_hdr_t) + clen, g_rate_mode, -55);
    }

    uint32_t wait_us = (g_rate_mode == NLTP_ESPNOW_RATE_LR_250K || g_rate_mode == NLTP_ESPNOW_RATE_LR_500K)
                           ? 1600000u
                           : 420000u;
    while (!g_raw.ack_got && (uint32_t)(micros() - t0_us) < wait_us) {
        poll_raw_v2();
        delayMicroseconds(150);
    }
    uint32_t dt_us = micros() - t0_us;
    uint8_t ok = (g_raw.ack_got && g_raw.ack_run_id == run_id) ? g_raw.ack_ok : 0u;
    uint32_t rcvd = (g_raw.ack_got && g_raw.ack_run_id == run_id) ? g_raw.ack_bytes : 0u;
    float mbps = dt_us > 0 ? (total_len * 8.0f) / (float)dt_us : 0.0f;

    Serial.printf("RAW_TX ok=%u ack=%u rcvd=%lu/%lu ms=%.2f mbps=%.3f max_tx_l2=%u\n",
                  (unsigned)ok, (unsigned)(g_raw.ack_got ? 1 : 0),
                  (unsigned long)rcvd, (unsigned long)total_len,
                  dt_us / 1000.0f, mbps, (unsigned)g_nltp_espnow_bus.max_tx_frame_len);
}

/* =========================================================================
 * 3. NLTP 1.3.2 Multi-Peer Hub (`nltp_espnow_hub_t`) + Optional L2 Cut-Through Relay
 * ========================================================================= */
static nltp_espnow_hub_t *g_hub = nullptr;
static bool g_nltp_tx_active = false;
static int  g_nltp_tx_slot = -1;
static uint32_t g_nltp_tx_len = 0;
static uint32_t g_nltp_tx_t0_us = 0;
static uint16_t g_max_nltp_rx_l2 = 0;
static uint32_t g_preauth_drops = 0;

static void on_hub_message(void *, const uint8_t src_mac[6], int8_t rssi,
                           const uint8_t *data, uint32_t len,
                           const char *filename, int sha_ok) {
    (void)data;
    Serial.printf("NLTP_RX src=%02x:%02x:%02x:%02x:%02x:%02x rssi=%d len=%lu sha_ok=%d file=%s max_rx_l2=%u\n",
                  src_mac[0], src_mac[1], src_mac[2], src_mac[3], src_mac[4], src_mac[5],
                  (int)rssi, (unsigned long)len, sha_ok, filename ? filename : "",
                  (unsigned)g_max_nltp_rx_l2);
}

/* Custom poll wrapper around g_hub that supports:
 *   - Identical RX drop simulation (`g_drop_pct`) as Raw ESP-NOW v2
 *   - Optional 2-hop L2 cut-through forwarding (`g_l2_nltp_relay_on`: A <-> Relay <-> B)
 *   - Exact `g_max_nltp_rx_l2` tracking
 *   - All v1.3.2 `nltp_espnow_frame_ok` & LRU peer/slot protections! */
static void poll_nltp_hub(void) {
    nltp_espnow_rx_item_t item;
    uint32_t now_ms = nltp_espnow_now_ms(NULL);

    while (g_nltp_espnow_bus.rx_q && xQueueReceive(g_nltp_espnow_bus.rx_q, &item, 0) == pdTRUE) {
        if (g_drop_pct > 0 && (fast_rng() % 100u) < g_drop_pct) {
            continue;
        }
        if (item.len > g_max_nltp_rx_l2) g_max_nltp_rx_l2 = item.len;

        /* If acting as an L2 cut-through relay between A and B (including loopback A->Relay->A), forward! */
        if (g_l2_nltp_relay_on) {
            if (memcmp(item.src_mac, g_relay_a_mac, 6) == 0) {
                nltp_espnow_raw_send(g_relay_b_mac, item.data, item.len, g_rate_mode, item.rssi);
                g_relayed_frames++;
                continue;
            } else if (memcmp(item.src_mac, g_relay_b_mac, 6) == 0) {
                nltp_espnow_raw_send(g_relay_a_mac, item.data, item.len, g_rate_mode, item.rssi);
                g_relayed_frames++;
                continue;
            }
        }

        if (g_hub->allow_cb && !g_hub->allow_cb(g_hub->user, item.src_mac)) {
            continue;
        }
        int known = 0;
        for (uint32_t i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
            if (g_hub->slots[i].in_use && memcmp(g_hub->slots[i].mac, item.src_mac, 6) == 0) {
                known = 1;
                break;
            }
        }
        if (!known && !nltp_espnow_frame_ok(item.data, item.len, g_hub->encrypted, g_hub->key32)) {
            if (item.len >= NLTP_HEADER_BYTES && item.data[0] == NLTP_MAGIC_0 && item.data[1] == NLTP_MAGIC_1) {
                g_preauth_drops++;
            }
            continue;
        }

        nltp_espnow_slot_t *slot = nltp_espnow_hub_get_or_create_slot(g_hub, item.src_mac, now_ms);
        if (!slot) continue;

        uint32_t valid0 = slot->ep.valid_rx;
        nltp_on_datagram(&slot->ep, item.data, item.len);
        if (slot->ep.valid_rx != valid0) {
            slot->last_active_ms = now_ms;
            slot->last_rssi = item.rssi;
        }
        if (slot->ep.rx.completed &&
            (slot->ep.rx.stream_id != slot->last_reported_sid ||
             slot->ep.rx.peer_cid != slot->last_reported_cid)) {
            slot->last_reported_sid = slot->ep.rx.stream_id;
            slot->last_reported_cid = slot->ep.rx.peer_cid;
            if (g_hub->on_message_cb) {
                g_hub->on_message_cb(g_hub->user, slot->mac, slot->last_rssi,
                                     slot->rx_buf, nltp_rx_len(&slot->ep),
                                     slot->ep.rx.filename, nltp_rx_sha_ok(&slot->ep));
            }
        }
    }

    for (uint32_t i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
        if (!g_hub->slots[i].in_use) continue;
        if ((now_ms - g_hub->slots[i].last_active_ms) >= g_hub->idle_timeout_ms) {
            nltp_espnow_hub_release_slot(&g_hub->slots[i]);
            continue;
        }
        nltp_poll(&g_hub->slots[i].ep);
    }

    if (g_nltp_tx_active && g_nltp_tx_slot >= 0) {
        nltp_espnow_slot_t *s = &g_hub->slots[g_nltp_tx_slot];
        if (!s->in_use || nltp_tx_done(&s->ep)) {
            g_nltp_tx_active = false;
            uint32_t dt_us = micros() - g_nltp_tx_t0_us;
            float mbps = dt_us > 0 ? (g_nltp_tx_len * 8.0f) / (float)dt_us : 0.0f;
            Serial.printf("NLTP_TX ok=%d len=%lu ms=%.2f mbps=%.3f max_tx_l2=%u pkts_tx=%lu\n",
                          s->in_use ? nltp_tx_verified(&s->ep) : 0,
                          (unsigned long)g_nltp_tx_len,
                          dt_us / 1000.0f,
                          mbps,
                          (unsigned)g_nltp_espnow_bus.max_tx_frame_len,
                          (unsigned long)(s->in_use ? s->ep.pkts_tx : 0));
        }
    }
}

/* =========================================================================
 * 4. Adversarial Fuzzing & v1.3.2 Stray-Packet Stall Test Helpers
 * ========================================================================= */
static void send_forged_and_spoofed_burst(const uint8_t dst_mac[6], uint32_t count, bool prime_replay) {
    uint8_t pkt[280];
    uint8_t bad_key[32];
    nltp_key_from_psk("wrong-attacker-key-1337", bad_key);
    uint8_t good_key[32];
    nltp_key_from_psk(g_psk, good_key);
    uint8_t replay_saved[120];
    size_t replay_len = 0;

    if (prime_replay) {
        /* Prime one valid harmless PONG frame (seq=1) so subsequent replays hit RFC 6479 window */
        const uint8_t pong_pl[4] = {0, 0, 0, 0};
        replay_len = nltp_pack_encrypted_frame(replay_saved, good_key, NLTP_PT_PONG, 0, 0x77889900u,
                                               1ULL, millis(), pong_pl, sizeof(pong_pl));
        nltp_espnow_raw_send(dst_mac, replay_saved, replay_len, g_rate_mode, -55);
        delay(4);
    }

    for (uint32_t i = 0; i < count; ++i) {
        uint32_t kind = i % 5u;
        if (kind == 0) {
            /* 1. Unauthenticated Raw ESP-NOW v2 forged frame ('R','F') -> Raw ESP-NOW accepts it! */
            raw_v2_hdr_t rf = {};
            rf.m0 = 'R';
            rf.m1 = 'F';
            rf.run_id = (uint16_t)(i + 1u);
            rf.total_bytes = 64;
            memcpy(rf.final_dst, dst_mac, 6);
            memcpy(rf.orig_src, g_my_mac, 6);
            memcpy(pkt, &rf, sizeof(rf));
            nltp_espnow_raw_send(dst_mac, pkt, sizeof(rf) + 40u, g_rate_mode, -55);
        } else if (kind == 1) {
            /* 2. Plaintext NLTP downgrade attack */
            const uint8_t pl[16] = {1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 4, 0, 0, 0, 'X', 'Y'};
            nltp_pack_header(pkt, NLTP_PT_DATA_SHARD, 0, 0xDEADBEEFu, i, pl, sizeof(pl));
            memcpy(pkt + NLTP_HEADER_BYTES, pl, sizeof(pl));
            nltp_espnow_raw_send(dst_mac, pkt, NLTP_HEADER_BYTES + sizeof(pl), g_rate_mode, -55);
        } else if (kind == 2) {
            /* 3. Wrong-PSK encrypted NLTP frame */
            const uint8_t pl[16] = {9, 9, 9, 9, 0, 0, 0, 0, 1, 0, 4, 0, 0, 0, 'A', 'B'};
            size_t n = nltp_pack_encrypted_frame(pkt, bad_key, NLTP_PT_DATA_SHARD, 0, 0xBADCAFE0u + i, i + 1u, i, pl, sizeof(pl));
            nltp_espnow_raw_send(dst_mac, pkt, n, g_rate_mode, -55);
        } else if (kind == 3 || !prime_replay) {
            /* 4. Validly encrypted frame with 1-bit Poly1305 ciphertext flip */
            const uint8_t pl[16] = {2, 0, 0, 0, 0, 0, 0, 0, 1, 0, 4, 0, 0, 0, 'T', 'M'};
            size_t n = nltp_pack_encrypted_frame(pkt, good_key, NLTP_PT_DATA_SHARD, 0, 0x77889900u, 800000ULL + i, i, pl, sizeof(pl));
            pkt[n - 5u] ^= 0x40u;
            nltp_espnow_raw_send(dst_mac, pkt, n, g_rate_mode, -55);
        } else {
            /* 5. Exact RFC 6479 Replay of seq=1 */
            if (replay_len > 0) {
                nltp_espnow_raw_send(dst_mac, replay_saved, replay_len, g_rate_mode, -55);
            }
        }
        if ((i & 7u) == 7u) delayMicroseconds(250);
    }
}

/* Test v1.3.2 commit 3775512: send an AUTHENTICATED shard with a DIFFERENT `conn_id` mid-stream!
 * Before 3775512, this wiped the receiver's active stream; in v1.3.2 (`NLTP_RX_TAKEOVER_MS=1000`),
 * the receiver ignores the stray `conn_id` while its current stream is actively arriving! */
static void send_stray_conn_id_shard(const uint8_t dst_mac[6]) {
    uint8_t pkt[128];
    uint8_t good_key[32];
    nltp_key_from_psk(g_psk, good_key);
    /* Valid shard payload for stream_id=99, gen=0, k=1, idx=0, rw=6 */
    const uint8_t pl[18] = {
        99, 0, 0, 0,
        0, 0, 0, 0,
        1, 0, 6, 0,
        4, 0, 'S', 'T', 'R', 'Y'
    };
    uint32_t stray_cid = (esp_random() | 1u) ^ 0xA5A55A5Au;
    size_t n = nltp_pack_encrypted_frame(pkt, good_key, NLTP_PT_DATA_SHARD, 0, stray_cid,
                                         900000ULL + (esp_random() & 0xFFFFu), millis(),
                                         pl, sizeof(pl));
    nltp_espnow_raw_send(dst_mac, pkt, n, g_rate_mode, -55);
}

/* =========================================================================
 * 5. Serial Command Dispatcher
 * ========================================================================= */
static void handle_cmd(const char *cmd) {
    if (strcmp(cmd, "stat") == 0) {
        uint32_t total_auth_drops = 0, total_crc_drops = 0, active_slots = 0;
        for (uint32_t i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
            if (g_hub->slots[i].in_use) {
                active_slots++;
                total_auth_drops += g_hub->slots[i].ep.auth_drops;
                total_crc_drops += g_hub->slots[i].ep.crc_drops;
            }
        }
        int8_t actual_pwr = 0;
        esp_wifi_get_max_tx_power(&actual_pwr);
        Serial.printf("STAT node=%u wifi_sta=%d ip=%s enow_v=%lu mtu=%u ch=%u lr=%u rate=%d txpwr=%d(%.1fdBm) drop=%u enc=%u slots=%lu peers=%u evict=%lu preauth_drops=%lu auth_drops=%lu crc_drops=%lu raw_forged=%lu relayed=%lu mac=%02x:%02x:%02x:%02x:%02x:%02x heap=%u\n",
                      (unsigned)MY_NODE_ID,
                      (int)(WiFi.status() == WL_CONNECTED),
                      WiFi.localIP().toString().c_str(),
                      (unsigned long)g_nltp_espnow_bus.espnow_version,
                      (unsigned)NLTP_ESPNOW_MTU,
                      (unsigned)MESH_CHANNEL,
                      (unsigned)g_nltp_espnow_bus.lr_enabled,
                      (int)g_rate_mode,
                      (int)actual_pwr, actual_pwr * 0.25f,
                      (unsigned)g_drop_pct,
                      (unsigned)g_hub->encrypted,
                      (unsigned long)active_slots,
                      (unsigned)g_nltp_espnow_bus.peer_n,
                      (unsigned long)g_nltp_espnow_bus.peer_evictions,
                      (unsigned long)g_preauth_drops,
                      (unsigned long)total_auth_drops,
                      (unsigned long)total_crc_drops,
                      (unsigned long)g_raw.rx_forged_accepted,
                      (unsigned long)g_relayed_frames,
                      g_my_mac[0], g_my_mac[1], g_my_mac[2], g_my_mac[3], g_my_mac[4], g_my_mac[5],
                      (unsigned)ESP.getFreeHeap());
        for (int i = 0; i < 8; ++i) {
            if (g_neighbors[i].known) {
                Serial.printf("PEER node=%u mac=%02x:%02x:%02x:%02x:%02x:%02x rssi=%d noise=%d snr=%d pkts=%lu age_ms=%lu\n",
                              (unsigned)g_neighbors[i].node_id,
                              g_neighbors[i].mac[0], g_neighbors[i].mac[1], g_neighbors[i].mac[2],
                              g_neighbors[i].mac[3], g_neighbors[i].mac[4], g_neighbors[i].mac[5],
                              (int)g_neighbors[i].rssi,
                              (int)g_neighbors[i].noise,
                              (int)g_neighbors[i].rssi - (int)g_neighbors[i].noise,
                              (unsigned long)g_neighbors[i].pkts,
                              (unsigned long)(millis() - g_neighbors[i].last_ms));
            }
        }
        Serial.println("END_STAT");
    } else if (strcmp(cmd, "beacon") == 0) {
        emit_l2_beacon();
        Serial.println("OK beacon");
    } else if (strncmp(cmd, "rate ", 5) == 0) {
        int r = atoi(cmd + 5);
        if (r < 0 || r > 6) r = 4;
        g_rate_mode = (nltp_espnow_rate_t)r;
        int want_lr = (g_rate_mode == NLTP_ESPNOW_RATE_LR_250K || g_rate_mode == NLTP_ESPNOW_RATE_LR_500K);
        nltp_espnow_set_wifi_lr_protocol(WIFI_IF_STA, want_lr, MESH_CHANNEL);
        g_hub->rate_mode = g_rate_mode;
        for (uint32_t i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
            g_hub->slots[i].rate_mode = g_rate_mode;
            if (g_hub->slots[i].in_use) {
                nltp_espnow_apply_peer_rate(g_hub->slots[i].mac, g_rate_mode, g_hub->slots[i].last_rssi);
            }
        }
        for (int i = 0; i < 8; ++i) {
            if (g_neighbors[i].known) {
                nltp_espnow_ensure_peer(g_neighbors[i].mac, g_rate_mode, g_neighbors[i].rssi);
            }
        }
        Serial.printf("OK rate=%d lr=%u\n", (int)g_rate_mode, (unsigned)g_nltp_espnow_bus.lr_enabled);
    } else if (strncmp(cmd, "txpwr ", 6) == 0) {
        int p = atoi(cmd + 6);
        if (p < 8) p = 8;
        if (p > 78) p = 78;
        g_txpwr_qdbm = (int8_t)p;
        esp_wifi_set_max_tx_power(g_txpwr_qdbm);
        int8_t actual = 0;
        esp_wifi_get_max_tx_power(&actual);
        Serial.printf("OK txpwr=%d dbm=%.2f\n", (int)actual, actual * 0.25f);
    } else if (strncmp(cmd, "drop ", 5) == 0) {
        int d = atoi(cmd + 5);
        if (d < 0) d = 0;
        if (d > 95) d = 95;
        g_drop_pct = (uint8_t)d;
        Serial.printf("OK drop=%u\n", (unsigned)g_drop_pct);
    } else if (strncmp(cmd, "psk ", 4) == 0) {
        const char *arg = cmd + 4;
        if (strcmp(arg, "off") == 0) {
            g_hub->encrypted = 0u;
            memset(g_hub->key32, 0, 32);
            for (uint32_t i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
                if (g_hub->slots[i].in_use) {
                    g_hub->slots[i].ep.encrypted = 0u;
                    g_hub->slots[i].ep.replay_valid = 0u;
                }
            }
            Serial.println("OK psk=off");
        } else {
            snprintf(g_psk, sizeof(g_psk), "%.95s", arg);
            nltp_key_from_psk(g_psk, g_hub->key32);
            g_hub->encrypted = 1u;
            for (uint32_t i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
                if (g_hub->slots[i].in_use) {
                    nltp_set_key(&g_hub->slots[i].ep, g_hub->key32);
                    g_hub->slots[i].ep.replay_valid = 0u;
                }
            }
            Serial.println("OK psk=on");
        }
    } else if (strncmp(cmd, "l2relay ", 8) == 0) {
        /* l2relay off  OR  l2relay <macA> <macB> */
        const char *arg = cmd + 8;
        if (strcmp(arg, "off") == 0) {
            g_l2_nltp_relay_on = false;
            Serial.println("OK l2relay=off");
        } else {
            char t1[32] = {0}, t2[32] = {0};
            if (sscanf(arg, "%31s %31s", t1, t2) == 2 &&
                parse_mac_or_node(t1, g_relay_a_mac) &&
                parse_mac_or_node(t2, g_relay_b_mac)) {
                g_l2_nltp_relay_on = true;
                Serial.println("OK l2relay=on");
            } else {
                Serial.println("ERR l2relay_args");
            }
        }
    } else if (strncmp(cmd, "raw_send ", 9) == 0) {
        /* raw_send <dst> <bytes> [via_relay] */
        char t_dst[32] = {0}, t_via[32] = {0};
        uint32_t sz = 0;
        int n = sscanf(cmd + 9, "%31s %lu %31s", t_dst, (unsigned long *)&sz, t_via);
        uint8_t dst[6], via[6];
        if (n >= 2 && parse_mac_or_node(t_dst, dst)) {
            if (sz < 1u) sz = 1u;
            if (sz > MAX_TEST_BYTES) sz = MAX_TEST_BYTES;
            bool has_via = (n == 3 && parse_mac_or_node(t_via, via));
            execute_raw_v2_sync(dst, has_via ? via : nullptr, sz);
        } else {
            Serial.println("ERR raw_send_args");
        }
    } else if (strncmp(cmd, "raw_async ", 10) == 0) {
        /* raw_async <dst> <bytes> -> non-blocking Raw ESP-NOW v2 burst for full-duplex collision test */
        char t_dst[32] = {0};
        uint32_t sz = 0;
        uint8_t dst[6];
        if (sscanf(cmd + 10, "%31s %lu", t_dst, (unsigned long *)&sz) == 2 && parse_mac_or_node(t_dst, dst)) {
            if (sz < 1u) sz = 1u;
            if (sz > MAX_TEST_BYTES) sz = MAX_TEST_BYTES;
            uint32_t max_chunk = NLTP_ESPNOW_MTU - sizeof(raw_v2_hdr_t);
            uint16_t run_id = g_next_raw_run++;
            if (run_id == 0) run_id = g_next_raw_run++;
            g_raw.ack_got = false;
            g_nltp_espnow_bus.max_tx_frame_len = 0;
            memcpy(g_raw.async_dst, dst, 6);
            g_raw.async_use_relay = false;
            g_raw.async_run_id = run_id;
            g_raw.async_next_seq = 0;
            g_raw.async_total_pkts = (uint16_t)((sz + max_chunk - 1u) / max_chunk);
            g_raw.async_total_len = sz;
            g_raw.async_t0_us = micros();
            g_raw.async_tx_active = true;
            Serial.printf("OK raw_async_started len=%lu\n", (unsigned long)sz);
        } else {
            Serial.println("ERR raw_async_args");
        }
    } else if (strncmp(cmd, "nltp_send ", 10) == 0 || strncmp(cmd, "wake_send ", 10) == 0 || strncmp(cmd, "v1_send ", 8) == 0) {
        bool is_v1 = (strncmp(cmd, "v1_send ", 8) == 0);
        bool is_wake = is_v1 || (strncmp(cmd, "wake_send ", 10) == 0);
        const char *arg = is_v1 ? (cmd + 8) : (cmd + 10);
        char t_dst[32] = {0};
        uint32_t sz = 0;
        uint8_t dst[6];
        if (sscanf(arg, "%31s %lu", t_dst, (unsigned long *)&sz) == 2 && parse_mac_or_node(t_dst, dst)) {
            if (sz < 1u) sz = 1u;
            if (sz > MAX_TEST_BYTES) sz = MAX_TEST_BYTES;
            nltp_espnow_slot_t *slot = nltp_espnow_hub_get_or_create_slot(g_hub, dst, nltp_espnow_now_ms(NULL));
            if (!slot) {
                Serial.println("ERR hub_slots_busy");
                return;
            }
            if (is_wake) {
                /* Fresh deep-sleep wake: brand-new random conn_id & reset TX/replay state */
                uint32_t new_cid = esp_random() | 1u;
                nltp_init(&slot->ep, new_cid, g_hub->max_kbps,
                          nltp_espnow_slot_send_cb, nltp_espnow_now_ms, slot);
                slot->v1_mode = is_v1 ? 1u : 0u;
                nltp_set_shard_bytes(&slot->ep, is_v1 ? NLTP_ESPNOW_V1_SHARD_BYTES : (uint16_t)NLTP_SHARD_BYTES);
                nltp_set_rx_buffer(&slot->ep, slot->rx_buf, slot->rx_cap);
                if (g_hub->encrypted) nltp_set_key(&slot->ep, g_hub->key32);
            }
            fill_tx_payload(0x66000000u + sz, sz);
            g_nltp_espnow_bus.max_tx_frame_len = 0;
            g_max_nltp_rx_l2 = 0;
            int sidx = (int)(slot - &g_hub->slots[0]);
            uint32_t sid = nltp_send_buffer(&slot->ep, g_tx_payload, sz, "mesh3.bin");
            if (sid == 0) {
                Serial.println("ERR nltp_tx_busy");
            } else {
                g_nltp_tx_slot = sidx;
                g_nltp_tx_len = sz;
                g_nltp_tx_t0_us = micros();
                g_nltp_tx_active = true;
                Serial.printf("OK nltp_send_started len=%lu cid=0x%08lx\n",
                              (unsigned long)sz, (unsigned long)slot->ep.conn_id);
            }
        } else {
            Serial.println("ERR nltp_send_args");
        }
    } else if (strncmp(cmd, "stray_inject ", 13) == 0) {
        uint8_t dst[6];
        if (parse_mac_or_node(cmd + 13, dst)) {
            send_stray_conn_id_shard(dst);
            Serial.println("OK stray_injected");
        } else {
            Serial.println("ERR stray_args");
        }
    } else if (strncmp(cmd, "fuzz ", 5) == 0 || strncmp(cmd, "fuzz_unknown ", 13) == 0) {
        bool unk = (strncmp(cmd, "fuzz_unknown ", 13) == 0);
        const char *arg = unk ? (cmd + 13) : (cmd + 5);
        char t_dst[32] = {0};
        uint32_t cnt = 100;
        uint8_t dst[6];
        if (sscanf(arg, "%31s %lu", t_dst, (unsigned long *)&cnt) >= 1 && parse_mac_or_node(t_dst, dst)) {
            if (cnt > 500u) cnt = 500u;
            send_forged_and_spoofed_burst(dst, cnt, !unk);
            Serial.printf("OK fuzz_sent=%lu\n", (unsigned long)cnt);
        } else {
            Serial.println("ERR fuzz_args");
        }
    } else if (strcmp(cmd, "clear_slots") == 0) {
        for (uint32_t i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
            if (g_hub->slots[i].in_use) nltp_espnow_hub_release_slot(&g_hub->slots[i]);
        }
        Serial.println("OK clear_slots");
    } else if (strcmp(cmd, "reset_counts") == 0) {
        g_raw.rx_forged_accepted = 0;
        g_relayed_frames = 0;
        g_preauth_drops = 0;
        g_max_nltp_rx_l2 = 0;
        g_nltp_espnow_bus.max_tx_frame_len = 0;
        for (uint32_t i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
            g_hub->slots[i].ep.auth_drops = 0;
            g_hub->slots[i].ep.crc_drops = 0;
        }
        Serial.println("OK reset_counts");
    }
}

/* =========================================================================
 * 6. Arduino Setup & Main Loop (100% Off-Wi-Fi)
 * ========================================================================= */
void setup() {
    Serial.begin(115200);
    delay(250);

    /* Strictly OFF any Wi-Fi network / SoftAP / DHCP */
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.disconnect(true, true);
    esp_wifi_get_mac(WIFI_IF_STA, g_my_mac);

    g_relay_q = xQueueCreate(12, sizeof(relay_item_t));
    g_hub = (nltp_espnow_hub_t *)calloc(1, sizeof(nltp_espnow_hub_t));
    if (!g_hub || !g_tx_payload || !g_relay_q) {
        Serial.println("FATAL alloc_failed");
        for (;;) delay(1000);
    }

    esp_err_t err = nltp_espnow_hub_init(
        g_hub,
        MESH_CHANNEL,
        g_rate_mode,
        g_psk,
        NULL,
        MAX_TEST_BYTES,
        25000u,
        60000u
    );
    if (err != ESP_OK) {
        Serial.printf("FATAL hub_init err=%d\n", (int)err);
        for (;;) delay(1000);
    }
    /* Eagerly allocate both hub slots so concurrent multi-peer RX never hits fragmentation */
    g_hub->slots[0].rx_buf = (uint8_t *)malloc(MAX_TEST_BYTES);
    g_hub->slots[1].rx_buf = (uint8_t *)malloc(MAX_TEST_BYTES);
    if (!g_hub->slots[1].rx_buf) {
        g_hub->slots[1].rx_buf = (uint8_t *)malloc(16384u);
        g_hub->slots[1].rx_cap = 16384u;
    }

    g_nltp_espnow_bus.non_nltp_rx_cb = on_non_nltp_frame_isr;
    g_hub->on_message_cb = on_hub_message;
    esp_wifi_set_max_tx_power(g_txpwr_qdbm);

    emit_l2_beacon();
    Serial.printf("READY mesh3_node=%u wifi_sta=0 enow_v=%lu mtu=%u ch=%u mac=%02x:%02x:%02x:%02x:%02x:%02x heap=%u\n",
                  (unsigned)MY_NODE_ID,
                  (unsigned long)g_nltp_espnow_bus.espnow_version,
                  (unsigned)NLTP_ESPNOW_MTU,
                  (unsigned)MESH_CHANNEL,
                  g_my_mac[0], g_my_mac[1], g_my_mac[2], g_my_mac[3], g_my_mac[4], g_my_mac[5],
                  (unsigned)ESP.getFreeHeap());
}

void loop() {
    static char line[160];
    static size_t lpos = 0;

    while (Serial.available() > 0) {
        int ch = Serial.read();
        if (ch == '\r' || ch == '\n') {
            if (lpos > 0) {
                line[lpos] = '\0';
                lpos = 0;
                handle_cmd(line);
            }
        } else if (lpos + 1 < sizeof(line)) {
            line[lpos++] = (char)ch;
        }
    }

    poll_nltp_hub();
    poll_raw_v2();

    uint32_t now = millis();
    if (!g_nltp_tx_active && !g_raw.async_tx_active && (now - g_last_beacon_ms) >= 750u) {
        g_last_beacon_ms = now;
        emit_l2_beacon();
    }
    delayMicroseconds(250);
}
