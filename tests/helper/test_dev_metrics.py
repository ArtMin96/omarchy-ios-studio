"""metrics.*: the pure math (features/metrics/stats.py), sessions and commands (core.py), the phone-side parsing
of the real taps (real.py: Sysmontap rows, energy/network/graphics gauges, battery, syslog -> events), and the
replies of the same commands in real and demo mode (demo.py) side by side.

The real module is imported against stub pymobiledevice3 when it isn't installed; the phone is faked at the
service boundary (taps filled by hand, no DVT connection).

    python -m pytest tests/helper/test_dev_metrics.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_metrics
"""
from __future__ import annotations

import asyncio
import csv
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import devtest
from support.pmd3_stub import stub_pmd3

devtest.isolate()

from iosstudio.device import backend, protocol  # noqa: E402
from iosstudio.device.client import DeviceError  # noqa: E402
from iosstudio.device.features.metrics import core as C  # noqa: E402
from iosstudio.device.features.metrics import stats as S  # noqa: E402

with stub_pmd3():   # core/stats stay the modules imported above (they were loaded before the block)
    from iosstudio.device.features.metrics import real as MR

MB = 1024 * 1024


def rows(n, step=1.0, t0=1000.0, **series):
    """n samples; each keyword is a value or a function of the index."""
    out = []
    for i in range(n):
        r = {"ts": t0 + i * step, "pid": 501}
        for k, v in series.items():
            r[k] = v(i) if callable(v) else v
        out.append(r)
    return out


# ---------------------------------------------------------------- stats.py

class Rounding(unittest.TestCase):
    def test_r(self):
        self.assertIsNone(S._r(None))
        self.assertIsNone(S._r(float("nan")))
        self.assertIsNone(S._r(float("inf")))
        self.assertEqual(S._r(2.0), 2)
        self.assertIsInstance(S._r(2.0), int)
        self.assertEqual(S._r(2.25, 1), 2.2)          # banker's rounding of round()
        self.assertEqual(S._r(1.23456, 2), 1.23)
        self.assertIsInstance(S._r(1e16), float)      # huge values stay floats


class Percentile(unittest.TestCase):
    def test_edges(self):
        self.assertIsNone(S.pct([], 0.5))
        self.assertEqual(S.pct([7], 0.95), 7)
        self.assertEqual(S.pct([3, 1, 2], 0.0), 1)
        self.assertEqual(S.pct([3, 1, 2], 1.0), 3)

    def test_linear_interpolation(self):
        self.assertEqual(S.pct([10, 20], 0.5), 15)
        self.assertAlmostEqual(S.pct(list(range(1, 101)), 0.95), 95.05)
        self.assertEqual(S.pct([5, 1, 4, 2, 3], 0.5), 3)

    def test_input_not_sorted_in_place(self):
        xs = [3, 1, 2]
        S.pct(xs, 0.5)
        self.assertEqual(xs, [3, 1, 2])


class SeriesStats(unittest.TestCase):
    def test_empty_and_missing(self):
        self.assertIsNone(S.series_stats([], "cpu"))
        self.assertIsNone(S.series_stats(rows(3, mem=1), "cpu"))
        self.assertIsNone(S.series_stats(rows(3, cpu=None), "cpu"))
        self.assertIsNone(S.series_stats(rows(3, cpu="12"), "cpu"))   # strings aren't numbers

    def test_one_sample(self):
        self.assertEqual(S.series_stats(rows(1, cpu=12.34), "cpu"),
                         {"min": 12.3, "avg": 12.3, "p95": 12.3, "max": 12.3, "last": 12.3, "n": 1})

    def test_values_and_rounding(self):
        st = S.series_stats(rows(4, cpu=lambda i: [10, 20, 30, 40][i]), "cpu")
        self.assertEqual(st, {"min": 10, "avg": 25, "p95": 38.5, "max": 40, "last": 40, "n": 4})
        e = S.series_stats(rows(2, eCpu=lambda i: [0.123, 0.456][i]), "eCpu")
        self.assertEqual((e["min"], e["max"]), (0.12, 0.46))         # energy keys keep 2 decimals

    def test_skips_gaps(self):
        st = S.series_stats(rows(3, cpu=lambda i: [5, None, 15][i]), "cpu")
        self.assertEqual((st["n"], st["avg"], st["last"]), (2, 10, 15))

    def test_nan_sample_is_ignored(self):
        # Was a bug: stats._vals (features/metrics/stats.py:65) lets float NaN through (only isinstance int/float is
        # checked; real.py's _num() passes NaN too). One NaN gauge value makes min (when it comes first), avg
        # and p95 of the whole series None in the summary instead of skipping that one sample.
        st = S.series_stats(rows(3, cpu=lambda i: [float("nan"), 10.0, 20.0][i]), "cpu")
        self.assertEqual((st["min"], st["avg"], st["max"], st["n"]), (10, 15, 20, 2))


class LinReg(unittest.TestCase):
    def test_edges(self):
        self.assertIsNone(S.linreg([]))
        self.assertIsNone(S.linreg([(0, 1), (1, 2)]))
        self.assertIsNone(S.linreg([(5, 1), (5, 2), (5, 3)]))      # no spread in t

    def test_line_and_flat(self):
        slope, icpt, r2 = S.linreg([(t, 2 * t + 1) for t in range(5)])
        self.assertAlmostEqual(slope, 2)
        self.assertAlmostEqual(icpt, 1)
        self.assertAlmostEqual(r2, 1)
        self.assertEqual(S.linreg([(t, 4.0) for t in range(5)]), (0.0, 4.0, 0.0))


