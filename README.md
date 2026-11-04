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
| **[NLTP](https://github.com/indian-aman/necklink/tree/main/packages/nltp)** | Cervical connective | Loss-healing `GF(2^8)` Cauchy FEC file transport with `ChaCha20-Poly1305` encryption over UDP (incl. 4G CGNAT traversal) or ESP-NOW between ESP32s, across Python, C99, JS/TS, Java and Arduino (`216/216` cross-language checks verified) | [`pynltp`](https://pypi.org/project/pynltp/) |
| **[NeckCompass](https://github.com/indian-aman/necklink/tree/main/packages/neckcompass)** | Central complex (`E-PG` / `CPU4`) | Ring-attractor compass that rejects `70°–150°` magnetometer spikes as well as a 3σ-gated EKF (`0.44°` vs `0.41°` RMSE), re-anchors after a 90° heading upset where the gated EKF stays stuck, and homes without GNSS (`324 B` RAM in C99) | [`neckcompass`](https://pypi.org/project/neckcompass/) |
| **[NeckSight](https://github.com/indian-aman/necklink/tree/main/packages/necksight)** | Optic lobe (`T4/T5` + `LPLC2`) | Glare-adapted motion detection and a four-quadrant looming reflex: `0.9988` AUROC on synthetic clutter vs `0.875` for compensated pyramidal Lucas-Kanade, gyro derotation so obstacles are still caught during fast turns, ~12,000 FPS at 32×32 in C99 (desktop) | [`necksight`](https://pypi.org/project/necksight/) |
| **[NeckCord](https://github.com/indian-aman/necklink/tree/main/packages/neckcord)** | Ventral nerve cord (`BANC` / `MANC`) | 4-byte motor command frame, self-healing six-leg gait generator, and a fault-tolerant thruster allocator that isolates a failed thruster in ~`42 ms` (on par with pseudo-inverse + threshold FDI) and can return it to service (`322 B` in C99) | [`neckcord`](https://pypi.org/project/neckcord/) |
| **[NeckSense](https://github.com/indian-aman/necklink/tree/main/packages/necksense)** | Antennal lobe & mushroom body | Concentration-invariant normalisation, multiplication-free FlyHash, one-shot novelty detection (`0.994` AUROC in 4 KB) and one-shot classification, with `Q16.16` C99 that is bit-identical to Python | not on PyPI yet: [install from this repo](https://github.com/indian-aman/necklink/tree/main/packages/necksense#install) |

Each product's README has its full benchmarks, its API and a "when not to use it" section. Each benchmark compares against the strongest simple classical baseline we could build: a gated EKF, threshold FDI, baseline-corrected k-NN, or compensated pyramidal optical flow. Where that baseline ties or wins, the README says so.

> [!IMPORTANT]
> **v1.1.0 audit (November 2026).** An audit of the four non-NLTP products changed several headline claims and fixed real bugs:
> - The 1.0.0 claims were measured against weak baselines: an EKF without outlier gating, an allocator without fault detection, classifiers without drift correction. They have been replaced with fair comparisons.
> - Bugs fixed:
>   - NeckCompass: re-anchoring never fired with real sensor noise, and self-calibration drifted.
>   - NeckSight: missed up to half of obstacles during fast turns.
>   - NeckSight: did not import on Python 3.9.
> - Improvements: NeckCord recovery probing, and sparser NeckSense novelty codes.
> - NLTP 1.3.2 fixes a C/ESP32 receiver bug from 1.2.1 (one stray or replayed packet could stall a transfer for good) and hardens the new ESP-NOW hub (peer-table exhaustion, unauthenticated slot takeover, an 8-slot default too large for an ESP32).
>
> The PyPI releases are still 1.0.0 (and `pynltp` 1.3.1). Until the new versions are published, install from this repository (below) to get the fixes. Each package's changelog has the details.

---

## Install

The fixes in v1.1.0 are in this repository but not yet on PyPI (PyPI has 1.0.0). Install from the repository:

```bash
pip install "git+https://github.com/indian-aman/necklink.git#subdirectory=packages/neckcompass"
pip install "git+https://github.com/indian-aman/necklink.git#subdirectory=packages/necksight"
pip install "git+https://github.com/indian-aman/necklink.git#subdirectory=packages/neckcord"
pip install "git+https://github.com/indian-aman/necklink.git#subdirectory=packages/necksense"
```

NLTP is published on PyPI as `pynltp`:

```bash
pip install pynltp
```

Every package supports Python 3.9+ and depends only on NumPy. Each one's C99 core is two files (`csrc/<name>.h` and `.c`) that you copy into your firmware.

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
pip install -e . pytest
python -m pytest                     # unit tests, plus every README example
make -C csrc clean all test          # zero-heap C99 core and its self-tests
python scripts/benchmark_neckcompass.py   # regenerates results/ and the chart (needs matplotlib)
```

CI ([`.github/workflows/packages-ci.yml`](https://github.com/indian-aman/necklink/blob/main/.github/workflows/packages-ci.yml)) runs the following on Python 3.9 and 3.13 for NeckCompass, NeckCord, NeckSense and NeckSight:
- builds each package's wheel and installs it;
- runs its CLI;
- runs its tests, including the README examples;
- builds the C core with `-Werror` and runs its self-tests.

NLTP also has JavaScript (`packages/nltp/js`), Java (`packages/nltp/java`) and ESP32 (`packages/nltp/esp32`) SDKs; see [its README](https://github.com/indian-aman/necklink/tree/main/packages/nltp#readme).

### History

NeckCompass, NeckSight, NeckCord and NeckSense started in their own repositories and were merged here with their full commit history on 4 November 2026. The old repositories are archived and read-only; their releases, including the NeckSense wheel, remain downloadable there.

---

## License

Released under the [MIT License](https://github.com/indian-aman/necklink/blob/main/LICENSE). Copyright (c) 2026 Amandeep Singh Bains.
