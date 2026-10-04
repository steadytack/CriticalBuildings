"""The pipeline lock: one fetch / run / archive / release at a time, and dead owners never block."""
import json
import os
import subprocess
import sys

import pytest

from critical_buildings import cli, locks, pipeline, retention, runs


@pytest.fixture
def data(isolated_paths):
    return isolated_paths


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_the_lock_is_taken_and_released(data):
    with locks.pipeline_lock("run"):
        assert locks.lock_path().exists()
        assert locks.holder()["action"] == "run" and locks.holder()["pid"] == os.getpid()
    assert not locks.lock_path().exists() and locks.holder() is None


def test_a_second_operation_is_refused_with_a_message_naming_the_first(data):
    with locks.pipeline_lock("run"):
        with (pytest.raises(locks.LockedError, match=r"Cannot start 'fetch'.*'run'.*already using"),
              locks.pipeline_lock("fetch")):
            pytest.fail("should not get the lock")
        assert locks.lock_path().exists()                       # the refused attempt must not release the owner's lock


def test_the_lock_is_released_when_the_work_fails(data):
    with pytest.raises(ValueError), locks.pipeline_lock("run"):
        raise ValueError("boom")
    with locks.pipeline_lock("run"):                            # and can be taken again
        pass


def test_a_lock_left_by_a_dead_process_is_taken_over(data):
    locks.lock_path().parent.mkdir(parents=True)
    locks.lock_path().write_text(json.dumps({"pid": dead_pid(), "action": "run", "started": "2026-10-01T00:00:00"}))
    assert locks.holder() is None
    with locks.pipeline_lock("fetch"):
        assert locks.holder()["action"] == "fetch"


def test_a_live_foreign_owner_blocks(data):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        locks.lock_path().parent.mkdir(parents=True)
        locks.lock_path().write_text(json.dumps({"pid": child.pid, "action": "run", "started": "now"}))
        with pytest.raises(locks.LockedError, match=str(child.pid)), locks.pipeline_lock("archive"):
            pass
        assert locks.lock_path().exists()
    finally:
        child.kill()
        child.wait()


def test_an_unreadable_fresh_lock_is_treated_as_held_but_an_old_one_as_stale(data):
    p = locks.lock_path()
    p.parent.mkdir(parents=True)
    p.write_text("")                                            # another process is mid-write
    with pytest.raises(locks.LockedError), locks.pipeline_lock("run"):
        pass
    old = p.stat().st_mtime - 3600
    os.utime(p, (old, old))                                     # junk left behind long ago
    with locks.pipeline_lock("run"):
        pass


def test_run_archive_and_release_all_respect_the_lock(data, store):
    with locks.pipeline_lock("fetch"):
        with pytest.raises(locks.LockedError):
            pipeline.run("suffolk", store=store)
        with pytest.raises(locks.LockedError):
            retention.apply(store, [])
        with pytest.raises(locks.LockedError):
            retention.restore(store, "gold", "R1")
        with pytest.raises(locks.LockedError):
            runs.release(store, "suffolk", "v1", "R1")


def test_a_dry_run_fetch_needs_no_lock(data):
    from critical_buildings import sources
    with locks.pipeline_lock("run"):
        # dry-run only prints what would be downloaded; the lock is for real fetches
        res = sources.fetch_all(rules=type("R", (), {"tags": []})(), area="suffolk", only=["codepoint"], dry_run=True)
    assert res["codepoint"]["status"] == "dry-run"


def test_the_cli_reports_a_busy_pipeline_instead_of_a_traceback(data, capsys, monkeypatch):
    monkeypatch.setattr(cli, "cmd_runs", lambda args: (_ for _ in ()).throw(locks.LockedError("busy: try later")))
    # main() resolves the handler through set_defaults, so go through a real subcommand
    monkeypatch.setattr(cli, "_setup_logging", lambda verbose=False: None)
    import argparse
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self, argv=None: argparse.Namespace(verbose=False, fn=cli.cmd_runs, area="suffolk"))
    assert cli.main([]) == 1
    assert "busy: try later" in capsys.readouterr().out
