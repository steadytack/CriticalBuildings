"""OS building <-> OSM feature matching on synthetic footprints."""
import pytest
from helpers import feature, gdf, point, square

from critical_buildings.gold import merge

FIRE = {"category": "Emergency", "building_type": "Fire Station"}
POLICE = {"category": "Emergency", "building_type": "Police Station"}
PRIMARY = {"category": "Education", "building_type": "Primary School"}
SECONDARY = {"category": "Education", "building_type": "Secondary School"}
STATION = {"category": "Transport", "building_type": "Railway Station"}
PRIO = {1: 50, 2: 50, 17: 50, 18: 50, 20: 90, 23: 50}


def os_row(sid, name, rule_ref, kind, geom, **kw):
    return feature("OS Important Buildings", sid, name, rule_ref, geometry=geom, **kind, **kw)


def osm_row(sid, name, rule_ref, kind, geom, **kw):
    return feature("OSM", sid, name, rule_ref, geometry=geom, **kind, **kw)


def run(os_rows, osm_rows):
    return merge.merge(gdf(osm_rows), gdf(os_rows), PRIO)


def test_overlapping_same_type_features_become_one_high_confidence_building():
    out = run([os_row("os1", "Hartismere", 1, FIRE, square(0, 0))], [osm_row("way/1", "Hartismere", 1, FIRE, square(2, 2))])
    assert len(out) == 1
    r = out.iloc[0]
    assert (r.source, r.method, r.confidence, r.review) == ("Both", "Overlap", "High", "")
    assert r.source_feature_id == "os1 / way/1"
    assert r.geometry.equals(square(0, 0))                    # the OS footprint is kept


def test_osm_point_inside_an_os_polygon_matches():
    out = run([os_row("os1", "", 1, FIRE, square(0, 0))], [osm_row("node/1", "", 1, FIRE, point(10, 10))])
    assert len(out) == 1 and out.iloc[0].method == "Overlap"


def test_a_nearby_feature_of_a_different_type_is_not_the_same_building():
    out = run([os_row("os1", "", 1, FIRE, square(0, 0))], [osm_row("way/2", "", 2, POLICE, square(30, 0))])
    assert sorted(out.source) == ["OS Important Buildings", "OSM"]      # 10 m apart but different types: kept separate


def test_proximity_with_agreeing_names_is_trusted():
    out = run([os_row("os1", "Elmswell Fire Station", 1, FIRE, square(0, 0))],
              [osm_row("way/1", "Elmswell Fire Station", 1, FIRE, square(40, 0))])
    r = out.iloc[0]
    assert (r.source, r.method, r.confidence, r.review) == ("Both", "Proximity (names agree)", "Medium", "")


def test_proximity_with_different_names_is_flagged_for_review():
    out = run([os_row("os1", "Elmswell Fire Station", 1, FIRE, square(0, 0))],
              [osm_row("way/1", "Retained", 1, FIRE, square(40, 0))])
    r = out.iloc[0]
    assert (r.source, r.confidence) == ("Both", "Low")
    assert "Proximity-only match" in r.review


def test_proximity_beyond_the_distance_limit_does_not_match():
    out = run([os_row("os1", "", 1, FIRE, square(0, 0))], [osm_row("way/1", "", 1, FIRE, square(60, 0))])   # 40 m gap
    assert len(out) == 2


def test_an_osm_point_is_matched_to_its_single_nearest_os_polygon():
    out = run([os_row("a", "", 1, FIRE, square(0, 0)), os_row("b", "", 1, FIRE, square(30, 0))],
              [osm_row("node/1", "", 1, FIRE, point(22, 10))])          # 2 m from A, 8 m from B
    both = out[out.source == "Both"]
    assert len(out) == 2 and len(both) == 1 and both.iloc[0].source_feature_id.startswith("a /")


def test_education_types_may_differ_between_sources_and_are_flagged():
    out = run([os_row("os1", "", 17, PRIMARY, square(0, 0, 40))], [osm_row("way/1", "", 18, SECONDARY, square(5, 5, 40))])
    r = out.iloc[0]
    assert r.source == "Both" and "OS rule 17 (Primary School) vs OSM rule 18 (Secondary School)" in r.review


def test_the_lower_priority_number_wins_between_two_rules():
    out = run([os_row("os1", "", 20, {"category": "Education", "building_type": "School"}, square(0, 0, 40))],
              [osm_row("way/1", "", 17, PRIMARY, square(5, 5, 40))])
    assert out.iloc[0].building_type == "Primary School"             # rule 17 (priority 50) beats generic rule 20 (90)


def test_unmatched_features_pass_through_with_their_own_source():
    out = run([os_row("os1", "", 1, FIRE, square(0, 0))], [osm_row("way/9", "", 1, FIRE, square(500, 500))])
    assert sorted(out.source) == ["OS Important Buildings", "OSM"]
    assert set(out.method) == {"OS only", "OSM only"}


def test_osm_area_with_no_buildings_is_an_expected_outcome_not_a_doubt():
    out = run([], [osm_row("way/7", "Airfield", 1, FIRE, square(0, 0, 500),
                           note="Area feature with no buildings found inside; area kept as the record")])
    r = out.iloc[0]
    assert r.method == "OSM area (no buildings)" and r.review == ""


def test_stations_get_a_wider_proximity_match_without_review():
    out = run([os_row("os1", "Haughley", 23, STATION, point(0, 0))], [osm_row("way/1", "Other name", 23, STATION, point(80, 0))])
    r = out.iloc[0]
    assert r.source == "Both" and r.confidence == "Medium" and r.review == ""


def test_thresholds_come_from_config():
    from critical_buildings import config
    os_rows = [os_row("os1", "", 1, FIRE, square(0, 0))]
    osm_rows = [osm_row("way/1", "", 1, FIRE, square(40, 0))]            # 20 m gap
    assert len(run(os_rows, osm_rows)) == 1
    with config.use({"merge": {"point_distance_m": 10}}):
        assert len(run(os_rows, osm_rows)) == 2


@pytest.mark.parametrize("n_osm", [0, 1])
def test_empty_inputs_do_not_crash(n_osm):
    osm = [osm_row("way/1", "", 1, FIRE, square(0, 0))][:n_osm]
    out = run([], osm)
    assert len(out) == n_osm
