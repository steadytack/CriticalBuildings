"""Rules editor UI.   python -m critical_buildings ui

Edit the business rules (what counts as a fire station, importance, priorities), the role settings and the register
mapping. Edits go to a DRAFT; Publish freezes the draft into a numbered version that the next pipeline run uses.
Every change needs your name and a reason and is kept in the audit trail.
"""
import pandas as pd
import streamlit as st

from critical_buildings import paths, runs
from critical_buildings.control import dumps
from critical_buildings.control import ruleset as rs
from critical_buildings.control.store import ControlError, ControlStore, current_user
from critical_buildings.publish.qa_report import suggest_action
from critical_buildings.ui import logic

st.set_page_config(page_title="Critical Buildings - rules", page_icon=":building_construction:", layout="wide")

CATEGORIES_HINT = "e.g. Emergency, Care, Education, Transport, Government Buildings, Miscellaneous"


@st.cache_resource
def _store_path():
    return str(paths.CONTROL / "control.db")


def store():
    return ControlStore(_store_path())


def bump():
    st.session_state["v"] = st.session_state.get("v", 0) + 1


def key(name):
    return f"{name}_{st.session_state.get('v', 0)}"


def show_messages(errors, warnings, ok_text="No problems found."):
    if errors:
        st.error("**Fix these before saving:**\n\n" + "\n".join(f"- {e}" for e in errors))
    else:
        st.success(ok_text)
    if warnings:
        with st.expander(f"{len(warnings)} note(s)"):
            for w in warnings:
                st.write("- " + w)


def save_form(label, form_key, can_save, on_save):
    """A reason box + button. on_save(user, reason) does the work; any ControlError is shown, not raised."""
    with st.form(form_key):
        reason = st.text_input("Why are you making this change? (required)", key=key(form_key + "_reason"))
        submitted = st.form_submit_button(label, disabled=not can_save, type="primary")
    if submitted:
        if not reason.strip():
            st.error("Please say why you are making this change.")
            return
        try:
            on_save(st.session_state["user"], reason.strip())
        except ControlError as e:
            st.error(str(e))
            return
        dumps.write_dumps(store())
        bump()
        st.rerun()


# ------------------------------------------------------------------------------------------------ sidebar
def sidebar(s):
    st.sidebar.title("Critical Buildings")
    st.sidebar.caption("Rules editor")
    st.sidebar.text_input("Your name", value=current_user(), key="user",
                          help="Recorded against every change in the audit trail.")
    try:
        vid, snap = s.get_version()
        st.sidebar.metric("Published version (used by runs)", rs.version_label(vid, snap))
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
                    st.success(f"Published version {vid}. The next `python -m critical_buildings run` uses it.")
                    bump()
                except ControlError as e:
                    st.error(str(e))
    st.sidebar.caption(f"Store: {paths.CONTROL / 'control.db'}")


