// ============================================================================
// NeckLink NLTP-NOW: 3-Node Interconnected ESP32 Mesh + Single Wi-Fi Gateway
// ============================================================================
// Architecture:
//   - NODE 0 (Gateway): ONLY node connected to home Wi-Fi (secrets.h).
//     Bridges Laptop UDP (:9700 -> Node 0, :9701 -> Node 1, :9702 -> Node 2)
//     and interconnects with Node 1 & Node 2 over raw Layer-2 ESP-NOW v2 (1470B MTU).
//     Can also toggle a private SoftAP ("NLTP_MESH_AP", 192.168.4.1) on demand
//      strictly to benchmark standard NLTP-over-SoftAP-UDP vs NLTP-NOW.
//   - NODE 1 (Room 2 Relay) & NODE 2 (Room 3 Leaf):
//     NEVER connect to home Wi-Fi! They scan for Node 0's L2 mesh beacon to lock
//     channel, discover all peers dynamically, forward multi-hop frames (0<->1<->2),
//     and run full ChaCha20-Poly1305 + GF(2^8) Cauchy FEC + SHA-256 NLTP directly
//     over raw 802.11 vendor action frames (NLTP-NOW).
// ============================================================================

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <fcntl.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "lwip/sockets.h"
#include "nltp_core.h"

#if MESH_NODE_ID == 0
#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Missing ../esp32/include/secrets.h for Gateway Node 0"
#endif
#endif

#ifndef MESH_NODE_ID
#define MESH_NODE_ID 0
#endif

