"""Runs before any test module is imported: isolates every test run from the user's real iOS Studio, and marks
tests by folder.

- Everything a run writes lives in pytest's own numbered temp folder (`/tmp/pytest-of-$USER/pytest-N`), which pytest
  rotates (it keeps the last 3 runs). XDG_* and IOSSTUDIO_RUNTIME_DIR point into it, so no test (or helper subprocess
  it starts) reads or writes ~/.config/ios-studio, ~/.local/state/ios-studio or the live device daemon's socket, and
  TMPDIR / tempfile.tempdir do too, so `tempfile.mkdtemp()` in a test lands there instead of loose in /tmp.
- A run killed with SIGKILL leaves its pytest lock behind, which would keep pytest from rotating that folder for three
  days. Before each run, locks of runs whose process is gone are released, so killed runs rotate out like finished ones.
- IOSSTUDIO_DEMO_DELAY=0 makes the demo's pretend-work sleeps instant (see demo_delay in helper/iosstudio/core/subproc.py).
- Each test gets os.environ and the machine-wide device state files (device.json, last-device, devices.json) back
  as they were, so a test that runs the real daemon can't leave a "known iPhone" behind for later tests.
- tests/cli/ is marked `slow`, tests/mcp/ `mcp`, tests/e2e/ `slow` (network tests there add `network` themselves).
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_XDG = ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR")
_SOCKET_ROOM = 60   # the daemon's socket names go under the runtime dir; AF_UNIX paths stop at 107 bytes
_TMP = Path()       # this run's folder, set in pytest_configure


def _release_dead_locks(root: Path) -> None:
    """Remove the `.lock` of pytest-N folders whose pytest process no longer runs (pytest writes its pid there), so
    pytest's normal rotation can delete them; a live run's lock is left alone."""
    for lock in root.glob("pytest-*/.lock"):
        try:
            os.kill(int(lock.read_text().strip()), 0)
        except ProcessLookupError:
            lock.unlink(missing_ok=True)
        except (OSError, ValueError):   # unreadable, not ours, or the process lives
            continue


def _user() -> str:
    from _pytest.tmpdir import get_user
    return get_user() or "unknown"


_FOLDER_MARKS = {"cli": ("slow",), "mcp": ("mcp",), "e2e": ("slow",)}


@pytest.hookimpl(trylast=True)   # after pytest's tmpdir plugin has made config._tmp_path_factory
def pytest_configure(config):
    global _TMP
    factory = config._tmp_path_factory
    if factory._given_basetemp is None:
        _release_dead_locks(Path(os.environ.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir()).resolve() / f"pytest-of-{_user()}")
    _TMP = factory.getbasetemp() / "s"   # short: socket paths under it stop at 107 bytes
    for var in _XDG:
        os.environ[var] = str(_TMP / var.lower())
        os.makedirs(os.environ[var], mode=0o700, exist_ok=True)
    runtime = _TMP / "run"
    if len(str(runtime)) > 107 - _SOCKET_ROOM:   # a long --basetemp: keep sockets short, removed at exit
        runtime = Path(tempfile.mkdtemp(prefix="iost-run-", dir="/tmp"))
        atexit.register(shutil.rmtree, runtime, ignore_errors=True)
    os.environ["IOSSTUDIO_RUNTIME_DIR"] = str(runtime)
    (_TMP / "tmp").mkdir(mode=0o700, exist_ok=True)
    os.environ["TMPDIR"] = tempfile.tempdir = str(_TMP / "tmp")
    os.environ["IOSSTUDIO_TESTS_TMP"] = str(_TMP)   # tells support.devtest.isolate() it's already done
    os.environ.setdefault("IOSSTUDIO_DEMO_DELAY", "0")
    os.environ.pop("IOSSTUDIO_DEMO", None)


# Files the device daemon writes for the whole machine; tests that write them must not leak them to later tests.
def _machine_state() -> tuple[Path, ...]:
    return (_TMP / "xdg_state_home/ios-studio/device.json",
            _TMP / "xdg_state_home/ios-studio/last-device",
            _TMP / "xdg_cache_home/ios-studio/devices.json")


@pytest.fixture(autouse=True)
def _restore_global_state():
    env = dict(os.environ)
    saved = {p: p.read_bytes() if p.is_file() else None for p in _machine_state()}
    yield
    os.environ.clear()
    os.environ.update(env)
    for p, data in saved.items():
        if data is None:
            p.unlink(missing_ok=True)
        elif not p.is_file() or p.read_bytes() != data:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)


def pytest_collection_modifyitems(config, items):
    tests = Path(__file__).parent
    for item in items:
        try:
            folder = Path(item.fspath).relative_to(tests).parts[0]
        except ValueError:
            continue
        for mark in _FOLDER_MARKS.get(folder, ()):
            item.add_marker(getattr(pytest.mark, mark))

