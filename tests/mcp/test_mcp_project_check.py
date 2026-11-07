"""MCP side of the project check (mcp/iosstudio_mcp/tools/project_check.py): ios_project_check against a mocked
`projects check` helper call: a normal result, the check filter, max_issues and budget cuts with a note, and a
not-a-project error with its fix hint. Also that the server registers the tool.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_project_check.py
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp import budget as B
from iosstudio_mcp import helper as H
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import project_check as T
from support.mcp_fakes import FakeMCP

PROJ = "/home/u/Developer/Glance"
CHECKS = ["xtool-keys", "usage-descriptions", "icon", "entitlements", "bundle-id", "version"]


def issue(check: str, i: int = 0, severity: str = "warning", msg: str | None = None, file: str | None = None,
          line: int | None = None) -> dict:
    return {"severity": severity, "category": "config", "check": check,
            "message": msg or f"{check} problem {i}", "file": file, "line": line, "hint": f"Fix {check} {i}."}


def payload(issues: list[dict], total: int | None = None, truncated: bool = False) -> dict:
    counts = {"error": 0, "warning": 0, "note": 0}
    for d in issues:
        counts[d["severity"]] += 1
    return {"path": PROJ, "issues": issues, "total": total if total is not None else len(issues),
            "shown": len(issues), "truncated": truncated, "counts": counts, "elapsedMs": 12,
            "keysSource": "xtool-cache", "checks": CHECKS}


class ProjectCheckTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mcp = FakeMCP()
        T.register(mcp)
        self.tool = mcp.tools["ios_project_check"]
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=AppState()))
        self.calls: list = []

    async def run_tool(self, result, **kw):
        async def fake_call(group, action, flags=None, **_):
            self.calls.append((group, action, dict(flags or {})))
            if isinstance(result, Exception):
                raise result
            return result

        async def fake_resolve(state, project):
            return {"name": "Glance", "path": PROJ}

        with mock.patch.object(T.helper, "call", fake_call), mock.patch.object(T, "resolve_project", fake_resolve):
            res = await self.tool(self.ctx, **{"project_path": "Glance", **kw})
        text = "".join(c.text for c in res.content if getattr(c, "text", None))
        return res.structured_content, text

    async def test_normal_result(self):
        data = payload([
            issue("xtool-keys", msg="Unknown key 'bundleId' in xtool.yml; did you mean 'bundleID'?",
                  file=f"{PROJ}/xtool.yml", line=3),
            issue("usage-descriptions", msg="Sources use AVCaptureDevice but Info.plist has no "
                                            "NSCameraUsageDescription.", file=f"{PROJ}/Sources/Glance/Cam.swift",
                  line=12),
            issue("version", severity="note"),
        ])
        sc, text = await self.run_tool(data)
        self.assertEqual(self.calls, [("projects", "check", {"path": PROJ, "max": T.HELPER_MAX})])
        self.assertEqual((sc["project"], sc["path"], sc["total"], sc["count"]), ("Glance", PROJ, 3, 3))
        self.assertFalse(sc.get("clean"))
        self.assertFalse(sc.get("truncated"))
        self.assertNotIn("note", sc)
        first = sc["issues"][0]
        self.assertEqual((first["file"], first["line"], first["check"], first["category"]),
                         ("xtool.yml", 3, "xtool-keys", "config"))
        self.assertIn("bundleID", first["message"])
        self.assertEqual(sc["issues"][1]["file"], "Sources/Glance/Cam.swift")
        self.assertEqual(sc["counts"], {"warning": 2, "note": 1})
        self.assertEqual(sc["by_check"], {"xtool-keys": 1, "usage-descriptions": 1, "version": 1})
        self.assertEqual(sc["checks_run"], CHECKS)
        self.assertEqual(sc["keys_source"], "xtool-cache")
        self.assertIn("ios_project_check", " ".join(sc["next_steps"]))
        self.assertIn("bundleID", text)

    async def test_clean_project(self):
        sc, _ = await self.run_tool(payload([]))
        self.assertTrue(sc["clean"])
        self.assertEqual(sc["total"], 0)
        self.assertIn("ios_build_start", sc["next_steps"][0])

    async def test_check_filter(self):
        data = payload([issue("icon", 0), issue("xtool-keys", 1), issue("icon", 2), issue("entitlements", 3)])
        sc, _ = await self.run_tool(data, check="icon")
        self.assertEqual(sc["total"], 2)
        self.assertEqual([i["check"] for i in sc["issues"]], ["icon", "icon"])
        self.assertEqual(sc["by_check"], {"icon": 2, "xtool-keys": 1, "entitlements": 1})   # whole project
        sc, _ = await self.run_tool(data, check="version")
        self.assertEqual(sc["total"], 0)
        self.assertNotIn("issues", sc)
        self.assertIn("version check found nothing", sc["next_steps"][0])

    async def test_max_issues_cut_has_a_note(self):
        data = payload([issue("xtool-keys" if i % 2 else "icon", i) for i in range(30)])
        sc, _ = await self.run_tool(data, max_issues=5)
        self.assertEqual((sc["count"], sc["total"], sc["omitted"]), (5, 30, 25))
        self.assertTrue(sc["truncated"])
        self.assertIn("max_issues=30", sc["note"])
        self.assertIn("check=", sc["note"])

    async def test_helper_side_truncation_keeps_the_true_total(self):
        data = payload([issue("icon", i) for i in range(10)], total=900, truncated=True)
        sc, _ = await self.run_tool(data, max_issues=10)
        self.assertEqual((sc["count"], sc["total"]), (10, 900))
        self.assertTrue(sc["truncated"])

    async def test_budget_cut(self):
        long = "x" * 380
        data = payload([issue("usage-descriptions", i, msg=f"{i} {long}", file=f"{PROJ}/Sources/A{i}.swift",
                              line=i) for i in range(200)])
        sc, text = await self.run_tool(data, max_issues=200)
        size = B.nbytes(text) + B.nbytes(json.dumps(sc, ensure_ascii=False, separators=(",", ":")))
        self.assertLessEqual(size, B.DEFAULT_BYTES)
        self.assertTrue(sc["truncated"])
        self.assertLess(sc["count"], 200)
        self.assertEqual(sc["omitted"], 200 - sc["count"])
        self.assertIn(f"Showing {sc['count']} of 200", sc["note"])
        sc2, _ = await self.run_tool(data, max_issues=200, max_bytes=40_000)
        self.assertGreater(sc2["count"], sc["count"])

    async def test_not_a_project(self):
        err = H.HelperFailure({"code": "not-a-project", "message": "That folder isn't an iOS app project."})
        with self.assertRaises(ToolError) as cm:
            await self.run_tool(err)
        msg = str(cm.exception)
        self.assertIn("[not-a-project]", msg)
        self.assertIn("ios_project_list", msg)


class Registration(unittest.TestCase):
    def test_server_lists_the_tool(self):
        from iosstudio_mcp import server
        mcp = server.create_server()
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        self.assertIn("ios_project_check", tools)
        t = tools["ios_project_check"]
        self.assertTrue(t.annotations.read_only_hint)
        self.assertEqual(t.parameters["properties"]["check"]["enum"][:-1], CHECKS)
        self.assertIn("ios_project_check", server.INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
