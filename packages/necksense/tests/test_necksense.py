"""Comprehensive pytest suite for NeckSense (`necksense` v2.0.0)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

import necksense
from necksense import (
    NS_VIB_CHANNELS,
    Q16_ONE,
    VIBRATION_CHANNEL_NAMES,
    AntennalNormalizer,
    KenyonHasher,
    KenyonIndex,
    MushroomNoveltyDetector,
    MushroomValenceClassifier,
    VibrationAnalyzer,
    VibrationFrontEnd,
    float_to_q16,
    popcount_overlap,
    popcount_u64,
    q16_to_float,
)
from necksense.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parent.parent


def _strip_c_comments(source: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", no_block)


def test_antennal_100x_concentration_invariance_and_noise_squelch() -> None:
    norm = AntennalNormalizer(8, sigma=0.005, gamma=1.0, hill_n=2.0)
    odor = np.array([0.85, 0.30, 0.08, 0.00, 0.55, 0.14, 0.02, 0.42], dtype=np.float64)

    r_base = norm.normalize(0.5 * odor)
    r_100x = norm.normalize(50.0 * odor)

    cos_sim = float(np.dot(r_base, r_100x) / (np.linalg.norm(r_base) * np.linalg.norm(r_100x)))
    assert cos_sim > 0.99999
    assert float(np.max(np.abs(r_base - r_100x))) < 5e-4

    # Electronic dark noise well below sigma must be squelched near zero (unlike L2 norm)
    dark_noise = np.array([1e-4, -2e-4, 5e-5, 0.0, 8e-5, -1e-4, 3e-5, 2e-5])
    r_noise = norm.normalize(dark_noise)
    assert float(np.max(r_noise)) < 1e-3


def test_antennal_dual_timescale_drift_removal_and_plume_recovery() -> None:
    rng = np.random.default_rng(101)
    norm = AntennalNormalizer(
        8,
        sigma=0.01,
        gamma=1.0,
        hill_n=2.0,
        tonic_alpha=0.03,
        tonic_slew_limit=0.006,
        phasic_gain=0.35,
        fast_alpha=0.35,
    )
    plume = np.array([0.80, 0.20, 0.05, 0.01, 0.60, 0.10, 0.02, 0.35], dtype=np.float64)
    clean_ref = norm.normalize(plume)

    # 400 steps of slow upward thermal drift (+1.60 baseline offset)
    for t in range(400):
        drift = 0.004 * t + np.linspace(0.0, 0.3, 8)
        norm.step(drift + rng.normal(0.0, 0.001, size=8))

    # Brief 25x strong plume spike should NOT corrupt tonic baseline upward
    high_drift = 0.004 * 400 + np.linspace(0.0, 0.3, 8)
    for _ in range(5):
        norm.step(high_drift + 25.0 * plume)

    # Settle 20 clean-air ticks, then test a weak 0.4x plume atop the drifted baseline
    for _ in range(20):
        norm.step(high_drift)
    raw_drifted_plume = high_drift + 0.4 * plume
    weak_resp = norm.step(raw_drifted_plume)
    unadapted_resp = norm.normalize(raw_drifted_plume)

    cos_adapted = float(
        np.dot(clean_ref, weak_resp) / (np.linalg.norm(clean_ref) * np.linalg.norm(weak_resp))
    )
    cos_unadapted = float(
        np.dot(clean_ref, unadapted_resp)
        / (np.linalg.norm(clean_ref) * np.linalg.norm(unadapted_resp))
    )
    assert cos_adapted > 0.985
    assert cos_unadapted < 0.80


def test_antennal_calibrate_reset_and_validation() -> None:
    rng = np.random.default_rng(7)
    norm = AntennalNormalizer(6, hill_n=1.5)
    ambient = 1.25 + rng.normal(0.0, 0.01, size=(20, 6))
    mean_b = norm.calibrate(ambient)
    assert mean_b.shape == (6,)
    assert np.allclose(norm.tonic_baseline, mean_b)

    norm.reset()
    assert np.all(norm.tonic_baseline == 0.0)

    with pytest.raises(ValueError):
        AntennalNormalizer(0)
    with pytest.raises(ValueError):
        AntennalNormalizer(4, sigma=0.0)


def test_kenyon_hasher_sparsity_determinism_and_tie_breaking() -> None:
    rng = np.random.default_rng(2026)
    X = rng.standard_normal((32, 64))

    h1 = KenyonHasher(64, n_kc=1024, d_proj=6, wta_frac=0.05, seed=42)
    h2 = KenyonHasher(64, n_kc=1024, d_proj=6, wta_frac=0.05, seed=42)
    assert h1.tag_bytes == 128
    assert h1.k_winners == 51

    tags_batch = h1.hash(X)
    assert tags_batch.shape == (32, 16)
    assert tags_batch.dtype == np.uint64
    assert np.array_equal(tags_batch, h2.hash(X))

    # Every row must have strictly k_winners=51 active bits and match 1D hashing
    active_per_row = np.sum(popcount_u64(tags_batch), axis=1)
    assert np.all(active_per_row == 51)
    for row_idx in range(8):
        assert np.array_equal(tags_batch[row_idx], h1.hash(X[row_idx]))

    # Degenerate all-zero vector (1024-way tie) must deterministically pick indices 0..50
    zero_vec = np.zeros(64, dtype=np.float64)
    zero_tag_1d = h1.hash(zero_vec)
    zero_tag_2d = h1.hash(np.vstack([zero_vec, zero_vec]))
    assert np.array_equal(zero_tag_1d, zero_tag_2d[0])
    assert np.array_equal(KenyonHasher.unpack_indices(zero_tag_1d), np.arange(51, dtype=np.int32))


def test_kenyon_hasher_similarity_monotonicity() -> None:
    rng = np.random.default_rng(99)
    hasher = KenyonHasher(64, n_kc=2048, d_proj=6, wta_frac=0.05, seed=77)
    assert hasher.tag_bytes == 256
    assert hasher.k_winners == 102

    noise_levels = [0.0, 0.05, 0.20, 0.60, 2.00]
    mean_overlaps: list[float] = []
    for sigma_n in noise_levels:
        ovs = []
        for _ in range(40):
            base = rng.standard_normal(64)
            pert = base + sigma_n * rng.standard_normal(64)
            ovs.append(popcount_overlap(hasher.hash(base), hasher.hash(pert)))
        mean_overlaps.append(float(np.mean(ovs)))

    # Mean overlap must strictly decrease with increasing perturbation magnitude
    for earlier, later in zip(mean_overlaps, mean_overlaps[1:]):
        assert earlier > later


def test_swar_popcount_matches_bit_counts() -> None:
    rng = np.random.default_rng(123)
    words = rng.integers(0, (1 << 63) - 1, size=(64, 16), dtype=np.uint64)
    counts = popcount_u64(words)
    expected = np.vectorize(lambda v: bin(int(v)).count("1"))(words)  # int.bit_count needs 3.10
    assert np.array_equal(counts, expected)


def test_kenyon_index_retrieval_and_serialization(tmp_path: Path) -> None:
    rng = np.random.default_rng(314)
    db = rng.standard_normal((600, 48))
    ids = [f"vec_{i:04d}" for i in range(600)]

    index = KenyonIndex(n_inputs=48, n_kc=1024, d_proj=6, wta_frac=0.05, seed=19)
    index.add(db, ids=ids)
    assert len(index) == 600
    assert index.memory_bytes == 600 * 128

    query = db[137] + 0.05 * rng.standard_normal(48)
    matches = index.query(query, top_k=5)
    assert len(matches) == 5
    assert matches[0].id == "vec_0137"
    assert matches[0].overlap >= 40
    assert 0.0 <= matches[0].similarity <= 1.0

    saved_path = index.save(tmp_path / "test_kenyon_idx")
    loaded = KenyonIndex.load(saved_path)
    assert len(loaded) == 600
    matches_loaded = loaded.query(query, top_k=5)
    assert matches_loaded == matches


def test_mushroom_novelty_1shot_habituation_and_recovery() -> None:
    rng = np.random.default_rng(55)
    det = MushroomNoveltyDetector(32, n_kc=1024, d_proj=6, wta_frac=0.05, lr=0.55, seed=12)

    normal_pat = rng.standard_normal(32)
    anomaly_pat = rng.standard_normal(32)

    s1 = det.update_and_score(normal_pat)
    s2 = det.update_and_score(normal_pat)
    s3 = det.update_and_score(normal_pat)
    s_after3 = det.score(normal_pat)
    s_anom = det.update_and_score(anomaly_pat)

    assert s1 > 0.95
    assert s1 > s2 > s3
    assert s3 < 0.25
    assert s_after3 < 0.25
    assert s_anom > 0.85

    # Test slow recovery (`tau_recover`)
    det_rec = MushroomNoveltyDetector(
        32, n_kc=1024, d_proj=6, wta_frac=0.05, lr=0.60, tau_recover=20.0, seed=12
    )
    _ = det_rec.update_and_score(normal_pat)
    habituated = det_rec.score(normal_pat)
    other = rng.standard_normal(32)
    for _ in range(30):
        det_rec.update_and_score(other)
    recovered = det_rec.score(normal_pat)
    assert recovered > habituated + 0.30


def test_mushroom_valence_classifier_1shot_and_aversive() -> None:
    rng = np.random.default_rng(88)
    ant = AntennalNormalizer(8, sigma=0.005, gamma=1.0, hill_n=2.0)
    clf = MushroomValenceClassifier(8, antennal=ant, n_kc=1024, d_proj=6, wta_frac=0.05, seed=42)

    odors = {
        "ethyl_butyrate": np.array([0.90, 0.20, 0.05, 0.01, 0.55, 0.08, 0.02, 0.25]),
        "isoamyl_acetate": np.array([0.10, 0.85, 0.72, 0.12, 0.05, 0.42, 0.03, 0.15]),
        "octenol": np.array([0.40, 0.12, 0.06, 0.88, 0.78, 0.10, 0.35, 0.04]),
        "benzaldehyde": np.array([0.05, 0.10, 0.75, 0.20, 0.10, 0.90, 0.65, 0.82]),
    }

    for label, vec in odors.items():
        clf.learn(vec, label, reward=1.0)

    # Verify 100% accuracy across 0.2x to 50x amplitude scaling + small noise
    for scale in (0.2, 1.0, 10.0, 50.0):
        for label, vec in odors.items():
            probe = scale * vec + rng.normal(0.0, 0.005 * scale, size=8)
            assert clf.predict(probe) == label

    # Verify aversive negative reward (`reward = -1.0`) produces negative valence
    clf_val = MushroomValenceClassifier(8, antennal=ant, n_kc=1024, seed=42)
    clf_val.learn(odors["benzaldehyde"], "aversive_compartment", reward=-1.0)
    scores = clf_val.predict_scores(odors["benzaldehyde"])
    assert scores["aversive_compartment"] < -0.90


def test_valence_imbalanced_fewshot_homeostasis() -> None:
    rng = np.random.default_rng(404)
    clf = MushroomValenceClassifier(16, n_kc=1024, d_proj=6, wta_frac=0.05, seed=7)
    class_a = rng.standard_normal(16)
    class_b = rng.standard_normal(16)

    # Expose class_a 25 times and class_b only 1 single time (25:1 imbalance)
    for _ in range(25):
        clf.learn(class_a + 0.05 * rng.standard_normal(16), "frequent_gas", reward=1.0)
    clf.learn(class_b, "rare_1shot_gas", reward=1.0)

    assert clf.predict(class_b + 0.05 * rng.standard_normal(16)) == "rare_1shot_gas"
    assert clf.predict(class_a + 0.05 * rng.standard_normal(16)) == "frequent_gas"


def test_kenyon_empty_index_and_batch_predict_edge_cases() -> None:
    idx = KenyonIndex(n_inputs=16, n_kc=2048, d_proj=6, wta_frac=0.05, seed=11)
    assert len(idx) == 0
    assert idx.query(np.ones(16), top_k=5) == []

    clf = MushroomValenceClassifier(16, n_kc=512, seed=11)
    with pytest.raises(RuntimeError):
        clf.predict(np.ones(16))
    clf.learn(np.arange(16, dtype=np.float64), 0)
    clf.learn(-np.arange(16, dtype=np.float64), 1)
    preds = clf.predict(np.vstack([np.arange(16, dtype=np.float64), -np.arange(16, dtype=np.float64)]))
    assert preds == [0, 1]


def test_cli_demo_benchmark_and_hash_csv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc_demo = cli_main(["demo", "--json", "--seed", "42"])
    assert rc_demo == 0
    demo_data = json.loads(capsys.readouterr().out)
    assert demo_data["antennal_100x_cosine_similarity"] > 0.9999
    assert demo_data["novelty_after_3_exposures"] < 0.25
    assert demo_data["novelty_orthogonal_anomaly"] > 0.85
    assert demo_data["vibration_1shot_10x_prediction"] == "bearing_spall"

    rc_bench = cli_main(["benchmark-quick", "--json", "--seed", "42"])
    assert rc_bench == 0
    bench_data = json.loads(capsys.readouterr().out)
    assert bench_data["retrieval_equal_active_k51"]["flyhash_recall_at_10"] > (
        bench_data["retrieval_equal_active_k51"]["simhash_51bit_recall_at_10"]
    )
    assert bench_data["streaming_1shot_anomaly"]["necksense_auroc"] > 0.90

    csv_in = tmp_path / "sensors.csv"
    csv_out = tmp_path / "hashes.csv"
    csv_in.write_text(
        "sample_id,s0,s1,s2,s3,s4,s5,s6,s7\n"
        "p_1x,0.8,0.3,0.1,0.0,0.5,0.2,0.05,0.4\n"
        "p_20x,16.0,6.0,2.0,0.0,10.0,4.0,1.0,8.0\n",
        encoding="utf-8",
    )
    rc_hash = cli_main(
        [
            "hash-csv",
            str(csv_in),
            "-o",
            str(csv_out),
            "--skip-header",
            "--id-col",
            "0",
            "--n-kc",
            "1024",
            "--antennal-normalize",
            "--c-parity",
        ]
    )
    assert rc_hash == 0
    lines = csv_out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    row1 = lines[1].split(",")
    row2 = lines[2].split(",")
    assert row1[0] == "p_1x" and row1[2] == "51"
    assert row2[0] == "p_20x" and row2[2] == "51"
    # 1x and 20x normalized hashes must have >= 49/51 overlapping bits
    tag1 = np.array([int(row1[3][i : i + 16], 16) for i in range(0, 256, 16)], dtype=np.uint64)
    tag2 = np.array([int(row2[3][i : i + 16], 16) for i in range(0, 256, 16)], dtype=np.uint64)
    assert popcount_overlap(tag1, tag2) >= 49


def test_c99_source_zero_heap_zero_float_static_audit() -> None:
    c_files = [
        REPO_ROOT / "csrc" / "necksense.h",
        REPO_ROOT / "csrc" / "necksense.c",
        REPO_ROOT / "csrc" / "tools" / "necksense_selftest.c",
        REPO_ROOT / "arduino" / "NeckSense" / "src" / "NeckSense.h",
        REPO_ROOT / "arduino" / "NeckSense" / "src" / "csrc" / "necksense.h",
        REPO_ROOT / "arduino" / "NeckSense" / "src" / "csrc" / "necksense.c",
        REPO_ROOT
        / "arduino"
        / "NeckSense"
        / "examples"
        / "ESP32_Vibration_And_Gas_Anomaly"
        / "ESP32_Vibration_And_Gas_Anomaly.ino",
    ]
    forbidden = re.compile(r"\b(float|double|malloc|calloc|realloc|free)\b")
    for path in c_files:
        assert path.exists(), f"Missing C99/Arduino source file: {path}"
        stripped = _strip_c_comments(path.read_text(encoding="utf-8"))
        hits = forbidden.findall(stripped)
        assert not hits, f"Forbidden heap/float token(s) {hits} found in {path.name}"


def test_python_c_parity_exact_q16_equivalence() -> None:
    x_in_q16 = np.array([55706, 16384, 3277, 0, 39322, 7864, 1311, 26214], dtype=np.int32)
    ant = AntennalNormalizer(
        8,
        sigma=655 / Q16_ONE,
        gamma=1.0,
        hill_n=2,
        tonic_alpha=655 / Q16_ONE,
        tonic_slew_limit=1311 / Q16_ONE,
        phasic_gain=0.5,
        fast_alpha=22938 / Q16_ONE,
        c_parity=True,
    )
    hasher = KenyonHasher(8, n_kc=1024, d_proj=6, k_winners=51, seed=42, c_parity=True)
    nov = MushroomNoveltyDetector(hasher=hasher, lr=36045 / Q16_ONE, c_parity=True)
    val = MushroomValenceClassifier(
        hasher=hasher,
        min_lr=6554 / Q16_ONE,
        opponent_inh=9830 / Q16_ONE,
        c_parity=True,
    )
    vib = VibrationFrontEnd(window_size=64, input_shift=6, c_parity=True)

    step1_q16 = ant.step_q16(x_in_q16)
    step2_q16 = ant.step_q16(x_in_q16 * 5 + 1200)
    tag1 = hasher.hash_q16(step1_q16)
    tag2 = hasher.hash_q16(step2_q16)
    ov_py = int(popcount_overlap(tag1, tag2))

    n1 = int(round(nov.update_and_score_tag(tag1) * Q16_ONE))
    n2 = int(round(nov.update_and_score_tag(tag1) * Q16_ONE))
    n3 = int(round(nov.update_and_score_tag(tag1) * Q16_ONE))
    na3 = int(round(nov.score_tag(tag1) * Q16_ONE))

    val.learn_tag(tag1, 0, reward=1.0)
    alt_q16 = np.array([4000, 62000, 51000, 9000, 2000, 31000, 1000, 5000], dtype=np.int32)
    alt_norm_q16 = ant.normalize_q16(alt_q16)
    val.learn_tag(hasher.hash_q16(alt_norm_q16), 1, reward=1.0)
    py_scores = val.predict_scores_tag(tag1)

    val.consolidate(0.25)
    for _ in range(15):
        val.learn_tag(tag2, 1, reward=1.0)
    py_ltm_scores = val.predict_scores_tag(tag1)

    sin32 = np.array(
        [
            0, 200, 392, 569, 724, 851, 946, 1004,
            1024, 1004, 946, 851, 724, 569, 392, 200,
            0, -200, -392, -569, -724, -851, -946, -1004,
            -1024, -1004, -946, -851, -724, -569, -392, -200,
        ],
        dtype=np.int32,
    )
    vib_in_q16 = np.empty(64, dtype=np.int32)
    for i in range(64):
        imp = 98304 if (i % 16 == 0) else (-65536 if (i % 16 == 1) else 0)
        vib_in_q16[i] = int(sin32[i & 31]) * 32 + imp + 4096
    vib_feat_py = vib.extract_q16(vib_in_q16)

    # Golden bit-exact reference emitted by `csrc/build/necksense_selftest --json-parity`
    c99_golden = {
        "step1_q16": [58857, 28349, 1939, 0, 53380, 9790, 318, 43332],
        "step2_q16": [58756, 28418, 2177, 10, 53249, 10095, 436, 43231],
        "tag1_hex": (
            "0000400001001000010000000000100000080000020000080004000200000000"
            "0802000000400400000000900000020022000000010040000000004a00800000"
            "0400000c00120204000000000000000488000000004002000000000000200040"
            "0100104000810000020000000000000800000810000000000000100050000000"
        ),
        "overlap_1_2": 51,
        "nov_q16": [65536, 29491, 13270, 5971],
        "val_scores_q16": [65536, 0],
        "whiff_count": 1,
        "whiff_rate_q16": 5898,
        "intermittency_q16": 12452,
        "ltm_scores_q16": [65536, 62024],
        "vib_feat_q16": [
            11050, 7396, 7970, 13717, 13547, 0, 9306, 13248,
            17306, 26324, 17496, 63019, 59819, 9180, 13505, 5568,
        ],
    }

    diag = ant.diagnostics()
    assert step1_q16.tolist() == c99_golden["step1_q16"]
    assert step2_q16.tolist() == c99_golden["step2_q16"]
    assert KenyonHasher.tag_to_hex(tag1) == c99_golden["tag1_hex"]
    assert ov_py == c99_golden["overlap_1_2"]
    assert [n1, n2, n3, na3] == c99_golden["nov_q16"]
    assert int(round(py_scores[0] * Q16_ONE)) == c99_golden["val_scores_q16"][0]
    assert int(round(py_scores[1] * Q16_ONE)) == c99_golden["val_scores_q16"][1]
    assert diag["whiff_count"] == c99_golden["whiff_count"]
    assert diag["whiff_rate_q16"] == c99_golden["whiff_rate_q16"]
    assert diag["intermittency_q16"] == c99_golden["intermittency_q16"]
    assert int(round(py_ltm_scores[0] * Q16_ONE)) == c99_golden["ltm_scores_q16"][0]
    assert int(round(py_ltm_scores[1] * Q16_ONE)) == c99_golden["ltm_scores_q16"][1]
    assert vib_feat_py.tolist() == c99_golden["vib_feat_q16"]
    assert q16_to_float(float_to_q16(0.5))[()] == pytest.approx(0.5)

    # On native POSIX or when NECKSENSE_TEST_WSL=1, also cross-check the live binaries
    selftest_bin = REPO_ROOT / "csrc" / "build" / "necksense_selftest"
    tiny_bin = REPO_ROOT / "csrc" / "build" / "necksense_selftest_tiny"
    if selftest_bin.exists() and (os.name != "nt" or os.environ.get("NECKSENSE_TEST_WSL") == "1"):
        if os.name != "nt":
            cmd = [str(selftest_bin), "--json-parity"]
            tiny_cmd = [str(tiny_bin)]
        else:
            wsl_path = "/mnt/" + selftest_bin.drive[0].lower() + selftest_bin.as_posix()[2:]
            cmd = ["wsl.exe", "--", wsl_path, "--json-parity"]
            wsl_tiny = "/mnt/" + tiny_bin.drive[0].lower() + tiny_bin.as_posix()[2:]
            tiny_cmd = ["wsl.exe", "--", wsl_tiny]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
        c_out = json.loads(proc.stdout)
        assert c_out["tag1_hex"] == c99_golden["tag1_hex"]
        assert c_out["step1_q16"] == c99_golden["step1_q16"]
        assert c_out["vib_feat_q16"] == c99_golden["vib_feat_q16"]
        assert c_out["ltm_scores_q16"] == c99_golden["ltm_scores_q16"]

        if tiny_bin.exists():
            t_proc = subprocess.run(tiny_cmd, capture_output=True, text=True, timeout=60, check=True)
            assert "All 8/8 ANSI C99 NeckSense self-tests PASSED." in t_proc.stdout
            m_tot = re.search(r"total=(\d+) B", t_proc.stdout)
            assert m_tot is not None and int(m_tot.group(1)) < 6000


def test_novelty_default_sparsity_detects_novel_chords_among_scaled_modes() -> None:
    """Regression for the 2% default: 20 recurring normal modes at 0.5x-5x scale plus novel
    4-channel chords. At 5% sparsity this scored ~0.96 AUROC; 2% should exceed 0.98."""
    from necksense.cli import _auroc_rank

    rng = np.random.default_rng(123)
    dim, n_modes = 16, 20
    modes = np.zeros((n_modes, dim))
    for m in range(n_modes):
        modes[m, rng.choice(dim, size=4, replace=False)] = rng.uniform(0.6, 1.4, size=4)
        modes[m] += rng.uniform(0.02, 0.12, size=dim)
    det = MushroomNoveltyDetector(
        dim, antennal=AntennalNormalizer(dim, sigma=0.01, tonic_alpha=0.0), n_kc=1024, lr=0.55, seed=1
    )
    assert det.hasher.k_winners == round(0.02 * 1024)
    scores, labels = [], []
    for t in range(2200):
        if t >= 200 and rng.random() < 0.05:
            x = rng.uniform(0.02, 0.10, size=dim)
            x[rng.choice(dim, size=4, replace=False)] = rng.uniform(0.8, 1.5, size=4)
            x *= float(np.exp(rng.uniform(np.log(0.6), np.log(4.0))))
            label = 1
        else:
            scale = float(np.exp(rng.uniform(np.log(0.5), np.log(5.0))))
            x = np.maximum(0.0, scale * modes[int(rng.integers(n_modes))] + rng.normal(0, 0.03 * scale, dim))
            label = 0
        s = det.update_and_score(x)
        if t >= 200:
            scores.append(s)
            labels.append(label)
    assert _auroc_rank(np.asarray(labels), np.asarray(scores)) > 0.98


def test_version_2_0_0_and_vibration_exports() -> None:
    assert necksense.__version__ == "2.0.0"
    assert VibrationAnalyzer is VibrationFrontEnd
    assert NS_VIB_CHANNELS == 16
    assert len(VIBRATION_CHANNEL_NAMES) == 16


def test_vibration_frontend_1shot_fault_detection_across_10x_amplitude_swings() -> None:
    rng = np.random.default_rng(2026)
    fe = VibrationFrontEnd(window_size=128)
    ant = AntennalNormalizer(16, sigma=0.004, gamma=1.0, hill_n=2.0)
    clf = MushroomValenceClassifier(16, antennal=ant, n_kc=1024, d_proj=6, wta_frac=0.05, seed=77)
    nov = MushroomNoveltyDetector(16, antennal=ant, n_kc=1024, wta_frac=0.02, lr=0.55, seed=77)

    def make_motor_window(mode: str, amp: float, phase: float) -> np.ndarray:
        t = np.arange(128, dtype=np.float64)
        w0 = 2.0 * np.pi * t / 32.0 + phase
        if mode == "healthy":
            sig = np.sin(w0) + 0.12 * np.sin(2.0 * w0)
        elif mode == "misalignment":
            sig = 0.25 * np.sin(w0) + 1.0 * np.sin(2.0 * w0) + 0.75 * np.sin(3.0 * w0)
        elif mode == "bearing_spall":
            sig = 0.25 * np.sin(w0)
            imp_idx = (np.arange(128) + int(round(phase * 5.0))) % 16
            sig += np.where(imp_idx == 0, 2.2, np.where(imp_idx == 1, -1.6, np.where(imp_idx == 2, 0.9, 0.0)))
        elif mode == "chipped_blade":
            s2 = np.sin(2.0 * w0)
            sig = np.where(s2 > 0.0, s2 + 0.5 * np.sin(5.0 * w0), 0.12 * s2)
        else:
            raise ValueError(mode)
        return amp * sig

    # Verify strict degree-1 homogeneity: f(10 * x) == 10 * f(x)
    f_1x = fe.extract(make_motor_window("bearing_spall", 1.0, 0.0))
    f_10x = fe.extract(make_motor_window("bearing_spall", 10.0, 0.0))
    assert np.allclose(f_10x, 10.0 * f_1x, rtol=1e-12, atol=1e-12)

    modes = ["healthy", "misalignment", "bearing_spall", "chipped_blade"]
    for m in modes:
        clf.learn(fe.extract(make_motor_window(m, 1.0, 0.0)), m)

    # Habituate novelty detector to healthy vibration across varying RPM load
    for step_i in range(4):
        s_h = nov.update_and_score(fe.extract(make_motor_window("healthy", 1.0 + 2.0 * step_i, 0.3 * step_i)))
    assert s_h < 0.25
    assert nov.score(fe.extract(make_motor_window("bearing_spall", 8.0, 1.1))) > 0.85

    # 100% 1-shot fault classification across 0.2x to 10x amplitude + random phase + sensor noise
    for amp in (0.2, 1.0, 4.0, 10.0):
        for m in modes:
            phase = float(rng.uniform(0.0, 2.0 * np.pi))
            noisy = make_motor_window(m, amp, phase) + rng.normal(0.0, 0.015 * amp, size=128)
            assert clf.predict(fe.extract(noisy)) == m


def test_vibration_frontend_3axis_tilted_gravity_and_batch() -> None:
    fe = VibrationFrontEnd(window_size=128)
    t = np.arange(128, dtype=np.float64)
    # Tilted IMU with static 1g vector (0.6g, -0.3g, +0.74g) + dynamic vibration
    xyz_1x = np.column_stack(
        [
            0.60 + 0.20 * np.sin(2.0 * np.pi * t / 32.0),
            -0.30 + 0.10 * np.cos(2.0 * np.pi * t / 32.0),
            0.74 + np.where((t % 16) == 0, 0.45, 0.0),
        ]
    )
    xyz_8x = np.column_stack(
        [
            -0.40 + 8.0 * 0.20 * np.sin(2.0 * np.pi * t / 32.0),
            0.85 + 8.0 * 0.10 * np.cos(2.0 * np.pi * t / 32.0),
            0.35 + 8.0 * np.where((t % 16) == 0, 0.45, 0.0),
        ]
    )
    ant = AntennalNormalizer(16, sigma=0.002)
    r1 = ant.normalize(fe.extract(xyz_1x))
    r8 = ant.normalize(fe.extract(xyz_8x))
    cos_sim = float(np.dot(r1, r8) / (np.linalg.norm(r1) * np.linalg.norm(r8)))
    assert cos_sim > 0.9999

    batch_out = fe.transform(np.stack([xyz_1x, xyz_8x], axis=0))
    assert batch_out.shape == (2, 16)

    i16_xyz = np.round(xyz_1x * 1000.0).astype(np.int16)
    q16_feat = fe.extract_i16(i16_xyz)
    assert q16_feat.shape == (16,) and q16_feat.dtype == np.int32


def test_antennal_whiff_intermittency_turbulent_vs_static_cloud() -> None:
    odor = np.array([0.50, 0.25, 0.12, 0.03, 0.38, 0.06, 0.02, 0.20], dtype=np.float64)
    clean_air = np.zeros(8, dtype=np.float64)

    # Static well-mixed cloud: single initial up-crossing, whiff_rate decays near 0
    ant_static = AntennalNormalizer(8, sigma=0.01)
    for _ in range(80):
        ant_static.step(odor)
    d_static = ant_static.diagnostics(sample_rate_hz=20.0)
    assert d_static["whiff_count"] == 1
    assert d_static["whiff_rate"] < 0.01

    # Turbulent pulsatile plume (near chemical source): repeated phasic whiffs
    ant_turb = AntennalNormalizer(8, sigma=0.01)
    for t in range(80):
        sample = odor if (t % 5) < 2 else clean_air
        norm_vec, w_rate = ant_turb.step_with_whiff(sample)
        assert norm_vec.shape == (8,)
    d_turb = ant_turb.diagnostics(sample_rate_hz=20.0)
    assert d_turb["whiff_count"] >= 14
    assert d_turb["whiff_rate"] > 0.12
    assert d_turb["whiff_hz"] > 2.4
    assert d_turb["intermittency"] > 0.25
    assert w_rate == pytest.approx(d_turb["whiff_rate"])


@pytest.mark.parametrize("c_parity", [False, True])
def test_valence_gamma_alpha_beta_zero_forgetting_continual_learning(c_parity: bool) -> None:
    rng = np.random.default_rng(707)
    dim = 20
    shared_bg = np.zeros(dim, dtype=np.float64)
    shared_bg[:8] = 1.0

    # 6 sequential tasks sharing a strong 8-channel carrier + 2 task-diagnostic channels
    prototypes: dict[str, np.ndarray] = {}
    for idx, name in enumerate(["A", "B", "C", "D", "E", "F"]):
        vec = shared_bg.copy()
        vec[8 + idx * 2] = 0.85
        vec[9 + idx * 2] = 0.85
        prototypes[name] = vec

    clf_stm_only = MushroomValenceClassifier(
        dim,
        n_kc=1024,
        wta_frac=0.05,
        min_lr=0.25,
        opponent_inh=0.55,
        ltm_alpha_beta=False,
        seed=42,
        c_parity=c_parity,
    )
    clf_ltm = MushroomValenceClassifier(
        dim,
        n_kc=1024,
        wta_frac=0.05,
        min_lr=0.25,
        opponent_inh=0.55,
        ltm_alpha_beta=True,
        seed=42,
        c_parity=c_parity,
    )

    # Phase 1: Learn early classes A and B from 1 single shot each, consolidating each task
    for early in ("A", "B"):
        clf_stm_only.learn(prototypes[early], early, reward=1.0)
        clf_ltm.learn(prototypes[early], early, reward=1.0)
        consolidated_synapses = clf_ltm.consolidate(thresh=0.50)
        assert consolidated_synapses > 0

    # Phase 2: Sequentially train new classes C, D, E, F for 35 trials each (140 updates)
    for later in ("C", "D", "E", "F"):
        for _ in range(35):
            noisy = prototypes[later] + rng.normal(0.0, 0.005, size=dim)
            clf_stm_only.learn(noisy, later, reward=1.0)
            clf_ltm.learn(noisy, later, reward=1.0)
        clf_ltm.consolidate(thresh=0.50)

    # 2-compartment KCg + KCab LTM retains 100% (6/6 classes, 0% forgetting of A..E)
    for name, proto in prototypes.items():
        assert clf_ltm.predict(proto) == name

    # Unconsolidated gamma-only STM suffers 100% catastrophic forgetting on earlier classes A & B
    assert clf_stm_only.predict(prototypes["A"]) != "A"
    assert clf_stm_only.predict(prototypes["B"]) != "B"
    stm_a = clf_stm_only.predict_scores(prototypes["A"])["A"]
    ltm_a = clf_ltm.predict_scores(prototypes["A"])["A"]
    assert ltm_a > 0.90
    assert ltm_a > stm_a + 0.65

    # Explicit aversive extinction (`reward = -1.0`) must still reverse a consolidated memory
    for _ in range(8):
        clf_ltm.learn(prototypes["A"], "A", reward=-1.0)
    assert clf_ltm.predict_scores(prototypes["A"])["A"] < 0.0


def test_arduino_library_structure_and_sha256_parity() -> None:
    ard_root = REPO_ROOT / "arduino" / "NeckSense"
    props_path = ard_root / "library.properties"
    umb_path = ard_root / "src" / "NeckSense.h"
    ino_path = (
        ard_root
        / "examples"
        / "ESP32_Vibration_And_Gas_Anomaly"
        / "ESP32_Vibration_And_Gas_Anomaly.ino"
    )
    assert props_path.exists()
    assert umb_path.exists()
    assert ino_path.exists()

    props = dict(
        line.split("=", 1)
        for line in props_path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert props["name"] == "NeckSense"
    assert props["version"] == "2.0.0"
    assert props["architectures"] == "*"
    assert props["includes"] == "NeckSense.h"

    for fname in ("necksense.h", "necksense.c"):
        h_csrc = hashlib.sha256((REPO_ROOT / "csrc" / fname).read_bytes()).hexdigest()
        h_ard = hashlib.sha256((ard_root / "src" / "csrc" / fname).read_bytes()).hexdigest()
        assert h_csrc == h_ard, f"SHA-256 mismatch between csrc/{fname} and arduino/NeckSense/src/csrc/{fname}"
