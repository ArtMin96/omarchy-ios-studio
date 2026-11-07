"""Daemon building blocks (helper/iosstudio/device/kit.py): the feature registry (staged registration, isolated
hooks and contributors, broken features skipped), reference-counted Resources (scopes, timeouts, concurrent opens,
failing closes) and the bounded CursorRing (count/bytes limits, missed entries, waiting readers).

    .venv/bin/pytest -q tests/helper/test_dev_kit.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_kit
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from support import devtest

devtest.isolate()

from iosstudio.device import kit  # noqa: E402


def quiet():
    logs: list[str] = []
    return logs, logs.append


# ====================================================================== registry

class Registry(unittest.IsolatedAsyncioTestCase):
    def test_lookup_accepts_any_separator(self):
        _, log = quiet()
        r = kit.FeatureRegistry(log)
        m = r.module("perf")
        m.command("perf.start", lambda c, a: 1)
        m.commit()
        for name in ("perf.start", "perf_start", "perf-start"):
            self.assertTrue(r.has(name))
        self.assertIsNone(r.lookup("perf.stop"))
        self.assertEqual(r.describe()["commands"], {"perf": ["perf.start"]})

    def test_duplicates_refused(self):
        r = kit.FeatureRegistry(quiet()[1])
        a = r.module("a")
        a.command("x.y", lambda c, a: 1)
        a.provide("syslog", object())
        a.commit()
        b = r.module("b")
        with self.assertRaisesRegex(ValueError, "already registered by a"):
            b.command("x_y", lambda c, a: 2)
        with self.assertRaises(ValueError):
            b.provide("syslog", object())

    async def test_contributors_and_hooks_are_isolated(self):
        logs, log = quiet()
        r = kit.FeatureRegistry(log)
        m = r.module("m")
        ran = []

        @m.on_shutdown
        def bad(ctx):
            raise RuntimeError("boom")

        @m.on_shutdown
        async def good(ctx):
            ran.append("good")
        m.info(lambda ctx, s: 1 / 0)
        m.info(lambda ctx, s: {"a": 1})
        m.welcome(lambda ctx: [("x", {})])

        @m.welcome
        async def w2(ctx):
            raise KeyError("w")
        m.commit()
        await r.run_hooks("shutdown", None)
        self.assertEqual(ran, ["good"])
        self.assertEqual(r.info(None, None), {"a": 1})
        self.assertEqual(await r.welcome(None), [("x", {})])
        self.assertEqual(len(logs), 3)

    async def test_hook_cancellation_propagates(self):
        r = kit.FeatureRegistry(quiet()[1])
        m = r.module("m")

        @m.on_client_gone
        async def cancelled(ctx, client):
            raise asyncio.CancelledError
        m.commit()
        with self.assertRaises(asyncio.CancelledError):
            await r.run_hooks("client_gone", None, None)

    def test_load_features_skips_broken_modules_without_partial_registration(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pkg = "devtest_kitpkg"
        (tmp / pkg / "features").mkdir(parents=True)
        (tmp / pkg / "__init__.py").write_text("")
        (tmp / pkg / "features" / "__init__.py").write_text("")
        feats = {
            "good": "def register(reg):\n    reg.command('good.cmd', lambda c, a: 'ok')\n",
            "halfway": "def register(reg):\n    reg.command('half.one', lambda c, a: 1)\n    raise RuntimeError('x')\n",
            "noreg": "X = 1\n",
            "syntax": "def register(reg:\n",
            "_private": "def register(reg):\n    raise AssertionError('never loaded')\n",
        }
        for name, src in feats.items():
            (tmp / pkg / "features" / name).mkdir()
            (tmp / pkg / "features" / name / "__init__.py").write_text("")
            (tmp / pkg / "features" / name / "demo.py").write_text(src)
        (tmp / pkg / "kitcopy.py").write_text(Path(kit.__file__).read_text())
        sys.path.insert(0, str(tmp))
        self.addCleanup(sys.path.remove, str(tmp))
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules) if k.startswith(pkg)])
        import importlib
        kc = importlib.import_module(f"{pkg}.kitcopy")
        logs, log = quiet()
        r = kc.load_features(kc.FeatureRegistry(log), "demo")
        self.assertEqual(r.loaded, ["good"])
        self.assertEqual(set(r.failed), {"halfway", "noreg", "syntax"})
        self.assertFalse(r.has("half.one"))              # staged command dropped with its module
        self.assertTrue(r.has("good.cmd"))
        self.assertEqual(len(logs), 3)


# ====================================================================== resources

class Res(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logs, log = quiet()
        self.res = kit.Resources(log)
        self.closed: list[tuple] = []

    def closer(self, h, reason):
        self.closed.append((h, reason))

    async def test_refcount_and_idempotent_acquire(self):
        opens = []
        open_fn = lambda: opens.append(1) or "H"   # noqa: E731
        self.assertEqual(await self.res.acquire("perf", "c1", open_fn, self.closer), "H")
        await self.res.acquire("perf", "c1", open_fn, self.closer)
        await self.res.acquire("perf", "c2", open_fn, self.closer)
        self.assertEqual((len(opens), self.res.holders("perf")), (1, 2))
        self.assertEqual(self.res.keys_for("c1"), ["perf"])
        self.assertEqual(await self.res.release("perf", "c1"), 1)   # one release drops c1 entirely
        self.assertEqual(self.closed, [])
        self.assertEqual(await self.res.release("perf", "c2"), 0)
        self.assertEqual(self.closed, [("H", "released")])
        self.assertEqual(await self.res.release("perf", "c2"), 0)   # unknown: no-op
        self.assertFalse(self.res.held("perf"))

    async def test_concurrent_acquires_share_one_open(self):
        gate = asyncio.Event()
        opens = []

        async def open_fn():
            opens.append(1)
            await gate.wait()
            return "H"
        t1 = asyncio.create_task(self.res.acquire("k", "a", open_fn, self.closer))
        await asyncio.sleep(0)
        self.assertFalse(self.res.held("k"))              # still opening
        self.assertIsNone(self.res.handle("k"))
        t2 = asyncio.create_task(self.res.acquire("k", "b", open_fn, self.closer))
        await asyncio.sleep(0)
        gate.set()
        self.assertEqual(await asyncio.gather(t1, t2), ["H", "H"])
        self.assertEqual((len(opens), self.res.holders("k")), (1, 2))

    async def test_failed_open_holds_nothing_and_waiters_see_the_error(self):
        gate = asyncio.Event()

        async def open_fn():
            await gate.wait()
            raise OSError("no service")
        t1 = asyncio.create_task(self.res.acquire("k", "a", open_fn, self.closer))
        await asyncio.sleep(0)
        t2 = asyncio.create_task(self.res.acquire("k", "b", open_fn, self.closer))
        await asyncio.sleep(0)
        gate.set()
        r = await asyncio.gather(t1, t2, return_exceptions=True)
        self.assertTrue(all(isinstance(e, OSError) for e in r))
        self.assertEqual(self.res.describe(), [])
        self.assertEqual(await self.res.acquire("k", "a", lambda: "H2", self.closer), "H2")   # retry works

    async def test_cancelled_open_lets_the_next_acquire_open_again(self):
        async def hang():
            await asyncio.Event().wait()
        t = asyncio.create_task(self.res.acquire("k", "a", hang, self.closer))
        await asyncio.sleep(0)
        t.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t
        self.assertEqual(await self.res.acquire("k", "b", lambda: "H", self.closer), "H")

    async def test_scopes_on_release_all(self):
        for key, scope in (("t", "tunnel"), ("s", "session"), ("d", "daemon")):
            await self.res.acquire(key, "c", lambda k=key: k, self.closer, scope=scope)
        await self.res.release_all("tunnel", ("tunnel",))
        self.assertEqual(self.closed, [("t", "tunnel")])
        await self.res.release_all("session", ("tunnel", "session"))
        self.assertEqual(self.closed[-1], ("s", "session"))
        self.assertTrue(self.res.held("d"))
        await self.res.release_all("shutdown")
        self.assertEqual(self.closed[-1], ("d", "shutdown"))
        with self.assertRaises(ValueError):
            await self.res.acquire("x", "c", lambda: 1, scope="forever")

    async def test_client_gone_closes_only_what_it_alone_held(self):
        await self.res.acquire("shared", "a", lambda: "S", self.closer)
        await self.res.acquire("shared", "b", lambda: "S", self.closer)
        await self.res.acquire("mine", "a", lambda: "M", self.closer)
        await self.res.client_gone("a")
        self.assertEqual(self.closed, [("M", "client")])
        self.assertEqual(self.res.holders("shared"), 1)

    async def test_timeout_closes_and_touch_restarts_it(self):
        loop = asyncio.get_running_loop()
        await self.res.acquire("k", "c", lambda: "H", self.closer, timeout=30)
        r = self.res._r["k"]
        first = r.timer.when()
        self.res.touch("k")
        self.assertGreaterEqual(r.timer.when(), first)
        self.assertAlmostEqual(r.timer.when(), loop.time() + 30, delta=1)
        await self.res._expire(r)                         # what the timer fires
        self.assertEqual(self.closed, [("H", "timeout")])
        self.assertIn("timed out", self.logs[0])
        await self.res._expire(r)                         # a late second fire is a no-op
        self.assertEqual(len(self.closed), 1)

    async def test_forget_and_close(self):
        await self.res.acquire("k", "c", lambda: "H", self.closer, timeout=30)
        timer = self.res._r["k"].timer
        self.res.forget("k")
        self.assertTrue(timer.cancelled())
        self.assertEqual(self.closed, [])
        await self.res.acquire("k", "c", lambda: "H", self.closer)
        self.assertTrue(await self.res.close("k", "error"))
        self.assertFalse(await self.res.close("k"))
        self.assertEqual(self.closed, [("H", "error")])

    async def test_failing_close_is_logged_not_raised(self):
        def bad(h, reason):
            raise RuntimeError("close broke")
        await self.res.acquire("k", "c", lambda: "H", bad)
        self.assertEqual(await self.res.release("k", "c"), 0)
        self.assertIn("close (released) failed", self.logs[0])
        self.assertFalse(self.res.held("k"))


# ====================================================================== cursor ring

class Ring(unittest.IsolatedAsyncioTestCase):
    def test_count_limit_and_missed(self):
        r = kit.CursorRing(max_items=3)
        for i in range(5):
            self.assertEqual(r.append({"i": i}), i)
        page = r.read(0)
        self.assertEqual(page["entries"], [{"i": 2}, {"i": 3}, {"i": 4}])
        self.assertEqual((page["missed"], page["bufferStart"], page["cursor"], page["nextCursor"], page["total"]),
                         (2, 2, 2, 5, 5))
        self.assertFalse(page["hasMore"])
        self.assertEqual(r.read(4)["entries"], [{"i": 4}])
        self.assertEqual(r.read(99)["cursor"], 5)              # a cursor from the future is clamped
        self.assertEqual(r.read(-3)["missed"], 2)

    def test_limit_pages(self):
        r = kit.CursorRing()
        r.extend(range(10))
        p = r.read(None, limit=4)
        self.assertEqual((p["entries"], p["nextCursor"], p["hasMore"]), ([0, 1, 2, 3], 4, True))
        p = r.read(p["nextCursor"], limit=0)                  # limit < 1 still makes progress
        self.assertEqual(p["entries"], [4])

    def test_byte_limit_keeps_at_least_one(self):
        r = kit.CursorRing(max_items=100, max_bytes=50, size=len)
        r.append("a" * 30)
        r.append("b" * 30)
        self.assertEqual((len(r), r.bytes), (1, 30))
        r.append("c" * 200)                                   # bigger than the limit on its own
        self.assertEqual(r.read()["entries"], ["c" * 200])

    def test_clear_keeps_numbering(self):
        r = kit.CursorRing()
        r.extend("abc")
        r.clear()
        self.assertEqual((len(r), r.bytes, r.buffer_start), (0, 0, 3))
        self.assertEqual(r.read(1)["missed"], 2)
        self.assertEqual(r.append("d"), 3)

    def test_unserializable_entries_have_a_size(self):
        r = kit.CursorRing()
        r.append({"x": object()})                             # default=str
        r.append({1j})                                        # TypeError: guessed size
        self.assertGreater(r.bytes, 0)

    async def test_wait_read_wakes_on_append_from_a_thread(self):
        r = kit.CursorRing()
        r.append("old")
        waiting = asyncio.create_task(r.wait_read(1, timeout=10))
        await asyncio.sleep(0)
        self.assertEqual(len(r._waiters), 1)
        th = threading.Thread(target=r.append, args=("new",))
        th.start()
        th.join()
        page = await asyncio.wait_for(waiting, 5)
        self.assertEqual(page["entries"], ["new"])
        self.assertEqual(r._waiters, [])

    async def test_wait_read_times_out_and_leaves_no_waiter(self):
        r = kit.CursorRing()
        page = await r.wait_read(None, timeout=0.01)
        self.assertEqual((page["entries"], page["cursor"]), ([], 0))
        self.assertEqual(r._waiters, [])
        t = asyncio.create_task(r.wait_read(0, timeout=10))
        await asyncio.sleep(0)
        t.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t
        self.assertEqual(r._waiters, [])                      # cancelled readers don't pile up

    async def test_wait_read_returns_immediately_with_data_or_zero_timeout(self):
        r = kit.CursorRing()
        self.assertEqual((await r.wait_read(None, timeout=0))["entries"], [])
        r.append(1)
        self.assertEqual((await r.wait_read(0, timeout=10))["entries"], [1])


if __name__ == "__main__":
    unittest.main()
