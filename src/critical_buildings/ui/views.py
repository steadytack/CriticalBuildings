"""The generic tabs: what the pipeline is, what it is doing, what it produced, where it came from.

Nothing here is specific to one dataset: it reads the stage list, source catalogue and product wording from settings.yml,
the run registry from the control store, and the files under data/.
"""
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from critical_buildings import (
    config,
    paths,
    pipeline,
    progress,
    publish,
    retention,
    runs,
    sources,
)
from critical_buildings import silver as silver_build
from critical_buildings.control.store import ControlError, current_user
from critical_buildings.locks import LockedError
from critical_buildings.ui import flow, jobs, logic
from critical_buildings.ui.common import bump, key
from critical_buildings.ui.common import store as common_store

ICONS = {"pending": "⚪", "running": "🔄", "succeeded": "✅", "failed": "❌"}


# ------------------------------------------------------------------------------------------------ shared helpers
def stage_cfgs():
    return list(config.get().pipeline.stages)


def source_rows(area):
    rows = []
    for sid, cat in config.load_sources().items():
        snap = paths.latest_snapshot(sid, area if cat.get("area_scoped") else None)
        retrieved = None
        if snap:
            retrieved = sources.read_manifest(paths.bronze_snapshot(sid, area if cat.get("area_scoped") else None))["retrieved_at"]
        age, status = flow.source_status(retrieved, cat.get("refresh"))
        rows.append({"id": sid, "name": cat["name"], "snapshot": snap, "age": age, "status": status, "refresh": cat.get("refresh", ""),
                     "licence": cat.get("licence", ""), "retrieved_at": retrieved})
    return rows



@st.cache_data(show_spinner="Drawing the map ...")
def qa_map_html(path, mtime, layer):
    """The QA map for a run, drawn on demand from its GeoPackage (it is no longer stored with every run).

    `mtime` is not used in the body: it is part of the cache key, so the map is redrawn when the file changes."""
    import tempfile

    import geopandas as gpd
    gdf = gpd.read_file(path, layer=layer)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "map.html"
        publish.write_map(gdf, out)
        return out.read_text(encoding="utf-8")


@st.cache_data(show_spinner=False)
def read_table(path, mtime):
    """A CSV as strings. `mtime` is part of the cache key only: the table is re-read when the file changes."""
    return pd.read_csv(path, dtype=str).fillna("")


def run_choices(s, area, only_ok=False):
    df = s.runs_df(area)
    if only_ok:
        df = df[df["status"] == "succeeded"]
    return df


def stage_table(summary, cfgs):
    labels = {c["id"]: c["label"] for c in cfgs}
    rows = [{"": ICONS.get(st_["status"], "⚪"), "Stage": labels.get(sid, sid), "Status": st_["status"],
             "Time": flow.fmt_seconds(st_["seconds"]), "Steps": len(st_["steps"])} for sid, st_ in summary["stages"].items() if sid in labels]
    return pd.DataFrame(rows)


def show_stage_detail(st_):
    """Notes (counts) and the timeline of steps for one stage of a run."""
    if st_["notes"]:
        st.markdown("\n".join(f"- {k}: **{v:,}**" if isinstance(v, int) else f"- {k}: **{v}**" for k, v in st_["notes"].items()))
    if st_["steps"]:
        st.markdown("**Steps**")
        for t, message in st_["steps"]:
            st.write(f"`{t:>6.1f} s`  {message}")
    if not st_["notes"] and not st_["steps"]:
        st.caption("Nothing recorded for this stage (a run from before stage tracking, or it has not started).")


def summary_from_registry(run_id, s, cfgs):
    """Per-stage view for a finished run: events when the log exists, else the registry rows alone."""
    summary = jobs.summarise_events(jobs.events_for(run_id), [c["id"] for c in cfgs if c.get("function")])
    if all(v["status"] == "pending" for v in summary["stages"].values()):          # no log: fall back to the registry
        for row in progress.stage_summary(run_id, s):
            if row["stage"] in summary["stages"]:
                summary["stages"][row["stage"]].update(status=row["status"], seconds=row["detail"].get("seconds"),
                                                       notes={k: v for k, v in row["detail"].items() if k != "seconds"})
    return summary


