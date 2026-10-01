"""Plain-English checks on a rule set snapshot. Used by the UI (drafts), by Publish, and by the pipeline's first step.

Same checks and wording as the old workbook-based rules check, now on the normalised model.
Each function returns messages; errors block saving/publishing, warnings are notes.
"""
import difflib
import re

from critical_buildings.control import ruleset as rs
from critical_buildings.control.ruleset import Tag


def check_rules(snapshot):
    """Returns (errors, warnings, tags). tags are the ACTIVE rule tags, in tag-id order (the order they were
    entered), which is the order the pipeline classifies in."""
    errors, warnings, tags = [], [], []
    rules = {r["rule_ref"]: r for r in snapshot["rules"]}
    by_rule = {}
    for t in snapshot["tags"]:
        by_rule.setdefault(t["rule_ref"], []).append(t)

    # rule-level checks (once per rule)
    for ref in sorted(rules):
        r = rules[ref]
        if not r["active"]:
            continue
        where = f"Rule {ref}"
        for label, val in (("Primary Category", r["primary_category"]), ("Building Type", r["building_type"]),
                           ("Operational Importance Driver", r["importance_driver"])):
            if not str(val or "").strip():
                errors.append(f"{where}: '{label}' is empty.")
        if r["importance"] not in rs.IMPORTANCE:
            errors.append(f"{where}: Operational Importance must be High, Medium or Low, found '{r['importance']}'.")
        if not isinstance(r["priority"], int) or isinstance(r["priority"], bool):
            errors.append(f"{where}: Priority must be a whole number (lower wins), found '{r['priority']}'.")
        if not [t for t in by_rule.get(ref, []) if t["active"]]:
            warnings.append(f"{where}: '{r['building_type']}' has no active tags - it will not match anything.")

    # tag-level checks, in tag-id order
    for t in sorted(snapshot["tags"], key=lambda t: t["id"]):
        r = rules.get(t["rule_ref"])
        if r is None or not r["active"] or not t["active"]:
            continue
        ref = t["rule_ref"]
        where = f"Rule {ref}"
        tw = f"{where} (tag {t['id']})"
        source, text = str(t["source"]).strip().upper(), str(t["tag"] or "").strip()
        if source == "MANUAL":
            warnings.append(f"{where}: '{r['building_type']}' has no automatic tag - it only appears via manual override.")
            continue
        if source not in ("OSM", "OS"):
            errors.append(f"{tw}: Source must be OSM, OS or MANUAL, found '{source}'.")
            continue
        if not text:
            errors.append(f"{tw}: Tag is empty.")
            continue
        if source == "OSM":
            if not re.fullmatch(r"[a-z_:0-9]+=(\*|[A-Za-z0-9_:;.\- ]+)", text) or " " in text.split("=")[0]:
                errors.append(f"{tw}: OSM tag '{text}' should look like key=value with no spaces "
                              f"(e.g. amenity=fire_station).")
                continue
            key, value = text.split("=", 1)
            if key not in rs.KNOWN_OSM_KEYS:
                hint = difflib.get_close_matches(key, rs.KNOWN_OSM_KEYS, n=1)
                warnings.append(f"{tw}: OSM key '{key}' is not one I recognise"
                                + (f" - did you mean '{hint[0]}'?" if hint else "."))
        else:
            if text.count(">") != 1:
                errors.append(f"{tw}: OS tag '{text}' should look like 'Group > Classification'.")
                continue
            key, value = [s.strip() for s in text.split(">")]
            if key not in rs.OS_GROUPS:
                hint = difflib.get_close_matches(key, rs.OS_GROUPS, n=1)
                warnings.append(f"{tw}: OS group '{key}' is not one I recognise"
                                + (f" - did you mean '{hint[0]}'?" if hint else "."))
        tags.append(Tag(ref, int(r["priority"]), str(r["primary_category"]).strip(), str(r["building_type"]).strip(),
                        str(r["importance"]).strip(), str(r["importance_driver"]).strip(), source, key, value,
                        text, str(t["notes"] or "").strip()))
    return errors, warnings, tags


def check_role_settings(snapshot):
    errors = []
    role = snapshot["role"]
    if not isinstance(role.get("min_area"), (int, float)) or role["min_area"] <= 0:
        errors.append(f"Role settings: minimum footprint must be a positive number, found '{role.get('min_area')}'.")
    for bt, v in role.get("per_type", {}).items():
        if not isinstance(v, (int, float)) or v <= 0:
            errors.append(f"Role settings: minimum for '{bt}' must be a positive number, found '{v}'.")
    both = set(role.get("ancillary", [])) & set(role.get("primary", []))
    if both:
        errors.append(f"Role settings: {', '.join(sorted(both))} appear in both the ancillary and primary lists.")
    return errors


def check_reference_mapping(snapshot):
    errors = []
    valid_types = {r["building_type"] for r in snapshot["rules"]}
    for m in snapshot["reference_mapping"]:
        if not m["active"]:
            continue
        where = f"Reference mapping row {m['sort_order']}"
        if m["source"] not in rs.MAPPING_SOURCES:
            errors.append(f"{where}: Reference Source must be DfE, CQC or NHS ODS, found '{m['source']}'.")
        if m["match"] not in ("Contains", "Equals"):
            errors.append(f"{where}: Match must be Contains or Equals, found '{m['match']}'.")
        if not str(m["field"] or "").strip() or not str(m["value"] or "").strip():
            errors.append(f"{where}: Field and Value must be filled in.")
        if m["building_type"] != "IGNORE" and m["building_type"] not in valid_types:
            errors.append(f"{where}: Building Type '{m['building_type']}' is not a Building Type on the Rules sheet.")
    return errors


def check_all(snapshot):
    """(errors, warnings) across rules, role settings and reference mapping."""
    errors, warnings, _ = check_rules(snapshot)
    return errors + check_role_settings(snapshot) + check_reference_mapping(snapshot), warnings
