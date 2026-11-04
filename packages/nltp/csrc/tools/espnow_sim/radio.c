/* Simulated ESP-NOW radio for host testing of nltp_espnow.h.
 * Each device is a separate translation unit (its own static bus); this file routes frames by MAC
 * and enforces ESP-NOW's 20-entry peer table per device. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_now.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#define MAXDEV 8
typedef struct {
    uint8_t mac[6];
    esp_now_recv_cb_t recv_cb;
    esp_now_send_cb_t send_cb;
    uint8_t peers[ESP_NOW_MAX_TOTAL_PEER_NUM][6];
    int np;
} dev_t;

static dev_t devs[MAXDEV];
int g_cur = 0;
int64_t g_us = 0;
int g_drop_pct = 0;
static uint32_t rng = 0x12345678u;

uint32_t esp_random(void) { rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5; return rng; }
int64_t esp_timer_get_time(void) { return g_us; }
void esp_rom_delay_us(uint32_t us) { g_us += us; }

void radio_set_mac(int d, const uint8_t mac[6]) { memcpy(devs[d].mac, mac, 6); }
const uint8_t *radio_mac(int d) { return devs[d].mac; }
int radio_peer_count(int d) { return devs[d].np; }

static int find_peer(dev_t *d, const uint8_t *mac) {
    int i;
    for (i = 0; i < d->np; ++i) if (!memcmp(d->peers[i], mac, 6)) return i;
    return -1;
}

esp_err_t esp_now_init(void) { return ESP_OK; }
esp_err_t esp_now_get_version(uint32_t *v) { *v = 2; return ESP_OK; }
esp_err_t esp_now_register_send_cb(esp_now_send_cb_t cb) { devs[g_cur].send_cb = cb; return ESP_OK; }
esp_err_t esp_now_register_recv_cb(esp_now_recv_cb_t cb) { devs[g_cur].recv_cb = cb; return ESP_OK; }
bool esp_now_is_peer_exist(const uint8_t *mac) { return find_peer(&devs[g_cur], mac) >= 0; }
esp_err_t esp_now_set_peer_rate_config(const uint8_t *mac, esp_now_rate_config_t *cfg) {
    (void)cfg;
    return find_peer(&devs[g_cur], mac) >= 0 ? ESP_OK : ESP_ERR_ESPNOW_NOT_FOUND;
}
esp_err_t esp_now_add_peer(const esp_now_peer_info_t *p) {
    dev_t *d = &devs[g_cur];
    if (find_peer(d, p->peer_addr) >= 0) return ESP_OK;
    if (d->np >= ESP_NOW_MAX_TOTAL_PEER_NUM) return ESP_ERR_ESPNOW_FULL;
    memcpy(d->peers[d->np++], p->peer_addr, 6);
    return ESP_OK;
}
esp_err_t esp_now_del_peer(const uint8_t *mac) {
    dev_t *d = &devs[g_cur];
    int i = find_peer(d, mac);
    if (i < 0) return ESP_ERR_ESPNOW_NOT_FOUND;
    memcpy(d->peers[i], d->peers[d->np - 1], 6);
    d->np--;
    return ESP_OK;
}

static void deliver(int to, const uint8_t *src, const uint8_t *dst, const uint8_t *data, size_t len) {
    esp_now_recv_info_t info;
    wifi_pkt_rx_ctrl_t ctrl = {-55, -96};
    uint8_t s[6], dd[6];
    int saved = g_cur;
    if (!devs[to].recv_cb) return;
    memcpy(s, src, 6);
    memcpy(dd, dst, 6);
    info.src_addr = s;
    info.des_addr = dd;
    info.rx_ctrl = &ctrl;
    g_cur = to;
    devs[to].recv_cb(&info, data, (int)len);
    g_cur = saved;
}

esp_err_t esp_now_send(const uint8_t *mac, const uint8_t *data, size_t len) {
    static const uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    dev_t *me = &devs[g_cur];
    int i;
    if (find_peer(me, mac) < 0) return ESP_ERR_ESPNOW_NOT_FOUND;
    for (i = 0; i < MAXDEV; ++i) {
        if (i == g_cur || !devs[i].recv_cb) continue;
        if (memcmp(mac, bcast, 6) && memcmp(mac, devs[i].mac, 6)) continue;
        if (g_drop_pct && (int)(esp_random() % 100u) < g_drop_pct) continue;
        deliver(i, me->mac, mac, data, len);
    }
    if (me->send_cb) me->send_cb(mac, ESP_NOW_SEND_SUCCESS);
    return ESP_OK;
}

/* A frame from an arbitrary (possibly spoofed) MAC arriving at device `to`. */
void radio_inject(int to, const uint8_t src[6], const uint8_t *data, size_t len) {
    deliver(to, src, devs[to].mac, data, len);
}

struct sim_queue { unsigned depth, head, tail, count; size_t item; unsigned char *buf; };
QueueHandle_t xQueueCreate(unsigned depth, size_t item) {
    QueueHandle_t q = calloc(1, sizeof(*q));
    q->depth = depth; q->item = item; q->buf = malloc(depth * item);
    return q;
}
int xQueueSend(QueueHandle_t q, const void *item, unsigned ticks) {
    (void)ticks;
    if (q->count == q->depth) return pdFALSE;
    memcpy(q->buf + q->tail * q->item, item, q->item);
    q->tail = (q->tail + 1) % q->depth; q->count++;
    return pdTRUE;
}
int xQueueReceive(QueueHandle_t q, void *item, unsigned ticks) {
    (void)ticks;
    if (!q->count) return pdFALSE;
    memcpy(item, q->buf + q->head * q->item, q->item);
    q->head = (q->head + 1) % q->depth; q->count--;
    return pdTRUE;
}
