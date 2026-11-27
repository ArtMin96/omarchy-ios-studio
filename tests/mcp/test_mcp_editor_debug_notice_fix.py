"""tools/editor_debug.py: a notice's fix (no-symbols: Download symbols) reaches the agent as an ios_open_app step.

The helper's `notice` events carry a fix ({label, page, tab, action}); the bridge job keeps it and bridge_view turns
it into "ask the user to press … in iOS Studio (ios_open_app(page=…))", once per label.

- In process: _apply + bridge_view on a job.
- Against the fake `debug editor` stream of test_mcp_editor_debug.py, its no-symbols notice given the helper's fix.
- The real helper in demo mode with a stand-in daemon whose debug.start says the symbols aren't ready.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_mcp_editor_debug import EditorCase

from iosstudio.features.debugger import SYMBOLS_FIX, SYMBOLS_UPDATE_FIX
from iosstudio_mcp import helper as HELPER
from iosstudio_mcp.core.jobs import Job
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import editor_debug as ED
from support.fake_deviced import FakeDeviced
from support.mcp_fakes import FakeMCP

READY = {"event": "ready", "bundleId": "XTL-A1.com.example.weather", "appName": "Weather", "port": 52917, "pid": 9,
         "launched": False, "editors": ["vscode"], "configName": "Attach to Weather on iPhone (iOS Studio)",
         "localBuild": "/p/xtool/Weather.app/Weather"}


def _job(*events: dict) -> Job:
    job = Job(id="dbe-1", kind=ED.KIND, action="editor", title="Debug in editor")
    b = ED.Bridge(key="com.example.weather")
    job.data["bridge"] = b
    for ev in events:
        ED._apply(job, b, ev)
    return job


def _fix_steps(view) -> list[str]:
    return [s for s in view.next_steps if "ios_open_app(" in s]


class InProcess(unittest.TestCase):
    def test_the_fix_is_kept_and_becomes_an_open_app_step(self):
        job = _job({"event": "notice", "id": "no-symbols", "level": "info", "message": "No symbols.", "fix": SYMBOLS_FIX},
                   {"event": "notice", "id": "no-local-build", "level": "warn", "message": "No build."}, READY)
        self.assertEqual(job.notices[0]["fix"], SYMBOLS_FIX)
        self.assertNotIn("fix", job.notices[1])
        v = ED.bridge_view(job)
        self.assertEqual(v.notices, ["No symbols.", "No build."])         # the notices stay plain text
        steps = _fix_steps(v)
        self.assertEqual(len(steps), 1, v.next_steps)
        self.assertIn("“Download symbols”", steps[0])
        self.assertIn("ios_open_app(page='device')", steps[0])
        self.assertIn("no-symbols", steps[0])

    def test_once_per_label_and_at_most_two(self):
        again = {"event": "notice", "id": "other", "level": "info", "message": "b", "fix": SYMBOLS_FIX}
        job = _job({"event": "notice", "id": "no-symbols", "level": "info", "message": "a", "fix": SYMBOLS_FIX}, again,
                   {"event": "notice", "id": "symbols-update", "level": "warn", "message": "c", "fix": SYMBOLS_UPDATE_FIX},
                   {"event": "notice", "id": "stale", "level": "warn", "message": "d",
                    "fix": {"label": "Run the app", "page": "projects"}}, READY)
        steps = _fix_steps(ED.bridge_view(job))
        self.assertEqual(len(steps), 2, steps)
        self.assertIn("Download symbols", steps[0])
        self.assertIn("Update symbols", steps[1])

    def test_a_fix_without_label_or_target_adds_nothing(self):
        job = _job({"event": "notice", "id": "a", "message": "a", "fix": {"page": "device"}},
                   {"event": "notice", "id": "b", "message": "b", "fix": "Download"},
                   {"event": "notice", "id": "c", "message": "c", "fix": {"label": "Docs", "url": "https://example.com/x"}},
                   READY)
        v = ED.bridge_view(job)
        self.assertEqual(_fix_steps(v), [])
        self.assertTrue(any("https://example.com/x" in s and "Docs" in s for s in v.next_steps))


class FakeStream(EditorCase):
    """The fake `debug editor` stream with the helper's no-symbols fix on its notice."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        text = self.helper.exe.read_text()
        old = 'message="This iPhone\'s debug symbols aren\'t downloaded. " * 20)'
        self.assertIn(old, text)
        self.helper.exe.write_text(text.replace(old, old[:-1] + f", fix={SYMBOLS_FIX!r})"))

    async def asyncTearDown(self):
        await self.state.jobs.shutdown()
        await super().asyncTearDown()

    async def test_ready_names_the_download(self):
        d = await self.data(ED, "ios_debug_editor_start", project_path="Weather")
        self.assertEqual(d["state"], "ready", d)
        steps = [s for s in d["next_steps"] if "ios_open_app(page='device')" in s]
        self.assertEqual(len(steps), 1, d["next_steps"])
        self.assertIn("Download symbols", steps[0])
        await self.data(ED, "ios_debug_editor_stop", job_id=d["job_id"])


class Demo(unittest.IsolatedAsyncioTestCase):
    """The real helper in demo mode: debug.start says the symbols aren't ready, so the bridge's notice has the fix."""

    async def test_demo_without_symbols(self):
        run = tempfile.mkdtemp(prefix="ief-", dir="/tmp")       # short enough for the socket path
        self.addCleanup(shutil.rmtree, run, True)
        replies = {"debug.start": {"bundleId": "XTL-A1B2C3D4E5.com.example.weatherglance", "port": 52917,
                                   "host": "127.0.0.1", "pid": 913, "launched": False, "symbolsReady": False},
                   "debug.stop": lambda a: {"bundleId": a["bundleId"], "stopped": True, "killed": None}}
        with mock.patch.dict(os.environ, {"IOSSTUDIO_DEMO": "1", "IOSSTUDIO_RUNTIME_DIR": run}), \
                FakeDeviced(Path(run), replies, demo=True):
            mcp = FakeMCP()
            ED.register(mcp)
            state = AppState()
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state),
                                  report_progress=None, info=None)
            try:
                proj = (await HELPER.call("projects", "list"))["projects"][0]["path"]
                d = (await mcp.tools["ios_debug_editor_start"](ctx, project_path=proj, write_config=False)
                     ).structured_content
                self.assertEqual(d["state"], "ready", d)
                self.assertFalse(d["symbols_ready"])
                self.assertTrue(any("debug symbols" in n for n in d["notices"]), d["notices"])
                self.assertTrue(any("“Download symbols”" in s and "ios_open_app(page='device')" in s
                                    for s in d["next_steps"]), d["next_steps"])
                await mcp.tools["ios_debug_editor_stop"](ctx)
            finally:
                await state.jobs.shutdown()


if __name__ == "__main__":
    unittest.main()
