"""helper/iosstudio/features/hotreload/buildinfo.py: the msgpack decoder, reading an xtool build (a fake build tree
and toolchain from tests/support/hot_reload.py), the baseline cache and its pruning."""
from __future__ import annotations

import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path

from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import buildinfo as B
from support.hot_reload import app_and_patch, fake_build, fake_toolchain, msgpack_str, task_store


class Msgpack(unittest.TestCase):
    def test_values(self):
        data = (b"\x93\x01\xa3abc\x82\xa1k\xc3\xa1n\xc0" + b"\xcd\x01\x00" + b"\xd0\xff" + b"\xcb" + b"\0" * 8 +
                b"\xc4\x02hi" + b"\xd4\x01\x02")
        v, i = B.mp_decode(data, 0)
        self.assertEqual(v, [1, "abc", {"k": True, "n": None}])
        v, i = B.mp_decode(data, i)
        self.assertEqual(v, 256)
        v, i = B.mp_decode(data, i)
        self.assertEqual(v, -1)
        v, i = B.mp_decode(data, i)
        self.assertEqual(v, 0.0)
        v, i = B.mp_decode(data, i)
        self.assertEqual(v, b"hi")
        v, i = B.mp_decode(data, i)
        self.assertEqual(v[0], "ext")
        self.assertEqual(i, len(data))

    def test_long_strings(self):
        s = "x" * 300
        v, _ = B.mp_decode(msgpack_str(s), 0)
        self.assertEqual(v, s)

    def test_truncated_and_bad_bytes(self):
        for data in (b"", b"\xa5ab", b"\xdc\xff\xff", b"\x9f", b"\xc1", b"\xdb\xff\xff\xff\xff", b"\x91" * 100):
            with self.subTest(data=data[:6]):
                with self.assertRaises(B.MsgpackError):
                    B.mp_decode(data, 0)

    def test_garbage_is_bounded(self):
        rnd = random.Random(7)
        start = time.monotonic()
        for _ in range(300):
            data = bytes(rnd.randrange(256) for _ in range(rnd.randint(1, 200)))
            try:
                B.mp_decode(data, 0)
            except B.MsgpackError:
                pass
            B.swiftc_runs(b"\xa2--" + data)
        self.assertLess(time.monotonic() - start, 5)

    def test_swiftc_runs_and_pick(self):
        a = ["/usr/bin/swiftc", "-module-name", "Lib", "-c"]
        b = ["/usr/bin/swiftc", "-module-name", "App", "-emit-module"]
        c = ["/usr/bin/swiftc", "-module-name", "App", "-c", "-Onone"]
        data = task_store(a, ["/usr/bin/clang", "-c"], b, c)
        runs = B.swiftc_runs(data)
        self.assertEqual(runs, [a, b, c])
        self.assertEqual(B.pick_compile_run(runs, "App"), c)
        self.assertIsNone(B.pick_compile_run(runs, "Missing"))


DRIVER = ["/sw/swiftc", "-parse-as-library", "-module-name", "App", "@/x/App.SwiftFileList", "-c", "-j8",
          "-enable-batch-mode", "-incremental", "-output-file-map", "/x/ofm.json", "-module-cache-path", "/mc",
          "-explicit-module-build", "-module-cache-path", "/explicit", "-emit-module", "-emit-module-path", "/x/m",
          "-index-store-path", "/idx", "-save-temps", "-color-diagnostics", "-validate-clang-modules-once",
          "-clang-build-session-file", "/s", "-emit-objc-header", "-emit-objc-header-path", "/h",
          "-experimental-emit-module-separately", "-emit-const-values", "-const-gather-protocols-list", "/c",
          "-emit-dependencies", "-serialize-diagnostics", "-dependency-scan-serialize-diagnostics-path", "/d",
          "-clang-scanner-module-cache-path", "/csm", "-sdk-module-cache-path", "/smc", "-swift-version", "6"]


