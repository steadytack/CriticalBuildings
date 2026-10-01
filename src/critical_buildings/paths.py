"""Every location in the repository, in one place.

Everything generated lives under data/ (git-ignored):
    data/bronze/<source>/<retrieved_date>/   raw files exactly as downloaded + _manifest.json
    data/bronze/<source>/LATEST.json         pointer to the snapshot runs use by default
    data/silver/<area>/conformed/<key>/      cleaned tables derived from bronze (key = hash of the bronze inputs)
    data/gold/<area>/<run_id>/               one immutable folder per pipeline run
    data/published/<area>/<release>/         pinned releases;  data/archive/  zipped old runs and snapshots
Human-governed inputs: settings.yml and governed/ (rules store, text dumps).
"""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("CB_ROOT") or Path(__file__).resolve().parents[2])
SETTINGS = ROOT / "settings.yml"
CONTROL = ROOT / "governed"
DATA = ROOT / "data"
BRONZE = DATA / "bronze"
SILVER = DATA / "silver"
GOLD = DATA / "gold"
PUBLISHED = DATA / "published"
ARCHIVE = DATA / "archive"
DEFAULT_AREA = "suffolk"

OS_TILE_ZIP = "opmplc_essh_{tile}.zip"
OS_TILE_INNER = "OS OpenMap Local (ESRI Shape File) {TILE}/data/{TILE}_{layer}.shp"


# ------------------------------------------------------------------ bronze
def bronze_source_dir(source, area=None):
    """Folder for a source. Area-scoped sources (e.g. OSM) get a '__<area>' suffix."""
    return BRONZE / (f"{source}__{area}" if area else source)


def latest_snapshot(source, area=None):
    """Name of the snapshot a run should use by default (from LATEST.json), or None if never fetched."""
    p = bronze_source_dir(source, area) / "LATEST.json"
    return json.loads(p.read_text(encoding="utf-8"))["snapshot"] if p.exists() else None


def bronze_snapshot(source, area=None, pin=None):
    """Folder of one bronze snapshot (pinned date, else LATEST)."""
    snap = pin or latest_snapshot(source, area)
    if not snap:
        raise FileNotFoundError(f"No bronze data for '{source}'. Run: python -m critical_buildings fetch")
    d = bronze_source_dir(source, area) / snap
    if not d.exists():
        raise FileNotFoundError(f"Bronze snapshot not found: {d}")
    return d


def bronze_file(source, filename, area=None, pin=None):
    return bronze_snapshot(source, area, pin) / filename


def os_layer_paths(layer, tiles=("tg", "tl", "tm"), pin=None):
    """GDAL paths to an OS Open Map Local layer, read straight from the downloaded zips (no extraction)."""
    snap = bronze_snapshot("os_open_map_local", pin=pin)
    return [f"zip://{(snap / OS_TILE_ZIP.format(tile=t)).as_posix()}!"
            + OS_TILE_INNER.format(TILE=t.upper(), layer=layer) for t in tiles]


# ------------------------------------------------------------------ keys / silver
def bronze_key(area=DEFAULT_AREA, sources=None):
    """Short content hash of the exact bronze inputs in use (sha256 of every file in each LATEST manifest)."""
    parts = []
    for d in sorted(p for p in BRONZE.glob("*") if p.is_dir()):
        if sources and d.name.split("__")[0] not in sources:
            continue
        if "__" in d.name and not d.name.endswith(f"__{area}"):
            continue
        latest = d / "LATEST.json"
        if not latest.exists():
            continue
        manifest = d / json.loads(latest.read_text(encoding="utf-8"))["snapshot"] / "_manifest.json"
        for f in sorted(json.loads(manifest.read_text(encoding="utf-8"))["files"], key=lambda f: f["name"]):
            parts.append(f"{d.name}/{f['name']}:{f['sha256']}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]


def silver_conformed_dir(area=DEFAULT_AREA):
    d = SILVER / area / "conformed" / bronze_key(area)
    d.mkdir(parents=True, exist_ok=True)
    return d