# ------------------------------------------------------------------------------------------------ rules tab
def tab_rules(s):
    snap = s.draft_snapshot()
    st.subheader("Rules")
    st.caption("A rule says: features carrying any of its **tags** are this **building type**, with this importance. "
               "If a feature matches several rules, the lowest **priority** number wins.")
    rules_df = pd.DataFrame(snap["rules"])
    types = sorted(rules_df.building_type)
    edited_rules = st.data_editor(
        rules_df, key=key("rules_grid"), hide_index=True, num_rows="fixed", width="stretch", disabled=["rule_ref"],
        column_config={
            "rule_ref": st.column_config.NumberColumn("Rule Ref", format="%d", width="small"),
            "primary_category": st.column_config.TextColumn("Primary Category", help=CATEGORIES_HINT, required=True),
            "building_type": st.column_config.TextColumn("Building Type", required=True),
            "importance": st.column_config.SelectboxColumn("Operational Importance", options=list(rs.IMPORTANCE), required=True),
            "importance_driver": st.column_config.TextColumn("Importance Driver", width="large", required=True),
            "priority": st.column_config.NumberColumn("Priority", format="%d", step=1, min_value=1, required=True,
                                                     help="Lower number wins when a feature matches several rules."),
            "active": st.column_config.CheckboxColumn("Active"),
            "notes": st.column_config.TextColumn("Notes", width="large"),
        })
    rules_rows = logic.rows_from_frame(edited_rules, ints=("rule_ref", "priority"), bools=("active",),
                                       strings=("primary_category", "building_type", "importance_driver", "notes"))

    st.subheader("Tags")
    refs = [r["rule_ref"] for r in snap["rules"]]
    label = {r["rule_ref"]: f"{r['rule_ref']} - {r['building_type']}" for r in snap["rules"]}
    choice = st.selectbox("Show tags for", ["All rules"] + refs, format_func=lambda x: x if x == "All rules" else label[x],
                          key=key("tag_filter"))
    tags_df = pd.DataFrame(snap["tags"])
    visible = tags_df if choice == "All rules" else tags_df[tags_df.rule_ref == choice]
    st.caption("**OSM**: `key=value` with no spaces (e.g. `amenity=fire_station`; `key=*` = any value).  "
               "**OS**: `Theme > Classification` (e.g. `Emergency Service > Fire Station`).  **MANUAL**: no automatic tag. "
               "Add rows at the bottom of the table; delete rows by selecting them.")
    edited_tags = st.data_editor(
        visible, key=key(f"tags_grid_{choice}"), hide_index=True, num_rows="dynamic", width="stretch", disabled=["id"],
        column_config={
            "id": st.column_config.NumberColumn("Tag #", format="%d", width="small"),
            "rule_ref": st.column_config.SelectboxColumn("Rule Ref", options=refs, required=True, width="small"),
            "source": st.column_config.SelectboxColumn("Source", options=list(rs.SOURCES), required=True, width="small"),
            "tag": st.column_config.TextColumn("Tag", width="large"),
            "active": st.column_config.CheckboxColumn("Active"),
            "notes": st.column_config.TextColumn("Notes", width="large"),
        })
    shown_rows = logic.rows_from_frame(edited_tags, ints=("rule_ref",), bools=("active",), strings=("tag", "notes", "source"))
    hidden_rows = [] if choice == "All rules" else [dict(r, active=int(r["active"])) for r in snap["tags"] if r["rule_ref"] != choice]
    tags_rows = hidden_rows + shown_rows

    proposed = logic.propose(snap, rules=rules_rows, tags=tags_rows)
    errors, warnings = logic.validation_messages(proposed)
    errors = logic.row_problems(rules_rows, shown_rows) + errors
    current = [dict(r, active=int(r["active"])) for r in snap["rules"]], [dict(t, active=int(t["active"])) for t in snap["tags"]]
    changed = (rules_rows != [dict(r) for r in current[0]]) or (
        sorted(map(str, shown_rows)) != sorted(map(str, logic.rows_from_frame(visible, ints=("rule_ref",), bools=("active",), strings=("tag", "notes", "source")))))
    st.markdown("**Check of your edits**")
    show_messages(errors, warnings)
    if changed:
        st.info("**Your edits would change:**\n\n" + "\n".join(
            f"- {l}" for l in logic.diff_snapshots(snap, proposed)) or "no effective change")

    def save(user, reason):
        s.apply_table("rule", rules_rows, user, reason)
        s.apply_table("rule_tag", tags_rows, user, reason)

    save_form("Save to the draft", "save_rules", changed and not errors, save)

    with st.expander("Add a new rule"):
        with st.form("new_rule"):
            c1, c2, c3 = st.columns(3)
            ref = c1.number_input("Rule Ref", min_value=1, value=max(refs, default=0) + 1, step=1)
            cat = c2.text_input("Primary Category", help=CATEGORIES_HINT)
            btype = c3.text_input("Building Type")
            c4, c5, c6 = st.columns(3)
            imp = c4.selectbox("Operational Importance", list(rs.IMPORTANCE))
            prio = c5.number_input("Priority", min_value=1, value=50, step=1)
            driver = c6.text_input("Importance Driver")
            c7, c8 = st.columns([1, 3])
            src = c7.selectbox("First tag source", list(rs.SOURCES))
            tag = c8.text_input("First tag", help="OSM: key=value. OS: Theme > Classification. Leave blank for MANUAL.")
            why = st.text_input("Why add this rule? (required)")
            go = st.form_submit_button("Add rule")
        if go:
            if not (cat.strip() and btype.strip() and driver.strip() and why.strip()):
                st.error("Category, Building Type, Importance Driver and the reason are all required.")
            elif int(ref) in refs:
                st.error(f"Rule {int(ref)} already exists.")
            else:
                new_rule = dict(rule_ref=int(ref), primary_category=cat.strip(), building_type=btype.strip(), importance=imp,
                                importance_driver=driver.strip(), priority=int(prio), active=1, notes="")
                new_tag = dict(id=None, rule_ref=int(ref), source=src, tag=tag.strip(), active=1, notes="")
                try:
                    s.apply_table("rule", [dict(r, active=int(r["active"])) for r in snap["rules"]] + [new_rule], st.session_state["user"], why.strip())
                    s.apply_table("rule_tag", [dict(t, active=int(t["active"])) for t in snap["tags"]] + [new_tag], st.session_state["user"], why.strip())
                    dumps.write_dumps(s)
                    bump()
                    st.rerun()
                except ControlError as e:
                    st.error(str(e))

    with st.expander("Delete a rule"):
        victim = st.selectbox("Rule to delete", refs, format_func=lambda r: label[r], key=key("victim"))
        st.warning("This also deletes the rule's tags. It only affects the draft until you publish, and it is recorded in the audit trail. "
                   "To stop using a rule without losing it, untick **Active** instead.")
        sure = st.checkbox("I understand", key=key("sure"))
        why = st.text_input("Why delete it? (required)", key=key("delwhy"))
        if st.button("Delete rule", disabled=not (sure and why.strip())):
            s.apply_table("rule", [dict(r, active=int(r["active"])) for r in snap["rules"] if r["rule_ref"] != victim],
                          st.session_state["user"], why.strip())
            dumps.write_dumps(s)
            bump()
            st.rerun()


