"""Reading the datasets the property table links to, each clipped to the area being built.

Everything here returns plain arrays and small frames; the linking itself is in layers.py. The two big sources (the flood zones, 2 GB of
text, and OS Open UPRN) are streamed in batches, and the flood zones are kept as a conformed table so a rerun does not parse them again.
"""
import datetime as dt
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import shapely
from pyogrio import read_dataframe
from pyproj import Transformer

from critical_buildings import paths, source_checks
from critical_buildings.errors import PipelineError

_TO_BNG = Transformer.from_crs(4326, 27700, always_xy=True)
_TO_WGS = Transformer.from_crs(27700, 4326, always_xy=True)
CHUNK_ROWS = 20_000

# ---- the deprivation file's long column names -> ours (matched by the start of the name, which is stable across re-issues)
IMD_PREFIXES = {
    "lsoa_code": "LSOA code", "lsoa_name": "LSOA name", "lad_code": "Local Authority District code",
    "lad_name": "Local Authority District name",
    "imd_score": "Index of Multiple Deprivation (IMD) Score", "imd_rank": "Index of Multiple Deprivation (IMD) Rank",
    "imd_decile": "Index of Multiple Deprivation (IMD) Decile",
    "income_decile": "Income Decile", "employment_decile": "Employment Decile", "education_decile": "Education, Skills and Training Decile",
    "health_decile": "Health Deprivation and Disability Decile", "crime_decile": "Crime Decile",
    "housing_barriers_decile": "Barriers to Housing and Services Decile", "living_environment_decile": "Living Environment Decile",
    "idaci_decile": "Income Deprivation Affecting Children Index (IDACI) Decile",
    "idaopi_decile": "Income Deprivation Affecting Older People (IDAOPI) Decile",
    "lsoa_population": "Total population", "lsoa_children_0_15": "Dependent Children aged 0-15",
    "lsoa_aged_60_plus": "Older population aged 60 and over",
}


def bbox_wgs84(bbox_bng):
    """The WGS84 box around a British National Grid box (corners only is enough at this scale; padded by 0.01 degrees)."""
    xs, ys = _TO_WGS.transform([bbox_bng[0], bbox_bng[2], bbox_bng[0], bbox_bng[2]], [bbox_bng[1], bbox_bng[1], bbox_bng[3], bbox_bng[3]])
    return min(xs) - 0.01, min(ys) - 0.01, max(xs) + 0.01, max(ys) + 0.01


# ====================================================================================================
# Areas and deprivation
# ====================================================================================================

def load_lsoa(bbox_bng):
    """GeoDataFrame(lsoa_code, geometry) of the generalised LSOA outlines that touch the box, in British National Grid."""
    snap = paths.bronze_snapshot("ons_lsoa")
    files = sorted(snap.glob("lsoa_*.geojson"))
    if not files:
        raise PipelineError("The ONS LSOA boundaries are missing. Run: python -m critical_buildings fetch --only lsoa")
    frames = [read_dataframe(f, bbox=tuple(bbox_bng), columns=["LSOA21CD"]) for f in files]
    g = pd.concat([f for f in frames if len(f)], ignore_index=True) if any(len(f) for f in frames) else gpd.GeoDataFrame(
        {"LSOA21CD": []}, geometry=[], crs=27700)
    g = gpd.GeoDataFrame(g.drop_duplicates("LSOA21CD").rename(columns={"LSOA21CD": "lsoa_code"}), geometry="geometry", crs=27700)
    return g.sort_values("lsoa_code").reset_index(drop=True)


