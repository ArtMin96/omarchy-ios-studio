"""helper/iosstudio/features/hotreload/{session,state}.py in process: the reload scheduler (debounce, one more after a
save during a reload, pause/pending/resume), a session's reloads with make_patch and the device faked (error
mapping, back to the build, the app gone), the runtime files (history bound, a dead session, wait/after-seq, busy,
pruning) and the prechecks."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import project_setup, session, state
from iosstudio.features.hotreload.patch import PatchError
from support import write_exe


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class SchedulerTests(unittest.TestCase):
    def test_debounce(self):
        s = session.ReloadScheduler(0.15)
        s.changed(["a.swift"], 0.0)
        self.assertIsNone(s.tick(0.1))
        self.assertAlmostEqual(s.due_in(0.1), 0.05)
        s.changed(["a.swift"], 0.1)               # another save pushes it back
        self.assertIsNone(s.tick(0.2))
        self.assertEqual(s.tick(0.25), ["a.swift"])
        self.assertTrue(s.running)
        self.assertIsNone(s.tick(5))

    def test_saves_that_never_settle_still_reload(self):
        s = session.ReloadScheduler(0.15, max_delay=2.0)
        for i in range(30):
            s.changed(["a.swift"], i * 0.1)
            if s.tick(i * 0.1) is not None:
                break
        self.assertLessEqual(i * 0.1, 2.0)

    def test_a_save_during_a_reload_queues_exactly_one_more(self):
        s = session.ReloadScheduler(0.15)
        s.changed(["a.swift"], 0)
        self.assertEqual(s.tick(1), ["a.swift"])
        s.changed(["b.swift"], 1.1)
        s.changed(["a.swift"], 1.2)
        s.changed(["b.swift"], 1.3)
        self.assertIsNone(s.tick(5))              # still running
        s.finished()
        self.assertEqual(s.tick(5), ["b.swift", "a.swift"])
        s.finished()
        self.assertIsNone(s.tick(10))

    def test_pause_keeps_saves_pending_and_resume_reloads_once(self):
        s = session.ReloadScheduler(0.15)
        s.pause()
        s.changed(["a.swift"], 0)
        s.changed(["b.swift"], 1)
        self.assertIsNone(s.tick(10))
        self.assertIsNone(s.due_in(10))
        self.assertEqual(s.files, ["a.swift", "b.swift"])
        s.resume(20)
        self.assertEqual(s.tick(20), ["a.swift", "b.swift"])
        s.finished()
        s.resume(30)
        self.assertIsNone(s.tick(30))

    def test_clamp_debounce(self):
        self.assertEqual(session.clamp_debounce("10"), session.MIN_DEBOUNCE_MS)
        self.assertEqual(session.clamp_debounce(99999), session.MAX_DEBOUNCE_MS)
        with self.assertRaises(HelperError):
            session.clamp_debounce("soon")


# ================================================================ a session with fakes

@dataclass
class FakeResult:
    seq: int
    dylib: str
    files: list[str]
    steps: dict = field(default_factory=lambda: {"check": 1, "compile": 2, "link": 3, "sign": 4})
    missing: list[str] = field(default_factory=list)


class FakeDevice:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.push: object = "ok"

    def request(self, cmd, args, timeout, cancel=None):
        self.calls.append((cmd, args))
        if cmd != "hotreload.push":
            return {}
        if isinstance(self.push, HelperError):
            raise self.push
        seq = args["seq"]
        if self.push == "noack":
            return {"seq": seq, "acked": False, "pushMs": 4}
        ok = self.push == "ok"
        return {"seq": seq, "acked": True, "pushMs": 4, "ackMs": 50,
                "ack": {"seq": seq, "ok": ok, "ms": 2.5, "swapped": 2 if ok else 0, "reverted": 1, "vtable": 0,
                        "error": None if ok else "dlopen: symbol not found: _x"}}

    def close(self):
        pass


class FakeWatcher:
    mode = "inotify"

    def __init__(self):
        self.pending: list[str] = []

    def fileno(self):
        return None

    def read(self, now=None):
        out, self.pending = self.pending, []
        return out

    def close(self):
        pass


class SessionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        os.environ["IOSSTUDIO_RUNTIME_DIR"] = str(self.tmp / "rt")
        self.root = self.tmp / "App"
        (self.root / "Sources/App").mkdir(parents=True)
        self.src = self.root / "Sources/App/Card.swift"
        self.src.write_text("v0\n")
        self.events: list[dict] = []
        self.dev = FakeDevice()
        self.patches: list[tuple[int, list[str]]] = []
        self.patch_error: Exception | None = None
        self.gate: threading.Event | None = None
        self.clock = Clock()
        self.watcher = FakeWatcher()
        self.s = session.Session(self.root, udid="U1", device=self.dev, patcher=self.patcher, write=self.events.append,
                                 check_parent=False, clock=self.clock, watcher_factory=lambda _r: (self.watcher, None))
        self.s.snap = {"event": "hotreload", "project": "App", "path": str(self.root), "lastReload": None,
                       "lastError": None, "notice": None}
        self.s.baseline = object()
        self.s.bundle_id = "XTL-T.dev.app"
        self.s.on_started()
        self.addCleanup(self.s.shutdown)

    def patcher(self, baseline, files, seq, *, cancel=None, on_step=None):
        if self.gate:
            self.gate.wait(5)
        on_step("compile")
        if self.patch_error:
            raise self.patch_error
        changed = ["Sources/App/Card.swift"]
        self.patches.append((seq, changed))
        out = self.tmp / f"patch-{seq}.dylib"
        out.write_bytes(b"x")
        return FakeResult(seq, str(out), changed)

    def save(self, *rels: str) -> None:
        for rel in rels:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"edit {time.monotonic()}\n")
        self.watcher.pending += list(rels)

    def run_until(self, pred, timeout: float = 5.0, advance: float = 0.2) -> None:
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > deadline:
                self.fail(f"timed out; last state {self.s.snap.get('state')}: {self.s.snap.get('message')}")
            self.clock.t += advance
            self.s.step(0.02)

    def reload(self, *rels: str) -> dict:
        seq = self.s.patch_seq
        self.save(*(rels or ("Sources/App/Card.swift",)))
        self.run_until(lambda: self.s.worker is None and self.s.history and self.s.history[-1]["seq"] > seq)
        return self.s.snap


class SessionReloadTests(SessionBase):
    def test_the_parent_closing_stops_the_session(self):
        # `hotreload run` watches the process that started it: a script that exits ends the session (by design)
        self.s.check_parent, self.s.ppid = True, -1
        self.s.step(0)
        self.assertEqual(self.s.stop_reason, "stopped")
        self.assertEqual(self.s.stop_message, "Stopped: the app or tool that started hot reload has closed")

    def test_success_fills_last_reload(self):
        snap = self.reload()
        self.assertEqual(snap["state"], "reloaded")
        r = snap["lastReload"]
        self.assertEqual((r["seq"], r["ok"], r["files"], r["swapped"], r["reverted"]),
                         (1, True, ["Sources/App/Card.swift"], 2, 1))
        self.assertEqual(r["steps"], {"check": 1, "compile": 2, "link": 3, "sign": 4, "push": 54, "apply": 2.5})
        self.assertIsNone(snap["lastError"])
        push = next(a for c, a in self.dev.calls if c == "hotreload.push")
        self.assertEqual((push["bundleId"], push["seq"], push["udid"]), ("XTL-T.dev.app", 1, "U1"))
        self.assertIn({"step": "compile", "detail": "Compiling"}, [e.get("progress") for e in self.events])
        self.clock.t += session.RELOADED_SHOWN + 1
        self.s.step(0)
        self.assertEqual(self.s.snap["state"], "watching")

    def test_unsupported_change_keeps_watching(self):
        blocker = {"kind": "stored-property", "type": "Card", "name": "count", "file": "Sources/App/Card.swift",
                   "line": 4, "message": "Adds the stored property count to Card."}
        self.patch_error = PatchError("unsupported-change", "Adds the stored property count to Card. This change "
                                      "needs a full build.", session.RUN_FIX, file=blocker["file"], line=4,
                                      blockers=[blocker])
        snap = self.reload()
        self.assertEqual(snap["state"], "failed")
        e = snap["lastError"]
        self.assertEqual((e["code"], e["seq"], e["line"], e["blockers"][0]["name"], e["fix"]["action"]),
                         ("unsupported-change", 1, 4, "count", "hot-reload-restart"))
        self.assertFalse(self.s.stop_reason)
        self.patch_error = None
        snap = self.reload()
        self.assertEqual((snap["state"], snap["lastError"], snap["lastReload"]["seq"]), ("reloaded", None, 2))

    def test_no_ack_is_app_not_running(self):
        self.dev.push = "noack"
        e = self.reload()["lastError"]
        self.assertEqual(e["code"], "app-not-running")
        self.assertIn("isn't running", e["message"])

    def test_ack_not_ok_is_load_failed_with_the_dlerror(self):
        self.dev.push = "bad"
        e = self.reload()["lastError"]
        self.assertEqual(e["code"], "load-failed")
        self.assertIn("symbol not found: _x", e["message"])

    def test_app_gone_ends_the_session(self):
        self.dev.push = HelperError("not-installed", "The app isn't installed on your iPhone.")
        self.save("Sources/App/Card.swift")
        self.run_until(lambda: self.s.stop_reason)
        self.assertEqual(self.s.stop_reason, "app-exited")
        self.assertEqual(self.s.snap["lastError"]["code"], "not-installed")

    def test_back_to_the_build(self):
        self.reload()
        self.patch_error = HelperError("no-changes", "Nothing changed since the app was built.")
        snap = self.reload()
        self.assertEqual((snap["state"], snap["message"]), ("watching", "Back to the built version"))
        self.assertEqual(snap["notice"]["id"], "back-to-build")
        self.assertEqual(self.s.history[-1]["unchanged"], True)
        self.assertEqual(len([c for c, _ in self.dev.calls if c == "hotreload.push"]), 1)

    def test_back_to_the_build_without_reloads_has_no_notice(self):
        self.patch_error = HelperError("no-changes", "Nothing changed since the app was built.")
        snap = self.reload()
        self.assertEqual(snap["message"], "Back to the built version")
        self.assertIsNone(snap["notice"])

    def test_save_during_reload_makes_one_more(self):
        self.gate = threading.Event()
        self.save("Sources/App/Card.swift")
        self.run_until(lambda: self.s.worker is not None)
        self.save("Sources/App/Card.swift")
        self.save("Sources/App/Other.swift")
        self.s.step(0.01)
        self.assertEqual(self.s.snap["pendingCount"], 2)
        self.assertIn("reloads after this one", self.s.snap["message"])
        self.gate.set()
        self.run_until(lambda: len(self.s.history) == 2 and self.s.worker is None)
        self.assertEqual([seq for seq, _ in self.patches], [1, 2])
        for _ in range(10):
            self.clock.t += 1
            self.s.step(0.01)
        self.assertEqual(len(self.patches), 2)

    def test_pause_and_resume_through_the_control_file(self):
        state.write_control(self.s.key, str(self.root), True)
        self.s.control = True
        self.s.step(0)
        self.assertEqual((self.s.snap["state"], self.s.snap["paused"]), ("paused", True))
        self.save("Sources/App/Card.swift")
        self.save("Sources/App/Other.swift")
        for _ in range(5):
            self.clock.t += 1
            self.s.step(0.01)
        self.assertEqual(self.patches, [])
        self.assertEqual((self.s.snap["pending"], self.s.snap["pendingCount"]),
                         (["Sources/App/Card.swift", "Sources/App/Other.swift"], 2))
        state.write_control(self.s.key, str(self.root), False)
        self.s.control = True
        self.run_until(lambda: len(self.s.history) == 1 and self.s.worker is None, advance=0)
        self.assertEqual(len(self.patches), 1)
        self.assertFalse(self.s.snap["paused"])

    def test_non_code_changes_get_a_notice(self):
        self.save("Package.swift")
        self.s.step(0)
        self.assertEqual(self.s.snap["notice"]["id"], "needs-run")
        self.assertEqual(self.s.sched.files, [])

    def app_module(self) -> None:
        from types import SimpleNamespace
        self.s.baseline = SimpleNamespace(project=str(self.root), frontend={"Sources/App/Card.swift": []})
        self.s.app_roots = self.s.module_folders()
        self.assertEqual(self.s.app_roots, ["Sources/App/"])

    def test_saves_in_another_target_get_a_notice_and_no_reload(self):
        self.app_module()
        self.save("Sources/Kit/Model.swift")
        for _ in range(5):
            self.clock.t += 1
            self.s.step(0.01)
        n = self.s.snap["notice"]
        self.assertEqual((n["id"], n["fix"]["action"]), ("other-target", "hot-reload-restart"))
        self.assertIn("Changes in Kit need a full Run", n["message"])
        self.assertEqual((self.patches, self.s.history, self.s.sched.files), ([], [], []))
        self.assertNotEqual(self.s.snap["message"], "Back to the built version")
        snap = self.reload("Sources/App/Card.swift", "Sources/Kit/Model.swift")   # the app file still reloads
        self.assertEqual(snap["state"], "reloaded")
        self.assertTrue(self.s.in_app("Sources"))                  # a folder above the module counts

    def test_a_save_during_the_build_reloads_right_after_the_start(self):
        from iosstudio.features.watch.fswatch import file_digest
        self.s.built_digests = {"Sources/App/Card.swift": file_digest(self.src)}
        self.src.write_text("v1 saved while building\n")
        self.s.filter.prime()
        self.s.catch_up()
        self.assertEqual(self.s.snap["notice"]["id"], "changed-while-building")
        self.run_until(lambda: self.s.history)
        self.assertEqual(self.patches[0][0], 1)
        self.assertEqual(self.s.snap["state"], "reloaded")

    def test_nothing_changed_during_the_build(self):
        from iosstudio.features.watch.fswatch import file_digest
        self.s.built_digests = {"Sources/App/Card.swift": file_digest(self.src)}
        self.s.catch_up()
        self.assertIsNone(self.s.snap["notice"])
        self.assertEqual(self.s.sched.files, [])

    def test_late_files_ask_for_a_run(self):
        self.s.late = ["Sources/App/Card.swift"]
        self.s.built_digests = {"Sources/App/Card.swift": "what the snapshot read"}
        self.s.catch_up()
        self.assertEqual(self.s.sched.files, [])                 # its copy is the live file: nothing to patch
        n = self.s.snap["notice"]
        self.assertEqual((n["id"], n["message"], n["fix"]["action"]),
                         ("changed-during-build", "Card.swift changed while building. Press Run to include it.",
                          "hot-reload-restart"))

    def test_shutdown_stays_within_its_budget(self):
        stuck = threading.Event()
        self.s.worker = threading.Thread(target=stuck.wait, daemon=True)
        self.s.worker.start()
        self.s.warm_thread = threading.Thread(target=stuck.wait, daemon=True)
        self.s.warm_thread.start()
        self.addCleanup(stuck.set)
        saved = session.JOIN_WAIT, session.SHUTDOWN_BUDGET
        session.JOIN_WAIT, session.SHUTDOWN_BUDGET = 0.3, 0.6
        try:
            t0 = time.monotonic()
            self.s.shutdown()
            self.assertLess(time.monotonic() - t0, 1.2)
        finally:
            session.JOIN_WAIT, session.SHUTDOWN_BUDGET = saved
        self.assertEqual(self.s.snap["state"], "stopped")

    def test_stop_waits_longer_than_a_shutdown_and_answers_in_time(self):
        self.assertGreater(state.STOP_WAIT, session.SHUTDOWN_BUDGET)
        self.assertLessEqual(session.JOIN_WAIT + session.WARM_WAIT + session.CLOSE_WAIT, session.SHUTDOWN_BUDGET)
        self.assertLess(state.STOP_WAIT + 2 + 1 + 1, 15)   # + the SIGKILL wait, a pid wait, startup < MCP's 15 s


class StartTests(unittest.TestCase):
    """Session.start with the build faked: the snapshot is taken before the build and handed to the baseline."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        os.environ["IOSSTUDIO_RUNTIME_DIR"] = str(self.tmp / "rt")
        os.environ["XDG_CACHE_HOME"] = str(self.tmp / "cache")
        self.root = self.tmp / "App"
        (self.root / "Sources/App").mkdir(parents=True)
        (self.root / "Sources/App/Card.swift").write_text("v0\n")
        self.order: list[str] = []

    def session(self, baseliner) -> session.Session:
        s = session.Session(self.root, udid="U1", device=FakeDevice(), write=lambda _e: None, check_parent=False,
                            checks=lambda _r, u: (u, []), stop_watch=lambda _r: None, baseliner=baseliner,
                            warmer=lambda *_a, **_k: None, build_factory=None)
        s.run_build = lambda: (self.order.append("build"),
                               (self.root / "Sources/App/Card.swift").write_text("v1\n"))[1] and {}
        s.installed_id = lambda _d: "XTL-T.dev.app"
        s.launch = lambda _d: None
        s.confirm_runtime = lambda _pid: None
        s.capture = lambda: (self.order.append("capture"), baseliner(s.root, "App", snapshot=s.snapshot,
                                                                      cancel=s.cancelled))[1]
        self.addCleanup(s.shutdown)
        return s

    def test_snapshot_before_the_build(self):
        seen = {}

        class Base:
            interposable = 1
            late: list = []

        def baseliner(root, module, *, snapshot, cancel):
            seen["copy"] = (Path(snapshot.dir) / "sources/Sources/App/Card.swift").read_text()
            seen["digest"] = snapshot.digests["Sources/App/Card.swift"]
            return Base()
        s = self.session(baseliner)
        s.start()
        self.assertEqual(self.order, ["build", "capture"])
        self.assertEqual(seen["copy"], "v0\n")
        self.assertEqual(s.built_digests["Sources/App/Card.swift"], seen["digest"])
        self.assertIsNone(s.snapshot)
        cache = self.tmp / "cache/ios-studio/hot-reload"
        self.assertEqual([p.name for p in cache.iterdir() if p.name.startswith(".")], [])   # discarded

    def test_a_failed_start_discards_the_snapshot(self):
        def baseliner(root, module, *, snapshot, cancel):
            raise HelperError("no-baseline", "The last build's records are missing.")
        s = self.session(baseliner)
        with self.assertRaises(HelperError):
            s.start()
        cache = self.tmp / "cache/ios-studio/hot-reload"
        self.assertEqual([p.name for p in cache.iterdir() if p.name.startswith(".")], [])


