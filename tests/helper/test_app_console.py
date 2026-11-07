"""App console (helper/iosstudio/device/features/console/core.py): line splitting, buffer caps, rate cap, cursor reads and the
console.* commands' session lifecycle, with a fake backend instead of an iPhone.

    python -m pytest tests/helper/test_app_console.py
"""
from __future__ import annotations

import asyncio
import unittest

from iosstudio.device.features.console import core as C
from iosstudio.device.kit import FeatureRegistry


class Splitting(unittest.TestCase):
    def test_lines_crlf_and_partial(self):
        sp = C.LineSplitter()
        self.assertEqual(sp.feed(b"hello\r\nwor"), [("hello", 0)])
        self.assertTrue(sp.pending)
        self.assertEqual(sp.feed(b"ld\n\n"), [("world", 0), ("", 0)])
        self.assertFalse(sp.pending)
        self.assertIsNone(sp.flush())

    def test_partial_line_flushes(self):
        sp = C.LineSplitter()
        sp.feed(b"Loading...")
        self.assertEqual(sp.flush(), ("Loading...", 0))
        self.assertFalse(sp.pending)

    def test_utf8_split_across_chunks_and_invalid_bytes(self):
        sp = C.LineSplitter()
        snow = "☃".encode()
        self.assertEqual(sp.feed(b"a" + snow[:1]), [])
        self.assertEqual(sp.feed(snow[1:] + b"\n"), [("a☃", 0)])
        (line, cut), = sp.feed(b"bad \xff\xfe end\n")
        self.assertEqual(line, "bad �� end")
        self.assertEqual(cut, 0)

    def test_long_line_keeps_head_and_counts_the_rest(self):
        sp = C.LineSplitter(max_line=100)
        out = sp.feed(b"x" * 60)
        out += sp.feed(b"y" * 60)
        out += sp.feed(b"z" * 1000 + b"\nnext\n")
        self.assertEqual(len(out), 2)
        text, cut = out[0]
        self.assertEqual(text, "x" * 60 + "y" * 40)
        self.assertEqual(cut, 20 + 1000)
        self.assertEqual(out[1], ("next", 0))

    def test_long_line_cut_never_splits_a_character(self):
        sp = C.LineSplitter(max_line=16)
        (text, cut), = sp.feed("é".encode() * 20 + b"\n")    # 2 bytes each
        self.assertEqual(text, "é" * 8)
        self.assertEqual(cut, 24)
        sp = C.LineSplitter(max_line=17)
        (text, cut), = sp.feed("é".encode() * 20 + b"\n")
        self.assertEqual(text, "é" * 8)                      # 17 would split the 9th; back off to 16
        self.assertEqual(cut, 24)

    def test_escapes_and_controls_are_removed(self):
        self.assertEqual(C.clean(b"\x1b[31mred\x1b[0m \x07bell\r"), "red bell")
        self.assertEqual(C.clean(b"10%\r50%\r100%"), "100%")
        self.assertEqual(C.clean(b"tab\tok"), "tab\tok")


