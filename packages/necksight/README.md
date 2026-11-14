<div align="center">

<a href="https://github.com/indian-aman/necklink"><img src="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/logo/necklink-mark.svg" alt="NeckLink" width="52"></a>

# NeckSight (`necksight` v3.0.0, research beta)

**A collision and thin-obstacle (pole/wire) avoidance reflex for moving cameras, modelled on the *Drosophila* optic lobe (`T4/T5` motion detectors + `LPLC2` radial & `LC4` bar looming neurons + `DNa02` directional dodge), in Python (NumPy), zero-heap/zero-float ANSI C99 (`14.3 KB` state), and Arduino / ESP32-CAM.**

[![PyPI](https://img.shields.io/pypi/v/necksight?style=flat-square&logo=pypi&logoColor=white&label=PyPI%20necksight&color=3775A9&cacheSeconds=180&v=200)](https://pypi.org/project/necksight/)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://pypi.org/project/necksight/)
[![ANSI C99](https://img.shields.io/badge/C99-zero--heap%20%7C%20zero--float%20%7C%2014.3%20KB-00599C?style=flat-square&logo=c&logoColor=white)](csrc/)
[![Arduino / ESP32](https://img.shields.io/badge/Arduino-ESP32--CAM%20ready-00979D?style=flat-square&logo=arduino&logoColor=white)](arduino/NeckSight/)
[![Tests](https://img.shields.io/badge/tests-32%20pytest%20%2B%20C99%20self--test-10B981?style=flat-square)](tests/test_necksight.py)
[![License: MIT](https://img.shields.io/badge/license-MIT-F59E0B?style=flat-square)](LICENSE)

[**Quick start**](#quick-start) ·
[**Turning platforms**](#turning-platforms-feed-the-gyro) ·
[**C99 & Arduino**](#zero-heap-zero-float-ansi-c99-core-csrc--arduino) ·
[**v3.0.0 results**](#v300-connectome-derived-receptive-fields-audited-results) ·
[**Benchmarks**](#benchmarks-analytic-detectors-pre-v3) ·
[**When to use it**](#when-to-use-necksight-and-when-not-to) ·
[**Changelog**](#changelog)

</div>

> [!NOTE]
> **v3.0.0: the looming detectors now use receptive fields compiled from the `BANC v888` connectome** — `64` of the `181` right-eye `LPLC2` neurons and `32` of the `114` `LC4` neurons, with their `T4/T5` inputs and lateral inhibition (about `18,700` numbers). Retinal positions are *inferred* from connectivity, not measured. The retina, `T4/T5` motion detectors and thresholds are hand-designed. `connectome_weights="banc_v888"` is the default; `connectome_weights="v2_analytic"` gives the earlier uniform detector grid, which scores higher. See [v3.0.0 results](#v300-connectome-derived-receptive-fields-audited-results). The older benchmarks further down were measured with the analytic detectors before v3 and have not been re-run.

<p align="center">
  <img src="https://raw.githubusercontent.com/indian-aman/necklink/main/packages/necksight/assets/necksight_benchmarks.png" alt="NeckSight benchmarks" width="100%">
</p>

---

## What it is

Cheap collision triggers on moving robots fail in two ways:
- **Brightness changes:** frame differencing fires on every sunlight step.
- **Camera rotation:** optical-flow divergence fires on every fast camera turn.

NeckSight copies how the fly separates *something is coming at me* from *I am turning*:

1. **`RetinaLaminaPreproc`.** Downsamples any `H×W` (or RGB) frame to a 32×32 or 64×64 grid. It applies log photoreceptor adaptation, cancels global brightness steps and AEC/AGC clipping oscillations, and splits the signal into ON (brightening) and OFF (darkening) channels.
2. **`T4T5MotionEngine`.** Four-direction elementary motion detectors (correlation with null-direction shunting inhibition) on the ON and OFF channels with connectome-grounded `T5:T4` OFF/ON weighting (`t5_off_gain=1.35`, motivated by `BANC v888` `6,586` `T5→LPLC2` vs `3,330` `T4→LPLC2` synapses), plus wide-field flow summaries (`vx`, `vy`, yaw, divergence).
3. **`LPLC2LoomingReflex` (`LPLC2` + `LC4` + `DNa02`).** Combines the 4-arm radial gate `(Q_L·Q_R·Q_U·Q_D)^¼` (`LPLC2`, `2,896` DN synapses in `BANC v888`) with a split-strip 2-arm opponent bar/wire channel (`LC4`/`LPLC1`, `2,053` DN synapses, `lc4_weight=0.42`) and `Li` lateral surround inhibition so thin vertical poles and horizontal wires trigger reliably while translating bars, fast pans, and flicker stay silent. Outputs `DNp01` brake (`triggered`), `focus_xy`, rough `est_ttc_s`, and `DNa02` contralateral avoidance steering `(avoid_x, avoid_y)`.
4. **Full 3-DOF `(yaw, pitch, roll)` gyro derotation (upgraded in 2.0.0).** Feed 3-axis IMU angular rates (`imu_to_ego_motion` in Python, `necksight_derotate_3dof` with integer BAM sin/cos in C99) to cancel yaw, pitch, and camera roll around grid center before stepping.

```mermaid
flowchart LR
    A["Camera frame"] --> B["Retina / lamina: log adapt, glare + AEC guard, ON / OFF"]
    G["3-axis IMU gyro (optional)"] -->|"3-DOF derotate"| B
    B --> C["T4 / T5 EMDs (T5 OFF gain)"]
    C --> D["Wide-field flow (pan)"]
    C --> E["LPLC2 4-arm radial + LC4 bar/wire gates"]
    D -->|"pan suppression"| E
    E --> F["DNp01 trigger + DNa02 avoid_xy + TTC"]
```

---

## Quick start

```bash
pip install necksight
# or from this repository:
pip install "git+https://github.com/indian-aman/necklink.git#subdirectory=packages/necksight"
```

```python
import numpy as np
from necksight import NeckSightPipeline, imu_to_ego_motion

pipe = NeckSightPipeline(grid_size=64, threshold=0.045)

# Synthetic stand-in for a camera: textured background with an off-centre dark disc approaching.
rng = np.random.default_rng(0)
background = rng.uniform(60, 200, size=(240, 320)).astype(np.float32)
yy, xx = np.mgrid[0:240, 0:320]
for t in range(36):
    radius = 10.0 / max((36 - t) / 60.0, 0.025)           # hyperbolic growth toward impact
    frame = np.where(np.hypot(xx - 120, yy - 120) < radius, 15.0, background)

    # From your 6-axis IMU: (yaw, pitch, roll) angular rates in rad/s.
    ego_3dof = imu_to_ego_motion(yaw_rate_rad_s=0.0, pitch_rate_rad_s=0.0,
                                 roll_rate_rad_s=0.0, dt=1 / 60, hfov_deg=70.0, grid_width=64)
    trig = pipe.step(frame, dt=1 / 60, ego_shift_px=ego_3dof)
    if trig.triggered:
        print(f"collision warning at frame {t}: score={trig.looming_score:.3f}, "
              f"ttc~{trig.est_ttc_s:.2f}s, focus={tuple(round(v, 1) for v in trig.focus_xy)}, "
              f"dodge=({trig.avoid_x:+.2f}, {trig.avoid_y:+.2f})")
        break
```

Stage by stage, with an out-of-line reflex message (for example `pynltp`'s `send_reflex`):

```python
import numpy as np
from necksight import RetinaLaminaPreproc, T4T5MotionEngine, LPLC2LoomingReflex, attach_nltp_reflex

retina = RetinaLaminaPreproc(grid_size=64)
emd = T4T5MotionEngine(grid_size=64)
lplc2 = LPLC2LoomingReflex(grid_size=64)

class PrintClient:  # stands in for a pynltp client: anything with .send_reflex(msg)
    def send_reflex(self, msg):
        print("reflex sent:", msg)

attach_nltp_reflex(PrintClient(), msg="LOOM_ESTOP", detector=lplc2)

frame = np.random.default_rng(1).uniform(0, 255, size=(120, 160))
lamina_out = retina.step(frame, dt=1 / 60)
motion_out = emd.step(lamina_out)
vx, vy, yaw_pan, radial_div = motion_out.lptc   # wide-field flow summary
trigger = lplc2.step(motion_out, dt=1 / 60)
```

```bash
necksight demo                     # grating, fast yaw pan, 100x glare, looming
necksight benchmark-quick --json
```

### Turning platforms: feed the gyro

Without rotation information, NeckSight avoids false alarms during turns by suppressing looming responses while the whole image is moving. That also suppresses **real** obstacles. On our yaw-oscillation benchmark (below), plain NeckSight detects:
- **81%** of approaching obstacles at 3 px/frame peak yaw;
- **56%** at 4 px/frame.

Passing `ego_shift_px` from a gyro (`gyro_to_shift_px(yaw_rate, pitch_rate, dt, hfov_deg, grid_width)`) shifts the internal state to cancel the rotation. Detection is then **100%** at every yaw rate tested, still with no false alarms. If your platform turns quickly, feed the gyro.

Don't try to estimate the shift from the images instead (for example by phase correlation). We tried it: the approaching obstacle itself biases the estimate.

---

## Zero-heap, zero-float ANSI C99 core (`csrc/`) & Arduino

[`csrc/necksight.h`](csrc/necksight.h) and [`csrc/necksight.c`](csrc/necksight.c) (mirrored byte-for-byte into [`arduino/NeckSight/src/`](arduino/NeckSight/src/)) run the 32×32 pipeline in integers only:
- a `Q8` log lookup and an 8×8 lattice-median glare + AEC/AGC clipping filter;
- an integer summed-area table, integer square roots, and a 65-entry `Q14` BAM sin/cos table for 3-DOF roll derotation;
- no `malloc` and no floating point;
- `sizeof(necksight_state_t) = 14,624 B`, checked at compile time to stay under 16 KB.

`necksight_step_frame_u8()` box-downsamples a QVGA or VGA `uint8_t` camera buffer in the same call. Drop [`arduino/NeckSight`](arduino/NeckSight) into `~/Arduino/libraries/` (or `lib_extra_dirs` in PlatformIO) and open [`ESP32CAM_Looming_Avoidance.ino`](arduino/NeckSight/examples/ESP32CAM_Looming_Avoidance/ESP32CAM_Looming_Avoidance.ino).

```c
#include "necksight.h"

static necksight_state_t optic_lobe;

/* 90 deg lens on the 32-px grid: focal = 16 / tan(45 deg) = 16 px = 256 in Q4 */
#define FOCAL_PX_Q4 256U

void setup_vision(void) {
    necksight_init(&optic_lobe);
}

/* Returns 1 when a looming obstacle/pole/wire is detected; angular rates in mrad/s. */
int on_camera_frame(const uint8_t *fb, uint16_t h, uint16_t w,
                    int32_t yaw_mrad_s, int32_t pitch_mrad_s, int32_t roll_mrad_s,
                    uint16_t dt_ms, int8_t *avoid_x_q7, int8_t *avoid_y_q7) {
    necksight_result_t res;
    necksight_derotate_3dof(
        &optic_lobe,
        necksight_gyro_to_shift_q4(yaw_mrad_s, dt_ms, FOCAL_PX_Q4),
        (int16_t)(-necksight_gyro_to_shift_q4(pitch_mrad_s, dt_ms, FOCAL_PX_Q4)),
        necksight_gyro_to_bam(roll_mrad_s, dt_ms)
    );
    necksight_step_frame_u8(&optic_lobe, fb, h, w, w, dt_ms, &res);
    *avoid_x_q7 = res.avoid_x_q7;
    *avoid_y_q7 = res.avoid_y_q7;
    return res.triggered; /* also: res.lc4_score_q10, res.focus_x, res.est_ttc_ms */
}
```

```bash
make -C csrc clean sanitize test   # ASan+UBSan + -O2 -std=c99 -Wall -Wextra -pedantic -Werror
```

The self-test covers a translating grating, a fast pan, 20× brightness steps, dark and bright looming discs, a QVGA buffer, throughput, and test (g). In (g) an obstacle approaches during ±3 px/frame yaw: it is **missed without derotation and caught with it**, with no false triggers on the pan alone. Measured throughput is ~11,000–13,000 frames/s on a desktop x86-64 core at `-O2`. It has not been measured on an ESP32 or other MCU.

---

## v3.0.0: connectome-derived receptive fields (audited results)

```python
import numpy as np
from necksight import NeckSightPipeline

for weights in ("banc_v888", "v2_analytic"):  # banc_v888 is the default in 3.0.0
    pipe = NeckSightPipeline(grid_size=64, threshold=0.045, connectome_weights=weights)
    yy, xx = np.mgrid[0:64, 0:64]
    peak = 0.0
    for t in range(30):  # a dark disc expanding toward the camera
        frame = np.where(np.hypot(xx - 32, yy - 32) < 2 + t, 15.0, 180.0)
        peak = max(peak, pipe.step(frame, dt=1 / 60).looming_score)
    print(weights, "peak looming score:", round(peak, 3))
```

Benchmark: `56` episodes rendered in 3D MuJoCo (EGL, `64×64`, `38` frames each) — `28` approaching obstacles (boulder, thin pole, thin wire, low-contrast wall) and `28` hard negatives (receding object, lateral pass-by, strong wave rotation, hover jitter) — all with camera rotation. Every method gets the same frames, the same 3-DOF gyro derotation and the same warm-up. Compared against `K = 100` degree-preserving (Maslov–Sneppen) shuffles of the raw optic-lobe matrices compiled with the identical pipeline; one-sided permutation `p = (1 + r) / (K + 1)`. Code and raw JSON: [`benchmarks/v3/`](https://github.com/indian-aman/necklink/tree/main/benchmarks/v3).

| Detector (AUROC, episode peak score) | All obstacles | Thin poles + wires |
| :--- | :---: | :---: |
| `banc_v888`, LPLC2 + LC4 (default) | `0.806` | `0.755` |
| Shuffled wiring, LPLC2 + LC4 (mean of 100) | `0.763` (`r = 7`, `p = 0.079`) | `0.672` (`r = 3`, `p = 0.040`) |
| `banc_v888`, LPLC2 only | `0.624` | — |
| Shuffled wiring, LPLC2 only (mean of 100) | `0.729` (`r = 99`, `p = 0.99`) | — |
| **`v2_analytic`**, LPLC2 + LC4 | **`0.890`** | **`0.987`** |
| `v2_analytic`, LPLC2 only | `0.707` | — |
| Gyro-derotated Farneback + CLAHE | `0.818` | `0.635` |
| Gyro-derotated pyramidal Lucas–Kanade + CLAHE | `0.740` | `0.480` |

What this shows:
- **`LC4` is what makes thin obstacles detectable.** The 4-arm `LPLC2` gate alone does poorly; adding the 2-arm `LC4` bar channel lifts every variant.
- **The real wiring is not clearly better than shuffled wiring.** With `LC4` it trends better (`p = 0.040` on thin obstacles, `0.079` overall — neither passes the pre-registered `0.01`); the real `LPLC2` receptive fields alone are *worse* than shuffled ones.
- **The analytic model wins**, and derotated Farneback beats the connectome detectors on all obstacles (it loses on thin poles and wires).
- **Caveats:** synthetic scenes, `56` episodes; retinotopy is inferred; the pipeline was tuned while looking at real-wiring results.

---

## Benchmarks (analytic detectors, pre-v3)

```bash
python scripts/benchmark_necksight.py    # ~45 s; needs matplotlib, make and a C compiler
```

> [!NOTE]
> All benchmark sequences are **synthetic**: procedurally generated clutter, discs and glare (`build_benchmark_dataset()`), not camera footage. NeckSight's thresholds were developed on this generator. On four freshly seeded datasets from the same generator it still scored AUROC 1.0, but real footage may differ.

### 1. Looming vs. hard negatives (64×64, 60 Hz, 60 sequences)

24 approaching obstacles (dark, bright, textured, off-centre) against 36 hard negatives:
- 12 fast camera pans (1.8–4.2 px/frame);
- 12 glare steps (15–100×);
- 12 receding or crossing objects.

"FAR @ 95% TPR" uses, for each detector, the threshold that catches 95% of the obstacles.

| Detector | AUROC | FAR @ 95% TPR | Yaw/glare FAR @ 95% TPR | Mean warning before impact | Python FPS (64×64) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **NeckSight** | **`0.9988`** | **`0.0%`** | **`0.0%`** | **`403 ms`** | ~700 |
| Pyramidal LK (3 levels) + gain normalisation + ego-motion compensation | `0.8750` | `41.7%` | `62.5%` | `199 ms` | ~170–200 |
| Ungated EMD (NeckSight without the LPLC2 gate) | `0.6100` | `55.6%` | `50.0%` | `343 ms` | ~1,200 |
| Single-scale Lucas-Kanade divergence | `0.5220` | `55.6%` | `83.3%` | `186 ms` | ~700 |
| Frame-difference energy | `0.3530` | `66.7%` | `100%` | `215 ms` | ~41,000 |

At NeckSight's fixed default threshold (`0.045`), it caught 24/24 obstacles with 1 false alarm in 36 negatives (2.8%).

### 2. Looming during yaw oscillation (new in 1.1.0)

A camera oscillating in yaw (24-frame period), with an obstacle moving with the scene, `σ = 4` image noise, and 16 sequences per row. The figures are detection and false-alarm rates at the default threshold:

| Peak yaw rate | Detected, no gyro | **Detected, gyro derotation** | False alarms, pan only (both) |
| :---: | :---: | :---: | :---: |
| 0–2 px/frame | `100%` | **`100%`** | `0%` |
| 3 px/frame | `81%` | **`100%`** | `0%` |
| 4 px/frame | `56%` | **`100%`** | `0%` |

### 3. Where NeckSight loses: slow sub-pixel speed

| Task | Lucas-Kanade | NeckSight T4/T5 |
| :--- | :---: | :---: |
| Translational speed MAE, 0.05–0.35 px/frame, varying contrast | **`0.055 px/frame`** | `0.133 px/frame` |

Correlation-type motion detectors mix speed with contrast and spatial frequency. For metric odometry use Lucas-Kanade or a VIO stack.

---

## When to use NeckSight, and when not to

**Good fit:**
- **A cheap, CPU-only collision reflex** for drones, boats, rovers or mast cameras. Brightness steps and camera turns are the main false-alarm sources, and you can feed it a gyro.
- **Microcontrollers** with tens of KB of RAM: 14.3 KB of state, no heap and no floating point.
- **A fast safety layer** in front of heavier perception (stereo, LiDAR, a CNN).

**Use something else, or add something, when:**
- **The platform turns fast and you have no gyro.** Detection drops to 56–81% at 3–4 px/frame of yaw. Feed `ego_shift_px`.
- **You need metric velocity or odometry.** Lucas-Kanade is 2.4× more accurate at slow sub-pixel speeds.
- **The obstacle is static and the camera is too.** With no change in the image there is no signal.
- **The obstacle is sub-pixel fine at collision range.** While `v2.0.0`'s `LC4` 2-arm opponent channel detects expanding vertical poles/trees and horizontal beams/wires once they span ≥ 2 grid cells, hair-thin guy wires below the 32×32 / 64×64 grid resolution will not register.
- **You need accurate time-to-contact.** The TTC output is a rough estimate (in the quick-start example: ~0.22 s reported vs 0.32 s true).
- **It has not been validated on your camera.** All numbers here come from synthetic sequences. Collect your own footage before relying on it for safety.

---

## Repository layout

| Path | Contents |
| :--- | :--- |
| [`necksight/retina.py`](necksight/retina.py) | `RetinaLaminaPreproc` (log adaptation, glare + AEC/AGC clipping guard) |
| [`necksight/emd.py`](necksight/emd.py) | `T4T5MotionEngine` (`t5_off_gain`), `MotionOutput` |
| [`necksight/looming.py`](necksight/looming.py) | `LPLC2LoomingReflex` (`LPLC2` + `LC4` + `DNa02`), `NeckSightPipeline`, `GiantFiberTrigger`, `attach_nltp_reflex` |
| [`necksight/egomotion.py`](necksight/egomotion.py) | `imu_to_ego_motion`, `warp_grid_3dof`, `gyro_to_shift_px`, `shift_grid` |
| [`necksight/cli.py`](necksight/cli.py) | `necksight demo`, `necksight benchmark-quick` |
| [`csrc/`](csrc/) | Zero-heap, zero-float ANSI C99 core (`14,624 B` state) and self-test |
| [`arduino/NeckSight/`](arduino/NeckSight/) | Drop-in Arduino / ESP32-CAM library and `ESP32CAM_Looming_Avoidance` sketch |
| [`scripts/benchmark_necksight.py`](scripts/benchmark_necksight.py) | Benchmark and chart generator |
| [`tests/`](tests/) | 32 pytest tests (thin poles/wires, 3-DOF roll derotation, DNa02 dodge, C99/Arduino SHA-256 parity, README examples) |

---

## Changelog

**3.0.0**
- *Connectome-derived receptive fields:* `LPLC2LoomingReflex` / `NeckSightPipeline` use `64` `LPLC2` and `32` `LC4` receptive fields, `T4/T5` input fractions, `LPi` and `Li` inhibition compiled from `BANC v888` by default (`connectome_weights="banc_v888"`), in Python and C99 (`necksight_banc_weights.h`). The earlier uniform detector grid remains available as `connectome_weights="v2_analytic"`.
- *Audited benchmark:* 3D MuJoCo comparison against `K = 100` shuffled wirings and gyro-derotated Farneback / Lucas–Kanade baselines (see above).

**2.0.0**
- *Thin-obstacle looming (`LC4`/`LPLC1`) + `BANC v888` `T5/T4` weighting:* added split-strip 2-arm opponent expansion channel (`enable_lc4_bar=True`, `lc4_weight=0.42` / `108` in Q8 matching `2,053 / (2,896 + 2,053)` `BANC v888` DN synapses) with `Li` lateral surround inhibition to catch thin vertical poles and horizontal wires missed by the 4-arm `LPLC2` gate, plus `t5_off_gain=1.35` (`346` in Q8, grounded in `6,586` `T5→LPLC2` vs `3,330` `T4→LPLC2` synapses) while preserving `0%` pan/glare/flicker false alarms.
- *3-DOF `(yaw, pitch, roll)` derotation + AEC/AGC clipping guard:* added `imu_to_ego_motion`, `warp_grid_3dof`, and integer BAM `necksight_derotate_3dof` around `(15.5, 15.5)` (`sizeof(necksight_state_t) = 14,624 B < 16 KB`), plus photoreceptor saturation/recovery suppression during AEC exposure swings.
- *Directional evasive reflex (`DNa02`):* added contralateral dodge vectors (`avoid_x`, `avoid_y` in `[-1, +1]`; `avoid_x_q7`, `avoid_y_q7` in `[-127, +127]`) and `bar_score` / `lc4_score_q10`.
- *Arduino / ESP32-CAM library:* added drop-in [`arduino/NeckSight/`](arduino/NeckSight/) library with `NeckSight` C++ wrapper, byte-identical C99 mirror, and `ESP32CAM_Looming_Avoidance.ino`.

**1.1.0**
- *Safety fix:* gyro derotation (`ego_shift_px`, `gyro_to_shift_px`; C: `necksight_shift_state`, `necksight_gyro_to_shift_q4`). Before this, a fast-turning platform could miss up to half of approaching obstacles, because pan suppression hid them along with the false alarms. The 1.0.0 benchmark never tested a collision during a turn.
- *Benchmark:* added a pyramidal Lucas-Kanade baseline with gain normalisation and ego-motion compensation, and the yaw-oscillation scenario.
  - C99 metrics are now measured locally. The old script only ran the C binary via `wsl.exe` and otherwise fell back to hard-coded numbers.
  - Fixed a crash in the dataset generator for some seeds.
- *Docs:* the synthetic-data caveat, the default-threshold false-alarm rate (2.8%), and runnable examples.

---

## References

1. Maisak, M. S. et al. (2013). A directional tuning map of *Drosophila* elementary motion detectors. *Nature*, 500, 212–216.
2. Haag, J., Mishra, A. & Borst, A. (2017). A common directional tuning mechanism of *Drosophila* motion-sensing neurons in the ON and in the OFF pathway. *eLife*, 6, e29044.
3. Klapoetke, N. C. et al. (2017). Ultra-selective looming detection from radial motion opponency. *Nature*, 551, 237–241.
4. Ache, J. M. et al. (2019). Neural basis for looming size and velocity encoding in the *Drosophila* giant fiber escape pathway. *Current Biology*, 29(6), 1073–1081.

---

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Amandeep Singh Bains.
