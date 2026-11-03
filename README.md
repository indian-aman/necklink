# NLTP (`necklink` v1.2.0)

**Loss-resilient UDP file and telemetry transport for unreliable wireless, marine radio, cellular-edge, and satellite links — with wire-compatible implementations in Python and zero-heap ANSI C99 (`ESP32` / POSIX).**

Full benchmark methodology, per-iteration ablations, and caveats are documented in [`results/FAIR_BENCHMARK_REPORT.md`](results/FAIR_BENCHMARK_REPORT.md).

---

## What NLTP Is — and Where to Use It

NLTP combines systematic `GF(2^8)` Cauchy forward error correction (8-shard generations with incremental Gauss-Jordan decoding), a BBR-style delivery-rate and packet-train pacer, optional `ChaCha20-Poly1305` authenticated encryption with an `RFC 6479` 64-bit anti-replay window, and built-in NAT traversal (`RFC 5389` STUN, low-TTL pre-punching, keepalives, and automatic fallback to a zero-knowledge UDP relay).

### Honest Standing Against Existing Protocols (`Linux 6.18` `netem`)

Tested head-to-head on identical emulated symmetric links against **9 real implementations**: Linux kernel `TCP BBR` and `TCP CUBIC`, production C `QUIC` (`ngtcp2` with `BBR` and `CUBIC`), `KCP` (`w128` and `w1024` fast mode), `kcptun` (`KCP + Reed-Solomon`), `UDT4`, `SRT`, and Python `aioquic`.

#### `1 MB` File / Firmware Transfer (Median Time; Wire Bytes ÷ File Size)

| Emulated Link (Symmetric) | `NLTP v1.2` | `TCP BBR` | `QUIC` (`ngtcp2`) | `KCP` (`w128`) | `kcptun` | `UDT4` / `SRT` |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Clean** (`20 Mbit`, `20 ms`, `0%`) | `0.58 s` (`1.24x`) | **`0.51 s` (`1.04x`)** | `0.57 s` (`1.05x`) | `0.60 s` (`1.54x`) | `0.74 s` (`2.25x`) | `0.87–0.95 s` |
| **Light Loss** (`20 Mbit`, `40 ms`, `2%`) | **`0.62 s` (`1.25x`)** | `0.65 s` (`1.07x`) | `0.67 s` (`1.07x`) | `0.85 s` (`1.25x`) | `0.81 s` (`1.62x`) | `1.37–3.02 s` |
| **Moderate Loss** (`10 Mbit`, `100 ms`, `5%`) | **`1.35 s` (`1.31x`)** | `1.49 s` (`1.12x`) | `1.57 s` (`1.07x`) | `1.74 s` (`1.45x`) | `1.52 s` (`1.75x`) | `6.49–9.98 s` |
| **Heavy Loss** (`5 Mbit`, `200 ms`, `10%`) | **`2.79 s` (`1.41x`)** | `3.45 s` (`1.15x`) | `4.42 s` (`1.16x`) | `4.08 s` (`1.61x`) | `3.15 s` (`1.72x`) | `14.7–26.5 s` |
| **Severe Loss** (`2 Mbit`, `300 ms`, `20%`) | **`6.70 s` (`1.64x`)** | `8.31 s` (`1.32x`) | `11.87 s` (`1.26x`) | `8.98 s` (`2.11x`) | `9.38 s` (`2.25x`) | `FAIL (0/3)` |
| **Bursty Fades** (`10 Mbit`, `100 ms`, `10%` burst) | **`1.34 s` (`1.35x`)** | `13.10 s` *(2/3 ok)* | `3.48 s` (`CUBIC`) | `2.16 s` (`1.63x`) | `2.07 s` (`1.71x`) | `4.11–9.55 s` |
| **GEO Satellite** (`600 ms` RTT, `3%` loss) | **`6.05 s`** | `8.85 s` | `7.29 s` | — | — | — |

### Summary of Strengths & Limitations

