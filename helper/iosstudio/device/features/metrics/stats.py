"""Stats for a metrics session (Performance tab and the MCP metrics tools). Stdlib only.

Input: the session's samples (compact rows, see SERIES) and events, as stored by core.py.
    summarize(samples, events, meta) -> {"window", "series", "memory", "cpuSpikes", "fpsDrops",
                                         "hitches", "battery", "energy", "io", "anomalies"}
    downsample(samples, key, points) -> buckets [{"t0", "t1", "min", "avg", "max", "n"}]

Every number is rounded so the summary stays small (the MCP summary is ~1-2 KB).
"""
from __future__ import annotations

import math

# key -> (label, unit). The order is the CSV column order.
SERIES = {
    "cpu": ("CPU", "%"),
    "mem": ("Memory footprint", "MB"),
    "rss": ("Resident memory", "MB"),
    "threads": ("Threads", ""),
    "wakeups": ("Wakeups", "/s"),
    "faults": ("Page faults", "/s"),
    "diskR": ("Disk read", "KB/s"),
    "diskW": ("Disk write", "KB/s"),
    "energy": ("Energy impact", ""),
    "eCpu": ("Energy: CPU", ""),
    "eGpu": ("Energy: GPU", ""),
    "eNet": ("Energy: network", ""),
    "eDisplay": ("Energy: display", ""),
    "eLoc": ("Energy: location", ""),
    "eOverhead": ("Energy: overhead", ""),
    "thermal": ("Thermal state cost", ""),
    "fps": ("Display FPS (whole screen)", "fps"),
    "gpu": ("GPU utilization", "%"),
    "gpuMem": ("GPU memory", "MB"),
    "netRx": ("Network in", "KB/s"),
    "netTx": ("Network out", "KB/s"),
    "batt": ("Battery level", "%"),
    "battMw": ("Battery power", "mW"),
    "sysCpu": ("CPU, whole phone", "%"),
    "hitchMs": ("Hitch time", "ms"),
}
ENERGY_PARTS = {"eCpu": "CPU", "eGpu": "GPU", "eNet": "Network", "eDisplay": "Display", "eLoc": "Location",
                "eOverhead": "Overhead"}

LEAK_SLOPE_MB_MIN = 1.0      # memory growth that counts as a possible leak
LEAK_R2 = 0.6
LEAK_MIN_S = 120
CPU_SPIKE = 80.0             # % of one core
CPU_SPIKE_S = 3
FPS_DROP = 50.0
FPS_ACTIVE = 10.0            # below this the screen is (mostly) idle, not dropping frames
FPS_DROP_S = 2


def _r(v, nd=1):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    v = round(float(v), nd)
    return int(v) if v == int(v) and abs(v) < 1e15 else v


