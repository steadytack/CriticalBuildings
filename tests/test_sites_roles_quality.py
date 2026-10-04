"""Site grouping, building roles and the data quality score."""
import geopandas as gpd
import pandas as pd
import pytest
from helpers import point, square
from shapely.geometry import box

from critical_buildings.gold import roles, sites, steps

EDU = {"category": "Education", "building_type": "Primary School", "importance": "Medium"}
FIRE = {"category": "Emergency", "building_type": "Fire Station", "importance": "High"}


def recs(*rows):
    cols = ["name", "category", "building_type", "importance", "geometry"]
    return gpd.GeoDataFrame([dict(zip(cols, r)) for r in rows], geometry="geometry", crs=27700)


def candidates(*rows):
    cols = ["name", "category", "origin", "key", "geometry"]
    return gpd.GeoDataFrame([dict(zip(cols, r)) for r in rows], columns=cols, geometry="geometry", crs=27700)


# ------------------------------------------------------------------ sites
def test_buildings_inside_a_functional_site_form_one_named_site():
    r = recs(("Hall", *EDU.values(), square(10, 10)), ("Block A", *EDU.values(), square(40, 10)),
             ("Gym", *EDU.values(), square(70, 10)))
    cands = candidates(("Greenfield School", "Education", "OS Functional Site", "fs1", box(0, 0, 100, 60)))
    out, s = sites.assign_sites(r, cands)
    assert len(s) == 1 and s.iloc[0].n_buildings == 3
    assert s.iloc[0]["name"] == "Greenfield School" and s.iloc[0].method == "OS Functional Site"
    assert set(out.site_method) == {"OS Functional Site"}


def test_a_building_of_another_category_is_not_pulled_into_the_site():
    r = recs(("Hall", *EDU.values(), square(10, 10)), ("Station", *FIRE.values(), square(40, 10)))
    cands = candidates(("Greenfield School", "Education", "OS Functional Site", "fs1", box(0, 0, 100, 60)))
    _, s = sites.assign_sites(r, cands)
    assert len(s) == 2


def test_a_same_named_stray_just_outside_the_outline_joins_the_site():
    r = recs(("Hall", *EDU.values(), square(10, 10)), ("Greenfield School", *EDU.values(), square(110, 10)),
             ("Somewhere Else", *EDU.values(), square(112, 40)))
    cands = candidates(("Greenfield School", "Education", "OS Functional Site", "fs1", box(0, 0, 100, 60)))
    out, _ = sites.assign_sites(r, cands)
    assert out.loc[1, "site_group"] == out.loc[0, "site_group"]           # same name, 10 m outside: attached
    assert out.loc[2, "site_group"] != out.loc[0, "site_group"]           # different name: its own site


def test_nearby_buildings_without_a_site_polygon_form_a_proximity_cluster():
    r = recs(("", *FIRE.values(), square(0, 0)), ("", *FIRE.values(), square(40, 0)), ("", *FIRE.values(), square(900, 900)))
    _, s = sites.assign_sites(r, candidates())
    methods = dict(zip(s.method, s.n_buildings))
    assert methods == {"Proximity cluster": 2, "Single building": 1}


def test_overlapping_os_and_osm_site_polygons_are_one_site_keeping_the_os_name():
    r = recs(("A", *EDU.values(), square(20, 20)), ("B", *EDU.values(), square(50, 20)))
    cands = candidates(("OS School Name", "Education", "OS Functional Site", "fs1", box(0, 0, 100, 100)),
                       ("", "Education", "OSM site", "way/1", box(5, 5, 98, 98)))
    _, s = sites.assign_sites(r, cands)
    assert len(s) == 1 and s.iloc[0]["name"] == "OS School Name"
    assert s.iloc[0].method == "OS Functional Site + OSM site"


def test_a_site_holding_one_building_is_called_a_single_building():
    r = recs(("Hall", *EDU.values(), square(10, 10)))
    _, s = sites.assign_sites(r, candidates(("S", "Education", "OS Functional Site", "fs1", box(0, 0, 100, 60))))
    assert s.iloc[0].method == "Single building"


def test_site_importance_is_the_highest_of_its_buildings():
    r = recs(("A", "Education", "Primary School", "Medium", square(10, 10)),
             ("B", "Education", "Primary School", "High", square(40, 10)))
    _, s = sites.assign_sites(r, candidates(("S", "Education", "OS Functional Site", "fs1", box(0, 0, 100, 60))))
    assert s.iloc[0].importance == "High"


