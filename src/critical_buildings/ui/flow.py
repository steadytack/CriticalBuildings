"""The pipeline drawn as a graph (Graphviz DOT text for st.graphviz_chart), coloured by what the latest run did.

Pure functions, no Streamlit, so they can be tested.
"""
from datetime import date, datetime

PALETTE = {                      # fill, border (light fills so dark text stays readable in light and dark themes)
    "succeeded": ("#d7f0dc", "#2e7d32"),
    "running": ("#fff1c2", "#b7791f"),
    "failed": ("#fbd5d5", "#c62828"),
    "stale": ("#ffe8cc", "#d9730d"),
    None: ("#eceff1", "#90a4ae"),
}
CADENCE_DAYS = (("daily", 2), ("weekly", 9), ("monthly", 35), ("quarterly", 100), ("six-monthly", 190), ("annual", 380), ("yearly", 380))


def cadence_days(refresh):
    """How old a snapshot may be before it is overdue, from the catalogue's free-text `refresh` (None if unknown)."""
    text = (refresh or "").lower()
    for word, days in CADENCE_DAYS:
        if text.startswith(word):
            return days
    return None


def source_status(retrieved_at, refresh, today=None):
    """(age in days or None, 'stale' when older than its refresh cadence, else 'succeeded'; None if never fetched)."""
    if not retrieved_at:
        return None, None
    today = today or date.today()  # noqa: DTZ011 - local time is intended
    age = (today - datetime.fromisoformat(str(retrieved_at)).date()).days
    limit = cadence_days(refresh)
    return age, ("stale" if limit is not None and age > limit else "succeeded")


def _node(name, label, status, shape="box"):
    fill, border = PALETTE.get(status, PALETTE[None])
    label = label.replace('"', "'")
    return f'  {name} [label="{label}", shape={shape}, fillcolor="{fill}", color="{border}"];'


def fmt_seconds(s):
    if s is None:
        return ""
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.1f} min"


def stage_caption(cfg, row):
    """Lines under a stage's name: its layer, time and the headline fact it recorded."""
    parts = [cfg["layer"]]
    if row:
        detail = row.get("detail", {})
        if detail.get("seconds") is not None:
            parts.append(fmt_seconds(detail["seconds"]))
        if "silver cache" in detail:
            parts.append(f"cache {detail['silver cache']}")
    return " - ".join(parts)


def flow_dot(source_rows, stage_cfgs, stage_rows, releases=0, latest_release=None):
    """source_rows: [{id, name, age, status}]; stage_cfgs: settings `pipeline.stages`; stage_rows: {stage id: registry row with detail}."""
    out = ["digraph pipeline {", "  rankdir=LR; bgcolor=\"transparent\"; nodesep=0.25; ranksep=0.5;",
           '  node [style="rounded,filled", fontname="Helvetica", fontsize=11, penwidth=1.6, fontcolor="#1b1b1b"];',
           '  edge [color="#78909c"];']
    for s in source_rows:
        age = "never fetched" if s["age"] is None else f"{s['age']} days old"
        out.append(_node(f"src_{s['id']}", f"{s['name']}\\n{age}", s["status"]))
    prev = None
    for cfg in stage_cfgs:
        row = stage_rows.get(cfg["id"])
        status = row["status"] if row else None
        out.append(_node(f"st_{cfg['id']}", f"{cfg['label']}\\n{stage_caption(cfg, row)}", status))
        if cfg["id"] == "fetch":
            for s in source_rows:
                out.append(f"  src_{s['id']} -> st_fetch;")
        elif prev:
            out.append(f"  st_{prev} -> st_{cfg['id']};")
        prev = cfg["id"]
    rel = f"Releases\\n{releases} made" + (f"\\nlatest {latest_release}" if latest_release else "")
    out.append(_node("releases", rel, "succeeded" if releases else None, shape="folder"))
    if prev:
        out.append(f"  st_{prev} -> releases [style=dashed];")
    out.append("}")
    return "\n".join(out)


def lineage_dot(lin):
    """A run's inputs -> outputs graph from lineage.json: bronze snapshots, rule set, silver build, run, output files."""
    out = ["digraph lineage {", "  rankdir=LR; bgcolor=\"transparent\"; nodesep=0.2;",
           '  node [style="rounded,filled", fontname="Helvetica", fontsize=10, fontcolor="#1b1b1b", fillcolor="#eceff1", color="#90a4ae"];',
           '  edge [color="#78909c"];']
    for i, b in enumerate(lin["bronze"]["snapshots"]):
        out.append(f'  b{i} [label="{b["source"]}\\n{b["snapshot"]}", fillcolor="#e3f2fd", color="#1565c0"];')
        out.append(f"  b{i} -> silver;")
    out.append(f'  rules [label="Rule set\\n{lin["rules"]["label"]}", fillcolor="#f3e5f5", color="#7b1fa2"];')
    out.append("  rules -> silver;")
    out.append(f'  settings [label="Settings\\n{lin["config"]["settings_hash"]}", fillcolor="#f3e5f5", color="#7b1fa2"];')
    out.append("  settings -> silver;")
    out.append(f'  silver [label="Silver build\\n{lin["silver"]["key"]}\\n{"reused" if lin["silver"]["reused"] else "rebuilt"}"];')
    out.append(f'  gold [label="Run\\n{lin["run_id"]}\\n{lin["gold"]["buildings"]} buildings, {lin["gold"]["sites"]} sites", '
               'fillcolor="#d7f0dc", color="#2e7d32"];')
    out.append("  silver -> gold;")
    out.append("  decisions [label=\"Decisions and\\nID registries\", fillcolor=\"#f3e5f5\", color=\"#7b1fa2\"];")
    out.append("  decisions -> gold;")
    for i, f in enumerate(lin["outputs"]["files"]):
        out.append(f'  o{i} [label="{f["name"]}\\n{f["bytes"] // 1024} KB", fillcolor="#fff8e1", color="#f9a825"];')
        out.append(f"  gold -> o{i};")
    out.append("}")
    return "\n".join(out)
