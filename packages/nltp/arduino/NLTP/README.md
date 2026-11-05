# NLTP Arduino Library (`v1.4.0`)

Drop-in Arduino IDE and PlatformIO library wrapping the zero-float, zero-heap-churn ANSI C99 NLTP v1.4.0 core (`nltp_core.c`, `nltp_net.c`, `nltp_sha256.c`, `nltp_espnow.h`, `nltp_wire.h`).

100% wire-compatible with the Python `nltp` / `necklink` package (`NLTPServer`, `NLTPClient`, and `NLTPRelayHub`), including `ChaCha20-Poly1305` authenticated encryption with per-session keys and `RFC 6479` 64-bit replay protection.

> **Encryption in 1.4:** encrypted endpoints run a short session handshake ([security model](../../README.md#24-encrypted-sessions--security-model-v14)), so encrypted 1.4 boards only talk to 1.4 peers: upgrade every device together. The handshake needs a secure random source: ESP32 and ESP8266 use their hardware RNG automatically (and `ARDUINO_ARCH_RP2040` wires `rp2040.hwrand32()`, untested on RP2040 hardware); on other boards call `nltp.setRandomSource(fn)` with a secure source (e.g. a TRNG) before `begin()`, otherwise `begin()` with a key returns `false`. Prefer a key from `nltp gen-key` over a passphrase.

## Install in Arduino IDE

1. Zip this folder (`cd packages/nltp/arduino && zip -r NLTP-Arduino-1.4.0.zip NLTP`), then click **Sketch → Include Library → Add .ZIP Library...** and choose the zip.
2. Include `<NLTP.h>` in your sketch:

```cpp
#include <WiFi.h>
#include <NLTP.h>

NLTPWiFiNode nltp;

void setup() {
  Serial.begin(115200);
  WiFi.begin("YOUR_SSID", "YOUR_PASSWORD");
  while (WiFi.status() != WL_CONNECTED) delay(300);

  // Port 9700, ChaCha20-Poly1305 passphrase, 32 KB receive buffer
  nltp.begin(9700, "my-marine-secret-2026", 32 * 1024);
}

void loop() {
  nltp.poll();
  if (nltp.hasReceivedFile()) {
    Serial.printf("Verified '%s' (%lu B)\n",
                  nltp.receivedFilename(), (unsigned long)nltp.receivedLength());
    nltp.clearReceived();
  }
  delay(1);
}
```
