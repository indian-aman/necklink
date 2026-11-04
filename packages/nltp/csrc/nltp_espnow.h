/*
 * nltp_espnow.h — Portable Zero-Wrapper ESP-NOW v2 (1,470 B MTU) Transport for NLTP C99
 *
 * Wire Interoperability Guarantee:
 *   Every ESP-NOW v2 vendor action frame carries an UNMODIFIED NLTP v1.2/v1.3 datagram
 *   starting with magic bytes "NL" (0x4E 0x4C) — zero custom L2 wrapper headers.
 *   An encrypted full-size shard is 20 B AEAD header + 12 B shard header + 2 B length + 960 B data
 *   + 16 B Poly1305 tag = 1,010 B (well below ESP_NOW_MAX_DATA_LEN_V2 = 1,470 B), so full-sized NLTP shards
 *   cross ESP-NOW v2 without fragmentation and can be bridged byte-for-byte transparently
 *   to/from Python (`pynltp`), Linux/macOS/Windows UDP (`nltp_cli`), or 4G relay rooms.
 *
 * Compatible with:
 *   - ESP-IDF v5.x (ESP32, ESP32-S2, ESP32-S3, ESP32-C3, ESP32-C5 dual-band, ESP32-C6)
 *   - Arduino-ESP32 core v3.x (ESP-IDF 5.x base)
 */
#ifndef NLTP_ESPNOW_H
#define NLTP_ESPNOW_H

#if defined(ESP_PLATFORM) || defined(ARDUINO_ARCH_ESP32) || defined(ESP32)

#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#include "esp_err.h"
#include "esp_idf_version.h"
#if ESP_IDF_VERSION_MAJOR < 5
#error "nltp_espnow.h needs ESP-IDF 5.x (Arduino-ESP32 core 3.x); the UDP transport works on older cores"
#endif
#include "esp_now.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#include "nltp_core.h"

