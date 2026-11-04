/* Host stand-in for the Arduino-ESP32 WiFi object: the STA MAC is the simulated radio's MAC. */
#pragma once
#include "Arduino.h"

extern "C" const uint8_t *radio_mac(int d);

typedef enum { WIFI_STA = 1 } wifi_mode_t_stub;
typedef enum { WL_IDLE_STATUS = 0, WL_CONNECTED = 3 } wl_status_t;

class WiFiClass {
public:
    bool mode(wifi_mode_t_stub) { return true; }
    bool setSleep(bool) { return true; }
    bool disconnect(bool = false, bool = false) { return true; }
    int begin(const char *, const char *) { return WL_CONNECTED; }
    wl_status_t status() { return WL_CONNECTED; }
    bool reconnect() { return true; }
    int32_t channel() { return 6; }
    IPAddress localIP() { return IPAddress(192, 168, 1, 50); }
    String macAddress() {
        const uint8_t *m = radio_mac(g_cur);
        char s[18];
        snprintf(s, sizeof s, "%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
        return String(s);
    }
};
extern WiFiClass WiFi;
