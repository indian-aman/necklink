# NLTP Examples (Python & Arduino / ESP32)

## 1. Python Examples (`examples/python/`)

Install from the repository root (or directly from GitHub via `pip install "git+https://github.com/indian-aman/necklink.git#subdirectory=packages/nltp"`):

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

---

## 2. Arduino IDE & PlatformIO Library (`arduino/NLTP/`)

### Option A — Install in Arduino IDE (`.ZIP` Library)

1. Download or locate **`deliverables/NLTP-Arduino-1.2.0.zip`** (or zip the `arduino/NLTP` directory).
2. In Arduino IDE, click **Sketch → Include Library → Add .ZIP Library...** and select `NLTP-Arduino-1.2.0.zip`.
3. Open ready-made sketches via **File → Examples → NLTP**:
   - **`ESP32_Encrypted_Receiver`**: Receives encrypted config/OTA files from Python (`04_talk_to_arduino_esp32.py send`).
   - **`ESP32_Marine_Buoy_Sender`**: Collects packed 16-byte sensor records and uploads encrypted batches to shore every 10 s with 15 s NAT keepalives.
   - **`ESP32_Relay_CGNAT_Node`**: Connects an ESP32 behind a 4G SIM router to a cloud `NLTPRelayHub` room with zero port forwarding.

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