static const uint8_t MY_NODE_ID = (uint8_t)MESH_NODE_ID;
static const uint8_t MAX_NODES = 4;
static const uint8_t BCAST_MAC[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

static const char *SOFTAP_SSID = "NLTP_MESH_PRIV";
static const char *SOFTAP_PASS = "nltp_mesh_2026";
static const uint16_t UDP_BASE_PORT = 9700;  // 9700=node0, 9701=node1, 9702=node2
static const uint32_t PAYLOAD_CAP = 32 * 1024;
static const uint32_t MAX_KBPS = 25000;

// 8-byte L2 Mesh Header (1072B NLTP encrypted shard + 8B = 1080B <= 1470B ESP-NOW v2 MTU!)
enum MeshFrameKind : uint8_t {
    MK_BEACON     = 0x01,  // Periodic channel/MAC/RSSI discovery
    MK_NLTP       = 0x02,  // Full NLTP C99 wire packet (META/DATA_SHARD/PARITY/ACK/FIN/REFLEX)
    MK_PING       = 0x03,  // Range & latency probe request
    MK_PONG       = 0x04,  // Range & latency probe response (carries receiver's measured RSSI)
    MK_RAW_DATA   = 0x05,  // Raw ESP-NOW speed/loss benchmark chunk (no FEC)
    MK_RAW_DONE   = 0x06,  // Raw ESP-NOW end-of-burst marker
    MK_RAW_REPORT = 0x07,  // Raw ESP-NOW receiver count & integrity report
    MK_CMD_REMOTE = 0x08,  // Remote control command over L2 (set rate/txpwr/drop on remote room nodes!)
};

struct __attribute__((packed)) MeshHdr {
    uint8_t magic0;      // 'N' (0x4E)
    uint8_t magic1;      // 'M' (0x4D)
    uint8_t kind;        // MeshFrameKind
    uint8_t src_node;    // Origin node (0..2, or 3 = Laptop bridged via Gateway)
    uint8_t dst_node;    // Final destination node (0..2, or 3 = Laptop, or 0xFF = broadcast)
    uint8_t via_node;    // Last transmitter node ID (so receiver attributes hop RSSI accurately)
    uint8_t ttl;         // Remaining hops (default 3)
    uint8_t flags;       // Reserved / rate tag
    uint16_t seq;        // Frame sequence number
};

struct PeerEntry {
    bool known;
    uint8_t mac[6];
    int8_t last_rssi;
    int8_t noise_floor;
    uint32_t last_seen_ms;
    uint32_t rx_pkts;
};

static PeerEntry peers[MAX_NODES];
static uint8_t current_channel = 11;
static uint8_t current_rate_mode = 3;  // 0=1M_L, 1=11M_S, 2=MCS4, 3=MCS7_SGI, 4=AUTO
static int8_t current_tx_pwr_qdbm = 78; // 78 = +19.5 dBm (max), 8 = +2.0 dBm (min)
static int8_t forced_relay_hop = -1;   // -1 = direct/auto, 0..2 = force next-hop relay
static uint8_t sim_rx_drop_pct = 0;    // Artificial RX drop % to test FEC under lossy walls

// Hardware TX flow control (prevents ESP_ERR_ESPNOW_NO_MEM during 32KB bursts)
static volatile int32_t tx_inflight = 0;
static volatile uint32_t tx_ok_cnt = 0;
static volatile uint32_t tx_fail_cnt = 0;
static uint16_t mesh_seq = 0;

// Lock-free RX queue from Wi-Fi task -> Arduino loop()
struct RxFrame {
    uint16_t len;
    int8_t rssi;
    int8_t noise;
    uint8_t src_mac[6];
    uint8_t buf[1472];
};
static QueueHandle_t rx_queue = nullptr;

// NLTP C99 Endpoint & buffers
static nltp_endpoint_t *ep = nullptr;
static uint8_t *shared_buf = nullptr;
static uint8_t active_nltp_peer_node = 0;
static bool use_udp_transport = false;  // false = NLTP-NOW (L2), true = SoftAP UDP baseline
static struct sockaddr_in udp_peer_addr;
static bool have_udp_peer = false;
static int udp_socks[3] = {-1, -1, -1}; // Node 0 opens 9700, 9701 (bridge->1), 9702 (bridge->2)
static struct sockaddr_in laptop_client_for_node[3];
static bool have_laptop_for_node[3] = {false, false, false};

// Transfer & Ping measurement state
static uint32_t rx_sid_seen = 0, rx_first_ms = 0, rx_reported_sid = 0;
static uint32_t tx_started_us = 0, tx_len = 0;
static bool tx_reported = true;
static uint32_t last_beacon_ms = 0;

// Ping accumulator
static bool ping_active = false;
static uint8_t ping_target = 0;
static uint16_t ping_total = 0, ping_sent = 0, ping_rcvd = 0, ping_payload_len = 32;
static uint32_t ping_last_tx_us = 0;
static uint32_t ping_rtt_sum_us = 0, ping_rtt_min_us = 0xFFFFFFFFu, ping_rtt_max_us = 0;
static int32_t ping_remote_rssi_sum = 0, ping_local_rssi_sum = 0;

// Raw ESP-NOW receiver & sender benchmark state
static uint32_t raw_rx_bytes = 0, raw_rx_pkts = 0, raw_rx_expected = 0;
static uint32_t raw_tx_start_us = 0, raw_tx_total_bytes = 0;
static bool raw_tx_waiting_report = false;

static uint32_t xorshift_state = 0x12345678u;
static inline uint32_t fast_rand() {
    uint32_t x = xorshift_state;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return (xorshift_state = x);
}

static void fill_test_data(uint8_t *buf, uint32_t n) {
    uint32_t x = 0x9E3779B9u;
    for (uint32_t i = 0; i < n; ++i) {
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        buf[i] = (uint8_t)x;
    }
}

static uint32_t compute_xor_digest(const uint8_t *buf, uint32_t n) {
    uint32_t h = 2166136261u;
    for (uint32_t i = 0; i < n; ++i) {
        h ^= buf[i];
        h *= 16777619u;
    }
    return h;
}

static void apply_peer_rate(const uint8_t mac[6], uint8_t mode, int8_t rssi_hint) {
    uint8_t eff = mode;
    if (eff == 4) { // RSSI-Adaptive Auto Rate!
        if (rssi_hint >= -67) eff = 3;       // MCS7_SGI (72.2 Mbps)
        else if (rssi_hint >= -76) eff = 2;  // MCS4_LGI (39.0 Mbps)
        else if (rssi_hint >= -85) eff = 1;  // 11M_S    (11.0 Mbps)
        else eff = 0;                        // 1M_L     ( 1.0 Mbps deep wall punch)
    }
    esp_now_rate_config_t cfg = {};
    if (eff == 0) {
        cfg.phymode = WIFI_PHY_MODE_11B;
        cfg.rate = WIFI_PHY_RATE_1M_L;
    } else if (eff == 1) {
        cfg.phymode = WIFI_PHY_MODE_11B;
        cfg.rate = WIFI_PHY_RATE_11M_S;
    } else if (eff == 2) {
        cfg.phymode = WIFI_PHY_MODE_HT20;
        cfg.rate = WIFI_PHY_RATE_MCS4_LGI;
    } else if (eff == 5) {
        cfg.phymode = WIFI_PHY_MODE_LR;
        cfg.rate = WIFI_PHY_RATE_LORA_250K;
    } else {
        cfg.phymode = WIFI_PHY_MODE_HT20;
        cfg.rate = WIFI_PHY_RATE_MCS7_SGI;
    }
    cfg.ersu = false;
    cfg.dcm = false;
    esp_now_set_peer_rate_config(mac, &cfg);
}

static void ensure_espnow_peer(const uint8_t mac[6], int8_t rssi_hint = -55) {
    if (!esp_now_is_peer_exist(mac)) {
        esp_now_peer_info_t pi = {};
        memcpy(pi.peer_addr, mac, 6);
        pi.channel = 0;  // current channel
        pi.ifidx = WIFI_IF_STA;
        pi.encrypt = false; // NLTP provides 256-bit ChaCha20-Poly1305 AEAD with unlimited peers!
        esp_now_add_peer(&pi);
    }
    if (memcmp(mac, BCAST_MAC, 6) != 0) {
        apply_peer_rate(mac, current_rate_mode, rssi_hint);
    }
}

static void refresh_all_peer_rates() {
    for (uint8_t i = 0; i < MAX_NODES; ++i) {
        if (peers[i].known) {
            apply_peer_rate(peers[i].mac, current_rate_mode, peers[i].last_rssi);
        }
    }
}

// Choose next hop MAC for dst_node (supports forced multi-hop OR automatic weak-RSSI relay!)
static const uint8_t *pick_next_hop_mac(uint8_t dst_node, uint8_t *chosen_hop_out) {
    if (dst_node == 0xFF) {
        if (chosen_hop_out) *chosen_hop_out = 0xFF;
        return BCAST_MAC;
    }
    // Laptop (node 3) is always reached via Gateway (node 0)
    uint8_t target = (dst_node == 3) ? 0 : dst_node;
    if (forced_relay_hop >= 0 && forced_relay_hop < MAX_NODES &&
        (uint8_t)forced_relay_hop != MY_NODE_ID && (uint8_t)forced_relay_hop != target &&
        peers[forced_relay_hop].known) {
        if (chosen_hop_out) *chosen_hop_out = (uint8_t)forced_relay_hop;
        return peers[forced_relay_hop].mac;
    }
    // Auto multi-hop: if direct link is unknown or < -83 dBm, and an intermediate node has > -76 dBm, relay!
    if (target < MAX_NODES && (!peers[target].known || peers[target].last_rssi < -83)) {
        for (uint8_t mid = 0; mid < 3; ++mid) {
            if (mid != MY_NODE_ID && mid != target && peers[mid].known && peers[mid].last_rssi > -76) {
                if (chosen_hop_out) *chosen_hop_out = mid;
                return peers[mid].mac;
            }
        }
    }
    if (target < MAX_NODES && peers[target].known) {
        if (chosen_hop_out) *chosen_hop_out = target;
        return peers[target].mac;
    }
    if (chosen_hop_out) *chosen_hop_out = 0xFF;
    return BCAST_MAC;
}

static void on_espnow_tx(const esp_now_send_info_t *, esp_now_send_status_t status) {
    int32_t cur = tx_inflight;
    if (cur > 0) tx_inflight = cur - 1;
    if (status == ESP_NOW_SEND_SUCCESS) tx_ok_cnt = tx_ok_cnt + 1;
    else tx_fail_cnt = tx_fail_cnt + 1;
}

static void on_espnow_rx(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    if (!rx_queue || len < (int)sizeof(MeshHdr) || len > 1472) return;
    if (data[0] != 'N' || data[1] != 'M') return;
    RxFrame item;
    item.len = (uint16_t)len;
    item.rssi = info && info->rx_ctrl ? (int8_t)info->rx_ctrl->rssi : -60;
    item.noise = info && info->rx_ctrl ? (int8_t)info->rx_ctrl->noise_floor : -95;
    if (info && info->src_addr) memcpy(item.src_mac, info->src_addr, 6);
    else memset(item.src_mac, 0, 6);
    memcpy(item.buf, data, (size_t)len);
    xQueueSend(rx_queue, &item, 0);
}

static bool send_mesh_frame(uint8_t kind, uint8_t src_node, uint8_t dst_node, uint8_t ttl,
                            const uint8_t *payload, size_t payload_len, int max_inflight = 7) {
    if (payload_len + sizeof(MeshHdr) > ESP_NOW_MAX_DATA_LEN_V2) return false;
    static uint8_t frame_buf[1472];
    MeshHdr hdr;
    hdr.magic0 = 'N';
    hdr.magic1 = 'M';
    hdr.kind = kind;
    hdr.src_node = src_node;
    hdr.dst_node = dst_node;
    hdr.via_node = MY_NODE_ID;
    hdr.ttl = ttl;
    hdr.flags = current_rate_mode;
    hdr.seq = ++mesh_seq;
    memcpy(frame_buf, &hdr, sizeof(hdr));
    if (payload && payload_len > 0) memcpy(frame_buf + sizeof(hdr), payload, payload_len);

    uint8_t hop = 0xFF;
    const uint8_t *mac = pick_next_hop_mac(dst_node, &hop);
    ensure_espnow_peer(mac, (hop < MAX_NODES && peers[hop].known) ? peers[hop].last_rssi : -55);

    // Wait briefly if hardware MAC queue is full so burst shards never drop locally
    uint32_t t0 = micros();
    while (tx_inflight >= max_inflight && (uint32_t)(micros() - t0) < 12000u) {
        delayMicroseconds(60);
    }

    for (int attempt = 0; attempt < 5; ++attempt) {
        tx_inflight = tx_inflight + 1;
        esp_err_t rc = esp_now_send(mac, frame_buf, sizeof(hdr) + payload_len);
        if (rc == ESP_OK) return true;
        if (tx_inflight > 0) tx_inflight = tx_inflight - 1;
        if (rc == ESP_ERR_ESPNOW_NO_MEM) {
            delayMicroseconds(180);
            continue;
        }
        break;
    }
    return false;
}

static uint32_t now_cb(void *) { return millis(); }

static void nltp_transport_send_cb(void *, const uint8_t *pkt, size_t len) {
    if (use_udp_transport) {
        if (have_udp_peer && udp_socks[0] >= 0) {
            sendto(udp_socks[0], pkt, len, 0, (struct sockaddr *)&udp_peer_addr, sizeof(udp_peer_addr));
        }
        return;
    }
    // If peer is Laptop (node 3) and we ARE Gateway (Node 0), send directly over Wi-Fi UDP!
    if (MY_NODE_ID == 0 && active_nltp_peer_node == 3 && have_laptop_for_node[0] && udp_socks[0] >= 0) {
        sendto(udp_socks[0], pkt, len, 0,
               (struct sockaddr *)&laptop_client_for_node[0], sizeof(laptop_client_for_node[0]));
        return;
    }
    send_mesh_frame(MK_NLTP, MY_NODE_ID, active_nltp_peer_node, 3, pkt, len, 7);
}

static void bind_udp_sockets() {
    int count = (MY_NODE_ID == 0) ? 3 : 1;
    for (int i = 0; i < count; ++i) {
        if (udp_socks[i] >= 0) close(udp_socks[i]);
        udp_socks[i] = socket(AF_INET, SOCK_DGRAM, 0);
        struct sockaddr_in a = {};
        a.sin_family = AF_INET;
        a.sin_port = htons((uint16_t)(UDP_BASE_PORT + i));
        a.sin_addr.s_addr = htonl(INADDR_ANY);
        bind(udp_socks[i], (struct sockaddr *)&a, sizeof(a));
        fcntl(udp_socks[i], F_SETFL, fcntl(udp_socks[i], F_GETFL, 0) | O_NONBLOCK);
    }
}

static void emit_beacon() {
    uint8_t pl[2] = {current_channel, (uint8_t)current_tx_pwr_qdbm};
    send_mesh_frame(MK_BEACON, MY_NODE_ID, 0xFF, 1, pl, sizeof(pl), 12);
}

#if MESH_NODE_ID != 0
// Off-Wi-Fi Room Nodes (Node 1 & Node 2) scan channels 1..13 to lock onto Node 0's channel automatically
static void lock_onto_mesh_channel() {
    Serial.printf("NODE %u (OFF-WIFI) scanning 2.4GHz channels for Mesh Gateway...\n", MY_NODE_ID);
    // Check channel 11 first (most common) for instant 150ms lock!
    const uint8_t order[13] = {11, 6, 1, 2, 3, 4, 5, 7, 8, 9, 10, 12, 13};
    for (int round = 0; round < 2; ++round) {
        for (int idx = 0; idx < 13; ++idx) {
            uint8_t ch = order[idx];
            esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
            current_channel = ch;
            emit_beacon();
            uint32_t t0 = millis();
            while (millis() - t0 < 140) {
                RxFrame f;
                if (xQueueReceive(rx_queue, &f, pdMS_TO_TICKS(15)) == pdTRUE) {
                    const MeshHdr *h = (const MeshHdr *)f.buf;
                    if (h->via_node < MAX_NODES) {
                        peers[h->via_node].known = true;
                        memcpy(peers[h->via_node].mac, f.src_mac, 6);
                        peers[h->via_node].last_rssi = f.rssi;
                        peers[h->via_node].noise_floor = f.noise;
                        peers[h->via_node].last_seen_ms = millis();
                        peers[h->via_node].rx_pkts++;
                        ensure_espnow_peer(f.src_mac, f.rssi);
                    }
                    if (h->kind == MK_BEACON && f.len >= sizeof(MeshHdr) + 1) {
                        uint8_t gw_ch = f.buf[sizeof(MeshHdr)];
                        esp_wifi_set_channel(gw_ch, WIFI_SECOND_CHAN_NONE);
                        current_channel = gw_ch;
                        Serial.printf("LOCKED channel=%u via node=%u rssi=%ddBm\n", gw_ch, h->via_node, f.rssi);
                        return;
                    }
                }
            }
        }
    }
    esp_wifi_set_channel(11, WIFI_SECOND_CHAN_NONE);
    current_channel = 11;
    Serial.println("DEFAULTED channel=11 (waiting for Gateway beacon)");
}
#endif

static void handle_command_str(const char *cmd, uint8_t reply_node = 0xFF) {
    if (strncmp(cmd, "stat", 4) == 0) {
        int8_t pwr = 0;
        esp_wifi_get_max_tx_power(&pwr);
        Serial.printf("STAT node=%u wifi_sta=%d ip=%s ch=%u rate_mode=%u txpwr_qdbm=%d(%.1fdBm) drop_pct=%u heap=%u\n",
                      MY_NODE_ID, (int)(WiFi.status() == WL_CONNECTED),
                      WiFi.localIP().toString().c_str(), current_channel, current_rate_mode,
                      pwr, pwr * 0.25f, sim_rx_drop_pct, (unsigned)ESP.getFreeHeap());
        for (uint8_t i = 0; i < MAX_NODES; ++i) {
            if (peers[i].known) {
                Serial.printf("PEER id=%u mac=%02x:%02x:%02x:%02x:%02x:%02x rssi=%d noise=%d snr=%d pkts=%lu age_ms=%lu\n",
                              i, peers[i].mac[0], peers[i].mac[1], peers[i].mac[2],
                              peers[i].mac[3], peers[i].mac[4], peers[i].mac[5],
                              peers[i].last_rssi, peers[i].noise_floor,
                              (int)peers[i].last_rssi - (int)peers[i].noise_floor,
                              (unsigned long)peers[i].rx_pkts,
                              (unsigned long)(millis() - peers[i].last_seen_ms));
            }
        }
        Serial.println("END_STAT");
    } else if (strncmp(cmd, "psk ", 4) == 0) {
        const char *arg = cmd + 4;
        if (strcmp(arg, "off") == 0) {
            nltp_set_key(ep, nullptr);
            Serial.println("PSK off");
        } else {
            uint8_t key[32];
            nltp_key_from_psk(arg, key);
            nltp_set_key(ep, key);
            Serial.println("PSK on");
        }
    } else if (strncmp(cmd, "rate ", 5) == 0) {
        uint8_t next_mode = (uint8_t)atoi(cmd + 5);
        if (next_mode == 5 && current_rate_mode != 5) {
            esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N | WIFI_PROTOCOL_LR);
            esp_wifi_set_channel(current_channel, WIFI_SECOND_CHAN_NONE);
        } else if (next_mode != 5 && current_rate_mode == 5) {
            esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
            esp_wifi_set_channel(current_channel, WIFI_SECOND_CHAN_NONE);
        }
        current_rate_mode = next_mode;
        refresh_all_peer_rates();
        Serial.printf("OK rate=%u ch=%u\n", current_rate_mode, current_channel);
    } else if (strncmp(cmd, "txpwr ", 6) == 0) {
        int p = atoi(cmd + 6);
        if (p < 8) p = 8;
        if (p > 78) p = 78;
        current_tx_pwr_qdbm = (int8_t)p;
        esp_wifi_set_max_tx_power(current_tx_pwr_qdbm);
        int8_t actual = 0;
        esp_wifi_get_max_tx_power(&actual);
        Serial.printf("OK txpwr=%d dbm=%.2f\n", actual, actual * 0.25f);
    } else if (strncmp(cmd, "drop ", 5) == 0) {
        sim_rx_drop_pct = (uint8_t)atoi(cmd + 5);
        Serial.printf("OK drop=%u\n", sim_rx_drop_pct);
    } else if (strncmp(cmd, "hop ", 4) == 0) {
        forced_relay_hop = (int8_t)atoi(cmd + 4);
        Serial.printf("OK hop=%d\n", (int)forced_relay_hop);
    } else if (strncmp(cmd, "remote ", 7) == 0) {
        int dst = atoi(cmd + 7);
        const char *sub = strchr(cmd + 7, ' ');
        if (sub && sub[1]) {
            sub++;
            send_mesh_frame(MK_CMD_REMOTE, MY_NODE_ID, (uint8_t)dst, 3, (const uint8_t *)sub, strlen(sub) + 1);
            Serial.printf("OK remote_sent dst=%d cmd=%s\n", dst, sub);
        }
    } else if (strncmp(cmd, "softap ", 7) == 0) {
        const char *arg = cmd + 7;
        if (MY_NODE_ID == 0) {
            if (strncmp(arg, "on", 2) == 0) {
                WiFi.mode(WIFI_AP_STA);
                WiFi.softAP(SOFTAP_SSID, SOFTAP_PASS, current_channel, 0, 4);
                bind_udp_sockets();
                Serial.printf("OK softap=on ip=%s ch=%u\n", WiFi.softAPIP().toString().c_str(), current_channel);
            } else {
                WiFi.softAPdisconnect(true);
                WiFi.mode(WIFI_STA);
                if (WiFi.status() != WL_CONNECTED) {
                    esp_wifi_set_channel(current_channel, WIFI_SECOND_CHAN_NONE);
                }
                Serial.printf("OK softap=off ch=%u\n", current_channel);
            }
        } else {
            if (strncmp(arg, "join", 4) == 0) {
                WiFi.begin(SOFTAP_SSID, SOFTAP_PASS);
                uint32_t t0 = millis();
                while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) delay(100);
                use_udp_transport = (WiFi.status() == WL_CONNECTED);
                bind_udp_sockets();
                memset(&udp_peer_addr, 0, sizeof(udp_peer_addr));
                udp_peer_addr.sin_family = AF_INET;
                udp_peer_addr.sin_port = htons(UDP_BASE_PORT);
                udp_peer_addr.sin_addr.s_addr = inet_addr("192.168.4.1");
                have_udp_peer = use_udp_transport;
                Serial.printf("OK softap_join=%d ip=%s rssi=%d\n", (int)use_udp_transport,
                              WiFi.localIP().toString().c_str(), WiFi.RSSI());
            } else {
                use_udp_transport = false;
                WiFi.disconnect(false, true);
                esp_wifi_set_channel(current_channel, WIFI_SECOND_CHAN_NONE);
                Serial.printf("OK softap_leave ch=%u\n", current_channel);
            }
        }
    } else if (strncmp(cmd, "mode_udp ", 9) == 0) {
        // On Node 0: set UDP target IP for SoftAP test (e.g. "mode_udp 192.168.4.2")
        const char *ip_s = cmd + 9;
        if (strcmp(ip_s, "off") == 0) {
            use_udp_transport = false;
            Serial.println("OK mode_udp=off");
        } else {
            memset(&udp_peer_addr, 0, sizeof(udp_peer_addr));
            udp_peer_addr.sin_family = AF_INET;
            udp_peer_addr.sin_port = htons(UDP_BASE_PORT);
            udp_peer_addr.sin_addr.s_addr = inet_addr(ip_s);
            have_udp_peer = true;
            use_udp_transport = true;
            Serial.printf("OK mode_udp=%s\n", ip_s);
        }
    } else if (strncmp(cmd, "ping ", 5) == 0) {
        int dst = 2, cnt = 50, sz = 64;
        sscanf(cmd + 5, "%d %d %d", &dst, &cnt, &sz);
        if (sz < 8) sz = 8;
        if (sz > 1400) sz = 1400;
        ping_target = (uint8_t)dst;
        ping_total = (uint16_t)cnt;
        ping_payload_len = (uint16_t)sz;
        ping_sent = 0; ping_rcvd = 0;
        ping_rtt_sum_us = 0; ping_rtt_min_us = 0xFFFFFFFFu; ping_rtt_max_us = 0;
        ping_remote_rssi_sum = 0; ping_local_rssi_sum = 0;
        ping_active = true;
        ping_last_tx_us = 0;
#if MESH_NODE_ID == 0
    } else if (strncmp(cmd, "wifi_rejoin", 11) == 0) {
        current_rate_mode = 3;
        if (WiFi.status() != WL_CONNECTED) {
            WiFi.disconnect(true, true);
            delay(100);
            WiFi.mode(WIFI_STA);
            WiFi.setSleep(false);
            WiFi.begin(WIFI_SSID, WIFI_PASS);
            uint32_t t0 = millis();
            while (WiFi.status() != WL_CONNECTED && millis() - t0 < 14000) delay(200);
        }
        if (WiFi.status() == WL_CONNECTED) {
            current_channel = (uint8_t)WiFi.channel();
        } else {
            esp_wifi_set_channel(current_channel, WIFI_SECOND_CHAN_NONE);
        }
        bind_udp_sockets();
        refresh_all_peer_rates();
        Serial.printf("OK wifi_rejoin=%d status=%d ip=%s ch=%u\n",
                      (int)(WiFi.status() == WL_CONNECTED), (int)WiFi.status(),
                      WiFi.localIP().toString().c_str(), current_channel);
#endif
    } else if (strncmp(cmd, "nltp_send ", 10) == 0) {
        int dst = 2;
        unsigned long len = 16384;
        sscanf(cmd + 10, "%d %lu", &dst, &len);
        if (len == 0 || len > PAYLOAD_CAP) {
            Serial.println("ERROR invalid len");
            return;
        }
        if (ep->tx.active && !ep->tx.completed) {
            ep->tx.completed = 1;
            ep->tx.verified = 0;
        }
        active_nltp_peer_node = (uint8_t)dst;
        fill_test_data(shared_buf, (uint32_t)len);
        tx_len = (uint32_t)len;
        tx_ok_cnt = 0; tx_fail_cnt = 0;
        tx_started_us = micros();
        tx_reported = false;
        if (!nltp_send_buffer(ep, shared_buf, (uint32_t)len, "mesh_batch.bin")) {
            tx_reported = true;
            Serial.println("ERROR nltp_busy");
        }
    } else if (strncmp(cmd, "raw_bench ", 10) == 0) {
        int dst = 2, mtu = 1080;
        unsigned long total = 16384;
        sscanf(cmd + 10, "%d %lu %d", &dst, &total, &mtu);
        if (total == 0 || total > PAYLOAD_CAP) total = 16384;
        if (mtu < 200) mtu = 240;
        if (mtu > 1450) mtu = 1450;
        fill_test_data(shared_buf, (uint32_t)total);
        uint32_t expect_hash = compute_xor_digest(shared_buf, (uint32_t)total);

        raw_tx_total_bytes = (uint32_t)total;
        raw_tx_start_us = micros();
        raw_tx_waiting_report = true;

        uint32_t off = 0;
        while (off < total) {
            uint32_t chunk = (total - off > (uint32_t)mtu) ? (uint32_t)mtu : (uint32_t)(total - off);
            uint8_t hdr_and_chunk[1460];
            uint32_t meta[2] = {(uint32_t)total, off};
            memcpy(hdr_and_chunk, meta, 8);
            memcpy(hdr_and_chunk + 8, shared_buf + off, chunk);
            send_mesh_frame(MK_RAW_DATA, MY_NODE_ID, (uint8_t)dst, 3, hdr_and_chunk, chunk + 8, 8);
            off += chunk;
        }
        // Send completion query carrying expected total & hash
        uint32_t done_pl[2] = {(uint32_t)total, expect_hash};
        for (int k = 0; k < 3; ++k) {
            send_mesh_frame(MK_RAW_DONE, MY_NODE_ID, (uint8_t)dst, 3, (const uint8_t *)done_pl, 8, 8);
            delay(2);
        }
    } else if (strncmp(cmd, "beacon", 6) == 0) {
        emit_beacon();
        Serial.println("OK beacon");
    }
}

