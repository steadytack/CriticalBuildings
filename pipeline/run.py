"""Run the whole pipeline for the pilot area:  python pipeline/run.py

rules check -> classify -> merge -> stable IDs -> apply overrides -> write outputs -> change tracking -> QA map
Outputs go to outputs/.
"""
import datetime as dt
import shutil
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

import classify
import merge
import osm_geom
import docs
import qa
import ref_data
import refmatch
import roles
import sites as sitelib
import rules_io

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)
REGISTRY = OUT / "id_registry.csv"
OVERRIDES = OUT / "overrides.csv"
OVR_COLS = ["Primary Category Override", "Building Type Override", "Operational Importance Override",
            "Operational Importance Driver Override", "Manual Override Notes", "Building Role Override", "Review Decision", "Verification Override"]
SCHEMA = ["Building ID", "Source", "Source Feature ID", "Primary Category Default", "Building Type Default",
          "DESNZ Priority Consumer Category A Default", "BUUK Catastrophic Risk - Public Default",
          "BUUK Catastrophic Risk - Environmental Default", "BUUK Catastrophic Risk - Reputational Default",
          "Operational Importance Default", "Operational Importance Driver Default",
          "Primary Category Override", "Building Type Override", "DESNZ Priority Consumer Category A Override",
          "BUUK Catastrophic Risk - Public Override", "BUUK Catastrophic Risk - Environmental Override",
          "BUUK Catastrophic Risk - Reputational Override", "BUUK Catastrophic Risk - Catastrophic Risk Override",
          "Operational Importance Override", "Operational Importance Driver Override", "Manual Override Notes",
          "Count UPRNs", "UPRNs inside footprint", "Geom", "Created Date", "Created By", "Last Edit Date",
          "Last Edit By", "Rule Version", "Run Date"]
REF_COLS = ["Verification", "Verification Note", "Authoritative Distance (m)", "Authoritative Source", "Authoritative ID", "Authoritative Name", "Authoritative Status",
            "Capacity", "Capacity Unit", "Authoritative Detail", "CQC Rating", "Operator / Trust",
            "Authoritative Postcode", "UPRN", "ODS Code"]
EXTRA = ["Name", "Primary Category", "Building Type", "Operational Importance", "Match Method", "Confidence",
         "Review Reason", "OS Rule Ref", "OSM Rule Ref", "All Matched Rule Refs", "Excluded", "Rule Notes",
         "Site ID", "Site Name", "Site Type", "Site Method", "Buildings in Site",
         "Building Role", "Building Role Default", "Building Role Reason", "Building Role Override",
         "Classification Note", "Review Status", "Review Decision", "Verification Override",
         "Latitude", "Longitude", "Data Quality Score", "Data Quality Band"] + REF_COLS
SITE_REGISTRY = OUT / "site_registry.csv"


