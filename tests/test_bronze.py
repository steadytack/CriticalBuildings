"""Bronze layer: immutable snapshots, manifests, change detection (against a real local web server)."""
import datetime as dt
import functools
import http.server
import json
import threading

import pytest

from critical_buildings import paths, sources


@pytest.fixture
def web(tmp_path):
    """Serve tmp_path/web on localhost; returns (folder, base url)."""
    root = tmp_path / "web"
    root.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def fetch(base, name="data.csv", dry_run=False):
    return sources.snapshot("dfe_gias", [{"name": name, "url": f"{base}/{name}"}], dry_run=dry_run)


def test_first_fetch_creates_a_dated_snapshot_with_manifest_and_latest_pointer(isolated_paths, web):
    root, base = web
    (root / "data.csv").write_bytes(b"a,b\n1,2\n")
    res = fetch(base)
    assert res["status"] == "new"
    snap = paths.bronze_snapshot("dfe_gias")
    assert snap.name == dt.date.today().isoformat() and (snap / "data.csv").read_bytes() == b"a,b\n1,2\n"  # noqa: DTZ011 - local time is intended
    m = sources.read_manifest(snap)
    f = m["files"][0]
    assert f["sha256"] == sources.sha256_file(snap / "data.csv") and f["bytes"] == 8 and f["url"].endswith("/data.csv")
    assert m["licence"] == "Open Government Licence v3.0" and m["source"] == "dfe_gias"
    assert paths.latest_snapshot("dfe_gias") == snap.name


def test_an_unchanged_download_stores_nothing(isolated_paths, web):
    root, base = web
    (root / "data.csv").write_text("same\n")
    fetch(base)
    res = fetch(base)
    assert res["status"] == "unchanged"
    assert len(sources.list_snapshots("dfe_gias")) == 1
    assert not list(paths.bronze_source_dir("dfe_gias").glob("_tmp_*"))          # temp folder cleaned up


def test_a_changed_download_makes_a_new_snapshot_and_keeps_the_old_one(isolated_paths, web):
    root, base = web
    (root / "data.csv").write_text("v1\n")
    fetch(base)
    first = paths.bronze_snapshot("dfe_gias")
    (root / "data.csv").write_text("v2\n")
    res = fetch(base)
    assert res["status"] == "new" and res["snapshot"].endswith("-2")            # second snapshot on the same day
    assert (first / "data.csv").read_text() == "v1\n"                          # immutable
    assert (paths.bronze_snapshot("dfe_gias") / "data.csv").read_text() == "v2\n"
    assert sources.list_snapshots("dfe_gias") == [first.name, res["snapshot"]]


def test_a_pinned_snapshot_can_still_be_read_after_a_newer_one_arrives(isolated_paths, web):
    root, base = web
    (root / "data.csv").write_text("v1\n")
    first = fetch(base)["snapshot"]
    (root / "data.csv").write_text("v2\n")
    fetch(base)
    assert (paths.bronze_file("dfe_gias", "data.csv", pin=first)).read_text() == "v1\n"


def test_dry_run_downloads_nothing(isolated_paths, web):
    _, base = web
    assert fetch(base, dry_run=True)["status"] == "dry-run"
    assert not paths.bronze_source_dir("dfe_gias").exists()


def test_download_failure_raises_and_leaves_no_snapshot(isolated_paths, web, monkeypatch):
    _, base = web
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="Could not download"):
        fetch(base, name="missing.csv")
    assert not sources.list_snapshots("dfe_gias")


