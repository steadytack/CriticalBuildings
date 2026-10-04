"""Reference data tabs: rules, role settings, register mapping, review decisions, versions. Owned by the critical buildings
product (they edit its governed tables); the generic shell in app.py only knows that a product supplies tabs."""
import pandas as pd
import streamlit as st

from critical_buildings import paths, runs
from critical_buildings.control import dumps
from critical_buildings.control import rules as rs
from critical_buildings.control.store import ControlError
from critical_buildings.publish import suggest_action
from critical_buildings.ui import logic
from critical_buildings.ui.common import (
    CATEGORIES_HINT,
    bump,
    key,
    save_form,
    show_messages,
)


# ------------------------------------------------------------------------------------------------ rules tab
def tab_rules(s):
    snap = s.draft_snapshot()
    st.subheader("Rules")
    st.caption("A rule says: features carrying any of its **tags** are this **building type**, with this importance. "
               "If a feature matches several rules, the lowest **priority** number wins.")
    rules_df = pd.DataFrame(snap["rules"])
    sorted(rules_df.building_type)
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
                new_rule = {"rule_ref": int(ref), "primary_category": cat.strip(), "building_type": btype.strip(), "importance": imp,
                                "importance_driver": driver.strip(), "priority": int(prio), "active": 1, "notes": ""}
                new_tag = {"id": None, "rule_ref": int(ref), "source": src, "tag": tag.strip(), "active": 1, "notes": ""}
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
                s.save_overrides([{"building_id": r["Building ID"], "review_decision": r["Decision"], "building_type_override": r["Type override"],
                                       "exclude": "Y" if r["Exclude building"] else "", "exclude_site": "Y" if r["Exclude whole site"] else "",
                                       "manual_override_notes": r["Notes"]} for r in todo.to_dict("records")], user, reason)

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
        _, latest = s.get_version()
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