def assign_ids(recs, today):
    """Keep Building IDs stable across runs (match on OS id, then OSM id, then footprint overlap)."""
    recs = recs.copy()
    recs["os_key"] = recs.source_feature_id.str.split(" / ").str[0].where(recs.source != "OSM")
    recs["osm_key"] = recs.source_feature_id.str.split(" / ").str[-1].where(recs.source != "OS Important Buildings")
    reg = pd.read_csv(REGISTRY, dtype=str) if REGISTRY.exists() else pd.DataFrame(
        columns=["building_id", "os_key", "osm_key", "created", "wkt"])
    by_os = {k: b for k, b in zip(reg.os_key, reg.building_id) if isinstance(k, str)}
    osm_only = reg[reg.os_key.isna()]
    by_osm = {k: b for k, b in zip(osm_only.osm_key, osm_only.building_id) if isinstance(k, str)}
    created = dict(zip(reg.building_id, reg.created))
    nxt = (reg.building_id.str[5:].astype(int).max() + 1) if len(reg) else 1
    ids = []
    taken = set()
    for _, r in recs.iterrows():
        b = by_os.get(r.os_key) if isinstance(r.os_key, str) else None
        if b is None and isinstance(r.osm_key, str) and r.source == "OSM":
            b = by_osm.get(r.osm_key)
        if b is None and isinstance(r.osm_key, str) and r.source == "Both":
            b = by_osm.get(r.osm_key)          # was OSM-only last run, now also matched to OS
        if b in taken:
            b = None
        ids.append(b)
        if b:
            taken.add(b)
    # footprint-overlap fallback for the rest (e.g. OSM feature redrawn under a new id)
    if len(reg) and any(i is None for i in ids):
        old = gpd.GeoSeries(gpd.GeoSeries.from_wkt(reg.wkt), crs=27700)
        for n, (i, r) in enumerate(zip(ids, recs.itertuples())):
            if i is not None:
                continue
            hit = old[old.intersects(r.geometry)]
            for k in hit.index:
                cand = reg.at[k, "building_id"]
                if cand in taken:
                    continue
                ov = old[k].intersection(r.geometry).area / max(min(old[k].area, r.geometry.area), 1e-9)
                if ov >= 0.5:
                    ids[n] = cand
                    taken.add(cand)
                    break
    new = 0
    for n, i in enumerate(ids):
        if i is None:
            ids[n] = f"BLDG-{nxt:06d}"
            nxt += 1
            new += 1
        created.setdefault(ids[n], str(today))
    recs["Building ID"] = ids
    recs["Created Date"] = recs["Building ID"].map(created)
    pd.DataFrame(dict(building_id=recs["Building ID"], os_key=recs.os_key, osm_key=recs.osm_key,
                      created=recs["Created Date"], wkt=recs.geometry.to_wkt())).to_csv(REGISTRY, index=False)
    return recs, new


def assign_site_ids(recs, sites, today):
    """Stable Site IDs: a site keeps the ID most of its buildings had in the previous run."""
    prev_csv = OUT / "critical_buildings.csv"
    prev = pd.read_csv(prev_csv, dtype=str) if prev_csv.exists() else pd.DataFrame()
    old_site = dict(zip(prev["Building ID"], prev["Site ID"])) if "Site ID" in prev else {}
    reg = pd.read_csv(SITE_REGISTRY, dtype=str) if SITE_REGISTRY.exists() else pd.DataFrame(
        columns=["site_id", "created"])
    created = dict(zip(reg.site_id, reg.created))
    nxt = (reg.site_id.str[5:].astype(int).max() + 1) if len(reg) else 1
    taken, mapping = set(), {}
    for g, n in sites.sort_values("n_buildings", ascending=False)[["site_group", "n_buildings"]].itertuples(index=False):
        votes = pd.Series([old_site.get(b) for b in recs.loc[recs.site_group == g, "Building ID"]]).dropna()
        sid = next((v for v in votes.value_counts().index if v not in taken), None)
        if sid is None:
            sid = f"SITE-{nxt:06d}"
            nxt += 1
        taken.add(sid)
        mapping[g] = sid
        created.setdefault(sid, str(today))
    recs = recs.copy()
    recs["Site ID"] = recs.site_group.map(mapping)
    sites = sites.copy()
    sites["Site ID"] = sites.site_group.map(mapping)
    sites["Created Date"] = sites["Site ID"].map(created)
    pd.DataFrame({"site_id": list(created), "created": list(created.values())}).to_csv(SITE_REGISTRY, index=False)
    return recs, sites


def apply_overrides(df):
    if not OVERRIDES.exists():
        pd.DataFrame(columns=["Building ID"] + OVR_COLS + ["Exclude (Y)"]).to_csv(OVERRIDES, index=False)
    ov = pd.read_csv(OVERRIDES, dtype=str).fillna("")
    for c in OVR_COLS + ["Exclude (Y)", "Exclude Site (Y)"]:      # people may fill in only the columns they need
        if c not in ov:
            ov[c] = ""
    ov = ov[ov["Building ID"].isin(df["Building ID"])]
    ov = ov[["Building ID"] + OVR_COLS + ["Exclude (Y)", "Exclude Site (Y)"]].drop_duplicates("Building ID", keep="last")
    df = df.drop(columns=OVR_COLS).merge(ov, on="Building ID", how="left")
    for c in OVR_COLS:
        df[c] = df[c].fillna("")
    df["Excluded"] = df["Exclude (Y)"].fillna("").str.upper().eq("Y")
    # site-level decisions: marking any one building of a site applies to the whole site
    ex_sites = set(df.loc[df["Exclude Site (Y)"].fillna("").str.upper().eq("Y"), "Site ID"])
    df.loc[df["Site ID"].isin(ex_sites), "Excluded"] = True
    vo = df[df["Verification Override"] != ""].groupby("Site ID")["Verification Override"].first()
    df["Verification"] = df["Site ID"].map(vo).fillna(df["Verification"])
    return df.drop(columns=["Exclude (Y)", "Exclude Site (Y)"], errors="ignore")


