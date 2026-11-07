"""Watch mode through helper/run.py iosstudio with a fake xtool: compile on save through the real `build compile`, a stop
that kills the running build, replacing another project's watcher, a follower that detaches, demo mode."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from support import HELPER_PY as HELPER
from support import isolated_env, write_exe
from support.projects import app_project as make_project
from support.projects import atomic_save

FAKE_XTOOL = """\
#!/usr/bin/env python3
import os, pathlib, sys, time
print("fake xtool", " ".join(sys.argv[1:]), flush=True)
delay = float(os.environ.get("FAKE_XTOOL_SECONDS") or 0.3)
time.sleep(delay)
bad = []
for f in sorted(pathlib.Path("Sources").rglob("*.swift")):
    for i, line in enumerate(f.read_text().splitlines(), 1):
        if "BROKEN" in line:
            bad.append(f"{f.resolve()}:{i}:{line.index('BROKEN') + 1}: error: cannot find 'BROKEN' in scope")
if bad:
    print("\\n".join(bad))
    print("error: fatalError")
    sys.exit(1)
print("Build complete! (0.1 secs)")
"""


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="watch-cli-"))
        self.env = isolated_env(self.tmp)
        self.env["IOSSTUDIO_RUNTIME_DIR"] = str(self.tmp / "rt")
        self.env["IOS_STUDIO_XTOOL"] = str(write_exe(self.tmp / "xtool", FAKE_XTOOL))
        self.p = make_project(self.tmp)
        self.owner = subprocess.Popen(["sleep", "600"])

    def tearDown(self):
        self.helper("watch", "stop")
        self.owner.kill()
        self.owner.wait()

    def helper(self, *args, env=None, timeout=60) -> dict:
        r = subprocess.run(HELPER + list(args), env=env or self.env, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        lines = [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]
        return lines[-1] if lines else {"raw": r.stdout, "err": r.stderr}

    def start(self, project=None, env=None, *extra) -> dict:
        return self.helper("watch", "run", "--project", str(project or self.p), "--detach", "--owner",
                           str(self.owner.pid), *extra, env=env)

    def test_compile_on_save_through_the_real_build(self):
        out = self.start()
        self.assertTrue(out["ok"], out)
        self.assertIn(out["data"]["state"], ("watching", "compiling"))   # the first build starts at once
        st = self.helper("watch", "status", "--wait-seconds", "20")["data"]
        self.assertEqual((st["state"], st["lastBuild"]["result"]), ("ok", "ok"))
        atomic_save(self.p / "Sources/App/ContentView.swift", "import SwiftUI\nlet x = BROKEN\n")
        st = self.helper("watch", "status", "--project", str(self.p), "--wait-seconds", "20")["data"]
        self.assertEqual(st["state"], "failed", st)
        lb = st["lastBuild"]
        self.assertEqual(lb["errors"], 1)
        d = lb["diagnostics"][0]
        self.assertEqual((d["line"], d["col"], d["message"]), (2, 9, "cannot find 'BROKEN' in scope"))
        self.assertTrue(d["file"].endswith("Sources/App/ContentView.swift"))
        hist = self.helper("build", "history", "--limit", "1")["data"]["builds"][0]
        self.assertEqual((hist["buildId"], hist["result"], hist["kind"], hist["trigger"]),
                         (lb["id"], "failed", "compile", "watch"))
        stop = self.helper("watch", "stop", "--project", str(self.p))["data"]
        self.assertTrue(stop["stopped"])
        self.assertEqual(self.helper("watch", "status")["data"]["state"], "stopped")

    def test_stop_kills_a_running_compile(self):
        env = dict(self.env, FAKE_XTOOL_SECONDS="30")
        self.start(env=env)
        deadline = time.monotonic() + 10
        st = {}
        while time.monotonic() < deadline:
            st = self.helper("watch", "status")["data"]
            if st.get("state") == "compiling" and st.get("logFile"):
                break
            time.sleep(0.2)
        self.assertEqual(st.get("state"), "compiling")
        time.sleep(0.5)
        t0 = time.monotonic()
        out = self.helper("watch", "stop")["data"]
        self.assertLess(time.monotonic() - t0, 10)
        self.assertTrue(out["stopped"])
        time.sleep(0.5)
        left = subprocess.run(["pgrep", "-f", str(self.tmp / "xtool")], capture_output=True, text=True).stdout
        self.assertEqual(left.strip(), "")
        hist = self.helper("build", "history", "--limit", "1")["data"]["builds"][0]
        self.assertEqual(hist["result"], "cancelled")

    def test_owner_death_stops_the_watcher(self):
        self.start()
        self.assertTrue(self.helper("watch", "status")["data"]["watching"])
        self.owner.kill()
        self.owner.wait()
        deadline = time.monotonic() + 8
        st = {}
        while time.monotonic() < deadline:
            st = self.helper("watch", "status")["data"]
            if not st.get("watching"):
                break
            time.sleep(0.2)
        self.assertEqual((st["state"], st["stopReason"]), ("stopped", "no-owner"))

    def test_one_watcher_starting_another_project_replaces_it(self):
        other = make_project(self.tmp, "Other")
        first = self.start()["data"]
        second = self.start(other)["data"]
        self.assertEqual(second["path"], str(other))
        self.assertEqual(second["replaced"]["path"], str(self.p))
        self.assertNotEqual(first["pid"], second["pid"])
        again = self.start(other)["data"]                       # the same project: joins it
        self.assertTrue(again["attached"])
        self.assertEqual(again["pid"], second["pid"])
        changed = self.start(other, None, "--auto-run", "--debounce-ms", "900")["data"]
        self.assertEqual((changed["pid"], changed["autoRun"], changed["debounceMs"]), (second["pid"], True, 900))
        # only what's passed changes: no flags keep both, --auto-run false keeps the debounce
        kept = self.start(other)["data"]
        self.assertEqual((kept["autoRun"], kept["debounceMs"]), (True, 900))
        off = self.start(other, None, "--auto-run", "false")["data"]
        self.assertEqual((off["autoRun"], off["debounceMs"]), (False, 900))
        on = self.start(other, None, "--auto-run")["data"]
        self.assertEqual((on["autoRun"], on["debounceMs"], on["pid"]), (True, 900, second["pid"]))
        st = self.helper("watch", "status", "--project", str(self.p))["data"]
        self.assertFalse(st["watching"])
        self.assertEqual(st["other"]["path"], str(other))
        self.assertFalse(self.helper("watch", "stop", "--project", str(self.p))["data"]["stopped"])

    def test_dead_watcher_is_detected(self):
        out = self.start()["data"]
        os.kill(out["pid"], signal.SIGKILL)
        time.sleep(0.3)
        st = self.helper("watch", "status")["data"]
        self.assertFalse(st["watching"])
        self.assertEqual(st["stopReason"], "watcher-died")
        again = self.start()["data"]                            # a new one starts cleanly
        self.assertIn(again["state"], ("watching", "compiling"))   # the first build may have started
        self.assertNotEqual(again["pid"], out["pid"])

    def test_follower_streams_and_detaches(self):
        p = subprocess.Popen(HELPER + ["watch", "run", "--project", str(self.p)], env=self.env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
        try:
            states = []
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                ev = json.loads(p.stdout.readline())
                states.append(ev.get("state"))
                if ev.get("state") == "ok":
                    break
            self.assertEqual(states[0], "watching")
            self.assertIn("compiling", states)
            self.assertEqual(states[-1], "ok")
            p.stdin.write('{"cmd":"quit"}\n')
            p.stdin.flush()
            tail = [json.loads(ln) for ln in p.stdout.read().splitlines()]
            self.assertEqual(tail[-1]["event"], "done")
            self.assertTrue(tail[-1]["data"]["detached"])
        finally:
            p.kill()
            p.wait()
            p.stdin.close()
            p.stdout.close()
        deadline = time.monotonic() + 8                          # it was the only owner
        while time.monotonic() < deadline and self.helper("watch", "status")["data"].get("watching"):
            time.sleep(0.2)
        self.assertEqual(self.helper("watch", "status")["data"]["stopReason"], "no-owner")

    def test_missing_project(self):
        out = self.helper("watch", "run", "--project", str(self.tmp / "nope"), "--detach")
        self.assertEqual(out["error"]["code"], "not-found")
        self.assertEqual(self.helper("watch", "run")["error"]["code"], "usage")

    def test_demo_cycle(self):
        env = dict(self.env, IOSSTUDIO_DEMO="ready")
        out = self.helper("watch", "run", "--project", "/home/demo/Developer/HabitTracker", "--detach", "--owner",
                          str(self.owner.pid), env=env)["data"]
        self.assertEqual(out["state"], "watching")
        st = self.helper("watch", "status", "--wait-seconds", "10", env=env)["data"]
        self.assertEqual(st["state"], "failed")
        self.assertEqual(st["lastBuild"]["errors"], 1)
        self.assertEqual(st["lastBuild"]["diagnostics"][0]["message"], "cannot find 'habbits' in scope")
        self.assertFalse(self.helper("watch", "status")["data"].get("watching"))   # real dir is separate
        self.assertTrue(self.helper("watch", "stop", env=env)["data"]["stopped"])
