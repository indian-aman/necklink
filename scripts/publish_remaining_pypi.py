#!/usr/bin/env python3
"""
Upload remaining NeckLink brand packages (`necksense`, `necklink` umbrella) once
PyPI's 4-new-projects/day anti-squatting rate-limit window resets.

Reads `PYPI_API_TOKEN` from `~/.env` (or environment) without printing secrets.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _load_token() -> str:
    tok = os.environ.get("PYPI_API_TOKEN", "").strip()
    if tok:
        return tok
    env_file = Path.home() / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s.startswith("PYPI_API_TOKEN="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("PYPI_API_TOKEN not found in environment or ~/.env")


def main() -> int:
    token = _load_token()
    root = Path(__file__).resolve().parents[1]
    targets = [
        ("necksense v1.0.0", root.parent / "necksense" / "dist"),
        ("necklink umbrella v1.3.0", root / "umbrella" / "dist"),
    ]

    env = os.environ.copy()
    env["TWINE_USERNAME"] = "__token__"
    env["TWINE_PASSWORD"] = token

    rc_any = 0
    for label, dist_dir in targets:
        files = sorted(dist_dir.glob("*"))
        if not files:
            print(f"[SKIP] {label}: no files in {dist_dir}")
            continue
        print(f"[UPLOAD] {label} ({len(files)} files)...")
        proc = subprocess.run(
            [sys.executable, "-m", "twine", "upload", "--non-interactive", "--skip-existing", *[str(f) for f in files]],
            env=env,
            capture_output=True,
            text=True,
        )
        out = (proc.stdout + "\n" + proc.stderr).replace(token, "[REDACTED]")
        if proc.returncode == 0:
            print(f"[OK] {label} live on PyPI!")
        else:
            print(f"[PENDING] {label}:\n{out.strip()}")
            rc_any = proc.returncode
    return rc_any


if __name__ == "__main__":
    raise SystemExit(main())
