"""`debug` group (helper/iosstudio/features/debugger.py) without a phone: the lldb toolchain lookup (fake lldb,
cache), source discovery, the local build lookup (.app, .build, .ipa), argument checks; and lldb_driver.py's pure
parts (clip, stdin commands, views over fake SB objects, main()'s failure path) under a stand-in lldb module."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from iosstudio.core.output import HelperError
from iosstudio.features import debugger as dbg
from support import ROOT, write_exe
from support.fake_lldb import write_fake_lldb
from support.projects import project_with_files

DRIVER = ROOT / "helper/iosstudio/features/lldb_driver.py"


def events(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


# ================================================================ toolchain

def fake_root(tmp_path: Path, script: str) -> Path:
    root = tmp_path / "toolchain"
    (root / "usr/lib/swift/host/compiler").mkdir(parents=True)
    (root / "usr/bin").mkdir(parents=True)
    write_exe(root / "usr/bin/lldb", script)
    return root


@pytest.fixture
def tc(tmp_path, monkeypatch):
    """Point the toolchain lookup at a fake root (set per test) and an empty cache."""
    from iosstudio.features import setup
    monkeypatch.setattr(dbg, "TOOLCHAIN_CACHE", tmp_path / "lldb-toolchain.json")
    holder = {"root": None}
    monkeypatch.setattr(setup, "toolchain_root", lambda: holder["root"])
    return holder


def test_env_points_at_the_toolchain_libs(tmp_path, monkeypatch):
    root = fake_root(tmp_path, "#!/bin/sh\n")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/keep")
    monkeypatch.setenv("PYTHONPATH", "/bad")
    monkeypatch.setenv("PYTHONHOME", "/bad")
    env = dbg._env(str(root / "usr/bin/lldb"))
    assert env["LD_LIBRARY_PATH"] == f"{root}/usr/lib/swift/host/compiler:{root}/usr/lib:/keep"
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env


def test_no_swift_means_needs_lldb(tc, capsys):
    with pytest.raises(HelperError) as ei:
        dbg._toolchain()
    assert ei.value.code == "needs-lldb" and ei.value.fix["step"] == "swift"
    assert dbg.cmd_check({}) == 0
    d = events(capsys)[0]["data"]
    assert d["ready"] is False and d["error"]["code"] == "needs-lldb"


def test_missing_libpython_means_needs_python(tc, tmp_path):
    tc["root"] = fake_root(tmp_path, "#!/bin/sh\necho 'lldb: error while loading shared libraries: "
                                     "libpython3.12.so.1.0: cannot open shared object file' >&2\nexit 127\n")
    with pytest.raises(HelperError) as ei:
        dbg._toolchain()
    assert ei.value.code == "needs-python" and "3.12" in ei.value.message and ei.value.fix["step"] == "debugger"


def test_lldb_python_not_installed(tc, tmp_path):
    tc["root"] = fake_root(tmp_path, "#!/bin/sh\necho /usr/lib/python3.99/site-packages\n")
    with pytest.raises(HelperError) as ei:
        dbg._toolchain()
    assert ei.value.code == "needs-python" and "3.99" in ei.value.message


def test_lldb_crash_is_needs_lldb(tc, tmp_path):
    tc["root"] = fake_root(tmp_path, "#!/bin/sh\necho 'Segmentation fault' >&2\nexit 139\n")
    with pytest.raises(HelperError) as ei:
        dbg._toolchain()
    assert ei.value.code == "needs-lldb" and "Segmentation" in ei.value.detail


def test_working_toolchain_is_cached(tc, tmp_path, capsys):
    import re
    found = sorted(p.name for p in Path("/usr/bin").glob("python3.*") if re.fullmatch(r"python3\.\d+", p.name))
    if not found:
        pytest.skip("no /usr/bin/python3.N")
    ver = found[-1][len("python"):]
    log = tmp_path / "calls"
    tc["root"] = fake_root(tmp_path, f'#!/bin/sh\necho "$1" >> {log}\n'
                                     f'[ "$1" = -P ] && echo /usr/lib/python{ver}/site-packages\n'
                                     '[ "$1" = --version ] && echo "lldb version 21.0.0"\nexit 0\n')
    t = dbg.toolchain()
    assert t.version == "lldb version 21.0.0" and t.python == f"/usr/bin/python{ver}"
    assert t.pythonpath == f"/usr/lib/python{ver}/site-packages"
    assert log.read_text().split() == ["-P", "--version"]
    again = dbg.toolchain()                                  # from the cache: lldb isn't asked again
    assert again.lldb == t.lldb and log.read_text().split() == ["-P", "--version"]
    os.utime(t.lldb, (1, 1))                                  # a different lldb file: asked again
    dbg.toolchain()
    assert len(log.read_text().split()) == 4
    assert dbg.cmd_check({}) == 0 and events(capsys)[0]["data"]["ready"] is True


# ================================================================ sources

def test_source_roots_follow_target_paths_and_local_packages(tmp_path):
    lib = project_with_files(tmp_path / "LocalPkg", {"Sources/Lib/L.swift": "x\n"})
    deeper = project_with_files(tmp_path / "Deeper", {"Sources/D/D.swift": ""})
    (lib / "Package.swift").write_text(f'.package(path: "{deeper}")')
    app = project_with_files(tmp_path / "App", {
        "Package.swift": '.package(path: "../LocalPkg")\n.target(name: "App", path: "Generated")\n'
                         '.testTarget(name: "AppTests", path: "Tests")\n',
        "Sources/App/A.swift": "a\nb\n", "Sources/App/Tests/T.swift": "", "Sources/App/.hidden/H.swift": "",
        "Sources/App/.build/B.swift": "", "Sources/App/notes.txt": "", "Generated/G.swift": "",
        "Tests/T.swift": ""})
    (app / "Generated" / "Link.swift").symlink_to(app / "Sources/App/A.swift")
    roots = [str(r.resolve().relative_to(tmp_path)) for r in dbg.source_roots(app)]
    assert roots == ["App/Sources", "LocalPkg/Sources", "Deeper/Sources", "App/Generated"]   # Tests/ skipped
    files = {f["rel"]: f for f in dbg.swift_sources(app)}
    assert set(files) == {"Sources/App/A.swift", "../LocalPkg/Sources/Lib/L.swift", "../Deeper/Sources/D/D.swift",
                          "Generated/G.swift"}           # Link.swift is A.swift again: listed once
    assert files["Sources/App/A.swift"]["lines"] == 2


def test_source_roots_depth_limit(tmp_path):
    c = project_with_files(tmp_path / "C", {"Sources/C/C.swift": ""})
    b = project_with_files(tmp_path / "B", {"Package.swift": f'.package(path: "{c}")', "Sources/B/B.swift": ""})
    a = project_with_files(tmp_path / "A", {"Package.swift": f'.package(path: "{b}")', "Sources/A/A.swift": ""})
    assert len(dbg.source_roots(a, depth=1)) == 2 and len(dbg.source_roots(a, depth=2)) == 3


def test_swift_sources_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(dbg, "MAX_SOURCES", 3)
    app = project_with_files(tmp_path / "A", {f"Sources/A/F{i}.swift": "" for i in range(5)})
    assert len(dbg.swift_sources(app)) == 3


# ================================================================ local builds

def app_bundle(path: Path, exe: str = "Hello") -> Path:
    path.mkdir(parents=True)
    (path / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": exe}))
    (path / exe).write_bytes(b"\x7fELF-ish")
    return path / exe


def ipa(path: Path, exe: str = "Hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/Hello.app/Info.plist", plistlib.dumps({"CFBundleExecutable": exe}))
        z.writestr(f"Payload/Hello.app/{exe}", b"binary")
    return path


def test_local_executable_prefers_the_newest_build(tmp_path):
    p = tmp_path / "Proj"
    assert dbg.local_executable(p, {"product": "Hello"}) is None
    a = app_bundle(p / "xtool" / "Hello.app")
    built = p / ".build" / "debug" / "Hello-App"
    built.parent.mkdir(parents=True)
    built.write_bytes(b"x")
    os.utime(a, (time.time() - 100, time.time() - 100))
    assert dbg.local_executable(p, {"product": "Hello"}) == str(built)
    os.utime(built, (time.time() - 200, time.time() - 200))
    assert dbg.local_executable(p, {"product": "Hello"}) == str(a)
    assert dbg.local_executable(p, {}) == str(a)               # any .app without a product name


def test_app_without_its_executable_is_skipped(tmp_path):
    app = tmp_path / "X.app"
    app.mkdir()
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "Missing"}))
    assert dbg._app_executable(app) is None
    (app / "Info.plist").write_text("not a plist")
    assert dbg._app_executable(app) is None


def test_ipa_is_unpacked_once_and_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(dbg, "DEBUG_BUILDS", tmp_path / "debug-builds")
    p = tmp_path / "Proj"
    f = ipa(p / "xtool" / "Hello.ipa")
    exe = dbg.local_executable(p, {"product": "Hello"})
    assert exe and Path(exe).read_bytes() == b"binary" and Path(exe).parent.name.startswith("Proj-")
    assert str(dbg._ipa_executable(f)) == exe                       # cached by mtime and size
    for i in range(3):                                         # three rebuilds: only the newest few stay
        ipa(f, exe="Hello")
        os.utime(f, (1000 + i, 1000 + i))
        dbg._ipa_executable(f)
    assert len(list((tmp_path / "debug-builds").iterdir())) <= dbg.DEBUG_BUILDS_KEEP


def test_broken_ipas_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(dbg, "DEBUG_BUILDS", tmp_path / "debug-builds")
    bad = tmp_path / "xtool" / "bad.ipa"
    bad.parent.mkdir()
    bad.write_text("not a zip")
    assert dbg._ipa_executable(bad) is None
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("Payload/A.app/Info.plist", plistlib.dumps({"CFBundleExecutable": "Gone"}))
    assert dbg._ipa_executable(bad) is None
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("readme", "no payload")
    assert dbg._ipa_executable(bad) is None


# ================================================================ commands

def test_find_project_matches_xtool_prefixed_bundle_ids(tmp_path, monkeypatch):
    from iosstudio.features.projects import model
    p = project_with_files(tmp_path / "App", {"xtool.yml": "version: 1\nbundleID: com.example.App\n"})
    monkeypatch.setattr(model, "scan", lambda: ([p], []))
    monkeypatch.setattr(model, "project_info", lambda path: {"bundleId": "com.example.App", "name": "App"})
    assert dbg.find_project("XTL-TEAM123.com.example.App")[0] == p
    assert dbg.find_project("com.example.App")[0] == p
    assert dbg.find_project("org.other.com.example.Apps") == (None, None)
    assert dbg.find_project("xcom.example.App") == (None, None)


def test_sources_command(tmp_path, monkeypatch, capsys):
    assert dbg.cmd_sources({}) == 1 and events(capsys)[0]["error"]["code"] == "bad-args"
    monkeypatch.setattr(dbg, "find_project", lambda b: (None, None))
    dbg.cmd_sources({"bundle_id": "com.x"})
    assert events(capsys)[0]["data"] == {"project": None, "files": [], "localBuild": None}
    p = project_with_files(tmp_path / "App", {"Sources/App/A.swift": ""})
    monkeypatch.setattr(dbg, "find_project", lambda b: (p, {"name": "App", "displayPath": "~/App", "product": "App"}))
    dbg.cmd_sources({"bundle_id": "com.x"})
    d = events(capsys)[0]["data"]
    assert d["project"]["name"] == "App" and [f["name"] for f in d["files"]] == ["A.swift"] and d["localBuild"] is None


def test_json_list():
    assert dbg._json_list({}, "breakpoints") == [] and dbg._json_list({"breakpoints": True}, "breakpoints") == []
    assert dbg._json_list({"commands": '["a", " ", 3]'}, "commands") == ["a", "3"]
    assert len(dbg._json_list({"commands": json.dumps(list(range(80)))}, "commands")) == 50
    for bad in ("{oops", '{"a": 1}'):
        with pytest.raises(HelperError) as ei:
            dbg._json_list({"breakpoints": bad}, "breakpoints")
        assert ei.value.code == "bad-args" and "--breakpoints" in ei.value.message


def test_stream_argument_errors_are_done_events(monkeypatch, capsys):
    assert dbg.cmd_backtrace({}) == 1
    assert events(capsys) == [{"event": "done", "ok": False, "error": {
        "code": "bad-args", "message": "Which app? (--bundle-id missing)"}}]
    assert dbg.cmd_session({"bundle_id": "com.x", "breakpoints": "nope"}) == 1
    ev = events(capsys)
    assert ev[0]["event"] == "step" and ev[0]["state"] == "failed" and ev[-1]["error"]["code"] == "bad-args"
    monkeypatch.setattr(dbg, "toolchain", lambda: (_ for _ in ()).throw(HelperError("needs-python", "Py")))
    assert dbg.cmd_session({"bundle_id": "com.x"}) == 1
    ev = events(capsys)
    assert ev[0] == {"event": "debug", "mode": "session", "bundleId": "com.x"}
    assert ev[-1]["error"]["code"] == "needs-python"


# ================================================================ lldb_driver (subprocess, stand-in lldb)

PROBE = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("lldb_driver", sys.argv[2])
d = importlib.util.module_from_spec(spec)
sys.argv = [sys.argv[2], sys.argv[1]]
spec.loader.exec_module(d)
import lldb
out = {}
out["clip"] = [d.clip(None), d.clip("abc", 2), d.clip(12345, 10)]
inp = d.Input()
cmds = []
while not inp.eof:
    cmds += inp.poll(2)
out["cmds"] = cmds

class V:
    def __init__(self, name, value=None, summary=None, kids=(), err=None):
        self.name, self.value, self.summary, self.kids, self.err = name, value, summary, list(kids), err
    def GetName(self): return self.name
    def GetDisplayTypeName(self): return "Int"
    def GetTypeName(self): return "Swift.Int"
    def GetSummary(self): return self.summary
    def GetValue(self): return self.value
    def GetError(self):
        e = self.err
        class E:
            def Fail(s): return e is not None
            def GetCString(s): return e
        return E()
    def GetNumChildren(self, cap): return min(len(self.kids), cap)
    def GetChildAtIndex(self, i): return self.kids[i]

class BP:
    def __init__(self, i, ok=True): self.i, self.ok = i, ok
    def IsValid(self): return self.ok
    def GetID(self): return self.i
    def GetNumLocations(self): return 1
    def GetNumResolvedLocations(self): return 1
    def GetHitCount(self): return 0

class T:
    def __init__(self): self.made = []
    def BreakpointCreateByLocation(self, f, line): self.made.append(("loc", f, line)); return BP(len(self.made))
    def BreakpointCreateByName(self, n): self.made.append(("name", n)); return BP(len(self.made), n != "bad")
    def GetNumBreakpoints(self): return len(self.made)
    def GetBreakpointAtIndex(self, i): return BP(i + 1)

drv = object.__new__(d.Driver)
drv.cfg, drv.breakpoints, drv.target = {}, {}, T()
out["bp"] = [drv.add_breakpoint(" Sources/App/A.swift:12 "), drv.add_breakpoint("viewDidLoad")]
try:
    drv.add_breakpoint("bad")
except d.Fail as exc:
    out["bpFail"] = exc.code
out["bpList"] = drv.breakpoint_list()
out["made"] = drv.target.made
many = V("arr", summary="100 values", kids=[V(f"[{i}]", value=str(i)) for i in range(25)])
out["value"] = drv.value_json(many, 1)
out["value0"] = drv.value_json(V("s", value="x" * 500, err="boom"), 0)

class Th:
    def __init__(self, r, desc=""): self.r, self.desc = r, desc
    def GetStopReason(self): return self.r
    def GetNumFrames(self): return 0
    def GetIndexID(self): return 1
    def GetThreadID(self): return 77
    def GetName(self): return ""
    def GetQueueName(self): return "main"
    def GetStopDescription(self, n): return self.desc
out["reasons"] = [d.Driver.reason(Th(r)) for r in (lldb.eStopReasonBreakpoint, lldb.eStopReasonTrace,
                  lldb.eStopReasonPlanComplete, lldb.eStopReasonSignal, lldb.eStopReasonException, 99)]
out["crashed"] = [drv.thread_json(Th(lldb.eStopReasonSignal, "signal SIGSTOP"), 5)["crashed"],
                  drv.thread_json(Th(lldb.eStopReasonSignal, "signal SIGABRT"), 5)["crashed"],
                  drv.thread_json(Th(lldb.eStopReasonException, "EXC_BAD_ACCESS"), 5)["crashed"],
                  "description" in drv.thread_json(Th(lldb.eStopReasonNone), 5)]
d.emit("probe", **out)
'''


def run_driver(tmp_path: Path, args: list[str], stdin: str = "", env: dict | None = None) -> list[dict]:
    lldb_dir = write_fake_lldb(tmp_path / "fake-lldb")
    cfg = json.dumps({"lldbPythonPath": str(lldb_dir), "mode": "backtrace", "port": 1})
    r = subprocess.run([sys.executable, "-I", *[a.replace("{cfg}", cfg) for a in args]], input=stdin,
                       capture_output=True, text=True, timeout=30, env={**os.environ, **(env or {})})
    return [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()] + [{"_rc": r.returncode, "_err": r.stderr}]


def test_driver_pure_parts(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    stdin = ('{"cmd": "step"}\ncontinue\n\n[1, 2]\n{"cancel": true}\n'
             '{"prompt": "p1", "value": "next"}\n{"cmd": "evaluate", "expr": "x", "requestId": 4}\n')
    *evs, last = run_driver(tmp_path, [str(probe), "{cfg}", str(DRIVER)], stdin)
    assert last["_rc"] == 0, last["_err"]
    out = [e for e in evs if e["event"] == "probe"][0]
    assert out["clip"] == ["", "ab…", "12345"]
    assert out["cmds"] == [{"cmd": "step"}, {"cmd": "continue"}, {"cmd": "detach", "reason": "cancel"},
                           {"prompt": "p1", "value": "next", "cmd": "next"},
                           {"cmd": "evaluate", "expr": "x", "requestId": 4}, {"cmd": "detach", "reason": "eof"}]
    assert out["bp"] == [{"id": 1, "spec": "Sources/App/A.swift:12", "locations": 1, "resolved": 1},
                         {"id": 2, "spec": "viewDidLoad", "locations": 1, "resolved": 1}]
    assert out["made"][:2] == [["loc", "Sources/App/A.swift", 12], ["name", "viewDidLoad"]]
    assert out["bpFail"] == "bad-breakpoint"
    assert [b["spec"] for b in out["bpList"]] == ["Sources/App/A.swift:12", "viewDidLoad", "?"]
    v = out["value"]
    assert v["value"] == "100 values" and v["childCount"] == 21 and len(v["children"]) == 20 and v["more"]
    v0 = out["value0"]
    assert len(v0["value"]) == 401 and v0["error"] == "boom" and "children" not in v0
    assert out["reasons"] == ["breakpoint", "step", "step", "signal", "exception", "other"]
    assert out["crashed"] == [False, True, True, False]


@pytest.mark.parametrize("env, code, ended", [({"FAKE_LLDB_CREATE": "raise"}, "lldb-failed", "none"),
                                              ({}, "lldb-failed", "none")])
def test_driver_main_always_reports_done(tmp_path, env, code, ended):
    *evs, last = run_driver(tmp_path, [str(DRIVER), "{cfg}"], env=env)
    assert last["_rc"] == 1
    done = evs[-1]
    assert done["event"] == "done" and done["ok"] is False and done["error"]["code"] == code
    assert done["ended"] == ended
    if env:
        assert "no lldb today" in done["error"]["detail"]
    else:   # connect() got as far as loading the target, then failed with lldb's own error text
        assert done["error"]["message"] == "lldb couldn't load the app's executable."
        assert any(e.get("event") == "console" and "memory-module-load-level minimal" in e["text"] for e in evs)
