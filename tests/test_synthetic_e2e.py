"""The whole pipeline, from raw source files to the delivered outputs, on a tiny synthetic area (tests/synthetic_area.py).

Unlike the regression test on the real Suffolk data this needs no downloads, no governed store and no local state, so it
runs on any clone and in CI. It starts from an empty ID registry, so the IDs it produces are the same every time.
"""
import json
import os
from pathlib import Path

import golden_utils
import pandas as pd
import pytest
import shapely
import synthetic_area
from shapely.geometry import Point

from critical_buildings import config, pipeline, postcodes

BASELINE = Path(__file__).parent / "golden" / "synthetic.json"


def _synthetic(isolated_paths, seeded_store, monkeypatch, extract):
    raw = config._raw
    area = synthetic_area.area_config(extract)
    monkeypatch.setattr(config, "_raw", lambda path=None: {**raw(path), "areas": {**raw(path)["areas"], synthetic_area.AREA: area}})
    postcodes.postcode_table.cache_clear()
    synthetic_area.build_bronze(isolated_paths / "work", extract)
    seeded_store.publish("tester", "synthetic area rules")
    return seeded_store


@pytest.fixture
def synthetic(isolated_paths, seeded_store, monkeypatch):
    """Bronze data for the synthetic area in a temp data folder, the area registered, and a published rule set."""
    yield _synthetic(isolated_paths, seeded_store, monkeypatch, extract=False)
    postcodes.postcode_table.cache_clear()


@pytest.fixture
def synthetic_extract(isolated_paths, seeded_store, monkeypatch):
    """The same area, but OpenStreetMap comes from an extract file (the route used for whole countries)."""
    yield _synthetic(isolated_paths, seeded_store, monkeypatch, extract=True)
    postcodes.postcode_table.cache_clear()


# Columns left out of the comparison: dates (a fresh registry stamps today) and coordinates, in text form (they come from a
# projection library, and the last digit may differ between platforms). The coordinates are still checked below, to a centimetre.
DROP = [*golden_utils.VOLATILE, "Created Date", "Latitude", "Longitude", "Geom"]


def run(store, **kw):
    return pipeline.run(synthetic_area.AREA, store=store, **kw)


def test_the_pipeline_reproduces_the_synthetic_baseline_with_and_without_silver_reuse(synthetic):
    first = run(synthetic, reuse_silver=False)
    if os.environ.get("UPDATE_GOLDEN"):                       # regenerate the baseline after an intended change
        BASELINE.write_text(json.dumps(golden_utils.describe(first["dest"], DROP), indent=2, sort_keys=True), encoding="utf-8")
    assert golden_utils.compare(first["dest"], BASELINE, DROP) == []
    second = run(synthetic, reuse_silver=True)
    assert second["silver_reused"] is True and second["previous_run_id"] == first["run_id"]
    assert golden_utils.compare(second["dest"], BASELINE, DROP) == []
    lin = json.loads((second["dest"] / "lineage.json").read_text(encoding="utf-8"))
    assert lin["gold"]["changes_vs_previous"] == {}           # same inputs and rules: nothing added, removed or reclassified
    assert second["new_ids"] == 0                             # the second run reused every ID from the registry


def test_each_synthetic_case_comes_out_as_designed(synthetic):
    out = run(synthetic, reuse_silver=False)["dest"]
    b = pd.read_csv(out / "buildings.csv", dtype=str).fillna("").set_index("Name")
    assert (b.loc["Testland Fire Station", ["Source", "Match Method", "Building Type"]] == ["Both", "Overlap", "Fire Station"]).all()
    oak = b.loc["Oak Primary School"]
    assert oak.Source == "Both" and oak.Verification == "Verified" and oak["Authoritative Name"] == "Oak Primary School"
    assert b.loc["Testland Music Academy", "Source"] == "OSM" and b.loc["Testland Music Academy", "Building Type"] == "School"
    elm = b.loc["Elm Primary School"]
    assert elm.Source == "DfE GIAS" and elm.Verification == "Authoritative only"       # a register gap, snapped to a footprint
    shed = pd.read_csv(out / "buildings.csv", dtype=str).fillna("").query("Name == '' and `Source` == 'OS Important Buildings'")
    assert len(shed) == 1 and shed.iloc[0]["Building Role"] == "Ancillary"             # the 100 m2 OS building inside the school site
    assert shed.iloc[0]["Site ID"] == oak["Site ID"]
    assert set(b.index) == {"Testland Fire Station", "Oak Primary School", "Testland Music Academy", "Elm Primary School", ""}
    music = shapely.from_wkt(b.loc["Testland Music Academy", "Geom"])
    assert music.distance(Point(*synthetic_area.MUSIC)) < 0.01                       # a point comes back to within a centimetre
    assert shapely.from_wkt(b.loc["Testland Fire Station", "Geom"]).symmetric_difference(synthetic_area.FIRE_OS).area < 1e-6
    lat, lon = pd.to_numeric(b["Latitude"]), pd.to_numeric(b["Longitude"])
    assert lat.between(51, 53).all() and lon.between(0, 2).all()
    sites = pd.read_csv(out / "sites.csv", dtype=str).fillna("")
    links = pd.read_csv(out / "site_uprns.csv", dtype=str)
    by_name = sites.set_index("Site Name")
    for name, count, postcode, method in (("Testland Fire Station", "3", "TL1 9ZZ", "Code-Point inside outline"),
                                          ("Oak Primary School", "2", "TL1 1AA", "Register"),
                                          ("Elm Primary School", "1", "TL1 2BB", "Register"),
                                          ("Testland Music Academy", "1", "TL1 8YY", "Code-Point nearest")):
        site = by_name.loc[name]
        assert (site["UPRN Count"], site["Postcode"], site["Postcode Method"]) == (count, postcode, method), name
        assert sorted(links.loc[links["Site ID"] == site["Site ID"], "UPRN"]) == sorted(site["UPRNs"].split(";"))     # list and table agree
    assert by_name.loc["Testland Music Academy", "Postcode Distance (m)"] == "100"
    assert by_name.loc["Testland Fire Station", "UPRNs"] == "100000000001;100000000002;100000000003"
    assert len(links) == 7 and "100000000099" not in set(links.UPRN)                      # the UPRN outside every site is in none
    assert sorted(sites["Site Type"]) == ["Fire Station", "Primary School", "Primary School", "School"]
    assert int(sites.set_index("Site Name").loc["Oak Primary School", "Buildings in Site"]) == 2


def test_the_extract_route_gives_the_same_dataset_as_the_overpass_route(synthetic_extract):
    """Reading OpenStreetMap from an extract file must produce exactly the baseline the Overpass JSON route produces."""
    out = run(synthetic_extract, reuse_silver=False)
    assert golden_utils.compare(out["dest"], BASELINE, DROP) == []





def test_a_second_run_reuses_the_cached_extract_read(synthetic_extract):
    """Reading a country's extract takes minutes, so it is cached: a rebuild of silver with the same extract and OSM tags (here
    forced with reuse_silver=False) must not read the file again."""
    first = run(synthetic_extract, reuse_silver=False)
    second = run(synthetic_extract, reuse_silver=False)
    stages = synthetic_extract.stages_df()
    notes = {r.run_id: json.loads(r.detail_json) for r in stages[stages.stage == "silver"].itertuples()}
    assert notes[first["run_id"]]["OSM extract read"] == "from the extract file"
    assert notes[second["run_id"]]["OSM extract read"] == "cached"
    assert golden_utils.compare(second["dest"], BASELINE, DROP) == []
