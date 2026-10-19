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
import zipfile
from contextlib import nullcontext
from pathlib import Path

import requests

from critical_buildings import config, locks, paths
from critical_buildings.control.rules import AREA_TAGS
from critical_buildings.errors import PipelineError

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
        "retrieved_at": retrieved_at or dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
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


def all_manifests(area=None):
    """Manifest of the LATEST snapshot of every source present (used for lineage).

    With `area`, area-scoped sources of other areas are left out. The manifest's area is taken from the folder name
    (source__area), so snapshots moved there by migrate_area_scoped_bronze are described correctly."""
    out = []
    for d in sorted(p for p in paths.BRONZE.glob("*") if p.is_dir()):
        dir_area = d.name.split("__", 1)[1] if "__" in d.name else None
        if area is not None and dir_area not in (None, area):
            continue
        latest = d / "LATEST.json"
        if latest.exists():
            snap = json.loads(latest.read_text(encoding="utf-8"))["snapshot"]
            out.append({**read_manifest(d / snap), "area": dir_area})
    return out


def legacy_unscoped_sources(area):
    """Area-scoped sources whose snapshots still sit in the old shared folder (data/bronze/<source>) with none yet
    for `area`: these need migrate_area_scoped_bronze."""
    return [s for s, cat in load_sources().items()
            if cat.get("area_scoped") and (paths.BRONZE / s / "LATEST.json").exists()
            and not (paths.bronze_source_dir(s, area) / "LATEST.json").exists()]


def migrate_area_scoped_bronze(area):
    """One-off: move the old shared bronze folder of each area-scoped source to <source>__<area>.

    Before OS Open Map Local was area-scoped its snapshots lived in data/bronze/os_open_map_local, whatever the area.
    Only run this for the area those snapshots were fetched for. Snapshot contents are untouched. Returns the moved
    folder names. (The bronze key changes with the folder name, so the next run rebuilds silver once.)"""
    moved = []
    with locks.pipeline_lock("migrate-bronze"):
        for s in legacy_unscoped_sources(area):
            src, dest = paths.BRONZE / s, paths.bronze_source_dir(s, area)
            if dest.exists():
                raise RuntimeError(f"{dest} already exists without a LATEST pointer; resolve it by hand.")
            src.rename(dest)
            moved.append(f"{s} -> {dest.name}")
    return moved


# ====================================================================================================
# Downloading and snapshotting
# ====================================================================================================

HEADERS = {"User-Agent": "critical-buildings-pipeline/0.1"}


class DownloadError(PipelineError, RuntimeError):
    """A source could not be fetched (the server failed, the file was not what we asked for, or its link was not found)."""


class BadDownload(ValueError):
    """A download that arrived (HTTP 200) but is not the data we asked for."""


