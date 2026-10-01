"""Command line:  python -m critical_buildings <command>

    check                        validate rules, settings and data, in plain English
    fetch [--only osm,dfe ...]   download source data into new immutable bronze snapshots
    run                          build the dataset (silver -> gold -> publish)
    control init|dump|versions|import-overrides|publish    manage the rules store
"""
import argparse
import logging
import sys

from critical_buildings import paths


def _setup_logging(verbose=False):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")
    logging.getLogger("pyogrio").setLevel(logging.WARNING)


def cmd_check(args):
    from critical_buildings import pipeline
    problems = pipeline.check(args.area)
    if problems:
        print("Check FAILED:")
        for p in problems:
            print("  -", p)
        return 1
    print("All checks passed.")
    return 0


def cmd_fetch(args):
    from critical_buildings import bronze
    from critical_buildings.control.store import ControlStore
    rules = ControlStore().ruleset().require_valid()
    only = args.only.split(",") if args.only else None
    results = bronze.fetch_all(rules, args.area, only, args.dry_run)
    for name, res in results.items():
        print(f"{name}: {res['status']}" + (f" ({res['snapshot']})" if "snapshot" in res else ""))
    return 0


def cmd_run(args):
    from critical_buildings import pipeline
    problems = pipeline.check(args.area)
    if problems:
        print("Cannot run - fix these first:")
        for p in problems:
            print("  -", p)
        return 1
    result = pipeline.run(args.area, args.type, args.notes or "", not args.no_reuse)
    reused = " (silver reused)" if result["silver_reused"] else ""
    print(f"run {result['run_id']}: {result['buildings']} buildings in {result['sites']} sites{reused}")
    print(f"  -> {result['dest']}")
    return 0


def cmd_runs(args):
    from critical_buildings.control.store import ControlStore
    store = ControlStore()
    df = store.runs_df(args.area)
    cols = ["run_id", "run_type", "status", "started_at", "rule_version_label", "buildings", "sites", "notes"]
    print(df[cols].to_string(index=False) if len(df) else "No runs yet.")
    rel = store.releases_df()
    if len(rel):
        print()
        print("Releases:")
        print(rel[["release", "area", "run_id", "released_at", "released_by"]].to_string(index=False))
    return 0


def cmd_archive(args):
    from critical_buildings import retention
    from critical_buildings.control.store import ControlStore
    store = ControlStore()
    actions = retention.plan(store)
    if not actions:
        print("Nothing to archive: everything is within the retention policy.")
        return 0
    total = sum(a.bytes for a in actions)
    print(("Archiving" if args.apply else "DRY RUN - would act on") + f" {len(actions)} item(s), {total / 1e6:.0f} MB:")
    for a in actions:
        print(f"  {a.kind:7} {a.layer:6} {a.path.relative_to(a.path.parents[2] if a.layer != 'bronze' else a.path.parents[1])}  ({a.bytes / 1e6:.0f} MB) - {a.reason}")
    if args.apply:
        retention.apply(store, actions)
        print("done.")
    else:
        print("Nothing changed. Re-run with --apply to carry this out.")
    return 0


def cmd_release(args):
    from critical_buildings import runs
    from critical_buildings.control import dumps
    from critical_buildings.control.store import ControlError, ControlStore
    store = ControlStore()
    try:
        dest = runs.release(store, args.area, args.name, args.run, args.notes or "")
    except ControlError as e:
        print(e)
        return 1
    dumps.write_dumps(store)
    print("released ->", dest)
    return 0


def cmd_control(args):
    from pathlib import Path

    from critical_buildings.control import dumps, migrate
    from critical_buildings.control import ruleset as rs
    from critical_buildings.control.store import ControlError, ControlStore, current_user
    store = ControlStore()
    try:
        if args.action == "init":
            if not args.from_excel:
                print("control init needs --from-excel <rules workbook.xlsx>")
                return 2
            print("imported:", migrate.import_workbook(store, Path(args.from_excel)))
            if args.overrides:
                print("overrides:", migrate.import_overrides_csv(store, Path(args.overrides)))
            vid = store.publish(args.user or current_user(), "Initial import from the Excel workbook")
            dumps.write_dumps(store)
            print("published version", vid, rs.version_label(vid, store.get_version(vid)[1]))
        elif args.action == "dump":
            print("wrote:", ", ".join(dumps.write_dumps(store)))
        elif args.action == "versions":
            print(store.versions_df().to_string(index=False))
            print("unpublished changes:", store.has_unpublished_changes())
        elif args.action == "publish":
            vid = store.publish(args.user or current_user(), args.reason or "")
            dumps.write_dumps(store)
            print("published version", vid)
        elif args.action == "import-overrides":
            n = migrate.import_overrides_csv(store, Path(args.file), reason=args.reason or "Imported overrides")
            dumps.write_dumps(store)
            print(f"imported {n} overrides")
    except ControlError as e:
        print(e)
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="critical_buildings", description="Critical buildings pipeline")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--area", default=paths.DEFAULT_AREA)
        sp.set_defaults(fn=fn)
        return sp

    add("check", cmd_check, "validate rules, settings and data")
    f = add("fetch", cmd_fetch, "download source data into new bronze snapshots")
    f.add_argument("--only", help="comma-separated: osm,os,codepoint,dfe,cqc,nhs")
    f.add_argument("--dry-run", action="store_true", help="show what would be downloaded")
    r = add("run", cmd_run, "build the dataset into a new gold run folder")
    r.add_argument("--type", choices=["dev", "candidate"], default="dev")
    r.add_argument("--notes")
    r.add_argument("--no-reuse", action="store_true", help="rebuild silver even if a matching build is cached")
    add("runs", cmd_runs, "list runs and releases")
    ar = add("archive", cmd_archive, "archive/prune old data per the retention policy (dry run unless --apply)")
    ar.add_argument("--apply", action="store_true")
    rl = add("release", cmd_release, "publish a run as an immutable release")
    rl.add_argument("name", nargs="?", help="e.g. v2026.10.0 (default: next in the month)")
    rl.add_argument("--run", help="run id (default: the latest successful run)")
    rl.add_argument("--notes")
    c = sub.add_parser("control", help="manage the rules store")
    c.add_argument("action", choices=["init", "dump", "versions", "publish", "import-overrides"])
    c.add_argument("--from-excel")
    c.add_argument("--overrides")
    c.add_argument("--file")
    c.add_argument("--reason")
    c.add_argument("--user")
    c.set_defaults(fn=cmd_control)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
