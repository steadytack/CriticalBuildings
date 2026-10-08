"""Silver stage: conform and classify every source for an area.

Reads the latest bronze snapshots, clips to the area boundary, classifies OSM and OS features with the rule tags and
loads the authoritative registers (DfE / CQC / NHS ODS) mapped to building types. Pure function of
(bronze inputs, rule set, area); the lifecycle layer caches it under a build key.
"""
import functools
import hashlib
import json
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize, unary_union

from critical_buildings import config, osm_extract, paths, progress, source_checks
from critical_buildings.control.rules import AREA_TAGS, content_hash

# ====================================================================================================
# OpenStreetMap geometry: Overpass JSON to GeoDataFrames in British National Grid
# ====================================================================================================

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


def area_boundary(area=paths.DEFAULT_AREA):
    """Boundary polygon of the area (EPSG:27700), from the bronze OSM boundary snapshot."""
    g = load_elements(paths.bronze_file("osm_overpass", "area_boundary.json", area=area))
    boundary = unary_union(g.geometry.buffer(0).values)
    shapely.prepare(boundary)          # a country's outline has 100,000+ vertices: prepared, 'is this point inside' is fast
    return boundary


# ====================================================================================================
# Applying the rule tags to OSM and OS Open Map Local features
# ====================================================================================================

IMP_RANK = {"High": 0, "Medium": 1, "Low": 2}
CLASSIFIED_COLS = ["source", "source_id", "name", "rule_ref", "category", "building_type", "importance", "driver",
        "matched_refs", "note", "rule_notes", "osm_building", "geometry"]


def _best(matches):
    """Pick the winning rule: lowest Priority number, then higher importance, then lowest ref."""
    return min(matches, key=lambda t: (t.priority, IMP_RANK[t.importance], t.rule_ref))


def _osm_match(tags, rule):
    v = tags.get(rule.key)
    if v is None:
        return False
    if rule.value == "*":
        return v.strip().lower() != "no"
    return rule.value in [s.strip() for s in v.split(";")]


def _row(source, sid, name, rule, refs, geom, note="", osm_building=""):
    return {"source": source, "source_id": sid, "name": name, "rule_ref": rule.rule_ref, "category": rule.category,
                "building_type": rule.building_type, "importance": rule.importance, "driver": rule.driver,
                "matched_refs": ";".join(str(r) for r in sorted(refs)), "note": note, "rule_notes": rule.notes, "osm_building": osm_building, "geometry": geom}


def _extract_features(pbf, osm_tags, bbox, area):
    """(features, area buildings) from the extract. Reading a country takes about 15 minutes, so the result is kept as a conformed
    table: it only has to be read again when the extract or the OSM tags the rules ask for change, not for any other rule edit."""
    name = osm_extract.cache_name(osm_tags, bbox)
    inputs = [("osm_extract", None)]
    cached = paths.conformed_lookup(area, name, inputs)
    if cached is not None:
        progress.step("Using the cached read of the OpenStreetMap extract (same extract and same OSM tags as before)")
        progress.note("OSM extract read", "cached")
        return osm_extract.load_cache(cached)
    progress.note("OSM extract read", "from the extract file")
    feats, blds = osm_extract.read_all(pbf, osm_tags, bbox)
    osm_extract.save_cache(paths.silver_conformed_dir(area) / name, feats, blds)
    paths.conformed_stamp(area, name, inputs)
    return feats, blds


