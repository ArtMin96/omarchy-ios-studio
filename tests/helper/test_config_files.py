"""Shared config-file editing (helper/iosstudio/features/config_files.py, used by editor setup and AI agents):
set / remove one member in JSON with comments byte for byte, the merge that only inserts, writes through a
symlink with the mode kept, commit's restore when verification fails, and the backups it keeps. Plus the
`agents` group end to end on a temp home (connect / disconnect by editing JSONC, TOML and crushrc files,
backups, a symlinked config) and editor setup writing through a symlinked settings file.

Everything runs in temp dirs, never the real ~/.config."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from iosstudio.core import jsonc
from iosstudio.core.output import HelperError
from iosstudio.features import config_files as C
from iosstudio.features import editor_setup as E
from support import HELPER_PY, ROOT, isolated_env

JSONC = """{
    // mine
    "theme": "dark", // keep
    "mcpServers": {
        "other": {"command": "x"},
    },
}
"""


class JsoncEdits(unittest.TestCase):
    def test_set_adds_member_keeping_the_rest(self):
        new = C.set_member(JSONC, ["mcpServers", "iosstudio"], {"command": "/a", "args": []})
        self.assertTrue(new.startswith('{\n    // mine\n    "theme": "dark", // keep\n'))
        self.assertEqual(jsonc.load(new)["mcpServers"]["iosstudio"], {"command": "/a", "args": []})
        self.assertEqual(jsonc.load(new)["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(C.remove_member(new, ["mcpServers", "iosstudio"]), JSONC)

    def test_set_creates_parents_and_empty_file(self):
        self.assertEqual(json.loads(C.set_member("", ["a", "b"], 1)), {"a": {"b": 1}})
        new = C.set_member('{"x": 1}', ["a", "b"], 1)
        self.assertEqual(jsonc.load(new), {"x": 1, "a": {"b": 1}})

    def test_set_replaces_value(self):
        new = C.set_member(JSONC, ["theme"], "light")
        self.assertEqual(new, JSONC.replace('"dark"', '"light"'))

    def test_remove_drops_emptied_parent(self):
        text = '{\n  "a": 1,\n  "s": {\n    "iosstudio": {}\n  }\n}\n'
        self.assertEqual(jsonc.load(C.remove_member(text, ["s", "iosstudio"])), {"a": 1})

    def test_tab_layout(self):
        text = '{\n\t"a": {\n\t\t"b": 1\n\t}\n}\n'
        new = C.set_member(text, ["a", "c"], {"d": 2})
        self.assertIn('\n\t\t"c": {\n\t\t\t"d": 2\n\t\t}', new)
        merged = C.merge_missing(text, {"a": {"e": [1]}})
        self.assertEqual(merged["status"], "merged")
        self.assertIn('\t\t"e": [\n\t\t\t1\n\t\t]', merged["text"])

    def test_merge_never_overwrites(self):
        r = C.merge_missing(JSONC, {"theme": "light", "new": True})
        self.assertEqual(r["added"], ["new"])
        self.assertEqual(r["kept"], [{"key": "theme", "yours": "dark", "ours": "light"}])
        self.assertEqual(jsonc.load(r["text"])["theme"], "dark")
        self.assertEqual(C.merge_missing("[1]", {"a": 1})["status"], "skip")


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="config-files-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_limited(self):
        self.assertEqual(C.read_limited(self.tmp / "nope", 10), (None, None))
        (self.tmp / "big").write_text("x" * 2048)
        self.assertEqual(C.read_limited(self.tmp / "big", 1024), (None, "is larger than 1 KB"))
        self.assertEqual(C.read_limited(self.tmp, 1024), (None, "isn't a file"))

    def test_write_through_symlink_keeps_mode(self):
        real = self.tmp / "dots/settings.json"
        real.parent.mkdir()
        real.write_text("{}")
        real.chmod(0o600)
        link = self.tmp / "settings.json"
        link.symlink_to(real)
        self.assertEqual(C.write_keeping_mode(link, '{"a": 1}'), real)
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), '{"a": 1}')
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)

    def test_commit_restores_when_verify_fails(self):
        f = self.tmp / "c.json"
        f.write_text("old")
        backups = self.tmp / "b"

        def bad(_text: str) -> None:
            raise ValueError("nope")
        with self.assertRaises(HelperError) as cm:
            C.commit(f, "old", "new", bad, lambda t: C.backup_into(backups, "x-", t))
        self.assertEqual(cm.exception.code, "config-write")
        self.assertEqual(f.read_text(), "old")
        # a new file that fails verification is removed again
        g = self.tmp / "g.json"
        with self.assertRaises(HelperError):
            C.commit(g, "", "new", bad, lambda t: C.backup_into(backups, "x-", t))
        self.assertFalse(g.exists())
        res = C.commit(f, "old", "new", lambda t: None, lambda t: C.backup_into(backups, "x-", t))
        self.assertTrue(res["changed"])
        self.assertEqual(Path(res["backup"]).read_text(), "old")
        self.assertEqual(C.commit(f, "new", "new", bad, lambda t: None), {"changed": False, "file": str(f)})

    def test_backup_keeps_newest(self):
        f = self.tmp / "a.toml"
        f.write_text("1")
        for _ in range(4):
            C.backup_into(self.tmp / "b", "p-", f, keep=2)
        self.assertEqual(len(list((self.tmp / "b").iterdir())), 2)
        self.assertIsNone(C.backup_into(self.tmp / "b", "p-", self.tmp / "missing"))


class AgentsCli(unittest.TestCase):
    """`agents connect / disconnect --via file` on a temp home: the real code path, no AI tool needed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agents-cli-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for tool in ("python3", "bash"):
            os.symlink(shutil.which(tool), bindir / tool)
        self.env = isolated_env(self.tmp, HOME=str(self.home), PATH=str(bindir), XDG_RUNTIME_DIR=str(self.tmp / "run"))
        self.env.pop("CODEX_HOME", None)
        self.config = self.tmp / "xdg_config_home"
        self.launcher = str(ROOT / "bin/iosstudio-mcp")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_agents(self, *args: str, env: dict | None = None) -> dict:
        r = subprocess.run([*HELPER_PY, "agents", *args], capture_output=True, text=True, env=env or self.env,
                           timeout=60)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_json_connect_and_disconnect_round_trip(self):
        f = self.home / ".claude.json"
        f.write_text(JSONC)
        dry = self.run_agents("connect", "--client", "claude", "--via", "file", "--dry-run")["data"]
        self.assertTrue(dry["changed"])
        self.assertIn('+        "iosstudio": {', dry["diff"])
        self.assertEqual(f.read_text(), JSONC)
        r = self.run_agents("connect", "--client", "claude", "--via", "file")["data"]
        self.assertTrue(r["connected"])
        self.assertEqual(Path(r["backup"]).read_text(), JSONC)
        self.assertEqual(jsonc.load(f.read_text())["mcpServers"]["iosstudio"]["command"], self.launcher)
        listed = {c["id"]: c for c in self.run_agents("list")["data"]["clients"]}
        self.assertTrue(listed["claude"]["current"])
        self.run_agents("disconnect", "--client", "claude")
        self.assertEqual(f.read_text(), JSONC)

    def test_codex_toml_and_crushrc(self):
        codex = self.home / ".codex/config.toml"
        codex.parent.mkdir()
        codex.write_text('[model]\nname = "o3"\n')
        self.run_agents("connect", "--client", "codex", "--demo")
        data = tomllib.loads(codex.read_text())
        self.assertEqual(data["mcp_servers"]["iosstudio"]["env"], {"IOSSTUDIO_DEMO": "1"})
        self.run_agents("disconnect", "--client", "codex")
        self.assertEqual(codex.read_text(), '[model]\nname = "o3"\n')
        crushrc = self.config / "crush/crushrc"
        crushrc.parent.mkdir(parents=True)
        crushrc.write_text("alias x=y\n")
        self.run_agents("connect", "--client", "crush")
        self.assertIn("mcp add iosstudio --type stdio --command", crushrc.read_text())
        self.run_agents("disconnect", "--client", "crush")
        self.assertEqual(crushrc.read_text(), "alias x=y\n")

    def test_symlinked_config_is_written_through(self):
        real = self.tmp / "dots/zed.json"
        real.parent.mkdir()
        real.write_text('{"theme": "One"}\n')
        link = self.config / "zed/settings.json"
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        self.run_agents("connect", "--client", "zed")
        self.assertTrue(link.is_symlink())
        self.assertIn("context_servers", jsonc.load(real.read_text()))

    def test_broken_config_is_left_alone(self):
        f = self.home / ".cursor/mcp.json"
        f.parent.mkdir()
        f.write_text('{"mcpServers": ')
        r = self.run_agents("connect", "--client", "cursor")
        self.assertEqual(r["error"]["code"], "config-unreadable")
        self.assertEqual(f.read_text(), '{"mcpServers": ')

    def test_demo_state_lives_in_the_demo_runtime_dir(self):
        env = {**self.env, "IOSSTUDIO_DEMO": "1", "IOSSTUDIO_RUNTIME_DIR": str(self.tmp / "rt")}
        self.run_agents("connect", "--client", "codex", env=env)
        self.assertEqual(json.loads((self.tmp / "rt/ios-studio-demo/agents.json").read_text()), ["claude", "codex"])


