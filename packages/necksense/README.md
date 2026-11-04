<div align="center">

<a href="https://github.com/indian-aman/necklink"><img src="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/logo/necklink-mark.svg" alt="NeckLink" width="52"></a>

# NeckSense (`necksense` v1.0.0)

**Concentration-invariant sparse hashing, 1-shot novelty detection, and online associative classification inspired by the *Drosophila* Antennal Lobe & Mushroom Body — in Python (`NumPy`) and zero-heap, zero-float ANSI C99 (`ESP32` / Cortex-M).**

*Product #2 in the [**NeckLink**](https://github.com/indian-aman/necklink) fruit-fly brain engineering suite.*

[![PyPI](https://img.shields.io/pypi/v/necksense?style=flat-square&logo=pypi&logoColor=white&label=PyPI%20necksense&color=0284C7)](https://pypi.org/project/necksense/)
[![Release](https://img.shields.io/badge/release-v1.0.0-0EA5E9?style=flat-square)](https://github.com/indian-aman/necksense/releases/tag/v1.0.0)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://pypi.org/project/necksense/)
[![ANSI C99](https://img.shields.io/badge/C99-zero--heap%20%7C%20zero--float%20Q16.16-00599C?style=flat-square&logo=c&logoColor=white)](https://github.com/indian-aman/necksense/tree/main/csrc)
[![Tests](https://img.shields.io/badge/tests-14%2F14%20pytest%20%2B%205%2F5%20C99-10B981?style=flat-square)](https://github.com/indian-aman/necksense/tree/main/tests)
[![License: MIT](https://img.shields.io/badge/license-MIT-F59E0B?style=flat-square)](https://github.com/indian-aman/necksense/blob/main/LICENSE)

[**30-Second Quick Start**](#-30-second-quick-start) ·
[**CLI Usage**](#2-command-line-interface-necksense) ·
[**Zero-Heap ANSI C99 (`ESP32` / MCU)**](#3-zero-heap-zero-float-ansi-c99-core-csrc) ·
[**Empirical Benchmarks**](#-empirical-benchmarks-5-seed-mean) ·
[**When to Use — and When Not To**](#-when-to-use-necksense--and-when-not-to)

</div>

<p align="center">
  <img src="https://raw.githubusercontent.com/indian-aman/necksense/main/assets/necksense_benchmarks.png" alt="NeckSense v1.0.0 Empirical Benchmarks" width="100%">
</p>

---

## Why NeckSense?

Real-world chemical arrays (electrochemical, MOX, PID electronic noses), vibration probes, and edge telemetry nodes suffer from three problems simultaneously: **slow thermal/aging baseline drift**, **`100x+` plume concentration swings**, and **tiny MCU memory/compute budgets** where matrix inversions or backpropagation cannot run online.

The fruit fly (*Drosophila melanogaster*) solves all three in ~2,500 neurons. **NeckSense** translates that four-stage circuit into clean, deterministic Python (`numpy>=1.24`) and bit-identical `Q16.16` fixed-point ANSI C99:

```
Raw Sensor Stream x(t)
   │
   ▼  [1] ORN Dual-Timescale Adaptation (subtracts slow tonic drift, keeps phasic transients)
   │
   ▼  [2] Antennal Lobe Olsen-Wilson LN Normalization:  r_i = p_i^n / (sigma^n + (gamma * mean_pool)^n + p_i^n)
   │
   ▼  [3] Mushroom Body Calyx Sparse Projection (d_proj=6 random inputs summed per KC — ZERO float muls!)
   │      + APL Global Inhibition (strict top-5% WTA packed into 128 B / 256 B uint64 bitwords)
   │
   ├──► KenyonIndex:              64-bit AND + Popcount similarity search
   ├──► MushroomNoveltyDetector:  1-shot MBON-alpha'3 anti-Hebbian anomaly score in [0, 1]
   └──► MushroomValenceClassifier: 1-shot / few-shot DAN -> KC -> MBON associative classifier
```

- **Concentration-Invariant Across `100x–250x` Swings**: Olsen-Wilson lateral GABAergic divisive gain control preserves combinatorial channel ratios (`cosine > 0.9999` across `100x` scaling) while `sigma^n` squelches zero-odor dark noise instead of blowing it onto the unit sphere like naive `L2` normalization.
- **Zero Floating-Point Multiplications in Projection**: Each Kenyon Cell sums `d_proj=6` deterministically chosen inputs (`ADD` only) before `APL` global winner-take-all sparsification (`wta_frac=0.05`), requiring **0 bytes** of floating-point weight matrix RAM.
- **True 1-Shot Online Plasticity**: `5%`-sparse Kenyon codes have `< 0.3%` random overlap between unrelated states, allowing `MBON-alpha'3` novelty habituation and `DAN -> KC -> MBON` class learning in **a single exposure** without catastrophic interference.
- **Bit-Identical Python ↔ C99 (`Q16.16`) Parity**: Set `c_parity=True` in Python to obtain **100% bit-for-bit identical** `Q16.16` outputs, `uint64` Kenyon tags, novelty scores, and valence scores as `csrc/necksense.c`.

---

## ⚡ 30-Second Quick Start

### 1. Python (`pip install necksense`)

```bash
pip install necksense
# Or from source:
pip install git+https://github.com/indian-aman/necksense.git
```

Condition drifting sensor streams, index sparse `128 B` binary tags, detect 1-shot anomalies, and classify `20x`-scaled plumes in 20 lines:

```python
import numpy as np
from necksense import (
    AntennalNormalizer,
    KenyonIndex,
    MushroomNoveltyDetector,
    MushroomValenceClassifier,
)

# 1. Antennal Lobe: drift removal + 100x concentration-invariant normalization
ant = AntennalNormalizer(n_channels=8, sigma=0.008, gamma=1.0, hill_n=2.0, tonic_alpha=0.03)
ethanol_1x = np.array([0.90, 0.35, 0.10, 0.02, 0.50, 0.05, 0.01, 0.20])
acetone_1x = np.array([0.45, 0.15, 0.08, 0.80, 0.75, 0.10, 0.30, 0.05])

# 2. 1-shot associative odor learning (DAN -> KC -> MBON)
clf = MushroomValenceClassifier(n_inputs=8, antennal=ant, n_kc=1024, d_proj=6, wta_frac=0.05)
clf.learn(ethanol_1x, "ethanol", reward=1.0)
clf.learn(acetone_1x, "acetone", reward=1.0)
assert clf.predict(20.0 * acetone_1x) == "acetone"  # Recognizes 20x plume from a single 1x shot!

# 3. 1-shot streaming anomaly detection (MBON-alpha'3 anti-Hebbian depression)
nov = MushroomNoveltyDetector(n_inputs=8, antennal=ant, n_kc=1024, lr=0.55)
s_unseen = nov.update_and_score(ethanol_1x)   # ~1.000 (novel)
_ = nov.update_and_score(ethanol_1x)
s_habituated = nov.update_and_score(ethanol_1x)  # ~0.203 (habituated in 3 exposures)
s_anomaly = nov.update_and_score(acetone_1x)     # >0.900 (unseen chemical pattern)

# 4. Fast Popcount similarity search over 128-byte (1024-bit) Kenyon tags
idx = KenyonIndex(n_inputs=64, n_kc=1024, d_proj=6, wta_frac=0.05)
db = np.random.default_rng(0).standard_normal((5000, 64))
idx.add(db)
best = idx.query(db[42] + 0.05, top_k=5)[0]
assert best.id == 42
```

---

### 2. Command-Line Interface (`necksense`)

```bash
# Run interactive 4-stage Drosophila circuit showcase:
necksense demo

# Run fast 3-task empirical verification (~1.5 s):
necksense benchmark-quick

# Hash numeric CSV rows into deterministic 128-byte hex Kenyon tags:
necksense hash-csv sensors.csv --id-col 0 --n-kc 1024 --antennal-normalize -o tags.csv
```

---

### 3. Zero-Heap, Zero-Float ANSI C99 Core (`csrc/`)

[`csrc/necksense.h`](https://github.com/indian-aman/necksense/blob/main/csrc/necksense.h) and [`csrc/necksense.c`](https://github.com/indian-aman/necksense/blob/main/csrc/necksense.c) use pure `int32_t` / `uint32_t` / `uint64_t` (`Q16.16` fixed-point, `1.0 == 65536`) with **zero `malloc`** and **zero `float`/`double`**, compiling cleanly under `-O2 -std=c99 -Wall -Wextra -pedantic -Werror`:

```bash
make -C csrc clean all test
```

Static memory footprints (default `NS_MAX_KC=2048`; shrinkable via `-DNS_MAX_KC=1024 -DNS_MAX_CLASSES=6` to `< 30 KB` total SRAM on `$4` `ESP32` or `STM32` Cortex-M):

- `ns_antennal_t`: **`548 B`**
- `ns_kenyon_t`: **`24 B`** (**`0 B`** projection weight table — deterministic divisionless PRNG on-the-fly)
- `ns_novelty_t`: **`8,216 B`** (`4.1 KB` at `M=1024`)
- Full 3-stage C99 pipeline (`ns_antennal_step` + `ns_kenyon_hash` + `ns_novelty_update_and_score` at `D=16, M=1024`): **`24.3 us/sample`** (`> 41,000 samples/sec` on a single core).

```c
#include "necksense.h"

static ns_antennal_t ant;
static ns_kenyon_t   ken;
static ns_novelty_t  nov;

void sensor_init(void) {
    ns_antennal_init(&ant, 8u, 512, NS_Q16_ONE, 2u, 655, 1311, 32768, 22938);
    ns_kenyon_init(&ken, 8u, 1024u, 6u, 51u, 42u);
    ns_novelty_init(&nov, 1024u, 36045 /* lr=0.55 */, 0);
}

int32_t sensor_tick_q16(const int32_t raw_q16[8]) {
    int32_t norm_q16[8];
    uint64_t kc_tag[16];
    ns_antennal_step(&ant, raw_q16, norm_q16);
    ns_kenyon_hash(&ken, norm_q16, kc_tag);
    return ns_novelty_update_and_score(&nov, kc_tag); /* [0 .. 65536] */
}
```

---

## 📊 Empirical Benchmarks (5-Seed Mean)

Reproduce all numbers and regenerate `assets/necksense_benchmarks.png` and `results/benchmark_summary.json` locally:

```bash
python scripts/benchmark_necksense.py
```

### 1. Nearest-Neighbor Retrieval (`N=5,000`, `D=64` Continuous Embeddings)

#### A. Equal Active-Code Dimension `k` (Dasgupta, Stevens & Navlakha, *Science* 2017 Protocol)

Comparing `FlyHash` (`M = 20k` Kenyon cells, `5%` WTA = `k` active spikes, **0 float multiplications**) against `Dense Gaussian SimHash` (`k` Gaussian hyperplanes, `64 * k` float multiplications):

| Active Spikes / Projections (`k`) | **`FlyHash` (`KenyonHasher`) `Recall@10`** | `SimHash` (`Gaussian LSH`) `Recall@10` | `FlyHash` Gain | `FlyHash` Float Muls | `SimHash` Float Muls |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`k = 16`** (`M = 320`) | **`18.29% ± 0.53%`** | `9.48% ± 0.38%` | **`+92.9%` (`1.93x`)** | **`0`** | `1,024` |
| **`k = 32`** (`M = 640`) | **`23.41% ± 0.23%`** | `13.48% ± 0.46%` | **`+73.7%` (`1.74x`)** | **`0`** | `2,048` |
| **`k = 64`** (`M = 1280`) | **`29.61% ± 0.53%`** | `16.81% ± 0.46%` | **`+76.1%` (`1.76x`)** | **`0`** | `4,096` |
| **`k = 128`** (`M = 2560`) | **`36.14% ± 0.25%`** | `22.42% ± 0.24%` | **`+61.2%` (`1.61x`)** | **`0`** | `8,192` |

#### B. Honest Raw Uncompressed Bitset Comparison (`M_bits` Dense Bitwords)

When both methods are packed as raw dense `M_bits` bitvectors (`64 B`, `128 B`, `256 B`) with no sparse index compression, **Dense Gaussian SimHash achieves higher `Recall@10` per raw uncompressed bit** because a `50%`-dense bitvector carries `1.00` bit of Shannon entropy per bit vs `0.29` bits for a `5%`-sparse bitvector — at the cost of `32K–131K` float multiplications and `128–512 KB` of Gaussian weight RAM:

| Dense Tag Size (`M_bits`) | `FlyHash` `Recall@10` | **`SimHash` `Recall@10`** | **`FlyHash` Float Muls / Weight RAM** | `SimHash` Float Muls / Weight RAM | Query Latency (`N=5,000`) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`64 B`** (`512` bits) | `21.31%` (`k=26`) | **`40.74%`** | **`0` muls / `0 KB`** | `32,768` muls / `128 KB` | **`220 us`** vs `221 us` |
| **`128 B`** (`1024` bits) | `27.54%` (`k=51`) | **`50.65%`** | **`0` muls / `0 KB`** | `65,536` muls / `256 KB` | **`377 us`** vs `413 us` |
| **`256 B`** (`2048` bits) | `34.05%` (`k=102`) | **`59.74%`** | **`0` muls / `0 KB`** | `131,072` muls / `512 KB` | **`658 us`** vs `728 us` |

---

### 2. Multi-Gas Plume Classification (`8`-Sensor Array, `6` Cross-Sensitive Gases, `3` Calibration Shots/Gas)

Evaluated across `300` test plumes per seed spanning `250x` dynamic concentration swings (`0.1x–25x`) and slow sinusoidal + linear thermal baseline drift:

| Field Regime | **`NeckSense` (`3-shot`)** | **`NeckSense` (`1-shot`)** | `L2-Normalized k-NN` | `Raw Z-Score Nearest-Centroid` |
| :--- | :--- | :--- | :--- | :--- |
| **Clean Reference (`1.0x`, No Drift)** | **`100.0%`** | **`100.0%`** | **`100.0%`** | **`100.0%`** |
| **Pure `0.1x–25x` Swings (No Drift)** | **`100.0%`** | **`100.0%`** | **`100.0%`** | `89.9% ± 2.9%` |
| **Weak Plumes (`0.1x–0.5x`) + Thermal Drift** | **`99.3% ± 0.7%`** | **`99.5% ± 0.6%`** | `33.4% ± 4.7%` | `30.1% ± 4.4%` |
| **Full Field Stream (`0.1x–25x` + Thermal Drift)** | **`99.5% ± 0.5%`** | **`99.5% ± 0.5%`** | `76.5% ± 4.1%` | `72.7% ± 3.2%` |

*(Why `L2-Normalized k-NN` drops to `33.4%` on weak plumes under drift: dividing `(0.2 * s_gas + b_drift)` by its L2 norm points the unit vector along `b_drift` rather than `s_gas`. `AntennalNormalizer` strips `b_drift` via slew-guarded tonic adaptation before applying Olsen-Wilson divisive normalization.)*

---

### 3. Multi-Regime Streaming 1-Shot Anomaly Detection (`D=16`, `20` Normal Modes, `0.5x–5x` Swings)

| Online Detector | **Streaming `AUROC` (5-Seed Mean)** | Python Update Latency | ANSI C99 Latency (`Q16.16`) | Memory & Structural Notes |
| :--- | :--- | :--- | :--- | :--- |
| **`NeckSense MBON-alpha'3`** | **`0.9658 ± 0.0048`** | `118 us` (`71 us` batch) | **`24.3 us`** (full 3-stage) | Habituates `20` full-rank modes in 1–2 visits (`4 KB` weights) |
| `Online Mahalanobis` *(Woodbury)* | `0.8453 ± 0.0209` | `19.7 us` | Float matrix (`O(D^2)`) | Unimodal Gaussian blurs multi-modal operating states |
| `Rolling Z-Score` *(Per-Channel EMA)* | `0.6349 ± 0.0400` | **`11.4 us`** | Trivial scalar | Fast on scalar spikes; false-alarms on normal mode switches |
| `Online Half-Space` *(32 Projections)* | `0.5902 ± 0.0346` | `48.8 us` | Float projections | 1D marginal bins miss `0.5x–5x` scaled multi-channel chords |

---

## 🎯 When to Use NeckSense — and When Not To

> **Reach for NeckSense when:**
> - **Multi-channel chemical, optical, acoustic, or vibration sensors experience baseline drift and `10x–250x` amplitude swings** (`AntennalNormalizer` + `MushroomValenceClassifier` reaches `99.5%` from 1–3 calibration shots where `L2 k-NN` drops to `33–76%`).
> - **Deploying online 1-shot anomaly detection or associative learning onto `$4` `ESP32` / Cortex-M microcontrollers** with **zero `malloc`**, **zero floating-point**, and **zero projection matrix RAM** (`24.3 us/sample` in ANSI C99).
> - **Downstream tasks need sparse, non-interfering binary codes (`5%` active)** so new classes or normal operating modes can be written in 1 shot without overwriting prior memories.

> **Stick with standard tools when:**
> - **Pure server-side ANN vector search with floating-point SIMD/GPUs and dense bit budgets**: At equal uncompressed bitword size (`128 B` = `1024` bits), **Dense Gaussian SimHash** (`50.7%` vs `27.5%` `Recall@10`), **HNSW (`faiss` / `hnswlib`)**, or **Product Quantization (PQ)** deliver substantially higher recall per byte on CPUs/GPUs that have abundant RAM (`256–512 KB` projection matrices) and fast `FMA` hardware.
> - **Simple 1D threshold / scalar amplitude spike monitoring**: Per-channel **`Rolling Z-Score`** runs in `11 us` in Python (`< 1 us` in C) and is simpler when normal operation has a single stationary baseline and faults are pure amplitude excursions.
> - **Large labeled training sets (`10,000+` examples) on cloud compute**: Gradient-boosted trees (`LightGBM`/`XGBoost`) or supervised neural networks trained offline will outperform 1-shot Hebbian/anti-Hebbian associative rules when labeled drift/concentration data is plentiful.

---

## 📚 Scientific Foundations

1. **Olsen, S. R. & Wilson, R. I. (2008).** Lateral presynaptic inhibition mediates gain control in an olfactory circuit. *Nature*, 452, 956–960.
2. **Nagel, K. I. & Wilson, R. I. (2011).** Biophysical mechanisms underlying olfactory receptor neuron dynamics. *Nature Neuroscience*, 14, 208–216.
3. **Dasgupta, S., Stevens, C. F. & Navlakha, S. (2017).** A neural algorithm for a fundamental computing problem. *Science*, 358(6364), 793–796.
4. **Hattori, D. et al. (2017).** Representations of novelty and familiarity in a mushroom body compartment. *Cell*, 169(5), 956–969.
5. **Aso, Y. et al. (2014).** Mushroom body output neurons encode valence and guide memory-based action selection in *Drosophila*. *eLife*, 3, e04580.

---

## 🗂️ Repository Layout

| Path | Description |
| :--- | :--- |
| [`necksense/antennal.py`](https://github.com/indian-aman/necksense/blob/main/necksense/antennal.py) | `AntennalNormalizer` (dual-timescale drift adaptation + Olsen-Wilson `LN` divisive gain + `Q16.16` parity) |
| [`necksense/kenyon.py`](https://github.com/indian-aman/necksense/blob/main/necksense/kenyon.py) | `KenyonHasher` (zero-multiply `d_proj=6` sparse projection + `APL` WTA) & `KenyonIndex` (Popcount search + `.npz` I/O) |
| [`necksense/mushroom.py`](https://github.com/indian-aman/necksense/blob/main/necksense/mushroom.py) | `MushroomNoveltyDetector` (`MBON-alpha'3`) & `MushroomValenceClassifier` (`DAN -> KC -> MBON`) |
| [`necksense/cli.py`](https://github.com/indian-aman/necksense/blob/main/necksense/cli.py) | `necksense` CLI (`demo`, `benchmark-quick`, `hash-csv`) |
| [`csrc/`](https://github.com/indian-aman/necksense/tree/main/csrc) | Zero-heap, zero-float `Q16.16` ANSI C99 library (`necksense.h`, `necksense.c`), `Makefile`, and `5/5` self-test |
| [`scripts/benchmark_necksense.py`](https://github.com/indian-aman/necksense/blob/main/scripts/benchmark_necksense.py) | Reproducible 5-seed benchmark suite & 3-panel hero chart generator |
| [`results/benchmark_summary.json`](https://github.com/indian-aman/necksense/blob/main/results/benchmark_summary.json) | Complete empirical metrics across all 3 benchmark tasks |
| [`tests/test_necksense.py`](https://github.com/indian-aman/necksense/blob/main/tests/test_necksense.py) | `14`-test `pytest` suite including static C99 zero-heap/float audit and live C99↔Python bit-parity |

---

## License

Released under the [MIT License](https://github.com/indian-aman/necksense/blob/main/LICENSE). Copyright (c) 2026 Amandeep Singh Bains.