def classify_osm(tags, boundary=None, area=paths.DEFAULT_AREA):
    osm_rules = [t for t in tags if t.source == "OSM"]
    osm_tags = sorted({(t.key, t.value) for t in osm_rules})
    pbf = paths.bronze_file("osm_extract", "extract.osm.pbf", area=area) if config.load_area(area).get("osm_extract") else None
    bbox = tuple(gpd.GeoSeries([box(*boundary.bounds)], crs=27700).to_crs(4326).total_bounds) if pbf and boundary is not None else None
    if pbf:                         # an extract: stream the tagged features out of the PBF file (minutes for a country), cached
        feats, blds = _extract_features(pbf, osm_tags, bbox, area)
    else:
        feats = load_elements(paths.bronze_file("osm_overpass", "osm_features.json", area=area))
    rows, areas, sites = [], [], []
    rules_by_key = {}
    for r in osm_rules:
        rules_by_key.setdefault(r.key, []).append(r)
    for f in feats.itertuples(index=False):
        # only rules whose tag key the feature has (the winner is picked by priority, so hit order does not matter)
        hits = [r for k in f.tags for r in rules_by_key.get(k, ()) if _osm_match(f.tags, r)]
        if not hits:
            continue
        refs = {h.rule_ref for h in hits}
        non_area = [h for h in hits if (h.key, h.value) not in AREA_TAGS]
        if non_area:
            win = _best(non_area)
            rows.append(_row("OSM", f.osm_id, f.tags.get("name", ""), win, refs, f.geometry,
                             osm_building=f.tags.get("building", "")))
            if f.geometry.geom_type in ("Polygon", "MultiPolygon"):
                sites.append({"name": f.tags.get("name", ""), "category": win.category, "origin": "OSM site",
                                  "key": f.osm_id, "geometry": f.geometry})
        else:
            areas.append((f, _best(hits), refs))

    # area-type features (port, military land, aerodrome): classify the buildings inside them
    own_ids = {r["source_id"] for r in rows}
    if not pbf:
        blds = load_elements(paths.bronze_file("osm_overpass", "osm_area_buildings.json", area=area))
    blds = blds[~blds.osm_id.isin(own_ids)]
    pts = blds.geometry.representative_point()
    inherited = {}
    for f, win, refs in areas:
        if f.geometry.geom_type in ("Polygon", "MultiPolygon"):
            sites.append({"name": f.tags.get("name", ""), "category": win.category, "origin": "OSM area",
                              "key": f.osm_id, "geometry": f.geometry})
        inside = blds.index[pts.within(f.geometry)]
        for i in inside:
            cur = inherited.get(i)
            if cur is None or _best([cur[0], win]) is win and win is not cur[0]:
                inherited[i] = (win, refs, f.osm_id)
        if len(inside) == 0:
            rows.append(_row("OSM", f.osm_id, f.tags.get("name", ""), win, refs, f.geometry,
                             "Area feature with no buildings found inside; area kept as the record",
                             f.tags.get("building", "")))
    for i, (win, refs, area_id) in inherited.items():
        b = blds.loc[i]
        rows.append(_row("OSM", b.osm_id, b.tags.get("name", ""), win, refs, b.geometry,
                         f"Building inside area {area_id}", b.tags.get("building", "")))
    out = gpd.GeoDataFrame(rows, columns=CLASSIFIED_COLS, geometry="geometry", crs=27700)
    site_gdf = gpd.GeoDataFrame(sites, columns=["name", "category", "origin", "key", "geometry"],
                                geometry="geometry", crs=27700)
    if boundary is not None:
        out = out[out.geometry.representative_point().within(boundary)].copy()
        site_gdf = site_gdf[site_gdf.geometry.representative_point().within(boundary)].copy()
    return out, site_gdf


def load_os_sites(tags, boundary, area=paths.DEFAULT_AREA):
    """OS Open Map Local FunctionalSite polygons whose theme/classification maps to one of our rules."""
    os_rules = {(t.key, t.value): t for t in tags if t.source == "OS"}
    g = _read_layer("FunctionalSite", boundary, area)
    rows = []
    for r in g.itertuples(index=False):
        rule = next((os_rules[(r.SITETHEME, c.strip())] for c in str(r.CLASSIFICA).split(",")
                     if (r.SITETHEME, c.strip()) in os_rules), None)
        if rule:
            rows.append({"name": r.DISTNAME or "", "category": rule.category, "origin": "OS Functional Site",
                             "key": r.ID, "geometry": r.geometry})
    return gpd.GeoDataFrame(rows, columns=["name", "category", "origin", "key", "geometry"],
                            geometry="geometry", crs=27700)


