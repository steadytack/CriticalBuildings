"""The Duckle pipeline files compile (no data, network or DuckDB needed)."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DUCKLE = shutil.which("duckle") or str(Path(sys.executable).with_name("duckle.exe"))


@pytest.mark.skipif(not Path(DUCKLE).exists() and not shutil.which("duckle"), reason="duckle not installed (pip install -e .[canvas])")
def test_every_pipeline_file_validates():
    out = subprocess.run([DUCKLE, "validate"], cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert len(list((REPO / "pipelines").glob("*.json"))) >= 4