class Runs(unittest.TestCase):
    def test_cpu_spike_needs_its_duration(self):
        hot = rows(10, cpu=lambda i: 95 if 2 <= i < 6 else 10)    # 4 samples at 1 s
        self.assertEqual(S.cpu_spikes(hot, 2.5), [{"ts": 1002, "seconds": 4, "peak": 95}])
        short = rows(10, cpu=lambda i: 95 if i == 3 else 10)
        self.assertEqual(S.cpu_spikes(short, 2.5), [])

    def test_a_gap_splits_a_run(self):
        a = rows(3, cpu=99)
        b = rows(3, cpu=99, t0=1100)
        self.assertEqual(len(S.cpu_spikes(a + b, 2.5)), 2)

    def test_spike_at_the_end_counts(self):
        self.assertEqual(len(S.cpu_spikes(rows(6, cpu=lambda i: 99 if i >= 2 else 0), 2.5)), 1)

    def test_fps_idle_is_not_a_drop(self):
        self.assertEqual(S.fps_drops(rows(8, fps=5), 2.5), [])
        self.assertEqual(S.fps_drops(rows(8, fps=60), 2.5), [])
        self.assertEqual(S.fps_drops(rows(8, fps=lambda i: 40 + i), 2.5), [{"ts": 1000, "seconds": 8, "low": 40}])

    def test_gap(self):
        self.assertEqual(S._gap(rows(1)), 2.5)             # one row: 1 s assumed
        self.assertEqual(S._gap(rows(5, step=2.0)), 5.0)
        self.assertEqual(S._gap(rows(5, step=0.1)), 1.0)       # never below a second


class MemoryTrend(unittest.TestCase):
    def test_too_few_points(self):
        self.assertIsNone(S.memory_trend([]))
        self.assertIsNone(S.memory_trend(rows(9, mem=lambda i: 100 + i)))

    def test_steady_growth_is_a_likely_leak(self):
        m = S.memory_trend(rows(300, mem=lambda i: 100 + i * 2 / 60))   # 2 MB/min for 5 min
        self.assertTrue(m["likelyLeak"])
        self.assertEqual((m["slopeMbPerMin"], m["r2"], m["from"]), (2.0, 1, 100))
        self.assertEqual(set(m), {"slopeMbPerMin", "r2", "seconds", "growthMb", "from", "to", "likelyLeak"})

    def test_short_or_flat_or_noisy_is_not(self):
        self.assertFalse(S.memory_trend(rows(60, mem=lambda i: 100 + i * 2 / 60))["likelyLeak"])   # 1 min
        self.assertFalse(S.memory_trend(rows(300, mem=100.0))["likelyLeak"])
        noisy = rows(300, mem=lambda i: 100 + (50 if i % 2 else 0) + i / 600)
        self.assertFalse(S.memory_trend(noisy)["likelyLeak"])

    def test_only_since_the_last_relaunch(self):
        before = rows(200, mem=lambda i: 100 + i)                   # steep growth, then a relaunch
        after = rows(20, t0=1300, mem=50.0)
        for r in after:
            r["pid"] = 777
        m = S.memory_trend(before + after)
        self.assertEqual((m["from"], m["to"], m["likelyLeak"]), (50, 50, False))


class Battery(unittest.TestCase):
    def test_none_without_battery_data(self):
        self.assertIsNone(S.battery(rows(5, cpu=1)))
        self.assertIsNone(S.battery([]))

    def test_drain_and_energy(self):
        r = rows(301, batt=lambda i: 80 - i / 60, battMw=1000)        # 1 %/min for 5 min
        b = S.battery(r)
        self.assertEqual((b["from"], b["to"], b["drainPctPerHour"], b["charging"]), (80, 75, 60, False))
        self.assertEqual(b["mWh"], round(1000 * 300 / 3600, 1))
        self.assertEqual(b["avgMw"], 1000)

    def test_short_span_has_no_drain_and_big_gaps_add_no_energy(self):
        b = S.battery(rows(3, step=50, batt=50, battMw=3600))   # 100 s: too short for a trend
        self.assertNotIn("drainPctPerHour", b)
        self.assertEqual(b["mWh"], 0)                                 # 50 s apart: not integrated

    def test_charging_from_the_last_rows(self):
        r = rows(10, batt=50)
        r[-1]["charging"] = 1
        self.assertTrue(S.battery(r)["charging"])
        r[-1]["charging"] = 0
        r[0]["charging"] = 1
        self.assertFalse(S.battery(r)["charging"])


class Energy(unittest.TestCase):
    def test_shares(self):
        e = S.energy(rows(2, energy=4.0, eCpu=3.0, eGpu=1.0, eNet=0.0))
        self.assertEqual(list(e["byComponent"]), ["CPU", "GPU"])        # biggest first, zeros left out
        self.assertEqual(e["byComponent"]["CPU"], {"avg": 3, "share": 75})
        self.assertEqual((e["avg"], e["max"]), (4, 4))

    def test_parts_only_or_nothing(self):
        self.assertIsNone(S.energy(rows(3, cpu=1)))
        e = S.energy(rows(2, eDisplay=0.0))
        self.assertEqual((e["avg"], e["byComponent"]), (None, {}))


class Downsample(unittest.TestCase):
    def test_edges(self):
        self.assertEqual(S.downsample([], "cpu"), [])
        self.assertEqual(S.downsample(rows(3, mem=1), "cpu"), [])
        one = S.downsample(rows(1, cpu=5), "cpu", 60)
        self.assertEqual(one, [{"t0": 1000, "t1": 1000, "min": 5, "avg": 5, "max": 5, "n": 1}])
        self.assertEqual(len(S.downsample(rows(10, cpu=1), "cpu", 0)), 1)          # at least one bucket
        self.assertEqual(len(S.downsample(rows(10, cpu=1), "cpu", 500)), 10)       # never more than points

    def test_buckets_cover_every_point(self):
        b = S.downsample(rows(100, cpu=lambda i: i), "cpu", 10)
        self.assertEqual(len(b), 10)
        self.assertEqual(sum(x["n"] for x in b), 100)
        self.assertEqual((b[0]["min"], b[-1]["max"]), (0, 99))
        self.assertTrue(all(x["min"] <= x["avg"] <= x["max"] for x in b))