static void handle_serial() {
    static char line[128];
    static size_t pos = 0;
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') {
            line[pos] = '\0';
            pos = 0;
            if (line[0]) handle_command_str(line);
        } else if (pos + 1 < sizeof(line)) {
            line[pos++] = c;
        }
    }
}

static void process_mesh_frame(const RxFrame &f) {
    const MeshHdr *h = (const MeshHdr *)f.buf;
    const uint8_t *pl = f.buf + sizeof(MeshHdr);
    size_t pl_len = f.len - sizeof(MeshHdr);

    // Record direct neighbor link quality
    if (h->via_node < MAX_NODES) {
        peers[h->via_node].known = true;
        memcpy(peers[h->via_node].mac, f.src_mac, 6);
        peers[h->via_node].last_rssi = f.rssi;
        peers[h->via_node].noise_floor = f.noise;
        peers[h->via_node].last_seen_ms = millis();
        peers[h->via_node].rx_pkts++;
        ensure_espnow_peer(f.src_mac, f.rssi);
    }

    if (h->kind == MK_BEACON) return;

    // Optional artificial wall-fade packet drop for controlled FEC testing (applies to data/raw frames)
    if (sim_rx_drop_pct > 0 && (h->kind == MK_NLTP || h->kind == MK_RAW_DATA)) {
        if ((fast_rand() % 100u) < sim_rx_drop_pct) return;
    }

    // Multi-hop forwarding! If frame is destined for another node (or Laptop=3 via Gateway=0)
    if (h->dst_node != 0xFF && h->dst_node != MY_NODE_ID) {
        if (MY_NODE_ID == 0 && h->dst_node == 3) {
            // Gateway bridges L2 reply from Node 1 or Node 2 back to Laptop UDP socket!
            uint8_t src = h->src_node;
            if (src < 3 && have_laptop_for_node[src] && udp_socks[src] >= 0 && h->kind == MK_NLTP) {
                sendto(udp_socks[src], pl, pl_len, 0,
                       (struct sockaddr *)&laptop_client_for_node[src],
                       sizeof(laptop_client_for_node[src]));
            }
            return;
        }
        if (h->ttl > 1) {
            send_mesh_frame(h->kind, h->src_node, h->dst_node, (uint8_t)(h->ttl - 1), pl, pl_len, 8);
        }
        return;
    }

    // Frame is addressed to MY_NODE_ID:
    if (h->kind == MK_NLTP) {
        uint32_t valid0 = ep->valid_rx;
        uint8_t prev_peer = active_nltp_peer_node;
        active_nltp_peer_node = h->src_node;
        use_udp_transport = false;
        nltp_on_datagram(ep, pl, pl_len);
        if (ep->valid_rx == valid0) {
            active_nltp_peer_node = prev_peer;
        }
    } else if (h->kind == MK_PING && pl_len >= 8) {
        uint8_t pong[12];
        memcpy(pong, pl, 8); // echo seq + tx_us
        pong[8] = (uint8_t)f.rssi;
        pong[9] = (uint8_t)f.noise;
        pong[10] = MY_NODE_ID;
        pong[11] = h->via_node;
        send_mesh_frame(MK_PONG, MY_NODE_ID, h->src_node, 3, pong, sizeof(pong), 10);
    } else if (h->kind == MK_PONG && pl_len >= 10 && ping_active) {
        uint32_t sent_us = 0;
        memcpy(&sent_us, pl + 4, 4);
        uint32_t rtt_us = micros() - sent_us;
        int8_t rem_rssi = (int8_t)pl[8];
        ping_rcvd++;
        ping_rtt_sum_us += rtt_us;
        if (rtt_us < ping_rtt_min_us) ping_rtt_min_us = rtt_us;
        if (rtt_us > ping_rtt_max_us) ping_rtt_max_us = rtt_us;
        ping_remote_rssi_sum += rem_rssi;
        ping_local_rssi_sum += f.rssi;
    } else if (h->kind == MK_RAW_DATA && pl_len > 8) {
        uint32_t total = 0, off = 0;
        memcpy(&total, pl, 4);
        memcpy(&off, pl + 4, 4);
        uint32_t chunk = (uint32_t)(pl_len - 8);
        if (off == 0 || total != raw_rx_expected) {
            raw_rx_expected = total;
            raw_rx_bytes = 0;
            raw_rx_pkts = 0;
            memset(shared_buf, 0, PAYLOAD_CAP);
        }
        if (off + chunk <= PAYLOAD_CAP) {
            memcpy(shared_buf + off, pl + 8, chunk);
            raw_rx_bytes += chunk;
            raw_rx_pkts++;
        }
    } else if (h->kind == MK_RAW_DONE && pl_len >= 8) {
        uint32_t expected_total = 0, expected_hash = 0;
        memcpy(&expected_total, pl, 4);
        memcpy(&expected_hash, pl + 4, 4);
        uint32_t got_hash = compute_xor_digest(shared_buf, expected_total <= PAYLOAD_CAP ? expected_total : PAYLOAD_CAP);
        uint32_t rep[3] = {raw_rx_bytes, raw_rx_pkts, (uint32_t)(got_hash == expected_hash && raw_rx_bytes >= expected_total ? 1 : 0)};
        send_mesh_frame(MK_RAW_REPORT, MY_NODE_ID, h->src_node, 3, (const uint8_t *)rep, sizeof(rep), 10);
        raw_rx_expected = 0;
    } else if (h->kind == MK_RAW_REPORT && pl_len >= 12 && raw_tx_waiting_report) {
        raw_tx_waiting_report = false;
        uint32_t r_bytes = 0, r_pkts = 0, r_ok = 0;
        memcpy(&r_bytes, pl, 4);
        memcpy(&r_pkts, pl + 4, 4);
        memcpy(&r_ok, pl + 8, 4);
        uint32_t dt_us = micros() - raw_tx_start_us;
        float mbps = dt_us > 0 ? (raw_tx_total_bytes * 8.0f) / (float)dt_us : 0.0f;
        Serial.printf("RESULT raw_espnow ok=%lu rx_bytes=%lu total=%lu pkts=%lu us=%lu mbps=%.3f\n",
                      (unsigned long)r_ok, (unsigned long)r_bytes, (unsigned long)raw_tx_total_bytes,
                      (unsigned long)r_pkts, (unsigned long)dt_us, mbps);
    } else if (h->kind == MK_CMD_REMOTE && pl_len > 0) {
        char cmd_copy[96];
        size_t clen = pl_len < sizeof(cmd_copy) - 1 ? pl_len : sizeof(cmd_copy) - 1;
        memcpy(cmd_copy, pl, clen);
        cmd_copy[clen] = '\0';
        handle_command_str(cmd_copy, h->src_node);
    }
}

