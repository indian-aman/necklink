# Installing NLTP (Python, JavaScript/TypeScript, Java, Arduino IDE, and PlatformIO)

## 1. Python (`pip install pynltp`)

Install the Python library (`import nltp`, `import pynltp`, and `import necklink`) plus the `nltp` / `pynltp` CLI commands on any laptop or Linux server:

```bash
pip install pynltp
```

Or install directly from GitHub / the [v1.2.0 GitHub Release](https://github.com/indian-aman/necklink/releases/tag/v1.2.0):

```bash
pip install git+https://github.com/indian-aman/necklink.git
# or prebuilt wheel:
pip install https://github.com/indian-aman/necklink/releases/download/v1.2.0/pynltp-1.2.0-py3-none-any.whl
```

Verify installation:

```bash
nltp gen-key
python -c "import nltp; print('NLTP version:', nltp.__version__)"
```

---

## 2. Install in Arduino IDE (`ESP32` / Wi-Fi Microcontrollers)

1. Download **`NLTP-Arduino-1.2.0.zip`** from the [v1.2.0 GitHub Release](https://github.com/indian-aman/necklink/releases/tag/v1.2.0).
2. Open **Arduino IDE** → **Sketch → Include Library → Add .ZIP Library...** → select `NLTP-Arduino-1.2.0.zip`.
3. Open **File → Examples → NLTP** to flash any of the three ready-made sketches:
   - `ESP32_Encrypted_Receiver`
   - `ESP32_Marine_Buoy_Sender`
   - `ESP32_Relay_CGNAT_Node`

---

## 3. Install in PlatformIO (`platformio.ini`)

Add the repository (or release `.zip`) directly under `lib_deps`:

```ini
[env:esp32dev]
platform = espressif32
board = esp32dev
framework = arduino
lib_deps =
    https://github.com/indian-aman/necklink/releases/download/v1.2.0/NLTP-Arduino-1.2.0.zip
```

---

## 4. Install in JavaScript / TypeScript (Node.js 18+, Deno, Bun, Browser)

Install the zero-dependency ESM + CommonJS + `.d.ts` package directly from the [v1.2.0 GitHub Release](https://github.com/indian-aman/necklink/releases/tag/v1.2.0):

```bash
npm install https://github.com/indian-aman/necklink/releases/download/v1.2.0/nltp-js-1.2.0.tgz
```

Verify installation:

```bash
npx nltp-js gen-key
node -e "import('nltp').then(m => console.log('Generated key:', m.generateNltpKey()))"
```

For Browser / WebRTC / WebTransport environments without `node:dgram`, import `nltp/core` (`NLTPEndpoint`, `packNltpFrame`, `unpackNltpFrame`, `AEADCipherContext`). See [`js/README.md`](../js/README.md) for full details.

---

## 5. Install in Java 11+ / Android (`io.necklink:nltp`)

Download the standalone zero-dependency library + CLI JAR (**`nltp-java-1.2.0.jar`**, compiled with `--release 11`) from the [v1.2.0 GitHub Release](https://github.com/indian-aman/necklink/releases/tag/v1.2.0):

```bash
java -jar nltp-java-1.2.0.jar gen-key
```

Or build and install into your local Maven repository (`~/.m2`) or Gradle build:

```bash
mvn -f java/pom.xml clean install
# or
gradle -p java test jar
```

See [`java/README.md`](../java/README.md) for full builder and `NLTPEndpoint<P>` documentation.

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
