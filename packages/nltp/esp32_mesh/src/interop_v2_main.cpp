/*
 * interop_v2_main.cpp — Complete Hardware Benchmark, Cross-Version Interop & Torture-Stress Firmware
 * for Raw ESP-NOW v1 (250 B), Raw ESP-NOW v2 (1,470 B), NLTP over ESP-NOW v1 (242 B),
 * and NLTP over ESP-NOW v2 (1,010 B).
 *
 * Role 0 (`INTEROP_ROLE == 0`, COM9):
 *   - C99 `nltp_espnow_hub_t` multi-peer MAC-keyed bridge + allow-list + v1/v2 mode
 *   - Bare-metal Raw ESP-NOW v1 (250 B) & Raw ESP-NOW v2 (1,470 B) TX/RX comparator
 *   - Adversarial L2 radio fuzzer (`fuzz`) + RFC 1055 SLIP L2 tunnel for Host Python `pynltp`
 *
 * Role 1 (`INTEROP_ROLE == 1`, COM12):
 *   - Arduino C++ `NLTPESPNowNode` (`<NLTP.h>`) + v1/v2 mode + strict RX MTU filter
 *   - Bare-metal Raw ESP-NOW v1 (250 B) & Raw ESP-NOW v2 (1,470 B) TX/RX comparator
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include "NLTP.h"

#ifndef INTEROP_ROLE
#define INTEROP_ROLE 0
#endif

static const uint8_t DEFAULT_CH = 11;
static const uint32_t MAX_TEST_BYTES = 32u * 1024u;

/* RFC 1055 SLIP constants for transparent binary NLTP <-> Host Python interop */
static const uint8_t SLIP_END     = 0xC0;
static const uint8_t SLIP_ESC     = 0xDB;
static const uint8_t SLIP_ESC_END = 0xDC;
static const uint8_t SLIP_ESC_ESC = 0xDD;

static uint8_t *tx_pattern_buf = nullptr;
static uint32_t tx_started_us = 0;
static uint32_t tx_target_len = 0;
static bool tx_waiting_fin = false;
static uint8_t sim_drop_pct = 0;
static uint16_t rx_mtu_cap = NLTP_ESPNOW_MTU;
static uint32_t rx_mtu_drops = 0;
static uint32_t rng_state = 0xCAFEBABEu;

static inline uint32_t xorshift32() {
    uint32_t x = rng_state;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return (rng_state = x);
}

static void fill_deterministic(uint8_t *buf, uint32_t len, uint32_t seed) {
    uint32_t x = seed ? seed : 0x9E3779B9u;
    for (uint32_t i = 0; i < len; ++i) {
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        buf[i] = (uint8_t)x;
    }
}

static inline uint8_t raw_pattern_byte(uint32_t seed, uint32_t idx) {
    uint32_t h = seed ^ (idx * 0x9E3779B9u);
    h ^= h >> 16;
    h *= 0x85EBCA6Bu;
    h ^= h >> 13;
    return (uint8_t)h;
}