def _read_layer(layer, boundary, area=paths.DEFAULT_AREA):
    files = paths.os_layer_paths(layer, tiles=sorted(config.load_area(area)["os_grid_squares"]), area=area)
    if not files:                                       # no tile of the area has this layer (e.g. no railway station at all)
        return gpd.GeoDataFrame({c: [] for c in source_checks.OS_LAYER_FIELDS[layer]}, geometry=[], crs=27700)
    g = pd.concat([gpd.read_file(f) for f in files], ignore_index=True)
    source_checks.require_columns(g.columns, source_checks.OS_LAYER_FIELDS[layer], f"The OS Open Map Local {layer} layer")
    # A feature that crosses the edge of two 100 km tiles is in both tiles' files under the same ID: keep it once (in England,
    # 87 buildings and 127 site outlines were counted twice).
    g = g.drop_duplicates(subset="ID", keep="first")
    g = gpd.GeoDataFrame(g, geometry=gpd.GeoSeries(g.geometry, crs=27700).force_2d(), crs=27700)
    return g[g.geometry.representative_point().within(boundary)]


def classify_os(tags, boundary, area=paths.DEFAULT_AREA):
    """Classify OS Open Map Local features: ImportantBuilding polygons and RailwayStation points."""
    os_rules = {(t.key, t.value): t for t in tags if t.source == "OS"}
    layers = [("ImportantBuilding", None), ("RailwayStation", "Rail Transport")]   # (layer, fixed theme)
    rows, total = [], 0
    for layer, fixed_theme in layers:
        g = _read_layer(layer, boundary, area)
        total += len(g)
        for r in g.itertuples(index=False):
            rule = os_rules.get((fixed_theme or r.BUILDGTHEM, r.CLASSIFICA))
            if rule:
                rows.append(_row("OS Important Buildings", r.ID, r.DISTNAME or "", rule, {rule.rule_ref}, r.geometry))
    return gpd.GeoDataFrame(rows, columns=CLASSIFIED_COLS, geometry="geometry", crs=27700), total


def _valid_polygonal(geom):
    """A valid version of an invalid polygon that is still polygonal (make_valid can return a collection with stray lines)."""
    fixed = shapely.make_valid(geom)
    if fixed.geom_type in ("Polygon", "MultiPolygon"):
        return fixed
    parts = [p for g in getattr(fixed, "geoms", [fixed]) for p in (g.geoms if g.geom_type == "MultiPolygon" else [g])
             if p.geom_type == "Polygon"]
    return unary_union(parts) if parts else geom.buffer(0)


def repair_geometries(gdf):
    """Make every invalid polygon in `gdf` valid. A self-intersecting outline (a figure of eight, a ring touching itself) is a mapping
    slip, common enough in a country's worth of data, and makes GEOS refuse to intersect it ('TopologyException'), which stopped
    the whole run."""
    gdf = gdf.copy()
    polygonal = gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
    bad = polygonal & ~gdf.geometry.is_valid
    if bad.any():
        gdf.loc[bad, gdf.geometry.name] = gdf.geometry[bad].map(_valid_polygonal).to_numpy()
        log.info("repaired %s invalid polygons", int(bad.sum()))
    return gdf


def classify_features(tags, boundary=None, area=paths.DEFAULT_AREA):
    """Classify every OSM and OS feature in the area with the rule tags. Returns osm, os, os_total, site candidates."""
    boundary = boundary if boundary is not None else area_boundary(area)
    osm, osm_sites = classify_osm(tags, boundary, area)
    os_, os_total = classify_os(tags, boundary, area)
    os_sites = load_os_sites(tags, boundary, area)
    return (repair_geometries(osm), repair_geometries(os_), os_total,
            repair_geometries(pd.concat([os_sites, osm_sites], ignore_index=True)))


