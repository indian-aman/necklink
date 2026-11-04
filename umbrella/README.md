<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/logo/necklink-logo-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/logo/necklink-logo-light.png">
  <img alt="NeckLink" src="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/logo/necklink-logo-light.png" width="320">
</picture>

### Fruit-fly brain edge autonomy, in Python and zero-heap ANSI C99

**Five open-source modules modelled on the adult *Drosophila* central nervous system, built for lossy radios, GPS-denied vessels, micro-drones and low-cost microcontrollers.**

[![Website](https://img.shields.io/badge/website-indian--aman.github.io%2Fnecklink-0EA5E9?style=flat-square&logo=githubpages&logoColor=white)](https://indian-aman.github.io/necklink/)
[![PyPI pynltp](https://img.shields.io/pypi/v/pynltp?style=flat-square&logo=pypi&logoColor=white&label=pynltp&color=3775A9)](https://pypi.org/project/pynltp/)
[![PyPI neckcompass](https://img.shields.io/pypi/v/neckcompass?style=flat-square&logo=pypi&logoColor=white&label=neckcompass&color=F59E0B)](https://pypi.org/project/neckcompass/)
[![PyPI necksight](https://img.shields.io/pypi/v/necksight?style=flat-square&logo=pypi&logoColor=white&label=necksight&color=06B6D4)](https://pypi.org/project/necksight/)
[![PyPI neckcord](https://img.shields.io/pypi/v/neckcord?style=flat-square&logo=pypi&logoColor=white&label=neckcord&color=F43F5E)](https://pypi.org/project/neckcord/)
[![NeckSense wheel](https://img.shields.io/badge/necksense-v1.0.0%20wheel-10B981?style=flat-square&logo=python&logoColor=white)](https://github.com/indian-aman/necksense/releases/tag/v1.0.0)
[![License: MIT](https://img.shields.io/badge/license-MIT-F59E0B?style=flat-square)](https://github.com/indian-aman/necklink/blob/main/LICENSE)

[**Website**](https://indian-aman.github.io/necklink/) ·
[**Products**](#products) ·
[**Install**](#install) ·
[**Repository layout**](#repository-layout) ·
[**Development**](#development)

</div>

<p align="center">
  <img src="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/necklink_brand_banner.png" alt="The five NeckLink modules mapped onto the fruit-fly nervous system" width="100%">
</p>

---

## Products

NeckLink is the brand; each product translates one subsystem of the fruit-fly nervous system into a standalone library. Every product has a Python package and a zero-heap, zero-float ANSI C99 core for microcontrollers (`ESP32`, `STM32`, `RP2040`), and each one is benchmarked against a strong classical baseline, including where the baseline still wins.

| Product | Fly circuit | What it does | Package |
| :--- | :--- | :--- | :--- |
| **[NLTP](https://github.com/indian-aman/necklink/tree/main/packages/nltp)** | Cervical connective | Loss-healing telemetry, `GF(2^8)` Cauchy FEC file transport, `ChaCha20-Poly1305` encryption and 4G CGNAT traversal across Python, C99, JS/TS, Java and ESP32 (`210/210` cross-language links verified) | [`pynltp`](https://pypi.org/project/pynltp/) |
| **[NeckCompass](https://github.com/indian-aman/necklink/tree/main/packages/neckcompass)** | Central complex (`E-PG` / `CPU4`) | 16-wedge ring-attractor compass with `10.2×` lower heading error than a 2-state EKF under `70°–150°` magnetic hull spikes, and GPS-denied homing (`316 B` RAM in C99) | [`neckcompass`](https://pypi.org/project/neckcompass/) |
| **[NeckSight](https://github.com/indian-aman/necklink/tree/main/packages/necksight)** | Optic lobe (`T4/T5` + `LPLC2`) | `100×` glare adaptation, motion detection and a four-quadrant looming E-stop (`0.9988` AUROC, `0.0%` false alarms under fast yaw and glare, `12,463 FPS` in C99) | [`necksight`](https://pypi.org/project/necksight/) |
| **[NeckCord](https://github.com/indian-aman/necklink/tree/main/packages/neckcord)** | Ventral nerve cord (`BANC` / `MANC`) | 4-byte descending motor command codec, self-healing six-limb CPG and a `42 ms` thruster fault reallocator (`5.1×` less USV spin-out, `304 B` RAM in C99) | [`neckcord`](https://pypi.org/project/neckcord/) |
| **[NeckSense](https://github.com/indian-aman/necklink/tree/main/packages/necksense)** | Antennal lobe & mushroom body | Concentration-invariant odour normalisation and a multiplication-free 5% FlyHash for `99.5%` one-shot plume ID and `0.966` anomaly AUROC (`Q16.16` C99) | [wheel v1.0.0](https://github.com/indian-aman/necksense/releases/tag/v1.0.0) |

Each product's README has its full benchmarks, honest baseline comparisons and API.

---

## Install

```bash
# NLTP, NeckCompass, NeckSight and NeckCord from PyPI
pip install pynltp neckcompass necksight neckcord

# NeckSense from its v1.0.0 release wheel
pip install https://github.com/indian-aman/necksense/releases/download/v1.0.0/necksense-1.0.0-py3-none-any.whl
```

Any product can also be installed straight from this repository:

```bash
pip install "git+https://github.com/indian-aman/necklink.git#subdirectory=packages/neckcompass"
```

### Run all five together

The `necklink` command ships with `pynltp`. With the five packages installed:

```bash
necklink products                  # List the installed NeckLink products and versions
necklink suite-demo --loss 0.15    # Closed-loop five-module reflex mission over encrypted, lossy UDP
```

```python
from necklink import run_autonomous_reflex_demo

report = run_autonomous_reflex_demo(loss=0.15)
print(report)
```

The `necklink` umbrella installer in [`umbrella/`](https://github.com/indian-aman/necklink/tree/main/umbrella) (`pip install necklink`, which pulls in all five) is not on PyPI yet; until it is, install the packages as above.

---

## Repository layout

```text
necklink/
├── packages/
│   ├── nltp/          NLTP wire protocol: Python, C99, JS/TS, Java, Arduino, ESP32 firmware
│   ├── neckcompass/   Central-complex compass and path integrator
│   ├── necksight/     Optic-lobe motion and looming reflex
│   ├── neckcord/      Ventral-nerve-cord motor codec and thruster allocator
│   └── necksense/     Antennal-lobe and mushroom-body odour engine
├── umbrella/          The `necklink` meta-package that installs all five
├── web/               Source of the website (deployed to the gh-pages branch)
└── assets/            NeckLink logo, brand banner and shared images
```

Every folder under `packages/` is a self-contained project with its own `pyproject.toml`, tests, C99 core, benchmarks, README and licence, and is released to PyPI on its own. Python import names are unchanged (`import nltp`, `import neckcompass`, and so on).

---

## Development

Work inside the package you are changing:

```bash
cd packages/neckcompass
pip install -e .
python -m pytest            # Python test suite
make -C csrc all test       # Zero-heap C99 core and its self-tests
```

NLTP also has JavaScript (`packages/nltp/js`), Java (`packages/nltp/java`) and ESP32 (`packages/nltp/esp32`) SDKs; see [its README](https://github.com/indian-aman/necklink/tree/main/packages/nltp#readme).

### History

NeckCompass, NeckSight, NeckCord and NeckSense started in their own repositories and were merged here with their full commit history on 4 November 2026. The old repositories are archived and read-only; their releases, including the NeckSense wheel, remain downloadable there.

---

## License

Released under the [MIT License](https://github.com/indian-aman/necklink/blob/main/LICENSE). Copyright (c) 2026 Amandeep Singh Bains.
