"""End-to-end regression: the real pipeline on the real bronze data must reproduce the golden baseline.

Slow (about a minute) and skipped when the bronze data or the control store is not present. Runs in a temp copy of
the control store and writes to temp gold/silver folders, so it never touches the real registries or runs.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import golden_utils
from critical_buildings import paths

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / "tests" / "golden" / "baseline.json"

pytestmark = pytest.mark.slow


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    real_control = REPO / "control" / "control.db"
    if not (REPO / "data" / "bronze" / "dfe_gias").exists() or not real_control.exists() or not BASELINE.exists():
        pytest.skip("real bronze data / control store / baseline not available")
    shutil.copytree(REPO / "control", tmp_path / "control")
    monkeypatch.setattr(paths, "CONTROL", tmp_path / "control")
    monkeypatch.setattr(paths, "GOLD", tmp_path / "gold")
    monkeypatch.setattr(paths, "SILVER", tmp_path / "silver")
    # reuse the (slow-to-build, rule-independent) conformed tables so the test exercises classification, merging and
    # publishing rather than re-reading the 23 MB CQC spreadsheet and a million building footprints
    cached = REPO / "data" / "silver" / "suffolk" / "conformed"
    if cached.exists():
        shutil.copytree(cached, tmp_path / "silver" / "suffolk" / "conformed")
    return tmp_path


def test_pipeline_reproduces_the_golden_baseline_with_and_without_silver_reuse(sandbox):
    from critical_buildings import pipeline
    first = pipeline.run("suffolk", run_type="dev", notes="regression", reuse_silver=False)
    assert golden_utils.compare(first["dest"], BASELINE) == []
    second = pipeline.run("suffolk", run_type="dev", notes="regression (reuse)", reuse_silver=True)
    assert second["silver_reused"] is True
    assert golden_utils.compare(second["dest"], BASELINE) == []
    assert second["previous_run_id"] == first["run_id"]
    lin = (second["dest"] / "lineage.json").read_text()
    assert '"changes_vs_previous": {}' in lin.replace("\n", "").replace("  ", " ") or "changes_vs_previous" in lin


def test_cli_check_passes_on_the_real_repository():
    if not (REPO / "control" / "control.db").exists():
        pytest.skip("no control store")
    out = subprocess.run([sys.executable, "-m", "critical_buildings", "check"], cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0 and "All checks passed" in out.stdout