class SessionBuffer(unittest.TestCase):
    def test_ring_is_bounded_and_reads_with_a_cursor(self):
        s = C.ConsoleSession("c1", "com.example.app", ring_lines=50, rate=10 ** 6, burst=10 ** 6)
        s.feed("out", b"".join(b"line %d\n" % i for i in range(120)))
        self.assertEqual(s.ring.seq, 120)
        self.assertEqual(len(s.ring), 50)
        page = s.ring.read(0, limit=10)
        self.assertEqual(page["missed"], 70)
        self.assertEqual(page["cursor"], 70)
        self.assertEqual(page["entries"][0]["text"], "line 70")
        nxt = s.ring.read(page["nextCursor"], limit=1000)
        self.assertEqual(nxt["entries"][-1]["text"], "line 119")
        self.assertFalse(nxt["hasMore"])

    def test_ring_byte_cap(self):
        s = C.ConsoleSession("c1", "com.example.app", ring_bytes=10_000, rate=10 ** 6, burst=10 ** 6)
        s.feed("out", (b"x" * 900 + b"\n") * 100)
        self.assertLessEqual(s.ring.bytes, 10_000)
        self.assertEqual(s.ring.seq, 100)

    def test_rate_cap_drops_and_says_so(self):
        s = C.ConsoleSession("c1", "com.example.app", rate=1, burst=5)
        kept = s.feed("out", b"".join(b"%d\n" % i for i in range(20)))
        self.assertEqual(kept, 5)
        self.assertEqual(s.dropped, 15)
        s._tokens = 5                       # time passes
        s.feed("out", b"later\n")
        texts = [e["text"] for e in s.ring.read(0, 100)["entries"]]
        self.assertIn("15 lines dropped", texts[5])
        self.assertEqual(texts[-1], "later")
        self.assertEqual(s.ring.read(0, 100)["entries"][5]["stream"], "meta")

    def test_cut_lines_are_marked(self):
        s = C.ConsoleSession("c1", "com.example.app")
        s.feed("out", b"a" * (C.MAX_LINE + 10) + b"\n")
        e = s.ring.read(0, 5)["entries"][0]
        self.assertEqual(len(e["text"]), C.MAX_LINE)
        self.assertEqual(e["cut"], 10)
        self.assertEqual(s.cut_lines, 1)

    def test_partial_flush_waits_for_quiet(self):
        s = C.ConsoleSession("c1", "com.example.app")
        s.feed("stdout", b"Fetching", now=100.0)
        self.assertEqual(s.flush_partial(idle=1.0, now=100.5), 0)
        self.assertEqual(s.flush_partial(idle=1.0, now=101.5), 1)
        self.assertEqual(s.ring.read(0, 5)["entries"][0]["text"], "Fetching")

    def test_unsent_batches_and_skips_overflow(self):
        s = C.ConsoleSession("c1", "com.example.app", rate=10 ** 6, burst=10 ** 6)
        s.feed("out", b"a\nb\nc\n")
        first, lines, skipped = s.unsent(limit=2)
        self.assertEqual((first, [x["text"] for x in lines], skipped), (0, ["a", "b"], 1))
        self.assertEqual(s.unsent(), (3, [], 0))


# ------------------------------------------------------------------ commands, with a fake backend

class FakeStream:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def read(self) -> bytes:
        return await self.q.get()

    async def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self, split=False, unavailable=False):
        self.events: list = []
        self.split = split
        self.unavailable = unavailable
        self.streams: list[FakeStream] = []
        self.plain = 0

    def emit(self, name, data):
        self.events.append((name, data))

    @staticmethod
    def err(code, message, fix=None, detail=None):
        e = RuntimeError(message)
        e.code = code
        return e

    async def plain_launch(self, ctx, a):
        self.plain += 1
        return {"bundleId": a["bundleId"], "pid": 7}

    async def launch(self, ctx, sess, a):
        if self.unavailable:
            raise C.ConsoleUnavailable("no console here")
        names = ["stdout", "stderr"] if self.split else ["out"]
        rs = []
        for n in names:
            st = FakeStream()
            self.streams.append(st)
            rs.append(C.Reader(n, st.read, st.close))
        return {"bundleId": "XTL-T." + a["bundleId"], "pid": 42}, rs


class Ctx:
    def client(self):
        return None


def setup(backend):
    reg = FeatureRegistry(log=lambda *_: None)
    mod = reg.module("console")
    sessions = C.register_commands(mod, backend)
    mod.commit()
    return reg, sessions


