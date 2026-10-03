"""The pipeline UI.   python -m critical_buildings ui

A generic shell (what the pipeline is, what it is doing, what it produced, where it came from) plus the product's own
reference-data editors. Edits to rules go to a DRAFT; Publish freezes the draft into a numbered version that the next run
uses. Every change needs your name and a reason and is kept in the audit trail.
"""
import streamlit as st

from critical_buildings import config, paths
from critical_buildings.control import dumps
from critical_buildings.control import rules as rs
from critical_buildings.control.store import ControlError, current_user
from critical_buildings.ui import logic, rules_tabs, views
from critical_buildings.ui.common import bump, key, store

PRODUCT = config.get().product
st.set_page_config(page_title=PRODUCT.name, page_icon=":building_construction:", layout="wide")


def sidebar(s):
    st.sidebar.title(PRODUCT.name)
    st.sidebar.text_input("Your name", value=current_user(), key="user",
                          help="Recorded against every change in the audit trail.")
    areas = config.areas()
    area = st.sidebar.selectbox(PRODUCT.area_label, areas, index=areas.index(paths.DEFAULT_AREA), key="area")
    try:
        vid, snap = s.get_version()
        st.sidebar.metric("Published rule set (used by runs)", rs.version_label(vid, snap))
    except ControlError:
        st.sidebar.warning("Nothing published yet.")
    if s.has_unpublished_changes():
        st.sidebar.warning("The draft has changes that are **not published**. Runs still use the published version.")
    else:
        st.sidebar.success("Draft = published version.")
    errors, _ = logic.validation_messages(s.draft_snapshot())
    st.sidebar.write(f"Draft check: {'**%d problem(s)**' % len(errors) if errors else 'no problems'}")

    with st.sidebar.expander("Publish the draft", expanded=False):
        reason = st.text_input("Reason for this version", key=key("publish_reason"))
        approver = st.text_input("Approved by (optional)", key=key("publish_approver"))
        if st.button("Publish", type="primary", disabled=not s.has_unpublished_changes() or bool(errors)):
            if not reason.strip():
                st.error("Please give a reason.")
            else:
                try:
                    vid = s.publish(st.session_state["user"], reason.strip(), approver.strip())
                    dumps.write_dumps(s)
                    st.success(f"Published version {vid}. The next build uses it.")
                    bump()
                except ControlError as e:
                    st.error(str(e))
    st.sidebar.caption(f"Store: {paths.CONTROL / 'control.db'}")
    return area


def tab_help():
    st.subheader("How this works")
    stages = "\n".join(f"{i}. **{c['label']}** ({c['layer']}): {c['description']}" for i, c in enumerate(config.get().pipeline.stages, 1))
    st.markdown(f"""
**The pipeline** runs these stages in order. The **Pipeline** tab draws them and shows what the latest run did.

{stages}

**Tabs**

- **Pipeline**: the flow diagram, with source freshness and each stage's counts and steps.
- **Run**: fetch, build and release, with live progress while a build runs.
- **Runs**: history, size over time, one run in detail, changes against the previous run, archive and tidy up.
- **Data**: what is on disk in each layer, with previews of any file.
- **Lineage**: a run's inputs and outputs as a graph, and a "why is this record like this?" lookup.
- **Quality**: counts and charts for a run.
- **Settings**: the technical thresholds in settings.yml (each change needs a reason).
- **Reference data**: the editable business rules, role settings, register mapping and review decisions. Edits go to a **draft**;
  **Publish** (sidebar) freezes it as a numbered, immutable version. Runs always use a published version, so any past run can be reproduced.

Every save and publish rewrites a plain-text copy, `governed/control_dump.txt`, so a change shows up as an ordinary diff in git.
""")


def main():
    s = store()
    area = sidebar(s)
    st.title(PRODUCT.name)
    names = ["Pipeline", "Run", "Runs", "Data", "Lineage", "Quality", "Settings", "Reference data", "Help"]
    tabs = dict(zip(names, st.tabs(names)))
    with tabs["Pipeline"]:
        views.tab_pipeline(s, area)
    with tabs["Run"]:
        views.tab_run(s, area)
    with tabs["Runs"]:
        views.tab_runs(s, area)
    with tabs["Data"]:
        views.tab_data(s, area)
    with tabs["Lineage"]:
        views.tab_lineage(s, area)
    with tabs["Quality"]:
        views.tab_quality(s, area)
    with tabs["Settings"]:
        views.tab_settings(s)
    with tabs["Reference data"]:
        sub = dict(zip(["Rules", "Role settings", "Register mapping", "Review & decisions", "Versions & history"],
                       st.tabs(["Rules", "Role settings", "Register mapping", "Review & decisions", "Versions & history"])))
        with sub["Rules"]:
            rules_tabs.tab_rules(s)
        with sub["Role settings"]:
            rules_tabs.tab_roles(s)
        with sub["Register mapping"]:
            rules_tabs.tab_mapping(s)
        with sub["Review & decisions"]:
            rules_tabs.tab_review(s)
        with sub["Versions & history"]:
            rules_tabs.tab_history(s)
    with tabs["Help"]:
        tab_help()


main()
