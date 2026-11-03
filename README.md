# NLTP (`necklink` v1.2.0)

**Loss-resilient two-way UDP file and telemetry transport for unreliable wireless, marine radio, cellular-edge (`4G`/`5G` CGNAT), and satellite links — with wire-compatible implementations in Python, zero-heap ANSI C99, and Arduino / ESP32.**

---

## Architecture Overview

NLTP v1.2 combines four components in a single wire format shared across Python (`necklink` / `nltp`) and a zero-malloc, zero-float ANSI C99 engine (`csrc/` & `arduino/NLTP/`):

- **Systematic `GF(2^8)` Cauchy FEC**: 8-shard generations (`960 B` shards) with incremental Gauss-Jordan decoding, proactive parity sizing from queue-free loss estimates, and tail-generation parity protection.
- **Adaptive Delivery-Rate & Packet-Train Pacer**: 1-RTT packet-train bottleneck probe, jitter-aware RTT variance tracking, and HyStart-style queue control.
- **Authenticated Encryption**: Optional `ChaCha20-Poly1305` (256-bit key or passphrase) with an `RFC 6479` 64-bit sliding anti-replay window and end-to-end `SHA-256` payload verification.
- **Built-In NAT & CGNAT Traversal**: `RFC 5389` STUN discovery, low-TTL (`TTL=2`) direct P2P UDP hole-punching, NAT keepalives, and automatic 3-second fallback to a zero-knowledge 1-port UDP relay (`NLTPRelayHub`).

---

## Post-Upgrade Benchmark & Verification Results

All protocol comparisons below were measured after all v1.2 rate-control, packet-train, jitter, tail-parity, C99, and NAT-traversal upgrades on `Linux 6.18` network namespaces (`tc netem` + `tbf`, symmetric loss and rate limits in both directions, wire bytes counted via `iptables` including dropped packets).

### 1. `1 MB` File / Firmware Transfer vs. 8 Competing Implementations

Median completion time across 3 runs (total wire bytes ÷ file size in parentheses):

| Emulated Link (Symmetric) | **`NLTP v1.2`** | `TCP BBR` | `QUIC` (`ngtcp2`) | `KCP` (`w128`) | `kcptun` (`FEC`) | `UDT4` | `SRT` |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Clean** (`20 Mbit`, `20 ms`, `0%`) | `0.58 s` (`1.23x`) | **`0.51 s` (`1.04x`)** | `0.57 s` (`1.05x`) | `0.60 s` (`1.54x`) | `0.74 s` (`2.25x`) | `0.95 s` (`1.15x`) | `0.87 s` (`1.15x`) |
| **Light Loss** (`20 Mbit`, `40 ms`, `2%`) | **`0.60 s` (`1.20x`)** | `0.65 s` (`1.07x`) | `0.67 s` (`1.07x`) | `0.85 s` (`1.25x`) | `0.81 s` (`1.62x`) | `3.02 s` (`1.07x`) | `1.37 s` (`1.05x`) |
| **Moderate Loss** (`10 Mbit`, `100 ms`, `5%`) | **`1.28 s` (`1.30x`)** | `1.49 s` (`1.12x`) | `1.57 s` (`1.07x`) | `1.74 s` (`1.45x`) | `1.52 s` (`1.75x`) | `9.98 s` (`1.19x`) | `6.49 s` (`1.24x`) |
| **Heavy Loss** (`5 Mbit`, `200 ms`, `10%`) | **`2.78 s` (`1.41x`)** | `3.45 s` (`1.15x`) | `4.42 s` (`1.16x`) | `4.08 s` (`1.61x`) | `3.15 s` (`1.72x`) | `26.47 s` (`1.37x`) | `14.73 s` *(1/3)* |
| **Severe Loss** (`2 Mbit`, `300 ms`, `20%`) | **`6.36 s` (`1.54x`)** | `8.31 s` (`1.32x`) | `11.87 s` (`1.26x`) | `8.98 s` (`2.11x`) | `9.38 s` (`2.25x`) | `FAIL (0/3)` | `FAIL (0/3)` |
| **Bursty Fades** (`10 Mbit`, `100 ms`, `10%` burst) | **`1.84 s` (`1.49x`)** | `13.10 s` *(2/3)* | `3.48 s` (`CUBIC`) | `2.16 s` (`1.63x`) | `2.07 s` (`1.71x`) | `9.55 s` (`1.34x`) | `4.11 s` *(2/3)* |

