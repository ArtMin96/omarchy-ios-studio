"""Runs before any test module is imported: isolates every test run from the user's real iOS Studio, and marks
tests by folder.

- XDG_* and IOSSTUDIO_RUNTIME_DIR point into one temp folder, so no test (or helper subprocess it starts) reads or
  writes ~/.config/ios-studio, ~/.local/state/ios-studio or the live device daemon's socket.
- IOSSTUDIO_DEMO_DELAY=0 makes the demo's pretend-work sleeps instant (see demo_delay in helper/iosstudio/core/subproc.py).
- Each test gets os.environ and the machine-wide device state files (device.json, last-device, devices.json) back
  as they were, so a test that runs the real daemon can't leave a "known iPhone" behind for later tests.
- tests/cli/ is marked `slow`, tests/mcp/ `mcp`, tests/e2e/ `slow` (network tests there add `network` themselves).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ios-studio-tests-"))
for _var in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
    os.environ[_var] = str(_TMP / _var.lower())
    os.makedirs(os.environ[_var], mode=0o700, exist_ok=True)
os.environ["IOSSTUDIO_RUNTIME_DIR"] = str(_TMP / "runtime")
os.environ.setdefault("IOSSTUDIO_DEMO_DELAY", "0")
os.environ.pop("IOSSTUDIO_DEMO", None)

_FOLDER_MARKS = {"cli": ("slow",), "mcp": ("mcp",), "e2e": ("slow",)}


# Files the device daemon writes for the whole machine; tests that write them must not leak them to later tests.
_MACHINE_STATE = (
    _TMP / "xdg_state_home/ios-studio/device.json",
    _TMP / "xdg_state_home/ios-studio/last-device",
    _TMP / "xdg_cache_home/ios-studio/devices.json",
)


@pytest.fixture(autouse=True)
def _restore_global_state():
    env = dict(os.environ)
    saved = {p: p.read_bytes() if p.is_file() else None for p in _MACHINE_STATE}
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


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
