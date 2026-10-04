"""One lock for everything that changes the data folders.

Fetching, building, archiving, restoring and releasing all read or rewrite shared state (the bronze LATEST pointers,
the CURRENT run, run folders, the ID registries), so two of them at once - say a run started from the UI while the
CLI fetches - could mix inputs, collide on a run folder or archive something a run is reading. They take this lock
and a second one fails at once with a message saying what holds it.

The lock is a file, data/locks/pipeline.lock, holding the owner's process id. A lock whose process has died (a
crash, a closed window, the PC sleeping) is detected and taken over, so it never needs clearing by hand.
"""
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime

from critical_buildings import paths


class LockedError(RuntimeError):
    """Another pipeline operation is already running."""


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259                                       # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def lock_path():
    return paths.DATA / "locks" / "pipeline.lock"


def holder():
    """{pid, action, started} of whoever holds the lock now, or None (no lock, or its owner has died)."""
    info = _read(lock_path())
    return info if info is not None and pid_alive(info.get("pid")) else None


def _read(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _describe(info):
    when = f" since {info['started']}" if info.get("started") else ""
    return f"'{info.get('action', 'an operation')}' (process {info.get('pid')}){when}"


@contextmanager
def pipeline_lock(action):
    """Hold the pipeline lock for the `with` block; raises LockedError if another live process holds it."""
    p = lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    me = {"pid": os.getpid(), "action": action, "started": datetime.now().isoformat(timespec="seconds")}
    for attempt in (1, 2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = _read(p)
            if info is None:                                               # empty/unreadable: mid-write, or junk
                try:
                    stale = time.time() - p.stat().st_mtime >= 10
                except FileNotFoundError:                                  # released just now: try again
                    continue
            else:
                stale = not pid_alive(info.get("pid"))
            if stale and attempt == 1 and _read(p) == info:                # its owner is gone: take over
                p.unlink(missing_ok=True)
                continue
            raise LockedError(f"Cannot start '{action}': {_describe(info or {})} is already using the data folder. "
                              "Wait for it to finish, then try again.") from None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(me, f)
        break
    else:                                                                  # lost the race for a stale lock
        raise LockedError(f"Cannot start '{action}': another operation has just started. Try again in a moment.")
    try:
        yield
    finally:
        info = _read(p)
        if info is not None and info.get("pid") == os.getpid():
            p.unlink(missing_ok=True)
