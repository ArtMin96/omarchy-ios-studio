"""The projects feature package (helper/iosstudio/features/projects/): its command table, no old private aliases,
and that one-shot commands don't load the build machinery."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from support import ROOT


class Package(unittest.TestCase):
    def test_groups(self):
        from iosstudio.features import projects
        self.assertEqual(set(projects.GROUPS), {"projects", "build"})
        for group in projects.GROUPS.values():
            for action, handler in group.items():
                self.assertTrue(callable(handler), action)
        for action in ("run", "compile", "ipa", "install", "history", "log", "clean"):
            self.assertIn(action, projects.GROUPS["build"])

    def test_no_private_aliases(self):
        from iosstudio.features import projects
        for name in ("_state", "_history", "_record_signing", "_scan", "_project_info", "_is_project", "_auth_ok"):
            self.assertFalse(hasattr(projects, name), name)

    def test_one_shot_commands_import_little(self):
        code = ("import sys; from iosstudio.features import projects; projects.GROUPS['projects']['list']; "
                "print(' '.join(sorted(m for m in sys.modules if m.startswith('iosstudio.'))))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                             env={**os.environ, "PYTHONPATH": str(ROOT / "helper")})
        self.assertEqual(out.returncode, 0, out.stderr)
        loaded = set(out.stdout.split())
        for heavy in ("iosstudio.features.projects.runner", "iosstudio.features.projects.build",
                      "iosstudio.features.projects.renew", "iosstudio.features.diagnostics"):
            self.assertNotIn(heavy, loaded)
