"""Data dictionary, methodology, licences and release notes - generated on every run so they never go stale.

Writes the data-dictionary sheets of report.xlsx and ATTRIBUTION.txt
(a slim, easy view of the main dataset) into the run folder.
"""

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill

from critical_buildings import config, paths, sources
from critical_buildings.errors import PipelineError
from critical_buildings.gold import steps

# ------------------------------------------------------------------ data quality score
_bands = steps.bands_label


# ------------------------------------------------------------------ column dictionary
# name: (group, description, values / example)
D = {
    # identity
    "Building ID": ("Identity", "Stable unique ID for the building record. Kept across runs when the same real building is found again.", "BLDG-000123"),
    "Source": ("Identity", "Which dataset(s) the record comes from.", "OSM; OS Important Buildings; Both; DfE GIAS; CQC; NHS ODS"),
    "Source Feature ID": ("Identity", "ID(s) of the source feature(s) so the record can be traced back. 'Both' shows OS id / OSM id.", "way/7000000; <OS GUID> / way/123; CQC:1-12345"),
    "Name": ("Identity", "Best available name of the building.", "Hartismere Hospital"),
    # classification
    "Primary Category": ("Classification", "Final category after any override: Emergency, Care, Education, Transport, Government Buildings, Miscellaneous.", "Education"),
    "Building Type": ("Classification", "Final building type after any override (a Building Type defined by the rules).", "Primary School"),
    "Operational Importance": ("Classification", "Final operational importance after any override.", "High; Medium; Low"),
    "Primary Category Default": ("Classification", "Category assigned by the rules (before overrides).", "Emergency"),
    "Building Type Default": ("Classification", "Building type assigned by the rules (before overrides).", "Fire Station"),
    "Operational Importance Default": ("Classification", "Importance assigned by the rules.", "High"),
    "Operational Importance Driver Default": ("Classification", "Plain-English reason for the importance, from the rules.", "Emergency response"),
    "Rule Notes": ("Classification", "Notes on the rule tag that classified the record, from the rules.", ""),
    "Classification Note": ("Classification", "Set when an authoritative source changed the building type chosen from OS/OSM.", "Building Type set from CQC (...); OS/OSM said Hospital"),
    "OS Rule Ref": ("Classification", "Rule number matched from OS Important Buildings (blank if none).", "17"),
    "OSM Rule Ref": ("Classification", "Rule number matched from OSM tags (blank if none).", "18"),
    "All Matched Rule Refs": ("Classification", "Every rule number the record matched, separated by ;", "17;20"),
    # overrides
    "Primary Category Override": ("Overrides", "Manual category; when filled it replaces the default.", ""),
    "Building Type Override": ("Overrides", "Manual building type; replaces the default.", ""),
    "Operational Importance Override": ("Overrides", "Manual importance; replaces the default.", ""),
    "Operational Importance Driver Override": ("Overrides", "Manual reason for the importance.", ""),
    "Manual Override Notes": ("Overrides", "Free-text reason for any manual decision on this record.", ""),
    "Building Role Override": ("Overrides", "Manual building role; replaces the default role.", ""),
    "Review Decision": ("Overrides", "Decision recorded against a review item (for example Accepted).", "Accepted"),
    "Verification Override": ("Overrides", "Manual verification result for the whole site.", "Verified (manual)"),
    "Excluded": ("Overrides", "True if a manual decision removed the record. Excluded records are not in the delivered files.", "False"),
    # site
    "Site ID": ("Site", "Stable ID of the site (school, hospital, airfield ...) the building belongs to.", "SITE-000012"),
    "Site Name": ("Site", "Name of the site.", "Culford School"),
    "Site Type": ("Site", "Most common building type among the site's buildings.", "Primary School"),
    "Site Method": ("Site", "How the site was formed: OS Functional Site, OSM site, OSM area, Proximity cluster, Single building, Authoritative source.", "OS Functional Site"),
    "Buildings in Site": ("Site", "Number of building records in the site.", "12"),
    # role
    "Building Role": ("Building role", "Final role of the building within its site after any override.", "Primary; Ancillary; Standalone; Whole site; Unknown"),
    "Building Role Default": ("Building role", "Role chosen by the pipeline.", "Primary"),
    "Building Role Reason": ("Building role", "Plain-English reason for the role.", "Footprint 420 m2 is at least 150 m2"),
    # authoritative
    "Verification": ("Authoritative check", "Whether DfE / CQC / NHS ODS confirms the site. Verified, Verified (manual), Authoritative only (added from the register), Unverified (type is covered but nothing matched), Closed per register, Not covered (no register for this type).", "Verified"),
    "Verification Note": ("Authoritative check", "Why a site is unverified or how far a match was.", "No authoritative record found within 300 m"),
    "Authoritative Distance (m)": ("Authoritative check", "Distance in metres between the site and the matched authoritative point. Over 500 is sent for review.", "35"),
    "Authoritative Source": ("Authoritative check", "Register(s) that matched the site.", "DfE GIAS; CQC; NHS ODS"),
    "Authoritative ID": ("Authoritative check", "Register ID(s): DfE URN, CQC Location ID, NHS ODS code.", "124807"),
    "Authoritative Name": ("Authoritative check", "Name(s) the register uses.", ""),
    "Authoritative Status": ("Authoritative check", "Status from the register.", "Open; Active"),
    "Capacity": ("Authoritative check", "Pupils (DfE) or beds (CQC), summed over matched records.", "243"),
    "Capacity Unit": ("Authoritative check", "Unit of Capacity.", "pupils; beds"),
    "Authoritative Detail": ("Authoritative check", "Type/phase (DfE) or inspection category (CQC).", "Academy converter / Primary"),
    "CQC Rating": ("Authoritative check", "Latest overall CQC rating.", "Good"),
    "Operator / Trust": ("Authoritative check", "Trust, provider or operator.", ""),
    "Authoritative Postcode": ("Authoritative check", "Postcode from the register.", ""),
    "UPRN": ("Authoritative check", "UPRN(s) given by CQC for the location. Not every UPRN at the site: see UPRN Count and UPRNs on the sites file.", ""),
    "ODS Code": ("Authoritative check", "NHS ODS organisation code.", ""),
    # quality and review
    "Match Method": ("Quality and review", "How the record was built: Overlap, Proximity, Proximity (names agree), OS only, OSM only, OSM area (no buildings), Authoritative only.", "Overlap"),
    "Confidence": ("Quality and review", "Confidence in the OS/OSM match: High (overlap), Medium, Low.", "High"),
    "Review Reason": ("Quality and review", "Why the record was flagged for a human look.", ""),
    "Review Status": ("Quality and review", "Blank = nothing to review; Open = needs a decision; Decided = a decision is recorded (rules UI, Review & decisions).", "Open"),
    "Data Quality Score": ("Quality and review", "0-100 score of how well the record is evidenced (see 'Quality score' sheet).", "82"),
    "Data Quality Band": ("Quality and review", f"Band of the score: {_bands()}.", "High"),
    # location
    "Latitude": ("Location", "Latitude (WGS84, EPSG:4326) of a point inside the record.", "52.2406"),
    "Longitude": ("Location", "Longitude (WGS84, EPSG:4326) of a point inside the record.", "0.7167"),
    "Geom": ("Location", "Geometry as WKT in British National Grid (EPSG:27700). The GeoPackage stores the real geometry instead.", "POLYGON((...))"),
    # audit
    "Created Date": ("Audit", "Date the record (or site) first appeared.", "2026-10-01"),
    "Created By": ("Audit", "Who created it ('pipeline' for automatic).", "pipeline"),
    "Last Edit Date": ("Audit", "Date of the last run that produced the record.", "2026-10-01"),
    "Last Edit By": ("Audit", "Who last edited it.", "pipeline"),
    "Rule Version": ("Audit", "Version of the published rule set that produced the record: v<number>-<content hash>.", "v1-cdb511"),
    "Run Date": ("Audit", "Date of the pipeline run.", "2026-10-01"),
}
# columns that exist only on the sites file, or mean something different there
SITE_ONLY = {
    "Primary Buildings": ("Site", "Number of buildings at the site whose role is Primary, Standalone or Whole site.", "3"),
    "Primary Category": ("Classification", "Category of the site.", "Education"),
    "Operational Importance": ("Classification", "Highest importance among the site's buildings.", "High"),
    "Site Method": ("Site", "How the site was formed (see buildings file).", "OS Functional Site"),
    "Created Date": ("Audit", "Date the site first appeared.", "2026-10-01"),
    "UPRN Count": ("Join keys", "Number of OS Open UPRN points inside (or on) the site outline. A UPRN on the shared edge of two sites counts for both.", "42"),
    "UPRNs": ("Join keys", "The UPRNs inside the site outline, separated by semicolons, in numeric order. Holds at most the first 200: UPRN Count is the full number and site_uprns.csv has every one.", "100012345678;100012345679"),
    "Postcode": ("Join keys", "A postcode for the site: the register's when a register record matched, otherwise from Code-Point Open (see Postcode Method).", "IP1 1AA"),
    "Postcode Method": ("Join keys", "Register = given by the matched DfE / CQC / NHS record. Code-Point inside outline = a postcode point lies inside the site (the one nearest its middle). Code-Point nearest = the nearest postcode point within 250 m. Blank = none found.", "Register"),
    "Postcode Distance (m)": ("Join keys", "For Code-Point nearest, the distance from the middle of the site to the postcode point. 0 inside the outline; blank for Register.", "35"),
}


