"""Build the UPRN property table for an area: one row per OS Open UPRN inside the area, linked to the open datasets in layers.py.

    python -m critical_buildings properties --area suffolk

Output (not in git): data/properties/<area>/<run id>/table/tile=<TM>/part-0.parquet, one folder per 100 km OS grid square (point any
parquet reader at table/), plus lineage.json (what went in), columns.csv (what each column means) and summary.json (how many rows had
each link) beside it. CURRENT.json in the area folder points at the latest run.

How it scales: the UPRN file is read once and split by grid square; each square is then linked on its own, so memory is bounded by the
largest square (a few million UPRNs) and not by the size of the area.
"""
import datetime as dt
import json
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from pyogrio import read_dataframe

from critical_buildings import (
    __version__,
    areas,
    config,
    lineage,
    locks,
    paths,
    postcodes,
    progress,
)
from critical_buildings.errors import PipelineError
from critical_buildings.properties import columns as cols
from critical_buildings.properties import grid, inputs, layers

REQUIRED = [("os_open_uprn", False), ("os_codepoint_open", False), ("os_open_map_local", True), ("osm_overpass", True),
            ("ons_lsoa", False), ("iod2025", False), ("planning_data", False), ("ea_flood_zones", False)]


def now():
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def properties_dir(area):
    return paths.DATA / "properties" / area


def current_run_id(area):
    p = properties_dir(area) / "CURRENT.json"
    return json.loads(p.read_text(encoding="utf-8"))["run_id"] if p.exists() else None


def check(area):
    """Plain-English problems that would stop the build (empty = ready)."""
    problems = []
    try:
        config.load_area(area)
    except PipelineError as e:
        return [str(e)]
    for source, scoped in REQUIRED:
        if not paths.latest_snapshot(source, area if scoped else None):
            how = f"fetch --area {area} --only osm,os" if scoped else "fetch --only properties"
            problems.append(f"No data for '{source}'. Run: python -m critical_buildings {how}")
    return problems


class Context:
    """Everything shared by all grid squares: the datasets clipped to the area, loaded once."""

    def __init__(self, area, boundary):
        cfg = config.get().properties
        self.cfg = cfg
        self.area = area
        bbox = boundary.bounds
        box_wgs = inputs.bbox_wgs84(bbox)
        progress.step("Reading the census areas (LSOA) and deprivation scores")
        lsoa = inputs.load_lsoa(bbox)
        imd = inputs.load_imd()
        self.lsoa_geoms = lsoa.geometry.to_numpy()
        shapely.prepare(self.lsoa_geoms)
        position = imd.index.get_indexer(lsoa["lsoa_code"])                       # each LSOA polygon's row in the deprivation table (-1: none)
        known = position >= 0
        self.lsoa_codes = lsoa["lsoa_code"].to_numpy(object)
        self.imd = {}
        for c in imd.columns:                                                       # one value per LSOA polygon; missing where it has no scores
            col = imd[c].to_numpy()
            if col.dtype == object:
                self.imd[c] = np.where(known, col[position] if len(col) else None, None)
            else:
                self.imd[c] = np.where(known, col[position].astype(float) if len(col) else np.nan, np.nan)
        progress.note("LSOAs in the area", len(lsoa))
        progress.step("Reading the planning layers (conservation areas, listed buildings, Article 4 directions)")
        ca, self.conservation_polys = inputs.planning_polygons("conservation-area", ["name"], box_wgs)
        self.conservation_names = ca["name"].fillna("").to_numpy(object) if len(ca) else np.array([], dtype=object)
        _, self.article4_polys = inputs.planning_polygons("article-4-direction-area", [], box_wgs)
        _, self.listed_polys = inputs.planning_polygons("listed-building-outline", [], box_wgs)
        grade, lx, ly = inputs.listing_points(box_wgs)
        self.listing_grades = grade
        self.listing_points = shapely.points(lx, ly) if len(lx) else np.array([], dtype=object)
        progress.note("conservation areas", len(self.conservation_polys))
        progress.note("listed building outlines", len(self.listed_polys))
        progress.note("listing points", len(self.listing_points))
        progress.step("Reading the flood zones (a 2 GB file the first time, then kept)")
        self.flood = inputs.flood_layer(area, box_wgs)
        progress.note("flood zone 3 polygons", self.flood.counts["3"])
        progress.note("flood zone 2 polygons", self.flood.counts["2"])
        progress.step("Reading the postcode points (Code-Point Open)")
        self.postcodes = layers.PostcodeIndex(postcodes.postcode_table())
        self.site_run, sites = inputs.critical_sites(area)
        if sites is None:
            progress.warn(f"{area} has no current critical-buildings run, so the critical_site columns will be empty. "
                          f"Run: python -m critical_buildings run --area {area}")
            self.site_geoms, self.sites = np.array([], dtype=object), None
        else:
            self.site_geoms, self.sites = sites.geometry.to_numpy(), sites
            shapely.prepare(self.site_geoms)
            progress.note("critical sites linked (from run)", f"{len(sites)} ({self.site_run})")


