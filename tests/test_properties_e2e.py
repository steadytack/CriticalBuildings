"""The whole UPRN property table on the synthetic area (tests/synthetic_area.py), every link checked on every UPRN.

Geometry reminders (British National Grid metres): the fire station is 600100-600140 x 240100-240130; the Oak school site
600500-600700 x 240500-240650; the Elm Primary footprint 601490-601520 x 241490-241520; the music academy is a point at (601000, 241000)
(a 15 m site around it); a building in no critical site (b2) is 601200-601210 x 241200-241210. The two LSOAs split at easting 601250.
"""
import json

import pandas as pd
import pyarrow.dataset as ds
import pytest
import synthetic_area
from test_synthetic_e2e import run, synthetic  # noqa: F401 - the fixture

from critical_buildings import paths
from critical_buildings.errors import PipelineError
from critical_buildings.properties import build, columns


def read_table(result):
    return ds.dataset(result["dest"] / "table", format="parquet", partitioning="hive").to_table().to_pandas()


@pytest.fixture
def table(synthetic, isolated_paths):  # noqa: F811
    """The critical-buildings run, then the property table; returns (frame indexed by UPRN, result, sites.csv of the run)."""
    first = run(synthetic, reuse_silver=False)
    synthetic_area.build_property_bronze(isolated_paths / "work_properties")
    result = build.build(synthetic_area.AREA)
    frame = read_table(result).set_index("uprn").sort_index()
    sites = pd.read_csv(first["dest"] / "sites.csv", dtype=str).fillna("").set_index("Site Name")
    return frame, result, sites, first


def test_every_uprn_in_the_area_is_there_once_and_the_columns_are_the_documented_ones(table):
    frame, result, _, _ = table
    inside = [u for u, _, _ in synthetic_area.UPRNS + synthetic_area.EXTRA_UPRNS if u != 100000000099]       # 99 is outside the area
    assert sorted(frame.index) == sorted(inside) and result["rows"] == 9 and result["tiles"] == ["TM"]
    assert [c for c in frame.columns if c != "tile"] == [c for c in columns.NAMES if c != "uprn"]


def test_location_building_and_postcode_links(table):
    frame, _, _, _ = table
    elm = frame.loc[100000000021]
    assert (elm.building_id, elm.building_area_m2, elm.uprns_in_building) == ("b1", 900.0, 1)                        # inside the Elm footprint
    shared = frame.loc[[100000000041, 100000000042]]
    assert shared.building_id.tolist() == ["b2", "b2"] and shared.uprns_in_building.tolist() == [2, 2] and shared.building_area_m2.tolist() == [100, 100]
    fire = frame.loc[100000000001]
    assert pd.isna(fire.building_id) and fire.uprns_in_building == 0                                                   # in no footprint
    assert (frame.loc[100000000011].postcode, frame.loc[100000000011].postcode_distance_m) == ("TL1 1AA", 0)
    assert frame.loc[100000000001].postcode == "TL1 9ZZ" and round(frame.loc[100000000001].postcode_distance_m) == 11
    assert frame.loc[100000000031].postcode == "TL1 8YY" and round(frame.loc[100000000031].postcode_distance_m) == 95
    assert (frame.loc[[100000000041, 100000000042]].postcode == "TL1 8YY").all()                                      # 226 m and 233 m: inside 250
    assert frame.latitude.between(51, 53).all() and frame.longitude.between(0, 2).all()


def test_census_area_and_deprivation_links(table):
    frame, _, _, _ = table
    west, east = frame.loc[100000000001], frame.loc[100000000021]
    assert (west.lsoa_code, west.lsoa_name, west.lsoa_method) == (synthetic_area.LSOA_WEST, "Testland 001A", "inside")
    assert (east.lsoa_code, east.imd_decile, east.imd_rank, east.lad_name) == (synthetic_area.LSOA_EAST, 10, 31000, "Testland")
    assert (west.imd_score, west.imd_decile, west.lsoa_population, west.lsoa_aged_60_plus) == (31.5, 2, 1800, 400)
    assert frame.lsoa_code.notna().all() and (frame.lsoa_method == "inside").all()


