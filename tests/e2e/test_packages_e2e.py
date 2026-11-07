"""Swift packages with the real swift (skipped without it): every remove fixture in tests/fixtures/packages/remove/
(before and after) is a valid manifest for `swift package dump-package`; with network too, an end-to-end run adds
swift-collections to a throwaway project, links one product, resolves and removes it."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

import package_manifest as M
from support import FIXTURES, HELPER
from support.projects import package_project as make_project

FIX = FIXTURES / "packages"
SWIFT = shutil.which("swift")


def _network() -> bool:
    try:
        socket.create_connection(("github.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


@unittest.skipUnless(SWIFT, "needs swift")
class RealManifests(unittest.TestCase):
    cases = json.loads((FIX / "remove/cases.json").read_text())

    def test_fixtures_are_valid_manifests(self):
        names = {c.get("source", n) for n, c in self.cases.items()} | {n + ".expected" for n, c in self.cases.items()
                                                                      if not c.get("problems")}
        with tempfile.TemporaryDirectory() as tmp:
            for name in sorted(names):
                with self.subTest(name):
                    d = Path(tmp) / name.replace(".", "-")
                    d.mkdir()
                    shutil.copy(FIX / "remove" / f"{name}.swift", d / "Package.swift")
                    r = subprocess.run([SWIFT, "package", "--package-path", str(d), "--scratch-path",
                                        str(Path(tmp) / "scratch" / name), "dump-package"], capture_output=True,
                                       text=True, timeout=120)
                    self.assertEqual(r.returncode, 0, r.stderr[-500:])


@pytest.mark.network
@unittest.skipUnless(SWIFT, "needs swift")
class EndToEnd(unittest.TestCase):
    def test_add_link_resolve_remove(self):
        if not _network():
            self.skipTest("needs network (github.com)")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(os.path.realpath(tmp))
            proj = make_project(d / "E2E")
            env = {**os.environ, "XDG_CACHE_HOME": str(d / "cache"), "XDG_STATE_HOME": str(d / "state")}
            env.pop("IOSSTUDIO_DEMO", None)
            env.pop("IOSSTUDIO_SWIFT", None)

            def cli(*args):
                r = subprocess.run([str(HELPER), "packages", *args, "--project", str(proj)], capture_output=True,
                                   text=True, env=env, timeout=600, stdin=subprocess.DEVNULL)
                last = json.loads(r.stdout.strip().splitlines()[-1])
                return {"ok": last["ok"], "data": last.get("data"), "error": last.get("error")}

            res = cli("add", "--url", "https://github.com/apple/swift-collections", "--version", "1.1.0",
                      "--product", "DequeModule")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["data"]["product_added"], "DequeModule")
            self.assertIn("Collections", res["data"]["products"])
            pins = M.parse_resolved((proj / "Package.resolved").read_text())
            self.assertIn("swift-collections", pins)
            res = cli("list")
            (pkg,) = res["data"]["packages"]
            self.assertEqual(pkg["products_used"], ["DequeModule"])
            self.assertEqual(pkg["resolved"]["version"], pins["swift-collections"]["version"])
            res = cli("resolve")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["data"]["changes"], [])
            res = cli("remove", "--package", "swift-collections", "--confirm")
            self.assertTrue(res["data"]["removed"], res)
            self.assertNotIn("swift-collections", (proj / "Package.swift").read_text())