- **Where NLTP wins**:
  - **Medium payloads (`50 KB – 2 MB` logs, telemetry batches, configs, OTA firmware)** over lossy (`2–20%`), bursty-fade, and high-RTT satellite links.
  - **Heavy bursty fades at `10 MB`**: NLTP was the only tested protocol to complete `3/3` runs (`13.8 s`), whereas `TCP BBR` and `QUIC` each timed out on `2/3` runs.
  - **Embedded + Shore parity**: Identical wire format across Python and a zero-malloc, zero-float ANSI C99 core running on a `\$4` `ESP32` (`~65 KB` RAM with 8 receive slots; **48/48** encrypted transfers verified across emulated `wifi-far`, `4g-weak`, `marine-radio`, and `20%`-loss profiles).
  - **Urgent out-of-line reflex lane**: `send_reflex("ESTOP")` bypasses active file queues with zero head-of-line blocking (verified over a live `257 ms` transcontinental Internet link).
- **Where standard tools are better (do not use NLTP here)**:
  - **Clean wired/Wi-Fi links & large (`>= 10 MB`) steady-loss transfers**: Kernel `TCP BBR` and `QUIC` are `12–27%` faster and send fewer bytes.
  - **Tiny (`<= 64 KB`) isolated messages on private links**: `KCP` fast mode is faster on most cells (`0.05–0.17 s` vs `0.05–0.36 s`).
  - **Wire-byte overhead**: Proactive and tail parity shards send **`1.25–1.64x`** the file size (vs `1.05–1.32x` for `TCP`/`QUIC`), which must be weighed on per-MB metered satellite plans.
  - **Raw noisy sensor streams**: On real electrochemical AQMS sensor recordings (`825` runs), simple send-on-delta with periodic keyframes (`AbsSoD-KF32`) or `16`-sample `zstd` batching (`Batch16-zstd`) outperformed the experimental `NeckLink` predictive codec (`necklink/codec.py`) in `102/105` configurations. Batch sensor readings and send them reliably over NLTP, or use `AbsSoD-KF32` for unacknowledged beacons.

---

## Repository Layout

| Path | Contents |
| :--- | :--- |
| `necklink/` & `nltp/` | Python package & `import nltp` alias (`NLTPServer`, `NLTPClient`, `NLTPRelayHub`, STUN/hole-punch, `ChaCha20-Poly1305` AEAD, `nltp` CLI, optional `aioquic` adapter) |
| `arduino/NLTP/` | Drop-in Arduino IDE (`.zip`) & PlatformIO library (`#include <NLTP.h>`) with 3 ESP32 `.ino` examples (receiver, buoy batch sender, and 4G CGNAT relay node) |
| `examples/python/` | 4 runnable Python scripts (encrypted file + `ESTOP`, 4G CGNAT relay, STUN P2P hole-punching, and ESP32 companion client/listener) |
| `csrc/` | Portable zero-heap, zero-float ANSI C99 core (`nltp_core.{h,c}`, `nltp_net.{h,c}`, `nltp_sha256.{h,c}`, `nltp_wire.h`), POSIX CLI (`tools/nltp_cli.c`), and `9/9` security self-test (`tools/nltp_selftest.c`) |
| `esp32/` | PlatformIO `ESP32` firmware (`Arduino-ESP32`), verified over encrypted Wi-Fi and emulated marine radio links |
| `scripts/` | Field-test kit (`field_test.py`), ESP32 & radio emulator (`esp32_test.py`, `radio_emulator.py`), C↔Python 72-pair interop (`c_interop_test.py`), dual-NAT namespace suite (`virtual_nat_test.py`), and `netem` benchmark harnesses |
| `tests/` | `pytest` unit and integration suite (`20` tests) |
| `results/` | Full engineering report ([`FAIR_BENCHMARK_REPORT.md`](results/FAIR_BENCHMARK_REPORT.md)) and reproducible JSON/log artifacts |

---

## Installation & Quick Start

### 1. Python Package & CLI

```bash
pip install -e ".[dev,quic]"
pytest tests -v
```