def main():
    errs, warns, tags = rules_io.check_rules()
    role_settings, role_errs = rules_io.load_role_settings()
    errs += role_errs
    for w in warns:
        print("note:", w)
    if errs:
        print("Rules check FAILED - fix these in the Rules sheet, then run again:")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    version = rules_io.rule_version()
    merge.PRIO = {t.rule_ref: t.priority for t in tags}
    today = dt.date.today()
    print("classifying ...")
    osm, os_, os_total, site_cands = classify.run()
    print(f"  OSM {len(osm)} / OS {len(os_)} classified")
    print("merging ...")
    recs = merge.merge(osm, os_)
    recs, sites_df = sitelib.assign_sites(recs, site_cands)
    print("matching authoritative sources (DfE / CQC / NHS ODS) ...")
    boundary = osm_geom.suffolk_boundary()
    ref_all = ref_data.load_reference(boundary, keep_ignored=True)
    ref = ref_all[~ref_all.building_type.isin(["IGNORE", "UNMAPPED"])].copy()
    recs, sites_df, ref_gaps, ref_matches = refmatch.attach_reference(recs, sites_df, ref, tags, boundary, ref_all, ref_data.postcode_points())
    print(f"  {len(ref)} reference records: {ref_matches.ri.nunique()} matched to sites, {len(ref_gaps)} added as new buildings")
    recs, new_ids = assign_ids(recs, today)
    recs, sites_df = assign_site_ids(recs, sites_df, today)
    recs = roles.assign_roles(recs, role_settings)

    df = pd.DataFrame(index=recs.index)
    df["Building ID"] = recs["Building ID"]
    df["Source"] = recs.source
    df["Source Feature ID"] = recs.source_feature_id
    df["Primary Category Default"] = recs.category
    df["Building Type Default"] = recs.building_type
    df["Operational Importance Default"] = recs.importance
    df["Operational Importance Driver Default"] = recs.driver
    for c in SCHEMA:
        if c not in df:
            df[c] = ""
    df["Geom"] = recs.geometry.to_wkt()
    df["Created Date"] = recs["Created Date"]
    df["Created By"] = "pipeline"
    df["Last Edit Date"] = str(today)
    df["Last Edit By"] = "pipeline"
    df["Rule Version"] = version
    df["Run Date"] = str(today)
    df["Name"] = recs.name
    df["Match Method"] = recs.method
    df["Confidence"] = recs.confidence
    df["Review Reason"] = recs.review
    df["OS Rule Ref"] = recs.os_ref
    df["OSM Rule Ref"] = recs.osm_ref
    df["All Matched Rule Refs"] = recs.refs
    df["Rule Notes"] = recs.rule_notes
    df["Site ID"] = recs["Site ID"]
    df["Site Method"] = recs.site_method
    sinfo = sites_df.set_index("Site ID")
    df["Site Name"] = df["Site ID"].map(sinfo["name"])
    df["Site Type"] = df["Site ID"].map(sinfo["building_type"])
    df["Buildings in Site"] = df["Site ID"].map(sinfo["n_buildings"])
    for c in REF_COLS:
        df[c] = recs["ref_" + c]
    df["Capacity"] = pd.to_numeric(df["Capacity"]).astype("Int64")
    df["Authoritative Distance (m)"] = pd.to_numeric(df["Authoritative Distance (m)"]).astype("Int64")
    df["Classification Note"] = recs.classification_note.fillna("")
    df["Building Role Default"] = recs.role
    df["Building Role Reason"] = recs.role_reason
    df["Building Role Override"] = ""
    df["Review Decision"] = ""
    df["Verification Override"] = ""
    df = apply_overrides(df)
    # Review Status: '' = nothing to review, Open = needs a decision, Decided = a decision is recorded in overrides.csv
    df["Review Status"] = ""
    has_reason = df["Review Reason"] != ""
    df.loc[has_reason, "Review Status"] = "Open"
    df.loc[has_reason & (df["Review Decision"] != ""), "Review Status"] = "Decided"
    df["Building Role"] = df["Building Role Override"].where(df["Building Role Override"] != "", df["Building Role Default"])
    ev = lambda o, d: df[o].where(df[o] != "", df[d])
    df["Primary Category"] = ev("Primary Category Override", "Primary Category Default")
    df["Building Type"] = ev("Building Type Override", "Building Type Default")
    df["Operational Importance"] = ev("Operational Importance Override", "Operational Importance Default")
    # WGS84 coordinates and a transparent data quality score (formula documented in the data dictionary)
    pt = gpd.GeoSeries.from_wkt(df["Geom"], crs=27700).representative_point().to_crs(4326)
    df["Longitude"] = pt.x.round(6)
    df["Latitude"] = pt.y.round(6)
    df["Data Quality Score"] = docs.quality_score(df)
    df["Data Quality Band"] = pd.cut(df["Data Quality Score"], [-1, 49, 74, 100], labels=["Low", "Medium", "High"]).astype(str)
    df = df[SCHEMA + EXTRA]
    gdf = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df["Geom"], crs=27700), crs=27700)

    # change tracking against the previous run
    prev_csv = OUT / "critical_buildings.csv"
    changes = None
    if prev_csv.exists():
        prev = pd.read_csv(prev_csv, dtype=str).fillna("")
        shutil.copy(prev_csv, OUT / "critical_buildings_previous.csv")
        changes = qa.diff_runs(prev, df)

    final = gdf[~gdf.Excluded]
    final = final.drop(columns=["Geom"])   # the layer's own geometry replaces the WKT text column (CSV keeps it)
    final.to_file(OUT / "critical_buildings.gpkg", layer="critical_buildings", driver="GPKG")
    df[~df.Excluded].to_csv(OUT / "critical_buildings.csv", index=False)
    sites_out = sites_df.rename(columns={"name": "Site Name", "building_type": "Site Type",
                                         "category": "Primary Category", "importance": "Operational Importance",
                                         "n_buildings": "Buildings in Site", "method": "Site Method"})
    live_roles = df[~df.Excluded & df["Building Role"].isin(["Primary", "Standalone", "Whole site"])]
    for c in REF_COLS:
        sites_out[c] = sites_out["ref_" + c]
    sites_out["Verification"] = sites_out["Site ID"].map(df.groupby("Site ID")["Verification"].first())
    sites_out = sites_out[sites_out["Site ID"].isin(df.loc[~df.Excluded, "Site ID"])].copy()
    sites_out["Capacity"] = pd.to_numeric(sites_out["Capacity"]).astype("Int64")
    sites_out["Authoritative Distance (m)"] = pd.to_numeric(sites_out["Authoritative Distance (m)"]).astype("Int64")
    sites_out["Primary Buildings"] = sites_out["Site ID"].map(live_roles.groupby("Site ID").size()).fillna(0).astype(int)
    sites_out = sites_out[["Site ID", "Site Name", "Site Type", "Primary Category", "Operational Importance",
                           "Buildings in Site", "Primary Buildings", "Site Method", "Created Date", "geometry"]
                          + [c for c in REF_COLS]]
    sites_out.to_file(OUT / "critical_sites.gpkg", layer="critical_sites", driver="GPKG")
    sites_out.drop(columns="geometry").to_csv(OUT / "critical_sites.csv", index=False)
    qa.write_reports(df, changes, version, today, new_ids, os_total, OUT, sites_out, ref, ref_gaps, sites_df)
    qa.write_map(final, OUT / "qa_map.html")
    docs.write_docs(df[~df.Excluded], sites_out, version, today, changes, OUT, ROOT)
    print(f"done: {len(final)} buildings in {len(sites_out)} sites ({new_ids} new building IDs) -> outputs/")


if __name__ == "__main__":
    main()