def load_imd():
    """DataFrame indexed by LSOA code with the deprivation and population columns under their short names."""
    raw = pd.read_csv(paths.bronze_file("iod2025", "iod2025_file7.csv"), dtype=str, encoding="utf-8-sig")
    out = {}
    for name, prefix in IMD_PREFIXES.items():
        match = [c for c in raw.columns if c.startswith(prefix)]
        if len(match) != 1:
            raise PipelineError(f"The deprivation file should have exactly one column starting '{prefix}' but has {len(match)}. "
                                + source_checks.ADVICE)
        out[name] = raw[match[0]]
    df = pd.DataFrame(out)
    for c in df.columns:
        if c not in ("lsoa_code", "lsoa_name", "lad_code", "lad_name"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.drop_duplicates("lsoa_code").set_index("lsoa_code")


# ====================================================================================================
# planning.data.gov.uk layers (WGS84 well-known text in a CSV)
# ====================================================================================================

def _current(chunk, today):
    """Entries that have not ended."""
    end = chunk["end-date"] if "end-date" in chunk else pd.Series("", index=chunk.index)
    end = end.fillna("")
    return (end == "") | (end > today)


def _iter_wkt_layer(path, columns, geometry_column, box_wgs, today):
    """Chunks of a planning.data.gov.uk CSV, as (frame, geometries): only rows that have not ended and whose geometry meets the box.
    A chunk at a time, so a 2 GB file never has to fit in memory."""
    bbox_geom = shapely.box(*box_wgs)
    header = pd.read_csv(path, nrows=0).columns
    need = [c for c in [geometry_column, "end-date", *columns] if c in header]
    if geometry_column not in need:
        raise PipelineError(f"{path.name} has no '{geometry_column}' column. " + source_checks.ADVICE)
    for chunk in pd.read_csv(path, usecols=need, dtype=str, chunksize=CHUNK_ROWS):
        chunk = chunk[_current(chunk, today) & chunk[geometry_column].notna()]
        if chunk.empty:
            continue
        g = shapely.from_wkt(chunk[geometry_column].to_numpy(object), on_invalid="ignore")
        ok = ~shapely.is_missing(g)
        hit = np.zeros(len(g), dtype=bool)
        hit[ok] = shapely.intersects(g[ok], bbox_geom)
        if hit.any():
            yield chunk[hit].drop(columns=[geometry_column]), g[hit]


def _read_wkt_layer(path, columns, geometry_column, box_wgs, today):
    """All the rows _iter_wkt_layer yields, together: (frame, geometries array). For layers small enough to hold."""
    kept, geoms = [], []
    for frame, g in _iter_wkt_layer(path, columns, geometry_column, box_wgs, today):
        kept.append(frame)
        geoms.append(g)
    if not kept:
        return pd.DataFrame(columns=["end-date", *columns]), np.array([], dtype=object)
    return pd.concat(kept, ignore_index=True), np.concatenate(geoms)


def planning_polygons(dataset, columns, box_wgs, today=None):
    """(attributes, WGS84 polygons) of one planning.data.gov.uk polygon dataset within the box."""
    today = today or dt.date.today().isoformat()  # noqa: DTZ011 - local time is intended
    return _read_wkt_layer(paths.bronze_file("planning_data", f"{dataset}.csv"), columns, "geometry", box_wgs, today)


def listing_points(box_wgs, today=None):
    """(grade per listing, easting, northing) of the listed-building points within the box, for the nearest-listing grade."""
    today = today or dt.date.today().isoformat()  # noqa: DTZ011 - local time is intended
    frame, pts = _read_wkt_layer(paths.bronze_file("planning_data", "listed-building.csv"), ["listed-building-grade"], "point", box_wgs, today)
    if not len(pts):
        return np.array([], dtype=object), np.array([]), np.array([])
    x, y = _TO_BNG.transform(shapely.get_x(pts), shapely.get_y(pts))
    return frame["listed-building-grade"].fillna("").to_numpy(object), np.asarray(x), np.asarray(y)


# ====================================================================================================
# Flood zones (2 GB): parsed once per area and kept
# ====================================================================================================

FLOOD_SCHEMA = pa.schema([("level", pa.int8()), ("type", pa.string()), ("wkb", pa.binary())])


class FloodLayer:
    """The flood zone 2 and 3 polygons of an area, kept one folder per OS grid square (tile=TM/part-0.parquet).

    A polygon is stored under every square its bounding box touches, so a square can be linked from its own folder alone: memory is bounded
    by one square's polygons however big the area, and the 2 GB text file is parsed once, a chunk at a time, not once per run."""

    def __init__(self, folder, counts):
        self.folder, self.counts = Path(folder), counts

    def for_tile(self, tile):
        """{level: (polygons, types)} (WGS84) for the zone 2 and zone 3 polygons that touch the grid square."""
        path = self.folder / f"tile={tile}" / "part-0.parquet"
        df = pd.read_parquet(path) if path.exists() else pd.DataFrame({"level": [], "type": [], "wkb": []})
        out = {}
        for level in (2, 3):
            part = df[df["level"] == level].sort_values(["type", "wkb"], kind="stable")          # a fixed order, so ties are settled the same way
            out[level] = (shapely.from_wkb(part["wkb"].to_numpy(object)) if len(part) else np.array([], dtype=object), part["type"].to_numpy(object))
        return out


def _tiles_touched(geoms):
    """For each geometry (WGS84), the OS grid squares its bounding box touches, padded by 100 m: a list of codes per geometry."""
    b = shapely.bounds(geoms)
    lon = np.stack([b[:, 0], b[:, 2], b[:, 0], b[:, 2]])
    lat = np.stack([b[:, 1], b[:, 1], b[:, 3], b[:, 3]])
    e, n = _TO_BNG.transform(lon.ravel(), lat.ravel())
    e, n = np.asarray(e).reshape(lon.shape), np.asarray(n).reshape(lon.shape)
    e0, e1 = np.floor((e.min(axis=0) - 100) / 100_000).astype(int), np.floor((e.max(axis=0) + 100) / 100_000).astype(int)
    n0, n1 = np.floor((n.min(axis=0) - 100) / 100_000).astype(int), np.floor((n.max(axis=0) + 100) / 100_000).astype(int)
    from critical_buildings.properties import grid
    return [[grid.TILES[(i, j)] for i in range(a, b_ + 1) for j in range(c, d + 1) if (i, j) in grid.TILES]
            for a, b_, c, d in zip(e0, e1, n0, n1, strict=True)]


def flood_layer(area, box_wgs, today=None):
    """The area's flood zone polygons (a FloodLayer), parsed from the 2 GB file on first use and cached under the area's silver folder."""
    today = today or dt.date.today().isoformat()  # noqa: DTZ011 - local time is intended
    key = hashlib.sha256((paths.inputs_key(area, [("ea_flood_zones", None)]) + json.dumps([round(float(v), 3) for v in box_wgs])).encode()).hexdigest()[:12]
    folder = paths.SILVER / area / "properties" / f"flood_{key}"
    done = folder / "_done.json"
    if done.exists():
        return FloodLayer(folder, json.loads(done.read_text(encoding="utf-8")))
    tmp = folder.with_name(folder.name + "_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    writers, counts = {}, {"2": 0, "3": 0}
    try:
        for frame, geoms in _iter_wkt_layer(paths.bronze_file("ea_flood_zones", "flood-risk-zone.csv"), ["flood-risk-level", "flood-risk-type"],
                                            "geometry", box_wgs, today):
            level = pd.to_numeric(frame["flood-risk-level"], errors="coerce").fillna(0).astype(int).to_numpy()
            kind = frame["flood-risk-type"].fillna("").to_numpy(object)
            wkb = shapely.to_wkb(geoms)
            rows = {}
            for i, tiles in enumerate(_tiles_touched(geoms)):
                if level[i] in (2, 3):
                    counts[str(level[i])] += 1
                    for t in tiles:
                        rows.setdefault(t, []).append(i)
            for t, idx in rows.items():
                if t not in writers:
                    (tmp / f"tile={t}").mkdir()
                    writers[t] = pq.ParquetWriter(tmp / f"tile={t}" / "part-0.parquet", FLOOD_SCHEMA, compression="zstd")
                writers[t].write_table(pa.table({"level": pa.array(level[idx], type=pa.int8()), "type": pa.array(kind[idx], type=pa.string()),
                                                 "wkb": pa.array(wkb[idx], type=pa.binary())}, schema=FLOOD_SCHEMA))
    finally:
        for w in writers.values():
            w.close()
    (tmp / "_done.json").write_text(json.dumps(counts), encoding="utf-8")
    shutil.rmtree(folder, ignore_errors=True)
    tmp.rename(folder)                                                   # all or nothing: a half-written cache is never visible
    return FloodLayer(folder, counts)


# ====================================================================================================
# The critical sites from the current critical-buildings run
# ====================================================================================================

def critical_sites(area):
    """(run id, GeoDataFrame(site_id, site_type, importance, geometry)) from the area's current run, or (None, None) when it has none."""
    run_id = paths.current_run_id(area)
    folder = paths.run_dir(area, run_id) if run_id else None
    gpkg = paths.find_output(folder, "gpkg") if folder else None
    if gpkg is None:
        return None, None
    g = read_dataframe(gpkg, layer="sites", columns=["Site ID", "Site Type", "Operational Importance"])
    g = g.rename(columns={"Site ID": "site_id", "Site Type": "site_type", "Operational Importance": "importance"})
    return run_id, g.sort_values("site_id").reset_index(drop=True)


# ====================================================================================================
# OS Open UPRN (41.7 million points) as batches of arrays
# ====================================================================================================

UPRN_FIELDS = ["UPRN", "X_COORDINATE", "Y_COORDINATE", "LATITUDE", "LONGITUDE"]


def uprn_batches(zip_path, block_bytes=64 << 20):
    """Dicts of numpy arrays (uprn, x, y, lat, lon), one per batch of the CSV inside the OS Open UPRN zip."""
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise PipelineError("The OS Open UPRN download holds no CSV file. " + source_checks.ADVICE)
        with z.open(names[0]) as f:
            try:
                reader = pacsv.open_csv(f, read_options=pacsv.ReadOptions(block_size=block_bytes), convert_options=pacsv.ConvertOptions(
                    include_columns=UPRN_FIELDS, column_types={"UPRN": pa.int64(), **{c: pa.float64() for c in UPRN_FIELDS[1:]}}))
            except (pa.ArrowInvalid, pa.ArrowKeyError) as e:
                raise PipelineError(f"The OS Open UPRN file no longer has the columns {', '.join(UPRN_FIELDS)} ({e}). " + source_checks.ADVICE) from e
            for batch in reader:
                yield {k: batch.column(c).to_numpy(zero_copy_only=False) for k, c in zip(("uprn", "x", "y", "lat", "lon"), UPRN_FIELDS, strict=True)}
