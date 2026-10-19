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
TILE = "tm"                    # every feature has an easting of 600000 or more, which is OS grid square TM
BOUNDARY = box(599000, 239000, 602000, 242000)
_TO_WGS84 = Transformer.from_crs(27700, 4326, always_xy=True)


def area_config(extract=False):
    """extract=True makes the area read OpenStreetMap from an extract file instead of the Overpass JSON."""
    cfg = {"name": "Testland", "description": "Synthetic area for tests.", "osm_boundary": {"ref_gss": "E00000000", "admin_level": 6},
           "os_grid_squares": [TILE], "crs": 27700}
    return {**cfg, "osm_extract": "test/testland"} if extract else cfg


def _lonlat(x, y):
    """WGS84 to 7 decimal places, the precision OpenStreetMap stores, so the Overpass JSON and the extract file agree exactly."""
    lon, lat = _TO_WGS84.transform(x, y)
    return {"lat": round(lat, 7), "lon": round(lon, 7)}


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


def _overpass(dest, boundary_only=False):
    boundary = {"type": "relation", "id": 1, "tags": {"name": "Testland"},
                "members": [{"type": "way", "ref": 2, "role": "outer", "geometry": _ring(BOUNDARY)}]}
    features = [
        _way(10, FIRE_OSM, amenity="fire_station", name="Testland Fire Station"),
        _way(11, SCHOOL_OSM, school="primary", building="school", name="Oak Primary School"),
        _node(12, *MUSIC, amenity="music_school", name="Testland Music Academy"),
    ]
    _write_json(dest / "area_boundary.json", {"elements": [boundary]})
    if boundary_only:                                           # an area with an extract only asks Overpass for its boundary
        return ["area_boundary.json"]
    _write_json(dest / "osm_features.json", {"elements": features})
    _write_json(dest / "osm_area_buildings.json", {"elements": [_way(20, box(601800, 241800, 601820, 241820), building="yes")]})
    return ["area_boundary.json", "osm_features.json", "osm_area_buildings.json"]


def _extract(dest):
    """The same three OSM features and the area building as OSM XML, which GDAL's OSM driver reads exactly like a PBF."""
    nodes, ways = [], []

    def node(x, y, tags=None):
        i = len(nodes) + 1000
        p = _lonlat(x, y)
        inner = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in (tags or {}).items())
        nodes.append(f'<node id="{i}" lat="{p["lat"]}" lon="{p["lon"]}" version="1" timestamp="2026-01-01T00:00:00Z">{inner}</node>')
        return i

    def way(i, poly, **tags):
        refs = [node(x, y) for x, y in list(poly.exterior.coords)[:-1]]
        refs.append(refs[0])                                  # a closed way ends on the node it started from
        inner = "".join(f'<nd ref="{r}"/>' for r in refs) + "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
        ways.append(f'<way id="{i}" version="1" timestamp="2026-01-01T00:00:00Z">{inner}</way>')

    # nodes must come in ascending id order, so the standalone node goes first
    p = _lonlat(*MUSIC)
    nodes.append(f'<node id="12" lat="{p["lat"]}" lon="{p["lon"]}" version="1" timestamp="2026-01-01T00:00:00Z">'
                 '<tag k="amenity" v="music_school"/><tag k="name" v="Testland Music Academy"/></node>')
    way(10, FIRE_OSM, amenity="fire_station", name="Testland Fire Station")
    way(11, SCHOOL_OSM, school="primary", building="school", name="Oak Primary School")
    way(20, box(601800, 241800, 601820, 241820), building="yes")
    xml = '<?xml version="1.0" encoding="UTF-8"?><osm version="0.6" generator="synthetic">' + "".join(nodes + ways) + "</osm>"
    (dest / "extract.osm.pbf").write_text(xml, encoding="utf-8")        # the file name the pipeline looks for
    return ["extract.osm.pbf"]


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
        z.writestr("Data/CSV/tl.csv", "TL1 1AA,10,600550,240540,E92\nTL1 2BB,10,601505,241505,E92\nTL1 5EE,10,600400,240400,E92\n"
                   "TL1 9ZZ,10,600120,240115,E92\nTL1 8YY,10,601100,241000,E92\n")      # 9ZZ is inside the fire station, 8YY 100 m from the music academy
    return ["codepo_gb.zip"]