# ------------------------------------------------------------------------------------------------ Pipeline tab
def tab_pipeline(s, area):
    cfgs = stage_cfgs()
    st.subheader("What the pipeline is")
    st.caption("Sources feed the stages left to right. Colours show what the latest run did; amber sources are older than their refresh cadence.")
    runs_df = s.runs_df(area)
    latest = runs_df.iloc[0] if len(runs_df) else None
    stage_rows = {}
    if latest is not None:
        stage_rows = {r["stage"]: r for r in progress.stage_summary(latest["run_id"], s)}
    rel = s.releases_df()
    rel = rel[rel["area"] == area] if len(rel) else rel
    st.graphviz_chart(flow.flow_dot(source_rows(area), cfgs, stage_rows, releases=len(rel),
                                    latest_release=rel.iloc[0]["release"] if len(rel) else None), width="stretch")
    if latest is None:
        st.info("No runs yet. Use the **Run** tab to fetch the sources and build.")
        return
    ok = runs_df[runs_df["status"] == "succeeded"]
    c = st.columns(5)
    c[0].metric("Latest run", latest["status"])
    c[1].metric("Rule set", latest["rule_version_label"] or "-")
    c[2].metric(config.get().product.entity.title() + "s", f"{int(latest['buildings']):,}" if pd.notna(latest["buildings"]) else "-")
    c[3].metric("Sites", f"{int(latest['sites']):,}" if pd.notna(latest["sites"]) else "-")
    c[4].metric("Successful runs", len(ok))
    st.caption(f"Run {latest['run_id']}, started {latest['started_at']}")
    st.markdown("**Stage detail**")
    coded = [c_ for c_ in cfgs if c_["id"] in stage_rows or c_.get("function")]
    pick = st.selectbox("Stage", [c_["id"] for c_ in coded], format_func=lambda i: next(c_["label"] for c_ in cfgs if c_["id"] == i),
                        key="pipe_stage")
    cfg = next(c_ for c_ in cfgs if c_["id"] == pick)
    st.write(cfg["description"])
    summary = summary_from_registry(latest["run_id"], s, cfgs)
    if pick in summary["stages"]:
        show_stage_detail(summary["stages"][pick])
    elif pick == "fetch":
        st.dataframe(pd.DataFrame(source_rows(area))[["name", "snapshot", "retrieved_at", "age", "refresh", "licence"]],
                     hide_index=True, width="stretch")


# ------------------------------------------------------------------------------------------------ Run tab
def preflight(s, area):
    """Plain-English facts about what a Build would do now. Returns (problems, facts)."""
    problems = pipeline.check(area, store=s)
    facts = []
    try:
        rules = s.ruleset()          # raises ControlError when nothing is published yet
        facts.append(f"Rule set **{rules.label}** will be used" + (" (the draft has unpublished edits that will not be)." if s.has_unpublished_changes() else "."))
        ok = s.runs_df(area)
        ok = ok[ok["status"] == "succeeded"]
        if not problems:
            key_ = silver_build.silver_key(rules, area)
            cached = (paths.SILVER / area / "classified" / key_ / "_meta.json").exists()
            facts.append("The cleaned data can be **reused** (same sources, rules and settings as an earlier build)." if cached
                         else "The sources, rules or settings changed, so the cleaning stage will **rebuild** (a few minutes).")
            if len(ok):
                facts.append("Source data is **unchanged** since the last run." if ok.iloc[0]["bronze_key"] == paths.bronze_key(area)
                             else "Source data has **changed** since the last run.")
    except ControlError:
        problems = problems + ["Nothing is published yet."]
    return problems, facts


