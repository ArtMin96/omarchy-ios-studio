"""Metrics tools (mcp/iosstudio_mcp/tools/metrics.py) against a fake device daemon that answers metrics.* like the
real one (docs/deviced-protocol.md "Metrics"): typed results within budget, one-line errors with a next step, the
right annotations, and the two profiling jobs (dry run without confirm, then state='running' + job_id).

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_cov_metrics.py
"""
from __future__ import annotations

import time

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.tools import jobs as JOBS
from iosstudio_mcp.tools import metrics as M
from support.mcp_harness import ToolCase, err, registered, size_of

APP = {"bundleId": "XTL-A1B2C3D4E5.com.example.app", "name": "MyApp", "developer": True, "running": True}
SESSION = {"sessionId": "m1", "bundleId": APP["bundleId"], "name": "MyApp", "pid": 812, "follow": True,
           "sets": ["process", "energy", "graphics", "network", "battery", "events"], "intervalMs": 1000,
           "running": True, "status": "running", "samples": 295, "events": 9, "notes": []}
STATS = {"window": {"samples": 300, "start": 1793549531, "end": 1793549830, "seconds": 299},
         "series": {"cpu": {"min": 2, "avg": 21.3, "p95": 33.3, "max": 96.4, "last": 18, "n": 300},
                    "mem": {"min": 138.4, "avg": 145, "p95": 151, "max": 152.2, "n": 300}},
         "memory": {"slopeMbPerMin": 3.26, "r2": 0.87, "seconds": 258, "growthMb": 14, "from": 138.4, "to": 152.2,
                    "likelyLeak": True},
         "cpuSpikes": {"count": 3, "longest": 5, "items": [{"ts": 1793549651, "seconds": 5, "peak": 96}]},
         "fpsDrops": {"count": 3, "items": [{"ts": 1793549636, "seconds": 5, "low": 44}]},
         "hitches": {"count": 9, "totalMs": 853, "worstMs": 128},
         "battery": {"charging": False, "from": 78, "to": 76, "drainPctPerHour": 16.5, "mWh": 120.9, "avgMw": 1437},
         "energy": {"avg": 5.75, "max": 11.96, "byComponent": {"Display": {"avg": 2.15, "share": 37}}},
         "eventCounts": {"jetsam": 1, "hang": 1, "hitch": 9},
         "anomalies": [{"kind": "leak", "level": "warn", "text": "Memory grew 3.26 MB/min for 4 minutes: a possible "
                                                                  "leak. Run Check for leaks."}]}
NOT_FOUND = err("not-found", "No metrics session 'm9'.")


def ann(name):
    return registered(M).annotations[name]


class Annotations(ToolCase):
    DAEMON = False

    async def test_kinds(self):
        for name in ("ios_metrics_summary", "ios_metrics_series", "ios_metrics_events"):
            a = ann(name)
            self.assertTrue(a.read_only_hint, name)
            self.assertTrue(a.open_world_hint, name)
        for name in ("ios_metrics_start", "ios_metrics_stop", "ios_launch_profile", "ios_memory_leaks"):
            a = ann(name)
            self.assertFalse(a.read_only_hint, name)
            self.assertFalse(a.destructive_hint, name)
            self.assertTrue(a.open_world_hint, name)
        self.assertTrue(ann("ios_metrics_stop").idempotent_hint)


