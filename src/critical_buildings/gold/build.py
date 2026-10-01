"""Gold stage: merge OS + OSM, group into sites, check against the registers, assign stable IDs and roles.

Pure function of (silver tables, rule set, human decisions + identity registries in the control store).
"""
import logging
from dataclasses import dataclass

import pandas as pd

from critical_buildings import paths
from critical_buildings.gold import assemble, changes as change_tracking, ids, merge, roles
from critical_buildings.gold import reference_match as refmatch
from critical_buildings.gold import sites as sitelib

log = logging.getLogger(__name__)


@dataclass
class GoldResult:
    df: pd.DataFrame            # one row per building (incl. excluded; filter on df.Excluded)
    sites_out: object           # GeoDataFrame, one row per site
    sites_df: object            # working site table (before output formatting)
    changes: object             # DataFrame of changes vs the previous run, or None on a first run
    new_ids: int
    ref_gaps: object
    ref_matches: object


def build(silver, rules, store, run_id, today, previous_buildings=None, area=paths.DEFAULT_AREA):
    """previous_buildings: the previous run's buildings table (strings), or None on a first run."""
    log.info("merging ...")
    recs = merge.merge(silver.osm, silver.os_, rules.priorities)
    recs, sites_df = sitelib.assign_sites(recs, silver.site_cands)
    log.info("matching authoritative sources (DfE / CQC / NHS ODS) ...")
    recs, sites_df, ref_gaps, ref_matches = refmatch.attach_reference(
        recs, sites_df, silver.ref, rules.tags, silver.boundary, silver.ref_all, silver.postcodes, area)
    log.info("  %s reference records: %s matched to sites, %s added as new buildings",
             len(silver.ref), ref_matches.ri.nunique(), len(ref_gaps))
    recs, new_ids = ids.assign_ids(recs, today, store, run_id)
    recs, sites_df = ids.assign_site_ids(recs, sites_df, today, store, run_id)
    recs = roles.assign_roles(recs, rules.role)
    df, sites_out = assemble.assemble(recs, sites_df, rules.label, today, store)
    changes = change_tracking.diff_runs(previous_buildings, df) if previous_buildings is not None else None
    return GoldResult(df, sites_out, sites_df, changes, new_ids, ref_gaps, ref_matches)
