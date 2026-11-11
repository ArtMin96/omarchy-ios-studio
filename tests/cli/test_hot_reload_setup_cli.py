"""`hotreload enable` / `hotreload disable` through bin/iosstudio-helper (/usr/bin/python3 -I): demo mode answers,
real mode on a temp project with the fake swift (tests/support/hot_reload_setup_swift.py) and stand-in templates
(tests/support/hot_reload_setup.py), a manifest that wouldn't work, and once with the real swift when it's
installed (the block must evaluate in a real Package.swift). The real-project round trip runs on a temp copy
of ~/Projects/CrashLab (skipped when it's missing).

    .venv/bin/pytest -q tests/cli/test_hot_reload_setup_cli.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import HELPER, isolated_env
from support.hot_reload_setup import FAKE_SWIFT, make_templates
from support.projects import package_project

BEGIN = "// >>> iOS Studio hot reload"
CRASHLAB = Path.home() / "Projects" / "CrashLab"     # the owner's real-iPhone test app: only ever copied


class CLI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.templates = make_templates(self.tmp / "templates")
        self.env = isolated_env(self.tmp, IOSSTUDIO_SWIFT=str(FAKE_SWIFT),
                                IOSSTUDIO_HOT_RELOAD_TEMPLATES=str(self.templates))
        self.env.pop("FAKE_HR_SWIFT_BROKEN", None)
        self.root = package_project(self.tmp / "App")
        self.before = (self.root / "Package.swift").read_bytes()

    def helper(self, *args: str, env: dict | None = None) -> dict:
        r = subprocess.run([str(HELPER), "hotreload", *args], capture_output=True, text=True,
                           env={**self.env, **(env or {})}, timeout=120)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, r.stdout + r.stderr)
        return json.loads(lines[0])

    def test_enable_disable_real_mode(self):
        res = self.helper("enable", "--project", str(self.root), "--dry-run")
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["data"]["dryRun"], res["data"]["changed"]), (True, True))
        self.assertFalse((self.root / ".iosstudio").exists())
        res = self.helper("enable", "--project", str(self.root))
        self.assertTrue(res["ok"], res)
        d = res["data"]
        self.assertEqual((d["enabled"], d["mode"], d["appTarget"], d["changed"]), (True, "on", "App", True))
        self.assertIn(BEGIN, (self.root / "Package.swift").read_text())
        self.assertTrue((self.root / ".iosstudio/hot-reload/Package.swift").is_file())
        self.assertEqual((self.root / ".iosstudio/.gitignore").read_text(), "*\n")
        res = self.helper("enable", "--project", str(self.root))
        self.assertEqual(res["data"]["changed"], False)
        res = self.helper("disable", "--project", str(self.root))
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["data"]["enabled"], res["data"]["mode"], res["data"]["importedBy"]), (False, "off", []))
        self.assertEqual((self.root / "Package.swift").read_bytes(), self.before)
        self.assertFalse((self.root / ".iosstudio").exists())

    def test_disable_to_stub(self):
        self.helper("enable", "--project", str(self.root))
        src = self.root / "Sources/App/ContentView.swift"
        src.parent.mkdir(parents=True)
        src.write_text("import SwiftUI\nimport HotReload\n")
        res = self.helper("disable", "--project", str(self.root))
        self.assertEqual((res["data"]["mode"], res["data"]["importedBy"]), ("stub", ["Sources/App/ContentView.swift"]))
        self.assertNotIn("linkerSettings", (self.root / "Package.swift").read_text())

    def test_broken_manifest_changes_nothing(self):
        res = self.helper("enable", "--project", str(self.root), env={"FAKE_HR_SWIFT_BROKEN": "1"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "manifest-invalid")
        self.assertTrue(res["error"].get("diagnostics"))
        self.assertEqual((self.root / "Package.swift").read_bytes(), self.before)
        self.assertFalse((self.root / ".iosstudio").exists())

    def test_errors(self):
        res = self.helper("enable")
        self.assertEqual((res["ok"], res["error"]["code"]), (False, "usage"))
        res = self.helper("enable", "--project", str(self.tmp / "nope"))
        self.assertEqual(res["error"]["code"], "not-found")
        r = subprocess.run([str(HELPER), "hotreload", "nope"], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "usage")

    def test_demo_mode(self):
        env = {"IOSSTUDIO_DEMO": "1"}
        project = str(self.tmp / "Developer/WeatherGlance")
        res = self.helper("enable", "--project", project, env=env)
        self.assertTrue(res["ok"], res)
        d = res["data"]
        self.assertEqual((d["enabled"], d["mode"], d["appTarget"], d["demo"]), (True, "on", "WeatherGlance", True))
        self.assertIn("+// >>> iOS Studio hot reload", d["diff"])
        res = self.helper("disable", "--project", project, env=env)
        self.assertEqual((res["data"]["mode"], len(res["data"]["importedBy"])), ("stub", 2))
        res = self.helper("disable", "--project", str(self.tmp / "Developer/RecipeBox"), env=env)
        self.assertEqual((res["data"]["mode"], res["data"]["importedBy"]), ("off", []))
        res = self.helper("enable", env=env)
        self.assertEqual(res["error"]["code"], "usage")
        self.assertFalse((self.tmp / "Developer").exists())

@unittest.skipUnless(CRASHLAB.joinpath("Package.swift").is_file(), "needs ~/Projects/CrashLab")
class CrashLab(unittest.TestCase):
    """A copy of ~/Projects/CrashLab (never the folder itself: it has the owner's uncommitted changes), whose
    manifest has a remote package and a target with a long product list: enable and disable give back its bytes."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "CrashLab"
        shutil.copytree(CRASHLAB, self.root, symlinks=True, ignore=shutil.ignore_patterns(".build", "xtool", ".git"))
        self.before = (self.root / "Package.swift").read_bytes()
        self.env = isolated_env(self.tmp, IOSSTUDIO_HOT_RELOAD_TEMPLATES=str(make_templates(self.tmp / "templates")))
        self.env.pop("FAKE_HR_SWIFT_BROKEN", None)

    def helper(self, *args: str, env: dict) -> dict:
        r = subprocess.run([str(HELPER), "hotreload", *args, "--project", str(self.root)], capture_output=True,
                           text=True, env=env, timeout=300)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def round_trip(self, env: dict) -> None:
        res = self.helper("enable", env=env)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["data"]["appTarget"], res["data"]["mode"]), ("CrashLab", "on"))
        text = (self.root / "Package.swift").read_bytes()
        self.assertTrue(text.startswith(self.before))
        self.assertIn(b'target.name == "CrashLab"', text)
        self.assertEqual(self.helper("enable", env=env)["data"]["changed"], False)
        res = self.helper("disable", env=env)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["mode"], "off")
        self.assertEqual((self.root / "Package.swift").read_bytes(), self.before)
        self.assertFalse((self.root / ".iosstudio").exists())

    def test_round_trip_with_fake_swift(self):
        self.round_trip({**self.env, "IOSSTUDIO_SWIFT": str(FAKE_SWIFT)})

    @unittest.skipUnless(shutil.which("swift"), "needs swift")
    def test_real_swift_accepts_the_block(self):
        res = self.helper("enable", env=self.env)
        self.assertTrue(res["ok"], res)
        dump = subprocess.run(["swift", "package", "--package-path", str(self.root), "--scratch-path",
                               str(self.tmp / "scratch"), "dump-package"], capture_output=True, text=True,
                              timeout=300)
        self.assertEqual(dump.returncode, 0, dump.stderr)
        app = next(t for t in json.loads(dump.stdout)["targets"] if t["name"] == "CrashLab")
        self.assertIn({"product": ["HotReload", "hot-reload", None, None]}, app["dependencies"])
        self.assertEqual(len(app["dependencies"]), 13)        # its 12 products are kept
        self.assertIn("-interposable", json.dumps(app.get("settings")))
        res = self.helper("disable", env=self.env)
        self.assertTrue(res["ok"], res)
        self.assertEqual((self.root / "Package.swift").read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