# ------------------------------------------------------------------------------------------------ role settings tab
def tab_roles(s):
    snap = s.draft_snapshot()
    role = snap["role"]
    st.subheader("Building role settings")
    st.caption("Each building at a multi-building site is labelled **Primary** (a main building) or **Ancillary** "
               "(an outbuilding). Footprint size decides, unless OpenStreetMap's `building=` value says otherwise.")
    min_area = st.number_input("Minimum primary footprint (m²)", min_value=1.0, value=float(role["min_area"]), step=10.0,
                               key=key("min_area"), help="At least this big = Primary, smaller = Ancillary.")
    c1, c2 = st.columns(2)
    anc = c1.text_area("Always ANCILLARY when OSM building= is (one per line)", "\n".join(role["ancillary"]), height=200, key=key("anc"))
    pri = c2.text_area("Always PRIMARY when OSM building= is (one per line)", "\n".join(role["primary"]), height=200, key=key("pri"))
    types = sorted({r["building_type"] for r in snap["rules"]})
    per_df = pd.DataFrame({"building_type": types, "min_area": [role["per_type"].get(t) for t in types]})
    st.markdown("**Optional: a different minimum for one building type** (blank = use the default above)")
    per_edit = st.data_editor(per_df, key=key("pertype"), hide_index=True, disabled=["building_type"], width="stretch",
                              column_config={"building_type": st.column_config.TextColumn("Building Type"),
                                             "min_area": st.column_config.NumberColumn("Minimum footprint (m²)", min_value=1.0)})
    new_role = {"min_area": float(min_area),
                "ancillary": sorted({x.strip() for x in anc.splitlines() if x.strip()}),
                "primary": sorted({x.strip() for x in pri.splitlines() if x.strip()}),
                "per_type": {r["building_type"]: float(r["min_area"]) for r in per_edit.to_dict("records") if not pd.isna(r["min_area"])}}
    errors, _ = logic.validation_messages(logic.propose(snap, role=new_role))
    errors = [e for e in errors if e.startswith("Role settings")]
    changed = new_role != {"min_area": float(role["min_area"]), "ancillary": sorted(role["ancillary"]),
                           "primary": sorted(role["primary"]), "per_type": {k: float(v) for k, v in role["per_type"].items()}}
    show_messages(errors, [])
    if changed and not errors:
        st.info("**Your edits would change:**\n\n" + "\n".join(f"- {l}" for l in logic.diff_snapshots(snap, logic.propose(snap, role=new_role))))
    save_form("Save to the draft", "save_role", changed and not errors,
              lambda u, r: s.set_role_settings(new_role["min_area"], new_role["ancillary"], new_role["primary"], new_role["per_type"], u, r))


