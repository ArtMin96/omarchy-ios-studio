"""The helper as one package (helper/iosstudio): no sys.path hacks inside it, run.py starts it under `python -I`,
the daemon's feature loader finds every feature folder's demo side, and device/client.py stays loadable by file path."""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import ROOT, isolated_env

PKG = ROOT / "helper" / "iosstudio"


class Layout(unittest.TestCase):
    def test_no_sys_path_edits_in_the_package(self):
        offenders = []
        for path in PKG.rglob("*.py"):
            if path.name == "lldb_driver.py":   # runs inside lldb's own Python and adds lldb's module folder
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Attribute) and node.attr == "path" and getattr(node.value, "id", "") == "sys":
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_run_py_starts_the_helper_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, "-I", str(ROOT / "helper" / "run.py"), "iosstudio", "nope", "x"],
                               capture_output=True, text=True, timeout=30, env=isolated_env(Path(tmp)))
        self.assertIn('"code":"usage"', r.stdout)

    def test_feature_folders_load(self):
        from iosstudio.device import kit
        reg = kit.load_features(kit.FeatureRegistry(log=lambda _msg: None), "demo")
        self.assertEqual(reg.failed, {})
        self.assertIn("apps", reg.loaded)
        folders = PKG / "device" / "features"
        real = {p.parent.name for p in folders.glob("*/real.py")}
        self.assertEqual(set(reg.loaded), real)   # every feature has both sides
        self.assertEqual(list(folders.glob("*.py")), [folders / "__init__.py"])   # features live in folders only

    def test_device_client_loads_by_path(self):
        spec = importlib.util.spec_from_file_location("layout_device_client", PKG / "device" / "client.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.deviced_path(), ROOT / "bin" / "iosstudio-deviced")


if __name__ == "__main__":
    unittest.main()
