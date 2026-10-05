"""A tiny made-up area ("testland") whose raw source files are written into a bronze layer, so the whole pipeline can run
from scratch in a test: no downloads, no real data, no local state.

Everything is in British National Grid metres around (600000, 240000) and mirrors the real file layouts: Overpass JSON,
OS Open Map Local shapefiles inside a zip, GIAS CSV, a CQC spreadsheet (.ods), the NHS ODS CSVs and a Code-Point zip.
What is placed where (each case exercises one rule of the pipeline):

    fire station     OS building + OSM way on top of each other        -> one 'Both' record, matched by overlap
    Oak Primary      OS site + OS building + OSM way + a DfE record    -> a verified site; a small second OS building -> ancillary
    music academy    an OSM node only                                  -> OSM only, a different rule
    Elm Primary      a DfE record only, inside an OS Building polygon  -> a register gap added as a new building
"""
import json
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point, box

from critical_buildings import paths, sources

AREA = "testland"
TILE = "tl"
BOUNDARY = box(599000, 239000, 602000, 242000)
_TO_WGS84 = Transformer.from_crs(27700, 4326, always_xy=True)


def area_config():
    return {"name": "Testland", "description": "Synthetic area for tests.", "osm_boundary": {"ref_gss": "E00000000", "admin_level": 6},
            "os_grid_squares": [TILE], "crs": 27700}


def _lonlat(x, y):
    lon, lat = _TO_WGS84.transform(x, y)
    return {"lat": lat, "lon": lon}


def _ring(poly):
    return [_lonlat(x, y) for x, y in poly.exterior.coords]


def _way(i, poly, **tags):
    return {"type": "way", "id": i, "tags": tags, "geometry": _ring(poly)}


def _node(i, x, y, **tags):
    return {"type": "node", "id": i, "tags": tags, **_lonlat(x, y)}


FIRE_OS = box(600100, 240100, 600140, 240130)
FIRE_OSM = box(600098, 240098, 600142, 240132)
SCHOOL_SITE = box(600500, 240500, 600700, 240650)
SCHOOL_OS = box(600520, 240520, 600580, 240560)
SCHOOL_OSM = box(600522, 240522, 600578, 240558)
SCHOOL_SHED = box(600640, 240520, 600650, 240530)          # a 100 m2 OS building in the school site: an ancillary
MUSIC = (601000, 241000)
ELM = (601505, 241505)
ELM_FOOTPRINT = box(601490, 241490, 601520, 241520)


def _write_json(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")


def _overpass(dest):
    boundary = {"type": "relation", "id": 1, "tags": {"name": "Testland"},
                "members": [{"type": "way", "ref": 2, "role": "outer", "geometry": _ring(BOUNDARY)}]}
    features = [
        _way(10, FIRE_OSM, amenity="fire_station", name="Testland Fire Station"),
        _way(11, SCHOOL_OSM, school="primary", building="school", name="Oak Primary School"),
        _node(12, *MUSIC, amenity="music_school", name="Testland Music Academy"),
    ]
    _write_json(dest / "area_boundary.json", {"elements": [boundary]})
    _write_json(dest / "osm_features.json", {"elements": features})
    _write_json(dest / "osm_area_buildings.json", {"elements": [_way(20, box(601800, 241800, 601820, 241820), building="yes")]})
    return ["area_boundary.json", "osm_features.json", "osm_area_buildings.json"]


def _os_zip(dest, work):
    """opmplc_essh_tl.zip holding the four layers the pipeline reads, as shapefiles."""
    layers = {
        "FunctionalSite": ([{"ID": "fs1", "DISTNAME": "Oak Primary School", "SITETHEME": "Education",
                             "CLASSIFICA": "Primary Education", "geometry": SCHOOL_SITE}]),
        "ImportantBuilding": ([
            {"ID": "ib1", "DISTNAME": "Testland Fire Station", "BUILDGTHEM": "Emergency Service", "CLASSIFICA": "Fire Station", "geometry": FIRE_OS},
            {"ID": "ib2", "DISTNAME": "Oak Primary School", "BUILDGTHEM": "Education", "CLASSIFICA": "Primary Education", "geometry": SCHOOL_OS},
            {"ID": "ib3", "DISTNAME": "", "BUILDGTHEM": "Education", "CLASSIFICA": "Primary Education", "geometry": SCHOOL_SHED}]),
        "RailwayStation": ([{"ID": "rs1", "DISTNAME": "Testland", "CLASSIFICA": "Railway Station", "geometry": Point(600900, 240900)}]),
        "Building": ([{"ID": "b1", "geometry": ELM_FOOTPRINT}, {"ID": "b2", "geometry": box(601200, 241200, 601210, 241210)}]),
    }
    name = f"opmplc_essh_{TILE}.zip"
    top = f"OS OpenMap Local (ESRI Shape File) {TILE.upper()}/data"
    with zipfile.ZipFile(dest / name, "w") as z:
        for layer, rows in layers.items():
            folder = Path(work) / layer
            folder.mkdir(parents=True)
            shp = folder / f"{TILE.upper()}_{layer}.shp"
            gpd.GeoDataFrame(rows, geometry="geometry", crs=27700).to_file(shp)
            for f in sorted(folder.iterdir()):
                z.write(f, f"{top}/{f.name}")
    return [name]


def _gias(dest):
    cols = ["URN", "EstablishmentName", "EstablishmentStatus (name)", "TypeOfEstablishment (name)", "PhaseOfEducation (name)",
            "NumberOfPupils", "Trusts (name)", "Easting", "Northing", "Postcode"]
    rows = [
        [100001, "Oak Primary School", "Open", "Community school", "Primary", 210, "", 600550, 240540, "TL1 1AA"],
        [100002, "Elm Primary School", "Open", "Community school", "Primary", 95, "Elm Trust", ELM[0], ELM[1], "TL1 2BB"],
        [100003, "Testland Secondary", "Open", "Academy", "Secondary", 800, "", 601900, 240200, "TL1 3CC"],     # no rule maps it: unmapped
    ]
    pd.DataFrame(rows, columns=cols).to_csv(dest / "edubasealldata.csv", index=False, encoding="cp1252")
    return ["edubasealldata.csv"]


def _ods_bytes(header, rows):
    """A minimal OpenDocument spreadsheet (one sheet, HSCA_Active_Locations) that python-calamine can read."""
    def cell(v):
        if isinstance(v, int | float) and not isinstance(v, bool):
            return f'<table:table-cell office:value-type="float" office:value="{v}"/>'
        text = str(v).replace("&", "&amp;").replace("<", "&lt;")
        return f'<table:table-cell office:value-type="string"><text:p>{text}</text:p></table:table-cell>' if text else "<table:table-cell/>"
    body = "".join("<table:table-row>" + "".join(cell(v) for v in r) + "</table:table-row>" for r in [header, *rows])
    return ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
            'office:version="1.2"><office:body><office:spreadsheet><table:table table:name="HSCA_Active_Locations">'
            f"{body}</table:table></office:spreadsheet></office:body></office:document-content>")


