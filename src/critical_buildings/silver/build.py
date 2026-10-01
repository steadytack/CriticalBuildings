"""Silver stage: conform and classify every source for an area.

Reads the latest bronze snapshots, clips to the area boundary, classifies OSM and OS features with the rule tags and
loads the authoritative registers (DfE / CQC / NHS ODS) mapped to building types. Pure function of
(bronze inputs, rule set, area) - the lifecycle layer caches it under a build key.
"""
import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field

import geopandas as gpd
import pandas as pd

from critical_buildings import paths
from critical_buildings.control.ruleset import content_hash
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


# ------------------------------------------------------------------ caching: one silver build per distinct input set
SILVER_SCHEMA = 1        # bump when the silver tables' meaning or shape changes, to invalidate cached builds


def silver_key(rules, area=paths.DEFAULT_AREA):
    """Same bronze inputs + same rule set + same area (+ same silver schema) = same silver build."""
    raw = f"{SILVER_SCHEMA}|{area}|{paths.bronze_key(area)}|{content_hash(rules.snapshot)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def classified_dir(area, key):
    return paths.SILVER / area / "classified" / key


def save(silver, d):
    tmp = d.parent / f"_tmp_{d.name}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    silver.osm.to_parquet(tmp / "osm.parquet")
    silver.os_.to_parquet(tmp / "os.parquet")
    silver.site_cands.to_parquet(tmp / "site_candidates.parquet")
    silver.ref_all.to_parquet(tmp / "reference_all.parquet")
    pc = pd.DataFrame([(k, v[0], v[1]) for k, v in silver.postcodes.items()], columns=["pc", "e", "n"])
    pc.to_parquet(tmp / "postcodes.parquet")
    (tmp / "_meta.json").write_text(json.dumps({"os_total": int(silver.os_total), "area": silver.area}), encoding="utf-8")
    shutil.rmtree(d, ignore_errors=True)
    tmp.rename(d)                                   # atomic publish: a half-written build is never visible


def load(d, area):
    meta = json.loads((d / "_meta.json").read_text(encoding="utf-8"))
    ref_all = gpd.read_parquet(d / "reference_all.parquet")
    pc = pd.read_parquet(d / "postcodes.parquet")
    return SilverTables(
        area, osm_geom.area_boundary(area), gpd.read_parquet(d / "osm.parquet"), gpd.read_parquet(d / "os.parquet"),
        meta["os_total"], gpd.read_parquet(d / "site_candidates.parquet"), ref_all,
        ref_all[~ref_all.building_type.isin(["IGNORE", "UNMAPPED"])].copy(), dict(zip(pc.pc, zip(pc.e, pc.n))))


def build_or_load(rules, area=paths.DEFAULT_AREA, reuse=True):
    """Returns (silver tables, build key, reused?)."""
    key = silver_key(rules, area)
    d = classified_dir(area, key)
    if reuse and (d / "_meta.json").exists():
        log.info("reusing silver build %s", key)
        return load(d, area), key, True
    silver = build(rules, area)
    save(silver, d)
    log.info("saved silver build %s", key)
    return silver, key, False