# ====================================================================================================
# The authoritative registers (DfE GIAS, CQC, NHS ODS) as one table of points
# ====================================================================================================

REFERENCE_COLS = ["ref_source", "ref_id", "ref_name", "building_type", "status", "capacity", "capacity_unit", "detail",
        "rating", "provider", "postcode", "uprn", "ods_code", "ods_status", "ods_parent", "geometry"]


def map_type(source, row, mapping):
    """First matching active mapping row wins. Returns building type, 'IGNORE' or None (no match)."""
    for m in mapping:
        if m["source"] != source:
            continue
        v = row.get(m["field"])
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        v, target = str(v).strip().lower(), m["value"].lower()
        if (m["match"] == "Equals" and v == target) or (m["match"] == "Contains" and target in v):
            return m["building_type"]
    return None


def _norm_pc(x):
    return str(x).replace(" ", "").upper() if isinstance(x, str) else ""


@functools.lru_cache(maxsize=1)
def _postcode_table():
    """All Code-Point Open postcodes (about 1.7 million) as a table indexed by normalised postcode, columns EA / NO."""
    wanted = ("PC", "EA", "NO")          # postcode, easting, northing
    with zipfile.ZipFile(paths.bronze_file("os_codepoint_open", "codepo_gb.zip")) as z:
        cols = pd.read_csv(z.open("Doc/Code-Point_Open_Column_Headers.csv"), header=None).iloc[0].tolist()
        pos = [cols.index(c) for c in wanted]
        assert pos == sorted(pos), "Code-Point column order changed"        # the reader keeps file order
        frames = [pd.read_csv(z.open(n), header=None, names=list(wanted), usecols=pos, engine="pyarrow")
                  for n in z.namelist() if n.startswith("Data/CSV/") and n.endswith(".csv")]
    cp = pd.concat(frames)
    cp.index = cp["PC"].str.replace(" ", "", regex=False).str.upper()
    return cp[~cp.index.duplicated(keep="last")][["EA", "NO"]]


def postcode_points(wanted=None):
    """Postcode -> (easting, northing) from OS Code-Point Open, for the normalised postcodes in `wanted` (all if None)."""
    table = _postcode_table()
    if wanted is not None:
        table = table[table.index.isin(list(wanted))]
    return dict(zip(table.index, zip(table["EA"], table["NO"], strict=False), strict=False))


def _ods():
    """ODS lookups: code -> dict(name, postcode, status, parent, kind) from ets, epraccur and etr."""
    def columns(name, *positions):
        """The given columns of an ODS file as plain Python lists of strings ('' where empty): only these are parsed."""
        path = paths.bronze_file("nhs_ods", name)
        source_checks.check_ods_layout(name, pd.read_csv(path, header=None, dtype=str, nrows=500))      # the files have no header row
        df = pd.read_csv(path, header=None, dtype=str, usecols=list(positions)).fillna("")
        return [df[p].tolist() for p in positions]

    code, trust_name = columns("etr.csv", 0, 1)
    trust = dict(zip(code, trust_name, strict=False))
    out = {}
    for code, name, postcode, closed, parent in zip(*columns("ets.csv", 0, 1, 9, 11, 14), strict=False):
        out[code] = {"name": name, "postcode": postcode, "status": "Closed" if closed else "Active",
                     "parent": trust.get(parent, parent), "kind": "ets"}
    for code, name, postcode, status, parent in zip(*columns("epraccur.csv", 0, 1, 9, 12, 14), strict=False):
        out.setdefault(code, {"name": name, "postcode": postcode, "status": "Active" if status == "ACTIVE" else "Closed",
                              "parent": parent, "kind": "epraccur"})
    return out