void setup() {
    Serial.begin(115200);
    delay(250);
    xorshift_state ^= esp_random();
    memset(peers, 0, sizeof(peers));

    rx_queue = xQueueCreate(16, sizeof(RxFrame));
    ep = (nltp_endpoint_t *)malloc(sizeof(nltp_endpoint_t));
    shared_buf = (uint8_t *)malloc(PAYLOAD_CAP);
    if (!rx_queue || !ep || !shared_buf) {
        Serial.printf("ERROR oom heap=%u\n", (unsigned)ESP.getFreeHeap());
        for (;;) delay(1000);
    }

    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);

#if MESH_NODE_ID == 0
    // Gateway Node 0: ONLY node that connects to home Wi-Fi!
    // (Do not call esp_wifi_set_protocol before WiFi.begin in Arduino-ESP32 3.3.x)
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.printf("NODE 0 (GATEWAY) joining Wi-Fi '%s'", WIFI_SSID);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
        delay(250);
        Serial.print('.');
    }
    Serial.println();
    if (WiFi.status() == WL_CONNECTED) {
        current_channel = (uint8_t)WiFi.channel();
        Serial.printf("GATEWAY_WIFI ok=1 ip=%s ch=%u rssi=%ddBm mac=%s\n",
                      WiFi.localIP().toString().c_str(), current_channel, WiFi.RSSI(),
                      WiFi.macAddress().c_str());
    } else {
        WiFi.disconnect(false, true);
        esp_wifi_set_channel(11, WIFI_SECOND_CHAN_NONE);
        current_channel = 11;
        Serial.printf("GATEWAY_WIFI ok=0 ch=%u mac=%s\n", current_channel, WiFi.macAddress().c_str());
    }
