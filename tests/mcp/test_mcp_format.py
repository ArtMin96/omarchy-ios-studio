"""MCP side of format and lint (mcp/iosstudio_mcp/tools/format.py): ios_format's dry run and apply against a mocked
`projects format` helper call (flags, capped file lists, the truncated diff with full_output_path and a note, the
result staying under the byte budget), the missing-tool error with its next step, and ios_format_lint (relative
paths, max_items and budget cuts with a note, the cached result). Also a round trip through the real helper in
demo mode, and that the server registers both tools.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_format.py
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp import budget as B
from iosstudio_mcp import helper as H
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import format as T
from support.mcp_fakes import FakeMCP

PROJ = "/home/u/Developer/Glance"


def fmt_payload(n_files: int = 2, diff: str | None = None, truncated: bool = False, errors: int = 0,
                apply: bool = False) -> dict:
    files = [f"Sources/Glance/F{i}.swift" for i in range(n_files)]
    if diff is None:
        diff = "".join(f"--- a/{f}\n+++ b/{f}\n@@ -1,2 +1,2 @@\n-let x=1\n+let x = 1\n" for f in files[:3])
    return {"path": PROJ, "apply": apply, "files_checked": n_files + 3, "files_changed": files[:200],
            "files_changed_total": n_files, "diff": diff, "diff_bytes": len(diff) * (4 if truncated else 1),
            "truncated": truncated, "full_output_path": "/home/u/.cache/ios-studio/format/x.diff" if truncated else None,
            "config_path": ".swift-format", "nested_configs": [], "tool": "swift-format", "tool_version": "6.4.2",
            "errors": [{"file": f"Sources/Glance/Bad{i}.swift", "message": "1:8: error: expected ')'"}
                       for i in range(errors)][:50],
            "errors_total": errors, "timed_out": False, "files_skipped": 0, "elapsed_ms": 40,
            "note": "Diff cut at 100 of 400 bytes; the whole diff is in full_output_path." if truncated else None}


def diag(i: int, rule: str = "Spacing", source: str = "swift-format", sev: str = "warning") -> dict:
    d = {"severity": sev, "category": "style", "message": f"add 1 space ({i})", "file": f"{PROJ}/Sources/Glance/F{i % 7}.swift",
         "line": i + 1, "col": 3, "rule": rule, "source": source}
    if rule in ("Spacing", "Indentation"):
        d["hint"] = "Format project fixes this."     # the helper's app-facing wording
    return d


def lint_payload(diags: list[dict], cached: bool = False) -> dict:
    by_rule: dict = {}
    for d in diags:
        by_rule[d["rule"]] = by_rule.get(d["rule"], 0) + 1
    return {"path": PROJ, "diagnostics": diags, "total": len(diags), "truncated": False,
            "counts": {"error": 0, "warning": len(diags)}, "by_source": {"swift-format": len(diags)},
            "by_rule": by_rule, "files_checked": 7, "config_path": None, "tools": ["swift-format"],
            "swiftlint": False, "errors": [], "timed_out": False, "elapsed_ms": 50, "at": 1_790_000_000.0,
            "cached": cached, "note": None}


class FormatTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mcp = FakeMCP()
        T.register(mcp)
        self.fmt = mcp.tools["ios_format"]
        self.lint = mcp.tools["ios_format_lint"]
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=AppState()))
        self.calls: list = []

    async def run_tool(self, fn, result, **kw):
        async def fake_call(group, action, flags=None, **_):
            self.calls.append((group, action, dict(flags or {})))
            if isinstance(result, Exception):
                raise result
            return result

        async def fake_resolve(state, project):
            return {"name": "Glance", "path": PROJ}

        with mock.patch.object(T.helper, "call", fake_call), mock.patch.object(T, "resolve_project", fake_resolve):
            res = await fn(self.ctx, **{"project_path": "Glance", **kw})
        text = "".join(c.text for c in res.content if getattr(c, "text", None))
        return res.structured_content, text, res

    # ------------------------------------------------------------ ios_format

    async def test_dry_run(self):
        sc, text, _ = await self.run_tool(self.fmt, fmt_payload(2))
        group, action, flags = self.calls[0]
        self.assertEqual((group, action), ("projects", "format"))
        self.assertEqual(flags["project"], PROJ)
        self.assertFalse(flags["apply"])
        self.assertIsNone(flags["files"])
        self.assertLess(flags["max_bytes"], B.DEFAULT_BYTES // 2)     # the diff is sent twice
        self.assertFalse(sc.get("applied", False))
        self.assertEqual(sc["files_changed"], ["Sources/Glance/F0.swift", "Sources/Glance/F1.swift"])
        self.assertIn("+let x = 1", sc["diff"])
        self.assertIn("+let x = 1", text)                 # the diff as plain lines in the text block
        self.assertIn("apply=true", " ".join(sc["next_steps"]))
        self.assertEqual(sc["config_path"], ".swift-format")
        self.assertFalse(sc.get("truncated", False))

    async def test_files_and_apply_flags(self):
        sc, _, _ = await self.run_tool(self.fmt, fmt_payload(1, apply=True), apply=True,
                                       files=["Sources/Glance/F0.swift", " "])
        flags = self.calls[0][2]
        self.assertTrue(flags["apply"])
        self.assertEqual(json.loads(flags["files"]), ["Sources/Glance/F0.swift"])
        self.assertTrue(sc["applied"])
        self.assertIn("ios_build_start", " ".join(sc["next_steps"]))

    async def test_truncated_diff_has_path_and_note(self):
        big = "".join(f"--- a/F{i}.swift\n+++ b/F{i}.swift\n@@ -1 +1 @@\n-" + "x" * 80 + "\n+" + "y" * 80 + "\n"
                      for i in range(40))
        sc, text, res = await self.run_tool(self.fmt, fmt_payload(300, diff=big, truncated=True, errors=60))
        self.assertTrue(sc["truncated"])
        self.assertEqual(sc["full_output_path"], "/home/u/.cache/ios-studio/format/x.diff")
        self.assertIn("full_output_path", sc["note"])
        self.assertEqual(len(sc["files_changed"]), T.FILES_CAP)
        self.assertEqual(sc["files_changed_total"], 300)
        self.assertEqual(len(sc["errors"]), T.ERRORS_CAP)
        self.assertEqual(sc["errors_total"], 60)
        size = len(text.encode()) + len(json.dumps(res.structured_content, ensure_ascii=False,
                                                   separators=(",", ":")).encode())
        self.assertLessEqual(size, B.DEFAULT_BYTES)

    async def test_nothing_to_change(self):
        sc, _, _ = await self.run_tool(self.fmt, {**fmt_payload(0, diff=""), "note": "Already formatted."})
        self.assertEqual(sc.get("files_changed_total", 0), 0)
        self.assertIn("Nothing to change", sc["next_steps"][0])
        self.assertEqual(sc["note"], "Already formatted.")

    async def test_missing_swift_format(self):
        err = H.HelperFailure({"code": "missing-swift-format", "message": "swift-format isn't installed."})
        with self.assertRaises(ToolError) as cm:
            await self.run_tool(self.fmt, err)
        self.assertIn("ios_toolchain_check", str(cm.exception))

    # ------------------------------------------------------------ ios_format_lint

    async def test_lint_relative_paths(self):
        data = lint_payload([diag(0), diag(1, "force_unwrapping", "swiftlint", "error")])
        sc, text, _ = await self.run_tool(self.lint, data)
        self.assertEqual(self.calls[0][:2], ("projects", "format-lint"))
        self.assertEqual(self.calls[0][2]["max"], T.HELPER_MAX_DIAGS)
        self.assertFalse(self.calls[0][2]["last"])
        first = sc["diagnostics"][0]
        self.assertEqual((first["file"], first["line"], first["rule"], first["category"]),
                         ("Sources/Glance/F0.swift", 1, "Spacing", "style"))
        self.assertEqual(sc["diagnostics"][1]["source"], "swiftlint")
        self.assertEqual(first["hint"], T.HINT_FIXABLE)    # app wording -> the tool an agent calls
        self.assertNotIn("Format project", first["hint"])
        self.assertIsNone(sc["diagnostics"][1].get("hint"))
        self.assertFalse(sc.get("clean", False))
        self.assertIn("ios_format", " ".join(sc["next_steps"]))
        self.assertTrue(sc["linted_at"].startswith("2026"))

    async def test_lint_syntax_hint_names_tools(self):
        d = {**diag(0, "syntax", sev="error"), "hint": "swift-format can't read this file until its syntax is fixed; "
                                                       "build it to see the compiler's error."}
        sc, _, _ = await self.run_tool(self.lint, lint_payload([d]))
        self.assertIn("ios_build_diagnostics", sc["diagnostics"][0]["hint"])

    async def test_lint_max_items_and_budget(self):
        diags = [diag(i, "Indentation" if i % 2 else "Spacing") for i in range(400)]
        sc, text, res = await self.run_tool(self.lint, lint_payload(diags), max_items=200)
        self.assertTrue(sc["truncated"])
        self.assertLess(sc["count"], 200)                  # the budget cut it further
        self.assertEqual(sc["total"], 400)
        self.assertIn("max_items", sc["note"])
        size = len(text.encode()) + len(json.dumps(res.structured_content, ensure_ascii=False,
                                                   separators=(",", ":")).encode())
        self.assertLessEqual(size, B.DEFAULT_BYTES)

        sc, _, _ = await self.run_tool(self.lint, lint_payload(diags[:30]), max_items=5)
        self.assertEqual((sc["count"], sc["total"], sc["omitted"]), (5, 30, 25))

    async def test_lint_clean_and_cached(self):
        sc, _, _ = await self.run_tool(self.lint, lint_payload([]))
        self.assertTrue(sc["clean"])
        sc, _, _ = await self.run_tool(self.lint, {**lint_payload([]), "at": None}, cached=True)
        self.assertTrue(self.calls[-1][2]["last"])
        self.assertIsNone(self.calls[-1][2]["files"])
        self.assertIn("without cached", sc["next_steps"][0])
        sc, _, _ = await self.run_tool(self.lint, lint_payload([diag(1)], cached=True), cached=True)
        self.assertTrue(sc["cached"])


class DemoRoundTrip(unittest.IsolatedAsyncioTestCase):
    """The real helper in demo mode: canned diff and warnings for a demo project."""

    async def test_demo(self):
        mcp = FakeMCP()
        T.register(mcp)
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=AppState()))
        proj = str(Path.home() / "Developer/WeatherGlance")

        async def fake_resolve(state, project):
            return {"name": "WeatherGlance", "path": proj}

        with mock.patch.dict(os.environ, {"IOSSTUDIO_DEMO": "1"}), \
                mock.patch.object(T, "resolve_project", fake_resolve):
            res = await mcp.tools["ios_format"](ctx, project_path=proj)
            lint = await mcp.tools["ios_format_lint"](ctx, project_path=proj)
        sc = res.structured_content
        self.assertEqual(sc["files_changed_total"], 2)
        self.assertIn("--- a/Sources/WeatherGlance/ForecastView.swift", sc["diff"])
        ls = lint.structured_content
        self.assertGreater(ls["total"], 0)
        self.assertTrue(all(d["file"].startswith("Sources/WeatherGlance/") for d in ls["diagnostics"]))
        self.assertIn("swiftlint", ls["by_source"])


class Registration(unittest.TestCase):
    def test_registered(self):
        from iosstudio_mcp.tools import MODULES
        self.assertIn("format", MODULES)
        from iosstudio_mcp import server
        self.assertIn("ios_format", server.INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