def _cqc_locations(area=paths.DEFAULT_AREA):
    """CQC active locations as a conformed silver table (parquet), built once per bronze snapshot."""
    inputs = [("cqc_hsca", None)]
    cache = paths.conformed_lookup(area, "cqc_locations.parquet", inputs)
    if cache is not None:
        return pd.read_parquet(cache)
    cache = paths.silver_conformed_dir(area) / "cqc_locations.parquet"
    c = pd.read_excel(paths.bronze_file("cqc_hsca", "active_locations.ods"), engine="calamine",
                      sheet_name="HSCA_Active_Locations")
    c.to_parquet(cache)
    paths.conformed_stamp(area, "cqc_locations.parquet", inputs)
    return c


def load_reference(boundary, mapping, keep_ignored=False, area=paths.DEFAULT_AREA):
    """keep_ignored=True also returns IGNORE/unmapped records (building_type 'IGNORE' / 'UNMAPPED') for diagnostics."""
    ods = _ods()
    rows, dropped = [], {"unmapped": 0, "ignored": 0}

    # ---- DfE GIAS ----
    g = pd.read_csv(paths.bronze_file("dfe_gias", "edubasealldata.csv"), encoding="cp1252", low_memory=False)
    source_checks.require_columns(g.columns, source_checks.GIAS_COLUMNS, "The DfE GIAS file")
    source_checks.check_mapping_fields(mapping, "DfE", g.columns, "the DfE GIAS file")
    g = g.dropna(subset=["Easting", "Northing"])
    g = g[gpd.GeoSeries(gpd.points_from_xy(g.Easting, g.Northing), crs=27700, index=g.index).within(boundary)]
    for r, pt in zip(g.to_dict("records"), gpd.points_from_xy(g.Easting, g.Northing), strict=False):      # dicts and one point array: no per-row Series
        t = map_type("DfE", r, mapping)
        if t in (None, "IGNORE"):
            dropped["unmapped" if t is None else "ignored"] += 1
            if not keep_ignored:
                continue
            t = "IGNORE" if t else "UNMAPPED"
        pupils = r.get("NumberOfPupils")
        trust = r.get("Trusts (name)")
        rows.append({"ref_source": "DfE GIAS", "ref_id": str(r["URN"]), "ref_name": r["EstablishmentName"], "building_type": t,
                     "status": r["EstablishmentStatus (name)"], "capacity": pupils if pd.notna(pupils) else None,
                     "capacity_unit": "pupils",
                     "detail": f"{r['TypeOfEstablishment (name)']} / {r['PhaseOfEducation (name)']}",
                     "rating": "", "provider": trust if pd.notna(trust) else "",
                     "postcode": r.get("Postcode", ""), "uprn": "", "ods_code": "", "ods_status": "", "ods_parent": "",
                     "geometry": pt})

    # ---- CQC (HSCA active locations) ----
    c = _cqc_locations(area)
    source_checks.require_columns(c.columns, source_checks.CQC_COLUMNS, "The CQC active locations sheet")
    source_checks.check_mapping_fields(mapping, "CQC", c.columns, "the CQC active locations sheet")
    c = c.dropna(subset=["Location Latitude", "Location Longitude"])
    cg = gpd.GeoSeries(gpd.points_from_xy(c["Location Longitude"], c["Location Latitude"]), crs=4326,
                       index=c.index).to_crs(27700)
    inside = cg.within(boundary)
    c, cg = c[inside], cg[inside]
    cqc_ods = set()
    for r, pt in zip(c.to_dict("records"), cg, strict=False):          # c and cg were filtered together, so they line up
        code = str(r["Location ODS Code"]) if pd.notna(r["Location ODS Code"]) else ""
        t = map_type("CQC", r, mapping)
        if t in (None, "IGNORE"):
            dropped["unmapped" if t is None else "ignored"] += 1
            if not keep_ignored:
                continue
            t = "IGNORE" if t else "UNMAPPED"
        if code:
            cqc_ods.add(code)
        o = ods.get(code, {})
        beds = r["Care homes beds"]
        rating = r["Location Latest Overall Rating"]
        rows.append({"ref_source": "CQC", "ref_id": r["Location ID"], "ref_name": r["Location Name"], "building_type": t,
                         "status": "Active", "capacity": beds if pd.notna(beds) and beds > 0 else None,
                         "capacity_unit": "beds", "detail": r["Location Primary Inspection Category"],
                         "rating": rating if pd.notna(rating) else "", "provider": r["Provider Name"],
                         "postcode": r["Location Postal Code"],
                         "uprn": str(int(r["Location UPRN ID"])) if pd.notna(r["Location UPRN ID"]) else "",
                         "ods_code": code, "ods_status": o.get("status", ""), "ods_parent": o.get("parent", ""),
                         "geometry": pt})

    # ---- NHS ODS trust sites not already known to CQC (located by postcode) ----
    pc = postcode_points({_norm_pc(o["postcode"]) for o in ods.values() if o["kind"] == "ets"})
    for code, o in ods.items():
        if o["kind"] != "ets" or o["status"] != "Active" or code in cqc_ods:
            continue
        t = map_type("NHS ODS", {"Name": o["name"]}, mapping)
        if t in (None, "IGNORE"):
            continue
        xy = pc.get(_norm_pc(o["postcode"]))
        if xy is None:
            continue
        pt = gpd.points_from_xy([xy[0]], [xy[1]])[0]
        if not boundary.contains(pt):
            continue
        rows.append({"ref_source": "NHS ODS", "ref_id": code, "ref_name": o["name"].title(), "building_type": t,
                         "status": "Active", "capacity": None, "capacity_unit": "", "detail": "NHS trust site",
                         "rating": "", "provider": o["parent"], "postcode": o["postcode"], "uprn": "", "ods_code": code,
                         "ods_status": "Active", "ods_parent": o["parent"], "geometry": pt})
    out = gpd.GeoDataFrame(rows, columns=REFERENCE_COLS, geometry="geometry", crs=27700)
    # ODS lists some sites under several codes: keep one record per (name, postcode)
    key = out.ref_name.str.lower().str.replace(r"[^a-z0-9]", "", regex=True) + out.postcode.map(_norm_pc)
    out = out[~((out.ref_source == "NHS ODS") & key.duplicated(keep="first"))].copy()
    out.attrs["dropped"] = dropped
    return out


