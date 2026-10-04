"""Retention and archiving across the medallion layers (dry-run by default).

Policy (settings.yml -> retention):
    bronze   keep the latest N snapshots per source; older ones are zipped into archive/bronze/
    silver   keep the latest N builds per area; older ones are deleted (reproducible from bronze + rules)
    gold     keep the latest N runs per area; older ones are zipped into archive/gold/ (dev runs are deleted)
Never touched: LATEST/CURRENT pointers, and anything a release depends on (its run, bronze snapshots, silver build).
Releases in data/published/ are kept forever.
"""
import datetime as dt
import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from critical_buildings import config, lineage, locks, paths, runs

SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")


@dataclass
class Action:
    layer: str            # bronze | silver | gold
    path: Path
    kind: str             # archive | delete
    reason: str
    bytes: int = 0
    meta: dict = field(default_factory=dict)


def _size(p):
    p = Path(p)
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else (p.stat().st_size if p.exists() else 0)


def protected(store):
    """What releases depend on: run ids, (bronze dir, snapshot) pairs and silver keys."""
    runs_, bronze, silver, conformed = set(), set(), set(), set()
    for rel in store.releases_df().itertuples():
        run = store.get_run(rel.run_id)
        runs_.add(rel.run_id)
        if not run:
            continue
        for s in json.loads(run["bronze_snapshots_json"] or "[]"):
            bronze.add((f"{s['source']}__{s['area']}" if s.get("area") else s["source"], s["snapshot"]))
            if not s.get("area"):       # runs made before a source was area-scoped; its folder may have been migrated
                bronze.add((f"{s['source']}__{run['area']}", s["snapshot"]))
        if run["silver_key"]:
            silver.add(run["silver_key"])
        if run["bronze_key"]:
            conformed.add(run["bronze_key"])
    return {"runs": runs_, "bronze": bronze, "silver": silver, "conformed": conformed}


def plan(store, today=None):
    cfg = config.get().retention
    today = today or dt.datetime.now(dt.timezone.utc)
    prot = protected(store)
    actions = []

    # ---- bronze: per source folder
    for src_dir in sorted(p for p in paths.BRONZE.glob("*") if p.is_dir()):
        snaps = sorted(d.name for d in src_dir.iterdir() if d.is_dir() and SNAPSHOT_RE.match(d.name))
        latest = (json.loads((src_dir / "LATEST.json").read_text(encoding="utf-8"))["snapshot"]
                  if (src_dir / "LATEST.json").exists() else None)
        newest = set(snaps[-cfg.bronze_keep:]) if cfg.bronze_keep else set()     # [-0:] would be every snapshot
        keep = newest | ({latest} if latest else set())
        for s in snaps:
            if s in keep:
                continue
            if (src_dir.name, s) in prot["bronze"]:
                continue
            actions.append(Action("bronze", src_dir / s, "archive", f"older than the latest {cfg.bronze_keep} snapshots",
                                  _size(src_dir / s), {"source": src_dir.name, "snapshot": s}))

    # ---- silver: per area; classified builds and conformed tables
    for area_dir in sorted(p for p in paths.SILVER.glob("*") if p.is_dir()):
        for sub, prot_keys in (("classified", prot["silver"]), ("conformed", prot["conformed"])):
            d = area_dir / sub
            if not d.exists():
                continue
            builds = sorted((b for b in d.iterdir() if b.is_dir() and not b.name.startswith("_")),
                            key=lambda b: b.stat().st_mtime)
            for b in builds[:-cfg.silver_keep] if cfg.silver_keep else builds:
                if b.name in prot_keys:
                    continue
                actions.append(Action("silver", b, "delete", f"older than the latest {cfg.silver_keep} {sub} builds "
                                      "(reproducible from bronze + rules)", _size(b)))

    # ---- gold: per area, by the run registry
    for area_dir in sorted(p for p in paths.GOLD.glob("*") if p.is_dir()):
        area = area_dir.name
        df = store.runs_df(area)
        df = df[(df.status == "succeeded")].sort_values("started_at", ascending=False).reset_index(drop=True)
        current = runs.current_run_id(area)
        for rank, r in df.iterrows():
            d = area_dir / r.run_id
            if not d.exists() or r.run_id == current or r.run_id in prot["runs"]:
                continue
            age = (today - dt.datetime.fromisoformat(r.started_at)).days
            if rank >= cfg.gold_keep:
                reason = f"older than the latest {cfg.gold_keep} runs"
            elif r.run_type == "dev" and age > cfg.dev_run_days:
                reason = f"dev run older than {cfg.dev_run_days} days"
            else:
                continue
            actions.append(Action("gold", d, "delete" if r.run_type == "dev" else "archive", reason, _size(d),
                                  {"run_id": r.run_id, "area": area, "run_type": r.run_type}))
    return actions


def _zip_dir(src, dest_zip):
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path(src).rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src).as_posix())
    with zipfile.ZipFile(dest_zip) as z:
        if z.testzip() is not None:
            raise RuntimeError(f"Archive verification failed for {dest_zip}")
    return {"zip": dest_zip.name, "sha256": lineage.sha256_file(dest_zip), "bytes": dest_zip.stat().st_size}


def apply(store, actions, archive_dir=None):
    """Carry out a plan. Archives are verified before the original is removed. Cannot overlap a run or fetch."""
    with locks.pipeline_lock("archive"):
        return _apply(store, actions, archive_dir)


def _apply(store, actions, archive_dir):
    archive_dir = Path(archive_dir or paths.ARCHIVE)
    done = []
    for a in actions:
        if a.kind == "archive":
            rel = a.path.relative_to(paths.DATA)
            dest = archive_dir / rel.parent / f"{a.path.name}.zip"
            info = _zip_dir(a.path, dest)
            (dest.with_suffix(".json")).write_text(json.dumps({**info, "original": rel.as_posix(), "archived_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "reason": a.reason, **a.meta}, indent=2), encoding="utf-8")
            shutil.rmtree(a.path)
            if a.layer == "gold":
                store.update_run(a.meta["run_id"], status="archived", archive_path=str(dest))
        else:
            shutil.rmtree(a.path)
            if a.layer == "gold":
                store.update_run(a.meta["run_id"], status="archived", archive_path="")   # deleted, not archived
        done.append(a)
    return done


def restore(store, layer, name, archive_dir=None, area=paths.DEFAULT_AREA):
    """Unzip an archived gold run (name = run id) or bronze snapshot (name = '<source dir>/<snapshot>')."""
    with locks.pipeline_lock("restore"):
        return _restore(store, layer, name, archive_dir, area)


def _restore(store, layer, name, archive_dir, area):
    archive_dir = Path(archive_dir or paths.ARCHIVE)
    if layer == "gold":
        zp, dest = archive_dir / "gold" / area / f"{name}.zip", runs.run_dir(area, name)
    elif layer == "bronze":
        zp, dest = archive_dir / "bronze" / f"{name}.zip", paths.BRONZE / name
    else:
        raise ValueError("layer must be gold or bronze")
    if not zp.exists():
        raise FileNotFoundError(f"No archive at {zp}")
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    with zipfile.ZipFile(zp) as z:
        z.extractall(dest)
    if layer == "gold":
        store.update_run(name, status="succeeded", archive_path=None)
    return dest
