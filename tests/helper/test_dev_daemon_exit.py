"""real/daemon.py main(): the process exits soon after its main coroutine ends (a real-iPhone run: after
`daemon.shutdown {"all": true}` the daemon logged "bye" and never exited, ignoring SIGTERM; asyncio.run waited
without a limit for a task that wouldn't cancel and for the executor's threads).

run_bounded() cancels what's left, waits EXIT_GRACE_S at most, skips the executor wait, and a timer exits the
process if the loop itself is blocked. Each case runs in its own Python process, as the daemon does.

    .venv/bin/pytest -q tests/helper/test_dev_daemon_exit.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_daemon_exit
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2] / "helper"

PRELUDE = """
import asyncio, os, sys, time
from iosstudio.device.real import core, daemon
core.dlog = daemon.dlog = lambda *a, **k: print(*a, file=sys.stderr)
"""


def run(body: str, timeout: float = 20) -> tuple[int, float, str]:
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)], capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, "PYTHONPATH": str(HELPER)})
    return p.returncode, time.monotonic() - t0, p.stderr


class BoundedExit(unittest.TestCase):
    def test_a_task_that_wont_cancel_and_a_stuck_thread_dont_keep_it_alive(self):
        code, took, err = run("""
            async def stubborn():
                while True:
                    try:
                        await asyncio.sleep(3600)
                    except asyncio.CancelledError:
                        pass            # like a connection that swallows the cancel mid-read

            async def main():
                asyncio.ensure_future(stubborn(), loop=None).set_name("stubborn")
                asyncio.get_running_loop().run_in_executor(None, time.sleep, 60)   # a to_thread stuck in I/O
                await asyncio.sleep(0.05)
                return 3

            t0 = time.monotonic()
            code = daemon.run_bounded(main(), grace=0.3)
            print(f"returned {code} after {time.monotonic() - t0:.2f}", file=sys.stderr)
            os._exit(code)
        """)
        self.assertEqual(code, 3, err)
        self.assertLess(took, 8, err)
        self.assertIn("returned 3", err)
        self.assertIn("1 task(s) that didn't stop: stubborn", err)

    def test_tasks_that_end_on_cancel_are_cleaned_up(self):
        code, _, err = run("""
            cleaned = []

            async def polite():
                try:
                    await asyncio.sleep(3600)
                finally:
                    cleaned.append(1)

            async def main():
                asyncio.ensure_future(polite())
                await asyncio.sleep(0.05)
                return 0

            code = daemon.run_bounded(main(), grace=2)
            print("cleaned", cleaned, file=sys.stderr)
            os._exit(code)
        """)
        self.assertEqual(code, 0, err)
        self.assertIn("cleaned [1]", err)
        self.assertNotIn("didn't stop", err)

    def test_a_blocked_loop_still_exits(self):
        code, took, err = run("""
            async def blocker():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    time.sleep(30)      # blocks the loop itself: only the backstop timer gets out

            async def main():
                asyncio.ensure_future(blocker())
                await asyncio.sleep(0.05)
                return 5

            daemon.run_bounded(main(), grace=0.2)
            print("not reached", file=sys.stderr)
        """)
        self.assertEqual(code, 5, err)
        self.assertLess(took, 10, err)
        self.assertNotIn("not reached", err)


if __name__ == "__main__":
    unittest.main()
