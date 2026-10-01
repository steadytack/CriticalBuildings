"""Bronze layer: immutable snapshots, manifests, change detection (against a real local web server)."""
import datetime as dt
import functools
import http.server
import json
import threading

import pytest

from critical_buildings import paths
from critical_buildings import sources


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
    assert snap.name == dt.date.today().isoformat() and (snap / "data.csv").read_bytes() == b"a,b\n1,2\n"
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
    snap = paths.bronze_source_dir("os_open_map_local") / "2026-10-01"
    snap.mkdir(parents=True)
    sources.set_latest("os_open_map_local", "2026-10-01")
    got = paths.os_layer_paths("ImportantBuilding", tiles=("tl", "tm"))
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
