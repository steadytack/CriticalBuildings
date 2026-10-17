"""Silver lookups built from the bronze files: Code-Point postcodes (subset on demand) and NHS ODS codes."""
import zipfile

import pytest

from critical_buildings import paths, postcodes, silver


@pytest.fixture
def codepoint(tmp_path, monkeypatch):
    """A tiny Code-Point Open zip: ip.csv and co.csv, columns PC, PQ, EA, NO, ... (as in the real file)."""
    z = tmp_path / "codepo_gb.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Doc/Code-Point_Open_Column_Headers.csv", "PC,PQ,EA,NO,CY\n")
        f.writestr("Data/CSV/", "")
        f.writestr("Data/CSV/ip.csv", 'IP1 1AA,10,615838,244034,E92\n"IP1 1AB",10,615900,244100,E92\nIP1 1AA,10,1,2,E92\n')
        f.writestr("Data/CSV/co.csv", "CO1 1AA,10,600000,225000,E92\n")
    monkeypatch.setattr(paths, "bronze_file", lambda source, name, **kw: z)
    postcodes.postcode_table.cache_clear()
    yield
    postcodes.postcode_table.cache_clear()


def test_postcode_points_returns_only_the_requested_postcodes(codepoint):
    got = postcodes.postcode_points({"IP11AB", "CO11AA", "ZZ99ZZ"})
    assert got == {"IP11AB": (615900, 244100), "CO11AA": (600000, 225000)}      # unknown postcodes are simply absent


def test_postcode_points_without_a_filter_returns_everything_and_the_last_duplicate_wins(codepoint):
    got = postcodes.postcode_points()
    assert set(got) == {"IP11AA", "IP11AB", "CO11AA"} and got["IP11AA"] == (1, 2)


def test_postcodes_are_matched_after_normalising_spaces_and_case(codepoint):
    assert postcodes.norm_pc("ip1 1ab") == "IP11AB"
    assert postcodes.postcode_points({postcodes.norm_pc("ip1 1ab")}) == {"IP11AB": (615900, 244100)}


def test_ods_codes_are_read_with_their_trust_and_status(tmp_path, monkeypatch):
    def row(n, **cells):
        r = [""] * n
        for i, v in cells.items():
            r[int(i[1:])] = v
        return ",".join(r)
    files = {
        "etr.csv": "RXX,Some NHS Trust\n",
        "ets.csv": "\n".join([row(15, c0="RXX01", c1="Town Hospital", c9="IP1 1AA", c14="RXX"),
                              row(15, c0="RXX02", c1="Closed Site", c9="IP1 1AB", c11="20200101", c14="RXX")]) + "\n",
        "epraccur.csv": "\n".join([row(15, c0="A1", c1="High Street Surgery", c9="CO1 1AA", c12="ACTIVE", c14="PCN1"),
                                   row(15, c0="RXX01", c1="Duplicate of a site code", c9="X", c12="ACTIVE")]) + "\n",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    monkeypatch.setattr(paths, "bronze_file", lambda source, name, **kw: tmp_path / name)
    ods = silver._ods()
    assert ods["RXX01"] == {"name": "Town Hospital", "postcode": "IP1 1AA", "status": "Active",
                            "parent": "Some NHS Trust", "kind": "ets"}          # ets wins over epraccur for the same code
    assert ods["RXX02"]["status"] == "Closed"
    assert ods["A1"] == {"name": "High Street Surgery", "postcode": "CO1 1AA", "status": "Active",
                         "parent": "PCN1", "kind": "epraccur"}


def test_silver_key_changes_with_the_silver_code_and_area_settings(monkeypatch):
    from types import SimpleNamespace

    from critical_buildings import config, silver
    rules = SimpleNamespace(snapshot={"rules": []})
    monkeypatch.setattr(silver.paths, "bronze_key", lambda area: "bronze")
    base = silver.silver_key(rules, "suffolk")
    assert silver.silver_key(rules, "suffolk") == base
    monkeypatch.setattr(silver, "_code_fingerprint", lambda: "edited")
    assert silver.silver_key(rules, "suffolk") != base
    monkeypatch.undo()
    monkeypatch.setattr(silver.paths, "bronze_key", lambda area: "bronze")
    area = config.load_area("suffolk")
    monkeypatch.setattr(config, "load_area", lambda name: {**area, "os_grid_squares": ["tl"]})
    assert silver.silver_key(rules, "suffolk") != base


def test_invalid_polygons_are_repaired_so_that_intersections_do_not_fail():
    import geopandas as gpd
    from shapely.geometry import Point, Polygon, box
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])                    # a figure of eight: self-intersecting
    gdf = gpd.GeoDataFrame({"k": [1, 2, 3]}, geometry=[bowtie, box(0, 0, 5, 5), Point(1, 1)], crs=27700)
    assert not gdf.geometry.iloc[0].is_valid
    fixed = silver.repair_geometries(gdf)
    assert fixed.geometry.is_valid.all() and fixed.geometry.iloc[0].geom_type in ("Polygon", "MultiPolygon")
    assert fixed.geometry.iloc[0].area == pytest.approx(50.0)                 # both lobes of the figure of eight are kept
    assert fixed.geometry.iloc[1].equals(box(0, 0, 5, 5)) and fixed.geometry.iloc[2].equals(Point(1, 1))
    assert fixed.geometry.iloc[0].intersection(box(0, 0, 5, 5)).area > 0      # no TopologyException any more
    assert not gdf.geometry.iloc[0].is_valid                                  # the input frame is not modified


def test_a_feature_in_two_overlapping_os_tiles_is_read_once(tmp_path, monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box
    for tile, rows in (("tl", [("a", box(0, 0, 1, 1)), ("edge", box(9, 0, 10, 1))]), ("tm", [("edge", box(9, 0, 10, 1)), ("b", box(20, 0, 21, 1))])):
        gpd.GeoDataFrame({"ID": [r[0] for r in rows], "DISTNAME": ["", ""], "SITETHEME": ["", ""], "CLASSIFICA": ["", ""]},
                         geometry=[r[1] for r in rows], crs=27700).to_file(tmp_path / f"{tile}.shp")
    monkeypatch.setattr(paths, "os_layer_paths", lambda layer, tiles, area: [str(tmp_path / "tl.shp"), str(tmp_path / "tm.shp")])
    got = silver._read_layer("FunctionalSite", box(-5, -5, 50, 50), "suffolk")
    assert sorted(got.ID) == ["a", "b", "edge"]                       # 'edge' is in both tiles' files and comes back once