# OS Open UPRN points: 3 inside the fire station, 2 inside the Oak school site, 1 in the Elm footprint, 1 at the music academy, 1 nowhere
UPRNS = [(100000000001, 600110, 240110), (100000000002, 600120, 240120), (100000000003, 600130, 240125),
         (100000000011, 600550, 240540), (100000000012, 600600, 240600),
         (100000000021, 601505, 241505), (100000000031, 601005, 241000), (100000000099, 610000, 250000)]
# two more, in a building that is in no critical site (OS Building b2), for the property table: they share a building
EXTRA_UPRNS = [(100000000041, 601202, 241202), (100000000042, 601207, 241207)]


def _uprn(dest):
    rows = "".join(f"{u},{x},{y},{lat:.8f},{lon:.8f}\n" for u, x, y, lat, lon in
                   ((u, x, y, *reversed(_TO_WGS84.transform(x, y))) for u, x, y in UPRNS + EXTRA_UPRNS))
    with zipfile.ZipFile(dest / "osopenuprn_csv.zip", "w") as z:
        z.writestr("osopenuprn_202609.csv", "UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE\n" + rows)
    return ["osopenuprn_csv.zip"]


def _snapshot(source, files_fn, work, area=None, snapshot="2026-01-01"):
    d = paths.bronze_source_dir(source, area) / snapshot
    d.mkdir(parents=True)
    names = files_fn(d)
    sources.write_manifest(source, d, [{"name": n} for n in names], area=area, retrieved_at="2026-01-01T00:00:00+00:00")
    sources.set_latest(source, snapshot, area)


def build_bronze(work, extract=False):
    """Write one snapshot of every source for the synthetic area into the (patched) bronze folder. `work` is scratch space.
    extract=True gives the area an OSM extract file (and only a boundary from Overpass) instead of the Overpass feature JSON."""
    work = Path(work)
    _snapshot("osm_overpass", lambda d: _overpass(d, boundary_only=extract), work, AREA)
    if extract:
        _snapshot("osm_extract", _extract, work, AREA)
    _snapshot("os_open_map_local", lambda d: _os_zip(d, work / "os"), work, AREA)
    _snapshot("os_codepoint_open", _codepoint, work)
    _snapshot("os_open_uprn", _uprn, work)
    _snapshot("dfe_gias", _gias, work)
    _snapshot("cqc_hsca", _cqc, work)
    _snapshot("nhs_ods", _nhs, work)


# ====================================================================================================
# The sources the property table adds: census areas, deprivation, planning layers and flood zones
# ====================================================================================================

def _lon_lat_text(x, y):
    lon, lat = _TO_WGS84.transform(x, y)
    return f"{lon:.7f} {lat:.7f}"


def _wkt_box(x0, y0, x1, y1):
    """A British National Grid rectangle as WGS84 well-known text, the way planning.data.gov.uk publishes outlines."""
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return "MULTIPOLYGON (((" + ", ".join(_lon_lat_text(x, y) for x, y in ring) + ")))"


def _wkt_point(x, y):
    return f"POINT ({_lon_lat_text(x, y)})"


LSOA_WEST, LSOA_EAST = "E01900001", "E01900002"      # split at easting 601250 (the Elm Primary UPRN is east of it, everything else west)


def _lsoa(dest):
    def feature(code, name, x0, x1):
        ring = [[x0, 239000], [x1, 239000], [x1, 242000], [x0, 242000], [x0, 239000]]
        return {"type": "Feature", "properties": {"LSOA21CD": code, "LSOA21NM": name}, "geometry": {"type": "Polygon", "coordinates": [ring]}}
    doc = {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:27700"}},
           "features": [feature(LSOA_WEST, "Testland 001A", 599000, 601250), feature(LSOA_EAST, "Testland 001B", 601250, 602000)]}
    (dest / "lsoa_000.geojson").write_text(json.dumps(doc), encoding="utf-8")
    return ["lsoa_000.geojson"]