def stage_uprns(zip_path, boundary, tiles, folder):
    """Split the UPRNs inside the area by grid square into one parquet file per square. Returns {tile: (path, rows)}."""
    minx, miny, maxx, maxy = boundary.bounds
    shapely.prepare(boundary)
    wanted = set(tiles)
    schema = pa.schema([("uprn", pa.int64()), ("x", pa.float64()), ("y", pa.float64()), ("lat", pa.float64()), ("lon", pa.float64())])
    writers, rows, read = {}, {}, 0
    try:
        for batch in inputs.uprn_batches(zip_path):
            read += len(batch["uprn"])
            keep = (batch["x"] >= minx) & (batch["x"] <= maxx) & (batch["y"] >= miny) & (batch["y"] <= maxy)
            if not keep.any():
                continue
            part = {k: v[keep] for k, v in batch.items()}
            part_ok = shapely.contains_xy(boundary, part["x"], part["y"])
            part = {k: v[part_ok] for k, v in part.items()}
            tile = grid.tile_of(part["x"], part["y"])
            for t in np.unique(tile):
                if t not in wanted:
                    continue
                m = tile == t
                if t not in writers:
                    writers[t] = pq.ParquetWriter(Path(folder) / f"{t}.parquet", schema, compression="zstd")
                    rows[t] = 0
                writers[t].write_table(pa.table({k: v[m] for k, v in part.items()}, schema=schema))
                rows[t] += int(m.sum())
    finally:
        for w in writers.values():
            w.close()
    progress.note("OS Open UPRN points read", read)
    return {t: (Path(folder) / f"{t}.parquet", n) for t, n in rows.items()}


