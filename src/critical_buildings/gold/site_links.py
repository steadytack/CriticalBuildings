"""Join keys for linking sites to other data (utility networks, customer records): the UPRNs inside each site and a postcode for it.

UPRNs come from OS Open UPRN (one point per addressable location, about 41 million for Great Britain). The file is streamed in
batches and each batch is tested against the site outlines with a spatial index, so memory stays small however big the area.

Postcodes come from the register when a register record matched the site, otherwise from OS Code-Point Open (one point per
postcode): a postcode point inside the outline, or failing that the nearest one within POSTCODE_MAX_M.
"""
import hashlib
import zipfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import shapely

from critical_buildings import paths, progress, source_checks
from critical_buildings.errors import PipelineError
from critical_buildings.silver import _norm_pc, _postcode_table

POSTCODE_MAX_M = 250        # a Code-Point postcode further than this from the site is not offered
UPRNS_INLINE = 200          # the UPRNs column holds at most this many; UPRN Count is the full number, site_uprns.csv the full list
SITE_COLUMNS = ["UPRN Count", "UPRNs", "Postcode", "Postcode Method", "Postcode Distance (m)"]
UPRN_FIELDS = ["UPRN", "X_COORDINATE", "Y_COORDINATE"]
BLOCK_BYTES = 64 << 20


def format_postcode(normalised):
    """'IP11AA' -> 'IP1 1AA' (the inward part is always the last three characters)."""
    p = str(normalised).replace(" ", "").upper()
    return f"{p[:-3]} {p[-3:]}" if len(p) > 3 else p


# ====================================================================================================
# UPRNs inside each site
# ====================================================================================================

def _uprn_batches(zip_path):
    """(uprns, x, y) numpy arrays, one set per batch of the OS Open UPRN CSV inside the zip."""
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise PipelineError("The OS Open UPRN download holds no CSV file. " + source_checks.ADVICE)
        with z.open(names[0]) as f:
            try:
                reader = pacsv.open_csv(
                    f, read_options=pacsv.ReadOptions(block_size=BLOCK_BYTES),
                    convert_options=pacsv.ConvertOptions(
                        include_columns=UPRN_FIELDS,
                        column_types={"UPRN": pa.int64(), "X_COORDINATE": pa.float64(), "Y_COORDINATE": pa.float64()}))
            except (pa.ArrowInvalid, pa.ArrowKeyError) as e:        # a column the reader needs is missing
                raise PipelineError(f"The OS Open UPRN file no longer has the columns {', '.join(UPRN_FIELDS)} ({e}). "
                                    + source_checks.ADVICE) from e
            for batch in reader:
                yield (batch.column("UPRN").to_numpy(zero_copy_only=False), batch.column("X_COORDINATE").to_numpy(zero_copy_only=False),
                       batch.column("Y_COORDINATE").to_numpy(zero_copy_only=False))


def geometry_fingerprint(geometries):
    """Short hash of the ordered site outlines: what a cached UPRN join was made for."""
    h = hashlib.sha256()
    for g in geometries:
        h.update(shapely.to_wkb(g))
    return h.hexdigest()[:12]


def uprns_in_sites(geometries, zip_path):
    """DataFrame(pos, uprn): for each UPRN point inside (or on) a site outline, the position of that site in `geometries`.
    Sorted by position then UPRN. A UPRN on the shared edge of two sites appears for both."""
    geoms = np.asarray(geometries, dtype=object)
    shapely.prepare(geoms)
    tree = shapely.STRtree(geoms)
    pos_parts, uprn_parts, n = [], [], 0
    for uprns, x, y in _uprn_batches(zip_path):
        hit_points, hit_sites = tree.query(shapely.points(x, y), predicate="intersects")
        if len(hit_points):
            pos_parts.append(hit_sites)
            uprn_parts.append(uprns[hit_points])
        n += len(uprns)
    progress.note("OS Open UPRN points read", n)
    if not pos_parts:
        return pd.DataFrame({"pos": np.array([], dtype=np.int64), "uprn": np.array([], dtype=np.int64)})
    out = pd.DataFrame({"pos": np.concatenate(pos_parts), "uprn": np.concatenate(uprn_parts)})
    return out.drop_duplicates().sort_values(["pos", "uprn"]).reset_index(drop=True)


def cached_uprns_in_sites(geometries, area):
    """uprns_in_sites, kept as a conformed table: streaming the national file takes minutes, and the answer only changes
    when the UPRN file or the site outlines do."""
    name = f"site_uprns_{geometry_fingerprint(geometries)}.parquet"
    inputs = [("os_open_uprn", None)]
    cached = paths.conformed_lookup(area, name, inputs)
    if cached is not None:
        progress.note("UPRN join", "cached")
        return pd.read_parquet(cached)
    progress.note("UPRN join", "streamed from the OS Open UPRN file")
    out = uprns_in_sites(geometries, paths.bronze_file("os_open_uprn", "osopenuprn_csv.zip"))
    out.to_parquet(paths.silver_conformed_dir(area) / name)
    paths.conformed_stamp(area, name, inputs)
    return out


