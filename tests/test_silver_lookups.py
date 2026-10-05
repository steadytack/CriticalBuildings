"""Silver lookups built from the bronze files: Code-Point postcodes (subset on demand) and NHS ODS codes."""
import zipfile

import pytest

from critical_buildings import paths, silver


@pytest.fixture
def codepoint(tmp_path, monkeypatch):
    """A tiny Code-Point Open zip: ip.csv and co.csv, columns PC, PQ, EA, NO, ... (as in the real file)."""
    z = tmp_path / "codepo_gb.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Doc/Code-Point_Open_Column_Headers.csv", "PC,PQ,EA,NO,CY\n")
        f.writestr("Data/CSV/", "")
        f.writestr("Data/CSV/ip.csv", 'IP1 1AA,10,615838,244034,E92\n"IP1 1AB",10,615900,244100,E92\nIP1 1AA,10,1,2,E92\n')
        f.writestr("Data/CSV/co.csv", "CO1 1AA,10,600000,225000,E92\n")
    monkeypatch.setattr(paths, "bronze_file", lambda source, name, **kw: z)
    silver._postcode_table.cache_clear()
    yield
    silver._postcode_table.cache_clear()


def test_postcode_points_returns_only_the_requested_postcodes(codepoint):
    got = silver.postcode_points({"IP11AB", "CO11AA", "ZZ99ZZ"})
    assert got == {"IP11AB": (615900, 244100), "CO11AA": (600000, 225000)}      # unknown postcodes are simply absent


def test_postcode_points_without_a_filter_returns_everything_and_the_last_duplicate_wins(codepoint):
    got = silver.postcode_points()
    assert set(got) == {"IP11AA", "IP11AB", "CO11AA"} and got["IP11AA"] == (1, 2)


def test_postcodes_are_matched_after_normalising_spaces_and_case(codepoint):
    assert silver._norm_pc("ip1 1ab") == "IP11AB"
    assert silver.postcode_points({silver._norm_pc("ip1 1ab")}) == {"IP11AB": (615900, 244100)}


def test_ods_codes_are_read_with_their_trust_and_status(tmp_path, monkeypatch):
    def row(n, **cells):
        r = [""] * n
        for i, v in cells.items():
            r[int(i[1:])] = v
        return ",".join(r)
    files = {
        "etr.csv": "RXX,Some NHS Trust\n",
        "ets.csv": "\n".join([row(15, c0="RXX01", c1="Town Hospital", c9="IP1 1AA", c14="RXX"),
                              row(15, c0="RXX02", c1="Closed Site", c9="IP1 1AB", c11="20200101", c14="RXX")]) + "\n",
        "epraccur.csv": "\n".join([row(15, c0="A1", c1="High Street Surgery", c9="CO1 1AA", c12="ACTIVE", c14="PCN1"),
                                   row(15, c0="RXX01", c1="Duplicate of a site code", c9="X", c12="ACTIVE")]) + "\n",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    monkeypatch.setattr(paths, "bronze_file", lambda source, name, **kw: tmp_path / name)
    ods = silver._ods()
    assert ods["RXX01"] == {"name": "Town Hospital", "postcode": "IP1 1AA", "status": "Active",
                            "parent": "Some NHS Trust", "kind": "ets"}          # ets wins over epraccur for the same code
    assert ods["RXX02"]["status"] == "Closed"
    assert ods["A1"] == {"name": "High Street Surgery", "postcode": "CO1 1AA", "status": "Active",
                         "parent": "PCN1", "kind": "epraccur"}


def test_silver_key_changes_with_the_silver_code_and_area_settings(monkeypatch):
    from types import SimpleNamespace

    from critical_buildings import config, silver
    rules = SimpleNamespace(snapshot={"rules": []})
    monkeypatch.setattr(silver.paths, "bronze_key", lambda area: "bronze")
    base = silver.silver_key(rules, "suffolk")
    assert silver.silver_key(rules, "suffolk") == base
    monkeypatch.setattr(silver, "_code_fingerprint", lambda: "edited")
    assert silver.silver_key(rules, "suffolk") != base
    monkeypatch.undo()
    monkeypatch.setattr(silver.paths, "bronze_key", lambda area: "bronze")
    area = config.load_area("suffolk")
    monkeypatch.setattr(config, "load_area", lambda name: {**area, "os_grid_squares": ["tl"]})
    assert silver.silver_key(rules, "suffolk") != base
