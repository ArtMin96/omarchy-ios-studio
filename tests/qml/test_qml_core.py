"""Runs the QML core tests (tests/qml/tst_*.qml: Resource.qml, Model.js formatters) with Qt's qmltestrunner.

They only need QtQuick and QtTest (no Quickshell, no Omarchy shell), so they run headless on the offscreen
platform. Skipped where qmltestrunner isn't installed (Arch: qt6-declarative).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
RUNNER = shutil.which("qmltestrunner6") or shutil.which("qmltestrunner") or (
    "/usr/lib/qt6/bin/qmltestrunner" if Path("/usr/lib/qt6/bin/qmltestrunner").exists() else None
)


@pytest.mark.skipif(RUNNER is None, reason="qmltestrunner (qt6-declarative) not installed")
@pytest.mark.parametrize("test_file", sorted(p.name for p in HERE.glob("tst_*.qml")))
def test_qml(test_file: str) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run(
        [RUNNER, "-input", str(HERE / test_file)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
