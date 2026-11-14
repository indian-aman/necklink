<div align="center">

<a href="https://github.com/indian-aman/necklink"><img src="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/logo/necklink-mark.svg" alt="NeckLink" width="52"></a>

# NeckSense (`necksense` v3.0.0, research beta)

**Concentration- and load-invariant normalisation, zero-float vibration/acoustic spectrum conditioning, sparse binary hashing, one-shot novelty detection and continual associative classification, modelled on the *Drosophila* chordotonal organ, antennal lobe and two-compartment mushroom body (`BANC v888` / `MaleCNS`), in Python (NumPy), ANSI C99 (`< 6 KB` SRAM with `-DNS_TINY=1`) and Arduino.**

[![PyPI](https://img.shields.io/pypi/v/necksense?style=flat-square&logo=pypi&logoColor=white&label=PyPI%20necksense&color=10B981&cacheSeconds=180&v=200)](https://pypi.org/project/necksense/)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://pypi.org/project/necksense/)
[![ANSI C99](https://img.shields.io/badge/C99-zero--heap%20%7C%20zero--float%20Q16.16-00599C?style=flat-square&logo=c&logoColor=white)](csrc/)
[![SRAM](https://img.shields.io/badge/MCU%20SRAM-5.8%20KB%20(NS__TINY)-0EA5E9?style=flat-square)](csrc/)
[![Tests](https://img.shields.io/badge/tests-24%20pytest%20%2B%208%20C99-10B981?style=flat-square)](tests/)
[![License: MIT](https://img.shields.io/badge/license-MIT-F59E0B?style=flat-square)](LICENSE)

[**Install**](#install) ·
[**Quick start**](#quick-start) ·
[**C99 & Arduino**](#zero-heap-zero-float-ansi-c99-core-csrc--arduino-library) ·
[**Benchmarks**](#benchmarks-5-seed-mean) ·
[**When to use it**](#when-to-use-necksense-and-when-not-to) ·
[**Changelog**](#changelog)

</div>

> [!NOTE]
> **v3.0.0: the mushroom-body projection now uses connectome wiring by default** — `1,024` of the `4,122` Kenyon cells, each with its real `6` projection-neuron inputs from `BANC v888`, plus real `APL` feedback and `KC → MBON` weights (about `10,000` numbers). How your sensor channels map onto fly glomeruli is a design choice with no biological meaning. **Result:** shuffled wiring performs the same as the real wiring on every task, and classical baselines win. See [v3.0.0 results](#v300-connectome-derived-projection-audited-results). The older benchmarks further down were measured with the `xorshift32` projection (now `connectome_weights="v2_analytic"`) before v3 and have not been re-run.

<p align="center">
  <img src="https://raw.githubusercontent.com/indian-aman/necklink/main/packages/necksense/assets/necksense_benchmarks.png" alt="NeckSense benchmarks" width="100%">
</p>

---

## What it is

Multi-channel chemical, IMU vibration, piezo acoustic and telemetry sensors face slow baseline drift, `10×–100×` load/concentration swings, sequential class forgetting and tight microcontroller SRAM budgets. NeckSense turns the fly's sensory conditioning and Mushroom Body memory circuits (`BANC v888` connectome: `3,006` ORNs, `2,132` frequency-tuned chordotonal mechanoreceptors, `2,057` `KCγ` short-term + `1,943` `KCα/β` long-term + `122` `KCα'/β'` novelty Kenyon cells) into a compact Python + ANSI C99 library:

```
raw IMU / piezo window w[0..N-1] (optional)
  │  [0] Chordotonal VibrationFrontEnd: 16-ch integer stationary Haar + resonator
  │      + impulse crest + envelope AM filterbank (strips static 1g tilt on 3-axis IMUs)
  ▼
raw or spectral vector x(t)
  │  [1] ORN dual-timescale adaptation + phasic plume whiff-rate / intermittency tracking
  │  [2] Antennal-lobe divisive normalisation: r_i = p_i^n / (sigma^n + (gamma*mean)^n + p_i^n)
  │  [3] Mushroom-body projection: each Kenyon cell sums d_proj=6 inputs (additions only),
  │      then global APL winner-take-all keeps top k cells -> packed uint64 bit tag
  ├──► KenyonIndex                 popcount similarity search over binary tags
  ├──► MushroomNoveltyDetector     KCα'/β' -> MBON-α'3 one-shot habituation & anomaly score
  └──► MushroomValenceClassifier   KCγ (fast STM) + KCα/β (consolidated anti-forgetting LTM)
```

- **Concentration & motor-load invariance.** Channel *ratios* survive scaling: cosine similarity between `1×` and `100×` inputs exceeds `0.99999`, and `VibrationFrontEnd` uses homogeneous degree-1 integer features so 1-shot bearing/propeller/spindle fault signatures survive `10×+` throttle or load swings.
- **Plume whiff intermittency.** `AntennalNormalizer.step_with_whiff()` and `diagnostics()` track phasic ORN up-crossings (`2–20 Hz` turbulent odor filaments vs static background clouds) for chemical source proximity.
- **Two-compartment continual memory (`KCγ` + `KCα/β`).** Calling `clf.consolidate()` (`ns_valence_consolidate()` in C) protects consolidated `α/β`-lobe synapses from cross-class opponent erosion while preserving explicit opposite-sign reward extinction (`0%` catastrophic forgetting across sequential multi-class updates).
- **Weights.** With `connectome_weights="v2_analytic"`, a deterministic `xorshift32` projection uses `0 B` of weights and zero multiplications. With the v3 default (`"banc_v888"`, `n_kc <= 1024`), the Kenyon-cell claws, `APL` and `MBON` weights are `const` tables (`necksense_banc_weights.h`) that live in flash. The C99 suite state fits in **`20,284 B`** by default and **`5,804 B` (`< 6 KB`)** with `-DNS_TINY=1`.
- **Bit-identical Python ↔ C.** With `c_parity=True`, Python reproduces the C core's `Q16.16` / `Q14` outputs, tags, whiff rates, vibration features and scores bit-for-bit.

---

## Install

```bash
pip install necksense
# or from this repository:
pip install "git+https://github.com/indian-aman/necklink.git#subdirectory=packages/necksense"
```

---

## Quick start

```python
import numpy as np
from necksense import (
    AntennalNormalizer,
    KenyonIndex,
    MushroomNoveltyDetector,
    MushroomValenceClassifier,
    VibrationFrontEnd,
)

ethanol_1x = np.array([0.90, 0.35, 0.10, 0.02, 0.50, 0.05, 0.01, 0.20])
acetone_1x = np.array([0.45, 0.15, 0.08, 0.80, 0.75, 0.10, 0.30, 0.05])

# 1. Antennal lobe + associative learning + KC-alpha/beta LTM consolidation
ant = AntennalNormalizer(n_channels=8, sigma=0.008, gamma=1.0, hill_n=2.0, tonic_alpha=0.03)
clf = MushroomValenceClassifier(n_inputs=8, antennal=ant, n_kc=1024, d_proj=6, wta_frac=0.05)
clf.learn(ethanol_1x, "ethanol", reward=1.0)
clf.consolidate()  # protect in KC-alpha/beta LTM against future multi-class overwriting
clf.learn(acetone_1x, "acetone", reward=1.0)
assert clf.predict(20.0 * acetone_1x) == "acetone"

# 2. One-shot IMU / acoustic bearing-fault classification across a 10x load swing
vib = VibrationFrontEnd(n_samples=128)
vib_clf = MushroomValenceClassifier(n_inputs=16, antennal=AntennalNormalizer(16, sigma=0.004))
t = np.arange(128, dtype=np.float64)
healthy_1x = 0.20 * np.sin(2.0 * np.pi * t / 32.0)
spall_1x = healthy_1x + np.where((t % 16) == 0, 0.55, 0.0)
vib_clf.learn(vib.extract(healthy_1x), "healthy")
vib_clf.learn(vib.extract(spall_1x), "bearing_spall")
assert vib_clf.predict(vib.extract(10.0 * spall_1x)) == "bearing_spall"

# 3. Streaming novelty + turbulent plume whiff rate
nov = MushroomNoveltyDetector(n_inputs=8, antennal=AntennalNormalizer(8, sigma=0.008), lr=0.55)
scores = [nov.update_and_score(ethanol_1x) for _ in range(3)]   # 1.00 -> 0.45 -> 0.20
s_anomaly = nov.update_and_score(acetone_1x)                    # unseen pattern: > 0.75
_, whiff_rate = ant.step_with_whiff(ethanol_1x)
print([round(s, 2) for s in scores], round(s_anomaly, 2), round(whiff_rate, 3))

# 4. Popcount similarity search over 128-byte (1024-bit) Kenyon tags
idx = KenyonIndex(n_inputs=64, n_kc=1024, d_proj=6, wta_frac=0.05)
db = np.random.default_rng(0).standard_normal((5000, 64))
idx.add(db)
assert idx.query(db[42] + 0.05, top_k=5)[0].id == 42
```

### Command line

```bash
necksense demo                    # four-stage circuit + vibration walkthrough
necksense benchmark-quick         # short version of the three benchmarks
necksense hash-csv sensors.csv --id-col 0 --n-kc 1024 --antennal-normalize -o tags.csv
```

---

## Zero-heap, zero-float ANSI C99 core (`csrc/`) & Arduino library

[`csrc/necksense.h`](csrc/necksense.h) and [`csrc/necksense.c`](csrc/necksense.c) use only fixed-width integer types (`int16_t`, `uint16_t`, `int32_t`, `uint32_t`, `int64_t`, `uint64_t` in `Q16.16` / `Q14`), with zero heap allocation and zero floating-point types. Both the standard profile and `-DNS_TINY=1` compile cleanly under `-O2 -std=c99 -Wall -Wextra -pedantic -Werror` and `-fsanitize=address,undefined`.

```bash
make -C csrc clean all test sanitize
```

```c
#include "necksense.h"

static ns_vib_fe_t   vib;
static ns_antennal_t ant;
static ns_kenyon_t   ken;
static ns_novelty_t  nov;

void sensor_init(void) {
    ns_vib_init(&vib, 128u, 5u);
    ns_antennal_init(&ant, NS_VIB_CHANNELS, 256, NS_Q16_ONE, 2u, 655, 1311, 32768, 22938);
    ns_kenyon_init(&ken, NS_VIB_CHANNELS, 512u, 6u, 10u, 42u); /* 2% active for novelty */
    ns_novelty_init(&nov, 512u, 36045 /* lr = 0.55 */, 16);
}

int32_t imu_window_novelty_q16(const int16_t accel_xyz_mg[128 * 3]) {
    int32_t feat_q16[NS_VIB_CHANNELS];
    int32_t norm_q16[NS_VIB_CHANNELS];
    uint64_t kc_tag[8];
    ns_vib_extract_3axis_i16(&vib, accel_xyz_mg, 128u, feat_q16);
    ns_antennal_normalize(&ant, feat_q16, norm_q16);
    ns_kenyon_hash(&ken, norm_q16, kc_tag);
    return ns_novelty_update_and_score(&nov, kc_tag); /* 0 .. 65536 */
}
```

### Static SRAM footprint (verified by `necksense_selftest` & `necksense_selftest_tiny`)

| Struct | v1.1.0 Default | **v2.0.0 Default** (`M=1024, C=8`) | **v2.0.0 `-DNS_TINY=1`** (`M=512, C=4`) |
| :--- | :---: | :---: | :---: |
| `ns_antennal_t` (drift + divisive + whiff) | `548 B` | **`576 B`** | **`256 B`** |
| `ns_kenyon_t` (0-byte weight matrix) | `24 B` | **`24 B`** | **`24 B`** |
| `ns_novelty_t` (lossless 17-bit `Q16.16`) | `8,216 B` | **`2,200 B`** | **`1,112 B`** |
| `ns_valence_t` (`Q14` + `KCα/β` LTM mask) | `131,172 B` | **`17,472 B`** | **`4,400 B`** |
| `ns_vib_fe_t` (16-ch chordotonal front-end) | — | **`12 B`** | **`12 B`** |
| **Total 5-struct suite** | `139,960 B` (`~140 KB`) | **`20,284 B` (`6.9×` smaller)** | **`5,804 B` (`< 6 KB`, `24×` smaller)** |

Full C99 pipeline (`D=16` normalise + hash + novelty): **~14 us/sample** (`-DNS_TINY=1`, `M=512`) to **~35 us/sample** (`M=1024`) on a single desktop x86-64 core at `-O2`.

### Drop-in Arduino / PlatformIO library (`arduino/NeckSense/`)

Copy [`arduino/NeckSense`](arduino/NeckSense) into `~/Documents/Arduino/libraries/NeckSense` (or add `lib_deps = symlink://.../packages/necksense/arduino/NeckSense` in `platformio.ini`) and `#include <NeckSense.h>`. AVR and ESP8266 targets automatically enable `NS_TINY=1` (`5,804 B` total SRAM); see [`ESP32_Vibration_And_Gas_Anomaly.ino`](arduino/NeckSense/examples/ESP32_Vibration_And_Gas_Anomaly/ESP32_Vibration_And_Gas_Anomaly.ino) for a complete 3-axis IMU vibration + 8-channel MOX gas anomaly sketch.

---

## v3.0.0: connectome-derived projection (audited results)

```python
import numpy as np
from necksense import MushroomNoveltyDetector

rng = np.random.default_rng(0)
pattern = rng.uniform(0.2, 1.0, 16)                       # normal channel ratios
normal = [pattern * rng.uniform(0.5, 5.0) * rng.normal(1.0, 0.03, 16) for _ in range(200)]
novel = rng.permutation(pattern) * 2.0                    # same values, different ratios
for weights in ("banc_v888", "v2_analytic"):  # banc_v888 is the default in 3.0.0
    det = MushroomNoveltyDetector(n_inputs=16, n_kc=1024, seed=0, connectome_weights=weights)
    for x in normal:
        det.update_and_score(x)
    print(weights, "familiar:", round(det.score(pattern * 3.0), 3), "novel:", round(det.score(novel), 3))
```

Benchmark: `N = 30` seeds on real public data with strict chronological / cross-load splits (no test data seen in training), against `K = 100` degree-preserving (Maslov–Sneppen) shuffles of the raw `ALPN → KC` matrices compiled with the identical pipeline; one-sided permutation `p = (1 + r) / (K + 1)`. Classical baselines get the same L2 normalisation, with scalers fit on training data only. Code and raw JSON: [`benchmarks/v3/`](https://github.com/indian-aman/necklink/tree/main/benchmarks/v3).

| Task | `banc_v888` | Shuffled wiring (mean) | `r / 100`, `p` | `v2_analytic` | Classical |
| :--- | :---: | :---: | :---: | :---: | :---: |
| UCI gas drift: novel-gas detection, batches 1–2 → 3–10 (AUROC) | `0.666` | `0.672` | `74`, `0.74` | `0.642` | **One-Class SVM `0.698`**, Isolation Forest `0.694` |
| UCI gas drift: 5-shot class-incremental, 6-class accuracy (task-1 retained) | `42.7%` (`51.4%`) | `43.2%` | `65`, `0.65` | `46.3%` (`54.1%`) | **Nearest centroid `60.1%` (`65.5%`)**; replay-free MLP `16.1%` (`0%`) |
| CWRU bearings: train 0 HP normal → test 2 HP faults (AUROC) | `0.9995` | `0.99998` | `100`, `1.00` | `1.000` | One-Class SVM `1.000`, Isolation Forest `0.9985` |

What this shows:
- **The specific fly wiring does not matter here.** Any sparse expand-and-sparsify projection with the same degree distribution performs the same, consistent with Dasgupta et al. (2017). This is expected when sensor channels have no real correspondence to fly glomeruli.
- **Classical methods win** on gas novelty and few-shot classification. CWRU is near the ceiling for every method, so it does not separate them.
- **Where it helps:** it retains old classes far better than a naively trained replay-free network, in a few KB of integer state.

---

## Benchmarks (5-seed mean, `xorshift32` projection, pre-v3)

```bash
python scripts/benchmark_necksense.py     # ~40 s; writes results/benchmark_summary.json and the chart
```

### 1. Nearest-neighbour retrieval (`N = 5,000`, `D = 64`, Recall@10)

**A. Equal number of active bits `k`.** This is the protocol of Dasgupta, Stevens & Navlakha (*Science* 2017). FlyHash uses `M = 20k` Kenyon cells; SimHash uses `k` Gaussian hyperplanes.

| `k` | FlyHash | SimHash | FlyHash float multiplications | SimHash float multiplications |
| :--- | :---: | :---: | :---: | :---: |
| 16 | **`17.85% ± 0.58%`** | `9.96% ± 0.21%` | 0 | 1,024 |
| 32 | **`23.21% ± 0.30%`** | `13.69% ± 0.43%` | 0 | 2,048 |
| 64 | **`29.93% ± 0.44%`** | `16.68% ± 0.48%` | 0 | 4,096 |
| 128 | **`36.19% ± 0.35%`** | `22.53% ± 0.18%` | 0 | 8,192 |

**B. Equal stored bits per item.** Both are stored as dense bit vectors. **SimHash wins clearly**, because a 50%-dense bit carries more information than a 5%-sparse one:

| Tag size | FlyHash | **SimHash** | FlyHash weight RAM | SimHash weight RAM |
| :--- | :---: | :---: | :---: | :---: |
| 64 B (512 bits) | `21.67%` | **`40.64%`** | 0 KB | 128 KB |
| 128 B (1024 bits) | `27.50%` | **`50.51%`** | 0 KB | 256 KB |
| 256 B (2048 bits) | `34.02%` | **`59.80%`** | 0 KB | 512 KB |

FlyHash's advantage is that it needs no weight matrix and no multiplications, which matters on an MCU. If you have the RAM and an FPU, SimHash, PQ or HNSW give better recall per byte.

### 2. Gas classification: 8 sensors, 6 gases, 3 calibration shots per gas

300 test plumes per seed at `0.1×–25×` concentration, with sinusoidal + linear baseline drift in the drift regimes. **"Baseline-corrected L2 k-NN"** is the standard e-nose pipeline: subtract a moving average of the clean-air readings between plumes (the same information NeckSense's drift tracker gets), L2-normalise, then 3-NN.

| Regime | NeckSense (3-shot) | NeckSense (1-shot) | **Baseline-corrected L2 k-NN** | L2 k-NN (no drift correction) | Z-score centroid |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Clean 1× | `100%` | `100%` | `100%` | `100%` | `100%` |
| `0.1×–25×`, no drift | `100%` | `100%` | `100%` | `100%` | `89.9%` |
| Weak `0.1×–0.5×` + drift | `99.3% ± 0.7%` | `99.5% ± 0.6%` | **`100%`** | `33.4%` | `30.1%` |
| Full `0.1×–25×` + drift | `99.5% ± 0.5%` | `99.5% ± 0.5%` | **`100%`** | `76.5%` | `72.7%` |

What this shows:
- Drift correction plus ratio normalisation is what solves this task. Any pipeline with those two steps, NeckSense or the classical one, reaches ≥ 99%.
- NeckSense's version runs in integer C with one-shot learning.
- Without drift correction, k-NN collapses: dividing `0.2·gas + drift` by its norm points mostly along the drift.

### 3. Streaming one-shot anomaly detection (`D = 16`, 20 recurring normal modes at `0.5×–5×`, 5% novel chords)

| Detector | AUROC | State | Python time / sample |
| :--- | :---: | :---: | :---: |
| **NeckSense novelty** (2% sparse, 1024 cells) | **`0.9942 ± 0.0018`** | 4 KB (`Q16` weights) | ~100–130 us |
| L2 + nearest-neighbour memory, 512 slots | **`0.9993 ± 0.0002`** | 32 KB (`float32`) | ~11 us |
| L2 + nearest-neighbour memory, 64 slots | `0.9403 ± 0.0081` | 4 KB (`float32`) | ~10 us |
| L2 + online Mahalanobis | `0.9581 ± 0.0098` | 1 KB + `O(D²)` math | ~20 us |
| Online Mahalanobis (raw) | `0.8453 ± 0.0209` | | ~18 us |
| Rolling z-score (raw) | `0.6349 ± 0.0400` | | ~12 us |
| Half-space, 32 projections (raw) | `0.5902 ± 0.0346` | | ~36 us |

What this shows:
- **At equal memory** (4 KB), NeckSense beats a nearest-neighbour memory (0.994 vs 0.940).
- **Given 8× the memory** and floating point, plain nearest-neighbour is better (0.999).
- Normalisation matters most: every raw-input detector does poorly.
- The Python implementation is slower than the baselines; use the C core where speed matters.

---

## When to use NeckSense, and when not to

**Good fit:**
- **One-shot novelty or one-shot classification on a microcontroller.** Integer-only, no weight matrix, a few KB of state, and bit-identical Python for development.
- **Sensors with amplitude swings and slow drift.** `AntennalNormalizer` gives drift tracking and ratio normalisation in one integer-friendly block.
- **Sparse binary codes** that can be ANDed, popcounted and updated online.

**Use something else when:**
- **You have a server with RAM and an FPU.** SimHash, PQ or HNSW (`faiss`, `hnswlib`) beat FlyHash on recall per byte, and a plain normalised nearest-neighbour memory beats the novelty detector if you can afford the memory.
- **You already run baseline correction plus normalisation** feeding k-NN. On our gas benchmark it is as accurate (100% vs 99.5%).
- **You have thousands of labelled examples.** Gradient-boosted trees or a trained network will outperform one-shot associative rules.
- **Faults are single-channel amplitude spikes.** A per-channel z-score is simpler and faster.

---

## Repository layout

| Path | Contents |
| :--- | :--- |
| [`necksense/vibration.py`](necksense/vibration.py) | `VibrationFrontEnd` (`VibrationAnalyzer`): 16-ch zero-float chordotonal spectrum & envelope extractor |
| [`necksense/antennal.py`](necksense/antennal.py) | `AntennalNormalizer`: drift tracking, divisive normalisation, plume whiff intermittency, `Q16.16` parity |
| [`necksense/kenyon.py`](necksense/kenyon.py) | `KenyonHasher` (FlyHash) and `KenyonIndex` (popcount search, `.npz` save/load) |
| [`necksense/mushroom.py`](necksense/mushroom.py) | `MushroomNoveltyDetector` and 2-compartment `MushroomValenceClassifier` (`KCγ` STM + `KCα/β` LTM) |
| [`necksense/cli.py`](necksense/cli.py) | `necksense demo`, `benchmark-quick`, `hash-csv` |
| [`csrc/`](csrc/) | ANSI C99 core, `Makefile` (`default`, `NS_TINY=1`, `ASan+UBSan`), 8 self-tests & `--json-parity` |
| [`arduino/NeckSense/`](arduino/NeckSense/) | Drop-in Arduino / PlatformIO library + `ESP32_Vibration_And_Gas_Anomaly.ino` |
| [`scripts/benchmark_necksense.py`](scripts/benchmark_necksense.py) | Reproducible benchmark and chart generator |
| [`tests/`](tests/) | 24 pytest items, including README execution, zero-heap/zero-float C/Arduino audit & live WSL bit parity |

---

## Changelog

**3.0.0**
- *Connectome-derived projection:* `KenyonHasher`, `KenyonIndex`, `MushroomNoveltyDetector` and `MushroomValenceClassifier` use `BANC v888` `ALPN → KC` claws, `APL` feedback and `KC → MBON` weights by default (`connectome_weights="banc_v888"`, `n_kc <= 1024`), in Python and C99 (`necksense_banc_weights.h`). With `n_kc > 1024`, or `connectome_weights="v2_analytic"`, the `xorshift32` projection is used.
- *Audited benchmark:* `K = 100` Maslov–Sneppen shuffle comparison on UCI gas drift and CWRU bearing data (see above). Shuffled wiring performs the same; classical baselines win.

**2.0.0**
- *C99 SRAM reduction (`6.9×–24×`) & `-DNS_TINY=1` MCU profile:* lossless 17-bit `ns_novelty_t` (`uint16_t` + full-weight bitmask) and `Q14` `int16_t` + consolidated `ltm_ab` bitmask in `ns_valence_t` shrink the default 5-struct suite from `139,960 B` (`~140 KB`) to `20,284 B`, and to **`5,804 B` (`< 6 KB`)** under `-DNS_TINY=1` while keeping `v1.1.0` `Q16.16` golden-vector bit parity.
- *Zero-float vibration & acoustic front-end:* added `VibrationFrontEnd` (`VibrationAnalyzer`) and C99 `ns_vib_fe_t` (`ns_vib_extract_q16`, `ns_vib_extract_i16`, `ns_vib_extract_3axis_i16`) extracting 16 homogeneous degree-1 stationary Haar, quadrature resonator, impulse crest and envelope modulation channels (`10×+` load-invariant 1-shot bearing/motor fault ID; strips static `1g` tilt on 3-axis accelerometers).
- *Plume whiff intermittency:* added phasic ORN up-crossing tracking (`step_with_whiff()`, `diagnostics()`, `ns_antennal_step_whiff`) distinguishing pulsatile turbulent plumes from static background clouds.
- *Two-compartment `KCγ` + `KCα/β` anti-forgetting memory:* added `MushroomValenceClassifier.consolidate()` and `ns_valence_consolidate()` protecting consolidated synapses from sequential cross-class opponent erosion (`0%` catastrophic forgetting across sequential tasks) while preserving active opposite-sign extinction.
- *Arduino library:* added drop-in `arduino/NeckSense` package (`library.properties`, `NeckSense.h`, byte-mirrored C99 core and `ESP32_Vibration_And_Gas_Anomaly.ino`).

**1.1.0**
- *Improvement:* `MushroomNoveltyDetector` now defaults to 2% sparse codes (`wta_frac=0.02`). Streaming anomaly AUROC rose from `0.9658` to `0.9942` on the benchmark seeds and from `0.961` to `0.997` on held-out seeds. For the C core, pass `k = 20` (of 1024) to `ns_kenyon_init` for novelty.
- *Benchmark:* added baseline-corrected k-NN, nearest-neighbour memories at equal and 8× memory, and L2 + Mahalanobis. 1.0.0 compared only against baselines without drift correction or normalisation, which overstated the gap: "99.5% where k-NN drops to 33%", and "0.966 vs 0.845 AUROC".
- *Docs & packaging:* published `necksense` `1.1.0` to PyPI (`pip install necksense`). The C memory table now includes the classifier struct (131 KB by default). Every Python example runs as written.

---

## Scientific foundations

1. Olsen, S. R. & Wilson, R. I. (2008). Lateral presynaptic inhibition mediates gain control in an olfactory circuit. *Nature*, 452, 956–960.
2. Nagel, K. I. & Wilson, R. I. (2011). Biophysical mechanisms underlying olfactory receptor neuron dynamics. *Nature Neuroscience*, 14, 208–216.
3. Dasgupta, S., Stevens, C. F. & Navlakha, S. (2017). A neural algorithm for a fundamental computing problem. *Science*, 358(6364), 793–796.
4. Hattori, D. et al. (2017). Representations of novelty and familiarity in a mushroom body compartment. *Cell*, 169(5), 956–969.
5. Aso, Y. et al. (2014). Mushroom body output neurons encode valence and guide memory-based action selection in *Drosophila*. *eLife*, 3, e04580.

---

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Amandeep Singh Bains.