def test_a_missing_file_fails_at_once_without_retrying(isolated_paths, web, monkeypatch):
    _, base = web
    sleeps = []
    monkeypatch.setattr(sources.time, "sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        fetch(base, name="missing.csv")
    assert sleeps == []


@pytest.mark.parametrize("name,body", [
    ("osm.json", json.dumps({"elements": [], "remark": "runtime error: Query timed out in \"query\" at line 3"})),
    ("osm.json", json.dumps({"elements": [{"type": "node", "id": 1}], "remark": "runtime error: out of memory"})),
    ("osm.json", json.dumps({"elements": []})),
    ("osm.json", "<html>Too many requests</html>"),
    ("osm.json", json.dumps({"version": 0.6})),
    ("data.csv", "<!DOCTYPE html><html><body>Service unavailable</body></html>"),
    ("data.csv", ""),
    ("pack.zip", "this is not a zip"),
])
def test_a_bad_200_response_is_never_stored_as_a_snapshot(isolated_paths, web, monkeypatch, name, body):
    root, base = web
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    (root / name).write_text(body)
    with pytest.raises(RuntimeError, match="Could not download"):
        fetch(base, name=name)
    assert not sources.list_snapshots("dfe_gias") and paths.latest_snapshot("dfe_gias") is None


def test_a_complete_overpass_response_is_accepted(isolated_paths, web):
    root, base = web
    (root / "osm.json").write_text(json.dumps({"elements": [{"type": "node", "id": 1, "lat": 52, "lon": 1}]}))
    assert fetch(base, name="osm.json")["status"] == "new"


def test_a_transient_bad_response_is_retried(isolated_paths, web, monkeypatch):
    root, base = web
    (root / "osm.json").write_text(json.dumps({"elements": [], "remark": "runtime error: Query timed out"}))

    def server_recovers(seconds):
        (root / "osm.json").write_text(json.dumps({"elements": [{"type": "node", "id": 1}]}))
    monkeypatch.setattr(sources.time, "sleep", server_recovers)
    assert fetch(base, name="osm.json")["status"] == "new"


def test_min_elements_can_be_raised_per_file(isolated_paths, web, monkeypatch):
    root, base = web
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)          # the shortfall is retried like any bad download
    (root / "osm.json").write_text(json.dumps({"elements": [{"type": "node", "id": 1}]}))
    spec = [{"name": "osm.json", "url": f"{base}/osm.json", "min_elements": 5}]
    with pytest.raises(RuntimeError, match="at least 5"):
        sources.snapshot("dfe_gias", spec)


def test_bronze_key_changes_only_when_content_changes(isolated_paths, web):
    root, base = web
    (root / "data.csv").write_text("v1\n")
    fetch(base)
    k1 = paths.bronze_key("suffolk")
    assert paths.bronze_key("suffolk") == k1
    (root / "data.csv").write_text("v2\n")
    fetch(base)
    assert paths.bronze_key("suffolk") != k1


def test_missing_source_gives_a_helpful_message(isolated_paths):
    with pytest.raises(FileNotFoundError, match="python -m critical_buildings fetch"):
        paths.bronze_snapshot("cqc_hsca")


def test_os_layers_are_read_straight_from_the_zips(isolated_paths):
    snap = paths.bronze_source_dir("os_open_map_local", "suffolk") / "2026-10-01"
    snap.mkdir(parents=True)
    sources.set_latest("os_open_map_local", "2026-10-01", "suffolk")
    got = paths.os_layer_paths("ImportantBuilding", tiles=("tl", "tm"), area="suffolk")
    assert got[0].startswith("zip://") and got[0].endswith("!OS OpenMap Local (ESRI Shape File) TL/data/TL_ImportantBuilding.shp")
    assert got[1].endswith("TM/data/TM_ImportantBuilding.shp") and len(got) == 2


def test_the_cqc_link_is_found_on_the_page():
    html = '<a href="/system/files/2026-10/01_October_2026_HSCA_Active_Locations.ods">Care directory</a>'
    assert sources.resolve_cqc_url(html) == "https://www.cqc.org.uk/system/files/2026-10/01_October_2026_HSCA_Active_Locations.ods"
    with pytest.raises(RuntimeError, match="Could not find"):
        sources.resolve_cqc_url("<html>nothing here</html>")


def test_all_manifests_lists_the_latest_snapshot_of_each_source(isolated_paths, web):
    root, base = web
    (root / "data.csv").write_text("x\n")
    fetch(base)
    ms = sources.all_manifests()
    assert [m["source"] for m in ms] == ["dfe_gias"] and json.dumps(ms)


# ------------------------------------------------------------------ OS Open Map Local is scoped per area
def _os_snapshot(area, snapshot, sha="aaa"):
    d = paths.bronze_source_dir("os_open_map_local", area) / snapshot
    d.mkdir(parents=True)
    (d / "_manifest.json").write_text(json.dumps({"source": "os_open_map_local", "snapshot": snapshot, "retrieved_at": "x",
                                                  "licence": "OGL", "files": [{"name": "t.zip", "sha256": sha, "bytes": 1}]}))
    sources.set_latest("os_open_map_local", snapshot, area)


def test_os_open_map_local_and_osm_are_area_scoped_but_national_sources_are_not():
    assert paths.scoped_area("os_open_map_local", "suffolk") == "suffolk"
    assert paths.scoped_area("osm_overpass", "suffolk") == "suffolk"
    assert paths.scoped_area("dfe_gias", "suffolk") is None and paths.scoped_area("os_codepoint_open", "suffolk") is None


