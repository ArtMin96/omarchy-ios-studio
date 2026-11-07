"""Device daemon event-loop hygiene (helper/deviced_core.py, device_link.py, device_broker.py,
deviced_features/syslog.py and mirror.py): tracked background tasks, mtime caches, the tmpfs frame
folder, syslog parsing and filtering on the worker thread, and the mirror's pure stream parts.

    python -m pytest tests/helper/test_daemon_loop_perf.py
"""
from __future__ import annotations

import asyncio
import os
import struct
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import common
import device_broker as B
import device_link as link
import deviced_core as core
from deviced_features import mirror as M
from deviced_features import syslog as SL

# ---------------------------------------------------------------- tasks and caches

class TaskSetTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracks_until_done(self):
        ts = core.TaskSet()
        ev = asyncio.Event()
        t = ts.spawn(ev.wait(), name="waiter")
        self.assertEqual(len(ts), 1)
        ev.set()
        await t
        await asyncio.sleep(0)
        self.assertEqual(len(ts), 0)

    async def test_close_waits_grace_then_cancels(self):
        ts = core.TaskSet()
        quick = ts.spawn(asyncio.sleep(0.01))
        slow = ts.spawn(asyncio.sleep(60))
        t0 = time.monotonic()
        await ts.close(grace=0.2)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertTrue(quick.done() and not quick.cancelled())
        self.assertTrue(slow.cancelled())
        self.assertEqual(len(ts), 0)

    async def test_failure_is_retrieved(self):
        ts = core.TaskSet()

        async def boom():
            raise RuntimeError("x")
        t = ts.spawn(boom())
        await asyncio.gather(t, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertEqual(len(ts), 0)


class MtimeFileTests(unittest.TestCase):
    def test_reloads_only_on_change(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            loads = []

            def load(path):
                loads.append(1)
                return path.read_text()
            f = core.MtimeFile(p, load, "none")
            self.assertEqual(f.get(), "none")       # missing -> default, nothing loaded
            p.write_text("a")
            self.assertEqual(f.get(), "a")
            self.assertEqual(f.get(), "a")
            self.assertEqual(len(loads), 1)
            p.write_text("bb")                       # size changes even within one mtime tick
            self.assertEqual(f.get(), "bb")
            self.assertEqual(len(loads), 2)
            p.unlink()
            self.assertEqual(f.get(), "none")

    def test_bad_content_gives_default(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "x"
            p.write_text("{")
            f = core.MtimeFile(p, lambda q: common.json.loads(q.read_text()), {})
            self.assertEqual(f.get(), {})


class PairRecordCacheTests(unittest.TestCase):
    def test_records_reread_only_when_folder_changes(self):
        with TemporaryDirectory() as d:
            remote, lockdown = Path(d) / "remote", Path(d) / "lockdown"
            remote.mkdir()
            lockdown.mkdir()
            reads = []

            class Pmd3:
                remote_dir = remote

                @staticmethod
                def iter_remote_pair_records_by_identifier():
                    reads.append(1)
                    for p in sorted(remote.glob("remote_*")):
                        yield p.name.split("remote_", 1)[1].split(".")[0], p, {}
            pr = link.PairRecords(Pmd3, lockdown)
            self.assertEqual(pr.read(), ({}, set()))
            n = len(reads)
            for _ in range(5):
                pr.read()
            self.assertEqual(len(reads), n)              # nothing changed: no re-read
            (remote / "remote_UDID1.plist").write_bytes(b"x")
            (lockdown / "UDID2.plist").write_bytes(b"x")
            (lockdown / "SystemConfiguration.plist").write_bytes(b"x")
            wifi, usb = pr.read()
            self.assertEqual(set(wifi), {"UDID1"})
            self.assertEqual(usb, {"UDID2"})
            n = len(reads)
            (remote / "remote_UDID1.plist").write_bytes(b"longer")   # rewritten in place
            pr.read()
            self.assertEqual(len(reads), n + 1)

    def test_signature_of_missing_folder(self):
        self.assertIsNone(link.dir_signature(Path("/nonexistent/ios-studio-test")))


class FramesDirTests(unittest.TestCase):
    def test_frames_go_to_the_runtime_dir_not_the_cache(self):
        d = core.frames_dir()
        self.assertEqual(d, Path(os.environ["IOSSTUDIO_RUNTIME_DIR"]) / "ios-studio" / "screens")
        self.assertFalse(str(d).startswith(str(common.CACHE_DIR)))
        self.assertEqual(core.SCREENS_DIR, common.CACHE_DIR / "screens")   # screenshots stay in the cache

    def test_sink_writes_rotating_files_there(self):
        with TemporaryDirectory() as d:
            sink = M.FrameSink(Path(d))
            sink.reset("UD/ID")
            paths = []
            orig = M.emit
            M.emit = lambda name, data: paths.append(data["path"])
            try:
                for _ in range(M.FRAME_FILES + 2):
                    sink.publish(b"jpeg", "jpg", 2, 2, "stream")
            finally:
                M.emit = orig
            self.assertEqual(len(set(paths)), M.FRAME_FILES)
            self.assertTrue(all(p.startswith(d) for p in paths))
            self.assertEqual(len(list(Path(d).iterdir())), M.FRAME_FILES)


# ---------------------------------------------------------------- syslog

def raw_record(i: int, proc: str = "MyApp", level: int = 0x00, msg: str = "", sub: str = "com.x", cat: str = "c",
               pid: int = 812) -> bytes:
    """A raw os_trace record in pymobiledevice3's _SyslogRecordHeader layout."""
    filename = f"/private/var/containers/{proc}".encode() + b"\0"
    image = b"/usr/lib/libSystem.B.dylib\0"
    m = (msg or f"line {i}").encode() + b"\0"
    s, c = (sub.encode() + b"\0", cat.encode() + b"\0") if sub else (b"", b"")
    hdr = (b"\0" * 9 + struct.pack("<IQ", pid, pid) + b"\0" * 34
           + struct.pack("<III", 1_700_000_000, 0, 250_000) + b"\0" + bytes([level]) + b"\0" * 6
           + struct.pack("<QQ", i, 1) + b"\0" * 16
           + struct.pack("<HHHIIII", len(image), len(m), 0, 0, len(s), len(c), 0))
    return hdr + filename + image + m + s + c


class Client:
    def __init__(self):
        self.events: list[dict] = []

    def send(self, obj):
        self.events.append(obj)

    def lines(self):
        return [e for ev in self.events if ev.get("event") == "syslog" for e in ev["data"]]


class Backend:
    def __init__(self):
        self.opened: list[int] = []

    def syslog_target(self, a):
        return "UDID"

    def syslog_open(self, pid):
        self.opened.append(pid)

    async def syslog_close(self):
        pass

    async def syslog_resolve(self, bid):
        return "MyApp", bid


class SyslogDecodeTests(unittest.TestCase):
    def test_fast_entry(self):
        e = SL.fast_entry(raw_record(1, level=0x10, msg="héllo"), 0)
        self.assertEqual(e, {"ts": 1_700_000_000.25, "process": "MyApp", "pid": 812, "level": "error",
                             "subsystem": "com.x", "category": "c", "message": "héllo",
                             "image": "libSystem.B.dylib"})
        self.assertIsNone(SL.fast_entry(raw_record(1, level=0x02), SL.LEVELS["info"]))   # debug cut
        e = SL.fast_entry(raw_record(1, sub=""), 0)
        self.assertEqual((e["subsystem"], e["category"]), ("", ""))

    def test_long_message_is_cut(self):
        e = SL.fast_entry(raw_record(1, msg="x" * (SL.SYSLOG_MSG_MAX + 50)), 0)
        self.assertEqual(len(e["message"]), SL.SYSLOG_MSG_MAX + 1)
        self.assertTrue(e["message"].endswith("…"))

    def test_same_entry_tolerates_float_noise(self):
        a = SL.fast_entry(raw_record(1), 0)
        self.assertTrue(SL.same_entry(a, {**a, "ts": a["ts"] + 1e-7}))
        self.assertFalse(SL.same_entry(a, {**a, "message": "other"}))
        self.assertFalse(SL.same_entry(a, None))


class SyslogFilterTests(unittest.TestCase):
    def test_match_uses_compiled_filters(self):
        sub = B.SyslogSub(Client(), B.norm_filters({"processes": ["MyApp"], "minLevel": "info", "text": "HELLO"}))
        e = {"level": "default", "pid": 1, "process": "MyApp", "message": "say hello", "subsystem": ""}
        self.assertTrue(sub.match(e))
        self.assertFalse(sub.match({**e, "level": "debug"}))
        self.assertFalse(sub.match({**e, "process": "Other"}))
        sub.filters = B.norm_filters({"pids": [2]})          # replaced: recompiled
        self.assertEqual(sub.min, 0)
        self.assertFalse(sub.match(e))
        self.assertTrue(sub.match({**e, "pid": 2}))

    def test_bundle_filter_follows_resolution(self):
        sub = B.SyslogSub(Client(), {"bundleId": "com.example.app"})
        e = {"level": "default", "pid": 1, "process": "MyApp", "message": "", "subsystem": ""}
        self.assertFalse(sub.match(e))
        sub.set_resolved("MyApp", "com.example.app")
        self.assertTrue(sub.match(e))

    def test_route_parses_once_and_sorts(self):
        a = B.SyslogSub(Client(), {"minLevel": "error"})
        b = B.SyslogSub(Client(), {"processes": ["MyApp"]})
        raws = [raw_record(0, level=0x10), raw_record(1, proc="Other"), raw_record(2)]
        out = dict(B.route(raws, (a, b), SL.fast_entry, 0))
        self.assertEqual([e["message"] for e in out[a]], ["line 0"])
        self.assertEqual([e["message"] for e in out[b]], ["line 0", "line 2"])
        pauses = []
        B.route([raw_record(i) for i in range(40)], (b,), SL.fast_entry, 0, lambda: pauses.append(1))
        self.assertEqual(len(pauses), (40 - 1) // B.SYSLOG_YIELD_EVERY)


class SyslogWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = B.Broker(demo=None)
        self.broker.loop = asyncio.get_running_loop()
        self.broker.backend = Backend()
        self.hub = self.broker.syslog

    async def asyncTearDown(self):
        for c in list(self.hub.subs):
            await self.hub.stop(c)
        await self.hub.close()
        for t in list(self.broker._bg):
            t.cancel()

    async def _settle(self, cond, timeout=3.0):
        t0 = time.monotonic()
        while not cond() and time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.02)

    async def test_batches_are_filtered_off_the_loop(self):
        app, errors = Client(), Client()
        await self.hub.start(app, {"filters": {"processes": ["MyApp"]}})
        await self.hub.start(errors, {"filters": {"minLevel": "error"}})
        self.hub.feed_batch([raw_record(i, proc="MyApp" if i % 2 else "Other", level=0x10 if i % 5 == 0 else 0)
                             for i in range(100)], SL.fast_entry)
        self.assertIsNotNone(self.hub.worker)
        self.assertTrue(self.hub.worker.thread.is_alive())
        await self._settle(lambda: len(app.lines()) == 50 and len(errors.lines()) == 20)
        self.assertEqual([e["message"] for e in app.lines()][:3], ["line 1", "line 3", "line 5"])
        self.assertEqual(len(errors.lines()), 20)

    async def test_buffer_is_bounded_and_drops_counted(self):
        c = Client()
        await self.hub.start(c, {})
        sub = self.hub.subs[c]
        self.hub.deliver([(sub, [{"i": i} for i in range(B.SYSLOG_BUFFER + 30)])])
        self.assertEqual(len(sub.buf), B.SYSLOG_BUFFER)
        self.assertEqual(sub.dropped, 30)
        self.assertEqual(sub.buf[0], {"i": 30})                       # the oldest went

    async def test_results_for_a_gone_client_are_ignored(self):
        c = Client()
        await self.hub.start(c, {})
        sub = self.hub.subs[c]
        await self.hub.stop(c)
        self.hub.deliver([(sub, [{"i": 1}])])
        self.assertEqual(len(sub.buf), 0)

    async def test_worker_stops_with_the_last_subscriber(self):
        c = Client()
        await self.hub.start(c, {})
        self.hub.feed_batch([raw_record(1)], SL.fast_entry)
        thread = self.hub.worker.thread
        await self.hub.stop(c)
        self.assertIsNone(self.hub.worker)
        thread.join(2)
        self.assertFalse(thread.is_alive())

    async def test_full_queue_drops_whole_batches(self):
        c = Client()
        await self.hub.start(c, {})
        w = B.SyslogWorker(self.hub, asyncio.get_running_loop())
        w.close()                       # thread gone: nothing drains the queue
        self.hub.worker = w
        for _ in range(B.SYSLOG_QUEUE):
            self.assertTrue(self.hub.feed_batch([raw_record(1)], SL.fast_entry))
        self.assertFalse(self.hub.feed_batch([raw_record(1)] * 5, SL.fast_entry))
        self.assertEqual(self.hub.subs[c].dropped, 5)
        self.hub.worker = None

    async def test_gone_client_is_tracked_task(self):
        c = Client()
        await self.hub.start(c, {})
        self.hub.client_gone(c)
        self.assertEqual(self.hub.snapshot, ())      # the worker stops routing to it at once
        self.assertTrue(any(t.get_name() == "syslog-sync" for t in self.broker._bg))
        await asyncio.gather(*self.broker._bg, return_exceptions=True)


class BatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_size_and_timer_flush(self):
        got = []
        b = SL.Batcher(got.append, size=3, wait=0.05)
        for i in range(4):
            b.add(i)
        self.assertEqual(got, [[0, 1, 2]])
        await asyncio.sleep(0.12)
        self.assertEqual(got, [[0, 1, 2], [3]])
        b.add(9)
        b.flush()
        self.assertEqual(got[-1], [9])
        await asyncio.sleep(0.1)
        self.assertEqual(len(got), 3)          # the cancelled timer didn't flush again


# ---------------------------------------------------------------- mirror

def rtp(seq: int, payload: bytes, marker: bool = False) -> bytes:
    return bytes([0x80, (0x80 if marker else 0) | 96]) + seq.to_bytes(2, "big") + b"\0" * 8 + payload


def nal(t: int, body: bytes = b"x") -> bytes:
    return bytes([t << 1, 1]) + body


def one_nal(payload, fu, out):   # stand-in for pymobiledevice3's depacketize_hevc: one NAL per packet
    out.append(bytes(payload))


class DepacketizerTests(unittest.TestCase):
    def test_access_units_and_parameter_sets(self):
        d = M.Depacketizer(one_nal)
        self.assertIsNone(d.push(rtp(1, nal(M._VPS))))
        self.assertIsNone(d.push(rtp(2, nal(M._SPS))))
        self.assertIsNone(d.push(rtp(3, nal(M._PPS))))
        kind, au, key = d.push(rtp(4, nal(19), marker=True))
        self.assertEqual((kind, key, len(au)), ("au", True, 4))
        kind, au, key = d.push(rtp(5, nal(1), marker=True))
        self.assertEqual((kind, key, len(au)), ("au", False, 1))
        d.reset()                                         # new stream: the parameter sets stay
        kind, au, key = d.push(rtp(100, nal(19), marker=True))
        self.assertEqual([(n[0] >> 1) & 0x3F for n in au], [M._VPS, M._SPS, M._PPS, 19])
        self.assertEqual(d.highest, 100)

    def test_gap_breaks_the_picture(self):
        d = M.Depacketizer(one_nal)
        d.push(rtp(1, nal(1)))
        self.assertEqual(d.push(rtp(3, nal(1), marker=True)), ("loss",))
        self.assertEqual(d.push(rtp(4, nal(1), marker=True))[0], "au")

    def test_short_and_rtcp_packets_ignored(self):
        d = M.Depacketizer(one_nal)
        self.assertIsNone(d.push(b"\x80"))
        self.assertIsNone(d.push(bytes([0x80, 0xC9]) + b"\0" * 20))
        self.assertEqual(d.highest, 0)


class RateWindowTests(unittest.TestCase):
    def test_running_total_expires(self):
        w = M.RateWindow(1.0)
        w.add(10.0, 100)
        w.add(10.5, 50)
        self.assertEqual(w.value(10.6), 150)
        self.assertEqual(w.value(11.2), 50)
        self.assertEqual(w.value(20.0), 0)
        self.assertEqual(len(w.items), 0)


class RecorderCapTests(unittest.TestCase):
    def test_max_seconds_clamped_and_frames_capped(self):
        with TemporaryDirectory() as d:
            r = M.Recorder("screenshot", 10_000, None, folder=Path(d))
            try:
                self.assertEqual(r.max_seconds, M.REC_MAX_S)
                r.max_frames = 3
                for i in range(10):
                    r.add_frame(b"jpg", "jpg", float(i))
                self.assertEqual((r.frames, len(r.files), len(r.times)), (3, 3, 3))
            finally:
                r.cleanup()


if __name__ == "__main__":
    unittest.main()