def validate_overpass(path, min_elements=1):
    """Overpass answers HTTP 200 even when the query timed out or ran out of memory: the JSON then carries a
    'remark' and partial or no elements. Refuse those rather than store them as the latest snapshot."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise BadDownload(f"not valid JSON ({e})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("elements"), list):
        raise BadDownload("no 'elements' list in the Overpass response")
    remark = str(doc.get("remark", ""))
    if "error" in remark.lower() or "timed out" in remark.lower() or "out of memory" in remark.lower():
        raise BadDownload(f"Overpass reported a problem: {remark}")
    if len(doc["elements"]) < min_elements:
        raise BadDownload(f"only {len(doc['elements'])} elements (expected at least {min_elements})")


def validate_download(path, min_elements=1):
    """Cheap sanity checks by file type: not empty, not an HTML error page, zips/ods really are zips, Overpass JSON complete."""
    path = Path(path)
    if path.stat().st_size == 0:
        raise BadDownload("empty file")
    suffix = path.suffix.lower()
    if suffix == ".json":
        validate_overpass(path, min_elements)
    elif suffix in (".zip", ".ods"):
        if not zipfile.is_zipfile(path):
            raise BadDownload("not a valid zip/ods file")
    elif suffix == ".geojson":
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise BadDownload(f"not valid JSON ({e})") from e
        if not isinstance(doc, dict) or "error" in doc or not doc.get("features"):
            raise BadDownload("no features in the response" + (f": {doc['error']}" if isinstance(doc, dict) and "error" in doc else ""))
    elif suffix == ".pbf":
        with open(path, "rb") as f:
            if b"OSMHeader" not in f.read(64):
                raise BadDownload("not an OpenStreetMap PBF file")
    else:
        with open(path, "rb") as f:
            head = f.read(512).lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")):
            raise BadDownload("got an HTML page instead of data")


def http_download(url, dest, retries=3, post_data=None, timeout=400, validate=None):
    """Stream a URL (or POST an Overpass query) to dest, retrying on server errors and on downloads that fail
    `validate(dest)` (which raises BadDownload). Client errors such as 404 fail at once: retrying cannot help."""
    for attempt in range(retries + 1):
        r = None
        try:
            if post_data is None:
                r = requests.get(url, stream=True, timeout=timeout, headers=HEADERS)
            else:
                r = requests.post(url, data=post_data, stream=True, timeout=timeout, headers=HEADERS)
            if r.status_code == 200:
                with open(dest, "wb") as f:
                    f.writelines(r.iter_content(1 << 20))
                if validate is None:
                    return
                try:
                    validate(dest)
                    return
                except BadDownload as e:
                    log.warning("%s -> bad download: %s (attempt %s)", url, e, attempt + 1)
                    last_problem = str(e)
            else:
                log.warning("%s -> HTTP %s (attempt %s)", url, r.status_code, attempt + 1)
                last_problem = f"HTTP {r.status_code}"
                if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                    break
        except requests.RequestException as e:
            log.warning("%s -> %s (attempt %s)", url, e, attempt + 1)
            last_problem = str(e)
        finally:
            if r is not None:
                r.close()                                  # give the connection back, whatever happened
        if attempt < retries:
            time.sleep(15 * (attempt + 1))
    raise DownloadError(f"Could not download {url}: {last_problem}")


def _unique_snapshot_dir(source_dir, today):
    name, n = today.isoformat(), 1
    while (source_dir / name).exists():
        n += 1
        name = f"{today.isoformat()}-{n}"
    return source_dir / name


def snapshot(source, specs, area=None, dry_run=False, note=""):
    """specs: list of dicts {name, url, post_data(optional)}, or {name, path} to store a file you downloaded yourself.
    Returns {'status': 'new'|'unchanged'|'dry-run', ...}."""
    source_dir = paths.bronze_source_dir(source, area)
    if dry_run:
        for s in specs:
            log.info("[dry run] %s <- %s", s["name"], s.get("url") or s["path"])
        return {"status": "dry-run", "files": [s["name"] for s in specs]}
    source_dir.mkdir(parents=True, exist_ok=True)
    tmp = source_dir / f"_tmp_{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    try:
        for s in specs:
            if "path" in s:                                   # a file you downloaded yourself (a source that needs a sign-in)
                log.info("storing %s", s["name"])
                shutil.copy2(s["path"], tmp / s["name"])
                continue
            log.info("downloading %s", s["name"])
            http_download(s["url"], tmp / s["name"], post_data=s.get("post_data"),
                          validate=lambda p, s=s: validate_download(p, s.get("min_elements", 1)))
        new_hashes = {s["name"]: sha256_file(tmp / s["name"]) for s in specs}
        latest = paths.latest_snapshot(source, area)
        if latest:
            old = {f["name"]: f["sha256"] for f in read_manifest(source_dir / latest)["files"]}
            if old == new_hashes:
                log.info("%s unchanged since %s - nothing stored", source, latest)
                return {"status": "unchanged", "snapshot": latest}
        dest = _unique_snapshot_dir(source_dir, dt.date.today())  # noqa: DTZ011 - local time is intended
        shutil.move(str(tmp), str(dest))
        write_manifest(source, dest, [{"name": s["name"], "url": s.get("url", "")} for s in specs], area=area, note=note)
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
    if area_cfg.get("osm_extract"):             # the features come from the extract (fetch_osm_extract); only the boundary is queried
        queries = {"area_boundary.json": queries["area_boundary.json"]}
    specs = [{"name": name, "url": OVERPASS, "post_data": {"data": q}} for name, q in queries.items()]
    return snapshot("osm_overpass", specs, area=area, dry_run=dry_run,
                             note=f"Query built from {len(osm_tags)} active OSM rule tags")


def uses_extract(area):
    """True when the area reads OpenStreetMap from a Geofabrik extract instead of the Overpass API (settings.yml: osm_extract)."""
    return bool(config.load_area(area).get("osm_extract"))


def fetch_osm_extract(area="suffolk", dry_run=False):
    """The Geofabrik PBF extract named by the area's osm_extract setting."""
    path = config.load_area(area).get("osm_extract")
    if not path:
        raise PipelineError(f"Area '{area}' reads OpenStreetMap from the Overpass API: it has no osm_extract setting in settings.yml.")
    url = load_sources()["osm_extract"]["url"].format(path=path)
    return snapshot("osm_extract", [{"name": "extract.osm.pbf", "url": url}], area=area, dry_run=dry_run,
                    note=f"Geofabrik extract {path}")


