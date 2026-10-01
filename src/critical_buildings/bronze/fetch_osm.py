"""Fetch OpenStreetMap features for an area from the Overpass API into a bronze snapshot.

The query is driven by the active OSM tags in the rules, so changing a rule changes what is fetched next time.
Files: osm_features.json (tagged features), osm_area_buildings.json (buildings inside port / military / aerodrome
areas) and area_boundary.json (the administrative boundary used to clip every source).
"""
import logging

from critical_buildings import config
from critical_buildings.bronze import download
from critical_buildings.rules_io import AREA_TAGS, load_rules

log = logging.getLogger(__name__)
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


def fetch(area="suffolk", dry_run=False):
    area_cfg = config.load_area(area)
    tags, _ = load_rules()
    osm_tags = sorted({(t.key, t.value) for t in tags if t.source == "OSM"})
    queries = build_queries(area_cfg, osm_tags)
    specs = [{"name": name, "url": OVERPASS, "post_data": {"data": q}} for name, q in queries.items()]
    return download.snapshot("osm_overpass", specs, area=area, dry_run=dry_run,
                             note=f"Query built from {len(osm_tags)} active OSM rule tags")
