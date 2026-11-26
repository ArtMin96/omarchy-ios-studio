"""Zed's machine-wide setup and Debug in editor: when the toolchain has lldb-dap (next to the real swift), setup
points dap.Swift.binary in Zed's user settings at iOS Studio's lldb-dap launcher (lldb_shadow.launcher_path()),
keeping a value of the user's own (migrating one setup wrote before: tests/helper/test_editor_debug_shadow.py).

Every test runs on a fake home folder, PATH and toolchain in a temp dir (Env), never the real ~/.config.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from iosstudio.core import jsonc
from iosstudio.features import editor_setup as E
from iosstudio.features import lldb_shadow


class ZedDap(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editor-setup-dap-"))
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.tc = self.tmp / "toolchain/usr/bin"
        for d in (self.bin, self.tc, self.home / ".local/share/zed/extensions/installed/swift"):
            d.mkdir(parents=True)
        self.exe(self.bin, "zeditor", "sourcekit-lsp", "xtool")
        self.exe(self.tc, "swift", "lldb", "lldb-dap")
        (self.bin / "swift").symlink_to(self.tc / "swift")     # only swift is on PATH, like the real toolchain
        self.zs = self.home / ".config/zed/settings.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def exe(d: Path, *names: str) -> None:
        for n in names:
            (d / n).write_text("#!/bin/sh\nexit 0\n")
            (d / n).chmod(0o755)

    def env(self) -> E.Env:
        return E.Env(home=self.home, config=self.home / ".config", data=self.home / ".local/share",
                     path=str(self.bin), state=self.home / ".local/state/ios-studio", swift_version=(6, 4),
                     probe_swift=False)

    def test_on_path_still_points_at_the_launcher(self):
        self.exe(self.bin, "lldb-dap")             # a plain lldb-dap on PATH can't evaluate Swift for an iPhone app
        acts = E.zed_machine_actions(self.env())
        self.assertEqual([a["kind"] for a in acts], ["write"])
        self.assertEqual(E.editor_info("zed", self.env())["state"], "needs-setup")

    def test_missing_adds_merge_action(self):
        self.zs.parent.mkdir(parents=True)
        self.zs.write_text('// Zed settings\n{\n  "theme": "Omazed",\n}\n')
        acts = E.zed_machine_actions(self.env())
        self.assertEqual([a["kind"] for a in acts], ["backup", "merge"])
        self.assertEqual(acts[1]["keys"], ["dap"])
        self.assertEqual(E.editor_info("zed", self.env())["state"], "needs-setup")
        E.apply_actions(acts, self.env())
        text = self.zs.read_text()
        self.assertTrue(text.startswith("// Zed settings"))
        v = jsonc.parse(text).value
        self.assertEqual(v["theme"], "Omazed")
        self.assertEqual(v["dap"]["Swift"]["binary"], str(lldb_shadow.launcher_path()))
        self.assertNotIn("lsp", v)                               # sourcekit-lsp is on PATH
        self.assertEqual(E.editor_info("zed", self.env())["state"], "ready")

    def test_new_file_with_both_paths(self):
        (self.bin / "sourcekit-lsp").unlink()
        self.exe(self.tc, "sourcekit-lsp")
        acts = E.zed_machine_actions(self.env())
        self.assertEqual([a["kind"] for a in acts], ["write"])
        self.assertIn("lldb-dap", acts[0]["detail"])
        E.apply_actions(acts, self.env())
        v = jsonc.parse(self.zs.read_text()).value
        self.assertEqual(v["dap"]["Swift"]["binary"], str(lldb_shadow.launcher_path()))
        self.assertEqual(v["lsp"]["sourcekit-lsp"]["binary"]["path"], str(self.tc / "sourcekit-lsp"))

    def test_existing_user_value_kept(self):
        self.zs.parent.mkdir(parents=True)
        mine = self.tmp / "opt/my"
        mine.mkdir(parents=True)
        self.exe(mine, "lldb-dap")                 # an lldb-dap of the user's own (another toolchain)
        self.zs.write_text('{"dap": {"Swift": {"binary": "%s"}}}\n' % (mine / "lldb-dap"))
        acts = E.zed_machine_actions(self.env())
        self.assertEqual([a["kind"] for a in acts], ["skip"])
        self.assertTrue(acts[0]["configured"])
        self.assertEqual(acts[0]["kept"][0]["yours"], str(mine / "lldb-dap"))
        self.assertEqual(E.editor_info("zed", self.env())["state"], "ready")

    def test_no_toolchain_lldb_dap(self):
        (self.tc / "lldb-dap").unlink()
        self.assertEqual(E.zed_machine_actions(self.env()), [])
        self.assertEqual(E.editor_info("zed", self.env())["state"], "ready")
        (self.bin / "swift").unlink()                            # no toolchain at all
        self.assertEqual(E.zed_machine_actions(self.env()), [])


if __name__ == "__main__":
    unittest.main()
