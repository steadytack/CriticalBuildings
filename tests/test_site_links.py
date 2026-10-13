"""UPRNs inside each site and a postcode for each site (gold/site_links.py), on small hand-made inputs."""
import zipfile

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from critical_buildings import paths
from critical_buildings.errors import PipelineError
from critical_buildings.gold import site_links


def uprn_zip(path, rows, header="UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE"):
    body = "".join(f"{u},{x},{y},52.0,1.0\n" for u, x, y in rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("osopenuprn_202609.csv", header + "\n" + body)
    return path


def test_each_uprn_goes_to_the_site_it_lies_in(tmp_path):
    a, b = box(0, 0, 10, 10), box(100, 0, 110, 10)
    z = uprn_zip(tmp_path / "u.zip", [(5, 5, 5), (3, 1, 1), (9, 105, 5), (7, 500, 500)])
    got = site_links.uprns_in_sites([a, b], z)
    assert list(zip(got.pos, got.uprn, strict=True)) == [(0, 3), (0, 5), (1, 9)]         # sorted by site then UPRN; 7 is in no site


def test_a_uprn_on_the_shared_edge_of_two_sites_belongs_to_both(tmp_path):
    z = uprn_zip(tmp_path / "u.zip", [(1, 10, 5)])
    got = site_links.uprns_in_sites([box(0, 0, 10, 10), box(10, 0, 20, 10)], z)
    assert sorted(got.pos) == [0, 1] and set(got.uprn) == {1}


def test_batches_do_not_change_the_answer(tmp_path, monkeypatch):
    rng = np.random.default_rng(1)
    rows = [(i, float(x), float(y)) for i, (x, y) in enumerate(rng.uniform(0, 100, (500, 2)))]
    z = uprn_zip(tmp_path / "u.zip", rows)
    sites = [box(0, 0, 50, 50), box(40, 40, 100, 100)]
    whole = site_links.uprns_in_sites(sites, z)
    monkeypatch.setattr(site_links, "BLOCK_BYTES", 2048)                             # many small batches
    assert site_links.uprns_in_sites(sites, z).equals(whole) and len(whole) > 100


def test_a_changed_uprn_file_is_an_error_that_says_so(tmp_path):
    z = uprn_zip(tmp_path / "u.zip", [(1, 1, 1)], header="UPRN,EASTING,NORTHING")
    with pytest.raises(PipelineError, match="no longer has the columns UPRN, X_COORDINATE, Y_COORDINATE"):
        site_links.uprns_in_sites([box(0, 0, 10, 10)], z)
    empty = tmp_path / "e.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "x")
    with pytest.raises(PipelineError, match="holds no CSV file"):
        site_links.uprns_in_sites([box(0, 0, 10, 10)], empty)


def test_the_join_is_cached_until_the_outlines_change(isolated_paths, monkeypatch):
    snap = paths.bronze_source_dir("os_open_uprn") / "2026-01-01"
    snap.mkdir(parents=True)
    uprn_zip(snap / "osopenuprn_csv.zip", [(1, 5, 5)])
    from critical_buildings import sources
    sources.write_manifest("os_open_uprn", snap, [{"name": "osopenuprn_csv.zip"}])
    sources.set_latest("os_open_uprn", "2026-01-01")
    sites = [box(0, 0, 10, 10)]
    first = site_links.cached_uprns_in_sites(sites, "x")
    calls = []
    real = site_links.uprns_in_sites
    monkeypatch.setattr(site_links, "uprns_in_sites", lambda *a: calls.append(1) or real(*a))
    assert site_links.cached_uprns_in_sites(sites, "x").equals(first) and calls == []            # same outlines: read from the cache
    site_links.cached_uprns_in_sites([box(0, 0, 11, 11)], "x")
    assert calls == [1]                                                                         # other outlines: streamed again


def test_attach_adds_counts_a_capped_inline_list_and_the_full_table(isolated_paths, monkeypatch):
    many = site_links.UPRNS_INLINE + 25
    pairs = pd.DataFrame({"pos": [0] * many + [1], "uprn": list(range(1000, 1000 + many)) + [5]})
    sites = gpd.GeoDataFrame({"Site ID": ["SITE-1", "SITE-2", "SITE-3"], "Authoritative Postcode": ["AB1 2CD", "", ""]},
                             geometry=[box(0, 0, 1, 1), box(5, 5, 6, 6), box(9, 9, 10, 10)], crs=27700)
    monkeypatch.setattr(site_links, "cached_uprns_in_sites", lambda geoms, area: pairs)
    monkeypatch.setattr(site_links, "postcodes_for_sites", lambda g, r, table=None: pd.DataFrame(
        {"Postcode": ["AB1 2CD", "", ""], "Postcode Method": ["Register", "", ""],
         "Postcode Distance (m)": pd.array([None, None, None], dtype="Int64")}))
    out, long = site_links.attach(sites, "x")
    assert list(out["UPRN Count"]) == [many, 1, 0]
    assert len(out.loc[0, "UPRNs"].split(";")) == site_links.UPRNS_INLINE and out.loc[0, "UPRNs"].startswith("1000;1001")
    assert out.loc[1, "UPRNs"] == "5" and out.loc[2, "UPRNs"] == ""
    assert len(long) == many + 1 and set(long["Site ID"]) == {"SITE-1", "SITE-2"}            # the table is complete, not capped


def table(*rows):
    """A Code-Point style table indexed by normalised postcode."""
    return pd.DataFrame({"EA": [r[1] for r in rows], "NO": [r[2] for r in rows]}, index=[r[0] for r in rows])


def test_postcode_rules_register_then_inside_then_nearest():
    sites = [box(0, 0, 100, 100), box(1000, 0, 1100, 100), box(2000, 0, 2100, 100), box(5000, 0, 5100, 100), box(7000, 0, 7100, 100)]
    pc = table(("AB11AA", 50, 50),          # inside site 1
               ("AB11AB", 10, 10),          # also inside site 1, but further from its middle
               ("AB12CD", 1250, 50),        # 200 m from the middle of site 2
               ("AB13EF", 2050, 50),        # inside site 3 (register gives another)
               ("ZZ99ZZ", 9000, 9000))      # far from everything
    got = site_links.postcodes_for_sites(sites, ["", "", "xy1  2ab | other", "", ""], pc)
    assert list(got["Postcode"]) == ["AB1 1AA", "AB1 2CD", "XY1 2AB", "", ""]
    assert list(got["Postcode Method"]) == ["Code-Point inside outline", "Code-Point nearest", "Register", "", ""]
    assert list(got["Postcode Distance (m)"].astype("object").where(got["Postcode Distance (m)"].notna(), None)) == [0, 200, None, None, None]


def test_a_postcode_beyond_the_limit_is_not_offered():
    site = [box(0, 0, 100, 100)]
    near = site_links.postcodes_for_sites(site, [""], table(("AB11AA", 50 + site_links.POSTCODE_MAX_M - 10, 50 + 50 + 0)))
    far = site_links.postcodes_for_sites(site, [""], table(("AB11AA", 50 + site_links.POSTCODE_MAX_M + 60, 50)))
    assert near["Postcode Method"].iloc[0] == "Code-Point nearest" and far["Postcode"].iloc[0] == ""


def test_postcodes_are_formatted_with_the_inward_part_last():
    assert site_links.format_postcode("IP11AA") == "IP1 1AA" and site_links.format_postcode("sw1a1aa") == "SW1A 1AA"
    assert site_links.format_postcode("M11AE") == "M1 1AE"
