"""The debugger can't hang the helper or the test suite (v0.4.8 reliability pass):

- `debug editor`'s bridge ends even when one of its watch tasks swallows its cancellation (Python 3.11's
  asyncio.wait_for does that when the awaited reply lands at the same moment: the app watcher then polled the daemon
  forever and a full pytest run hung for 20+ minutes in test_bridge_without_the_shadow_says_so).
- lldb_driver's connect waits for the process's state event before attaching (attaching on a state that read
  "stopped" a moment early hung lldb for good, 1 in ~50 sessions under load), against stand-in SB objects.
- lldb reads memory in lines of the target's page size (16 KB lines on a 4 KB-page Linux process broke expression
  results: "Couldn't dematerialize a result variable").
- tests/support/hang_guard.py turns a hung test into a failed run with every thread's stack.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_demo_variants_048 import from_driver

from iosstudio.device.client import DeviceClient
from iosstudio.features import debugger as dbg
from iosstudio.features import editor_debug_bridge as bridge
from iosstudio.features import lldb_shadow
from support.fake_deviced import FakeDeviced
from support.fake_lldb import write_fake_lldb

DRIVER = Path(dbg.__file__).with_name("lldb_driver.py")
TESTS = Path(__file__).resolve().parent.parent


# ================================================================ the bridge always ends

def test_bridge_ends_when_a_watch_task_swallows_its_cancel(tmp_path, monkeypatch, capsys):
    tc = tmp_path / "tc/usr/bin"
    tc.mkdir(parents=True)
    for name in ("lldb", "lldb-dap"):
        (tc / name).write_text("#!/bin/sh\n")
        (tc / name).chmod(0o755)
    monkeypatch.setattr(dbg, "toolchain", lambda: dbg.Toolchain(str(tc / "lldb"), "lldb-21", sys.executable, "", {}))
    monkeypatch.setattr(lldb_shadow, "for_lldb", lambda *a: (None, "No SDK."))
    monkeypatch.setattr(bridge, "POLL", 0.02)
    monkeypatch.setattr(bridge, "APP_POLL", 0.01)
    monkeypatch.setenv("IOSSTUDIO_EDITOR_DEBUG_LIFETIME", "0.3")
    real = DeviceClient.request

    async def request(self, cmd, args=None, timeout=60.0):
        if cmd != "processes.list":
            return await real(self, cmd, args, timeout)
        try:
            await asyncio.sleep(0.005)
        except asyncio.CancelledError:      # as 3.11's wait_for when the reply and the cancel meet
            pass
        return [{"pid": 9}]
    monkeypatch.setattr(DeviceClient, "request", request)
    start = {"bundleId": "com.example.c", "port": 50011, "host": "127.0.0.1", "pid": 9, "launched": False,
             "symbolsReady": True}
    result: list[int] = []
    with FakeDeviced(Path(os.environ["IOSSTUDIO_RUNTIME_DIR"]), {"debug.start": start, "debug.stop": {}}):
        t = threading.Thread(target=lambda: result.append(bridge.cmd_editor({"bundle_id": "com.example.c"})),
                             daemon=True)
        t.start()
        t.join(20)
    assert not t.is_alive(), "the bridge didn't end"
    assert result == [0]
    done = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()][-1]
    assert done["ok"] and done["data"]["ended"] == "timeout"


# ================================================================ lldb_driver: connect, cache lines

CONNECT_PROBE = r'''
import importlib.util, json, sys
cfg = json.loads(sys.argv[3])
spec = importlib.util.spec_from_file_location("lldb_driver", sys.argv[2])
d = importlib.util.module_from_spec(spec)
sys.argv = [sys.argv[2], sys.argv[1]]
spec.loader.exec_module(d)
import lldb
lldb.eStateConnected = 20
log = []

class Proc:
    """Reads "stopped" before its state event is pulled (lldb's public state in that window), "connected" after."""
    pulled = False
    def IsValid(self): return True
    def GetState(self): return lldb.eStateConnected if Proc.pulled else lldb.eStateStopped

class Target:
    def IsValid(self): return True
    def GetProcess(self): return Proc()

class Listener:
    polls = 0
    def GetNextEvent(self, ev):
        Listener.polls += 1
        if Listener.polls == 3 and not Proc.pulled:     # the event comes on the third poll
            Proc.pulled = True
            ev.state = lldb.eStateConnected
            return True
        return False

class Ev:
    state = None
class P:
    EventIsProcessEvent = staticmethod(lambda ev: ev.state is not None)
    GetStateFromEvent = staticmethod(lambda ev: ev.state)
    GetRestartedFromEvent = staticmethod(lambda ev: False)
lldb.SBProcess, lldb.SBEvent = P, Ev

class CI:
    def HandleCommand(self, cmd, r):
        log.append(("cmd", cmd, Proc.pulled))
class Dbg:
    def SetAsync(self, v): log.append(("async", v, Proc.pulled))
    def CreateTarget(self, *a): return Target()
    def SetSelectedTarget(self, t): pass
    def GetListener(self): return Listener()

d.time.sleep = lambda s: None
drv = object.__new__(d.Driver)
drv.cfg = cfg
drv.dbg, drv.ci, drv.app_module = Dbg(), CI(), ""
drv.connect()
d.emit("probe", log=log, polls=Listener.polls)
'''


@pytest.mark.parametrize("platform, line", [("remote-ios", 16384), ("remote-linux", 4096)])
def test_connect_waits_for_the_state_event_and_sets_page_size_lines(tmp_path, platform, line):
    probe = tmp_path / "probe.py"
    probe.write_text(CONNECT_PROBE)
    lldb_dir = write_fake_lldb(tmp_path / "fake-lldb")
    cfg = {"port": 5, "platform": platform}
    r = subprocess.run([sys.executable, "-I", str(probe), json.dumps({"lldbPythonPath": str(lldb_dir)}), str(DRIVER),
                        json.dumps(cfg)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = [json.loads(x) for x in r.stdout.splitlines() if x.strip()][-1]
    assert out["polls"] >= 3                                    # it kept polling while the state read "stopped"
    assert out["log"][-1] == ["async", False, True]             # sync again (for the attach) only after the event
    cmds = [c for kind, c, _ in out["log"] if kind == "cmd"]
    assert f"settings set target.process.memory-cache-line-size {line}" in cmds


def test_cache_lines_are_the_targets_page_size():
    ns = from_driver("MEMORY_CACHE_LINE", "PAGE_SIZES", "cache_line")
    assert (ns["cache_line"]("remote-ios"), ns["cache_line"]("remote-linux"), ns["cache_line"]("x")) == \
        (16384, 4096, 4096)


# ================================================================ a hung test ends the run

def test_hang_guard_ends_a_hung_run_with_the_stacks(tmp_path):
    (tmp_path / "test_hangs.py").write_text("import time\n\ndef test_ok():\n    pass\n\n"
                                            "def test_sleeps_forever():\n    time.sleep(120)\n")
    env = {**os.environ, "IOSSTUDIO_TEST_TIMEOUT": "1", "PYTHONPATH": str(TESTS)}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "support.hang_guard", "-p", "no:randomly",
                        "-p", "no:cacheprovider", "test_hangs.py"], cwd=tmp_path, env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 1
    assert "Timeout (0:00:01)!" in r.stderr + r.stdout and "test_sleeps_forever" in r.stderr + r.stdout
