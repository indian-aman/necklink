/*
 * ESP32_Marine_Buoy_Sender.ino — Periodic encrypted telemetry-batch uploader for buoys / AQMS nodes.
 *
 * Collects 64 compact 16-byte sensor records (1,024 bytes total) and uploads the batch
 * reliably with ChaCha20-Poly1305 encryption + SHA-256 verification to a shore Python server,
 * while sending 15-second NAT keepalives when idle.
 *
 * Run the shore listener on your laptop first:
 *   python examples/python/04_talk_to_arduino_esp32.py listen --port 9700 --psk "my-marine-secret-2026"
 */
#include <WiFi.h>
#include <NLTP.h>

const char *WIFI_SSID = "YOUR_WIFI_SSID";
const char *WIFI_PASS = "YOUR_WIFI_PASSWORD";

// IP address and UDP port of your shore server / laptop running `04_talk_to_arduino_esp32.py listen`
const IPAddress SHORE_IP(192, 168, 1, 100);
const uint16_t SHORE_PORT = 9700;
const char *SHARED_PSK = "my-marine-secret-2026";

// Packed 16-byte marine buoy record (matches `struct.unpack_from("<IIhhHh", ...)` in Python!)
struct __attribute__((packed)) BuoySample {
  uint32_t seq;
  uint32_t uptime_ms;
  int16_t  water_temp_c_x100;  // e.g. 2645 = 26.45 C
  int16_t  wave_height_cm;     // e.g. 142  = 1.42 m
  uint16_t battery_mv;         // e.g. 12680 = 12.68 V
  int16_t  rssi_dbm;
};

static const size_t SAMPLES_PER_BATCH = 64;
static BuoySample txBatch[SAMPLES_PER_BATCH];  // Must remain valid until `nltp.isSendComplete()`!
static uint32_t nextSeq = 1;
static uint32_t lastUploadMs = 0;

NLTPWiFiNode nltp;

void setup() {
  Serial.begin(115200);
  delay(200);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(300);

  Serial.printf("Buoy Wi-Fi up: %s -> Shore %s:%u\n",
                WiFi.localIP().toString().c_str(), SHORE_IP.toString().c_str(), SHORE_PORT);

  // Only sending small configs back, so 8 KB RX buffer is plenty (~65 KB + 8 KB total RAM)
  if (!nltp.begin(0, SHARED_PSK, 8 * 1024, 5000)) {
    Serial.println("ERROR: NLTP init failed");
    for (;;) delay(1000);
  }
  nltp.setPeer(SHORE_IP, SHORE_PORT);
  nltp.setKeepaliveMs(15000);  // Keep 4G/satellite NAT pinhole open every 15 s
}

void loop() {
  nltp.poll();

  // Upload a new 64-sample batch every 10 seconds once any previous transfer finishes
  uint32_t now = millis();
  if (nltp.isSendComplete() && (uint32_t)(now - lastUploadMs) >= 10000u) {
    lastUploadMs = now;
    for (size_t i = 0; i < SAMPLES_PER_BATCH; ++i) {
      txBatch[i].seq = nextSeq++;
      txBatch[i].uptime_ms = millis();
      txBatch[i].water_temp_c_x100 = 2640 + (int16_t)(i % 15);
      txBatch[i].wave_height_cm = 135 + (int16_t)(i % 25);
      txBatch[i].battery_mv = 12720;
      txBatch[i].rssi_dbm = (int16_t)WiFi.RSSI();
    }

    char fname[32];
    snprintf(fname, sizeof(fname), "buoy_batch_%lu.bin", (unsigned long)txBatch[0].seq);
    if (nltp.sendBytes((const uint8_t *)txBatch, sizeof(txBatch), fname)) {
      Serial.printf("[NLTP TX] Started %s (%u B)...\n", fname, (unsigned)sizeof(txBatch));
    }
  }
  delay(1);
}
