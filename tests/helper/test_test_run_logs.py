"""`build log --file` (helper/iosstudio/features/projects/commands.py cmd_build_log) also opens test-run logs
from $XDG_STATE_HOME/ios-studio/test-runs/ (features/testing/store.py), so the Log tab can show a unit-test run;
any other file, a non-.log file in test-runs or a missing log is refused.

    python -m pytest tests/helper/test_test_run_logs.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.core.output import HelperError
from iosstudio.features.projects import commands, store
from iosstudio.features.testing import store as test_store


class TestRunLogs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test-run-logs-")).resolve()
        state = self.tmp / "state"
        self.patches = [mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}),
                        mock.patch.object(store, "BUILD_LOGS", state / "ios-studio" / "build-logs")]
        for p in self.patches:
            p.start()
        self.runs = test_store.runs_dir()
        self.runs.mkdir(parents=True)
        store.BUILD_LOGS.mkdir(parents=True)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, path: Path) -> dict:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            commands.cmd_build_log({"file": str(path)})
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertTrue(out["ok"], out)
        return out["data"]

    def refused(self, path: Path) -> None:
        with self.assertRaises(HelperError) as cm:
            self.read(path)
        self.assertEqual(cm.exception.code, "not-found")

    def test_a_test_run_log_is_read(self):
        log = self.runs / "20261125-101010-123-CrashLab.log"
        log.write_text("# CrashLab: unit tests\nExecuted 5 tests, with 0 failures\n")
        d = self.read(log)
        self.assertEqual(d["file"], str(log))
        self.assertIn("Executed 5 tests", d["text"])
        self.assertFalse(d["truncated"])

    def test_a_build_log_is_still_read(self):
        log = store.BUILD_LOGS / "20261125-CrashLab-run.log"
        log.write_text("Build complete!\n")
        self.assertIn("Build complete!", self.read(log)["text"])

    def test_files_outside_the_two_folders_are_refused(self):
        outside = self.tmp / "secret.log"
        outside.write_text("no")
        self.refused(outside)
        self.refused(self.runs / ".." / ".." / ".." / "secret.log")          # .. out of test-runs
        sneaky = self.runs / "link.log"
        sneaky.symlink_to(outside)                                          # a link that points outside
        self.refused(sneaky)
        (self.runs / "last.json").write_text("{}")                          # test-runs: only .log files
        self.refused(self.runs / "last.json")
        self.refused(self.runs / "gone.log")


if __name__ == "__main__":
    unittest.main()
