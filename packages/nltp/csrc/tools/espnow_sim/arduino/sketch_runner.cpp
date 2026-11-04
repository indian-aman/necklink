/* Runs the unmodified example sketches ESP32_ESPNow_Sensor_Node.ino and ESP32_ESPNow_Gateway.ino as
 * two simulated ESP32s on the simulated ESP-NOW radio (radio.c). The gateway's Wi-Fi/UDP side is a
 * real UDP socket to a real NLTP server on 127.0.0.1 (see scripts/esp32_sketch_e2e_test.py).
 *
 *   sketch_runner <shore_udp_port> <sim_seconds> [espnow_drop_pct]
 *
 * Simulated time runs in 1 ms steps; it fast-forwards while the UDP side is idle and tracks real
 * time while UDP traffic is in flight, so the real server sees realistic timing. */
#include <time.h>
#include "Arduino.h"
#include "WiFi.h"

extern "C" {
void radio_set_mac(int d, const uint8_t mac[6]);
extern int g_drop_pct;
}

void gateway_setup(); void gateway_loop();
void sensor_setup();  void sensor_loop();

HardwareSerial Serial;
WiFiClass WiFi;
uint16_t sim_shore_port = 9000;
int64_t sim_udp_last_activity_us = -10000000;

const char *sim_device_name(int dev) { return dev == 0 ? "gateway" : "sensor"; }
uint32_t analogReadMilliVolts(uint8_t) { return 1200u + (uint32_t)((g_us / 1000000) % 50); }

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s <shore_udp_port> <sim_seconds> [espnow_drop_pct]\n", argv[0]);
        return 2;
    }
    sim_shore_port = (uint16_t)atoi(argv[1]);
    const int64_t end_us = (int64_t)atof(argv[2]) * 1000000;
    g_drop_pct = argc > 3 ? atoi(argv[3]) : 0;

    /* MACs as written in the sketches: the gateway is ...:02, the sensor is ...:01 */
    const uint8_t gw_mac[6] = {0x24, 0x0A, 0xC4, 0x00, 0x00, 0x02};
    const uint8_t sn_mac[6] = {0x24, 0x0A, 0xC4, 0x00, 0x00, 0x01};
    radio_set_mac(0, gw_mac);
    radio_set_mac(1, sn_mac);
    g_us = 1000;
    g_cur = 0; gateway_setup();
    g_cur = 1; sensor_setup();

    const timespec one_ms = {0, 1000000};
    while (g_us < end_us) {
        g_us += 1000;
        g_cur = 0; gateway_loop();
        g_cur = 1; sensor_loop();
        if (g_us - sim_udp_last_activity_us < 500000) nanosleep(&one_ms, nullptr);
    }
    return 0;
}
