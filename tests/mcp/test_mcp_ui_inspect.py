"""MCP side of UI inspection (mcp/iosstudio_mcp/tools/ui_inspect.py): tree filtering, paging and the output
budget, pressing by id/label, error mapping, and audit grouping, against a mocked dcall.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_ui_inspect.py
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from device_client import DeviceError
from iosstudio_mcp.core import budget as B
from iosstudio_mcp.core.errors import device_error
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import ui_inspect as T
from support.mcp_fakes import FakeMCP, size_of

ROLES = ["button", "staticText", "textField", "image", "switch", "cell"]


def big_tree(n: int = 400, complete: bool = True) -> dict:
    els = []
    for i in range(n):
        role = ROLES[i % len(ROLES)]
        els.append({"id": f"e{i + 1}", "role": role, "label": f"Row {i} with a fairly long accessibility label",
                    "value": f"value {i}" if role in ("textField", "switch") else None,
                    "hint": "Double tap to open the details screen" if role == "cell" else None,
                    "identifier": f"row.{i}", "traits": [role] + (["selected"] if i % 7 == 0 else []),
                    "frame": {"x": 16.0, "y": 100.5 + i * 44, "w": 358.0, "h": 44.0},
                    "enabled": False if i == 3 else True})
    return {"elements": els, "count": n, "truncated": False, "complete": complete, "method": "focus-walk",
            "elapsedMs": 4200, "screen": {"w": 390, "h": 844}}


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mcp = FakeMCP()
        with mock.patch.object(T, "tool", lambda m, *a, **k: m.tool(**k)):   # the bare functions: models, not results
            T.register(mcp)
        self.tools = mcp.tools
        self.state = AppState()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.state))
        self.calls: list = []
        self.replies: dict = {}

    async def dcall(self, state, cmd, args=None, *, timeout=60.0, udid=None, hints=None):
        self.calls.append((cmd, dict(args or {}), udid))
        r = self.replies[cmd]
        if isinstance(r, Exception):
            raise device_error(r, hints)      # what the real dcall does with a DeviceError
        return r

    async def call(self, name, **kw):
        with mock.patch.object(T, "dcall", self.dcall):
            return await self.tools[name](self.ctx, **kw)

    async def call_wrapped(self, name, **kw):
        """Through the real @tool wrapper (to_result + error clipping), as the server runs it."""
        from iosstudio_mcp.core.registry import tool as real_tool
        rec = FakeMCP()
        with mock.patch.object(T, "tool", lambda m, *a, **k: real_tool(rec, *a, **k)), \
                mock.patch.object(T, "dcall", self.dcall):
            T.register(rec)
            return await rec.tools[name](self.ctx, **kw)


class Helpers(unittest.TestCase):
    def test_element_is_compact(self):
        e = T.element({"id": "e1", "role": "button", "label": "  Sign\n in ", "value": "", "traits": ["button", "header"],
                       "frame": {"x": 1.4, "y": 2.6, "w": 100, "h": 44}, "enabled": True})
        d = B.dump(e)
        self.assertEqual(d, {"id": "e1", "role": "button", "label": "Sign in", "traits": ["header"],
                             "frame": [1, 3, 100, 44]})
        self.assertFalse(B.dump(T.element({"id": "e2", "enabled": False}))["enabled"])
        self.assertNotIn("frame", B.dump(T.element({"id": "e3", "frame": None})))

    def test_filters(self):
        els = big_tree(12)["elements"]
        self.assertEqual(len(T.filter_elements(els, "ROW 1")), 3)          # 1, 10, 11
        self.assertEqual(len(T.filter_elements(els, "row.5")), 1)          # identifier
        self.assertEqual(len(T.filter_elements(els, "value 4")), 1)        # value
        self.assertEqual({e["role"] for e in T.filter_elements(els, roles=["BUTTON", "switch"])}, {"button", "switch"})
        self.assertEqual(len(T.filter_elements(els, roles=["selected"])), 2)  # traits match too (0, 7)
        inter = T.filter_elements(els, interactive_only=True)
        self.assertEqual({e["role"] for e in inter}, {"button", "textField", "switch", "cell"})
        self.assertEqual(len(T.filter_elements(els, "row", roles=["button"], interactive_only=True)), 2)

    def test_group_issues_orders_by_count(self):
        raw = [{"type": "contrast"}, {"type": "hit-region"}, {"type": "hit-region"}, {"type": "contrast"},
               {"type": "hit-region"}, {"type": "missing-label"}]
        counts, ordered = T.group_issues(raw)
        self.assertEqual(list(counts.items()), [("hit-region", 3), ("contrast", 2), ("missing-label", 1)])
        self.assertEqual([i["type"] for i in ordered], ["hit-region"] * 3 + ["contrast"] * 2 + ["missing-label"])


class Tree(Base):
    async def test_big_tree_stays_bounded_and_pages(self):
        self.replies["ui.tree"] = big_tree(400)
        res = await self.call_wrapped("ios_ui_tree", max_elements=400)
        sc = res.structured_content
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        self.assertTrue(sc["truncated"])
        self.assertEqual(sc["total"], 400)
        self.assertEqual(sc["walked"], 400)
        self.assertTrue(sc["complete"])
        nxt = sc["next_cursor"]
        self.assertEqual(nxt, sc["count"])
        self.assertIn(f"cursor={nxt}", sc["note"])
        self.assertEqual(self.calls[0], ("ui.tree", {"max": 400}, None))

        seen = [e["id"] for e in sc["elements"]]
        pages = 1
        while nxt is not None:
            res = await self.call_wrapped("ios_ui_tree", max_elements=400, cursor=nxt)
            sc = res.structured_content
            self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
            self.assertTrue(sc["cached"])
            self.assertEqual(sc["offset"], nxt)
            seen += [e["id"] for e in sc["elements"]]
            nxt = sc.get("next_cursor")
            pages += 1
            self.assertLess(pages, 40)
        self.assertEqual(seen, [f"e{i + 1}" for i in range(400)])      # no gaps, no repeats
        self.assertEqual(len(self.calls), 1)                             # one walk for every page
        self.assertNotIn("truncated", sc)                                # last page, complete walk

    async def test_max_bytes_raises_the_page(self):
        self.replies["ui.tree"] = big_tree(400)
        small = (await self.call("ios_ui_tree")).elements
        big = (await self.call("ios_ui_tree", max_bytes=40_000)).elements
        self.assertGreater(len(big), len(small))

    async def test_filter_and_new_walk_without_cursor(self):
        self.replies["ui.tree"] = big_tree(60)
        m = await self.call("ios_ui_tree", bundle_id="com.example.app", interactive_only=True, filter="row 1")
        self.assertEqual([e.id for e in m.elements], ["e11", "e12", "e13", "e15", "e17", "e18", "e19"])
        self.assertEqual(m.total, 7)   # rows 10-19 minus staticText/image (13, 15, 19); row 1 is staticText
        self.assertIsNone(m.next_cursor)
        self.assertFalse(m.truncated)
        await self.call("ios_ui_tree", bundle_id="com.example.app")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][1], {"max": 150, "bundleId": "com.example.app"})

    async def test_incomplete_walk_is_flagged(self):
        t = big_tree(10, complete=False)
        self.replies["ui.tree"] = t
        m = await self.call("ios_ui_tree")
        self.assertFalse(m.complete)
        self.assertTrue(m.truncated)
        self.assertIn("timed out", m.note)
        t2 = big_tree(150)
        t2["truncated"] = True
        self.replies["ui.tree"] = t2
        m = await self.call("ios_ui_tree")
        self.assertFalse(m.complete)
        self.assertIn("max_elements=150", m.note)

    async def test_stopped_by_and_bundle_echo(self):
        for why, word in (("timeout", "timed out"), ("no-focus", "focus couldn't move"), ("max", "max_elements"),
                          ("foreign", "left the app")):
            with self.subTest(why=why):
                t = big_tree(8)
                t.update(stoppedBy=why, bundleId="com.example.app", screen=None)
                self.replies["ui.tree"] = t
                m = await self.call("ios_ui_tree", bundle_id="com.example.app")
                self.assertFalse(m.complete)
                self.assertTrue(m.truncated)
                self.assertIn(word, m.note)
                self.assertEqual((m.stopped_by, m.bundle_id, m.screen), (why, "com.example.app", None))
        t = big_tree(8)
        t["stoppedBy"] = "loop"
        self.replies["ui.tree"] = t
        m = await self.call("ios_ui_tree")
        self.assertTrue(m.complete)
        self.assertIsNone(m.note)
        self.assertNotIn("raw", self.calls[-1][1])

    async def test_real_phone_shape_no_frames(self):
        t = big_tree(5)
        for e in t["elements"]:
            e["frame"] = None
        t.update(screen=None, bundleId="XTL-T.dev.iosstudio.uiprobe", pid=4242, otherProcessElements=3,
                 stoppedBy="loop")
        self.replies["ui.tree"] = t
        res = await self.call_wrapped("ios_ui_tree")
        sc = res.structured_content
        self.assertTrue(all("frame" not in e for e in sc["elements"]))
        self.assertNotIn("screen", sc)
        self.assertEqual((sc["bundle_id"], sc["pid"], sc["other_process_elements"]),
                         ("XTL-T.dev.iosstudio.uiprobe", 4242, 3))
        self.assertTrue(sc["complete"])

    async def test_empty_screen_and_no_match(self):
        self.replies["ui.tree"] = {"elements": [], "complete": True, "screen": None}
        m = await self.call("ios_ui_tree")
        self.assertEqual(m.count, 0)
        self.assertIn("unlocked", m.next_steps[0])
        self.replies["ui.tree"] = big_tree(5)
        m = await self.call("ios_ui_tree", filter="nothing like this")
        self.assertIn("Nothing matched", m.next_steps[0])

    async def test_cursor_past_end(self):
        self.replies["ui.tree"] = big_tree(5)
        with self.assertRaises(ToolError):
            await self.call("ios_ui_tree", cursor=50)


class Press(Base):
    REPLY = {"pressed": {"id": "e4", "role": "button", "label": "Sign in", "frame": {"x": 0, "y": 0, "w": 10, "h": 10}},
             "changes": {"added": ["Welcome", "Log out"], "removed": ["Sign in"],
                         "changedValues": [{"label": "Status", "from": "Idle", "to": "Busy"}]},
             "after": {"count": 12}}

    async def test_by_id(self):
        self.replies["ui.press"] = self.REPLY
        r = await self.call("ios_ui_press", element_id="e4", bundle_id="com.example.app")
        self.assertEqual(self.calls[0][:2], ("ui.press", {"id": "e4", "bundleId": "com.example.app"}))
        self.assertEqual(r.pressed.label, "Sign in")
        self.assertEqual(r.added, ["Welcome", "Log out"])
        self.assertEqual(r.removed, ["Sign in"])
        self.assertEqual((r.changed_values[0].before, r.changed_values[0].after), ("Idle", "Busy"))
        self.assertEqual(r.elements_after, 12)
        self.assertEqual((r.bundle_id, r.pid), ("com.example.app", None))   # echo falls back to the argument
        self.assertTrue(r.screen_changed)

    async def test_by_label_and_it_forgets_the_walk(self):
        self.replies["ui.tree"] = big_tree(400)
        await self.call("ios_ui_tree")
        self.replies["ui.press"] = {"pressed": {"id": "e9", "label": "Next"}, "changes": {}, "after": {"count": 3}}
        r = await self.call("ios_ui_press", label="next", match="contains")
        self.assertEqual(self.calls[-1][1], {"label": "next", "match": "contains"})
        self.assertFalse(r.screen_changed)
        self.assertIn("Nothing visible changed", r.next_steps[0])
        m = await self.call("ios_ui_tree", cursor=10)               # cache dropped: a new walk
        self.assertFalse(m.cached)
        self.assertEqual(sum(1 for c in self.calls if c[0] == "ui.tree"), 2)

    async def test_identifier_as_change_label_and_app_echo(self):
        self.replies["ui.press"] = {"pressed": {"id": "e2", "role": "button", "label": "Increment",
                                                "identifier": "incrementButton", "frame": None},
                                    "changes": {"changedValues": [{"label": "countLabel", "from": "Count: 0",
                                                                   "to": "Count: 1"}]},
                                    "after": {"count": 4, "complete": True}, "bundleId": "XTL-T.dev.x", "pid": 77}
        res = await self.call_wrapped("ios_ui_press", label="incrementButton")
        sc = res.structured_content
        self.assertEqual(sc["changed_values"], [{"label": "countLabel", "before": "Count: 0", "after": "Count: 1"}])
        self.assertNotIn("frame", sc["pressed"])
        self.assertEqual((sc["bundle_id"], sc["pid"]), ("XTL-T.dev.x", 77))
        self.assertTrue(sc["screen_changed"])

    async def test_partial_walk_after_press(self):
        self.replies["ui.press"] = {"pressed": {"id": "e1", "label": "Add habit"},
                                    "changes": {"added": ["New habit 4"]}, "after": {"count": 80, "complete": False},
                                    "elapsedMs": 1100}
        r = await self.call("ios_ui_press", element_id="e1")
        self.assertIs(r.after_complete, False)
        self.assertEqual(r.elapsed_ms, 1100)
        self.assertTrue(r.truncated)
        self.assertIn("stopped early", r.note)

    async def test_exactly_one_target(self):
        for kw in ({}, {"element_id": "e1", "label": "x"}, {"element_id": " "}):
            with self.assertRaises(ToolError):
                await self.call("ios_ui_press", **kw)
        self.assertEqual(self.calls, [])

    async def test_change_lists_are_capped(self):
        self.replies["ui.press"] = {"pressed": {"id": "e1"}, "changes": {"added": [f"L{i}" for i in range(300)]}}
        res = await self.call_wrapped("ios_ui_press", element_id="e1")
        sc = res.structured_content
        self.assertEqual(len(sc["added"]), T.CHANGES_CAP)
        self.assertEqual(sc["omitted"], 300 - T.CHANGES_CAP)
        self.assertTrue(sc["truncated"])
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_error_mapping(self):
        cases = {
            "no-element": (DeviceError("no-element", "No element labelled 'Sgin in'.",
                                       detail=["Sign in", "Sign up"]), ["Sign in; Sign up", "ios_ui_tree"]),
            "ambiguous-label": (DeviceError("ambiguous-label", "2 elements are labelled 'OK'.",
                                            detail="e3 'OK', e9 'OK'"), ["e3 'OK', e9 'OK'", "element_id"]),
            "stale-element": (DeviceError("stale-element", "e4 is gone."), ["Call ios_ui_tree again"]),
            "wrong-app": (DeviceError("wrong-app", "com.apple.springboard is in front."),
                          ["ios_app_launch(bundle_id) first"]),
            "no-app-in-front": (DeviceError("no-app-in-front", "No app is in front."),
                                ["ios_app_launch(bundle_id) first", "pass bundle_id"]),
            "press-failed": (DeviceError("press-failed", "The press didn't take."), ["parent cell"]),
            "bad-args": (DeviceError("bad-args", "Which element?"), ["exactly one of element_id"]),
            "ax-unavailable": (DeviceError("ax-unavailable", "Accessibility service unavailable."),
                               ["ios_device_prepare", "ios_device_reconnect"]),
        }
        for code, (err, wants) in cases.items():
            with self.subTest(code=code):
                self.replies["ui.press"] = err
                with self.assertRaises(ToolError) as cm:
                    await self.call_wrapped("ios_ui_press", label="Sgin in", bundle_id="com.example.app")
                msg = str(cm.exception)
                self.assertIn(f"[{code}]", msg)
                for w in wants:
                    self.assertIn(w, msg)


class Audit(Base):
    async def test_grouping_cap_and_unsupported(self):
        issues = []
        for i in range(120):
            t = ["contrast", "hit-region", "clipped-text"][i % 3] if i < 90 else "missing-label"
            issues.append({"type": t, "rawType": "testTypeContrast" if t == "contrast" else t,
                           "message": f"Issue {i}: " + "the text is hard to read " * 3,
                           "element": {"label": f"Label {i}", "frame": {"x": 1, "y": 2, "w": 3, "h": 4}} if i % 5 else None,
                           "details": {"ratio": 2.1} if t == "contrast" else {}})
        self.replies["ui.audit"] = {"issues": issues, "supported": ["contrast", "hit-region", "clipped-text",
                                                                   "missing-label"],
                                    "unsupported": ["dynamic-type"], "elapsedMs": 3100, "pid": 812}
        res = await self.call_wrapped("ios_a11y_audit", bundle_id="com.example.app", max_issues=50)
        sc = res.structured_content
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        self.assertEqual(sc["total"], 120)
        self.assertEqual(sum(sc["counts"].values()), 120)
        self.assertEqual(list(sc["counts"])[0], "contrast")         # 30 each; ties keep first-seen order
        self.assertEqual(sc["counts"]["missing-label"], 30)
        self.assertLessEqual(sc["count"], 50)
        self.assertEqual(sc["count"], len(sc["issues"]))
        self.assertTrue(sc["truncated"])
        self.assertIn("counts cover all", sc["note"])
        self.assertEqual(sc["unsupported"], ["dynamic-type"])
        self.assertEqual((sc["bundle_id"], sc["pid"]), ("com.example.app", 812))
        self.assertTrue(any("dynamic-type" in s and "informational" in s for s in sc["next_steps"]))
        first = sc["issues"][0]
        self.assertEqual(first["type"], "contrast")
        self.assertEqual(first["raw_type"], "testTypeContrast")
        self.assertEqual(first["details"], {"ratio": 2.1})
        self.assertNotIn("label", first)                              # issue 0 has no element
        self.assertEqual(sc["issues"][1]["label"], "Label 3")
        self.assertEqual(sc["issues"][1]["frame"], [1, 2, 3, 4])
        types = [i["type"] for i in sc["issues"]]
        self.assertEqual(types, sorted(types, key=list(sc["counts"]).index))   # grouped

    async def test_types_are_passed_and_clean_screen(self):
        self.replies["ui.audit"] = {"issues": [], "supported": ["contrast"], "unsupported": []}
        m = await self.call("ios_a11y_audit", types=[" contrast ", ""])
        self.assertEqual(self.calls[0][1], {"types": ["contrast"]})
        self.assertEqual((m.total, m.count, m.counts), (0, 0, {}))
        self.assertIn("No issues", m.next_steps[0])
        self.assertFalse(m.truncated)

    async def test_timed_out_is_partial(self):
        self.replies["ui.audit"] = {"issues": [{"type": "contrast", "message": "low", "details": {"elementId": "e5",
                                                                                                "code": 12}}],
                                    "supported": ["contrast"], "unsupported": [], "timedOut": True}
        m = await self.call("ios_a11y_audit")
        self.assertIs(m.timed_out, True)
        self.assertIsNone(m.pid)
        self.assertTrue(m.truncated)
        self.assertIn("partial", m.note)
        self.assertEqual(m.issues[0].details, {"elementId": "e5", "code": 12})
        self.replies["ui.audit"] = {"issues": [], "timedOut": True}
        m = await self.call("ios_a11y_audit")
        self.assertFalse(any("No issues" in x for x in m.next_steps))

    async def test_ax_unavailable_on_every_command(self):
        for cmd, name, kw in (("ui.tree", "ios_ui_tree", {}), ("ui.press", "ios_ui_press", {"label": "OK"}),
                              ("ui.audit", "ios_a11y_audit", {})):
            self.replies[cmd] = DeviceError("ax-unavailable", "Accessibility service unavailable.")
            with self.subTest(cmd=cmd), self.assertRaises(ToolError) as cm:
                await self.call(name, **kw)
            self.assertIn("[ax-unavailable]", str(cm.exception))

    async def test_wrong_app(self):
        self.replies["ui.audit"] = DeviceError("wrong-app", "Another app is in front.")
        with self.assertRaises(ToolError) as cm:
            await self.call("ios_a11y_audit", bundle_id="com.example.app")
        self.assertIn("ios_app_launch", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