def tab_run(s, area):
    st.subheader("Fetch, build and release")
    problems, facts = preflight(s, area)
    for f in facts:
        st.write("- " + f)
    if problems:
        st.error("Cannot build yet:\n\n" + "\n".join(f"- {p}" for p in problems))
    job = jobs.latest_job()
    busy = bool(job and job["state"] == "running")
    if busy:
        st.warning(f"**{job['label']}** is running. Buttons are disabled until it ends; you can leave this page open or reload it.")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**1. Fetch sources**")
        st.caption("Downloads each source. A new snapshot is stored only if the data changed.")
        if st.button("Fetch", disabled=busy):
            jobs.start_job("Fetch", "fetch", "--area", area)
            st.rerun()
    with c2:
        st.markdown("**2. Build**")
        run_type = st.radio("Run type", ["dev", "candidate"], horizontal=True, help="dev = scratch; candidate = may be released")
        notes = st.text_input("Notes (optional)", key=key("run_notes"))
        if st.button("Build", type="primary", disabled=busy or bool(problems)):
            jobs.start_job("Build", "run", "--area", area, "--type", run_type, "--notes", notes)
            st.rerun()
    with c3:
        st.markdown("**3. Release**")
        st.caption("A permanent, checksum-verified copy of the current run to share.")
        name = st.text_input("Release name", value=runs.next_release_name(s), key=key("release_name"))
        rnotes = st.text_input("Release notes", key=key("release_notes"))
        if st.button("Release", disabled=busy or not runs.current_run_id(area)):
            jobs.start_job("Release", "release", name.strip(), "--area", area, "--notes", rnotes)
            st.rerun()
    st.divider()
    (st.fragment(live_panel, run_every=2) if busy else st.fragment(live_panel))(area)


def live_panel(area):
    """Progress of the latest job: stages, the current step, a bar when there is one, counts so far, and the output."""
    job = jobs.latest_job()
    if job is None:
        st.info("Nothing has been started from this page yet.")
        return
    cfgs = stage_cfgs()
    state_text = {"running": "running", "succeeded": "finished", "failed": "FAILED", "interrupted": "stopped unexpectedly"}[job["state"]]
    st.markdown(f"**{job['label']}** {state_text}  -  started {job['started'].replace('T', ' ')}")
    if job["args"] and job["args"][0] == "run":
        run_id = jobs.run_id_of(job)
        if job["state"] == "interrupted" and run_id and runs.abandon(common_store(), area, run_id):
            st.warning("The build stopped unexpectedly (for example the PC slept or the process was closed). It was marked failed and its "
                       "half-written folder removed; start it again.")
        summary = jobs.summarise_events(jobs.events_for(run_id), [c["id"] for c in cfgs if c.get("function")])
        st.dataframe(stage_table(summary, cfgs), hide_index=True, width="stretch")
        if job["state"] == "running" and summary["current_step"]:
            since = (datetime.now() - datetime.fromisoformat(job["started"])).total_seconds()  # noqa: DTZ005 - local time is intended
            st.info(f"Now: {summary['current_step']}   (build running for {flow.fmt_seconds(since)})")
            if summary["bar"]:
                done, total = summary["bar"]
                st.progress(min(done / max(total, 1), 1.0), text=f"{done:,} of {total:,}")
        for w in summary["warnings"]:
            st.warning(w)
        for sid, st_ in summary["stages"].items():
            if st_["status"] != "pending":
                with st.expander(f"{ICONS[st_['status']]} {next((c['label'] for c in cfgs if c['id'] == sid), sid)}",
                                 expanded=(st_["status"] == "running")):
                    show_stage_detail(st_)
    with st.expander("Command output", expanded=job["state"] in ("failed", "interrupted")):
        st.code(job["output"] or "(nothing printed yet)")
    seen = st.session_state.get("_job_seen")
    if job["state"] == "running":
        st.session_state["_job_seen"] = job["id"]
    elif seen == job["id"]:                                    # it just ended: refresh the rest of the page once
        st.session_state["_job_seen"] = None
        bump()
        st.rerun()