def link_tile(tile, table, ctx):
    """The finished arrow table for one grid square's UPRNs."""
    area, cfg = ctx.area, ctx.cfg
    uprn = table["uprn"].to_numpy()
    x, y, lat, lon = (table[c].to_numpy() for c in ("x", "y", "lat", "lon"))
    n = len(uprn)
    out = {"uprn": pa.array(uprn), "easting": pa.array(x), "northing": pa.array(y), "latitude": pa.array(lat), "longitude": pa.array(lon),
           "os_tile": layers.coded(np.zeros(n, dtype=np.int32), [tile])}

    # ---- building footprint
    files = paths.os_layer_paths("Building", tiles=[tile.lower()], area=area)
    if files:
        b = read_dataframe(files[0], columns=["ID"])
        footprints = shapely.force_2d(b.geometry.to_numpy())
        ids = b["ID"].to_numpy(object)
    else:
        footprints, ids = np.array([], dtype=object), np.array([], dtype=object)
    bidx, barea, bcount = layers.building_link(x, y, footprints)
    out["building_id"] = layers.lookup(bidx, ids, pa.string())
    out["building_area_m2"] = pa.array(barea, from_pandas=True)
    out["uprns_in_building"] = pa.array(bcount)

    # ---- postcode
    pidx, pdist = ctx.postcodes.nearest(x, y, cfg.postcode_max_m)
    out["postcode"] = ctx.postcodes.display(pidx)
    out["postcode_distance_m"] = pa.array(pdist, from_pandas=True)

    # ---- census area and deprivation
    lidx = layers.inside(x, y, ctx.lsoa_geoms)
    method = np.where(lidx >= 0, 0, -1)
    missing = np.flatnonzero(lidx < 0)
    if len(missing) and len(ctx.lsoa_geoms):
        near, _ = layers.nearest(x[missing], y[missing], ctx.lsoa_geoms, cfg.lsoa_near_m)
        lidx[missing] = near
        method[missing[near >= 0]] = 1
    out["lsoa_code"] = layers.lookup(lidx, ctx.lsoa_codes, pa.string())
    out["lsoa_method"] = layers.coded(method, ["inside", "nearest"])
    for name in ("lsoa_name", "lad_code", "lad_name"):
        out[name] = layers.lookup(lidx, ctx.imd[name], pa.string())
    for name in cols.NAMES:
        if name in ctx.imd and name not in out:
            out[name] = layers.lookup(lidx, ctx.imd[name], pa.float64() if name == "imd_score" else pa.int64())

    # ---- planning
    ca = layers.inside(lon, lat, ctx.conservation_polys)
    out["conservation_area"] = pa.array(ca >= 0)
    out["conservation_area_name"] = layers.lookup(ca, ctx.conservation_names, pa.string())
    out["article_4_area"] = pa.array(layers.inside(lon, lat, ctx.article4_polys) >= 0)
    out["listed_in_outline"] = pa.array(layers.inside(lon, lat, ctx.listed_polys) >= 0)
    gidx, gdist = layers.nearest(x, y, ctx.listing_points, cfg.listed_near_m)
    out["listed_grade"] = layers.lookup(gidx, ctx.listing_grades, pa.string())
    out["listed_distance_m"] = pa.array(gdist, from_pandas=True)

    # ---- flood
    flood = ctx.flood.for_tile(tile)                                    # only this square's polygons are ever in memory
    zone, in3, in2 = layers.flood_zone(lon, lat, flood[3][0], flood[2][0])
    out["flood_zone"] = pa.array(zone)
    ftype = np.full(n, None, dtype=object)
    in_3 = in3 >= 0
    ftype[in_3] = flood[3][1][in3[in_3]]
    only_2 = ~in_3 & (in2 >= 0)
    ftype[only_2] = flood[2][1][in2[only_2]]
    out["flood_zone_type"] = pa.array(ftype, type=pa.string())

    # ---- critical sites
    if ctx.sites is not None:
        sidx, sdist = layers.nearest(x, y, ctx.site_geoms, cfg.critical_site_near_m)
    else:
        sidx, sdist = np.full(n, -1), np.full(n, np.nan)
    sites = ctx.sites
    out["critical_site_id"] = layers.lookup(sidx, sites["site_id"].to_numpy(object) if sites is not None else [], pa.string())
    out["critical_site_type"] = layers.lookup(sidx, sites["site_type"].to_numpy(object) if sites is not None else [], pa.string())
    out["critical_site_importance"] = layers.lookup(sidx, sites["importance"].to_numpy(object) if sites is not None else [], pa.string())
    out["critical_site_distance_m"] = pa.array(sdist, from_pandas=True)

    extra = sorted(set(out) - set(cols.NAMES))
    missing_cols = [c for c in cols.NAMES if c not in out]
    if extra or missing_cols:
        raise PipelineError(f"The property table's columns differ from its description (undescribed: {extra}; not produced: {missing_cols}).")
    return pa.table({c: out[c] for c in cols.NAMES}), {
        "rows": n, "in_building": int((bidx >= 0).sum()), "with_postcode": int((pidx >= 0).sum()),
        "lsoa_inside": int((method == 0).sum()), "lsoa_nearest": int((method == 1).sum()), "with_deprivation": int((~np.isnan(
            np.asarray(out["imd_score"].to_numpy(zero_copy_only=False), dtype=float))).sum()),
        "conservation_area": int((ca >= 0).sum()), "article_4_area": int(out["article_4_area"].to_numpy(zero_copy_only=False).sum()),
        "listed_in_outline": int(out["listed_in_outline"].to_numpy(zero_copy_only=False).sum()),
        "flood_zone_3": int((zone == 3).sum()), "flood_zone_2": int((zone == 2).sum()),
        "in_critical_site": int((sdist == 0).sum()), "near_critical_site": int(((sdist > 0) & (sdist <= cfg.critical_site_near_m)).sum())}


def build(area=paths.DEFAULT_AREA, tiles=None):
    """Build the property table for `area` (optionally only the named OS grid squares). Returns a summary dict."""
    problems = check(area)
    if problems:
        raise PipelineError("Cannot build the property table:\n  " + "\n  ".join(problems))
    with locks.pipeline_lock("properties"):
        return _build(area, tiles)


