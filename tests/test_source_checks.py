"""Source files that change layout fail with a message that says what moved, not a bare KeyError or silent bad data."""
import pandas as pd
import pytest

from critical_buildings import paths, silver, source_checks
from critical_buildings.errors import PipelineError


def test_a_missing_column_is_named():
    source_checks.require_columns(["URN", "Easting"], ["URN"], "The file")
    with pytest.raises(PipelineError, match=r"'Northing'.*Compare it with the previous snapshot"):
        source_checks.require_columns(["URN", "Easting"], ["URN", "Easting", "Northing"], "The DfE GIAS file")


def test_a_register_mapping_that_tests_a_missing_column_is_refused():
    mapping = [{"source": "DfE", "field": "TypeOfEstablishment (name)"}, {"source": "DfE", "field": "Gone"},
               {"source": "CQC", "field": "Other"}]
    source_checks.check_mapping_fields(mapping, "CQC", ["Other"], "the sheet")
    with pytest.raises(PipelineError, match="'Gone'"):
        source_checks.check_mapping_fields(mapping, "DfE", ["TypeOfEstablishment (name)"], "the DfE file")


def _ods(rows, width=15):
    return pd.DataFrame([r + [""] * (width - len(r)) for r in rows], dtype=str)


def test_ods_layout_accepts_the_expected_columns_and_rejects_a_shifted_or_short_file():
    good = _ods([["RXX01", "Town Hospital", "", "", "", "", "", "", "", "IP1 1AA"], ["RXX02", "Other", "", "", "", "", "", "", "", "ip11ab"]])
    source_checks.check_ods_layout("ets.csv", good)
    shifted = _ods([["", "RXX01", "Town Hospital", "", "", "", "", "", "", "", "IP1 1AA"]])           # a column inserted at the front
    with pytest.raises(PipelineError, match="column 0 should hold the site code"):
        source_checks.check_ods_layout("ets.csv", shifted)
    with pytest.raises(PipelineError, match="only 0 of 1 values look like one"):
        source_checks.check_ods_layout("ets.csv", _ods([["RXX01", "Town Hospital", "", "", "", "", "", "", "", "not a postcode"]]))
    with pytest.raises(PipelineError, match="has 3 columns but the reader needs at least 15"):
        source_checks.check_ods_layout("ets.csv", _ods([["RXX01", "Town Hospital", "x"]], width=3))
    with pytest.raises(PipelineError, match="is empty"):
        source_checks.check_ods_layout("etr.csv", pd.DataFrame())


def test_silver_refuses_an_ods_file_whose_columns_moved(tmp_path, monkeypatch):
    (tmp_path / "etr.csv").write_text("RXX,Some NHS Trust\n")
    (tmp_path / "ets.csv").write_text(",RXX01,Town Hospital,,,,,,,,IP1 1AA,,,,RXX\n")
    (tmp_path / "epraccur.csv").write_text("A1,Surgery,,,,,,,,CO1 1AA,,,ACTIVE,,PCN1\n")
    monkeypatch.setattr(paths, "bronze_file", lambda source, name, **kw: tmp_path / name)
    with pytest.raises(PipelineError, match="ets.csv: column 0"):
        silver._ods()


def test_os_layer_fields_are_listed_for_every_layer_the_readers_use():
    assert set(source_checks.OS_LAYER_FIELDS) == {"FunctionalSite", "ImportantBuilding", "RailwayStation", "Building"}
