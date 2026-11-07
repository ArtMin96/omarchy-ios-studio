"""A temp folder with a fake swift-format in it, for helper/iosstudio/features/format.py tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.features import format as F
from support.fakes import FAKE_SWIFT_FORMAT


class TempHome(unittest.TestCase):
    """A temp folder, a fake swift-format in it, and the cache/state folders pointed into it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.fake = self.dir / "bin/swift-format"
        self.fake.parent.mkdir()
        self.fake.write_text(FAKE_SWIFT_FORMAT)
        self.fake.chmod(0o755)
        self.patches = [mock.patch.object(F, "DIFF_DIR", self.dir / "cache/format"),
                        mock.patch.object(F, "LINT_DIR", self.dir / "state/lint")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