# ====================================================================================================
# Ordnance Survey: OS Open Map Local (by 100 km grid square) and Code-Point Open (GB)
# ====================================================================================================



def fetch_open_map_local(area="suffolk", dry_run=False):
    cat = load_sources()["os_open_map_local"]
    tiles = config.load_area(area)["os_grid_squares"]
    specs = [{"name": f"opmplc_essh_{t}.zip", "url": cat["url"].format(tile=t.upper())} for t in tiles]
    return snapshot("os_open_map_local", specs, area=area, dry_run=dry_run,
                             note=f"Grid squares: {', '.join(t.upper() for t in tiles)}")


def lsoa_page_urls(cat, count):
    """One request per page of areas: the service returns at most page_size at a time, in a fixed order."""
    size = int(cat["page_size"])
    return [(f"lsoa_{i:03d}.geojson",
             f"{cat['url']}?where=1%3D1&outFields=LSOA21CD,LSOA21NM&outSR=27700&orderByFields=LSOA21CD"
             f"&resultOffset={off}&resultRecordCount={size}&f=geojson")
            for i, off in enumerate(range(0, count, size))]


def fetch_lsoa(dry_run=False):
    cat = load_sources()["ons_lsoa"]
    count = 35672                                         # England and Wales, December 2021; asked of the service for a real fetch
    if not dry_run:
        try:
            r = requests.get(cat["url"], params={"where": "1=1", "returnCountOnly": "true", "f": "json"}, timeout=60, headers=HEADERS)
            count = int(r.json()["count"])
        except (requests.RequestException, ValueError, KeyError) as e:
            raise DownloadError(f"Could not ask the ONS service how many LSOAs there are: {e}") from e
    specs = [{"name": n, "url": u} for n, u in lsoa_page_urls(cat, count)]
    return snapshot("ons_lsoa", specs, dry_run=dry_run, note=f"{count} LSOAs in {len(specs)} pages")


def fetch_iod(dry_run=False):
    cat = load_sources()["iod2025"]
    return snapshot("iod2025", [{"name": "iod2025_file7.csv", "url": cat["url"]}], dry_run=dry_run)


def fetch_planning(dry_run=False):
    cat = load_sources()["planning_data"]
    specs = [{"name": f"{d}.csv", "url": cat["url"].format(dataset=d)} for d in cat["datasets"]]
    return snapshot("planning_data", specs, dry_run=dry_run)


def fetch_flood_zones(dry_run=False):
    cat = load_sources()["ea_flood_zones"]
    return snapshot("ea_flood_zones", [{"name": "flood-risk-zone.csv", "url": cat["url"]}], dry_run=dry_run)


def fetch_open_uprn(dry_run=False):
    cat = load_sources()["os_open_uprn"]
    return snapshot("os_open_uprn", [{"name": "osopenuprn_csv.zip", "url": cat["url"]}], dry_run=dry_run)


def fetch_codepoint(dry_run=False):
    cat = load_sources()["os_codepoint_open"]
    return snapshot("os_codepoint_open", [{"name": "codepo_gb.zip", "url": cat["url"]}], dry_run=dry_run)


EPC_INBOX = ("inbox", "epc_domestic")
EPC_HOW = ("Sign in at https://get-energy-performance-data.communities.gov.uk/data-access-options (a GOV.UK One Login), download the domestic "
           "certificates as CSV, and put the file(s) (.zip or .csv) in {folder}. Then run: python -m critical_buildings fetch --only epc")


def epc_inbox():
    return paths.DATA.joinpath(*EPC_INBOX)


def epc_files():
    """The files you have placed in the EPC inbox (.csv and .zip; a recommendations file is not needed but harmless)."""
    folder = epc_inbox()
    return sorted(p for p in folder.glob("*") if p.suffix.lower() in (".csv", ".zip")) if folder.exists() else []


