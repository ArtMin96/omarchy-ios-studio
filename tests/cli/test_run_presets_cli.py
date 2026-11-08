"""Run presets through bin/iosstudio-helper with a fake xtool: storage in projects.json, `build run --preset` failing
fast on an unknown name, and demo mode."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import ROOT, isolated_env, write_exe

GERMAN = {"name": "German", "language": "de", "region": "de_DE"}
DEBUG = {"name": "Debug flags", "args": ["-ShowDebugMenu", "YES"], "env": {"LOG_LEVEL": "debug"}}

# A fake xtool: signed in, no USB devices, every build succeeds without compiling anything.
FAKE_XTOOL = """\
#!/usr/bin/env python3
import sys
if sys.argv[1:3] == ["auth", "status"]:
    print("Logged in."); sys.exit(0)
if sys.argv[1:2] == ["devices"]:
    sys.exit(0)
print("fake xtool", " ".join(sys.argv[1:]))
print("Building for debugging...")
print("[1/2] Compiling App App.swift")
print("Build complete! (0.1 secs)")
"""


class HelperBase(unittest.TestCase):
    """Runs bin/iosstudio-helper with its own XDG dirs (state in a temp folder, never the user's)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="run-presets-"))
        self.env = isolated_env(self.tmp)
        self.env["IOS_STUDIO_XTOOL"] = str(write_exe(self.tmp / "xtool", FAKE_XTOOL))
        self.proj = self.tmp / "App"
        self.proj.mkdir()
        (self.proj / "Package.swift").write_text('// swift-tools-version: 6.0\nlet package = Package(name: "App")\n')
        (self.proj / "xtool.yml").write_text("version: 1\nbundleID: com.example.app\nproduct: App\n")
        self.state_file = self.tmp / "xdg_state_home" / "ios-studio" / "projects.json"

    def helper(self, *args, env: dict | None = None) -> tuple[int, list[dict]]:
        r = subprocess.run([str(ROOT / "bin" / "iosstudio-helper"), *args], env=env or self.env, capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        events = [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]
        return r.returncode, events

    def presets(self, *args) -> dict:
        rc, ev = self.helper("projects", "presets", "--path", str(self.proj), *args)
        return ev[-1]


class Storage(HelperBase):
    def test_round_trip(self):
        self.assertEqual(self.presets()["data"], {"path": str(self.proj), "presets": [], "selected": None})
        d = self.presets("--save", json.dumps(GERMAN))["data"]
        self.assertEqual(d["saved"], "German")
        self.assertEqual(d["presets"][0]["resolved"]["args"], ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"])
        self.presets("--save", json.dumps(DEBUG))
        self.assertEqual(self.presets("--select", "german")["data"]["selected"], "German")
        # a new helper process reads it back from projects.json
        d = self.presets()["data"]
        self.assertEqual([p["name"] for p in d["presets"]], ["German", "Debug flags"])
        self.assertEqual(d["selected"], "German")
        stored = json.loads(self.state_file.read_text())["meta"][str(self.proj)]
        self.assertEqual(stored["runPreset"], "German")
        self.assertNotIn("resolved", stored["runPresets"][0])
        self.assertEqual(self.presets("--resolve", "Debug flags")["data"],
                         {"name": "Debug flags", "args": ["-ShowDebugMenu", "YES"], "env": {"LOG_LEVEL": "debug"}})
        rc, ev = self.helper("projects", "info", "--path", str(self.proj))
        self.assertEqual(ev[-1]["data"]["runPreset"], "German")
        self.assertEqual(len(ev[-1]["data"]["runPresets"]), 2)
        self.assertIsNone(self.presets("--select", "")["data"]["selected"])
        self.assertNotIn("runPreset", json.loads(self.state_file.read_text())["meta"][str(self.proj)])

    def test_delete_dry_run_changes_nothing(self):
        self.presets("--save", json.dumps(GERMAN))
        self.presets("--select", "German")
        before = self.state_file.read_text()
        d = self.presets("--delete", "german", "--dry-run")["data"]
        self.assertEqual((d["deleted"], d["dryRun"], d["selected"]), ("German", True, "German"))
        self.assertEqual(self.state_file.read_text(), before)
        d = self.presets("--delete", "German")["data"]
        self.assertEqual((d["presets"], d["selected"], d["dryRun"]), ([], None, False))
        self.assertNotIn(str(self.proj), json.loads(self.state_file.read_text())["meta"])

    def test_errors(self):
        self.presets("--save", json.dumps(GERMAN))
        e = self.presets("--select", "French")["error"]
        self.assertEqual(e, {"code": "unknown-preset", "message": "No run preset named 'French'. This project has: German."})
        self.assertEqual(self.presets("--save", '{"name": "x", "region": "nowhere"}')["error"]["code"], "invalid-preset")
        self.assertEqual(self.presets("--save", "not json")["error"]["code"], "invalid-preset")
        (self.proj / "xtool.yml").unlink()
        self.assertEqual(self.presets()["error"]["code"], "not-a-project")

    def test_old_state_without_presets_stays_readable(self):
        self.state_file.parent.mkdir(parents=True)
        old = {"added": [str(self.proj)], "hidden": [], "meta": {str(self.proj): {"autoRenew": True}}}
        self.state_file.write_text(json.dumps(old))
        rc, ev = self.helper("projects", "info", "--path", str(self.proj))
        self.assertEqual((ev[-1]["data"]["runPresets"], ev[-1]["data"]["runPreset"]), ([], None))
        self.assertTrue(ev[-1]["data"]["autoRenew"])
        self.presets("--save", json.dumps(GERMAN))
        meta = json.loads(self.state_file.read_text())["meta"][str(self.proj)]
        self.assertTrue(meta["autoRenew"])                  # other settings survive a save


class BuildRun(HelperBase):
    UDID = "00008030-001A2B3C4D5E6F70"

    def run_build(self, *extra) -> tuple[dict, list[dict]]:
        rc, ev = self.helper("build", "run", "--path", str(self.proj), "--udid", self.UDID, *extra)
        return ev[-1], ev

    def test_unknown_preset_fails_before_building(self):
        self.presets("--save", json.dumps(GERMAN))
        done, ev = self.run_build("--preset", "French")
        self.assertFalse(done["ok"])
        self.assertEqual(done["error"]["code"], "unknown-preset")
        self.assertIn("This project has: German.", done["error"]["message"])
        self.assertFalse(any(e.get("event") in ("build", "step", "log") for e in ev), ev)   # nothing started
        rc, hist = self.helper("build", "history")
        self.assertEqual(hist[-1]["data"]["builds"], [])

    def test_preset_reaches_the_launch(self):
        self.presets("--save", json.dumps(GERMAN))
        done, _ev = self.run_build("--preset", "german")
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["data"]["launch"], {"bundleId": "com.example.app", "udid": self.UDID,
                                                  "args": ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"],
                                                  "env": {}, "preset": "German"})
        rc, hist = self.helper("build", "history")
        self.assertEqual(hist[-1]["data"]["builds"][0]["preset"], "German")

    def test_no_preset_launches_plain(self):
        done, _ev = self.run_build()
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["data"]["launch"], {"bundleId": "com.example.app", "udid": self.UDID, "args": [],
                                                  "env": {}, "preset": None})


class Demo(HelperBase):
    def demo(self, *args) -> dict:
        rc, ev = self.helper(*args, env={**self.env, "IOSSTUDIO_DEMO": "1"})
        return ev[-1]

    def test_demo_presets_and_saves_change_nothing(self):
        ht = os.path.expanduser("~/Developer/HabitTracker")
        d = self.demo("projects", "presets", "--path", ht)["data"]
        self.assertTrue(d["demo"])
        self.assertEqual([p["name"] for p in d["presets"]], ["German", "Debug flags"])
        self.assertEqual(d["presets"][0]["resolved"]["args"], ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"])
        saved = self.demo("projects", "presets", "--path", ht, "--save", '{"name": "French", "language": "fr"}')["data"]
        self.assertEqual(saved["saved"], "French")
        self.assertEqual(len(saved["presets"]), 3)
        self.assertEqual(len(self.demo("projects", "presets", "--path", ht)["data"]["presets"]), 2)   # not kept
        self.assertEqual(self.demo("projects", "presets", "--path", ht, "--resolve", "x")["error"]["message"],
                         "No run preset named 'x'. This project has: German, Debug flags.")
        info = self.demo("projects", "info", "--path", ht)["data"]
        self.assertEqual((len(info["runPresets"]), info["runPreset"]), (2, "German"))
        self.assertFalse(self.state_file.exists())

    def test_demo_build_echoes_the_preset(self):
        wg = os.path.expanduser("~/Developer/WeatherGlance")
        done = self.demo("build", "run", "--path", wg, "--udid", "U", "--preset", "UI tests")
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["data"]["launch"]["preset"], "UI tests")
        self.assertEqual(done["data"]["launch"]["args"], ["-ResetData", "-UITestMode", "YES"])
        self.assertEqual(done["data"]["launch"]["env"], {"WEATHER_STUB": "sunny"})
        done = self.demo("build", "run", "--path", wg, "--udid", "U", "--preset", "Nope")
        self.assertEqual(done["error"]["code"], "unknown-preset")
