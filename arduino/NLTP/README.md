# NLTP Arduino Library (`v1.2.0`)

Drop-in Arduino IDE and PlatformIO library wrapping the zero-float, zero-heap-churn ANSI C99 NLTP v1.2 core (`nltp_core.c`, `nltp_net.c`, `nltp_sha256.c`, `nltp_wire.h`).

100% wire-compatible with the Python `nltp` / `necklink` package (`NLTPServer`, `NLTPClient`, and `NLTPRelayHub`), including `ChaCha20-Poly1305` authenticated encryption and `RFC 6479` 64-bit replay protection.

## Install in Arduino IDE

1. Click **Sketch → Include Library → Add .ZIP Library...** and choose `NLTP-Arduino-1.2.0.zip`.
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
