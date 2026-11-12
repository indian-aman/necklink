/*
 * ESP8266_UltraLight_LZ4_Sensor.ino — Ultra-light LZ4-compressed AQMS/buoy sensor node.
 *
 * Works out-of-the-box on both ESP8266 (80 KB total SRAM) and ESP32:
 *   - Default ESP8266 build uses ~20 KB RAM (`rxCapBytes = 0` allocates zero bulk RX buffer).
 *   - Passing `build_flags = -DNLTP_TX_ONLY=1` (PlatformIO) or
 *     `--build-property "build.extra_flags=-DNLTP_TX_ONLY=1"` (arduino-cli) shrinks
 *     `sizeof(nltp_endpoint_t)` down to < 4.7 KB RAM while preserving:
 *       * Full X25519+PSK forward-secret ChaCha20-Poly1305 AEAD + 512-pkt anti-replay
 *       * Built-in zero-heap LZ4 block compression (`sendBytesCompressed`, 3x-6x smaller CSV/JSON)
 *       * GF(2^8) Cauchy FEC + end-to-end SHA-256 verification on the uncompressed payload
 *       * Bidirectional confirmed out-of-line Reflex commands (`popReflex` / `sendReflex`)
 */
#if defined(ESP8266)
#include <ESP8266WiFi.h>
#else
#include <WiFi.h>
#endif
#include <NLTP.h>

const char *WIFI_SSID = "YOUR_RUT200_SSID";
const char *WIFI_PASS = "YOUR_RUT200_PASSWORD";

const IPAddress SERVER_IP(192, 168, 1, 100);
const uint16_t  SERVER_PORT = 9700;
const char     *SHARED_PSK  = "my-aqms-station-secret-2026";

NLTPWiFiNode nltp;

// Persistent TX payload & LZ4 scratch buffer (zero heap allocations during loop)
static char    csvPayload[2048];
static uint8_t lz4Scratch[2048];
static uint32_t batchId = 1;
static uint32_t lastUploadMs = 0;

void setup() {
  Serial.begin(115200);
  delay(200);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(300);

  // rxCapBytes = 0 -> zero heap allocated for bulk RX; endpoint uses < 4.8 KB RAM!
  if (!nltp.begin(0, SHARED_PSK, 0u, 5000u)) {
    Serial.println("NLTP TX-only init failed");
    for (;;) delay(1000);
  }
  nltp.setPeer(SERVER_IP, SERVER_PORT);
  nltp.setKeepaliveMs(15000);
  Serial.printf("Ultra-light NLTP v1.6 ready (endpoint=%u bytes RAM)\n",
                (unsigned)sizeof(nltp_endpoint_t));
}

void loop() {
  nltp.poll();

  // Receive instant confirmed Reflex commands from server even in NLTP_TX_ONLY mode!
  uint8_t cmdBuf[128];
  uint16_t cmdLen = 0;
  if (nltp.popReflex(cmdBuf, sizeof(cmdBuf) - 1, &cmdLen)) {
    cmdBuf[cmdLen] = '\0';
    Serial.printf("[REFLEX RX] %s\n", (const char *)cmdBuf);
  }

  uint32_t now = millis();
  if (nltp.isSendComplete() && (uint32_t)(now - lastUploadMs) >= 10000u) {
    lastUploadMs = now;
    size_t pos = (size_t)snprintf(csvPayload, sizeof(csvPayload),
                                  "station,seq,pm25,pm10,so2_ppb,no2_ppb,rssi_dbm\n");
    for (int i = 0; i < 30 && pos + 64 < sizeof(csvPayload); ++i) {
      pos += (size_t)snprintf(csvPayload + pos, sizeof(csvPayload) - pos,
                              "AQMS-01,%lu,41.8,88.2,11.4,23.7,%d\n",
                              (unsigned long)(batchId * 30u + (uint32_t)i), (int)WiFi.RSSI());
    }
    char fname[32];
    snprintf(fname, sizeof(fname), "aqms_%06lu.csv", (unsigned long)batchId++);
    if (nltp.sendBytesCompressed((const uint8_t *)csvPayload, (uint32_t)pos,
                                 lz4Scratch, sizeof(lz4Scratch), fname)) {
      Serial.printf("[NLTP LZ4 TX] %s (%u B raw -> %u B wire)\n",
                    fname, (unsigned)pos, (unsigned)nltp.endpoint()->tx.len);
    }
  }
  delay(2);
}