static bool parse_mac_str(const char *s, uint8_t out[6]) {
    unsigned int b[6];
    if (sscanf(s, "%02x:%02x:%02x:%02x:%02x:%02x",
               &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6) {
        return false;
    }
    for (int i = 0; i < 6; ++i) out[i] = (uint8_t)b[i];
    return true;
}

/* =========================================================================
 * Shared Bare-Metal Raw ESP-NOW v1 (250 B) & Raw ESP-NOW v2 (1,470 B) Engine
 * ========================================================================= */

typedef struct __attribute__((packed)) {
    uint8_t  m0;          /* 'R' */
    uint8_t  m1;          /* 'W' = data chunk, 'A' = completion echo */
    uint8_t  ver;         /* 1 (<=250B) or 2 (<=1470B) */
    uint8_t  flags;       /* 0x01 = FIN chunk; for 'A': hash_ok (0/1) */
    uint16_t run_id;
    uint16_t seq;         /* for 'A': rcvd_pkts */
    uint32_t total_bytes; /* for 'A': rcvd_bytes */
} raw_bench_hdr_t;

typedef struct {
    volatile bool     rx_active;
    volatile bool     rx_report_ready;
    uint8_t           rx_ver;
    uint8_t           rx_src_mac[6];
    int8_t            rx_rssi;
    uint16_t          rx_run_id;
    uint16_t          rx_expected_pkts;
    uint16_t          rx_rcvd_pkts;
    uint16_t          rx_max_l2;
    uint32_t          rx_total_bytes;
    uint32_t          rx_rcvd_bytes;
    uint32_t          rx_corrupt_bytes;
    uint32_t          rx_last_us;
    uint8_t           rx_seen[32]; /* up to 256 chunks */

    volatile bool     ack_got;
    uint16_t          ack_run_id;
    uint8_t           ack_ok;
    uint16_t          ack_pkts;
    uint32_t          ack_bytes;
} raw_bench_state_t;

static raw_bench_state_t g_raw = {};
static uint16_t g_next_raw_run = 1;

static void on_non_nltp_espnow_isr(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    if (!data || len < (int)sizeof(raw_bench_hdr_t)) return;
    if ((uint16_t)len > rx_mtu_cap) {
        rx_mtu_drops++;
        return;
    }
    if (data[0] != 'R') return;

    const raw_bench_hdr_t *h = (const raw_bench_hdr_t *)data;
    if (h->m1 == 'A') {
        g_raw.ack_run_id = h->run_id;
        g_raw.ack_ok     = h->flags;
        g_raw.ack_pkts   = h->seq;
        g_raw.ack_bytes  = h->total_bytes;
        g_raw.ack_got    = true;
        return;
    }
    if (h->m1 != 'W') return;

    /* Apply simulated wireless packet loss identically to NLTP */
    if (sim_drop_pct > 0 && (xorshift32() % 100u) < sim_drop_pct) return;

    uint32_t max_chunk = (h->ver == 1) ? (250u - sizeof(raw_bench_hdr_t)) : (1470u - sizeof(raw_bench_hdr_t));
    uint32_t exp_pkts = (h->total_bytes == 0) ? 1u : ((h->total_bytes + max_chunk - 1u) / max_chunk);
    if (exp_pkts > 256u) return;

    if (!g_raw.rx_active || g_raw.rx_run_id != h->run_id) {
        g_raw.rx_active = true;
        g_raw.rx_report_ready = false;
        g_raw.rx_ver = h->ver;
        if (info && info->src_addr) memcpy(g_raw.rx_src_mac, info->src_addr, 6);
        g_raw.rx_run_id = h->run_id;
        g_raw.rx_expected_pkts = (uint16_t)exp_pkts;
        g_raw.rx_rcvd_pkts = 0;
        g_raw.rx_max_l2 = 0;
        g_raw.rx_total_bytes = h->total_bytes;
        g_raw.rx_rcvd_bytes = 0;
        g_raw.rx_corrupt_bytes = 0;
        memset(g_raw.rx_seen, 0, sizeof(g_raw.rx_seen));
    }

    g_raw.rx_rssi = (info && info->rx_ctrl) ? (int8_t)info->rx_ctrl->rssi : -55;
    if ((uint16_t)len > g_raw.rx_max_l2) g_raw.rx_max_l2 = (uint16_t)len;
    g_raw.rx_last_us = (uint32_t)esp_timer_get_time();

    uint16_t seq = h->seq;
    uint32_t chunk_len = (uint32_t)len - (uint32_t)sizeof(raw_bench_hdr_t);
    if (seq < 256u && !(g_raw.rx_seen[seq >> 3] & (1u << (seq & 7u)))) {
        g_raw.rx_seen[seq >> 3] |= (uint8_t)(1u << (seq & 7u));
        g_raw.rx_rcvd_pkts++;
        g_raw.rx_rcvd_bytes += chunk_len;

        uint32_t seed = 0x77000000u + h->total_bytes;
        uint32_t base_off = (uint32_t)seq * max_chunk;
        const uint8_t *pl = data + sizeof(raw_bench_hdr_t);
        for (uint32_t j = 0; j < chunk_len; ++j) {
            if (pl[j] != raw_pattern_byte(seed, base_off + j)) {
                g_raw.rx_corrupt_bytes++;
            }
        }
    }

    if ((h->flags & 0x01u) || g_raw.rx_rcvd_pkts >= g_raw.rx_expected_pkts) {
        g_raw.rx_report_ready = true;
    }
}

static void poll_raw_bench(nltp_espnow_rate_t cur_rate) {
    if (!g_raw.rx_active) return;
    uint32_t now_us = (uint32_t)esp_timer_get_time();
    uint32_t tail_timeout_us = (cur_rate == NLTP_ESPNOW_RATE_LR_250K || cur_rate == NLTP_ESPNOW_RATE_LR_500K)
                                   ? 180000u
                                   : 45000u;
    if (!g_raw.rx_report_ready && (uint32_t)(now_us - g_raw.rx_last_us) < tail_timeout_us) {
        return;
    }

    g_raw.rx_active = false;
    g_raw.rx_report_ready = false;
    uint8_t ok = (g_raw.rx_rcvd_pkts == g_raw.rx_expected_pkts &&
                  g_raw.rx_rcvd_bytes == g_raw.rx_total_bytes &&
                  g_raw.rx_corrupt_bytes == 0)
                     ? 1u
                     : 0u;

    /* Send 12-byte raw completion echo back to sender */
    raw_bench_hdr_t ack = {};
    ack.m0 = 'R';
    ack.m1 = 'A';
    ack.ver = g_raw.rx_ver;
    ack.flags = ok;
    ack.run_id = g_raw.rx_run_id;
    ack.seq = g_raw.rx_rcvd_pkts;
    ack.total_bytes = g_raw.rx_rcvd_bytes;
    nltp_espnow_raw_send(g_raw.rx_src_mac, (const uint8_t *)&ack, sizeof(ack), cur_rate, g_raw.rx_rssi);

    Serial.printf("RAW_RX v=%u len=%lu/%lu pkts=%u/%u hash_ok=%u max_l2=%u rssi=%d\n",
                  (unsigned)g_raw.rx_ver,
                  (unsigned long)g_raw.rx_rcvd_bytes,
                  (unsigned long)g_raw.rx_total_bytes,
                  (unsigned)g_raw.rx_rcvd_pkts,
                  (unsigned)g_raw.rx_expected_pkts,
                  (unsigned)ok,
                  (unsigned)g_raw.rx_max_l2,
                  (int)g_raw.rx_rssi);
}

static void execute_raw_send(uint8_t ver, const uint8_t dst_mac[6], uint32_t total_len, nltp_espnow_rate_t rate_mode) {
    static uint8_t pkt_buf[1470];
    uint32_t max_frame = (ver == 1) ? 250u : 1470u;
    uint32_t max_chunk = max_frame - (uint32_t)sizeof(raw_bench_hdr_t);
    uint32_t n_pkts = (total_len == 0) ? 1u : ((total_len + max_chunk - 1u) / max_chunk);
    uint16_t run_id = g_next_raw_run++;
    if (run_id == 0) run_id = g_next_raw_run++;

    g_raw.ack_got = false;
    g_nltp_espnow_bus.max_tx_frame_len = 0;

    uint32_t seed = 0x77000000u + total_len;
    uint32_t t0_us = micros();

    for (uint32_t i = 0; i < n_pkts; ++i) {
        uint32_t off = i * max_chunk;
        uint32_t clen = (total_len > off) ? (total_len - off) : 0u;
        if (clen > max_chunk) clen = max_chunk;

        raw_bench_hdr_t *h = (raw_bench_hdr_t *)pkt_buf;
        h->m0 = 'R';
        h->m1 = 'W';
        h->ver = ver;
        h->flags = (i + 1u == n_pkts) ? 0x01u : 0x00u;
        h->run_id = run_id;
        h->seq = (uint16_t)i;
        h->total_bytes = total_len;

        uint8_t *pl = pkt_buf + sizeof(raw_bench_hdr_t);
        for (uint32_t j = 0; j < clen; ++j) {
            pl[j] = raw_pattern_byte(seed, off + j);
        }
        nltp_espnow_raw_send(dst_mac, pkt_buf, sizeof(raw_bench_hdr_t) + clen, rate_mode, -55);
    }

    uint32_t wait_limit_us = (rate_mode == NLTP_ESPNOW_RATE_LR_250K || rate_mode == NLTP_ESPNOW_RATE_LR_500K)
                                 ? 1500000u
                                 : 350000u;
    while (!g_raw.ack_got && (uint32_t)(micros() - t0_us) < wait_limit_us) {
        delayMicroseconds(150);
    }
    uint32_t dt_us = micros() - t0_us;
    uint8_t ok = (g_raw.ack_got && g_raw.ack_run_id == run_id) ? g_raw.ack_ok : 0u;
    uint32_t rcvd = (g_raw.ack_got && g_raw.ack_run_id == run_id) ? g_raw.ack_bytes : 0u;
    float mbps = dt_us > 0 ? (total_len * 8.0f) / (float)dt_us : 0.0f;

    Serial.printf("RAW_TX v=%u ok=%u ack=%u rcvd=%lu/%lu ms=%.2f mbps=%.3f max_tx_l2=%u\n",
                  (unsigned)ver,
                  (unsigned)ok,
                  (unsigned)(g_raw.ack_got ? 1 : 0),
                  (unsigned long)rcvd,
                  (unsigned long)total_len,
                  dt_us / 1000.0f,
                  mbps,
                  (unsigned)g_nltp_espnow_bus.max_tx_frame_len);
}

/* =========================================================================
 * ROLE 0 (COM9): C99 `nltp_espnow_hub_t` Bridge + Fuzzer + SLIP Tunnel
 * ========================================================================= */
#if INTEROP_ROLE == 0

static nltp_espnow_hub_t *g_hub = nullptr;
static const uint32_t HUB_SLOT_CAP = 32u * 1024u;
static const uint32_t ACTIVE_POOL_SLOTS = 2u;

static bool g_tunnel_mode = false;
static uint8_t g_tunnel_peer_mac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static nltp_espnow_rate_t g_rate_mode = NLTP_ESPNOW_RATE_AUTO;
static char g_psk[96] = "necklink-v2-interop-key";

static bool g_allow_filter_enabled = false;
static uint8_t g_allowed_mac[6] = {0};
static uint32_t g_blocked_drops = 0;
static uint16_t g_max_rx_frame_len = 0;

static int bridge_allow_cb(void *, const uint8_t src_mac[6]) {
    if (!g_allow_filter_enabled) return 1;
    if (memcmp(src_mac, g_allowed_mac, 6) == 0) return 1;
    g_blocked_drops++;
    return 0;
}

static void bridge_on_msg_cb(void *, const uint8_t src_mac[6], int8_t rssi,
                             const uint8_t *data, uint32_t len,
                             const char *filename, int sha_ok) {
    Serial.printf("HUB_RX mac=%02x:%02x:%02x:%02x:%02x:%02x rssi=%d len=%lu sha_ok=%d file=%s max_l2=%u max_tx_l2=%u\n",
                  src_mac[0], src_mac[1], src_mac[2], src_mac[3], src_mac[4], src_mac[5],
                  (int)rssi, (unsigned long)len, sha_ok, filename ? filename : "",
                  (unsigned)g_max_rx_frame_len, (unsigned)g_nltp_espnow_bus.max_tx_frame_len);
    (void)data;
}

static void slip_write_frame(const uint8_t *pkt, size_t len) {
    Serial.write(SLIP_END);
    for (size_t i = 0; i < len; ++i) {
        uint8_t b = pkt[i];
        if (b == SLIP_END) {
            Serial.write(SLIP_ESC);
            Serial.write(SLIP_ESC_END);
        } else if (b == SLIP_ESC) {
            Serial.write(SLIP_ESC);
            Serial.write(SLIP_ESC_ESC);
        } else {
            Serial.write(b);
        }
    }
    Serial.write(SLIP_END);
}

/* Transmits `count` adversarial L2 frames over real 2.4 GHz ESP-NOW radio */
static void send_adversarial_fuzz_burst(const uint8_t dst_mac[6], uint32_t count) {
    uint8_t pkt[320];
    uint8_t bad_key[32];
    nltp_key_from_psk("attacker-wrong-psk-9999", bad_key);
    uint8_t good_key[32];
    nltp_key_from_psk(g_psk, good_key);

    uint8_t saved_replay[128];
    size_t saved_replay_len = 0;

    for (uint32_t i = 0; i < count; ++i) {
        uint32_t kind = i % 6u;
        if (kind == 0) {
            /* 1. Truncated NLTP header (< 16 B) */
            const uint8_t trunc[9] = {'N', 'L', 0x15, 0x80, 1, 2, 3, 4, 5};
            nltp_espnow_raw_send(dst_mac, trunc, sizeof(trunc), g_rate_mode, -55);
        } else if (kind == 1) {
            /* 2. Unsupported NLTP version (v2 header nibble = 0x20) */
            memset(pkt, 0xAA, 40);
            pkt[0] = 'N'; pkt[1] = 'L'; pkt[2] = 0x25;
            nltp_espnow_raw_send(dst_mac, pkt, 40, g_rate_mode, -55);
        } else if (kind == 2) {
            /* 3. Plaintext downgrade frame (valid CRC32 header, unencrypted!) */
            uint8_t pl[8] = {1, 0, 0, 0, 0, 0, 0, 0};
            memcpy(pkt + NLTP_HEADER_BYTES, pl, sizeof(pl));
            nltp_pack_header(pkt, NLTP_PT_PING, 0, 0xDEAD0001u, (uint32_t)millis(), pkt + NLTP_HEADER_BYTES, sizeof(pl));
            nltp_espnow_raw_send(dst_mac, pkt, NLTP_HEADER_BYTES + sizeof(pl), g_rate_mode, -55);
        } else if (kind == 3) {
            /* 4. Wrong-key ChaCha20-Poly1305 AEAD frame */
            uint8_t pl[24] = {0};
            memcpy(pkt + NLTP_ENC_HEADER_BYTES, pl, sizeof(pl));
            size_t n = nltp_pack_encrypted_frame(pkt, bad_key, NLTP_PT_DATA_SHARD, 0, 0xBAD00001u,
                                                 100u + i, (uint32_t)millis(), pkt + NLTP_ENC_HEADER_BYTES, sizeof(pl));
            nltp_espnow_raw_send(dst_mac, pkt, n, g_rate_mode, -55);
        } else if (kind == 4) {
            /* 5. Good-key AEAD frame with 1 bit flipped in ciphertext! */
            uint8_t pl[24] = {'T', 'A', 'M', 'P', 'E', 'R'};
            memcpy(pkt + NLTP_ENC_HEADER_BYTES, pl, sizeof(pl));
            size_t n = nltp_pack_encrypted_frame(pkt, good_key, NLTP_PT_PING, 0, 0xBEEF1111u,
                                                 500u + i, (uint32_t)millis(), pkt + NLTP_ENC_HEADER_BYTES, sizeof(pl));
            pkt[NLTP_ENC_HEADER_BYTES + 3] ^= 0x04u;
            nltp_espnow_raw_send(dst_mac, pkt, n, g_rate_mode, -55);
        } else {
            /* 6. Replay attack: send a valid PING once, then replay exact same (conn_id, seq64) */
            if (saved_replay_len == 0) {
                uint8_t pl[4] = {1, 2, 3, 4};
                memcpy(saved_replay + NLTP_ENC_HEADER_BYTES, pl, 4);
                saved_replay_len = nltp_pack_encrypted_frame(saved_replay, good_key, NLTP_PT_PING, 0, 0xCAFE7777u,
                                                             1u, (uint32_t)millis(), saved_replay + NLTP_ENC_HEADER_BYTES, 4);
                /* Send original + immediate duplicate replay */
                nltp_espnow_raw_send(dst_mac, saved_replay, saved_replay_len, g_rate_mode, -55);
            }
            nltp_espnow_raw_send(dst_mac, saved_replay, saved_replay_len, g_rate_mode, -55);
        }
    }
    Serial.printf("OK fuzz_sent=%lu\n", (unsigned long)count);
}

static void handle_line_cmd(const char *cmd) {
    if (strncmp(cmd, "stat", 4) == 0) {
        uint32_t auth_d = 0, crc_d = 0;
        for (uint32_t i = 0; i < ACTIVE_POOL_SLOTS; ++i) {
            if (g_hub->slots[i].in_use) {
                auth_d += g_hub->slots[i].ep.auth_drops;
                crc_d  += g_hub->slots[i].ep.crc_drops;
            }
        }
        Serial.printf("STAT role=bridge_c99 enow_v=%lu mtu=%u v1=%u rxmtu=%u ch=%u lr=%u mode=%s rate=%d enc=%u auth_drops=%lu crc_drops=%lu mtu_drops=%lu blocked=%lu max_l2=%u max_tx_l2=%u mac=%s heap=%u\n",
                      (unsigned long)g_nltp_espnow_bus.espnow_version,
                      (unsigned)NLTP_ESPNOW_MTU,
                      (unsigned)g_hub->v1_mode,
                      (unsigned)rx_mtu_cap,
                      (unsigned)g_nltp_espnow_bus.channel,
                      (unsigned)g_nltp_espnow_bus.lr_enabled,
                      g_tunnel_mode ? "slip_tunnel" : "c99_hub",
                      (int)g_rate_mode,
                      (unsigned)g_hub->encrypted,
                      (unsigned long)auth_d,
                      (unsigned long)crc_d,
                      (unsigned long)rx_mtu_drops,
                      (unsigned long)g_blocked_drops,
                      (unsigned)g_max_rx_frame_len,
                      (unsigned)g_nltp_espnow_bus.max_tx_frame_len,
                      WiFi.macAddress().c_str(),
                      (unsigned)ESP.getFreeHeap());
    } else if (strncmp(cmd, "reset_max", 9) == 0) {
        g_max_rx_frame_len = 0;
        g_nltp_espnow_bus.max_tx_frame_len = 0;
        rx_mtu_drops = 0;
        Serial.println("OK reset_max");
    } else if (strncmp(cmd, "v1mode ", 7) == 0) {
        int en = atoi(cmd + 7);
        nltp_espnow_hub_set_v1_mode(g_hub, en);
        g_max_rx_frame_len = 0;
        g_nltp_espnow_bus.max_tx_frame_len = 0;
        Serial.printf("OK v1mode=%d\n", en ? 1 : 0);
    } else if (strncmp(cmd, "rxmtu ", 6) == 0) {
        rx_mtu_cap = (uint16_t)atoi(cmd + 6);
        rx_mtu_drops = 0;
        Serial.printf("OK rxmtu=%u\n", (unsigned)rx_mtu_cap);
    } else if (strncmp(cmd, "rate ", 5) == 0) {
        int r = atoi(cmd + 5);
        g_rate_mode = (nltp_espnow_rate_t)r;
        int want_lr = (g_rate_mode == NLTP_ESPNOW_RATE_LR_250K || g_rate_mode == NLTP_ESPNOW_RATE_LR_500K);
        nltp_espnow_set_wifi_lr_protocol(WIFI_IF_STA, want_lr, DEFAULT_CH);
        g_hub->rate_mode = g_rate_mode;
        for (uint32_t i = 0; i < ACTIVE_POOL_SLOTS; ++i) {
            g_hub->slots[i].rate_mode = g_rate_mode;
            if (g_hub->slots[i].in_use) {
                nltp_espnow_apply_peer_rate(g_hub->slots[i].mac, g_rate_mode, g_hub->slots[i].last_rssi);
            }
        }
        nltp_espnow_apply_peer_rate(g_tunnel_peer_mac, g_rate_mode, -55);
        Serial.printf("OK rate=%d lr=%u\n", (int)g_rate_mode, (unsigned)g_nltp_espnow_bus.lr_enabled);
    } else if (strncmp(cmd, "psk ", 4) == 0) {
        const char *arg = cmd + 4;
        if (strcmp(arg, "off") == 0) {
            g_psk[0] = '\0';
            g_hub->encrypted = 0u;
            memset(g_hub->key32, 0, 32);
            for (uint32_t i = 0; i < ACTIVE_POOL_SLOTS; ++i) {
                if (g_hub->slots[i].in_use) nltp_set_key(&g_hub->slots[i].ep, nullptr);
            }
            Serial.println("OK psk=off");
        } else {
            snprintf(g_psk, sizeof(g_psk), "%s", arg);
            nltp_key_from_psk(g_psk, g_hub->key32);
            g_hub->encrypted = 1u;
            for (uint32_t i = 0; i < ACTIVE_POOL_SLOTS; ++i) {
                if (g_hub->slots[i].in_use) nltp_set_key(&g_hub->slots[i].ep, g_hub->key32);
            }
            Serial.println("OK psk=on");
        }
    } else if (strncmp(cmd, "allow ", 6) == 0) {
        const char *arg = cmd + 6;
        if (strcmp(arg, "all") == 0) {
            g_allow_filter_enabled = false;
            Serial.println("OK allow=all");
        } else if (parse_mac_str(arg, g_allowed_mac)) {
            g_allow_filter_enabled = true;
            g_blocked_drops = 0;
            Serial.printf("OK allow=%s\n", arg);
        } else {
            Serial.println("ERROR bad_mac");
        }
    } else if (strncmp(cmd, "drop ", 5) == 0) {
        sim_drop_pct = (uint8_t)atoi(cmd + 5);
        Serial.printf("OK drop=%u\n", (unsigned)sim_drop_pct);
    } else if (strncmp(cmd, "cancel_tx", 9) == 0) {
        for (uint32_t i = 0; i < ACTIVE_POOL_SLOTS; ++i) {
            if (g_hub->slots[i].in_use && g_hub->slots[i].ep.tx.active && !g_hub->slots[i].ep.tx.completed) {
                g_hub->slots[i].ep.tx.completed = 1;
                g_hub->slots[i].ep.tx.verified = 0;
            }
        }
        tx_waiting_fin = false;
        Serial.println("OK cancel_tx");
    } else if (strncmp(cmd, "hub_send ", 9) == 0) {
        char mac_s[24] = {0};
        unsigned long len = 4096;
        if (sscanf(cmd + 9, "%23s %lu", mac_s, &len) == 2 && len > 0 && len <= MAX_TEST_BYTES) {
            uint8_t mac[6];
            if (parse_mac_str(mac_s, mac)) {
                nltp_espnow_slot_t *slot = nltp_espnow_hub_get_or_create_slot(g_hub, mac, nltp_espnow_now_ms(nullptr));
                if (!slot) { Serial.println("ERROR slot_oom"); return; }
                nltp_espnow_ensure_peer(mac, g_rate_mode, slot->last_rssi);
                if (slot->ep.tx.active && !slot->ep.tx.completed) {
                    slot->ep.tx.completed = 1;
                    slot->ep.tx.verified = 0;
                }
                g_nltp_espnow_bus.max_tx_frame_len = 0;
                fill_deterministic(tx_pattern_buf, (uint32_t)len, 0x13572468u + (uint32_t)len);
                tx_target_len = (uint32_t)len;
                tx_started_us = micros();
                tx_waiting_fin = true;
                nltp_send_buffer(&slot->ep, tx_pattern_buf, (uint32_t)len, "c99_to_ard.bin");
                Serial.printf("OK hub_send_started len=%lu\n", len);
            }
        }
    } else if (strncmp(cmd, "raw_send ", 9) == 0) {
        unsigned int ver = 2;
        char mac_s[24] = {0};
        unsigned long len = 4096;
        if (sscanf(cmd + 9, "%u %23s %lu", &ver, mac_s, &len) == 3 && (ver == 1 || ver == 2) && len <= MAX_TEST_BYTES) {
            uint8_t mac[6];
            if (parse_mac_str(mac_s, mac)) {
                execute_raw_send((uint8_t)ver, mac, (uint32_t)len, g_rate_mode);
            }
        }
    } else if (strncmp(cmd, "fuzz ", 5) == 0) {
        char mac_s[24] = {0};
        unsigned long cnt = 60;
        if (sscanf(cmd + 5, "%23s %lu", mac_s, &cnt) == 2) {
            uint8_t mac[6];
            if (parse_mac_str(mac_s, mac)) {
                send_adversarial_fuzz_burst(mac, (uint32_t)cnt);
            }
        }
    } else if (strncmp(cmd, "tunnel_on ", 10) == 0) {
        if (parse_mac_str(cmd + 10, g_tunnel_peer_mac)) {
            nltp_espnow_ensure_peer(g_tunnel_peer_mac, g_rate_mode, -55);
            g_max_rx_frame_len = 0;
            g_nltp_espnow_bus.max_tx_frame_len = 0;
            Serial.printf("OK tunnel=on peer=%s\n", cmd + 10);
            Serial.flush();
            g_tunnel_mode = true;
        } else {
            Serial.println("ERROR bad_mac");
        }
    } else if (strncmp(cmd, "tunnel_off", 10) == 0) {
        g_tunnel_mode = false;
        Serial.println("OK tunnel=off");
    }
}

void setup() {
    Serial.setRxBufferSize(4096);
    Serial.setTxBufferSize(4096);
    Serial.begin(115200);
    delay(250);
    rng_state ^= esp_random();

    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.disconnect(false, true);

    g_hub = (nltp_espnow_hub_t *)calloc(1, sizeof(nltp_espnow_hub_t));
    tx_pattern_buf = (uint8_t *)malloc(MAX_TEST_BYTES);
    if (!g_hub || !tx_pattern_buf) {
        Serial.printf("ERROR oom heap=%u\n", (unsigned)ESP.getFreeHeap());
        for (;;) delay(1000);
    }

    esp_err_t rc = nltp_espnow_hub_init(g_hub, DEFAULT_CH, g_rate_mode, g_psk, nullptr, HUB_SLOT_CAP, 25000u, 45000u);
    if (rc != ESP_OK) {
        Serial.printf("ERROR hub_init rc=0x%x heap=%u\n", (unsigned)rc, (unsigned)ESP.getFreeHeap());
        for (;;) delay(1000);
    }
    g_nltp_espnow_bus.non_nltp_rx_cb = on_non_nltp_espnow_isr;
    g_hub->allow_cb = bridge_allow_cb;
    g_hub->on_message_cb = bridge_on_msg_cb;

    Serial.printf("READY role=bridge_c99 enow_v=%lu mtu=%u ch=%u mac=%s heap=%u\n",
                  (unsigned long)g_nltp_espnow_bus.espnow_version,
                  (unsigned)NLTP_ESPNOW_MTU,
                  (unsigned)DEFAULT_CH,
                  WiFi.macAddress().c_str(),
                  (unsigned)ESP.getFreeHeap());
}

void loop() {
    static uint8_t serial_buf[1600];
    static size_t serial_len = 0;
    static bool slip_esc = false;

    poll_raw_bench(g_rate_mode);

    if (g_tunnel_mode) {
        nltp_espnow_rx_item_t item;
        if (Serial.availableForWrite() >= 2100 &&
            g_nltp_espnow_bus.rx_q &&
            xQueueReceive(g_nltp_espnow_bus.rx_q, &item, 0) == pdTRUE) {
            if (item.len <= rx_mtu_cap) {
                if (item.len > g_max_rx_frame_len) g_max_rx_frame_len = item.len;
                if (!(sim_drop_pct > 0 && (xorshift32() % 100u) < sim_drop_pct)) {
                    slip_write_frame(item.data, item.len);
                }
            } else {
                rx_mtu_drops++;
            }
        }

        while (Serial.available()) {
            uint8_t b = (uint8_t)Serial.read();
            if (b == SLIP_END) {
                if (serial_len > 0) {
                    if (serial_len == 11 && memcmp(serial_buf, "!tunnel_off", 11) == 0) {
                        g_tunnel_mode = false;
                        serial_len = 0;
                        slip_esc = false;
                        Serial.println("\nOK tunnel=off");
                        break;
                    }
                    if (serial_len >= NLTP_HEADER_BYTES && serial_buf[0] == 'N' && serial_buf[1] == 'L') {
                        nltp_espnow_raw_send(g_tunnel_peer_mac, serial_buf, serial_len, g_rate_mode, -55);
                    }
                    serial_len = 0;
                }
                slip_esc = false;
            } else if (slip_esc) {
                if (b == SLIP_ESC_END && serial_len < sizeof(serial_buf)) serial_buf[serial_len++] = SLIP_END;
                else if (b == SLIP_ESC_ESC && serial_len < sizeof(serial_buf)) serial_buf[serial_len++] = SLIP_ESC;
                slip_esc = false;
            } else if (b == SLIP_ESC) {
                slip_esc = true;
            } else if (serial_len < sizeof(serial_buf)) {
                serial_buf[serial_len++] = b;
            }
        }
        return;
    }

    nltp_espnow_rx_item_t peek_item;
    while (g_nltp_espnow_bus.rx_q && xQueueReceive(g_nltp_espnow_bus.rx_q, &peek_item, 0) == pdTRUE) {
        if (peek_item.len > rx_mtu_cap) {
            rx_mtu_drops++;
            continue;
        }
        if (peek_item.len > g_max_rx_frame_len) g_max_rx_frame_len = peek_item.len;
        if (sim_drop_pct > 0 && (xorshift32() % 100u) < sim_drop_pct) continue;

        uint32_t now_ms = nltp_espnow_now_ms(nullptr);
        if (g_hub->allow_cb && !g_hub->allow_cb(g_hub->user, peek_item.src_mac)) continue;
        nltp_espnow_slot_t *slot = nltp_espnow_hub_get_or_create_slot(g_hub, peek_item.src_mac, now_ms);
        if (!slot) continue;
        slot->last_rssi = peek_item.rssi;
        nltp_espnow_ensure_peer(peek_item.src_mac, g_hub->rate_mode, peek_item.rssi);
        uint32_t valid0 = slot->ep.valid_rx;
        nltp_on_datagram(&slot->ep, peek_item.data, peek_item.len);
        if (slot->ep.valid_rx != valid0) slot->last_active_ms = now_ms;
        if (slot->ep.rx.completed &&
            (slot->ep.rx.stream_id != slot->last_reported_sid || slot->ep.rx.peer_cid != slot->last_reported_cid)) {
            slot->last_reported_sid = slot->ep.rx.stream_id;
            slot->last_reported_cid = slot->ep.rx.peer_cid;
            bridge_on_msg_cb(nullptr, slot->mac, slot->last_rssi, slot->rx_buf,
                             nltp_rx_len(&slot->ep), slot->ep.rx.filename, nltp_rx_sha_ok(&slot->ep));
        }
    }

    for (uint32_t i = 0; i < ACTIVE_POOL_SLOTS; ++i) {
        if (g_hub->slots[i].in_use) {
            nltp_poll(&g_hub->slots[i].ep);
            if (tx_waiting_fin && nltp_tx_done(&g_hub->slots[i].ep)) {
                tx_waiting_fin = false;
                uint32_t dt_us = micros() - tx_started_us;
                float mbps = dt_us > 0 ? (tx_target_len * 8.0f) / (float)dt_us : 0.0f;
                Serial.printf("HUB_TX ok=%d len=%lu ms=%.2f mbps=%.3f max_l2=%u max_tx_l2=%u\n",
                              nltp_tx_verified(&g_hub->slots[i].ep), (unsigned long)tx_target_len,
                              dt_us / 1000.0f, mbps,
                              (unsigned)g_max_rx_frame_len,
                              (unsigned)g_nltp_espnow_bus.max_tx_frame_len);
            }
        }
    }

    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') {
            serial_buf[serial_len] = '\0';
            serial_len = 0;
            if (serial_buf[0]) handle_line_cmd((const char *)serial_buf);
            if (g_tunnel_mode) break;
        } else if (serial_len + 1 < sizeof(serial_buf)) {
            serial_buf[serial_len++] = (uint8_t)c;
        }
    }
    delay(1);
}

