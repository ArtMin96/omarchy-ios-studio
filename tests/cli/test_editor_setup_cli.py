"""Editor setup through helper/run.py iosstudio: demo mode, a compile-only build writing .bsp/xtool.json before `done`, new
projects marked and given VS Code files, the real CLI on a temp home."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from iosstudio.features import editor_setup as E
from support import ROOT


class Cli(unittest.TestCase):
    def run_helper(self, *args: str, demo: bool = True, env_extra: dict | None = None) -> dict:
        env = {**os.environ, **(env_extra or {})}
        if demo:
            env["IOSSTUDIO_DEMO"] = "1"
        else:
            env.pop("IOSSTUDIO_DEMO", None)
        out = subprocess.run([sys.executable, str(ROOT / "helper/run.py"), "iosstudio", *args], capture_output=True, text=True,
                             env=env, timeout=30)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_demo_machine_status_and_setup(self):
        d = self.run_helper("editor", "status")["data"]
        self.assertEqual({e["id"]: e["state"] for e in d["editors"]},
                         {"vscode": "not-installed", "zed": "ready", "neovim": "needs-setup"})
        self.assertNotIn("_plugin", json.dumps(d))
        r = self.run_helper("editor", "setup", "--editor", "neovim")["data"]
        self.assertTrue(r["dryRun"])
        self.assertEqual(r["actions"][0]["kind"], "write")
        r = self.run_helper("editor", "setup", "--editor", "neovim", "--confirm")["data"]
        self.assertTrue(r["applied"])
        self.assertEqual(r["state"], "ready")

    def test_demo_project_status_and_fix(self):
        d = self.run_helper("editor", "status", "--project", "/home/u/Developer/HabitTracker", "--fix")["data"]
        self.assertFalse(d["ready"])
        self.assertEqual(d["hint"], "Build once to enable completion")
        self.assertTrue(d["fix"]["dryRun"])
        info = self.run_helper("projects", "info", "--path", os.path.expanduser("~/Developer/WeatherGlance"))
        self.assertTrue(info["data"]["editor"]["ready"])

    def test_successful_build_writes_bsp_before_done(self):
        tmp = Path(tempfile.mkdtemp(prefix="editor-build-"))
        try:
            xtool = tmp / "xtool"
            xtool.write_text('#!/usr/bin/env python3\nprint("Build complete! (0.1 secs)")\n')
            xtool.chmod(0o755)
            # A fake home, and the fake xtool on PATH: editor status calls .bsp/xtool.json stale when no xtool
            # is on PATH, which is the case on a machine without the toolchain (CI).
            env = {"HOME": str(tmp), "IOS_STUDIO_XTOOL": str(xtool),
                   "PATH": f"{tmp}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"}
            for var in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
                env[var] = str(tmp / var.lower())
            (tmp / "xdg_config_home/swiftpm/swift-sdks/darwin.artifactbundle").mkdir(parents=True)
            proj = tmp / "App"
            (proj / "Sources/App").mkdir(parents=True)
            (proj / "Package.swift").write_text('// swift-tools-version: 6.0\nlet package = Package(name: "App")\n')
            (proj / "xtool.yml").write_text("version: 1\nbundleID: com.example.App\n")
            full = {**os.environ, **env}
            full.pop("IOSSTUDIO_DEMO", None)
            r = subprocess.run([sys.executable, str(ROOT / "helper/run.py"), "iosstudio", "build", "compile", "--path", str(proj)],
                               capture_output=True, text=True, env=full, timeout=60, stdin=subprocess.DEVNULL)
            done = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertTrue(done["ok"], done)
            self.assertEqual((proj / E.BSP_REL).read_text(), E.BSP_TEXT)   # there when `done` was printed
            d = self.run_helper("editor", "status", "--project", str(proj), demo=False, env_extra=env)["data"]
            self.assertTrue(d["ready"])
            self.assertTrue(d["auto"]["wrote"][0]["path"].endswith(E.BSP_REL))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_new_project_is_marked_and_gets_vscode_files(self):
        tmp = Path(tempfile.mkdtemp(prefix="editor-new-"))
        try:
            env = {"HOME": str(tmp), "PATH": "/usr/bin:/bin"}
            for var in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
                env[var] = str(tmp / var.lower())
            sf = tmp / "xdg_config_home/ios-studio/settings.json"
            sf.parent.mkdir(parents=True)
            sf.write_text(json.dumps({"editorSetup": "vscode"}))
            d = self.run_helper("projects", "new", "--name", "Glance", "--dir", str(tmp / "dev"), "--git", "0",
                                demo=False, env_extra=env)
            self.assertTrue(d["ok"], d)
            proj = tmp / "dev/Glance"
            self.assertTrue((proj / E.BSP_REL).is_file())
            self.assertEqual(json.loads((proj / E.VSCODE_EXTENSIONS).read_text())["recommendations"], [E.VSCODE_EXT])
            meta = json.loads((tmp / "xdg_state_home/ios-studio/projects.json").read_text())["meta"]
            self.assertEqual(meta[str(proj.resolve())]["createdFrom"], "swiftui-app")
            s = self.run_helper("editor", "status", "--project", str(proj), demo=False, env_extra=env)["data"]
            self.assertTrue(s["created"])
            self.assertEqual(s["pending"], [])          # (not ready only because this PATH has no xtool)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_cli_on_temp_home(self):
        tmp = Path(tempfile.mkdtemp(prefix="editor-cli-"))
        try:
            proj = tmp / "App"
            proj.mkdir()
            (proj / "Package.swift").write_text("// swift-tools-version: 6.0\n")
            (proj / "xtool.yml").write_text("version: 1\n")
            env = {"HOME": str(tmp), "XDG_CONFIG_HOME": str(tmp / "c"), "XDG_DATA_HOME": str(tmp / "d"),
                   "XDG_STATE_HOME": str(tmp / "s"), "XDG_CACHE_HOME": str(tmp / "k")}
            d = self.run_helper("editor", "status", "--project", str(proj), demo=False, env_extra=env)
            self.assertTrue(d["ok"], d)
            self.assertIn(d["data"]["hint"], ("Build once to enable completion", None))
            d = self.run_helper("editor", "setup", "--editor", "nope", demo=False, env_extra=env)
            self.assertFalse(d["ok"])
            self.assertEqual(d["error"]["code"], "usage")
            d = self.run_helper("editor", "status", "--project", str(tmp / "missing"), demo=False, env_extra=env)
            self.assertEqual(d["error"]["code"], "not-found")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
