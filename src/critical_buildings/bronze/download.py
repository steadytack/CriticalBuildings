"""Download files into a new immutable bronze snapshot (skipped if nothing has changed since the latest one).

Each file is fetched to a temporary folder, hashed, and compared with the latest snapshot's manifest.
Identical -> nothing is stored. Different -> a new dated snapshot folder with a manifest, and LATEST.json moves.
"""
import datetime as dt
import logging
import shutil
import time
import uuid

import requests

from critical_buildings import paths
from critical_buildings.bronze import manifest

log = logging.getLogger(__name__)
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
        new_hashes = {s["name"]: manifest.sha256_file(tmp / s["name"]) for s in specs}
        latest = paths.latest_snapshot(source, area)
        if latest:
            old = {f["name"]: f["sha256"] for f in manifest.read_manifest(source_dir / latest)["files"]}
            if old == new_hashes:
                log.info("%s unchanged since %s - nothing stored", source, latest)
                return {"status": "unchanged", "snapshot": latest}
        dest = _unique_snapshot_dir(source_dir, dt.date.today())
        shutil.move(str(tmp), str(dest))
        manifest.write_manifest(source, dest, [{"name": s["name"], "url": s["url"]} for s in specs], area=area, note=note)
        manifest.set_latest(source, dest.name, area)
        log.info("%s: new snapshot %s", source, dest.name)
        return {"status": "new", "snapshot": dest.name}
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