/* =========================================================================
 * ROLE 1 (COM12): Pure Arduino C++ `NLTPESPNowNode` (`<NLTP.h>`) + Raw v1/v2
 * ========================================================================= */
#else

static NLTPESPNowNode g_node;
static char g_psk[96] = "necklink-v2-interop-key";
static bool g_encrypted = true;
static bool g_v1_mode = false;
static nltp_espnow_rate_t g_rate = NLTP_ESPNOW_RATE_AUTO;
static uint8_t g_peer_mac[6] = {0};
static bool g_have_peer = false;
static uint16_t g_max_rx_l2 = 0;

static int node_rx_filter_cb(void *, const uint8_t *, int8_t, const uint8_t *, size_t len) {
    if (len > rx_mtu_cap) {
        rx_mtu_drops++;
        return 0;
    }
    if (len > g_max_rx_l2) g_max_rx_l2 = (uint16_t)len;
    if (sim_drop_pct > 0 && (xorshift32() % 100u) < sim_drop_pct) return 0;
    return 1;
}

static void attach_hooks() {
    g_nltp_espnow_bus.non_nltp_rx_cb = on_non_nltp_espnow_isr;
    if (g_node.rawNode()) {
        g_node.rawNode()->rx_filter = node_rx_filter_cb;
    }
    g_node.setEspNowV1Mode(g_v1_mode);
}

