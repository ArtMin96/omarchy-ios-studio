"""MCP side of crash symbolication (mcp/iosstudio_mcp/tools/crashes.py): ios_crash_get's app_frames and
symbolicated status from crash.pull's summary, and file:line in section='threads' from the saved result.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_crash_symbolication.py
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.device import device_crash
from iosstudio.features import crash_symbols as S
from iosstudio_mcp.backends import crashes as T
from iosstudio_mcp.state import AppState
from iosstudio_mcp.core import budget as B
from support.crash import ips

PROJECT = "/work/Crashy"


def resolved() -> dict:
    def at(fn, line=None):
        out = {"image": "Crashy", "function": fn, "line": line, "column": 17 if line else None}
        if line:
            out.update(file="Sources/Crashy/Crashy.swift", path=PROJECT + "/Sources/Crashy/Crashy.swift")
        return out
    return S.make_result("full", "ok", "Resolved the app's frames to file:line.", project=PROJECT,
                         frames={"0:1": at("Forecast.first()", 6), "0:2": at("static Crashy.main()", 14),
                                 "1:0": at("static Crashy.main()", 14)})


class CrashGet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "Crashy-2026-11-05-100000.ips"
        self.path.write_text(ips())
        self.res = resolved()
        Path(str(self.path) + S.SIDE).write_text(json.dumps(self.res))

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, res: dict, **kw):
        summary = S.apply(device_crash.parse_ips(ips()), res)

        async def fake(state, cmd, args, **_):
            return {"path": str(self.path), "summary": summary}
        async def go():
            return await T.crash_detail(AppState(), "/" + self.path.name, **kw)
        with mock.patch.object(T, "dcall", fake):
            return asyncio.run(go())

    def test_summary_has_app_frames(self):
        out = self.get(self.res)
        self.assertEqual(out.symbolicated, "full")
        self.assertEqual(out.project_path, PROJECT)
        self.assertEqual([(f.index, f.file, f.line) for f in out.app_frames],
                         [(1, "Sources/Crashy/Crashy.swift", 6), (2, "Sources/Crashy/Crashy.swift", 14)])
        self.assertIn("(Sources/Crashy/Crashy.swift:6:17)", out.frames[1])
        self.assertTrue(any("Sources/Crashy/Crashy.swift:6" in s for s in out.next_steps), out.next_steps)
        self.assertLess(len(json.dumps(B.dump(out))), B.DEFAULT_BYTES)

    def test_no_build_says_why(self):
        res = S.make_result("none", "no-build", "No build on this computer matches Crashy.", S.HINT_REBUILD)
        out = self.get(res)
        self.assertEqual(out.symbolicated, "none")
        self.assertEqual(out.app_frames, [])
        self.assertIn("Rebuild", out.symbolication_note)

    def test_threads_section_uses_saved_result(self):
        out = self.get(self.res, section="threads")
        self.assertIn("Forecast.first() (Sources/Crashy/Crashy.swift:6:17)", out.text)
        self.assertIn("Thread 1", out.text)
        self.assertIn("static Crashy.main() (Sources/Crashy/Crashy.swift:14:17)", out.text)
        self.assertEqual(out.app_frames, [])

    def test_render_without_symbols_is_unchanged(self):
        lines = T.render_section(ips(), "threads")
        self.assertIn("  1 Crashy + 0x41d0", lines)


if __name__ == "__main__":
    unittest.main()
