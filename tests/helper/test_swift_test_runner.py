"""The `test` group's runner pieces in process (helper/iosstudio/features/testing/{project,runner,store}.py):
dump-package parsing, the Apple-only import scan (#if guards, comments, attributes), the facts cache and its
invalidation, seeding .build/host-tests from xtool's .build, --filter checks, the run lock path and log pruning."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.core.output import HelperError
from iosstudio.features.testing import project, runner, store
from support import ROOT

FIXTURES = ROOT / "tests" / "fixtures" / "swift-test"


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swift-test-unit-")).resolve()
        self.root = self.tmp / "CrashLab"
        self.root.mkdir()
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache"),
                                                "XDG_STATE_HOME": str(self.tmp / "state")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class DumpPackage(Base):
    def test_real_dump(self):
        dump = json.loads((FIXTURES / "dump-package.json").read_text())
        write(self.root, "Sources/AppCore/Math.swift", "")
        facts = project.parse_dump(dump, self.root)
        self.assertEqual(facts.name, "CrashLab")
        self.assertEqual(facts.product, "CrashLabPackageTests")
        self.assertEqual(facts.test_targets, [{"name": "AppCoreTests", "path": "Tests/AppCoreTests"},
                                              {"name": "AppCoreSwiftTests", "path": "Tests/AppCoreSwiftTests"}])
        self.assertEqual(facts.tested_targets, ["AppCore"])   # not the app target, not swift-collections
        self.assertEqual(facts.paths["AppCore"], "Sources/AppCore")

    def test_transitive_local_targets_and_custom_paths(self):
        dump = {"name": "Pkg", "targets": [
            {"name": "App", "type": "regular", "dependencies": [{"byName": ["Core", None]}]},
            {"name": "Core", "type": "regular", "path": "Lib/Core/",
             "dependencies": [{"target": ["Model", None]}, {"product": ["Algorithms", "swift-algorithms", None, None]}]},
            {"name": "Model", "type": "regular", "dependencies": [{"byName": ["Collections", None]}]},
            {"name": "CoreTests", "type": "test", "path": "Checks", "dependencies": [{"byName": ["Core", None]}]},
        ]}
        write(self.root, "Source/Model/M.swift", "")
        facts = project.parse_dump(dump, self.root)
        self.assertEqual(facts.tested_targets, ["Core", "Model"])
        self.assertEqual(facts.paths, {"CoreTests": "Checks", "Core": "Lib/Core", "Model": "Source/Model"})
        self.assertEqual(facts.test_targets, [{"name": "CoreTests", "path": "Checks"}])

    def test_no_test_targets(self):
        facts = project.parse_dump({"name": "X", "targets": [{"name": "X", "type": "regular"}]}, self.root)
        self.assertEqual((facts.test_targets, facts.tested_targets), ([], []))


class Blockers(Base):
    def facts(self) -> project.PackageFacts:
        return project.PackageFacts("CrashLab", [{"name": "AppCoreTests", "path": "Tests/AppCoreTests"}], ["AppCore"],
                                    {"AppCore": "Sources/AppCore", "AppCoreTests": "Tests/AppCoreTests"})

    def test_imports(self):
        f = write(self.root, "Sources/AppCore/A.swift", "\n".join([
            "// import UIKit in a comment",
            "/* import SwiftUI",
            "   still a comment */",
            "import Foundation",
            "@preconcurrency import Combine",
            "@_exported public import MapKit",
            "import struct SwiftUI.Color",
            "#if canImport(UIKit)",
            "import UIKit",
            "#elseif os(macOS)",
            "import AppKit",
            "#endif",
            "#if DEBUG",
            "#if canImport(WidgetKit)",
            "import WidgetKit",
            "#endif",
            "#endif",
            "import XCTest",
            "let importantValue = 1   // not an import",
        ]))
        self.assertEqual(project.scan_imports(f), [(5, "Combine"), (6, "MapKit"), (7, "SwiftUI")])

    def test_tested_and_test_targets_only(self):
        write(self.root, "Sources/AppCore/Deep/Nested/View.swift", "import SwiftUI\n")
        write(self.root, "Sources/CrashLab/App.swift", "import SwiftUI\n")          # the app: not tested
        write(self.root, "Tests/AppCoreTests/UITests.swift", "@testable import AppCore\nimport UIKit\n")
        write(self.root, "Sources/AppCore/.hidden/X.swift", "import UIKit\n")
        write(self.root, "Sources/AppCore/notes.txt", "import UIKit\n")
        got = project.host_blockers(self.root, self.facts())
        self.assertEqual(got, [
            {"target": "AppCore", "file": str(self.root / "Sources/AppCore/Deep/Nested/View.swift"), "line": 1,
             "module": "SwiftUI"},
            {"target": "AppCoreTests", "file": str(self.root / "Tests/AppCoreTests/UITests.swift"), "line": 2,
             "module": "UIKit"}])

    def test_capped(self):
        for i in range(30):
            write(self.root, f"Sources/AppCore/V{i:02d}.swift", "import SwiftUI\n")
        self.assertEqual(len(project.host_blockers(self.root, self.facts())), project.MAX_BLOCKERS)
        with mock.patch.object(project, "SCAN_FILES", 3):
            self.assertEqual(len(project.host_blockers(self.root, self.facts())), 3)

    def test_reads_only_the_start_of_a_file(self):
        f = write(self.root, "Sources/AppCore/Big.swift", "let x = 1\n" * 10_000 + "import UIKit\n")
        self.assertEqual(project.scan_imports(f), [])


class FactsCache(Base):
    def setUp(self):
        super().setUp()
        write(self.root, "Package.swift", 'let package = Package(name: "CrashLab")\n')
        self.swift = write(self.tmp, "swift", "#!/bin/sh\n")
        self.swift.chmod(0o755)
        self.dumps = 0

    def fake_dump(self, root, swift):
        self.dumps += 1
        return {"name": f"CrashLab{self.dumps}", "targets": [{"name": "T", "type": "test"}]}

    def facts(self, root: Path | None = None) -> project.PackageFacts:
        with mock.patch.object(project, "_dump_package", self.fake_dump):
            return project.package_facts(root or self.root, str(self.swift))

    def test_cached_until_package_swift_or_swift_changes(self):
        self.assertEqual(self.facts().name, "CrashLab1")
        self.assertEqual(self.facts().name, "CrashLab1")
        self.assertEqual(self.dumps, 1)
        (self.root / "Package.swift").write_text('let package = Package(name: "CrashLab")\n// more\n')
        self.assertEqual(self.facts().name, "CrashLab2")
        st = self.swift.stat()
        os.utime(self.swift, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
        self.assertEqual(self.facts().name, "CrashLab3")
        self.assertEqual(self.facts().name, "CrashLab3")

    def test_bounded(self):
        with mock.patch.object(project, "CACHE_MAX", 3):
            roots = []
            for i in range(5):
                r = self.tmp / f"P{i}"
                write(r, "Package.swift", "x\n")
                roots.append(r)
                self.facts(r)
            data = json.loads((self.tmp / "cache" / "ios-studio" / project.CACHE_NAME).read_text())
            self.assertEqual(list(data["projects"]), [str(r) for r in roots[-3:]])

    def test_build_system_choice(self):
        sig = project.swift_signature(str(self.swift))
        self.assertIsNone(project.build_system(sig))
        project.remember_build_system(sig, "default")
        self.assertEqual(project.build_system(sig), "default")
        self.facts()     # the facts cache keeps it
        self.assertEqual(project.build_system(sig), "default")

    def test_project_root_and_toolchain(self):
        with self.assertRaises(HelperError) as cm:
            project.project_root({})
        self.assertEqual(cm.exception.code, "not_project")
        with self.assertRaises(HelperError) as cm:
            project.project_root({"project": str(self.tmp)})
        self.assertEqual(cm.exception.code, "not_project")
        self.assertEqual(project.project_root({"project": str(self.root) + "/"}), self.root)
        with mock.patch.dict(os.environ, {"IOSSTUDIO_SWIFT": str(self.tmp / "nope")}):
            with self.assertRaises(HelperError) as cm:
                project.find_swift()
        self.assertEqual(cm.exception.code, "toolchain")


class Seed(Base):
    def test_seeds_once_from_xtools_build(self):
        build = self.root / ".build"
        write(build, "checkouts/swift-collections/Package.swift", "// pkg\n")
        write(build, "repositories/swift-collections-1/HEAD", "ref\n")
        write(build, "workspace-state.json", "{}\n")
        write(build, "debug/Big.o", "x" * 1000)                      # build products are not copied
        scratch = build / "host-tests"
        self.assertEqual(runner.seed(self.root, scratch), "repositories, checkouts, workspace-state.json")
        self.assertEqual((scratch / "checkouts/swift-collections/Package.swift").read_text(), "// pkg\n")
        self.assertTrue((scratch / "repositories/swift-collections-1/HEAD").is_file())
        self.assertFalse((scratch / "debug").exists())
        (scratch / "checkouts/swift-collections/Package.swift").write_text("changed\n")
        self.assertEqual((build / "checkouts/swift-collections/Package.swift").read_text(), "// pkg\n",
                         "a copy, never a hard link into xtool's checkout")
        self.assertIsNone(runner.seed(self.root, scratch), "only the first run seeds")

    def test_nothing_to_seed(self):
        self.assertIsNone(runner.seed(self.root, self.root / ".build/host-tests"))
        self.assertFalse((self.root / ".build/host-tests").exists())

    def test_failed_copy_falls_back(self):
        write(self.root, ".build/workspace-state.json", "{}\n")
        write(self.root, ".build/checkouts/a/x", "")
        with mock.patch.object(runner.subproc, "run_group",
                               return_value=mock.Mock(returncode=1)):
            self.assertIsNone(runner.seed(self.root, self.root / ".build/host-tests"))


class Filter(unittest.TestCase):
    def test_filter(self):
        self.assertIsNone(runner.parse_filter({}))
        self.assertIsNone(runner.parse_filter({"filter": True}))
        self.assertIsNone(runner.parse_filter({"filter": "  "}))
        self.assertEqual(runner.parse_filter({"filter": " MathTests/testAdd$ "}), "MathTests/testAdd$")
        for bad in ("Math(", "[a", "x" * (runner.FILTER_MAX + 1)):
            with self.assertRaises(HelperError) as cm:
                runner.parse_filter({"filter": bad})
            self.assertEqual(cm.exception.code, "bad_filter")

    def test_native_rejected_pattern(self):
        self.assertTrue(runner.NATIVE_REJECTED.match("error: Invalid value 'native' for '--build-system <x>'"))
        self.assertTrue(runner.NATIVE_REJECTED.match("Error: Unknown option '--build-system'"))
        self.assertFalse(runner.NATIVE_REJECTED.match(
            "warning: '--build-system native' has been deprecated and will be removed in a future release"))


class Store(Base):
    def test_last_runs_capped_and_newest_last(self):
        with mock.patch.object(store, "LAST_MAX", 3):
            for i in range(5):
                store.remember(Path(f"/p/{i}"), {"ok": True, "n": i})
            store.remember(Path("/p/2"), {"ok": False})
        runs = store.last_runs()
        self.assertEqual(list(runs), ["/p/3", "/p/4", "/p/2"])
        self.assertEqual(store.last_run(Path("/p/2")), {"ok": False})
        self.assertIsNone(store.last_run(Path("/p/0")))

    def test_logs_pruned_and_capped(self):
        d = store.runs_dir()
        d.mkdir(parents=True)
        for i in range(25):
            f = d / f"old-{i:02d}.log"
            f.write_text("x" * 100)
            os.utime(f, (1_600_000_000 + i,) * 2)
        log = store.RunLog("My App/1")
        self.assertRegex(log.path.name, r"^\d{8}-\d{6}-\d{3}-My_App_1\.log$")
        with mock.patch.object(store, "LOG_FILE_MAX", 50):
            for _ in range(10):
                log.write("0123456789")
        path = log.close()
        self.assertEqual(len(list(d.glob("*.log"))), store.LOG_KEEP)
        self.assertTrue(Path(path).is_file())
        self.assertIn("log cut", Path(path).read_text())
        self.assertLess(Path(path).stat().st_size, 200)

    def test_lock_is_per_project_and_not_the_build_lock(self):
        a, b = store.lock_file(Path("/p/a")), store.lock_file(Path("/p/b"))
        self.assertNotEqual(a, b)
        self.assertEqual(a.parent, store.runs_dir() / "locks")


class Timeouts(unittest.TestCase):
    def test_env_override(self):
        with mock.patch.dict(os.environ, {"IOSSTUDIO_TEST_RUN_TIMEOUT": "5"}):
            self.assertEqual(runner._timeout("IOSSTUDIO_TEST_RUN_TIMEOUT", 600), 5)
        with mock.patch.dict(os.environ, {"IOSSTUDIO_TEST_RUN_TIMEOUT": "soon"}):
            self.assertEqual(runner._timeout("IOSSTUDIO_TEST_RUN_TIMEOUT", 600), 600)
        self.assertEqual(runner._minutes(1200), "20 minutes")
        self.assertEqual(runner._minutes(2), "2 s")


if __name__ == "__main__":
    unittest.main()
