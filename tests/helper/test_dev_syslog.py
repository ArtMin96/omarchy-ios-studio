"""The phone log behind syslog.start / syslog.filter / syslog.stop: filter semantics (broker.norm_filters,
SyslogSub, SyslogHub), the raw os_trace decoding and stream loop of features/syslog/real.py, and the demo
stream (features/syslog/demo.py) giving entries of the same shape.

Not repeated here (see test_daemon_loop_perf.py): fast_entry basics, SyslogSub compile/replace, route(),
the worker thread and Batcher. Bundle id resolution through the AppIndex: test_lookup_followups.py.

    python -m pytest tests/helper/test_dev_syslog.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_syslog
"""
from __future__ import annotations

import asyncio
import datetime as dt
import plistlib
import struct
import sys
import types
import unittest
from unittest import mock

from support import devtest

devtest.isolate()

from iosstudio.device import broker as B  # noqa: E402
from iosstudio.device.features.syslog import demo as SD  # noqa: E402
from iosstudio.device.features.syslog import real as SL  # noqa: E402

ENTRY_KEYS = {"ts", "process", "pid", "level", "subsystem", "category", "message", "image"}


def raw_record(proc="MyApp", level=0x00, msg="hello", sub="com.x", cat="c", pid=812, sec=1_700_000_000,
               usec=250_000, image="/usr/lib/libSystem.B.dylib") -> bytes:
    """A raw os_trace record in pymobiledevice3's _SyslogRecordHeader layout (what SL._HDR unpacks)."""
    filename = f"/private/var/containers/{proc}".encode() + b"\0"
    img = image.encode() + b"\0" if image else b""
    m = msg.encode() + b"\0"
    s, c = (sub.encode() + b"\0", cat.encode() + b"\0") if sub else (b"", b"")
    hdr = (b"\0" * 9 + struct.pack("<IQ", pid, pid) + b"\0" * 34
           + struct.pack("<III", sec, 0, usec) + b"\0" + bytes([level]) + b"\0" * 6
           + struct.pack("<QQ", 1, 1) + b"\0" * 16
           + struct.pack("<HHHIIII", len(img), len(m), 0, 0, len(s), len(c), 0))
    assert len(hdr) == SL.HEADER_SIZE
    return hdr + filename + img + m + s + c


def entry(**kw) -> dict:
    e = {"ts": 1.0, "process": "MyApp", "pid": 812, "level": "default", "subsystem": "com.example.app",
         "category": "net", "message": "hello", "image": "MyApp"}
    e.update(kw)
    return e


def sub_for(filters: dict) -> B.SyslogSub:
    return B.SyslogSub(Client(), B.norm_filters(filters))


class Client:
    def __init__(self):
        self.events: list[dict] = []

    def send(self, obj):
        self.events.append(obj)

    def lines(self):
        return [e for ev in self.events if ev.get("event") == "syslog" for e in ev["data"]]

    def states(self):
        return [ev["data"] for ev in self.events if ev.get("event") == "syslogState"]


# ---------------------------------------------------------------- filters

class NormFilters(unittest.TestCase):
    def test_junk_is_dropped(self):
        for bad in (None, [], "text", 3):
            self.assertEqual(B.norm_filters(bad), {})
        self.assertEqual(B.norm_filters({"minLevel": "verbose", "processes": [], "pids": [], "text": "",
                                         "subsystem": None, "bundleId": "", "other": 1}), {})

    def test_values_are_normalized(self):
        f = B.norm_filters({"processes": ["MyApp", "", 7], "pids": ["12", 13], "minLevel": "error",
                            "text": 42, "subsystem": "com.x", "bundleId": "com.example.app"})
        self.assertEqual(f, {"processes": ["MyApp", "7"], "pids": [12, 13], "minLevel": "error", "text": "42",
                             "subsystem": "com.x", "bundleId": "com.example.app"})

    def test_input_not_mutated(self):
        src = {"processes": ["A"]}
        B.norm_filters(src)["processes"].append("B")
        self.assertEqual(src, {"processes": ["A"]})