# ------------------------------------------------------------------ roles
SETTINGS = {"min_area": 150.0, "ancillary": {"garage"}, "primary": {"school"}, "per_type": {"Fire Station": 50.0}}


def role_recs(*rows):
    cols = ["site_group", "building_type", "osm_building", "method", "geometry"]
    return gpd.GeoDataFrame([dict(zip(cols, r)) for r in rows], geometry="geometry", crs=27700)


def roles_of(*rows):
    return list(roles.assign_roles(role_recs(*rows), SETTINGS).role)


def test_the_only_building_at_a_site_is_standalone():
    assert roles_of((1, "Primary School", "", "Overlap", square(0, 0, 5))) == ["Standalone"]


def test_size_decides_primary_or_ancillary():
    assert roles_of((1, "Primary School", "", "Overlap", square(0, 0, 25)),    # 625 m2
                    (1, "Primary School", "", "Overlap", square(50, 0, 10))   # 100 m2
                    ) == ["Primary", "Ancillary"]


def test_osm_building_tags_override_size():
    assert roles_of((1, "Primary School", "", "Overlap", square(0, 0, 25)),
                    (1, "Primary School", "garage", "Overlap", square(50, 0, 25)),      # big but a garage
                    (1, "Primary School", "school", "Overlap", square(100, 0, 5))        # tiny but tagged school
                    ) == ["Primary", "Ancillary", "Primary"]


def test_per_type_minimum_footprint_applies():
    # 60 m2 is under the default 150 but over the Fire Station minimum of 50
    assert roles_of((1, "Fire Station", "", "Overlap", square(0, 0, 25)),
                    (1, "Fire Station", "", "Overlap", box(50, 0, 56, 10))) == ["Primary", "Primary"]


def test_a_point_at_a_multi_building_site_is_unknown_and_an_area_record_is_the_whole_site():
    got = roles_of((1, "Primary School", "", "Overlap", square(0, 0, 25)),
                   (1, "Primary School", "", "Overlap", point(5, 5)),
                   (2, "Airport Buildings", "", "OSM area (no buildings)", square(0, 0, 500)))
    assert got == ["Primary", "Unknown", "Whole site"]


def test_every_multi_building_site_gets_at_least_one_primary():
    out = roles.assign_roles(role_recs((1, "Primary School", "", "Overlap", square(0, 0, 8)),
                                       (1, "Primary School", "", "Overlap", square(50, 0, 10))), SETTINGS)
    assert list(out.role) == ["Ancillary", "Primary"]
    assert "Largest building" in out.role_reason.iloc[1]


def test_role_reasons_are_plain_english():
    out = roles.assign_roles(role_recs((1, "Primary School", "", "Overlap", square(0, 0, 25)),
                                       (1, "Primary School", "", "Overlap", square(50, 0, 10))), SETTINGS)
    assert out.role_reason.iloc[0] == "Footprint 625 m2 is at least 150 m2"
    assert out.role_reason.iloc[1] == "Footprint 100 m2 is under 150 m2"


# ------------------------------------------------------------------ quality score
def score_frame(rows):
    return pd.DataFrame(rows, columns=["Verification", "Source", "Confidence", "Geom", "Review Status"])


POLY = "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"


def test_quality_score_components_add_up():
    df = score_frame([
        ("Verified", "Both", "High", POLY, ""),                        # 40 + 25 + 20 + 15
        ("Unverified", "OSM", "Medium", "POINT (0 0)", "Open"),        # 10 + 12 + 12 + 5 - 10
        ("Authoritative only", "CQC", "High", POLY, ""),               # 40 + 20 (register) + 20 + 15
        ("Closed per register", "OS Important Buildings", "Low", "POINT (1 1)", ""),   # 0 + 12 + 5 + 5
    ])
    assert list(steps.quality_score(df)) == [100, 29, 95, 22]


@pytest.mark.parametrize("score,band", [(100, "High"), (75, "High"), (74, "Medium"), (50, "Medium"), (49, "Low"), (0, "Low")])
def test_quality_bands(score, band):
    assert steps.quality_band(pd.Series([score])).iloc[0] == band


def test_quality_label_describes_the_bands():
    assert steps.bands_label() == "High 75-100, Medium 50-74, Low 0-49"