def _cqc(dest):
    header = ["Location ID", "Location Name", "Location ODS Code", "Location Latitude", "Location Longitude", "Location Postal Code",
              "Location UPRN ID", "Location Primary Inspection Category", "Location Latest Overall Rating", "Care homes beds",
              "Provider Name", "Dormant (Y/N)"]
    lon, lat = _TO_WGS84.transform(600300, 240300)
    rows = [["1-100", "Testland Care Home", "RXX99", lat, lon, "TL1 4DD", 10001, "Residential social care", "Good", 40, "Care Ltd", "N"],
            ["1-101", "Dormant Home", "", lat, lon, "TL1 4DE", "", "Residential social care", "", 0, "Care Ltd", "Y"]]
    path = dest / "active_locations.ods"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/manifest.xml", '<?xml version="1.0" encoding="UTF-8"?><manifest:manifest '
                   'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2"/>')
        z.writestr("content.xml", _ods_bytes(header, rows))
    return [path.name]


def _nhs(dest):
    def row(n, **cells):
        r = [""] * n
        for i, v in cells.items():
            r[int(i[1:])] = v
        return ",".join(r)
    (dest / "etr.csv").write_text("RXX,Testland NHS Trust\n", encoding="utf-8")
    (dest / "ets.csv").write_text(row(15, c0="RXX01", c1="Testland Hospital", c9="TL1 5EE", c14="RXX") + "\n", encoding="utf-8")
    (dest / "epraccur.csv").write_text(row(15, c0="A1", c1="High Street Surgery", c9="TL1 6FF", c12="ACTIVE", c14="PCN1") + "\n",
                                       encoding="utf-8")
    return ["etr.csv", "ets.csv", "epraccur.csv"]


def _codepoint(dest):
    with zipfile.ZipFile(dest / "codepo_gb.zip", "w") as z:
        z.writestr("Doc/Code-Point_Open_Column_Headers.csv", "PC,PQ,EA,NO,CY\n")
        z.writestr("Data/CSV/tl.csv", "TL1 1AA,10,600550,240540,E92\nTL1 2BB,10,601505,241505,E92\nTL1 5EE,10,600400,240400,E92\n")
    return ["codepo_gb.zip"]


def _snapshot(source, files_fn, work, area=None, snapshot="2026-01-01"):
    d = paths.bronze_source_dir(source, area) / snapshot
    d.mkdir(parents=True)
    names = files_fn(d)
    sources.write_manifest(source, d, [{"name": n} for n in names], area=area, retrieved_at="2026-01-01T00:00:00+00:00")
    sources.set_latest(source, snapshot, area)


def build_bronze(work):
    """Write one snapshot of every source for the synthetic area into the (patched) bronze folder. `work` is scratch space."""
    work = Path(work)
    _snapshot("osm_overpass", _overpass, work, AREA)
    _snapshot("os_open_map_local", lambda d: _os_zip(d, work / "os"), work, AREA)
    _snapshot("os_codepoint_open", _codepoint, work)
    _snapshot("dfe_gias", _gias, work)
    _snapshot("cqc_hsca", _cqc, work)
    _snapshot("nhs_ods", _nhs, work)
