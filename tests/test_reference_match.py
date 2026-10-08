"""Matching register records to sites, and explaining why a site stayed unverified (synthetic points and footprints)."""
import geopandas as gpd
from helpers import point, square

from critical_buildings.gold import reference_match as rm

CATEGORIES = {"Primary School": "Education", "Hospital": "Care", "Fire Station": "Emergency"}


def refs(*rows):
    cols = ["ref_source", "ref_id", "ref_name", "building_type", "status", "geometry"]
    return gpd.GeoDataFrame([dict(zip(cols, r, strict=False)) for r in rows], columns=cols, geometry="geometry", crs=27700)


def sites_of(*rows):
    cols = ["site_group", "name", "building_type", "geometry"]
    return gpd.GeoDataFrame([dict(zip(cols, r, strict=False)) for r in rows], columns=cols, geometry="geometry", crs=27700)


def test_a_register_record_on_a_similarly_named_site_is_matched():
    s = sites_of((1, "Greenfield Primary School", "Primary School", square(0, 0)),
                 (2, "Elsewhere Fire Station", "Fire Station", square(5000, 0)))
    r = refs(("DfE GIAS", "100", "Greenfield Primary School", "Primary School", "Open", point(10, 10)))
    got = rm.match_reference(r, s, dict(zip(s.site_group, s.name, strict=False)), CATEGORIES)
    assert [(ri, g) for ri, g, _, _ in got] == [(0, 1)]


def test_a_record_of_the_wrong_kind_or_far_away_is_not_matched():
    s = sites_of((1, "Greenfield Primary School", "Primary School", square(0, 0)))
    r = refs(("CQC", "c1", "Greenfield Hospital", "Hospital", "Active", point(10, 10)),
             ("DfE GIAS", "100", "Greenfield Primary School", "Primary School", "Open", point(9000, 0)))
    assert rm.match_reference(r, s, dict(zip(s.site_group, s.name, strict=False)), CATEGORIES) == []


def test_a_closed_school_listed_by_the_register_explains_an_unverified_site():
    site = sites_of((1, "Old Mill Primary School", "Primary School", square(0, 0))).iloc[0]
    ref_all = refs(("DfE GIAS", "555", "Old Mill Primary School", "IGNORE", "Closed", point(20, 20)),
                   ("DfE GIAS", "556", "Old Mill Primary School", "IGNORE", "Open", point(20, 20)))
    verdict, note = rm.explain_unverified(site, ref_all, ref_all.iloc[:0], CATEGORIES)
    assert verdict == "Closed per register" and "URN 555" in note


def test_a_closed_school_beyond_the_conflict_distance_is_ignored():
    site = sites_of((1, "Old Mill Primary School", "Primary School", square(0, 0))).iloc[0]
    ref_all = refs(("DfE GIAS", "555", "Old Mill Primary School", "IGNORE", "Closed", point(5000, 0)))
    assert rm.explain_unverified(site, ref_all, ref_all.iloc[:0], CATEGORIES)[0] == "Unverified"


def test_the_nearest_compatible_record_within_300_m_is_named():
    site = sites_of((1, "Greenfield Primary School", "Primary School", square(0, 0))).iloc[0]
    valid = refs(("DfE GIAS", "1", "Unrelated Name", "Hospital", "Open", point(10, 10)),                  # wrong kind, different name
                 ("DfE GIAS", "2", "Greenfield Primary", "Primary School", "Open", point(200, 0)),        # compatible, 180 m
                 ("DfE GIAS", "3", "Greenfield Primary", "Primary School", "Open", point(100, 0)),        # compatible, closer
                 ("DfE GIAS", "4", "Greenfield Primary", "Primary School", "Open", point(400, 0)))        # too far
    verdict, note = rm.explain_unverified(site, valid, valid, CATEGORIES)
    assert verdict == "Unverified" and "Nearest authoritative record not matched" in note
    assert "Greenfield Primary (DfE GIAS, Primary School, 80 m" in note          # id 3: 80 m from the site edge beats id 2 at 180 m


def test_nothing_within_300_m_gives_the_plain_message():
    site = sites_of((1, "Greenfield Primary School", "Primary School", square(0, 0))).iloc[0]
    valid = refs(("DfE GIAS", "4", "Greenfield Primary", "Primary School", "Open", point(900, 0)))
    assert rm.explain_unverified(site, valid, valid, CATEGORIES) == ("Unverified", "No authoritative record found within 300 m")


def test_prebuilt_candidates_give_the_same_answer_as_building_them_each_time():
    site = sites_of((1, "Greenfield Primary School", "Primary School", square(0, 0))).iloc[0]
    valid = refs(("DfE GIAS", "3", "Greenfield Primary", "Primary School", "Open", point(100, 0)))
    once = rm._Candidates(valid, valid)
    assert rm.explain_unverified(site, valid, valid, CATEGORIES, once) == rm.explain_unverified(site, valid, valid, CATEGORIES)
