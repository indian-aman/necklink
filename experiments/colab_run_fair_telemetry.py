"""Runs the full-scale fair telemetry benchmark on a Colab VM (see scripts/fair_telemetry_benchmark.py)."""
import os
import subprocess

env = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
r = subprocess.run(
    ["python", "scripts/fair_telemetry_benchmark.py", "--full", "--seeds", "5", "--workers", "46", "--aqms-dir", "/content/aqms"],
    cwd="/content/necklink", env=env, capture_output=True, text=True,
)
print(r.stdout[-4000:])
print(r.stderr[-3000:])
print("rc", r.returncode)