SUMMARY_KEYS = {"window", "series", "cpuSpikes", "fpsDrops", "hitches", "io", "eventCounts", "anomalies"}


class Summarize(unittest.TestCase):
    def test_empty(self):
        out = S.summarize([], [])
        self.assertEqual(set(out), SUMMARY_KEYS)
        self.assertEqual(out["window"], {"samples": 0})
        self.assertEqual(out["cpuSpikes"], {"count": 0, "longest": 0, "items": []})
        self.assertEqual(out["hitches"], {"count": 0, "totalMs": 0, "worstMs": 0})
        self.assertEqual(out["anomalies"], [])

    def test_bad_rows_are_skipped(self):
        out = S.summarize([{"cpu": 1}, "junk", {"ts": "x", "cpu": 2}, {"ts": 5.0, "cpu": 3}], [])
        self.assertEqual(out["window"], {"samples": 1, "start": 5, "end": 5, "seconds": 0})
        self.assertEqual(out["series"]["cpu"]["n"], 1)

    def test_events_and_anomalies_worst_first(self):
        evs = [{"kind": "hitch", "durationMs": 50}, {"kind": "hitch", "durationMs": 120.4},
               {"kind": "hitch"}, {"kind": "thermal"}, {"kind": "crash"}, {"kind": "hang", "durationMs": 1800},
               {"kind": "jetsam"}, {"kind": "jetsam"}]
        samples = rows(30, cpu=lambda i: 95 if i < 10 else 5)
        out = S.summarize(samples, evs, {"name": "Habits"})
        self.assertEqual(out["hitches"], {"count": 3, "totalMs": 170, "worstMs": 120})
        self.assertEqual(out["eventCounts"], {"hitch": 3, "thermal": 1, "crash": 1, "hang": 1, "jetsam": 2})
        kinds = [a["kind"] for a in out["anomalies"]]
        self.assertEqual(kinds, ["jetsam", "crash", "hang", "cpu", "hitch", "thermal"])
        text = {a["kind"]: a["text"] for a in out["anomalies"]}
        self.assertEqual(text["jetsam"], "Habits was killed for using too much memory (jetsam) twice.")
        self.assertEqual(text["crash"], "Habits crashed.")
        self.assertIn("the longest 1.8 s", text["hang"])
        self.assertIn("once (longest 10 s, peak 95%)", text["cpu"])
        self.assertEqual(text["hitch"], "3 hitches: 170 ms spent in slow frames, the worst 120 ms.")

    def test_battery_anomaly_only_when_discharging_fast(self):
        drain = rows(301, batt=lambda i: 80 - i / 60)
        self.assertIn("battery", [a["kind"] for a in S.summarize(drain, [])["anomalies"]])
        drain[-1]["charging"] = 1
        self.assertNotIn("battery", [a["kind"] for a in S.summarize(drain, [])["anomalies"]])
        slow = rows(301, batt=lambda i: 80 - i / 3600)
        self.assertNotIn("battery", [a["kind"] for a in S.summarize(slow, [])["anomalies"]])

    def test_format_helpers(self):
        self.assertEqual([S._times(n) for n in (1, 2, 5)], ["once", "twice", "5 times"])
        self.assertEqual([S._fmt_dur(s) for s in (30, 89.4, 89.6, 100, 600)], ["30 s", "89 s", "1.5 minutes", "1.7 minutes", "10 minutes"])


# ---------------------------------------------------------------- core.py

