"""The UPRN property table app.   python -m critical_buildings properties-ui

A separate app from the critical-buildings one, so it is clear which pipeline you are looking at: this one fetches the property sources,
builds the table, and explores it. It has no rules to edit and nothing to publish.
"""
import streamlit as st

from critical_buildings import config, paths
from critical_buildings.ui import properties_tab

TITLE = "UPRN Property Table"
st.set_page_config(page_title=TITLE, page_icon=":house:", layout="wide")


def tab_help():
    st.subheader("How this works")
    st.markdown("""
The table has **one row for every address point (UPRN)** in the area, linked to open data about it. A UPRN is any addressable location,
so it is not only homes.

1. **Fetch the property sources** (census areas, deprivation, planning layers, flood zones). Later fetches only download what changed.
2. **Build the table.** It uses the area's current critical-buildings run for the nearest critical site, so build that first (in the
   critical-buildings app, `python -m critical_buildings ui`).
3. **Explore** it by district, flood zone, deprivation, planning status and nearest critical site, and download matching UPRNs as a CSV.

The table is parquet, so it can also be read directly with pandas, DuckDB, R or QGIS. It contains open data only and nothing about people.
Every column is described under *What each column means*, and the credits to include when you share it are under *Credits*.
""")


def main():
    st.sidebar.title(TITLE)
    areas = config.areas()
    area = st.sidebar.selectbox("Area", areas, index=areas.index(paths.DEFAULT_AREA), key="area")
    st.sidebar.caption("This is the **property table** app. The critical-buildings pipeline has its own: `python -m critical_buildings ui`.")
    st.sidebar.caption(f"Tables: {paths.DATA / 'properties'}")
    st.title(TITLE)
    table, help_ = st.tabs(["Property table", "Help"])
    with table:
        properties_tab.tab_properties(None, area)
    with help_:
        tab_help()


main()
