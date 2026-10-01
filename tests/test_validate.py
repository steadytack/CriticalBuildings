"""Plain-English rule checks."""
import copy

import helpers
from critical_buildings.control import validate


def snap():
    return copy.deepcopy(helpers.snapshot())


def test_good_rule_set_has_no_errors_and_one_manual_note():
    errors, warnings, tags = validate.check_rules(snap())
    assert errors == []
    assert any("only appears via manual override" in w for w in warnings)
    # the MANUAL tag is not a pipeline tag; the five automatic ones are
    assert len(tags) == 5


def test_tags_are_returned_in_tag_id_order():
    s = snap()
    s["tags"].reverse()
    _, _, tags = validate.check_rules(s)
    assert [t.raw for t in tags][:2] == ["amenity=fire_station", "Emergency Service > Fire Station"]


def test_osm_tag_with_spaces_is_an_error():
    s = snap()
    s["tags"][0]["tag"] = "amenity = fire_station"
    errors, _, _ = validate.check_rules(s)
    assert any("should look like key=value" in e and "tag 1" in e for e in errors)


def test_osm_key_typo_gets_a_did_you_mean_hint():
    s = snap()
    s["tags"][0]["tag"] = "amenit=fire_station"
    errors, warnings, _ = validate.check_rules(s)
    assert errors == []
    assert any("did you mean 'amenity'" in w for w in warnings)


def test_osm_wildcard_value_is_allowed():
    s = snap()
    s["tags"][0]["tag"] = "military_service=*"
    errors, _, tags = validate.check_rules(s)
    assert errors == []
    assert tags[0].value == "*"


def test_os_tag_must_have_group_and_classification():
    s = snap()
    s["tags"][1]["tag"] = "Fire Station"
    errors, _, _ = validate.check_rules(s)
    assert any("Group > Classification" in e for e in errors)


def test_unknown_os_group_is_a_warning_not_an_error():
    s = snap()
    s["tags"][1]["tag"] = "Emergancy Service > Fire Station"
    errors, warnings, _ = validate.check_rules(s)
    assert errors == []
    assert any("did you mean 'Emergency Service'" in w for w in warnings)


def test_empty_tag_is_an_error():
    s = snap()
    s["tags"][2]["tag"] = ""
    errors, _, _ = validate.check_rules(s)
    assert any("Tag is empty" in e for e in errors)


def test_bad_importance_and_priority_are_errors():
    s = snap()
    s["rules"][0]["importance"] = "Critical"
    s["rules"][1]["priority"] = "50"
    errors, _, _ = validate.check_rules(s)
    assert any("Rule 1" in e and "High, Medium or Low" in e for e in errors)
    assert any("Rule 17" in e and "Priority must be a whole number" in e for e in errors)


def test_empty_required_fields_are_errors():
    s = snap()
    s["rules"][0]["building_type"] = " "
    errors, _, _ = validate.check_rules(s)
    assert any("'Building Type' is empty" in e for e in errors)


def test_inactive_rule_and_inactive_tag_are_skipped():
    s = snap()
    s["rules"][1]["active"] = False          # rule 17 off
    s["tags"][0]["active"] = False           # one rule-1 tag off
    errors, _, tags = validate.check_rules(s)
    assert errors == []
    assert {t.rule_ref for t in tags} == {1, 20}
    assert [t.raw for t in tags if t.rule_ref == 1] == ["Emergency Service > Fire Station"]


def test_rule_with_no_active_tags_warns():
    s = snap()
    for t in s["tags"]:
        if t["rule_ref"] == 20:
            t["active"] = False
    _, warnings, _ = validate.check_rules(s)
    assert any("Rule 20" in w and "no active tags" in w for w in warnings)


def test_tag_carries_rule_attributes():
    _, _, tags = validate.check_rules(snap())
    t = next(t for t in tags if t.raw == "school=primary")
    assert (t.rule_ref, t.priority, t.category, t.building_type, t.importance) == (17, 50, "Education", "Primary School", "Medium")


def test_reference_mapping_checks():
    s = snap()
    assert validate.check_reference_mapping(s) == []
    s["reference_mapping"][0]["building_type"] = "Prymary School"
    s["reference_mapping"][1]["source"] = "Ofsted"
    errors = validate.check_reference_mapping(s)
    assert any("'Prymary School' is not a Building Type" in e for e in errors)
    assert any("must be DfE, CQC or NHS ODS" in e for e in errors)


def test_reference_mapping_ignore_and_inactive_rows():
    s = snap()
    s["reference_mapping"][1]["building_type"] = "IGNORE"
    s["reference_mapping"].append(dict(sort_order=3, source="CQC", field="x", match="Contains", value="y",
                                       building_type="Nonsense", active=False, notes=""))
    assert validate.check_reference_mapping(s) == []


def test_role_settings_checks():
    s = snap()
    assert validate.check_role_settings(s) == []
    s["role"]["min_area"] = 0
    s["role"]["ancillary"] = ["shed", "school"]
    errors = validate.check_role_settings(s)
    assert any("minimum footprint must be a positive number" in e for e in errors)
    assert any("both the ancillary and primary lists" in e for e in errors)


def test_check_all_combines_everything():
    s = snap()
    s["tags"][0]["tag"] = "bad"
    s["role"]["min_area"] = -1
    errors, warnings = validate.check_all(s)
    assert len(errors) >= 2 and warnings
