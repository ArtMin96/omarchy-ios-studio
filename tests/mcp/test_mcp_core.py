"""The MCP server's shared core (mcp/iosstudio_mcp/core): tool kinds -> annotations, the one error mapper, and the
layering rule (tool modules import core/ and backends/, never each other).

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_core.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp.core import errors as E
from iosstudio_mcp.core.registry import annotations

TOOLS = Path(__file__).resolve().parents[2] / "mcp" / "iosstudio_mcp" / "tools"


class Kinds(unittest.TestCase):
    def hints(self, a):
        return (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint)

    def test_presets(self):
        self.assertEqual(self.hints(annotations("t", "read")), (True, False, True, False))
        self.assertEqual(self.hints(annotations("t", "read_remote")), (True, False, True, True))
        self.assertEqual(self.hints(annotations("t", "write")), (False, False, False, False))
        self.assertEqual(self.hints(annotations("t", "destructive")), (False, True, False, False))

    def test_overrides(self):
        a = annotations("Uninstall", "destructive", idempotent=True, open_world=True)
        self.assertEqual(self.hints(a), (False, True, True, True))
        self.assertEqual(a.title, "Uninstall")
        self.assertFalse(annotations("Shot", "read_remote", idempotent=False).idempotent_hint)


class Errors(unittest.TestCase):
    def exc(self, code, message="It failed.", **kw):
        return SimpleNamespace(code=code, message=message, fix=kw.get("fix"), detail=kw.get("detail"))

    def test_device_code_gets_its_next_step(self):
        text = str(E.device_error(self.exc("locked")))
        self.assertEqual(text, "It failed. [locked] Next: Ask the user to unlock the iPhone, then retry.")

    def test_feature_hint_wins(self):
        text = str(E.device_error(self.exc("busy"), {"busy": "Wait for the capture."}))
        self.assertTrue(text.endswith("[busy] Next: Wait for the capture."))
        self.assertEqual(text.count("Next:"), 1)

    def test_not_found_only_with_a_hint(self):
        self.assertNotIn("Next:", str(E.device_error(self.exc("not-found"))))
        self.assertIn("Next: Call ios_crash_list", str(E.device_error(self.exc("not-found"),
                                                                       {"not-found": "Call ios_crash_list."})))

    def test_fix_page_and_detail(self):
        text = str(E.device_error(self.exc("failed", detail="x" * 900, fix={"page": "device"})))
        self.assertIn("Detail: " + "x" * 300 + " Next:", text)
        self.assertIn("ios_open_app(page='device')", text)

    def test_rehint_swaps_the_next_step(self):
        err = ToolError("Can't do it. [app-running] Next: generic.")
        self.assertEqual(str(E.rehint(err, {"app-running": "Stop it first."})),
                         "Can't do it. [app-running] Next: Stop it first.")
        self.assertEqual(str(E.rehint(err, {})), str(err))


class Layering(unittest.TestCase):
    def test_tool_modules_never_import_each_other(self):
        bad = []
        for path in sorted(TOOLS.glob("*.py")):
            if path.name == "__init__.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    bad.append(f"{path.name}: from .{node.module or ''} import …")
        self.assertEqual(bad, [])

    def test_no_hand_made_annotations(self):
        for path in sorted(TOOLS.glob("*.py")):
            src = path.read_text()
            self.assertNotIn("annotations=", src, path.name)
            self.assertNotIn("@mcp.tool(", src, path.name)


class Published(unittest.TestCase):
    def test_every_tool_has_all_four_hints_and_destructive_ones_confirm(self):
        with mock.patch.dict(os.environ, {"IOSSTUDIO_DEMO": "1"}):
            from iosstudio_mcp import server
            tools = asyncio.run(server.mcp.list_tools())
        self.assertGreater(len(tools), 90)
        for t in tools:
            a = t.annotations
            self.assertTrue(a and a.title, t.name)
            self.assertNotIn(None, (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint),
                             t.name)
            if a.destructive_hint:
                self.assertIn("confirm", t.input_schema.get("properties", {}), t.name)
        by = {t.name: t.annotations for t in tools}
        self.assertFalse(by["ios_device_select"].open_world_hint)
        self.assertFalse(by["ios_project_create"].open_world_hint)
        for name in ("ios_launch_profile", "ios_memory_leaks"):     # both stop/pause the app, delete nothing
            self.assertFalse(by[name].destructive_hint, name)
            self.assertTrue(by[name].open_world_hint, name)


if __name__ == "__main__":
    unittest.main()
