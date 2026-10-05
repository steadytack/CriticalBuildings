"""Stable identities across runs, and configuration handling."""
import geopandas as gpd
import pandas as pd
import pytest
from helpers import square

from critical_buildings import config
from critical_buildings.gold import steps


def recs_of(*rows):
    cols = ["source", "source_feature_id", "geometry"]
    return gpd.GeoDataFrame([dict(zip(cols, r)) for r in rows], columns=cols, geometry="geometry", crs=27700)


A = ("Both", "os-a / way/1", square(0, 0))
B = ("OSM", "way/2", square(100, 0))
C = ("OS Important Buildings", "os-c", square(200, 0))


# ------------------------------------------------------------------ building IDs
def test_building_ids_are_stable_across_runs(store):
    first, new1 = steps.assign_ids(recs_of(A, B, C), "2026-10-01", store, "run1")
    assert list(first["Building ID"]) == ["BLDG-000001", "BLDG-000002", "BLDG-000003"] and new1 == 3
    second, new2 = steps.assign_ids(recs_of(C, A, B), "2026-11-01", store, "run2")          # different order
    assert dict(zip(second.source_feature_id, second["Building ID"])) == dict(zip(first.source_feature_id, first["Building ID"]))
    assert new2 == 0
    assert (second["Created Date"].isin(["2026-10-01"])).all()                              # created date is kept


def test_an_osm_feature_redrawn_under_a_new_id_keeps_its_building_id(store):
    first, _ = steps.assign_ids(recs_of(A, B), "d", store, "run1")
    redrawn = ("OSM", "way/999", square(2, 2, 96))                  # overlaps B's old footprint? no: overlaps nothing
    moved_b = ("OSM", "way/999", square(101, 1))                    # same place as B, new OSM id
    second, new = steps.assign_ids(recs_of(A, moved_b), "d", store, "run2")
    assert second["Building ID"].tolist() == first["Building ID"].tolist() and new == 0
    assert redrawn  # (kept to document the non-overlapping case below)


def test_an_osm_only_building_that_gains_an_os_match_keeps_its_id(store):
    first, _ = steps.assign_ids(recs_of(B), "d", store, "run1")
    second, _ = steps.assign_ids(recs_of(("Both", "os-new / way/2", square(100, 0))), "d", store, "run2")
    assert second["Building ID"].tolist() == first["Building ID"].tolist()


def test_new_buildings_get_the_next_number_and_ids_are_never_reused(store):
    steps.assign_ids(recs_of(A, B, C), "d", store, "run1")
    steps.assign_ids(recs_of(A, B), "d", store, "run2")               # C disappears (retired, not deleted)
    third, new = steps.assign_ids(recs_of(A, B, ("OSM", "way/77", square(900, 900))), "d", store, "run3")
    assert third["Building ID"].tolist() == ["BLDG-000001", "BLDG-000002", "BLDG-000004"] and new == 1
    assert len(store.table_df("building_registry")) == 4


def test_a_retired_building_that_returns_gets_its_old_id_back(store):
    steps.assign_ids(recs_of(A, C), "d", store, "run1")
    steps.assign_ids(recs_of(A), "d", store, "run2")
    back, new = steps.assign_ids(recs_of(A, C), "d", store, "run3")
    assert back["Building ID"].tolist() == ["BLDG-000001", "BLDG-000002"] and new == 0


def test_footprint_fallback_needs_at_least_half_the_smaller_footprint_to_overlap(store):
    steps.assign_ids(recs_of(("OSM", "way/1", square(0, 0))), "d", store, "run1")
    mostly = ("OSM", "way/2", square(8, 0))                       # 60% of the old footprint overlaps; the registry now holds 8..28
    little = ("OSM", "way/4", square(23, 0))                      # only 25% of it overlaps
    far = ("OSM", "way/3", square(5000, 0))
    got, new = steps.assign_ids(recs_of(mostly), "d", store, "run2")
    assert got["Building ID"].tolist() == ["BLDG-000001"] and new == 0
    got, new = steps.assign_ids(recs_of(little), "d", store, "run3")
    assert got["Building ID"].tolist() == ["BLDG-000002"] and new == 1
    got, _ = steps.assign_ids(recs_of(far), "d", store, "run4")
    assert got["Building ID"].tolist() == ["BLDG-000003"]


