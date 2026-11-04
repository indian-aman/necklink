/* Host test of nltp_espnow.h on a simulated ESP-NOW radio (no ESP32 needed): .
 * The radio enforces ESP-NOW's 20-entry peer table; every device is its own translation unit.
 *   A. 30 sensors report one after another: the hub must keep answering (peer-table cleanup).
 *   B. A transfer while forged frames from random MACs flood in: no slot or peer entry for them.
 *   C. Two sensors mid-transfer when a third arrives: no busy slot may be evicted. */
#include <stdio.h>
#include <string.h>
#include <stdint.h>

extern int g_cur;
extern int64_t g_us;
void radio_set_mac(int d, const uint8_t mac[6]);
const uint8_t *radio_mac(int d);
int radio_peer_count(int d);
void radio_inject(int to, const uint8_t src[6], const uint8_t *data, size_t len);
uint32_t esp_random(void);

void hub_init(int enc);
void hub_poll(void);
int hub_msgs(void);
int hub_slots_in_use(void);
#define NODE_API(n) \
    void node##n##_init(const uint8_t hub_mac[6], int enc); \
    void node##n##_send(uint32_t len); \
    void node##n##_poll(void); \
    int node##n##_verified(void);
NODE_API(1)
NODE_API(2)
NODE_API(3)

static void tick(int nodes_active) {
    g_us += 1000;
    g_cur = 0; hub_poll();
    if (nodes_active & 1) { g_cur = 1; node1_poll(); }
    if (nodes_active & 2) { g_cur = 2; node2_poll(); }
    if (nodes_active & 4) { g_cur = 3; node3_poll(); }
}

int main(int argc, char **argv) {
    int enc = argc > 1 ? argv[1][0] == 'e' : 1;
    const char *only = argc > 2 ? argv[2] : "ABC";
    uint8_t hub_mac[6] = {0x24, 0x0A, 0xC4, 0x00, 0x00, 0x01};
    uint8_t m[6] = {0x30, 0xAE, 0xA4, 0x00, 0x00, 0x00};
    int r, t, ok = 0, failures = 0;

    radio_set_mac(0, hub_mac);
    g_cur = 0; hub_init(enc);
    printf("[%s hub, %d slots]\n", enc ? "encrypted" : "plaintext", 2);

    if (strchr(only, 'A')) {
    /* A. 30 different sensors report one after another (e.g. a hub running for days). */
    for (r = 0; r < 30; ++r) {
        m[5] = (uint8_t)(10 + r);
        radio_set_mac(1, m);
        g_cur = 1; node1_init(hub_mac, enc); node1_send(4000);
        for (t = 0; t < 3000 && !node1_verified(); ++t) tick(1);
        ok += node1_verified();
        for (t = 0; t < 50; ++t) tick(1);
    }
    printf("  A. 30 sensors in turn: %2d/30 transfers acknowledged; hub ESP-NOW peer table %d/20\n", ok, radio_peer_count(0));
        failures += ok != 30;
    }

    /* B. One real transfer while forged frames from random MACs arrive every 2 ms. */
    if (strchr(only, 'B')) {
        uint8_t fake[200], src[6];
        int i, forged = 0;
        m[5] = 0xB1; radio_set_mac(1, m);
        g_cur = 1; node1_init(hub_mac, enc); node1_send(60000);
        for (t = 0; t < 10000 && !node1_verified(); ++t) {
            if (t % 2 == 0) {
                for (i = 0; i < 6; ++i) src[i] = (uint8_t)esp_random();
                for (i = 0; i < 200; ++i) fake[i] = (uint8_t)esp_random();
                fake[0] = 'N'; fake[1] = 'L'; fake[2] = (uint8_t)((1u << 4) | 1u); fake[3] = enc ? 0x80 : 0x00;
                radio_inject(0, src, fake, sizeof fake);
                forged++;
            }
            tick(1);
        }
        printf("  B. 60 KB transfer under a flood of %d forged frames: %s after %.1f s; hub peer table %d/20, slots %d\n",
               forged, node1_verified() ? "acknowledged" : "NOT acknowledged", t / 1000.0, radio_peer_count(0), hub_slots_in_use());
        failures += !node1_verified() || radio_peer_count(0) > 4;
    }

    /* C. Two sensors mid-transfer, then a third arrives (hub has 2 slots). */
    if (strchr(only, 'C')) {
        uint8_t m2[6] = {0x30, 0xAE, 0xA4, 0, 0, 0xC2}, m3[6] = {0x30, 0xAE, 0xA4, 0, 0, 0xC3};
        int v1 = 0, v2 = 0, v3 = 0;
        for (t = 0; t < 46000; ++t) tick(0);   /* let earlier slots expire */
        m[5] = 0xC1; radio_set_mac(1, m); radio_set_mac(2, m2); radio_set_mac(3, m3);
        g_cur = 1; node1_init(hub_mac, enc); node1_send(60000);
        g_cur = 2; node2_init(hub_mac, enc); node2_send(60000);
        for (t = 0; t < 5; ++t) tick(3);
        g_cur = 3; node3_init(hub_mac, enc); node3_send(60000);
        for (t = 0; t < 20000; ++t) {
            tick(7);
            v1 = node1_verified(); v2 = node2_verified(); v3 = node3_verified();
            if (v1 && v2 && v3) break;
        }
        printf("  C. 2 sensors mid-transfer + a 3rd arrives: sensor1=%d sensor2=%d sensor3=%d (all done by %.1f s)\n",
               v1, v2, v3, t / 1000.0);
        failures += !(v1 && v2 && v3);
    }
    printf("%s\n", failures ? "ESP-NOW SIM FAILED" : "ESP-NOW SIM PASSED");
    return failures ? 1 : 0;
}
