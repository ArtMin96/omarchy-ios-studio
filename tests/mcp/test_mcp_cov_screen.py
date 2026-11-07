"""Screen tools (mcp/iosstudio_mcp/tools/screen.py) against a fake device daemon that answers record.* and
mirror.status like the real one (docs/deviced-protocol.md "Screen"): typed results within budget, one-line errors
with a next step, the right annotations, and the recording job (state='recording' + job_id, pollable with
ios_job_status and the tool itself, cancel keeps the file).

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_cov_screen.py
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.tools import jobs as JOBS
from iosstudio_mcp.tools import screen as S
from support.mcp_harness import ToolCase, err, registered, size_of

_sleep = asyncio.sleep
PATH = "/home/u/.cache/ios-studio/recordings/rec-20261101-142233.mp4"


def recording(state, **kw):
    out = {"state": state, "mode": "screenshot", "seconds": 1.0, "bytes": 0, "frames": 8, "maxSeconds": 2,
           "path": PATH, "started": 1793536000.0}
    if state == "done":
        out.update(seconds=2.0, bytes=812_000, frames=17, fps=8.5, reason="timeout")
    return {**out, **kw}


class Annotations(ToolCase):
    DAEMON = False

    async def test_kinds(self):
        a = registered(S).annotations
        for name in ("ios_screen_record", "ios_mirror_status"):
            self.assertTrue(a[name].read_only_hint, name)
            self.assertFalse(a[name].destructive_hint, name)
            self.assertTrue(a[name].open_world_hint, name)
        self.assertFalse(a["ios_screen_record"].idempotent_hint)


class Mirror(ToolCase):
    def which(self, found):
        p = mock.patch.object(S, "shutil", SimpleNamespace(which=lambda name: "/usr/bin/uxplay" if found else None))
        p.start()
        self.addCleanup(p.stop)

    async def test_status_with_the_last_recording(self):
        self.which(True)
        self.daemon.replies["mirror.status"] = (0, {
            "running": True, "mode": "screenshot", "requestedMode": "auto", "fps": 8, "measuredFps": 1.7,
            "notice": "An app is using the camera.", "noticeCode": "media-in-use",
            "available": {"stream": {"available": False, "reason": "media-in-use", "message": "…"},
                          "screenshot": {"available": True, "reason": None}, "jpeg": True, "ffmpeg": True},
            "viewers": 2, "record": recording("done", thumbnail=PATH.replace(".mp4", ".jpg"))})
        res = await self.call(S, "ios_mirror_status")
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        d = res.structured_content
        self.assertEqual((d["running"], d["mode"], d["measured_fps"], d["viewers"]), (True, "screenshot", 1.7, 2))
        modes = {m["mode"]: m for m in d["modes"]}
        self.assertEqual(modes["stream"]["reason"], "media-in-use")
        self.assertTrue(modes["airplay"]["available"])
        self.assertEqual(d["recording"]["state"], "done")
        self.assertTrue(d["recording"]["path"].startswith("/home/u/") or d["recording"]["path"].startswith("~"))
        self.assertTrue(any("camera or microphone" in s for s in d["next_steps"]))
        self.assertFalse(any("UxPlay" in s for s in d["next_steps"]))

    async def test_nothing_installed_and_no_recording(self):
        self.which(False)
        self.daemon.replies["mirror.status"] = (0, {"running": False, "available": {}, "record": {"state": "idle"}})
        d = await self.data(S, "ios_mirror_status")
        self.assertNotIn("recording", d)
        self.assertEqual(next(m for m in d["modes"] if m["mode"] == "airplay")["reason"], "not-installed")
        self.assertTrue(any("UxPlay" in s for s in d["next_steps"]))
        self.assertTrue(any("ffmpeg" in s for s in d["next_steps"]))

    async def test_a_device_error_has_its_next_step(self):
        self.daemon.replies["mirror.status"] = (0, err("locked", "The iPhone is locked."))
        text = await self.error(S, "ios_mirror_status")
        self.assertIn("[locked] Next: Ask the user to unlock the iPhone", text)


class Record(ToolCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        fast = SimpleNamespace(sleep=lambda s: _sleep(0.05), to_thread=asyncio.to_thread)   # the job polls every 1 s
        p = mock.patch.object(S, "asyncio", fast)
        p.start()
        self.addCleanup(p.stop)

    def follow(self, *replies):
        seq = list(replies)
        self.daemon.replies["record.status"] = lambda a: (0, seq.pop(0) if len(seq) > 1 else seq[0])

    async def test_a_recording_is_a_job_then_the_video_with_a_thumbnail(self):
        thumb = self.tmp / "rec.jpg"
        Image.new("RGB", (320, 694), (30, 90, 200)).save(thumb, "JPEG")
        self.daemon.replies["record.start"] = (0, recording("recording", seconds=0))
        self.follow(recording("recording"), recording("recording"), recording("done", thumbnail=str(thumb)))
        res = await self.call(S, "ios_screen_record", seconds=2, wait_seconds=0)
        d = res.structured_content
        self.assertEqual(d["state"], "recording")
        jid = d["job_id"]
        self.assertIn(f"ios_screen_record(job_id='{jid}'", d["next_steps"][0])
        self.assertEqual(self.daemon.sent("record.start")[0], {"maxSeconds": 2, "mode": "auto"})
        st = await self.data(JOBS, "ios_job_status", job_id=jid, wait_seconds=4)
        self.assertEqual(st["state"], "succeeded")
        res = await self.call(S, "ios_screen_record", job_id=jid, wait_seconds=0)
        d = res.structured_content
        self.assertEqual((d["state"], d["seconds"], d["fps"], d["stop_reason"]), ("done", 2.0, 8.5, "timeout"))
        self.assertTrue(d["inline_thumbnail"])
        self.assertEqual(res.content[0].type, "image")
        self.assertLessEqual(len(res.content[0].data) * 3 // 4, S.THUMB_BYTES)
        self.assertIn("ios_mirror_status", d["next_steps"][1])          # screenshot-made video: says why

    async def test_without_thumbnail_only_text(self):
        self.daemon.replies["record.start"] = (0, recording("recording"))
        self.follow(recording("done"))
        res = await self.call(S, "ios_screen_record", seconds=1, wait_seconds=3, thumbnail=False)
        self.assertEqual([c.type for c in res.content], ["text"])
        self.assertIn("done", res.content[0].text)
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_start_errors_are_one_line_with_a_next_step(self):
        self.daemon.replies["record.start"] = (0, err("busy", "A recording is already running."))
        text = await self.error(S, "ios_screen_record", seconds=2)
        self.assertIn("[busy] Next:", text)
        self.assertEqual(self.state.jobs.jobs, {})

    async def test_a_recording_that_ends_without_a_file_fails(self):
        self.daemon.replies["record.start"] = (0, recording("recording"))
        self.follow({"state": "idle"})
        res = await self.call(S, "ios_screen_record", seconds=2, wait_seconds=3)
        d = res.structured_content
        self.assertEqual(d["state"], "failed")
        self.assertIn("without a file", d["error"])
        self.assertIn("ios_mirror_status", d["next_steps"][0])

    async def test_cancel_stops_it_and_keeps_the_file(self):
        self.daemon.replies["record.start"] = (0, recording("recording"))
        self.daemon.replies["record.stop"] = (0, recording("done", reason="stopped"))
        self.daemon.replies["record.status"] = lambda a: (0, recording("done", reason="stopped")
                                                          if self.daemon.count("record.stop") else recording("recording"))
        d = (await self.call(S, "ios_screen_record", seconds=30, wait_seconds=0)).structured_content
        await self.data(JOBS, "ios_job_cancel", job_id=d["job_id"])
        self.assertEqual(self.daemon.count("record.stop"), 1)
        d = (await self.call(S, "ios_screen_record", job_id=d["job_id"], wait_seconds=0)).structured_content
        self.assertEqual((d["state"], d["stop_reason"]), ("done", "stopped"))

    async def test_a_job_id_of_another_kind_is_refused(self):
        job = self.state.jobs.run(lambda j: _noop(), "pcap", title="x")
        text = await self.error(S, "ios_screen_record", job_id=job.id)
        self.assertIn("recording", text)


async def _noop():
    return {}
