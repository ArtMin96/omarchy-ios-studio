"""MCP side of the app console (mcp/iosstudio_mcp/tools/console.py): line rendering, the capture
summary's selection, and ios_app_console_read's cursor paging and output budget against a fake daemon
that serves a real helper/iosstudio/device/app_console.py session.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_console.py
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from iosstudio.device import app_console as C
from iosstudio_mcp import budget as B
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import console as T
from support.mcp_fakes import FakeMCP


def make_session(n: int, text=lambda i: f"line {i}", stream=lambda i: "stdout") -> C.ConsoleSession:
    s = C.ConsoleSession("c1", "XTL-T.com.example.app", rate=10 ** 6, burst=10 ** 6, ring_lines=3000)
    s.status = "running"
    for i in range(n):
        s.add(stream(i), text(i))
    return s


def fake_daemon(sess: C.ConsoleSession, calls: list):
    async def dcall(state, cmd, args=None, *, timeout=60.0, udid=None, not_found=None):
        calls.append((cmd, dict(args or {})))
        assert cmd == "console.read"
        page = sess.ring.read(args.get("cursor"), limit=args.get("limit") or 500)
        return {**page, "session": sess.describe()}
    return dcall


class Helpers(unittest.TestCase):
    def test_fmt_line(self):
        e = {"ts": 0, "stream": "stderr", "text": "x" * 500, "cut": 12}
        line = T.fmt_line(7, e, cut=100)
        self.assertTrue(line.startswith("7 "))
        self.assertIn("[stderr] ", line)
        self.assertIn("…[+400]", line)
        self.assertIn("line cut, +12 bytes", line)
        self.assertNotIn("[stderr]", T.fmt_line(None, {"ts": 0, "stream": "out", "text": "hi"}))

    def test_page_folds_repeats_and_respects_room(self):
        entries = [(i, {"ts": 0, "stream": "out", "text": "same" if i < 10 else f"n{i}"}) for i in range(40)]
        lines, shown, cut = T.page(entries, limit=40, room=10_000)
        self.assertIn("×10", lines[0])
        self.assertEqual(shown, 40)
        self.assertEqual(cut, 0)
        lines, shown, cut = T.page(entries, limit=40, room=120)
        self.assertLess(shown, 40)
        self.assertGreater(cut, 0)
        self.assertLessEqual(sum(B.nbytes(x) + 1 for x in lines), 120)

    def test_select_lines_keeps_stderr_and_the_latest(self):
        entries = [{"ts": 0, "stream": "stderr" if i == 3 else "stdout", "text": f"l{i}"} for i in range(100)]
        lines, left_out = T.select_lines(entries, max_lines=10, room=10_000)
        self.assertEqual(len(lines), 10)
        self.assertIn("[stderr] l3", lines[0])
        self.assertTrue(lines[-1].endswith("l99"))
        self.assertEqual(left_out, 90)


class ReadTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mcp = FakeMCP()
        T.register(mcp)
        self.tool = mcp.tools["ios_app_console_read"]
        self.state = AppState()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.state))

    async def read(self, sess, **kw):
        calls: list = []
        with mock.patch.object(T, "dcall", fake_daemon(sess, calls)):
            res = await self.tool(self.ctx, **{"bundle_id": "com.example.app", **kw})
        text = "".join(c.text for c in res.content if getattr(c, "text", None))
        return res.structured_content, text, calls

    async def test_pages_with_a_cursor_and_continues(self):
        sess = make_session(250)
        sc, _, _ = await self.read(sess, limit=100)
        self.assertEqual(sc["count"], 100)
        self.assertEqual(sc["next_cursor"], 100)
        self.assertTrue(sc["has_more"])
        self.assertTrue(sc["lines"][0].startswith("0 "))
        sc, _, _ = await self.read(sess, limit=100)            # no cursor: continues where it ended
        self.assertTrue(sc["lines"][0].startswith("100 "))
        sc, _, _ = await self.read(sess, cursor=240, limit=100)
        self.assertEqual(sc["count"], 10)
        self.assertFalse(sc["has_more"])
        self.assertEqual(sc["next_cursor"], 250)

    async def test_budget_cuts_with_a_note(self):
        sess = make_session(2000, text=lambda i: f"{i} " + "word " * 50)
        sc, text, _ = await self.read(sess, limit=2000, cursor=0)
        size = B.nbytes(text) + B.nbytes(json.dumps(sc, ensure_ascii=False, separators=(",", ":")))
        self.assertLessEqual(size, B.DEFAULT_BYTES)
        self.assertTrue(sc["truncated"])
        self.assertIn("cursor=", sc["note"])
        nxt = sc["next_cursor"]
        self.assertEqual(nxt, sc["count"])
        sc2, _, _ = await self.read(sess, limit=2000, cursor=0, max_bytes=40_000)
        self.assertGreater(sc2["count"], sc["count"])

    async def test_grep_and_stream_filters(self):
        sess = make_session(300, text=lambda i: f"tick {i}" if i % 50 else f"ERROR {i}",
                            stream=lambda i: "stderr" if i % 100 == 0 else "stdout")
        sc, _, _ = await self.read(sess, grep="error", cursor=0)
        self.assertEqual(sc["count"], 6)
        self.assertEqual(sc["next_cursor"], 300)
        sc, _, _ = await self.read(sess, stream="stderr", cursor=0)
        self.assertEqual([x.split(" ", 1)[0] for x in sc["lines"]], ["0", "100", "200"])

    async def test_newest(self):
        sess = make_session(500)
        sc, _, _ = await self.read(sess, newest=True, limit=5)
        self.assertEqual([x.split(" ", 1)[0] for x in sc["lines"]], ["495", "496", "497", "498", "499"])
        self.assertEqual(sc["next_cursor"], 500)

    async def test_missed_lines_are_reported(self):
        sess = make_session(3500)   # ring keeps 3000
        sc, _, _ = await self.read(sess, cursor=0, limit=10)
        self.assertEqual(sc["missed"], 500)
        self.assertTrue(sc["lines"][0].startswith("500 "))

    async def test_needs_a_target(self):
        from mcp.server.mcpserver.exceptions import ToolError
        with self.assertRaises(ToolError):
            await self.tool(self.ctx, bundle_id=None, session_id=None)


if __name__ == "__main__":
    unittest.main()
