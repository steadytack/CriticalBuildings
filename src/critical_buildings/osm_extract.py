"""Read the OpenStreetMap features the rules need from a PBF extract (e.g. Geofabrik's england-latest.osm.pbf).

The Overpass API cannot answer a query for a whole country (it times out), but an extract can be read directly with GDAL's OSM
driver, which streams the file: memory stays small however big the extract is. The result has the same shape as the Overpass
route in areas.load_elements, a table of (osm_id, tags, geometry), so the classification code does not care where it came from.

Only the tags the rules use are kept (plus the name and the building tag), so the reader is told exactly which keys to look at,
the same way the Overpass query is built from the active rule tags.
"""
import hashlib
import json
import re
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import shapely
from pyogrio import read_dataframe
from pyogrio.raw import open_arrow

from critical_buildings import progress
from critical_buildings.control.rules import AREA_TAGS

COLUMN_KEY = re.compile(r"^[a-z_]+$")            # keys that can be a plain column; keys like plant:output:hot_water stay in other_tags
ALWAYS = ("name", "building")                    # read for every feature: the name is shown, the building tag drives roles
HSTORE = re.compile(r'"((?:[^"\\]|\\.)*)"=>"((?:[^"\\]|\\.)*)"')
# nodes, then closed ways and relations (areas). Open ways (GDAL's 'lines' layer) are left out: every pass over a country's file
# takes minutes and England has 3 features of that kind, which are mapping mistakes (an area tagged on a way that is not closed).
LAYERS = ("points", "multipolygons")


def osm_keys(osm_tags):
    return sorted({k for k, _ in osm_tags})


def config_text(osm_tags):
    """An osmconf.ini for GDAL: which keys become columns, and that every closed way is an area (as the Overpass route does)."""
    keys = osm_keys(osm_tags)
    columns = sorted({*ALWAYS, *[k for k in keys if COLUMN_KEY.match(k)]})
    layer = f"other_tags=yes\nall_tags=no\nosm_id=yes\nattributes={','.join(columns)}\n"
    return ("[general]\n"
            # building is always an area: the buildings inside port, military and aerodrome areas are read whatever the rules say
            f"closed_ways_are_polygons={','.join(sorted({*keys, 'building'}))}\n"
            "report_all_nodes=no\nreport_all_ways=no\n"
            f"[points]\n{layer}[lines]\n{layer}[multipolygons]\n{layer}osm_way_id=yes\n"
            "[multilinestrings]\nother_tags=yes\n[other_relations]\nother_tags=yes\n")


def _quote(v):
    return "'" + str(v).replace("'", "''") + "'"


def _like(key, value=None):
    """other_tags LIKE '%"key"=>"value"%' (or any value): the hstore text GDAL keeps the other tags in."""
    pattern = '%"' + key + '"=>' + ('%' if value is None else '"' + value + '"%')
    return "other_tags LIKE " + _quote(pattern)


def where_clause(osm_tags):
    """OGR SQL that keeps a feature when it has any of the active (key, value) tags ('*' = any value)."""
    by_key = {}
    for k, v in osm_tags:
        by_key.setdefault(k, set()).add(v)
    parts = []
    for k, values in sorted(by_key.items()):
        if COLUMN_KEY.match(k):
            if "*" in values:
                parts.append(k + " IS NOT NULL")
            else:
                parts.append(k + " IN (" + ", ".join(_quote(v) for v in sorted(values)) + ")")
        elif "*" in values:
            parts.append(_like(k))
        else:
            parts += [_like(k, v) for v in sorted(values)]
    return " OR ".join(parts)


def parse_hstore(text):
    return dict(HSTORE.findall(text)) if isinstance(text, str) else {}


def _tags(row, columns, osm_tags_by_key):
    tags = {c: row[c] for c in columns if isinstance(row[c], str) and row[c] != ""}
    extra = parse_hstore(row["other_tags"]) if "other_tags" in row else {}
    tags.update({k: v for k, v in extra.items() if k in osm_tags_by_key})
    return tags


def _to_rows(df, layer, osm_tags):
    """One layer's GDAL rows as (osm_id, tags, geometry) rows in WGS84, geometry made like areas.element_geometry."""
    columns = [c for c in df.columns if c not in ("geometry", "other_tags", "osm_id", "osm_way_id")]
    keys = set(osm_keys(osm_tags))
    rows = []
    for rec in df.to_dict("records"):
        geom = rec["geometry"]
        if geom is None or geom.is_empty:
            continue
        if layer == "lines":
            geom, kind, oid = geom.centroid, "way", rec["osm_id"]
        elif layer == "multipolygons":
            way = rec.get("osm_way_id")
            kind, oid = ("way", way) if isinstance(way, str) and way else ("relation", rec["osm_id"])
        else:
            kind, oid = "node", rec["osm_id"]
        rows.append({"osm_id": f"{kind}/{oid}", "tags": _tags(rec, columns, keys), "geometry": geom})
    return rows


def _open_options(osm_tags, tmp):
    ini = Path(tmp) / "osmconf.ini"
    ini.write_text(config_text(osm_tags), encoding="utf-8")
    return {"CONFIG_FILE": str(ini)}


def _read_layer(pbf, layer, where, bbox, opts):
    """One layer of the extract (runs in its own process)."""
    return read_dataframe(pbf, layer=layer, where=where, bbox=bbox, **opts)


