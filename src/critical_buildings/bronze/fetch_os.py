"""Fetch Ordnance Survey open data: OS Open Map Local (by 100 km grid square) and Code-Point Open (GB)."""
import logging

from critical_buildings import config
from critical_buildings.bronze import download, manifest

log = logging.getLogger(__name__)


def fetch_open_map_local(area="suffolk", dry_run=False):
    cat = manifest.load_sources()["os_open_map_local"]
    tiles = config.load_area(area)["os_grid_squares"]
    specs = [{"name": f"opmplc_essh_{t}.zip", "url": cat["url"].format(tile=t.upper())} for t in tiles]
    return download.snapshot("os_open_map_local", specs, dry_run=dry_run,
                             note=f"Grid squares: {', '.join(t.upper() for t in tiles)}")


def fetch_codepoint(dry_run=False):
    cat = manifest.load_sources()["os_codepoint_open"]
    return download.snapshot("os_codepoint_open", [{"name": "codepo_gb.zip", "url": cat["url"]}], dry_run=dry_run)
