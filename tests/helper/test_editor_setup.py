"""Editor setup (helper/iosstudio/features/editor_setup.py): the JSONC merge (new file, extra keys kept byte for byte, a
conflicting value kept and reported, comments and trailing commas, invalid JSON skipped, never overwriting),
the build server file, the machine-wide part per editor (Zed and VS Code extension detection, LazyVim extra
with a backup, the plugin file when there's no extra, non-LazyVim left alone, sourcekit-lsp off PATH), the
preferred editor in settings.json and the automatic per-project part (the CLI and demo mode:
tests/cli/test_editor_setup_cli.py).

Every test runs on a fake home folder and PATH in a temp dir (Env), never the real ~/.config.
Fixtures: tests/fixtures/editor-setup/.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from iosstudio.core import jsonc
from iosstudio.features import editor_setup as E
from support import ROOT

FIX = ROOT / "tests/fixtures/editor-setup"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editor-setup-"))
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.bin.mkdir(parents=True)
        (self.home / ".config/swiftpm/swift-sdks/darwin.artifactbundle").mkdir(parents=True)   # xtool's iOS SDK
        (self.home / ".local/share").mkdir(parents=True)
        self.project = self.tmp / "Glance"
        (self.project / "Sources/Glance").mkdir(parents=True)
        (self.project / "Package.swift").write_text("// swift-tools-version: 6.0\n")
        (self.project / "xtool.yml").write_text("version: 1\nbundleID: com.example.Glance\n")
        self.tools("sourcekit-lsp", "xtool", "swift")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tools(self, *names: str) -> None:
        for n in names:
            f = self.bin / n
            f.write_text("#!/bin/sh\nexit 0\n")
            f.chmod(0o755)

    def env(self, swift=(6, 4)) -> E.Env:
        return E.Env(home=self.home, config=self.home / ".config", data=self.home / ".local/share",
                     path=str(self.bin), state=self.home / ".local/state/ios-studio", swift_version=swift,
                     probe_swift=False)


# ================================================================ JSONC merge

class MergeText(unittest.TestCase):
    WANT = {"swift.autoGenerateLaunchConfigurations": False, "files.watcherExclude": {"**/.build/**": True}}

    def parsed(self, text: str):
        return jsonc.parse(text).value

    def test_empty_file_is_written_fresh(self):
        r = E.merge_text("", self.WANT)
        self.assertEqual(r["status"], "merged")
        self.assertEqual(json.loads(r["text"]), self.WANT)

    def test_extra_keys_kept_byte_for_byte(self):
        text = '{\n  "editor.fontSize": 13,\n  "a": [1, 2]\n}\n'
        r = E.merge_text(text, self.WANT)
        self.assertEqual(r["status"], "merged")
        self.assertTrue(r["text"].startswith('{\n  "editor.fontSize": 13,\n  "a": [1, 2],\n'))
        v = json.loads(r["text"])
        self.assertEqual(v["editor.fontSize"], 13)
        self.assertEqual(v["a"], [1, 2])
        self.assertEqual(v["files.watcherExclude"], {"**/.build/**": True})
        self.assertIn("swift.autoGenerateLaunchConfigurations", r["added"])

    def test_conflicting_value_is_kept_and_reported(self):
        text = (FIX / "settings-conflict.json").read_text()
        r = E.merge_text(text, self.WANT)
        self.assertEqual(r["status"], "merged")
        self.assertTrue(json.loads(r["text"])["swift.autoGenerateLaunchConfigurations"])
        self.assertEqual(r["kept"], [{"key": "swift.autoGenerateLaunchConfigurations", "yours": True, "ours": False}])

    def test_jsonc_comments_and_trailing_commas(self):
        text = (FIX / "settings-jsonc.json").read_text()
        r = E.merge_text(text, self.WANT)
        self.assertEqual(r["status"], "merged")
        out = r["text"]
        for line in text.splitlines():       # every original line survives (only insertions)
            if line.strip() not in ("},", '"**/node_modules/**": true,'):
                self.assertIn(line, out)
        self.assertIn("// Team settings: keep these", out)
        self.assertIn("/* block", out)
        v = self.parsed(out)
        self.assertEqual(v["files.watcherExclude"], {"**/node_modules/**": True, "**/.build/**": True})
        self.assertEqual(v["url"], "https://example.com/a//b")
        self.assertEqual(v["editor.tabSize"], 4)
        self.assertIn("files.watcherExclude › **/.build/**", r["added"])

    def test_vscode_extensions_template_with_comments(self):
        text = (FIX / "vscode-extensions-template.json").read_text()
        r = E.merge_text(text, E.VSCODE_PROJECT_EXTENSIONS)
        self.assertEqual(r["status"], "merged")
        v = self.parsed(r["text"])
        self.assertEqual(v["recommendations"], [E.VSCODE_EXT])
        self.assertEqual(v["unwantedRecommendations"], [])
        self.assertIn("// Extension identifier format", r["text"])

    def test_list_append_keeps_existing_items(self):
        for text in ('{"recommendations": ["a.b",\n "c.d"]}', '{\n  "recommendations": [\n    "a.b"\n  ]\n}',
                     '{"recommendations": []}', '{"recommendations": ["a.b",],}'):
            r = E.merge_text(text, E.VSCODE_PROJECT_EXTENSIONS)
            self.assertEqual(r["status"], "merged", text)
            self.assertEqual(self.parsed(r["text"])["recommendations"][-1], E.VSCODE_EXT, text)

    def test_inline_object_stays_inline(self):
        r = E.merge_text('{"a": 1}', self.WANT)
        self.assertNotIn("\n", r["text"])
        self.assertEqual(json.loads(r["text"])["a"], 1)

    def test_already_there_is_unchanged(self):
        r = E.merge_text(json.dumps(self.WANT), self.WANT)
        self.assertEqual(r["status"], "unchanged")

    def test_invalid_json_is_skipped(self):
        r = E.merge_text((FIX / "settings-invalid.json").read_text(), self.WANT)
        self.assertEqual(r["status"], "skip")
        self.assertIn("isn't valid JSON", r["reason"])
        self.assertRegex(r["reason"], r"\(line \d+\)")
        r = E.merge_text('["not", "an object"]', self.WANT)
        self.assertEqual(r["status"], "skip")
        r = E.merge_text('{"a": 1 /* never ends', self.WANT)
        self.assertEqual(r["status"], "skip")

    def test_type_mismatch_is_a_conflict(self):
        r = E.merge_text('{"files.watcherExclude": "nope"}', self.WANT)
        self.assertEqual(r["kept"][0]["key"], "files.watcherExclude")
        self.assertEqual(self.parsed(r["text"])["files.watcherExclude"], "nope")


class JsonAction(Base):
    def test_write_then_never_overwrite(self):
        acts = E.vscode_project_actions(self.project)
        self.assertEqual([a["kind"] for a in acts], ["write", "write"])
        E._apply(acts, self.env())
        path = self.project / E.VSCODE_SETTINGS
        self.assertEqual(json.loads(path.read_text()), E.VSCODE_PROJECT_SETTINGS)
        path.write_text('{\n  // mine\n  "swift.autoGenerateLaunchConfigurations": true,\n}\n')
        before = path.read_text()
        acts = E.vscode_project_actions(self.project)
        self.assertEqual(acts[0]["kind"], "merge")
        self.assertEqual(acts[0]["kept"][0]["yours"], True)
        E._apply(acts, self.env())
        after = path.read_text()
        self.assertIn("// mine", after)
        self.assertIn('"swift.autoGenerateLaunchConfigurations": true', after)
        self.assertTrue(after.startswith(before.split("}")[0].rstrip().rstrip(",")))
        again = E.vscode_project_actions(self.project)
        self.assertEqual([a["kind"] for a in again], ["skip", "skip"])
        self.assertTrue(all(a.get("configured") for a in again))

    def test_invalid_file_left_alone(self):
        path = self.project / E.VSCODE_SETTINGS
        path.parent.mkdir()
        shutil.copy(FIX / "settings-invalid.json", path)
        before = path.read_text()
        act = E.vscode_project_actions(self.project)[0]
        self.assertEqual(act["kind"], "skip")
        self.assertIn("left alone", act["detail"])
        E._apply([act], self.env())
        self.assertEqual(path.read_text(), before)

    def test_unwanted_recommendation_is_respected(self):
        path = self.project / E.VSCODE_EXTENSIONS
        path.parent.mkdir()
        path.write_text(json.dumps({"unwantedRecommendations": [E.VSCODE_EXT]}))
        act = E.vscode_project_actions(self.project)[1]
        self.assertEqual(act["kind"], "skip")
        self.assertIn("unwanted list", act["detail"])

    def test_write_skipped_when_file_appears_meanwhile(self):
        acts = E.vscode_project_actions(self.project)
        (self.project / ".vscode").mkdir()
        (self.project / E.VSCODE_SETTINGS).write_text("{}")
        E._apply(acts, self.env())
        self.assertEqual((self.project / E.VSCODE_SETTINGS).read_text(), "{}")
        self.assertEqual(acts[0]["kind"], "skip")


# ================================================================ build server file

class Bsp(Base):
    def test_missing_is_written_like_xtool(self):
        act = E.bsp_action(self.project, self.env())
        self.assertEqual(act["kind"], "write")
        E._apply([act], self.env())
        self.assertEqual((self.project / E.BSP_REL).read_text(), E.BSP_TEXT)
        st = E.bsp_status(self.project, self.env())
        self.assertTrue(st["present"])
        self.assertFalse(st["stale"])
        self.assertEqual(E.bsp_action(self.project, self.env())["kind"], "skip")

    def test_not_xtools_is_stale_and_kept(self):
        (self.project / ".bsp").mkdir()
        (self.project / E.BSP_REL).write_text('{"argv": ["/usr/bin/other-server"]}')
        st = E.bsp_status(self.project, self.env())
        self.assertTrue(st["stale"])
        self.assertIn("other-server", st["reason"])
        act = E.bsp_action(self.project, self.env())
        self.assertEqual(act["kind"], "skip")
        (self.project / E.BSP_REL).write_text("not json")
        self.assertTrue(E.bsp_status(self.project, self.env())["stale"])

    def test_xtool_off_path_is_stale(self):
        (self.bin / "xtool").unlink()
        E._apply([E.bsp_action(self.project, self.env())], self.env())
        st = E.bsp_status(self.project, self.env())
        self.assertTrue(st["stale"])
        self.assertIn("PATH", st["reason"])
        (self.home / ".local/bin").mkdir(parents=True)     # in ~/.local/bin only: a note, not stale
        (self.home / ".local/bin/xtool").write_text("")
        st = E.bsp_status(self.project, self.env())
        self.assertFalse(st["stale"])
        self.assertIn("~/.local/bin", st["note"])

    def test_missing_ios_sdk_is_stale(self):
        E._apply([E.bsp_action(self.project, self.env())], self.env())
        shutil.rmtree(self.home / ".config/swiftpm")
        st = E.bsp_status(self.project, self.env())
        self.assertTrue(st["stale"])
        self.assertEqual(st["reason"], "xtool's iOS SDK isn't set up: finish it on the Toolchain page")
        ps = E.project_status(self.project, self.env())
        self.assertFalse(ps["ready"])
        self.assertIn("Toolchain page", ps["hint"])
        act = E.bsp_action(self.project, self.env())
        self.assertEqual(act["kind"], "skip")
        self.assertNotIn("delete it", act["detail"])
        (self.project / E.LEGACY_REL).parent.mkdir()
        (self.project / E.LEGACY_REL).write_text(E.LEGACY_TEXT)
        self.assertFalse(E.bsp_status(self.project, self.env(swift=(6, 2)))["stale"])   # legacy: not checked
        (self.home / ".swiftpm/swift-sdks/darwin.artifactbundle").mkdir(parents=True)   # the other place
        self.assertFalse(E.bsp_status(self.project, self.env())["stale"])

    def test_skip_lsp_is_respected(self):
        (self.project / "xtool.yml").write_text("version: 1\nskipLSP: true\n")
        act = E.bsp_action(self.project, self.env())
        self.assertEqual(act["kind"], "skip")
        self.assertIn("skipLSP", act["detail"])
        self.assertIn("skipLSP", E.project_status(self.project, self.env())["hint"])

    def test_older_swift_gets_the_legacy_file(self):
        act = E.bsp_action(self.project, self.env(swift=(6, 2)))
        self.assertTrue(act["path"].endswith(E.LEGACY_REL))
        self.assertEqual(act["_text"], E.LEGACY_TEXT)


# ================================================================ machine-wide

class Machine(Base):
    def info(self, ed: str, env=None) -> dict:
        return E.editor_info(ed, env or self.env())

    def test_nothing_installed(self):
        st = E.machine_status(self.env())
        self.assertEqual({e["id"]: e["state"] for e in st["editors"]},
                         {"vscode": "not-installed", "zed": "not-installed", "neovim": "not-installed"})
        self.assertEqual(st["sourcekit_lsp"], str(self.bin / "sourcekit-lsp"))
        self.assertIsNone(st["preferred"])

    def test_zed_extension(self):
        self.tools("zeditor")
        i = self.info("zed")
        self.assertEqual(i["state"], "partial")
        self.assertIn("Swift extension", i["note"])
        (self.home / ".local/share/zed/extensions/installed/swift").mkdir(parents=True)
        i = self.info("zed")
        self.assertEqual(i["state"], "ready")
        self.assertTrue(i["extension"].endswith("installed/swift"))
        self.assertEqual(E.zed_machine_actions(self.env()), [])     # sourcekit-lsp on PATH: nothing to write

    def test_vscode_extension(self):
        self.tools("code")
        self.assertEqual(self.info("vscode")["state"], "partial")
        (self.home / ".vscode/extensions/swiftlang.swift-vscode-2.16.0").mkdir(parents=True)
        self.assertEqual(self.info("vscode")["state"], "ready")

    def test_sourcekit_off_path_goes_into_user_settings_with_backup(self):
        (self.bin / "sourcekit-lsp").unlink()
        tc = self.tmp / "toolchain/usr/bin"
        tc.mkdir(parents=True)
        for n in ("swift", "sourcekit-lsp"):
            (tc / n).write_text("#!/bin/sh\n")
            (tc / n).chmod(0o755)
        (self.bin / "swift").unlink()
        (self.bin / "swift").symlink_to(tc / "swift")
        zs = self.home / ".config/zed/settings.json"
        zs.parent.mkdir(parents=True)
        zs.write_text('// Zed settings\n{\n  "theme": "Omazed"\n}\n')
        acts = E.zed_machine_actions(self.env())
        self.assertEqual([a["kind"] for a in acts], ["backup", "merge"])
        self.assertEqual(acts[1]["scope"], "user")
        E._apply(acts, self.env())
        self.assertTrue(Path(acts[0]["path"]).read_text().startswith("// Zed settings"))
        v = jsonc.parse(zs.read_text()).value
        self.assertEqual(v["theme"], "Omazed")
        self.assertEqual(v["lsp"]["sourcekit-lsp"]["binary"]["path"], str(tc / "sourcekit-lsp"))
        self.assertEqual(E.vscode_machine_actions(self.env())[-1]["keys"], ["swift.sourcekit-lsp.serverPath"])

    def test_missing_sourcekit_is_a_note(self):
        (self.bin / "sourcekit-lsp").unlink()
        self.tools("zeditor")
        (self.home / ".local/share/zed/extensions/installed/swift").mkdir(parents=True)
        i = self.info("zed")
        self.assertEqual(i["state"], "partial")
        self.assertIn("sourcekit-lsp isn't installed", i["note"])

    def test_preferred_from_settings(self):
        sf = self.env().settings_file
        sf.parent.mkdir(parents=True)
        sf.write_text(json.dumps({"editorCommand": "code --new-window"}))
        self.assertEqual(E.preferred(self.env()), "vscode")
        sf.write_text(json.dumps({"editorCommand": "zeditor", "editorSetup": "neovim"}))
        self.assertEqual(E.preferred(self.env()), "neovim")

    def test_setup_remembers_the_editor_and_keeps_settings(self):
        self.tools("zeditor")
        sf = self.env().settings_file
        sf.parent.mkdir(parents=True)
        sf.write_text(json.dumps({"notifications": False, "editorCommand": ""}))
        dry = E.machine_setup("zed", False, self.env())
        self.assertTrue(dry["dryRun"])
        self.assertIsNone(dry["next"])                       # the UI shows dryRun itself
        self.assertEqual(dry["actions"][0]["detail"], "Remember Zed as your editor.")
        self.assertEqual([a["kind"] for a in dry["actions"]], ["merge"])
        self.assertNotIn("editorSetup", json.loads(sf.read_text()))
        done = E.machine_setup("zed", True, self.env())
        self.assertTrue(done["applied"])
        self.assertEqual(json.loads(sf.read_text()), {"notifications": False, "editorCommand": "",
                                                      "editorSetup": "zed"})
        self.assertEqual(E.machine_setup("zed", False, self.env())["actions"][0]["kind"], "skip")
        self.assertEqual(E.machine_setup("vscode", True, self.env(), prefer=False)["actions"], [])
        self.assertEqual(E.preferred(self.env()), "zed")


class Neovim(Base):
    def setUp(self):
        super().setUp()
        self.tools("nvim")
        self.cfg = self.home / ".config/nvim"
        (self.cfg / "lua/plugins").mkdir(parents=True)
        (self.cfg / "init.lua").write_text('require("config.lazy")\n')
        shutil.copy(FIX / "lazyvim.json", self.cfg / "lazyvim.json")
        self.lazy = self.home / ".local/share/nvim/lazy/LazyVim/lua/lazyvim/plugins/extras/lang"
        self.lazy.mkdir(parents=True)

    def test_lazyvim_extra_with_backup(self):
        (self.lazy / "swift.lua").write_text("return {}\n")
        before = (self.cfg / "lazyvim.json").read_text()
        r = E.machine_setup("neovim", False, self.env())
        kinds = [a["kind"] for a in r["actions"]]
        self.assertEqual(kinds[:2], ["backup", "merge"])
        self.assertTrue(all(a["scope"] == "user" for a in r["actions"]))
        self.assertIn("every project", r["actions"][1]["detail"])
        self.assertEqual((self.cfg / "lazyvim.json").read_text(), before)    # dry run
        r = E.machine_setup("neovim", True, self.env())
        bak = Path(r["actions"][0]["path"])
        self.assertEqual(bak.read_text(), before)
        data = json.loads((self.cfg / "lazyvim.json").read_text())
        self.assertIn(E.LAZY_EXTRA, data["extras"])
        self.assertIn("lazyvim.plugins.extras.editor.neo-tree", data["extras"])
        self.assertEqual(data["news"], {"NEWS.md": "10960"})
        self.assertEqual(r["state"], "ready")
        self.assertEqual(E.neovim_actions(self.env())[0]["kind"], "skip")

    def test_no_extra_writes_a_plugin_file(self):
        acts = E.neovim_actions(self.env())
        self.assertEqual([a["kind"] for a in acts], ["write"])
        self.assertEqual(acts[0]["detail"], E.NVIM_PLUGIN_DETAIL)
        self.assertTrue(E.editor_info("neovim", self.env())["lazyvim"])
        self.assertEqual(self.info_state(), "needs-setup")
        E._apply(acts, self.env())
        text = (self.cfg / E.NVIM_PLUGIN_REL).read_text()
        self.assertIn("sourcekit = {", text)
        self.assertIn('ensure_installed = { "swift" }', text)
        self.assertNotIn("cmd =", text)            # on PATH: no machine path
        self.assertEqual(self.info_state(), "ready")
        self.assertEqual((self.cfg / "lazyvim.json").read_text(), (FIX / "lazyvim.json").read_text())

    def test_existing_sourcekit_setup_detected(self):
        (self.cfg / "lua/plugins/swift.lua").write_text(
            '-- sourcekit is configured below\nreturn { "neovim/nvim-lspconfig", opts = { servers = { sourcekit = {} } } }\n')
        acts = E.neovim_actions(self.env())
        self.assertEqual(acts[0]["kind"], "skip")
        self.assertTrue(acts[0]["configured"])
        self.assertEqual(self.info_state(), "ready")

    def test_commented_out_mention_doesnt_count(self):
        (self.cfg / "lua/plugins/x.lua").write_text("-- TODO: sourcekit some day\nreturn {}\n")
        self.assertEqual(E.neovim_actions(self.env())[0]["kind"], "write")

    def test_not_lazyvim_is_left_alone(self):
        (self.cfg / "lazyvim.json").unlink()
        shutil.rmtree(self.home / ".local/share/nvim/lazy")
        acts = E.neovim_actions(self.env())
        self.assertEqual(acts[0]["kind"], "skip")
        self.assertIn("Not a LazyVim config", acts[0]["detail"])
        self.assertFalse(E.editor_info("neovim", self.env())["lazyvim"])
        self.assertEqual(self.info_state(), "partial")

    def test_invalid_lazyvim_json_left_alone(self):
        (self.lazy / "swift.lua").write_text("return {}\n")
        (self.cfg / "lazyvim.json").write_text("{oops")
        acts = E.neovim_actions(self.env())
        self.assertEqual(acts[0]["kind"], "skip")
        self.assertEqual((self.cfg / "lazyvim.json").read_text(), "{oops")

    def info_state(self) -> str:
        return E.editor_info("neovim", self.env())["state"]


# ================================================================ per project

class Project(Base):
    def prefer(self, ed: str) -> None:
        sf = self.env().settings_file
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps({"editorSetup": ed}))

    def test_status_before_and_after_auto(self):
        st = E.project_status(self.project, self.env())
        self.assertFalse(st["ready"])
        self.assertEqual(st["hint"], "Build once to enable completion")
        self.assertIsNone(st["auto"])
        r = E.auto_project(self.project, self.env())
        self.assertEqual([Path(w["path"]).name for w in r["wrote"]], ["xtool.json"])
        self.assertFalse((self.project / ".vscode").exists())      # not the preferred editor
        self.assertFalse((self.project / ".zed").exists())         # Zed needs nothing in the project
        st = E.project_status(self.project, self.env())
        self.assertTrue(st["ready"])
        self.assertIsNone(st["hint"])
        self.assertTrue(st["auto"]["wrote"][0]["path"].endswith(E.BSP_REL))
        self.assertEqual(E.auto_project(self.project, self.env()), {"wrote": []})   # idempotent

    def mark_created(self) -> None:
        f = self.env().state / "projects.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"added": [str(self.project)], "meta": {str(self.project): {"createdFrom": "swiftui-app"}}}))

    def test_added_project_gets_no_editor_files_by_itself(self):
        self.prefer("vscode")
        r = E.auto_project(self.project, self.env())
        self.assertEqual([Path(w["path"]).name for w in r["wrote"]], ["xtool.json"])   # .bsp for every project
        self.assertFalse((self.project / ".vscode").exists())
        st = E.project_status(self.project, self.env())
        self.assertFalse(st["created"])
        self.assertFalse(st["ready"])
        self.assertEqual(st["hint"], "VS Code settings for this project: set up from Settings › Code editor")
        E.project_fix(self.project, True, self.env())                                   # explicit --fix --confirm
        self.assertTrue((self.project / E.VSCODE_EXTENSIONS).is_file())
        self.assertTrue(E.project_status(self.project, self.env())["ready"])

    def test_vscode_preferred_gets_project_files(self):
        self.prefer("vscode")
        self.mark_created()
        (self.project / ".vscode").mkdir()
        (self.project / E.VSCODE_SETTINGS).write_text('{\n  "editor.rulers": [100]\n}\n')
        r = E.auto_project(self.project, self.env())
        self.assertEqual(sorted(w["kind"] for w in r["wrote"]), ["merge", "write", "write"])
        v = json.loads((self.project / E.VSCODE_SETTINGS).read_text())
        self.assertEqual(v["editor.rulers"], [100])
        self.assertFalse(v["swift.autoGenerateLaunchConfigurations"])
        self.assertTrue(E.project_status(self.project, self.env())["ready"])

    def test_auto_leaves_unmergeable_files(self):
        self.prefer("vscode")
        self.mark_created()
        (self.project / ".vscode").mkdir()
        shutil.copy(FIX / "settings-invalid.json", self.project / E.VSCODE_SETTINGS)
        E.auto_project(self.project, self.env())
        self.assertEqual((self.project / E.VSCODE_SETTINGS).read_text(),
                         (FIX / "settings-invalid.json").read_text())

    def test_fix_is_a_dry_run_unless_confirmed(self):
        r = E.project_fix(self.project, False, self.env(), editor="vscode")
        self.assertTrue(r["dryRun"])
        self.assertEqual([a["kind"] for a in r["actions"]], ["write", "write", "write"])
        self.assertFalse((self.project / ".bsp").exists())
        r = E.project_fix(self.project, True, self.env(), editor="vscode")
        self.assertTrue(r["applied"])
        self.assertTrue((self.project / E.VSCODE_EXTENSIONS).is_file())
        r = E.project_fix(self.project, False, self.env(), editor="vscode")
        self.assertEqual({a["kind"] for a in r["actions"]}, {"skip"})
        self.assertEqual(r["next"], "Nothing left to write for this project.")

    def test_brief_is_small(self):
        b = E.brief(self.project, self.env())
        self.assertEqual(set(b), {"bsp", "ready", "hint", "preferred", "auto", "created"})
        self.assertEqual(set(b["bsp"]), {"present", "stale", "reason"})


# ================================================================ CLI and demo mode


if __name__ == "__main__":
    unittest.main()
