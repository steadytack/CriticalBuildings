"""Fetch OSM features for the pilot area via the Overpass API, driven by the active OSM tags in the Rules sheet.

Writes data/osm_features.json (tagged features) and data/osm_area_buildings.json (buildings inside
area-type features such as ports, military land and aerodromes).
"""
import json
import time
from pathlib import Path

import requests

from rules_io import load_rules

DATA = Path(__file__).resolve().parent.parent / "data"
DATA.mkdir(exist_ok=True)
OVERPASS = "https://overpass-api.de/api/interpreter"
AREA = '["ref:gss"="E10000029"][boundary=administrative][admin_level=6]'   # Suffolk, UK (not Suffolk, Virginia)
# tags that describe an area rather than a building
AREA_TAGS = {("landuse", "port"), ("landuse", "military"), ("aeroway", "aerodrome")}


def post(query, dest):
    if dest.exists():
        print("have", dest.name)
        return
    for attempt in range(4):
        r = requests.post(OVERPASS, data={"data": query}, timeout=400,
                          headers={"User-Agent": "critical-buildings-pipeline/0.1"})
        if r.status_code == 200:
            dest.write_text(r.text, encoding="utf-8")
            print("saved", dest.name, f"{len(r.text) / 1e6:.1f} MB")
            return
        print("overpass", r.status_code, "retrying")
        time.sleep(30 * (attempt + 1))
    raise SystemExit("Overpass failed")


def main():
    tags, _ = load_rules()
    osm = sorted({(t.key, t.value) for t in tags if t.source == "OSM"})
    parts = []
    for k, v in osm:
        sel = f'["{k}"]' if v == "*" else f'["{k}"="{v}"]'
        parts.append(f"  nwr{sel}(area.a);")
    q1 = (f"[out:json][timeout:300];\narea{AREA}->.a;\n(\n" + "\n".join(parts) + "\n);\nout tags geom;")
    post(q1, DATA / "osm_features.json")

    big = [f'  nwr["{k}"="{v}"](area.a);' for k, v in osm if (k, v) in AREA_TAGS]
    q2 = (f"[out:json][timeout:300];\narea{AREA}->.a;\n(\n" + "\n".join(big) + "\n)->.big;\n"
          ".big map_to_area->.bigA;\nnwr[building](area.bigA);\nout tags geom;")
    post(q2, DATA / "osm_area_buildings.json")


if __name__ == "__main__":
    main()