LIMITATIONS = [
    "Pilot area only: Suffolk. Counts and coverage will differ elsewhere.",
    "Importance comes from the rules, by building type. It is not an assessment of each individual building.",
    "OS Open Map Local has no coastguard, lifeboat, government or conference classes, so those types come from OSM only and depend on OSM mapping.",
    "OSM-only records are as good as the volunteer mapping. Verification only exists for types a register covers (schools, care homes, hospitals, hospices, GPs, clinics, mental health).",
    "Private nurseries are not in the DfE data, so nursery sites show Not covered rather than Unverified.",
    "'Closed per register' means the registered organisation is closed; the building may still be in use by another organisation. It is not reported when an open record of a similar name is within 300 m (a merged infant and junior school).",
    "Further and higher education sites show Not covered rather than Unverified when nothing matches: the register has one record per institution, while the map data has a site for each university building.",
    "Building Role is a footprint-size and OSM-tag heuristic (default 150 m2). A small but critical building can be labelled Ancillary; use overrides.",
    "Name matching is heuristic. Matches more than 500 m from the register's point are flagged for review; exact-name matches are accepted up to 2 km.",
    "Hospital sites collect every CQC service and NHS ODS code at the campus, so Authoritative Name/Rating can list several values.",
    "UPRNs are counted per site (UPRN Count, UPRNs, site_uprns.csv), not per building (the buildings file has no UPRN columns). OS Open UPRN points are address locations, so a site with no addresses (a field, a substation) has none.",
    "A site's postcode from Code-Point is a postcode centroid, not the postcode of every address at the site: a large site can span several postcodes, and a small one may be given a neighbour's. Use the UPRNs when an exact link matters.",
    "The data quality score is a heuristic for sorting and filtering, not a probability that a record is correct.",
    "OSM data is licensed under ODbL (share-alike). Check with your information governance or legal team before sharing this dataset outside the organisation.",
]


