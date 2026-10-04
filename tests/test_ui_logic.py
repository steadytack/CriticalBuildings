"""The Streamlit-free parts of the rules UI."""
import copy

import helpers
import numpy as np
import pandas as pd

from critical_buildings.ui import jobs, logic


def snap():
    return copy.deepcopy(helpers.snapshot())


# ------------------------------------------------------------------ grid <-> rows
def test_rows_from_frame_coerces_types_and_blanks():
    df = pd.DataFrame({"id": [1.0, np.nan], "rule_ref": [1, 17], "tag": [" amenity=x ", None], "active": [True, False],
                       "priority": [50.0, 90.0]})
    rows = logic.rows_from_frame(df, ints=("rule_ref", "priority"), bools=("active",), strings=("tag",))
    assert rows[0] == {"id": 1.0, "rule_ref": 1, "tag": "amenity=x", "active": 1, "priority": 50}
    assert rows[1]["id"] is None and rows[1]["tag"] == "" and rows[1]["active"] == 0 and rows[1]["priority"] == 90


def test_propose_replaces_only_the_edited_parts():
    base = snap()
    rows = [dict(r, active=1) for r in base["rules"]]
    rows[0]["importance"] = "Low"
    p = logic.propose(base, rules=rows)
    assert p["rules"][0]["importance"] == "Low" and p["rules"][0]["active"] is True
    assert p["tags"] == base["tags"] and base["rules"][0]["importance"] == "High"       # original untouched


def test_live_validation_of_an_unsaved_edit():
    base = snap()
    tags = [dict(t, active=1) for t in base["tags"]]
    tags[0]["tag"] = "amenity = fire_station"
    errors, _ = logic.validation_messages(logic.propose(base, tags=tags))
    assert any("should look like key=value" in e for e in errors)


def test_half_edited_tag_rows_are_reported():
    rules = [dict(r, active=1) for r in snap()["rules"]]
    tags = [{"id": 1, "rule_ref": None, "source": "OSM", "tag": "a=b"}, {"id": 2, "rule_ref": 999, "source": "OSM", "tag": "a=b"},
            {"id": 3, "rule_ref": 1, "source": "", "tag": "a=b"}]
    problems = logic.row_problems(rules, tags)
    assert any("Tag row 1" in p and "choose which rule" in p for p in problems)
    assert any("Tag row 2" in p and "no rule 999" in p for p in problems)
    assert any("Tag row 3" in p and "Source is blank" in p for p in problems)
    assert logic.row_problems(rules, [{"id": 1, "rule_ref": 1, "source": "OSM", "tag": "a=b"}]) == []


# ------------------------------------------------------------------ plain-English diffs
def test_identical_snapshots_have_no_diff():
    assert logic.diff_snapshots(snap(), snap()) == []


def test_diff_describes_rule_tag_mapping_and_role_changes():
    old, new = snap(), snap()
    new["rules"][0]["importance"] = "Medium"
    new["rules"].append({"rule_ref": 40, "primary_category": "Care", "building_type": "Dentist", "importance": "Low",
                             "importance_driver": "d", "priority": 50, "active": True, "notes": ""})
    new["rules"] = [r for r in new["rules"] if r["rule_ref"] != 20]
    new["tags"][0]["tag"] = "amenity=fire_station;"
    new["tags"].append({"id": 7, "rule_ref": 40, "source": "OSM", "tag": "amenity=dentist", "active": True, "notes": ""})
    new["reference_mapping"].append({"sort_order": 3, "source": "CQC", "field": "Care home?", "match": "Equals", "value": "Y",
                                         "building_type": "Primary School", "active": True, "notes": ""})
    new["role"]["min_area"] = 200.0
    new["role"]["ancillary"] = ["garage"]
    new["role"]["per_type"] = {"Fire Station": 60.0}
    text = "\n".join(logic.diff_snapshots(old, new))
    assert "Rule 1 (Fire Station): importance 'High' -> 'Medium'" in text
    assert "Added rule 40: Dentist (Care, Low)" in text and "Removed rule 20: School" in text
    assert "tag 'amenity=fire_station' -> 'amenity=fire_station;'" in text.replace("tag amenity=fire_station: tag 'amenity=fire_station'", "tag 'amenity=fire_station'")
    assert "Rule 40 (Dentist): added OSM tag amenity=dentist" in text
    assert "Reference mapping: added CQC Care home? equals 'Y' -> Primary School" in text
    assert "minimum primary footprint 150 -> 200 m2" in text
    assert "removed 'shed' from the ancillary OSM building values" in text
    assert "minimum footprint for Fire Station: default -> 60.0" in text


# ------------------------------------------------------------------ Run tab helpers
def _runs(*rows):
    import pandas as pd
    return pd.DataFrame(rows, columns=["run_id", "status", "started_at", "buildings", "sites"])


def test_cli_command_uses_the_current_interpreter():
    import sys
    assert jobs.cli_command("run", "--area", "suffolk") == [sys.executable, "-m", "critical_buildings", "run", "--area", "suffolk"]


def test_gdal_plugin_noise_is_removed_from_output():
    raw = ("ERROR 1: Can't load requested DLL: C:\\gdalplugins\\gdal_PDF.dll\n"
           "127: The specified procedure could not be found.\n\n"
           "run R1: 3 buildings")
    assert jobs.clean_output(raw) == "run R1: 3 buildings"


def test_sanity_gate_flags_empty_and_large_swings_only():
    assert logic.sanity_problems(_runs()) == []
    assert logic.sanity_problems(_runs(("R1", "succeeded", "2026-01-01", 100, 10))) == []
    assert logic.sanity_problems(_runs(("R1", "succeeded", "2026-01-01", 100, 10), ("R2", "succeeded", "2026-01-02", 105, 10))) == []
    big = logic.sanity_problems(_runs(("R1", "succeeded", "2026-01-01", 100, 10), ("R2", "succeeded", "2026-01-02", 150, 10)))
    assert len(big) == 1 and "100 to 150" in big[0]
    assert logic.sanity_problems(_runs(("R1", "succeeded", "2026-01-01", 0, 0)))
    # failed runs are ignored
    assert logic.sanity_problems(_runs(("R1", "succeeded", "2026-01-01", 100, 10), ("R2", "failed", "2026-01-02", 0, 0))) == []