def _iod(dest):
    cols = {"LSOA code (2021)": [LSOA_WEST, LSOA_EAST], "LSOA name (2021)": ["Testland 001A", "Testland 001B"],
            "Local Authority District code (2024)": ["E07900001", "E07900001"], "Local Authority District name (2024)": ["Testland", "Testland"],
            "Index of Multiple Deprivation (IMD) Score": [31.5, 5.25], "Index of Multiple Deprivation (IMD) Rank (where 1 is most deprived)": [4000, 31000],
            "Index of Multiple Deprivation (IMD) Decile (where 1 is most deprived 10% of LSOAs)": [2, 10]}
    for i, name in enumerate(["Income", "Employment", "Education, Skills and Training", "Health Deprivation and Disability", "Crime",
                              "Barriers to Housing and Services", "Living Environment",
                              "Income Deprivation Affecting Children Index (IDACI)", "Income Deprivation Affecting Older People (IDAOPI)"]):
        cols[f"{name} Decile (where 1 is most deprived 10% of LSOAs)"] = [1 + i % 9, 10 - i % 9]
    cols.update({"Total population: mid 2022": [1800, 1500], "Dependent Children aged 0-15: mid 2022": [300, 200],
                 "Older population aged 60 and over: mid 2022": [400, 600]})
    pd.DataFrame(cols).to_csv(dest / "iod2025_file7.csv", index=False, encoding="utf-8-sig")
    return ["iod2025_file7.csv"]


def _planning(dest):
    def write(name, rows, columns):
        pd.DataFrame(rows, columns=columns).to_csv(dest / f"{name}.csv", index=False)
    write("conservation-area", [
        ("conservation-area", "", "Oak Conservation Area", _wkt_box(600500, 240500, 600700, 240650)),
        ("conservation-area", "2001-01-01", "Ended Area", _wkt_box(600900, 240900, 601100, 241100))],      # ended: must not count
        ["dataset", "end-date", "name", "geometry"])
    write("article-4-direction-area", [("article-4-direction-area", "", _wkt_box(601490, 241490, 601520, 241520))],
          ["dataset", "end-date", "geometry"])
    write("listed-building-outline", [("listed-building-outline", "", _wkt_box(600098, 240098, 600142, 240132))],
          ["dataset", "end-date", "geometry"])
    write("listed-building", [("listed-building", "", "II*", _wkt_point(600128, 240123)), ("listed-building", "", "I", _wkt_point(601900, 241900))],
          ["dataset", "end-date", "listed-building-grade", "point"])
    return [f"{n}.csv" for n in ("conservation-area", "listed-building-outline", "listed-building", "article-4-direction-area")]


def _flood(dest):
    rows = [("flood-risk-zone", "", 3, "Fluvial Models", _wkt_box(601480, 241480, 601530, 241530)),
            ("flood-risk-zone", "", 2, "Fluvial Models", _wkt_box(601400, 241400, 601600, 241600)),
            ("flood-risk-zone", "", 2, "Tidal Models", _wkt_box(600090, 240090, 600150, 240140))]
    pd.DataFrame(rows, columns=["dataset", "end-date", "flood-risk-level", "flood-risk-type", "geometry"]).to_csv(dest / "flood-risk-zone.csv", index=False)
    return ["flood-risk-zone.csv"]


def build_property_bronze(work):
    """Add the sources the property table needs (on top of build_bronze) to the bronze folder."""
    work = Path(work)
    _snapshot("ons_lsoa", _lsoa, work)
    _snapshot("iod2025", _iod, work)
    _snapshot("planning_data", _planning, work)
    _snapshot("ea_flood_zones", _flood, work)