# ------------------------------------------------------------------------------------------------ reference mapping tab
def tab_mapping(s):
    snap = s.draft_snapshot()
    st.subheader("Register mapping (DfE schools, CQC care and health, NHS ODS)")
    st.caption("Decides which of your building types each authoritative-register record becomes. Rows are checked top to bottom "
               "within a source; the **first match wins**. Building Type `IGNORE` drops the record. "
               "Fields are the register's own column names.")
    df = pd.DataFrame(snap["reference_mapping"])[["id", "sort_order", "source", "field", "match", "value", "building_type", "active", "notes"]]
    types = sorted({r["building_type"] for r in snap["rules"]}) + ["IGNORE"]
    edited = st.data_editor(
        df, key=key("map_grid"), hide_index=True, num_rows="dynamic", width="stretch", disabled=["id"],
        column_config={
            "id": None,
            "sort_order": st.column_config.NumberColumn("Order", format="%d", step=1, width="small", required=True),
            "source": st.column_config.SelectboxColumn("Source", options=list(rs.MAPPING_SOURCES), required=True, width="small"),
            "field": st.column_config.TextColumn("Field", width="medium", required=True),
            "match": st.column_config.SelectboxColumn("Match", options=["Contains", "Equals"], required=True, width="small"),
            "value": st.column_config.TextColumn("Value", width="medium", required=True),
            "building_type": st.column_config.SelectboxColumn("Building Type", options=types, required=True, width="medium"),
            "active": st.column_config.CheckboxColumn("Active"),
            "notes": st.column_config.TextColumn("Notes", width="large"),
        })
    rows = logic.rows_from_frame(edited, ints=("sort_order",), bools=("active",), strings=("source", "field", "match", "value", "building_type", "notes"))
    errors, _ = logic.validation_messages(logic.propose(snap, mapping=rows))
    errors = [e for e in errors if e.startswith("Reference mapping")]
    before = [dict(r, active=int(r["active"])) for r in snap["reference_mapping"]]
    changed = rows != logic.rows_from_frame(df, ints=("sort_order",), bools=("active",), strings=("source", "field", "match", "value", "building_type", "notes"))
    show_messages(errors, [])
    save_form("Save to the draft", "save_map", changed and not errors, lambda u, r: s.apply_table("reference_mapping", rows, u, r))
    assert before is not None


# ------------------------------------------------------------------------------------------------ review / decisions tab
def tab_review(s):
    st.subheader("Review queue and decisions")
    area = paths.DEFAULT_AREA
    rid, df = runs.load_previous(area)
    if df is None:
        st.info("No pipeline run found yet. Run `python -m critical_buildings run`, then come back to review its flagged records.")
    else:
        open_ = df[df["Review Status"] == "Open"]
        st.caption(f"From run **{rid}**. {len(open_)} open item(s). Decisions are saved against the building and survive reruns; "
                   "they take effect on the next run.")
        if open_.empty:
            st.success("Nothing is waiting for review.")
        else:
            types = sorted({r["building_type"] for r in s.draft_snapshot()["rules"]})
            grid = pd.DataFrame({
                "Building ID": open_["Building ID"], "Name": open_["Name"], "Building Type": open_["Building Type"],
                "Why flagged": open_["Review Reason"], "Suggested action": open_["Review Reason"].map(suggest_action),
                "Decision": "", "Type override": "", "Exclude building": False, "Exclude whole site": False, "Notes": ""}).reset_index(drop=True)
            edited = st.data_editor(
                grid, key=key("review_grid"), hide_index=True, num_rows="fixed", width="stretch",
                disabled=["Building ID", "Name", "Building Type", "Why flagged", "Suggested action"],
                column_config={
                    "Decision": st.column_config.SelectboxColumn("Decision", options=["", "Accepted"]),
                    "Type override": st.column_config.SelectboxColumn("Building Type override", options=[""] + types),
                    "Exclude building": st.column_config.CheckboxColumn("Exclude building"),
                    "Exclude whole site": st.column_config.CheckboxColumn("Exclude whole site"),
                })
            todo = edited[(edited.Decision != "") | (edited["Type override"] != "") | edited["Exclude building"] | edited["Exclude whole site"]]
            st.write(f"{len(todo)} decision(s) ready to save.")

            def save(user, reason):
                s.save_overrides([dict(building_id=r["Building ID"], review_decision=r["Decision"], building_type_override=r["Type override"],
                                       exclude="Y" if r["Exclude building"] else "", exclude_site="Y" if r["Exclude whole site"] else "",
                                       manual_override_notes=r["Notes"]) for r in todo.to_dict("records")], user, reason)

            save_form("Save decisions", "save_review", len(todo) > 0, save)

    st.markdown("**All recorded decisions**")
    ov = s.table_df("override", "building_id")
    if ov.empty:
        st.caption("None yet.")
        return
    st.dataframe(ov, hide_index=True, width="stretch")
    gone = st.multiselect("Withdraw decisions for", list(ov.building_id), key=key("withdraw"))
    why = st.text_input("Why withdraw? (required)", key=key("withdraw_why"))
    if st.button("Withdraw selected", disabled=not (gone and why.strip())):
        for b in gone:
            s.delete_override(b, st.session_state["user"], why.strip())
        dumps.write_dumps(s)
        bump()
        st.rerun()


