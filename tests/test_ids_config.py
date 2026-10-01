"""Stable identities across runs, and configuration handling."""
import geopandas as gpd
import pandas as pd
import pytest

from critical_buildings import config
from critical_buildings.gold import ids
from helpers import square


def recs_of(*rows):
    cols = ["source", "source_feature_id", "geometry"]
    return gpd.GeoDataFrame([dict(zip(cols, r)) for r in rows], columns=cols, geometry="geometry", crs=27700)


A = ("Both", "os-a / way/1", square(0, 0))
B = ("OSM", "way/2", square(100, 0))
C = ("OS Important Buildings", "os-c", square(200, 0))


# ------------------------------------------------------------------ building IDs
def test_building_ids_are_stable_across_runs(store):
    first, new1 = ids.assign_ids(recs_of(A, B, C), "2026-10-01", store, "run1")
    assert list(first["Building ID"]) == ["BLDG-000001", "BLDG-000002", "BLDG-000003"] and new1 == 3
    second, new2 = ids.assign_ids(recs_of(C, A, B), "2026-11-01", store, "run2")          # different order
    assert dict(zip(second.source_feature_id, second["Building ID"])) == dict(zip(first.source_feature_id, first["Building ID"]))
    assert new2 == 0
    assert (second["Created Date"].isin(["2026-10-01"])).all()                              # created date is kept


def test_an_osm_feature_redrawn_under_a_new_id_keeps_its_building_id(store):
    first, _ = ids.assign_ids(recs_of(A, B), "d", store, "run1")
    redrawn = ("OSM", "way/999", square(2, 2, 96))                  # overlaps B's old footprint? no: overlaps nothing
    moved_b = ("OSM", "way/999", square(101, 1))                    # same place as B, new OSM id
    second, new = ids.assign_ids(recs_of(A, moved_b), "d", store, "run2")
    assert second["Building ID"].tolist() == first["Building ID"].tolist() and new == 0
    assert redrawn  # (kept to document the non-overlapping case below)


def test_an_osm_only_building_that_gains_an_os_match_keeps_its_id(store):
    first, _ = ids.assign_ids(recs_of(B), "d", store, "run1")
    second, _ = ids.assign_ids(recs_of(("Both", "os-new / way/2", square(100, 0))), "d", store, "run2")
    assert second["Building ID"].tolist() == first["Building ID"].tolist()


def test_new_buildings_get_the_next_number_and_ids_are_never_reused(store):
    ids.assign_ids(recs_of(A, B, C), "d", store, "run1")
    ids.assign_ids(recs_of(A, B), "d", store, "run2")               # C disappears (retired, not deleted)
    third, new = ids.assign_ids(recs_of(A, B, ("OSM", "way/77", square(900, 900))), "d", store, "run3")
    assert third["Building ID"].tolist() == ["BLDG-000001", "BLDG-000002", "BLDG-000004"] and new == 1
    assert len(store.table_df("building_registry")) == 4


def test_a_retired_building_that_returns_gets_its_old_id_back(store):
    ids.assign_ids(recs_of(A, C), "d", store, "run1")
    ids.assign_ids(recs_of(A), "d", store, "run2")
    back, new = ids.assign_ids(recs_of(A, C), "d", store, "run3")
    assert back["Building ID"].tolist() == ["BLDG-000001", "BLDG-000002"] and new == 0


# ------------------------------------------------------------------ site IDs
def sites_of(*counts):
    return pd.DataFrame({"site_group": range(1, len(counts) + 1), "n_buildings": counts})


def with_ids(store, rows, groups):
    r, _ = ids.assign_ids(recs_of(*rows), "d", store, "r")
    r["site_group"] = groups
    return r


def test_site_ids_follow_their_buildings_even_if_group_numbers_change(store):
    r1 = with_ids(store, [A, B, C], [1, 1, 2])
    out1, s1 = ids.assign_site_ids(r1, sites_of(2, 1), "d", store, "run1")
    sid_ab, sid_c = out1.loc[0, "Site ID"], out1.loc[2, "Site ID"]
    assert {sid_ab, sid_c} == {"SITE-000001", "SITE-000002"}
    r2 = with_ids(store, [A, B, C], [7, 7, 3])                       # same buildings, different group numbers
    out2, _ = ids.assign_site_ids(r2, pd.DataFrame({"site_group": [7, 3], "n_buildings": [2, 1]}), "d", store, "run2")
    assert out2.loc[0, "Site ID"] == sid_ab and out2.loc[2, "Site ID"] == sid_c


def test_a_new_site_gets_a_new_id_and_vanished_sites_are_retired(store):
    r1 = with_ids(store, [A, B], [1, 2])
    ids.assign_site_ids(r1, sites_of(1, 1), "d", store, "run1")
    r2 = with_ids(store, [A], [1])
    out2, _ = ids.assign_site_ids(r2, sites_of(1), "d", store, "run2")
    assert out2.loc[0, "Site ID"] == "SITE-000001"
    reg = store.table_df("site_registry").set_index("site_id")
    assert reg.loc["SITE-000002", "retired_run"] == "run2"
    r3 = with_ids(store, [A, C], [1, 2])
    out3, _ = ids.assign_site_ids(r3, sites_of(1, 1), "d", store, "run3")
    assert out3.loc[1, "Site ID"] == "SITE-000003"                    # never reuses SITE-000002


# ------------------------------------------------------------------ configuration
def test_settings_load_with_the_documented_defaults():
    s = config.get()
    assert s.merge.point_distance_m == 25 and s.sites.cluster_gap_m == 30
    assert s.matching.loose_categories == {"Education"}
    assert frozenset(("Care Home", "Assisted Living Facilities")) in s.reference_match.compatible_pairs
    assert "school" in s.reference_match.stop_words
    assert s.quality.confidence_points == {"High": 20, "Medium": 12, "Low": 5}      # lookup tables stay plain dicts


def test_use_overrides_temporarily_and_restores():
    before = config.get().merge.point_distance_m
    with config.use({"merge": {"point_distance_m": 7}}):
        assert config.get().merge.point_distance_m == 7
    assert config.get().merge.point_distance_m == before


def test_settings_validate_cleanly_and_hash_is_stable():
    assert config.validate() == []
    assert config.settings_hash() == config.settings_hash() and len(config.settings_hash()) == 12


def test_area_files():
    area = config.load_area("suffolk")
    assert area["osm_boundary"]["ref_gss"] == "E10000029" and area["os_grid_squares"] == ["tl", "tm", "tg"]
    with pytest.raises(SystemExit, match="Unknown area"):
        config.load_area("atlantis")
