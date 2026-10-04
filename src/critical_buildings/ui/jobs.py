"""Background jobs for the UI: start a pipeline command that keeps running if the page is reloaded, and read its progress.

A job is `python -m critical_buildings <args>` run in a detached process. Files in data/logs/jobs/:
    <id>.json   what was started (label, command, start time, process id)
    <id>.out    everything the command printed
    <id>.exit   written when it ends, holds the exit code (no file and no live process = interrupted)
No Streamlit here, so it can be tested.
"""
import json
import os
import subprocess
import sys
from datetime import datetime

from critical_buildings import paths, progress
from critical_buildings.locks import pid_alive

_WRAPPER = ("import subprocess, sys; out = open(sys.argv[1], 'w', encoding='utf-8'); "
            "p = subprocess.run(sys.argv[3:], stdout=out, stderr=subprocess.STDOUT); out.close(); "
            "open(sys.argv[2], 'w').write(str(p.returncode))")
NOISE = ("gdalplugins", "The specified procedure could not be found")     # harmless system-GDAL probing on some PCs


def jobs_dir():
    return paths.DATA / "logs" / "jobs"


def cli_command(*args):
    """The command line for `python -m critical_buildings <args>`, using the interpreter running the UI."""
    return [sys.executable, "-m", "critical_buildings", *[str(a) for a in args]]


def clean_output(text):
    """Command output without the harmless GDAL plugin noise lines and runs of blank lines."""
    keep = [ln for ln in text.splitlines() if not any(n in ln for n in NOISE)]
    return "\n".join(ln for i, ln in enumerate(keep) if ln.strip() or (i and keep[i - 1].strip())).strip()


def start_job(label, *cli_args):
    """Start a command in the background. Returns the job id."""
    d = jobs_dir()
    d.mkdir(parents=True, exist_ok=True)
    job_id = datetime.now().strftime("%Y%m%dT%H%M%S")  # noqa: DTZ005 - local time is intended
    out, exit_file = d / f"{job_id}.out", d / f"{job_id}.exit"
    flags = (0x08000000 | 0x00000200) if os.name == "nt" else 0       # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP (no stray console to close)
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", _WRAPPER, str(out), str(exit_file), *cli_command(*cli_args)],
                            cwd=paths.ROOT, env=env, creationflags=flags, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (d / f"{job_id}.json").write_text(json.dumps({
        "id": job_id, "label": label, "args": [str(a) for a in cli_args], "pid": proc.pid,
        "started": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")  # noqa: DTZ005 - local time is intended
    return job_id


def job_status(job_id, tail=40):
    """{id, label, state (running | succeeded | failed | interrupted), exit_code, output (last lines), started}."""
    d = jobs_dir()
    meta = json.loads((d / f"{job_id}.json").read_text(encoding="utf-8"))
    exit_file, out = d / f"{job_id}.exit", d / f"{job_id}.out"
    code = int(exit_file.read_text().strip() or -1) if exit_file.exists() else None
    if code is not None:
        state = "succeeded" if code == 0 else "failed"
    else:
        state = "running" if pid_alive(meta.get("pid")) else "interrupted"
    text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
    lines = clean_output(text).splitlines()
    return {**meta, "state": state, "exit_code": code, "output": "\n".join(lines[-tail:]), "lines": len(lines)}


def latest_job():
    """The most recently started job's status, or None."""
    d = jobs_dir()
    if not d.exists():
        return None
    ids = sorted(p.stem for p in d.glob("*.json"))
    return job_status(ids[-1]) if ids else None


def run_id_of(job):
    """The run a Build job is producing: the newest progress log started at or after the job."""
    if not job or not paths.DATA.joinpath("logs").exists():
        return None
    started = datetime.fromisoformat(job["started"]).timestamp() - 2
    logs = [p for p in paths.DATA.joinpath("logs").glob("*.jsonl") if p.stat().st_mtime >= started]
    return max(logs, key=lambda p: p.stat().st_mtime).stem if logs else None


def summarise_events(events, stage_ids):
    """Fold a run's events into what the UI shows.

    Returns {state, stages: {id: {status, seconds, steps: [(elapsed_s, message)], notes: {}}}, current_step, bar, warnings, elapsed_s}.
    """
    stages = {s: {"status": "pending", "seconds": None, "steps": [], "notes": {}, "_t0": None} for s in stage_ids}
    state, current, bar, warnings, elapsed = "running", None, None, [], 0
    for e in events:
        elapsed = e.get("elapsed_s", elapsed)
        kind, sid = e["kind"], e.get("stage")
        st = stages.setdefault(sid, {"status": "pending", "seconds": None, "steps": [], "notes": {}, "_t0": None}) if sid else None
        if kind == "stage_start":
            st["status"], st["_t0"], bar = "running", e["elapsed_s"], None
        elif kind == "stage_end":
            st["status"], st["seconds"] = e["status"], round(e["elapsed_s"] - (st["_t0"] or 0), 1)
        elif kind == "step":
            current, bar = e["message"], None
            if st is not None:
                st["steps"].append((e["elapsed_s"], e["message"]))
        elif kind == "note" and st is not None:
            st["notes"][e["name"]] = e["value"]
        elif kind == "bar":
            bar = (e["done"], e["total"])
        elif kind == "warning":
            warnings.append(e["message"])
        elif kind == "run_end":
            state = e["status"]
    for st in stages.values():
        st.pop("_t0", None)
    return {"state": state, "stages": stages, "current_step": current, "bar": bar, "warnings": warnings, "elapsed_s": elapsed}


def events_for(run_id):
    return progress.read_events(run_id) if run_id else []