# ====================================================================================================
# The silver build and its cache
# ====================================================================================================

log = logging.getLogger(__name__)


@dataclass
class SilverTables:
    area: str
    boundary: object                 # shapely geometry (EPSG:27700)
    osm: gpd.GeoDataFrame            # classified OSM features
    os_: gpd.GeoDataFrame            # classified OS Important Buildings / stations
    os_total: int                    # all OS Important Buildings in the area (any type)
    site_cands: gpd.GeoDataFrame     # candidate site polygons (OS functional sites + OSM sites/areas)
    ref_all: gpd.GeoDataFrame        # every register record, incl. IGNORE / UNMAPPED (diagnostics, closed schools)
    ref: gpd.GeoDataFrame            # register records mapped to a building type
    postcodes: dict = field(default_factory=dict)   # postcode -> (easting, northing)


def build(rules, area=paths.DEFAULT_AREA):
    boundary = area_boundary(area)
    progress.step("Applying the rules to OpenStreetMap and OS features")
    osm, os_, os_total, site_cands = classify_features(rules.tags, boundary, area)
    progress.note("OSM features classified", len(osm))
    progress.note("OS buildings classified", len(os_))
    progress.note("OS buildings in area (any type)", os_total)
    progress.note("candidate site outlines", len(site_cands))
    progress.step("Loading and mapping the registers (DfE, CQC, NHS ODS)")
    ref_all = load_reference(boundary, rules.mapping, keep_ignored=True, area=area)
    ref = ref_all[~ref_all.building_type.isin(["IGNORE", "UNMAPPED"])].copy()
    progress.note("register records in area", len(ref_all))
    progress.note("register records mapped to a building type", len(ref))
    # gold only looks up the postcodes of register records, so keep just those (not all 1.7 million)
    postcodes = postcode_points({_norm_pc(p) for p in ref_all.postcode})
    return SilverTables(area, boundary, osm, os_, os_total, site_cands, ref_all, ref, postcodes)