#else
    // Room Nodes 1 & 2: Explicitly disconnected from all Wi-Fi APs!
    WiFi.disconnect(false, true);
    esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
    esp_wifi_set_channel(11, WIFI_SECOND_CHAN_NONE);
#endif

    esp_wifi_set_max_tx_power(current_tx_pwr_qdbm);

    if (esp_now_init() != ESP_OK) {
        Serial.println("ERROR esp_now_init");
        for (;;) delay(1000);
    }
    uint32_t enow_ver = 0;
    esp_now_get_version(&enow_ver);
    esp_now_register_send_cb(on_espnow_tx);
    esp_now_register_recv_cb(on_espnow_rx);
    ensure_espnow_peer(BCAST_MAC);

#if MESH_NODE_ID != 0
    lock_onto_mesh_channel();
#endif

    bind_udp_sockets();
    nltp_init(ep, esp_random(), MAX_KBPS, nltp_transport_send_cb, now_cb, nullptr);
    nltp_set_rx_buffer(ep, shared_buf, PAYLOAD_CAP);

    emit_beacon();
    Serial.printf("READY node=%u role=%s enow_v=%lu ch=%u mac=%s heap=%u\n",
                  MY_NODE_ID, (MY_NODE_ID == 0 ? "GATEWAY_WIFI" : "MESH_ROOM_OFF_WIFI"),
                  (unsigned long)enow_ver, current_channel, WiFi.macAddress().c_str(),
                  (unsigned)ESP.getFreeHeap());
}