#ifdef __cplusplus
extern "C" {
#endif

#define NLTP_ESPNOW_V1_MTU         250u
#define NLTP_ESPNOW_V1_SHARD_BYTES 192u  /* 20B AEAD hdr + 12B shard hdr + 2B len + 192B data + 16B tag = 242B <= 250B */

#ifndef ESP_NOW_MAX_DATA_LEN_V2
#define NLTP_ESPNOW_MTU 250u
#else
#define NLTP_ESPNOW_MTU ESP_NOW_MAX_DATA_LEN_V2
#endif

#ifndef NLTP_ESPNOW_RX_QUEUE_DEPTH
#define NLTP_ESPNOW_RX_QUEUE_DEPTH 10u
#endif

/* Hub slots. Each slot embeds a full nltp_endpoint_t (~66.7 KB on ESP32 with the default
 * NLTP_RX_SLOTS=8, ~19.8 KB with -DNLTP_RX_SLOTS=2 -DNLTP_RX_MAX_GENS=64), so keep this small. */
#ifndef NLTP_ESPNOW_MAX_PEERS
#define NLTP_ESPNOW_MAX_PEERS 2u
#endif

/* Compile-time guard: nltp_espnow_hub_t must fit in ESP32 RAM next to Wi-Fi. Raise only if you know
 * your chip has the memory (e.g. PSRAM); otherwise lower NLTP_ESPNOW_MAX_PEERS or NLTP_RX_SLOTS. */
#ifndef NLTP_ESPNOW_HUB_MAX_BYTES
#define NLTP_ESPNOW_HUB_MAX_BYTES (160u * 1024u)
#endif
typedef char nltp_espnow_hub_exceeds_NLTP_ESPNOW_HUB_MAX_BYTES_lower_MAX_PEERS_or_RX_SLOTS
    [(sizeof(nltp_endpoint_t) * NLTP_ESPNOW_MAX_PEERS <= NLTP_ESPNOW_HUB_MAX_BYTES) ? 1 : -1];

/* ESP-NOW allows at most 20 peers in total (ESP_NOW_MAX_TOTAL_PEER_NUM). NLTP tracks the peers it adds
 * and evicts the least recently used one when it needs room, leaving headroom for the application. */
#ifndef NLTP_ESPNOW_PEER_CACHE
#define NLTP_ESPNOW_PEER_CACHE 16u
#endif

typedef enum {
    NLTP_ESPNOW_RATE_1M      = 0,  /* 802.11b 1 Mbps DSSS (~-95 dBm) */
    NLTP_ESPNOW_RATE_11M     = 1,  /* 802.11b 11 Mbps CCK (~-88 dBm) */
    NLTP_ESPNOW_RATE_MCS4    = 2,  /* 802.11n HT20 MCS4 (~39 Mbps) */
    NLTP_ESPNOW_RATE_MCS7    = 3,  /* 802.11n HT20 MCS7 SGI (~72.2 Mbps raw) */
    NLTP_ESPNOW_RATE_AUTO    = 4,  /* RSSI-adaptive across MCS7 / MCS4 / 11M / 1M */
    NLTP_ESPNOW_RATE_LR_250K = 5,  /* Espressif Long Range 250 kbps (~-102 dBm) */
    NLTP_ESPNOW_RATE_LR_500K = 6   /* Espressif Long Range 500 kbps (~-100 dBm) */
} nltp_espnow_rate_t;

typedef struct {
    uint16_t len;
    int8_t rssi;
    int8_t noise_floor;
    uint8_t src_mac[6];
    uint8_t dst_mac[6];
    uint8_t data[NLTP_ESPNOW_MTU];
} nltp_espnow_rx_item_t;

/* Optional hook invoked in poll() BEFORE nltp_on_datagram(); return 0 to drop,
 * 1 to feed local NLTP endpoint, or 2 if bridged elsewhere (skip local endpoint). */
typedef int (*nltp_espnow_rx_filter_fn)(void *user,
                                        const uint8_t src_mac[6],
                                        int8_t rssi,
                                        const uint8_t *pkt,
                                        size_t len);

/* Optional hook for non-NLTP raw ESP-NOW v1/v2 frames sharing the same radio. */
typedef void (*nltp_espnow_non_nltp_rx_fn)(const esp_now_recv_info_t *info,
                                           const uint8_t *data,
                                           int len);

/* Global singleton ISR/Wi-Fi-task bridge (ESP-IDF provides one global esp_now callback). */
typedef struct {
    QueueHandle_t rx_q;
    int32_t tx_inflight;            /* updated with __atomic builtins: Wi-Fi task and app task share it */
    uint32_t tx_ok;
    uint32_t tx_fail;
    uint32_t tx_oversize_drops;     /* v1 mode: frames > 250 B that could not be sent */
    uint32_t peer_evictions;        /* ESP-NOW peer-table entries freed to make room */
    volatile uint16_t max_tx_frame_len;
    uint8_t peer_n;
    uint8_t peer_mac[NLTP_ESPNOW_PEER_CACHE][6];
    uint32_t peer_used_ms[NLTP_ESPNOW_PEER_CACHE];
    uint8_t initialized;
    uint8_t lr_enabled;
    uint8_t channel;
    wifi_interface_t ifidx;
    uint32_t espnow_version;
    nltp_espnow_non_nltp_rx_fn non_nltp_rx_cb;
} nltp_espnow_bus_t;

static nltp_espnow_bus_t g_nltp_espnow_bus;  /* zero-initialised; ifidx is set by nltp_espnow_bus_init() */

static inline uint32_t nltp_espnow_now_ms(void *user) {
    (void)user;
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

static inline void nltp_espnow_set_wifi_lr_protocol(wifi_interface_t ifidx, int enable_lr, uint8_t channel) {
    uint8_t bmap = (uint8_t)(WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N |
                             (enable_lr ? WIFI_PROTOCOL_LR : 0));
#if defined(SOC_WIFI_SUPPORT_5G) && SOC_WIFI_SUPPORT_5G
    /* Dual-band chips (e.g. ESP32-C5): configure 2.4 GHz protocol bitmap via esp_wifi_set_protocols */
    wifi_protocols_t protos;
    memset(&protos, 0, sizeof(protos));
    protos.ghz_2g = bmap;
    protos.ghz_5g = 0;
    esp_wifi_set_protocols(ifidx, &protos);
#else
    esp_wifi_set_protocol(ifidx, bmap);
#endif
    if (channel >= 1u && channel <= 14u) {
        esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
    }
    g_nltp_espnow_bus.lr_enabled = (uint8_t)(enable_lr ? 1u : 0u);
}

static inline esp_err_t nltp_espnow_apply_peer_rate(const uint8_t mac[6],
                                                    nltp_espnow_rate_t mode,
                                                    int8_t rssi_hint) {
    static const uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    esp_now_rate_config_t cfg;
    nltp_espnow_rate_t eff = mode;

    if (memcmp(mac, bcast, 6) == 0) return ESP_OK;

    if ((eff == NLTP_ESPNOW_RATE_LR_250K || eff == NLTP_ESPNOW_RATE_LR_500K) && !g_nltp_espnow_bus.lr_enabled) {
        nltp_espnow_set_wifi_lr_protocol(g_nltp_espnow_bus.ifidx, 1, g_nltp_espnow_bus.channel);
    }

    if (eff == NLTP_ESPNOW_RATE_AUTO) {
        if (rssi_hint >= -67) eff = NLTP_ESPNOW_RATE_MCS7;
        else if (rssi_hint >= -76) eff = NLTP_ESPNOW_RATE_MCS4;
        else if (rssi_hint >= -86) eff = NLTP_ESPNOW_RATE_11M;
        else eff = NLTP_ESPNOW_RATE_1M;
    }

    memset(&cfg, 0, sizeof(cfg));
    switch (eff) {
    case NLTP_ESPNOW_RATE_1M:
        cfg.phymode = WIFI_PHY_MODE_11B;
        cfg.rate = WIFI_PHY_RATE_1M_L;
        break;
    case NLTP_ESPNOW_RATE_11M:
        cfg.phymode = WIFI_PHY_MODE_11B;
        cfg.rate = WIFI_PHY_RATE_11M_S;
        break;
    case NLTP_ESPNOW_RATE_MCS4:
        cfg.phymode = WIFI_PHY_MODE_HT20;
        cfg.rate = WIFI_PHY_RATE_MCS4_LGI;
        break;
    case NLTP_ESPNOW_RATE_LR_250K:
        cfg.phymode = WIFI_PHY_MODE_LR;
        cfg.rate = WIFI_PHY_RATE_LORA_250K;
        break;
    case NLTP_ESPNOW_RATE_LR_500K:
        cfg.phymode = WIFI_PHY_MODE_LR;
        cfg.rate = WIFI_PHY_RATE_LORA_500K;
        break;
    case NLTP_ESPNOW_RATE_MCS7:
    default:
        cfg.phymode = WIFI_PHY_MODE_HT20;
        cfg.rate = WIFI_PHY_RATE_MCS7_SGI;
        break;
    }
    cfg.ersu = false;
    cfg.dcm = false;
    return esp_now_set_peer_rate_config(mac, &cfg);
}

static inline uint32_t nltp_espnow_now_ms_u32(void) {
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

static inline int nltp_espnow_is_bcast(const uint8_t mac[6]) {
    static const uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    return memcmp(mac, bcast, 6) == 0;
}

static inline int nltp_espnow_cache_find(const uint8_t mac[6]) {
    uint32_t i;
    for (i = 0; i < g_nltp_espnow_bus.peer_n; ++i) {
        if (memcmp(g_nltp_espnow_bus.peer_mac[i], mac, 6) == 0) return (int)i;
    }
    return -1;
}

static inline void nltp_espnow_cache_remove(int idx) {
    uint32_t last = (uint32_t)g_nltp_espnow_bus.peer_n - 1u;
    if (idx < 0 || (uint32_t)idx > last) return;
    if ((uint32_t)idx != last) {
        memcpy(g_nltp_espnow_bus.peer_mac[idx], g_nltp_espnow_bus.peer_mac[last], 6);
        g_nltp_espnow_bus.peer_used_ms[idx] = g_nltp_espnow_bus.peer_used_ms[last];
    }
    g_nltp_espnow_bus.peer_n = (uint8_t)last;
}

/* Frees the least recently used NLTP-added peer from the ESP-NOW peer table. */
static inline int nltp_espnow_evict_lru_peer(void) {
    uint32_t i, now = nltp_espnow_now_ms_u32(), best_age = 0;
    int best = -1;
    for (i = 0; i < g_nltp_espnow_bus.peer_n; ++i) {
        uint32_t age = now - g_nltp_espnow_bus.peer_used_ms[i];
        if (best < 0 || age > best_age) {
            best = (int)i;
            best_age = age;
        }
    }
    if (best < 0) return 0;
    esp_now_del_peer(g_nltp_espnow_bus.peer_mac[best]);
    nltp_espnow_cache_remove(best);
    g_nltp_espnow_bus.peer_evictions = g_nltp_espnow_bus.peer_evictions + 1u;
    return 1;
}

/* Removes `mac` from the ESP-NOW peer table if NLTP added it (e.g. when a hub slot expires). */
static inline void nltp_espnow_forget_peer(const uint8_t mac[6]) {
    int idx = nltp_espnow_cache_find(mac);
    if (idx < 0) return;
    esp_now_del_peer(mac);
    nltp_espnow_cache_remove(idx);
}

/* Adds `mac` to the ESP-NOW peer table if needed (evicting the least recently used NLTP peer when the
 * table is full) and applies the PHY rate. Call only for peers that sent an authentic packet or that
 * the application chose explicitly: the table holds at most 20 entries. */
static inline esp_err_t nltp_espnow_ensure_peer(const uint8_t mac[6],
                                                nltp_espnow_rate_t rate_mode,
                                                int8_t rssi_hint) {
    int idx = nltp_espnow_cache_find(mac);
    if (idx >= 0) {
        g_nltp_espnow_bus.peer_used_ms[idx] = nltp_espnow_now_ms_u32();
    }
    if (!esp_now_is_peer_exist(mac)) {
        esp_now_peer_info_t pi;
        esp_err_t err;
        int tries;
        if (idx >= 0) {
            nltp_espnow_cache_remove(idx);  /* deleted behind our back (e.g. by the application) */
            idx = -1;
        }
        memset(&pi, 0, sizeof(pi));
        memcpy(pi.peer_addr, mac, 6);
        pi.channel = 0;  /* follow active interface channel */
        pi.ifidx = g_nltp_espnow_bus.ifidx;
        pi.encrypt = false;  /* NLTP provides 256-bit ChaCha20-Poly1305 AEAD on top */
        if (!nltp_espnow_is_bcast(mac) && g_nltp_espnow_bus.peer_n >= NLTP_ESPNOW_PEER_CACHE) {
            nltp_espnow_evict_lru_peer();
        }
        err = esp_now_add_peer(&pi);
        for (tries = 0; err == ESP_ERR_ESPNOW_FULL && tries < 2 && nltp_espnow_evict_lru_peer(); ++tries) {
            err = esp_now_add_peer(&pi);
        }
        if (err != ESP_OK) return err;
        if (!nltp_espnow_is_bcast(mac) && g_nltp_espnow_bus.peer_n < NLTP_ESPNOW_PEER_CACHE) {
            memcpy(g_nltp_espnow_bus.peer_mac[g_nltp_espnow_bus.peer_n], mac, 6);
            g_nltp_espnow_bus.peer_used_ms[g_nltp_espnow_bus.peer_n] = nltp_espnow_now_ms_u32();
            g_nltp_espnow_bus.peer_n = (uint8_t)(g_nltp_espnow_bus.peer_n + 1u);
        }
    }
    return nltp_espnow_apply_peer_rate(mac, rate_mode, rssi_hint);
}

static inline void nltp_espnow_inflight_dec(void) {
    int32_t cur = __atomic_load_n(&g_nltp_espnow_bus.tx_inflight, __ATOMIC_RELAXED);
    while (cur > 0 &&
           !__atomic_compare_exchange_n(&g_nltp_espnow_bus.tx_inflight, &cur, cur - 1, 0,
                                        __ATOMIC_RELAXED, __ATOMIC_RELAXED)) {
    }
}

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
static void nltp_espnow_tx_isr_cb(const esp_now_send_info_t *tx_info, esp_now_send_status_t status) {
#else
static void nltp_espnow_tx_isr_cb(const uint8_t *tx_info, esp_now_send_status_t status) {
#endif
    (void)tx_info;
    nltp_espnow_inflight_dec();
    if (status == ESP_NOW_SEND_SUCCESS) {
        __atomic_add_fetch(&g_nltp_espnow_bus.tx_ok, 1u, __ATOMIC_RELAXED);
    } else {
        __atomic_add_fetch(&g_nltp_espnow_bus.tx_fail, 1u, __ATOMIC_RELAXED);
    }
}

static void nltp_espnow_rx_isr_cb(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    nltp_espnow_rx_item_t item;
    if (!data || len <= 0 || len > (int)NLTP_ESPNOW_MTU) {
        return;
    }
    /* Route non-NLTP vendor frames to optional coexistence / raw ESP-NOW handler */
    if (len < (int)NLTP_HEADER_BYTES ||
        data[0] != NLTP_MAGIC_0 ||
        data[1] != NLTP_MAGIC_1 ||
        (((uint8_t)data[2] >> 4) != NLTP_VERSION)) {
        if (g_nltp_espnow_bus.non_nltp_rx_cb) {
            g_nltp_espnow_bus.non_nltp_rx_cb(info, data, len);
        }
        return;
    }
    if (!g_nltp_espnow_bus.rx_q) return;

    item.len = (uint16_t)len;
    item.rssi = (info && info->rx_ctrl) ? (int8_t)info->rx_ctrl->rssi : -60;
    item.noise_floor = (info && info->rx_ctrl) ? (int8_t)info->rx_ctrl->noise_floor : -96;
    if (info && info->src_addr) memcpy(item.src_mac, info->src_addr, 6);
    else memset(item.src_mac, 0, 6);
    if (info && info->des_addr) memcpy(item.dst_mac, info->des_addr, 6);
    else memset(item.dst_mac, 0xFF, 6);
    memcpy(item.data, data, (size_t)len);
    xQueueSend(g_nltp_espnow_bus.rx_q, &item, 0);
}

/* Initializes the shared ESP-NOW v2 bus on `ifidx` and `channel` (pass channel=0 to keep current Wi-Fi channel). */
static inline esp_err_t nltp_espnow_bus_init(wifi_interface_t ifidx, uint8_t channel, int enable_lr) {
    static const uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    esp_err_t err;
    if (g_nltp_espnow_bus.initialized) {
        if (channel >= 1u && channel <= 14u) {
            g_nltp_espnow_bus.channel = channel;
            esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
        }
        if (enable_lr && !g_nltp_espnow_bus.lr_enabled) {
            nltp_espnow_set_wifi_lr_protocol(ifidx, 1, g_nltp_espnow_bus.channel);
        }
        return ESP_OK;
    }

    g_nltp_espnow_bus.ifidx = ifidx;
    g_nltp_espnow_bus.channel = channel;
    if (enable_lr) {
        nltp_espnow_set_wifi_lr_protocol(ifidx, 1, channel);
    } else if (channel >= 1u && channel <= 14u) {
        esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
    }

    if (!g_nltp_espnow_bus.rx_q) {
        g_nltp_espnow_bus.rx_q = xQueueCreate(NLTP_ESPNOW_RX_QUEUE_DEPTH, sizeof(nltp_espnow_rx_item_t));
        if (!g_nltp_espnow_bus.rx_q) return ESP_ERR_NO_MEM;
    }

    err = esp_now_init();
    if (err != ESP_OK) return err;

    esp_now_get_version(&g_nltp_espnow_bus.espnow_version);
    esp_now_register_send_cb(nltp_espnow_tx_isr_cb);
    esp_now_register_recv_cb(nltp_espnow_rx_isr_cb);
    nltp_espnow_ensure_peer(bcast, NLTP_ESPNOW_RATE_1M, -60);
    g_nltp_espnow_bus.initialized = 1u;
    return ESP_OK;
}

/* Raw L2 transmit helper with hardware MAC queue backpressure (prevents ESP_ERR_ESPNOW_NO_MEM during bursts). */
static inline esp_err_t nltp_espnow_raw_send(const uint8_t dst_mac[6],
                                             const uint8_t *pkt,
                                             size_t len,
                                             nltp_espnow_rate_t rate_mode,
                                             int8_t rssi_hint) {
    int attempt;
    int64_t t0;
    if (len > NLTP_ESPNOW_MTU) return ESP_ERR_INVALID_SIZE;
    if ((uint16_t)len > g_nltp_espnow_bus.max_tx_frame_len) {
        g_nltp_espnow_bus.max_tx_frame_len = (uint16_t)len;
    }
    {
        esp_err_t perr = nltp_espnow_ensure_peer(dst_mac, rate_mode, rssi_hint);
        if (perr != ESP_OK) {
            __atomic_add_fetch(&g_nltp_espnow_bus.tx_fail, 1u, __ATOMIC_RELAXED);
            return perr;
        }
    }

    t0 = esp_timer_get_time();
    while (__atomic_load_n(&g_nltp_espnow_bus.tx_inflight, __ATOMIC_RELAXED) >= 7 &&
           (esp_timer_get_time() - t0) < 15000LL) {
        esp_rom_delay_us(60);
    }

    for (attempt = 0; attempt < 5; ++attempt) {
        esp_err_t rc;
        __atomic_add_fetch(&g_nltp_espnow_bus.tx_inflight, 1, __ATOMIC_RELAXED);
        rc = esp_now_send(dst_mac, pkt, len);
        if (rc == ESP_OK) return ESP_OK;
        nltp_espnow_inflight_dec();
        if (rc == ESP_ERR_ESPNOW_NO_MEM) {
            esp_rom_delay_us(180);
            continue;
        }
        return rc;
    }
    return ESP_ERR_ESPNOW_NO_MEM;
}

/* =========================================================================
 * 1. Single-Peer NLTP over ESP-NOW v2 Endpoint (`nltp_espnow_node_t`)
 * ========================================================================= */

typedef struct {
    nltp_endpoint_t ep;
    uint8_t peer_mac[6];
    uint8_t has_peer;
    uint8_t v1_mode;
    int8_t last_rssi;
    int8_t last_noise;
    nltp_espnow_rate_t rate_mode;
    nltp_espnow_rx_filter_fn rx_filter;
    void *filter_user;
} nltp_espnow_node_t;

static void nltp_espnow_node_send_cb(void *user, const uint8_t *pkt, size_t len) {
    nltp_espnow_node_t *node = (nltp_espnow_node_t *)user;
    if (!node || !node->has_peer) return;
    if (node->v1_mode && len > NLTP_ESPNOW_V1_MTU) {
        __atomic_add_fetch(&g_nltp_espnow_bus.tx_oversize_drops, 1u, __ATOMIC_RELAXED);
        return;
    }
    nltp_espnow_raw_send(node->peer_mac, pkt, len, node->rate_mode, node->last_rssi);
}

static inline esp_err_t nltp_espnow_node_init(nltp_espnow_node_t *node,
                                              uint8_t channel,
                                              nltp_espnow_rate_t rate_mode,
                                              const char *psk,
                                              uint8_t *rx_buf,
                                              uint32_t rx_cap,
                                              uint32_t max_kbps) {
    int want_lr = (rate_mode == NLTP_ESPNOW_RATE_LR_250K || rate_mode == NLTP_ESPNOW_RATE_LR_500K);
    esp_err_t err = nltp_espnow_bus_init(WIFI_IF_STA, channel, want_lr);
    if (err != ESP_OK) return err;

    memset(node, 0, sizeof(*node));
    node->rate_mode = rate_mode;
    node->last_rssi = -60;
    node->last_noise = -96;

    nltp_init(&node->ep, esp_random() | 1u, max_kbps, nltp_espnow_node_send_cb, nltp_espnow_now_ms, node);
    nltp_set_rx_buffer(&node->ep, rx_buf, rx_cap);
#if NLTP_ESPNOW_MTU <= NLTP_ESPNOW_V1_MTU
    node->v1_mode = 1u;
    nltp_set_shard_bytes(&node->ep, NLTP_ESPNOW_V1_SHARD_BYTES);
#endif
    if (psk && psk[0]) {
        uint8_t key32[32];
        nltp_key_from_psk(psk, key32);
        nltp_set_key(&node->ep, key32);
        memset(key32, 0, sizeof(key32));
    }
    return ESP_OK;
}

static inline void nltp_espnow_node_set_peer(nltp_espnow_node_t *node, const uint8_t peer_mac[6]) {
    memcpy(node->peer_mac, peer_mac, 6);
    node->has_peer = 1u;
    nltp_espnow_ensure_peer(peer_mac, node->rate_mode, node->last_rssi);
}

static inline void nltp_espnow_node_set_rate(nltp_espnow_node_t *node, nltp_espnow_rate_t rate_mode) {
    int want_lr = (rate_mode == NLTP_ESPNOW_RATE_LR_250K || rate_mode == NLTP_ESPNOW_RATE_LR_500K);
    if (want_lr != (int)g_nltp_espnow_bus.lr_enabled) {
        nltp_espnow_set_wifi_lr_protocol(g_nltp_espnow_bus.ifidx, want_lr, g_nltp_espnow_bus.channel);
    }
    node->rate_mode = rate_mode;
    if (node->has_peer) {
        nltp_espnow_apply_peer_rate(node->peer_mac, rate_mode, node->last_rssi);
    }
}

static inline void nltp_espnow_node_set_v1_mode(nltp_espnow_node_t *node, int enable_v1) {
    node->v1_mode = (uint8_t)(enable_v1 ? 1u : 0u);
    nltp_set_shard_bytes(&node->ep, enable_v1 ? NLTP_ESPNOW_V1_SHARD_BYTES : (uint16_t)NLTP_SHARD_BYTES);
}

static inline void nltp_espnow_node_poll(nltp_espnow_node_t *node) {
    nltp_espnow_rx_item_t item;
    while (g_nltp_espnow_bus.rx_q && xQueueReceive(g_nltp_espnow_bus.rx_q, &item, 0) == pdTRUE) {
        uint32_t valid_before;
        uint8_t prev_mac[6];
        uint8_t had_peer;

        if (node->rx_filter) {
            int action = node->rx_filter(node->filter_user, item.src_mac, item.rssi, item.data, item.len);
            if (action != 1) continue;
        }

        valid_before = node->ep.valid_rx;
        memcpy(prev_mac, node->peer_mac, 6);
        had_peer = node->has_peer;

        /* Replies sent while handling this packet go to its sender. The ESP-NOW peer-table entry is
         * only created when a reply is actually sent (nltp_espnow_raw_send), i.e. for packets that
         * passed the CRC / ChaCha20-Poly1305 check, so forged frames cannot fill the 20-entry table. */
        memcpy(node->peer_mac, item.src_mac, 6);
        node->has_peer = 1u;

        nltp_on_datagram(&node->ep, item.data, item.len);

        if (node->ep.valid_rx == valid_before) {
            /* Rejected: keep the previous peer */
            memcpy(node->peer_mac, prev_mac, 6);
            node->has_peer = had_peer;
        } else {
            node->last_rssi = item.rssi;
            node->last_noise = item.noise_floor;
        }
    }
    nltp_poll(&node->ep);
}

/* =========================================================================
 * 2. Multi-Peer MAC-Keyed Bridge Hub (`nltp_espnow_hub_t`)
 *    Keyed directly by L2 hardware MAC (`esp_now_recv_info->src_addr`) —
 *    ideal for multi-transmitter sensor/waiter-call bridges with allow-lists.
 * ========================================================================= */

typedef void (*nltp_espnow_hub_rx_fn)(void *user,
                                      const uint8_t src_mac[6],
                                      int8_t rssi,
                                      const uint8_t *data,
                                      uint32_t len,
                                      const char *filename,
                                      int sha_ok);

typedef int (*nltp_espnow_allow_mac_fn)(void *user, const uint8_t src_mac[6]);

typedef struct {
    uint8_t in_use;
    uint8_t v1_mode;
    uint8_t mac[6];
    int8_t last_rssi;
    uint32_t last_active_ms;
    uint32_t last_reported_sid;
    uint32_t last_reported_cid;
    nltp_espnow_rate_t rate_mode;
    nltp_endpoint_t ep;
    uint8_t *rx_buf;
    uint32_t rx_cap;
} nltp_espnow_slot_t;

typedef struct {
    nltp_espnow_slot_t slots[NLTP_ESPNOW_MAX_PEERS];
    uint8_t key32[32];
    uint8_t encrypted;
    uint8_t v1_mode;
    uint32_t max_kbps;
    uint32_t idle_timeout_ms;
    nltp_espnow_rate_t rate_mode;
    nltp_espnow_allow_mac_fn allow_cb;
    nltp_espnow_hub_rx_fn on_message_cb;
    void *user;
} nltp_espnow_hub_t;

/* Stateless check that `pkt` is a well-formed NLTP frame for this hub: with a key, a valid
 * ChaCha20-Poly1305 tag (no decryption, no replay state); without a key, a valid CRC32 (which only
 * filters corruption -- plaintext hubs should also set an allow-list callback). */
static inline int nltp_espnow_frame_ok(const uint8_t *pkt, size_t len, int encrypted, const uint8_t key32[32]) {
    if (encrypted) {
        uint8_t type, flags;
        uint32_t cid, ts;
        size_t pt_len;
        return nltp_unpack_encrypted_frame(pkt, len, key32, NULL, &type, &flags, &cid, &ts, NULL, &pt_len);
    }
    if (len < NLTP_HEADER_BYTES || (pkt[3] & NLTP_FLAG_ENCRYPTED)) return 0;
    {
        uint8_t hdr[NLTP_HEADER_BYTES];
        uint32_t crc;
        memcpy(hdr, pkt, NLTP_HEADER_BYTES);
        nltp_write_u32_le(hdr + 8, 0u);
        crc = nltp_crc32_ieee(hdr, NLTP_HEADER_BYTES, 0u);
        if (len > NLTP_HEADER_BYTES) crc = nltp_crc32_ieee(pkt + NLTP_HEADER_BYTES, len - NLTP_HEADER_BYTES, crc);
        return crc == nltp_read_u32_le(pkt + 8);
    }
}

/* A slot is busy while it is receiving or sending an unfinished transfer; busy slots are never evicted. */
static inline int nltp_espnow_slot_busy(const nltp_espnow_slot_t *slot) {
    return (slot->ep.rx.active && !slot->ep.rx.completed) || (slot->ep.tx.active && !slot->ep.tx.completed);
}

static inline void nltp_espnow_hub_release_slot(nltp_espnow_slot_t *slot) {
    if (slot->in_use) nltp_espnow_forget_peer(slot->mac);
    slot->in_use = 0;
}

static void nltp_espnow_slot_send_cb(void *user, const uint8_t *pkt, size_t len) {
    nltp_espnow_slot_t *slot = (nltp_espnow_slot_t *)user;
    if (!slot || !slot->in_use) return;
    if (slot->v1_mode && len > NLTP_ESPNOW_V1_MTU) {
        __atomic_add_fetch(&g_nltp_espnow_bus.tx_oversize_drops, 1u, __ATOMIC_RELAXED);
        return;
    }
    nltp_espnow_raw_send(slot->mac, pkt, len, slot->rate_mode, slot->last_rssi);
}

static inline esp_err_t nltp_espnow_hub_init(nltp_espnow_hub_t *hub,
                                             uint8_t channel,
                                             nltp_espnow_rate_t rate_mode,
                                             const char *psk,
                                             uint8_t *rx_pool,
                                             uint32_t per_slot_rx_cap,
                                             uint32_t max_kbps,
                                             uint32_t idle_timeout_ms) {
    uint32_t i;
    int want_lr = (rate_mode == NLTP_ESPNOW_RATE_LR_250K || rate_mode == NLTP_ESPNOW_RATE_LR_500K);
    esp_err_t err = nltp_espnow_bus_init(WIFI_IF_STA, channel, want_lr);
    if (err != ESP_OK) return err;

    memset(hub, 0, sizeof(*hub));
    hub->rate_mode = rate_mode;
    hub->max_kbps = max_kbps;
    hub->idle_timeout_ms = idle_timeout_ms ? idle_timeout_ms : 45000u;
    if (psk && psk[0]) {
        nltp_key_from_psk(psk, hub->key32);
        hub->encrypted = 1u;
    }
    for (i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
        hub->slots[i].rx_buf = rx_pool ? (rx_pool + (i * per_slot_rx_cap)) : NULL;
        hub->slots[i].rx_cap = per_slot_rx_cap;
        hub->slots[i].rate_mode = rate_mode;
    }
    return ESP_OK;
}

static inline nltp_espnow_slot_t *nltp_espnow_hub_get_or_create_slot(nltp_espnow_hub_t *hub,
                                                                     const uint8_t mac[6],
                                                                     uint32_t now_ms) {
    uint32_t i;
    int free_idx = -1;
    int oldest_idx = -1;
    uint32_t oldest_age = 0;

    for (i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
        if (hub->slots[i].in_use && memcmp(hub->slots[i].mac, mac, 6) == 0) {
            return &hub->slots[i];
        }
        if (!hub->slots[i].in_use && free_idx < 0) {
            free_idx = (int)i;
        } else if (hub->slots[i].in_use && !nltp_espnow_slot_busy(&hub->slots[i])) {
            /* only idle slots may be recycled: never drop a transfer in progress */
            uint32_t age = now_ms - hub->slots[i].last_active_ms;
            if (oldest_idx < 0 || age >= oldest_age) {
                oldest_age = age;
                oldest_idx = (int)i;
            }
        }
    }
    if (free_idx < 0 && oldest_idx < 0) {
        return NULL;  /* every slot is mid-transfer: the new sender retries later */
    }
    if (free_idx < 0) {
        nltp_espnow_hub_release_slot(&hub->slots[oldest_idx]);
    }

    i = (uint32_t)(free_idx >= 0 ? free_idx : oldest_idx);
    if (!hub->slots[i].rx_buf && hub->slots[i].rx_cap > 0u) {
        hub->slots[i].rx_buf = (uint8_t *)malloc(hub->slots[i].rx_cap);
        if (!hub->slots[i].rx_buf) return NULL;
    }
    hub->slots[i].in_use = 1u;
    memcpy(hub->slots[i].mac, mac, 6);
    hub->slots[i].last_rssi = -60;
    hub->slots[i].last_active_ms = now_ms;
    hub->slots[i].last_reported_sid = 0;
    hub->slots[i].last_reported_cid = 0;
    hub->slots[i].v1_mode = hub->v1_mode;
    hub->slots[i].rate_mode = hub->rate_mode;
    nltp_init(&hub->slots[i].ep, esp_random() | 1u, hub->max_kbps,
              nltp_espnow_slot_send_cb, nltp_espnow_now_ms, &hub->slots[i]);
    nltp_set_shard_bytes(&hub->slots[i].ep,
                         hub->v1_mode ? NLTP_ESPNOW_V1_SHARD_BYTES : (uint16_t)NLTP_SHARD_BYTES);
    nltp_set_rx_buffer(&hub->slots[i].ep, hub->slots[i].rx_buf, hub->slots[i].rx_cap);
    if (hub->encrypted) {
        nltp_set_key(&hub->slots[i].ep, hub->key32);
    }
    return &hub->slots[i];
}

static inline void nltp_espnow_hub_set_v1_mode(nltp_espnow_hub_t *hub, int enable_v1) {
    uint32_t i;
    hub->v1_mode = (uint8_t)(enable_v1 ? 1u : 0u);
    for (i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
        hub->slots[i].v1_mode = hub->v1_mode;
        if (hub->slots[i].in_use) {
            nltp_set_shard_bytes(&hub->slots[i].ep,
                                 enable_v1 ? NLTP_ESPNOW_V1_SHARD_BYTES : (uint16_t)NLTP_SHARD_BYTES);
        }
    }
}

static inline void nltp_espnow_hub_poll(nltp_espnow_hub_t *hub) {
    nltp_espnow_rx_item_t item;
    uint32_t now_ms = nltp_espnow_now_ms(NULL);
    uint32_t i;

    while (g_nltp_espnow_bus.rx_q && xQueueReceive(g_nltp_espnow_bus.rx_q, &item, 0) == pdTRUE) {
        nltp_espnow_slot_t *slot;
        uint32_t valid0;
        if (hub->allow_cb && !hub->allow_cb(hub->user, item.src_mac)) {
            continue;  /* MAC not on allow-list: ignore before allocating slot or sending FIN_ACK */
        }
        {
            /* Unknown senders must present an authentic frame before they get a slot (and, through the
             * reply path, an entry in the 20-peer ESP-NOW table). Known senders go straight to their
             * endpoint, which does the full check including replay protection. */
            int known = 0;
            for (i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
                if (hub->slots[i].in_use && memcmp(hub->slots[i].mac, item.src_mac, 6) == 0) {
                    known = 1;
                    break;
                }
            }
            if (!known && !nltp_espnow_frame_ok(item.data, item.len, hub->encrypted, hub->key32)) {
                continue;
            }
        }
        slot = nltp_espnow_hub_get_or_create_slot(hub, item.src_mac, now_ms);
        if (!slot) continue;

        valid0 = slot->ep.valid_rx;
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
            if (hub->on_message_cb) {
                hub->on_message_cb(hub->user, slot->mac, slot->last_rssi,
                                   slot->rx_buf, nltp_rx_len(&slot->ep),
                                   slot->ep.rx.filename, nltp_rx_sha_ok(&slot->ep));
            }
        }
    }

    for (i = 0; i < NLTP_ESPNOW_MAX_PEERS; ++i) {
        if (!hub->slots[i].in_use) continue;
        if ((now_ms - hub->slots[i].last_active_ms) >= hub->idle_timeout_ms) {
            nltp_espnow_hub_release_slot(&hub->slots[i]);  /* also frees its ESP-NOW peer entry */
            continue;
        }
        nltp_poll(&hub->slots[i].ep);
    }
}

#ifdef __cplusplus
}
#endif

#endif /* ESP_PLATFORM || ARDUINO_ARCH_ESP32 || ESP32 */
#endif /* NLTP_ESPNOW_H */
