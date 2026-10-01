"""Rules API used by the pipeline stages, served from the control store's latest *published* rule set.

(Phase 3 replaces these module-level calls with an explicit RuleSet object passed down the stages.)
"""
from critical_buildings.control import ruleset as rs
from critical_buildings.control import validate
from critical_buildings.control.ruleset import AREA_TAGS, KNOWN_OSM_KEYS, OS_GROUPS, Tag  # noqa: F401  (re-exported)
from critical_buildings.control.store import ControlStore


def _published(version=None):
    return ControlStore().get_version(version)


def check_rules():
    """(errors, warnings, tags) for the published rule set."""
    _, snap = _published()
    return validate.check_rules(snap)


def load_rules():
    errors, warnings, tags = check_rules()
    if errors:
        raise SystemExit("Rules check FAILED:\n  " + "\n  ".join(errors))
    return tags, warnings


def rule_version():
    """Version label stamped on every output record, e.g. v1-cdb511."""
    vid, snap = _published()
    return rs.version_label(vid, snap)


def load_role_settings():
    """(settings, errors): settings has min_area, ancillary (set), primary (set), per_type (dict)."""
    _, snap = _published()
    role = snap["role"]
    settings = dict(min_area=float(role["min_area"]), ancillary=set(role["ancillary"]), primary=set(role["primary"]),
                    per_type={k: float(v) for k, v in role["per_type"].items()})
    return settings, validate.check_role_settings(snap)


def load_reference_mapping():
    """(rows, errors): the ACTIVE reference mapping rows in order (first match wins)."""
    _, snap = _published()
    rows = [dict(source=m["source"], field=m["field"], match=m["match"], value=m["value"], building_type=m["building_type"])
            for m in snap["reference_mapping"] if m["active"]]
    return rows, validate.check_reference_mapping(snap)
