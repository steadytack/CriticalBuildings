"""One-off (phase 4): fill building_registry.site_id from the pre-restructure baseline output.

Needed once, because Site ID continuity used to come from the previous run's output file."""
import pandas as pd

from critical_buildings import paths
from critical_buildings.control.store import ControlStore

base = pd.read_csv(paths.LEGACY / "baseline_2026-10-01" / "critical_buildings.csv", dtype=str)
store = ControlStore()
store.set_building_sites(dict(zip(base["Building ID"], base["Site ID"])))
print("backfilled site ids for", len(store.building_site_map()), "buildings")
