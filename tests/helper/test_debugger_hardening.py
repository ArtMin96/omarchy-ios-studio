"""Hardening of the built-in debugger after a real iPhone session over Wi-Fi without debug symbols (CrashLab, 2026-11):
the helper always ends (a driver stuck inside lldb is killed with its process group, and the forward is released),
a stop's locals no longer hold up the stop (lldb_driver.Reads, against stand-in SB objects at the lldb API boundary),
plain messages for expressions that fail because the build lost its precompiled modules or the frame has no Swift,
the stale-modules notice and the build that brings the module files back (projects/modules.py), the summarized
HostInfoError / missing-module console spam, and the demo's shapes."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_demo_variants_048 import from_driver

from iosstudio.demo import debugger_demo as demo
from iosstudio.features import debugger as dbg
from iosstudio.features.projects import build, modules
from support.fake_deviced import FakeDeviced
from support.fake_lldb import write_fake_lldb

DRIVER = Path(dbg.__file__).with_name("lldb_driver.py")
TROUBLE = ("_MODULE_TROUBLE", "_STALE_TYPES", "_NO_JIT", "_HOSTINFO_LINE", "strip_noise", "run_fix", "module_trouble")


def events(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


# ================================================================ the helper always ends

STUCK_DRIVER = r'''
import json, os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)          # as a driver busy inside an lldb call: SIGTERM does nothing
child = subprocess.Popen(["sleep", "300"])               # something it started, holding the same pipes
open(os.environ["FAKE_DRIVER_PIDS"], "w").write(f"{os.getpid()} {child.pid}")
def out(**ev):
    print(json.dumps(ev), flush=True)
out(event="console", text="(lldb) process attach --pid 7")
out(event="attached", pid=7, process="App", threads=1, appSymbols=True, module="App")
for _ in range(50):
    sys.stderr.write("error: Error while searching for Xcode SDK: HostInfoError\n")
for name in ("SwiftUI-1LCSM.pcm", "Foundation-EHZ7F.pcm"):
    sys.stderr.write(f"warning: (arm64) /p/.build/out/Products/Debug-iphoneos/App.o(A.o) "
                     f"'/p/.build/out/SDKExplicitPrecompiledModules/{name}' does not exist\n")
    sys.stderr.write("warning: (arm64) /p/App.o(A.o) Unable to locate module needed for external types.\n")
    sys.stderr.write("Debugging will be degraded due to missing types. Rebuilding the project will regenerate the "
                     "needed module files.\n")
sys.stderr.write("warning: libobjc.A.dylib is being read from process memory.\n")
sys.stderr.flush()
while True:                                              # never reads stdin, never ends by itself
    time.sleep(1)
'''


def pruned_project(root: Path) -> Path:
    """A built project whose SDK precompiled modules were pruned (the layout xtool's builds leave)."""
    p = root / "App"
    (p / ".build/out/SDKExplicitPrecompiledModules").mkdir(parents=True)
    (p / ".build/out/SDKExplicitPrecompiledModules/modules.timestamp").write_text("")
    objs = p / ".build/out/Intermediates.noindex/App.build/Debug-iphoneos/App-t.build/Objects-normal/arm64"
    objs.mkdir(parents=True)
    for n in ("A.o", "B.o"):
        (objs / n).write_bytes(b"obj")
    dep = p / ".build/out/Intermediates.noindex/swift-collections.build/Debug-iphoneos/Deque-t.build/Objects-normal/arm64"
    dep.mkdir(parents=True)
    (dep / "D.o").write_bytes(b"obj")
    (p / ".build/out/Intermediates.noindex/SwiftExplicitPrecompiledModules").mkdir()
    return p


@pytest.fixture
def stuck(tmp_path, monkeypatch):
    script = tmp_path / "driver.py"
    script.write_text(STUCK_DRIVER)
    pids = tmp_path / "pids"
    monkeypatch.setattr(dbg, "DRIVER", script)
    monkeypatch.setattr(dbg, "DETACH_GRACE", 0.3)
    monkeypatch.setattr(dbg, "KILL_GRACE", 0.3)
    monkeypatch.setattr(dbg, "PUMP_GRACE", 2)
    monkeypatch.setattr(modules, "_notes_file", lambda: tmp_path / "missing-modules.json")
    tc = dbg.Toolchain("/x/lldb", "lldb 21", sys.executable, "", {})
    monkeypatch.setattr(dbg, "toolchain", lambda: tc)
    monkeypatch.setattr(dbg, "session_lldb", lambda t: dbg.SessionLldb(sys.executable, "", {
        **os.environ, "FAKE_DRIVER_PIDS": str(pids)}, "/sdk"))
    proj = pruned_project(tmp_path)
    monkeypatch.setattr(dbg, "find_project", lambda bid: (proj, {"name": "App", "product": "App"}))
    monkeypatch.setattr(dbg, "local_executable", lambda path, info: str(proj / ".build/debug/App-App"))

    async def stdin_at_eof():          # the controller (UI / MCP) went away
        r = asyncio.StreamReader()
        r.feed_eof()
        return r
    monkeypatch.setattr(dbg, "_stdin_reader", stdin_at_eof)
    return {"pids": pids, "project": proj}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/stat") as f:          # a zombie isn't alive
            return f.read().split(")")[-1].split()[0] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False


def test_a_driver_that_ignores_sigterm_is_killed_and_the_forward_released(stuck, capsys):
    replies = {"debug.start": {"bundleId": "XTL-T.com.x.app", "port": 1, "pid": 7, "launched": True,
                               "remotePath": "/r/App", "symbolsReady": False},
               "debug.stop": lambda a: {"bundleId": a["bundleId"], "stopped": True, "killed": a["kill"]}}
    with FakeDeviced(Path(os.environ["IOSSTUDIO_RUNTIME_DIR"]), replies) as daemon:
        t0 = time.monotonic()
        rc = dbg.cmd_session({"bundle_id": "XTL-T.com.x.app"})
        took = time.monotonic() - t0
        stops = daemon.sent("debug.stop")
    assert rc == 1 and took < 10
    drv, child = (int(x) for x in stuck["pids"].read_text().split())
    assert not alive(drv) and not alive(child)            # the whole process group went
    assert stops == [{"bundleId": "XTL-T.com.x.app", "kill": True, "pid": 7}]   # a launch nobody detached: ended
    ev = events(capsys)
    done = ev[-1]
    assert done["event"] == "done" and done["ok"] is False and done["error"]["code"] == "lldb-failed"
    texts = [e["text"] for e in ev if e.get("event") == "console"]
    assert "The debugger didn't stop in time; ending it." in texts
    assert sum("HostInfoError" in t for t in texts) == 2  # one summary, then the count at the end
    assert sum("module files from the app's build are missing (e.g. SwiftUI-1LCSM.pcm)" in t for t in texts) == 1
    assert "warning: libobjc.A.dylib is being read from process memory." in texts    # real warnings stay
    assert texts[-1] == "Hidden repeats of lldb messages: 49 HostInfoError lines, 5 missing-module warnings."
    stale = [e for e in ev if e.get("id") == "stale-modules"]
    assert len(stale) == 1 and stale[0]["message"] == dbg.STALE_NOTE      # at the start (stat) and from lldb: once
    assert stale[0]["fix"] == {"label": "Run the app", "page": "projects", "action": "run",
                               "path": str(stuck["project"])}
    assert next(e for e in ev if e.get("id") == "no-symbols")["fix"] == dbg.SYMBOLS_FIX
    assert modules.stale(stuck["project"])                 # lldb's own report is kept for the next build


# ================================================================ console spam

def test_console_filter_keeps_real_lines_and_counts_repeats():
    f = dbg.ConsoleFilter()
    assert f.text("(lldb) process detach\nSending disconnect packet failed.") == \
        "(lldb) process detach\nSending disconnect packet failed."
    first = f.text("error: Error while searching for SDK: HostInfoError")
    assert first.startswith("lldb: “Error while searching for Xcode SDK: HostInfoError”") and "harmless" in first
    assert f.text("error: Error while searching for Xcode SDK: HostInfoError") is None
    assert f.text("error: cannot find 'x' in scope\nerror: Error while searching for Xcode SDK: HostInfoError") == \
        "error: cannot find 'x' in scope"
    assert not f.missing_modules
    assert "Run the app again" in f.text("Debugging will be degraded due to missing types.")
    assert f.missing_modules
    assert f.summary() == "Hidden repeats of lldb messages: 2 HostInfoError lines."
    assert dbg.ConsoleFilter().summary() is None


# ================================================================ module_trouble

@pytest.fixture(scope="module")
def trouble():
    return from_driver(*TROUBLE)


SELF = ('type for self cannot be reconstructed: type for typename "$s8CrashLab8DataTestOXMtD" was not found (cached)\n'
        "error: Couldn't realize Swift AST type of self. Hint: using `v` to directly inspect variable")


@pytest.mark.parametrize("error, code", [
    (SELF, "stale-build"),
    ("error: Couldn't realize Swift AST type of self", "stale-build"),
    ("warning: '/p/.build/out/SDKExplicitPrecompiledModules/SwiftUI-1.pcm' does not exist", "stale-build"),
    ("error: Expression can't be run, because there is no JIT compiled function\n", "frame-no-swift"),
    ("error: Error while searching for Xcode SDK: HostInfoError\nerror: missing required module 'SwiftShims'",
     "swift-modules"),
    ("error: Error while searching for Xcode SDK: HostInfoError", "swift-modules"),
    ("error: Error while searching for Xcode SDK: HostInfoError\nerror: cannot find 'nope' in scope", None),
    ("error: cannot find 'nope' in scope", None),
])
def test_module_trouble_names_the_cause(trouble, error, code):
    t = trouble["module_trouble"](error, True, "/home/u/Projects/CrashLab")
    assert (t or {}).get("code") == code
    if code == "stale-build":
        assert t["message"].startswith("Swift expressions need a fresh build") and "Run the app again" in t["message"]
        assert t["fix"] == {"label": "Run the app", "page": "projects", "action": "run",
                            "path": "/home/u/Projects/CrashLab"}
    if code == "frame-no-swift":
        assert "Select one of your app's frames" in t["message"] and t["fix"] is None


def test_module_trouble_without_the_shadow_or_a_project(trouble):
    t = trouble["module_trouble"]("error: missing required module 'SwiftShims'", False)
    assert t["fix"] == {"label": "Set up the iOS SDK", "page": "setup", "step": "sdk"}
    assert trouble["module_trouble"](SELF, True)["fix"] == {"label": "Open Projects", "page": "projects"}
    assert trouble["strip_noise"]("error: Error while searching for SDK: HostInfoError\nerror: x") == "error: x"


# ================================================================ Reads: slow reads off the main loop

READS_PROBE = r'''
import importlib.util, json, sys, time
spec = importlib.util.spec_from_file_location("lldb_driver", sys.argv[2])
d = importlib.util.module_from_spec(spec)
sys.argv = [sys.argv[2], sys.argv[1]]
spec.loader.exec_module(d)
import lldb

class Err:
    def Fail(self): return False
    def GetCString(self): return None

class Val:
    """A local whose summary takes `delay` seconds to read (phone memory over Wi-Fi)."""
    def __init__(self, name, delay): self.name, self.delay = name, delay
    def GetName(self): return self.name
    def GetDisplayTypeName(self): return "Int"
    def GetTypeName(self): return "Swift.Int"
    def GetSummary(self):
        time.sleep(self.delay)
        return None
    def GetValue(self): return "1"
    def GetError(self): return Err()
    def GetNumChildren(self, cap): return 0
    def GetObjectDescription(self): return None

class VL:
    def __init__(self, vals): self.vals = vals
    def GetSize(self): return len(self.vals)
    def GetValueAtIndex(self, i): return self.vals[i]

class Invalid:
    def IsValid(self): return False

class Frame:
    def __init__(self, vals): self.vals = vals
    def IsValid(self): return True
    def GetFrameID(self): return 0
    def GetDisplayFunctionName(self): return "tick"
    def GetFunctionName(self): return "tick"
    def GetVariables(self, *a): return VL(self.vals)
    def GetModule(self): return Invalid()
    def GetSymbol(self): return Invalid()
    def GetLineEntry(self): return Invalid()
    def GetPC(self): return 0x1000
    def EvaluateExpression(self, expr, opts): return Val("$R0", 0)

class Thread:
    def __init__(self): self.frame = Frame([])
    def IsValid(self): return True
    def GetSelectedFrame(self): return self.frame
    def GetFrameAtIndex(self, i): return self.frame
    def GetNumFrames(self): return 1
    def GetIndexID(self): return 1
    def GetThreadID(self): return 77
    def GetName(self): return None
    def GetQueueName(self): return None
    def GetStopReason(self): return lldb.eStopReasonBreakpoint
    def GetStopReasonDataAtIndex(self, i): return 1
    def GetStopDescription(self, n): return "breakpoint 1.1"

class Proc:
    def __init__(self): self.thread = Thread()
    def GetSelectedThread(self): return self.thread
    def SetSelectedThread(self, t): pass
    def GetNumThreads(self): return 1
    def GetThreadAtIndex(self, i): return self.thread

class Dbg:
    def __init__(self): self.calls = []
    def RequestInterrupt(self): self.calls.append("interrupt")
    def CancelInterruptRequest(self): self.calls.append("cancel")

class Opts:
    def SetTimeoutInMicroSeconds(self, n): pass
    def SetTryAllThreads(self, v): pass
lldb.SBExpressionOptions = Opts

d.QUICK_LOCALS, d.LOCALS_BUDGET, d.SLOW_LOCALS, d.STUCK_AFTER = 0.05, 0.7, 0.1, 0.2
drv = object.__new__(d.Driver)
drv.cfg, drv.breakpoints, drv.app_module, drv.stop_id, drv.target = {"symbolsReady": False}, {1: "a.swift:3"}, "App", 0, None
drv.process, drv.dbg = Proc(), Dbg()
reads = drv.reads = d.Reads(drv)
out = {}

def settle(seconds=5):
    end = time.monotonic() + seconds
    while reads.busy() and time.monotonic() < end:
        reads.tick()
        time.sleep(0.02)

# 1. slow locals: the stop goes out at once, the locals follow (partly read when the budget runs out)
drv.process.thread.frame.vals = [Val(f"v{i}", 0.3) for i in range(6)]
t0 = time.monotonic()
reads.on_stop()
out["stopTook"] = time.monotonic() - t0
reads.submit({"cmd": "frame_variables", "requestId": "fv"})           # the same locals: answered with the stop's
reads.submit({"cmd": "evaluate", "expr": "1 + 2", "requestId": "e1"})  # waits its turn
settle()
d.emit("mark", step=1)

# 2. a step while the next stop's locals are read: cancelled, nothing late
reads.on_stop()
t0 = time.monotonic()
out["cancelled"] = reads.cancel()
out["cancelTook"] = time.monotonic() - t0
time.sleep(0.4)
reads.tick()
d.emit("mark", step=2)

# 3. fast locals ride along in the stop, as before
drv.process.thread.frame.vals = [Val("n", 0)]
reads.on_stop()
d.emit("mark", step=3)

# 4. one call stuck past the budget: lldb is asked to interrupt it, and the request is withdrawn afterwards
d.LOCALS_BUDGET = 0.1
drv.process.thread.frame.vals = [Val("slow", 1.0)]
reads.on_stop()
settle()
out["dbgCalls"] = drv.dbg.calls
d.emit("probe", **out)
'''


def test_reads_send_the_stop_first_and_the_locals_after(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(READS_PROBE)
    lldb_dir = write_fake_lldb(tmp_path / "fake-lldb")
    r = subprocess.run([sys.executable, "-I", str(probe), json.dumps({"lldbPythonPath": str(lldb_dir)}), str(DRIVER)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    ev = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
    marks = [i for i, e in enumerate(ev) if e["event"] == "mark"]
    one, two, three, four = ev[:marks[0]], ev[marks[0] + 1:marks[1]], ev[marks[1] + 1:marks[2]], ev[marks[2] + 1:]
    out = ev[-1]
    # 1
    assert out["stopTook"] < 0.3
    stop = one[0]
    assert stop["event"] == "stopped" and stop["localsPending"] is True and "locals" not in stop
    assert stop["breakpoint"] == {"id": 1, "spec": "a.swift:3"} and stop["thread"]["frames"][0]["function"] == "tick"
    notice = next(e for e in one if e["event"] == "notice")
    assert (notice["id"], notice["fix"]) == ("slow-variables", {"label": "Download debug symbols", "page": "device",
                                                                "tab": "debug"})
    late = [e for e in one if e["event"] == "variables"]
    assert [e.get("requestId") for e in late] == [None, "fv"] and all(e["stopId"] == 1 for e in late)
    vs = late[0]["variables"]
    assert late[0]["partial"] is True and vs[0]["value"] == "1"
    assert vs[-1] == {"name": "v5", "type": "Int", "childCount": 0, "unread": True}
    ev1 = next(e for e in one if e["event"] == "evaluated")
    assert ev1["requestId"] == "e1" and ev1["ok"] is True
    # 2
    assert out["cancelled"] is True and out["cancelTook"] < 0.6
    assert [e["event"] for e in two] == ["stopped"]                 # no late locals for a stop that moved on
    # 3
    assert three[0]["event"] == "stopped" and three[0]["localsPending"] is False
    assert three[0]["locals"]["variables"][0]["name"] == "n" and three[0]["stopId"] == 3
    assert not [e for e in four if e["event"] == "notice"]          # the slow-variables notice comes once
    # 4
    calls = out["dbgCalls"]
    assert calls[-2:] == ["interrupt", "cancel"] and calls.count("interrupt") == calls.count("cancel")


# ================================================================ projects/modules.py and the build

def test_pruned_is_a_stat_check_on_the_real_layout(tmp_path):
    p = pruned_project(tmp_path)
    assert modules.pruned(p)
    (p / modules.PCM_DIR / "SwiftUI-1LCSM.pcm").write_bytes(b"x")
    assert not modules.pruned(p)
    (p / modules.PCM_DIR / "SwiftUI-1LCSM.pcm").unlink()
    (p / modules.PCM_DIR / "modules.timestamp").unlink()
    (p / modules.PCM_DIR).rmdir()
    assert modules.pruned(p)                                   # the folder gone, explicit modules in use
    (p / modules.INTERMEDIATES / "SwiftExplicitPrecompiledModules").rmdir()
    assert not modules.pruned(p)                               # a build without explicit modules
    assert not modules.pruned(tmp_path / "never-built")


def test_refresh_removes_only_the_projects_own_objects(tmp_path, monkeypatch):
    monkeypatch.setattr(modules, "_notes_file", lambda: tmp_path / "missing-modules.json")
    p = pruned_project(tmp_path)
    assert [o.name for o in modules.own_objects(p, "App", "debug")] == ["A.o", "B.o"]
    assert modules.refresh(p, "App", "debug") == 2
    assert not modules.own_objects(p, "App", "debug")
    assert list((p / modules.INTERMEDIATES).glob("swift-collections.build/**/*.o"))    # dependencies stay
    (p / modules.PCM_DIR / "SwiftUI-1LCSM.pcm").write_bytes(b"x")                    # the build brought them back
    assert modules.refresh(p, "App", "debug") == 0 and not modules.stale(p)


def test_a_note_from_the_debugger_makes_the_next_build_recompile_once(tmp_path, monkeypatch):
    monkeypatch.setattr(modules, "_notes_file", lambda: tmp_path / "missing-modules.json")
    monkeypatch.setattr(modules, "NOTES_KEEP", 3)
    p = pruned_project(tmp_path)
    (p / modules.PCM_DIR / "Other.pcm").write_bytes(b"x")       # not empty: only lldb knows some are missing
    assert not modules.stale(p)
    for i in range(4):
        modules.note_missing(tmp_path / f"other{i}")
    modules.note_missing(p)
    assert modules.stale(p)
    assert len(json.loads((tmp_path / "missing-modules.json").read_text())) == 3          # capped
    assert modules.refresh(p, "App", "debug") == 2 and not modules.stale(p)
    assert modules.refresh(p, "App", "debug") == 0


def test_the_build_logs_why_it_recompiles(tmp_path, monkeypatch):
    monkeypatch.setattr(modules, "_notes_file", lambda: tmp_path / "missing-modules.json")
    p = pruned_project(tmp_path)
    lines = []

    class Runner:
        def on_line(self, text):
            lines.append(text)
    build.refresh_modules(Runner(), p, {"package": "App"}, "release")    # no objects for that configuration
    assert lines == []
    build.refresh_modules(Runner(), p, {"package": "App", "product": "App"}, "debug")
    assert len(lines) == 1 and "recompiling the app's own code (2 files)" in lines[0]


# ================================================================ demo shapes = real

def test_demo_strings_are_the_real_ones():
    real = from_driver("SLOW_NOTE", "SYMBOLS_FIX")
    assert (demo.SLOW_NOTE, demo.SYMBOLS_FIX) == (real["SLOW_NOTE"], real["SYMBOLS_FIX"])
    assert demo.SYMBOLS_FIX == dbg.SYMBOLS_FIX and demo.STALE_NOTE == dbg.STALE_NOTE
    t = from_driver(*TROUBLE)["module_trouble"]("error: Couldn't realize Swift AST type of self", True, "/p")
    assert demo.STALE_EVAL == t["message"]


def demo_session(monkeypatch, capsys, start: dict, *cmds: dict) -> list[dict]:
    from test_demo_variants_048 import Script
    monkeypatch.setattr(demo, "_daemon", lambda cmd, args: (start, None) if cmd == "debug.start" else (None, None))
    Script.batches = [[{"cmd": "pause"}], [dict(c, requestId=i) for i, c in enumerate(cmds)]]
    monkeypatch.setattr(demo, "Stdin", Script)
    assert demo.session({"bundle_id": "XTL-A1B2C3D4E5.com.example.weatherglance"}) == 0
    return events(capsys)


START = {"port": 52917, "pid": 400, "launched": False, "remotePath": "/r/WeatherGlance", "executable": "WeatherGlance",
         "ios": "27.2", "build": "24B115"}


def test_demo_without_symbols_sends_the_locals_after_the_stop(monkeypatch, capsys):
    monkeypatch.delenv("IOSSTUDIO_DEMO_DEBUG", raising=False)
    ev = demo_session(monkeypatch, capsys, {**START, "symbolsReady": False})
    stop = next(e for e in ev if e.get("event") == "stopped")
    assert stop["localsPending"] is True and "locals" not in stop
    late = next(e for e in ev[ev.index(stop):] if e.get("event") == "variables")
    assert late["stopId"] == stop["stopId"] and late["partial"] is False and "requestId" not in late
    assert [e["id"] for e in ev if e.get("event") == "notice"] == ["no-symbols", "slow-variables"]


def test_demo_with_symbols_keeps_the_locals_in_the_stop(monkeypatch, capsys):
    monkeypatch.delenv("IOSSTUDIO_DEMO_DEBUG", raising=False)
    ev = demo_session(monkeypatch, capsys, {**START, "symbolsReady": True})
    stop = next(e for e in ev if e.get("event") == "stopped")
    assert stop["localsPending"] is False and stop["locals"]["variables"]
    assert not any(e.get("id") == "slow-variables" for e in ev)


def test_demo_stale_variant(monkeypatch, capsys):
    monkeypatch.setenv("IOSSTUDIO_DEMO_DEBUG", "stale")
    ev = demo_session(monkeypatch, capsys, {**START, "symbolsReady": True}, {"cmd": "evaluate", "expr": "p day"})
    notice = next(e for e in ev if e.get("id") == "stale-modules")
    assert notice["message"] == dbg.STALE_NOTE and notice["fix"]["action"] == "run"
    res = next(e for e in ev if e.get("event") == "evaluated")
    assert (res["ok"], res["code"], res["message"]) == (False, "stale-build", demo.STALE_EVAL)
    assert res["fix"]["action"] == "run"
