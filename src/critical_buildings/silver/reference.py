"""Load the authoritative reference datasets (DfE GIAS, CQC, NHS ODS) into one table of points.

Each record is mapped to one of our Building Types with the 'Reference mapping' sheet; unmapped and IGNORE rows
are dropped. Output columns: ref_source, ref_id, ref_name, building_type, status, capacity, capacity_unit, detail,
rating, provider, postcode, uprn, ods_code, ods_status, ods_parent, geometry (EPSG:27700).
"""
import functools
import zipfile

import geopandas as gpd
import pandas as pd

from critical_buildings import paths
from critical_buildings.rules_io import load_reference_mapping
from critical_buildings.silver import osm_geom

COLS = ["ref_source", "ref_id", "ref_name", "building_type", "status", "capacity", "capacity_unit", "detail",
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
def postcode_points():
    """Postcode -> (easting, northing) from OS Code-Point Open."""
    with zipfile.ZipFile(paths.bronze_file("os_codepoint_open", "codepo_gb.zip")) as z:
        cols = pd.read_csv(z.open("Doc/Code-Point_Open_Column_Headers.csv"), header=None).iloc[0].tolist()
        frames = [pd.read_csv(z.open(n), header=None, names=cols)
                  for n in z.namelist() if n.startswith("Data/CSV/") and n.endswith(".csv")]
    cp = pd.concat(frames)
    return dict(zip(cp["PC"].map(_norm_pc), zip(cp["EA"], cp["NO"])))   # PC = postcode, EA/NO = easting/northing


def _ods():
    """ODS lookups: code -> dict(name, postcode, status, parent, kind) from ets, epraccur and etr."""
    etr = pd.read_csv(paths.bronze_file("nhs_ods", "etr.csv"), header=None, dtype=str).fillna("")
    trust = dict(zip(etr[0], etr[1]))
    out = {}
    ets = pd.read_csv(paths.bronze_file("nhs_ods", "ets.csv"), header=None, dtype=str).fillna("")
    for r in ets.itertuples(index=False):
        out[r[0]] = dict(name=r[1], postcode=r[9], status="Closed" if r[11] else "Active",
                         parent=trust.get(r[14], r[14]), kind="ets")
    gp = pd.read_csv(paths.bronze_file("nhs_ods", "epraccur.csv"), header=None, dtype=str).fillna("")
    for r in gp.itertuples(index=False):
        out.setdefault(r[0], dict(name=r[1], postcode=r[9], status="Active" if r[12] == "ACTIVE" else "Closed",
                                  parent=r[14], kind="epraccur"))
    return out


def _cqc_locations():
    """CQC active locations as a conformed silver table (parquet), built once per bronze snapshot."""
    cache = paths.silver_conformed_dir() / "cqc_locations.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    c = pd.read_excel(paths.bronze_file("cqc_hsca", "active_locations.ods"), engine="odf",
                      sheet_name="HSCA_Active_Locations")
    c.to_parquet(cache)
    return c


def load_reference(boundary, keep_ignored=False):
    """keep_ignored=True also returns IGNORE/unmapped records (building_type 'IGNORE' / 'UNMAPPED') for diagnostics."""
    mapping, errors = load_reference_mapping()
    if errors:
        raise SystemExit("Reference mapping check FAILED:\n  " + "\n  ".join(errors))
    ods = _ods()
    rows, dropped = [], {"unmapped": 0, "ignored": 0}

    # ---- DfE GIAS ----
    g = pd.read_csv(paths.bronze_file("dfe_gias", "edubasealldata.csv"), encoding="cp1252", low_memory=False).dropna(
        subset=["Easting", "Northing"])
    g = g[gpd.GeoSeries(gpd.points_from_xy(g.Easting, g.Northing), crs=27700, index=g.index).within(boundary)]
    for _, r in g.iterrows():
        t = map_type("DfE", r, mapping)
        if t in (None, "IGNORE"):
            dropped["unmapped" if t is None else "ignored"] += 1
            if not keep_ignored:
                continue
            t = "IGNORE" if t else "UNMAPPED"
        pupils = r.get("NumberOfPupils")
        trust = r.get("Trusts (name)")
        rows.append(dict(ref_source="DfE GIAS", ref_id=str(r.URN), ref_name=r.EstablishmentName, building_type=t,
                         status=r["EstablishmentStatus (name)"], capacity=pupils if pd.notna(pupils) else None,
                         capacity_unit="pupils",
                         detail=f"{r['TypeOfEstablishment (name)']} / {r['PhaseOfEducation (name)']}",
                         rating="", provider=trust if pd.notna(trust) else "",
                         postcode=r.get("Postcode", ""), uprn="", ods_code="", ods_status="", ods_parent="",
                         geometry=gpd.points_from_xy([r.Easting], [r.Northing])[0]))

    # ---- CQC (HSCA active locations) ----
    c = _cqc_locations()
    c = c.dropna(subset=["Location Latitude", "Location Longitude"])
    cg = gpd.GeoSeries(gpd.points_from_xy(c["Location Longitude"], c["Location Latitude"]), crs=4326,
                       index=c.index).to_crs(27700)
    inside = cg.within(boundary)
    c, cg = c[inside], cg[inside]
    cqc_ods = set()
    for i, r in c.iterrows():
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
        rows.append(dict(ref_source="CQC", ref_id=r["Location ID"], ref_name=r["Location Name"], building_type=t,
                         status="Active", capacity=beds if pd.notna(beds) and beds > 0 else None,
                         capacity_unit="beds", detail=r["Location Primary Inspection Category"],
                         rating=rating if pd.notna(rating) else "", provider=r["Provider Name"],
                         postcode=r["Location Postal Code"],
                         uprn=str(int(r["Location UPRN ID"])) if pd.notna(r["Location UPRN ID"]) else "",
                         ods_code=code, ods_status=o.get("status", ""), ods_parent=o.get("parent", ""),
                         geometry=cg[i]))

    # ---- NHS ODS trust sites not already known to CQC (located by postcode) ----
    pc = postcode_points()
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
        rows.append(dict(ref_source="NHS ODS", ref_id=code, ref_name=o["name"].title(), building_type=t,
                         status="Active", capacity=None, capacity_unit="", detail="NHS trust site",
                         rating="", provider=o["parent"], postcode=o["postcode"], uprn="", ods_code=code,
                         ods_status="Active", ods_parent=o["parent"], geometry=pt))
    out = gpd.GeoDataFrame(rows, columns=COLS, geometry="geometry", crs=27700)
    # ODS lists some sites under several codes: keep one record per (name, postcode)
    key = out.ref_name.str.lower().str.replace(r"[^a-z0-9]", "", regex=True) + out.postcode.map(_norm_pc)
    out = out[~((out.ref_source == "NHS ODS") & key.duplicated(keep="first"))].copy()
    out.attrs["dropped"] = dropped
    return out


if __name__ == "__main__":
    ref = load_reference(osm_geom.area_boundary())
    print(len(ref), ref.attrs["dropped"])
    print(ref.groupby(["ref_source", "building_type"]).size().to_string())
