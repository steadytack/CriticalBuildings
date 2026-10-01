"""Bronze manifests: what was downloaded, when, from where, with what checksum and licence.

Every bronze snapshot folder holds the raw files exactly as downloaded plus an _manifest.json. Snapshots are
immutable and append-only; LATEST.json in the source folder says which one runs use by default.
"""
import datetime as dt
import hashlib
import json
from pathlib import Path

import yaml

from critical_buildings import paths


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def load_sources():
    return yaml.safe_load((paths.CONFIG / "sources.yml").read_text(encoding="utf-8"))


def write_manifest(source, snapshot_dir, files, area=None, retrieved_at=None, note=""):
    """files: list of dicts with name and (optional) url. Adds bytes and sha256 from the files on disk."""
    cat = load_sources().get(source, {})
    snapshot_dir = Path(snapshot_dir)
    entries = []
    for f in files:
        p = snapshot_dir / f["name"]
        entries.append({"name": f["name"], "url": f.get("url", ""), "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    manifest = {
        "source": source,
        "source_name": cat.get("name", source),
        "area": area,
        "snapshot": snapshot_dir.name,
        "retrieved_at": retrieved_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "licence": cat.get("licence", ""),
        "attribution": " ".join(str(cat.get("attribution", "")).split()),
        "files": entries,
        "note": note,
    }
    (snapshot_dir / "_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def read_manifest(snapshot_dir):
    return json.loads((Path(snapshot_dir) / "_manifest.json").read_text(encoding="utf-8"))


def set_latest(source, snapshot, area=None):
    d = paths.bronze_source_dir(source, area)
    (d / "LATEST.json").write_text(json.dumps({"snapshot": snapshot}, indent=2), encoding="utf-8")


def list_snapshots(source, area=None):
    d = paths.bronze_source_dir(source, area)
    return sorted(p.name for p in d.glob("*") if p.is_dir()) if d.exists() else []


def all_manifests():
    """Manifest of the LATEST snapshot of every source present (used for lineage)."""
    out = []
    for d in sorted(p for p in paths.BRONZE.glob("*") if p.is_dir()):
        latest = d / "LATEST.json"
        if latest.exists():
            snap = json.loads(latest.read_text(encoding="utf-8"))["snapshot"]
            out.append(read_manifest(d / snap))
    return out
