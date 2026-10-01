"""One-off migration (phase 1): move the flat pre-restructure data/ files into the bronze layer with manifests.

Already applied on 2026-10-01; kept for provenance. Not part of the package.
Files that are no longer needed (extracted copies, caches, diagnostics, a superseded CQC CSV) are NOT deleted:
they go to data/_pending_delete/ until the regression baseline has been re-checked.
"""
import datetime as dt
import shutil

from critical_buildings import paths
from critical_buildings.bronze import manifest

D = paths.DATA
AREA = paths.DEFAULT_AREA
OVERPASS = "https://overpass-api.de/api/interpreter"

# source, area, [(old name, new name, url)]
PLAN = [
    ("osm_overpass", AREA, [("osm_features.json", "osm_features.json", OVERPASS),
                            ("osm_area_buildings.json", "osm_area_buildings.json", OVERPASS),
                            ("suffolk_boundary.json", "area_boundary.json", OVERPASS)]),
    ("os_open_map_local", None, [(f"opmplc_essh_{t}.zip", f"opmplc_essh_{t}.zip",
                                  "https://api.os.uk/downloads/v1/products/OpenMapLocal/downloads?area=%s&format=ESRI%%C2%%AE+Shapefile&redirect" % t.upper())
                                 for t in ("tl", "tm", "tg")]),
    ("os_codepoint_open", None, [("codepo_gb.zip", "codepo_gb.zip",
                                  "https://api.os.uk/downloads/v1/products/CodePointOpen/downloads?area=GB&format=CSV&redirect")]),
    ("dfe_gias", None, [("gias_edubasealldata.csv", "edubasealldata.csv",
                         "https://ea-edubase-api-prod.azurewebsites.net/edubase/downloads/public/edubasealldata20261001.csv")]),
    ("cqc_hsca", None, [("cqc_active_locations.ods", "active_locations.ods",
                         "https://www.cqc.org.uk/system/files/2026-09/01_September_2026_HSCA_Active_Locations.ods")]),
    ("nhs_ods", None, [(f"ods_{r}.csv", f"{r}.csv",
                        f"https://www.odsdatasearchandexport.nhs.uk/api/getReport?report={r}") for r in ("ets", "etr", "epraccur")]),
]
PENDING = ["cqc_directory.csv", "unverified_diag.pkl", "cqc_hsca.pkl", "opmplc_tl", "opmplc_tm", "opmplc_tg", "codepo"]


def main():
    for source, area, files in PLAN:
        mtimes = [(D / old).stat().st_mtime for old, _, _ in files]
        when = dt.datetime.fromtimestamp(max(mtimes), dt.timezone.utc)
        snap = paths.bronze_source_dir(source, area) / when.date().isoformat()
        snap.mkdir(parents=True, exist_ok=True)
        for old, new, _ in files:
            shutil.move(str(D / old), str(snap / new))
        manifest.write_manifest(source, snap, [{"name": new, "url": url} for _, new, url in files], area=area,
                                retrieved_at=when.isoformat(timespec="seconds"),
                                note="Migrated from the flat data/ folder on 2026-10-01; retrieved_at is the file modified time.")
        manifest.set_latest(source, snap.name, area)
        print("bronze:", snap.relative_to(paths.ROOT))
    pending = D / "_pending_delete"
    pending.mkdir(exist_ok=True)
    for name in PENDING:
        if (D / name).exists():
            shutil.move(str(D / name), str(pending / name))
    # the clipped OS building footprints are a derived (silver) table, keyed by the bronze inputs
    cache = D / "os_buildings_suffolk.gpkg"
    if cache.exists():
        shutil.move(str(cache), str(paths.silver_conformed_dir(AREA) / "os_buildings.gpkg"))
    print("silver conformed:", paths.silver_conformed_dir(AREA).relative_to(paths.ROOT))


if __name__ == "__main__":
    main()