def read_features(pbf, osm_tags, bbox=None):
    """The features carrying any active rule tag, as a GeoDataFrame(osm_id, tags, geometry) in British National Grid.

    bbox: (minx, miny, maxx, maxy) in WGS84 to read only part of the extract (the area's bounds)."""
    where = where_clause(osm_tags)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        opts = _open_options(osm_tags, tmp)
        progress.step("Reading the points and the areas of the OpenStreetMap extract (a pass over the whole file for each, "
                      "in two processes)")
        # Separate processes, not threads: two GDAL OSM readers in one process slowed each other to a crawl (13 minutes for
        # England against 8 one after the other), whereas two processes each run at full speed.
        with ProcessPoolExecutor(max_workers=len(LAYERS)) as pool:
            frames = list(pool.map(_read_layer, [pbf] * len(LAYERS), LAYERS, [where] * len(LAYERS), [bbox] * len(LAYERS),
                                   [opts] * len(LAYERS)))
        for layer, df in zip(LAYERS, frames, strict=True):
            progress.note(f"OSM {layer} read", len(df))
            rows += _to_rows(df, layer, osm_tags)
    return gpd.GeoDataFrame(rows, columns=["osm_id", "tags", "geometry"], geometry="geometry", crs=4326).to_crs(27700)


def read_buildings_inside(pbf, osm_tags, areas, bbox=None, batch_size=100_000):
    """Every OSM building whose representative point lies inside one of `areas` (polygons in British National Grid), as the
    same kind of table as read_features. Buildings are streamed in batches and only those inside are kept, so even a country's
    15 million buildings never sit in memory together."""
    if not len(areas):
        return gpd.GeoDataFrame(columns=["osm_id", "tags", "geometry"], geometry="geometry", crs=27700)
    # The test is made in lon/lat, as the file stores it: reprojecting 15 million buildings to British National Grid just to ask
    # 'is it inside one of a few thousand areas' was the slow part. Only the buildings that are inside are reprojected.
    tree = shapely.STRtree(gpd.GeoSeries(list(areas), crs=27700).to_crs(4326).to_numpy())
    kept = []
    with tempfile.TemporaryDirectory() as tmp:
        opts = _open_options(osm_tags, tmp)
        with open_arrow(pbf, layer="multipolygons", where="building IS NOT NULL", bbox=bbox, batch_size=batch_size,
                        use_pyarrow=True, **opts) as (meta, reader):
            geom_col = meta["geometry_name"] or "wkb_geometry"
            for batch in reader:
                geoms = shapely.from_wkb(batch.column(geom_col).to_numpy(zero_copy_only=False))
                hit = np.unique(tree.query(shapely.point_on_surface(geoms), predicate="within")[0])
                if len(hit):
                    sub = batch.take(pa.array(hit)).to_pandas()
                    sub["geometry"] = gpd.GeoSeries(geoms[hit], crs=4326).to_crs(27700).to_numpy()
                    kept.append(sub)
    if not kept:
        return gpd.GeoDataFrame(columns=["osm_id", "tags", "geometry"], geometry="geometry", crs=27700)
    df = pd.concat(kept, ignore_index=True)
    rows = []
    keys = set(osm_keys(osm_tags))
    columns = [c for c in df.columns if c not in ("geometry", "other_tags", "osm_id", "osm_way_id", "wkb_geometry")]
    for rec in df.to_dict("records"):
        way = rec.get("osm_way_id")
        kind, oid = ("way", way) if isinstance(way, str) and way else ("relation", rec["osm_id"])
        rows.append({"osm_id": f"{kind}/{oid}", "tags": _tags(rec, columns, keys), "geometry": rec["geometry"]})
    return gpd.GeoDataFrame(rows, columns=["osm_id", "tags", "geometry"], geometry="geometry", crs=27700)


def cache_name(osm_tags, bbox):
    """File name for the cached read of an extract: it depends on the tags asked for and the part of the file read."""
    key = json.dumps([sorted(osm_tags), None if bbox is None else [round(float(v), 4) for v in bbox]])
    return "osm_extract_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] + ".parquet"


def save_cache(path, feats, blds):
    """The features and the area buildings in one parquet file (tags as JSON text)."""
    both = pd.concat([feats.assign(kind="feature"), blds.assign(kind="building")], ignore_index=True)
    both["tags"] = both["tags"].map(json.dumps)
    gpd.GeoDataFrame(both, geometry="geometry", crs=27700).to_parquet(path)


def load_cache(path):
    both = gpd.read_parquet(path)
    both["tags"] = both["tags"].map(json.loads)
    feats = both[both.kind == "feature"].drop(columns="kind").reset_index(drop=True)
    blds = both[both.kind == "building"].drop(columns="kind").reset_index(drop=True)
    return feats, blds


def read_all(pbf, osm_tags, bbox=None):
    """The tagged features and the buildings inside the area-type features (port, military land, aerodrome), read from the extract.

    The buildings are those inside any area-type polygon (a superset of what the rules end up using), so the result depends only
    on the extract and the tags asked for: it can be cached."""
    feats = read_features(pbf, osm_tags, bbox)
    progress.step("Finding the buildings inside port, military and aerodrome areas")
    return feats, read_buildings_inside(pbf, osm_tags, area_geometries(feats), bbox)


def area_geometries(features):
    """The area-type features (port, military land, aerodrome) among `features`: where buildings inherit the area's rule."""
    def is_area(tags):
        return any(tags.get(k) == v for k, v in AREA_TAGS)
    return [g for tags, g in zip(features["tags"], features.geometry, strict=True)
            if is_area(tags) and g.geom_type in ("Polygon", "MultiPolygon")]