def fetch_epc(dry_run=False):
    """Store what is in the EPC inbox as a snapshot. The register needs a GOV.UK One Login to download, which this tool cannot do for
    you, so this is the one source you place by hand; nothing in the inbox is not an error (the property table just has no EPC columns)."""
    files = epc_files()
    if not files:
        return {"status": "skipped", "note": "no EPC file placed. " + EPC_HOW.format(folder=epc_inbox())}
    return snapshot("epc_domestic", [{"name": p.name, "path": p} for p in files], dry_run=dry_run,
                    note=f"{len(files)} file(s) placed in {epc_inbox()}")


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
    today = today or dt.date.today()  # noqa: DTZ011 - local time is intended
    for back in range(4):
        url = cat["url"].format(yyyymmdd=(today - dt.timedelta(days=back)).strftime("%Y%m%d"))
        if dry_run or _reachable(url):
            return snapshot("dfe_gias", [{"name": "edubasealldata.csv", "url": url}], dry_run=dry_run)
    raise DownloadError("No GIAS file found for the last 4 days")


def resolve_cqc_url(page_html, base="https://www.cqc.org.uk", pattern=r"HSCA_Active_Locations\.ods"):
    """The CQC file link changes every month: find it on the 'using CQC data' page."""
    m = re.search(rf'href="([^"]*{pattern})"', page_html)
    if not m:
        raise DownloadError("Could not find the CQC HSCA active locations link on the CQC page")
    link = m.group(1)
    return link if link.startswith("http") else base + link


def fetch_cqc(dry_run=False):
    cat = load_sources()["cqc_hsca"]
    try:
        page = requests.get(cat["page"], timeout=60, headers=HEADERS)
        page.raise_for_status()
    except requests.RequestException as e:
        raise DownloadError(f"Could not read the CQC data page {cat['page']}: {e}") from e
    html = page.text
    url = resolve_cqc_url(html, pattern=cat["link_pattern"])
    return snapshot("cqc_hsca", [{"name": "active_locations.ods", "url": url}], dry_run=dry_run)


def fetch_nhs(dry_run=False):
    cat = load_sources()["nhs_ods"]
    specs = [{"name": f"{r}.csv", "url": cat["url"].format(report=r)} for r in cat["reports"]]
    return snapshot("nhs_ods", specs, dry_run=dry_run)


# ====================================================================================================
# Fetch everything
# ====================================================================================================

ALL = ("osm", "osmx", "os", "codepoint", "uprn", "dfe", "cqc", "nhs")
PROPERTY_SOURCES = ("uprn", "codepoint", "lsoa", "iod", "planning", "flood", "epc")      # what the property table uses (epc is optional)
GROUPS = {"properties": PROPERTY_SOURCES}                                         # fetch --only properties      # osmx (the Geofabrik extract) only for areas that set osm_extract


def fetch_all(rules, area="suffolk", only=None, dry_run=False):
    """Fetch every source (or the named subset). Returns {source: result}. Never overwrites existing snapshots.

    A source that cannot be fetched is reported as {"status": "failed", "error": ...} and the rest are still fetched."""
    jobs = {
        "osm": lambda: fetch_osm(rules, area, dry_run),
        "osmx": lambda: fetch_osm_extract(area, dry_run),
        "os": lambda: fetch_open_map_local(area, dry_run),
        "codepoint": lambda: fetch_codepoint(dry_run),
        "uprn": lambda: fetch_open_uprn(dry_run),
        "lsoa": lambda: fetch_lsoa(dry_run),
        "iod": lambda: fetch_iod(dry_run),
        "planning": lambda: fetch_planning(dry_run),
        "flood": lambda: fetch_flood_zones(dry_run),
        "epc": lambda: fetch_epc(dry_run),
        "dfe": lambda: fetch_dfe(dry_run),
        "cqc": lambda: fetch_cqc(dry_run),
        "nhs": lambda: fetch_nhs(dry_run),
    }
    only = [n for name in (only or ()) for n in GROUPS.get(name, (name,))] or None        # a group name stands for its sources
    unknown = [n for n in (only or ()) if n not in jobs]
    if unknown:
        raise PipelineError(f"Unknown source(s): {', '.join(unknown)}. Choose from: {', '.join(jobs)}, or a group: {', '.join(GROUPS)}.")
    out = {}
    with nullcontext() if dry_run else locks.pipeline_lock("fetch"):
        for name in only or [n for n in ALL if n != "osmx" or uses_extract(area)]:
            log.info("== fetching %s", name)
            try:
                out[name] = jobs[name]()
            except PipelineError as e:           # one source being down must not stop the others from being fetched
                log.warning("%s failed: %s", name, e)
                out[name] = {"status": "failed", "error": str(e)}
    return out
