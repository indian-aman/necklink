# Installing NLTP (Python, JavaScript/TypeScript, Java, Arduino IDE, and PlatformIO)

> **Version note.** Every NLTP component in this repository is **1.3.2**. That version is not yet
> published as a release, so the instructions below install it from a clone of this repository.
> The older v1.2.0 GitHub release assets (Arduino zip, JS tarball, Java JAR) and `pip install pynltp`
> (1.3.1 on PyPI) still work, but lack the v1.3.2 receive-stall fix and the ESP-NOW transport fixes.

```bash
git clone https://github.com/indian-aman/necklink.git
cd necklink/packages/nltp
```

## 1. Python (`pynltp`)

Installs the Python library (`import nltp`, `import pynltp`, `import necklink`) and the `nltp` CLI:

```bash
pip install .
# or without cloning:
pip install "git+https://github.com/indian-aman/necklink.git#subdirectory=packages/nltp"
```

Verify:

```bash
nltp gen-key
python -c "import nltp; print('NLTP version:', nltp.__version__)"
```

---

## 2. Arduino IDE (`ESP32` / Wi-Fi microcontrollers)

1. Zip the library folder:
   ```bash
   cd arduino && zip -r NLTP-Arduino-1.3.2.zip NLTP
   ```
2. In **Arduino IDE** → **Sketch → Include Library → Add .ZIP Library...** → select `NLTP-Arduino-1.3.2.zip`.
3. Open **File → Examples → NLTP**: `ESP32_Encrypted_Receiver`, `ESP32_Marine_Buoy_Sender`,
   `ESP32_Relay_CGNAT_Node`, and `ESPNowV2Interop`.

`NLTPWiFiNode` (UDP) works on any Arduino-ESP32 core. `NLTPESPNowNode` (NLTP over ESP-NOW) needs
Arduino-ESP32 core 3.x (ESP-IDF 5.x); on older cores it is simply not compiled.

---

## 3. PlatformIO (`platformio.ini`)

Point `lib_deps` at the library folder of your clone:

```ini
[env:esp32dev]
platform = espressif32
board = esp32dev
framework = arduino
lib_deps =
    symlink://../necklink/packages/nltp/arduino/NLTP
```

---

## 4. JavaScript / TypeScript (Node.js 18+, Deno, Bun, Browser)

Zero-dependency ESM + CommonJS + `.d.ts` package:

```bash
npm install ./js             # from packages/nltp, or: npm install /path/to/necklink/packages/nltp/js
```

Verify:

```bash
npx nltp-js gen-key
node -e "import('nltp').then(m => console.log('Generated key:', m.generateNltpKey()))"
```

For Browser / WebRTC / WebTransport environments without `node:dgram`, import `nltp/core` (`NLTPEndpoint`, `packNltpFrame`, `unpackNltpFrame`, `AEADCipherContext`). See [`js/README.md`](../js/README.md).

---

## 5. Java 11+ / Android (`io.necklink:nltp`)

Build the zero-dependency library + CLI JAR (`--release 11`):

```bash
mvn -f java/pom.xml clean package        # -> java/target/nltp-1.3.2.jar
java -jar java/target/nltp-1.3.2.jar gen-key
# or install into ~/.m2 for your own Maven/Gradle build:
mvn -f java/pom.xml clean install
```

See [`java/README.md`](../java/README.md) for the builder and `NLTPEndpoint<P>` documentation.

---

## 6. Publishing to Official PyPI (`pypi.org`)

> **Note on PyPI package names**: The literal name `nltp` on PyPI is occupied by an unrelated 2020 text-preprocessing package (`nltp 0.1.0`), whereas **`necklink`**, **`pynltp`**, and **`nltp-transport`** are available. Because this repository ships a top-level `nltp/` module and `nltp` console script inside the wheel, installing `pip install necklink` (or `pip install pynltp`) still gives users `from nltp import NLTPClient, NLTPServer` and the `nltp` CLI.

1. Register an account at `https://pypi.org/account/register/`, enable 2FA, and create an API token (`pypi-...`) under **Account Settings → API tokens**.
2. Build and upload from the repository root:

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

Enter `__token__` as the username and paste your `pypi-...` token as the password.