# ---- energy performance certificates, as the register's bulk download looks: a zip of certificates (and a recommendations file that must be
# ignored) plus a loose file in the older lower-case, hyphenated spelling. The address and postcode fields are present, as they are in the real files.
EPC_HEADER = ["CERTIFICATE_NUMBER", "ADDRESS1", "POSTCODE", "CURRENT_ENERGY_RATING", "POTENTIAL_ENERGY_RATING", "CURRENT_ENERGY_EFFICIENCY",
              "POTENTIAL_ENERGY_EFFICIENCY", "PROPERTY_TYPE", "BUILT_FORM", "LODGEMENT_DATE", "TOTAL_FLOOR_AREA", "CONSTRUCTION_AGE_BAND",
              "TENURE", "MAIN_FUEL", "MAINS_GAS_FLAG", "NUMBER_HABITABLE_ROOMS", "WALLS_DESCRIPTION", "ROOF_DESCRIPTION", "MAINHEAT_DESCRIPTION",
              "CO2_EMISSIONS_CURRENT", "UPRN"]
EPC_ROWS = [
    # UPRN 1 has two certificates: the later one (2023) is the one that counts
    ["C-001-OLD", "1 Fire Lane", "TL1 9ZZ", "E", "C", "45", "70", "House", "Detached", "2015-03-02", "120.5", "England and Wales: 1950-1966",
     "owner-occupied", "mains gas (not community)", "Y", "5", "Solid brick, as built, no insulation (assumed)", "Pitched, 100 mm loft insulation",
     "Boiler and radiators, mains gas", "6.1", "100000000001"],
    ["C-001-NEW", "1 Fire Lane", "TL1 9ZZ", "C", "B", "71", "82", "House", "Detached", "2023-07-19", "120.5", "England and Wales: 1950-1966",
     "owner-occupied", "mains gas (not community)", "Y", "5", "Solid brick, as built, insulated (assumed)", "Pitched, 270 mm loft insulation",
     "Boiler and radiators, mains gas", "3.2", "100000000001"],
    ["C-021", "Elm Cottage", "TL1 1AA", "G", "D", "12", "55", "Bungalow", "Semi-Detached", "2019-11-05", "85.5", "England and Wales: before 1900",
     "rental (private)", "oil (not community)", "N", "4", "Cob, as built", "Thatched", "Boiler and radiators, oil", "11.4", "100000000021"],
    ["C-NOUPRN", "2 Nowhere Road", "TL1 1AA", "B", "A", "85", "90", "Flat", "Mid-Terrace", "2024-01-01", "50", "England and Wales: 2012 onwards",
     "", "", "", "2", "", "", "", "1.0", ""],                                           # no UPRN: cannot be linked, must be dropped
    ["C-UNKNOWN", "3 Far Away", "ZZ9 9ZZ", "D", "C", "60", "75", "House", "Terraced", "2020-02-02", "70", "England and Wales: 1983-1990",
     "", "", "", "3", "", "", "", "4.0", "100000000777"],                               # a UPRN that is not in the area: no row in the table
]
EPC_LOOSE_HEADER = ["certificate-number", "address1", "postcode", "current-energy-rating", "total-floor-area", "lodgement-datetime", "uprn"]
EPC_LOOSE_ROWS = [["C-011", "Oak School House", "TL1 1AA", "A", "300.25", "2025-05-05 10:11:12", "100000000011"]]


def add_epc_to_inbox():
    """Place the register's files in the (patched) EPC inbox, as you would after downloading them."""
    import csv
    import io
    inbox = paths.DATA / "inbox" / "epc_domestic"
    inbox.mkdir(parents=True)

    def text(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
        return buf.getvalue()

    with zipfile.ZipFile(inbox / "all-domestic-certificates.zip", "w") as z:
        z.writestr("certificates.csv", text(EPC_HEADER, EPC_ROWS))
        z.writestr("recommendations.csv", "LMK_KEY,IMPROVEMENT_ITEM\nC-001-NEW,1\n")
    (inbox / "older-format.csv").write_text(text(EPC_LOOSE_HEADER, EPC_LOOSE_ROWS), encoding="utf-8")
    return inbox
