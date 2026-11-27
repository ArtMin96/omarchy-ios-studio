"""A hard bound on every test, so a hung test fails the run instead of hanging it (a full run once hung 20+ minutes).

A test still running after IOSSTUDIO_TEST_TIMEOUT seconds (default DEFAULT; 0 turns it off) ends the whole pytest
process with exit status 1, after printing every thread's stack to the terminal. faulthandler does the waiting and
printing in C, without the GIL, so it fires even while a thread is stuck inside a C call (lldb, a socket read).
Loaded by tests/conftest.py (pytest_plugins).
"""
from __future__ import annotations

import faulthandler
import os

import pytest

DEFAULT = 600.0       # the slowest test takes ~30 s; ten minutes is a hang, not a slow machine
_out = None           # the terminal's stderr, which pytest's output capture doesn't swallow


def limit() -> float:
    try:
        return max(0.0, float(os.environ.get("IOSSTUDIO_TEST_TIMEOUT") or DEFAULT))
    except ValueError:
        return DEFAULT


def _terminal_stderr(config) -> int:
    """pytest's own copy of the terminal's stderr (its capture saved it), else fd 2."""
    capman = config.pluginmanager.getplugin("capturemanager")
    err = getattr(getattr(capman, "_global_capturing", None), "err", None)
    fd = getattr(err, "targetfd_save", None)
    return fd if isinstance(fd, int) else 2


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    global _out
    _out = os.fdopen(os.dup(_terminal_stderr(config)), "w")


def pytest_unconfigure(config):
    global _out
    faulthandler.cancel_dump_traceback_later()
    if _out is not None:
        _out.close()
        _out = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    seconds = limit()
    if seconds and _out is not None:     # the stacks name the test: its function is in the main thread's
        faulthandler.dump_traceback_later(seconds, exit=True, file=_out)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