class Match(unittest.TestCase):
    def test_no_filters_match_everything(self):
        sub = sub_for({})
        for lvl in B.LEVELS:
            self.assertTrue(sub.match(entry(level=lvl)))

    def test_min_level_is_inclusive(self):
        sub = sub_for({"minLevel": "error"})
        self.assertFalse(sub.match(entry(level="default")))
        self.assertTrue(sub.match(entry(level="error")))
        self.assertTrue(sub.match(entry(level="fault")))

    def test_unknown_entry_level_counts_as_default(self):
        self.assertTrue(sub_for({"minLevel": "default"}).match(entry(level="notice")))
        self.assertFalse(sub_for({"minLevel": "error"}).match(entry(level="notice")))

    def test_process_is_exact(self):
        sub = sub_for({"processes": ["MyApp", "Widget"]})
        self.assertTrue(sub.match(entry(process="Widget")))
        self.assertFalse(sub.match(entry(process="myapp")))
        self.assertFalse(sub.match(entry(process="MyAppExtension")))

    def test_subsystem_is_a_prefix(self):
        sub = sub_for({"subsystem": "com.example"})
        self.assertTrue(sub.match(entry(subsystem="com.example.app")))
        self.assertFalse(sub.match(entry(subsystem="org.com.example")))
        self.assertFalse(sub.match(entry(subsystem="")))

    def test_text_is_case_insensitive_and_looks_at_process_and_subsystem(self):
        sub = sub_for({"text": "NETWORK"})
        self.assertTrue(sub.match(entry(message="Network down")))
        self.assertTrue(sub.match(entry(message="x", process="NetworkDaemon")))
        self.assertTrue(sub.match(entry(message="x", subsystem="com.apple.network")))
        self.assertFalse(sub.match(entry(message="x", category="network")))   # category isn't searched

    def test_text_is_literal_not_a_regex(self):
        sub = sub_for({"text": "a.c"})
        self.assertFalse(sub.match(entry(message="abc")))
        self.assertTrue(sub.match(entry(message="xa.cx")))
        self.assertTrue(sub_for({"text": "(["}).match(entry(message="bad ([ bracket")))

    def test_bundle_before_resolution_matches_the_id_in_the_message(self):
        sub = sub_for({"bundleId": "com.example.app"})
        self.assertTrue(sub.match(entry(process="SpringBoard", message="launched com.example.app")))
        self.assertFalse(sub.match(entry(process="MyApp", message="hi")))

    def test_bundle_after_resolution(self):
        sub = sub_for({"bundleId": "com.example.app"})
        sub.set_resolved("MyApp", "XTL-TEAM.com.example.app")
        self.assertTrue(sub.match(entry(process="MyApp")))
        self.assertTrue(sub.match(entry(process="kernel", message="memorystatus: MyApp [812]")))   # exe in message
        self.assertTrue(sub.match(entry(process="SpringBoard", message="XTL-TEAM.com.example.app scene")))
        self.assertFalse(sub.match(entry(process="SpringBoard", message="com.example.app")))  # the resolved id now
        sub.set_resolved(None, "com.example.app")      # not installed: only the id in messages
        self.assertFalse(sub.match(entry(process="MyApp", message="hi")))

    def test_all_filters_must_hold(self):
        sub = sub_for({"processes": ["MyApp"], "pids": [812], "minLevel": "info", "subsystem": "com.example",
                       "text": "hello"})
        self.assertTrue(sub.match(entry()))
        for change in ({"process": "Other"}, {"pid": 1}, {"level": "debug"}, {"subsystem": "com.apple"},
                       {"message": "bye", "process": "MyApp"}):
            self.assertFalse(sub.match(entry(**change)), change)


