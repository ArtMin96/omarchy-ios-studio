"""The whole tool registry (every tool the server registers), prompts.py and resources.py.

Registry: every tool has a short description, a `kind` (all four annotation hints set, in a combination
core/registry.py KINDS allows), no published outputSchema, destructive tools take `confirm`, job ids are only
ever called `job_id`, and tools/list stays under its byte ceiling.
Prompts and resources: listed with their arguments, rendered with the caller's values; iosstudio://guide and
iosstudio://jobs/{job_id}/log (bounded, unknown job -> ResourceError).

    python -m pytest tests/mcp/test_mcp_cov_registry.py
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ResourceError

from iosstudio_mcp import prompts, resources
from iosstudio_mcp.core.registry import KINDS
from iosstudio_mcp.state import AppState

LIST_CEILING = 110 * 1024
DESCRIPTION_MAX = 400
# Parameters named *_id that are not job ids (a job id is always `job_id`).
OTHER_IDS = {"bundle_id", "prompt_id", "session_id", "element_id"}


def _server():
    with mock.patch.dict(os.environ, {"IOSSTUDIO_DEMO": "1"}):
        from iosstudio_mcp import server
        return server


class Registry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _server()
        cls.tools = cls.server.mcp._tool_manager.list_tools()
        cls.listed = asyncio.run(cls.server.mcp.list_tools())
        cls.wire = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in cls.listed]

    def test_every_tool_is_listed(self):
        self.assertEqual({t.name for t in self.tools}, {t["name"] for t in self.wire})
        self.assertGreaterEqual(len(self.tools), 100)

    def test_descriptions_fit(self):
        for t in self.wire:
            self.assertTrue(t.get("description"), t["name"])
            self.assertLessEqual(len(t["description"]), DESCRIPTION_MAX, t["name"])

    def test_every_tool_has_a_kind(self):
        pairs = {(ro, de) for ro, de, _, _ in KINDS.values()}
        for t in self.tools:
            a = t.annotations
            hints = (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint)
            self.assertNotIn(None, hints, t.name)
            self.assertIn((a.read_only_hint, a.destructive_hint), pairs, t.name)
            self.assertTrue(a.title, t.name)

    def test_destructive_tools_take_confirm_and_read_tools_dont(self):
        for t in self.tools:
            props = t.parameters.get("properties", {})
            if t.annotations.destructive_hint:
                self.assertIn("confirm", props, t.name)
                self.assertIs(props["confirm"].get("default"), False, t.name)
            if t.annotations.read_only_hint:
                self.assertNotIn("confirm", props, t.name)

    def test_no_output_schema_is_published(self):
        self.assertFalse([t["name"] for t in self.wire if "outputSchema" in t])

    def test_job_ids_are_called_job_id(self):
        for t in self.wire:
            for p in t["inputSchema"].get("properties", {}):
                if p.endswith(("_id", "_ids")) and p != "job_id":
                    self.assertIn(p, OTHER_IDS, f"{t['name']}.{p}")

    def test_tools_list_fits_its_ceiling(self):
        size = len(json.dumps({"tools": self.wire}, separators=(",", ":"), ensure_ascii=False).encode())
        self.assertLessEqual(size, LIST_CEILING)


class Prompts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mcp = _server().mcp

    async def test_listed_with_their_arguments(self):
        listed = {p.name: p for p in await self.mcp.list_prompts()}
        self.assertEqual(set(listed), {"fix_build_errors", "diagnose_crash", "new_app", "get_ready", "verify_change"})
        self.assertEqual([(a.name, a.required) for a in listed["fix_build_errors"].arguments], [("project", True)])
        self.assertEqual({a.name: a.required for a in listed["new_app"].arguments}, {"name": True, "template": False})
        self.assertFalse(listed["get_ready"].arguments)
        for p in listed.values():
            self.assertTrue(p.title and p.description, p.name)

    async def test_rendered_with_the_callers_values(self):
        res = await self.mcp.get_prompt("fix_build_errors", {"project": "WeatherGlance"})
        text = res.messages[0].content.text
        self.assertIn('ios_build_start(project_path="WeatherGlance", action="compile")', text)
        res = await self.mcp.get_prompt("diagnose_crash", {"bundle_id": "com.ex.App"})
        self.assertIn('ios_crash_list(bundle_id="com.ex.App")', res.messages[0].content.text)
        res = await self.mcp.get_prompt("new_app", {"name": "Glance"})
        self.assertIn('template="swiftui-app"', res.messages[0].content.text)
        res = await self.mcp.get_prompt("get_ready", {})
        self.assertIn("ios_status", res.messages[0].content.text)

    async def test_every_tool_a_prompt_names_exists(self):
        import re
        names = {t.name for t in self.mcp._tool_manager.list_tools()}
        for fn in ("fix_build_errors", "diagnose_crash", "new_app", "get_ready"):
            args = {"project": "X", "bundle_id": "b", "name": "N"}
            p = await self.mcp.get_prompt(fn, {k: v for k, v in args.items() if k in _params(fn)})
            missing = set(re.findall(r"\bios_[a-z_]+[a-z]", p.messages[0].content.text)) - names
            self.assertFalse(missing, fn)

    async def test_a_missing_argument_is_refused(self):
        with self.assertRaises(Exception):
            await self.mcp.get_prompt("fix_build_errors", {})


def _params(fn: str) -> set[str]:
    return {"fix_build_errors": {"project"}, "diagnose_crash": {"bundle_id"}, "new_app": {"name"}}.get(fn, set())


class Recorder:
    def __init__(self):
        self.resources, self.prompts = {}, {}

    def resource(self, uri, **kw):
        def deco(fn):
            self.resources[uri] = fn
            return fn
        return deco

    def prompt(self, **kw):
        def deco(fn):
            self.prompts[fn.__name__] = fn
            return fn
        return deco


class Resources(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rec = Recorder()
        resources.register(self.rec)
        prompts.register(self.rec)
        self.state = AppState()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.state))

    async def asyncTearDown(self):
        await self.state.jobs.shutdown()

    async def test_listed(self):
        mcp = _server().mcp
        uris = {str(r.uri) for r in await mcp.list_resources()}
        self.assertIn("iosstudio://guide", uris)
        templates = {t.uri_template for t in await mcp.list_resource_templates()}
        self.assertIn("iosstudio://jobs/{job_id}/log", templates)

    async def test_the_guide(self):
        text = await self.rec.resources["iosstudio://guide"]()
        self.assertEqual(text, resources.GUIDE.read_text())
        self.assertLessEqual(len(text.encode()), resources.MAX_TEXT + 200)

    async def test_a_missing_guide_is_a_resource_error(self):
        with mock.patch.object(resources, "GUIDE", resources.GUIDE.with_name("nope.md")):
            with self.assertRaises(ResourceError):
                await self.rec.resources["iosstudio://guide"]()

    async def test_a_huge_guide_is_cut_with_a_pointer(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            big = Path(d) / "guide.md"
            big.write_text("line\n" * 30_000)
            with mock.patch.object(resources, "GUIDE", big):
                text = await self.rec.resources["iosstudio://guide"]()
        self.assertLessEqual(len(text.encode()), resources.MAX_TEXT + 200)
        self.assertIn("the whole guide is", text)

    async def test_a_jobs_log(self):
        async def work(job):
            for i in range(3):
                job.add_line(f"line {i}")
            return {}
        job = self.state.jobs.run(work, "t", title="t")
        await self.state.jobs.wait(job, 2)
        text = await self.rec.resources["iosstudio://jobs/{job_id}/log"](job.id, self.ctx)
        self.assertEqual(text, "line 0\nline 1\nline 2")

    async def test_a_long_log_keeps_its_tail_and_says_how_to_read_more(self):
        async def work(job):
            for i in range(5000):
                job.add_line(f"{i:05d} " + "x" * 60)
            return {}
        job = self.state.jobs.run(work, "t", title="t")
        await self.state.jobs.wait(job, 2)
        text = await self.rec.resources["iosstudio://jobs/{job_id}/log"](job.id, self.ctx)
        self.assertLessEqual(len(text.encode()), resources.MAX_TEXT + 200)
        self.assertIn(f"ios_build_log(job_id='{job.id}'", text.splitlines()[0])
        self.assertTrue(text.endswith(job.lines[-1]))

    async def test_an_unknown_job_is_a_resource_error(self):
        with self.assertRaises(ResourceError) as cm:
            await self.rec.resources["iosstudio://jobs/{job_id}/log"]("b-999", self.ctx)
        self.assertIn("b-999", str(cm.exception))
