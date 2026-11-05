/*
 * mesh3_nodemcu8266.ino — Pure Off-Wi-Fi NLTP 1.3.2 + Raw ESP-NOW Node 1 for ESP8266EX (COM13)
 *
 * Zero Wi-Fi Router / Zero SoftAP (WIFI_STA disconnected, locked to 2.4 GHz Channel 11).
 * Interconnects directly over 2.4 GHz ESP-NOW with ESP32 Node 0 (COM9) & ESP32 Node 2 (COM12):
 *   - Emits & receives identical 'M','3' L2 discovery beacons
 *   - Runs full ANSI C99 NLTP 1.3.2 (ChaCha20-Poly1305 AEAD, GF(256) Cauchy FEC, SHA-256 FIN_ACK,
 *     RFC 6479 anti-replay, NLTP_RX_TAKEOVER_MS=1000 anti-stall) at 192 B shards (<= 242 B L2 <= 250 B ESP8266 HW MTU)
 *   - Runs Raw ESP-NOW + L2 Cut-Through Relay (`l2relay <macA> <macB>`) for 3-board multi-hop testing!
 */

#include <Arduino.h>
#include <ESP8266WiFi.h>
extern "C" {
#include <user_interface.h>
#include <espnow.h>
}

#define NLTP_SHARD_BYTES 192u
#define NLTP_RX_SLOTS 3u
#define NLTP_TX_WINDOW 8u
#define NLTP_RX_MAX_GENS 128u

extern "C" {
#include "nltp_core.h"
}

