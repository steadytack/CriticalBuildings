"""Turn Overpass JSON (out geom) into a GeoDataFrame in British National Grid (EPSG:27700)."""
import json
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import polygonize, unary_union

DATA = Path(__file__).resolve().parent.parent / "data"


def _line(geom):
    return LineString([(p["lon"], p["lat"]) for p in geom]) if geom and len(geom) >= 2 else None


def element_geometry(e):
    """WGS84 shapely geometry for one Overpass element (None if not buildable)."""
    if e["type"] == "node":
        return Point(e["lon"], e["lat"])
    if e["type"] == "way":
        pts = e.get("geometry") or []
        if len(pts) >= 4 and pts[0] == pts[-1]:
            return Polygon([(p["lon"], p["lat"]) for p in pts])
        ln = _line(pts)
        return ln.centroid if ln is not None else None
    if e["type"] == "relation":
        outer = [_line(m.get("geometry")) for m in e.get("members", []) if m["role"] == "outer" and m["type"] == "way"]
        inner = [_line(m.get("geometry")) for m in e.get("members", []) if m["role"] == "inner" and m["type"] == "way"]
        outer = [x for x in outer if x is not None]
        if not outer:
            pts = [Point(m["lon"], m["lat"]) for m in e.get("members", []) if m["type"] == "node" and "lon" in m]
            return unary_union(pts).centroid if pts else None
        polys = list(polygonize(unary_union(outer)))
        if not polys:
            return unary_union(outer).centroid
        shape = unary_union(polys)
        holes = list(polygonize(unary_union([x for x in inner if x is not None]))) if inner else []
        if holes:
            shape = shape.difference(unary_union(holes))
        return shape
    return None


def load_elements(path):
    els = json.loads(Path(path).read_text(encoding="utf-8"))["elements"]
    rows = []
    for e in els:
        g = element_geometry(e)
        if g is None or g.is_empty:
            continue
        rows.append({"osm_id": f"{e['type']}/{e['id']}", "tags": e.get("tags", {}), "geometry": g})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=4326).to_crs(27700)


def suffolk_boundary():
    """Suffolk boundary polygon (EPSG:27700), fetched once from Overpass and cached."""
    cache = DATA / "suffolk_boundary.json"
    if not cache.exists():
        import requests
        q = ('[out:json][timeout:120];rel["ref:gss"="E10000029"][boundary=administrative][admin_level=6];out geom;')
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": q}, timeout=200,
                          headers={"User-Agent": "critical-buildings-pipeline/0.1"})
        r.raise_for_status()
        cache.write_text(r.text, encoding="utf-8")
    g = load_elements(cache)
    return unary_union(g.geometry.buffer(0).values)
