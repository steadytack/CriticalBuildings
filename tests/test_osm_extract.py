"""Reading tagged features from an OpenStreetMap extract (here a tiny OSM XML file, which GDAL reads exactly like a PBF)."""
import pytest
from shapely.geometry import box

from critical_buildings import osm_extract

TAGS = [("amenity", "fire_station"), ("landuse", "military"), ("telecom:data_center", "*"), ("school", "primary")]


def _xml(nodes, ways):
    return ('<?xml version="1.0" encoding="UTF-8"?><osm version="0.6" generator="test">' + "".join(nodes) + "".join(ways) + "</osm>")


def _node(i, lon, lat, **tags):
    inner = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
    return f'<node id="{i}" lat="{lat}" lon="{lon}" version="1" timestamp="2026-01-01T00:00:00Z">{inner}</node>'


def _way(i, refs, **tags):
    inner = "".join(f'<nd ref="{r}"/>' for r in refs) + "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
    return f'<way id="{i}" version="1" timestamp="2026-01-01T00:00:00Z">{inner}</way>'


@pytest.fixture
def extract(tmp_path):
    """A fire station node, a data centre node (a key with colons), a military area, a building inside it, one outside it,
    and an open way tagged as a fire station."""
    nodes = [_node(1, 1.0, 52.0, amenity="fire_station", name="Station A"),
             _node(2, 1.01, 52.0, **{"telecom:data_center": "yes"}),
             # military area corners 3-6 and a building inside (7-10) and outside (11-14)
             _node(3, 1.10, 52.10), _node(4, 1.12, 52.10), _node(5, 1.12, 52.12), _node(6, 1.10, 52.12),
             _node(7, 1.105, 52.105), _node(8, 1.106, 52.105), _node(9, 1.106, 52.106), _node(10, 1.105, 52.106),
             _node(11, 1.20, 52.20), _node(12, 1.201, 52.20), _node(13, 1.201, 52.201), _node(14, 1.20, 52.201),
             _node(15, 1.30, 52.30), _node(16, 1.31, 52.30)]
    ways = [_way(100, [3, 4, 5, 6, 3], landuse="military", name="Range"),
            _way(101, [7, 8, 9, 10, 7], building="yes", name="Hut"),
            _way(102, [11, 12, 13, 14, 11], building="yes"),
            _way(103, [15, 16], amenity="fire_station", name="Open way")]
    p = tmp_path / "extract.osm"
    p.write_text(_xml(nodes, ways), encoding="utf-8")
    return str(p)


def test_the_filter_and_config_are_built_from_the_rule_tags():
    where = osm_extract.where_clause(TAGS)
    assert "amenity IN ('fire_station')" in where and "school IN ('primary')" in where
    assert 'other_tags LIKE \'%"telecom:data_center"=>%\'' in where           # a key with colons lives in other_tags
    cfg = osm_extract.config_text(TAGS)
    assert "closed_ways_are_polygons=amenity,building,landuse,school,telecom:data_center" in cfg
    assert "attributes=amenity,building,landuse,name,school" in cfg            # plain keys become columns, colon keys do not
    assert osm_extract.parse_hstore('"a"=>"1","b:c"=>"x \\"y\\""') == {"a": "1", "b:c": 'x \\"y\\"'}
    assert osm_extract.parse_hstore(None) == {}


def test_features_come_back_as_osm_id_tags_geometry(extract):
    f = osm_extract.read_features(extract, TAGS)
    by_id = {r.osm_id: r for r in f.itertuples()}
    assert set(by_id) == {"node/1", "node/2", "way/100", "way/103"}              # the huts have no active tag, so they are not read
    assert by_id["node/1"].tags == {"amenity": "fire_station", "name": "Station A"}
    assert by_id["node/2"].tags == {"telecom:data_center": "yes"}                # picked out of other_tags
    assert by_id["way/100"].geometry.geom_type in ("Polygon", "MultiPolygon")    # a closed way is an area
    assert by_id["way/103"].geometry.geom_type == "Point"                        # an open way is its centroid, as on the Overpass route
    assert f.crs.to_epsg() == 27700 and 0 < by_id["node/1"].geometry.x < 700_000


def test_buildings_are_read_only_inside_the_given_areas(extract):
    f = osm_extract.read_features(extract, TAGS)
    areas = osm_extract.area_geometries(f.assign(tags=[{**t} for t in f.tags]))
    assert areas == [] or all(a.geom_type in ("Polygon", "MultiPolygon") for a in areas)      # landuse=military is an area tag
    area = f[f.osm_id == "way/100"].geometry.iloc[0]
    inside = osm_extract.read_buildings_inside(extract, TAGS, [area])
    assert list(inside.osm_id) == ["way/101"] and inside.tags.iloc[0] == {"name": "Hut", "building": "yes"}
    assert osm_extract.read_buildings_inside(extract, TAGS, []).empty
    assert osm_extract.read_buildings_inside(extract, TAGS, [box(0, 0, 1, 1)]).empty


def test_a_missing_or_foreign_file_is_an_error_not_an_empty_result(tmp_path):
    bad = tmp_path / "x.osm"
    bad.write_text("not an osm file", encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017, PT011 - GDAL's own error type; the point is that it does not return nothing
        osm_extract.read_features(str(bad), TAGS)
