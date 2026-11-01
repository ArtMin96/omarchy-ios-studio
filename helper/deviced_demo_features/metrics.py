"""Demo: metrics.* with made-up series (same commands as deviced_features/metrics.py).

Each session starts with five minutes of back-dated history so the report card has something to say
right away: a sawtooth leak in the memory footprint, CPU spikes, the display FPS dipping from 60 to
~42 with hitches, the battery falling 0.3 %/min, one jetsam kill (with a relaunch) and one hang.
Then it keeps going live at 1 Hz. Launch profile and leak check return canned reports.
"""
from __future__ import annotations

import asyncio
import random
import time

import metrics_core
from common import HelperError
from deviced_demo_core import APPS, DIR, event, pids, running

SEED_S = 300


def _err(code, message, fix=None, detail=None):
    return HelperError(code, message, fix, detail)


def _app(bid: str):
    for name, b, *_ in APPS:
        if b == bid or b.endswith("." + bid) or name.lower() == str(bid).lower():
            return name, b
    return None, None


class Synth:
    """Deterministic-looking series for one demo session; t = seconds since the (fake) launch."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.base_mem = 138.0

    def row(self, t: float, sets: list[str], hitching: bool, w: float) -> tuple[dict, list[tuple]]:
        r = self.rng
        out: dict = {}
        events: list[tuple] = []
        spike = (t % 75) < 5 and t > 20
        dip = (t % 90) >= 60 and (t % 90) < 65
        if "process" in sets:
            cpu = (86 + r.random() * 11) if spike else max(2.0, 17 + r.gauss(0, 5))
            mem = self.base_mem + 3.1 * t / 60 + 5.5 * ((t % 50) / 50) + r.gauss(0, 0.25)
            burst = (t % 30) < 2
            out.update(cpu=round(cpu, 1), mem=round(mem, 1), rss=round(mem * 0.84 + 18, 1),
                       threads=int(15 + (3 if spike else 0) + r.random() * 2), wakeups=round(110 + r.gauss(0, 25), 1),
                       faults=round(260 + r.gauss(0, 60), 1), diskR=round((420 if burst else 6) + r.random() * 8, 1),
                       diskW=round((180 if burst else 3) + r.random() * 5, 1),
                       sysCpu=round(cpu * 0.3 + 11 + r.gauss(0, 2), 1))
        if "energy" in sets:
            ecpu = (out.get("cpu", 18) or 18) / 22
            egpu = 1.0 + (1.6 if dip else 0) + r.random() * 0.3
            enet = (1.4 if (t % 30) < 2 else 0.25) + r.random() * 0.1
            edisp, eloc, eover = 2.1 + r.random() * 0.1, 0.3, 0.7 + r.random() * 0.1
            out.update(eCpu=round(ecpu, 2), eGpu=round(egpu, 2), eNet=round(enet, 2), eDisplay=round(edisp, 2),
                       eLoc=eloc, eOverhead=round(eover, 2), energy=round(ecpu + egpu + enet + edisp + eloc + eover, 2),
                       thermal=round(0.15 + t / 3000, 2))
        if "graphics" in sets:
            fps = (42 + r.random() * 6) if dip else (58.5 + r.random() * 1.5)
            out.update(fps=round(fps, 1), gpu=round((68 if dip else 21) + r.gauss(0, 4), 1), gpuMem=48.5)
            if dip and (hitching or r.random() < 0.6):
                events.append(("hitch", 40 + r.random() * 90))
        if "network" in sets:
            on = (t % 30) < 2
            out.update(netRx=round((185 if on else 0.4) + r.random() * 10, 1),
                       netTx=round((22 if on else 0.2) + r.random() * 2, 1))
        if "battery" in sets:
            out.update(batt=round(78 - 0.3 * w / 60), battMw=round(1420 + (520 if spike else 0) + r.gauss(0, 90)),
                       charging=0)
        return out, events


class DemoBackend:
    emit = staticmethod(event)
    err = staticmethod(_err)
    export_dir = DIR / "metrics"

    async def attach(self, ctx, sess, a):
        if sess.bundle_id:
            name, bid = _app(sess.bundle_id)
            if bid is None:
                raise _err("not-found", "That app isn't installed on your iPhone.")
            sess.bundle_id = bid
            if not a.get("name"):
                sess.name = name
            running.add(bid)
            sess.pid = pids.get(bid) or 812
        sess.data["synth"] = Synth(hash(sess.id) & 0xFFFF)
        sess.data["t0"] = time.time() - SEED_S
        self._seed(sess)
        sess.set_status("running")

    def _seed(self, sess):
        """Five minutes of history (no events emitted for the back-dated rows: clients read them)."""
        emit, sess._emit = sess._emit, None
        syn: Synth = sess.data["synth"]
        t0 = sess.data["t0"]
        pid0 = (sess.pid or 812) - 1
        jetsam_at, hang_at = 40, 170
        for i in range(SEED_S):
            ts = round(t0 + i, 2)
            if i == jetsam_at:
                sess.add_event("jetsam", "Killed for using too much memory (jetsam). memorystatus: killing "
                               f"{sess.name} [{pid0}] (per-process-limit)", ts=ts)
                sess.add_event("launch", f"{sess.name} relaunched (pid {sess.pid}).", ts=ts + 4)
            if i == hang_at:
                sess.add_event("hang", "Hang: the main thread was blocked. HangTracer: hang detected (1.8 s)",
                               ts=ts, durationMs=1800.0)
            if jetsam_at <= i < jetsam_at + 5:
                continue   # the app wasn't running
            t = i - (jetsam_at + 5) if i > jetsam_at else i
            if i < jetsam_at:
                t = i + 160   # memory had already grown before the kill
            row, evs = syn.row(t, sess.sets, False, i)
            row["ts"], row["pid"] = ts, pid0 if i < jetsam_at else sess.pid
            for kind, ms in evs:
                sess.add_event(kind, f"Hitch: a FrameUpdate took {ms:.0f} ms", ts=ts, durationMs=round(ms, 1))
            sess.add_sample(row)
        sess._emit = emit
        sess.data["tLive"] = SEED_S - (jetsam_at + 5)

    async def detach(self, ctx, sess):
        return None

    async def collect(self, ctx, sess):
        syn: Synth = sess.data["synth"]
        sess.data["tLive"] = sess.data.get("tLive", 0) + sess.interval
        row, evs = syn.row(sess.data["tLive"], sess.sets, sess.hitches_on(), time.time() - sess.data["t0"])
        for kind, ms in evs:
            if sess.hitches_on() or kind != "hitch" or random.random() < 0.5:
                sess.add_event(kind, f"Hitch: a FrameUpdate took {ms:.0f} ms", durationMs=round(ms, 1))
        return row

    async def hitches(self, ctx, sess, seconds):
        sess.hitch_until = time.time() + seconds

    async def launch_profile(self, ctx, a):
        name, bid = _app(str(a["bundleId"]))
        if bid is None:
            raise _err("not-found", "That app isn't installed on your iPhone.")
        await asyncio.sleep(2.5)
        running.add(bid)
        pids[bid] = 812
        return {"bundleId": bid, "name": name, "pid": 812, "seconds": 4.2,
                "phases": [{"name": "Loading libraries (dyld)", "ms": 182.4},
                           {"name": "Static initializers", "ms": 41.7},
                           {"name": "First frame (timed on this computer)", "ms": 690}],
                "preMainMs": 224.1, "firstFrameMs": 690,
                "counts": {"images": 312, "dlopen": 4},
                "verdict": "OK launch (690 ms). Apple suggests the first frame within 400 ms."}

    async def leaks(self, ctx, a):
        name, bid = _app(str(a["bundleId"]))
        if bid is None:
            raise _err("not-found", "That app isn't installed on your iPhone.")
        await asyncio.sleep(3)
        p = DIR / "metrics" / f"{name.replace(' ', '')}-demo.memgraph"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"demo memgraph (made up)\n")
        return {"bundleId": bid, "name": name, "pid": pids.get(bid) or 812, "leakCount": 3, "leakedBytes": 2304,
                "seconds": 3.1, "memgraphPath": str(p), "memgraphBytes": p.stat().st_size,
                "leaks": [{"type": "ForecastCache", "count": 2, "bytes": 1536},
                          {"type": "NSMutableArray", "count": 1, "bytes": 768}],
                "verdict": "3 leaked allocations found (2.3 KB). The ForecastCache objects keep each other alive: "
                           "look for a retain cycle between ForecastCache and its delegate."}


def register(reg):
    metrics_core.register_commands(reg, DemoBackend())
