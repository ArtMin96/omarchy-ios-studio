"""A small tools/list (mcp/iosstudio_mcp/core/registry.py slim_schemas, server.py INSTRUCTIONS, the
iosstudio://guide resource): every tool's description ≤ 400 characters on one line, input schemas without
noise, no published outputSchema (results are still validated against the tool's model on the server), short
server instructions that point to the guide, and the whole tools/list under its byte budget.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_slim.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import unittest
from unittest import mock

from iosstudio_mcp.core.registry import _slim

LIST_BUDGET = 105_000       # bytes of tools/list (110 tools; it was 277 KB before slimming)
DESCRIPTION_MAX = 400


def _listed():
    with mock.patch.dict(os.environ, {"IOSSTUDIO_DEMO": "1"}):
        from iosstudio_mcp import server
        return server, asyncio.run(server.mcp.list_tools())


class ToolsList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.tools = _listed()
        cls.wire = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in cls.tools]

    def test_the_whole_list_fits_its_budget(self):
        size = len(json.dumps({"tools": self.wire}, separators=(",", ":"), ensure_ascii=False).encode())
        self.assertLessEqual(size, LIST_BUDGET)

    def test_descriptions_are_short_and_on_one_line(self):
        for t in self.wire:
            d = t.get("description", "")
            self.assertTrue(d, t["name"])
            self.assertLessEqual(len(d), DESCRIPTION_MAX, t["name"])
            self.assertNotIn("\n", d, t["name"])
            self.assertNotIn("  ", d, t["name"])

    def test_no_output_schema_is_published_but_results_are_still_checked(self):
        self.assertFalse([t["name"] for t in self.wire if "outputSchema" in t])
        checked = [t for t in self.server.mcp._tool_manager.list_tools() if t.fn_metadata.output_schema]
        self.assertGreater(len(checked), 90)       # convert_result() validates structuredContent against these

    def test_input_schemas_have_no_titles_or_null_noise(self):
        text = json.dumps([t["inputSchema"] for t in self.wire])
        self.assertNotIn('"title"', text)
        self.assertNotIn('"default": null', text)
        props = {t["name"]: t["inputSchema"].get("properties", {}) for t in self.wire}
        self.assertEqual(props["ios_device_info"]["udid"]["type"], "string")    # optional: no null advertised

    def test_instructions_are_short_and_point_to_the_guide(self):
        lines = self.server.INSTRUCTIONS.strip().splitlines()
        self.assertLessEqual(len(lines), 15)
        self.assertIn("iosstudio://guide", self.server.INSTRUCTIONS)

    def test_every_tool_the_guide_and_instructions_name_exists(self):
        names = {t["name"] for t in self.wire}
        guide = self.server.resources.GUIDE.read_text()
        for text in (guide, self.server.INSTRUCTIONS):
            missing = set(re.findall(r"\bios_[a-z_]+[a-z]", text)) - names
            self.assertFalse(missing)

    def test_the_guide_is_a_resource(self):
        res = asyncio.run(self.server.mcp.list_resources())
        self.assertIn("iosstudio://guide", {str(r.uri) for r in res})


class Slim(unittest.TestCase):
    def test_optional_null_is_dropped_required_kept(self):
        schema = {"type": "object", "title": "X", "required": ["a"],
                  "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "A"},
                                 "b": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None,
                                       "description": "Two\n      lines"}}}
        out = _slim(schema)
        self.assertEqual(out["properties"]["a"], {"anyOf": [{"type": "string"}, {"type": "null"}]})
        self.assertEqual(out["properties"]["b"], {"description": "Two lines", "type": "integer"})
        self.assertNotIn("title", out)

    def test_refs_and_defaults_stay(self):
        schema = {"properties": {"p": {"anyOf": [{"$ref": "#/$defs/P"}, {"type": "null"}], "default": None},
                                 "n": {"type": "integer", "default": 20}},
                  "$defs": {"P": {"title": "P", "properties": {"x": {"type": "string", "title": "X"}}}}}
        out = _slim(schema)
        self.assertEqual(out["properties"]["p"], {"$ref": "#/$defs/P"})
        self.assertEqual(out["properties"]["n"]["default"], 20)
        self.assertEqual(out["$defs"]["P"], {"properties": {"x": {"type": "string"}}})


if __name__ == "__main__":
    unittest.main()
