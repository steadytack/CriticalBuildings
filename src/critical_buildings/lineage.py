"""Lineage: a machine-readable record of exactly what produced a run (inputs, rules, settings, code, outputs).

Written as lineage.json into every gold run folder and copied into every release. Record-level lineage lives in the
data itself (Source Feature ID, rule refs, Rule Version, Run Date, Run ID).
"""
import hashlib
import json
import subprocess
from pathlib import Path

import pandas as pd

from critical_buildings import __version__, paths
from critical_buildings.bronze import manifest

VOLATILE = ["Run Date", "Last Edit Date", "Rule Version", "Run ID"]


def git_info():
    """Commit hash of the code that ran, and whether there were uncommitted changes."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=paths.ROOT, capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "config"], cwd=paths.ROOT, capture_output=True,
                               text=True, timeout=10)
        if commit.returncode != 0:
            return {"commit": "", "dirty": None}
        return {"commit": commit.stdout.strip(), "dirty": bool(dirty.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"commit": "", "dirty": None}


def sha256_file(path):
    return manifest.sha256_file(path)


def file_entry(path):
    p = Path(path)
    return {"name": p.name, "bytes": p.stat().st_size, "sha256": sha256_file(p)}


def canonical_hash(csv_path, key="Building ID", drop=VOLATILE):
    """sha256 of a CSV after sorting by key, dropping volatile columns and fixing column order (content, not formatting)."""
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    df = df.drop(columns=[c for c in drop if c in df.columns]).sort_values(key).reset_index(drop=True)
    df = df[sorted(df.columns)]
    return hashlib.sha256(df.to_csv(index=False).encode("utf-8")).hexdigest()


def bronze_inputs():
    """The snapshots currently marked LATEST for every source, with file checksums."""
    out = []
    for m in manifest.all_manifests():
        out.append({"source": m["source"], "area": m.get("area"), "snapshot": m["snapshot"], "retrieved_at": m["retrieved_at"],
                    "licence": m["licence"], "files": [{"name": f["name"], "sha256": f["sha256"], "bytes": f["bytes"]} for f in m["files"]]})
    return out


def build_lineage(ctx, rules, silver_key, silver_reused, gold, dest, deliverables, settings_hash, area_hash):
    buildings_csv = Path(dest) / "critical_buildings.csv"
    df = gold.df[~gold.df.Excluded]
    changes = {} if gold.changes is None else gold.changes.groupby("Change").size().to_dict()
    return {
        "run_id": ctx.run_id, "area": ctx.area, "run_type": ctx.run_type,
        "started_at": ctx.started_at, "tool_version": __version__, "git": git_info(),
        "rules": {"version_id": rules.version_id, "label": rules.label},
        "config": {"settings_hash": settings_hash, "area_hash": area_hash},
        "bronze": {"key": paths.bronze_key(ctx.area), "snapshots": bronze_inputs()},
        "silver": {"key": silver_key, "reused": silver_reused},
        "gold": {
            "buildings": int(len(df)), "sites": int(len(gold.sites_out)), "new_building_ids": int(gold.new_ids),
            "by_source": df["Source"].value_counts().sort_index().to_dict(),
            "by_verification": df["Verification"].value_counts().sort_index().to_dict(),
            "previous_run_id": ctx.previous_run_id, "changes_vs_previous": changes,
        },
        "outputs": {"files": [file_entry(Path(dest) / n) for n in deliverables if (Path(dest) / n).exists()],
                    "canonical_buildings_sha256": canonical_hash(buildings_csv)},
    }


def write_lineage(dest, lineage):
    p = Path(dest) / "lineage.json"
    p.write_text(json.dumps(lineage, indent=2), encoding="utf-8")
    return p
