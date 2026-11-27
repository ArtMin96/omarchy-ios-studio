"""Debug in editor through helper/run.py iosstudio: demo shapes, `debug editor-config` on a temp project, errors, the
bridge stream against a stand-in device daemon, and end to end: the toolchain's lldb-dap (what VS Code and Zed run)
attaches through the attach file to a process under a local lldb-server, stops at a breakpoint, reads a variable and
detaches; afterwards the stub attach file refuses with a readable message."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from iosstudio.core import jsonc
from iosstudio.features.setup import toolchain_root
from support import ROOT
from support.dap_client import DapClient
from support.fake_deviced import FakeDeviced
from support.lldb_ready import debugger_ready
from support.lldb_target import BREAK_LINE, build, start_gdbserver, start_target

HELPER = [sys.executable, str(ROOT / "helper/run.py"), "iosstudio"]
# The helper's own lookup (the realpath of /usr/bin/swift, as Setup installs it), not PATH: CI's runner has a Swift on
# PATH under /usr/share/swift that the helper never uses.
TOOLCHAIN_BIN = (toolchain_root() or Path("/nonexistent")) / "usr/bin"


class Stream:
    """The helper as a running stream: JSON lines read in a thread, stdin kept open for {"cancel": true}."""

    def __init__(self, args: list[str], env: dict):
        self.p = subprocess.Popen(HELPER + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=env)
        self.lines: list[dict] = []
        self.stderr: list[str] = []     # drained, so a chatty helper never blocks on a full pipe
        self.logs: list[Path] = []      # other processes' logs a failing wait shows (lldb-server's)
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self) -> None:
        for line in self.p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            with self._cv:
                self.lines.append(ev)
                self._cv.notify_all()

    def _read_err(self) -> None:
        for line in self.p.stderr:
            self.stderr = (self.stderr + [line])[-50:]

    def story(self) -> str:
        """What the stream said, for a failing assertion: every event's name, the last few in full, stderr's tail."""
        tail = "\n".join(json.dumps(e)[:600] for e in self.lines[-8:])
        logs = "".join(f"\n{p.name}: {p.read_text(errors='replace')[-2000:]}" for p in self.logs if p.is_file())
        return (f"events {[e.get('event') for e in self.lines]}\nlast:\n{tail}\n"
                f"stderr: {''.join(self.stderr)[-2000:]}{logs}")

    def wait(self, pred, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                hit = next((e for e in self.lines if pred(e)), None)
                if hit is not None:
                    return hit
                left = deadline - time.monotonic()
                if left <= 0 or self.p.poll() is not None:
                    raise AssertionError(f"no matching event within {timeout:g} s; {self.story()}")
                self._cv.wait(min(left, 0.5))

    def event(self, name: str, timeout: float = 60, **match) -> dict:
        return self.wait(lambda e: e.get("event") == name and all(e.get(k) == v for k, v in match.items()), timeout)

    def cancel(self) -> None:
        self.p.stdin.write(json.dumps({"cancel": True}) + "\n")
        self.p.stdin.flush()

    def close(self) -> None:
        """Cancel (the helper detaches and releases the forward as it would for the app), then kill if it lingers."""
        if self.p.poll() is None:
            with contextlib.suppress(OSError, ValueError):
                self.cancel()
                self.p.stdin.close()
            try:
                self.p.wait(20)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.p.wait(10)
        for f in (self.p.stdin, self.p.stdout, self.p.stderr):
            with contextlib.suppress(OSError, ValueError):
                f.close()


def make_project(base: Path, name: str = "Target", bundle: str = "com.example.target") -> Path:
    p = base / name
    (p / "Sources" / name).mkdir(parents=True)
    (p / "Package.swift").write_text('// swift-tools-version: 6.0\nimport PackageDescription\n'
                                     f'let package = Package(name: "{name}")\n')
    (p / "xtool.yml").write_text(f"version: 1\nbundleID: {bundle}\nproduct: {name}\n")
    return Path(os.path.realpath(p))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {**os.environ, "HOME": str(self.home), "XDG_STATE_HOME": str(self.home / ".local/state"),
                    "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_DATA_HOME": str(self.home / ".local/share"),
                    "XDG_CACHE_HOME": str(self.home / ".cache")}
        self.env.pop("IOSSTUDIO_DEMO", None)
        # a runtime dir of our own, short enough for the socket
        self.run_dir = Path(tempfile.mkdtemp(prefix="ied-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        self.env["IOSSTUDIO_RUNTIME_DIR"] = str(self.run_dir)

    def helper(self, *args: str, demo: bool = False) -> tuple[dict, int]:
        env = dict(self.env)
        if demo:
            env["IOSSTUDIO_DEMO"] = "1"
        r = subprocess.run(HELPER + list(args), capture_output=True, text=True, env=env, timeout=60)
        return json.loads(r.stdout.strip().splitlines()[-1]), r.returncode


class Demo(Base):
    def test_editor_config_shape(self):
        out, rc = self.helper("debug", "editor-config", "--project", str(self.tmp / "WeatherGlance"), "--dry-run",
                              demo=True)
        self.assertEqual(rc, 0)
        d = out["data"]
        self.assertEqual(set(d), {"project", "bundleId", "appName", "configName", "editors", "dryRun", "applied",
                                  "scriptPath", "notes"})
        self.assertEqual(d["configName"], "Attach to WeatherGlance on iPhone (iOS Studio)")
        self.assertEqual(set(d["editors"][0]), {"id", "name", "path", "state", "action"})
        self.assertFalse((self.tmp / "WeatherGlance").exists())   # nothing written in demo mode

    def test_stream_ready_attached_then_cancel(self):
        replies = {"debug.start": {"bundleId": "XTL-A1B2C3D4E5.com.example.weatherglance", "port": 52917,
                                   "host": "127.0.0.1", "pid": 913, "launched": True, "symbolsReady": True},
                   "debug.stop": lambda a: {"bundleId": a["bundleId"], "stopped": True, "killed": None}}
        with FakeDeviced(self.run_dir, replies, demo=True) as d:
            s = Stream(["debug", "editor", "--bundle-id", "XTL-A1B2C3D4E5.com.example.weatherglance"],
                       {**self.env, "IOSSTUDIO_DEMO": "1"})
            self.addCleanup(s.close)
            ready = s.event("ready")
            self.assertEqual(set(ready) - {"event"}, {"bundleId", "appName", "port", "host", "pid", "launched",
                                                      "scriptPath", "editors", "configName", "symbolsReady", "localBuild",
                                                      "swiftExpressions"})
            s.event("editor", state="attached")
            s.cancel()
            done = s.event("done")
            self.assertTrue(done["ok"])
            self.assertEqual(done["data"]["ended"], "cancelled")
            self.assertTrue(done["data"]["attached"])
            self.assertEqual(len(d.sent("debug.stop")), 1)
        self.assertFalse((self.home / ".local/state/ios-studio/editor-debug").exists())   # no files in demo mode

    def test_status_shape(self):
        out, _ = self.helper("debug", "editor-status", demo=True)
        self.assertEqual(out["data"]["bridges"], [])


class Real(Base):
    def test_editor_config_writes_launch_json_then_skips(self):
        proj = make_project(self.tmp)
        out, rc = self.helper("debug", "editor-config", "--project", str(proj), "--editor", "vscode")
        self.assertEqual(rc, 0, out)
        d = out["data"]
        self.assertTrue(d["applied"])
        self.assertEqual(d["editors"][0]["action"]["kind"], "write")
        doc = jsonc.load((proj / ".vscode/launch.json").read_text())
        cfg = doc["configurations"][0]
        self.assertEqual((cfg["name"], cfg["type"], cfg["request"]), (d["configName"], "lldb-dap", "attach"))
        self.assertEqual(cfg["debugAdapterExecutable"], "${userHome}/.cache/ios-studio/lldb-ios/lldb-dap")
        self.assertIn("'~/.local/state/ios-studio/editor-debug/com.example.target.lldb'", cfg["attachCommands"][0])
        launcher = self.home / ".cache/ios-studio/lldb-ios/lldb-dap"
        if TOOLCHAIN_BIN.joinpath("lldb-dap").is_file():   # the launcher is there before the first bridge
            self.assertIn(str(TOOLCHAIN_BIN / "lldb-dap"), launcher.read_text())
        else:                                              # no toolchain lldb-dap: nothing to point it at yet
            self.assertFalse(launcher.exists())
        self.assertNotIn(str(self.home), (proj / ".vscode/launch.json").read_text())   # no machine path in the project
        self.assertEqual(sorted(p.name for p in proj.rglob("*") if p.is_file()),
                         ["Package.swift", "launch.json", "xtool.yml"])   # writes only launch.json
        stub = Path(d["scriptPath"]).read_text()
        self.assertIn("iosstudio-fail", stub)
        again, _ = self.helper("debug", "editor-config", "--project", str(proj), "--editor", "vscode")
        self.assertEqual(again["data"]["editors"][0]["state"], "present")
        self.assertEqual(again["data"]["editors"][0]["action"]["kind"], "skip")
        self.assertFalse(again["data"]["applied"])

    def test_dry_run_writes_nothing(self):
        proj = make_project(self.tmp)
        out, _ = self.helper("debug", "editor-config", "--project", str(proj), "--editor", "zed", "--dry-run")
        self.assertTrue(out["data"]["dryRun"])
        self.assertIn('"adapter": "Swift"', out["data"]["editors"][0]["action"]["preview"])
        self.assertFalse((proj / ".zed").exists())

    def test_errors(self):
        out, rc = self.helper("debug", "editor-config", "--project", str(self.tmp / "nope"))
        self.assertEqual((rc, out["error"]["code"]), (1, "not-found"))
        out, rc = self.helper("debug", "editor-config", "--project", str(make_project(self.tmp)), "--editor", "emacs")
        self.assertEqual(out["error"]["code"], "usage")
        out, rc = self.helper("debug", "editor")
        self.assertEqual((out["event"], out["ok"], out["error"]["code"]), ("done", False, "bad-args"))
        out, rc = self.helper("debug", "editor", "--bundle-id", "x", "--launch", "sometimes")
        self.assertEqual(out["error"]["code"], "bad-args")

    def test_status_ignores_dead_bridges(self):
        folder = self.home / ".local/state/ios-studio/editor-debug"
        folder.mkdir(parents=True)
        (folder / "com.example.gone.json").write_text(json.dumps({"bundleId": "com.example.gone", "helperPid": 1,
                                                                  "helperStart": "nope"}))
        out, _ = self.helper("debug", "editor-status")
        self.assertEqual(out["data"]["bridges"], [])


def _tools() -> dict | None:
    """The toolchain's lldb-dap and lldb-server, a C compiler, and lldb's Python (Setup's debugger support)."""
    dap, server, cc = TOOLCHAIN_BIN / "lldb-dap", TOOLCHAIN_BIN / "lldb-server", shutil.which("cc")
    if not (dap.is_file() and server.is_file() and cc):
        return None
    if not debugger_ready():   # the helper's own check: CI's Swift has lldb-dap but not lldb's Python
        return None
    return {"dap": str(dap), "server": str(server), "cc": cc}




@unittest.skipUnless(_tools(), "needs the Swift toolchain's lldb-dap and lldb-server, lldb's Python and cc")
class EndToEnd(Base):
    def test_lldb_dap_attaches_through_the_bridge(self):
        tools = _tools()
        proj = make_project(self.tmp)
        src = proj / "Sources/Target/target.c"
        exe = build(tools["cc"], src, proj / ".build/debug/Target-App")   # where local_executable finds an xtool build
        line = BREAK_LINE
        target, pid = start_target(self, exe)
        _server, port = start_gdbserver(self, tools["server"], self.tmp / "lldb-server.log")
        installed = "XTL-TEAM123456.com.example.target"
        replies = {
            "apps.list": {"apps": [{"bundleId": "com.example.other"}, {"bundleId": installed}]},
            "debug.start": {"bundleId": installed, "port": port, "host": "127.0.0.1", "pid": pid, "launched": False,
                            "remotePath": str(exe), "symbolsReady": False},
            "debug.stop": lambda a: {"bundleId": a["bundleId"], "stopped": True, "killed": None},
            "processes.list": [{"pid": pid, "name": "Target", "isApp": True}],
        }
        env = {**self.env, "IOSSTUDIO_DEBUG_PLATFORM": "remote-linux"}
        # IOSSTUDIO_TEST_DARWIN_SDK=<darwin.artifactbundle>: run it with the shadow lldb (built from that SDK into
        # this test's cache), so the editor's lldb-dap is the launcher's shadow and the attach file sets the SDK
        sdk = os.environ.get("IOSSTUDIO_TEST_DARWIN_SDK")
        if sdk:
            link = self.home / ".config/swiftpm/swift-sdks/darwin.artifactbundle"
            link.parent.mkdir(parents=True)
            link.symlink_to(sdk)
        with FakeDeviced(self.run_dir, replies) as daemon:
            bridge = Stream(["debug", "editor", "--project", str(proj), "--editor", "vscode"], env)
            self.addCleanup(bridge.close)
            bridge.logs.append(self.tmp / "lldb-server.log")
            ready = bridge.event("ready", timeout=120)
            self.assertEqual((ready["bundleId"], ready["port"], ready["pid"]), (installed, port, pid))
            # without an iOS SDK the launcher runs the toolchain's lldb-dap, and the bridge says so
            self.assertEqual(ready["swiftExpressions"], bool(sdk))
            self.assertEqual(any(e.get("id") == "lldb-plain" for e in bridge.lines), not sdk)
            self.assertEqual(ready["editors"], ["vscode"])
            self.assertEqual(ready["localBuild"], str(exe))
            self.assertEqual(daemon.sent("debug.start")[0]["bundleId"], installed)
            cfg = jsonc.load((proj / ".vscode/launch.json").read_text())["configurations"][0]
            launcher = cfg["debugAdapterExecutable"].replace("${userHome}", str(self.home))   # what VS Code runs
            self.assertEqual("iosstudio-sdk-path" in Path(ready["scriptPath"]).read_text(), bool(sdk))

            dap = self.attach(launcher, cfg, env)
            r = dap.request("setBreakpoints", {"source": {"path": str(src)}, "breakpoints": [{"line": line}]})
            self.assertTrue(r["body"]["breakpoints"][0]["verified"], r)
            self.assertTrue(dap.request("configurationDone")["success"])
            stop = dap.event("stopped")
            self.assertEqual(stop["body"]["reason"], "breakpoint")
            frame = dap.request("stackTrace", {"threadId": stop["body"]["threadId"], "levels": 1})["body"]["stackFrames"][0]
            self.assertEqual((frame["name"], frame["line"]), ("tick", line))
            scope = dap.request("scopes", {"frameId": frame["id"]})["body"]["scopes"][0]
            names = [v["name"] for v in dap.request("variables", {"variablesReference": scope["variablesReference"]})
                     ["body"]["variables"]]
            self.assertIn("n", names)
            if sdk:   # the shadow's liblldb: the SDK was set, no "another lldb-dap" warning
                self.assertNotIn("another lldb-dap", "".join(dap.output))
                maps = Path(f"/proc/{dap.p.pid}/maps").read_text()
                self.assertIn("/lldb-ios/", maps)
                self.assertNotIn(str(TOOLCHAIN_BIN.parent / "lib/liblldb"), maps)
            bridge.event("editor", state="attached")
            self.assertTrue(dap.request("disconnect", {"terminateDebuggee": False})["success"])
            dap.close()
            bridge.event("editor", state="detached", timeout=30)
            self.assertIsNone(target.poll())                 # detaching let the process run on

            bridge.cancel()
            done = bridge.event("done", timeout=60)
            self.assertTrue(done["ok"], done)
            self.assertEqual((done["data"]["ended"], done["data"]["attached"]), ("cancelled", True))
            self.assertEqual(daemon.sent("debug.stop")[-1]["kill"], False)

        # after the bridge: the same config now fails with the readable message instead of connecting
        script = Path(ready["scriptPath"])
        self.assertIn("iosstudio-fail", script.read_text())
        dap = DapClient([launcher], env=env)
        try:
            dap.request("initialize", {"adapterID": "lldb-dap", "clientID": "test", "pathFormat": "path"})
            r = dap.request("attach", {k: v for k, v in cfg.items() if k not in ("name", "type")}, timeout=120)
            self.assertFalse(r["success"])
            self.assertIn("Start 'Debug in editor' in iOS Studio first", "".join(dap.output))
        finally:
            dap.close()

    def attach(self, dap_bin: str, cfg: dict, env: dict) -> DapClient:
        dap = DapClient([dap_bin], env=env)
        self.addCleanup(dap.close)
        self.assertTrue(dap.request("initialize", {"adapterID": "lldb-dap", "clientID": "test", "linesStartAt1": True,
                                                   "columnsStartAt1": True, "pathFormat": "path"})["success"])
        seq = dap.send("attach", {k: v for k, v in cfg.items() if k not in ("name", "type")})
        r = dap.wait(lambda m: m.get("type") == "response" and m.get("request_seq") == seq, timeout=120)
        self.assertTrue(r["success"], "".join(dap.output)[-3000:])
        return dap


if __name__ == "__main__":
    unittest.main()
