/*
 * ESPNowV2Interop.ino — Zero-Wrapper NLTP over ESP-NOW v2 (1,470 B MTU)
 *
 * Demonstrates standard wire-compatible NLTP frames ("NL", ChaCha20-Poly1305 AEAD,
 * GF(256) Cauchy FEC, end-to-end SHA-256 STREAM_FIN_ACK) running directly over
 * ESP-NOW v2 without SoftAP association, DHCP, or custom L2 wrapper headers.
 *
 * Interoperates out of the box with:
 *   - C99 ESP-IDF v5.x (`nltp_espnow_node_t` and `nltp_espnow_hub_t` in `nltp_espnow.h`)
 *   - Arduino-ESP32 v3.x (`NLTPESPNowNode` in `<NLTP.h>`)
 *   - Python (`pynltp` / `NLTPEndpoint`), C99 CLI, Java, or JS across any transparent
 *     L2-to-UDP / SLIP bridge (full 960 B encrypted shards = 1,010 B <= 1,470 B v2 MTU).
 */

#include <WiFi.h>
#include <NLTP.h>

static const uint8_t WIFI_CHANNEL = 11;
static const char   *SHARED_PSK   = "replace-with-your-shared-secret";

// Replace with your bridge or peer's STA MAC address (or leave FF:FF:FF:FF:FF:FF until first RX)
static const uint8_t PEER_MAC[6]  = {0x24, 0x0A, 0xC4, 0x00, 0x00, 0x01};  // placeholder: your peer's MAC

NLTPESPNowNode nltp;
static uint32_t lastSendMs = 0;
static bool waitingAck = false;

void setup() {
    Serial.begin(115200);
    delay(200);

    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.disconnect(false, true);

    // Use NLTP_ESPNOW_RATE_AUTO (up to 72 Mbps HT20 MCS7) or NLTP_ESPNOW_RATE_LR_250K (~-102 dBm Long Range)
    if (!nltp.begin(WIFI_CHANNEL, SHARED_PSK, NLTP_ESPNOW_RATE_AUTO, 16u * 1024u)) {
        Serial.println("NLTP ESP-NOW v2 init failed!");
        for (;;) delay(1000);
    }
    nltp.setPeerMac(PEER_MAC);
    // Optional: talk to legacy ESP-NOW v1 (<= 250 B MTU) peers using 192 B shards (242 B encrypted frames):
    // nltp.setEspNowV1Mode(true);

    Serial.printf("NLTP over ESP-NOW v%lu ready (MTU=%u B, MAC=%s)\n",
                  (unsigned long)nltp.espNowVersion(),
                  (unsigned)NLTP_ESPNOW_MTU,
                  WiFi.macAddress().c_str());
}

void loop() {
    nltp.poll();

    if (nltp.hasReceivedFile()) {
        Serial.printf("RX %lu B (file='%s', sha256_ok=%d, rssi=%d dBm)\n",
                      (unsigned long)nltp.receivedLength(),
                      nltp.receivedFilename(),
                      (int)nltp.isReceivedShaOk(),
                      (int)nltp.lastRssi());
        nltp.clearReceived();
    }

    if (waitingAck && nltp.isSendComplete()) {
        waitingAck = false;
        Serial.printf("TX delivered & SHA-256 verified=%d\n", (int)nltp.isSendVerified());
    }

    if (!waitingAck && (millis() - lastSendMs) >= 5000) {
        lastSendMs = millis();
        const uint8_t callMsg[4] = {'C', 'A', 'L', '1'};
        if (nltp.sendBytes(callMsg, sizeof(callMsg), "call.bin")) {
            waitingAck = true;
        }
    }
    delay(2);
}
