/*
 * ESP32_Encrypted_Receiver.ino — Minimal Arduino sketch receiving encrypted files from Python.
 *
 * Install in Arduino IDE:
 *   Sketch -> Include Library -> Add .ZIP Library... -> select `NLTP-Arduino-1.3.2.zip` (zip of the arduino/NLTP folder)
 *
 * Test from your laptop with Python:
 *   python examples/python/04_talk_to_arduino_esp32.py send --esp-ip <ESP32_IP> --port 9700 --psk "my-marine-secret-2026"
 */
#include <WiFi.h>
#include <NLTP.h>

// Fill in your test Wi-Fi network credentials before uploading
const char *WIFI_SSID = "YOUR_WIFI_SSID";
const char *WIFI_PASS = "YOUR_WIFI_PASSWORD";

// Must match the `psk=` argument passed in Python!
const char *SHARED_PSK = "my-marine-secret-2026";
const uint16_t LOCAL_PORT = 9700;

NLTPWiFiNode nltp;

void setup() {
  Serial.begin(115200);
  delay(200);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // Disable Wi-Fi power-save sleep for crisp UDP latency
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting to Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print('.');
  }
  Serial.printf("\nConnected! ESP32 IP: %s (RSSI %d dBm)\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());

  // Allocate a 48 KB receive buffer and enable ChaCha20-Poly1305 encryption
  if (!nltp.begin(LOCAL_PORT, SHARED_PSK, 48 * 1024)) {
    Serial.println("ERROR: Failed to initialize NLTP endpoint!");
    for (;;) delay(1000);
  }
  Serial.printf("NLTP encrypted receiver ready on UDP port %u\n", LOCAL_PORT);
}

void loop() {
  nltp.poll();

  if (nltp.hasReceivedFile()) {
    Serial.printf("[NLTP RX] file='%s' bytes=%lu sha256_ok=%d\n",
                  nltp.receivedFilename(),
                  (unsigned long)nltp.receivedLength(),
                  nltp.isReceivedShaOk() ? 1 : 0);

    // Access the verified bytes via `nltp.receivedData()` (e.g., write to LittleFS / OTA partition)
    nltp.clearReceived();
  }
  delay(1);
}
