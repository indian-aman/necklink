/*
 * ESP32_ESPNow_Gateway.ino  (real-project example, part 2 of 2)
 *
 * Sensor node --ESP-NOW--> this gateway --Wi-Fi/UDP (NLTP)--> shore server (`nltp serve`).
 *
 * The gateway joins your Wi-Fi router, listens for NLTP batches from a sensor node over ESP-NOW
 * and forwards each verified batch to a shore/cloud server over the same encrypted, FEC-protected
 * NLTP protocol. Run the server side with the Python package, for example:
 *
 *     pip install necklink
 *     nltp serve --port 9000 --psk "$NLTP_PSK" --save-dir ./sensor_inbox
 *
 * Requirements: Arduino-ESP32 core 3.x (ESP-IDF 5.x). Not tested on hardware by this repo's CI.
 *
 * Notes for real deployments:
 *   - ESP-NOW and Wi-Fi share one radio, so ESP-NOW runs on the router's channel. begin(0, ...)
 *     keeps the current channel; the gateway prints it so you can set it on the sensor nodes.
 *   - NLTPESPNowNode is point-to-point (one sensor per gateway). For many sensors per gateway use
 *     the C hub `nltp_espnow_hub_t` from <nltp_espnow.h> (ESP-IDF), sized with
 *     NLTP_ESPNOW_HUB_MAX_PEERS (default 2 concurrent transfers; idle peers are evicted LRU).
 *   - RAM: one ESP-NOW node + one Wi-Fi node + buffers is roughly 150 KB of heap.
 */

#include <WiFi.h>
#include <NLTP.h>

static const char   *WIFI_SSID   = "your-ssid";
static const char   *WIFI_PASS   = "your-password";
static const char   *SHARED_PSK  = "replace-with-output-of-nltp-gen-key";
static const uint8_t SENSOR_MAC[6] = {0x24, 0x0A, 0xC4, 0x00, 0x00, 0x01};  // placeholder: sensor's STA MAC
static const IPAddress SHORE_IP(203, 0, 113, 10);                              // your server's IP
static const uint16_t  SHORE_PORT = 9000;
static const uint16_t  LOCAL_UDP_PORT = 9100;

static const uint32_t MAX_BATCH = 8u * 1024u;

NLTPESPNowNode sensors;   // ESP-NOW side
NLTPWiFiNode   shore;     // Wi-Fi/UDP side

// NLTP sends zero-copy, so a received batch is copied here and kept until the shore verifies it.
static uint8_t  fwdBuf[MAX_BATCH];
static uint32_t fwdLen = 0;
static char     fwdName[80];
static bool     fwdPending = false;
static bool     fwdInFlight = false;
static uint32_t fwdStartedMs = 0;
static uint32_t forwarded = 0, dropped = 0;

static void startForward() {
    fwdInFlight = shore.sendBytes(fwdBuf, fwdLen, fwdName);
    fwdStartedMs = millis();
}

void setup() {
    Serial.begin(115200);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);   // modem sleep makes ESP-NOW reception unreliable
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    while (WiFi.status() != WL_CONNECTED) delay(200);

    if (!shore.begin(LOCAL_UDP_PORT, SHARED_PSK, 4u * 1024u, 5000u)) {
        Serial.println("NLTP Wi-Fi init failed");
        for (;;) delay(1000);
    }
    shore.setPeer(SHORE_IP, SHORE_PORT);
    shore.setKeepaliveMs(15000);   // keeps the NAT mapping open if the server is behind 4G/CGNAT

    if (!sensors.begin(0 /* stay on the router's channel */, SHARED_PSK, NLTP_ESPNOW_RATE_AUTO, MAX_BATCH)) {
        Serial.println("NLTP ESP-NOW init failed");
        for (;;) delay(1000);
    }
    sensors.setPeerMac(SENSOR_MAC);

    Serial.printf("gateway MAC %s, channel %d, IP %s -> set these on the sensor node\n",
                  WiFi.macAddress().c_str(), (int)WiFi.channel(), WiFi.localIP().toString().c_str());
}

void loop() {
    sensors.poll();
    shore.poll();
    const uint32_t now = millis();

    // 1) A batch arrived from the sensor (NLTP already checked its SHA-256 and acknowledged it).
    if (sensors.hasReceivedFile()) {
        if (!sensors.isReceivedShaOk()) {
            Serial.println("sensor batch failed SHA-256; ignored");
        } else if (fwdPending) {
            dropped++;   // previous batch not yet confirmed by shore; a real gateway would queue to flash
            Serial.printf("shore busy/unreachable: dropped batch %s (%lu dropped so far)\n",
                          sensors.receivedFilename(), (unsigned long)dropped);
        } else if (sensors.receivedLength() <= MAX_BATCH) {
            fwdLen = sensors.receivedLength();
            memcpy(fwdBuf, sensors.receivedData(), fwdLen);
            snprintf(fwdName, sizeof(fwdName), "%02X%02X_%s", SENSOR_MAC[4], SENSOR_MAC[5],
                     sensors.receivedFilename());
            fwdPending = true;
            Serial.printf("from sensor: %s (%lu B, RSSI %d dBm)\n", fwdName, (unsigned long)fwdLen,
                          (int)sensors.lastRssi());
            startForward();
        }
        sensors.clearReceived();
    }

    // 2) Forward result.
    if (fwdInFlight && shore.isSendComplete()) {
        fwdInFlight = false;
        if (shore.isSendVerified()) {
            forwarded++;
            fwdPending = false;
            Serial.printf("forwarded %s to shore, verified (%lu total)\n", fwdName, (unsigned long)forwarded);
        }
    }
    if (fwdPending && !fwdInFlight && now - fwdStartedMs >= 5000) {
        startForward();   // retry until the shore confirms
    }

    if (WiFi.status() != WL_CONNECTED) {
        WiFi.reconnect();
    }
    delay(1);
}