# ------------------------------------------------------------------------------------------------ Runs tab
def tab_runs(s, area):
    st.subheader("Runs and releases")
    df = s.runs_df(area)
    for w in logic.sanity_problems(df):
        st.warning(w)
    if df.empty:
        st.info("No runs yet.")
        return
    cols = ["run_id", "run_type", "status", "started_at", "rule_version_label", "buildings", "sites", "notes"]
    st.dataframe(df[cols], hide_index=True, width="stretch")
    ok = df[df["status"] == "succeeded"].sort_values("started_at")
    if len(ok) > 1:
        st.markdown("**Size over time**")
        trend = ok.set_index("started_at")[["buildings", "sites"]].astype(int)
        st.line_chart(trend)
    st.markdown("**One run in detail**")
    rid = st.selectbox("Run", list(df["run_id"]), key=f"runs_pick_{df.iloc[0]['run_id']}")
    row = df[df["run_id"] == rid].iloc[0]
    cfgs = stage_cfgs()
    summary = summary_from_registry(rid, s, cfgs)
    st.dataframe(stage_table(summary, cfgs), hide_index=True, width="stretch")
    for sid, st_ in summary["stages"].items():
        if st_["notes"] or st_["steps"]:
            with st.expander(next((c["label"] for c in cfgs if c["id"] == sid), sid)):
                show_stage_detail(st_)
    if row["error"]:
        st.error(row["error"])
    ch = s.run_changes_df(rid)
    if len(ch):
        st.markdown(f"**Changes against the previous run** ({len(ch)})")
        st.dataframe(ch, hide_index=True, width="stretch")
        st.bar_chart(ch["change"].value_counts())
    rel = s.releases_df()
    if len(rel):
        st.markdown("**Releases**")
        st.dataframe(rel[["release", "area", "run_id", "released_at", "released_by", "notes"]], hide_index=True, width="stretch")
    st.markdown("**Archive and tidy up**")
    plan = retention.plan(s)
    if not plan:
        st.success("Everything is within the retention policy (see `retention` in Settings). Nothing to archive.")
    else:
        st.dataframe(pd.DataFrame([{"Layer": a.layer, "Action": a.kind, "Item": a.path.name, "Why": a.reason,
                                    "MB": round(a.bytes / 1e6, 1)} for a in plan]), hide_index=True, width="stretch")
        st.caption("Nothing a release depends on is ever touched. Archived runs and snapshots can be restored with the `restore` command.")
        if st.button("Apply this plan", key=key("apply_plan")):
            try:
                retention.apply(s, plan)
            except LockedError as e:
                st.error(str(e))
            else:
                bump()
                st.rerun()


# ------------------------------------------------------------------------------------------------ Data tab
def human(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def folder_size(p):
    return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file()) if Path(p).exists() else 0


def tab_data(s, area):
    st.subheader("What is on disk")
    layer = st.radio("Layer", ["Sources (bronze)", "Cleaned (silver)", "Runs (gold)", "Releases"], horizontal=True, key="data_layer")
    if layer.startswith("Sources"):
        rows = []
        for d in sorted(p for p in paths.BRONZE.glob("*") if p.is_dir()):
            for snap in sorted(p for p in d.iterdir() if p.is_dir()):
                latest = paths.latest_snapshot(d.name.split("__")[0], area if "__" in d.name else None) == snap.name
                rows.append({"Source": d.name, "Snapshot": snap.name, "Latest": "yes" if latest else "", "Size": human(folder_size(snap))})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        pick = st.selectbox("Show a manifest", [f"{r['Source']}/{r['Snapshot']}" for r in rows], key="data_manifest") if rows else None
        if pick:
            st.json(sources.read_manifest(paths.BRONZE / pick))
        return
    if layer.startswith("Cleaned"):
        rows = []
        for kind in ("conformed", "classified"):
            for d in sorted((paths.SILVER / area / kind).glob("*")):
                rows.append({"Kind": kind, "Key": d.name, "Size": human(folder_size(d)), "Modified": datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})  # noqa: DTZ006 - local time is intended
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption("Cleaned data is rebuilt from the sources and rules on demand, so old builds can be deleted safely.")
        return
    base = paths.PUBLISHED / area if layer == "Releases" else paths.GOLD / area
    folders = sorted([p for p in base.glob("*") if p.is_dir()], reverse=True) if base.exists() else []
    if not folders:
        st.info("Nothing here yet.")
        return
    pick = st.selectbox("Folder", [f.name for f in folders], key="data_folder")
    folder = base / pick
    files = sorted(p for p in folder.iterdir() if p.is_file())
    st.dataframe(pd.DataFrame([{"File": f.name, "Size": human(f.stat().st_size)} for f in files]), hide_index=True, width="stretch")
    preview_file(folder, files)