*(Note: `TCP CUBIC` degraded from `1.80 s` at `2%` loss to `42.50 s` at `10%` loss and `FAIL 0/3` at `20%` loss; `KCP w1024` overflowed router queues on `1 MB`, taking `6.22–70.48 s` and `3.1–6.2x` wire bytes.)*

### 2. `64 KB` Short Transfers & Extended Link Profiles

| Scenario | Payload | **`NLTP v1.2`** | `TCP BBR` | `QUIC` (`ngtcp2`) | `KCP` (`fast`) | `kcptun` (`FEC`) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **GEO Satellite** (`2 Mbit`, `600 ms`, `3%`) | `64 KB` | **`0.91 s` (enc) / `1.02 s`** | `2.77 s` | `2.67 s` | `1.12 s` (`3.25x`) | `1.57 s` (`3.10x`) |
| **GEO Satellite** (`2 Mbit`, `600 ms`, `3%`) | `1 MB` | **`6.05 s` (`1.23x`)** | `8.85 s` | `7.29 s` | `8.56 s` (`1.68x`) | `7.32 s` (`1.67x`) |
| **Large Bursty Fades** (`10 Mbit`, `100 ms`, `10%` burst) | `10 MB` | **`13.79 s` (`3/3` ok)** | `10.86 s` *(1/3 ok)* | `11.25 s` *(1/3 ok)* | — | `21.00 s` (`3/3` ok) |
| **Large Steady Loss** (`10 Mbit`, `100 ms`, `5%`) | `10 MB` | `11.28 s` (`1.28x`) | **`9.81 s` (`1.10x`)** | `9.85 s` (`1.11x`) | — | `14.41 s` (`1.78x`) |
| **Large Clean Link** (`20 Mbit`, `20 ms`, `0%`) | `10 MB` | `5.64 s` (`1.24x`) | **`4.45 s` (`1.04x`)** | `4.78 s` (`1.05x`) | — | `7.13 s` (`2.31x`) |
| **Packet Reordering** (`10 Mbit`, `60 ms`, `2%`, `10%` reorder) | `1 MB` | `1.19 s` (`1.20x`) | **`1.12 s` (`1.08x`)** | `1.22 s` (`1.05x`) | `1.42 s` (`1.32x`) | `1.62 s` (`1.91x`) |
| **Heavy Jitter** (`10 Mbit`, `100 ± 20 ms`, `5%`) | `1 MB` | `3.28 s` (`3/3` ok) | `2.60 s` | `1.52 s` | `2.36 s` | **`1.39 s`** |
| **Short Transfers (`0–20%` loss)** | `64 KB` | `0.08–1.42 s` (**`0.17 s` warm @ 5%**) | `0.09–3.27 s` | `0.17–2.73 s` | **`0.05–0.77 s`** | `0.06–1.37 s` |

### 3. Real `ESP32` Hardware & Degraded Radio Verification (`48/48` Encrypted)

Tested on an `ESP32-D0WDQ6` (`Arduino-ESP32`, `~65 KB` endpoint RAM across 8 receive slots, `~89 KB` free heap alongside active Wi-Fi) with `ChaCha20-Poly1305` AEAD enabled in both directions (`Laptop ↔ ESP32`) through `scripts/radio_emulator.py`:

| Radio Profile | Link Emulation (Symmetric + Fades) | Measured Loss (Up / Down) | `16 KB` (`PC → ESP32` / `ESP32 → PC`) | `58 KB` (`PC → ESP32` / `ESP32 → PC`) | Verified |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`wifi-far`** | `5 Mbit`, `10 ± 5 ms`, `5%` loss | `5.3%` / `6.4%` | `0.13 s` / `0.15 s` | `0.25 s` / `0.24 s` | **`12/12`** |
| **`4g-weak`** | `2 Mbit`, `60 ± 20 ms`, `3%` + `4%` fades | `8.5%` / `12.9%` | `0.31 s` / `0.39 s` | `0.85 s` / `0.63 s` | **`12/12`** |
| **`marine-radio`** | `1 Mbit`, `150 ± 30 ms`, `5%` + `6%` fades | `18.1%` / `6.4%` | `0.53 s` / `0.55 s` | `1.53 s` / `0.89 s` | **`12/12`** |
| **`very-bad`** | `1 Mbit`, `250 ± 40 ms`, `20%` loss | `20.8%` / `20.8%` | `1.52 s` / `0.94 s` | `1.68 s` / `2.80 s` | **`12/12`** |

