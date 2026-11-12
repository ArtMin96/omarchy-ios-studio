"""backend.TaskSet.close() is bounded: a background task that swallows its cancel (a pymobiledevice3
connection mid-read) is logged and left behind after `limit` seconds instead of holding up the daemon's
shutdown forever (asyncio.gather waited without a limit). Same bound as the broker's cancelled requests.

    .venv/bin/pytest -q tests/helper/test_dev_taskset_close.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_taskset_close
"""
from __future__ import annotations

import asyncio
import time
import unittest

from iosstudio.device import backend, broker


class TaskSetCloseBound(unittest.IsolatedAsyncioTestCase):
    async def test_a_task_that_wont_cancel_is_left_behind_after_the_limit(self):
        logged: list[str] = []
        ts = backend.TaskSet(logged.append)
        release = asyncio.Event()

        async def stubborn():
            while not release.is_set():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    pass            # swallows the cancel, like a connection stuck mid-read

        t = ts.spawn(stubborn(), name="stubborn")
        polite = ts.spawn(asyncio.sleep(3600), name="polite")
        await asyncio.sleep(0)
        t0 = time.monotonic()
        await ts.close(limit=0.3)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertTrue(polite.cancelled())
        self.assertFalse(t.done())
        self.assertEqual(len(logged), 1, logged)
        self.assertIn("1 background task(s) didn't stop within 0.3 s: stubborn", logged[0])
        release.set()
        t.cancel()
        await asyncio.wait({t}, timeout=2)

    async def test_tasks_that_end_on_cancel_are_awaited_and_nothing_is_logged(self):
        logged: list[str] = []
        ts = backend.TaskSet(logged.append)
        cleaned: list[int] = []

        async def tidy():
            try:
                await asyncio.sleep(3600)
            finally:
                await asyncio.sleep(0.05)   # a bounded close
                cleaned.append(1)

        ts.spawn(tidy(), name="tidy")
        await asyncio.sleep(0)
        await ts.close(limit=5)
        self.assertEqual(cleaned, [1])
        self.assertEqual(logged, [])
        self.assertEqual(len(ts), 0)

    def test_same_bound_as_the_broker(self):
        self.assertEqual(backend.CANCEL_WAIT_S, broker.REQUEST_CANCEL_S)


if __name__ == "__main__":
    unittest.main()