static void handle_node_cmd(const char *cmd) {
    if (strncmp(cmd, "stat", 4) == 0) {
        nltp_endpoint_t *ep = g_node.endpoint();
        Serial.printf("STAT role=arduino_node enow_v=%lu mtu=%u v1=%u rxmtu=%u ch=%u lr=%u rate=%d enc=%u auth_drops=%lu crc_drops=%lu mtu_drops=%lu max_l2=%u max_tx_l2=%u rssi=%d mac=%s heap=%u\n",
                      (unsigned long)g_node.espNowVersion(),
                      (unsigned)NLTP_ESPNOW_MTU,
                      (unsigned)(g_node.isEspNowV1Mode() ? 1 : 0),
                      (unsigned)rx_mtu_cap,
                      (unsigned)g_nltp_espnow_bus.channel,
                      (unsigned)g_nltp_espnow_bus.lr_enabled,
                      (int)g_rate,
                      (unsigned)(ep ? ep->encrypted : 0),
                      (unsigned long)(ep ? ep->auth_drops : 0),
                      (unsigned long)(ep ? ep->crc_drops : 0),
                      (unsigned long)rx_mtu_drops,
                      (unsigned)g_max_rx_l2,
                      (unsigned)g_nltp_espnow_bus.max_tx_frame_len,
                      (int)g_node.lastRssi(),
                      WiFi.macAddress().c_str(),
                      (unsigned)ESP.getFreeHeap());
    } else if (strncmp(cmd, "reset_max", 9) == 0) {
        g_max_rx_l2 = 0;
        g_nltp_espnow_bus.max_tx_frame_len = 0;
        rx_mtu_drops = 0;
        Serial.println("OK reset_max");
    } else if (strncmp(cmd, "v1mode ", 7) == 0) {
        g_v1_mode = (atoi(cmd + 7) != 0);
        g_node.setEspNowV1Mode(g_v1_mode);
        g_max_rx_l2 = 0;
        g_nltp_espnow_bus.max_tx_frame_len = 0;
        Serial.printf("OK v1mode=%d\n", g_v1_mode ? 1 : 0);
    } else if (strncmp(cmd, "rxmtu ", 6) == 0) {
        rx_mtu_cap = (uint16_t)atoi(cmd + 6);
        rx_mtu_drops = 0;
        Serial.printf("OK rxmtu=%u\n", (unsigned)rx_mtu_cap);
    } else if (strncmp(cmd, "peer ", 5) == 0) {
        if (parse_mac_str(cmd + 5, g_peer_mac)) {
            g_have_peer = true;
            g_node.setPeerMac(g_peer_mac);
            Serial.printf("OK peer=%s\n", cmd + 5);
        } else {
            Serial.println("ERROR bad_mac");
        }
    } else if (strncmp(cmd, "rate ", 5) == 0) {
        g_rate = (nltp_espnow_rate_t)atoi(cmd + 5);
        g_node.setRateMode(g_rate);
        Serial.printf("OK rate=%d lr=%u\n", (int)g_rate, (unsigned)g_nltp_espnow_bus.lr_enabled);
    } else if (strncmp(cmd, "psk ", 4) == 0) {
        const char *arg = cmd + 4;
        if (strcmp(arg, "off") == 0) {
            g_encrypted = false;
            g_psk[0] = '\0';
            if (g_node.endpoint()) nltp_set_key(g_node.endpoint(), nullptr);
            Serial.println("OK psk=off");
        } else {
            g_encrypted = true;
            snprintf(g_psk, sizeof(g_psk), "%s", arg);
            uint8_t k32[32];
            nltp_key_from_psk(g_psk, k32);
            if (g_node.endpoint()) nltp_set_key(g_node.endpoint(), k32);
            memset(k32, 0, sizeof(k32));
            Serial.println("OK psk=on");
        }
    } else if (strncmp(cmd, "drop ", 5) == 0) {
        sim_drop_pct = (uint8_t)atoi(cmd + 5);
        Serial.printf("OK drop=%u\n", (unsigned)sim_drop_pct);
    } else if (strncmp(cmd, "txkbps ", 7) == 0) {
        unsigned long k = strtoul(cmd + 7, nullptr, 10);
        if (k >= 10 && g_node.endpoint()) {
            nltp_endpoint_t *ep = g_node.endpoint();
            ep->rate.cap_kbps = (uint32_t)k;
            ep->rate.rate_kbps = (uint32_t)k;
            ep->rate.mode = (k < 500u) ? 1u : 0u;
            ep->pacer.rate_kbps = (uint32_t)k;
            ep->pacer.max_burst = (k < 500u) ? 1400 : 16384;
            ep->pacer.tokens = 0;
            ep->pacer.bonus = 0;
            Serial.printf("OK txkbps=%lu\n", k);
        }
    } else if (strncmp(cmd, "cancel_tx", 9) == 0) {
        if (g_node.endpoint() && g_node.endpoint()->tx.active && !g_node.endpoint()->tx.completed) {
            g_node.endpoint()->tx.completed = 1;
            g_node.endpoint()->tx.verified = 0;
        }
        tx_waiting_fin = false;
        Serial.println("OK cancel_tx");
    } else if (strncmp(cmd, "wake_reset", 10) == 0) {
        g_node.begin(DEFAULT_CH, g_encrypted ? g_psk : nullptr, g_rate, MAX_TEST_BYTES, 25000u);
        attach_hooks();
        if (g_have_peer) g_node.setPeerMac(g_peer_mac);
        tx_waiting_fin = false;
        Serial.printf("OK wake_reset cid=0x%08lx\n", (unsigned long)g_node.endpoint()->conn_id);
    } else if (strncmp(cmd, "send ", 5) == 0) {
        unsigned long len = strtoul(cmd + 5, nullptr, 10);
        if (len == 0 || len > MAX_TEST_BYTES || !g_have_peer) {
            Serial.println("ERROR bad_send");
            return;
        }
        if (g_node.endpoint()->tx.active && !g_node.endpoint()->tx.completed) {
            g_node.endpoint()->tx.completed = 1;
            g_node.endpoint()->tx.verified = 0;
        }
        g_nltp_espnow_bus.max_tx_frame_len = 0;
        fill_deterministic(tx_pattern_buf, (uint32_t)len, 0xCAFE0000u + (uint32_t)len);
        tx_target_len = (uint32_t)len;
        tx_started_us = micros();
        tx_waiting_fin = true;
        if (g_node.sendBytes(tx_pattern_buf, (uint32_t)len, "ard_v2.bin")) {
            Serial.printf("OK send_started len=%lu\n", len);
        } else {
            tx_waiting_fin = false;
            Serial.println("ERROR send_busy");
        }
    } else if (strncmp(cmd, "raw_send ", 9) == 0) {
        unsigned int ver = 2;
        unsigned long len = 4096;
        if (sscanf(cmd + 9, "%u %lu", &ver, &len) == 2 && (ver == 1 || ver == 2) && len <= MAX_TEST_BYTES && g_have_peer) {
            execute_raw_send((uint8_t)ver, g_peer_mac, (uint32_t)len, g_rate);
        } else {
            Serial.println("ERROR bad_raw_send");
        }
    }
}

