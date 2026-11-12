"""tools/hot_reload.py, PR #46 review fixes: what a cut or failed result says.

- ios_hot_reload_set(enabled=false) that fails after the session was stopped says so (not "nothing was changed").
- last_error counts its blockers / compiler errors (blockersTotal / diagnosticsTotal from the helper, or the lists)
  and a cut list sets truncated with a note on where the rest is.
- ios_hot_reload_status(history=HISTORY_MAX) with failed reloads fits the budget without cutting the history
  (20 failed items with 200-char messages next to a failed last_error don't fit 12 KB at any useful clip).
- ios_hot_reload_control(action='stop') errors carry the feature's next step.
- Name-like fields from the helper (blocker type/name, step keys) are clipped.
- A stop outlasts the session's whole shutdown (SHUTDOWN_BUDGET, 9 s) before SIGKILL, and a cancel whose caller
  gives up early (jobs.cancel waits 10 s for on_cancel) keeps the one stop going instead of starting another.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_hot_reload_bounds.py
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from test_mcp_hot_reload import PROJ, SRC, TPROJ, HotReloadCase, snapshot

from iosstudio_mcp.tools import hot_reload as HR
from support.mcp_harness import size_of


def reload_error(blockers: int, diagnostics: int, **extra) -> dict:
    return {"code": "compile-failed", "seq": 7, "message": "The change doesn't compile.",
            "blockers": [{"kind": "stored-property", "type": "ContentView", "name": f"p{i}", "file": SRC, "line": i,
                          "message": f"Adds the stored property p{i}"} for i in range(1, blockers + 1)],
            "diagnostics": [{"severity": "error", "file": f"{PROJ}/{SRC}", "line": i, "col": 2,
                             "message": f"error {i}"} for i in range(1, diagnostics + 1)], **extra}


class SetOffFailure(HotReloadCase):
    async def test_a_stopped_session_is_named_when_package_swift_is_left(self):
        self.helper.fail("hotreload", "disable", "manifest-invalid", "Package.swift wouldn't work after the change.",
                         sessionStopped=True)
        e = await self.error(HR, "ios_hot_reload_set", project_path="Weather", enabled=False)
        self.assertIn("[manifest-invalid]", e)
        self.assertNotIn("Nothing was changed", e)
        nxt = e.split(" Next: ", 1)[1]
        self.assertTrue(nxt.startswith("The session was stopped, but Package.swift was left as it is."))
        self.assertIn("fix its errors", nxt)
        self.assertIn(f"ios_hot_reload_start(project_path='{TPROJ}')", nxt)

    async def test_this_servers_session_counts_as_stopped(self):
        started = await self.data(HR, "ios_hot_reload_start", project_path="Weather")
        self.assertEqual(started["state"], "watching")
        self.helper.fail("hotreload", "disable", "block-damaged", "The hot reload lines were changed by hand.")
        e = await self.error(HR, "ios_hot_reload_set", project_path="Weather", enabled=False)
        self.assertIn("[block-damaged]", e)
        self.assertIn("session was stopped, but Package.swift was left as it is. Ask the user to restore", e)
        self.assertTrue(e.endswith(f"ios_hot_reload_start(project_path='{TPROJ}') starts it again."), e)
        self.assertEqual(self.state.jobs.get(started["job_id"]).state, "cancelled")

    async def test_no_session_no_claim(self):
        self.helper.fail("hotreload", "disable", "package-busy", "Another change is running.", sessionStopped=False)
        e = await self.error(HR, "ios_hot_reload_set", project_path="Weather", enabled=False)
        self.assertNotIn("session was stopped", e)
        self.assertIn("wait a minute", e)
        self.helper.fail("hotreload", "enable", "manifest-invalid", "Broken.")
        e = await self.error(HR, "ios_hot_reload_set", project_path="Weather", enabled=True)
        self.assertNotIn("session was stopped", e)
        self.assertIn("Package.swift was left as it is", e)


class ErrorTotals(HotReloadCase):
    async def status(self, err: dict) -> dict:
        self.helper.reply("hotreload", "status", {"project": PROJ, "enabled": True, "mode": "on",
                                                  "session": snapshot("failed", lastError=err, patchSeq=7)})
        return await self.data(HR, "ios_hot_reload_status", project_path="Weather")

    async def test_helper_totals_mark_the_cut(self):
        d = await self.status(reload_error(10, 5, blockersTotal=23, diagnosticsTotal=8))
        e = d["last_error"]
        self.assertEqual((len(e["blockers"]), e["blockers_total"]), (10, 23))
        self.assertEqual((len(e["diagnostics"]), e["diagnostics_total"]), (5, 8))
        self.assertTrue(d["truncated"])
        self.assertIn("10 of 23 blockers and 5 of 8 compiler errors", d["note"])
        self.assertIn("save again", d["note"])

    async def test_a_build_failure_points_at_build_diagnostics(self):
        d = await self.status(reload_error(0, 5, code="build-failed", diagnosticsTotal=40, logFile="/tmp/b.log"))
        self.assertEqual(d["last_error"]["diagnostics_total"], 40)
        self.assertIn(f"ios_build_diagnostics(project_path='{TPROJ}')", d["note"])

    async def test_without_totals_the_lists_count(self):
        d = await self.status(reload_error(3, 2))
        e = d["last_error"]
        self.assertEqual((e["blockers_total"], e["diagnostics_total"]), (3, 2))
        self.assertNotIn("truncated", d)
        self.assertNotIn("note", d)
        d = await self.status(reload_error(14, 0))           # more than the tool lists, no total: still counted
        self.assertEqual((len(d["last_error"]["blockers"]), d["last_error"]["blockers_total"]), (10, 14))
        self.assertNotIn("diagnostics_total", d["last_error"])
        self.assertTrue(d["truncated"])


class FullHistory(HotReloadCase):
    async def test_the_most_history_fits_without_a_cut(self):
        """20 failed reloads with 200-char messages and a failed last reload: history=HISTORY_MAX lists the newest
        HISTORY_MAX whole (the bound is what fits next to last_error and next_steps), more is refused."""
        hist = [{"seq": i, "ok": False, "time": 1_790_000_000 + i, "files": [SRC] * 8, "ms": 2000 + i,
                 "code": "compile-failed", "message": f"{i:03d} " + "m" * 196} for i in range(1, 21)]
        err = {**reload_error(1, 5), "seq": 20, "message": "x" * 300}
        for g in err["diagnostics"]:
            g["message"] = "z" * 200
        self.helper.reply("hotreload", "status", {"project": PROJ, "enabled": True, "mode": "on",
                                                  "session": snapshot("failed", lastError=err, patchSeq=20,
                                                                      history=hist)})
        res = await self.call(HR, "ios_hot_reload_status", project_path="Weather", history=HR.HISTORY_MAX)
        self.assertLessEqual(size_of(res), 12_000)
        d = res.structured_content
        self.assertNotIn("truncated", d, d.get("note"))
        self.assertEqual([h["seq"] for h in d["history"]], list(range(20, 20 - HR.HISTORY_MAX, -1)))
        self.assertEqual(d["history_total"], 20)
        self.assertTrue(all(h["message"].startswith(f"{h['seq']:03d} ") for h in d["history"]))
        self.assertEqual(len(d["last_error"]["diagnostics"]), 5)
        bound = self.fn(HR, "ios_hot_reload_status").__signature__.parameters["history"].annotation.__metadata__
        self.assertIn(HR.HISTORY_MAX, [m.le for f in bound for m in getattr(f, "metadata", []) if hasattr(m, "le")])


class ControlStop(HotReloadCase):
    async def test_a_failed_stop_names_the_next_step(self):
        self.helper.fail("hotreload", "stop", "busy", "Hot reload of this project is being changed.")
        e = await self.error(HR, "ios_hot_reload_control", project_path="Weather", action="stop")
        self.assertIn("[busy]", e)
        self.assertIn(f"ios_hot_reload_status(project_path='{TPROJ}')", e)


class Clips(unittest.TestCase):
    def test_name_like_fields_are_clipped(self):
        err = HR._error({"code": "c" * 500, "message": "m", "blockers": [
            {"kind": "k" * 500, "type": "T" * 500, "name": "n" * 500, "message": "x"}],
            "diagnostics": [{"severity": "s" * 500, "message": "d"}]}, PROJ)
        b = err.blockers[0]
        for v in (err.code, b.kind, b.type, b.name, err.diagnostics[0].severity):
            self.assertLessEqual(len(v), 100)
        r = HR._reload({"seq": 1, "steps": {("s" * 300 + str(i)): i for i in range(40)}})
        self.assertLessEqual(len(r.steps), HR.STEPS)
        self.assertTrue(all(len(k) <= 30 for k in r.steps))


class StopGrace(unittest.IsolatedAsyncioTestCase):
    def test_grace_outlasts_the_session_shutdown(self):
        self.assertGreaterEqual(HR.STOP_GRACE + HR.TERM_GRACE, 9.0 + 1)     # session.SHUTDOWN_BUDGET + 1 s

    async def test_a_cut_cancel_keeps_the_one_stop(self):
        gate, calls = asyncio.Event(), []

        async def slow_stop(s):
            calls.append(s)
            await gate.wait()

        s = HR.Live(path=PROJ)
        job = mock.Mock(data={"live": s})
        with mock.patch.object(HR, "_stop_proc", slow_stop):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(HR._on_cancel(job), 0.05)
            self.assertFalse(s.stopper.done())
            self.assertIs(HR._stopping(s), s.stopper)
            gate.set()
            await asyncio.wait_for(asyncio.shield(HR._stopping(s)), 1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
