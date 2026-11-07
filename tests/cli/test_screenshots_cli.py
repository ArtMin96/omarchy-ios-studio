"""Screenshot compare through bin/iosstudio-helper: save, compare, masks, list and demo mode."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import FIXTURES, HELPER

FIX = FIXTURES / "screenshots"


class Cli(unittest.TestCase):
    def run_helper(self, *args, env=None):
        e = dict(os.environ, IOSSTUDIO_SCREENSHOTS_PURE="1", **(env or {}))
        out = subprocess.run([str(HELPER), "screenshots", *args], capture_output=True, text=True, env=e, timeout=60)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_real_cli(self):
        with tempfile.TemporaryDirectory() as t:
            proj = Path(t) / "App"
            proj.mkdir()
            env = {"XDG_DATA_HOME": t + "/data", "IOSSTUDIO_DEMO": ""}
            r = self.run_helper("save", "--project", str(proj), "--name", "Home", "--shot", str(FIX / "base.png"), env=env)
            self.assertTrue(r["ok"], r)
            self.assertTrue(r["data"]["baseline_path"].startswith(t + "/data/ios-studio/screenshots/"))
            r = self.run_helper("compare", "--project", str(proj), "--name", "Home", "--shot", str(FIX / "status.png"),
                                "--threshold", "0.5", env=env)
            self.assertEqual((r["data"]["verdict"], r["data"]["threshold"]), ("same", 0.5))
            r = self.run_helper("compare", "--project", str(proj), "--name", "Home", "--shot", str(FIX / "status.png"),
                                "--mask-top", "0", env=env)
            self.assertEqual((r["data"]["verdict"], r["data"]["masked_top"]), ("changed", 0))

    def test_demo_mode(self):
        with tempfile.TemporaryDirectory() as t:
            env = {"IOSSTUDIO_DEMO": "1", "XDG_RUNTIME_DIR": t}
            proj = str(Path.home() / "Developer" / "WeatherGlance")
            lst = self.run_helper("list", "--project", proj, env=env)["data"]["baselines"]
            self.assertEqual([b["name"] for b in lst], ["Home", "Today"])
            import demo_art
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": t}):
                shot = demo_art.phone_screen(Path(t) / "shot.png")
            home = self.run_helper("compare", "--project", proj, "--name", "Home", "--shot", str(shot), env=env)["data"]
            self.assertEqual(home["verdict"], "changed")
            self.assertEqual(len(home["regions"]), 3)
            self.assertTrue(Path(home["diff_path"]).is_file())
            today = self.run_helper("compare", "--project", proj, "--name", "Today", "--shot", str(shot), env=env)["data"]
            self.assertEqual((today["verdict"], today["diff_percent"]), ("same", 0.0))
            bad = self.run_helper("list", "--project", "/elsewhere/Nope", env=env)
            self.assertEqual(bad["error"]["code"], "not-found")
