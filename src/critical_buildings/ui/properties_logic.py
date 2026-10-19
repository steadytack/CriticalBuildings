"""Everything the Properties tab needs that is not drawing: finding the latest table, saying if it is out of date, and querying it.

No Streamlit here, so it can be tested. The table is a folder of parquet (one subfolder per OS grid square, up to 35 million rows for
England), so every query reads only the columns it needs and filters as it reads; nothing loads the whole table.
"""
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds

from critical_buildings import paths
from critical_buildings.properties import build

DISPLAY_COLUMNS = ["uprn", "postcode", "lad_name", "epc_rating", "epc_property_type", "epc_floor_area_m2", "epc_age_band", "lsoa_code", "imd_decile", "flood_zone", "conservation_area", "article_4_area",
                   "listed_in_outline", "listed_grade", "building_id", "uprns_in_building", "critical_site_id", "critical_site_type",
                   "critical_site_importance", "critical_site_distance_m", "easting", "northing", "latitude", "longitude"]
EXPORT_LIMIT = 200_000
LINKS = [("In a building footprint", "in_building"), ("Has a postcode within 250 m", "with_postcode"), ("Inside its LSOA outline", "lsoa_inside"),
         ("Took the nearest LSOA (just outside the outlines)", "lsoa_nearest"), ("Has deprivation scores", "with_deprivation"),
         ("In a conservation area", "conservation_area"), ("In an Article 4 direction area", "article_4_area"),
         ("Inside a listed building outline", "listed_in_outline"), ("In flood zone 3 (high)", "flood_zone_3"),
         ("In flood zone 2 (medium)", "flood_zone_2"), ("Inside a critical site", "in_critical_site"),
         ("Within 1 km of a critical site (includes those inside)", "near_critical_site"),
         ("Has an energy certificate (EPC)", "with_epc"), ("EPC rating E, F or G", "epc_efg")]


# ---------------------------------------------------------------- the latest table
def latest_table(area):
    """The area's current property table as a dict (run id, folders, row count, size, summary, lineage), or None when none was built."""
    run_id = build.current_run_id(area)
    if not run_id:
        return None
    dest = build.properties_dir(area) / run_id
    table = dest / "table"
    if not table.exists():
        return None
    lineage = json.loads((dest / "lineage.json").read_text(encoding="utf-8"))
    summary = json.loads((dest / "summary.json").read_text(encoding="utf-8"))
    return {"run_id": run_id, "dest": dest, "table": table, "rows": summary.get("rows", 0), "summary": summary, "lineage": lineage,
            "tiles": lineage.get("tiles", []), "built_at": lineage.get("finished_at", ""), "critical_run": lineage.get("critical_buildings_run"),
            "size_mb": sum(f.stat().st_size for f in table.rglob("*.parquet")) / 1e6}


def out_of_date(area, latest):
    """Plain-English reasons the latest table no longer matches its inputs (empty = current)."""
    reasons = []
    current = paths.current_run_id(area)
    if current and latest["critical_run"] != current:
        reasons.append(f"There is a newer critical-buildings run ({current}); the critical_site columns are from {latest['critical_run'] or 'none'}.")
    elif not latest["critical_run"] and current:
        reasons.append("The critical_site columns are empty because there was no critical-buildings run when this was built.")
    used = {s["source"] for s in latest["lineage"].get("bronze", {}).get("snapshots", [])}
    if "epc_domestic" not in used and paths.latest_snapshot("epc_domestic"):
        reasons.append("An EPC file has been stored since this table was built, so the epc_* columns are empty.")
    for snap in latest["lineage"].get("bronze", {}).get("snapshots", []):
        scoped = snap.get("area")
        newest = paths.latest_snapshot(snap["source"], scoped)
        if newest and newest != snap["snapshot"]:
            reasons.append(f"{snap['source']} has a newer snapshot ({newest}) than the one used ({snap['snapshot']}).")
    return reasons


