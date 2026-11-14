# NeckLink v3.0.0 Audited Shuffled-Wiring Benchmark Suite

This directory contains the scripts, compact raw `BANC v888` neuron-to-neuron subgraphs, and full machine-readable output JSON backing the `v3.0.0` tables in the root [`README.md`](../../README.md#v300-what-the-connectome-does-and-what-we-measured) and per-package READMEs.

## Files

| Path | Description |
| :--- | :--- |
| `run_all_v3_benchmarks.py` | Master runner: generates `K` Maslov–Sneppen degree- and weight-multiset-preserving rewirings of the raw neuron-to-neuron matrices (`39` matrices across 4 organs), compiles each shuffle with `compile_circuits_from_subgraphs` (zero per-shuffle tuning), and runs all 5 organ benchmarks. |
| `bench_1_compass.py` | Central complex (`NeckCompass`): 6-DOF wave + `125°` hull magnetic spikes, 10 s sensor blackout drift, and closed-loop PFL3/PFL2 homing vs `v2_analytic`, χ²-gated EKF, ungated EKF, Madgwick, and gyro integration. |
| `bench_2_sight.py` | Optic lobe (`NeckSight`): 3D MuJoCo (EGL) collision + thin-pole/wire detection with identical 3-DOF gyro derotation across `v3_banc_real`, `v3_banc_real_lplc2_only`, `v2_analytic`, dense Farneback, and sparse Lucas–Kanade. |
| `bench_3_sense.py` | Mushroom body & chordotonal (`NeckSense`): real public UCI Gas Sensor Array Drift Dataset (Vergara et al., 2012; chronological batch splits) novelty & 5-shot continual learning, plus real CWRU bearing fault vibration data (cross-load split: train 0/1 HP, test 2/3 HP) vs One-Class SVM, Isolation Forest, Mahalanobis, RBF-SVM, and nearest centroid. |
| `bench_4_cord.py` | Ventral nerve cord (`NeckCord`): emergent tripod gait order (`R_tripod`), 3D MuJoCo hexapod turning & leg-amputation recovery, and 30-seed MuJoCo hexacopter rotor-loss allocation (`PROVENANCE = "engineering"`) vs `v2_analytic` and SciPy bounded QP. |
| `bench_5_whole_cns.py` | Unified controller (`NeckLinkCNS`): closed-loop USV boulder/pole/plume/wave/spike mission across `v3_banc_real`, `K` shuffled brains, `v2_analytic`, and a classical stack (ungated EKF + non-derotated flow + PID). |
| `datasets.py` | Deterministic download + SHA-256 caching helpers for public UCI Gas Drift and CWRU bearing `.mat` datasets. |
| `export_raw_subgraphs.py` | Optional regenerator for `artifacts/banc_v888_raw_subgraphs.pkl.gz` from the full `BANC v888` release (`connectome_compiled.npz` + `neurons_meta.parquet`), with round-trip verification against `packages/*/data/banc_*_v3.npz`. |
| `artifacts/banc_v888_raw_subgraphs.pkl.gz` | Pre-extracted raw neuron-to-neuron sparse matrices (`635 KB`, SHA-256 in `artifacts/raw_subgraphs_manifest.json`) so the full `K = 100` shuffle suite runs standalone without downloading the multi-GB connectome dump. |
| `artifacts/v3_audit_benchmarks.json` | Complete post-audit `K = 100, N = 30` output (generated on Linux x86-64 with 48 vCPUs + NVIDIA RTX PRO 6000 Blackwell EGL for MuJoCo rendering). |

## Reproducing

Install the workspace packages plus benchmark dependencies (`scipy`, `scikit-learn`, `opencv-python-headless`, `mujoco`):

```bash
pip install -e packages/necksight -e packages/neckcompass -e packages/neckcord \
            -e packages/necksense -e "packages/necklink_cns[connectome]" \
            scikit-learn opencv-python-headless mujoco
```

### Quick local smoke check (`K = 2` shuffles, `N = 6` seeds)

```bash
python benchmarks/v3/run_all_v3_benchmarks.py --k-shuffles 2 --n-seeds 6 --workers 2 \
    --out /tmp/v3_smoke.json
```

### Full `K = 100, N = 30` reproduction

On Linux with headless EGL (`MUJOCO_GL=egl` is set automatically on Linux):

```bash
python benchmarks/v3/run_all_v3_benchmarks.py --k-shuffles 100 --n-seeds 30 --workers 24 \
    --out benchmarks/v3/artifacts/v3_audit_benchmarks.json
```

## Methodological Notes & Caveats

1. **One-sided permutation p-values:** `p = (1 + r) / (K + 1)`, where `r` is the number of Maslov–Sneppen shuffles at least as good as `v3_banc_real`. With `K = 100`, the minimum attainable p-value is `1 / 101 ≈ 0.0099`.
2. **Post-hoc architecture caveat:** Circuit compilation rules and dynamics gains were designed while inspecting real `BANC v888` behaviour before freezing the pipeline for shuffling; shuffles receive zero re-tuning, which biases marginal effects in favour of real wiring.
3. **No multiple-comparison adjustment:** Because 12+ endpoints are reported across 5 organs, treat `p = 0.02–0.04` rows as exploratory/marginal rather than confirmatory.
4. **Accuracy vs wiring specificity:** Passing the shuffle gate means specific connectome topology matters inside these dynamics — **not** that `v3_banc_real` beats `v2_analytic` or tuned classical estimators. On every accuracy metric tested, either `v2_analytic` or a classical baseline achieves the best raw score.
5. **Simulation scope:** Only Benchmark 3 (UCI gas drift + CWRU bearing vibration) uses physical sensor recordings; Benchmarks 1, 2, 4, and 5 are numerical / MuJoCo 3D simulations, not closed-loop physical robot trials.
