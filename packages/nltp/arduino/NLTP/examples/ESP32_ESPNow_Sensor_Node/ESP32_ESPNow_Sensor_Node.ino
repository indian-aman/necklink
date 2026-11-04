/*
 * ESP32_ESPNow_Sensor_Node.ino  (real-project example, part 1 of 2)
 *
 * Battery sensor node -> ESP-NOW -> gateway (ESP32_ESPNow_Gateway) -> Wi-Fi/UDP -> shore server.
 *
 * The node samples a sensor once per second, packs 60 samples into a CSV batch and sends the batch
 * to the gateway over ESP-NOW (no router association, no DHCP, so it is fast and cheap on battery).
 * NLTP adds encryption, FEC loss repair and an end-to-end SHA-256 check. A batch counts as
 * delivered only when the gateway has verified it; until then it is kept and retried, so a
 * gateway reboot or a burst of interference does not lose data.
 *
 * Requirements: Arduino-ESP32 core 3.x (ESP-IDF 5.x). Not tested on hardware by this repo's CI;
 * the ESP-NOW logic is exercised in the simulated-radio test (`make -C csrc espnow-sim`).
 *
 * Setup:
 *   1. Flash ESP32_ESPNow_Gateway first and note the "gateway MAC" and "channel" it prints.
 *   2. Put them in GATEWAY_MAC and WIFI_CHANNEL below (ESP-NOW peers must share the router's channel).
 *   3. Use the same SHARED_PSK on every device (generate one with `nltp gen-key`).
 */

#include <WiFi.h>
#include <NLTP.h>

static const uint8_t WIFI_CHANNEL   = 6;     // = the channel the gateway prints (its router's channel)
static const char   *SHARED_PSK     = "replace-with-output-of-nltp-gen-key";
static const uint8_t GATEWAY_MAC[6] = {0x24, 0x0A, 0xC4, 0x00, 0x00, 0x02};  // placeholder: gateway's STA MAC
static const int     SENSOR_PIN     = 34;    // any ADC pin; replace readSensor() for I2C/SPI sensors

static const uint16_t SAMPLES_PER_BATCH = 60;
static const uint32_t SAMPLE_PERIOD_MS  = 1000;
static const uint32_t RETRY_PERIOD_MS   = 5000;

NLTPESPNowNode radio;

// NLTP sends straight from this buffer (zero-copy), so it must not change until the send completes.
static char     txBatch[SAMPLES_PER_BATCH * 32];
static uint32_t txLen = 0;
static uint32_t txSeq = 0;
static bool     txPending = false;    // a finished batch is waiting for verified delivery
static bool     txInFlight = false;
static uint32_t txStartedMs = 0;

// Samples collected while the previous batch is still being delivered.
static char     building[SAMPLES_PER_BATCH * 32];
static uint32_t buildingLen = 0;
static uint16_t buildingCount = 0;
static uint32_t lastSampleMs = 0;

static float readSensor() {
    // Replace with your sensor (e.g. a BME280 over I2C). Here: ADC pin scaled to volts.
    return analogReadMilliVolts(SENSOR_PIN) / 1000.0f;
}

static void startSend() {
    char name[32];
    snprintf(name, sizeof(name), "batch_%06lu.csv", (unsigned long)txSeq);
    txInFlight = radio.sendBytes((const uint8_t *)txBatch, txLen, name);
    txStartedMs = millis();
    if (!txInFlight) Serial.println("send could not start (previous transfer still active?)");
}

void setup() {
    Serial.begin(115200);
    WiFi.mode(WIFI_STA);
    WiFi.disconnect(false, true);

    // 16 KB receive buffer is plenty: this node only receives small control messages.
    if (!radio.begin(WIFI_CHANNEL, SHARED_PSK, NLTP_ESPNOW_RATE_AUTO, 16u * 1024u)) {
        Serial.println("NLTP ESP-NOW init failed");
        for (;;) delay(1000);
    }
    radio.setPeerMac(GATEWAY_MAC);
    Serial.printf("sensor node ready, MAC %s, channel %u\n", WiFi.macAddress().c_str(), WIFI_CHANNEL);
}

void loop() {
    radio.poll();
    const uint32_t now = millis();

    // 1) Sample the sensor into the batch being built.
    if (now - lastSampleMs >= SAMPLE_PERIOD_MS) {
        lastSampleMs = now;
        if (buildingCount == 0) {
            buildingLen = snprintf(building, sizeof(building), "uptime_ms,value\n");
        }
        buildingLen += snprintf(building + buildingLen, sizeof(building) - buildingLen,
                                "%lu,%.4f\n", (unsigned long)now, readSensor());
        buildingCount++;
    }

    // 2) When a batch is full and the previous one is delivered, hand it to NLTP.
    if (buildingCount >= SAMPLES_PER_BATCH && !txPending) {
        memcpy(txBatch, building, buildingLen);
        txLen = buildingLen;
        txSeq++;
        txPending = true;
        buildingCount = 0;
        startSend();
    }

    // 3) Delivery result: verified -> done; otherwise retry the same batch later.
    if (txInFlight && radio.isSendComplete()) {
        txInFlight = false;
        if (radio.isSendVerified()) {
            Serial.printf("batch %lu delivered, SHA-256 verified by gateway (%lu ms)\n",
                          (unsigned long)txSeq, (unsigned long)(now - txStartedMs));
            txPending = false;
        } else {
            Serial.printf("batch %lu not confirmed; will retry\n", (unsigned long)txSeq);
        }
    }
    if (txPending && !txInFlight && now - txStartedMs >= RETRY_PERIOD_MS) {
        startSend();
    }

    // If samples pile up while the gateway is unreachable, drop the oldest batch-in-progress
    // rather than overflowing RAM (a real node could log to flash/SD here instead).
    if (buildingCount >= SAMPLES_PER_BATCH && txPending) {
        buildingCount = 0;
        Serial.println("gateway unreachable for a full batch period: oldest unsent samples dropped");
    }

    delay(2);
}
