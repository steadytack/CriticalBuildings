"""One deterministic text copy of the governed tables, so rule changes show up as a normal git diff.

governed/control.db is binary; reviewers read governed/control_dump.txt instead. It is regenerated on every save and
publish and is a read-only artefact: edit through the UI (or the store API), never by hand.

Format: one section per table, `## <table> (<n> rows)`, followed by the rows as CSV. Sections and rows are sorted.
"""
import io

import pandas as pd

from critical_buildings import paths

HEADER = ("# Read-only text copy of governed/control.db, written automatically when rules or settings are saved or published.\n"
          "# Do not edit: make changes in the UI (python -m critical_buildings ui). Regenerate with: python -m critical_buildings control dump\n")


def _role_rows(store):
    role = store.draft_snapshot()["role"]
    rows = [("setting", "min_area", role["min_area"])]
    rows += [("ancillary_osm_building", v, "") for v in sorted(role["ancillary"])]
    rows += [("primary_osm_building", v, "") for v in sorted(role["primary"])]
    rows += [("type_min_area", k, v) for k, v in sorted(role["per_type"].items())]
    return pd.DataFrame(rows, columns=["kind", "key", "value"])


def render(store):
    """The dump as text."""
    sections = [
        ("rule", store.table_df("rule", "rule_ref")),
        ("rule_tag", store.table_df("rule_tag", "id")),
        ("reference_mapping", store.table_df("reference_mapping", "sort_order, id")),
        ("role_settings", _role_rows(store)),
        ("override", store.table_df("override", "building_id")),
        ("rule_set_version", store.versions_df().sort_values("version_id")),
        ("change_log", store.table_df("change_log", "id")),
    ]
    out = io.StringIO()
    out.write(HEADER)
    for name, df in sections:
        out.write(f"\n## {name} ({len(df)} rows)\n")
        df.to_csv(out, index=False, lineterminator="\n")
    return out.getvalue()


def write_dumps(store, path=None):
    """Write the dump (default governed/control_dump.txt). Returns the path."""
    path = path or (paths.CONTROL / "control_dump.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(store))
    return path
