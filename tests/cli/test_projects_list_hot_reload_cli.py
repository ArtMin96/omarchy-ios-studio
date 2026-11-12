"""The Projects list after hot reload ran on a folder that isn't on it (a copy of an app outside the project
folders, never added): the session leaves state (its runtime snapshot, a builds.json entry), but no project.
Real mode with the fakes of tests/support/hot_reload_session.py; the project folders point at a temp dir.

    .venv/bin/pytest -q tests/cli/test_projects_list_hot_reload_cli.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from support import HELPER_PY
from support.hot_reload_session import SessionWorld


def helper(env: dict, *args: str) -> dict:
    r = subprocess.run(HELPER_PY + list(args), env=env, capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    lines = [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]
    assert lines, r.stdout + r.stderr
    return lines[-1]


class ProjectsListAfterHotReload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="hrlist-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.w = SessionWorld(self.tmp / "elsewhere")          # the copy hot reload runs on: outside the roots
        self.env = self.w.env
        self.roots = self.tmp / "roots"
        self.listed = self.roots / "App"                       # the registered app, same name ("App")
        self.listed.mkdir(parents=True)
        for f in ("Package.swift", "xtool.yml"):
            shutil.copy(self.w.proj / f, self.listed / f)
        cfg = Path(self.env["XDG_CONFIG_HOME"]) / "ios-studio"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "settings.json").write_text(json.dumps({"projectRoots": [str(self.roots)]}))

    def paths(self) -> list[str]:
        return [p["path"] for p in helper(self.env, "projects", "list")["data"]["projects"]]

    def run_session(self) -> None:
        with self.w.deviced():
            proc = subprocess.Popen(HELPER_PY + ["hotreload", "run", "--project", str(self.w.proj)], env=self.env,
                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(10)))
            deadline, state = time.monotonic() + 30, None
            while time.monotonic() < deadline and state != "watching":
                snap = helper(self.env, "hotreload", "status", "--project", str(self.w.proj))["data"]["session"]
                state = (snap or {}).get("state")
                time.sleep(0.2)
            self.assertEqual(state, "watching")
            self.assertTrue(helper(self.env, "hotreload", "stop", "--project", str(self.w.proj))["data"]["stopped"])
            proc.wait(20)

    def test_a_session_on_an_unlisted_folder_adds_no_project(self):
        self.run_session()
        copy = str(self.w.proj)
        # what the session left behind: its snapshot and a build history entry for the copy
        status = helper(self.env, "hotreload", "status")["data"]
        self.assertIn(copy, [s["path"] for s in status["sessions"]])
        hist = helper(self.env, "build", "history", "--limit", "1")["data"]["builds"][0]
        self.assertEqual((hist["path"], hist["trigger"]), (copy, "hot-reload"))
        # ... and still only the app in the project folders is listed
        self.assertEqual(self.paths(), [str(self.listed)])
        self.assertEqual(list(status["projects"]), [str(self.listed)])

    def test_two_apps_with_one_name_differ_by_folder_and_the_copy_can_go(self):
        self.run_session()
        copy = str(self.w.proj)
        added = helper(self.env, "projects", "add", "--path", copy)
        self.assertTrue(added["ok"], added)
        items = helper(self.env, "projects", "list")["data"]["projects"]
        self.assertEqual(sorted(p["path"] for p in items), sorted([copy, str(self.listed)]))
        self.assertEqual({p["name"] for p in items}, {"App"})
        self.assertEqual(len({p["displayPath"] for p in items}), 2)       # what the cards tell apart
        self.assertEqual(set(helper(self.env, "hotreload", "status")["data"]["projects"]), {copy, str(self.listed)})

        removed = helper(self.env, "projects", "remove", "--path", copy)
        self.assertTrue(removed["ok"], removed)
        self.assertEqual(self.paths(), [str(self.listed)])
        self.assertEqual(list(helper(self.env, "hotreload", "status")["data"]["projects"]), [str(self.listed)])


if __name__ == "__main__":
    unittest.main()
