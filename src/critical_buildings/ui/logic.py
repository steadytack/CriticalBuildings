"""UI logic that has nothing to do with Streamlit (so it can be tested): grid <-> rows, proposed snapshots, plain-English diffs."""
import copy
import math

import pandas as pd

from critical_buildings.control import validate


def _missing(v):
    if v is None or v is pd.NA:
        return True
    return isinstance(v, float) and math.isnan(v)


def rows_from_frame(df, ints=(), bools=(), strings=()):
    """Edited grid -> list of row dicts. NaN -> None (or '' for string columns), ints/bools coerced, text stripped."""
    rows = []
    for rec in df.to_dict("records"):
        row = {}
        for k, v in rec.items():
            if k in strings and _missing(v):
                v = ""
            elif _missing(v):
                v = None
            elif k in ints:
                v = int(v)
            elif k in bools:
                v = 1 if bool(v) else 0
            elif isinstance(v, str):
                v = v.strip()
            row[k] = v
        rows.append(row)
    return rows


def snapshot_rows(rows, bools=("active",)):
    """DB-style rows (active 0/1) -> snapshot-style rows (active True/False)."""
    out = []
    for r in rows:
        r = dict(r)
        for b in bools:
            if b in r:
                r[b] = bool(r[b])
        out.append(r)
    return out


def propose(snapshot, rules=None, tags=None, mapping=None, role=None):
    """A copy of a snapshot with some parts replaced by (unsaved) edits, for live validation."""
    s = copy.deepcopy(snapshot)
    if rules is not None:
        s["rules"] = snapshot_rows(rules)
    if tags is not None:
        s["tags"] = snapshot_rows(tags)
    if mapping is not None:
        s["reference_mapping"] = snapshot_rows(mapping)
    if role is not None:
        s["role"] = role
    return s


def row_problems(rules_rows=None, tags_rows=None):
    """Problems the schema checks cannot see in a half-edited grid (blank keys, unknown rule numbers)."""
    problems = []
    if tags_rows is not None:
        refs = {r["rule_ref"] for r in rules_rows} if rules_rows is not None else None
        for i, t in enumerate(tags_rows, start=1):
            if t.get("rule_ref") is None:
                problems.append(f"Tag row {i}: choose which rule it belongs to (Rule Ref is blank).")
            elif refs is not None and t["rule_ref"] not in refs:
                problems.append(f"Tag row {i}: there is no rule {t['rule_ref']}.")
            if not str(t.get("source") or "").strip():
                problems.append(f"Tag row {i}: Source is blank (OSM, OS or MANUAL).")
    return problems


def validation_messages(proposed):
    """(errors, warnings) in plain English for a proposed snapshot."""
    return validate.check_all(proposed)


# ------------------------------------------------------------------ plain-English diff of two snapshots
def _rule_label(snap_rules, ref):
    r = snap_rules.get(ref)
    return f"Rule {ref} ({r['building_type']})" if r else f"Rule {ref}"


def _by(rows, key):
    return {r[key]: r for r in rows}


def diff_snapshots(old, new):
    """What changed from `old` to `new`, as short plain-English lines (empty list = identical)."""
    lines = []
    ro, rn = _by(old["rules"], "rule_ref"), _by(new["rules"], "rule_ref")
    for ref in sorted(set(rn) - set(ro)):
        lines.append(f"Added rule {ref}: {rn[ref]['building_type']} ({rn[ref]['primary_category']}, {rn[ref]['importance']})")
    for ref in sorted(set(ro) - set(rn)):
        lines.append(f"Removed rule {ref}: {ro[ref]['building_type']}")
    labels = {"primary_category": "category", "building_type": "building type", "importance": "importance",
              "importance_driver": "importance driver", "priority": "priority", "active": "active", "notes": "notes"}
    for ref in sorted(set(ro) & set(rn)):
        for k, label in labels.items():
            if ro[ref][k] != rn[ref][k]:
                lines.append(f"{_rule_label(rn, ref)}: {label} {ro[ref][k]!r} -> {rn[ref][k]!r}")
    to, tn = _by(old["tags"], "id"), _by(new["tags"], "id")
    for i in sorted(set(tn) - set(to)):
        lines.append(f"{_rule_label(rn, tn[i]['rule_ref'])}: added {tn[i]['source']} tag {tn[i]['tag'] or '(manual)'}")
    for i in sorted(set(to) - set(tn)):
        lines.append(f"{_rule_label(ro, to[i]['rule_ref'])}: removed {to[i]['source']} tag {to[i]['tag'] or '(manual)'}")
    for i in sorted(set(to) & set(tn)):
        for k, label in (("tag", "tag"), ("source", "source"), ("active", "active"), ("notes", "notes")):
            if to[i][k] != tn[i][k]:
                lines.append(f"{_rule_label(rn, tn[i]['rule_ref'])}: tag {to[i]['tag'] or '(manual)'}: {label} {to[i][k]!r} -> {tn[i][k]!r}")
    mo, mn = _by(old["reference_mapping"], "sort_order"), _by(new["reference_mapping"], "sort_order")
    key = lambda m: (m["source"], m["field"], m["match"], m["value"], m["building_type"], m["active"])      # noqa: E731
    so, sn = {key(m) for m in old["reference_mapping"]}, {key(m) for m in new["reference_mapping"]}
    for m in sorted(sn - so):
        lines.append(f"Reference mapping: added {m[0]} {m[1]} {m[2].lower()} '{m[3]}' -> {m[4]}" + ("" if m[5] else " (inactive)"))
    for m in sorted(so - sn):
        lines.append(f"Reference mapping: removed {m[0]} {m[1]} {m[2].lower()} '{m[3]}' -> {m[4]}")
    ro_, rn_ = old["role"], new["role"]
    if ro_["min_area"] != rn_["min_area"]:
        lines.append(f"Role settings: minimum primary footprint {ro_['min_area']:g} -> {rn_['min_area']:g} m2")
    for kind in ("ancillary", "primary"):
        for v in sorted(set(rn_[kind]) - set(ro_[kind])):
            lines.append(f"Role settings: added '{v}' to the {kind} OSM building values")
        for v in sorted(set(ro_[kind]) - set(rn_[kind])):
            lines.append(f"Role settings: removed '{v}' from the {kind} OSM building values")
    for bt in sorted(set(rn_["per_type"]) | set(ro_["per_type"])):
        a, b = ro_["per_type"].get(bt), rn_["per_type"].get(bt)
        if a != b:
            lines.append(f"Role settings: minimum footprint for {bt}: {a if a is not None else 'default'} -> {b if b is not None else 'default'}")
    return lines
