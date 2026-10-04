"""Stage and step reporting: events on disk, stage rows in the registry, no-op when no run is active."""

import pytest

from critical_buildings import config, progress
from critical_buildings.control.store import now


def _run_row(store, run_id):
    store.insert_run({"run_id": run_id, "area": "a", "run_type": "dev", "status": "running", "started_at": now(), "started_by": "t"})


def test_calls_are_harmless_without_an_active_run():
    with progress.stage("gold"):
        progress.step("x")
        progress.note("n", 1)
        progress.bar(1, 2)


def test_a_run_records_events_and_stage_rows(isolated_paths, store):
    _run_row(store, "R1")
    with progress.run("R1", store):
        with progress.stage("silver"):
            progress.step("Reading")
            progress.note("features", 12)
            progress.bar(1, 4)
        with progress.stage("gold"):
            progress.warn("careful")
    kinds = [e["kind"] for e in progress.read_events("R1")]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert {"stage_start", "stage_end", "step", "note", "bar", "warning"} <= set(kinds)
    rows = {r["stage"]: r for r in progress.stage_summary("R1", store)}
    assert rows["silver"]["status"] == "succeeded" and rows["silver"]["detail"]["features"] == 12
    assert rows["silver"]["detail"]["seconds"] >= 0 and rows["gold"]["finished_at"]


def test_a_failing_stage_is_recorded_and_the_error_propagates(isolated_paths, store):
    _run_row(store, "R2")
    with pytest.raises(ValueError, match="boom"), progress.run("R2", store), progress.stage("gold"):
        raise ValueError("boom")
    row = progress.stage_summary("R2", store)[0]
    assert row["status"] == "failed" and "boom" in row["detail"]["error"]
    assert progress.read_events("R2")[-1]["status"] == "failed"


def test_a_half_written_last_line_is_ignored(isolated_paths, store):
    _run_row(store, "R3")
    with progress.run("R3", store):
        progress.step("one")
    with progress.log_path("R3").open("a", encoding="utf-8") as f:
        f.write('{"kind": "ste')
    assert all("kind" in e for e in progress.read_events("R3"))


def test_the_configured_stages_all_resolve_to_functions():
    import importlib
    stages = config.get().pipeline.stages
    assert [s["id"] for s in stages] == ["fetch", "silver", "gold", "publish"]
    for s in stages:
        if s.get("function"):
            module, _, name = s["function"].partition(":")
            assert callable(getattr(importlib.import_module(module), name))


# ------------------------------------------------------------------ folding events for the UI
def test_summarise_events_tracks_stages_steps_notes_and_progress():
    from critical_buildings.ui import jobs
    ev = [{"kind": "run_start", "elapsed_s": 0, "stage": None},
          {"kind": "stage_start", "elapsed_s": 1, "stage": "silver"},
          {"kind": "step", "elapsed_s": 1.2, "stage": "silver", "message": "Reading"},
          {"kind": "note", "elapsed_s": 2, "stage": "silver", "name": "features", "value": 5},
          {"kind": "bar", "elapsed_s": 2.5, "stage": "silver", "done": 3, "total": 10},
          {"kind": "warning", "elapsed_s": 3, "stage": "silver", "message": "careful"},
          {"kind": "stage_end", "elapsed_s": 4, "stage": "silver", "status": "succeeded"},
          {"kind": "stage_start", "elapsed_s": 4, "stage": "gold"}]
    s = jobs.summarise_events(ev, ["silver", "gold", "publish"])
    assert s["state"] == "running" and s["warnings"] == ["careful"]
    assert s["stages"]["silver"] == {"status": "succeeded", "seconds": 3, "steps": [(1.2, "Reading")], "notes": {"features": 5}}
    assert s["stages"]["gold"]["status"] == "running" and s["stages"]["publish"]["status"] == "pending"
    assert s["current_step"] == "Reading"
    ev.append({"kind": "run_end", "elapsed_s": 9, "stage": None, "status": "failed"})
    assert jobs.summarise_events(ev, ["silver"])["state"] == "failed"


def test_a_background_job_reports_its_result(tmp_path, monkeypatch):
    import sys
    import time

    from critical_buildings import paths
    from critical_buildings.ui import jobs
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    monkeypatch.setattr(jobs, "cli_command", lambda *a: [sys.executable, "-c", "print('hello'); raise SystemExit(3)"])
    jid = jobs.start_job("Demo", "x")
    for _ in range(50):
        status = jobs.job_status(jid)
        if status["state"] != "running":
            break
        time.sleep(0.2)
    assert status["state"] == "failed" and status["exit_code"] == 3 and "hello" in status["output"]
    assert jobs.latest_job()["id"] == jid


def test_flow_graph_colours_stages_by_status_and_flags_stale_sources():
    from critical_buildings.ui import flow
    assert flow.cadence_days("monthly") == 35 and flow.cadence_days("six-monthly (OS cycle)") == 190 and flow.cadence_days("ad hoc") is None
    from datetime import date
    assert flow.source_status("2026-01-01T00:00:00+00:00", "monthly", today=date(2026, 3, 1)) == (59, "stale")
    assert flow.source_status("2026-02-20T00:00:00+00:00", "monthly", today=date(2026, 3, 1)) == (9, "succeeded")
    assert flow.source_status(None, "monthly") == (None, None)
    stages = [{"id": "fetch", "label": "Fetch", "layer": "bronze"}, {"id": "gold", "label": "Gold", "layer": "gold"}]
    dot = flow.flow_dot([{"id": "a", "name": "Src A", "age": 3, "status": "stale"}], stages,
                        {"gold": {"status": "failed", "detail": {"seconds": 12}}}, releases=2, latest_release="v1")
    assert "src_a -> st_fetch" in dot and "st_fetch -> st_gold" in dot and flow.PALETTE["failed"][1] in dot and "2 made" in dot
