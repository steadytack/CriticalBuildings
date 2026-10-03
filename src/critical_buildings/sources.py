"""Bronze layer: fetch raw data from each source into immutable dated snapshots with manifests.

Every snapshot folder holds the raw files exactly as downloaded plus an _manifest.json (url, retrieved time,
sha256, size, licence). Snapshots are never edited; a changed download makes a new snapshot and moves LATEST.
To add a source: add it to `sources:` in settings.yml and write a fetch_<name> function below.
"""
import datetime as dt
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

import requests

from critical_buildings import config, paths
from critical_buildings.control.rules import AREA_TAGS

log = logging.getLogger(__name__)


# ====================================================================================================
# Manifests and the LATEST pointer
# ====================================================================================================

def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def load_sources():
    return config.load_sources()


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


# ====================================================================================================
# Downloading and snapshotting
# ====================================================================================================

HEADERS = {"User-Agent": "critical-buildings-pipeline/0.1"}


def http_download(url, dest, retries=3, post_data=None, timeout=400):
    """Stream a URL (or POST an Overpass query) to dest, retrying on server errors."""
    for attempt in range(retries + 1):
        try:
            if post_data is None:
                r = requests.get(url, stream=True, timeout=timeout, headers=HEADERS)
            else:
                r = requests.post(url, data=post_data, stream=True, timeout=timeout, headers=HEADERS)
            if r.status_code == 200:
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                return
            log.warning("%s -> HTTP %s (attempt %s)", url, r.status_code, attempt + 1)
        except requests.RequestException as e:
            log.warning("%s -> %s (attempt %s)", url, e, attempt + 1)
        time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"Could not download {url}")


def _unique_snapshot_dir(source_dir, today):
    name, n = today.isoformat(), 1
    while (source_dir / name).exists():
        n += 1
        name = f"{today.isoformat()}-{n}"
    return source_dir / name


def snapshot(source, specs, area=None, dry_run=False, note=""):
    """specs: list of dicts {name, url, post_data(optional)}. Returns {'status': 'new'|'unchanged'|'dry-run', ...}."""
    source_dir = paths.bronze_source_dir(source, area)
    if dry_run:
        for s in specs:
            log.info("[dry run] %s <- %s", s["name"], s["url"])
        return {"status": "dry-run", "files": [s["name"] for s in specs]}
    source_dir.mkdir(parents=True, exist_ok=True)
    tmp = source_dir / f"_tmp_{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    try:
        for s in specs:
            log.info("downloading %s", s["name"])
            http_download(s["url"], tmp / s["name"], post_data=s.get("post_data"))
        new_hashes = {s["name"]: sha256_file(tmp / s["name"]) for s in specs}
        latest = paths.latest_snapshot(source, area)
        if latest:
            old = {f["name"]: f["sha256"] for f in read_manifest(source_dir / latest)["files"]}
            if old == new_hashes:
                log.info("%s unchanged since %s - nothing stored", source, latest)
                return {"status": "unchanged", "snapshot": latest}
        dest = _unique_snapshot_dir(source_dir, dt.date.today())
        shutil.move(str(tmp), str(dest))
        write_manifest(source, dest, [{"name": s["name"], "url": s["url"]} for s in specs], area=area, note=note)
        set_latest(source, dest.name, area)
        log.info("%s: new snapshot %s", source, dest.name)
        return {"status": "new", "snapshot": dest.name}
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


# ====================================================================================================
# OpenStreetMap (Overpass): query is built from the active OSM tags in the rules
# ====================================================================================================

OVERPASS = "https://overpass-api.de/api/interpreter"


def area_filter(area_cfg):
    b = area_cfg["osm_boundary"]
    return f'["ref:gss"="{b["ref_gss"]}"][boundary=administrative][admin_level={b["admin_level"]}]'


def build_queries(area_cfg, osm_tags):
    """osm_tags: sorted list of (key, value). Returns {filename: overpass query}."""
    af = area_filter(area_cfg)
    parts = []
    for k, v in osm_tags:
        sel = f'["{k}"]' if v == "*" else f'["{k}"="{v}"]'
        parts.append(f"  nwr{sel}(area.a);")
    features = f"[out:json][timeout:300];\narea{af}->.a;\n(\n" + "\n".join(parts) + "\n);\nout tags geom;"
    big = [f'  nwr["{k}"="{v}"](area.a);' for k, v in osm_tags if (k, v) in AREA_TAGS]
    area_buildings = (f"[out:json][timeout:300];\narea{af}->.a;\n(\n" + "\n".join(big) + "\n)->.big;\n"
                      ".big map_to_area->.bigA;\nnwr[building](area.bigA);\nout tags geom;")
    boundary = f"[out:json][timeout:120];rel{af};out geom;"
    return {"osm_features.json": features, "osm_area_buildings.json": area_buildings, "area_boundary.json": boundary}