def test_planning_links_use_only_current_entries(table):
    frame, _, _, _ = table
    oak = frame.loc[[100000000011, 100000000012]]
    assert oak.conservation_area.all() and (oak.conservation_area_name == "Oak Conservation Area").all()
    assert not frame.loc[100000000031].conservation_area                      # the area that ended in 2001 does not count
    assert frame.loc[100000000021].article_4_area and not frame.loc[100000000011].article_4_area
    fire = frame.loc[[100000000001, 100000000002, 100000000003]]
    assert fire.listed_in_outline.all()                                                                                  # all inside the outline
    near_point = fire.loc[[100000000002, 100000000003]]                                                                  # 8.5 m and 2.8 m from the listing point
    assert (near_point.listed_grade == "II*").all() and near_point.listed_distance_m.between(2, 9).all()
    assert pd.isna(fire.loc[100000000001].listed_grade)                                                                  # 22 m away: beyond the 10 m limit
    assert not frame.loc[100000000011].listed_in_outline and pd.isna(frame.loc[100000000011].listed_grade)


def test_flood_zones_take_the_highest_level_and_say_how_it_was_modelled(table):
    frame, _, _, _ = table
    assert (frame.loc[100000000021].flood_zone, frame.loc[100000000021].flood_zone_type) == (3, "Fluvial Models")     # in zone 3 and zone 2
    assert (frame.loc[100000000001].flood_zone, frame.loc[100000000001].flood_zone_type) == (2, "Tidal Models")
    assert (frame.loc[[100000000011, 100000000031]].flood_zone == 1).all() and pd.isna(frame.loc[100000000011].flood_zone_type)


def test_critical_site_links_are_the_site_it_is_in_or_the_nearest_within_a_kilometre(table):
    frame, _, sites, _ = table
    fire_site = sites.loc["Testland Fire Station", "Site ID"]
    fire = frame.loc[100000000001]
    assert (fire.critical_site_id, fire.critical_site_type, fire.critical_site_importance, fire.critical_site_distance_m) == (
        fire_site, "Fire Station", "High", 0)
    assert frame.loc[100000000011].critical_site_id == sites.loc["Oak Primary School", "Site ID"]
    shared = frame.loc[100000000041]                                                                                  # in no site: the music academy is nearest
    assert shared.critical_site_id == sites.loc["Testland Music Academy", "Site ID"] and 200 < shared.critical_site_distance_m < 300


def test_the_output_is_repeatable_documented_and_recorded(table, isolated_paths):
    frame, result, _, first = table
    again = build.build(synthetic_area.AREA)
    assert again["run_id"] != result["run_id"]
    other = read_table(again).set_index("uprn").sort_index()
    pd.testing.assert_frame_equal(frame, other)                                                                      # same inputs, same table
    assert build.current_run_id(synthetic_area.AREA) == again["run_id"]
    lineage = json.loads((result["dest"] / "lineage.json").read_text(encoding="utf-8"))
    assert lineage["critical_buildings_run"] == first["run_id"] and lineage["rows"] == 9 and lineage["coverage"]["flood_zone_3"] == 1
    assert {s["source"] for s in lineage["bronze"]["snapshots"]} >= {"os_open_uprn", "ons_lsoa", "iod2025", "planning_data", "ea_flood_zones"}
    credits = (result["dest"] / "ATTRIBUTION.txt").read_text(encoding="utf-8")
    for source in ("OS Open UPRN", "Lower layer Super Output Areas", "English Indices of Deprivation", "planning.data.gov.uk", "Flood Map for Planning"):
        assert source in credits and "Open Government Licence" in credits
    assert "critical_site_* columns come from the critical-buildings dataset" in credits and "OpenStreetMap" in credits
    described = pd.read_csv(result["dest"] / "columns.csv")
    assert described["column"].tolist() == columns.NAMES and described["description"].notna().all()


def test_without_a_critical_buildings_run_the_site_columns_are_empty_not_an_error(synthetic, isolated_paths):  # noqa: F811
    synthetic_area.build_property_bronze(isolated_paths / "work_properties")
    result = build.build(synthetic_area.AREA)
    frame = read_table(result)
    assert len(frame) == 9 and frame.critical_site_id.isna().all() and frame.critical_site_distance_m.isna().all()
    assert frame.flood_zone.notna().all()                                                                            # everything else still linked


def test_a_missing_source_is_named_before_anything_is_built(synthetic, isolated_paths):  # noqa: F811
    problems = build.check(synthetic_area.AREA)                                                                      # the property sources are not fetched
    assert any("'ons_lsoa'" in p and "fetch --only properties" in p for p in problems) and len(problems) == 4
    with pytest.raises(PipelineError, match="Cannot build the property table"):
        build.build(synthetic_area.AREA)
    assert not (paths.DATA / "properties").exists()                                                                  # nothing was started
