"""Publish stage: the delivered files for one run (GeoPackage/CSV outputs, QA workbook and map).
"""
import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

from critical_buildings import dictionary as docs
from critical_buildings import progress

log = logging.getLogger(__name__)


# ====================================================================================================
# QA outputs: change tracking, summary/review workbook, and the clickable map
# ====================================================================================================

CAT_COLOURS = {"Emergency": "#d62728", "Care": "#e377c2", "Education": "#1f77b4", "Transport": "#2ca02c",
               "Government Buildings": "#7f4fb0", "Miscellaneous": "#ff7f0e"}
SRC_COLOURS = {"Both": "#000000", "OS Important Buildings": "#1f4e9c", "OSM": "#8c564b",
               "DfE GIAS": "#17becf", "CQC": "#bcbd22", "NHS ODS": "#7f7f7f"}


def suggest_action(reason):
    """Plain-English next step for a review item. Decisions are recorded in the rules UI (Review & decisions tab)."""
    r = str(reason)
    if "no building within" in r:
        return "Check the point on the map. If it is on a real building accept it; if the site has closed, exclude it."
    if "Possible duplicate" in r:
        return "If this is the same place as the existing site named, exclude this record."
    if "Proximity-only" in r:
        return "Open on the map. If both features are the same building, accept; if different buildings, tell the data owner."
    if "OS rule" in r:
        return "Record the correct Building Type override, or accept."
    if "OSM candidates" in r:
        return "Accept unless the map shows the wrong OSM feature was chosen."
    if "away from the OS/OSM location" in r:
        return "The registers and OS/OSM disagree on where this is. Check on the map which is right, then accept or exclude."
    return "Check on the map, then accept or correct."


def suggest_verification_action(verification, note):
    note = str(note)
    if verification == "Closed per register":
        return "Confirm the closure, then exclude the whole site, or record a Verification Override if it is still open."
    if note.startswith("Nearest authoritative record not matched"):
        return ("If the nearest record is the same place, record a Verification Override of 'Verified (manual)'. "
                "Otherwise check the site is still open.")
    return "No register recognises this site. Check it is still open (e.g. NHS / CQC / Google); if not, exclude the site."


def write_reports(xw, df, changes, version, today, new_ids, os_total, sites=None, ref=None, ref_gaps=None):
    """Add the run-summary sheets to the open workbook `xw`."""
    live = df[~df.Excluded]
    summary = pd.DataFrame([
        ("Run date", str(today)), ("Rule version", version), ("Buildings in dataset", len(live)),
        ("  from OSM only", int((live.Source == "OSM").sum())),
        ("  from OS only", int((live.Source == "OS Important Buildings").sum())),
        ("  matched in both", int((live.Source == "Both").sum())),
        ("Sites", "" if sites is None else len(sites)),
        ("  authoritative-only records (DfE / CQC / NHS ODS)", int((~live.Source.isin(["OSM", "OS Important Buildings", "Both"])).sum())),
        ("Sites verified by an authoritative source", "" if sites is None else int((sites["Verification"] == "Verified").sum())),
        ("Sites unverified (type is covered, no match)", "" if sites is None else int((sites["Verification"] == "Unverified").sum())),
        ("Sites closed per register", "" if sites is None else int((sites["Verification"] == "Closed per register").sum())),
        ("Review items still open", int((live["Review Status"] == "Open").sum())),
        ("Review items decided", int((live["Review Status"] == "Decided").sum())),
        ("Excluded by override", int(df.Excluded.sum())),
        ("New Building IDs this run", new_ids),
        ("OS Important Buildings in area (all types)", os_total),
        ("Changes vs previous run", "first run" if changes is None else len(changes)),
    ], columns=["Item", "Value"])
    by_type = (live.pivot_table(index=["Primary Category", "Building Type"], columns="Source",
                                values="Building ID", aggfunc="count", fill_value=0)
               .assign(Total=lambda x: x.sum(axis=1)).reset_index())
    by_conf = live.groupby(["Confidence", "Match Method"]).size().rename("Records").reset_index()
    review = live[live["Review Status"] != ""][["Building ID", "Review Status", "Name", "Building Type", "Source",
                                                 "Review Reason", "Review Decision", "Manual Override Notes",
                                                 "Source Feature ID", "Confidence"]].copy()
    review.insert(6, "Suggested Action", review["Review Reason"].map(suggest_action))
    review = review.sort_values(["Review Status", "Building Type"], ascending=[False, True])
    summary.to_excel(xw, sheet_name="Run summary", index=False)
    by_type.to_excel(xw, sheet_name="Coverage by type", index=False)
    by_conf.to_excel(xw, sheet_name="Confidence", index=False)
    (live.groupby(["Primary Category", "Building Role"]).size().unstack(fill_value=0).reset_index()
     ).to_excel(xw, sheet_name="Building roles", index=False)
    if sites is not None and ref is not None:
        cov = (sites.drop(columns="geometry").groupby(["Site Type", "Verification"]).size().unstack(fill_value=0))
        cov["Authoritative records"] = ref.groupby("building_type").size().reindex(cov.index).fillna(0).astype(int)
        cov.reset_index().to_excel(xw, sheet_name="Authoritative coverage", index=False)
        ref_gaps[["ref_source", "ref_id", "ref_name", "building_type", "detail", "postcode", "capacity",
                  "capacity_unit"]].to_excel(xw, sheet_name="Authoritative gaps added", index=False)
        unv = sites[sites["Verification"].isin(["Unverified", "Closed per register"])].drop(columns="geometry")[
            ["Site ID", "Site Name", "Site Type", "Verification", "Verification Note", "Buildings in Site",
             "Site Method"]].copy()
        unv.insert(5, "Suggested Action", [suggest_verification_action(v, n) for v, n in
                                           zip(unv["Verification"], unv["Verification Note"])])
        unv.sort_values(["Verification", "Site Type", "Site Name"]).to_excel(
            xw, sheet_name="Unverified sites", index=False)
    if sites is not None:
        st = (sites.drop(columns="geometry").groupby(["Primary Category", "Site Type", "Site Method"])
              .agg(Sites=("Site ID", "count"), Buildings=("Buildings in Site", "sum")).reset_index())
        st.to_excel(xw, sheet_name="Sites by type", index=False)
    review.to_excel(xw, sheet_name="Review queue", index=False)
    (changes if changes is not None else pd.DataFrame({"Note": ["First run - no previous output to compare"]})
     ).to_excel(xw, sheet_name="Changes vs previous", index=False)
    return summary


