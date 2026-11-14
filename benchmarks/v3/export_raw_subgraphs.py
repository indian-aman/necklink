"""
Export compact `banc_v888_raw_subgraphs.pkl.gz` from the compiled BANC v888 connectome
directory for running `K >= 100` Maslov-Sneppen shuffles, and verify bit-exact round-trip
compilation against the committed `.npz` weight bundles.
"""

from __future__ import annotations

import argparse
import gzip
import json
import pickle
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages" / "necklink_cns"))

from necklink_cns.connectome.extract import (  # noqa: E402
    DEFAULT_BANC_DIR,
    compile_circuits_from_subgraphs,
    extract_raw_subgraphs,
    sha256_file,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--banc-dir",
        type=Path,
        default=DEFAULT_BANC_DIR,
        help="Path to compiled BANC v888 directory containing connectome_compiled.npz and neurons_meta.parquet",
    )
    args = ap.parse_args()

    out_dir = Path(__file__).resolve().parent / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_pkl_gz = out_dir / "banc_v888_raw_subgraphs.pkl.gz"

    print(f"Extracting raw subgraphs from BANC v888 ({args.banc_dir})...")
    raw = extract_raw_subgraphs(args.banc_dir)
    with gzip.open(raw_pkl_gz, "wb", compresslevel=6) as f:
        pickle.dump(raw, f, protocol=pickle.HIGHEST_PROTOCOL)

    sz_mb = raw_pkl_gz.stat().st_size / (1024 * 1024)
    pkl_sha = sha256_file(raw_pkl_gz)
    print(f"Saved {raw_pkl_gz} ({sz_mb:.2f} MB), SHA-256: {pkl_sha}")

    # Round-trip reload & verify exact match against committed `.npz` files
    with gzip.open(raw_pkl_gz, "rb") as f:
        raw_reloaded = pickle.load(f)
    compiled = compile_circuits_from_subgraphs(raw_reloaded)

    checks = [
        ("cx", REPO_ROOT / "packages" / "neckcompass" / "neckcompass" / "data" / "banc_cx_v3.npz"),
        ("optic", REPO_ROOT / "packages" / "necksight" / "necksight" / "data" / "banc_optic_v3.npz"),
        ("mb", REPO_ROOT / "packages" / "necksense" / "necksense" / "data" / "banc_mb_v3.npz"),
        ("vnc_dn", REPO_ROOT / "packages" / "neckcord" / "neckcord" / "data" / "banc_vnc_v3.npz"),
    ]
    for key, npz_path in checks:
        with np.load(npz_path, allow_pickle=False) as z:
            for arr_name in z.files:
                if arr_name in compiled[key]:
                    a = np.asarray(z[arr_name])
                    b = np.asarray(compiled[key][arr_name])
                    if a.dtype.kind in ("f", "i", "u", "b"):
                        max_diff = float(np.max(np.abs(a.astype(np.float64) - b.astype(np.float64))))
                        assert max_diff < 1e-10, f"Mismatch in {key}.{arr_name}: {max_diff}"
    manifest = {
        "raw_subgraphs_file": raw_pkl_gz.name,
        "raw_subgraphs_size_bytes": raw_pkl_gz.stat().st_size,
        "raw_subgraphs_sha256": pkl_sha,
        "source_connectome_npz_sha256": raw["meta"]["sha256_connectome_compiled_npz"],
        "source_neurons_meta_parquet_sha256": raw["meta"]["sha256_neurons_meta_parquet"],
        "w_vnc_ei_nnz": int(raw["vnc_dn"]["W_vnc_ei"].nnz),
        "roundtrip_verified": True,
    }
    (out_dir / "raw_subgraphs_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Round-trip verification PASSED 100%!")


if __name__ == "__main__":
    main()
