"""The Properties tab: build the UPRN property table, see what it covers and whether it is out of date, and explore it.

The linking itself runs in a separate process (the `properties` command, started as a background job like a build), so it survives a page
reload and a 30-minute England build does not tie up the page. The explorer reads the finished parquet table through pyarrow, reading only
the columns a question needs, so it stays quick even for England's 35 million rows.
"""
import json

import pandas as pd
import streamlit as st

from critical_buildings import paths
from critical_buildings.properties import build
from critical_buildings.ui import flow, jobs, logic, views
from critical_buildings.ui import properties_logic as pl

FLOOD_CHOICES = {"Any": 1, "Zone 2 or 3": 2, "Zone 3 only": 3}
CRITICAL_CHOICES = {"Any": None, "Inside a critical site": "inside", "Within a distance of a critical site": "within"}


# ------------------------------------------------------------------------------------------------ cached queries
# Arguments are plain strings so Streamlit can cache on them; the dataset is opened inside (opening it only reads file footers).
@st.cache_data(show_spinner=False)
def _districts(table_dir):
    return pl.districts(pl.open_table(table_dir))


@st.cache_data(show_spinner=False)
def _site_types(table_dir):
    return pl.critical_site_types(pl.open_table(table_dir))


@st.cache_data(show_spinner="Counting ...", ttl=900)
def _count(table_dir, filters_json):
    return pl.count(pl.open_table(table_dir), pl.expression(json.loads(filters_json)))


@st.cache_data(show_spinner=False, ttl=900)
def _breakdown(table_dir, filters_json, column):
    return pl.breakdown(pl.open_table(table_dir), pl.expression(json.loads(filters_json)), column)


@st.cache_data(show_spinner="Finding properties ...", ttl=900)
def _sample(table_dir, filters_json, n):
    return pl.sample(pl.open_table(table_dir), pl.expression(json.loads(filters_json)), pl.DISPLAY_COLUMNS, n)


# ------------------------------------------------------------------------------------------------ the tab
def tab_properties(s, area):
    st.subheader("The UPRN property table")
    st.caption("One row for every address point (UPRN) in the area, linked to open data about it: building, postcode, census area and "
               "deprivation, conservation and listed status, flood zone, and the nearest critical site. A UPRN is any addressable location, "
               "so it is not only homes.")
    latest = pl.latest_table(area)
    problems = build.check(area)
    job = jobs.latest_job()
    busy = bool(job and job["state"] == "running")

    if latest is None:
        st.info(f"No property table has been built for **{area}** yet.")
    else:
        c = st.columns(4)
        c[0].metric("UPRNs", f"{latest['rows']:,}")
        c[1].metric("Grid squares", len(latest["tiles"]))
        c[2].metric("Size on disk", f"{latest['size_mb']:,.0f} MB")
        c[3].metric("Built", str(latest["built_at"])[:10])
        for reason in pl.out_of_date(area, latest):
            st.warning(reason + " Build again to bring the table up to date.")

    if busy:
        st.warning(f"**{job['label']}** is running. Buttons are disabled until it ends; you can leave this page open or reload it.")
    if problems:
        st.error("Cannot build yet:\n\n" + "\n".join(f"- {p}" for p in problems))

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**1. Fetch the property sources**")
        st.caption("Census areas, deprivation, planning layers and flood zones (about 2.4 GB the first time; later only what changed).")
        if st.button("Fetch the property sources", disabled=busy):
            jobs.start_job("Fetch the property sources", "fetch", "--area", area, "--only", "properties")
            st.rerun()
    with c2:
        st.markdown("**2. Build the table**")
        st.caption("About a minute for Suffolk; about 30 minutes and 10 GB of memory for England. The critical_site columns use the area's "
                   "current critical-buildings run, so build that first.")
        if st.button("Build the property table", type="primary", disabled=busy or bool(problems)):
            jobs.start_job("Build the property table", "properties", "--area", area)
            st.rerun()

    with st.expander("How the table is made"):
        rows = logic.source_rows(area, only=build.ATTRIBUTION_SOURCES)
        st.graphviz_chart(flow.properties_dot(rows, latest, latest["critical_run"] if latest else None), width="stretch")
        st.dataframe(pd.DataFrame(rows)[["name", "snapshot", "retrieved_at", "age", "refresh", "licence"]], hide_index=True, width="stretch")

    st.divider()
    (st.fragment(views.live_panel, run_every=2) if busy else st.fragment(views.live_panel))(area)
    if latest is None:
        return

    st.divider()
    st.markdown("### What it covers")
    cover = pd.DataFrame(pl.coverage_rows(latest["summary"]))
    st.dataframe(cover.assign(Share=cover["Share"].map("{:.1%}".format), UPRNs=cover["UPRNs"].map("{:,}".format)), hide_index=True, width="stretch")
    st.caption("Deprivation is an area measure; the postcode is the nearest postcode centre; flood zones are rivers and sea only and ignore "
               "defences. The guide lists every caveat, and the column descriptions are below.")

    explorer(latest)

    with st.expander("What each column means"):
        cols = pd.read_csv(latest["dest"] / "columns.csv")
        group = st.selectbox("Group", ["All"] + list(dict.fromkeys(cols["group"])), key="prop_col_group")
        st.dataframe(cols if group == "All" else cols[cols["group"] == group], hide_index=True, width="stretch")
    with st.expander("Credits to include when you share it"):
        st.code((latest["dest"] / "ATTRIBUTION.txt").read_text(encoding="utf-8"))
        st.caption(f"Table {latest['run_id']}. Linked to critical-buildings run {latest['critical_run'] or 'none'}.")


