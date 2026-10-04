"""Gold-stage steps after matching: stable IDs, human overrides, quality score, change tracking, final assembly,
and the gold build that strings the whole stage together.

Pure function of (silver tables, rule set, human decisions + identity registries in the control store).
"""
import logging
from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import pandas as pd

from critical_buildings import config, paths, progress
from critical_buildings.gold import merge, roles
from critical_buildings.gold import reference_match as refmatch
from critical_buildings.gold import sites as sitelib

log = logging.getLogger(__name__)


# ====================================================================================================
# Stable identities: Building IDs and Site IDs survive reruns (registries in the control store)
# ====================================================================================================

def assign_ids(recs, today, store, run_id, pending=None):
    """Keep Building IDs stable across runs (match on OS id, then OSM id, then footprint overlap).

    The registry is written straight away unless `pending` (a dict) is given: then the write is left in it for
    store.commit_identities, to be applied only once the whole run has succeeded."""
    recs = recs.copy()
    recs["os_key"] = recs.source_feature_id.str.split(" / ").str[0].where(recs.source != "OSM")
    recs["osm_key"] = recs.source_feature_id.str.split(" / ").str[-1].where(recs.source != "OS Important Buildings")
    reg = store.building_registry_df()
    by_os = {k: b for k, b in zip(reg.os_key, reg.building_id) if isinstance(k, str)}
    osm_only = reg[reg.os_key.isna()]
    by_osm = {k: b for k, b in zip(osm_only.osm_key, osm_only.building_id) if isinstance(k, str)}
    created = dict(zip(reg.building_id, reg.created))
    nxt = (reg.building_id.str[5:].astype(int).max() + 1) if len(reg) else 1
    ids = []
    taken = set()
    for os_key, osm_key, source in zip(recs.os_key, recs.osm_key, recs.source):
        b = by_os.get(os_key) if isinstance(os_key, str) else None
        if b is None and isinstance(osm_key, str) and source == "OSM":
            b = by_osm.get(osm_key)
        if b is None and isinstance(osm_key, str) and source == "Both":
            b = by_osm.get(osm_key)          # was OSM-only last run, now also matched to OS
        if b in taken:
            b = None
        ids.append(b)
        if b:
            taken.add(b)
    # footprint-overlap fallback for the rest (e.g. OSM feature redrawn under a new id)
    todo = [n for n, i in enumerate(ids) if i is None]
    if len(reg) and todo:
        old = gpd.GeoSeries(gpd.GeoSeries.from_wkt(reg.wkt), crs=27700).reset_index(drop=True)
        old_ids = reg.building_id.to_numpy()
        geoms = recs.geometry.iloc[todo].reset_index(drop=True)
        # one spatial-index query for every unmatched record (not a scan of the whole registry per record);
        # pairs are visited record by record, registry order within a record, so the result is deterministic
        qi, ti = old.sindex.query(geoms.values, predicate="intersects")
        order = np.lexsort((ti, qi))
        done = set()
        for q, t in zip(qi[order], ti[order]):
            cand = old_ids[t]
            if q in done or cand in taken:
                continue
            g, o = geoms.iloc[q], old.iloc[t]
            if o.intersection(g).area / max(min(o.area, g.area), 1e-9) >= 0.5:
                ids[todo[q]] = cand
                taken.add(cand)
                done.add(q)
    new = 0
    for n, i in enumerate(ids):
        if i is None:
            ids[n] = f"BLDG-{nxt:06d}"
            nxt += 1
            new += 1
        created.setdefault(ids[n], str(today))
    recs["Building ID"] = ids
    recs["Created Date"] = recs["Building ID"].map(created)
    registry = pd.DataFrame({"building_id": recs["Building ID"], "os_key": recs.os_key, "osm_key": recs.osm_key,
                                 "created": recs["Created Date"], "wkt": recs.geometry.to_wkt()})
    if pending is None:
        store.save_building_registry(registry, run_id)
    else:
        pending["buildings"] = registry
    return recs, new


def assign_site_ids(recs, sites, today, store, run_id, pending=None):
    """Stable Site IDs: a site keeps the ID most of its buildings were last assigned (remembered in the registry).
    `pending` works as in assign_ids."""
    old_site = store.building_site_map()
    reg = store.site_registry_df()
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
    site_created = {sid: created[sid] for sid in mapping.values()}
    building_sites = dict(zip(recs["Building ID"], recs["Site ID"]))
    if pending is None:
        store.save_site_registry(site_created, run_id)
        store.set_building_sites(building_sites)
    else:
        pending["sites"], pending["building_sites"] = site_created, building_sites
    return recs, sites


# ====================================================================================================
# Human decisions (from the control store) applied to the assembled building table
# ====================================================================================================

OVR_COLS = ["Primary Category Override", "Building Type Override", "Operational Importance Override",
            "Operational Importance Driver Override", "Manual Override Notes", "Building Role Override",
            "Review Decision", "Verification Override"]


def apply_overrides(df, store):
    ov = store.overrides_df().fillna("")
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


# ====================================================================================================
# Data quality score (0-100) and band (points and band edges come from settings.yml)
# ====================================================================================================

def bands_label():
    q = config.get().quality
    return f"High {q.band_high_min}-100, Medium {q.band_medium_min}-{q.band_high_min - 1}, Low 0-{q.band_medium_min - 1}"


