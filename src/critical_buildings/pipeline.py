"""Orchestrates one pipeline run:  rules check -> silver -> gold -> publish.

    from critical_buildings import pipeline
    pipeline.run(area="suffolk")
"""
import datetime as dt
import logging

from critical_buildings import config, paths, runs
from critical_buildings.control.store import ControlStore
from critical_buildings.gold import build as gold_build
from critical_buildings.publish import outputs
from critical_buildings.silver import build as silver_build

log = logging.getLogger(__name__)


def check(area=paths.DEFAULT_AREA, store=None, rules_version=None):
    """Everything that must be right before a run. Returns a list of plain-English problems (empty = ready)."""
    store = store or ControlStore()
    problems = list(config.validate())
    try:
        rules = store.ruleset(rules_version)
    except Exception as e:                                   # noqa: BLE001 - no published rule set yet
        return problems + [str(e)]
    problems += rules.errors
    for source in ("osm_overpass", "os_open_map_local", "os_codepoint_open", "dfe_gias", "cqc_hsca", "nhs_ods"):
        if not paths.latest_snapshot(source, area if source == "osm_overpass" else None):
            problems.append(f"No bronze data for '{source}'. Run: python -m critical_buildings fetch")
    return problems


def run(area=paths.DEFAULT_AREA, run_type="dev", notes="", reuse_silver=True, store=None, rules_version=None):
    """Build the area. Each run gets its own immutable gold folder and a row in the run registry.

    rules_version: a published rule set number to use instead of the latest (to reproduce an old run).
    Returns a summary dict. On failure the run is marked failed and no partial output is left behind."""
    store = store or ControlStore()
    rules = store.ruleset(rules_version).require_valid()
    for w in rules.warnings:
        log.info("note: %s", w)
    previous_run_id, previous = runs.load_previous(area)
    ctx = runs.start(store, area, run_type, rules, notes, previous_run_id)
    today = dt.date.today()
    try:
        silver, silver_key, reused = silver_build.build_or_load(rules, area, reuse_silver)
        gold = gold_build.build(silver, rules, store, ctx.run_id, today, previous, area)
        n_buildings, n_sites = outputs.write_outputs(gold, silver, ctx.dir, rules.label, today)
        runs.finish(store, ctx, rules, silver_key, reused, gold)
    except BaseException as exc:
        runs.fail(store, ctx, exc)
        raise
    log.info("run %s done: %s buildings in %s sites (%s new building IDs)", ctx.run_id, n_buildings, n_sites, gold.new_ids)
    return dict(run_id=ctx.run_id, buildings=n_buildings, sites=n_sites, new_ids=gold.new_ids, dest=ctx.dir,
                silver_reused=reused, previous_run_id=previous_run_id)
