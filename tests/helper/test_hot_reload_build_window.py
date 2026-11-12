"""Hot reload and what happens while the first build runs (second review round): a Swift file created during the
build is new (the pre-build snapshot says what existed, not the executable's mtime), a prebuild's output is taken
as built (the snapshot is taken again when the prebuild step is done), run_tool's temp files are capped, and
pruning a lock file never takes it from a session that is starting."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from iosstudio.features.hotreload import buildinfo as B
from iosstudio.features.hotreload import patch, session, state
from support.hot_reload import fake_build, fake_toolchain


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        os.environ["IOSSTUDIO_RUNTIME_DIR"] = str(self.tmp / "rt")
        os.environ["XDG_CACHE_HOME"] = str(self.tmp / "xdg-cache")
        self.tools = fake_toolchain(self.tmp / "tools")
        self.cache = self.tmp / "cache"


class NewFilesDuringTheBuild(Base):
    def build(self) -> tuple[Path, B.SourceSnapshot]:
        proj = fake_build(self.tmp / "App", self.tools)
        excluded = proj / "Sources/App/Resources/Template.swift"     # there before the build, not compiled
        excluded.parent.mkdir(parents=True)
        excluded.write_text("// a resource\n")
        snap = B.snapshot_sources(proj, self.cache)                   # the build starts
        return proj, snap

    def test_a_file_created_before_the_link_is_new(self):
        proj, snap = self.build()
        time.sleep(0.02)
        (proj / "Sources/App/Late.swift").write_text("struct Late {}\n")   # after SwiftPM planned, before the link
        time.sleep(0.02)
        exe = proj / "xtool/App.app/App"
        now = time.time()
        os.utime(exe, (now, now))                                      # the link ends after it
        base = B.capture_baseline(proj, "App", self.cache, snapshot=snap)
        self.assertEqual(base.excluded, ["Sources/App/Resources/Template.swift"])
        self.assertAlmostEqual(base.started_at, snap.started)
        self.assertGreater(base.built_at, base.started_at)
        self.assertEqual(patch.source_changes(base).added, ["Sources/App/Late.swift"])
        loaded = B.load_baseline(proj, self.cache)                     # kept in baseline.json
        self.assertEqual((loaded.excluded, loaded.started_at), (base.excluded, base.started_at))
        with self.assertRaises(patch.PatchError) as cm:
            patch.make_patch(base, None, 1, sign=False)
        self.assertEqual((cm.exception.code, cm.exception.file), ("new-file", "Sources/App/Late.swift"))

    def test_an_excluded_file_touched_later_stays_excluded(self):
        proj, snap = self.build()
        base = B.capture_baseline(proj, "App", self.cache, snapshot=snap)
        later = time.time() + 60
        os.utime(proj / "Sources/App/Resources/Template.swift", (later, later))    # edited during the session
        self.assertTrue(patch.source_changes(base).empty)

    def test_without_a_complete_snapshot_file_times_decide_from_the_snapshot_start(self):
        proj, snap = self.build()
        snap.complete = False                                          # over scan_files' cap
        time.sleep(0.02)
        (proj / "Sources/App/Late.swift").write_text("struct Late {}\n")
        base = B.capture_baseline(proj, "App", self.cache, snapshot=snap)
        self.assertIsNone(base.excluded)
        self.assertEqual(patch.source_changes(base).added, ["Sources/App/Late.swift"])

    def test_an_old_baseline_goes_by_the_build_time(self):
        proj, _snap = self.build()
        base = B.capture_baseline(proj, "App", self.cache)            # no snapshot: started_at 0, excluded None
        self.assertEqual((base.started_at, base.excluded), (0.0, None))
        time.sleep(0.02)
        base.built_at = time.time()                                    # the link ends after the resource
        self.assertTrue(patch.source_changes(base).empty)
        time.sleep(0.02)
        (proj / "Sources/App/Added.swift").write_text("struct Added {}\n")
        self.assertEqual(patch.source_changes(base).added, ["Sources/App/Added.swift"])


class FakeBuild:
    """A build child that emits the prebuild's step events; `on_prebuild` runs as the prebuild command."""

    def __init__(self, events: list[dict], between):
        self.events, self.between = list(events), between

    def finished(self):
        return not self.events

    def fileno(self):
        return None

    def read_events(self):
        if not self.events:
            return []
        ev = self.events.pop(0)
        if ev.get("id") == "prebuild" and ev.get("state") == "running":
            self.between()
        return [ev]

    def enforce(self):
        pass

    def cancel(self):
        pass

    def result(self):
        return {"ok": True, "data": {}}