# ------------------------------------------------------------------------------------------------ history tab
def tab_history(s):
    st.subheader("Versions and history")
    snap = s.draft_snapshot()
    try:
        vid, latest = s.get_version()
        if s.has_unpublished_changes():
            st.markdown("**Unpublished draft changes (compared with the published version):**")
            for line in logic.diff_snapshots(latest, snap) or ["(no effective differences)"]:
                st.write("- " + line)
    except ControlError:
        st.info("Nothing published yet.")
    versions = s.versions_df()
    if versions.empty:
        return
    st.markdown("**Published versions**")
    st.dataframe(versions, hide_index=True, width="stretch")
    ids = list(versions.version_id)
    if len(ids) >= 2:
        c1, c2 = st.columns(2)
        a = c1.selectbox("From version", ids, index=1, key=key("vfrom"))
        b = c2.selectbox("To version", ids, index=0, key=key("vto"))
        lines = logic.diff_snapshots(s.get_version(a)[1], s.get_version(b)[1])
        st.write("**What changed:**" if lines else "No differences.")
        for line in lines:
            st.write("- " + line)
    st.markdown("**Audit trail** (latest 300 changes)")
    log = s.change_log_df(300)
    st.dataframe(log[["ts", "user", "table_name", "key", "action", "reason"]], hide_index=True, width="stretch")


def tab_help():
    st.subheader("How this works")
    st.markdown("""
1. **Edit** rules, role settings or the register mapping. Edits go to a **draft**; nothing changes for runs yet.
2. **Save** needs your name (sidebar) and a reason. Every change is recorded in the audit trail.
3. **Publish** (sidebar) freezes the draft as a numbered, immutable **version**. Runs always use a published version, so any past run can be reproduced.
4. **Run** the pipeline: `python -m critical_buildings run`. The output records which rule version made it.

**Reviewing a change in git:** every save and publish rewrites plain-text copies in `control/dumps/`, so a rule change shows up as a normal diff.

**Tag formats**: OSM `key=value` (no spaces; `key=*` means any value) - OS `Theme > Classification` - MANUAL for no automatic tag.

**Priority**: when a feature matches several rules, the **lowest** number wins (50 = normal, 90 = generic fallback).

**Unticking Active** switches a rule or tag off without losing it.
""")


def main():
    s = store()
    sidebar(s)
    st.title("Critical Buildings - rules")
    tabs = st.tabs(["Rules", "Role settings", "Register mapping", "Review & decisions", "Versions & history", "Help"])
    with tabs[0]:
        tab_rules(s)
    with tabs[1]:
        tab_roles(s)
    with tabs[2]:
        tab_mapping(s)
    with tabs[3]:
        tab_review(s)
    with tabs[4]:
        tab_history(s)
    with tabs[5]:
        tab_help()


main()