def test_two_redrawn_features_over_one_old_building_do_not_share_its_id(store):
    steps.assign_ids(recs_of(("OSM", "way/1", square(0, 0))), "d", store, "run1")
    first = ("OSM", "way/8", square(1, 0))
    second = ("OSM", "way/9", square(2, 0))
    got, new = steps.assign_ids(recs_of(first, second), "d", store, "run2")
    assert got["Building ID"].tolist() == ["BLDG-000001", "BLDG-000002"] and new == 1       # first in order wins


def test_a_footprint_match_never_steals_an_id_already_matched_by_key(store):
    steps.assign_ids(recs_of(("OSM", "way/1", square(0, 0)), ("OSM", "way/2", square(100, 0))), "d", store, "run1")
    keeps_key = ("OSM", "way/1", square(100, 0))                  # key says building 1, but sits where building 2 was
    redrawn = ("OSM", "way/55", square(101, 0))                   # footprint says building 2
    got, new = steps.assign_ids(recs_of(keeps_key, redrawn), "d", store, "run2")
    assert got["Building ID"].tolist() == ["BLDG-000001", "BLDG-000002"] and new == 0


def test_footprint_fallback_scales_to_a_large_registry(store):
    n = 3000
    old = recs_of(*[("OSM", f"way/{i}", square((i % 60) * 50, (i // 60) * 50)) for i in range(n)])
    first, _ = steps.assign_ids(old, "d", store, "run1")
    redrawn = recs_of(*[("OSM", f"way/new{i}", square((i % 60) * 50 + 1, (i // 60) * 50 + 1)) for i in range(n)])
    second, new = steps.assign_ids(redrawn, "d", store, "run2")           # every OSM id changed: all matched by footprint
    assert second["Building ID"].tolist() == first["Building ID"].tolist() and new == 0


# ------------------------------------------------------------------ site IDs
def sites_of(*counts):
    return pd.DataFrame({"site_group": range(1, len(counts) + 1), "n_buildings": counts})


def with_ids(store, rows, groups):
    r, _ = steps.assign_ids(recs_of(*rows), "d", store, "r")
    r["site_group"] = groups
    return r


def test_site_ids_follow_their_buildings_even_if_group_numbers_change(store):
    r1 = with_ids(store, [A, B, C], [1, 1, 2])
    out1, _ = steps.assign_site_ids(r1, sites_of(2, 1), "d", store, "run1")
    sid_ab, sid_c = out1.loc[0, "Site ID"], out1.loc[2, "Site ID"]
    assert {sid_ab, sid_c} == {"SITE-000001", "SITE-000002"}
    r2 = with_ids(store, [A, B, C], [7, 7, 3])                       # same buildings, different group numbers
    out2, _ = steps.assign_site_ids(r2, pd.DataFrame({"site_group": [7, 3], "n_buildings": [2, 1]}), "d", store, "run2")
    assert out2.loc[0, "Site ID"] == sid_ab and out2.loc[2, "Site ID"] == sid_c


def test_a_new_site_gets_a_new_id_and_vanished_sites_are_retired(store):
    r1 = with_ids(store, [A, B], [1, 2])
    steps.assign_site_ids(r1, sites_of(1, 1), "d", store, "run1")
    r2 = with_ids(store, [A], [1])
    out2, _ = steps.assign_site_ids(r2, sites_of(1), "d", store, "run2")
    assert out2.loc[0, "Site ID"] == "SITE-000001"
    reg = store.table_df("site_registry").set_index("site_id")
    assert reg.loc["SITE-000002", "retired_run"] == "run2"
    r3 = with_ids(store, [A, C], [1, 2])
    out3, _ = steps.assign_site_ids(r3, sites_of(1, 1), "d", store, "run3")
    assert out3.loc[1, "Site ID"] == "SITE-000003"                    # never reuses SITE-000002


# ------------------------------------------------------------------ registry writes are deferred until a run succeeds
def test_pending_identities_do_not_touch_the_registry_until_committed(store):
    steps.assign_ids(recs_of(A, B), "d", store, "run1")                    # an earlier, successful run
    pending = {}
    r2, new = steps.assign_ids(recs_of(A, C), "d", store, "run2", pending)  # B vanishes, C is new
    r2["site_group"] = [1, 2]
    steps.assign_site_ids(r2, sites_of(1, 1), "d", store, "run2", pending)
    reg = store.table_df("building_registry")
    assert list(reg.building_id) == ["BLDG-000001", "BLDG-000002"] and reg.retired_run.isna().all()
    assert store.table_df("site_registry").empty and store.building_site_map() == {}

    store.commit_identities(pending, "run2")                               # the run succeeded
    reg = store.table_df("building_registry").set_index("building_id")
    assert list(reg.index) == ["BLDG-000001", "BLDG-000002", "BLDG-000003"] and new == 1
    assert reg.loc["BLDG-000002", "retired_run"] == "run2"
    assert len(store.table_df("site_registry")) == 2 and len(store.building_site_map()) == 2


def test_a_failed_run_does_not_burn_ids(store):
    steps.assign_ids(recs_of(A), "d", store, "run1")
    steps.assign_ids(recs_of(A, B), "d", store, "failed-run", {})          # never committed
    again, new = steps.assign_ids(recs_of(A, B), "d", store, "run2")
    assert again["Building ID"].tolist() == ["BLDG-000001", "BLDG-000002"] and new == 1


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


# ------------------------------------------------------------------ editing thresholds keeps the file's comments
def test_settings_edit_changes_only_the_named_value_and_keeps_comments(tmp_path, monkeypatch):
    import shutil
    copy = tmp_path / "settings.yml"
    shutil.copy(config.SETTINGS_FILE, copy)
    monkeypatch.setattr(config, "SETTINGS_FILE", copy)
    config._cached.cache_clear()
    before = copy.read_text(encoding="utf-8")
    try:
        done = config.save_settings({("merge", "point_distance_m"): 30, ("quality", "band_high_min"): 80})
        assert [(s, k, o, n) for s, k, o, n in done] == [("merge", "point_distance_m", "25", "30"), ("quality", "band_high_min", "75", "80")]
        after = copy.read_text(encoding="utf-8")
        assert config.get().merge.point_distance_m == 30 and config.get().quality.band_high_min == 80
        assert len(after.splitlines()) == len(before.splitlines())
        assert "# OS and OSM features this close (metres) may be the same building" in after
        changed = [i for i, (a, b) in enumerate(zip(before.splitlines(), after.splitlines())) if a != b]
        assert len(changed) == 2
    finally:
        monkeypatch.undo()
        config._cached.cache_clear()


def test_settings_edit_that_breaks_validation_is_rolled_back(tmp_path, monkeypatch):
    import shutil
    copy = tmp_path / "settings.yml"
    shutil.copy(config.SETTINGS_FILE, copy)
    monkeypatch.setattr(config, "SETTINGS_FILE", copy)
    config._cached.cache_clear()
    before = copy.read_text(encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="band_medium_min"):
            config.save_settings({("quality", "band_medium_min"): 90})        # not lower than band_high_min
        assert copy.read_text(encoding="utf-8") == before
    finally:
        monkeypatch.undo()
        config._cached.cache_clear()


def test_editable_settings_lists_single_numbers_with_their_help_text():
    items = {(i["section"], i["key"]): i for i in config.editable_settings()}
    assert items[("merge", "point_distance_m")]["value"] == 25 and "same building" in items[("merge", "point_distance_m")]["help"]
    assert ("merge", "point_distance_by_type") not in items and ("matching", "loose_categories") not in items


def test_finishing_a_run_is_all_or_nothing(store):
    """If recording the run itself fails, the IDs it computed are not stored either (they used to be committed first)."""
    import sqlite3

    store.insert_run({"run_id": "R1", "area": "x", "run_type": "dev", "status": "running", "started_at": "t"})
    registry = pd.DataFrame({"building_id": ["BLDG-000001"], "os_key": ["os1"], "osm_key": [None], "created": ["2026-01-01"],
                             "wkt": ["POINT (1 2)"]})
    pending = {"buildings": registry, "sites": {"SITE-000001": "2026-01-01"}, "building_sites": {"BLDG-000001": "SITE-000001"}}
    with pytest.raises(sqlite3.IntegrityError):
        store.finish_run("R1", pending, None, status="not-a-status")             # violates the run table's status check
    assert store.building_registry_df().empty and store.site_registry_df().empty
    assert store.get_run("R1")["status"] == "running"
    store.finish_run("R1", pending, None, status="succeeded", buildings=1, sites=1)
    assert list(store.building_registry_df().building_id) == ["BLDG-000001"]
    assert store.get_run("R1")["status"] == "succeeded" and store.building_site_map() == {"BLDG-000001": "SITE-000001"}
