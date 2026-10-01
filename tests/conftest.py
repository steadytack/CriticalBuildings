import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from critical_buildings.control.store import ControlStore  # noqa: E402
import helpers  # noqa: E402


@pytest.fixture
def store(tmp_path):
    """An empty control store in a temp folder."""
    return ControlStore(tmp_path / "control.db")


@pytest.fixture
def seeded_store(store):
    """A control store whose draft holds the helper rule set (not yet published)."""
    snap = helpers.snapshot()
    reason = "test setup"
    store.apply_table("rule", [{**r, "active": 1} for r in snap["rules"]], "tester", reason)
    store.apply_table("rule_tag", [{**t, "active": 1} for t in snap["tags"]], "tester", reason)
    store.apply_table("reference_mapping", [{**m, "active": 1} for m in snap["reference_mapping"]], "tester", reason)
    role = snap["role"]
    store.set_role_settings(role["min_area"], role["ancillary"], role["primary"], role["per_type"], "tester", reason)
    return store


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Point every data location at a temp folder (settings.yml stays real), so lifecycle code can run safely."""
    from critical_buildings import paths
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    for name in ("CONTROL", "DATA", "PUBLISHED", "ARCHIVE"):
        monkeypatch.setattr(paths, name, tmp_path / name.lower())
    monkeypatch.setattr(paths, "BRONZE", tmp_path / "data" / "bronze")
    monkeypatch.setattr(paths, "SILVER", tmp_path / "data" / "silver")
    monkeypatch.setattr(paths, "GOLD", tmp_path / "data" / "gold")
    return tmp_path