void loop() {
    bool busy = false;

    // 1. Drain Layer-2 NLTP-NOW / Mesh frames
    RxFrame f;
    while (xQueueReceive(rx_queue, &f, 0) == pdTRUE) {
        busy = true;
        process_mesh_frame(f);
    }

    // 2. Drain UDP sockets (Gateway Laptop Bridge :9700..9702 + SoftAP UDP mode)
    int sock_cnt = (MY_NODE_ID == 0 && !use_udp_transport) ? 3 : 1;
    uint8_t ubuf[1200];
    for (int s = 0; s < sock_cnt; ++s) {
        if (udp_socks[s] < 0) continue;
        for (;;) {
            struct sockaddr_in from;
            socklen_t fl = sizeof(from);
            int n = recvfrom(udp_socks[s], ubuf, sizeof(ubuf), 0, (struct sockaddr *)&from, &fl);
            if (n <= 0) break;
            busy = true;
            if (use_udp_transport) {
                uint32_t v0 = ep->valid_rx;
                struct sockaddr_in prev = udp_peer_addr;
                bool had = have_udp_peer;
                udp_peer_addr = from;
                have_udp_peer = true;
                nltp_on_datagram(ep, ubuf, (size_t)n);
                if (ep->valid_rx == v0) { udp_peer_addr = prev; have_udp_peer = had; }
            } else if (MY_NODE_ID == 0) {
                laptop_client_for_node[s] = from;
                have_laptop_for_node[s] = true;
                if (s == 0) {
                    // Direct Laptop <-> Gateway Node 0
                    active_nltp_peer_node = 3;
                    nltp_on_datagram(ep, ubuf, (size_t)n);
                } else {
                    // Transparent Wi-Fi Gateway -> Off-Wi-Fi Room Node s (1 or 2) over NLTP-NOW!
                    send_mesh_frame(MK_NLTP, 3, (uint8_t)s, 3, ubuf, (size_t)n, 8);
                }
            }
        }
    }

    // 3. Drive NLTP C99 state machine (FEC parity repairs, pacing, ACKs)
    nltp_poll(ep);

    // 4. Report completed NLTP RX / TX streams
    if (ep->rx.active && ep->rx.stream_id != rx_sid_seen) {
        rx_sid_seen = ep->rx.stream_id;
        rx_first_ms = millis();
    }
    if (ep->rx.completed && rx_reported_sid != ep->rx.stream_id) {
        rx_reported_sid = ep->rx.stream_id;
        uint32_t dt_ms = millis() - rx_first_ms;
        if (dt_ms == 0) dt_ms = 1;
        Serial.printf("RESULT nltp_rx ok=%d bytes=%lu ms=%lu mbps=%.3f mode=%s\n",
                      nltp_rx_sha_ok(ep), (unsigned long)nltp_rx_len(ep), (unsigned long)dt_ms,
                      (nltp_rx_len(ep) * 8.0f) / (dt_ms * 1000.0f),
                      use_udp_transport ? "softap_udp" : "nltp_now_l2");
    }
    if (!tx_reported && nltp_tx_done(ep)) {
        tx_reported = true;
        uint32_t dt_us = micros() - tx_started_us;
        float mbps = dt_us > 0 ? (tx_len * 8.0f) / (float)dt_us : 0.0f;
        Serial.printf("RESULT nltp_tx ok=%d bytes=%lu us=%lu ms=%.2f mbps=%.3f mode=%s l2_ok=%lu l2_fail=%lu\n",
                      nltp_tx_verified(ep), (unsigned long)tx_len, (unsigned long)dt_us,
                      dt_us / 1000.0f, mbps, use_udp_transport ? "softap_udp" : "nltp_now_l2",
                      (unsigned long)tx_ok_cnt, (unsigned long)tx_fail_cnt);
    }

    // 5. Step active range/latency ping sweep
    if (ping_active) {
        uint32_t now_u = micros();
        uint32_t gap_u = (current_rate_mode == 5) ? 32000u : 12000u;
        if (ping_sent < ping_total && (now_u - ping_last_tx_us >= gap_u)) {
            uint8_t pbuf[1408];
            memset(pbuf, 0xA5, ping_payload_len);
            uint32_t seq32 = ping_sent;
            memcpy(pbuf, &seq32, 4);
            memcpy(pbuf + 4, &now_u, 4);
            send_mesh_frame(MK_PING, MY_NODE_ID, ping_target, 3, pbuf, ping_payload_len, 10);
            ping_sent++;
            ping_last_tx_us = now_u;
        } else if (ping_sent >= ping_total && (now_u - ping_last_tx_us >= 120000u)) {
            ping_active = false;
            float pdr = ping_total > 0 ? (100.0f * ping_rcvd) / ping_total : 0.0f;
            float avg_ms = ping_rcvd > 0 ? (ping_rtt_sum_us / (float)ping_rcvd) / 1000.0f : 0.0f;
            float min_ms = ping_rcvd > 0 ? ping_rtt_min_us / 1000.0f : 0.0f;
            float max_ms = ping_rcvd > 0 ? ping_rtt_max_us / 1000.0f : 0.0f;
            float rem_rssi = ping_rcvd > 0 ? ping_remote_rssi_sum / (float)ping_rcvd : -100.0f;
            float loc_rssi = ping_rcvd > 0 ? ping_local_rssi_sum / (float)ping_rcvd : -100.0f;
            Serial.printf("RESULT ping dst=%u sent=%u rcvd=%u pdr=%.1f avg_ms=%.2f min_ms=%.2f max_ms=%.2f tx_rssi=%.1f rx_rssi=%.1f\n",
                          ping_target, ping_total, ping_rcvd, pdr, avg_ms, min_ms, max_ms, rem_rssi, loc_rssi);
        }
    }

    // 6. Periodic mesh discovery beacon (every 1.5s when idle)
    if (!ping_active && tx_reported && !ep->rx.active && (millis() - last_beacon_ms >= 1500u)) {
        last_beacon_ms = millis();
        emit_beacon();
    }

    handle_serial();
    if (!busy) delay(1);
}
