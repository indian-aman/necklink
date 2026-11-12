# NLTP Examples (Python, JavaScript, Java, Arduino / ESP32)

## 0. Real-project examples (start here)

Three complete patterns you can copy into a product. Each one has a self-checking mode that CI runs
on every push, so they are known to work against the current code.

| Example | Real-world setup | Patterns shown | Self-test |
| :--- | :--- | :--- | :--- |
| [`python/05_vessel_shore_link.py`](python/05_vessel_shore_link.py) | Survey boat / buoy / rover uploading over 4G or radio; operator on shore | Store-and-forward spool (deleted only after the shore verifies SHA-256), status heartbeats, JSON settings from shore, **E-stop reflex retransmitted until acknowledged**, PSK from `NLTP_PSK` | `python 05_vessel_shore_link.py demo --loss 0.15` |
| [`javascript/shore_dashboard.mjs`](javascript/shore_dashboard.mjs) | Control-room web page for any NLTP peers (Python, ESP32, Java, JS) | Per-peer status table, E-STOP button showing *ACKED in N ms* or *NOT ACKED*; untrusted peer data rendered safely; binds to 127.0.0.1 by default | `node shore_dashboard.mjs --demo` |
| [`ESP32_ESPNow_Sensor_Node`](../arduino/NLTP/examples/ESP32_ESPNow_Sensor_Node/ESP32_ESPNow_Sensor_Node.ino) + [`ESP32_ESPNow_Gateway`](../arduino/NLTP/examples/ESP32_ESPNow_Gateway/ESP32_ESPNow_Gateway.ino) | Battery sensor → ESP-NOW → Wi-Fi gateway → shore server (`nltp serve`) | Batches retried until verified, zero-copy buffers kept until delivery, gateway on the router's channel, keepalive for CGNAT | `python scripts/esp32_sketch_e2e_test.py` |

The two halves interoperate: run the dashboard, point `05_vessel_shore_link.py vessel` at its UDP
port, and press E-STOP in the browser.

How the ESP32 sketches are tested without boards: both unmodified `.ino` files are compiled as two
simulated ESP32s on the simulated ESP-NOW radio (`csrc/tools/espnow_sim/arduino/`). The gateway's
Wi-Fi side is a real UDP socket to a real Python `NLTPServer`. Three batches must arrive SHA-256
verified with clean links, with 10% loss on both hops, and with 20% ESP-NOW / 15% UDP loss. This
checks the sketch logic and protocol, **not** real radio behaviour, ESP32 RAM/timing or the Arduino
toolchain: flash real boards before deploying.

Safety note: an NLTP E-stop is a fast, acknowledged *software* command. Keep a hardware interlock
or dead-man switch on any machine that can hurt someone.


## 1. Python Examples (`examples/python/`)

Install from PyPI (`pip install --upgrade pynltp`) or editable from `packages/nltp`:

```bash
pip install -e .
```

Both `import nltp` and `import necklink` (and CLI commands `nltp` / `necklink`) are registered automatically:

| Script | What It Demonstrates |
| :--- | :--- |
| [`01_encrypted_file_and_estop.py`](python/01_encrypted_file_and_estop.py) | Encrypted `NLTPServer` + `NLTPClient` (`ChaCha20-Poly1305`), urgent `ESTOP` reflex lane, JSON dict delivery, and `64 KB` file transfer over `10%` loss with `SHA-256` receipt verification |
| [`02_relay_cgnat_traversal.py`](python/02_relay_cgnat_traversal.py) | End-to-end encrypted transfer across strict 4G/5G SIM Symmetric CGNAT using a zero-knowledge `NLTPRelayHub` room |
| [`03_stun_and_nat_hole_punch.py`](python/03_stun_and_nat_hole_punch.py) | Live `RFC 5389` STUN public endpoint & NAT Cone/Symmetric detection + direct P2P hole-punching |
| [`04_talk_to_arduino_esp32.py`](python/04_talk_to_arduino_esp32.py) | Send encrypted files to an ESP32 (`send`) or receive periodic buoy/AQMS sensor batches (`listen`) |
| [`05_vessel_shore_link.py`](python/05_vessel_shore_link.py) | Real-project vessel ↔ shore link (see section 0) |

JavaScript (`examples/javascript/`, run with `node`): `encrypted_file_and_reflex.mjs`,
`cgnat_relay_demo.mjs`, `shore_dashboard.mjs`. Java: `examples/java/EncryptedFileAndReflexExample.java`.

---

## 2. Arduino IDE & PlatformIO Library (`arduino/NLTP/`)

### Option A — Install in Arduino IDE (`.ZIP` Library)

1. Zip the `arduino/NLTP` directory: `cd arduino && zip -r NLTP-Arduino-1.6.0.zip NLTP`.
2. In Arduino IDE, click **Sketch → Include Library → Add .ZIP Library...** and select `NLTP-Arduino-1.6.0.zip`.
3. Open ready-made sketches via **File → Examples → NLTP**:
   - **`ESP8266_UltraLight_LZ4_Sensor`**: Ultra-light `< 4.8 KB` RAM (`-DNLTP_TX_ONLY=1` + `rxCapBytes = 0`) AQMS/buoy telemetry sender with built-in zero-heap `LZ4` compression (`sendBytesCompressed`) and bidirectional Reflex commands on `ESP8266` or `ESP32`.
   - **`ESP32_Encrypted_Receiver`**: Receives encrypted config/OTA files from Python (`04_talk_to_arduino_esp32.py send`).
   - **`ESP32_Marine_Buoy_Sender`**: Collects packed 16-byte sensor records and uploads encrypted batches to shore every 10 s with 15 s NAT keepalives.
   - **`ESP32_Relay_CGNAT_Node`**: Connects an ESP32 behind a 4G SIM router to a cloud `NLTPRelayHub` room with zero port forwarding.
   - **`ESP32_ESPNow_Sensor_Node`** + **`ESP32_ESPNow_Gateway`**: sensor → ESP-NOW → Wi-Fi gateway → shore (section 0). Needs Arduino-ESP32 core 3.x.
   - **`ESPNowV2Interop`**: minimal NLTP-over-ESP-NOW point-to-point sketch. Needs Arduino-ESP32 core 3.x.

### Option B — Install in PlatformIO (`platformio.ini`)

Point `lib_deps` at `arduino/NLTP` (local path or symlink) and include `<NLTP.h>`:

```ini
[env:esp32dev]
platform = espressif32
board = esp32dev
framework = arduino
lib_deps =
    file://../arduino/NLTP
```
