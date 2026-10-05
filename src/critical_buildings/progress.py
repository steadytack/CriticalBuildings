"""What the pipeline is doing, as it does it.

Stage code reports through four calls and never needs to know who is listening:

    with progress.stage("gold"):            # a named stage of the pipeline (see `pipeline.stages` in settings.yml)
        progress.step("Merging OS and OSM")  # what is happening now
        progress.note("matched", 1204)       # a count worth keeping (shown in the UI, kept with the run)
        progress.bar(i, n)                   # optional progress inside a step

With no run active every call is a cheap no-op (plain log line), so tests and library use are unaffected.

While a run is active, events are appended to data/logs/<run_id>.jsonl (the UI tails it live and shows it as a timeline
afterwards; it survives a failed run, whose output folder is deleted) and each stage's status is stored in the run registry.
"""
import contextlib
import json
import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime

from critical_buildings import paths

log = logging.getLogger("critical_buildings.progress")
_run = None          # the active run (a _Run), or None


def log_path(run_id):
    return paths.DATA / "logs" / f"{run_id}.jsonl"


def read_events(run_id):
    """All events of a run, oldest first (empty list if there is no log)."""
    p = log_path(run_id)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):          # a half-written last line while the run is still going
            out.append(json.loads(line))
    return out


class _Run:
    def __init__(self, run_id, store):
        self.run_id, self.store = run_id, store
        self.stage_id = None
        self.t0 = time.time()
        self.stage_t0 = {}
        self.notes = {}              # stage -> {name: value}
        self.path = log_path(run_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind, **fields):
        event = {"t": datetime.now(UTC).isoformat(timespec="milliseconds"), "elapsed_s": round(time.time() - self.t0, 1),
                 "kind": kind, "stage": self.stage_id, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")


@contextmanager
def run(run_id, store):
    """Make `run_id` the active run for the duration of the block."""
    global _run
    previous, _run = _run, _Run(run_id, store)
    try:
        _run.emit("run_start")
        yield _run
        _run.emit("run_end", status="succeeded")
    except BaseException as exc:
        _run.emit("run_end", status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
        raise
    finally:
        _run = previous


@contextmanager
def stage(stage_id):
    r = _run
    if r is None:
        yield
        return
    r.stage_id = stage_id
    r.stage_t0[stage_id] = time.time()
    r.notes[stage_id] = {}
    started = datetime.now(UTC).isoformat(timespec="seconds")
    r.store.save_stage(r.run_id, stage_id, status="running", started_at=started, finished_at=None, detail_json="{}")
    r.emit("stage_start")
    try:
        yield
    except BaseException as exc:
        _finish(r, stage_id, started, "failed", f"{type(exc).__name__}: {exc}"[:500])
        raise
    _finish(r, stage_id, started, "succeeded")


def _finish(r, stage_id, started, status, error=None):
    detail = dict(r.notes.get(stage_id, {}), seconds=round(time.time() - r.stage_t0[stage_id], 1))
    if error:
        detail["error"] = error
    r.store.save_stage(r.run_id, stage_id, status=status, started_at=started,
                       finished_at=datetime.now(UTC).isoformat(timespec="seconds"), detail_json=json.dumps(detail, default=str))
    r.emit("stage_end", status=status)
    r.stage_id = None


def step(message):
    """What is happening now."""
    log.info("%s", message)
    if _run:
        _run.emit("step", message=message)


def note(name, value):
    """Record a count or fact for the current stage (kept in the run registry and lineage)."""
    log.info("  %s: %s", name, value)
    if _run and _run.stage_id:
        _run.notes[_run.stage_id][name] = value
        _run.emit("note", name=name, value=value)


def warn(message):
    log.warning("%s", message)
    if _run:
        _run.emit("warning", message=message)


def bar(done, total):
    """Progress inside the current step (done of total)."""
    if _run:
        _run.emit("bar", done=done, total=total)


def stage_summary(run_id, store):
    """Stage rows for a run as plain dicts, with each stage's notes (from the registry)."""
    out = []
    for row in store.stages_df(run_id).to_dict("records"):
        row["detail"] = json.loads(row.pop("detail_json") or "{}")
        out.append(row)
    return out
