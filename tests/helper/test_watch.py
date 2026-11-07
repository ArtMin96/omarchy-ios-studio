"""Watch mode (helper/iosstudio/features/watch.py): the debounce/supersede scheduler on a fake clock, which files count as a change,
atomic saves seen through real inotify in a temp folder, the polling fallback, the watcher loop with fake builds
(supersede, busy retry, auto-run skips), the lock and stale-lock detection and owners. The CLI, demo mode and
compiles through the real `build compile` with a fake xtool are in tests/cli/test_watch_cli.py. Stdlib only:
    python -m pytest tests/helper/test_watch.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from iosstudio.features import watch as W
from support.projects import app_project as make_project
from support.projects import atomic_save

_RT = os.environ["IOSSTUDIO_RUNTIME_DIR"]   # a temp folder (tests/conftest.py)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


# ---------------------------------------------------------------- scheduler

class SchedulerTests(unittest.TestCase):
    def test_rapid_saves_make_one_build(self):
        s = W.Scheduler(0.6)
        t = 0.0
        for i in range(5):
            self.assertFalse(s.changed([f"Sources/A/F{i}.swift"], t))
            self.assertIsNone(s.tick(t + 0.3))   # still inside the debounce
            t += 0.3
        self.assertAlmostEqual(s.due_in(t), 0.3, places=5)
        files = s.tick(t + 0.31)
        self.assertEqual(len(files), 5)
        self.assertIsNone(s.tick(t + 5))         # building: nothing else starts
        s.finished()
        self.assertIsNone(s.tick(t + 10))        # nothing pending

    def test_saves_that_never_pause_still_compile(self):
        s = W.Scheduler(0.6, max_delay=2.0)
        t = 0.0
        started = None
        while t < 3:
            s.changed(["Sources/A/F.swift"], t)
            if s.tick(t) is not None:
                started = t
                break
            t += 0.2
        self.assertIsNotNone(started)
        self.assertLessEqual(started, 2.21)   # by the first tick after 2 s

    def test_change_during_build_supersedes_once_then_builds_again(self):
        s = W.Scheduler(0.5)
        s.changed(["Sources/A/F.swift"], 0)
        self.assertEqual(s.tick(0.5), ["Sources/A/F.swift"])
        self.assertTrue(s.changed(["Sources/A/G.swift"], 1.0))    # cancel the running build
        self.assertFalse(s.changed(["Sources/A/H.swift"], 1.2))   # already cancelling
        self.assertEqual(s.superseded, 1)
        self.assertIsNone(s.tick(5))                                # never two builds at once
        s.finished()
        self.assertEqual(s.tick(5), ["Sources/A/G.swift", "Sources/A/H.swift"])
        s.finished()
        self.assertIsNone(s.tick(10))

    def test_endless_saves_still_give_a_result(self):
        """After 2 builds in a row were cancelled by saves, the next one finishes; then one more follows."""
        s = W.Scheduler(0.5)
        t = 0.0
        s.changed(["F"], t)
        results = []
        for build in range(4):                 # saves every 0.3 s, builds take 3 s
            while s.tick(t) is None:
                t += 0.1
            start = t
            cancelled = False
            while t < start + 3:
                t += 0.3
                if s.changed(["F"], t):
                    cancelled = True
                    break
            s.finished(superseded=cancelled)
            results.append(cancelled)
        self.assertEqual(results[:3], [True, True, False])   # the third build ran to the end
        self.assertTrue(s.pending)                            # and the saves during it build again
        self.assertEqual(s.streak, 1)                          # the counting starts over

    def test_protected_build_ignores_saves_and_resets(self):
        s = W.Scheduler(0.5, supersede_limit=2)
        s.streak = 2
        s.changed(["F"], 0)
        s.tick(1)
        self.assertTrue(s.protected)
        self.assertFalse(s.changed(["G"], 1.5))   # no cancel
        self.assertIsNone(s.tick(5))               # still one build at a time
        s.finished()                                # finished normally
        self.assertEqual((s.streak, s.protected), (0, False))
        self.assertEqual(s.tick(5), ["G"])         # the save during it builds after the debounce
        self.assertFalse(s.protected)

    def test_request_builds_without_a_change(self):
        s = W.Scheduler(0.6)
        s.request(10.0)
        self.assertEqual(s.tick(10.0), [])
        s.finished()
        s.request(20.0, delay=2.0)        # busy retry
        self.assertIsNone(s.tick(21.0))
        self.assertEqual(s.tick(22.0), [])


# ---------------------------------------------------------------- what counts

class IgnoreTests(unittest.TestCase):
    def test_relevant_files(self):
        for rel in ("Package.swift", "xtool.yml", "Info.plist", "App.entitlements", "Sources/A/ContentView.swift",
                    "Sources/A/Assets.xcassets/Icon.png", "Sources/A/sub/deep/Model.swift"):
            self.assertTrue(W.relevant(rel), rel)

    def test_ignored_files(self):
        for rel in ("Package.resolved", "README.md", "Tests/AppTests/T.swift", ".build/debug/x.o",
                    "Sources/A/.ContentView.swift.swp", "Sources/A/ContentView.swift~", "Sources/A/4913",
                    "Sources/A/.tmpXyZ12", "Sources/A/ContentView.swift.tmp", "Sources/A/F.swift.tmp.1234",
                    "Sources/A/F.swift___jb_tmp___", "Sources/A/#F.swift#", "Sources/A/.#F.swift",
                    "Sources/.hidden/F.swift", "Sources/A/.build/x.swift", "xtool/App.app/Info.plist",
                    ".bsp/xtool.json", "Sources/A/F.swp", "Sources/A/F.swift.crswap"):
            self.assertFalse(W.relevant(rel), rel)

    def test_same_content_is_not_a_change(self):
        with tempfile.TemporaryDirectory() as d:
            p = make_project(Path(d))
            f = "Sources/App/ContentView.swift"
            flt = W.ContentFilter(p)
            flt.prime()
            atomic_save(p / f, (p / f).read_text())          # :w without edits
            self.assertEqual(flt.changed([f]), [])
            atomic_save(p / f, "let x = 2\n")
            self.assertEqual(flt.changed([f]), [f])
            (p / f).unlink()
            self.assertEqual(flt.changed([f]), [f])           # a deleted file is a change
            self.assertEqual(flt.changed(["Sources/App/Gone.swift"]), [])   # never existed


# ---------------------------------------------------------------- watchers

def drain(w, seconds: float = 1.0) -> set[str]:
    out: set[str] = set()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        out.update(w.read())
        time.sleep(0.05)
    return out


class InotifyTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.p = make_project(Path(self.d.name))
        try:
            self.w = W.InotifyWatcher(self.p)
        except OSError as exc:
            self.skipTest(f"inotify not available: {exc}")

    def tearDown(self):
        self.w.close()
        self.d.cleanup()

    def test_atomic_save_is_seen_as_the_real_file(self):
        atomic_save(self.p / "Sources/App/ContentView.swift", "let x = 2\n")
        self.assertEqual(drain(self.w, 0.5), {"Sources/App/ContentView.swift"})

    def test_vim_style_save_with_backup_and_probe(self):
        f = self.p / "Sources/App/ContentView.swift"
        os.rename(f, f.with_name("ContentView.swift~"))      # backupcopy=no: original becomes the backup
        (f.parent / "4913").write_text("")                    # Vim's write-permission probe
        (f.parent / "4913").unlink()
        (f.parent / ".ContentView.swift.swp").write_text("swap")
        f.write_text("let x = 3\n")
        self.assertEqual(drain(self.w, 0.5), {"Sources/App/ContentView.swift"})

    def test_in_place_write_and_root_files(self):
        (self.p / "Sources/App/ContentView.swift").write_text("let x = 4\n")   # VS Code
        atomic_save(self.p / "xtool.yml", "version: 1\nbundleID: com.example.B\n", ".xtool.yml.tmp")
        (self.p / "Package.resolved").write_text("{}")
        (self.p / ".build").mkdir()
        (self.p / ".build" / "x.o").write_text("")
        self.assertEqual(drain(self.w, 0.5), {"Sources/App/ContentView.swift", "xtool.yml"})

    def test_new_folders_are_watched_too(self):
        sub = self.p / "Sources/App/Feature/Deep"
        sub.mkdir(parents=True)                       # created and filled before its watch may exist
        (sub / "Model.swift").write_text("struct M {}\n")
        got = drain(self.w, 0.5)
        self.assertTrue(got & {"Sources/App/Feature", "Sources/App/Feature/Deep", "Sources/App/Feature/Deep/Model.swift"}, got)
        atomic_save(sub / "Model.swift", "struct M { let a = 1 }\n")
        self.assertIn("Sources/App/Feature/Deep/Model.swift", drain(self.w, 0.5))

    def test_sources_folder_created_later(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Bare"
            p.mkdir()
            (p / "Package.swift").write_text("// x\n")
            w = W.InotifyWatcher(p)
            try:
                (p / "Sources/Bare").mkdir(parents=True)
                drain(w, 0.3)
                (p / "Sources/Bare/A.swift").write_text("let a = 1\n")
                self.assertIn("Sources/Bare/A.swift", drain(w, 0.5))
            finally:
                w.close()


class PollTests(unittest.TestCase):
    def test_polling_finds_changes(self):
        with tempfile.TemporaryDirectory() as d:
            p = make_project(Path(d))
            clock = FakeClock()
            w = W.PollWatcher(p, interval=1.0, clock=clock)
            f = p / "Sources/App/ContentView.swift"
            atomic_save(f, "let x = 22\n")
            st = f.stat()
            os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
            self.assertEqual(w.read(), [])           # not yet: once per interval
            clock.advance(1.1)
            self.assertEqual(w.read(), ["Sources/App/ContentView.swift"])
            (p / "Sources/App/New.swift").write_text("x")
            (p / "Sources/App/.New.swift.swp").write_text("x")
            clock.advance(1.1)
            self.assertEqual(w.read(), ["Sources/App/New.swift"])
            clock.advance(1.1)
            self.assertEqual(w.read(), [])

    def test_forced_polling(self):
        with tempfile.TemporaryDirectory() as d:
            p = make_project(Path(d))
            os.environ["IOSSTUDIO_WATCH_POLL"] = "1"
            try:
                w, why = W.make_watcher(p)
            finally:
                os.environ.pop("IOSSTUDIO_WATCH_POLL")
            self.assertEqual((w.mode, why), ("poll", "forced"))


# ---------------------------------------------------------------- the watcher loop with fake builds

class FakeBuild:
    def __init__(self, clock, kind, build_id, extra, seconds=5.0, done=None):
        self.clock, self.kind, self.build_id, self.extra = clock, kind, build_id, extra
        self.end = clock() + seconds
        self.cancelled = False
        self.log_file = f"/logs/{build_id}.log"
        self._done = done or {"event": "done", "ok": True, "data": {"errors": 0, "warnings": 1, "duration": seconds,
                                                                   "diagnostics": [], "logFile": self.log_file}}

    def fileno(self):
        return None

    def read_events(self):
        return []

    def finished(self):
        return self.cancelled or self.clock() >= self.end

    def enforce(self):
        pass

    def cancel(self):
        self.cancelled = True

    def kill(self):
        self.cancelled = True

    def result(self):
        if self.cancelled:
            return {"event": "done", "ok": False, "error": {"code": "cancelled", "message": "Build cancelled."}}
        return self._done


class FakeWatcher:
    mode = "inotify"

    def __init__(self):
        self.queue: list[str] = []

    def fileno(self):
        return None

    def read(self, now=None):
        out, self.queue = self.queue, []
        return out

    def close(self):
        pass


FAILED = {"event": "done", "ok": False, "error": {"code": "build-failed", "message": "The build failed.", "build": {
    "errors": 1, "warnings": 0, "duration": 3.0, "diagnostics": [
        {"severity": "warning", "message": "unused", "file": "/p/Sources/App/A.swift", "line": 1, "col": 1},
        {"severity": "error", "message": "cannot find 'Txt' in scope", "file": "/p/Sources/App/A.swift", "line": 11,
         "col": 13, "category": "compiler"}]}}}


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        os.environ["IOSSTUDIO_RUNTIME_DIR"] = str(Path(self.d.name) / "rt")
        self.p = make_project(Path(self.d.name))
        self.f = self.p / "Sources/App/ContentView.swift"
        self.clock = FakeClock()
        self.fw = FakeWatcher()
        self.builds: list[FakeBuild] = []
        self.next_done = None
        self.seconds = 5.0

    def tearDown(self):
        os.environ["IOSSTUDIO_RUNTIME_DIR"] = _RT
        self.d.cleanup()

    def factory(self, kind, bid, extra):
        b = FakeBuild(self.clock, kind, bid, extra, self.seconds, self.next_done)
        self.builds.append(b)
        return b

    def watcher(self, **kw) -> W.Watcher:
        w = W.Watcher(self.p, debounce_ms=600, build_factory=self.factory, watcher=self.fw, clock=self.clock,
                      owner_check=False, **kw)
        w.setup()
        return w

    def save(self, text: str) -> None:
        atomic_save(self.f, text)
        self.fw.queue.append("Sources/App/ContentView.swift")

    def run_for(self, w, seconds: float, dt: float = 0.1) -> None:
        end = self.clock.t + seconds
        while self.clock.t < end:
            w.step(timeout=0)
            self.clock.advance(dt)

    def test_initial_build_then_save_builds_once(self):
        w = self.watcher()
        self.run_for(w, 6)
        self.assertEqual(len(self.builds), 1)
        self.assertEqual(w.snap["state"], "ok")
        self.assertEqual(w.snap["lastBuild"]["warnings"], 1)
        for i in range(4):
            self.save(f"let x = {i + 10}\n")
            self.run_for(w, 0.2)
        self.assertEqual(w.snap["state"], "changed")
        self.run_for(w, 7)
        self.assertEqual(len(self.builds), 2)
        st = json.loads(W.status_path().read_text())
        self.assertEqual((st["state"], st["lastBuild"]["id"]), ("ok", self.builds[1].build_id))
        self.assertTrue(self.builds[1].build_id.startswith("w-"))

    def test_save_without_edits_does_nothing(self):
        w = self.watcher(initial_build=False)
        self.save(self.f.read_text())
        self.run_for(w, 2)
        self.assertEqual(self.builds, [])
        self.assertEqual(w.snap["state"], "watching")

    def test_change_during_build_cancels_and_builds_after(self):
        w = self.watcher(initial_build=False)
        self.save("let x = 2\n")
        self.run_for(w, 1.5)
        self.assertEqual(w.snap["state"], "compiling")
        self.save("let x = 3\n")
        self.run_for(w, 0.2)
        self.assertTrue(self.builds[0].cancelled)
        self.run_for(w, 7)
        self.assertEqual(len(self.builds), 2)
        self.assertFalse(self.builds[1].cancelled)
        self.assertEqual(w.snap["state"], "ok")
        self.assertEqual(w.snap["superseded"], 1)

    def test_endless_saves_in_the_loop(self):
        w = self.watcher(initial_build=False)
        for i in range(60):                        # a save every 0.3 s for 18 s; builds take 5 s
            self.save(f"let x = {i + 100}\n")
            self.run_for(w, 0.3)
        self.assertEqual([b.cancelled for b in self.builds[:3]], [True, True, False])
        self.assertEqual(w.snap.get("lastBuild", {}).get("id"), self.builds[2].build_id)
        self.run_for(w, 12)                          # saves stopped: one more build of the latest state
        self.assertEqual(w.snap["state"], "ok")
        self.assertFalse(self.builds[-1].cancelled)

    def test_failed_build_reports_first_errors(self):
        self.next_done = FAILED
        w = self.watcher()
        self.run_for(w, 6)
        self.assertEqual(w.snap["state"], "failed")
        self.assertEqual(w.snap["message"], "1 error")
        d = w.snap["lastBuild"]["diagnostics"]
        self.assertEqual(d[0]["severity"], "error")         # errors first
        self.assertEqual((d[0]["line"], d[0]["col"]), (11, 13))
        self.assertEqual(w.snap["lastBuild"]["errorCode"], "build-failed")

    def test_busy_project_retries(self):
        self.next_done = {"event": "done", "ok": False, "error": {"code": "busy", "message": "busy"}}
        w = self.watcher()
        self.run_for(w, 5.5)
        self.assertEqual(w.snap["reason"], "busy")
        self.next_done = None
        self.run_for(w, 8)
        self.assertEqual(w.snap["state"], "ok")
        self.assertEqual(len(self.builds), 2)

    def test_auto_run_skipped_without_iphone(self):
        w = self.watcher(auto_run=True, device_check=lambda: (None, "no iPhone has been connected yet"),
                         auth_check=lambda: True)
        self.run_for(w, 6)
        w.run_job.join(3)
        self.assertEqual(w.snap["state"], "ok")
        self.assertEqual(w.snap["run"]["state"], "skipped")
        self.assertIn("no iPhone", w.snap["run"]["message"])
        self.assertEqual([b.kind for b in self.builds], ["compile"])

    def test_auto_run_skipped_when_not_signed_in(self):
        w = self.watcher(auto_run=True, device_check=lambda: ("U1", None), auth_check=lambda: False)
        self.run_for(w, 6)
        w.run_job.join(3)
        self.assertIn("Apple ID", w.snap["run"]["message"])

    def test_auto_run_installs_and_launches_and_skips_while_installing(self):
        import threading
        gate = threading.Event()
        calls = []

        class Steps:
            def install(self, udid, ipa):
                calls.append(("install", udid, ipa))
                gate.wait(5)
                return {"ok": True, "data": {"bundleId": "XTL-T.com.example.App"}}

            def launch(self, udid, bundle, args, env):
                calls.append(("launch", bundle))
                return {"ok": True, "data": {"bundleId": bundle}}

        run_done = {"event": "done", "ok": True, "data": {"needsInstall": True, "ipa": "/x/App.ipa",
                                                          "bundleId": "com.example.App", "launch": {}}}

        def factory(kind, bid, extra):
            b = FakeBuild(self.clock, kind, bid, extra, 0.0 if kind == "run" else 2.0,
                          run_done if kind == "run" else None)
            self.builds.append(b)
            return b

        w = W.Watcher(self.p, debounce_ms=600, build_factory=factory, watcher=self.fw, clock=self.clock,
                      owner_check=False, auto_run=True, device_check=lambda: ("U1", None), auth_check=lambda: True,
                      run_steps=Steps())
        w.setup()
        self.run_for(w, 3)
        deadline = time.monotonic() + 5
        while w.run_job.phase != "installing" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(w.run_job.phase, "installing")
        self.assertEqual(self.builds[1].extra, ["--udid", "U1"])
        self.save("let x = 5\n")                     # compiles while the phone installs
        self.run_for(w, 4)
        self.assertEqual(w.snap["state"], "ok")
        self.assertEqual(w.snap["run"]["state"], "skipped")
        self.assertIn("still installing", w.snap["run"]["message"])
        self.assertEqual([b.kind for b in self.builds], ["compile", "run", "compile"])
        self.assertEqual(w.snap["run"]["forBuild"], self.builds[2].build_id)   # the skipped one names its compile
        gate.set()
        w.run_job.join(3)
        self.assertEqual(calls, [("install", "U1", "/x/App.ipa"), ("launch", "XTL-T.com.example.App")])
        run = w.snap["run"]
        self.assertEqual((run["state"], run["buildId"], run["forBuild"]),
                         ("launched", self.builds[1].build_id, self.builds[0].build_id))

    def test_change_cancels_a_run_that_is_still_building(self):
        def factory(kind, bid, extra):
            b = FakeBuild(self.clock, kind, bid, extra, 30.0 if kind == "run" else 1.0)
            self.builds.append(b)
            return b

        w = W.Watcher(self.p, debounce_ms=600, build_factory=factory, watcher=self.fw, clock=self.clock,
                      owner_check=False, auto_run=True, device_check=lambda: ("U1", None), auth_check=lambda: True)
        w.setup()
        self.run_for(w, 2)
        deadline = time.monotonic() + 3
        while len(self.builds) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.save("let x = 6\n")
        self.run_for(w, 0.3)
        self.assertTrue(self.builds[1].cancelled)
        w.run_job.join(3)
        self.run_for(w, 3)
        self.assertEqual([b.kind for b in self.builds][:3], ["compile", "run", "compile"])
        self.assertIn(w.snap["run"]["state"], ("checking", "building"))   # the newer compile's auto-run started

    def test_stop_cancels_running_build(self):
        w = self.watcher()
        self.run_for(w, 1)
        self.assertEqual(w.snap["state"], "compiling")
        w.stop_reason = "stopped"
        w.shutdown()
        self.assertTrue(self.builds[0].cancelled)
        st = json.loads(W.status_path().read_text())
        self.assertEqual((st["state"], st["stopReason"]), ("stopped", "stopped"))


# ---------------------------------------------------------------- lock, owners, CLI


class LockTests(unittest.TestCase):
    def test_lock_holder_is_found_and_a_free_lock_means_none(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["IOSSTUDIO_RUNTIME_DIR"] = d
            try:
                self.assertIsNone(W.watcher_pid())
                code = ("import fcntl,os,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
                        "f.seek(0); f.truncate(); f.write(str(os.getpid())); f.flush(); print('x', flush=True); "
                        "time.sleep(60)")
                p = subprocess.Popen([sys.executable, "-c", code, str(W.lock_path())], stdout=subprocess.PIPE)
                p.stdout.readline()
                self.assertEqual(W.watcher_pid(), p.pid)
                p.kill()
                p.wait()
                p.stdout.close()
                self.assertIsNone(W.watcher_pid())
            finally:
                os.environ["IOSSTUDIO_RUNTIME_DIR"] = _RT

    def test_owners_survive_only_while_alive(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["IOSSTUDIO_RUNTIME_DIR"] = d
            try:
                p = subprocess.Popen(["sleep", "60"])
                W.add_owner(os.getpid())
                W.add_owner(p.pid)
                self.assertEqual(set(W.live_owners()), {str(os.getpid()), str(p.pid)})
                p.kill()
                p.wait()
                self.assertEqual(set(W.live_owners()), {str(os.getpid())})
                W.remove_owner(os.getpid())
                self.assertEqual(W.live_owners(), {})
                o = W.read_owners()
                o["999999"] = "1"                   # a pid that's gone (or reused with another start time)
                self.assertFalse(W.pid_alive(999999, "1"))
            finally:
                os.environ["IOSSTUDIO_RUNTIME_DIR"] = _RT


class RuntimeDirTests(unittest.TestCase):
    """XDG_RUNTIME_DIR unset (the MCP SDK's stdio_client drops it): /run/user/<uid> when it's ours, else /tmp."""

    def setUp(self):
        from iosstudio.device import client as device_client
        self.dc = device_client
        self.env = {k: v for k, v in os.environ.items() if k not in ("XDG_RUNTIME_DIR", "IOSSTUDIO_RUNTIME_DIR")}

    def test_order(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/x/rt", "IOSSTUDIO_RUNTIME_DIR": "/y"}):
            self.assertEqual(str(self.dc.runtime_base()), "/y")
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/x/rt"}, clear=True):
            self.assertEqual(str(self.dc.runtime_base()), "/x/rt")

    def test_run_user_when_ours(self):
        from unittest import mock
        uid = os.getuid()
        st = os.stat_result((0o40700, 0, 0, 0, uid, uid, 0, 0, 0, 0))
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(self.dc.os, "stat", return_value=st), \
                mock.patch.object(self.dc.os, "access", return_value=True):
            self.assertEqual(self.dc.xdg_runtime_dir(), f"/run/user/{uid}")
            self.assertEqual(str(self.dc.runtime_base()), f"/run/user/{uid}")
            self.assertEqual(self.dc.socket_path(False), Path(f"/run/user/{uid}/ios-studio/deviced.sock"))

    def test_tmp_when_missing_or_not_ours(self):
        from unittest import mock
        uid = os.getuid()
        other = os.stat_result((0o40700, 0, 0, 0, uid + 1, uid + 1, 0, 0, 0, 0))
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(self.dc.os, "stat", side_effect=FileNotFoundError):
                self.assertIsNone(self.dc.xdg_runtime_dir())
                self.assertEqual(str(self.dc.runtime_base()), f"/tmp/ios-studio-{uid}")
            with mock.patch.object(self.dc.os, "stat", return_value=other):
                self.assertIsNone(self.dc.xdg_runtime_dir())

    def test_watch_uses_the_same_base(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(self.dc, "xdg_runtime_dir", return_value=d):
            self.assertEqual(W.runtime_dir(), Path(d) / "ios-studio")
            self.assertEqual(self.dc.runtime_dir(False), Path(d) / "ios-studio")


class SummaryTests(unittest.TestCase):
    def test_summarize_failed_and_ok(self):
        s = W.summarize(FAILED, "w-3", "/l.log")
        self.assertEqual((s["result"], s["errors"], s["warnings"], s["logFile"]), ("failed", 1, 0, "/l.log"))
        self.assertEqual(s["diagnostics"][0]["message"], "cannot find 'Txt' in scope")
        many = {"event": "done", "ok": True, "data": {"diagnostics": [
            {"severity": "warning", "message": f"w{i}"} for i in range(9)]}}
        s = W.summarize(many, "w-4", None)
        self.assertEqual((s["result"], s["warnings"], len(s["diagnostics"])), ("ok", 9, 5))

    def test_debounce_bounds(self):
        self.assertEqual(W.clamp_debounce("5"), W.MIN_DEBOUNCE_MS)
        self.assertEqual(W.clamp_debounce(99999), W.MAX_DEBOUNCE_MS)
        self.assertEqual(W.clamp_debounce("600"), 600)


if __name__ == "__main__":
    unittest.main()