class Commands(unittest.TestCase):
    def test_frontend_request_drops_build_wide_flags(self):
        self.assertEqual(B.frontend_request(DRIVER),
                         ["/sw/swiftc", "-parse-as-library", "-module-name", "App", "@/x/App.SwiftFileList",
                          "-module-cache-path", "/mc", "-swift-version", "6", "-c", "-disable-batch-mode", "-###"])

    def test_parse_frontends(self):
        out = ("/sw/swift-frontend -frontend -c -primary-file /p/A.swift /p/B.swift -module-name App -o /o/A.o\n"
               "'/sw/swift-frontend' -frontend -c /p/A.swift -primary-file '/p/B c.swift' -o /o/B.o\n"
               "/sw/swift-frontend -frontend -c -primary-file /p/C.swift\n"
               "/sw/swift-frontend -frontend -c -primary-file /p/D.swift -emit-dependencies-path /b/D.d "
               "-serialize-diagnostics-path /b/D.dia -index-store-path /idx -o /o/D.o\n"
               "unrelated line\n")
        fe = B.parse_frontends(out)
        self.assertEqual(sorted(fe), ["/p/A.swift", "/p/B c.swift", "/p/D.swift"])
        self.assertEqual(fe["/p/D.swift"], ["/sw/swift-frontend", "-frontend", "-c", "-primary-file", "/p/D.swift",
                                            "-o", "/o/D.o"])
        self.assertEqual(fe["/p/B c.swift"][0], "/sw/swift-frontend")

    def test_patch_link_args(self):
        app = ["/sw/swiftc", "-target", "t", "-emit-executable", "@/x/App-App.LinkFileList", "-Xlinker", "-all_load",
               "-Xlinker", "-dead_strip", "-Xlinker", "-no_deduplicate", "-Xlinker", "-object_path_lto", "-Xlinker",
               "/lto.o", "-Xlinker", "-dependency_info", "-Xlinker", "/dep.dat", "-Xlinker", "-rpath", "-Xlinker",
               "@loader_path", "-o", "/out/App-App"]
        link = B.patch_link_args(app)
        self.assertEqual(link, ["/sw/swiftc", "-emit-library", "-target", "t", "-Xlinker", "-rpath", "-Xlinker",
                                "@loader_path", "-Xlinker", "-undefined", "-Xlinker", "dynamic_lookup", "-Xlinker",
                                "-interposable"])
        self.assertEqual(B.patch_link_args(app + ["-Xlinker", "-interposable"]).count("-interposable"), 1)
        self.assertEqual(B.link_command(link, ["/a.o"], "/p.dylib", "@rpath/p.dylib")[-5:],
                         ["-Xlinker", "@rpath/p.dylib", "/a.o", "-o", "/p.dylib"])

    def test_ld_command(self):
        tpl = ["/ld", "-dylib", B.OBJECTS, "-o", B.OUTPUT, "-install_name", B.INSTALL_NAME]
        self.assertEqual(B.ld_command(tpl, ["/a.o", "/b.o"], "/p.dylib", "@rpath/p-2.dylib"),
                         ["/ld", "-dylib", "/a.o", "/b.o", "-o", "/p.dylib", "-install_name", "@rpath/p-2.dylib"])

    def test_project_key(self):
        self.assertEqual(B.project_key("/home/me/App"), __import__("hashlib").sha1(b"/home/me/App").hexdigest()[:12])


