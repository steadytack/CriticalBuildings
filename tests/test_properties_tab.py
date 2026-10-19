"""The Properties tab itself, driven with Streamlit's own test runner on the synthetic area's table.

Skipped where Streamlit is not installed (CI installs only the dev extras); the logic underneath is tested in test_properties_ui_logic.py.
"""
import sys

import pytest
import synthetic_area
from test_properties_e2e import table  # noqa: F401 - the fixture
from test_synthetic_e2e import synthetic  # noqa: F401 - the fixture it uses

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

AREA = synthetic_area.AREA


@pytest.fixture(autouse=True)
def leave_main_alone():
    """AppTest runs its script as __main__; put that back, or later tests that start worker processes (spawn re-imports __main__) break."""
    main, saved = sys.modules.get("__main__"), dict(vars(sys.modules["__main__"]))
    yield
    sys.modules["__main__"] = main
    vars(main).clear()
    vars(main).update(saved)


def script(area):
    from critical_buildings.ui import properties_tab
    properties_tab.tab_properties(None, area)


def run():
    at = AppTest.from_function(script, args=(AREA,), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def metric(at, label):
    return next(m.value for m in at.metric if m.label == label)


def test_the_tab_shows_the_table_its_coverage_and_the_explorer(table):  # noqa: F811
    at = run()
    assert (metric(at, "UPRNs"), metric(at, "Grid squares")) == ("9", "1") and metric(at, "Matching UPRNs") == "9"
    assert any(b.label == "Build the property table" and not b.disabled for b in at.button)         # sources are all there, nothing is running
    assert not [w for w in at.warning if "out of date" in w.value or "newer" in w.value]            # nothing newer than the table
    assert any("What it covers" in m.value for m in at.markdown) and any("Explore" in m.value for m in at.markdown)


def test_the_explorer_filters_and_says_when_nothing_matches(table):  # noqa: F811
    at = run()
    at.selectbox(key="prop_flood").set_value("Zone 3 only").run()
    assert not at.exception and metric(at, "Matching UPRNs") == "1" and metric(at, "Share of the table") == "11.1%"
    at.selectbox(key="prop_flood").set_value("Any").run()
    at.checkbox(key="prop_ca").check().run()
    assert metric(at, "Matching UPRNs") == "2"
    at.checkbox(key="prop_a4").check().run()                                                # conservation area and Article 4: no UPRN is both
    assert not at.exception and any("No UPRNs match" in i.value for i in at.info)
    at.checkbox(key="prop_ca").uncheck()
    at.checkbox(key="prop_a4").uncheck().run()
    at.selectbox(key="prop_critical").set_value("Within a distance of a critical site").run()
    assert not at.exception and metric(at, "Matching UPRNs") == "7"                                 # the same 7 that are inside a site; the other two are further than 250 m
    at.multiselect(key="prop_imp").set_value(["High"]).run()                                        # only the fire station is High
    assert metric(at, "Matching UPRNs") == "3"


def test_the_download_is_prepared_on_request_and_goes_stale_when_filters_change(table):  # noqa: F811
    at = run()
    assert not at.get("download_button")
    next(b for b in at.button if b.key == "prop_prepare").click().run()
    assert len(at.get("download_button")) == 1
    at.selectbox(key="prop_flood").set_value("Zone 3 only").run()
    assert not at.get("download_button") and any("filters have changed" in c.value for c in at.caption)


def test_with_no_table_it_says_so_and_the_build_waits_for_the_sources(synthetic):  # noqa: F811
    at = run()                                                                                     # the property sources were never fetched
    assert any("No property table has been built" in i.value for i in at.info)
    assert any("Cannot build yet" in e.value and "ons_lsoa" in e.value for e in at.error)
    assert next(b for b in at.button if b.label == "Build the property table").disabled
    assert not next(b for b in at.button if b.label == "Fetch the property sources").disabled


def test_the_property_table_has_its_own_app_that_says_which_pipeline_it_is():
    from pathlib import Path

    from critical_buildings.ui import properties_app
    at = AppTest.from_file(properties_app.__file__, default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == "UPRN Property Table" and [t.label for t in at.tabs] == ["Property table", "Help"]
    assert not any("Rules" in t.label or "Publish" in t.label for t in at.tabs) and Path(properties_app.__file__).name == "properties_app.py"
    assert any("critical-buildings pipeline has its own" in c.value for c in at.sidebar.caption)
    critical = AppTest.from_file(str(Path(properties_app.__file__).with_name("app.py")), default_timeout=120)
    critical.run()
    assert not critical.exception, [e.value for e in critical.exception]
    assert "Properties" not in [t.label for t in critical.tabs] and critical.title[0].value == "Critical Buildings"
