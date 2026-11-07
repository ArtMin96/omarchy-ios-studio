"""The project check through helper/main.py: `projects check`, not-a-project, demo mode, and a compile-only build
through a fake xtool with the pre-build warnings first."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import FIXTURES, ROOT, isolated_env, write_exe

FIX = FIXTURES / "project-check"

FAKE_XTOOL = """\
#!/usr/bin/env python3
import sys
print("fake xtool", " ".join(sys.argv[1:]))
print("Building for debugging...")
print("Build complete! (0.1 secs)")
"""


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-cli-"))
        self.env = isolated_env(self.tmp)
        self.env["IOS_STUDIO_XTOOL"] = str(write_exe(self.tmp / "xtool", FAKE_XTOOL))

    def helper(self, *args, env=None) -> list[dict]:
        r = subprocess.run([sys.executable, str(ROOT / "helper" / "main.py"), *args], env=env or self.env,
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]

    def test_projects_check(self):
        [out] = self.helper("projects", "check", "--path", str(FIX / "xtool-keys"), "--max", "2")
        d = out["data"]
        self.assertEqual(set(d), {"path", "issues", "total", "shown", "truncated", "counts", "elapsedMs",
                                  "keysSource", "checks"})
        self.assertEqual((d["shown"], d["truncated"]), (2, True))
        self.assertGreater(d["total"], 2)
        self.assertEqual(d["counts"]["error"], 0)

    def test_not_a_project(self):
        [out] = self.helper("projects", "check", "--path", str(self.tmp))
        self.assertEqual(out["error"]["code"], "not-a-project")

    def test_demo(self):
        env = dict(self.env, IOSSTUDIO_DEMO="1")
        [out] = self.helper("projects", "check", "--path", "/x", env=env)
        msgs = " ".join(i["message"] for i in out["data"]["issues"])
        self.assertIn("did you mean 'bundleID'", msgs)
        self.assertIn("NSCameraUsageDescription", msgs)

    def test_prebuild_warnings_come_first_and_never_fail_the_build(self):
        proj = self.tmp / "App"
        proj.mkdir()
        (proj / "Package.swift").write_text('// swift-tools-version: 6.0\nlet package = Package(name: "App")\n')
        (proj / "xtool.yml").write_text("version: 1\nbundleID: com.example.App\nbundleId: x\n")
        (proj / "Sources/App").mkdir(parents=True)
        (proj / "Sources/App/Cam.swift").write_text("import AVFoundation\nlet s = AVCaptureSession()\n")
        ev = self.helper("build", "compile", "--path", str(proj))
        done = ev[-1]
        self.assertTrue(done["ok"], done)
        kinds = [e.get("event") for e in ev]
        first_diag, first_log = kinds.index("diagnostic"), kinds.index("log")
        self.assertLess(first_diag, first_log)
        diags = [e for e in ev if e.get("event") == "diagnostic"]
        self.assertEqual({d["check"] for d in diags}, {"xtool-keys", "usage-descriptions"})
        self.assertTrue(all(d["severity"] == "warning" and d["category"] == "config" for d in diags))
        cam = next(d for d in diags if d["check"] == "usage-descriptions")
        self.assertEqual(cam["file"], str(proj / "Sources/App/Cam.swift"))   # absolute, like compiler ones
        self.assertEqual(done["data"]["errors"], 0)
        hist = self.helper("build", "history")[-1]["data"]["builds"][0]
        self.assertEqual(hist["result"], "ok")
        self.assertTrue(any(d.get("check") == "xtool-keys" for d in hist["diagnostics"]))