class Session(unittest.TestCase):
    def test_add_sample_drops_none_and_folds_pending_hitches(self):
        s = C.MetricsSession("m1", {"pid": 7}, ["process"], 1.0)
        s.add_event("hitch", "slow", durationMs=40)
        s.add_event("hitch", "slow", durationMs=10.04)
        row = s.add_sample({"ts": 1.0, "cpu": 3, "mem": None})
        self.assertEqual(row, {"ts": 1.0, "cpu": 3, "pid": 7, "hitchMs": 50.0})
        self.assertEqual(s.add_sample({"ts": 2.0, "cpu": 4}), {"ts": 2.0, "cpu": 4, "pid": 7})

    def test_event_text_is_bounded_and_none_extras_dropped(self):
        s = C.MetricsSession("m1", {"bundleId": "com.x"}, [], 1.0)
        ev = s.add_event("note", "x" * 1000, ts=5.123, durationMs=None, source="kernel")
        self.assertEqual((len(ev["text"]), ev["ts"], ev.get("source")), (400, 5.12, "kernel"))
        self.assertNotIn("durationMs", ev)

    def test_names(self):
        self.assertEqual(C.MetricsSession("m", {"pid": 9}, [], 1).name, "pid 9")
        self.assertEqual(C.MetricsSession("m", {"bundleId": "a.b"}, [], 1).name, "a.b")
        self.assertEqual(C.MetricsSession("m", {"bundleId": "a.b", "name": "App"}, [], 1).name, "App")
        self.assertTrue(C.MetricsSession("m", {"bundleId": "a.b"}, [], 1).follow)
        self.assertFalse(C.MetricsSession("m", {"pid": 9}, [], 1).follow)

    def test_sets(self):
        self.assertEqual(C._sets(None), list(C.DEFAULT_SETS))
        self.assertEqual(C._sets("process, battery,nope"), ["process", "battery"])
        self.assertEqual(C._sets(["nope"]), list(C.DEFAULT_SETS))
        self.assertEqual(C._sets(["hitches"]), ["hitches"])

    def test_state_is_emitted_only_on_change(self):
        got = []
        s = C.MetricsSession("m", {"pid": 9}, [], 1)
        s._emit = lambda name, data: got.append(name)
        s.set_status("running")
        s.set_status("running")
        s.set_status("running", force=True)
        s.set_status("reconnecting", {"code": "unreachable"})
        self.assertEqual(got, ["metrics.state"] * 3)

    def test_export_name_with_a_dot(self):
        # Was a bug: features/metrics/core.py:153 `base.with_suffix(".csv")` treats everything after the last dot of
        # the session name as a suffix: a session named after its bundle id ("com.example.habits", the default
        # when the app has no display name) exports to "com.example.csv", so every export of every such session
        # overwrites the last one (the -events.csv file keeps the full name).
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        s = C.MetricsSession("m3", {"bundleId": "com.example.habits"}, ["process"], 1)
        out = C.export_csv(s, tmp)
        self.assertTrue(Path(out["path"]).name.startswith("com.example.habits-m3-"), out["path"])

    def test_export_csv(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        s = C.MetricsSession("m3", {"bundleId": "com.x", "name": "My App/../x"}, ["process"], 1)
        s.add_sample({"ts": 1000.0, "cpu": 5, "charging": 1})
        s.add_sample({"ts": 1001.0, "mem": 7.5})
        s.add_event("hang", "Hang, \"quoted\"", ts=1000.5, durationMs=900)
        out = C.export_csv(s, tmp)
        self.assertEqual((out["rows"], out["events"]), (2, 1))
        self.assertTrue(Path(out["eventsPath"]).name.startswith("My_App_.._x-m3-"))
        with open(out["path"]) as f:
            table = list(csv.DictReader(f))
        self.assertEqual(list(table[0]), out["columns"])
        self.assertEqual((table[0]["cpu"], table[0]["mem"], table[0]["charging"]), ("5", "", "1"))
        self.assertEqual(table[1]["mem"], "7.5")
        with open(out["eventsPath"]) as f:
            ev = list(csv.DictReader(f))
        self.assertEqual((ev[0]["kind"], ev[0]["durationMs"], ev[0]["text"]), ("hang", "900", 'Hang, "quoted"'))


class FakeMetricsBackend:
    """core.register_commands' backend interface with nothing behind it."""
    export_dir = None

    def __init__(self):
        self.attached: list = []
        self.detached: list = []

    emit = staticmethod(lambda name, data: None)

    @staticmethod
    def err(code, message, fix=None, detail=None):
        return DeviceError(code, message, fix, detail)

    async def attach(self, ctx, sess, a):
        self.attached.append(sess.id)
        sess.set_status("running")

    async def detach(self, ctx, sess):
        self.detached.append(sess.id)

    async def collect(self, ctx, sess):
        return None

    async def hitches(self, ctx, sess, seconds):
        sess.hitch_until = time.time() + seconds


class CoreCtx(backend.Backend):
    features = None

    def __init__(self, mb):
        super().__init__(log=lambda *_: None)
        mod = self.registry.module("metrics")
        self.sessions = C.register_commands(mod, mb)
        mod.commit()


async def code_of(coro) -> str | None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        return protocol.error_json(exc)["code"]
    return None


class Commands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mb = FakeMetricsBackend()
        self.ctx = CoreCtx(self.mb)
        self.addAsyncCleanup(self.ctx.shutdown)

    async def test_start_validation(self):
        for args in ({}, {"pid": ""}, {"pid": "abc"}):
            self.assertEqual(await code_of(self.ctx.dispatch("metrics.start", args)), "bad-args", args)
        self.assertEqual(self.mb.attached, [])

    async def test_interval_floor_and_sets(self):
        d = await self.ctx.dispatch("metrics.start", {"pid": 7, "intervalMs": 10, "sets": "process,hitches"})
        self.assertEqual((d["intervalMs"], d["sets"]), (1000, ["process"]))       # hitches is opt-in, not a set
        self.assertTrue(d["hitchTracking"]["running"])
        d2 = await self.ctx.dispatch("metrics.start", {"pid": 8, "intervalMs": "fast"})
        self.assertEqual(d2["intervalMs"], 1000)

    async def test_same_app_same_client_gets_the_running_session(self):
        a = await self.ctx.dispatch("metrics.start", {"bundleId": "com.x", "sets": ["process"]})
        b = await self.ctx.dispatch("metrics.start", {"bundleId": "com.x", "sets": ["process"]})
        self.assertEqual(a["sessionId"], b["sessionId"])
        c = await self.ctx.dispatch("metrics.start", {"bundleId": "com.x", "sets": ["process", "battery"]})
        self.assertNotEqual(a["sessionId"], c["sessionId"])                      # wants more: a new one

    async def test_running_cap_and_pruning(self):
        ids = [(await self.ctx.dispatch("metrics.start", {"pid": i}))["sessionId"] for i in range(C.MAX_RUNNING)]
        self.assertEqual(await code_of(self.ctx.dispatch("metrics.start", {"pid": 99})), "busy")
        self.assertEqual(await code_of(self.ctx.dispatch("metrics.read", {})), "bad-args")   # which one?
        for sid in ids:
            await self.ctx.dispatch("metrics.stop", {"sessionId": sid})
        for i in range(C.KEEP_STOPPED + 2):
            sid = (await self.ctx.dispatch("metrics.start", {"pid": 100 + i}))["sessionId"]
            await self.ctx.dispatch("metrics.stop", {"sessionId": sid})
        self.assertEqual(len(self.ctx.sessions), C.KEEP_STOPPED)
        self.assertEqual(await code_of(self.ctx.dispatch("metrics.read", {"sessionId": ids[0]})), "not-found")
        self.assertEqual(sorted(self.mb.detached), sorted(self.mb.attached))

    async def test_series_and_read_clamps(self):
        d = await self.ctx.dispatch("metrics.start", {"pid": 7})
        sess = self.ctx.sessions[d["sessionId"]]
        for r in rows(50, cpu=lambda i: i):
            sess.add_sample(r)
        self.assertEqual(await code_of(self.ctx.dispatch("metrics.series", {"metric": "nope"})), "bad-args")
        out = await self.ctx.dispatch("metrics.series", {"metric": "cpu", "points": 10_000})
        self.assertEqual(len(out["buckets"]), 50)
        out = await self.ctx.dispatch("metrics.series", {"metric": "cpu", "points": "x"})
        self.assertEqual(len(out["buckets"]), 50)                                 # default 60 > 50 samples
        page = await self.ctx.dispatch("metrics.read", {"limit": 0})          # 0 = the default
        self.assertEqual(len(page["entries"]), 50)
        page = await self.ctx.dispatch("metrics.read", {"limit": -5})
        self.assertEqual(len(page["entries"]), 1)
        page = await self.ctx.dispatch("metrics.read", {"limit": "x", "cursor": 45})
        self.assertEqual([e["cpu"] for e in page["entries"]], [45, 46, 47, 48, 49])

    async def test_hitches_command(self):
        d = await self.ctx.dispatch("metrics.start", {"pid": 7})
        out = await self.ctx.dispatch("metrics.hitches", {"seconds": 1})
        self.assertTrue(out["hitchTracking"]["running"])
        self.assertLessEqual(out["hitchTracking"]["until"] - time.time(), 5.5)     # at least 5 s, so ≤ 5 + slack
        out = await self.ctx.dispatch("metrics.hitches", {"stop": True})
        self.assertFalse(out["hitchTracking"]["running"])
        await self.ctx.dispatch("metrics.stop", {"sessionId": d["sessionId"]})
        self.assertEqual(await code_of(self.ctx.dispatch("metrics.hitches", {"sessionId": d["sessionId"]})),
                         "bad-args")

    async def test_snapshot_without_sessions(self):
        self.assertEqual(await self.ctx.dispatch("metrics.snapshot", {}), {"sessions": []})


# ---------------------------------------------------------------- real.py parsing

class FakeHub:
    def __init__(self):
        self.dead: list[int] = []
        self.notes: list = []
        self.sessions: dict = {}

    def on_dead(self, pid):
        self.dead.append(pid)

    def broadcast(self, kind, text):
        self.notes.append((kind, text))


ATTRS = ["pid", "cpuUsage", "physFootprint", "memResidentSize", "threadCount", "diskBytesRead", "diskBytesWritten",
         "intWakeups", "platIdleWakeups", "faults"]


class ProcessTapView(unittest.TestCase):
    def tap(self):
        t = MR.ProcessTap(FakeHub())
        t.attrs = list(ATTRS)
        t.shape_logged = True          # (no "first row" debug line on stderr)
        return t

    def test_rates_from_two_rows(self):
        t = self.tap()
        now = time.monotonic()
        t._procs({"Processes": {"501": [501, 12.34, 100 * MB, 80 * MB, 9, 0, 0, 10, 5, 100]}}, now - 1)
        t._procs({"Processes": {"501": [501, 20.0, 101 * MB, 81 * MB, 10, 2048, 1024, 30, 15, 150]}}, now)
        v = t.view(501)
        self.assertEqual(v, {"cpu": 20.0, "mem": 101.0, "rss": 81.0, "threads": 10, "wakeups": 30.0, "faults": 50.0,
                             "diskR": 2.0, "diskW": 1.0})

    def test_first_row_has_no_rates(self):
        t = self.tap()
        t._procs({"Processes": {501: [501, 1.0, MB, MB, 1, 0, 0, 0, 0, 0]}}, time.monotonic())
        v = t.view(501)
        self.assertEqual((v["cpu"], v["wakeups"], v["faults"], v["diskR"]), (1.0, None, None, None))

    def test_counter_going_backwards_gives_no_rate(self):
        t = self.tap()
        now = time.monotonic()
        t._procs({"Processes": {501: [501, 1.0, MB, MB, 1, 5000, 0, 0, 0, 0]}}, now - 1)
        t._procs({"Processes": {501: [501, 1.0, MB, MB, 1, 10, 0, 0, 0, 0]}}, now)   # wrapped / reset
        v = t.view(501)
        self.assertIsNone(v["diskR"])
        self.assertEqual(v["diskW"], 0.0)

    def test_stale_row_reads_zero_rates_and_dead_pids_none(self):
        t = self.tap()
        now = time.monotonic()
        t._procs({"Processes": {501: [501, 1.0, MB, MB, 1, 0, 0, 0, 0, 0]}}, now - 10)
        v = t.view(501)
        self.assertEqual((v["diskR"], v["faults"], v["wakeups"]), (0.0, 0.0, 0.0))
        self.assertIsNone(t.view(999))
        t._procs({"DeadProcesses": [501, "junk"]}, now)
        self.assertIsNone(t.view(501))
        self.assertEqual(t.hub.dead, [501])

    def test_bool_and_junk_values(self):
        t = self.tap()
        t._procs({"Processes": {"x": [1], 501: "junk", 502: [502, True, None, MB, 1, 0, 0, 0, 0, 0]}}, time.monotonic())
        self.assertNotIn(501, t.rows)
        self.assertEqual((t.view(502)["cpu"], t.view(502)["mem"]), (None, None))     # bools aren't numbers

    def test_dead_pids_are_bounded(self):
        t = self.tap()
        t._procs({"DeadProcesses": list(range(MR.DEAD_KEEP + 10))}, time.monotonic())
        self.assertEqual(len(t.dead), MR.DEAD_KEEP)
        self.assertNotIn(0, t.dead)

    def test_system_cpu(self):
        t = self.tap()
        t._sys({"SystemCPUUsage": {"CPU_TotalLoad": 300.0}, "CPUCount": 6}, 1.0)
        self.assertEqual(t.system["sysCpu"], 50.0)
        t._sys({"SystemCPUUsage": 80, "CPUCount": 0}, 2.0)
        self.assertEqual(t.system["sysCpu"], 80.0)
        t.sys_attrs = ["netBytesIn", "netBytesOut"]
        t._sys({"System": [[1, 2], [3, 4]]}, 3.0)
        self.assertEqual((t.system["netBytesIn"], t.system["netBytesOut"]), (3, 4))


class GaugeParsing(unittest.TestCase):
    def test_energy(self):
        e = MR.GaugeTap.parse_energy({"Energy.Cost": 4.567, "energy.cpu.cost": 2, "energy.networkning.cost": 0.5,
                                      "energy.cpu.overhead": 0.25, "energy.gpu.overhead": 0.5})
        self.assertEqual(e, {"energy": 4.57, "eCpu": 2, "eGpu": None, "eNet": 0.5, "eDisplay": None, "eLoc": None,
                             "eOverhead": 0.75, "thermal": None})
        self.assertEqual(MR.GaugeTap.parse_energy({"energy.overhead": 1.0, "energy.cpu.overhead": 9})["eOverhead"], 1.0)
        self.assertTrue(all(v is None for v in MR.GaugeTap.parse_energy("junk").values()))

    def test_network_deltas_and_counters(self):
        g = MR.GaugeTap(FakeHub())
        self.assertEqual(g.parse_net(501, {"net.rx.bytes.delta": 2048, "net.tx.bytes.delta": 1024}),
                         {"netRx": 2.0, "netTx": 1.0})
        clock = devtest.FakeClock()
        with mock.patch.object(MR.time, "monotonic", clock):
            self.assertEqual(g.parse_net(501, {"net.rx.bytes": 1000, "net.tx.bytes": 0}), {})   # first: no rate
            clock.advance(2)
            self.assertEqual(g.parse_net(501, {"net.rx.bytes": 1000 + 4096, "net.tx.bytes": 2048}),
                             {"netRx": 2.0, "netTx": 1.0})
            clock.advance(1)
            self.assertEqual(g.parse_net(501, {"net.rx.bytes": 10, "net.tx.bytes": 2048}),
                             {"netRx": 0.0, "netTx": 0.0})                     # counter reset: not negative

    def test_graphics(self):
        hub = FakeHub()
        g = MR.GaugeTap(hub)
        out = g.parse_graphics({"CoreAnimationFramesPerSecond": 59.94, "Renderer Utilization %": 12.0,
                                "Tiler Utilization %": 30.04, "Alloc system memory": 64 * MB, "recoveryCount": 0})
        self.assertEqual(out, {"fps": 59.9, "gpu": 30.0, "gpuMem": 64.0})
        out = g.parse_graphics({"Device Utilization %": 5, "In use system memory": 0, "recoveryCount": 1})
        self.assertEqual(out, {"fps": None, "gpu": 5, "gpuMem": None})
        self.assertEqual(hub.notes[0][0], "note")                            # the GPU recovered
        g.parse_graphics({"recoveryCount": 1})
        self.assertEqual(len(hub.notes), 1)

    def test_gauge_value_freshness(self):
        gauge = MR.Gauge("energy", "Energy")
        clock = devtest.FakeClock()
        with mock.patch.object(MR.time, "monotonic", clock):
            gauge.latest[5] = (clock(), {"energy": 1})
            self.assertEqual(gauge.value(5), {"energy": 1})
            clock.advance(MR.GAUGE_FRESH_S + 0.1)
            self.assertIsNone(gauge.value(5))
            gauge.failed()
            gauge.failed()
            self.assertEqual((gauge.fails, gauge.down_since), (2, clock()))
            gauge.ok()
            self.assertEqual((gauge.fails, gauge.down_since), (0, None))


class BatteryParsing(unittest.TestCase):
    def test_negative_amperage_as_unsigned_64_bit(self):
        b = MR.BatteryTap.parse({"CurrentCapacity": 81, "InstantAmperage": (1 << 64) - 500, "Voltage": 4000,
                                 "IsCharging": False})
        self.assertEqual(b, {"batt": 81, "battMw": 2000, "charging": 0, "sysPowerInMw": None})

    def test_telemetry_wins(self):
        b = MR.BatteryTap.parse({"InstantAmperage": 100, "Voltage": 4000, "IsCharging": True,
                                 "PowerTelemetryData": {"BatteryPower": 1234, "SystemPowerIn": 5000}})
        self.assertEqual((b["battMw"], b["charging"], b["sysPowerInMw"], b["batt"]), (1234, 1, 5000, None))

    def test_missing(self):
        self.assertEqual(MR.BatteryTap.parse({"PowerTelemetryData": "junk"}),
                         {"batt": None, "battMw": None, "charging": 0, "sysPowerInMw": None})


class SyslogEvents(unittest.TestCase):
    def setUp(self):
        self.hub = MR.Hub(None)
        self.sess = C.MetricsSession("m1", {"bundleId": "com.example.habits"}, ["events"], 1)
        self.sess.pid = 501
        self.sess.data["exe"] = "Habits"
        self.sess.set_status("running")
        self.hub.sessions["m1"] = self.sess

    def kinds(self):
        return [(e["kind"], e.get("durationMs")) for e in self.sess.all_events()]

    def feed(self, **e):
        base = {"ts": 100.0, "process": "", "subsystem": "", "category": "", "level": "default", "message": ""}
        self.hub.on_syslog({**base, **e})

    def test_jetsam_needs_the_app_mentioned(self):
        self.feed(process="kernel", message="memorystatus: killing Safari [90]")
        self.feed(process="kernel", message="memorystatus: killing_specific_process pid 501 [Habits] (per-process-limit)")
        self.assertEqual(self.kinds(), [("jetsam", None)])
        self.feed(process="kernel", message="memorystatus: killing pid 501 again")
        self.assertEqual(len(self.kinds()), 1)                    # repeats within 2 s are one event

    def test_pid_is_matched_as_a_whole_number(self):
        self.feed(subsystem="com.apple.watchdogd", message="pid 5012 timed out")
        self.feed(subsystem="com.apple.watchdogd", message="pid 1.501 timed out")
        self.assertEqual(self.kinds(), [])
        self.feed(subsystem="com.apple.watchdogd", message="pid 501 timed out")
        self.assertEqual(self.kinds(), [("watchdog", None)])

    def test_hang_duration(self):
        self.feed(process="HangTracerd", message="Hang detected in Habits: 1.8 s")
        self.sess.data["lastEvent"].clear()
        self.feed(process="HangTracerd", message="Hang detected in Habits: 250 ms")
        self.assertEqual(self.kinds(), [("hang", 1800.0), ("hang", 250.0)])

    def test_thermal_is_for_everyone_but_not_its_own_errors(self):
        self.feed(process="thermalmonitord", level="error", message="could not read sensor")
        self.feed(process="thermalmonitord", message="<Error> could not find sensor")
        self.assertEqual(self.kinds(), [])
        self.feed(subsystem="com.apple.cltm", message="Thermal level 2")
        self.feed(subsystem="com.apple.cltm", message="Thermal level 3")      # within 30 s: once
        self.assertEqual(self.kinds(), [("thermal", None)])

    def test_sessions_without_events_or_stopped_get_nothing(self):
        self.sess.sets = ["process"]
        self.feed(process="kernel", message="memorystatus: killing Habits")
        self.assertEqual(self.kinds(), [])


# ---------------------------------------------------------------- real vs demo replies

class FakeSession:
    udid = "00008150-METRICS"
    rsd = None
    info: dict = {}
    pmd3 = None

    async def get_rsd(self, timeout=None):
        return "rsd"

    async def ensure_ddi(self):
        pass


class RealCtx(backend.Backend):
    """The real daemon's ctx as metrics uses it. session=None: the taps' loops end at once (no phone)."""
    features = None

    def __init__(self):
        super().__init__(log=lambda *_: None)
        made = []

        class Backend(MR.Backend):
            def __init__(self):
                super().__init__()
                made.append(self)
        mod = self.registry.module("metrics")
        with mock.patch.object(MR, "Backend", Backend):
            MR.register(mod)
        mod.commit()
        self.metrics = made[0]
        self.session = None
        self.fake = FakeSession()

    def need_session(self, a):
        return self.fake


def fill_taps(hub: MR.Hub, pid: int, k: int = 0) -> None:
    """What the phone would have sent the shared taps for `pid` (rows k-1 and k, one second apart)."""
    now = time.monotonic()
    pt = hub.taps["process"]
    pt.attrs = list(ATTRS)
    for j, t in ((k, now - 1), (k + 1, now)):
        pt._procs({"Processes": {pid: [pid, 10.0 + j, (100 + j) * MB, 80 * MB, 9, j * 4096, j * 1024,
                                       j * 10, j * 5, j * 100]}}, t)
    pt._sys({"SystemCPUUsage": {"CPU_TotalLoad": 120.0}, "CPUCount": 6}, now)
    gt = hub.taps[MR.GaugeTap.name]
    gt.energy.latest[pid] = (now, MR.GaugeTap.parse_energy({
        "energy.cost": 5.0, "energy.cpu.cost": 2.0, "energy.gpu.cost": 1.0, "energy.networking.cost": 0.5,
        "energy.display.cost": 1.2, "energy.location.cost": 0.0, "energy.overhead": 0.3,
        "energy.thermalstate.cost": 0.1}))
    gt.network.latest[pid] = (now, gt.parse_net(pid, {"net.rx.bytes.delta": 4096, "net.tx.bytes.delta": 1024}))
    gt.graphics.latest[0] = (now, gt.parse_graphics({"CoreAnimationFramesPerSecond": 60, "Device Utilization %": 20,
                                                     "In use system memory": 48 * MB}))
    hub.taps["battery"].latest = (now, MR.BatteryTap.parse({"CurrentCapacity": 80, "InstantAmperage": -300,
                                                            "Voltage": 4000}))


class RealVsDemo(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def find_app(udid, bid, fetch):
            return {"bundleId": "XTL-TEAM.com.example.habits", "executable": "Habits", "name": "Habits"}

        async def pid_for(b, s, bid):
            return 501
        self.tmp = Path(tempfile.mkdtemp(prefix="metrics-cmd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (mock.patch.object(MR, "find_app", find_app), mock.patch.object(MR.Backend, "_pid_for", pid_for),
                  mock.patch.object(MR.Backend, "emit", staticmethod(lambda *a, **k: None)),
                  mock.patch.object(MR, "dlog", lambda *a, **k: None), mock.patch.object(MR, "EXPORT_DIR", self.tmp)):
            p.start()
            self.addCleanup(p.stop)
        from iosstudio.device.demo.daemon import DemoBackend
        from iosstudio.device.features.metrics import demo as MDemo
        p = mock.patch.object(MDemo.DemoBackend, "export_dir", self.tmp / "demo")
        p.start()
        self.addCleanup(p.stop)
        from iosstudio.device.demo import core as demo_core
        saved = set(demo_core.running), dict(demo_core.pids), set(demo_core.installed)

        def restore():                              # the demo's metrics.start launches the app: undo it for
            demo_core.running.clear()               # later tests (ui demo's front app depends on it)
            demo_core.running.update(saved[0])
            demo_core.pids.clear()
            demo_core.pids.update(saved[1])
            demo_core.installed.clear()
            demo_core.installed.update(saved[2])
        self.addCleanup(restore)
        self.real = RealCtx()
        self.demo = DemoBackend(None)
        self.addAsyncCleanup(self.demo.shutdown)
        self.addAsyncCleanup(self.real.shutdown)

    async def start_both(self):
        real = await self.real.dispatch("metrics.start", {"bundleId": "com.example.habits"})
        demo = await self.demo.dispatch("metrics.start", {"bundleId": "com.example.habits"})
        hub = self.real.metrics.hub
        sess = hub.sessions[real["sessionId"]]
        await asyncio.sleep(0)                      # the taps' loops see no session and end
        for k in range(5):                          # five real rows through the real tap -> row path
            fill_taps(hub, 501, k)
            row = await self.real.metrics.collect(self.real, sess)
            row["ts"] = 2000.0 + k
            sess.add_sample(row)
        sess.add_event("hang", "Hang: the main thread was blocked", durationMs=900.0)
        return real, demo, sess

    async def test_start_shape(self):
        real, demo, _ = await self.start_both()
        self.assertEqual(set(real), set(demo))
        for k in ("sessionId", "bundleId", "name", "pid", "follow", "sets", "intervalMs", "running", "status"):
            self.assertIs(type(real[k]), type(demo[k]), k)
        self.assertEqual((real["status"], real["pid"], real["name"]), ("running", 501, "Habits"))
        self.assertEqual(real["bundleId"], "XTL-TEAM.com.example.habits")
        self.assertTrue(demo["bundleId"].endswith("com.example.habits"))

    async def test_rows_have_the_same_columns(self):
        real, demo, sess = await self.start_both()
        real_row = sess.all_samples()[-1]
        page = await self.demo.dispatch("metrics.read", {"sessionId": demo["sessionId"], "cursor": 0, "limit": 50})
        demo_row = next(r for r in page["entries"] if "hitchMs" not in r)
        self.assertEqual(set(real_row), set(demo_row))
        self.assertLessEqual(set(real_row) - {"ts", "pid", "charging"}, set(S.SERIES))
        self.assertEqual((real_row["cpu"], real_row["diskR"], real_row["sysCpu"], real_row["battMw"]),
                         (15.0, 4.0, 20.0, 1200))

    async def test_collect_is_none_without_data(self):
        real, _demo, _ = await self.start_both()
        hub = self.real.metrics.hub
        sess = hub.sessions[real["sessionId"]]
        for t in hub.taps.values():
            if isinstance(t, MR.ProcessTap):
                t.rows.clear()
                t.system = {}
            if isinstance(t, MR.GaugeTap):
                for g in t.gauges():
                    g.latest.clear()
            if isinstance(t, MR.BatteryTap):
                t.latest = None
        self.assertIsNone(await self.real.metrics.collect(self.real, sess))

    async def test_series_read_snapshot_stop_shapes(self):
        real, demo, _ = await self.start_both()
        r_id, d_id = real["sessionId"], demo["sessionId"]
        for cmd, args in (("metrics.series", {"metric": "cpu", "points": 5}),
                          ("metrics.read", {"kind": "events"}),
                          ("metrics.read", {}),
                          ("metrics.snapshot", {"recentEvents": True})):
            r = await self.real.dispatch(cmd, {**args, "sessionId": r_id})
            d = await self.demo.dispatch(cmd, {**args, "sessionId": d_id})
            self.assertEqual(set(r), set(d), cmd)
            if cmd == "metrics.series":
                self.assertEqual(set(r["buckets"][0]), set(d["buckets"][0]))
                self.assertEqual(set(r["stats"]), set(d["stats"]))
            if cmd == "metrics.snapshot":
                self.assertLessEqual(SUMMARY_KEYS, set(r["stats"]))
                self.assertLessEqual(SUMMARY_KEYS, set(d["stats"]))
                self.assertEqual(set(r["stats"]["series"]), set(d["stats"]["series"]) - {"hitchMs"})
                self.assertEqual({"ts", "kind", "text", "durationMs"}, set(r["recentEvents"][-1]))
        r = await self.real.dispatch("metrics.stop", {"sessionId": r_id})
        d = await self.demo.dispatch("metrics.stop", {"sessionId": d_id})
        self.assertEqual(set(r), set(d))
        self.assertEqual((r["running"], r["status"]), (d["running"], d["status"]))
        self.assertEqual(r["summary"]["eventCounts"], {"hang": 1})
        self.assertEqual(r["summary"]["anomalies"][0]["kind"], "hang")

    async def test_errors_match(self):
        for cmd, args, code in (("metrics.start", {}, "bad-args"), ("metrics.series", {"sessionId": "m99"}, "not-found"),
                                ("metrics.read", {}, "bad-args"), ("metrics.leaks", {}, "bad-args"),
                                ("metrics.launchProfile", {}, "bad-args")):
            for b in (self.real, self.demo):
                self.assertEqual(await code_of(b.dispatch(cmd, args)), code, (cmd, b))

    async def test_export(self):
        real, demo, _ = await self.start_both()
        r = await self.real.dispatch("metrics.export", {"sessionId": real["sessionId"]})
        d = await self.demo.dispatch("metrics.export", {"sessionId": demo["sessionId"]})
        self.assertEqual(set(r), set(d))
        self.assertEqual(r["rows"], 5)
        self.assertTrue(Path(r["path"]).is_relative_to(self.tmp))


if __name__ == "__main__":
    unittest.main()
