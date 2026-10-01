"""Publish stage: write the delivered files for one run into a folder."""
import logging

import geopandas as gpd

from critical_buildings.publish import dictionary as docs
from critical_buildings.publish import qa_report as qa

log = logging.getLogger(__name__)

# Files every run folder contains (used by release/archive and the regression test)
DELIVERABLES = ["critical_buildings.gpkg", "critical_buildings.csv", "critical_sites.gpkg", "critical_sites.csv",
                "critical_buildings_summary.csv", "critical_buildings_data_dictionary.xlsx", "ATTRIBUTION.txt",
                "run_summary.xlsx", "qa_map.html"]


def write_outputs(gold, silver, dest, rule_version, today):
    df, sites_out = gold.df, gold.sites_out
    gdf = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df["Geom"], crs=27700), crs=27700)
    final = gdf[~gdf.Excluded].drop(columns=["Geom"])   # the layer's own geometry replaces the WKT text column (CSV keeps it)
    final.to_file(dest / "critical_buildings.gpkg", layer="critical_buildings", driver="GPKG")
    df[~df.Excluded].to_csv(dest / "critical_buildings.csv", index=False)
    sites_out.to_file(dest / "critical_sites.gpkg", layer="critical_sites", driver="GPKG")
    sites_out.drop(columns="geometry").to_csv(dest / "critical_sites.csv", index=False)
    qa.write_reports(df, gold.changes, rule_version, today, gold.new_ids, silver.os_total, dest, sites_out,
                     silver.ref, gold.ref_gaps, gold.sites_df)
    qa.write_map(final, dest / "qa_map.html")
    docs.write_docs(df[~df.Excluded], sites_out, rule_version, today, gold.changes, dest)
    log.info("wrote %s buildings in %s sites to %s", len(final), len(sites_out), dest)
    return len(final), len(sites_out)
