"""Orchestrates one pipeline run:  rules check -> silver -> gold -> publish.

    from critical_buildings import pipeline
    pipeline.run(area="suffolk")
"""
import datetime as dt
import logging

import pandas as pd

from critical_buildings import config, paths
from critical_buildings.control.store import ControlStore
from critical_buildings.gold import build as gold_build
from critical_buildings.publish import outputs
from critical_buildings.silver import build as silver_build

log = logging.getLogger(__name__)


def new_run_id(area):
    return dt.datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + area


def check(area=paths.DEFAULT_AREA, store=None):
    """Everything that must be right before a run. Returns a list of plain-English problems (empty = ready)."""
    store = store or ControlStore()
    problems = list(config.validate())
    try:
        rules = store.ruleset()
    except Exception as e:                                   # noqa: BLE001 - no published rule set yet
        return problems + [str(e)]
    problems += rules.errors
    for source in ("osm_overpass", "os_open_map_local", "os_codepoint_open", "dfe_gias", "cqc_hsca", "nhs_ods"):
        if not paths.latest_snapshot(source, area if source == "osm_overpass" else None):
            problems.append(f"No bronze data for '{source}'. Run: python -m critical_buildings fetch")
    return problems


def run(area=paths.DEFAULT_AREA, dest=None, store=None):
    """Build the area and write the deliverables to `dest` (default: the transitional outputs/ folder)."""
    store = store or ControlStore()
    rules = store.ruleset().require_valid()
    for w in rules.warnings:
        log.info("note: %s", w)
    dest = dest or paths.OUTPUTS
    dest.mkdir(parents=True, exist_ok=True)
    run_id, today = new_run_id(area), dt.date.today()
    prev_csv = dest / "critical_buildings.csv"
    previous = pd.read_csv(prev_csv, dtype=str).fillna("") if prev_csv.exists() else None

    silver = silver_build.build(rules, area)
    gold = gold_build.build(silver, rules, store, run_id, today, previous, area)
    n_buildings, n_sites = outputs.write_outputs(gold, silver, dest, rules.label, today)
    log.info("done: %s buildings in %s sites (%s new building IDs) -> %s", n_buildings, n_sites, gold.new_ids, dest)
    return dict(run_id=run_id, buildings=n_buildings, sites=n_sites, new_ids=gold.new_ids, dest=dest)
