"""The governed store: drafts, immutable versions, audit trail, decisions, identity registries, migrations."""
import json
import sqlite3
from pathlib import Path

import helpers
import pandas as pd
import pytest

from critical_buildings.control import dumps
from critical_buildings.control import rules as rs
from critical_buildings.control import store as store_mod
from critical_buildings.control.store import ControlError, ControlStore


# ------------------------------------------------------------------ edits and audit
def test_every_change_needs_a_reason(store):
    with pytest.raises(ControlError, match="reason is required"):
        store.apply_table("rule", [dict(rule_ref=1, primary_category="A", building_type="B", importance="High",
                                        importance_driver="d", priority=50, active=1, notes="")], "me", " ")


def test_apply_table_inserts_updates_and_deletes_with_audit(seeded_store):
    s = seeded_store
    rules = s.draft_snapshot()["rules"]
    assert len(rules) == 4
    rows = [{**r, "active": 1} for r in rules]
    rows[0]["importance"] = "Low"                              # update
    rows = [r for r in rows if r["rule_ref"] != 34]             # delete (cascades to its tag)
    s.apply_table("rule", rows, "ann", "tuning")
    snap = s.draft_snapshot()
    assert {r["rule_ref"]: r["importance"] for r in snap["rules"]}[1] == "Low"
    assert 34 not in {r["rule_ref"] for r in snap["rules"]}
    assert all(t["rule_ref"] != 34 for t in snap["tags"])
    log = s.change_log_df()
    ann = log[log.user == "ann"]
    assert set(ann.action) == {"update", "delete"}
    upd = ann[ann.action == "update"].iloc[0]
    assert json.loads(upd.old_json) == {"importance": "High"} and json.loads(upd.new_json) == {"importance": "Low"}
    assert (ann.reason == "tuning").all()


def test_new_tag_row_gets_an_id(seeded_store):
    s = seeded_store
    tags = [{**t, "active": 1} for t in s.draft_snapshot()["tags"]]
    tags.append(dict(rule_ref=1, source="OSM", tag="emergency=fire_hydrant", active=1, notes=""))
    s.apply_table("rule_tag", tags, "ann", "new tag")
    assert max(t["id"] for t in s.draft_snapshot()["tags"]) == 7


# ------------------------------------------------------------------ versions
def test_nothing_published_yet(seeded_store):
    assert seeded_store.has_unpublished_changes() is True
    with pytest.raises(ControlError, match="No published rule set"):
        seeded_store.get_version()


def test_publish_creates_an_immutable_numbered_version(seeded_store):
    s = seeded_store
    v1 = s.publish("ann", "first version")
    assert v1 == 1 and not s.has_unpublished_changes()
    _, snap1 = s.get_version(1)
    rows = [{**r, "active": 1} for r in s.draft_snapshot()["rules"]]
    rows[0]["priority"] = 10
    s.apply_table("rule", rows, "ann", "raise priority")
    assert s.has_unpublished_changes()
    assert s.get_version()[0] == 1                      # the pipeline still sees v1 until Publish
    v2 = s.publish("ann", "second version", approved_by="bob")
    assert v2 == 2
    assert s.get_version(1)[1] == snap1                 # v1 is frozen
    assert s.get_version(2)[1]["rules"][0]["priority"] == 10
    versions = s.versions_df()
    assert list(versions.version_id) == [2, 1] and versions.iloc[0].approved_by == "bob"


def test_publish_with_identical_content_is_refused(seeded_store):
    seeded_store.publish("ann", "v1")
    with pytest.raises(ControlError, match="Nothing to publish"):
        seeded_store.publish("ann", "again")


def test_publish_is_blocked_by_validation_errors(seeded_store):
    s = seeded_store
    tags = [{**t, "active": 1} for t in s.draft_snapshot()["tags"]]
    tags[0]["tag"] = "amenity = bad"
    s.apply_table("rule_tag", tags, "ann", "oops")
    with pytest.raises(ControlError, match="fix these first"):
        s.publish("ann", "should fail")
    assert s.versions_df().empty


def test_publish_needs_a_reason(seeded_store):
    with pytest.raises(ControlError, match="reason is required"):
        seeded_store.publish("ann", "")


def test_ruleset_view_for_the_pipeline(seeded_store):
    s = seeded_store
    s.publish("ann", "v1")
    r = s.ruleset()
    assert r.version_id == 1 and r.label.startswith("v1-") and len(r.label) == len("v1-") + 6
    assert r.errors == [] and len(r.tags) == 5
    assert r.priorities[20] == 90
    assert r.role["ancillary"] == {"garage", "shed"} and isinstance(r.role["primary"], set)
    assert r.mapping[0]["source"] == "DfE" and len(r.mapping) == 2


