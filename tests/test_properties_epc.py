"""The energy performance certificate (EPC) columns of the property table, on the synthetic area.

The register's bulk download needs a sign-in, so the tool takes a file you place in data/inbox/epc_domestic/. Certificates are matched to
UPRNs on the UPRN only; the latest certificate wins; address and postcode fields are never read.
"""
import json

import pandas as pd
import pytest
import synthetic_area
from test_properties_e2e import read_table, table  # noqa: F401 - the fixture
from test_properties_ui_logic import reference
from test_synthetic_e2e import run, synthetic  # noqa: F401 - the fixture

from critical_buildings import paths, sources
from critical_buildings.errors import PipelineError
from critical_buildings.properties import build, columns, inputs
from critical_buildings.ui import properties_logic as pl

AREA = synthetic_area.AREA


@pytest.fixture
def with_epc(synthetic, isolated_paths):  # noqa: F811
    """The critical-buildings run, the property sources, the EPC files stored, then the table; returns (frame by UPRN, result)."""
    run(synthetic, reuse_silver=False)
    synthetic_area.build_property_bronze(isolated_paths / "work_properties")
    synthetic_area.add_epc_to_inbox()
    assert sources.fetch_epc()["status"] == "new"
    result = build.build(AREA)
    return read_table(result).set_index("uprn").sort_index(), result


def test_with_no_epc_file_the_columns_exist_but_are_empty_and_it_says_how_to_add_one(table, isolated_paths):  # noqa: F811
    frame, result, _, _ = table
    epc = [c for c in columns.NAMES if c.startswith("epc_")]
    assert len(epc) == 19 and frame[[c for c in epc if c != "epc_certificates"]].isna().all().all() and frame.epc_certificates.isna().all()
    assert "epc_domestic" not in {s["source"] for s in json.loads((result["dest"] / "lineage.json").read_text())["bronze"]["snapshots"]}
    assert "Energy Performance" not in (result["dest"] / "ATTRIBUTION.txt").read_text(encoding="utf-8")
    skipped = sources.fetch_epc()
    assert skipped["status"] == "skipped" and "inbox" in skipped["note"] and "One Login" in skipped["note"]


def test_the_latest_certificate_of_each_uprn_is_linked(with_epc):
    frame, result = with_epc
    fire = frame.loc[100000000001]
    assert (fire.epc_certificate_number, fire.epc_rating, fire.epc_potential_rating, fire.epc_certificates) == ("C-001-NEW", "C", "B", 2)
    assert (fire.epc_efficiency, fire.epc_floor_area_m2, fire.epc_habitable_rooms, fire.epc_co2_tonnes) == (71, 120.5, 5, 3.2)
    assert str(fire.epc_lodged)[:10] == "2023-07-19" and fire.epc_mains_gas == "Y" and "insulated" in fire.epc_walls
    assert (fire.epc_property_type, fire.epc_built_form, fire.epc_tenure) == ("House", "Detached", "owner-occupied")
    elm = frame.loc[100000000021]
    assert (elm.epc_rating, elm.epc_floor_area_m2, elm.epc_age_band, elm.epc_roof, elm.epc_certificates) == (
        "G", 85.5, "England and Wales: before 1900", "Thatched", 1)
    assert result["rows"] == 9


def test_the_older_file_spelling_and_a_date_time_are_understood(with_epc):
    oak = with_epc[0].loc[100000000011]
    assert (oak.epc_certificate_number, oak.epc_rating, oak.epc_floor_area_m2) == ("C-011", "A", 300.25) and str(oak.epc_lodged)[:10] == "2025-05-05"
    assert pd.isna(oak.epc_walls) and pd.isna(oak.epc_efficiency)                  # that file has no such fields


def test_uprns_without_a_certificate_are_blank_and_unmatched_certificates_add_no_rows(with_epc):
    frame, result = with_epc
    none = frame.drop([100000000001, 100000000011, 100000000021])
    assert none.epc_rating.isna().all() and none.epc_certificate_number.isna().all() and (none.epc_certificates == 0).all()
    assert 100000000777 not in frame.index and len(frame) == 9                       # a certificate for a UPRN outside the table adds nothing
    assert result["summary"]["with_epc"] == 3 and result["summary"]["epc_efg"] == 1


