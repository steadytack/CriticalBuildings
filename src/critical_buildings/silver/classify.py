"""Apply the rule tags to OSM and OS Open Map Local features.

Every returned row has: source, source_id, name, rule_ref, category, building_type, importance, driver,
matched_refs (all rules that matched), note, geometry (EPSG:27700).
"""
import glob
from pathlib import Path

import geopandas as gpd
import pandas as pd

from critical_buildings import config, paths
from critical_buildings.control.ruleset import AREA_TAGS
from critical_buildings.silver import osm_geom

IMP_RANK = {"High": 0, "Medium": 1, "Low": 2}
COLS = ["source", "source_id", "name", "rule_ref", "category", "building_type", "importance", "driver",
        "matched_refs", "note", "rule_notes", "osm_building", "geometry"]


def _best(matches):
    """Pick the winning rule: lowest Priority number, then higher importance, then lowest ref."""
    return sorted(matches, key=lambda t: (t.priority, IMP_RANK[t.importance], t.rule_ref))[0]


def _osm_match(tags, rule):
    v = tags.get(rule.key)
    if v is None:
        return False
    if rule.value == "*":
        return v.strip().lower() != "no"
    return rule.value in [s.strip() for s in v.split(";")]


def _row(source, sid, name, rule, refs, geom, note="", osm_building=""):
    return dict(source=source, source_id=sid, name=name, rule_ref=rule.rule_ref, category=rule.category,
                building_type=rule.building_type, importance=rule.importance, driver=rule.driver,
                matched_refs=";".join(str(r) for r in sorted(refs)), note=note, rule_notes=rule.notes, osm_building=osm_building, geometry=geom)


def classify_osm(tags, boundary=None, area=paths.DEFAULT_AREA):
    osm_rules = [t for t in tags if t.source == "OSM"]
    feats = osm_geom.load_elements(paths.bronze_file("osm_overpass", "osm_features.json", area=area))
    rows, areas, sites = [], [], []
    for _, f in feats.iterrows():
        hits = [r for r in osm_rules if _osm_match(f.tags, r)]
        if not hits:
            continue
        refs = {h.rule_ref for h in hits}
        non_area = [h for h in hits if (h.key, h.value) not in AREA_TAGS]
        if non_area:
            win = _best(non_area)
            rows.append(_row("OSM", f.osm_id, f.tags.get("name", ""), win, refs, f.geometry,
                             osm_building=f.tags.get("building", "")))
            if f.geometry.geom_type in ("Polygon", "MultiPolygon"):
                sites.append(dict(name=f.tags.get("name", ""), category=win.category, origin="OSM site",
                                  key=f.osm_id, geometry=f.geometry))
        else:
            areas.append((f, _best(hits), refs))

    # area-type features (port, military land, aerodrome): classify the buildings inside them
    own_ids = {r["source_id"] for r in rows}
    blds = osm_geom.load_elements(paths.bronze_file("osm_overpass", "osm_area_buildings.json", area=area))
    blds = blds[~blds.osm_id.isin(own_ids)]
    pts = blds.geometry.representative_point()
    inherited = {}
    for f, win, refs in areas:
        if f.geometry.geom_type in ("Polygon", "MultiPolygon"):
            sites.append(dict(name=f.tags.get("name", ""), category=win.category, origin="OSM area",
                              key=f.osm_id, geometry=f.geometry))
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
    out = gpd.GeoDataFrame(rows, columns=COLS, geometry="geometry", crs=27700)
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
    for _, r in g.iterrows():
        rule = next((os_rules[(r.SITETHEME, c.strip())] for c in str(r.CLASSIFICA).split(",")
                     if (r.SITETHEME, c.strip()) in os_rules), None)
        if rule:
            rows.append(dict(name=r.DISTNAME or "", category=rule.category, origin="OS Functional Site",
                             key=r.ID, geometry=r.geometry))
    return gpd.GeoDataFrame(rows, columns=["name", "category", "origin", "key", "geometry"],
                            geometry="geometry", crs=27700)


def _read_layer(layer, boundary, area=paths.DEFAULT_AREA):
    g = pd.concat([gpd.read_file(f) for f in paths.os_layer_paths(layer, tiles=sorted(config.load_area(area)["os_grid_squares"]))], ignore_index=True)
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
        for _, r in g.iterrows():
            rule = os_rules.get((fixed_theme or r.BUILDGTHEM, r.CLASSIFICA))
            if rule:
                rows.append(_row("OS Important Buildings", r.ID, r.DISTNAME or "", rule, {rule.rule_ref}, r.geometry))
    return gpd.GeoDataFrame(rows, columns=COLS, geometry="geometry", crs=27700), total


def run(tags, boundary=None, area=paths.DEFAULT_AREA):
    """Classify every OSM and OS feature in the area with the rule tags. Returns osm, os, os_total, site candidates."""
    boundary = boundary if boundary is not None else osm_geom.area_boundary(area)
    osm, osm_sites = classify_osm(tags, boundary, area)
    os_, os_total = classify_os(tags, boundary, area)
    os_sites = load_os_sites(tags, boundary, area)
    return osm, os_, os_total, pd.concat([os_sites, osm_sites], ignore_index=True)
