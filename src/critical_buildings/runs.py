"""Run lifecycle: one immutable gold folder per run, a CURRENT pointer, the run registry, and releases.

    data/gold/<area>/<run_id>/        everything one run produced (+ lineage.json). Never modified after the run.
    data/gold/<area>/CURRENT.json     {"run_id": ...} - the latest successful run (what the next run diffs against)
    data/published/<area>/<release>/       a copy of one run's deliverables, kept permanently
"""
import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from critical_buildings import config, lineage, locks, paths
from critical_buildings.control.store import ControlError, current_user, now
from critical_buildings.publish import DELIVERABLES, find_output

log = logging.getLogger(__name__)


@dataclass
class RunContext:
    run_id: str
    area: str
    run_type: str
    started_at: str
    dir: Path
    previous_run_id: str = None


# ------------------------------------------------------------------ locations and pointers
def gold_dir(area):
    return paths.GOLD / area


def run_dir(area, run_id):
    return gold_dir(area) / run_id


def current_run_id(area):
    p = gold_dir(area) / "CURRENT.json"
    return json.loads(p.read_text(encoding="utf-8"))["run_id"] if p.exists() else None


def set_current(area, run_id):
    gold_dir(area).mkdir(parents=True, exist_ok=True)
    tmp = gold_dir(area) / "CURRENT.json.tmp"
    tmp.write_text(json.dumps({"run_id": run_id}, indent=2), encoding="utf-8")
    os.replace(tmp, gold_dir(area) / "CURRENT.json")          # all or nothing: the pointer is never half-written


def load_previous(area):
    """(run_id, buildings DataFrame) of the CURRENT run, or (None, None) on a first run / if its folder was archived."""
    rid = current_run_id(area)
    if not rid:
        return None, None
    csv = find_output(run_dir(area, rid), "buildings")
    if csv is None:
        return None, None
    return rid, pd.read_csv(csv, dtype=str).fillna("")


# ------------------------------------------------------------------ lifecycle
def new_run_id(area):
    return dt.datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + area  # noqa: DTZ005 - local time is intended


def start(store, area, run_type, rules, notes="", previous_run_id=None, user=None):
    if run_type not in ("dev", "candidate"):
        raise ControlError("A run is 'dev' or 'candidate'; use the release command to make a release.")
    run_id = new_run_id(area)
    dest = run_dir(area, run_id)
    dest.mkdir(parents=True)
    ctx = RunContext(run_id, area, run_type, now(), dest, previous_run_id)
    inputs = lineage.bronze_inputs(area)
    store.insert_run({
        "run_id": run_id, "area": area, "run_type": run_type, "status": "running", "started_at": ctx.started_at,
        "started_by": user or current_user(), "rule_version_id": rules.version_id, "rule_version_label": rules.label,
        "config_hash": config.settings_hash(), "bronze_key": paths.bronze_key(area),
        "bronze_snapshots_json": json.dumps([{"source": i["source"], "area": i["area"], "snapshot": i["snapshot"]} for i in inputs]),
        "git_commit": lineage.git_info()["commit"], "previous_run_id": previous_run_id, "notes": notes})
    return ctx


def finish(store, ctx, rules, silver_key, silver_reused, gold):
    area_hash = lineage.hashlib.sha256(json.dumps(config.load_area(ctx.area), sort_keys=True).encode()).hexdigest()[:12]
    lin = lineage.build_lineage(ctx, rules, silver_key, silver_reused, gold, ctx.dir, DELIVERABLES,
                                config.settings_hash(), area_hash)
    lin["finished_at"] = now()
    lineage.write_lineage(ctx.dir, lin)
    store.finish_run(ctx.run_id, gold.identities, gold.changes,       # IDs are only minted by a run that succeeded, atomically with its status
                     status="succeeded", finished_at=lin["finished_at"], silver_key=silver_key,
                     buildings=lin["gold"]["buildings"], sites=lin["gold"]["sites"],
                     output_hash=lin["outputs"]["canonical_buildings_sha256"])
    set_current(ctx.area, ctx.run_id)
    _backup_state(store)                                      # the IDs this run minted are now only in state.db
    return lin


def _backup_state(store, label=""):
    """Copy the state database aside (see ControlStore.backup_state). A failed backup is reported, never fatal."""
    try:
        store.backup_state(label)
    except (OSError, sqlite3.Error) as e:
        log.warning("Could not back up the run history and ID registries (data/state.db): %s", e)


def fail(store, ctx, exc):
    store.update_run(ctx.run_id, status="failed", finished_at=now(), error=f"{type(exc).__name__}: {exc}"[:2000])
    shutil.rmtree(ctx.dir, ignore_errors=True)       # a failed run leaves no half-written output behind


# ------------------------------------------------------------------ releases
def next_release_name(store, today=None):
    """v<YYYY>.<MM>.<n>: n counts releases already made in that month."""
    today = today or dt.date.today()  # noqa: DTZ011 - local time is intended
    prefix = f"v{today.year}.{today.month:02d}."
    existing = [r for r in store.releases_df()["release"] if r.startswith(prefix)]
    return f"{prefix}{len(existing)}"


def release(store, area, name=None, run_id=None, notes="", user=None):
    """Copy one run's deliverables to data/published/<area>/<name>/ (immutable) and record the release."""
    with locks.pipeline_lock("release"):
        return _release(store, area, name, run_id, notes, user)


def _release(store, area, name, run_id, notes, user):
    run_id = run_id or current_run_id(area)
    if not run_id:
        raise ControlError("No successful run to release. Run: python -m critical_buildings run")
    run = store.get_run(run_id)
    if run is None or run["status"] != "succeeded":
        raise ControlError(f"Run {run_id} is not a successful run (status: {run['status'] if run else 'unknown'}).")
    src = run_dir(area, run_id)
    if not src.exists():
        raise ControlError(f"The gold folder for {run_id} is no longer on disk (archived?). Restore it first.")
    name = name or next_release_name(store)
    dest = paths.PUBLISHED / area / name
    if dest.exists() or name in set(store.releases_df()["release"]):
        raise ControlError(f"Release {name} already exists. Releases are immutable; choose a new name.")
    dest.mkdir(parents=True)
    lin = json.loads((src / "lineage.json").read_text(encoding="utf-8"))
    expected = {f["name"]: f["sha256"] for f in lin["outputs"]["files"]}
    for fname in list(expected) + ["lineage.json"]:
        shutil.copy2(src / fname, dest / fname)
    for fname, digest in expected.items():                       # prove the copy is faithful
        if lineage.sha256_file(dest / fname) != digest:
            shutil.rmtree(dest, ignore_errors=True)
            raise ControlError(f"Checksum mismatch while copying {fname}; release aborted.")
    (dest / "RELEASE.json").write_text(json.dumps({
        "release": name, "area": area, "run_id": run_id, "released_at": now(), "released_by": user or current_user(),
        "rule_version": lin["rules"]["label"], "notes": notes}, indent=2), encoding="utf-8")
    store.insert_release(name, area, run_id, user or current_user(), notes)
    _backup_state(store, label=name)
    return dest


def abandon(store, area, run_id, reason="The process stopped before the run finished"):
    """Close a run that was left 'running' because its process died (e.g. the PC slept or a window was closed).
    Marks it failed and removes its half-written folder. Does nothing for a run that is not 'running'."""
    row = store.get_run(run_id)
    if row is None or row["status"] != "running":
        return False
    store.update_run(run_id, status="failed", finished_at=now(), error=reason)
    shutil.rmtree(run_dir(area, run_id), ignore_errors=True)
    return True