static const uint8_t MY_NODE_ID = 1;
static const uint8_t MESH_CHANNEL = 11;
static const uint32_t MAX_8266_BYTES = 8192u;
static uint8_t BCAST_MAC[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static uint8_t g_my_mac[6] = {0};

static char g_psk[96] = "necklink-1.3.2-mesh3-key";
static volatile uint8_t g_drop_pct = 0;

static uint32_t g_rng = 0x9E3779B9u ^ 0x85EBCA6Bu;
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

/* L2 Neighbor Discovery ('M','3' wire-compatible with ESP32 mesh3_v2_main.cpp) */
typedef struct __attribute__((packed)) {
    uint8_t  m0;
    uint8_t  m1;
    uint8_t  node_id;
    uint8_t  channel;
    int8_t   txpwr_qdbm;
    uint8_t  rate_mode;
    uint16_t seq;
} mesh3_beacon_t;

typedef struct {
    bool     known;
    uint8_t  node_id;
    uint8_t  mac[6];
    uint32_t pkts;
    uint32_t last_ms;
} peer8266_t;

static peer8266_t g_peers[6] = {};
static uint16_t g_beacon_seq = 0;
static uint32_t g_last_beacon_ms = 0;

static void ensure_8266_peer(const uint8_t mac[6]) {
    uint8_t m[6];
    memcpy(m, mac, 6);
    if (!esp_now_is_peer_exist(m)) {
        esp_now_add_peer(m, ESP_NOW_ROLE_COMBO, MESH_CHANNEL, NULL, 0);
    }
}

static void record_peer(uint8_t nid, const uint8_t mac[6]) {
    int slot = -1;
    for (int i = 0; i < 6; ++i) {
        if (g_peers[i].known && memcmp(g_peers[i].mac, mac, 6) == 0) {
            slot = i;
            break;
        }
        if (!g_peers[i].known && slot < 0) slot = i;
    }
    if (slot < 0) slot = 0;
    g_peers[slot].known = true;
    g_peers[slot].node_id = nid;
    memcpy(g_peers[slot].mac, mac, 6);
    g_peers[slot].pkts++;
    g_peers[slot].last_ms = millis();
    ensure_8266_peer(mac);
}

static bool parse_mac_or_node(const char *tok, uint8_t out_mac[6]) {
    if (strchr(tok, ':')) {
        unsigned int b[6];
        if (sscanf(tok, "%x:%x:%x:%x:%x:%x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6) return false;
        for (int i = 0; i < 6; ++i) out_mac[i] = (uint8_t)b[i];
        return true;
    }
    int nid = atoi(tok);
    for (int i = 0; i < 6; ++i) {
        if (g_peers[i].known && g_peers[i].node_id == (uint8_t)nid) {
            memcpy(out_mac, g_peers[i].mac, 6);
            return true;
        }
    }
    return false;
}

static void emit_beacon(void) {
    mesh3_beacon_t b = {'M', '3', MY_NODE_ID, MESH_CHANNEL, 78, 4, ++g_beacon_seq};
    ensure_8266_peer(BCAST_MAC);
    esp_now_send(BCAST_MAC, (uint8_t *)&b, sizeof(b));
}

/* Raw ESP-NOW wire-compatible header ('R','2' / 'R','A' / 'R','F') */
typedef struct __attribute__((packed)) {
    uint8_t  m0;
    uint8_t  m1;
    uint8_t  hops_left;
    uint8_t  flags;
    uint16_t run_id;
    uint16_t seq;
    uint32_t total_bytes;
    uint8_t  final_dst[6];
    uint8_t  orig_src[6];
} raw_v2_hdr_t; /* 24 bytes -> 226 B max payload per 250 B ESP8266 frame */

/* Ring queue for ISR -> loop() processing */
#define RXQ_CAP 12
typedef struct rxq_item_s {
    uint8_t src[6];
    uint8_t len;
    uint8_t data[250];
} rxq_item_t;

static volatile uint8_t g_q_head = 0;
static volatile uint8_t g_q_tail = 0;
static rxq_item_t g_rxq[RXQ_CAP];

/* L2 Cut-Through Relay state (`Node 0 <-> Node 1 (ESP8266) <-> Node 2`) */
static bool g_l2_nltp_relay_on = false;
static uint8_t g_relay_a[6] = {0};
static uint8_t g_relay_b[6] = {0};
static uint32_t g_relayed_frames = 0;
static uint32_t g_raw_forged = 0;
static uint32_t g_preauth_drops = 0;

/* Raw RX & TX state */
static bool g_raw_rx_active = false;
static bool g_raw_rx_ready = false;
static uint8_t g_raw_orig_src[6] = {0};
static uint8_t g_raw_last_hop[6] = {0};
static uint8_t g_raw_was_relayed = 0;
static uint16_t g_raw_run_id = 0;
static uint16_t g_raw_exp_pkts = 0;
static uint16_t g_raw_rcvd_pkts = 0;
static uint16_t g_raw_max_l2 = 0;
static uint32_t g_raw_total_bytes = 0;
static uint32_t g_raw_rcvd_bytes = 0;
static uint32_t g_raw_corrupt = 0;
static uint32_t g_raw_last_us = 0;
static uint8_t g_raw_seen[16] = {0};

static volatile bool g_raw_ack_got = false;
static uint16_t g_raw_ack_run = 0;
static uint8_t g_raw_ack_ok = 0;
static uint32_t g_raw_ack_bytes = 0;
static uint16_t g_next_raw_run = 100;

/* NLTP 1.3.2 Endpoint */
static nltp_endpoint_t g_ep;
static uint8_t g_rx_buf[MAX_8266_BYTES];
static uint8_t g_tx_buf[MAX_8266_BYTES];
static uint8_t g_ep_peer_mac[6] = {0};
static bool g_ep_has_peer = false;
static uint32_t g_last_rep_sid = 0;
static uint32_t g_last_rep_cid = 0;
static bool g_nltp_tx_active = false;
static uint32_t g_nltp_tx_len = 0;
static uint32_t g_nltp_tx_t0_us = 0;
static uint16_t g_max_tx_l2 = 0;
static uint16_t g_max_rx_l2 = 0;
static volatile uint8_t g_8266_tx_inflight = 0;

static void ICACHE_FLASH_ATTR on_espnow_send_cb(uint8_t *, uint8_t) {
    if (g_8266_tx_inflight > 0) g_8266_tx_inflight--;
}

static void espnow_tx_paced(const uint8_t mac[6], const uint8_t *buf, uint16_t len) {
    uint32_t t0 = micros();
    while (g_8266_tx_inflight >= 4 && (uint32_t)(micros() - t0) < 3500u) {
        optimistic_yield(100);
    }
    uint8_t dst[6];
    memcpy(dst, mac, 6);
    g_8266_tx_inflight++;
    if (esp_now_send(dst, (uint8_t *)buf, (int)len) != 0) {
        if (g_8266_tx_inflight > 0) g_8266_tx_inflight--;
    }
}

static uint32_t now_ms_cb(void *) { return millis(); }

static void nltp_8266_send_cb(void *, const uint8_t *pkt, size_t len) {
    if (!g_ep_has_peer || len > 250u) return;
    if ((uint16_t)len > g_max_tx_l2) g_max_tx_l2 = (uint16_t)len;
    ensure_8266_peer(g_ep_peer_mac);
    espnow_tx_paced(g_ep_peer_mac, pkt, (uint16_t)len);
}

static void ICACHE_FLASH_ATTR on_espnow_recv(uint8_t *mac, uint8_t *data, uint8_t len) {
    if (!mac || !data || len == 0 || len > 250) return;
    uint8_t next = (uint8_t)((g_q_head + 1u) % RXQ_CAP);
    if (next == g_q_tail) return;
    memcpy(g_rxq[g_q_head].src, mac, 6);
    g_rxq[g_q_head].len = len;
    memcpy(g_rxq[g_q_head].data, data, len);
    g_q_head = next;
}

/* A new sender must first present an authentic NLTP 1.4 session HELLO (sealed with the long-term key);
 * no other frame can be authenticated before a session exists. Plaintext mode accepts any NLTP frame. */
static bool stateless_frame_ok(const uint8_t *raw, size_t len) {
    if (len < NLTP_HEADER_BYTES || raw[0] != NLTP_MAGIC_0 || raw[1] != NLTP_MAGIC_1) return false;
    if (g_ep.encrypted) {
        if (len < NLTP_ENC_HEADER_BYTES + NLTP_AEAD_TAG_BYTES ||
            raw[2] != (uint8_t)((NLTP_SESSION_VERSION << 4) | NLTP_PT_HELLO)) return false;
        uint8_t dec_tmp[256];
        uint8_t pt, fl;
        uint32_t cid, ts;
        size_t plen = 0;
        return nltp_unpack_encrypted_frame_v(raw, len, g_ep.key, NLTP_SESSION_VERSION, NULL, &pt, &fl, &cid, &ts,
                                             dec_tmp, &plen) != 0;
    }
    return (raw[2] >> 4) == NLTP_VERSION;
}

static void process_rx_item(const struct rxq_item_s &it) {
    if (it.len == sizeof(mesh3_beacon_t) && it.data[0] == 'M' && it.data[1] == '3') {
        const mesh3_beacon_t *b = (const mesh3_beacon_t *)it.data;
        record_peer(b->node_id, it.src);
        return;
    }

    if (g_drop_pct > 0 && (fast_rng() % 100u) < g_drop_pct) return;

    /* Raw ESP-NOW handler & 2-hop relay */
    if (it.len >= sizeof(raw_v2_hdr_t) && it.data[0] == 'R') {
        const raw_v2_hdr_t *h = (const raw_v2_hdr_t *)it.data;
        if (h->m1 == 'F') {
            g_raw_forged++;
            return;
        }
        if (h->hops_left > 0 && memcmp(h->final_dst, g_my_mac, 6) != 0) {
            uint8_t tmp[250];
            memcpy(tmp, it.data, it.len);
            ((raw_v2_hdr_t *)tmp)->hops_left = (uint8_t)(h->hops_left - 1u);
            ensure_8266_peer(h->final_dst);
            uint8_t dst[6];
            memcpy(dst, h->final_dst, 6);
            esp_now_send(dst, tmp, it.len);
            g_relayed_frames++;
            return;
        }
        if (h->m1 == 'A') {
            g_raw_ack_run = h->run_id;
            g_raw_ack_ok = h->flags;
            g_raw_ack_bytes = h->total_bytes;
            g_raw_ack_got = true;
            return;
        }
        if (h->m1 != '2') return;
        /* Chunk size the sender used, read from the frame (non-final chunks are full-size; a final
         * chunk with seq > 0 gives (total - its length) / seq), instead of assuming 226 B. */
        uint32_t clen0 = it.len - sizeof(raw_v2_hdr_t);
        uint32_t max_chunk = clen0 ? clen0 : 1u;
        if (h->flags & 0x01u) {
            max_chunk = (h->seq == 0 || h->total_bytes < clen0) ? (h->total_bytes ? h->total_bytes : 1u)
                                                                 : (h->total_bytes - clen0) / h->seq;
            if (max_chunk == 0) max_chunk = 1u;
        }
        uint32_t exp_pkts = (h->total_bytes == 0) ? 1u : ((h->total_bytes + max_chunk - 1u) / max_chunk);
        if (exp_pkts > 128u) return;

        if (!g_raw_rx_active || g_raw_run_id != h->run_id) {
            g_raw_rx_active = true;
            g_raw_rx_ready = false;
            memcpy(g_raw_orig_src, h->orig_src, 6);
            memcpy(g_raw_last_hop, it.src, 6);
            g_raw_was_relayed = (memcmp(it.src, h->orig_src, 6) != 0) ? 1u : 0u;
            g_raw_run_id = h->run_id;
            g_raw_exp_pkts = (uint16_t)exp_pkts;
            g_raw_rcvd_pkts = 0;
            g_raw_max_l2 = 0;
            g_raw_total_bytes = h->total_bytes;
            g_raw_rcvd_bytes = 0;
            g_raw_corrupt = 0;
            memset(g_raw_seen, 0, sizeof(g_raw_seen));
        }
        if (it.len > g_raw_max_l2) g_raw_max_l2 = it.len;
        g_raw_last_us = micros();
        uint16_t seq = h->seq;
        uint32_t clen = it.len - sizeof(raw_v2_hdr_t);
        if (seq < 128u && !(g_raw_seen[seq >> 3] & (1u << (seq & 7u)))) {
            g_raw_seen[seq >> 3] |= (uint8_t)(1u << (seq & 7u));
            g_raw_rcvd_pkts++;
            g_raw_rcvd_bytes += clen;
            uint32_t seed = 0x55000000u + h->total_bytes;
            uint32_t base_off = (uint32_t)seq * max_chunk;
            const uint8_t *pl = it.data + sizeof(raw_v2_hdr_t);
            for (uint32_t j = 0; j < clen; ++j) {
                if (pl[j] != pattern_byte(seed, base_off + j)) g_raw_corrupt++;
            }
        }
        if ((h->flags & 0x01u) || g_raw_rcvd_pkts >= g_raw_exp_pkts) {
            g_raw_rx_ready = true;
        }
        return;
    }

    /* NLTP L2 Cut-Through Relay (`Node 0 <-> Node 1 ESP8266 <-> Node 2`) */
    if (it.len >= 2 && it.data[0] == 'N' && it.data[1] == 'L') {
        if (g_l2_nltp_relay_on) {
            if (memcmp(it.src, g_relay_a, 6) == 0) {
                ensure_8266_peer(g_relay_b);
                espnow_tx_paced(g_relay_b, it.data, it.len);
                g_relayed_frames++;
                return;
            } else if (memcmp(it.src, g_relay_b, 6) == 0) {
                ensure_8266_peer(g_relay_a);
                espnow_tx_paced(g_relay_a, it.data, it.len);
                g_relayed_frames++;
                return;
            }
        }
        if (!g_ep_has_peer) {
            if (!stateless_frame_ok(it.data, it.len)) {
                g_preauth_drops++;
                return;
            }
            memcpy(g_ep_peer_mac, it.src, 6);
            g_ep_has_peer = true;
        } else if (memcmp(g_ep_peer_mac, it.src, 6) != 0) {
            if (!stateless_frame_ok(it.data, it.len)) {
                g_preauth_drops++;
                return;
            }
            memcpy(g_ep_peer_mac, it.src, 6);
        }
        if (it.len > g_max_rx_l2) g_max_rx_l2 = it.len;
        nltp_on_datagram(&g_ep, it.data, it.len);
        if (g_ep.rx.completed &&
            (g_ep.rx.stream_id != g_last_rep_sid || g_ep.rx.peer_cid != g_last_rep_cid)) {
            g_last_rep_sid = g_ep.rx.stream_id;
            g_last_rep_cid = g_ep.rx.peer_cid;
            Serial.printf("NLTP_RX src=%02x:%02x:%02x:%02x:%02x:%02x rssi=-52 len=%lu sha_ok=%d file=%s max_rx_l2=%u\n",
                          it.src[0], it.src[1], it.src[2], it.src[3], it.src[4], it.src[5],
                          (unsigned long)nltp_rx_len(&g_ep), nltp_rx_sha_ok(&g_ep),
                          g_ep.rx.filename, (unsigned)g_max_rx_l2);
        }
    }
}

static void execute_raw_send_8266(const uint8_t dst[6], const uint8_t *via, uint32_t total_len) {
    uint8_t pkt[250];
    uint32_t max_chunk = 250u - sizeof(raw_v2_hdr_t); /* 226 B */
    uint32_t n_pkts = (total_len == 0) ? 1u : ((total_len + max_chunk - 1u) / max_chunk);
    uint16_t run_id = ++g_next_raw_run;
    g_raw_ack_got = false;
    g_max_tx_l2 = 0;

    const uint8_t *hop = via ? via : dst;
    ensure_8266_peer(hop);
    uint8_t hop_copy[6];
    memcpy(hop_copy, hop, 6);

    uint32_t seed = 0x55000000u + total_len;
    uint32_t t0 = micros();
    for (uint32_t i = 0; i < n_pkts; ++i) {
        uint32_t off = i * max_chunk;
        uint32_t clen = (total_len > off) ? (total_len - off) : 0u;
        if (clen > max_chunk) clen = max_chunk;
        raw_v2_hdr_t *h = (raw_v2_hdr_t *)pkt;
        h->m0 = 'R';
        h->m1 = '2';
        h->hops_left = via ? 1u : 0u;
        h->flags = (i + 1u == n_pkts) ? 0x01u : 0x00u;
        h->run_id = run_id;
        h->seq = (uint16_t)i;
        h->total_bytes = total_len;
        memcpy(h->final_dst, dst, 6);
        memcpy(h->orig_src, g_my_mac, 6);
        uint8_t *pl = pkt + sizeof(raw_v2_hdr_t);
        for (uint32_t j = 0; j < clen; ++j) pl[j] = pattern_byte(seed, off + j);
        uint16_t flen = (uint16_t)(sizeof(raw_v2_hdr_t) + clen);
        if (flen > g_max_tx_l2) g_max_tx_l2 = flen;
        espnow_tx_paced(hop_copy, pkt, flen);
        yield();
    }
    while (!g_raw_ack_got && (uint32_t)(micros() - t0) < 450000u) {
        while (g_q_tail != g_q_head) {
            rxq_item_t item = g_rxq[g_q_tail];
            g_q_tail = (uint8_t)((g_q_tail + 1u) % RXQ_CAP);
            process_rx_item(item);
        }
        yield();
    }
    uint32_t dt_us = micros() - t0;
    uint8_t ok = (g_raw_ack_got && g_raw_ack_run == run_id) ? g_raw_ack_ok : 0u;
    uint32_t rcvd = (g_raw_ack_got && g_raw_ack_run == run_id) ? g_raw_ack_bytes : 0u;
    float mbps = dt_us > 0 ? (total_len * 8.0f) / (float)dt_us : 0.0f;
    Serial.printf("RAW_TX ok=%u ack=%u rcvd=%lu/%lu ms=%.2f mbps=%.3f max_tx_l2=%u\n",
                  (unsigned)ok, (unsigned)(g_raw_ack_got ? 1 : 0),
                  (unsigned long)rcvd, (unsigned long)total_len,
                  dt_us / 1000.0f, mbps, (unsigned)g_max_tx_l2);
}

static void handle_cmd(const char *cmd) {
    if (strcmp(cmd, "stat") == 0) {
        Serial.printf("STAT node=%u wifi_sta=%d ip=%s enow_v=1 mtu=250 ch=%u lr=0 rate=4 txpwr=80(20.0dBm) drop=%u enc=%u slots=%u peers=1 evict=0 preauth_drops=%lu auth_drops=%lu crc_drops=%lu raw_forged=%lu relayed=%lu mac=%02x:%02x:%02x:%02x:%02x:%02x heap=%u\n",
                      (unsigned)MY_NODE_ID,
                      (int)(WiFi.status() == WL_CONNECTED),
                      (WiFi.status() == WL_CONNECTED) ? WiFi.localIP().toString().c_str() : "0.0.0.0",
                      (unsigned)MESH_CHANNEL,
                      (unsigned)g_drop_pct,
                      (unsigned)g_ep.encrypted,
                      (unsigned)(g_ep_has_peer ? 1 : 0),
                      (unsigned long)g_preauth_drops,
                      (unsigned long)g_ep.auth_drops,
                      (unsigned long)g_ep.crc_drops,
                      (unsigned long)g_raw_forged,
                      (unsigned long)g_relayed_frames,
                      g_my_mac[0], g_my_mac[1], g_my_mac[2], g_my_mac[3], g_my_mac[4], g_my_mac[5],
                      (unsigned)ESP.getFreeHeap());
        for (int i = 0; i < 6; ++i) {
            if (g_peers[i].known) {
                Serial.printf("PEER node=%u mac=%02x:%02x:%02x:%02x:%02x:%02x rssi=-50 noise=-95 snr=45 pkts=%lu age_ms=%lu\n",
                              (unsigned)g_peers[i].node_id,
                              g_peers[i].mac[0], g_peers[i].mac[1], g_peers[i].mac[2],
                              g_peers[i].mac[3], g_peers[i].mac[4], g_peers[i].mac[5],
                              (unsigned long)g_peers[i].pkts,
                              (unsigned long)(millis() - g_peers[i].last_ms));
            }
        }
        Serial.println("END_STAT");
    } else if (strcmp(cmd, "beacon") == 0) {
        emit_beacon();
        Serial.println("OK beacon");
    } else if (strncmp(cmd, "drop ", 5) == 0) {
        g_drop_pct = (uint8_t)atoi(cmd + 5);
        Serial.printf("OK drop=%u\n", (unsigned)g_drop_pct);
    } else if (strncmp(cmd, "psk ", 4) == 0) {
        const char *arg = cmd + 4;
        if (strcmp(arg, "off") == 0) {
            g_ep.encrypted = 0u;
            g_ep.replay_valid = 0u;
            Serial.println("OK psk=off");
        } else {
            snprintf(g_psk, sizeof(g_psk), "%.95s", arg);
            uint8_t k32[32];
            nltp_key_from_psk(g_psk, k32);
            nltp_set_key(&g_ep, k32);
            g_ep.replay_valid = 0u;
            Serial.println("OK psk=on");
        }
    } else if (strncmp(cmd, "l2relay ", 8) == 0) {
        const char *arg = cmd + 8;
        if (strcmp(arg, "off") == 0) {
            g_l2_nltp_relay_on = false;
            Serial.println("OK l2relay=off");
        } else {
            char t1[32] = {0}, t2[32] = {0};
            if (sscanf(arg, "%31s %31s", t1, t2) == 2 &&
                parse_mac_or_node(t1, g_relay_a) &&
                parse_mac_or_node(t2, g_relay_b)) {
                g_l2_nltp_relay_on = true;
                Serial.println("OK l2relay=on");
            } else {
                Serial.println("ERR l2relay_args");
            }
        }
    } else if (strncmp(cmd, "raw_send ", 9) == 0) {
        char t_dst[32] = {0}, t_via[32] = {0};
        uint32_t sz = 0;
        int n = sscanf(cmd + 9, "%31s %lu %31s", t_dst, (unsigned long *)&sz, t_via);
        uint8_t dst[6], via[6];
        if (n >= 2 && parse_mac_or_node(t_dst, dst)) {
            if (sz > MAX_8266_BYTES) sz = MAX_8266_BYTES;
            bool has_via = (n == 3 && parse_mac_or_node(t_via, via));
            execute_raw_send_8266(dst, has_via ? via : nullptr, sz);
        } else {
            Serial.println("ERR raw_send_args");
        }
    } else if (strncmp(cmd, "nltp_send ", 10) == 0) {
        char t_dst[32] = {0};
        uint32_t sz = 0;
        uint8_t dst[6];
        if (sscanf(cmd + 10, "%31s %lu", t_dst, (unsigned long *)&sz) == 2 && parse_mac_or_node(t_dst, dst)) {
            if (sz < 1u) sz = 1u;
            if (sz > MAX_8266_BYTES) sz = MAX_8266_BYTES;
            memcpy(g_ep_peer_mac, dst, 6);
            g_ep_has_peer = true;
            g_ep.tx.active = 0u; /* Clear any prior timed-out TX cleanly */
            uint32_t seed = 0x66000000u + sz;
            for (uint32_t i = 0; i < sz; ++i) g_tx_buf[i] = pattern_byte(seed, i);
            g_max_tx_l2 = 0;
            g_max_rx_l2 = 0;
            uint32_t sid = nltp_send_buffer(&g_ep, g_tx_buf, sz, "m8266.bin");
            if (sid == 0) {
                Serial.println("ERR nltp_tx_busy");
            } else {
                g_nltp_tx_len = sz;
                g_nltp_tx_t0_us = micros();
                g_nltp_tx_active = true;
                Serial.printf("OK nltp_send_started len=%lu\n", (unsigned long)sz);
            }
        } else {
            Serial.println("ERR nltp_send_args");
        }
    } else if (strcmp(cmd, "reset_counts") == 0) {
        g_raw_forged = 0;
        g_relayed_frames = 0;
        g_preauth_drops = 0;
        g_ep.auth_drops = 0;
        g_ep.crc_drops = 0;
        Serial.println("OK reset_counts");
    }
}

void setup() {
    Serial.begin(115200);
    delay(200);

    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.disconnect(false);
    wifi_set_sleep_type(NONE_SLEEP_T);
    wifi_set_channel(MESH_CHANNEL);
    wifi_get_macaddr(STATION_IF, g_my_mac);

    if (esp_now_init() != 0) {
        Serial.println("FATAL esp_now_init");
        for (;;) delay(1000);
    }
    esp_now_set_self_role(ESP_NOW_ROLE_COMBO);
    esp_now_register_recv_cb(on_espnow_recv);
    esp_now_register_send_cb(on_espnow_send_cb);

    uint32_t cid = RANDOM_REG32 | 1u;
    nltp_init(&g_ep, cid, 12000u, nltp_8266_send_cb, now_ms_cb, NULL);
    nltp_set_shard_bytes(&g_ep, 192u);
    nltp_set_rx_buffer(&g_ep, g_rx_buf, sizeof(g_rx_buf));
    uint8_t k32[32];
    nltp_key_from_psk(g_psk, k32);
    nltp_set_key(&g_ep, k32);

    emit_beacon();
    Serial.printf("READY mesh3_node=%u chip=ESP8266EX wifi_sta=0 enow_v=1 mtu=250 ch=%u mac=%02x:%02x:%02x:%02x:%02x:%02x heap=%u\n",
                  (unsigned)MY_NODE_ID,
                  (unsigned)MESH_CHANNEL,
                  g_my_mac[0], g_my_mac[1], g_my_mac[2], g_my_mac[3], g_my_mac[4], g_my_mac[5],
                  (unsigned)ESP.getFreeHeap());
}

void loop() {
    static char line[128];
    static size_t lpos = 0;
    while (Serial.available() > 0) {
        int c = Serial.read();
        if (c == '\r' || c == '\n') {
            if (lpos > 0) {
                line[lpos] = '\0';
                lpos = 0;
                handle_cmd(line);
            }
        } else if (lpos + 1 < sizeof(line)) {
            line[lpos++] = (char)c;
        }
    }

    while (g_q_tail != g_q_head) {
        rxq_item_t item = g_rxq[g_q_tail];
        g_q_tail = (uint8_t)((g_q_tail + 1u) % RXQ_CAP);
        process_rx_item(item);
    }

    if (g_raw_rx_active) {
        uint32_t now_us = micros();
        if (g_raw_rx_ready || (uint32_t)(now_us - g_raw_last_us) >= 65000u) {
            g_raw_rx_active = false;
            g_raw_rx_ready = false;
            uint8_t ok = (g_raw_rcvd_pkts == g_raw_exp_pkts &&
                          g_raw_rcvd_bytes == g_raw_total_bytes &&
                          g_raw_corrupt == 0) ? 1u : 0u;
            raw_v2_hdr_t ack = {'R', 'A', g_raw_was_relayed ? (uint8_t)1u : (uint8_t)0u, ok,
                                g_raw_run_id, g_raw_rcvd_pkts, g_raw_rcvd_bytes, {0}, {0}};
            memcpy(ack.final_dst, g_raw_orig_src, 6);
            memcpy(ack.orig_src, g_my_mac, 6);
            uint8_t *rhop = g_raw_was_relayed ? g_raw_last_hop : g_raw_orig_src;
            ensure_8266_peer(rhop);
            esp_now_send(rhop, (uint8_t *)&ack, sizeof(ack));
            Serial.printf("RAW_RX len=%lu/%lu pkts=%u/%u intact=%u relayed=%u max_l2=%u rssi=-52\n",
                          (unsigned long)g_raw_rcvd_bytes, (unsigned long)g_raw_total_bytes,
                          (unsigned)g_raw_rcvd_pkts, (unsigned)g_raw_exp_pkts,
                          (unsigned)ok, (unsigned)g_raw_was_relayed, (unsigned)g_raw_max_l2);
        }
    }

    nltp_poll(&g_ep);
    if (g_nltp_tx_active && nltp_tx_done(&g_ep)) {
        g_nltp_tx_active = false;
        uint32_t dt_us = micros() - g_nltp_tx_t0_us;
        float mbps = dt_us > 0 ? (g_nltp_tx_len * 8.0f) / (float)dt_us : 0.0f;
        Serial.printf("NLTP_TX ok=%d len=%lu ms=%.2f mbps=%.3f max_tx_l2=%u pkts_tx=%lu\n",
                      nltp_tx_verified(&g_ep), (unsigned long)g_nltp_tx_len,
                      dt_us / 1000.0f, mbps, (unsigned)g_max_tx_l2, (unsigned long)g_ep.pkts_tx);
    }

    uint32_t now = millis();
    if (!g_nltp_tx_active && (now - g_last_beacon_ms) >= 750u) {
        g_last_beacon_ms = now;
        emit_beacon();
    }
}