def _check_complete(df, sites):
    missing = [c for c in list(df.columns) if c not in D]
    missing += [c for c in sites.columns if c != "geometry" and c not in D and c not in SITE_ONLY]
    if missing:
        raise PipelineError("Data dictionary is missing descriptions for: " + ", ".join(sorted(set(missing))))


def _source_dates():
    """One row per source: catalogue text + the manifest of the snapshot in use (what we used, when, how big)."""
    catalogue = sources.load_sources()
    snaps = {m["source"]: m for m in sources.all_manifests()}
    out = []
    for key, cat in catalogue.items():
        if key not in paths.PIPELINE_SOURCES:                    # credit only the sources this dataset is built from
            continue
        m = snaps.get(key)
        files = "; ".join(f["name"] for f in m["files"]) if m else ""
        size = round(sum(f["bytes"] for f in m["files"]) / 1e6, 1) if m else 0
        out.append((cat["name"], cat["uses"], cat["licence"], " ".join(str(cat["attribution"]).split()), files,
                    m["retrieved_at"][:10] if m else "", size))
    return out


def write_docs(xw, df, sites, version, today, changes, out):
    """Add the data-dictionary sheets to the open workbook `xw` and write ATTRIBUTION.txt into `out`."""
    _check_complete(df, sites)
    df = df.copy()

    # ---- attribution ----
    src = _source_dates()
    lines = ["ATTRIBUTION AND LICENCES", "",
             "This dataset combines the sources below. Include the attribution lines when you share or publish it.", ""]
    for name, _use, lic, attr, _files, _date, _size in src:
        lines += [f"{name} - {lic}", f"  {attr}", ""]
    lines += [("Note: OpenStreetMap data is under the ODbL (share-alike). Check with your information governance "
               "or legal team before distributing this dataset outside the organisation."), ""]
    (out / "ATTRIBUTION.txt").write_text("\n".join(lines), encoding="utf-8")

    # ---- data dictionary workbook ----
    def dict_rows(cols, extra):
        rows = []
        for c in cols:
            if c == "geometry":
                continue
            g, desc, ex = extra.get(c) or D[c]
            rows.append((g, c, desc, ex))
        return pd.DataFrame(rows, columns=["Group", "Column", "What it means", "Allowed values / example"])

    b_cols = list(df.columns)
    s_cols = list(sites.columns)
    readme = pd.DataFrame({"Critical Buildings dataset - read me": [
        f"Run date: {today}   Rules version: {version}",
        "Area: Suffolk (pilot).",
        ("Files: dataset.gpkg (layers buildings and sites), buildings.csv and sites.csv (one row per building / site), "
         "report.xlsx (this workbook: Summary sheet = slim view, QA sheets, dictionary). The QA map is drawn on demand in the UI."),
        "Start with the summary CSV. Use 'Building Type', 'Operational Importance', 'Building Role' and 'Verification'.",
        ("Fields ending in Default come from the rules; fields ending in Override are manual. 'Building Type' and similar "
         "un-suffixed fields show the final value."),
        "Sheets: Buildings columns, Sites columns, Values, Quality score, Sources and licences, Methodology, Known limitations, Release.",
    ]})
    values = pd.DataFrame([
        ("Source", "OSM", "Found in OpenStreetMap only"), ("Source", "OS Important Buildings", "Found in OS Open Map Local only"),
        ("Source", "Both", "OSM and OS agree it is the same building"),
        ("Source", "DfE GIAS / CQC / NHS ODS", "Added from an authoritative register that had no matching building"),
        ("Building Role", "Standalone", "Only building at its site"), ("Building Role", "Primary", "Main building at a multi-building site"),
        ("Building Role", "Ancillary", "Outbuilding, shed, garage or small building"),
        ("Building Role", "Whole site", "The record is the site area itself (no buildings found inside)"),
        ("Building Role", "Unknown", "Point only, no footprint, at a multi-building site"),
        ("Verification", "Verified", "A register matched this site"), ("Verification", "Verified (manual)", "A reviewer confirmed it against a register"),
        ("Verification", "Authoritative only", "Added from a register; OSM and OS did not have it"),
        ("Verification", "Unverified", "A register covers this type but nothing matched; may be closed, mis-tagged or unregistered"),
        ("Verification", "Closed per register", "The matching register record is closed"),
        ("Verification", "Not covered", "No register exists for this building type"),
        ("Confidence", "High / Medium / Low", "High = OS and OSM footprints overlap; Low = only close together with differing names"),
        ("Operational Importance", "High / Medium / Low", "From the rules, by building type"),
    ], columns=["Column", "Value", "Meaning"])
    quality = pd.DataFrame([
        ("Authoritative check (0-40)", "Verified, Verified (manual), Authoritative only = 40; Not covered = 20; Unverified = 10; Closed per register = 0"),
        ("Source agreement (0-25)", "Both OSM and OS = 25; register only = 20; OSM only or OS only = 12"),
        ("Match confidence (0-20)", "High = 20; Medium = 12; Low = 5"),
        ("Geometry (0-15)", "Building footprint polygon = 15; point only = 5"),
        ("Open review item", "-10 while the record has an Open review status"),
        ("Total", f"Sum, capped 0-100. Bands: {_bands()}. A heuristic for sorting and filtering, not a probability."),
    ], columns=["Component", "Points"])
    sources = pd.DataFrame(src, columns=["Source", "What we use", "Licence", "Attribution", "Files", "Downloaded", "Size (MB)"])
    method = pd.DataFrame({"Step": [
        "1. Rules check: the Rules, Role settings and Reference mapping sheets are validated; the run stops with plain-English errors if wrong.",
        "2. Classify: each OSM feature (tags) and OS Important Building / station (theme > classification) is matched to a rule. The lowest Priority number wins.",
        ("3. Merge: an OS building and an OSM feature are the same building if same category (same building type outside Education) and they overlap "
         f"(>= {config.get().merge.overlap_fraction:.0%} of the OS polygon) or are within {config.get().merge.point_distance_m} m "
         f"(stations {config.get().merge.point_distance_by_type.get('Railway Station')} m). Proximity-only matches need the same type and similar names."),
        (f"4. Sites: buildings are grouped by OS Functional Site, then OSM site/area polygon, then {config.get().sites.cluster_gap_m} m proximity cluster; "
         "same-named strays within "
         f"{config.get().sites.attach_m} m join the site."),
        ("5. Authoritative check: DfE, CQC and NHS ODS records are matched to sites by type, distance and name. Unmatched register records are added as new buildings; "
         "same-name records of a different type reclassify the site; sites no register recognises are marked Unverified."),
        "6. Roles: buildings are labelled Primary / Ancillary / Standalone / Whole site / Unknown from footprint size and OSM building tags (Role settings sheet).",
        "7. Overrides: manual decisions recorded in the control store (rules UI, Review & decisions) are applied last and survive reruns. Building IDs and Site IDs stay stable between runs.",
        "8. Outputs, QA workbook, map and this dictionary are regenerated; changes since the previous run are listed in report.xlsx.xlsx.",
    ]})
    limits = pd.DataFrame({"Known limitation": LIMITATIONS})
    release = pd.DataFrame([
        ("Run date", str(today)), ("Rules version", version), ("Area", "Suffolk (pilot)"),
        ("Buildings", len(df)), ("Sites", len(sites)),
        ("Data quality bands", ", ".join(f"{k} {v}" for k, v in df["Data Quality Band"].value_counts().items())),
        ("Verification", ", ".join(f"{k} {v}" for k, v in df["Verification"].value_counts().items())),
        ("Review items open", int((df["Review Status"] == "Open").sum())),
        ("Changes since previous run", "first run" if changes is None else (
            ", ".join(f"{k} {v}" for k, v in changes.groupby("Change").size().items()) or "none")),
    ], columns=["Item", "Value"])

    readme.to_excel(xw, sheet_name="Read me", index=False)
    dict_rows(b_cols, {}).to_excel(xw, sheet_name="Buildings columns", index=False)
    dict_rows(s_cols, SITE_ONLY).to_excel(xw, sheet_name="Sites columns", index=False)
    values.to_excel(xw, sheet_name="Values", index=False)
    quality.to_excel(xw, sheet_name="Quality score", index=False)
    sources.to_excel(xw, sheet_name="Sources and licences", index=False)
    method.to_excel(xw, sheet_name="Methodology", index=False)
    limits.to_excel(xw, sheet_name="Known limitations", index=False)
    release.to_excel(xw, sheet_name="Release", index=False)