def quality_score(df):
    """0-100 score from how well a record is evidenced. A heuristic to help sorting and filtering, not a probability."""
    q = config.get().quality
    ver = df["Verification"].map(q.verification_points).fillna(10)
    src = df["Source"].map(q.source_points).fillna(q.register_points)
    conf = df["Confidence"].map(q.confidence_points).fillna(12)
    geom = np.where(df["Geom"].str.upper().str.startswith("POINT"), q.point_points, q.polygon_points)
    pen = np.where(df["Review Status"] == "Open", q.open_review_penalty, 0)
    return (ver + src + conf + geom + pen).clip(0, 100).astype(int)


def quality_band(score):
    q = config.get().quality
    return pd.cut(score, [-1, q.band_medium_min - 1, q.band_high_min - 1, 100], labels=["Low", "Medium", "High"]).astype(str)


# ====================================================================================================
# Change tracking between two runs
# ====================================================================================================

def diff_runs(prev, cur):
    """Compare two runs by Building ID. Returns a DataFrame of added / removed / reclassified / source changes.

    The previous run's buildings.csv holds live rows only, so excluded rows of `cur` are left out: excluding a building
    shows as Removed, and an already-excluded one is not reported as Added on every run."""
    if "Excluded" in cur:
        cur = cur[~cur["Excluded"].astype(bool)]
    p = prev.set_index("Building ID")
    c = cur.set_index("Building ID")
    rows = []
    for b in sorted(set(c.index) - set(p.index)):
        rows.append(("Added", b, c.at[b, "Building Type Default"], "", c.at[b, "Source Feature ID"]))
    for b in sorted(set(p.index) - set(c.index)):
        rows.append(("Removed", b, p.at[b, "Building Type Default"], "", p.at[b, "Source Feature ID"]))
    for b in sorted(set(p.index) & set(c.index)):
        if p.at[b, "Building Type Default"] != c.at[b, "Building Type Default"]:
            rows.append(("Reclassified", b, p.at[b, "Building Type Default"], c.at[b, "Building Type Default"],
                         c.at[b, "Source Feature ID"]))
        elif p.at[b, "Source"] != c.at[b, "Source"]:
            rows.append(("Source changed", b, p.at[b, "Source"], c.at[b, "Source"], c.at[b, "Source Feature ID"]))
    return pd.DataFrame(rows, columns=["Change", "Building ID", "Before / Type", "After", "Source Feature ID"])


# ====================================================================================================
# Assemble the final building and site tables (the delivered schema)
# ====================================================================================================

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
    ev = lambda o, d: df[o].where(df[o] != "", df[d])
    df["Primary Category"] = ev("Primary Category Override", "Primary Category Default")
    df["Building Type"] = ev("Building Type Override", "Building Type Default")
    df["Operational Importance"] = ev("Operational Importance Override", "Operational Importance Default")
    # WGS84 coordinates and the transparent data quality score (formula documented in the data dictionary)
    pt = gpd.GeoSeries.from_wkt(df["Geom"], crs=27700).representative_point().to_crs(4326)
    df["Longitude"] = pt.x.round(6)
    df["Latitude"] = pt.y.round(6)
    df["Data Quality Score"] = quality_score(df)
    df["Data Quality Band"] = quality_band(df["Data Quality Score"])
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


# ====================================================================================================
# The gold build
# ====================================================================================================

log = logging.getLogger(__name__)


@dataclass
class GoldResult:
    df: pd.DataFrame            # one row per building (incl. excluded; filter on df.Excluded)
    sites_out: object           # GeoDataFrame, one row per site
    sites_df: object            # working site table (before output formatting)
    changes: object             # DataFrame of changes vs the previous run, or None on a first run
    new_ids: int
    ref_gaps: object
    ref_matches: object
    identities: dict = field(default_factory=dict)    # pending registry writes (see store.commit_identities)


def build(silver, rules, store, run_id, today, previous_buildings=None, area=paths.DEFAULT_AREA):
    """previous_buildings: the previous run's buildings table (strings), or None on a first run."""
    progress.step("Merging OS buildings and OSM features into one record per building")
    recs = merge.merge(silver.osm, silver.os_, rules.priorities)
    for source, n in recs.source.value_counts().items():
        progress.note(f"records from {source}", int(n))
    progress.step("Grouping buildings into sites")
    recs, sites_df = sitelib.assign_sites(recs, silver.site_cands)
    progress.note("sites formed", len(sites_df))
    progress.step("Matching against the registers (DfE, CQC, NHS ODS)")
    recs, sites_df, ref_gaps, ref_matches = refmatch.attach_reference(
        recs, sites_df, silver.ref, rules.tags, silver.boundary, silver.ref_all, silver.postcodes, area)
    progress.note("register records matched to a site", int(ref_matches.ri.nunique()))
    progress.note("register records added as new buildings", len(ref_gaps))
    progress.step("Assigning stable Building and Site IDs")
    identities = {}      # registry writes, applied by runs.finish once the run has succeeded
    recs, new_ids = assign_ids(recs, today, store, run_id, identities)
    recs, sites_df = assign_site_ids(recs, sites_df, today, store, run_id, identities)
    progress.note("new building IDs", new_ids)
    progress.step("Assigning building roles, applying decisions and scoring quality")
    recs = roles.assign_roles(recs, rules.role)
    df, sites_out = assemble(recs, sites_df, rules.label, today, store)
    progress.note("buildings (before exclusions)", len(df))
    progress.note("excluded by a decision", int(df.Excluded.sum()))
    progress.step("Comparing with the previous run")
    changes = diff_runs(previous_buildings, df) if previous_buildings is not None else None
    progress.note("changes vs previous run", "no previous run" if changes is None else len(changes))
    return GoldResult(df, sites_out, sites_df, changes, new_ids, ref_gaps, ref_matches, identities)