class Commands(unittest.IsolatedAsyncioTestCase):
    async def call(self, reg, cmd, args):
        return await reg.lookup(cmd)(Ctx(), args)

    async def test_launch_read_and_exit(self):
        be = FakeBackend()
        reg, sessions = setup(be)
        res = await self.call(reg, "console.launch", {"bundleId": "com.example.app"})
        self.assertEqual(res["pid"], 42)
        sid = res["console"]["sessionId"]
        self.assertEqual(res["console"]["streams"], "merged")
        be.streams[0].q.put_nowait(b"hello\r\nworld\r\npart")
        page = await self.call(reg, "console.read", {"bundleId": "com.example.app", "wait": 2})
        self.assertEqual([e["text"] for e in page["entries"]], ["hello", "world"])
        self.assertEqual(page["nextCursor"], 2)
        be.streams[0].q.put_nowait(b"")                          # the app exited
        for _ in range(50):
            if not sessions[sid].live:
                break
            await asyncio.sleep(0.02)
        s = sessions[sid]
        self.assertEqual(s.status, "ended")
        self.assertTrue(be.streams[0].closed)
        self.assertEqual(s.tasks, [])
        page = await self.call(reg, "console.read", {"sessionId": sid, "cursor": 2})
        self.assertEqual([e["text"] for e in page["entries"]], ["part"])   # the partial line, flushed at exit
        self.assertFalse(page["session"]["running"])
        await asyncio.sleep(0.15)
        states = [d["status"] for n, d in be.events if n == "consoleState"]
        self.assertEqual(states[-1], "ended")
        lines = [x["text"] for n, d in be.events if n == "console" for x in d["lines"]]
        self.assertEqual(lines, ["hello", "world", "part"])
        await reg.run_hooks("shutdown", Ctx())

    async def test_split_streams_and_stop_closes_everything(self):
        be = FakeBackend(split=True)
        reg, sessions = setup(be)
        res = await self.call(reg, "console.launch", {"bundleId": "com.example.app"})
        sid = res["console"]["sessionId"]
        self.assertEqual(res["console"]["streams"], "split")
        be.streams[1].q.put_nowait(b"oops\n")
        page = await self.call(reg, "console.read", {"sessionId": sid, "wait": 2})
        self.assertEqual(page["entries"][0]["stream"], "stderr")
        out = await self.call(reg, "console.stop", {"sessionId": sid})
        self.assertEqual(out["status"], "stopped")
        self.assertTrue(all(st.closed for st in be.streams))
        self.assertEqual(sessions[sid].tasks, [])
        await reg.run_hooks("shutdown", Ctx())

    async def test_unavailable_console_falls_back_to_a_plain_launch(self):
        be = FakeBackend(unavailable=True)
        reg, sessions = setup(be)
        res = await self.call(reg, "console.launch", {"bundleId": "com.example.app"})
        self.assertEqual(be.plain, 1)
        self.assertEqual(res["console"]["error"]["code"], "console-unavailable")
        self.assertEqual(sessions, {})

    async def test_live_limit_and_kept_sessions(self):
        be = FakeBackend()
        reg, sessions = setup(be)
        for i in range(C.MAX_LIVE + 2):
            await self.call(reg, "console.launch", {"bundleId": f"com.example.app{i}"})
        live = [s for s in sessions.values() if s.live]
        self.assertEqual(len(live), C.MAX_LIVE)
        self.assertEqual(sessions["c1"].status, "stopped")
        # a relaunch of the same app doesn't evict another one
        await self.call(reg, "console.launch", {"bundleId": f"com.example.app{C.MAX_LIVE + 1}"})
        self.assertEqual(len([s for s in sessions.values() if s.live]), C.MAX_LIVE + 1)
        await reg.run_hooks("shutdown", Ctx())
        self.assertTrue(all(not s.live for s in sessions.values()))
        self.assertLessEqual(len(sessions), C.KEEP_ENDED)
        self.assertTrue(all(st.closed for st in be.streams))

    async def test_device_switch_stops_capture(self):
        be = FakeBackend()
        reg, sessions = setup(be)
        res = await self.call(reg, "console.launch", {"bundleId": "com.example.app"})
        await reg.run_hooks("session_end", Ctx(), None)
        self.assertEqual(sessions[res["console"]["sessionId"]].status, "stopped")

    async def test_errors(self):
        reg, _ = setup(FakeBackend())
        with self.assertRaises(RuntimeError) as cm:
            await self.call(reg, "console.read", {"bundleId": "com.nope"})
        self.assertEqual(cm.exception.code, "not-found")
        with self.assertRaises(RuntimeError) as cm:
            await self.call(reg, "console.launch", {})
        self.assertEqual(cm.exception.code, "bad-args")

    async def test_clear_keeps_numbering(self):
        be = FakeBackend()
        reg, sessions = setup(be)
        res = await self.call(reg, "console.launch", {"bundleId": "com.example.app"})
        be.streams[0].q.put_nowait(b"a\nb\n")
        await self.call(reg, "console.read", {"bundleId": "com.example.app", "wait": 2})
        await self.call(reg, "console.clear", {})
        be.streams[0].q.put_nowait(b"c\n")
        page = await self.call(reg, "console.read", {"sessionId": res["console"]["sessionId"], "cursor": 0, "wait": 2})
        self.assertEqual([e["text"] for e in page["entries"]], ["c"])
        self.assertEqual(page["cursor"], 2)
        self.assertEqual(page["missed"], 2)
        await reg.run_hooks("shutdown", Ctx())


if __name__ == "__main__":
    unittest.main()
