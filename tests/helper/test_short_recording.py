"""A screen recording stopped right after it started still has a picture (features/mirror/demo.py, real.py).

The demo failed a 1 s recording with record-empty ("no pictures") while its frames were still being painted;
the real recorder had the same edge: stopped before the first keyframe (stream) or the first screenshot arrived.

    python -m pytest tests/helper/test_short_recording.py
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.device.features.mirror import real as M


class DemoShortRecording(unittest.IsolatedAsyncioTestCase):
    async def test_stop_right_after_start_keeps_one_picture(self):
        from iosstudio.device.demo import core as demo_core
        from iosstudio.device.demo.daemon import DemoBackend
        shutil.rmtree(demo_core.DIR / "frames-cache", ignore_errors=True)   # cold: frames not painted yet
        b = DemoBackend(None)
        self.addAsyncCleanup(b.shutdown)
        with mock.patch.object(demo_core, "event", lambda *a, **k: None):
            await b.dispatch("record.start", {"maxSeconds": 1})
            result = await b.dispatch("record.stop", {})
        self.assertEqual(result["state"], "done", result)
        self.assertGreaterEqual(result["frames"], 1)


class RealShortRecording(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.m = M.Mirror()
        self.m.running = lambda: True
        for p in (mock.patch.object(M, "emit", lambda *a, **k: None), mock.patch.object(M, "REC_DIR", self.tmp)):
            p.start()
            self.addCleanup(p.stop)

    def recorder(self):
        rec = M.Recorder("screenshot", 5, None, folder=self.tmp)
        rec.finalize = lambda thumb: {"state": "done", "frames": rec.frames}   # no ffmpeg needed here
        self.m.recorder = rec
        return rec

    async def test_waits_for_the_first_picture(self):
        rec = self.recorder()
        threading.Timer(0.3, rec.add_frame, (b"jpg", "jpg", time.monotonic())).start()
        result = await asyncio.wait_for(await self.m._end_recording("stopped"), 5)
        self.assertEqual(result["frames"], 1)

    async def test_wait_is_bounded(self):
        rec = self.recorder()
        with mock.patch.object(M, "REC_FIRST_WAIT_S", 0.2):
            t0 = time.monotonic()
            result = await (await self.m._end_recording("stopped"))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(result["frames"], 0)
        self.assertEqual(rec.state, "done")

    async def test_no_wait_when_the_mirror_is_gone(self):
        self.m.running = lambda: False
        self.recorder()
        t0 = time.monotonic()
        await (await self.m._end_recording("tunnel"))
        self.assertLess(time.monotonic() - t0, 0.5)


if __name__ == "__main__":
    unittest.main()
