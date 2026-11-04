"""Every ```python block in README.md must run as written (keeps the docs honest)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BLOCKS = re.findall(r"```python\n(.*?)```", (ROOT / "README.md").read_text(encoding="utf-8"), re.S)


def test_readme_has_python_examples() -> None:
    assert BLOCKS, "README.md should contain at least one ```python example"


@pytest.mark.parametrize("index", range(len(BLOCKS)))
def test_readme_python_example_runs(index: int, tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", BLOCKS[index]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, f"README example #{index} failed:\n{proc.stderr[-2000:]}"
