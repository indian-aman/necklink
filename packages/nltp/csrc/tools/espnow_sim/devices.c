/* One simulated ESP32 per compilation: -DROLE_HUB or -DNODE_ID=<n>. */
#include <string.h>
#include "nltp_espnow.h"

#define PSK "espnow-sim-psk"

#ifdef ROLE_HUB
static nltp_espnow_hub_t hub;
static uint8_t pool[NLTP_ESPNOW_MAX_PEERS * 70000];
static int good_msgs;
static void on_msg(void *u, const uint8_t mac[6], int8_t rssi, const uint8_t *d, uint32_t n, const char *f, int ok) {
    (void)u; (void)mac; (void)rssi; (void)d; (void)n; (void)f;
    if (ok) good_msgs++;
}
void hub_init(int enc) {
    nltp_espnow_hub_init(&hub, 1, NLTP_ESPNOW_RATE_MCS7, enc ? PSK : NULL, pool, 70000, 20000, 45000);
    hub.on_message_cb = on_msg;
}
void hub_poll(void) { nltp_espnow_hub_poll(&hub); }
int hub_msgs(void) { return good_msgs; }
int hub_slots_in_use(void) {
    int i, n = 0;
    for (i = 0; i < (int)NLTP_ESPNOW_MAX_PEERS; ++i) n += hub.slots[i].in_use;
    return n;
}
#else
#define CAT2(a, b) a##b
#define CAT(a, b) CAT2(a, b)
#define F(x) CAT(CAT(node, NODE_ID), CAT(_, x))
static nltp_espnow_node_t node;
static uint8_t rxb[4096];
static uint8_t file[65536];
void F(init)(const uint8_t hub_mac[6], int enc) {
    nltp_espnow_node_init(&node, 1, NLTP_ESPNOW_RATE_MCS7, enc ? PSK : NULL, rxb, sizeof(rxb), 20000);
    nltp_espnow_node_set_peer(&node, hub_mac);
}
void F(send)(uint32_t len) {
    uint32_t i;
    for (i = 0; i < len; ++i) file[i] = (uint8_t)(i * 7u + NODE_ID);
    nltp_send_buffer(&node.ep, file, len, "sensor.bin");
}
void F(poll)(void) { nltp_espnow_node_poll(&node); }
int F(verified)(void) { return nltp_tx_verified(&node.ep); }
#endif