def fetch_osm(rules, area="suffolk", dry_run=False):
    """rules: the published RuleSet whose active OSM tags decide what is fetched."""
    area_cfg = config.load_area(area)
    osm_tags = sorted({(t.key, t.value) for t in rules.tags if t.source == "OSM"})
    queries = build_queries(area_cfg, osm_tags)
    specs = [{"name": name, "url": OVERPASS, "post_data": {"data": q}} for name, q in queries.items()]
    return snapshot("osm_overpass", specs, area=area, dry_run=dry_run,
                             note=f"Query built from {len(osm_tags)} active OSM rule tags")


# ====================================================================================================
# Ordnance Survey: OS Open Map Local (by 100 km grid square) and Code-Point Open (GB)
# ====================================================================================================



def fetch_open_map_local(area="suffolk", dry_run=False):
    cat = load_sources()["os_open_map_local"]
    tiles = config.load_area(area)["os_grid_squares"]
    specs = [{"name": f"opmplc_essh_{t}.zip", "url": cat["url"].format(tile=t.upper())} for t in tiles]
    return snapshot("os_open_map_local", specs, dry_run=dry_run,
                             note=f"Grid squares: {', '.join(t.upper() for t in tiles)}")


def fetch_codepoint(dry_run=False):
    cat = load_sources()["os_codepoint_open"]
    return snapshot("os_codepoint_open", [{"name": "codepo_gb.zip", "url": cat["url"]}], dry_run=dry_run)


# ====================================================================================================
# Authoritative registers: DfE GIAS, CQC active locations, NHS ODS
# ====================================================================================================



def _reachable(url):
    """GET (not HEAD: the DfE server answers HEAD with an error) and drop the body."""
    try:
        with requests.get(url, stream=True, timeout=60, headers=HEADERS) as r:
            return r.status_code == 200
    except requests.RequestException:
        return False


def fetch_dfe(dry_run=False, today=None):
    """GIAS publishes a date-stamped file; try today and the previous days."""
    cat = load_sources()["dfe_gias"]
    today = today or dt.date.today()
    for back in range(4):
        url = cat["url"].format(yyyymmdd=(today - dt.timedelta(days=back)).strftime("%Y%m%d"))
        if dry_run or _reachable(url):
            return snapshot("dfe_gias", [{"name": "edubasealldata.csv", "url": url}], dry_run=dry_run)
    raise RuntimeError("No GIAS file found for the last 4 days")


def resolve_cqc_url(page_html, base="https://www.cqc.org.uk", pattern=r"HSCA_Active_Locations\.ods"):
    """The CQC file link changes every month: find it on the 'using CQC data' page."""
    m = re.search(rf'href="([^"]*{pattern})"', page_html)
    if not m:
        raise RuntimeError("Could not find the CQC HSCA active locations link on the CQC page")
    link = m.group(1)
    return link if link.startswith("http") else base + link


def fetch_cqc(dry_run=False):
    cat = load_sources()["cqc_hsca"]
    html = requests.get(cat["page"], timeout=60, headers=HEADERS).text
    url = resolve_cqc_url(html, pattern=cat["link_pattern"])
    return snapshot("cqc_hsca", [{"name": "active_locations.ods", "url": url}], dry_run=dry_run)


def fetch_nhs(dry_run=False):
    cat = load_sources()["nhs_ods"]
    specs = [{"name": f"{r}.csv", "url": cat["url"].format(report=r)} for r in cat["reports"]]
    return snapshot("nhs_ods", specs, dry_run=dry_run)


# ====================================================================================================
# Fetch everything
# ====================================================================================================

ALL = ("osm", "os", "codepoint", "dfe", "cqc", "nhs")


def fetch_all(rules, area="suffolk", only=None, dry_run=False):
    """Fetch every source (or the named subset). Returns {source: result}. Never overwrites existing snapshots."""
    jobs = {
        "osm": lambda: fetch_osm(rules, area, dry_run),
        "os": lambda: fetch_open_map_local(area, dry_run),
        "codepoint": lambda: fetch_codepoint(dry_run),
        "dfe": lambda: fetch_dfe(dry_run),
        "cqc": lambda: fetch_cqc(dry_run),
        "nhs": lambda: fetch_nhs(dry_run),
    }
    out = {}
    for name in (only or ALL):
        log.info("== fetching %s", name)
        out[name] = jobs[name]()
    return out
