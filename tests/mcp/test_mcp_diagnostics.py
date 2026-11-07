"""MCP side of build diagnostics: grouping, the output models and next steps.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_diagnostics.py
"""
from __future__ import annotations

import json
import unittest

from iosstudio.features import diagnostics as D
from iosstudio_mcp.core import budget as B
from iosstudio_mcp.core.job_status import job_next_steps
from iosstudio_mcp.core.jobs import Job
from iosstudio_mcp.models import DiagGroup, DiagnosticsPage
from support import ROOT

FIXTURES = ROOT / "tests" / "fixtures" / "build-output"

PROJECT = "/home/user/Developer/LinkErr"


def parsed(name: str) -> list[dict]:
    return D.parse((FIXTURES / name).read_text().splitlines(), demangler=None)


class Grouping(unittest.TestCase):
    def test_old_history_entries_still_group(self):
        old = [{"severity": "error", "file": PROJECT + "/Sources/A.swift", "line": 3, "col": 1, "message": "boom 'x'"},
               {"severity": "error", "file": PROJECT + "/Sources/B.swift", "line": 9, "col": 2, "message": "boom 'y'"}]
        (g,) = B.group_diags(old, PROJECT)
        self.assertEqual(g["category"], "compiler")
        self.assertEqual(g["count"], 2)
        self.assertEqual(g["locations"], ["Sources/A.swift:3:1", "Sources/B.swift:9:2"])
        self.assertNotIn("hint", g)
        row = B.dump(DiagGroup(**{k: v for k, v in g.items() if k != "category"}))
        self.assertFalse({"hint", "details", "category"} & row.keys())   # nothing new on an old-style row

    def test_linker_groups_keep_hint_and_details(self):
        groups = B.group_diags(parsed("real-linker-lld.txt"), PROJECT)
        self.assertEqual([g["category"] for g in groups], ["linker", "linker"])
        self.assertEqual(groups[0]["locations"], ["Sources/LinkErr/App.swift:5"])
        self.assertEqual(groups[1]["locations"], [])
        self.assertEqual(groups[1]["more_locations"], 0)
        self.assertTrue(groups[0]["hint"] and groups[0]["details"])

    def test_same_message_in_two_categories_stays_apart(self):
        ds = [{"severity": "error", "message": "x", "category": "linker"},
              {"severity": "error", "message": "x", "category": "package"}]
        self.assertEqual(len(B.group_diags(ds)), 2)

    def test_page_fits_the_budget(self):
        many = []
        for name in sorted(FIXTURES.glob("*.txt")):
            many += parsed(name.name)
        many = many * 20
        groups = [DiagGroup(**g) for g in B.group_diags(many, PROJECT)]
        page = DiagnosticsPage(source="test", total=len(groups), count=len(groups), offset=0, has_more=False,
                               errors=len(many), warnings=0, diagnostics=groups).with_budget(None)
        size = len(json.dumps(B.dump(page), ensure_ascii=False).encode())
        self.assertLess(size, B.DEFAULT_BYTES)


class NextSteps(unittest.TestCase):
    def job(self, diags: list[dict]) -> Job:
        j = Job(id="b-1", kind="build", action="compile", title="t", project_path=PROJECT)
        j.state = "failed"
        j.error = {"code": "build-failed", "message": "The build failed."}
        j.diagnostics = diags
        return j

    def test_linker_failure_points_at_diagnostics_not_the_log(self):
        steps = job_next_steps(self.job(parsed("real-linker-lld.txt")))
        self.assertTrue(any(s.startswith("Linker error(s)") for s in steps))
        self.assertFalse(any("last resort" in s for s in steps))

    def test_no_diagnostics_is_the_last_resort(self):
        steps = job_next_steps(self.job([]))
        self.assertTrue(any("last resort" in s for s in steps))


if __name__ == "__main__":
    unittest.main()