def write_map(gdf, path):
    import folium
    pts = gdf.copy()
    pts["geometry"] = pts.geometry.representative_point()
    pts = pts.to_crs(4326)
    m = folium.Map(location=[52.19, 1.0], zoom_start=9, tiles="OpenStreetMap")
    groups = {s: folium.FeatureGroup(name=f"Source: {s}") for s in SRC_COLOURS}
    for _, r in pts.iterrows():
        pop = (f"<b>{r['Building Type']}</b> ({r['Primary Category']})<br>{r['Name']}<br>"
               f"Importance: {r['Operational Importance']}<br>ID: {r['Building ID']}<br>"
               f"Verification: {r['Verification']}" + (f" - {r['Authoritative Name']} ({r['Authoritative ID']}; {r['Authoritative Detail']})" if r['Authoritative ID'] else "") + f"<br>Role: {r['Building Role']} ({r['Building Role Reason']})<br>Site: {r['Site Name']} ({r['Site ID']}, {r['Buildings in Site']} bldgs, {r['Site Method']})<br>"
               f"Source: {r['Source']} ({r['Confidence']}, {r['Match Method']})<br>{r['Source Feature ID']}"
               + (f"<br><i>Review: {r['Review Reason']}</i>" if r["Review Reason"] else ""))
        folium.CircleMarker([r.geometry.y, r.geometry.x], radius=5 if r.Source == "Both" else 4,
                            color=SRC_COLOURS.get(r.Source, "#666666"), weight=2 if r.Source == "Both" else 1,
                            fill=True, fill_color=CAT_COLOURS.get(r["Primary Category"], "#999"),
                            fill_opacity=0.85, popup=folium.Popup(pop, max_width=320)).add_to(groups.setdefault(r.Source, folium.FeatureGroup(name=f"Source: {r.Source}")))
    for g in groups.values():
        g.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    legend = ("<div style='position:fixed;bottom:20px;left:20px;z-index:9999;background:white;padding:8px 10px;"
              "border:1px solid #888;font:12px sans-serif'><b>Category (fill)</b><br>" +
              "".join(f"<span style='color:{c}'>&#9679;</span> {k}<br>" for k, c in CAT_COLOURS.items()) +
              "<b>Source (outline)</b><br>" +
              "".join(f"<span style='color:{c}'>&#9675;</span> {k}<br>" for k, c in SRC_COLOURS.items()) + "</div>")
    m.get_root().html.add_child(folium.Element(legend))
    m.save(path)


# ====================================================================================================
# Writing the delivered files
# ====================================================================================================

# What every run folder contains (used by release/archive and the regression test). Older runs used longer file names
# (critical_buildings.csv, critical_sites.csv, critical_buildings.gpkg ...); find_output reads both.
DELIVERABLES = ["dataset.gpkg", "buildings.csv", "sites.csv", "report.xlsx", "ATTRIBUTION.txt"]
OUTPUT_NAMES = {"buildings": ("buildings.csv", "critical_buildings.csv"), "sites": ("sites.csv", "critical_sites.csv"),
                "gpkg": ("dataset.gpkg", "critical_buildings.gpkg")}


def find_output(folder, kind):
    """Path of one output ('buildings', 'sites' or 'gpkg') in a run or release folder, in either file layout; None if absent."""
    for name in OUTPUT_NAMES[kind]:
        if (Path(folder) / name).exists():
            return Path(folder) / name
    return None


def write_outputs(gold, silver, dest, rule_version, today):
    df, sites_out = gold.df, gold.sites_out
    live = df[~df.Excluded]
    gdf = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df["Geom"], crs=27700), crs=27700)
    final = gdf[~gdf.Excluded].drop(columns=["Geom"])   # the layer's own geometry replaces the WKT text column (CSV keeps it)
    progress.step("Writing the GeoPackage (buildings and sites layers) and the CSV files")
    final.to_file(dest / "dataset.gpkg", layer="buildings", driver="GPKG")
    sites_out.to_file(dest / "dataset.gpkg", layer="sites", driver="GPKG", mode="a")
    live.to_csv(dest / "buildings.csv", index=False)
    sites_out.drop(columns="geometry").to_csv(dest / "sites.csv", index=False)
    progress.step("Writing the report workbook (summary, review queue, data dictionary)")
    with pd.ExcelWriter(dest / "report.xlsx") as xw:
        live[docs.SLIM_COLUMNS].to_excel(xw, sheet_name="Summary", index=False)
        write_reports(xw, df, gold.changes, rule_version, today, gold.new_ids, silver.os_total, sites_out,
                      silver.ref, gold.ref_gaps)
        docs.write_docs(xw, live, sites_out, rule_version, today, gold.changes, dest)
        docs.style_workbook(xw)
    progress.note("buildings written", len(final))
    progress.note("sites written", len(sites_out))
    return len(final), len(sites_out)
