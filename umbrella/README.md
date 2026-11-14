<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/logo/necklink-logo-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/logo/necklink-logo-light.png">
  <img alt="NeckLink" src="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/logo/necklink-logo-light.png" width="320">
</picture>

### Adult *Drosophila* connectome edge autonomy, in Python, WebAssembly and zero-heap ANSI C99

**Six open-source engines empirically grounded in the `157,789`-neuron adult *Drosophila* `BANC v888` central nervous system — fitting a complete 4-organ survival copilot plus `MAVLink v2` inside `23.37 KB` of zero-float ANSI C99 RAM (`38.82 KB` standalone WASM, `14.5%` ESP32 RAM), alongside loss-healing `NLTP` transport.**

[![Website](https://img.shields.io/badge/website-indian--aman.github.io%2Fnecklink-0EA5E9?style=flat-square&logo=githubpages&logoColor=white)](https://indian-aman.github.io/necklink/)
[![PyPI necklink](https://img.shields.io/pypi/v/necklink?style=flat-square&logo=pypi&logoColor=white&label=necklink&color=6366F1&cacheSeconds=180&v=200)](https://pypi.org/project/necklink/)
[![PyPI necklink-cns](https://img.shields.io/pypi/v/necklink-cns?style=flat-square&logo=pypi&logoColor=white&label=necklink--cns&color=8B5CF6&cacheSeconds=180&v=200)](https://pypi.org/project/necklink-cns/)
[![PyPI pynltp](https://img.shields.io/pypi/v/pynltp?style=flat-square&logo=pypi&logoColor=white&label=pynltp&color=3775A9&cacheSeconds=180&v=160)](https://pypi.org/project/pynltp/)
[![PyPI neckcompass](https://img.shields.io/pypi/v/neckcompass?style=flat-square&logo=pypi&logoColor=white&label=neckcompass&color=F59E0B&cacheSeconds=180&v=200)](https://pypi.org/project/neckcompass/)
[![PyPI necksight](https://img.shields.io/pypi/v/necksight?style=flat-square&logo=pypi&logoColor=white&label=necksight&color=06B6D4&cacheSeconds=180&v=200)](https://pypi.org/project/necksight/)
[![PyPI neckcord](https://img.shields.io/pypi/v/neckcord?style=flat-square&logo=pypi&logoColor=white&label=neckcord&color=F43F5E&cacheSeconds=180&v=200)](https://pypi.org/project/neckcord/)
[![PyPI necksense](https://img.shields.io/pypi/v/necksense?style=flat-square&logo=pypi&logoColor=white&label=necksense&color=10B981&cacheSeconds=180&v=200)](https://pypi.org/project/necksense/)
[![License: MIT](https://img.shields.io/badge/license-MIT-F59E0B?style=flat-square)](https://github.com/indian-aman/necklink/blob/main/LICENSE)

[**Website & Live WASM Simulator**](https://indian-aman.github.io/necklink/) ·
[**Products**](#products) ·
[**Install**](#install) ·
[**Repository layout**](#repository-layout) ·
[**Development**](#development)

</div>

<p align="center">
  <img src="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/necklink_brand_banner.png" alt="The NeckLink modules mapped onto the fruit-fly nervous system" width="100%">
</p>

---

## Products

NeckLink is the brand; each product translates one empirically verified subsystem of the adult fruit-fly nervous system (`BANC v888`: `157,789` proofread neurons, `23.55M` synapses) into a standalone library, plus `NeckLink CNS` which fuses all four sensory-motor organs into a single `< 24 KB` survival copilot. Every product ships on PyPI and includes a zero-heap, zero-float ANSI C99 core + drop-in Arduino/`ESP32` library, benchmarked head-to-head against strong classical baselines on 3D `MuJoCo` physics and real sensor datasets — including where classical baselines still win.

| Product | Fly circuit (`BANC v888`) | What it does & verified headline result | Package |
| :--- | :--- | :--- | :--- |
| **[NeckLink CNS](https://github.com/indian-aman/necklink/tree/main/packages/necklink_cns)** (`v2.0.0`) | Whole-CNS (`157,789` neurons, `1,316 DNs → 805 MNs`) | Unified 4-organ survival copilot (`DNp01` brake, `MDN` reverse, `DNa02` dodge, `PFL3/PFL2` homing, `MBON` cast) + wire-valid **MAVLink v2** (`0xFD`) & **ROS 2** bridges: **`100.0%` (`250/250`) multi-hazard gauntlet survival** (`1.75 m` miss vs `77.2%` `v1.1` & `1.2%` classical), **`23,928 B` (`23.37 KB`)** C99 SRAM (`4,836 Hz`), `14.5%` ESP32 RAM, **`38.82 KB` standalone WASM** | [`necklink-cns`](https://pypi.org/project/necklink-cns/) |
| **[NLTP](https://github.com/indian-aman/necklink/tree/main/packages/nltp)** (`v1.6.0`) | Cervical connective | Loss-healing `GF(2^8)` Cauchy FEC file & priority-command transport with `X25519 + PSK` forward-secret `ChaCha20-Poly1305` encryption, zero-dependency `LZ4` compression, and a `4.76 KB` MCU sensor profile over UDP (incl. 4G CGNAT traversal) or routerless ESP-NOW across `ESP32` & `ESP8266`, in Python, C99, JS/TS (`Node.js`), Java 11+, and Arduino (`248/248` cross-language checks + `100%` `SHA-256` @ `35%` RF loss) | [`pynltp`](https://pypi.org/project/pynltp/) |
| **[NeckSight](https://github.com/indian-aman/necklink/tree/main/packages/necksight)** (`v2.0.0`) | Optic lobe (`T4/T5` + `LPLC2` + `LC4`, `9.96×` enriched) | `R1-R6` + AEC glare guard, `T5` OFF-dominant Reichardt motion, 3-DOF `(yaw, pitch, roll)` derotation, dual `LPLC2` radial + `LC4/LPLC1` thin-pole/wire reflex, and `DNa02` dodge vectors: **`0.9841` 3D `MuJoCo` camera AUROC**, **`85.7%` thin-pole & `100%` wire recall** (`0%` for Farneback/LK), `0` false alarms on real street video, `14,624 B` C99 RAM (`~6,557 FPS`, `ESP32-CAM` ready) | [`necksight`](https://pypi.org/project/necksight/) |
| **[NeckCompass](https://github.com/indian-aman/necklink/tree/main/packages/neckcompass)** (`v2.0.0`) | Central complex (`EPG / Delta7 / PFL3+PFL2`, `13.6×` enriched) | Zero-float 9-axis 3D tilt-compensated ring-attractor AHRS (`0.25g` wave-slam gate, `< 0.04°` dip error), `1,104 B` `Q12` online steel-hull self-calibration, `Delta7` sharpening & `PFL2` `180°` anti-goal boost: **`0.90°` RMS** under `±25°` tilt + `65°` dip + `130°` spike (vs `1.48°` `3σ-EKF` & `4.90°` `Madgwick`) and **`2.48 s`** `90°` upset recovery (vs `10.0 s` locked-out EKF) | [`neckcompass`](https://pypi.org/project/neckcompass/) |
| **[NeckCord](https://github.com/indian-aman/necklink/tree/main/packages/neckcord)** (`v2.0.0`) | Ventral nerve cord (`DN → MN`, `11.3×` enriched) | 4-byte CRC-4 motor frame, box-constrained `[u_min, u_max]` 3-DOF `T200` USV & `[0, u_max]` 4-DOF hexacopter/octocopter active-set allocator, sensorless ESC `RPM + Current` broken-blade observer (`< 50 ms`), and 18-servo 3-DOF hexapod IK: **`0.75°` 6-DOF `MuJoCo` dead-rotor hover tilt** (`0.7 mm` alt MAE, matching `scipy` QP `6×` faster vs `3.54°` `Pinv+FDI`), **`2.07×` faster 5-leg `MuJoCo` walking**, **`916 B` total C99 SRAM** (`5.57 µs`) | [`neckcord`](https://pypi.org/project/neckcord/) |
| **[NeckSense](https://github.com/indian-aman/necklink/tree/main/packages/necksense)** (`v2.0.0`) | Antennal lobe, chordotonal & mushroom body (`2.32×` enriched) | `24.1×` smaller integer Mushroom Body (`5,804 B` `NS_TINY=1` / `20,284 B` default), 2-compartment `KCγ + KCα/β` zero-forgetting continual memory, whiff-rate tracker & 16-band vibration front-end: **`1.0000` AUROC on real CWRU variable-load bearing faults** (vs `0.617` OCSVM / `0.513` RMS) and **`93.36%` zero-replay continual gas retention** (vs `60.52%` MLP) | [`necksense`](https://pypi.org/project/necksense/) |

<p align="center">
  <img src="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/necklink_v2_g4_benchmarks.png" alt="NeckLink v2.0.0 RTX PRO 6000 G4 3D MuJoCo, real sensor datasets, and BANC v888 specificity benchmarks" width="100%">
</p>

Each product's README has its full benchmarks, its API and a "when not to use it" section — explicitly reporting where classical baselines still win (for example: dense `Farneback` optical flow on completely untextured blank walls, batch-trained float `L2-RBF SVM` on static collinear MOX arrays, and `SimHash` recall-per-byte on servers with abundant RAM).

> **November 2026 release notes (`NeckLink CNS & Organ Suite v2.0.0` + `NLTP v1.6.0`):**
> - **Whole-CNS & Organ Suite `2.0.0` (`necklink`, `necklink-cns`, `necksight`, `neckcompass`, `neckcord`, `necksense`)**:
>   - **Unified `necklink-cns` (`23,928 B` C99 SRAM, `38.82 KB` standalone WASM, `14.5%` ESP32 RAM)**: Fuses all four `BANC v888`-grounded organs (`p <= 0.005` across `1,000` GPU degree-preserving permutations on `157,789` neurons / `23.55M` synapses) with wire-valid **MAVLink v2** (`0xFD`, X.25 CRC-16 + `CRC_EXTRA`) and **ROS 2** bridges, plus a live browser WebAssembly + webcam simulator at [indian-aman.github.io/necklink](https://indian-aman.github.io/necklink/#simulator).
>   - **Four upgraded `2.0.0` organs + 5 verified `ESP32` Arduino libraries**: `NeckSight 2.0.0` (`LC4` thin-pole/wire channel, 3-DOF roll/pitch/yaw derotation, `DNa02` dodge vector, `ESP32-CAM`), `NeckCompass 2.0.0` (zero-float 9-axis 3D tilt-compensated AHRS + `1,104 B` C99 online soft-iron calibration + `PFL2` `180°` boost), `NeckCord 2.0.0` (`[0, u_max]` 4-DOF multicopter & `T200` active-set allocator + sensorless ESC `RPM + Current` broken-blade observer + 18-servo 3-DOF IK in `916 B` RAM), and `NeckSense 2.0.0` (`24.1×` C99 SRAM cut to `5,804 B` tiny, `KCγ + KCα/β` continual LTM, 16-band vibration front-end).
> - **NLTP `1.6.0` (`pynltp`, compression + MCU RAM & speed release)**: Zero-dependency standard `LZ4` block compression (`4.29×` fewer wire bytes, `3.32×` faster delivery under `25%` loss), `4,760 B` (`4.65 KB`) `-DNLTP_TX_ONLY=1` MCU sensor profile, branchless `956 MB/s` `GF(2^8)` coding, `X25519 + PSK` forward-secret `ChaCha20-Poly1305` sessions, and `248/248` 4-language interop checks.

---

## Install

```bash
pip install --upgrade necklink
# or install individual modules:
pip install --upgrade necklink-cns pynltp neckcompass necksight neckcord necksense
```

Every package supports Python 3.9+ and depends only on NumPy (`pynltp` also uses `cryptography`). Each package's C99 core (`csrc/`) and Arduino library (`arduino/`) are self-contained with zero external C dependencies.

### Run the unified Whole-CNS copilot & encrypted NLTP reflex mission

```bash
necklink-cns demo                  # 4-organ Whole-CNS closed-loop survival + MAVLink v2 demo
necklink-cns priors                # Empirical BANC v888 (157,789-neuron) connectome priors
necklink products                  # List all installed NeckLink packages and versions
necklink suite-demo --loss 0.15    # Closed-loop reflex mission over encrypted, lossy NLTP UDP
```

```python
import numpy as np
from necklink_cns import NeckLinkCNS

cns = NeckLinkCNS(vehicle="hexacopter", grid_size=32)
out = cns.step(
    dt=0.02,
    speed_m_s=1.8,
    gyro_xyz=np.array([0.02, -0.01, 0.05]),
    accel_xyz=np.array([0.2, -0.1, 9.80]),
    mag_xyz=np.array([0.45, 0.05, 0.89]),
    camera_frame=np.full((32, 32), 160.0),
)
print(out.descending.behavior_mode, len(out.cervical_wire_4b), hex(out.mavlink_manual_pkt[0]))
```

---

## Repository layout

```text
necklink/
├── packages/
│   ├── necklink_cns/  Unified 4-organ Whole-CNS copilot + MAVLink v2 + ROS 2 + WASM (v2.0.0)
│   ├── nltp/          NLTP wire protocol (v1.6.0): Python, C99, JS/TS (Node.js), Java 11+, Arduino, ESP32/ESP8266
│   ├── neckcompass/   9-DOF tilt-compensated ring-attractor compass, plastic ring & homing (v2.0.0)
│   ├── necksight/     Optic-lobe LPLC2 + LC4 thin-pole/wire looming & 3-DOF derotation (v2.0.0)
│   ├── neckcord/      4-byte cervical codec, 3-DOF/4-DOF allocator, ESC observer & 18-servo IK (v2.0.0)
│   └── necksense/     Chordotonal vibration front-end & KCg/KCab continual mushroom body (v2.0.0)
├── umbrella/          The `necklink` PyPI meta-package (v2.0.0) that installs all six packages
├── web/               Live 38.82 KB C99 WebAssembly + Webcam website (deployed to gh-pages)
└── assets/            NeckLink logo, brand banner and RTX PRO 6000 (G4) benchmark figures
```

Every folder under `packages/` is a self-contained project with its own `pyproject.toml`, tests, zero-float ANSI C99 core, Arduino library, README and MIT licence, and is released independently to PyPI (`import necklink_cns`, `import nltp`, `import neckcompass`, `import necksight`, `import neckcord`, `import necksense`).

---

## Development

Work inside the package you are changing:

```bash
cd packages/necklink_cns
pip install -e . pytest
python -m pytest                         # unit tests, MAVLink v2, Arduino parity & README examples
make -C csrc clean all test sanitize     # zero-heap/zero-float C99 Whole-CNS (<24 KB) + ASan/UBSan
```

CI ([`.github/workflows/packages-ci.yml`](https://github.com/indian-aman/necklink/blob/main/.github/workflows/packages-ci.yml)) runs on Python 3.9 and 3.13 across `NeckLink CNS`, `NeckCompass`, `NeckCord`, `NeckSense`, `NeckSight`, and `NLTP` (including `-Werror` + ASan/UBSan C99 checks, Arduino `SHA-256` mirror parity, Node.js 20, Java 21, and `248/248` 4-language wire interop):
- builds each package's wheel and installs it outside the source tree;
- runs its CLI smoke checks;
- executes all unit tests and every README Python example;
- compiles its zero-heap ANSI C99 core with `-std=c99 -Wall -Wextra -pedantic -Werror` and runs its self-tests.

### History

NeckCompass, NeckSight, NeckCord and NeckSense started in their own repositories and were merged here with their full commit history on 4 November 2026. The old repositories are archived and read-only.

---

## License

Released under the [MIT License](https://github.com/indian-aman/necklink/blob/main/LICENSE). Copyright (c) 2026 Amandeep Singh Bains.