# ====================================================================================================
# A postcode for each site
# ====================================================================================================

def postcodes_for_sites(geometries, register_postcodes, table=None):
    """DataFrame(Postcode, Postcode Method, Postcode Distance (m)), one row per site in order.

    register_postcodes: the register's postcode for each site ('' when none; several joined with ' | ' use the first).
    table: Code-Point postcodes indexed by normalised postcode with EA / NO columns (default: the one in bronze)."""
    table = _postcode_table() if table is None else table
    n = len(geometries)
    postcode, method, distance = [""] * n, [""] * n, [None] * n
    for i, text in enumerate(register_postcodes):
        first = str(text or "").split("|")[0].strip()
        if first:
            postcode[i], method[i] = format_postcode(_norm_pc(first)), "Register"
    todo = [i for i in range(n) if not postcode[i]]
    if todo and len(table):
        geoms = np.asarray([geometries[i] for i in todo], dtype=object)
        shapely.prepare(geoms)
        tree = shapely.STRtree(shapely.points(table["EA"].to_numpy(float), table["NO"].to_numpy(float)))
        names = table.index.to_numpy()
        reps = shapely.point_on_surface(geoms)
        # a postcode point inside the outline: the one nearest the middle of the site
        inside_sites, inside_pcs = tree.query(geoms, predicate="intersects")
        best = {}
        if len(inside_sites):
            d = shapely.distance(reps[inside_sites], tree.geometries[inside_pcs])
            order = np.lexsort((inside_pcs, d, inside_sites))        # per site: nearest first, ties by postcode position
            first_per_site = order[np.unique(inside_sites[order], return_index=True)[1]]
            best = {int(inside_sites[k]): int(inside_pcs[k]) for k in first_per_site}
        for j, i in enumerate(todo):
            if j in best:
                postcode[i], method[i], distance[i] = format_postcode(names[best[j]]), "Code-Point inside outline", 0
        # otherwise the nearest postcode point within POSTCODE_MAX_M
        rest = [j for j in range(len(todo)) if j not in best]
        if rest:
            near_sites, near_pcs = tree.query_nearest(reps[rest], max_distance=POSTCODE_MAX_M, all_matches=False)
            dists = shapely.distance(reps[rest][near_sites], tree.geometries[near_pcs])
            for k, p, dist in zip(near_sites, near_pcs, dists, strict=True):
                i = todo[rest[int(k)]]
                postcode[i], method[i], distance[i] = format_postcode(names[int(p)]), "Code-Point nearest", int(round(dist))
    return pd.DataFrame({"Postcode": postcode, "Postcode Method": method,
                         "Postcode Distance (m)": pd.array(distance, dtype="Int64")})


# ====================================================================================================
# Put both on the sites table
# ====================================================================================================

def attach(sites_out, area):
    """Add the UPRN and postcode columns to the sites table. Returns (sites_out, site_uprns), where site_uprns is the long table
    DataFrame(Site ID, UPRN) with every UPRN of every site (UPRNs holds only the first UPRNS_INLINE)."""
    geoms = list(sites_out.geometry)
    sites_out = sites_out.copy()
    progress.step("Finding the UPRNs inside each site (OS Open UPRN)")
    pairs = cached_uprns_in_sites(geoms, area)
    site_ids = sites_out["Site ID"].to_numpy()
    long = pd.DataFrame({"Site ID": site_ids[pairs["pos"].to_numpy()], "UPRN": pairs["uprn"].to_numpy()})
    counts = np.bincount(pairs["pos"].to_numpy(), minlength=len(sites_out))
    inline = [""] * len(sites_out)
    for pos, grp in pairs.groupby("pos", sort=False)["uprn"]:
        inline[int(pos)] = ";".join(str(u) for u in grp.to_numpy()[:UPRNS_INLINE])
    sites_out["UPRN Count"] = counts.astype(int)
    sites_out["UPRNs"] = inline
    progress.note("sites with at least one UPRN", int((counts > 0).sum()))
    progress.note("site UPRN pairs", len(long))
    progress.step("Choosing a postcode for each site")
    pc = postcodes_for_sites(geoms, list(sites_out["Authoritative Postcode"].fillna("")))
    for c in pc.columns:
        sites_out[c] = pc[c].array                # .array keeps the nullable integer type of the distance
    progress.note("sites with a postcode", int((pc["Postcode"] != "").sum()))
    return sites_out, long