def test_addresses_and_postcodes_from_the_register_never_reach_the_table(with_epc):
    frame, _ = with_epc
    everything = " ".join(str(v) for v in frame.to_numpy().ravel())
    assert "Fire Lane" not in everything and "Elm Cottage" not in everything and "Oak School House" not in everything
    assert not [c for c in frame.columns if "address" in c.lower()]
    assert frame.loc[100000000001].postcode == "TL1 9ZZ"                              # still the nearest Code-Point postcode, not the register's


def test_credits_lineage_and_cache_say_the_epc_file_was_used(with_epc):
    _, result = with_epc
    credits = (result["dest"] / "ATTRIBUTION.txt").read_text(encoding="utf-8")
    assert "Energy Performance of Buildings Register" in credits and "Address and postcode fields are not included" in credits
    lineage = json.loads((result["dest"] / "lineage.json").read_text())
    assert "epc_domestic" in {s["source"] for s in lineage["bronze"]["snapshots"]}
    assert len(list((paths.SILVER / AREA / "properties").glob("epc_*.parquet"))) == 1            # kept, so the next build does not parse it again


def test_a_second_build_reuses_the_parsed_certificates(with_epc, monkeypatch):
    def refuse(*args):
        raise AssertionError("must not parse the certificates again")
    monkeypatch.setattr(inputs, "_epc_batches", refuse)
    build.build(AREA)


def test_storing_the_same_file_again_changes_nothing_and_a_new_file_is_a_new_snapshot(with_epc):
    assert sources.fetch_epc()["status"] == "unchanged"
    (sources.epc_inbox() / "another.csv").write_text("UPRN,CURRENT_ENERGY_RATING\n100000000031,B\n", encoding="utf-8")
    assert sources.fetch_epc()["status"] == "new"


def test_a_file_that_is_not_certificates_is_refused_in_plain_english(synthetic, isolated_paths):  # noqa: F811
    inbox = sources.epc_inbox()
    inbox.mkdir(parents=True)
    (inbox / "wrong.csv").write_text("name,colour\nfred,blue\n", encoding="utf-8")
    assert sources.fetch_epc()["status"] == "new"
    with pytest.raises(PipelineError, match="does not look like the register's domestic certificates"):
        inputs.epc_latest(paths.bronze_snapshot("epc_domestic"), isolated_paths / "cache")


def test_the_explorer_filters_on_epc_and_agrees_with_pandas(with_epc):
    _, result = with_epc
    flat = read_table(result)
    ds = pl.open_table(result["dest"] / "table")
    assert pl.count(ds, pl.expression({"has_epc": True})) == 3
    assert pl.count(ds, pl.expression({"epc_ratings": ["C", "G"]})) == 2 and pl.count(ds, pl.expression({"epc_ratings": ["B"]})) == 0
    assert pl.count(ds, pl.expression({"epc_ratings": ["A"], "has_epc": True})) == 1
    assert sorted(pl.sample(ds, pl.expression({"epc_ratings": ["C", "G"]}), ["uprn"], 10).uprn) == [100000000001, 100000000021]
    assert len(reference(flat, {})) == 9
    rows = {r["Link"]: r for r in pl.coverage_rows(result["summary"])}
    assert rows["Has an energy certificate (EPC)"]["UPRNs"] == 3 and rows["EPC rating E, F or G"]["UPRNs"] == 1


def test_the_tab_warns_when_an_epc_file_is_added_after_the_table_was_built(table, isolated_paths):  # noqa: F811
    latest = pl.latest_table(AREA)
    assert pl.out_of_date(AREA, latest) == []
    synthetic_area.add_epc_to_inbox()
    sources.fetch_epc()
    assert any("EPC file has been stored since this table was built" in r for r in pl.out_of_date(AREA, latest))