class EditorSymlink(unittest.TestCase):
    def test_editor_setup_writes_through_a_symlinked_settings_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="editor-link-"))
        try:
            home, tc, bindir = tmp / "home", tmp / "tc", tmp / "bin"
            for d in (home, tc, bindir):
                d.mkdir()
            (tc / "swift").write_text("#!/bin/sh\n")
            (tc / "sourcekit-lsp").write_text("#!/bin/sh\n")
            for f in ("swift", "sourcekit-lsp"):
                (tc / f).chmod(0o755)
            (bindir / "swift").symlink_to(tc / "swift")
            (bindir / "zeditor").symlink_to(tc / "swift")
            real = tmp / "dots/zed.json"
            real.parent.mkdir()
            real.write_text('{"theme": "One"}\n')
            link = home / ".config/zed/settings.json"
            link.parent.mkdir(parents=True)
            link.symlink_to(real)
            env = E.Env(home=home, config=home / ".config", data=home / ".local/share", path=str(bindir),
                        swift_version=(6, 4))
            r = E.machine_setup("zed", confirm=True, env=env, prefer=False)
            self.assertEqual([a["kind"] for a in r["actions"]], ["backup", "merge"])
            self.assertTrue(link.is_symlink())
            self.assertEqual(jsonc.load(real.read_text())["lsp"]["sourcekit-lsp"]["binary"]["path"],
                             str(tc / "sourcekit-lsp"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
