"""The building blocks of the property table (properties/grid.py, layers.py, inputs.py) on small hand-made inputs."""
import json

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest
import shapely
from shapely.geometry import Point, box

from critical_buildings import paths
from critical_buildings.errors import PipelineError
from critical_buildings.properties import grid, inputs, layers


# ---------------------------------------------------------------- the OS grid
def test_grid_letters_match_the_squares_the_data_is_supplied_in():
    assert grid.tile_of([650000, 550000, 650000, 530000, 450000, 430000], [250000, 250000, 350000, 180000, 120000, 560000]).tolist() == [
        "TM", "TL", "TG", "TQ", "SU", "NZ"]
    assert grid.tile_of([-1, 700000, 5], [5, 5, 1_300_000]).tolist() == ["", "", ""]               # off the grid
    assert grid.extent("tm") == (600000, 200000, 700000, 300000)
    assert len(set(grid.TILES.values())) == len(grid.TILES) == 91 and "I" not in "".join(grid.TILES.values())


# ---------------------------------------------------------------- hits and nearest
def test_a_point_in_two_overlapping_shapes_gets_the_lowest_numbered_one_every_time():
    polys = [box(0, 0, 10, 10), box(5, 5, 20, 20), box(100, 100, 110, 110)]
    got = layers.inside([7, 15, 50, 105, 10], [7, 15, 50, 105, 10], polys)
    assert got.tolist() == [0, 1, -1, 2, 0]                                  # (10, 10) is on the edge of both of the first two
    assert layers.inside([1], [1], []).tolist() == [-1]


def test_nearest_respects_the_limit_and_settles_ties_on_the_lowest_index():
    geoms = [Point(0, 100), Point(0, -100), Point(500, 0)]                   # the first two are equally far from the origin
    idx, dist = layers.nearest([0, 0, 0], [0, 450, 2000], geoms, 150)
    assert idx.tolist() == [0, -1, -1] and dist[0] == 100 and np.isnan(dist[1:]).all()
    assert layers.nearest([0], [0], [], 100)[0].tolist() == [-1]


# ---------------------------------------------------------------- arrow helpers
def test_coded_and_lookup_give_null_where_there_is_no_value():
    c = layers.coded(np.array([1, -1, 0, 1]), ["a", "b"])
    assert c.to_pylist() == ["b", None, "a", "b"] and isinstance(c, pa.DictionaryArray)
    assert layers.lookup(np.array([2, -1, 0]), np.array(["x", "y", "z"], dtype=object), pa.string()).to_pylist() == ["z", None, "x"]
    nums = layers.lookup(np.array([0, 1, -1, 0]), np.array([3.0, np.nan]), pa.int64())          # NaN and -1 both become null
    assert nums.to_pylist() == [3, None, None, 3] and nums.type == pa.int64()
    assert layers.lookup(np.array([0, 1]), [], pa.string()).to_pylist() == [None, None]            # no values at all: all null


# ---------------------------------------------------------------- buildings
def test_building_link_counts_the_uprns_that_share_a_footprint():
    foot = np.array([box(0, 0, 10, 10), box(20, 0, 30, 10)], dtype=object)
    b, area, count = layers.building_link(np.array([1, 2, 25, 50]), np.array([1, 2, 5, 50]), foot)
    assert b.tolist() == [0, 0, 1, -1] and area.tolist()[:3] == [100.0, 100.0, 100.0] and np.isnan(area[3])
    assert count.tolist() == [2, 2, 1, 0]
    b, area, count = layers.building_link(np.array([1]), np.array([1]), np.array([], dtype=object))
    assert b.tolist() == [-1] and np.isnan(area[0]) and count.tolist() == [0]


# ---------------------------------------------------------------- postcodes
def test_postcode_index_finds_the_nearest_within_the_limit_and_formats_it():
    table = pd.DataFrame({"EA": [0, 1000], "NO": [0, 0]}, index=["AB11AA", "SW1A1AA"])
    idx = layers.PostcodeIndex(table)
    near, dist = idx.nearest(np.array([30.0, 990.0, 500.0]), np.array([0.0, 0.0, 0.0]), 100)
    assert near.tolist()[0] != -1 and near.tolist()[1] != -1 and near.tolist()[2] == -1 and dist[0] == 30 and dist[1] == 10
    assert idx.display(near).to_pylist() == ["AB1 1AA", "SW1A 1AA", None]


# ---------------------------------------------------------------- flood
def test_flood_zone_is_the_highest_level_that_contains_the_point():
    z3, z2 = np.array([box(0, 0, 10, 10)], dtype=object), np.array([box(-50, -50, 50, 50)], dtype=object)
    zone, in3, in2 = layers.flood_zone(np.array([5, 30, 80]), np.array([5, 30, 80]), z3, z2)
    assert zone.tolist() == [3, 2, 1] and in3.tolist() == [0, -1, -1] and in2.tolist() == [0, 0, -1]