def _vals(samples, key):
    return [(s["ts"], s[key]) for s in samples
            if isinstance(s.get(key), (int, float)) and math.isfinite(s[key])]


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    k = (len(ys) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return ys[lo] if lo == hi else ys[lo] + (ys[hi] - ys[lo]) * (k - lo)


def series_stats(samples, key) -> dict | None:
    xs = [v for _, v in _vals(samples, key)]
    if not xs:
        return None
    nd = 2 if key in ("energy",) or key.startswith("e") else 1
    return {"min": _r(min(xs), nd), "avg": _r(sum(xs) / len(xs), nd), "p95": _r(pct(xs, 0.95), nd),
            "max": _r(max(xs), nd), "last": _r(xs[-1], nd), "n": len(xs)}


def linreg(points: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """(slope per second, intercept, R²) of y over t, or None with fewer than 3 points / no spread."""
    n = len(points)
    if n < 3:
        return None
    mt = sum(t for t, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((t - mt) ** 2 for t, _ in points)
    if sxx <= 0:
        return None
    sxy = sum((t - mt) * (y - my) for t, y in points)
    slope = sxy / sxx
    icpt = my - slope * mt
    ss_tot = sum((y - my) ** 2 for _, y in points)
    ss_res = sum((y - (icpt + slope * t)) ** 2 for t, y in points)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, icpt, max(0.0, r2)


def _runs(samples, key, pred, min_s, gap):
    """Stretches where pred(value) holds for at least min_s seconds -> [(t0, t1, values)]."""
    out, cur = [], []
    last_t = None
    for t, v in _vals(samples, key):
        if cur and last_t is not None and t - last_t > gap:
            if cur[-1][0] - cur[0][0] + gap / 2 >= min_s:
                out.append(cur)
            cur = []
        if pred(v):
            cur.append((t, v))
        else:
            if cur and cur[-1][0] - cur[0][0] + gap / 2 >= min_s:
                out.append(cur)
            cur = []
        last_t = t
    if cur and cur[-1][0] - cur[0][0] + gap / 2 >= min_s:
        out.append(cur)
    return out


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _fmt_dur(s: float) -> str:
    s = int(round(s))
    if s < 90:
        return f"{s} s"
    m = s / 60
    return f"{m:.0f} minutes" if m >= 2 else f"{m:.1f} minutes"


def _last_segment(samples):
    """Samples since the app's last (re)launch: the rows that share the newest pid."""
    if not samples:
        return []
    pid = samples[-1].get("pid")
    i = len(samples)
    while i > 0 and samples[i - 1].get("pid") == pid:
        i -= 1
    return samples[i:]


def memory_trend(samples) -> dict | None:
    seg = _last_segment(samples)
    pts = _vals(seg, "mem")
    if len(pts) < 10:
        return None
    best = None
    t_end = pts[-1][0]
    # The whole run since launch, and the last 10 / 5 minutes: report the window that looks most like a leak.
    for window in (None, 600, 300):
        sub = [p for p in pts if window is None or p[0] >= t_end - window]
        if len(sub) < 10:
            continue
        lr = linreg(sub)
        if lr is None:
            continue
        slope, _, r2 = lr
        dur = sub[-1][0] - sub[0][0]
        cand = {"slopeMbPerMin": _r(slope * 60, 2), "r2": _r(r2, 2), "seconds": int(dur),
                "growthMb": _r(slope * dur, 1), "from": _r(sub[0][1], 1), "to": _r(sub[-1][1], 1)}
        cand["likelyLeak"] = bool(slope * 60 >= LEAK_SLOPE_MB_MIN and r2 >= LEAK_R2 and dur >= LEAK_MIN_S)
        score = (cand["likelyLeak"], slope * 60 * r2)
        if best is None or score > best[0]:
            best = (score, cand)
    return best[1] if best else None


def cpu_spikes(samples, gap) -> list[dict]:
    out = []
    for run in _runs(samples, "cpu", lambda v: v > CPU_SPIKE, CPU_SPIKE_S, gap):
        out.append({"ts": _r(run[0][0], 0), "seconds": int(round(run[-1][0] - run[0][0] + gap / 2)),
                    "peak": _r(max(v for _, v in run), 0)})
    return out


def fps_drops(samples, gap) -> list[dict]:
    out = []
    for run in _runs(samples, "fps", lambda v: FPS_ACTIVE <= v < FPS_DROP, FPS_DROP_S, gap):
        out.append({"ts": _r(run[0][0], 0), "seconds": int(round(run[-1][0] - run[0][0] + gap / 2)),
                    "low": _r(min(v for _, v in run), 0)})
    return out


def battery(samples) -> dict | None:
    lv = [(t, v) for t, v in _vals(samples, "batt")]
    mw = _vals(samples, "battMw")
    charging = any(s.get("charging") for s in samples[-5:])
    out: dict = {"charging": charging}
    if lv:
        out["from"], out["to"] = _r(lv[0][1], 0), _r(lv[-1][1], 0)
        span = lv[-1][0] - lv[0][0]
        lr = linreg(lv) if span >= 120 else None
        if lr is not None:
            out["drainPctPerHour"] = _r(-lr[0] * 3600, 1)
    if mw:
        wh = 0.0
        for (t0, v0), (t1, v1) in zip(mw, mw[1:]):
            dt = t1 - t0
            if 0 < dt <= 30:
                wh += (v0 + v1) / 2 * dt / 3600.0
        out["mWh"] = _r(wh, 1)
        out["avgMw"] = _r(sum(v for _, v in mw) / len(mw), 0)
    return out if len(out) > 1 else None


def energy(samples) -> dict | None:
    tot = series_stats(samples, "energy")
    parts = {}
    for k, label in ENERGY_PARTS.items():
        xs = [v for _, v in _vals(samples, k)]
        if xs:
            parts[label] = sum(xs) / len(xs)
    if not tot and not parts:
        return None
    s = sum(parts.values()) or 0
    return {"avg": tot["avg"] if tot else None, "max": tot["max"] if tot else None,
            "byComponent": {k: {"avg": _r(v, 2), "share": _r(100 * v / s, 0) if s else None}
                            for k, v in sorted(parts.items(), key=lambda kv: -kv[1]) if v > 0}}


def io(samples) -> dict:
    out = {}
    for k in ("diskR", "diskW", "netRx", "netTx"):
        st = series_stats(samples, k)
        if st:
            out[k] = {"avg": st["avg"], "max": st["max"], "unit": "KB/s"}
    return out


def _gap(samples) -> float:
    ts = [s["ts"] for s in samples[-50:]]
    diffs = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    return max(1.0, (diffs[len(diffs) // 2] if diffs else 1.0) * 2.5)


def summarize(samples: list[dict], events: list[dict], meta: dict | None = None) -> dict:
    meta = meta or {}
    samples = [s for s in samples if isinstance(s, dict) and isinstance(s.get("ts"), (int, float))]
    out: dict = {"window": {"samples": len(samples)}}
    if samples:
        out["window"].update(start=_r(samples[0]["ts"], 0), end=_r(samples[-1]["ts"], 0),
                             seconds=int(samples[-1]["ts"] - samples[0]["ts"]))
    series = {}
    for k in SERIES:
        st = series_stats(samples, k)
        if st:
            series[k] = st
    out["series"] = series
    gap = _gap(samples) if samples else 2.5
    mem = memory_trend(samples)
    if mem:
        out["memory"] = mem
    spikes = cpu_spikes(samples, gap)
    drops = fps_drops(samples, gap)
    hitch_ev = [e for e in events if e.get("kind") == "hitch"]
    out["cpuSpikes"] = {"count": len(spikes), "longest": max((s["seconds"] for s in spikes), default=0),
                        "items": spikes[-5:]}
    out["fpsDrops"] = {"count": len(drops), "items": drops[-5:]}
    out["hitches"] = {"count": len(hitch_ev),
                      "totalMs": _r(sum(float(e.get("durationMs") or 0) for e in hitch_ev), 0),
                      "worstMs": _r(max((float(e.get("durationMs") or 0) for e in hitch_ev), default=0), 0)}
    b = battery(samples)
    if b:
        out["battery"] = b
    en = energy(samples)
    if en:
        out["energy"] = en
    out["io"] = io(samples)
    counts: dict[str, int] = {}
    for e in events:
        counts[e.get("kind", "?")] = counts.get(e.get("kind", "?"), 0) + 1
    out["eventCounts"] = counts
    out["anomalies"] = anomalies(out, events, meta)
    return out


def anomalies(st: dict, events: list[dict], meta: dict) -> list[dict]:
    """Plain-language findings, worst first: [{kind, level, text}]."""
    out: list[tuple[int, dict]] = []
    name = meta.get("name") or "The app"
    mem = st.get("memory")
    if mem and mem.get("likelyLeak"):
        out.append((0, {"kind": "leak", "level": "warn",
                        "text": f"Memory grew {mem['slopeMbPerMin']} MB/min for {_fmt_dur(mem['seconds'])} "
                                f"({mem['from']} → {mem['to']} MB): a possible leak. Run Check for leaks."}))
    for kind, word in (("jetsam", "was killed for using too much memory (jetsam)"),
                       ("crash", "crashed"), ("watchdog", "was killed by the watchdog (too slow to respond)")):
        n = sum(1 for e in events if e.get("kind") == kind)
        if n:
            out.append((0, {"kind": kind, "level": "error",
                            "text": f"{name} {word}{'' if n == 1 else ' ' + _times(n)}."}))
    hangs = [e for e in events if e.get("kind") == "hang"]
    if hangs:
        worst = max((float(e.get("durationMs") or 0) for e in hangs), default=0)
        out.append((1, {"kind": "hang", "level": "warn",
                        "text": f"{len(hangs)} hang{'s' if len(hangs) > 1 else ''} detected"
                                + (f", the longest {worst / 1000:.1f} s" if worst else "")
                                + ": the main thread was blocked."}))
    sp = st.get("cpuSpikes") or {}
    if sp.get("count"):
        peak = max((s["peak"] for s in sp.get("items") or []), default=0)
        out.append((2, {"kind": "cpu", "level": "warn",
                        "text": f"CPU stayed above {CPU_SPIKE:.0f}% {_times(sp['count'])} "
                                f"(longest {sp['longest']} s, peak {peak}%)."}))
    fd = st.get("fpsDrops") or {}
    if fd.get("count"):
        low = min((d["low"] for d in fd.get("items") or []), default=0)
        out.append((2, {"kind": "fps", "level": "warn",
                        "text": f"The display frame rate dipped below {FPS_DROP:.0f} fps {_times(fd['count'])} "
                                f"(down to {low} fps). It's measured for "
                                "the whole screen, not only your app."}))
    h = st.get("hitches") or {}
    if h.get("count"):
        out.append((2, {"kind": "hitch", "level": "warn",
                        "text": f"{h['count']} hitch{'es' if h['count'] > 1 else ''}: {h['totalMs']} ms spent "
                                f"in slow frames, the worst {h['worstMs']} ms."}))
    th = [e for e in events if e.get("kind") == "thermal"]
    if th:
        out.append((3, {"kind": "thermal", "level": "info", "text": f"The phone reported {len(th)} thermal "
                                                                     f"change{'s' if len(th) > 1 else ''}."}))
    b = st.get("battery") or {}
    if b.get("drainPctPerHour") is not None and not b.get("charging") and b["drainPctPerHour"] >= 15:
        out.append((3, {"kind": "battery", "level": "info",
                        "text": f"Battery drained at {b['drainPctPerHour']}%/hour while measuring."}))
    out.sort(key=lambda x: x[0])
    return [x for _, x in out]


def downsample(samples: list[dict], key: str, points: int = 60) -> list[dict]:
    pts = _vals(samples, key)
    if not pts:
        return []
    points = max(1, min(int(points), len(pts)))
    t0, t1 = pts[0][0], pts[-1][0]
    span = (t1 - t0) or 1.0
    buckets: list[list] = [[] for _ in range(points)]
    for t, v in pts:
        i = min(points - 1, int((t - t0) / span * points))
        buckets[i].append((t, v))
    nd = 2 if key.startswith("e") else 1
    out = []
    for b in buckets:
        if not b:
            continue
        vs = [v for _, v in b]
        out.append({"t0": _r(b[0][0], 0), "t1": _r(b[-1][0], 0), "min": _r(min(vs), nd),
                    "avg": _r(sum(vs) / len(vs), nd), "max": _r(max(vs), nd), "n": len(vs)})
    return out