def preview_file(folder, files):
    names = [f.name for f in files]
    if not names:
        return
    name = st.selectbox("Preview a file", names, key="data_file")
    path = folder / name
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = read_table(str(path), path.stat().st_mtime)
        text = st.text_input("Filter rows containing", key="data_filter")
        if text:
            df = df[df.apply(lambda col: col.str.contains(text, case=False, regex=False)).any(axis=1)]
        st.caption(f"{len(df):,} rows" + (" (first 500 shown)" if len(df) > 500 else ""))
        st.dataframe(df.head(500), hide_index=True, width="stretch")
    elif suffix == ".gpkg":
        import pyogrio
        layers = [name_ for name_, _ in pyogrio.list_layers(path)]
        layer = st.selectbox("Layer", layers, key="data_layer_pick")
        df = pyogrio.read_dataframe(path, layer=layer, read_geometry=False, max_features=500)
        st.dataframe(df, hide_index=True, width="stretch")
        if layer in ("buildings", "critical_buildings") and st.button("Draw the QA map (a few seconds)", key="draw_map"):
            import streamlit.components.v1 as components
            components.html(qa_map_html(str(path), path.stat().st_mtime, layer), height=650, scrolling=True)
    elif suffix == ".xlsx":
        sheets = pd.ExcelFile(path).sheet_names
        sheet = st.selectbox("Sheet", sheets, key="data_sheet")
        st.dataframe(pd.read_excel(path, sheet_name=sheet).head(500), hide_index=True, width="stretch")
    elif suffix == ".html":
        import streamlit.components.v1 as components
        components.html(path.read_text(encoding="utf-8"), height=650, scrolling=True)
    elif suffix in (".json", ".txt"):
        st.code(path.read_text(encoding="utf-8")[:20000], language="json" if suffix == ".json" else None)


# ------------------------------------------------------------------------------------------------ Lineage tab
def tab_lineage(s, area):
    st.subheader("Where a run came from")
    ok = run_choices(s, area, only_ok=True)
    if ok.empty:
        st.info("No successful run yet.")
        return
    rid = st.selectbox("Run", list(ok["run_id"]), key=f"lineage_run_{ok.iloc[0]['run_id']}")
    run_dir = runs.run_dir(area, rid)
    lin_path = run_dir / "lineage.json"
    if not lin_path.exists():
        st.warning("This run's folder is no longer on disk (archived or removed), so its lineage file is not available.")
        return
    lin = json.loads(lin_path.read_text(encoding="utf-8"))
    st.graphviz_chart(flow.lineage_dot(lin), width="stretch")
    c = st.columns(4)
    c[0].metric("Rule set", lin["rules"]["label"])
    c[1].metric("Code version", lin["git"]["commit"][:8] + (" (edited)" if lin["git"]["dirty"] else ""))
    c[2].metric("Source snapshots", len(lin["bronze"]["snapshots"]))
    c[3].metric("Silver build", "reused" if lin["silver"]["reused"] else "rebuilt")
    with st.expander("Input snapshots with checksums"):
        rows = [{"Source": b["source"], "Snapshot": b["snapshot"], "Retrieved": b.get("retrieved_at"), "Licence": b.get("licence", "")}
                for b in lin["bronze"]["snapshots"]]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.markdown("**Why is this record the way it is?**")
    st.caption(f"Type a {config.get().product.id_column} (for example BLDG-000123) or part of a name.")
    path = paths.find_output(run_dir, "buildings")
    if path is None:
        return
    df = read_table(str(path), path.stat().st_mtime)
    q = st.text_input("Look up", key="why_query").strip()
    if not q:
        return
    idc = config.get().product.id_column
    hit = df[(df[idc].str.upper() == q.upper()) | df["Name"].str.contains(q, case=False, regex=False)].head(5)
    if hit.empty:
        st.info("No match.")
        return
    why = ["Source", "Source Feature ID", "Match Method", "Confidence", "Review Reason", "All Matched Rule Refs", "Rule Notes",
           "Classification Note", "Site ID", "Site Method", "Building Role", "Building Role Reason", "Verification",
           "Verification Note", "Authoritative ID", "Authoritative Distance (m)", "Review Decision", "Manual Override Notes",
           "Rule Version", "Data Quality Score"]
    for _, r in hit.iterrows():
        with st.expander(f"{r[idc]}  {r['Name']}  ({r['Building Type']})", expanded=len(hit) == 1):
            st.dataframe(pd.DataFrame([{"Question": k, "Answer": r.get(k, "")} for k in why if r.get(k, "") != ""]), hide_index=True, width="stretch")
            refs = [x.strip() for x in str(r.get("All Matched Rule Refs", "")).replace(";", ",").split(",") if x.strip()]
            if refs:
                try:
                    snap = s.get_version(lin["rules"]["version_id"])[1]
                    rules = [x for x in snap["rules"] if str(x["rule_ref"]) in refs]
                    if rules:
                        st.markdown("Rules that fired (as published in that rule set):")
                        st.dataframe(pd.DataFrame(rules)[["rule_ref", "primary_category", "building_type", "importance", "priority"]],
                                     hide_index=True, width="stretch")
                except (ControlError, KeyError):
                    pass


