/*
 * ESP32_Relay_CGNAT_Node.ino — Traverse 4G/5G SIM Symmetric CGNAT with zero port forwarding.
 *
 * Connects an ESP32 behind a mobile hotspot / 4G router to a shore server through a 1-port
 * zero-knowledge `NLTPRelayHub` room (`nltp relay --host 0.0.0.0 --port 9000` on any cloud VM).
 * All packets remain end-to-end encrypted with ChaCha20-Poly1305; the relay holds zero keys.
 *
 * Shore sender command (from anywhere on the Internet):
 *   nltp send-file --relay <CLOUD_VM_IP>:9000 --room buoy-01 --psk "my-marine-secret-2026" --file mission.bin
 */
#include <WiFi.h>
#include <NLTP.h>

const char *WIFI_SSID = "YOUR_4G_HOTSPOT_SSID";
const char *WIFI_PASS = "YOUR_4G_HOTSPOT_PASS";

const IPAddress RELAY_SERVER_IP(203, 0, 113, 10);  // Replace with your cloud relay VM IP
const uint16_t  RELAY_PORT = 9000;
const char     *ROOM_NAME  = "buoy-01";
const char     *SHARED_PSK = "my-marine-secret-2026";

NLTPWiFiNode nltp;
static bool printedReg = false;

void setup() {
  Serial.begin(115200);
  delay(200);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(300);

  if (!nltp.beginRelay(0, RELAY_SERVER_IP, RELAY_PORT, ROOM_NAME, SHARED_PSK, 32 * 1024)) {
    Serial.println("ERROR: Failed to start NLTP relay node!");
    for (;;) delay(1000);
  }
  Serial.printf("Registering in CGNAT relay room '%s' via %s:%u...\n",
                ROOM_NAME, RELAY_SERVER_IP.toString().c_str(), RELAY_PORT);
}

void loop() {
  nltp.poll();

  if (!printedReg && nltp.isRelayRegistered()) {
    printedReg = true;
    Serial.println("Registered with NLTPRelayHub! Ready for encrypted transfers across 4G CGNAT.");
  }

  if (nltp.hasReceivedFile()) {
    Serial.printf("[Relay RX] Verified '%s' (%lu bytes)\n",
                  nltp.receivedFilename(), (unsigned long)nltp.receivedLength());
    nltp.clearReceived();
  }
  delay(1);
}
