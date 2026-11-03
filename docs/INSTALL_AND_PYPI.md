# Installing NLTP (Python, Arduino IDE, PlatformIO, and PyPI Publishing)

## 1. Direct `pip install` from GitHub (Works Immediately)

Install the Python library (`import nltp` and `import necklink`) plus the `nltp` CLI command on any laptop or Linux server in one line:

```bash
pip install git+https://github.com/indian-aman/necklink.git
```

Or install the prebuilt wheel from the [v1.2.0 GitHub Release](https://github.com/indian-aman/necklink/releases/tag/v1.2.0):

```bash
pip install https://github.com/indian-aman/necklink/releases/download/v1.2.0/necklink-1.2.0-py3-none-any.whl
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

## 4. Publishing to Official PyPI (`pypi.org`)

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
