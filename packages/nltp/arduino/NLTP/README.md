# NLTP Arduino Library (`v1.6.0`)

Drop-in Arduino IDE and PlatformIO library wrapping the zero-float, zero-heap-churn ANSI C99 NLTP v1.6.0 core (`nltp_core.c`, `nltp_net.c`, `nltp_sha256.c`, `nltp_espnow.h`, `nltp_wire.h`).

100% wire-compatible with the Python `nltp` / `necklink`, Node.js, and Java 11+ packages (`NLTPServer`, `NLTPClient`, and `NLTPRelayHub`), including:
- `ChaCha20-Poly1305` authenticated encryption with `X25519 + PSK` ephemeral forward secrecy and `RFC 6479` 512-packet replay protection.
- Built-in zero-dependency **`LZ4` raw block compression & decompression** (`sendBytesCompressed(data, len, scratch, scratchCap, filename)` with smart auto-bypass on incompressible data; receivers decompress automatically in-place).
- **Ultra-light `4.76 KB` RAM sensor profile** (`build_flags = -DNLTP_TX_ONLY=1` + `rxCapBytes = 0`) and **`20 KB` RAM default ESP8266 TX+RX profile** so even 80 KB SRAM `ESP8266` nodes run full `X25519 + PSK` encryption, `LZ4` telemetry compression, `GF(2^8)` Cauchy FEC, and bidirectional reliable Reflex commands (`sendReflex` / `isReflexAcked` / `popReflex`).
- Branchless 4×-unrolled `GF(2^8)` row-table math and systematic zero-`GF(2^8)` fast-path.

> **Compatibility note:** `v1.6.0` is 100% backward wire-compatible with `v1.5.0` when `LZ4` compression is not enabled; enabling `sendBytesCompressed` requires a `v1.6.0+` receiver. Encrypted endpoints run a 1-RTT `X25519 + PSK` session handshake ([security model](../../README.md#24-encrypted-sessions--security-model-v15)). ESP32 and ESP8266 use their hardware RNG automatically (and `ARDUINO_ARCH_RP2040` wires `rp2040.hwrand32()`, untested on RP2040 hardware); on other boards call `nltp.setRandomSource(fn)` before `begin()`. Prefer a key from `nltp gen-key` over a passphrase.

## Install in Arduino IDE

1. Zip this folder (`cd packages/nltp/arduino && zip -r NLTP-Arduino-1.6.0.zip NLTP`), then click **Sketch → Include Library → Add .ZIP Library...** and choose the zip.
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
