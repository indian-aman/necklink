<div align="center">

<a href="https://github.com/indian-aman/necklink"><img src="https://raw.githubusercontent.com/indian-aman/necklink/main/assets/logo/necklink-mark.svg" alt="NeckLink" width="52"></a>

# NeckLink CNS (`necklink-cns` v3.0.0, research beta)

**A 4-organ reflex controller whose descending decision layer runs on weight matrices compiled from the adult *Drosophila* `BANC v888` connectome — fusing `NeckSight`, `NeckCompass`, `NeckSense`, and `NeckCord` with `MAVLink v2` and `ROS 2` bridges in `24,024 B` of zero-heap, zero-float ANSI C99 (`107.5 KB` standalone WebAssembly) and Python.**

[![PyPI](https://img.shields.io/pypi/v/necklink-cns?style=flat-square&logo=pypi&logoColor=white&label=PyPI%20necklink--cns&color=6366F1&cacheSeconds=180&v=300)](https://pypi.org/project/necklink-cns/)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://pypi.org/project/necklink-cns/)
[![ANSI C99](https://img.shields.io/badge/C99-zero--heap%20%7C%20zero--float%20%7C%2023.5%20KB-00599C?style=flat-square&logo=c&logoColor=white)](csrc/)
[![WebAssembly](https://img.shields.io/badge/WASM-107.5%20KB%20standalone-654FF0?style=flat-square&logo=webassembly&logoColor=white)](https://indian-aman.github.io/necklink/#simulator)
[![Status](https://img.shields.io/badge/status-research%20beta-F59E0B?style=flat-square)](#v300-results)
[![License: MIT](https://img.shields.io/badge/license-MIT-F59E0B?style=flat-square)](LICENSE)

[**Live WASM Demo**](https://indian-aman.github.io/necklink/#simulator) ·
[**Quick start**](#quick-start) ·
[**Autopilot bridges**](#mavlink-v2--ros-2-autopilot-bridges) ·
[**C99, ESP32 & WASM**](#zero-heap-zero-float-ansi-c99-esp32--wasm) ·
[**v3.0.0 results**](#v300-results) ·
[**When to use it**](#when-to-use-necklink-cns-and-when-not-to)

</div>

> [!NOTE]
> **v3.0.0 in one paragraph.** The connectome now supplies real weight matrices — `BANC v888` brain populations → descending neurons (`10×11`) → leg motor pools (`6×10`) here, plus the compass, vision, memory and gait circuits in the four organ packages (about `33,000` numbers in total, from `23.55M` synapses). The dynamics around those matrices are hand-designed. In closed-loop simulation the real wiring clearly beats degree-preserving shuffled wiring, but the earlier hand-tuned symmetric model (`connectome_weights="v2_analytic"`) is still more accurate. Nothing has been validated on real hardware yet. Details: [v3.0.0 results](#v300-results).

---

## What it is

In the adult *Drosophila* Brain-and-Nerve-Cord connectome (`BANC v888`: `157,789` proofread neurons, `23,553,108` synapses), brain signals reach the body through **`1,316` Descending Neurons (`DNs`)** that drive **`805` Ventral Nerve Cord motor neurons**.

`necklink-cns` combines one library per subsystem into a single closed-loop controller:

1. **Optic lobe (`NeckSight`)**: retina/lamina preprocessing, `T4/T5` motion detectors, 3-DOF gyro derotation, and `64` `LPLC2` + `32` `LC4` looming detectors whose receptive fields are compiled from `BANC v888`.
2. **Central complex (`NeckCompass`)**: 9-axis tilt-compensated heading, an `EPG ↔ Delta7` ring attractor and `PFL3/PFL2` homing steering on `BANC v888`-compiled matrices, plus `CPU4`-style path integration.
3. **Mushroom body (`NeckSense`)**: vibration front-end, antennal normalisation, and a Kenyon-cell projection using real `BANC v888` claws (`1,024` cells), with novelty and 2-compartment memory.
4. **Descending decision layer (`ConnectomeArbitrator`)**: `11` brain populations (`LPLC2`, `LC4`, `LPLC1`, `PFL3`, `PFL2`, `MBON`) → `10` descending neurons (`DNp01`, `MDN`, `DNa02`, …) → `6` leg motor pools, by matrix multiplication through `W_brain_dn` and `W_dn_leg`. Turning direction is read from the motor-pool output, not hard-coded.
5. **Engineering layers (`PROVENANCE = "engineering"`)**: box-constrained thruster/rotor allocator, sensorless ESC fault observer, 18-servo hexapod IK, 4-byte CRC-4 command frame, **MAVLink v2** (`0xFD`, X.25 CRC-16 + `CRC_EXTRA`) codec and a **ROS 2 (`rclpy`)** adapter.

**Hand-designed, not from the connectome:** how sensor outputs drive the brain populations, the rate dynamics and thresholds, an expansive `DNa02` response `r·(0.5 + 1.5·r)` (motivated by Westeinde et al., *Nature* 2024), and forward-walking inhibition of `MDN`. These apply identically to real and shuffled wiring.

---

## Quick start

```bash
pip install --upgrade necklink-cns
necklink-cns --version
necklink-cns demo
necklink-cns priors
```

```python
import numpy as np
from necklink_cns import NeckLinkCNS

cns = NeckLinkCNS(vehicle="hexacopter", grid_size=32)   # connectome_weights="banc_v888" by default
frame = np.full((32, 32), 160.0)
out = cns.step(
    dt=0.02,
    speed_m_s=1.2,
    gyro_xyz=np.array([0.02, -0.01, 0.05]),
    accel_xyz=np.array([0.3, -0.2, 9.80]),
    mag_xyz=np.array([0.45, 0.05, 0.89]),
    camera_frame=frame,
)
assert len(out.cervical_wire_4b) == 4
assert out.mavlink_manual_pkt[0] == 0xFD

# The earlier hand-tuned symmetric model (more accurate in every v3 benchmark):
analytic = NeckLinkCNS(vehicle="usv_t200", grid_size=32, connectome_weights="v2_analytic")
```

The extraction and shuffling tools (`necklink_cns.connectome.extract_raw_subgraphs`, `shuffle_raw_subgraphs`, …) need SciPy/pandas and the raw `BANC v888` dataset: `pip install "necklink-cns[connectome]"`.

---

## MAVLink v2 & ROS 2 autopilot bridges

```python
import numpy as np
from necklink_cns import (
    MavLink2Codec,
    MavManualControl,
    NeckLinkCNS,
    NeckLinkROS2Adapter,
)

# 1. MAVLink v2 framing (ArduPilot / PX4 companion UART or UDP)
codec = MavLink2Codec(sysid=42, compid=191)
pkt = codec.encode_manual_control(MavManualControl(x=650, y=0, z=500, r=-250, buttons=0, target=1))
decoded = codec.feed(pkt)
assert len(decoded) == 1 and MavManualControl.from_payload(decoded[0].payload).r == -250

# 2. ROS 2 (rclpy-ready) sensor/cmd_vel adapter
adapter = NeckLinkROS2Adapter(NeckLinkCNS(vehicle="usv_t200", grid_size=32))
adapter.on_imu(np.array([0.01, -0.02, 0.05]), np.array([0.2, -0.1, 9.8]))
adapter.on_mag(np.array([0.45, 0.05, 0.88]))
step_out = adapter.step_camera(np.full((32, 32), 140.0), speed_m_s=1.0, dt=0.02)
assert isinstance(step_out["twist"].angular_z, float)
```

---

## Zero-heap, zero-float ANSI C99, ESP32 & WASM

[`csrc/necklink_cns.h`](csrc/necklink_cns.h), [`csrc/necklink_cns.c`](csrc/necklink_cns.c), and [`csrc/nl_mavlink.h`](csrc/nl_mavlink.h) combine all four organs, the `BANC v888` weight tables, and the MAVLink v2 codec into one static C struct (`nl_cns_t`) with **no `float`/`double`** and **no `malloc`/`free`**:

```bash
make -C csrc clean all test sanitize   # 7 C99 self-tests under -Werror + ASan/UBSan
```

| Target | Measured | Notes |
| :--- | :--- | :--- |
| ANSI C99 `nl_cns_t` | `24,024 B` (x86-64) / `23,968 B` (32-bit) state, `0 B` `.data`/`.bss` | Weight tables are `const` (flash) |
| x86-64 `-O2` | `~166 µs` per full tick | Desktop self-test timing; **not measured on a microcontroller** |
| ESP32 (`esp32dev`, PlatformIO, Xtensa toolchain) | `47,604 B` RAM (`14.5%`), `515,496 B` flash (`39.3%`) for the example sketch | **Compile-verified only** — not yet run on a board |
| Standalone WebAssembly | `110,129 B`, `0` imports | Runs in the [live browser demo](https://indian-aman.github.io/necklink/#simulator) |

[`arduino/NeckLinkCNS/`](arduino/NeckLinkCNS/) contains the Arduino library and an [`ESP32_MAVLink_Survival_Copilot.ino`](arduino/NeckLinkCNS/examples/ESP32_MAVLink_Survival_Copilot/ESP32_MAVLink_Survival_Copilot.ino) example sketch. Use `nl_cns_set_dn_weights(&cns, NULL)` to switch the C core to the analytic model.

---

<a id="v300-results"></a>

## v3.0.0 results

### Closed-loop whole-controller test (simulation)

A 3-DOF boat simulation (`dt = 0.05 s`, `45 s`): drive out `9 s`, then return home on the controller alone, past `2` buoys and `2` thin pilings with real collision checks, through a localised `+125°` magnetic anomaly, with one thruster losing `78%` of its thrust at `t = 11 s`, under wave roll/pitch. Camera frames are rendered from the live boat pose. `N = 30` seeds. "Safe arrival" = within `2.5 m` of home with zero collisions.

| Controller | Safe arrival | Collisions per mission | Closest approach to home |
| :--- | :---: | :---: | :---: |
| `necklink-cns`, `banc_v888` (default) | `66.7%` (`20/30`) | `0.70` | `2.83 m` |
| `necklink-cns`, `v2_analytic` | **`80.0%`** (`24/30`) | **`0.30`** | **`2.16 m`** |
| Classical: χ²-gated EKF + dead reckoning + gyro-derotated Farneback avoidance + health-weighted pseudo-inverse | `0.0%` | `1.53` | `8.16 m` |

**Shuffled-wiring test:** the whole controller with all four organs' raw matrices rewired by degree-preserving (Maslov–Sneppen) swaps, `K = 100` shuffles, `12` seeds each: safe arrival `58.3%` (real, same 12 seeds) vs `4.6%` (shuffled mean), `2/100` shuffles at least as good, `p = 0.030`, `z = +4.24`.

What this shows:
- **The real wiring matters for this controller:** scrambling it while keeping every neuron's degree and synapse weights collapses performance.
- **It is not the best controller:** the hand-tuned symmetric model does better.
- **The classical baseline is not state of the art.** Its allocator has no active-set redistribution and no reverse-escape manoeuvre, which partly explains `0%`.
- **Caveats:** simulation only; `p = 0.030` misses the pre-registered `0.01` threshold (12 seeds per shuffle); the pipeline (for example the `LPLC1` drive, `MDN` inhibition and `DNa02` response) was tuned while looking at real-wiring results, which favours the real wiring over the shuffles.

Per-organ results are in each package README ([NeckCompass](https://github.com/indian-aman/necklink/tree/main/packages/neckcompass#v300-connectome-derived-weights-audited-results), [NeckSight](https://github.com/indian-aman/necklink/tree/main/packages/necksight#v300-connectome-derived-receptive-fields-audited-results), [NeckSense](https://github.com/indian-aman/necklink/tree/main/packages/necksense#v300-connectome-derived-projection-audited-results), [NeckCord](https://github.com/indian-aman/necklink/tree/main/packages/neckcord#v300-connectome-derived-gait-coupling-audited-results)); the full table, code and raw JSON are in [`benchmarks/v3/`](https://github.com/indian-aman/necklink/tree/main/benchmarks/v3).

<details>
<summary>Earlier correction (v2.0.1): withdrawn v2.0.0 claims</summary>

The `v2.0.0` "250-episode closed-loop survival gauntlet" (`100%` vs `77.2%` vs `1.2%` vs `0%`) never ran this library — each controller was hand-scripted inside the benchmark — and was withdrawn, together with most other `v2.0.0` benchmark advantages (unfair baselines, train/test leakage, a mislabelled connectome "specificity" test that used random node sets rather than a degree-preserving null). See the [root README](https://github.com/indian-aman/necklink#earlier-corrections-v201) for the full list. The v3.0.0 benchmarks above were written from scratch to run the library itself, with identical inputs for every method.

</details>

---

## When to use NeckLink CNS, and when not to

**Reasonable to try:**
- **Research on connectome-constrained control:** compile real `BANC v888` circuits into a running controller and test them against shuffled wiring in your own simulations.
- **Experimenting with a small (`< 24 KB`) reflex layer** beneath ArduPilot, PX4, or ROS 2 — in simulation or on the bench first. For best behaviour use `connectome_weights="v2_analytic"`.

**Do not use it yet for:**
- **Anything safety-critical.** The controller has not been run on real hardware.
- **Metric 3D mapping, object recognition, or path planning.** This is a reflex controller, not a VIO/SLAM or perception stack.
- **Untextured environments** (blank walls): pair it with a rangefinder.

---

## Changelog

**3.0.0**
- *Connectome-driven decision layer:* `ConnectomeArbitrator` replaces the v2 hand-coded priority table with `BANC v888` `W_brain_dn` (`10×11`) and `W_dn_leg` (`6×10`) matrices in Python and C99. `connectome_weights="v2_analytic"` (C: `nl_cns_set_dn_weights(&cns, NULL)`) keeps the earlier model.
- *All four organs* use their v3 connectome-derived circuits by default.
- *Fix (found by the independent audit):* the C99/WASM core now initialises its ring compass with the `BANC v888` weights (it previously ran the analytic ring while Python ran `BANC v888`).
- *Fix:* WASM singletons are compiled only for WebAssembly builds (`0 B` `.bss` on MCUs).
- *Fix:* importing `necklink_cns` no longer requires SciPy/pandas; the extraction tools moved to the `[connectome]` extra.
- *Provenance:* `NeckLinkCNS`/`ConnectomeArbitrator` carry `PROVENANCE = "connectome"`; MAVLink, ROS 2 and cervical-link classes carry `PROVENANCE = "engineering"`.

---

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Amandeep Singh Bains.