SLIM_COLUMNS = ["Building ID", "Name", "Building Type", "Primary Category", "Operational Importance", "Building Role",
                "Site ID", "Site Name", "Verification", "Data Quality Band", "Data Quality Score", "Source",
                "Authoritative Source", "Capacity", "Capacity Unit", "Latitude", "Longitude"]


DICTIONARY_SHEETS = {"Read me", "Buildings columns", "Sites columns", "Values", "Quality score", "Sources and licences",
                     "Methodology", "Known limitations", "Release"}


def style_workbook(xw):
    """Header row, frozen header and column widths on every sheet; wrapped text on the dictionary sheets."""
    head = PatternFill("solid", fgColor="1F3864")
    for ws in xw.book.worksheets:
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head
        ws.freeze_panes = "A2"
        is_doc = ws.title in DICTIONARY_SHEETS
        for col in ws.columns:
            if is_doc:
                width = min(90, max(14, max(len(str(c.value or "")) for c in col[:60]) + 2))
            else:
                width = min(60, max(12, max(len(str(c.value or "")) for c in col[:200]) + 2))
            ws.column_dimensions[col[0].column_letter].width = width
        if is_doc:
            for row in ws.iter_rows(min_row=2):
                for c in row:
                    c.alignment = Alignment(wrap_text=True, vertical="top")