def _build(area, tiles):
    started = now()
    boundary = areas.area_boundary(area)
    squares = [t.upper() for t in (tiles or config.load_area(area)["os_grid_squares"])]
    run_id = paths.new_run_id(area, lambda r: (properties_dir(area) / r).exists())
    dest = properties_dir(area) / run_id
    dest.mkdir(parents=True)
    try:
        ctx = Context(area, boundary)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            progress.step("Splitting the UPRNs inside the area by grid square")
            staged = stage_uprns(paths.bronze_file("os_open_uprn", "osopenuprn_csv.zip"), boundary, squares, tmp)
            totals = {}
            for tile in sorted(staged):
                path, rows = staged[tile]
                progress.step(f"Linking grid square {tile} ({rows:,} UPRNs)")
                table, stats = link_tile(tile, pq.read_table(path), ctx)
                (dest / "table" / f"tile={tile}").mkdir(parents=True)
                pq.write_table(table, dest / "table" / f"tile={tile}" / "part-0.parquet", compression="zstd")
                for k, v in stats.items():
                    totals[k] = totals.get(k, 0) + v
        _write_documents(dest, area, run_id, started, ctx, totals, sorted(staged))
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)         # a failed build leaves no half-written table
        raise
    (properties_dir(area) / "CURRENT.json").write_text(json.dumps({"run_id": run_id}, indent=2), encoding="utf-8")
    progress.step(f"property table {run_id} done: {totals.get('rows', 0):,} UPRNs in {len(staged)} grid squares")
    return {"run_id": run_id, "dest": dest, "rows": totals.get("rows", 0), "tiles": sorted(staged), "summary": totals}


ATTRIBUTION_SOURCES = ["os_open_uprn", "os_codepoint_open", "os_open_map_local", "ons_lsoa", "iod2025", "planning_data", "ea_flood_zones",
                       "osm_overpass"]          # the last only draws the outline of the area the table is clipped to


def attribution_text(site_run_attribution=None):
    """The credit lines to include when the table is shared: one block per source it used (from the source catalogue), plus the
    critical-buildings dataset's own credits when the critical_site columns are filled."""
    catalogue = config.load_sources()
    lines = ["ATTRIBUTION AND LICENCES: UPRN property table", "",
             "This table combines the open datasets below. Include these lines when you share or publish it.", ""]
    for key in ATTRIBUTION_SOURCES:
        cat = catalogue[key]
        lines += [f"{cat['name']} - {cat['licence']}", f"  {' '.join(str(cat['attribution']).split())}", ""]
    if site_run_attribution:
        lines += ["The critical_site_* columns come from the critical-buildings dataset, whose own credits are:", "", site_run_attribution.strip(), ""]
    lines += ["Note: OpenStreetMap data is under the ODbL (share-alike). Check with your information governance or legal team before "
              "distributing this table outside the organisation.", ""]
    return "\n".join(lines)


def _write_documents(dest, area, run_id, started, ctx, totals, tiles):
    pd.DataFrame(cols.COLUMNS, columns=["column", "group", "description"]).to_csv(dest / "columns.csv", index=False)
    site_credits = None
    if ctx.site_run:
        credits = paths.find_output(paths.run_dir(area, ctx.site_run), "buildings")
        credits = Path(credits).parent / "ATTRIBUTION.txt" if credits else None
        site_credits = credits.read_text(encoding="utf-8") if credits and credits.exists() else None
    (dest / "ATTRIBUTION.txt").write_text(attribution_text(site_credits), encoding="utf-8")
    (dest / "summary.json").write_text(json.dumps(totals, indent=2), encoding="utf-8")
    (dest / "lineage.json").write_text(json.dumps({
        "run_id": run_id, "area": area, "started_at": started, "finished_at": now(), "tool_version": __version__, "git": lineage.git_info(),
        "settings": dict(vars(config.get().properties)),
        "critical_buildings_run": ctx.site_run, "tiles": tiles, "rows": totals.get("rows", 0),
        "bronze": {"snapshots": lineage.bronze_inputs(area, only=ATTRIBUTION_SOURCES)}, "coverage": totals,
        "built_on": dt.date.today().isoformat()}, indent=2, default=str), encoding="utf-8")  # noqa: DTZ011 - local time is intended