void setup() {
    Serial.begin(115200);
    delay(250);
    rng_state ^= esp_random();

    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.disconnect(false, true);

    tx_pattern_buf = (uint8_t *)malloc(MAX_TEST_BYTES);
    if (!tx_pattern_buf || !g_node.begin(DEFAULT_CH, g_psk, g_rate, MAX_TEST_BYTES, 25000u)) {
        Serial.printf("ERROR init heap=%u\n", (unsigned)ESP.getFreeHeap());
        for (;;) delay(1000);
    }
    attach_hooks();

    Serial.printf("READY role=arduino_node enow_v=%lu mtu=%u ch=%u mac=%s heap=%u\n",
                  (unsigned long)g_node.espNowVersion(),
                  (unsigned)NLTP_ESPNOW_MTU,
                  (unsigned)DEFAULT_CH,
                  WiFi.macAddress().c_str(),
                  (unsigned)ESP.getFreeHeap());
}

void loop() {
    static char line[128];
    static size_t pos = 0;

    poll_raw_bench(g_rate);
    g_node.poll();

    if (g_node.hasReceivedFile()) {
        Serial.printf("NODE_RX len=%lu sha_ok=%d file=%s rssi=%d max_l2=%u max_tx_l2=%u\n",
                      (unsigned long)g_node.receivedLength(),
                      (int)g_node.isReceivedShaOk(),
                      g_node.receivedFilename(),
                      (int)g_node.lastRssi(),
                      (unsigned)g_max_rx_l2,
                      (unsigned)g_nltp_espnow_bus.max_tx_frame_len);
        g_node.clearReceived();
    }

    if (tx_waiting_fin && g_node.isSendComplete()) {
        tx_waiting_fin = false;
        uint32_t dt_us = micros() - tx_started_us;
        float mbps = dt_us > 0 ? (tx_target_len * 8.0f) / (float)dt_us : 0.0f;
        Serial.printf("NODE_TX ok=%d len=%lu ms=%.2f mbps=%.3f max_l2=%u max_tx_l2=%u\n",
                      (int)g_node.isSendVerified(),
                      (unsigned long)tx_target_len,
                      dt_us / 1000.0f,
                      mbps,
                      (unsigned)g_max_rx_l2,
                      (unsigned)g_nltp_espnow_bus.max_tx_frame_len);
    }

    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') {
            line[pos] = '\0';
            pos = 0;
            if (line[0]) handle_node_cmd(line);
        } else if (pos + 1 < sizeof(line)) {
            line[pos++] = c;
        }
    }
    delay(1);
}
#endif