Generate a 256-bit key and probe your live NAT mapping via `RFC 5389` STUN:

```bash
python -m necklink gen-key
python -m necklink stun
```

Encrypted file transfer, urgent reflex lane, and telemetry in Python:

```python
from necklink import NLTPServer, NLTPClient, generate_nltp_key

key = generate_nltp_key()

with NLTPServer(("0.0.0.0", 9000), save_dir="./inbox", psk=key) as srv:
    with NLTPClient(("127.0.0.1", 9000), psk=key, max_tx_kbps=10_000) as cli:
        cli.send_reflex("ESTOP_CHECK")
        receipt = cli.send_file("mission_log.bin")
        assert receipt["verified_sha256"] is True
```

### 2. Traversing 4G/5G Carrier CGNAT (`NLTPRelayHub` + Direct Hole-Punching)

```bash
# On a 1-port public relay VM (holds zero keys, sees only encrypted ciphertext):
python -m necklink relay --host 0.0.0.0 --port 9000

# Receiver behind NAT/CGNAT:
python -m necklink serve --relay <RELAY_IP>:9000 --room buoy-01 --psk "<KEY>" --save-dir ./inbox

# Sender behind NAT/CGNAT:
python -m necklink send-file --relay <RELAY_IP>:9000 --room buoy-01 --psk "<KEY>" --file update.bin
```

### 3. Portable Zero-Heap ANSI C99 Core (`csrc/`)

Compiles cleanly under `-std=c99 -Wall -Wextra -pedantic -Werror` with zero dynamic memory allocation and zero floating-point operations:

```bash
make -C csrc all test
python scripts/c_interop_test.py
```

Direct P2P hole-punching with automatic 3-second relay fallback from the C CLI:

```bash
# Receiver:
./csrc/build/nltp_cli recv 0.0.0.0 0 ./out.bin \
  --psk "<KEY>" --stun stun.cloudflare.com:3478 --punch \
  --relay <RELAY_IP>:9000 --room buoy-01 --keepalive-ms 10000

# Sender:
./csrc/build/nltp_cli send 0.0.0.0 0 ./firmware.bin \
  --psk "<KEY>" --stun stun.cloudflare.com:3478 --punch \
  --relay <RELAY_IP>:9000 --room buoy-01 --keepalive-ms 10000
```

### 4. Arduino IDE Library (`arduino/NLTP/`) & ESP32 Firmware (`esp32/`)

- **Arduino IDE (`.ZIP` Library)**: Download [`NLTP-Arduino-1.2.0.zip`](https://github.com/indian-aman/necklink/releases/download/v1.2.0/NLTP-Arduino-1.2.0.zip) from Releases (or zip `arduino/NLTP/`), click **Sketch → Include Library → Add .ZIP Library...**, then open **File → Examples → NLTP** (`ESP32_Encrypted_Receiver`, `ESP32_Marine_Buoy_Sender`, or `ESP32_Relay_CGNAT_Node`) and `#include <NLTP.h>`.
- **PlatformIO hardware benchmark firmware (`esp32/`)**:
  1. Copy `esp32/include/secrets.h.example` to `esp32/include/secrets.h` and set your test Wi-Fi credentials (`secrets.h` is git-ignored).
  2. Flash with PlatformIO (`pio run -t upload` inside `esp32/`).
  3. Run the automated encrypted hardware & radio-profile suite from your laptop:

```bash
python scripts/esp32_test.py --port COM9 --psk auto --radio marine-radio
```

### 5. Running a Real Marine / Radio Field Trial

To benchmark NLTP side-by-side against kernel `TCP` over a live vessel, buoy, or cellular link between two reachable hosts:

```bash
# Shore / receiver host:
python scripts/field_test.py server --port 9700 --psk "<KEY>"

# Vessel / buoy host:
python scripts/field_test.py client --host <SHORE_IP> --port 9700 --psk "<KEY>" --sizes 64k,256k,1m --runs 5
```

---

## License

Released under the [MIT License](LICENSE).