class StateFileTests(SessionBase):
    def test_state_file_keeps_twenty_reloads(self):
        for _ in range(state.HISTORY_MAX + 3):
            self.reload()
        st = state.read_state(self.s.key)
        self.assertEqual(len(st["history"]), state.HISTORY_MAX)
        self.assertEqual(st["history"][-1]["seq"], state.HISTORY_MAX + 3)
        self.assertNotIn("history", self.events[-1])

    def test_stopped_snapshot_and_dead_session(self):
        self.s.shutdown()
        st = state.current(self.s.key)
        self.assertEqual((st["state"], st["stopReason"], st["running"]), ("stopped", "stopped", False))
        # a session that died without saying so: the lock is free but the file says watching
        state.write_state(self.s.key, {**st, "state": "watching"})
        st = state.current(self.s.key)
        self.assertEqual((st["state"], st["stopReason"]), ("stopped", "session-died"))
        self.assertEqual(state.read_state(self.s.key)["stopReason"], "session-died")


class RuntimeFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        os.environ["IOSSTUDIO_RUNTIME_DIR"] = str(self.tmp / "rt")
        self.k = state.key("/x/App")

    def test_busy(self):
        fh = state.acquire(self.k, "/x/App")
        self.addCleanup(state.release, fh)
        self.assertEqual(state.session_pid(self.k), os.getpid())
        with self.assertRaises(HelperError) as cm:
            state.acquire(self.k, "/x/App")
        self.assertEqual(cm.exception.code, "busy")
        self.assertIn(str(os.getpid()), cm.exception.message)

    def test_wait_returns_when_a_newer_reload_finished(self):
        fh = state.acquire(self.k, "/x/App")
        self.addCleanup(state.release, fh)
        state.write_state(self.k, {"state": "watching", "pid": os.getpid(), "history": [{"seq": 3, "ok": True}]})
        st = state.wait(self.k, 0.3)
        self.assertTrue(st["timedOut"])

        def later():
            time.sleep(0.3)
            state.write_state(self.k, {"state": "reloaded", "pid": os.getpid(),
                                       "history": [{"seq": 3, "ok": True}, {"seq": 4, "ok": False}]})
        threading.Thread(target=later).start()
        st = state.wait(self.k, 5)
        self.assertEqual((st["timedOut"], st["state"]), (False, "reloaded"))
        self.assertGreater(st["waited"], 0.1)
        st = state.wait(self.k, 5, after_seq=2)          # already newer than 2: at once
        self.assertEqual((st["timedOut"], st["waited"]), (False, 0.0))

    def test_wait_returns_when_the_session_is_gone(self):
        state.write_state(self.k, {"state": "watching", "pid": 999999, "history": []})
        st = state.wait(self.k, 5)
        self.assertEqual((st["state"], st["stopReason"], st["timedOut"]), ("stopped", "session-died", False))
        self.assertIsNone(state.wait(state.key("/never"), 1))

    def test_pause_without_a_session(self):
        with self.assertRaises(HelperError) as cm:
            state.set_paused(self.k, "/x/App", True)
        self.assertEqual((cm.exception.code, cm.exception.fix["action"]), ("not-running", "hot-reload-start"))
        self.assertEqual(state.stop(self.k), {"stopped": False, "session": None})

    def test_reading_never_creates_a_lock_file(self):
        self.assertIsNone(state.session_pid(self.k))
        self.assertIsNone(state.current(self.k))
        self.assertEqual(state.stop(self.k)["stopped"], False)
        with self.assertRaises(HelperError):
            state.set_paused(self.k, "/x/App", True)
        self.assertFalse(state.lock_path(self.k).exists())

    def test_prune_removes_orphan_locks_only(self):
        rt = state.runtime_dir()
        rt.mkdir(parents=True, exist_ok=True)
        old = time.time() - state.LOCK_ORPHAN_AGE - 5
        orphan, young, with_state = state.key("/x/Orphan"), state.key("/x/Young"), state.key("/x/Kept")
        for k in (orphan, young, with_state):
            state.lock_path(k).write_text("")
        for k in (orphan, with_state):
            os.utime(state.lock_path(k), (old, old))
        state.write_state(with_state, {"state": "stopped", "stoppedAt": time.time()})
        held = state.acquire(self.k, "/x/App")
        self.addCleanup(state.release, held)
        os.utime(state.lock_path(self.k), (old, old))
        state.prune()
        self.assertEqual([state.lock_path(k).exists() for k in (orphan, young, with_state, self.k)],
                         [False, True, True, True])
        self.assertEqual(state.live_keys(), [self.k])

    def test_acquire_after_its_lock_file_was_pruned(self):
        fh = state.acquire(self.k, "/x/App")
        state.release(fh)
        state.lock_path(self.k).unlink()
        fh = state.acquire(self.k, "/x/App")
        self.addCleanup(state.release, fh)
        self.assertEqual(state.session_pid(self.k), os.getpid())

    def test_prune_and_list(self):
        now = time.time()
        old, new = state.key("/x/Old"), state.key("/x/New")
        state.write_state(old, {"state": "stopped", "stoppedAt": now - state.PRUNE_AFTER - 5, "updatedAt": 1})
        state.write_state(new, {"state": "stopped", "stoppedAt": now - 5, "updatedAt": now, "history": [{"seq": 1}]})
        state.write_control(old, "/x/Old", True)
        self.assertEqual(state.prune(), 1)
        self.assertFalse(state.state_path(old).exists())
        self.assertFalse(state.control_path(old).exists())
        listed = state.sessions()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("history", listed[0])


class PrecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        self.root = self.tmp / "App"
        self.root.mkdir()
        self.manifest = '// swift-tools-version: 6.0\nimport PackageDescription\nlet package = Package(name: "App")\n'
        (self.root / "Package.swift").write_text(self.manifest)
        os.environ["IOS_STUDIO_XTOOL"] = str(write_exe(self.tmp / "xtool", "#!/bin/sh\necho 'Logged in.'\n"))
        os.environ["IOSSTUDIO_RCODESIGN"] = str(write_exe(self.tmp / "rcodesign", "#!/bin/sh\n"))

    def code(self, udid=None) -> str:
        with self.assertRaises(HelperError) as cm:
            session.prechecks(self.root, udid)
        return cm.exception.code

    def test_not_enabled(self):
        self.assertEqual(self.code(), "not-enabled")

    def test_enabled_refreshes_the_runtime_and_needs_a_device(self):
        (self.root / "Package.swift").write_text(project_setup.append_block(self.manifest, "App"))
        udid, notices = session.prechecks(self.root, "U1")
        self.assertEqual(udid, "U1")
        self.assertEqual([n["id"] for n in notices], ["runtime-updated"])
        self.assertTrue(project_setup.status(self.root)["current"])
        self.assertEqual(session.prechecks(self.root, "U1")[1], [])
        self.assertEqual(self.code(), "no-device")
        os.environ["IOSSTUDIO_RCODESIGN"] = ""
        os.environ["PATH"] = str(self.tmp / "nothing")
        from iosstudio.features.hotreload import signing
        if signing.rcodesign_path() is None:          # ~/.local/bin may have one
            self.assertEqual(self.code("U1"), "missing-tool")


if __name__ == "__main__":
    unittest.main()
