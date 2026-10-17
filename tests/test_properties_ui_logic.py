"""What the Properties tab does that is not drawing (ui/properties_logic.py, ui/flow.py), checked on the synthetic area's table.

Every query is also worked out with pandas on the same rows and the two must agree, so the pyarrow filters cannot drift from what they claim.
"""
import json

import pandas as pd
import pytest
import synthetic_area
from test_properties_e2e import read_table, table  # noqa: F401 - the fixture
from test_synthetic_e2e import synthetic  # noqa: F401 - the fixture it uses

from critical_buildings import paths
from critical_buildings.properties import build
from critical_buildings.ui import flow, logic
from critical_buildings.ui import properties_logic as pl

AREA = synthetic_area.AREA


def reference(frame, f):
    """The same filter worked out with pandas."""
    m = pd.Series(True, index=frame.index)
    if f.get("district"):
        m &= frame.lad_name == f["district"]
    m &= frame.flood_zone >= f.get("flood_min", 1)
    lo, hi = f.get("decile", (1, 10))
    m &= frame.imd_decile.between(lo, hi)
    for flag in ("conservation_area", "article_4_area", "listed_in_outline"):
        if f.get(flag):
            m &= frame[flag]
    if f.get("critical") == "inside":
        m &= frame.critical_site_distance_m == 0
    elif f.get("critical") == "within":
        m &= frame.critical_site_distance_m <= f.get("critical_m", 250)
    if f.get("critical") in ("inside", "within"):
        if f.get("importance"):
            m &= frame.critical_site_importance.isin(f["importance"])
        if f.get("site_types"):
            m &= frame.critical_site_type.isin(f["site_types"])
    return frame[m]


FILTERS = [
    {}, {"district": "Testland"}, {"district": "Nowhere"}, {"flood_min": 2}, {"flood_min": 3}, {"decile": [1, 3]}, {"decile": (9, 10)},
    {"conservation_area": True}, {"article_4_area": True}, {"listed_in_outline": True},
    {"conservation_area": True, "flood_min": 2}, {"critical": "inside"}, {"critical": "within", "critical_m": 300},
    {"critical": "within", "critical_m": 50}, {"critical": "inside", "importance": ["High"]}, {"critical": "inside", "importance": ["Medium"]},
    {"critical": "within", "critical_m": 1000, "site_types": ["Fire Station", "School"]}, {"critical": "inside", "site_types": ["Primary School"]},
    {"district": "Testland", "flood_min": 2, "decile": [1, 5], "critical": "within", "critical_m": 1000, "importance": ["High", "Medium"]},
]


def test_the_latest_table_is_found_and_described(table):  # noqa: F811
    frame, result, _, first = table
    latest = pl.latest_table(AREA)
    assert latest["run_id"] == result["run_id"] and latest["rows"] == 9 and latest["tiles"] == ["TM"] and latest["critical_run"] == first["run_id"]
    assert latest["table"] == result["dest"] / "table" and latest["size_mb"] > 0 and str(latest["built_at"]).startswith("20")
    assert pl.latest_table("atlantis") is None and pl.out_of_date(AREA, latest) == []


def test_it_says_when_the_table_is_out_of_date(table, isolated_paths):  # noqa: F811
    latest = pl.latest_table(AREA)
    (paths.gold_dir(AREA) / "CURRENT.json").write_text(json.dumps({"run_id": "20991231T000000_testland"}), encoding="utf-8")   # a newer critical run
    newer = pl.out_of_date(AREA, latest)
    assert len(newer) == 1 and "newer critical-buildings run (20991231T000000_testland)" in newer[0]
    snap = paths.bronze_source_dir("ea_flood_zones") / "2099-01-01"                                                           # a refreshed source
    snap.mkdir()
    (paths.bronze_source_dir("ea_flood_zones") / "LATEST.json").write_text(json.dumps({"snapshot": "2099-01-01"}), encoding="utf-8")
    reasons = pl.out_of_date(AREA, latest)
    assert any("ea_flood_zones has a newer snapshot (2099-01-01)" in r for r in reasons) and len(reasons) == 2