def coverage_rows(summary):
    """[{Link, UPRNs, Share}] from summary.json: how many rows have each link."""
    n = max(summary.get("rows", 0), 1)
    return [{"Link": label, "UPRNs": summary[k], "Share": summary[k] / n} for label, k in LINKS if k in summary]


# ---------------------------------------------------------------- querying
def open_table(table_dir):
    return ds.dataset(Path(table_dir), format="parquet", partitioning="hive")


def display_columns(dataset):
    """The display columns this table has (a table built before the EPC columns were added lacks them)."""
    have = set(dataset.schema.names)
    return [c for c in DISPLAY_COLUMNS if c in have]


def has_epc_columns(dataset):
    return "epc_rating" in dataset.schema.names


def districts(dataset):
    """The local authority district names in the table, sorted."""
    names = dataset.to_table(columns=["lad_name"]).column("lad_name").combine_chunks()
    return sorted(n for n in pc.unique(names).to_pylist() if n)


def critical_site_types(dataset):
    names = dataset.to_table(columns=["critical_site_type"]).column("critical_site_type").combine_chunks()
    return sorted(n for n in pc.unique(names).to_pylist() if n)


def expression(f):
    """A pyarrow filter from the explorer's choices (a dict; missing keys mean no filter), or None for everything."""
    parts = []
    if f.get("district"):
        parts.append(pc.field("lad_name") == f["district"])
    if f.get("flood_min", 1) > 1:
        parts.append(pc.field("flood_zone") >= f["flood_min"])
    lo, hi = f.get("decile", (1, 10))
    if (lo, hi) != (1, 10):
        parts.append((pc.field("imd_decile") >= lo) & (pc.field("imd_decile") <= hi))
    for flag in ("conservation_area", "article_4_area", "listed_in_outline"):
        if f.get(flag):
            parts.append(pc.field(flag))
    if f.get("epc_ratings"):
        parts.append(pc.field("epc_rating").cast(pa.string()).isin(list(f["epc_ratings"])))
    if f.get("has_epc"):
        parts.append(pc.field("epc_certificates") > 0)
    if f.get("critical") == "inside":
        parts.append(pc.field("critical_site_distance_m") == 0)
    elif f.get("critical") == "within":
        parts.append(pc.field("critical_site_distance_m") <= f.get("critical_m", 250))
    if f.get("critical") in ("inside", "within"):
        if f.get("importance"):
            parts.append(pc.field("critical_site_importance").isin(list(f["importance"])))
        if f.get("site_types"):
            parts.append(pc.field("critical_site_type").isin(list(f["site_types"])))
    if not parts:
        return None
    out = parts[0]
    for p in parts[1:]:
        out = out & p
    return out


def count(dataset, expr):
    return dataset.count_rows(filter=expr)


def breakdown(dataset, expr, column):
    """{value: rows} for one column of the matching rows (null shown as None)."""
    col = dataset.to_table(columns=[column], filter=expr).column(column).combine_chunks()
    counts = pc.value_counts(col)
    return {s["values"].as_py(): s["counts"].as_py() for s in counts}


def sample(dataset, expr, columns, n):
    """Up to n matching rows as a DataFrame, taken from every grid square in turn so that they are spread across the area."""
    fragments = list(dataset.get_fragments())
    per = max(n // max(len(fragments), 1), 1) + 1
    tables = []
    for fragment in fragments:
        t = fragment.head(per, columns=columns, filter=expr)
        if t.num_rows:
            tables.append(t)
    if not tables:
        return pa.table({c: [] for c in columns}).to_pandas()
    return pa.concat_tables(tables).slice(0, n).to_pandas()


def export_csv(dataset, expr, columns, limit=EXPORT_LIMIT):
    """(CSV bytes of up to `limit` matching rows, whether it was cut short)."""
    table = dataset.head(limit + 1, columns=columns, filter=expr)
    cut = table.num_rows > limit
    return table.slice(0, limit).to_pandas().to_csv(index=False).encode("utf-8"), cut