# ---------------------------------------------------------------- reading the sources
def test_the_deprivation_file_is_read_by_the_start_of_its_column_names(isolated_paths):
    snap = paths.bronze_source_dir("iod2025") / "2026-01-01"
    snap.mkdir(parents=True)
    cols = {name: ["1"] for name in ["LSOA code (2021)", "LSOA name (2021)", "Local Authority District code (2024)",
                                     "Local Authority District name (2024)"]}
    for key, prefix in inputs.IMD_PREFIXES.items():
        if not key.startswith(("lsoa_c", "lsoa_n", "lad_")) or key.startswith(("lsoa_pop", "lsoa_chi", "lsoa_aged")):
            cols[prefix + " (where 1 is most deprived)"] = ["7"]
    pd.DataFrame(cols).to_csv(snap / "iod2025_file7.csv", index=False)
    (snap / "_manifest.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    (paths.bronze_source_dir("iod2025") / "LATEST.json").write_text(json.dumps({"snapshot": "2026-01-01"}), encoding="utf-8")
    df = inputs.load_imd()
    assert list(df.columns) == [k for k in inputs.IMD_PREFIXES if k != "lsoa_code"] and df.index.tolist() == ["1"]
    pd.DataFrame({k: v for k, v in cols.items() if not k.startswith("Crime")}).to_csv(snap / "iod2025_file7.csv", index=False)
    with pytest.raises(PipelineError, match="exactly one column starting 'Crime Decile'"):
        inputs.load_imd()


def test_planning_layers_skip_ended_entries_and_those_outside_the_box(tmp_path, isolated_paths):
    snap = paths.bronze_source_dir("planning_data") / "2026-01-01"
    snap.mkdir(parents=True)
    wkt = lambda x0, y0: f"MULTIPOLYGON ((({x0} {y0}, {x0 + 1} {y0}, {x0 + 1} {y0 + 1}, {x0} {y0 + 1}, {x0} {y0})))"      # noqa: E731
    pd.DataFrame({"end-date": ["", "2001-01-01", "", ""], "name": ["live", "ended", "far away", "no outline"],
                  "geometry": [wkt(1, 51), wkt(1, 51), wkt(9, 55), None]}).to_csv(snap / "conservation-area.csv", index=False)
    (snap / "_manifest.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    (paths.bronze_source_dir("planning_data") / "LATEST.json").write_text(json.dumps({"snapshot": "2026-01-01"}), encoding="utf-8")
    frame, polys = inputs.planning_polygons("conservation-area", ["name"], (0, 50, 3, 53), today="2026-10-16")
    assert frame["name"].tolist() == ["live"] and len(polys) == 1 and shapely.contains_xy(polys[0], 1.5, 51.5)


def test_the_flood_zones_are_parsed_once_kept_per_grid_square_and_read_from_the_cache(isolated_paths, monkeypatch):
    snap = paths.bronze_source_dir("ea_flood_zones") / "2026-01-01"
    snap.mkdir(parents=True)
    small = "MULTIPOLYGON (((1.00 52.00, 1.01 52.00, 1.01 52.01, 1.00 52.01, 1.00 52.00)))"             # inside grid square TM
    straddling = "MULTIPOLYGON (((-0.5 52.0, 1.5 52.0, 1.5 52.1, -0.5 52.1, -0.5 52.0)))"               # crosses TL, TM and others
    ended = "MULTIPOLYGON (((1.02 52.02, 1.03 52.02, 1.03 52.03, 1.02 52.03, 1.02 52.02)))"
    pd.DataFrame({"end-date": ["", "", "2001-01-01"], "flood-risk-level": [3, 2, 3],
                  "flood-risk-type": ["Fluvial Models", "Tidal Models", "Old Models"], "geometry": [small, straddling, ended]}
                 ).to_csv(snap / "flood-risk-zone.csv", index=False)
    from critical_buildings import sources
    sources.write_manifest("ea_flood_zones", snap, [{"name": "flood-risk-zone.csv"}])
    sources.set_latest("ea_flood_zones", "2026-01-01")
    layer = inputs.flood_layer("x", (-1, 50, 3, 53), today="2026-10-16")
    assert layer.counts == {"2": 1, "3": 1}                                                          # the ended entry is not counted
    tm, tl, tg = layer.for_tile("TM"), layer.for_tile("TL"), layer.for_tile("TG")
    assert tm[3][1].tolist() == ["Fluvial Models"] and tm[2][1].tolist() == ["Tidal Models"]         # the big one touches TM ...
    assert len(tl[3][0]) == 0 and tl[2][1].tolist() == ["Tidal Models"]                              # ... and TL, but the small one is only in TM
    assert len(tg[3][0]) == 0 and len(tg[2][0]) == 0                                                 # TG is far from both
    assert shapely.contains_xy(tm[3][0][0], 1.005, 52.005)
    monkeypatch.setattr(inputs, "_iter_wkt_layer", lambda *a, **k: (_ for _ in ()).throw(AssertionError("parsed the 2 GB file again")))
    again = inputs.flood_layer("x", (-1, 50, 3, 53), today="2026-10-16")
    assert again.counts == layer.counts and again.for_tile("TM")[3][1].tolist() == ["Fluvial Models"]