# ------------------------------------------------------------------------------------------------ Quality tab
def tab_quality(s, area):
    st.subheader("What the latest run produced")
    ok = run_choices(s, area, only_ok=True)
    if ok.empty:
        st.info("No successful run yet.")
        return
    rid = st.selectbox("Run", list(ok["run_id"]), key=f"quality_run_{ok.iloc[0]['run_id']}")
    path = paths.find_output(runs.run_dir(area, rid), "buildings")
    if path is None:
        st.warning("This run's files are no longer on disk.")
        return
    df = read_table(str(path), path.stat().st_mtime)
    idc = config.get().product.id_column
    score = pd.to_numeric(df.get("Data Quality Score"), errors="coerce")
    c = st.columns(4)
    c[0].metric("Rows", f"{len(df):,}")
    c[1].metric("Sites", f"{df['Site ID'].nunique():,}")
    c[2].metric("Verified", f"{(df['Verification'].str.startswith('Verified')).mean():.0%}")
    c[3].metric("Mean quality score", f"{score.mean():.0f}")
    for title, col in (("By building type", "Building Type"), ("By source", "Source"), ("By verification", "Verification"),
                       ("By quality band", "Data Quality Band"), ("Review status", "Review Status")):
        if col in df:
            counts = df[col].replace("", "(blank)").value_counts().head(20)
            st.markdown(f"**{title}**")
            st.bar_chart(counts, horizontal=True)
    st.caption(f"Counts are rows of `{path.name}` ({idc} level).")


# ------------------------------------------------------------------------------------------------ Settings tab
def tab_settings(s):
    st.subheader("Settings (thresholds)")
    st.caption("Technical tuning values from settings.yml: how close two features must be to match, how the quality score is built, how long to keep old data. "
               "They change the output, so every change needs a reason and is recorded in the audit trail. Business rules live under Reference data.")
    items = config.editable_settings()
    sections = sorted({i["section"] for i in items}, key=[i["section"] for i in items].index)
    section = st.radio("Section", sections, horizontal=True, key="settings_section")
    new = {}
    for item in [i for i in items if i["section"] == section]:
        step = 0.05 if isinstance(item["value"], float) else 1
        new[(section, item["key"])] = st.number_input(item["key"], value=item["value"], step=step, help=item["help"] or None,
                                                      key=key(f"set_{section}_{item['key']}"))
    changed = [(k, i["value"], new[k]) for k in new for i in items if (i["section"], i["key"]) == k and new[k] != i["value"]]
    if changed:
        st.write("**Pending changes:** " + "; ".join(f"{k[0]}.{k[1]}: {old} -> {nv}" for k, old, nv in changed))
    reason = st.text_input("Why are you making this change? (required)", key=key("settings_reason"))
    if st.button("Save settings", type="primary", disabled=not changed):
        if not reason.strip():
            st.error("Please give a reason.")
        else:
            try:
                done = config.save_settings({k: nv for k, _, nv in changed})
                for sec, k, old, nv in done:
                    s.log_setting_change(st.session_state.get("user") or current_user(), f"{sec}.{k}", old, nv, reason)
                from critical_buildings.control import dumps
                dumps.write_dumps(s)
                st.success("Saved. The next run uses the new values.")
                bump()
                st.rerun()
            except (ValueError, ControlError) as e:
                st.error(f"Not saved: {e}")
