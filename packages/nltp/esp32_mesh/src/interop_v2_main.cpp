/*
 * interop_v2_main.cpp — Live Hardware Verification Firmware for Standard Wire-Format
 * NLTP over ESP-NOW v2 (1,470 B MTU, Zero Custom Wrapper Headers).
 *
 * Role 0 (`INTEROP_ROLE == 0`, COM9):
 *   - Runs pure C99 `nltp_espnow_hub_t` (multi-peer MAC-keyed bridge + allow-list)
 *   - Also supports transparent RFC 1055 SLIP binary framing (`0xC0`) so unmodified
 *     Host Python `pynltp` (`NLTPEndpoint`) can exchange raw "NL" encrypted 1,008 B
 *     ESP-NOW v2 frames directly with off-Wi-Fi ESP32 peers over 2.4 GHz radio!
 *
 * Role 1 (`INTEROP_ROLE == 1`, COM12):
 *   - Runs pure Arduino C++ `NLTPESPNowNode` from `<NLTP.h>` (`packages/nltp/arduino/NLTP/src/NLTP.h`).
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
 * ROLE 0 (COM9): C99 `nltp_espnow_hub_t` Multi-Peer Bridge + Python SLIP Tunnel
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
    Serial.printf("HUB_RX mac=%02x:%02x:%02x:%02x:%02x:%02x rssi=%d len=%lu sha_ok=%d file=%s max_l2=%u\n",
                  src_mac[0], src_mac[1], src_mac[2], src_mac[3], src_mac[4], src_mac[5],
                  (int)rssi, (unsigned long)len, sha_ok, filename ? filename : "",
                  (unsigned)g_max_rx_frame_len);
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

static void handle_line_cmd(const char *cmd) {
    if (strncmp(cmd, "stat", 4) == 0) {
        Serial.printf("STAT role=bridge_c99 enow_v=%lu mtu=%u ch=%u lr=%u mode=%s rate=%d blocked=%lu max_l2=%u mac=%s heap=%u\n",
                      (unsigned long)g_nltp_espnow_bus.espnow_version,
                      (unsigned)NLTP_ESPNOW_MTU,
                      (unsigned)g_nltp_espnow_bus.channel,
                      (unsigned)g_nltp_espnow_bus.lr_enabled,
                      g_tunnel_mode ? "slip_tunnel" : "c99_hub",
                      (int)g_rate_mode,
                      (unsigned long)g_blocked_drops,
                      (unsigned)g_max_rx_frame_len,
                      WiFi.macAddress().c_str(),
                      (unsigned)ESP.getFreeHeap());
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
        strncpy(g_psk, cmd + 4, sizeof(g_psk) - 1);
        nltp_key_from_psk(g_psk, g_hub->key32);
        g_hub->encrypted = 1u;
        for (uint32_t i = 0; i < ACTIVE_POOL_SLOTS; ++i) {
            if (g_hub->slots[i].in_use) nltp_set_key(&g_hub->slots[i].ep, g_hub->key32);
        }
        Serial.println("OK psk");
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
                fill_deterministic(tx_pattern_buf, (uint32_t)len, 0x13572468u + (uint32_t)len);
                tx_target_len = (uint32_t)len;
                tx_started_us = micros();
                tx_waiting_fin = true;
                nltp_send_buffer(&slot->ep, tx_pattern_buf, (uint32_t)len, "c99_to_ard.bin");
                Serial.printf("OK hub_send_started len=%lu\n", len);
            }
        }
    } else if (strncmp(cmd, "tunnel_on ", 10) == 0) {
        if (parse_mac_str(cmd + 10, g_tunnel_peer_mac)) {
            nltp_espnow_ensure_peer(g_tunnel_peer_mac, g_rate_mode, -55);
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

    if (g_tunnel_mode) {
        /* 1. Drain ESP-NOW v2 radio frames -> SLIP-encode raw "NL" wire bytes straight to Host Python
         *    without blocking UART RX (check availableForWrite >= 2100 so 1,010 B escaped frame fits immediately). */
        nltp_espnow_rx_item_t item;
        if (Serial.availableForWrite() >= 2100 &&
            g_nltp_espnow_bus.rx_q &&
            xQueueReceive(g_nltp_espnow_bus.rx_q, &item, 0) == pdTRUE) {
            if (item.len > g_max_rx_frame_len) g_max_rx_frame_len = item.len;
            if (!(sim_drop_pct > 0 && (xorshift32() % 100u) < sim_drop_pct)) {
                slip_write_frame(item.data, item.len);
            }
        }

        /* 2. Drain SLIP frames from Host Python -> transmit verbatim as ESP-NOW v2 frames! */
        while (Serial.available()) {
            uint8_t b = (uint8_t)Serial.read();
            if (b == SLIP_END) {
                if (serial_len > 0) {
                    /* Exit command `!tunnel_off` inside SLIP frame returns cleanly to ASCII CLI */
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

    /* Normal ASCII CLI + C99 `nltp_espnow_hub_t` mode */
    nltp_espnow_rx_item_t peek_item;
    while (g_nltp_espnow_bus.rx_q && xQueueReceive(g_nltp_espnow_bus.rx_q, &peek_item, 0) == pdTRUE) {
        if (peek_item.len > g_max_rx_frame_len) g_max_rx_frame_len = peek_item.len;
        if (sim_drop_pct > 0 && (xorshift32() % 100u) < sim_drop_pct) continue;

        /* Re-inject into hub logic directly */
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
                Serial.printf("HUB_TX ok=%d len=%lu ms=%.2f mbps=%.3f max_l2=%u\n",
                              nltp_tx_verified(&g_hub->slots[i].ep), (unsigned long)tx_target_len,
                              dt_us / 1000.0f, mbps, (unsigned)g_max_rx_frame_len);
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
 * ROLE 1 (COM12): Pure Arduino C++ `NLTPESPNowNode` (`<NLTP.h>`)
 * ========================================================================= */
#else

static NLTPESPNowNode g_node;
static char g_psk[96] = "necklink-v2-interop-key";
static nltp_espnow_rate_t g_rate = NLTP_ESPNOW_RATE_AUTO;
static uint8_t g_peer_mac[6] = {0};
static bool g_have_peer = false;
static uint16_t g_max_rx_l2 = 0;

static int node_rx_filter_cb(void *, const uint8_t *, int8_t, const uint8_t *, size_t len) {
    if (len > g_max_rx_l2) g_max_rx_l2 = (uint16_t)len;
    if (sim_drop_pct > 0 && (xorshift32() % 100u) < sim_drop_pct) return 0; /* drop */
    return 1; /* accept into NLTPESPNowNode */
}

static void attach_filter() {
    if (g_node.rawNode()) {
        g_node.rawNode()->rx_filter = node_rx_filter_cb;
    }
}

static void handle_node_cmd(const char *cmd) {
    if (strncmp(cmd, "stat", 4) == 0) {
        Serial.printf("STAT role=arduino_node enow_v=%lu mtu=%u ch=%u lr=%u rate=%d max_l2=%u rssi=%d mac=%s heap=%u\n",
                      (unsigned long)g_node.espNowVersion(),
                      (unsigned)NLTP_ESPNOW_MTU,
                      (unsigned)g_nltp_espnow_bus.channel,
                      (unsigned)g_nltp_espnow_bus.lr_enabled,
                      (int)g_rate,
                      (unsigned)g_max_rx_l2,
                      (int)g_node.lastRssi(),
                      WiFi.macAddress().c_str(),
                      (unsigned)ESP.getFreeHeap());
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
        strncpy(g_psk, cmd + 4, sizeof(g_psk) - 1);
        g_node.begin(DEFAULT_CH, g_psk, g_rate, MAX_TEST_BYTES, 25000u);
        attach_filter();
        if (g_have_peer) g_node.setPeerMac(g_peer_mac);
        Serial.println("OK psk");
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
    } else if (strncmp(cmd, "wake_reset", 10) == 0) {
        /* Simulates deep-sleep pad wake: brand-new random conn_id, sid=1, tx_seq=0 */
        g_node.begin(DEFAULT_CH, g_psk, g_rate, MAX_TEST_BYTES, 25000u);
        attach_filter();
        if (g_have_peer) g_node.setPeerMac(g_peer_mac);
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
    attach_filter();

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

    g_node.poll();

    if (g_node.hasReceivedFile()) {
        Serial.printf("NODE_RX len=%lu sha_ok=%d file=%s rssi=%d max_l2=%u\n",
                      (unsigned long)g_node.receivedLength(),
                      (int)g_node.isReceivedShaOk(),
                      g_node.receivedFilename(),
                      (int)g_node.lastRssi(),
                      (unsigned)g_max_rx_l2);
        g_node.clearReceived();
    }

    if (tx_waiting_fin && g_node.isSendComplete()) {
        tx_waiting_fin = false;
        uint32_t dt_us = micros() - tx_started_us;
        float mbps = dt_us > 0 ? (tx_target_len * 8.0f) / (float)dt_us : 0.0f;
        Serial.printf("NODE_TX ok=%d len=%lu ms=%.2f mbps=%.3f max_l2=%u\n",
                      (int)g_node.isSendVerified(),
                      (unsigned long)tx_target_len,
                      dt_us / 1000.0f,
                      mbps,
                      (unsigned)g_max_rx_l2);
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
