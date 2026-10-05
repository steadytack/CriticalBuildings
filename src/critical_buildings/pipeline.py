"""Orchestrates one pipeline run:  rules check -> silver -> gold -> publish.

    from critical_buildings import pipeline
    pipeline.run(area="suffolk")
"""
import datetime as dt
import importlib
import logging

from critical_buildings import config, locks, paths, progress, publish, runs, sources
from critical_buildings import silver as silver_build
from critical_buildings.control.store import ControlStore
from critical_buildings.errors import PipelineError
from critical_buildings.gold import steps as gold_steps

log = logging.getLogger(__name__)


def check(area=paths.DEFAULT_AREA, store=None, rules_version=None):
    """Everything that must be right before a run. Returns a list of plain-English problems (empty = ready)."""
    store = store or ControlStore()
    problems = list(config.validate())
    try:
        config.load_area(area)
    except PipelineError as e:                               # an unknown area: say so rather than list missing data
        return problems + [str(e)]
    try:
        rules = store.ruleset(rules_version)
    except Exception as e:                                   # noqa: BLE001 - no published rule set yet
        return problems + [str(e)]
    problems += rules.errors
    legacy = sources.legacy_unscoped_sources(area)
    for source in ("osm_overpass", "os_open_map_local", "os_codepoint_open", "dfe_gias", "cqc_hsca", "nhs_ods"):
        if source in legacy:
            problems.append(f"The '{source}' data is in the old shared folder. Move it to this area with: "
                            f"python -m critical_buildings migrate-bronze --area {area}")
        elif not paths.latest_snapshot(source, paths.scoped_area(source, area)):
            problems.append(f"No bronze data for '{source}'. Run: python -m critical_buildings fetch")
    return problems


def stages():
    """The configured stages of a run that have code (fetch is run separately)."""
    return [s for s in config.get().pipeline.stages if s.get("function")]


def silver_stage(state):
    state["silver"], state["silver_key"], state["silver_reused"] = silver_build.build_or_load(
        state["rules"], state["area"], state["reuse_silver"])


def gold_stage(state):
    state["gold"] = gold_steps.build(state["silver"], state["rules"], state["store"], state["ctx"].run_id, state["today"],
                                     state["previous"], state["area"])


def publish_stage(state):
    state["n_buildings"], state["n_sites"] = publish.write_outputs(state["gold"], state["silver"], state["ctx"].dir,
                                                                   state["rules"].label, state["today"])
    runs.finish(state["store"], state["ctx"], state["rules"], state["silver_key"], state["silver_reused"], state["gold"])


def run(area=paths.DEFAULT_AREA, run_type="dev", notes="", reuse_silver=True, store=None, rules_version=None):
    """Build the area. Each run gets its own immutable gold folder and a row in the run registry.

    rules_version: a published rule set number to use instead of the latest (to reproduce an old run).
    Returns a summary dict. On failure the run is marked failed and no partial output is left behind.
    Raises locks.LockedError if a fetch, run, archive or release is already in progress."""
    with locks.pipeline_lock("run"):
        return _run(area, run_type, notes, reuse_silver, store, rules_version)


def _run(area, run_type, notes, reuse_silver, store, rules_version):
    store = store or ControlStore()
    rules = store.ruleset(rules_version).require_valid()
    previous_run_id, previous = runs.load_previous(area)
    ctx = runs.start(store, area, run_type, rules, notes, previous_run_id)
    state = {"area": area, "rules": rules, "store": store, "ctx": ctx, "today": dt.date.today(), "previous": previous,  # noqa: DTZ011 - local time is intended
                 "reuse_silver": reuse_silver}
    try:
        with progress.run(ctx.run_id, store):
            for w in rules.warnings:
                progress.warn(w)
            for s in stages():
                with progress.stage(s["id"]):
                    module, _, name = s["function"].partition(":")
                    getattr(importlib.import_module(module), name)(state)
    except BaseException as exc:
        runs.fail(store, ctx, exc)
        raise
    progress.step(f"run {ctx.run_id} done: {state['n_buildings']} buildings in {state['n_sites']} sites "
                  f"({state['gold'].new_ids} new building IDs)")
    return {"run_id": ctx.run_id, "buildings": state["n_buildings"], "sites": state["n_sites"], "new_ids": state["gold"].new_ids,
                "dest": ctx.dir, "silver_reused": state["silver_reused"], "previous_run_id": previous_run_id}