class Sessions(ToolCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.daemon.replies["apps.list"] = (0, [APP])

    async def test_start_resolves_the_app_and_returns_the_session(self):
        self.daemon.replies["metrics.start"] = (0, SESSION)
        d = await self.data(M, "ios_metrics_start", bundle_id="com.example.app", sets=["process", "process", "energy"])
        self.assertEqual((d["session_id"], d["status"], d["pid"]), ("m1", "running", 812))
        self.assertIn("ios_metrics_summary(session_id='m1')", d["next_steps"][0])
        sent = self.daemon.sent("metrics.start")[0]
        self.assertEqual(sent["bundleId"], APP["bundleId"])     # the installed (XTL-) form
        self.assertEqual(sent["sets"], ["process", "energy"])   # duplicates dropped
        self.assertEqual(sent["intervalMs"], 1000)

    async def test_start_for_an_app_that_is_not_running_says_how_to_open_it(self):
        self.daemon.replies["metrics.start"] = (0, {**SESSION, "status": "waiting-app", "pid": None})
        d = await self.data(M, "ios_metrics_start", bundle_id="com.example.app")
        self.assertIn("ios_app_launch", d["next_steps"][0])

    async def test_start_needs_an_app(self):
        text = await self.error(M, "ios_metrics_start")
        self.assertIn("ios_app_list", text)

    async def test_start_for_an_unknown_app_names_the_installed_ones(self):
        text = await self.error(M, "ios_metrics_start", bundle_id="com.nope")
        self.assertIn(APP["bundleId"], text)
        self.assertIn("ios_run", text)
        self.assertEqual(self.daemon.count("metrics.start"), 0)

    async def test_a_device_error_has_its_next_step(self):
        self.daemon.replies["metrics.start"] = (0, err("developer-mode-off", "Developer Mode is off."))
        text = await self.error(M, "ios_metrics_start", bundle_id="com.example.app")
        self.assertIn("[developer-mode-off]", text)
        self.assertIn("Next: Call ios_device_prepare", text)

    async def test_summary_is_small_and_flags_the_leak(self):
        self.daemon.replies["metrics.snapshot"] = (0, {"session": SESSION, "stats": STATS})
        res = await self.call(M, "ios_metrics_summary", session_id="m1")
        self.assertLessEqual(size_of(res), 4000)
        d = res.structured_content
        self.assertEqual((d["seconds"], d["samples"], d["cpu_spikes"], d["fps_drops"]), (299, 300, 3, 3))
        self.assertEqual(d["series"]["cpu"]["unit"], "%")
        self.assertTrue(d["memory_trend"]["likelyLeak"])
        self.assertEqual(d["energy_by_component"], {"Display": 37})
        self.assertEqual(d["hitches"]["worstMs"], 128)
        self.assertTrue(any("ios_memory_leaks" in s for s in d["next_steps"]))
        self.assertTrue(d["anomalies"][0].startswith("Memory grew"))

    async def test_unknown_session_points_to_metrics_start(self):
        for cmd, name, kw in (("metrics.snapshot", "ios_metrics_summary", {}),
                              ("metrics.stop", "ios_metrics_stop", {}),
                              ("metrics.series", "ios_metrics_series", {"metric": "cpu"}),
                              ("metrics.read", "ios_metrics_events", {})):
            self.daemon.replies[cmd] = (0, NOT_FOUND)
            text = await self.error(M, name, session_id="m9", **kw)
            self.assertIn("[not-found] Next: ios_metrics_start starts a new session.", text, name)

    async def test_stop_returns_the_final_summary(self):
        self.daemon.replies["metrics.stop"] = (0, {**SESSION, "running": False, "status": "stopped", "summary": STATS})
        d = await self.data(M, "ios_metrics_stop", session_id="m1")
        self.assertEqual((d["status"], d["running"]), ("stopped", False))
        self.assertEqual(d["events"], {"jetsam": 1, "hang": 1, "hitch": 9})
        self.assertFalse(any("ios_metrics_stop" in s for s in d["next_steps"]))   # already stopped


class Series(ToolCase):
    async def test_buckets_and_the_csv_spilled_to_a_file(self):
        csv = self.tmp / "m1.csv"
        csv.write_text("ts,cpu,mem\n" + "".join(f"{i},{i % 50},{140 + i / 100}\n" for i in range(3000)))
        t0 = 1793549531.0
        self.daemon.replies["metrics.series"] = (0, {
            "sessionId": "m1", "metric": "mem", "label": "Memory", "unit": "MB",
            "buckets": [{"t0": t0 + 10 * i, "t1": t0 + 10 * (i + 1), "min": 140 + i, "avg": 141 + i, "max": 142 + i,
                         "n": 10} for i in range(60)],
            "stats": {"min": 140, "avg": 170, "p95": 198, "max": 201}})
        self.daemon.replies["metrics.export"] = (0, {"sessionId": "m1", "path": str(csv), "rows": 3000})
        res = await self.call(M, "ios_metrics_series", session_id="m1", metric="mem", points=60)
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        d = res.structured_content
        self.assertEqual(len(d["buckets"]), 60)
        self.assertEqual((d["buckets"][1]["t"], d["seconds_per_bucket"], d["unit"]), (10, 10.0, "MB"))
        self.assertTrue(d["csv_path"])
        self.assertEqual(self.daemon.sent("metrics.series")[0]["points"], 60)

    async def test_no_samples_and_a_failed_export_still_answer(self):
        self.daemon.replies["metrics.series"] = (0, {"sessionId": "m1", "metric": "fps", "buckets": [], "stats": {}})
        self.daemon.replies["metrics.export"] = (0, err("failed", "disk full"))
        d = await self.data(M, "ios_metrics_series", session_id="m1", metric="fps")
        self.assertEqual(d.get("buckets", []), [])
        self.assertTrue(any("CSV export failed" in s for s in d["next_steps"]))
        self.assertTrue(any("No samples" in s for s in d["next_steps"]))


class Events(ToolCase):
    def page(self, n, cursor=0, total=None):
        now = time.time()
        kinds = ["hang", "hitch", "jetsam", "launch"]
        return {"entries": [{"ts": now + i, "kind": kinds[i % 4], "text": f"event {cursor + i} " + "x" * 150,
                             **({"durationMs": 250.0} if i % 4 == 0 else {})} for i in range(n)],
                "cursor": cursor, "nextCursor": cursor + n, "hasMore": False, "missed": 0, "total": total or cursor + n,
                "kind": "events", "session": {**SESSION}}

    async def test_a_page_with_a_cursor(self):
        self.daemon.replies["metrics.read"] = (0, self.page(10, total=30))
        d = await self.data(M, "ios_metrics_events", session_id="m1", limit=5)
        self.assertEqual((d["count"], d["next_cursor"], d["has_more"]), (5, 5, True))
        self.assertTrue(d["events"][0].startswith("0 "))
        self.assertIn(" hang 250 ms ", d["events"][0])
        self.assertIn("cursor=5", d["next_steps"][0])
        self.assertEqual(self.daemon.sent("metrics.read")[0]["kind"], "events")

    async def test_kinds_filter_keeps_the_cursor_after_skipped_events(self):
        self.daemon.replies["metrics.read"] = (0, self.page(8, cursor=4))
        d = await self.data(M, "ios_metrics_events", session_id="m1", cursor=4, kinds=["jetsam"])
        self.assertEqual(d["count"], 2)
        self.assertTrue(all(" jetsam " in e for e in d["events"]))
        self.assertEqual((d["next_cursor"], d["has_more"]), (12, False))

    async def test_a_big_page_is_cut_to_the_budget_with_a_cursor_to_read_on(self):
        self.daemon.replies["metrics.read"] = (0, self.page(500))
        res = await self.call(M, "ios_metrics_events", session_id="m1", limit=100)
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        d = res.structured_content
        self.assertTrue(d["truncated"])
        self.assertLess(d["count"], 100)
        self.assertEqual(d["next_cursor"], d["count"])
        self.assertIn(f"cursor={d['count']}", d["note"])


class ProfileJobs(ToolCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.daemon.replies["apps.list"] = (0, [APP])

    async def test_without_confirm_both_are_dry_runs_that_touch_nothing(self):
        for name, cmd in (("ios_launch_profile", "metrics.launchProfile"), ("ios_memory_leaks", "metrics.leaks")):
            d = await self.data(M, name, bundle_id="com.example.app")
            self.assertEqual(d["state"], "dry_run", name)
            self.assertIn("confirm=true", d["message"])
            self.assertTrue(d["would"])
            self.assertEqual(self.daemon.count(cmd), 0, name)
        self.assertEqual(self.state.jobs.jobs, {})

    async def test_need_a_bundle_id(self):
        self.assertIn("job_id", await self.error(M, "ios_launch_profile", confirm=True))

    async def test_launch_profile_is_a_job_pollable_by_job_status(self):
        verdict = "OK launch (690 ms). Apple suggests the first frame within 400 ms."
        self.daemon.replies["metrics.launchProfile"] = (0.4, {"bundleId": APP["bundleId"], "preMainMs": 224.1,
                                                              "firstFrameMs": 690, "verdict": verdict})
        d = await self.data(M, "ios_launch_profile", bundle_id="com.example.app", confirm=True, wait_seconds=0)
        self.assertEqual(d["state"], "running")
        jid = d["job_id"]
        self.assertIn(f"ios_launch_profile(job_id='{jid}'", d["next_steps"][-1])
        text = await self.error(M, "ios_launch_profile", bundle_id="com.example.app", confirm=True)
        self.assertIn(jid, text)                                   # one at a time
        st = await self.data(JOBS, "ios_job_status", job_id=jid, wait_seconds=5)
        self.assertEqual(st["state"], "succeeded")
        done = await self.data(M, "ios_launch_profile", job_id=jid, wait_seconds=0)
        self.assertEqual((done["state"], done["message"]), ("succeeded", verdict))
        self.assertEqual(done["result"]["firstFrameMs"], 690)

    async def test_a_failed_leak_scan_reports_the_device_error(self):
        self.daemon.replies["metrics.leaks"] = (0, err("not-running", "MyApp isn't running."))
        d = await self.data(M, "ios_memory_leaks", bundle_id="com.example.app", confirm=True, wait_seconds=5)
        self.assertEqual(d["state"], "failed")
        self.assertIn("isn't running", d["error"])

    async def test_a_job_id_of_another_kind_is_refused(self):
        self.daemon.replies["metrics.leaks"] = (0, {"leakCount": 0, "verdict": "No leaks."})
        d = await self.data(M, "ios_memory_leaks", bundle_id="com.example.app", confirm=True, wait_seconds=5)
        text = await self.error(M, "ios_launch_profile", job_id=d["job_id"])
        self.assertIn("launch-profile", text)