def test_content_hash_ignores_key_order():
    a, b = helpers.snapshot(), helpers.snapshot()
    b["role"] = dict(reversed(list(b["role"].items())))
    assert rs.content_hash(a) == rs.content_hash(b)
    b["role"]["min_area"] = 151.0
    assert rs.content_hash(a) != rs.content_hash(b)


# ------------------------------------------------------------------ decisions
def test_overrides_round_trip_with_pipeline_column_names(store):
    store.save_overrides([dict(building_id="BLDG-000001", building_type_override="Hospital",
                               manual_override_notes="checked", exclude="Y")], "ann", "review")
    df = store.overrides_df()
    row = df.iloc[0]
    assert row["Building ID"] == "BLDG-000001" and row["Building Type Override"] == "Hospital"
    assert row["Exclude (Y)"] == "Y" and row["Review Decision"] == ""
    stored = store.table_df("override").iloc[0]
    assert stored.decided_by == "ann" and stored.decided_at
    store.delete_override("BLDG-000001", "ann", "withdrawn")
    assert store.overrides_df().empty
    assert {"insert", "delete"} <= set(store.change_log_df().action)


# ------------------------------------------------------------------ identity registries
def _reg(*rows):
    return pd.DataFrame(rows, columns=["building_id", "os_key", "osm_key", "created", "wkt"])


def test_building_registry_is_append_only(store):
    store.save_building_registry(_reg(("BLDG-000001", "os1", None, "2026-01-01", "POINT (0 0)"),
                                      ("BLDG-000002", None, "way/2", "2026-01-01", "POINT (1 1)")), "run1")
    store.save_building_registry(_reg(("BLDG-000001", "os1", None, "2026-01-01", "POINT (0 0)")), "run2")
    df = store.table_df("building_registry").set_index("building_id")
    assert len(df) == 2                                              # never deleted
    assert pd.isna(df.loc["BLDG-000001", "retired_run"])
    assert df.loc["BLDG-000002", "retired_run"] == "run2"           # absent from run2 -> retired
    store.save_building_registry(_reg(("BLDG-000002", None, "way/2", "2026-01-01", "POINT (1 1)")), "run3")
    df = store.table_df("building_registry").set_index("building_id")
    assert pd.isna(df.loc["BLDG-000002", "retired_run"])            # a returning building keeps its ID
    assert df.loc["BLDG-000002", "first_seen_run"] == "run1" and df.loc["BLDG-000002", "last_seen_run"] == "run3"
    assert len(store.building_registry_df()) == 2                    # retired rows are still matchable


def test_building_site_map(store):
    store.save_building_registry(_reg(("BLDG-000001", "os1", None, "d", "POINT (0 0)")), "r1")
    assert store.building_site_map() == {}
    store.set_building_sites({"BLDG-000001": "SITE-000007"})
    assert store.building_site_map() == {"BLDG-000001": "SITE-000007"}


def test_site_registry_append_only(store):
    store.save_site_registry({"SITE-000001": "2026-01-01", "SITE-000002": "2026-01-01"}, "r1")
    store.save_site_registry({"SITE-000001": "2026-01-01"}, "r2")
    df = store.table_df("site_registry").set_index("site_id")
    assert len(df) == 2 and df.loc["SITE-000002", "retired_run"] == "r2"


# ------------------------------------------------------------------ migrations and dumps
def test_old_database_is_upgraded_in_place(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript((Path(store_mod.__file__).parent / "schema.sql").read_text(encoding="utf-8"))
    c.execute("PRAGMA user_version = 1")
    c.execute("INSERT INTO rule VALUES (1, 'A', 'B', 'High', 'd', 50, 1, '')")
    c.commit()
    c.close()
    s = ControlStore(db)
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA user_version").fetchone()[0] == store_mod.SCHEMA_VERSION
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"run", "release", "run_change"} <= tables
    assert "site_id" in [r[1] for r in c.execute("PRAGMA table_info(building_registry)")]
    assert len(s.draft_snapshot()["rules"]) == 1                   # existing data survives


def test_dump_is_one_deterministic_readable_file(seeded_store, tmp_path):
    seeded_store.publish("ann", "v1")
    out = tmp_path / "control_dump.txt"
    dumps.write_dumps(seeded_store, out)
    first = out.read_bytes()
    dumps.write_dumps(seeded_store, out)
    assert out.read_bytes() == first and b"\r\n" not in first
    text = first.decode("utf-8")
    for section in ("rule", "rule_tag", "reference_mapping", "role_settings", "override", "rule_set_version", "change_log"):
        assert f"\n## {section} (" in text
    assert "Fire Station" in text