*(On clean home Wi-Fi, `27/27` plaintext and `27/27` encrypted hardware transfers also passed with `0` CRC/auth drops; encrypted NLTP `0.126–0.216 s` matched unencrypted lwIP `TCP` `0.131–0.212 s`.)*

### 4. NLTP v1.2 vs. ESP-NOW on `ESP32` Hardware (Speed & Capability Comparison)

Measured side-by-side on the same `ESP32-D0WDQ6` (`2.4 GHz` Ch 11, RSSI `-47 dBm`, `ESP-NOW v2.0`, 5 runs/cell; [`results/esp32_espnow_vs_nltp.json`](results/esp32_espnow_vs_nltp.json)). **NLTP v1.2** and **TCP** were measured **2-hop end-to-end (`ESP32 ↔ Wi-Fi Router ↔ Laptop`)** with `SHA-256` receipt verification; **ESP-NOW** was measured **1-hop over RF** with real IEEE 802.11 hardware `ACK` frames (`100%` ACKed, `0` drops — representing ESP-NOW's 1-hop hardware air-time ceiling without receiver reassembly overhead):

| Mode on `ESP32` (`-47 dBm`) | **`250 B`** *(1 pkt v1)* | **`1,470 B`** *(1 pkt v2)* | **`4 KB`** (`4,096 B`) | **`16 KB`** (`16,384 B`) | **`60 KB`** (`61,440 B`) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **ESP-NOW v1 Default** *(1 Mbps PHY, `250 B` mtu)* | **`3.35 ms`** (`0.60 Mbps`) | `22.20 ms` (`0.53 Mbps`) | `62.28 ms` (`0.53 Mbps`) | `279.07 ms` (`0.47 Mbps`) | `901.49 ms` (`0.55 Mbps`) |
| **ESP-NOW v2 Default** *(1 Mbps PHY, `1470 B` mtu)* | `3.31 ms` (`0.60 Mbps`) | `26.27 ms` (`0.45 Mbps`) | `65.42 ms` (`0.50 Mbps`) | `298.68 ms` (`0.44 Mbps`) | `1281.90 ms` (`0.38 Mbps`) |
| **NLTP v1.2 Plain** *(`ESP32 → PC`, 2-hop)* | `10.25 ms` (`0.20 Mbps`) | `17.85 ms` (`0.66 Mbps`) | `35.67 ms` (`0.92 Mbps`) | `91.01 ms` (`1.44 Mbps`) | `194.37 ms` (`2.53 Mbps`) |
| **NLTP v1.2 Encrypted** *(`ESP32 → PC`, 2-hop)* | `8.36 ms` (`0.24 Mbps`) | **`15.07 ms` (`0.78 Mbps`)** | **`17.67 ms` (`1.86 Mbps`)** | **`49.04 ms` (`2.67 Mbps`)** | `191.05 ms` (`2.57 Mbps`) |
| **NLTP v1.2 Encrypted** *(`PC → ESP32`, 2-hop)* | `14.76 ms` (`0.14 Mbps`) | `14.69 ms` (`0.80 Mbps`) | `18.46 ms` (`1.78 Mbps`) | `60.80 ms` (`2.16 Mbps`) | **`161.81 ms` (`3.04 Mbps`)** |
| **TCP** *(`PC → ESP32` lwIP, 2-hop)* | `27.85 ms` (`0.07 Mbps`) | `30.65 ms` (`0.38 Mbps`) | `46.44 ms` (`0.71 Mbps`) | `82.13 ms` (`1.60 Mbps`) | `211.30 ms` (`2.33 Mbps`) |
| **ESP-NOW v1 Tuned `MCS7`** *(72 Mbps PHY, `250 B`)* | **`0.62 ms` (`3.25 Mbps`)** | `2.25 ms` (`5.22 Mbps`) | `6.07 ms` (`5.40 Mbps`) | `24.25 ms` (`5.41 Mbps`) | `84.92 ms` (`5.79 Mbps`) |
| **ESP-NOW v2 Tuned `MCS7`** *(72 Mbps PHY, `1470 B`)* | `0.99 ms` (`2.01 Mbps`) | **`1.05 ms` (`11.24 Mbps`)** | **`1.70 ms` (`19.26 Mbps`)** | **`5.57 ms` (`23.52 Mbps`)** | **`19.04 ms` (`25.82 Mbps`)** |

- **Speed vs. Default ESP-NOW (`1 Mbps` 802.11b)**: Stock `esp_now_add_peer()` locks modulation to `1 Mbps` (`~0.44–0.55 Mbps` goodput). Default ESP-NOW is faster only on single `250 B` pings (`3.35 ms` vs `8.36 ms`); from `1.5 KB` to `60 KB`, **encrypted 2-hop NLTP is `1.5x–6.7x` faster** (`161.8 ms` vs `901.5–1,281.9 ms`).
- **Speed vs. Rate-Unlocked `MCS7` ESP-NOW**: Manually forcing `HT20 MCS7` via `esp_now_set_peer_rate_config` makes 1-hop ESP-NOW faster at short desk range (`5.79 Mbps` v1 / `25.82 Mbps` v2), at the cost of ~`26 dB` lower receiver sensitivity (`~-72 dBm` vs `~-98 dBm`) and zero automatic rate adaptation.
- **Architectural & Loss-Resilience Differences**:

| Feature | **ESP-NOW** (Espressif) | **NLTP v1.2** (`necklink`) |
| :--- | :--- | :--- |
| **Network Layer & Direction** | Layer 2 raw 802.11 action frames; **two-way** | Layer 4/7 over **UDP/IP** (or byte callback); **two-way** (full-duplex `tx`+`rx` on one port) |
| **Internet / 4G / Satellite** | **No** — local 2.4 GHz radio hop only (~100–400 m; cannot cross routers or SIM cards) | **Yes** — works over Wi-Fi routers, `4G`/`5G` CGNAT (`NLTPRelayHub` / STUN), satellite, and marine IP radios |
| **Direct Laptop / Cloud Link** | **No** — ESP-to-ESP only (requires an extra USB bridge ESP32 on PC) | **Yes** — `ESP32` talks directly to any Python laptop or Linux cloud server |
| **Payload Size & Loss Recovery** | `250 B` (v1) / `1,470 B` (v2) per packet; **no file chunking or FEC** (1 lost chunk breaks a multi-KB transfer) | **Multi-MB files & batches**; systematic `GF(2^8)` Cauchy FEC + `SHA-256` survives **`20%` packet loss** |
| **Encryption & Peer Limit** | `AES-128 CCMP`, capped at **6–17 encrypted peers** (`20` total MAC peers) | **`ChaCha20-Poly1305` (256-bit)** + `RFC 6479` replay window; **no hardware peer-table cap** |
| **Deep-Sleep Coin-Cell Use** | **Wins (`~1–3 ms` wake-and-tx, no Wi-Fi association)** | Higher power — requires an active Wi-Fi / cellular IP session (`~65 KB` RAM on ESP32) |

*(Note: Because `nltp_init()` in C99 accepts any packet-send callback `nltp_send_fn`, you can also combine both — e.g. deep-sleep deck sensors feeding an ESP32 gateway via ESP-NOW and forwarding shoreward via NLTP, or running NLTP's Cauchy FEC engine directly on top of `esp_now_send` in ESP-NOW v2.0.)*

### 5. Cross-Language C99 ↔ Python Interop & Dual-NAT / CGNAT Suite

- **C99 ↔ Python Interoperability (`72/72` passed, `scripts/c_interop_test.py`)**: Verified `C→C`, `C→Python`, `Python→C`, and `Python→Python` across plaintext, 256-bit base64 keys, and passphrases at `0%` and `15%` loss (`1 KB`, `64 KB`, and `1 MB`).
- **C99 Cryptographic & Security Self-Test (`9/9` passed, `csrc/tools/nltp_selftest.c`)**: Verified Python key-derivation parity, `5%` bit-flipped packet rejection + FEC recovery, `RFC 6479` replay-attack rejection, wrong-key rejection (`668/668` dropped), and plaintext-to-encrypted downgrade rejection (`671/671` dropped).
- **Dual-NAT & Symmetric CGNAT Suite (`18/18` passed, `scripts/virtual_nat_test.py`)**: Tested `1 MB` encrypted transfers across two separate Linux NAT routers (`40 ms` RTT + `5%` loss on each last mile):
  - Direct STUN + low-TTL UDP hole-punching (`C↔C` and `C↔Python`): **`9/9` direct P2P** (`1.22–2.26 s`).
  - Zero-knowledge `NLTPRelayHub` (`C↔Python` and `C↔C`): **`3/3`** (`1.31–2.19 s`).
  - Symmetric CGNAT (`MASQUERADE --random-fully`): automatic 3-second fallback to `NLTPRelayHub` completed in **`1.80 s`**.
  - `4 s` UDP NAT conntrack timeout across an `8 s` idle gap: **`1 s` keepalives** preserved the mapping (`1.59 s` second transfer).

### Summary: When to Use NLTP (and When Not To)

- **Use NLTP for**:
  - **Medium files, sensor batches, logs, configs, and OTA firmware (`50 KB – 2 MB`)** over lossy (`2–20%`), bursty-fade, cellular-edge, marine radio, or high-RTT satellite links.
  - **Large bursty-fade transfers (`10 MB`)** where `TCP BBR` and `QUIC` stall or time out.
  - **Two-way `ESP32` / embedded C99 ↔ Python cloud/shore links**, including behind carrier `4G`/`5G` CGNAT, plus urgent out-of-line `send_reflex("ESTOP")` commands.
- **Use standard tools instead for**:
  - **Clean wired/Wi-Fi links and large (`>= 10 MB`) steady-loss transfers**: Kernel `TCP BBR` and `QUIC` (`ngtcp2`) are `12–27%` faster and send fewer wire bytes (`1.04–1.32x` vs NLTP's `1.20–1.64x`).
  - **Tiny (`<= 64 KB`) isolated messages on private links**: `KCP` fast mode is faster on most cells (`0.05–0.77 s`), though at higher wire-byte cost under heavy loss (`2.15–2.61x`).

---

## Repository Layout

| Path | Contents |
| :--- | :--- |
| `necklink/` & `nltp/` | Python package & `import nltp` alias (`NLTPServer`, `NLTPClient`, `NLTPRelayHub`, STUN/hole-punch, `ChaCha20-Poly1305` AEAD, `nltp` CLI) |
| `arduino/NLTP/` | Drop-in Arduino IDE (`.zip`) & PlatformIO library (`#include <NLTP.h>`) with 3 ESP32 `.ino` examples (receiver, buoy batch sender, and 4G CGNAT relay node) |
| `examples/python/` | 4 runnable Python scripts (encrypted file + `ESTOP`, 4G CGNAT relay, STUN P2P hole-punching, and ESP32 companion client/listener) |
| `csrc/` | Portable zero-heap, zero-float ANSI C99 core (`nltp_core.{h,c}`, `nltp_net.{h,c}`, `nltp_sha256.{h,c}`, `nltp_wire.h`), POSIX CLI (`tools/nltp_cli.c`), and `9/9` security self-test (`tools/nltp_selftest.c`) |
| `esp32/` | PlatformIO `ESP32` firmware (`Arduino-ESP32`), verified bidirectional over encrypted Wi-Fi and emulated radio profiles |
| `scripts/` | Field-test kit (`field_test.py`), ESP32 & radio emulator (`esp32_test.py`, `radio_emulator.py`), C↔Python 72-pair interop (`c_interop_test.py`), dual-NAT namespace suite (`virtual_nat_test.py`), and `netem` benchmark harnesses |
| `tests/` | `pytest` unit and integration suite (`20` tests) |
| `docs/` | Installation & PyPI publishing guide ([`INSTALL_AND_PYPI.md`](docs/INSTALL_AND_PYPI.md)) and GitHub Actions CI template ([`github-ci.yml`](docs/github-ci.yml)) |

---

## Installation & Quick Start

### 1. Python Package & CLI (`import nltp` / `nltp`)

Install directly from GitHub on any machine:

```bash
pip install git+https://github.com/indian-aman/necklink.git
```

Or install locally in editable mode for development:

```bash
pip install -e ".[dev,quic]"
pytest tests -v
```

Generate a 256-bit key and probe your live NAT mapping via `RFC 5389` STUN:

```bash
nltp gen-key
nltp stun
```

Encrypted file transfer and urgent out-of-line reflex command in Python:

```python
from nltp import NLTPServer, NLTPClient, generate_nltp_key

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
nltp relay --host 0.0.0.0 --port 9000

# Receiver behind NAT/CGNAT:
nltp serve --relay <RELAY_IP>:9000 --room buoy-01 --psk "<KEY>" --save-dir ./inbox

# Sender behind NAT/CGNAT:
nltp send-file --relay <RELAY_IP>:9000 --room buoy-01 --psk "<KEY>" --file update.bin
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

### 5. Running a Live Marine / Radio Field Trial

Benchmark NLTP side-by-side against kernel `TCP` over a live vessel, buoy, or cellular link between two reachable hosts:

```bash
# Shore / receiver host:
python scripts/field_test.py server --port 9700 --psk "<KEY>"

# Vessel / buoy host:
python scripts/field_test.py client --host <SHORE_IP> --port 9700 --psk "<KEY>" --sizes 64k,256k,1m --runs 5
```

---

## License

Released under the [MIT License](LICENSE).
