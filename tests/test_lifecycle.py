"""Runs, releases, retention and archiving (synthetic run folders; nothing touches the real data)."""
import datetime as dt
import json
import os
import time

import pytest

from critical_buildings import config, lineage, paths, retention, runs
from critical_buildings.control.store import ControlError
from critical_buildings.publish import DELIVERABLES

NOW = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def iso(days_ago):
    return (NOW - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")


def make_run(store, run_id, days_ago=1, run_type="candidate", status="succeeded", silver_key=None, bronze=None,
             area="suffolk", on_disk=True):
    """A registered run with a gold folder holding every deliverable plus lineage.json."""
    store.insert_run({"run_id": run_id, "area": area, "run_type": run_type, "status": status, "started_at": iso(days_ago),
                          "silver_key": silver_key, "bronze_key": "bk-" + run_id,
                          "bronze_snapshots_json": json.dumps(bronze or [])})
    if on_disk:
        d = runs.run_dir(area, run_id)
        d.mkdir(parents=True)
        for name in DELIVERABLES:
            (d / name).write_text(f"{name} of {run_id}")
        lin = {"run_id": run_id, "rules": {"label": "v1-abc123"},
               "outputs": {"files": [lineage.file_entry(d / n) for n in DELIVERABLES]}}
        (d / "lineage.json").write_text(json.dumps(lin))
    return runs.run_dir(area, run_id)


# ------------------------------------------------------------------ releases
def test_release_copies_deliverables_verifies_them_and_records_it(isolated_paths, store):
    make_run(store, "R1")
    dest = runs.release(store, "suffolk", "v2026.10.0", "R1", notes="first", user="ann")
    assert dest == paths.PUBLISHED / "suffolk" / "v2026.10.0"
    assert {p.name for p in dest.iterdir()} == set(DELIVERABLES) | {"lineage.json", "RELEASE.json"}
    meta = json.loads((dest / "RELEASE.json").read_text())
    assert meta["run_id"] == "R1" and meta["released_by"] == "ann" and meta["rule_version"] == "v1-abc123"
    rel = store.releases_df().iloc[0]
    assert rel.release == "v2026.10.0" and rel.run_id == "R1"
    assert store.get_run("R1")["run_type"] == "release"


def test_releases_are_immutable_and_names_cannot_be_reused(isolated_paths, store):
    make_run(store, "R1")
    make_run(store, "R2")
    runs.release(store, "suffolk", "v1", "R1")
    with pytest.raises(ControlError, match="already exists"):
        runs.release(store, "suffolk", "v1", "R2")


def test_only_successful_runs_with_files_on_disk_can_be_released(isolated_paths, store):
    make_run(store, "BAD", status="failed")
    make_run(store, "GONE", on_disk=False)
    with pytest.raises(ControlError, match="not a successful run"):
        runs.release(store, "suffolk", "v1", "BAD")
    with pytest.raises(ControlError, match="no longer on disk"):
        runs.release(store, "suffolk", "v2", "GONE")
    with pytest.raises(ControlError, match="not a successful run"):
        runs.release(store, "suffolk", "v3", "NOPE")


def test_a_corrupted_copy_aborts_the_release(isolated_paths, store, monkeypatch):
    make_run(store, "R1")
    real = lineage.sha256_file
    monkeypatch.setattr(lineage, "sha256_file", lambda p: "wrong" if "published" in str(p) else real(p))
    with pytest.raises(ControlError, match="Checksum mismatch"):
        runs.release(store, "suffolk", "v1", "R1")
    assert not (paths.PUBLISHED / "suffolk" / "v1").exists() and store.releases_df().empty


def test_default_release_name_counts_releases_in_the_month(isolated_paths, store):
    make_run(store, "R1")
    make_run(store, "R2")
    assert runs.next_release_name(store, dt.date(2026, 10, 5)) == "v2026.10.0"
    runs.release(store, "suffolk", "v2026.10.0", "R1")
    assert runs.next_release_name(store, dt.date(2026, 10, 20)) == "v2026.10.1"
    assert runs.next_release_name(store, dt.date(2026, 11, 1)) == "v2026.11.0"


def test_previous_run_is_read_from_either_file_layout(isolated_paths, store):
    make_run(store, "R1")
    (runs.run_dir("suffolk", "R1") / "buildings.csv").write_text("Building ID,Site ID\nBLDG-000002,SITE-000002\n")
    runs.set_current("suffolk", "R1")
    assert runs.load_previous("suffolk")[1].iloc[0]["Site ID"] == "SITE-000002"


def test_current_pointer_and_previous_run(isolated_paths, store):
    assert runs.load_previous("suffolk") == (None, None)
    make_run(store, "R1")
    (runs.run_dir("suffolk", "R1") / "buildings.csv").unlink()                         # a run in the older file layout
    (runs.run_dir("suffolk", "R1") / "critical_buildings.csv").write_text("Building ID,Site ID\nBLDG-000001,SITE-000001\n")
    runs.set_current("suffolk", "R1")
    rid, df = runs.load_previous("suffolk")
    assert rid == "R1" and df.iloc[0]["Site ID"] == "SITE-000001"


def test_start_and_fail_leave_no_partial_output(isolated_paths, store):
    from critical_buildings.control.store import ControlStore  # noqa: F401
    snap_rules = type("R", (), {"version_id": 1, "label": "v1-x"})()
    ctx = runs.start(store, "suffolk", "dev", snap_rules, "trial")
    assert ctx.dir.exists() and store.get_run(ctx.run_id)["status"] == "running"
    runs.fail(store, ctx, ValueError("boom"))
    run = store.get_run(ctx.run_id)
    assert run["status"] == "failed" and "ValueError: boom" in run["error"] and not ctx.dir.exists()
    with pytest.raises(ControlError, match="use the release command"):
        runs.start(store, "suffolk", "release", snap_rules)


# ------------------------------------------------------------------ retention
def make_snapshots(source, names, latest):
    for n in names:
        (paths.BRONZE / source / n).mkdir(parents=True)
        (paths.BRONZE / source / n / "f.csv").write_text(n)
    (paths.BRONZE / source / "LATEST.json").write_text(json.dumps({"snapshot": latest}))


def planned(store, **retention_overrides):
    with config.use({"retention": retention_overrides}):
        return retention.plan(store, today=NOW)


def test_nothing_to_do_within_the_policy(isolated_paths, store):
    make_snapshots("dfe_gias", ["2026-09-01", "2026-10-01"], "2026-10-01")
    make_run(store, "R1")
    assert planned(store, bronze_keep=3, gold_keep=5) == []


def test_old_bronze_snapshots_are_archived_but_latest_and_newest_are_kept(isolated_paths, store):
    make_snapshots("dfe_gias", ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"], "2026-04-01")
    got = [(a.layer, a.kind, a.meta["snapshot"]) for a in planned(store, bronze_keep=2)]
    assert got == [("bronze", "archive", "2026-01-01"), ("bronze", "archive", "2026-02-01")]


def test_bronze_keep_zero_archives_everything_except_latest(isolated_paths, store):
    make_snapshots("dfe_gias", ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"], "2026-04-01")
    got = [a.meta["snapshot"] for a in planned(store, bronze_keep=0)]
    assert got == ["2026-01-01", "2026-02-01", "2026-03-01"]                # LATEST is still never touched


def test_bronze_keep_zero_still_spares_what_a_release_depends_on(isolated_paths, store):
    make_snapshots("dfe_gias", ["2026-01-01", "2026-02-01", "2026-04-01"], "2026-04-01")
    make_run(store, "R1", days_ago=60, bronze=[{"source": "dfe_gias", "area": None, "snapshot": "2026-01-01"}])
    runs.set_current("suffolk", "R1")
    runs.release(store, "suffolk", "v1", "R1")
    assert [a.meta["snapshot"] for a in planned(store, bronze_keep=0) if a.layer == "bronze"] == ["2026-02-01"]


def test_whatever_a_release_depends_on_is_never_planned_for_removal(isolated_paths, store):
    make_snapshots("dfe_gias", ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"], "2026-04-01")
    for k in ("k1", "k2", "k3", "k4"):
        d = paths.SILVER / "suffolk" / "classified" / k
        d.mkdir(parents=True)
        (d / "_meta.json").write_text("{}")
        os.utime(d, (time.time() - (10 - int(k[1])) * 3600,) * 2)          # k1 oldest
    make_run(store, "R1", days_ago=60, silver_key="k1", bronze=[{"source": "dfe_gias", "area": None, "snapshot": "2026-01-01"}])
    make_run(store, "R2", days_ago=1)
    runs.set_current("suffolk", "R2")
    runs.release(store, "suffolk", "v1", "R1")
    acts = planned(store, bronze_keep=2, silver_keep=2, gold_keep=1)
    removed = {(a.layer, a.path.name) for a in acts}
    assert ("bronze", "2026-01-01") not in removed          # pinned by the release
    assert ("bronze", "2026-02-01") in removed
    assert ("silver", "k1") not in removed and ("silver", "k2") in removed       # k1 pinned, k2 old
    assert not any(a.layer == "gold" for a in acts)         # R1 is released, R2 is CURRENT


def test_gold_runs_beyond_the_keep_count_are_archived_and_old_dev_runs_deleted(isolated_paths, store):
    for rid, days, typ in (("R5", 1, "candidate"), ("R4", 2, "dev"), ("R3", 3, "candidate"), ("R2", 4, "dev"), ("R1", 5, "candidate")):
        make_run(store, rid, days_ago=days, run_type=typ)
    runs.set_current("suffolk", "R5")
    acts = {a.meta["run_id"]: a.kind for a in planned(store, gold_keep=3, dev_run_days=14) if a.layer == "gold"}
    assert acts == {"R2": "delete", "R1": "archive"}        # R3 candidate is rank 2 (kept); R2 dev and R1 beyond keep
    old_dev = {a.meta["run_id"]: a.kind for a in planned(store, gold_keep=10, dev_run_days=3) if a.layer == "gold"}
    assert old_dev == {"R2": "delete"}                      # R4 is only 2 days old; R2 is 4 days old


def test_apply_archives_with_verification_then_restore_brings_it_back(isolated_paths, store):
    make_run(store, "R1", days_ago=9)
    make_run(store, "R2", days_ago=1)
    runs.set_current("suffolk", "R2")
    acts = planned(store, gold_keep=1)
    assert [a.meta["run_id"] for a in acts] == ["R1"]
    retention.apply(store, acts)
    zip_path = paths.ARCHIVE / "gold" / "suffolk" / "R1.zip"
    assert zip_path.exists() and zip_path.with_suffix(".json").exists()
    side = json.loads(zip_path.with_suffix(".json").read_text())
    assert side["sha256"] == lineage.sha256_file(zip_path) and side["run_id"] == "R1"
    assert not runs.run_dir("suffolk", "R1").exists()
    assert store.get_run("R1")["status"] == "archived" and store.get_run("R1")["archive_path"]
    retention.restore(store, "gold", "R1")
    assert (runs.run_dir("suffolk", "R1") / "buildings.csv").exists()
    assert store.get_run("R1")["status"] == "succeeded"
    with pytest.raises(FileExistsError):
        retention.restore(store, "gold", "R1")


def test_applying_a_dev_deletion_removes_the_folder_without_an_archive(isolated_paths, store):
    make_run(store, "R1", days_ago=9, run_type="dev")
    make_run(store, "R2", days_ago=1)
    runs.set_current("suffolk", "R2")
    retention.apply(store, planned(store, gold_keep=1))
    assert not runs.run_dir("suffolk", "R1").exists() and not (paths.ARCHIVE / "gold").exists()
    assert store.get_run("R1")["status"] == "archived" and store.get_run("R1")["archive_path"] == ""


def test_a_run_can_pin_an_older_published_rule_set(seeded_store):
    """store.ruleset(n) returns exactly version n, so an old run can be reproduced with its rules."""
    s = seeded_store
    s.publish("ann", "v1")
    rows = [{**r, "active": 1} for r in s.draft_snapshot()["rules"]]
    rows[0]["priority"] = 10
    s.apply_table("rule", rows, "ann", "change")
    s.publish("ann", "v2")
    assert s.ruleset().version_id == 2 and s.ruleset().priorities[1] == 10
    pinned = s.ruleset(1)
    assert pinned.version_id == 1 and pinned.priorities[1] == 50 and pinned.label.startswith("v1-")
    with pytest.raises(ControlError):
        s.ruleset(99)


def test_a_run_left_running_by_a_dead_process_can_be_closed(isolated_paths, store):
    make_run(store, "R1", status="running")
    assert runs.abandon(store, "suffolk", "R1") is True
    row = store.get_run("R1")
    assert row["status"] == "failed" and "stopped" in row["error"] and not runs.run_dir("suffolk", "R1").exists()
    assert runs.abandon(store, "suffolk", "R1") is False            # already closed
