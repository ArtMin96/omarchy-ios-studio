"""Helper state files (helper/iosstudio/features/projects/: store.py, model.py): build-log pruning, projects.json locking, template ids and
provisioning-profile dates. Stdlib only:
    python -m pytest tests/helper/test_helper_state.py
"""
from __future__ import annotations

import multiprocessing
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from iosstudio.core import appdirs
from iosstudio.core.output import HelperError
from iosstudio.features.projects import commands, model, store
from support import ROOT

_TMP = tempfile.mkdtemp(prefix="ios-studio-tests-")   # XDG dirs are isolated by tests/conftest.py


class _Sandbox(unittest.TestCase):
    """Every test gets its own state folder."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="state-", dir=_TMP))
        patches = {
            (appdirs, "STATE_DIR"): self.dir,
            (store, "PROJECTS_FILE"): self.dir / "projects.json",
            (store, "BUILDS_FILE"): self.dir / "builds.json",
            (store, "RENEWALS_FILE"): self.dir / "renewals.json",
            (store, "BUILD_LOGS"): self.dir / "build-logs",
        }
        for (mod, name), val in patches.items():
            p = mock.patch.object(mod, name, val)
            p.start()
            self.addCleanup(p.stop)


class BuildLogPruning(_Sandbox):
    def _log(self, name: str, age: float) -> Path:
        store.BUILD_LOGS.mkdir(parents=True, exist_ok=True)
        f = store.BUILD_LOGS / name
        f.write_text("log\n")
        t = time.time() - age
        os.utime(f, (t, t))
        return f

    def test_logs_dropped_from_history_are_deleted(self):
        with mock.patch.object(store, "MAX_HISTORY", 3):
            files = []
            for i in range(5):      # each build writes its log, then its history entry
                files.append(self._log(f"2026010{i}-App-compile.log", age=86400))
                store.append_history({"path": "/p", "logFile": str(files[-1]), "time": 0})
        self.assertEqual([f.exists() for f in files], [False, False, True, True, True])
        self.assertEqual(len(store.load_history()), 3)

    def test_a_running_builds_log_is_kept(self):
        fresh = self._log("20260109-Other-run.log", age=10)      # written by a build still running
        stale = self._log("20250101-Gone-run.log", age=3 * 86400)
        store.append_history({"path": "/p", "logFile": str(self._log("x.log", 0)), "time": 0})
        self.assertTrue(fresh.exists())
        self.assertFalse(stale.exists())


def _add_many(state_dir: str, start: int, n: int) -> None:
    appdirs.STATE_DIR = Path(state_dir)
    store.PROJECTS_FILE = Path(state_dir) / "projects.json"
    for i in range(start, start + n):
        with store.edit_state() as st:
            st["added"].append(f"/p/{i}")


class ProjectsJsonLocking(_Sandbox):
    def test_concurrent_edits_keep_every_change(self):
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_add_many, args=(str(self.dir), k * 100, 25)) for k in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        self.assertEqual(len(store.load_state()["added"]), 100)

    def test_failed_edit_is_not_saved(self):
        with self.assertRaises(RuntimeError):
            with store.edit_state() as st:
                st["added"].append("/half")
                raise RuntimeError("boom")
        self.assertEqual(store.load_state()["added"], [])


class TemplateIds(_Sandbox):
    def test_only_listed_templates(self):
        self.assertEqual(model.template_dir("swiftui-app"), model.TEMPLATES / "swiftui-app")
        for bad in ("../../x", "../templates/swiftui-app", "/etc", "swiftui-app/../tabs-app", "", None, True):
            with self.assertRaises(HelperError, msg=repr(bad)) as cm:
                model.template_dir(bad)
            self.assertEqual(cm.exception.code, "bad-template")

    def test_new_rejects_traversal_before_writing(self):
        dest = self.dir / "apps"
        with self.assertRaises(HelperError) as cm:
            commands.cmd_new({"name": "Evil", "template": "../../x", "dir": str(dest)})
        self.assertEqual(cm.exception.code, "bad-template")
        self.assertFalse(dest.exists())

    def test_listing_hides_the_folder(self):
        for t in model.templates():
            self.assertIn("id", t)
        out = subprocess.run([sys.executable, "-c", "import json,sys; from iosstudio.features import projects; projects.GROUPS[\"projects\"][\"templates\"]({})"],
                             cwd=ROOT / "helper", capture_output=True, text=True, timeout=30,
                             env={**os.environ, "PYTHONPATH": str(ROOT / "helper")})
        self.assertNotIn("_dir", out.stdout)
        self.assertIn("swiftui-app", out.stdout)


def _profile(expires: datetime) -> bytes:
    pl = plistlib.dumps({"ExpirationDate": expires, "CreationDate": expires, "Name": "p", "UUID": "u",
                         "TeamIdentifier": ["T"], "Entitlements": {}})
    return b"junk" + pl + b"junk"


class ProfileDates(unittest.TestCase):
    def test_expiry_is_utc_in_any_timezone(self):
        when = datetime(2026, 3, 1, 12, 0, 0)          # plists store UTC
        want = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        code = ("import sys; sys.path.insert(0, %r); import datetime; from iosstudio.features.projects import store; "
                "print(store.profile_from_bytes(%r)['expires'])") % (str(ROOT / "helper"), _profile(when))
        for tz in ("America/Los_Angeles", "Asia/Tokyo", "UTC"):
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                                 env={**os.environ, "TZ": tz})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(float(out.stdout.strip()), want, tz)


if __name__ == "__main__":
    unittest.main()