class HubFilters(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        p = mock.patch.object(B, "log", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.broker = B.Broker(demo=None)
        self.broker.loop = asyncio.get_running_loop()
        self.backend = types.SimpleNamespace(opened=[], closed=0)

        async def settle(cmd):
            pass

        async def close():
            self.backend.closed += 1

        async def resolve(bid):
            return "MyApp", "XTL." + bid
        self.backend.settle = settle
        self.backend.syslog_target = lambda a: "UDID"
        self.backend.syslog_open = self.backend.opened.append
        self.backend.syslog_close = close
        self.backend.syslog_resolve = resolve
        self.broker.backend = self.backend
        self.hub = self.broker.syslog

    async def asyncTearDown(self):
        for c in list(self.hub.subs):
            await self.hub.stop(c)
        await self.hub.close()
        for t in list(self.broker._bg):
            t.cancel()
        await asyncio.gather(*self.broker._bg, return_exceptions=True)

    async def test_filter_before_start_is_kept_for_start(self):
        c = Client()
        self.assertEqual(await self.hub.set_filters(c, {"minLevel": "error", "junk": 1}),
                         {"filters": {"minLevel": "error"}})
        self.assertEqual(self.hub.subs, {})
        r = await self.hub.start(c, {})
        self.assertEqual(r, {"running": True, "filters": {"minLevel": "error"}})
        self.assertEqual(c.states()[-1]["filters"], {"minLevel": "error"})
        self.assertEqual(self.hub.pending_filters, {})

    async def test_explicit_filters_win_over_pending(self):
        c = Client()
        await self.hub.set_filters(c, {"minLevel": "error"})
        r = await self.hub.start(c, {"filters": {"processes": ["A"]}})
        self.assertEqual(r["filters"], {"processes": ["A"]})

    async def test_phone_pid_filter_only_when_everyone_wants_the_same_pid(self):
        a, b = Client(), Client()
        await self.hub.start(a, {"filters": {"pids": [812]}})
        self.assertEqual(self.backend.opened, [812])
        await self.hub.start(b, {"filters": {"pids": [812]}})
        self.assertEqual(self.backend.opened, [812])        # unchanged: no restart
        await self.hub.set_filters(b, {"pids": [900]})
        self.assertEqual(self.backend.opened, [812, -1])    # different pids: unfiltered on the phone
        await self.hub.stop(b)
        self.assertEqual(self.backend.opened, [812, -1, 812])
        await self.hub.set_filters(a, {"pids": [812, 813]})
        self.assertEqual(self.backend.opened[-1], -1)       # several pids: unfiltered

    async def test_min_level_is_the_lowest_subscriber(self):
        a, b = Client(), Client()
        await self.hub.start(a, {"filters": {"minLevel": "error"}})
        self.assertEqual(self.hub.min_level(), B.LEVELS["error"])
        await self.hub.start(b, {"filters": {"minLevel": "info"}})
        self.assertEqual(self.hub.min_level(), B.LEVELS["info"])
        await self.hub.stop(b)
        self.assertEqual(self.hub.min_level(), B.LEVELS["error"])

    async def test_bundle_filter_is_resolved_at_start(self):
        c = Client()
        await self.hub.start(c, {"filters": {"bundleId": "com.example.app"}})
        sub = self.hub.subs[c]
        self.assertEqual((sub.exe, sub.bid), ("MyApp", "XTL.com.example.app"))
        self.hub.feed(entry(process="MyApp", message="x"))
        self.hub.feed(entry(process="Other", message="x"))
        self.assertEqual([e["process"] for e in sub.buf], ["MyApp"])

    async def test_failed_resolution_falls_back_to_the_id(self):
        async def boom(bid):
            raise RuntimeError("no tunnel")
        self.backend.syslog_resolve = boom
        c = Client()
        await self.hub.start(c, {"filters": {"bundleId": "com.example.app"}})
        sub = self.hub.subs[c]
        self.assertEqual((sub.exe, sub.bid), (None, "com.example.app"))

    async def test_feed_filters_per_client(self):
        errs, app = Client(), Client()
        await self.hub.start(errs, {"filters": {"minLevel": "error"}})
        await self.hub.start(app, {"filters": {"processes": ["MyApp"], "text": "net"}})
        for e in (entry(level="error", message="net down"), entry(process="Other", level="fault"),
                  entry(message="Network ok"), entry(message="ui")):
            self.hub.feed(e)
        await self.hub.stop(errs)
        await self.hub.stop(app)
        self.assertEqual([(e["process"], e["message"]) for e in errs.lines()],
                         [("MyApp", "net down"), ("Other", "hello")])
        self.assertEqual([e["message"] for e in app.lines()], ["net down", "Network ok"])

    async def test_state_shape_and_stream_failed(self):
        c = Client()
        await self.hub.start(c, {})
        st = c.states()[-1]
        self.assertEqual(set(st), {"running", "udid", "filters", "dropped", "reconnecting"})
        self.assertTrue(st["running"])
        self.hub.stream_failed({"code": "not-trusted", "message": "Trust"})
        st = c.states()[-1]
        self.assertFalse(st["running"])
        self.assertEqual(st["error"]["code"], "not-trusted")
        self.assertEqual(self.hub.subs, {})
        self.assertIsNone(self.hub.error)                # the next start begins clean


# ---------------------------------------------------------------- decoding

def fake_parse_syslog_entry(raw: bytes):
    """pymobiledevice3's parse_syslog_entry, as the object PhoneSyslog._entry reads."""
    pid, sec, usec, level, *_ = SL._HDR.unpack_from(raw)
    e = SL.fast_entry(raw, 0)
    return types.SimpleNamespace(
        level=level, message=e["message"], pid=pid, filename="/x/" + e["process"], image_name="/y/" + e["image"],
        timestamp=dt.datetime.fromtimestamp(sec + usec / 1e6),
        label=types.SimpleNamespace(subsystem=e["subsystem"], category=e["category"]) if e["subsystem"] else None)


def fake_os_trace(parse=fake_parse_syslog_entry):
    return types.SimpleNamespace(parse_syslog_entry=parse, OS_TRACE_RELAY_MESSAGE_FILTER_ALL=65535,
                                 OS_TRACE_RELAY_STREAM_FLAGS_DEFAULT=7)


class Decoding(unittest.TestCase):
    def test_every_raw_level(self):
        for raw, name in SL.RAW_LEVEL.items():
            self.assertEqual(SL.fast_entry(raw_record(level=raw), 0)["level"], name)
        self.assertEqual(SL.fast_entry(raw_record(level=0x7F), 0)["level"], "default")   # unknown byte

    def test_level_cut_for_every_threshold(self):
        for raw, name in SL.RAW_LEVEL.items():
            for min_name, min_lvl in SL.LEVELS.items():
                got = SL.fast_entry(raw_record(level=raw), min_lvl)
                self.assertEqual(got is not None, SL.LEVELS[name] >= min_lvl, (name, min_name))

    def test_no_image_and_bad_utf8(self):
        raw = raw_record(image="", msg="ok")
        self.assertEqual(SL.fast_entry(raw, 0)["image"], "")
        bad = raw_record(msg="xx").replace(b"xx\0", b"\xff\xfe\0")
        self.assertEqual(SL.fast_entry(bad, 0)["message"], "��")

    def test_missing_filename_terminator_raises(self):
        with self.assertRaises(ValueError):
            SL.fast_entry(raw_record()[:SL.HEADER_SIZE] + b"no-terminator", 0)

    def test_pmd3_entry_shape_and_message_cut(self):
        e = types.SimpleNamespace(level=0x11, message="m" * (SL.SYSLOG_MSG_MAX + 3), pid=5, filename="/bin/launchd",
                                  image_name=None, timestamp=None, label=None)
        out = SL.PhoneSyslog._entry(e, 0)
        self.assertEqual(set(out), ENTRY_KEYS)
        self.assertEqual((out["level"], out["process"], out["image"], out["subsystem"]), ("fault", "launchd", "", ""))
        self.assertEqual(len(out["message"]), SL.SYSLOG_MSG_MAX + 1)
        self.assertIsInstance(out["ts"], float)                   # no timestamp: now
        self.assertIsNone(SL.PhoneSyslog._entry(e, SL.LEVELS["fault"] + 1))

    def test_fast_and_pmd3_entries_agree(self):
        for raw in (raw_record(), raw_record(level=0x10, sub=""), raw_record(level=0x02, msg="é")):
            self.assertTrue(SL.same_entry(SL.fast_entry(raw, 0), SL.PhoneSyslog._entry(fake_parse_syslog_entry(raw), 0)))


class RawDecoder(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(SL, "dlog", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def decoder(self, parse=fake_parse_syslog_entry):
        with mock.patch.dict(sys.modules, {"pymobiledevice3.services.os_trace": fake_os_trace(parse)}):
            return SL.raw_decoder()

    def test_agreeing_layout_uses_the_fast_decoder(self):
        calls = []

        def counting(raw):
            calls.append(1)
            return fake_parse_syslog_entry(raw)
        parse = self.decoder(counting)
        self.assertEqual(parse(raw_record(msg="first"), 0)["message"], "first")
        self.assertEqual(len(calls), 1)
        for i in range(5):
            self.assertEqual(parse(raw_record(msg=f"m{i}"), 0)["message"], f"m{i}")
        self.assertEqual(len(calls), 1)                           # pmd3's parser only for the check

    def test_first_record_respects_the_level_cut(self):
        parse = self.decoder()
        self.assertIsNone(parse(raw_record(level=0x02), SL.LEVELS["info"]))   # debug, cut
        self.assertIsNotNone(parse(raw_record(level=0x10), SL.LEVELS["info"]))

    def test_changed_layout_falls_back_to_pmd3(self):
        def other(raw):
            e = fake_parse_syslog_entry(raw)
            e.message = "pmd3 says " + e.message
            return e
        parse = self.decoder(other)
        self.assertEqual(parse(raw_record(msg="a"), 0)["message"], "pmd3 says a")
        self.assertEqual(parse(raw_record(msg="b"), 0)["message"], "pmd3 says b")


# ---------------------------------------------------------------- the os_trace stream

class FakeService:
    """The lockdown service under OsTraceService: send_plist / recvall over a byte buffer."""

    def __init__(self, data: bytes):
        self.data = data
        self.sent: list[dict] = []

    async def send_plist(self, p):
        self.sent.append(p)

    async def recvall(self, n: int) -> bytes:
        if len(self.data) < n:
            raise asyncio.IncompleteReadError(self.data, n)
        out, self.data = self.data[:n], self.data[n:]
        return out


def stream_bytes(records: list[bytes], status: str = "RequestSuccessful", marker: bytes = b"\x02") -> bytes:
    resp = plistlib.dumps({"Status": status})
    length = len(resp).to_bytes(4, "little")
    out = struct.pack("<I", len(length)) + length + resp
    for r in records:
        out += marker + struct.pack("<I", len(r)) + r
    return out


class FakeOst:
    def __init__(self, data: bytes):
        self.service = FakeService(data)
        self.connected = False
        self.exited = False

    async def connect(self):
        self.connected = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False


class RawRecords(unittest.IsolatedAsyncioTestCase):
    async def collect(self, ost, pid=-1):
        out = []
        with mock.patch.dict(sys.modules, {"pymobiledevice3.services.os_trace": fake_os_trace()}):
            try:
                async for r in SL.raw_records(ost, pid):
                    out.append(r)
            except asyncio.IncompleteReadError:
                pass
        return out

    async def test_framing(self):
        recs = [raw_record(msg="a"), raw_record(msg="bb")]
        ost = FakeOst(stream_bytes(recs))
        self.assertEqual(await self.collect(ost, 812), recs)
        self.assertTrue(ost.connected)
        self.assertEqual(ost.service.sent, [{"Request": "StartActivity", "MessageFilter": 65535, "Pid": 812,
                                             "StreamFlags": 7}])

    async def test_refused_start(self):
        with self.assertRaises(SL.core.DeviceError) as cm:
            await self.collect(FakeOst(stream_bytes([], status="Denied")))
        self.assertEqual(cm.exception.code, "failed")

    async def test_bad_chunk_marker(self):
        with self.assertRaises(ConnectionError):
            await self.collect(FakeOst(stream_bytes([raw_record()], marker=b"\x03")))


class FakeHub:
    def __init__(self):
        self.batches: list = []
        self.states: list = []
        self.failed: list = []
        self.reconnecting = False
        self.got = asyncio.Event()

    async def resolve_missing(self):
        pass

    def stream_state(self, reconnecting, error=None):
        self.states.append((reconnecting, error))

    def feed_batch(self, batch, parse=None):
        self.batches.append([parse(r, 0) if parse else r for r in batch])
        self.got.set()
        return True

    def stream_failed(self, error):
        self.failed.append(error)


class PhoneStream(unittest.IsolatedAsyncioTestCase):
    """PhoneSyslog._run against a fake session whose OsTraceService replays raw records."""

    async def asyncSetUp(self):
        self.hub = FakeHub()
        self.drops: list[str] = []
        self.osts: list[FakeOst] = []
        test = self

        class OsTraceService(FakeOst):
            def __init__(self, lockdown=None):
                super().__init__(stream_bytes([raw_record(msg="one"), raw_record(proc="Other", level=0x10)]))
                test.osts.append(self)

        async def get_rsd(timeout=None):
            return "rsd"
        self.session = types.SimpleNamespace(get_rsd=get_rsd, drop=self.drops.append,
                                             pmd3=types.SimpleNamespace(OsTraceService=OsTraceService))
        for p in (mock.patch.object(SL.core, "BROKER", types.SimpleNamespace(syslog=self.hub)),
                  mock.patch.dict(sys.modules, {"pymobiledevice3.services.os_trace": fake_os_trace()}),
                  mock.patch.object(SL, "dlog", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    async def test_records_reach_the_hub_decoded_and_the_service_closes(self):
        phone = SL.PhoneSyslog()
        phone.open(self.session, -1)
        await asyncio.wait_for(self.hub.got.wait(), 2)
        await phone.close()
        self.assertIsNone(phone.task)
        entries = self.hub.batches[0]
        self.assertEqual([(e["process"], e["level"], e["message"]) for e in entries],
                         [("MyApp", "default", "one"), ("Other", "error", "hello")])
        self.assertTrue(all(set(e) == ENTRY_KEYS for e in entries))
        self.assertEqual(self.hub.states[0], (False, None))      # first connect: state announced
        self.assertTrue(self.osts[0].exited)
        # the stream ended (EOF): treated as a dropped tunnel, so the session reconnects
        self.assertEqual(self.drops, ["syslog stream broke"])
        self.assertEqual(self.hub.states[-1], (True, None))

    async def test_not_paired_on_first_connect_gives_up(self):
        class NotPairedError(Exception):
            pass

        async def get_rsd(timeout=None):
            raise NotPairedError("pair first")
        self.session.get_rsd = get_rsd
        phone = SL.PhoneSyslog()
        phone.open(self.session, -1)
        await asyncio.wait_for(phone.task, 2)
        self.assertEqual(self.hub.failed[0]["code"], "not-trusted")
        self.assertEqual(self.drops, [])


# ---------------------------------------------------------------- demo

class DemoStream(unittest.IsolatedAsyncioTestCase):
    async def test_demo_entries_have_the_real_shape_and_pass_the_same_filters(self):
        fed: list[dict] = []
        got = asyncio.Event()

        async def instant(_s):
            await asyncio.sleep(0)

        def feed(e):
            fed.append(e)
            if len(fed) >= len(SD.LOG_LINES):
                got.set()
        fake = SD.FakeSyslog()
        with mock.patch.object(SD, "asyncio", types.SimpleNamespace(sleep=instant, create_task=asyncio.create_task)):
            fake.open(types.SimpleNamespace(feed=feed), -1)
            await asyncio.wait_for(got.wait(), 2)
            await fake.close()
        self.assertIsNone(fake.task)
        real = SL.fast_entry(raw_record(), 0)
        for e in fed:
            self.assertEqual(set(e), ENTRY_KEYS)
            for k in ENTRY_KEYS:
                self.assertIsInstance(e[k], type(real[k]) if k != "ts" else (int, float), k)
            self.assertIn(e["level"], SL.LEVELS)
        # the per-client filter works on demo lines like on real ones
        sub = sub_for({"processes": ["WeatherGlance"], "minLevel": "error"})
        self.assertEqual({e["message"] for e in fed if sub.match(e)},
                         {m for p, lvl, _s, _c, m in SD.LOG_LINES if p == "WeatherGlance" and lvl in ("error", "fault")})

    async def test_demo_resolve(self):
        fake = SD.FakeSyslog()
        exe, bid = await fake.resolve("com.example.weatherglance")   # -> the installed (team-prefixed) id
        self.assertTrue(bid.endswith(".com.example.weatherglance"), bid)
        self.assertNotIn(" ", exe)
        self.assertEqual(await fake.resolve("com.example.gone"), (None, "com.example.gone"))

    async def test_real_resolve_without_a_tunnel(self):
        self.assertEqual(await SL.PhoneSyslog.resolve(None, "com.example.app"), (None, None))
        self.assertEqual(await SL.PhoneSyslog.resolve(types.SimpleNamespace(rsd=None), "x"), (None, None))


if __name__ == "__main__":
    unittest.main()