# ------------------------------------------------------------------ caching: one silver build per distinct input set
SILVER_SCHEMA = 1        # bump when the silver tables' meaning or shape changes, to invalidate cached builds


def _code_fingerprint():
    """Short hash of the source files that decide what a silver table contains, so editing them invalidates cached builds
    (line endings are normalised so a Windows and a Linux checkout agree)."""
    h = hashlib.sha256()
    for f in (Path(__file__), Path(__file__).parent / "control" / "rules.py"):
        h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def silver_key(rules, area=paths.DEFAULT_AREA):
    """Same bronze inputs + rule set + area settings + silver code (+ same silver schema) = same silver build."""
    area_cfg = hashlib.sha256(json.dumps(config.load_area(area), sort_keys=True, default=str).encode()).hexdigest()[:12]
    raw = f"{SILVER_SCHEMA}|{_code_fingerprint()}|{area}|{area_cfg}|{paths.bronze_key(area)}|{content_hash(rules.snapshot)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def classified_dir(area, key):
    return paths.SILVER / area / "classified" / key


def save(silver, d):
    tmp = d.parent / f"_tmp_{d.name}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    silver.osm.to_parquet(tmp / "osm.parquet")
    silver.os_.to_parquet(tmp / "os.parquet")
    silver.site_cands.to_parquet(tmp / "site_candidates.parquet")
    silver.ref_all.to_parquet(tmp / "reference_all.parquet")
    pc = pd.DataFrame([(k, v[0], v[1]) for k, v in silver.postcodes.items()], columns=["pc", "e", "n"])
    pc.to_parquet(tmp / "postcodes.parquet")
    (tmp / "_meta.json").write_text(json.dumps({"os_total": int(silver.os_total), "area": silver.area}), encoding="utf-8")
    shutil.rmtree(d, ignore_errors=True)
    tmp.rename(d)                                   # atomic publish: a half-written build is never visible


def load(d, area):
    meta = json.loads((d / "_meta.json").read_text(encoding="utf-8"))
    ref_all = gpd.read_parquet(d / "reference_all.parquet")
    pc = pd.read_parquet(d / "postcodes.parquet")
    return SilverTables(
        area, area_boundary(area), gpd.read_parquet(d / "osm.parquet"), gpd.read_parquet(d / "os.parquet"),
        meta["os_total"], gpd.read_parquet(d / "site_candidates.parquet"), ref_all,
        ref_all[~ref_all.building_type.isin(["IGNORE", "UNMAPPED"])].copy(), dict(zip(pc.pc, zip(pc.e, pc.n, strict=False), strict=False)))


def build_or_load(rules, area=paths.DEFAULT_AREA, reuse=True):
    """Returns (silver tables, build key, reused?)."""
    key = silver_key(rules, area)
    d = classified_dir(area, key)
    if reuse and (d / "_meta.json").exists():
        progress.step(f"Reusing the cached silver build {key} (same sources, rules, area settings and code as before)")
        progress.note("silver cache", "reused")
        progress.note("silver key", key)
        return load(d, area), key, True
    progress.note("silver cache", "rebuilt")
    progress.note("silver key", key)
    silver = build(rules, area)
    progress.step("Saving the silver build for reuse")
    save(silver, d)
    return silver, key, False