class PrebuildSnapshot(Base):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "App"
        (self.root / "Sources/App").mkdir(parents=True)
        (self.root / "Sources/App/Card.swift").write_text("v0\n")
        (self.root / "xtool.yml").write_text("version: 1\n")

    def session(self, events) -> session.Session:
        def prebuild():
            (self.root / "Sources/App/Generated.swift").write_text("// generated\n")
            (self.root / "xtool.yml").write_text("version: 1\nbundleID: dev.example\n")
        s = session.Session(self.root, udid="U1", write=lambda _e: None, check_parent=False,
                            build_factory=lambda _x, _b: FakeBuild(events, prebuild))
        self.addCleanup(s.shutdown)
        return s

    def test_the_snapshot_is_taken_again_after_the_prebuild(self):
        s = self.session([{"event": "step", "id": "prebuild", "state": "running"},
                          {"event": "step", "id": "prebuild", "state": "done"},
                          {"event": "step", "id": "resolve", "state": "running"}])
        s.snapshot = first = s.take_snapshot()
        self.assertNotIn("Sources/App/Generated.swift", first.digests)
        s.run_build()
        self.assertIsNot(s.snapshot, first)
        self.assertFalse(Path(first.dir).exists())                    # the first one is discarded
        self.assertIn("Sources/App/Generated.swift", s.snapshot.copied)
        self.assertEqual((Path(s.snapshot.dir) / "sources/Sources/App/Generated.swift").read_text(), "// generated\n")
        from iosstudio.features.watch.fswatch import file_digest
        self.assertEqual(s.snapshot.digests["xtool.yml"], file_digest(self.root / "xtool.yml"))   # no needs-run
        self.assertGreaterEqual(s.snapshot.started, first.started)
        B.discard_snapshot(s.snapshot)

    def test_without_a_prebuild_the_first_snapshot_stays(self):
        s = self.session([{"event": "step", "id": "resolve", "state": "running"},
                          {"event": "step", "id": "compile", "state": "running"}])
        s.snapshot = first = s.take_snapshot()
        s.run_build()
        self.assertIs(s.snapshot, first)
        B.discard_snapshot(first)

    def test_a_failed_second_snapshot_keeps_the_first(self):
        s = self.session([{"event": "step", "id": "prebuild", "state": "done"}])
        s.snapshot = first = s.take_snapshot()
        s.snapshotter = lambda _r: (_ for _ in ()).throw(OSError("disk full"))
        s.run_build()
        self.assertIs(s.snapshot, first)
        self.assertTrue(Path(first.dir).is_dir())
        B.discard_snapshot(first)


class RunToolCap(unittest.TestCase):
    def test_output_past_the_cap_kills_the_tool(self):
        script = "import sys, time\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()\n"
        saved = B.MAX_TOOL_OUTPUT
        B.MAX_TOOL_OUTPUT = 1 << 20
        try:
            t0 = time.monotonic()
            r = B.run_tool([sys.executable, "-c", script], timeout=20)
            self.assertLess(time.monotonic() - t0, 5)                 # long before the timeout
            self.assertEqual((r.returncode, r.stdout, r.stderr), (125, "", "output too large"))
            quick = B.run_tool([sys.executable, "-c", f"print('y' * {(1 << 20) + 10})"])   # all within one tick
            self.assertEqual(quick.returncode, 125)
        finally:
            B.MAX_TOOL_OUTPUT = saved
        ok = B.run_tool([sys.executable, "-c", "print('z' * 1000)"])
        self.assertEqual((ok.returncode, len(ok.stdout)), (0, 1001))


class PruneRace(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        os.environ["IOSSTUDIO_RUNTIME_DIR"] = str(self.tmp / "rt")
        state.runtime_dir().mkdir(parents=True, exist_ok=True)
        self.k = state.key("/x/App")

    def stale_lock(self) -> None:
        lock = state.lock_path(self.k)
        lock.write_text("")
        old = time.time() - state.LOCK_ORPHAN_AGE - 5
        os.utime(lock, (old, old))

    def test_a_starter_between_the_check_and_the_unlink_keeps_its_lock(self):
        """The old prune checked session_pid() and then unlinked: a session that took the lock in between lost
        its file (a second session could then start). Now prune unlinks only while holding the lock itself."""
        self.stale_lock()
        held: list = []
        starters: list[threading.Thread] = []
        real_unlink = Path.unlink

        def unlink(p, missing_ok=False):
            if p == state.lock_path(self.k) and not starters:     # a session starts right now
                t = threading.Thread(target=lambda: held.append(state.acquire(self.k, "/x/App")))
                starters.append(t)
                t.start()
                time.sleep(0.1)
            return real_unlink(p, missing_ok=missing_ok)
        from unittest import mock
        with mock.patch.object(Path, "unlink", unlink):
            state.prune()
        starters[0].join(3)
        self.addCleanup(lambda: [state.release(h) for h in held])
        self.assertEqual(len(held), 1)
        self.assertTrue(state.lock_path(self.k).exists())              # its lock file is the one on disk
        self.assertEqual(state.session_pid(self.k), os.getpid())

    def test_a_held_lock_is_never_removed(self):
        self.stale_lock()
        fh = state.acquire(self.k, "/x/App")
        self.addCleanup(state.release, fh)
        os.utime(state.lock_path(self.k), (1, 1))
        self.assertEqual(state.prune(), 0)
        self.assertEqual(state.session_pid(self.k), os.getpid())

    def test_a_starter_waits_for_a_prune_that_holds_the_lock(self):
        self.stale_lock()
        import fcntl
        pruner = open(state.lock_path(self.k))
        fcntl.flock(pruner, fcntl.LOCK_EX | fcntl.LOCK_NB)          # prune() got it: nobody holds it

        def finish():
            time.sleep(0.1)
            state.lock_path(self.k).unlink()
            pruner.close()
        t = threading.Thread(target=finish)
        t.start()
        fh = state.acquire(self.k, "/x/App")                          # not "busy": it takes the new file
        t.join()
        self.addCleanup(state.release, fh)
        self.assertTrue(state.lock_path(self.k).exists())
        self.assertEqual(state.session_pid(self.k), os.getpid())

    def test_stale_state_files_go_only_with_a_free_lock(self):
        state.write_state(self.k, {"state": "stopped", "stoppedAt": time.time() - state.PRUNE_AFTER - 5})
        fh = state.acquire(self.k, "/x/App")
        self.assertEqual(state.prune(), 0)
        self.assertTrue(state.state_path(self.k).exists())
        state.release(fh)
        self.assertEqual(state.prune(), 1)
        self.assertFalse(state.state_path(self.k).exists())
        self.assertFalse(state.lock_path(self.k).exists())


if __name__ == "__main__":
    unittest.main()
