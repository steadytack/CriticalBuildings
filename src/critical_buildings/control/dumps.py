"""Deterministic text dumps of the governed tables, so rule changes show up as normal git diffs.

control.db is binary; reviewers read governed/dumps/*.csv instead. The dumps are regenerated on every publish/save
and are read-only artefacts: edit through the UI (or the store API), never by hand.
"""
import json

import pandas as pd

from critical_buildings import paths


def write_dumps(store, out_dir=None):
    out = out_dir or (paths.CONTROL / "dumps")
    out.mkdir(parents=True, exist_ok=True)
    snap = store.draft_snapshot()

    def dump(name, df):
        df.to_csv(out / name, index=False, lineterminator="\n")

    dump("rule.csv", store.table_df("rule", "rule_ref"))
    dump("rule_tag.csv", store.table_df("rule_tag", "id"))
    dump("reference_mapping.csv", store.table_df("reference_mapping", "sort_order, id"))
    role = snap["role"]
    rows = [("setting", "min_area", role["min_area"])]
    rows += [("ancillary_osm_building", v, "") for v in sorted(role["ancillary"])]
    rows += [("primary_osm_building", v, "") for v in sorted(role["primary"])]
    rows += [("type_min_area", k, v) for k, v in sorted(role["per_type"].items())]
    dump("role_settings.csv", pd.DataFrame(rows, columns=["kind", "key", "value"]))
    dump("override.csv", store.table_df("override", "building_id"))
    v = store.versions_df().sort_values("version_id")
    dump("rule_set_version.csv", v)
    cl = store.table_df("change_log", "id")
    dump("change_log.csv", cl)
    (out / "README.md").write_text(
        "# governed/dumps\n\nRead-only text exports of `governed/control.db` so that changes can be reviewed as ordinary git diffs.\n"
        "Regenerated automatically on save/publish (`python -m critical_buildings control dump`). **Do not edit these files**: "
        "make changes in the rules UI (`python -m critical_buildings ui`).\n", encoding="utf-8")
    return sorted(p.name for p in out.glob("*.csv"))
