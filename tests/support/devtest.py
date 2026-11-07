"""Helpers for the device-layer tests (tests/helper/test_dev_*.py), which also run without pytest:

    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest $(ls helper/test_dev_*.py | sed 's#/#.#;s#\.py$##')

Call `isolate()` at import time: under pytest conftest.py has already pointed XDG_* and IOSSTUDIO_RUNTIME_DIR at a
temp folder (nothing changes); under plain unittest it does the same, so no test touches the user's live daemon
socket, config or caches.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import types
from pathlib import Path

_VARS = ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR")


def isolate() -> None:
    if os.environ.get("IOSSTUDIO_RUNTIME_DIR", "").startswith(tempfile.gettempdir()):
        return
    tmp = Path(tempfile.mkdtemp(prefix="ios-studio-devtests-"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    for var in _VARS:
        os.environ[var] = str(tmp / var.lower())
        os.makedirs(os.environ[var], mode=0o700, exist_ok=True)
    os.environ["IOSSTUDIO_RUNTIME_DIR"] = str(tmp / "runtime")
    os.environ.setdefault("IOSSTUDIO_DEMO_DELAY", "0")
    os.environ.pop("IOSSTUDIO_DEMO", None)


class Svc:
    """A pymobiledevice3 service: built from the lockdown/RSD, used as an async context manager. Subclass it and
    add the coroutine methods a feature calls; `Svc.opened` records which services were opened."""
    opened: list = []

    def __init__(self, rsd=None, *a, **k):
        self.rsd = rsd
        self.args = a
        self.kwargs = k
        Svc.opened.append(type(self).__name__)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeClock:
    """A monotonic clock tests advance by hand: patch a module's `time.monotonic` (or `time`) with it."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt

    # lets `mock.patch.object(mod, "time", clock.module())` cover time.time() and time.monotonic()
    def module(self) -> types.SimpleNamespace:
        import time as _time
        return types.SimpleNamespace(monotonic=self, time=self, perf_counter=self, sleep=_time.sleep,
                                     strftime=_time.strftime, localtime=_time.localtime, gmtime=_time.gmtime)
