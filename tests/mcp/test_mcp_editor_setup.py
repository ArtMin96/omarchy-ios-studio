"""MCP side of editor setup (mcp/iosstudio_mcp/tools/editor_setup.py): ios_editor_setup against mocked
`editor status` / `editor setup` helper calls: auto picks the installed editors and remembers one, dry run vs
confirm, user-level changes called out, the per-project fix, no editor installed. Also the `editor` field
of ios_project_info and that the server registers the tool.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_editor_setup.py
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp.models import ProjectDetail
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import editor_setup as T
from support.mcp_fakes import FakeMCP

PROJ = "/home/u/Developer/Glance"


def machine(zed=True, nvim=True, vscode=False, preferred=None):
    def e(i, inst, state):
        return {"id": i, "name": {"vscode": "VS Code", "zed": "Zed", "neovim": "Neovim"}[i], "installed": inst,
                "state": state}
    return {"swift": "6.4", "sourcekit_lsp": "/usr/bin/sourcekit-lsp", "preferred": preferred,
            "editors": [e("vscode", vscode, "not-installed" if not vscode else "partial"),
                        e("zed", zed, "ready" if zed else "not-installed"),
                        e("neovim", nvim, "needs-setup" if nvim else "not-installed")]}


def setup_answer(editor, confirm):
    acts = [{"kind": "write", "path": "/home/u/.config/nvim/lua/plugins/ios-studio-swift.lua", "scope": "user",
             "detail": "Create the plugin file", "preview": "return {}\n", "done": confirm}] if editor == "neovim" else []
    acts.append({"kind": "merge", "path": "/home/u/.config/ios-studio/settings.json", "scope": "user",
                 "detail": "Remember it", "keys": ["editorSetup"], "done": confirm})
    return {"editor": editor, "name": editor.title(), "dryRun": not confirm, "actions": acts, "applied": confirm,
            "state": "ready" if confirm else ("needs-setup" if editor == "neovim" else "ready"),
            "next": "Restart Neovim" if editor == "neovim" else "Open a project", "notes": []}


class EditorSetupTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mcp = FakeMCP()
        T.register(mcp)
        self.tool = mcp.tools["ios_editor_setup"]
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=AppState()))
        self.calls: list = []

    async def run_tool(self, m=None, project_answer=None, **kw):
        async def fake_call(group, action, flags=None, **_):
            flags = dict(flags or {})
            self.calls.append((group, action, flags))
            if action == "status" and not flags.get("project"):
                return m or machine()
            if action == "status":
                return project_answer
            return setup_answer(flags["editor"], bool(flags.get("confirm")))

        async def fake_resolve(state, project):
            return {"name": "Glance", "path": PROJ}

        with mock.patch.object(T.helper, "call", fake_call), mock.patch.object(T, "resolve_project", fake_resolve):
            res = await self.tool(self.ctx, **kw)
        return res.structured_content

    def setups(self):
        return [(f["editor"], f.get("confirm"), f.get("prefer")) for g, a, f in self.calls if a == "setup"]

    async def test_auto_dry_run_lists_installed_editors(self):
        out = await self.run_tool()
        self.assertTrue(out["dry_run"])
        self.assertEqual([e["editor"] for e in out["editors"]], ["zed", "neovim"])
        self.assertEqual(self.setups(), [("zed", None, None), ("neovim", None, "no")])
        self.assertIn("Dry run", out["message"])
        self.assertTrue(any("user's own editor config" in s for s in out["next_steps"]))
        self.assertTrue(any("confirm=true" in s for s in out["next_steps"]))
        self.assertTrue(out["editors"][1]["actions"][0]["path"].endswith("lua/plugins/ios-studio-swift.lua"))

    async def test_auto_keeps_the_remembered_editor(self):
        await self.run_tool(m=machine(preferred="neovim"), confirm=True)
        self.assertEqual(self.setups(), [("zed", True, "no"), ("neovim", True, None)])

    async def test_one_editor_confirmed(self):
        out = await self.run_tool(editor="neovim", confirm=True)
        self.assertFalse(out["dry_run"])
        self.assertEqual(out["preferred"], "neovim")
        self.assertTrue(out["editors"][0]["applied"])
        self.assertIn("Restart Neovim", out["next_steps"])

    async def test_nothing_installed(self):
        with self.assertRaises(ToolError):
            await self.run_tool(m=machine(zed=False, nvim=False))

    async def test_project_fix(self):
        ans = {"ready": False, "hint": "Build once to enable completion",
               "fix": {"dryRun": True, "applied": False, "actions": [
                   {"kind": "write", "path": PROJ + "/.bsp/xtool.json", "scope": "project", "detail": "Create it"}]}}
        out = await self.run_tool(editor="zed", project_path="Glance", project_answer=ans)
        self.assertEqual(out["project"]["actions"][0]["kind"], "write")
        self.assertFalse(out["project"]["ready"])
        fixes = [f for g, a, f in self.calls if a == "status" and f.get("project")]
        self.assertEqual(fixes, [{"project": PROJ, "fix": True, "confirm": None}])
        self.assertTrue(any("ios_build_start" in s for s in out["next_steps"]))


class ProjectInfoEditor(unittest.TestCase):
    def test_editor_field_is_small(self):
        p = {"name": "Glance", "path": PROJ, "editor": {
            "bsp": {"present": True, "stale": False, "reason": None, "path": PROJ + "/.bsp/xtool.json"},
            "ready": True, "hint": None, "preferred": "zed", "created": True,
            "auto": {"at": 1767225600, "wrote": [{"kind": "write", "path": PROJ + "/.bsp/xtool.json"}]}}}
        d = ProjectDetail.from_helper(p).model_dump()
        self.assertEqual(d["editor"]["bsp"], {"present": True, "stale": False, "reason": None})
        self.assertEqual(d["editor"]["auto"]["wrote"], ["xtool.json"])
        self.assertTrue(d["editor"]["created"])
        self.assertIsNone(ProjectDetail.from_helper({"name": "x", "path": "/x"}).editor)


class Registered(unittest.TestCase):
    def test_module_listed(self):
        from iosstudio_mcp import tools
        self.assertIn("editor_setup", tools.MODULES)


if __name__ == "__main__":
    unittest.main()