def explorer(latest):
    st.divider()
    st.markdown("### Explore")
    table_dir = str(latest["table"])
    total = latest["rows"]
    a = st.columns(3)
    district = a[0].selectbox("Local authority district", ["Whole area"] + _districts(table_dir), key="prop_district")
    flood = a[1].selectbox("Flood zone", list(FLOOD_CHOICES), key="prop_flood")
    decile = a[2].slider("Deprivation decile (1 = most deprived)", 1, 10, (1, 10), key="prop_decile")
    b = st.columns(4)
    conservation = b[0].checkbox("In a conservation area", key="prop_ca")
    article4 = b[1].checkbox("In an Article 4 area", key="prop_a4")
    listed = b[2].checkbox("Inside a listed building outline", key="prop_listed")
    critical = CRITICAL_CHOICES[b[3].selectbox("Critical sites", list(CRITICAL_CHOICES), key="prop_critical")]
    filters = {"district": None if district == "Whole area" else district, "flood_min": FLOOD_CHOICES[flood], "decile": list(decile),
               "conservation_area": conservation, "article_4_area": article4, "listed_in_outline": listed, "critical": critical}
    if critical:
        d = st.columns(3)
        if critical == "within":
            filters["critical_m"] = d[0].slider("Within (metres)", 50, 1000, 250, 50, key="prop_dist")
        filters["importance"] = d[1].multiselect("Importance of the site", ["High", "Medium", "Low"], key="prop_imp")
        filters["site_types"] = d[2].multiselect("Type of site", _site_types(table_dir), key="prop_types")
    fj = json.dumps(filters, sort_keys=True)

    n = _count(table_dir, fj)
    m = st.columns(3)
    m[0].metric("Matching UPRNs", f"{n:,}")
    m[1].metric("Share of the table", f"{n / max(total, 1):.1%}")
    if n == 0:
        st.info("No UPRNs match all of those. Loosen a filter.")
        return
    left, right = st.columns(2)
    with left:
        st.markdown("**By deprivation decile**")
        by_decile = _breakdown(table_dir, fj, "imd_decile")
        st.bar_chart(pd.Series({int(k): v for k, v in by_decile.items() if k is not None}).sort_index())
    with right:
        st.markdown("**By flood zone**")
        by_zone = _breakdown(table_dir, fj, "flood_zone")
        st.dataframe(pd.DataFrame({"Flood zone": [int(k) for k in by_zone], "UPRNs": list(by_zone.values())}).sort_values("Flood zone", ascending=False),
                     hide_index=True, width="stretch")

    sample = _sample(table_dir, fj, 2000)
    st.markdown(f"**A sample of {min(len(sample), 2000):,} of the matching UPRNs, spread across the grid squares**")
    st.map(sample[["latitude", "longitude"]].dropna(), size=8)
    st.dataframe(sample.head(500), hide_index=True, width="stretch")

    st.markdown("**Download**")
    if st.button(f"Prepare a CSV of the matching UPRNs (up to {pl.EXPORT_LIMIT:,} rows)", key="prop_prepare"):
        data, cut = pl.export_csv(pl.open_table(table_dir), pl.expression(filters), pl.DISPLAY_COLUMNS)
        st.session_state["prop_download"] = (fj, data, cut)
    ready = st.session_state.get("prop_download")
    if ready and ready[0] == fj:
        st.download_button("Download the CSV", ready[1], file_name="properties.csv", mime="text/csv")
        if ready[2]:
            st.warning(f"There are more than {pl.EXPORT_LIMIT:,} matches, so the file holds only the first {pl.EXPORT_LIMIT:,}. "
                       "Add filters, or read the parquet table directly (see the guide).")
    elif ready:
        st.caption("The filters have changed since the CSV was prepared; prepare it again.")
    st.caption(f"The full table is the parquet folder {paths.DATA / 'properties'} (read it with pandas, DuckDB, R or QGIS).")
