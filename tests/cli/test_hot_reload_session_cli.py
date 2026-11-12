"""`hotreload run|status|stop|pause|resume` with the helper as a subprocess: the demo stream and its control from a
second process, then real mode with fakes at the process boundary (tests/support/hot_reload_session.py: fake
xtool for `build run`, fake swiftc/ld/rcodesign under the real patch pipeline, a fake device daemon answering
apps.list, app.launch and hotreload.*).

    .venv/bin/pytest -q tests/cli/test_hot_reload_session_cli.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from support import HELPER_PY, isolated_env
from support.hot_reload import OPENSSL
from support.hot_reload_session import CARD, INSTALLED, UDID, SessionWorld
from support.hot_reload_setup import FAKE_SWIFT

DEMO_PROJECT = "/home/me/Developer/WeatherGlance"


class Stream:
    """A `hotreload run` process and the JSON lines it printed (read in a thread)."""

    def __init__(self, project: str, env: dict, *extra: str):
        self.proc = subprocess.Popen(HELPER_PY + ["hotreload", "run", "--project", project, *extra], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                self.events.append(ev)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.events)

    def wait_for(self, pred, timeout: float = 20, what: str = "") -> dict:
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            evs = self.snapshot()
            for ev in evs[seen:]:
                if pred(ev):
                    return ev
            seen = len(evs)
            if self.proc.poll() is not None and seen == len(self.snapshot()):
                break
            time.sleep(0.05)
        last = [(e.get("state"), e.get("message")) for e in self.snapshot()[-5:]]
        raise AssertionError(f"no event: {what}; last: {last}; stderr: {self.stderr_tail()}")

    def state(self, name: str, timeout: float = 20) -> dict:
        return self.wait_for(lambda e: e.get("state") == name, timeout, f"state {name}")

    def done(self, timeout: float = 20) -> dict:
        ev = self.wait_for(lambda e: e.get("event") == "done", timeout, "done")
        self.proc.wait(10)
        return ev

    def stderr_tail(self) -> str:
        if self.proc.poll() is None:
            return ""
        return (self.proc.stderr.read() or "")[-2000:]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        for f in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except OSError:
                pass


def helper(env: dict, *args: str, timeout: float = 60) -> dict:
    r = subprocess.run(HELPER_PY + list(args), env=env, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    lines = [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]
    assert lines, r.stdout + r.stderr
    return lines[-1]


# ================================================================ demo mode

class DemoCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = isolated_env(self.tmp, IOSSTUDIO_DEMO="1", IOSSTUDIO_RUNTIME_DIR=str(self.tmp / "rt"),
                                IOSSTUDIO_DEMO_DELAY="0.15")
        self.s = Stream(DEMO_PROJECT, self.env)
        self.addCleanup(self.s.close)

    def hr(self, *args: str) -> dict:
        return helper(self.env, "hotreload", *args)

    def test_stream_and_control_from_another_process(self):
        first = self.s.wait_for(lambda e: e.get("event") == "hotreload", what="the first snapshot")
        for k in ("seq", "state", "message", "project", "path", "pid", "startedAt", "updatedAt", "reloads", "notice",
                  "progress", "lastReload", "lastError", "paused", "pending", "pendingCount", "bundleId"):
            self.assertIn(k, first)
        self.assertEqual((first["state"], first["project"], first["path"]),
                         ("starting", "WeatherGlance", DEMO_PROJECT))
        building = self.s.wait_for(lambda e: e.get("state") == "building" and (e.get("progress") or {}).get("fraction"))
        self.assertIn("detail", building["progress"])
        for name in ("installing", "launching", "watching", "reloading"):
            self.s.state(name)
        ok = self.s.state("reloaded")
        r = ok["lastReload"]
        self.assertEqual(set(r["steps"]), {"check", "compile", "link", "sign", "push", "apply"})
        self.assertEqual((r["ok"], r["files"]), (True, ["Sources/WeatherGlance/ContentView.swift"]))
        failed = self.s.state("failed")
        e = failed["lastError"]
        self.assertEqual((e["code"], e["blockers"][0]["kind"], e["blockers"][0]["name"], e["fix"]["label"]),
                         ("unsupported-change", "stored-property", "count", "Run again"))

        busy = helper(self.env, "hotreload", "run", "--project", DEMO_PROJECT)
        self.assertEqual((busy["event"], busy["ok"], busy["error"]["code"]), ("done", False, "busy"))

        res = self.hr("pause", "--project", DEMO_PROJECT)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["data"]["paused"], res["data"]["session"]["paused"]), (True, True))
        self.s.wait_for(lambda e: e.get("state") == "paused" and e.get("pendingCount", 0) >= 1, what="a pending save")
        st = self.hr("status", "--project", DEMO_PROJECT)["data"]
        self.assertEqual((st["enabled"], st["mode"], st["session"]["paused"]), (True, "on", True))
        self.assertTrue(st["session"]["running"])
        self.assertIn("history", st["session"])
        after = max(h["seq"] for h in st["session"]["history"])

        res = self.hr("resume", "--project", DEMO_PROJECT)
        self.assertEqual(res["data"]["paused"], False)
        st = self.hr("status", "--project", DEMO_PROJECT, "--wait-seconds", "15", "--after-seq", str(after))["data"]
        self.assertFalse(st["timedOut"])
        self.assertGreater(max(h["seq"] for h in st["session"]["history"]), after)

        everything = self.hr("status")["data"]
        listed = everything["sessions"]
        self.assertEqual([x["path"] for x in listed], [DEMO_PROJECT])
        self.assertIn({"enabled": True, "mode": "on"}, everything["projects"].values())
        self.assertNotIn("history", listed[0])

        stop = self.hr("stop", "--project", DEMO_PROJECT)["data"]
        self.assertTrue(stop["stopped"])
        self.assertEqual(stop["session"]["state"], "stopped")
        done = self.s.done()
        self.assertTrue(done["ok"])
        self.assertEqual((done["data"]["state"], done["data"]["stopReason"]), ("stopped", "stopped"))
        self.assertEqual(self.s.proc.returncode, 0)
        self.assertFalse(self.hr("stop", "--project", DEMO_PROJECT)["data"]["stopped"])

    def test_cancel_on_stdin(self):
        self.s.state("building")
        self.s.proc.stdin.write('{"cancel": true}\n')
        self.s.proc.stdin.flush()
        done = self.s.done(10)
        self.assertTrue(done["ok"])
        self.assertEqual(done["data"]["stopReason"], "stopped")


# ================================================================ real mode with fakes

class RealCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="hr-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.w = SessionWorld(self.tmp)
        self.env = self.w.env

    def hr(self, *args: str, env: dict | None = None) -> dict:
        return helper(env or self.env, "hotreload", *args)

    def start(self, env: dict | None = None) -> Stream:
        s = Stream(str(self.w.proj), env or self.env)
        self.addCleanup(s.close)
        return s

    def test_reload_on_save(self):
        with self.w.deviced() as d:
            s = self.start()
            watching = s.state("watching", 30)
            self.assertEqual((watching["bundleId"], watching["udid"]), (INSTALLED, UDID))
            notices = {(e.get("notice") or {}).get("id") for e in s.snapshot()}
            self.assertIn("runtime-updated", notices)         # .iosstudio/hot-reload was copied in
            st = self.hr("status", "--project", str(self.w.proj))["data"]
            self.assertEqual((st["enabled"], st["mode"], st["session"]["state"]), (True, "on", "watching"))
            helper(self.env, "projects", "add", "--path", str(self.w.proj))
            projects = self.hr("status")["data"]["projects"]
            self.assertEqual(projects[str(self.w.proj)], {"enabled": True, "mode": "on"})
            launch = d.sent("app.launch")[0]
            self.assertEqual((launch["bundleId"], launch["udid"], launch["console"]), (INSTALLED, UDID, True))
            self.assertEqual(d.sent("hotreload.clean")[0]["bundleId"], INSTALLED)

            self.w.card.write_text(CARD.replace('Text("Hello")', 'Text("Changed")', 1))
            st = self.hr("status", "--project", str(self.w.proj), "--wait-seconds", "20", "--after-seq", "0")["data"]
            self.assertFalse(st["timedOut"], st)
            r = st["session"]["lastReload"]
            self.assertEqual((r["seq"], r["ok"], r["files"], r["swapped"]), (1, True, ["Sources/App/Card.swift"], 1))
            self.assertEqual(set(r["steps"]), {"check", "compile", "link", "sign", "push", "apply"})
            push = d.sent("hotreload.push")[0]
            self.assertEqual((push["seq"], push["bundleId"], push["waitMs"]), (1, INSTALLED, 5000))
            self.assertTrue(push["src"].endswith("patches/patch-1.dylib"))
            self.assertEqual(push["src"], os.path.realpath(push["src"]))
            self.assertEqual(push["src"].split("/")[-3], push["key"])
            if OPENSSL:
                self.assertTrue(any(c.startswith("sign ") for c in self.w.calls()))

            self.w.card.write_text(CARD.replace("struct Card: View {", "struct Card: View {\n    @State var count = 0"))
            st = self.hr("status", "--project", str(self.w.proj), "--wait-seconds", "20", "--after-seq", "1")["data"]
            self.assertFalse(st["timedOut"], st)
            self.assertEqual(st["session"]["state"], "failed")
            self.assertEqual(st["session"]["lastError"]["blockers"][0]["name"], "count")
            self.assertEqual(len(d.sent("hotreload.push")), 1)

            hist = helper(self.env, "build", "history", "--limit", "1")["data"]["builds"][0]
            self.assertEqual((hist["kind"], hist["trigger"], hist["result"]), ("run", "hot-reload", "ok"))

            stop = self.hr("stop", "--project", str(self.w.proj))["data"]
            self.assertTrue(stop["stopped"])
            done = s.done()
            self.assertTrue(done["ok"])
            self.assertEqual([h["seq"] for h in done["data"]["history"]], [1, 2])

    def test_a_save_during_the_build_reloads_at_once(self):
        changed = CARD.replace('Text("Hello")', 'Text("Saved while building")', 1)
        with self.w.deviced() as d:
            s = self.start(dict(self.env, FAKE_XTOOL_SECONDS="0.5", FAKE_XTOOL_EDIT=f"{self.w.card}::{changed}"))
            s.state("watching", 30)
            ok = s.state("reloaded", 20)          # nobody saved after the start: the build-time save is the patch
            self.assertEqual(ok["lastReload"]["files"], ["Sources/App/Card.swift"])
            notices = [e["notice"] for e in s.snapshot() if e.get("notice")]
            self.assertIn("changed-while-building", [n["id"] for n in notices])
            self.assertEqual(len(d.sent("hotreload.push")), 1)
            cache = Path(self.env["XDG_CACHE_HOME"]) / "ios-studio/hot-reload"
            copies = list(cache.glob("*/sources/Sources/App/Card.swift"))
            self.assertEqual([c.read_text() for c in copies], [CARD])      # the baseline is what was built
            self.assertEqual([x.name for x in cache.iterdir() if x.name.startswith(".")], [])

            # a Swift file of another target: a notice, nothing compiled or pushed
            kit = self.w.proj / "Sources/Kit/Model.swift"
            kit.parent.mkdir()
            kit.write_text("struct Model {}\n")
            ev = s.wait_for(lambda e: (e.get("notice") or {}).get("id") == "other-target", what="other-target")
            self.assertIn("Changes in Kit need a full Run", ev["notice"]["message"])
            self.assertEqual(ev["notice"]["fix"]["action"], "hot-reload-restart")
            time.sleep(1)
            self.assertEqual(len(d.sent("hotreload.push")), 1)
            self.assertNotIn("Back to the built version", [e.get("message") for e in s.snapshot()])
            s.proc.send_signal(signal.SIGTERM)
            self.assertTrue(s.done()["ok"])

    def test_a_stored_property_added_during_the_build_is_caught(self):
        changed = CARD.replace("struct Card: View {", "struct Card: View {\n    @State var count = 0")
        with self.w.deviced() as d:
            s = self.start(dict(self.env, FAKE_XTOOL_SECONDS="0.5", FAKE_XTOOL_EDIT=f"{self.w.card}::{changed}"))
            failed = s.state("failed", 30)
            self.assertEqual(failed["lastError"]["blockers"][0]["name"], "count")
            self.assertEqual(d.sent("hotreload.push"), [])
            s.proc.send_signal(signal.SIGTERM)
            self.assertTrue(s.done()["ok"])

    def test_status_and_stop_leave_no_lock_file(self):
        other = str(self.tmp / "Elsewhere")
        self.hr("status", "--project", other)
        self.hr("stop", "--project", other)
        self.assertEqual(list((self.tmp / "rt").glob("**/hotreload-*.lock")), [])

    def test_build_failure_ends_the_session(self):
        with self.w.deviced():
            s = self.start(dict(self.env, FAKE_XTOOL_FAIL="1"))
            done = s.done(30)
        self.assertFalse(done["ok"])
        self.assertEqual(done["data"]["stopReason"], "build-failed")
        err = done["error"]
        self.assertTrue(err["message"])
        self.assertTrue(any("BROKEN" in d["message"] for d in err.get("diagnostics") or []), err)

    def test_not_enabled(self):
        w = SessionWorld(self.tmp / "off", enabled=False)
        s = Stream(str(w.proj), w.env)
        self.addCleanup(s.close)
        done = s.done()
        self.assertEqual((done["ok"], done["error"]["code"], done["error"]["fix"]["action"]),
                         (False, "not-enabled", "hot-reload-enable"))
        self.assertEqual(done["data"]["stopReason"], "error")

    def test_app_removed_from_the_phone_ends_the_session(self):
        self.w.push_mode = "gone"
        with self.w.deviced():
            s = self.start()
            s.state("watching", 30)
            self.w.card.write_text(CARD.replace("Hello", "Hi"))
            done = s.done()
        self.assertTrue(done["ok"])
        self.assertEqual((done["data"]["stopReason"], done["data"]["lastError"]["code"]), ("app-exited", "not-installed"))

    def test_no_ack_keeps_watching(self):
        self.w.push_mode = "noack"
        with self.w.deviced():
            s = self.start()
            s.state("watching", 30)
            self.w.card.write_text(CARD.replace("Hello", "Hi"))
            failed = s.state("failed")
            self.assertEqual(failed["lastError"]["code"], "app-not-running")
            self.w.push_mode = "load-failed"
            self.w.card.write_text(CARD.replace("Hello", "Hey"))
            failed = s.wait_for(lambda e: (e.get("lastError") or {}).get("code") == "load-failed", what="load-failed")
            self.assertIn("symbol not found", failed["lastError"]["message"])
            s.proc.send_signal(signal.SIGTERM)
            self.assertTrue(s.done()["ok"])

    def test_stop_kills_the_running_build(self):
        with self.w.deviced():
            s = self.start(dict(self.env, FAKE_XTOOL_SECONDS="60"))
            s.state("building", 30)
            xtool = str(self.tmp / "xtool")
            running = lambda: subprocess.run(["pgrep", "-f", xtool], capture_output=True,  # noqa: E731
                                             text=True).stdout.strip()
            deadline = time.monotonic() + 20
            while not running() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(running(), "the fake xtool never ran")
            t0 = time.monotonic()
            self.assertTrue(self.hr("stop", "--project", str(self.w.proj))["data"]["stopped"])
            self.assertLess(time.monotonic() - t0, 10)
            s.done()
            deadline = time.monotonic() + 10
            while running() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(running(), "the build's xtool outlived the session")

    def test_disable_stops_the_session_first(self):
        env = dict(self.env, IOSSTUDIO_SWIFT=str(FAKE_SWIFT))
        with self.w.deviced():
            s = self.start(env)
            s.state("watching", 30)
            res = self.hr("disable", "--project", str(self.w.proj), env=env)
            self.assertTrue(res["ok"], res)
            self.assertEqual((res["data"]["sessionStopped"], res["data"]["mode"]), (True, "off"))
            self.assertTrue(s.done()["ok"])
        self.assertNotIn("iOS Studio hot reload", (self.w.proj / "Package.swift").read_text())

    def test_a_killed_session_reads_as_died(self):
        with self.w.deviced():
            s = self.start()
            s.state("watching", 30)
            s.proc.kill()
            s.proc.wait(10)
        st = self.hr("status", "--project", str(self.w.proj))["data"]["session"]
        self.assertEqual((st["state"], st["stopReason"], st["running"]), ("stopped", "session-died", False))


if __name__ == "__main__":
    unittest.main()