class Capture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.tools = fake_toolchain(self.tmp / "tools")
        self.cache = self.tmp / "cache"

    def test_capture_and_load(self):
        app, _ = app_and_patch(["_f"], ["_f", "_g"], [])
        proj = fake_build(self.tmp / "App", self.tools, app_binary=app,
                          objects={"ContentView.swift": b"cv-object", "App.swift": b"app-object"})
        base = B.capture_baseline(proj, "App", self.cache)
        self.assertEqual(base.module, "App")
        self.assertEqual(base.key, B.project_key(proj))
        self.assertEqual(sorted(base.frontend), ["Sources/App/App.swift", "Sources/App/ContentView.swift"])
        fe = base.frontend["Sources/App/ContentView.swift"]
        self.assertEqual(fe[0], str(self.tools / "swift-frontend"))
        self.assertEqual(fe[fe.index("-primary-file") + 1], str(proj / "Sources/App/ContentView.swift"))
        self.assertEqual(base.ld[0], str(self.tools / "ld64.lld"))
        self.assertIn(B.OBJECTS, base.ld)
        self.assertIn("-emit-library", base.link)
        self.assertNotIn("-dead_strip", base.link)
        self.assertEqual(base.team, "ABCDE12345")
        self.assertEqual(base.bundle_id, "dev.example.app")
        self.assertEqual(base.interposable, 1)
        root = self.cache / base.key
        self.assertEqual((root / "sources/Sources/App/ContentView.swift").read_text(), "struct ContentView {}\n")
        self.assertEqual(Path(base.objects["Sources/App/ContentView.swift"]).read_bytes(), b"cv-object")
        self.assertEqual((root / "app.bin").read_bytes(), app)
        self.assertEqual(json.loads((root / "baseline.json").read_text())["module"], "App")
        again = B.load_baseline(proj, self.cache)
        self.assertEqual(again, base)
        self.assertIsNone(B.load_baseline(self.tmp / "Other", self.cache))

    def test_recapture_drops_old_patches(self):
        proj = fake_build(self.tmp / "App", self.tools)
        base = B.capture_baseline(proj, "App", self.cache)
        base.patches_dir.mkdir(parents=True)
        (base.patches_dir / "patch-1.dylib").write_bytes(b"x")
        B.capture_baseline(proj, "App", self.cache)
        self.assertFalse((base.patches_dir / "patch-1.dylib").exists())

    def test_working_dir_is_kept(self):
        proj = fake_build(self.tmp / "App", self.tools)
        base = B.capture_baseline(proj, "App", self.cache)
        self.assertEqual(base.working_dir, str(proj / "xtool"))
        self.assertEqual(B.load_baseline(proj, self.cache).working_dir, str(proj / "xtool"))
        doc = json.loads((self.cache / base.key / "baseline.json").read_text())
        self.assertEqual(doc["working_dir"], str(proj / "xtool"))

    def test_the_app_link_among_several(self):
        proj = fake_build(self.tmp / "App", self.tools)
        bd = proj / ".build/out/Intermediates.noindex/XCBuildData/abc.xcbuilddata"
        manifest = json.loads((bd / "manifest.json").read_text())
        cmds = manifest["commands"]
        app_key = next(k for k in cmds if "target-App-App:" in k)
        app_cmd = cmds.pop(app_key)
        ext = ["/sw/swiftc", "-emit-executable", "-o", "/out/Products/Widget-Ext"]
        manifest["commands"] = {"P2:target-Widget-Ext:Ld /out/Products/Widget-Ext normal": {"args": ext},
                                app_key: app_cmd, **cmds}
        (bd / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(B.link_args(bd, "App", "App"), app_cmd["args"])
        self.assertEqual(B.link_args(bd), ext)                         # no module given: the first, as before
        # keys that don't say which target: the -o that is the app's executable
        renamed = {"P2:a:Ld x normal": {"args": ext},
                   "P2:b:Ld y normal": {"args": ["/sw/swiftc", "-emit-executable", "-o", "/out/MyApp"]}}
        (bd / "manifest.json").write_text(json.dumps({"commands": renamed}))
        self.assertEqual(B.link_args(bd, "App", "MyApp")[-1], "/out/MyApp")
        self.assertEqual(B.link_args(bd, "App", "Nope"), ext)          # nothing matches: the first
        base = B.capture_baseline(proj, "App", self.cache)            # the app's executable here is "App"
        self.assertNotIn("/out/MyApp", base.link)

    def test_unsigned_dev_build_has_no_team(self):
        proj = fake_build(self.tmp / "App", self.tools, team=None)
        self.assertIsNone(B.capture_baseline(proj, "App", self.cache).team)

    def test_missing_pieces_are_no_baseline(self):
        cases = {
            "no build": lambda p: __import__("shutil").rmtree(p / ".build"),
            "no app": lambda p: __import__("shutil").rmtree(p / "xtool"),
            "no link": lambda p: (p / ".build/out/Intermediates.noindex/XCBuildData/abc.xcbuilddata/manifest.json")
            .write_text('{"commands": {}}'),
            "bad manifest": lambda p: (p / ".build/out/Intermediates.noindex/XCBuildData/abc.xcbuilddata/"
                                           "manifest.json").write_text("{"),
            "no executable": lambda p: (p / "xtool/App.app/App").unlink(),
            "garbage executable": lambda p: (p / "xtool/App.app/App").write_bytes(b"garbage"),
        }
        for name, breakit in cases.items():
            with self.subTest(name):
                proj = fake_build(self.tmp / name.replace(" ", "-"), self.tools)
                breakit(proj)
                with self.assertRaises(HelperError) as cm:
                    B.capture_baseline(proj, "App", self.cache)
                self.assertEqual(cm.exception.code, "no-baseline")
                self.assertNotIn("terminal", cm.exception.message)

    def test_wrong_module(self):
        proj = fake_build(self.tmp / "App", self.tools)
        with self.assertRaisesRegex(HelperError, "no compile step"):
            B.capture_baseline(proj, "Nope", self.cache)

    def test_prune_keeps_the_cache_small(self):
        old = []
        for i in range(3):
            d = self.cache / f"old{i}"
            d.mkdir(parents=True)
            (d / "app.bin").write_bytes(b"x" * 1000)
            os.utime(d, (time.time() - 1000 + i, time.time() - 1000 + i))
            old.append(d)
        keep = self.cache / "current"
        keep.mkdir()
        (keep / "app.bin").write_bytes(b"x" * 1000)
        B.prune_cache(self.cache, protect=keep, max_bytes=2500)
        self.assertTrue(keep.exists())
        self.assertEqual([d.exists() for d in old], [False, False, True])


class Snapshot(unittest.TestCase):
    """The baseline is the sources from before the build, swapped in only when complete; baseline.json is compact."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        os.environ["IOSSTUDIO_RUNTIME_DIR"] = str(self.tmp / "rt")
        self.tools = fake_toolchain(self.tmp / "tools")
        self.cache = self.tmp / "cache"

    def staging(self) -> list[str]:
        return [p.name for p in self.cache.iterdir() if p.name.startswith(".")]

    def test_a_save_during_the_build_differs_from_the_baseline(self):
        from iosstudio.features.hotreload import patch
        proj = fake_build(self.tmp / "App", self.tools)
        snap = B.snapshot_sources(proj, self.cache)          # the build starts
        self.assertIn("Sources/App/ContentView.swift", snap.copied)
        cv = proj / "Sources/App/ContentView.swift"
        cv.write_text("struct ContentView { var x = 1 }\n")  # saved while it builds
        base = B.capture_baseline(proj, "App", self.cache, snapshot=snap)
        self.assertEqual(base.source_copy("Sources/App/ContentView.swift").read_text(), "struct ContentView {}\n")
        self.assertEqual(patch.source_changes(base).changed, ["Sources/App/ContentView.swift"])
        self.assertEqual(base.late, [])
        self.assertEqual(self.staging(), [])
        self.assertEqual(snap.digests["Sources/App/ContentView.swift"],
                         __import__("hashlib").sha1(b"struct ContentView {}\n").hexdigest())

    def test_files_the_snapshot_missed_and_changed_are_late(self):
        proj = fake_build(self.tmp / "App", self.tools)
        snap = B.snapshot_sources(proj, self.cache)
        snap.copied.discard("Sources/App/App.swift")            # as if over the snapshot's size cap
        time.sleep(0.01)
        (proj / "Sources/App/App.swift").write_text("struct App { }\n")
        base = B.capture_baseline(proj, "App", self.cache, snapshot=snap)
        self.assertEqual(base.late, ["Sources/App/App.swift"])

    def test_other_targets_are_not_kept(self):
        proj = fake_build(self.tmp / "App", self.tools)
        (proj / "Sources/Kit").mkdir()
        (proj / "Sources/Kit/Model.swift").write_text("struct Model {}\n")
        snap = B.snapshot_sources(proj, self.cache)
        self.assertIn("Sources/Kit/Model.swift", snap.copied)
        base = B.capture_baseline(proj, "App", self.cache, snapshot=snap)
        self.assertFalse((base.sources_dir / "Sources/Kit").exists())

    def test_a_failed_capture_keeps_the_previous_baseline(self):
        proj = fake_build(self.tmp / "App", self.tools)
        base = B.capture_baseline(proj, "App", self.cache)
        base.patches_dir.mkdir(parents=True)
        (base.patches_dir / "patch-1.dylib").write_bytes(b"x")
        snap = B.snapshot_sources(proj, self.cache)
        real = B.self_bound
        B.self_bound = lambda _b: (_ for _ in ()).throw(RuntimeError("boom"))   # fails after the copies
        try:
            with self.assertRaises(RuntimeError):
                B.capture_baseline(proj, "App", self.cache, snapshot=snap)
        finally:
            B.self_bound = real
        self.assertEqual(B.load_baseline(proj, self.cache), base)
        self.assertTrue(base.source_copy("Sources/App/ContentView.swift").is_file())
        self.assertTrue((base.patches_dir / "patch-1.dylib").is_file())
        self.assertEqual(self.staging(), [])

    def test_cancel_stops_the_capture(self):
        from iosstudio.core import subproc
        proj = fake_build(self.tmp / "App", self.tools)
        snap = B.snapshot_sources(proj, self.cache)
        with self.assertRaises(subproc.Cancelled):
            B.capture_baseline(proj, "App", self.cache, snapshot=snap, cancel=lambda: True)
        self.assertFalse((self.cache / B.project_key(proj)).exists())
        B.discard_snapshot(snap)                                 # what the session does after a failed start
        self.assertEqual(self.staging(), [])

    def test_baseline_json_is_compact_and_loads_back(self):
        files = {f"View{i:03}.swift": f"struct View{i} {{}}\n" for i in range(60)}
        proj = fake_build(self.tmp / "App", self.tools, files=files)
        base = B.capture_baseline(proj, "App", self.cache)
        doc = (self.cache / base.key / "baseline.json").read_text()
        self.assertNotIn("\n ", doc)
        self.assertNotIn('"frontend":', doc)
        unpacked = len(json.dumps(base.frontend))
        self.assertLess(len(doc), unpacked / 10)                # O(files), not O(files²)
        self.assertEqual(B.load_baseline(proj, self.cache), base)

    def test_pack_round_trip_odd_commands(self):
        fe = {"a.swift": ["/fe", "-c", "-primary-file", "/a.swift", "/b.swift", "-o", "/o/a.o"],
              "b.swift": ["/fe", "-c", "/a.swift", "-primary-file", "/b.swift", "-o", "/o/b.o"],
              "e.swift": ["/fe", "-c", "-primary-file", "/b.swift", "/a.swift", "-o", "/o/e.o"],  # moved first
              "c.swift": ["/fe", "-c", "/c.swift", "-o", "/o/c.o"],                     # no -primary-file
              "d.swift": ["/fe", "-primary-file", "/d.swift", "-o", "/o/d.o"]}          # another length
        shared, files = B.pack_frontends(fe)
        self.assertEqual(files["b.swift"], {"primary": 3, "set": {"5": "/o/b.o"}})
        self.assertEqual(files["e.swift"], {"primary": 2, "from": 3, "set": {"5": "/o/e.o"}})
        self.assertIn("argv", files["c.swift"])
        self.assertEqual(B.unpack_frontends(json.loads(json.dumps(shared)), json.loads(json.dumps(files))), fe)
        with self.assertRaises(ValueError):
            B.unpack_frontends(shared, {"x": {"primary": 99, "set": {}}})

    def test_prune_spares_live_sessions_and_removes_old_staging(self):
        from iosstudio.features.hotreload import state
        live_key = state.key("/x/Live")
        fh = state.acquire(live_key, "/x/Live")
        self.addCleanup(state.release, fh)
        dirs = []
        for i, name in enumerate([live_key, "old1", "old2"]):
            d = self.cache / name
            d.mkdir(parents=True)
            (d / "app.bin").write_bytes(b"x" * 1000)
            os.utime(d, (time.time() - 1000 + i, time.time() - 1000 + i))
            dirs.append(d)
        keep = self.cache / "current"
        keep.mkdir()
        (keep / "app.bin").write_bytes(b"x" * 1000)
        stale, fresh = self.cache / ".abc-old", self.cache / ".abc-new"
        stale.mkdir()
        fresh.mkdir()
        os.utime(stale, (time.time() - B.STAGE_STALE - 5,) * 2)
        B.prune_cache(self.cache, protect=keep, max_bytes=1500)
        self.assertEqual([d.exists() for d in dirs], [True, False, False])
        self.assertTrue(keep.exists())
        self.assertEqual((stale.exists(), fresh.exists()), (False, True))


class RunTool(unittest.TestCase):
    def test_long_lines_whole_and_stderr_apart(self):
        import sys
        r = B.run_tool([sys.executable, "-c", "import sys; print('x' * 200000); sys.stderr.write('warn')"])
        self.assertEqual((r.returncode, len(r.stdout), r.stderr), (0, 200001, "warn"))

    def test_timeout_and_cancel_kill_the_group(self):
        import subprocess
        import sys

        from iosstudio.core import subproc
        script = "import subprocess, time; subprocess.Popen(['sleep', '30']); time.sleep(30)"
        t0 = time.monotonic()
        r = B.run_tool([sys.executable, "-c", script], timeout=0.5)
        self.assertEqual(r.returncode, 124)
        calls = []
        with self.assertRaises(subproc.Cancelled):
            B.run_tool([sys.executable, "-c", script], cancel=lambda: calls.append(1) or len(calls) > 2)
        self.assertLess(time.monotonic() - t0, 5)
        time.sleep(0.2)
        left = subprocess.run(["pgrep", "-f", script], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")
        self.assertEqual(B.run_tool(["/nonexistent/tool"]).returncode, 127)


if __name__ == "__main__":
    unittest.main()
