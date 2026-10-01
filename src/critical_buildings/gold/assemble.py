"""Assemble the final building and site tables (the delivered schema) from the merged records."""
import geopandas as gpd
import pandas as pd

from critical_buildings.gold import quality
from critical_buildings.gold.overrides import OVR_COLS, apply_overrides  # noqa: F401

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
REF_COLS = ["Verification", "Verification Note", "Authoritative Distance (m)", "Authoritative Source",
            "Authoritative ID", "Authoritative Name", "Authoritative Status", "Capacity", "Capacity Unit",
            "Authoritative Detail", "CQC Rating", "Operator / Trust", "Authoritative Postcode", "UPRN", "ODS Code"]
EXTRA = ["Name", "Primary Category", "Building Type", "Operational Importance", "Match Method", "Confidence",
         "Review Reason", "OS Rule Ref", "OSM Rule Ref", "All Matched Rule Refs", "Excluded", "Rule Notes",
         "Site ID", "Site Name", "Site Type", "Site Method", "Buildings in Site",
         "Building Role", "Building Role Default", "Building Role Reason", "Building Role Override",
         "Classification Note", "Review Status", "Review Decision", "Verification Override",
         "Latitude", "Longitude", "Data Quality Score", "Data Quality Band"] + REF_COLS


def assemble(recs, sites_df, rule_version, today, store):
    """Returns (df, sites_out). df has one row per building (incl. excluded ones; see df.Excluded); sites_out one per site."""
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
    df["Rule Version"] = rule_version
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
    df = apply_overrides(df, store)
    # Review Status: '' = nothing to review, Open = needs a decision, Decided = a decision is recorded for it
    df["Review Status"] = ""
    has_reason = df["Review Reason"] != ""
    df.loc[has_reason, "Review Status"] = "Open"
    df.loc[has_reason & (df["Review Decision"] != ""), "Review Status"] = "Decided"
    df["Building Role"] = df["Building Role Override"].where(df["Building Role Override"] != "", df["Building Role Default"])
    ev = lambda o, d: df[o].where(df[o] != "", df[d])     # noqa: E731  (effective value = override, else default)
    df["Primary Category"] = ev("Primary Category Override", "Primary Category Default")
    df["Building Type"] = ev("Building Type Override", "Building Type Default")
    df["Operational Importance"] = ev("Operational Importance Override", "Operational Importance Default")
    # WGS84 coordinates and the transparent data quality score (formula documented in the data dictionary)
    pt = gpd.GeoSeries.from_wkt(df["Geom"], crs=27700).representative_point().to_crs(4326)
    df["Longitude"] = pt.x.round(6)
    df["Latitude"] = pt.y.round(6)
    df["Data Quality Score"] = quality.quality_score(df)
    df["Data Quality Band"] = quality.quality_band(df["Data Quality Score"])
    df = df[SCHEMA + EXTRA]

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
                          + list(REF_COLS)]
    return df, sites_out