def test_coverage_rows_are_the_summary_with_shares(table):  # noqa: F811
    rows = {r["Link"]: r for r in pl.coverage_rows(pl.latest_table(AREA)["summary"])}
    assert rows["In flood zone 3 (high)"]["UPRNs"] == 1 and rows["In a conservation area"]["UPRNs"] == 2
    assert rows["Inside a critical site"]["Share"] == pytest.approx(7 / 9) and rows["Has deprivation scores"]["Share"] == 1.0


@pytest.mark.parametrize("f", FILTERS, ids=lambda f: json.dumps(f, sort_keys=True) or "no filter")
def test_every_filter_agrees_with_pandas(table, f):  # noqa: F811
    frame = read_table(table[1]).copy()
    ds = pl.open_table(table[1]["dest"] / "table")
    expected = reference(frame, f)
    expr = pl.expression(f)
    assert pl.count(ds, expr) == len(expected)
    got = pl.sample(ds, expr, ["uprn"], 100)
    assert sorted(got.uprn) == sorted(expected.uprn)                                          # nine rows: the sample is all of them
    if len(expected):
        zones = pl.breakdown(ds, expr, "flood_zone")
        assert zones == expected.flood_zone.value_counts().to_dict()


def test_a_few_answers_worked_out_by_hand(table):  # noqa: F811
    ds = pl.open_table(table[1]["dest"] / "table")
    assert pl.count(ds, None) == 9 and pl.count(ds, pl.expression({"flood_min": 3})) == 1 and pl.count(ds, pl.expression({"flood_min": 2})) == 4
    assert pl.count(ds, pl.expression({"conservation_area": True})) == 2 and pl.count(ds, pl.expression({"listed_in_outline": True})) == 3
    assert pl.count(ds, pl.expression({"critical": "inside"})) == 7
    assert pl.districts(ds) == ["Testland"] and "Fire Station" in pl.critical_site_types(ds)
    assert pl.expression({}) is None and pl.expression({"decile": (1, 10), "flood_min": 1}) is None


def test_the_sample_is_capped_and_the_export_says_when_it_was_cut_short(table):  # noqa: F811
    ds = pl.open_table(table[1]["dest"] / "table")
    assert len(pl.sample(ds, None, pl.DISPLAY_COLUMNS, 4)) == 4 and list(pl.sample(ds, None, pl.DISPLAY_COLUMNS, 4).columns) == pl.DISPLAY_COLUMNS
    assert pl.sample(ds, pl.expression({"district": "Nowhere"}), ["uprn"], 5).empty
    data, cut = pl.export_csv(ds, None, ["uprn", "flood_zone"])
    assert not cut and data.decode().splitlines()[0] == "uprn,flood_zone" and len(data.decode().splitlines()) == 10
    data, cut = pl.export_csv(ds, None, ["uprn"], limit=3)
    assert cut and len(data.decode().splitlines()) == 4                                       # the header and three rows


def test_the_property_flow_diagram_shows_each_source_the_critical_run_and_the_table(table):  # noqa: F811
    rows = logic.source_rows(AREA, only=build.ATTRIBUTION_SOURCES)
    assert {r["id"] for r in rows} >= {"os_open_uprn", "ons_lsoa", "iod2025", "planning_data", "ea_flood_zones"} and all(r["age"] is not None for r in rows)
    dot = flow.properties_dot(rows, pl.latest_table(AREA), "RUN-1")
    assert all(f"src_{r['id']} -> link;" in dot for r in rows) and "critical -> link;" in dot and "link -> table;" in dot
    assert "Property table\\n9 UPRNs" in dot and "RUN-1" in dot
    empty = flow.properties_dot(rows, None, None)
    assert "not built yet" in empty and "no current run" in empty


def test_the_critical_buildings_diagram_shows_only_the_sources_that_pipeline_reads(table):  # noqa: F811
    ids = {r["id"] for r in logic.source_rows(AREA)}
    assert ids <= set(paths.PIPELINE_SOURCES) and not ids & {"ons_lsoa", "iod2025", "planning_data", "ea_flood_zones"}
    assert "osm_extract" not in ids                                                           # testland reads OpenStreetMap from Overpass
    assert "osm_extract" in {r["id"] for r in logic.source_rows("england")}                   # england uses an extract
    assert {r["id"] for r in logic.source_rows(AREA, only=None)} >= {"ons_lsoa", "ea_flood_zones", "os_open_uprn"}
