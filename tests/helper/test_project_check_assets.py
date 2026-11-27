"""The asset-catalogs project check (helper/iosstudio/features/project_check.py, _check_assets).

An .xcassets in an app target whose Package.swift entry lists no resources: SwiftPM still compiles Assets.car, but
xtool only packs a target's resource bundle when the target lists resources, so colors and images go missing on the
iPhone. Each test writes a small fake package to a temp folder. Stdlib only:
    python -m pytest tests/helper/test_project_check_assets.py
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from iosstudio.features import project_check as C

NO_CACHE = Path(tempfile.gettempdir()) / "ios-studio-test-no-xtool-cache"

HEAD = '// swift-tools-version: 6.0\nimport PackageDescription\n\nlet package = Package(\n    name: "App",\n'


def manifest(targets: str) -> str:
    return HEAD + '    products: [.library(name: "App", targets: ["App"])],\n    targets: [\n' + targets + "    ]\n)\n"


class Assets(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "xtool.yml").write_text("version: 1\nbundleID: com.example.App\n")

    def tearDown(self):
        self._tmp.cleanup()

    def pkg(self, targets: str, catalogs: tuple[str, ...] = ("Sources/App/Assets.xcassets",)) -> list[dict]:
        (self.root / "Package.swift").write_text(manifest(targets))
        (self.root / "Sources/App").mkdir(parents=True, exist_ok=True)
        (self.root / "Sources/App/App.swift").write_text("import SwiftUI\n")
        for c in catalogs:
            (self.root / c / "AccentColor.colorset").mkdir(parents=True, exist_ok=True)
            (self.root / c / "Contents.json").write_text("{}")
        return self.issues()

    def issues(self) -> list[dict]:
        return [i for i in C.check(self.root, cache_dir=NO_CACHE)["issues"] if i["check"] == "asset-catalogs"]

    def test_undeclared_catalog_warns(self):
        [i] = self.pkg('        .target(\n            name: "App",\n            dependencies: []\n        ),\n')
        self.assertEqual((i["severity"], i["category"], i["file"], i["line"]), ("warning", "config", "Package.swift", 8))
        self.assertIn("Assets.xcassets in target App won't be in the app", i["message"])
        self.assertIn('resources: [.process("Assets.xcassets")]', i["hint"])
        self.assertNotIn("terminal", i["hint"])
        self.assertIn("asset-catalogs", C.CHECKS)

    def test_declared_catalog_is_quiet(self):
        self.assertEqual(self.pkg('        .target(name: "App", resources: [.process("Assets.xcassets")]),\n'), [])
        self.assertEqual(self.pkg('        .target(name: "App", resources: [.copy("Assets.xcassets")]),\n'), [])

    def test_parent_folder_or_any_resource_is_enough(self):
        cats = ("Sources/App/Resources/Assets.xcassets",)
        self.assertEqual(self.pkg('        .target(name: "App", resources: [.process("Resources")]),\n', cats), [])
        # any listed resource makes xtool pack the bundle, and SwiftPM puts the catalog in it
        self.assertEqual(self.pkg('        .target(name: "App", resources: [.process("data.json")]),\n', cats), [])

    def test_empty_or_commented_resources_warn(self):
        self.assertEqual(len(self.pkg('        .target(name: "App", resources: []),\n')), 1)
        self.assertEqual(len(self.pkg('        .target(name: "App"\n            // , resources: [.process("Assets.xcassets")]\n'
                                      '        ),\n')), 1)

    def test_custom_path(self):
        cats = ("App/Assets.xcassets", "App/More/Icons.xcassets")
        [i] = self.pkg('        .executableTarget(name: "App", path: "App"),\n', cats)
        self.assertIn("The asset catalogs Assets.xcassets, More/Icons.xcassets in target App", i["message"])
        self.assertIn('.process("Assets.xcassets"), .process("More/Icons.xcassets")', i["hint"])
        self.assertEqual(self.pkg('        .executableTarget(name: "App", path: "App", resources: [.process("More")]),\n',
                                  cats), [])

    def test_test_targets_and_catalogs_outside_targets_are_quiet(self):
        cats = ("Tests/AppTests/Fixtures.xcassets", "Design/Assets.xcassets", "Sources/Unused/Assets.xcassets")
        self.assertEqual(self.pkg('        .target(name: "App", resources: [.process("Assets.xcassets")]),\n'
                                  '        .testTarget(name: "AppTests", dependencies: [.target(name: "App")]),\n',
                                  cats + ("Sources/App/Assets.xcassets",)), [])

    def test_target_dependency_is_not_a_target(self):
        # `.target(name: "Core")` in dependencies names another target; only the real Core target counts
        cats = ("Sources/Core/Colors.xcassets",)
        (self.root / "Sources/Core").mkdir(parents=True)
        issues = self.pkg('        .target(name: "App", dependencies: [.target(name: "Core")]),\n'
                          '        .target(name: "Core"),\n', cats)
        self.assertEqual([i["line"] for i in issues], [9])
        self.assertIn("target Core", issues[0]["message"])

    def test_excluded_catalog_and_unreadable_resources_are_quiet(self):
        self.assertEqual(self.pkg('        .target(name: "App", exclude: ["Assets.xcassets"]),\n'), [])
        self.assertEqual(self.pkg('        .target(name: "App", resources: appResources),\n'), [])

    def test_broken_or_missing_manifest_is_quiet(self):
        self.assertEqual(self.pkg('        .target(name: "App", resources: [\n'), [])
        (self.root / "Package.swift").unlink()
        self.assertEqual(self.issues(), [])

    def test_catalog_insides_not_walked_and_budget_respected(self):
        big = self.root / "Sources/App/Assets.xcassets"
        for n in range(50):
            (big / f"Image{n}.imageset/Nested.xcassets").mkdir(parents=True)
        self.assertEqual(len(self.pkg('        .target(name: "App"),\n')), 1)
        res = C.check(self.root, deadline=time.monotonic() - 1, cache_dir=NO_CACHE)
        self.assertTrue(res["partial"])
        self.assertEqual([i for i in res["issues"] if i["check"] == "asset-catalogs"], [])


if __name__ == "__main__":
    unittest.main()