def test_the_os_fetch_stores_each_areas_tiles_under_that_area(isolated_paths, monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "snapshot", lambda source, specs, **kw: calls.append((source, specs, kw)) or {"status": "dry-run"})
    sources.fetch_open_map_local("suffolk", dry_run=True)
    monkeypatch.setattr(sources.config, "load_area", lambda name: {"os_grid_squares": ["tq"]})
    sources.fetch_open_map_local("kent", dry_run=True)
    (_, suffolk_specs, suffolk_kw), (_, kent_specs, kent_kw) = calls
    assert suffolk_kw["area"] == "suffolk" and kent_kw["area"] == "kent"
    assert [s["name"] for s in kent_specs] == ["opmplc_essh_tq.zip"] and len(suffolk_specs) == 3


def test_one_areas_os_snapshot_does_not_replace_anothers(isolated_paths):
    _os_snapshot("suffolk", "2026-10-01", "aaa")
    _os_snapshot("kent", "2026-10-02", "bbb")
    assert paths.latest_snapshot("os_open_map_local", "suffolk") == "2026-10-01"
    assert paths.latest_snapshot("os_open_map_local", "kent") == "2026-10-02"
    assert paths.bronze_key("suffolk") != paths.bronze_key("kent")
    assert [(m["area"], m["snapshot"]) for m in sources.all_manifests("suffolk")] == [("suffolk", "2026-10-01")]
    assert len(sources.all_manifests()) == 2                                  # no area: everything


def test_the_old_shared_os_folder_is_migrated_to_the_named_area(isolated_paths):
    old = paths.bronze_source_dir("os_open_map_local") / "2026-10-01"
    old.mkdir(parents=True)
    (old / "_manifest.json").write_text(json.dumps({"source": "os_open_map_local", "snapshot": "2026-10-01", "area": None,
                                                    "retrieved_at": "x", "licence": "OGL", "files": []}))
    sources.set_latest("os_open_map_local", "2026-10-01")
    assert sources.legacy_unscoped_sources("suffolk") == ["os_open_map_local"]
    assert sources.migrate_area_scoped_bronze("suffolk") == ["os_open_map_local -> os_open_map_local__suffolk"]
    assert paths.latest_snapshot("os_open_map_local", "suffolk") == "2026-10-01"
    assert not paths.bronze_source_dir("os_open_map_local").exists()
    assert sources.all_manifests("suffolk")[0]["area"] == "suffolk"           # lineage now records the area
    assert sources.legacy_unscoped_sources("suffolk") == [] and sources.migrate_area_scoped_bronze("suffolk") == []


def test_check_tells_you_to_migrate_when_the_os_data_is_still_in_the_old_folder(isolated_paths, seeded_store):
    from critical_buildings import pipeline
    seeded_store.publish("tester", "test setup")                 # check() looks at the bronze data once rules are published
    old = paths.bronze_source_dir("os_open_map_local") / "2026-10-01"
    old.mkdir(parents=True)
    sources.set_latest("os_open_map_local", "2026-10-01")
    problems = pipeline.check("suffolk", store=seeded_store)
    assert any("migrate-bronze --area suffolk" in p for p in problems)


# ------------------------------------------------------------------ conformed tables are reused when only unrelated sources changed
def _fake_snapshot(source, snapshot, sha, filename="data.csv"):
    d = paths.bronze_source_dir(source) / snapshot
    d.mkdir(parents=True, exist_ok=True)
    (d / "_manifest.json").write_text(json.dumps({"files": [{"name": filename, "sha256": sha, "bytes": 1}]}))
    (paths.bronze_source_dir(source) / "LATEST.json").write_text(json.dumps({"snapshot": snapshot}))


def test_a_conformed_table_is_reused_unless_its_own_sources_changed(isolated_paths):
    _fake_snapshot("cqc_hsca", "2026-01-01", "aaa")
    _fake_snapshot("dfe_gias", "2026-01-01", "111")
    inputs = [("cqc_hsca", None)]
    assert paths.conformed_lookup("a", "t.parquet", inputs) is None
    (paths.silver_conformed_dir("a") / "t.parquet").write_text("table")
    paths.conformed_stamp("a", "t.parquet", inputs)
    first_dir = paths.silver_conformed_dir("a")
    _fake_snapshot("dfe_gias", "2026-02-01", "222")                      # an unrelated source is refreshed: new build folder
    assert paths.silver_conformed_dir("a") != first_dir
    reused = paths.conformed_lookup("a", "t.parquet", inputs)
    assert reused is not None and reused.read_text() == "table" and reused.parent == paths.silver_conformed_dir("a")
    _fake_snapshot("cqc_hsca", "2026-02-01", "bbb")                      # the table's own source changes: must rebuild
    assert paths.conformed_lookup("a", "t.parquet", inputs) is None
