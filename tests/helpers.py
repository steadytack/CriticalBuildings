"""Small builders for synthetic test data (British National Grid metres)."""
import geopandas as gpd
from shapely.geometry import Point, box

OSM_COLS = ["source", "source_id", "name", "rule_ref", "category", "building_type", "importance", "driver",
            "matched_refs", "note", "rule_notes", "osm_building", "geometry"]


def feature(source, sid, name, rule_ref, category, building_type, geometry, importance="High", driver="d",
            note="", osm_building=""):
    """One classified OSM / OS feature row (the shape silver.classify produces)."""
    return dict(source=source, source_id=sid, name=name, rule_ref=rule_ref, category=category,
                building_type=building_type, importance=importance, driver=driver, matched_refs=str(rule_ref), note=note,
                rule_notes="", osm_building=osm_building, geometry=geometry)


def gdf(rows, cols=OSM_COLS):
    return gpd.GeoDataFrame(rows, columns=cols, geometry="geometry", crs=27700)


def square(x, y, side=20.0):
    """A square footprint with its lower-left corner at (x, y)."""
    return box(x, y, x + side, y + side)


def point(x, y):
    return Point(x, y)


def snapshot(**overrides):
    """A small valid rule set snapshot (what 'Publish' stores)."""
    snap = {
        "rules": [
            dict(rule_ref=1, primary_category="Emergency", building_type="Fire Station", importance="High",
                 importance_driver="Emergency response", priority=50, active=True, notes=""),
            dict(rule_ref=17, primary_category="Education", building_type="Primary School", importance="Medium",
                 importance_driver="Continuity of education", priority=50, active=True, notes=""),
            dict(rule_ref=20, primary_category="Education", building_type="School", importance="Medium",
                 importance_driver="Continuity of education", priority=90, active=True, notes=""),
            dict(rule_ref=34, primary_category="Miscellaneous", building_type="Commercially Important", importance="High",
                 importance_driver="Business critical", priority=50, active=True, notes=""),
        ],
        "tags": [
            dict(id=1, rule_ref=1, source="OSM", tag="amenity=fire_station", active=True, notes=""),
            dict(id=2, rule_ref=1, source="OS", tag="Emergency Service > Fire Station", active=True, notes="OS Open Map Local"),
            dict(id=3, rule_ref=17, source="OSM", tag="school=primary", active=True, notes=""),
            dict(id=4, rule_ref=17, source="OS", tag="Education > Primary Education", active=True, notes=""),
            dict(id=5, rule_ref=20, source="OSM", tag="amenity=music_school", active=True, notes=""),
            dict(id=6, rule_ref=34, source="MANUAL", tag="", active=True, notes=""),
        ],
        "role": {"min_area": 150.0, "ancillary": ["garage", "shed"], "primary": ["school"], "per_type": {}},
        "reference_mapping": [
            dict(sort_order=1, source="DfE", field="PhaseOfEducation (name)", match="Contains", value="primary",
                 building_type="Primary School", active=True, notes=""),
            dict(sort_order=2, source="CQC", field="Dormant (Y/N)", match="Equals", value="Y", building_type="IGNORE",
                 active=True, notes=""),
        ],
    }
    snap.update(overrides)
    return snap
