"""Regressions: excluded buildings in the run diff, and partial override saves."""
import pandas as pd

from critical_buildings.control.store import ControlStore
from critical_buildings.gold.steps import diff_runs


def _buildings(*rows):
    return pd.DataFrame(rows, columns=["Building ID", "Building Type Default", "Source", "Source Feature ID", "Excluded"])


def _prev(df):
    """What the previous run's buildings.csv looks like once read back: live rows only, all strings."""
    return df[~df.Excluded].astype(str).drop(columns="Excluded")


def test_already_excluded_building_is_not_reported_as_added_every_run():
    cur = _buildings(("BLDG-000001", "Hospital", "OSM", "way/1", False),
                     ("BLDG-000002", "Fire Station", "OSM", "way/2", True))
    prev = _prev(cur)
    assert diff_runs(prev, cur).empty


def test_newly_excluded_building_is_reported_as_removed():
    before = _buildings(("BLDG-000001", "Hospital", "OSM", "way/1", False),
                        ("BLDG-000002", "Fire Station", "OSM", "way/2", False))
    after = before.copy()
    after.loc[after["Building ID"] == "BLDG-000002", "Excluded"] = True
    changes = diff_runs(_prev(before), after)
    assert list(zip(changes.Change, changes["Building ID"])) == [("Removed", "BLDG-000002")]


def test_diff_without_excluded_column_still_works():
    cur = _buildings(("BLDG-000001", "Hospital", "OSM", "way/1", False)).drop(columns="Excluded")
    prev = pd.DataFrame(columns=cur.columns)
    assert list(diff_runs(prev, cur).Change) == ["Added"]


def test_partial_override_save_keeps_other_columns(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.save_overrides([dict(building_id="BLDG-000001", building_role_override="Ancillary",
                               verification_override="Verified (manual)", manual_override_notes="site visit")],
                         "ann", "first")
    store.save_overrides([dict(building_id="BLDG-000001", review_decision="Accepted", exclude="Y")], "bob", "review")
    row = store.overrides_df().iloc[0]
    assert row["Building Role Override"] == "Ancillary"
    assert row["Verification Override"] == "Verified (manual)"
    assert row["Manual Override Notes"] == "site visit"
    assert row["Review Decision"] == "Accepted" and row["Exclude (Y)"] == "Y"
    assert store.table_df("override").iloc[0].decided_by == "bob"


def test_override_save_can_still_clear_a_column_explicitly(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.save_overrides([dict(building_id="BLDG-000001", exclude="Y")], "ann", "first")
    store.save_overrides([dict(building_id="BLDG-000001", exclude="")], "ann", "undo")
    assert store.overrides_df().iloc[0]["Exclude (Y)"] == ""
