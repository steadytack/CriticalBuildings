"""Silver stage: conform and classify every source for an area.

Reads the latest bronze snapshots, clips to the area boundary, classifies OSM and OS features with the rule tags and
loads the authoritative registers (DfE / CQC / NHS ODS) mapped to building types. Pure function of
(bronze inputs, rule set, area) - the lifecycle layer caches it under a build key.
"""
import logging
from dataclasses import dataclass, field

import geopandas as gpd
import pandas as pd

from critical_buildings import paths
from critical_buildings.silver import classify, osm_geom
from critical_buildings.silver import reference as ref_data

log = logging.getLogger(__name__)


@dataclass
class SilverTables:
    area: str
    boundary: object                 # shapely geometry (EPSG:27700)
    osm: gpd.GeoDataFrame            # classified OSM features
    os_: gpd.GeoDataFrame            # classified OS Important Buildings / stations
    os_total: int                    # all OS Important Buildings in the area (any type)
    site_cands: gpd.GeoDataFrame     # candidate site polygons (OS functional sites + OSM sites/areas)
    ref_all: gpd.GeoDataFrame        # every register record, incl. IGNORE / UNMAPPED (diagnostics, closed schools)
    ref: gpd.GeoDataFrame            # register records mapped to a building type
    postcodes: dict = field(default_factory=dict)   # postcode -> (easting, northing)


def build(rules, area=paths.DEFAULT_AREA):
    boundary = osm_geom.area_boundary(area)
    log.info("classifying ...")
    osm, os_, os_total, site_cands = classify.run(rules.tags, boundary, area)
    log.info("  OSM %s / OS %s classified", len(osm), len(os_))
    log.info("loading authoritative registers (DfE / CQC / NHS ODS) ...")
    ref_all = ref_data.load_reference(boundary, rules.mapping, keep_ignored=True, area=area)
    ref = ref_all[~ref_all.building_type.isin(["IGNORE", "UNMAPPED"])].copy()
    return SilverTables(area, boundary, osm, os_, os_total, site_cands, ref_all, ref, ref_data.postcode_points())
