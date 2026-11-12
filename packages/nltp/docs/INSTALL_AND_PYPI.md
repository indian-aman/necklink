# Installing NLTP (Python, JavaScript/TypeScript, Java, Arduino IDE, and PlatformIO)

> **Version note.** Every NLTP component in this repository (`pynltp 1.6.0` / `necklink 1.6.0`) is **1.6.0**, adding built-in zero-dependency `LZ4` block compression (`--compress` / `-z` with smart auto-bypass), the `4.76 KB` RAM `-DNLTP_TX_ONLY=1` MCU sensor profile + `20 KB` ESP8266 default RAM profile, and branchless `GF(2^8)` + clean-link BDP window capping on top of `v1.5.0`'s 1-RTT `X25519 + PSK` forward-secret session handshake and 512-packet anti-replay window. `v1.6.0` is 100% backward wire-compatible with `v1.5.0` when `LZ4` compression is not enabled (`--compress` requires `v1.6.0+` on the receiver). Build or install the C99, Arduino/PlatformIO, JavaScript/TypeScript, and Java 11+ SDKs from a clone of this repository as shown below.

```bash
git clone https://github.com/indian-aman/necklink.git
cd necklink/packages/nltp
```

## 1. Python (`pynltp`)

Installs the Python library (`import nltp`, `import pynltp`, `import necklink`) and the `nltp` CLI:

```bash
pip install --upgrade pynltp   # or install 1.6.0 directly from a local clone:
# or from a local clone:
pip install .
```

Verify:

```bash
nltp gen-key
python -c "import nltp; print('NLTP version:', nltp.__version__)"
```

---

## 2. Arduino IDE (`ESP32` / `ESP8266` / Wi-Fi microcontrollers)

1. Zip the library folder:
   ```bash
   cd arduino && zip -r NLTP-Arduino-1.6.0.zip NLTP
   ```
2. In **Arduino IDE** → **Sketch → Include Library → Add .ZIP Library...** → select `NLTP-Arduino-1.6.0.zip`.
3. Open **File → Examples → NLTP**: `ESP8266_UltraLight_LZ4_Sensor`, `ESP32_Encrypted_Receiver`,
   `ESP32_Marine_Buoy_Sender`, `ESP32_Relay_CGNAT_Node`, `ESP32_ESPNow_Sensor_Node`,
   `ESP32_ESPNow_Gateway`, and `ESPNowV2Interop`.

`NLTPWiFiNode` (UDP) works on `ESP32` and `ESP8266` (`ESP8266` defaults to `NLTP_RX_SLOTS = 2`, ~`20 KB` RAM; pass `-DNLTP_TX_ONLY=1` with `rxCapBytes = 0` for `< 4.8 KB` RAM on sensor-only stations). `NLTPESPNowNode` (NLTP over ESP-NOW) needs Arduino-ESP32 core 3.x (ESP-IDF 5.x); on older cores it is simply not compiled.

---

## 3. PlatformIO (`platformio.ini`)

Point `lib_deps` at the library folder of your clone (optionally add `build_flags = -DNLTP_TX_ONLY=1` on sensor/station nodes that only send telemetry + bidirectional Reflex commands):

```ini
[env:esp32dev]
platform = espressif32
board = esp32dev
framework = arduino
lib_deps =
    symlink://../necklink/packages/nltp/arduino/NLTP
; Optional ultra-light 4.76 KB RAM TX + bidirectional Reflex profile:
; build_flags = -DNLTP_TX_ONLY=1
```

---

## 4. JavaScript / TypeScript (Node.js 18+)

Zero-dependency ESM + CommonJS + `.d.ts` package (tested on Node.js 20 in CI):

```bash
npm install ./js             # from packages/nltp, or: npm install /path/to/necklink/packages/nltp/js
```

Verify:

```bash
npx nltp-js gen-key
node -e "import('nltp').then(m => console.log('Generated key:', m.generateNltpKey()))"
```

For environments without `node:dgram`, `nltp/core` (`NLTPEndpoint`, `packNltpFrame`, `unpackNltpFrame`, `AEADCipherContext`) uses only `Uint8Array` / `DataView` with zero `node:*` imports *(non-Node runtimes are not yet tested in CI)*. See [`js/README.md`](../js/README.md).

---

## 5. Java 11+ (`io.necklink:nltp`)

Build the zero-dependency library + CLI JAR (`--release 11`):

```bash
mvn -f java/pom.xml clean package        # -> java/target/nltp-1.6.0.jar
java -jar java/target/nltp-1.6.0.jar gen-key
# or install into ~/.m2 for your own Maven/Gradle build:
mvn -f java/pom.xml clean install
```

See [`java/README.md`](../java/README.md) for the builder and `NLTPEndpoint<P>` documentation.

---

## 6. Publishing to Official PyPI (`pypi.org`)

> **Note on PyPI package names**: The literal name `nltp` on PyPI is occupied by an unrelated 2020 text-preprocessing package (`nltp 0.1.0`), so this package is published as **`pynltp`** (live on PyPI). Because the wheel ships a top-level `nltp/` module and `nltp` console script, `pip install pynltp` still gives users `from nltp import NLTPClient, NLTPServer` and the `nltp` CLI. The **`necklink`** umbrella installer (`pip install necklink`) is also live on PyPI and installs all five NeckLink packages (`pynltp`, `neckcompass`, `necksight`, `neckcord`, `necksense`).

1. Register an account at `https://pypi.org/account/register/`, enable 2FA, and create an API token (`pypi-...`) under **Account Settings → API tokens**.
2. Build and upload from the repository root:

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

Enter `__token__` as the username and paste your `pypi-...` token as the password.
