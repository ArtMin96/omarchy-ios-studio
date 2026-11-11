"""App icon (#18), MCP side (mcp/iosstudio_mcp/tools/app_icon.py): ios_project_icon against a fake helper that answers
`projects icon` with the reply shape of docs/features (the contract): a dry run without --confirm, --confirm after
the user agreed, fit/background passed through, the background normalized and validated before the helper runs,
image_path checks, the demo answer surfaced as demo (nothing written), helper errors with an agent next step, the
result kept under its budget, and the tool registered as destructive with confirm defaulting to false. The guide
and project AGENTS.md naming only real tools is checked by test_agent_instructions_mcp.py.

    python -m pytest tests/mcp/test_app_icon_mcp.py
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from iosstudio_mcp.tools import app_icon as M
from iosstudio_mcp.tools import project_check as PC
from support.mcp_harness import FakeHelper, ToolCase, size_of

PROJ = "/home/u/Developer/Glance"


def reply(**over) -> dict:
    d = {"name": "Glance", "path": PROJ,
         "source": {"path": "/home/u/Pictures/logo.png", "width": 600, "height": 400, "format": "PNG",
                    "hasAlpha": True},
         "fit": "fit", "background": "#ffffff", "target": "Resources/AppIcon.png",
         "targetPath": PROJ + "/Resources/AppIcon.png", "replaces": False, "yml": "add", "previous": None,
         "preview": "/home/u/.cache/ios-studio/icons/preview-1.png",
         "warnings": ["The image is 600×400, smaller than 1024×1024: it was scaled up and may look soft."],
         "backend": "pillow", "applied": False, "check": []}
    d.update(over)
    return d


class IconTool(ToolCase):
    DAEMON = False

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.helper.reply("projects", "list", {"projects": [{"name": "Glance", "path": PROJ}]})
        self.image = self.tmp / "logo.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n")
        env = mock.patch.dict(os.environ, {"IOSSTUDIO_MCP_ALLOW_PATHS": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)

    async def icon(self, **kw) -> dict:
        return await self.data(M, "ios_project_icon", **{"project_path": "Glance", "image_path": str(self.image), **kw})

    def argv(self) -> list[str]:
        calls = self.helper.calls("projects", "icon")
        self.assertEqual(len(calls), 1)
        return calls[0]

    async def test_dry_run_by_default(self):
        self.helper.reply("projects", "icon", reply())
        sc = await self.icon()
        argv = self.argv()
        self.assertIsNone(FakeHelper.flag(argv, "confirm"))
        self.assertEqual(FakeHelper.flag(argv, "path"), PROJ)
        self.assertEqual(FakeHelper.flag(argv, "image"), str(self.image.resolve()))
        self.assertEqual((FakeHelper.flag(argv, "fit"), FakeHelper.flag(argv, "background")), ("auto", "#ffffff"))
        self.assertFalse(sc["done"])
        self.assertFalse(sc["demo"])
        self.assertEqual(sc["preview_path"], "/home/u/.cache/ios-studio/icons/preview-1.png")
        self.assertEqual((sc["target"], sc["yml"], sc["fit"]), ("Resources/AppIcon.png", "add", "fit"))
        src = sc["source"]
        self.assertEqual((src["width"], src["height"], src["format"], src["has_alpha"]), (600, 400, "PNG", True))
        self.assertIn("Nothing was changed", sc["message"])
        self.assertEqual(len(sc["would"]), 2)
        self.assertIn("iconPath", sc["would"][1])
        self.assertIn("confirm=true", sc["next_steps"][0])
        self.assertIn("preview_path", sc["next_steps"][0])
        self.assertEqual(len(sc["warnings"]), 1)

    async def test_confirm_writes(self):
        self.helper.reply("projects", "icon", reply(applied=True, replaces=True, yml="change",
                                                    previous="Icon.jpg", fit="fill"))
        sc = await self.icon(confirm=True, fit="fill", background="#1E90FF")
        argv = self.argv()
        self.assertEqual(FakeHelper.flag(argv, "confirm"), "")
        self.assertEqual((FakeHelper.flag(argv, "fit"), FakeHelper.flag(argv, "background")), ("fill", "#1e90ff"))
        self.assertTrue(sc["done"])
        self.assertNotIn("would", sc)
        self.assertEqual((sc["replaces"], sc["yml"], sc["previous_icon"]), (True, "change", "Icon.jpg"))
        steps = " ".join(sc["next_steps"])
        self.assertIn("ios_project_check", steps)
        self.assertIn("ios_run", steps)

    async def test_check_findings_after_writing(self):
        self.helper.reply("projects", "icon", reply(applied=True, check=[
            {"severity": "warning", "message": "The icon has transparency (an alpha channel).", "hint": "Flatten it."}]))
        sc = await self.icon(confirm=True)
        self.assertEqual(sc["check"][0]["message"], "The icon has transparency (an alpha channel).")
        self.assertIn("problems", sc["next_steps"][0])

    async def test_short_color_is_normalized(self):
        self.helper.reply("projects", "icon", reply())
        await self.icon(background="0aF")
        self.assertEqual(FakeHelper.flag(self.argv(), "background"), "#00aaff")

    async def test_bad_inputs_fail_before_the_helper(self):
        for kw, words in (({"background": "white"}, "hex color"), ({"background": "#12345"}, "hex color"),
                          ({"image_path": str(self.tmp / "none.png")}, "No file"),
                          ({"image_path": str(self.tmp)}, "folder"), ({"image_path": " "}, "required")):
            text = await self.error(M, "ios_project_icon", ctx=self.ctx, project_path="Glance",
                                    **{"image_path": str(self.image), **kw})
            self.assertIn(words, text, kw)
        self.assertEqual(self.helper.calls("projects", "icon"), [])

    async def test_image_outside_home_is_refused(self):
        with mock.patch.dict(os.environ, {"IOSSTUDIO_MCP_ALLOW_PATHS": ""}), \
                mock.patch.object(M.helper, "HOME", self.tmp / "home"):
            text = await self.error(M, "ios_project_icon", ctx=self.ctx, project_path=PROJ, image_path="/etc/passwd")
        self.assertIn("home folder", text)

    async def test_demo_reply(self):
        self.helper.reply("projects", "icon", reply(demo=True, applied=True))
        sc = await self.icon(confirm=True)
        self.assertTrue(sc["demo"])
        self.assertFalse(sc["done"])          # demo never writes, whatever it says
        self.assertIn("Demo mode", sc["message"])

    async def test_helper_error_gets_an_agent_hint(self):
        self.helper.fail("projects", "icon", "generated-yml", "xtool.yml is generated by a script.",
                         fix={"page": "projects"})
        text = await self.error(M, "ios_project_icon", ctx=self.ctx, project_path="Glance", image_path=str(self.image))
        self.assertIn("[generated-yml]", text)
        self.assertIn("Next: This project's xtool.yml is generated", text)

    async def test_output_is_bounded(self):
        many = [f"warning {i} " + "x" * 400 for i in range(40)]
        findings = [{"severity": "warning", "message": "m" * 600, "hint": "h" * 600} for _ in range(40)]
        self.helper.reply("projects", "icon", reply(applied=True, warnings=many, check=findings))
        res = await self.call(M, "ios_project_icon", project_path="Glance", image_path=str(self.image), confirm=True)
        sc = res.structured_content
        self.assertLessEqual(size_of(res), 12_000)
        self.assertTrue(sc["truncated"])
        self.assertLessEqual(len(sc["warnings"]), M.WARNINGS_CAP)
        self.assertLessEqual(len(sc["check"]), M.FINDINGS_CAP)
        self.assertNotIn("base64", str(sc))

    def test_registered_as_destructive_with_confirm(self):
        rec = self.rec(M)
        a = rec.annotations["ios_project_icon"]
        self.assertEqual((a.read_only_hint, a.destructive_hint, a.open_world_hint), (False, True, False))
        import inspect
        params = inspect.signature(rec.tools["ios_project_icon"]).parameters
        self.assertIs(params["confirm"].default, False)
        self.assertEqual(params["fit"].default, "auto")
        self.assertEqual(params["background"].default, "#ffffff")


class ProjectCheckNamesTheTool(ToolCase):
    DAEMON = False

    async def test_icon_findings_point_at_ios_project_icon(self):
        self.helper.reply("projects", "list", {"projects": [{"name": "Glance", "path": PROJ}]})
        self.helper.reply("projects", "check", {"path": PROJ, "total": 1, "issues": [
            {"severity": "note", "check": "icon", "message": "The app has no icon.", "file": PROJ + "/xtool.yml"}]})
        sc = await self.data(PC, "ios_project_check", project_path="Glance")
        self.assertIn("ios_project_icon", " ".join(sc["next_steps"]))


class Registration(unittest.TestCase):
    def test_listed_and_in_the_guide(self):
        from iosstudio_mcp.tools import MODULES
        self.assertIn("app_icon", MODULES)
        with mock.patch.dict(os.environ, {"IOSSTUDIO_DEMO": "1"}):
            from iosstudio_mcp import server
        self.assertIn("ios_project_icon", {t.name for t in server.mcp._tool_manager.list_tools()})
        self.assertIn("ios_project_icon", server.resources.GUIDE.read_text())


if __name__ == "__main__":
    unittest.main()
