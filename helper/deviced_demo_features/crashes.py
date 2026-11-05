"""Demo: crash.list, crash.pull."""
from __future__ import annotations

import json
import time
from pathlib import Path

import crash_symbols
from deviced_demo_core import DIR, TEAM


def crash_items(now: float | None = None) -> list:
    # Anchored to a 15-minute step so a name from crash.list still resolves in a later crash.pull.
    now = (time.time() // 900) * 900 if now is None else now
    out = []
    for i, ago in enumerate((3.2 * 3600, 27 * 3600, 30 * 3600)):
        ts = now - ago
        stamp = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(ts))
        proc = "WeatherGlance" if i != 1 else "HabitTracker"
        out.append({"name": f"/{proc}-{stamp}.ips", "file": f"{proc}-{stamp}.ips", "process": proc,
                    "date": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)), "ts": ts, "kind": "crash", "retired": False})
    ts = now - 50 * 3600
    out.append({"name": "/JetsamEvent-demo.ips", "file": "JetsamEvent-demo.ips", "process": "JetsamEvent",
                "date": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)), "ts": ts, "kind": "jetsam", "retired": False})
    return out


def crash_pull(name: str) -> dict:
    proc = "HabitTracker" if "Habit" in name else ("JetsamEvent" if "Jetsam" in name else "WeatherGlance")
    base = (time.time() // 900) * 900
    item = next((c for c in crash_items(base) + crash_items(base - 900) if c["name"] == name), None)
    when = time.strftime("%Y-%m-%d %H:%M:%S.00", time.localtime(item["ts"] if item else time.time()))
    path = DIR / "crashes" / Path(name).name
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = ["0 libsystem_kernel.dylib __pthread_kill + 8", "1 libsystem_pthread.dylib pthread_kill + 268",
              "2 libsystem_c.dylib abort + 148", "3 libswiftCore.dylib _assertionFailure(_:_:file:line:flags:) + 1240",
              f"4 {proc} ForecastStore.load(from:) + 412" if proc == "WeatherGlance" else f"4 {proc} StreakView.body.getter + 96",
              f"5 {proc} closure #1 in ContentView.body.getter + 88", "6 SwiftUI ViewGraph.updateOutputs() + 220",
              "7 UIKitCore -[UIApplication _run] + 888", "8 UIKitCore UIApplicationMain + 340", f"9 {proc} main + 64"]
    summary = {"process": proc, "bundleId": f"XTL-{TEAM}.com.example." + ("habits" if proc == "HabitTracker" else "weatherglance"),
               "appVersion": "1.2.0 (14)", "osVersion": "iPhone OS 27.2 (24B115)", "date": when,
               "exceptionType": "EXC_BREAKPOINT" if proc == "WeatherGlance" else "EXC_CRASH",
               "signal": "SIGTRAP" if proc == "WeatherGlance" else "SIGABRT",
               "terminationReason": "Fatal error: Unexpectedly found nil while unwrapping an Optional value" if proc == "WeatherGlance" else "Abort trap: 6",
               "crashedThread": 0, "frames": frames, "bugType": "309", "incident": "11111111-2222-4333-8444-555555555555"}
    if proc == "JetsamEvent":
        summary.update(exceptionType=None, signal=None, terminationReason="Memory pressure", frames=[], bugType="298")
    else:
        crash_symbols.apply(summary, demo_symbols(proc))
    header = {"app_name": proc, "timestamp": summary["date"], "app_version": "1.2.0", "bug_type": summary["bugType"],
              "os_version": summary["osVersion"], "incident_id": summary["incident"]}
    path.write_text(json.dumps(header) + "\n" + json.dumps({"exception": {"type": summary["exceptionType"], "signal": summary["signal"]},
                                                            "frames": frames}, indent=2) + "\n")
    return {"path": str(path), "summary": summary}


def demo_symbols(proc: str) -> dict:
    """What crash_symbols.symbolicate() returns for the demo crashes: WeatherGlance's build is in the symbol
    cache (file:line), HabitTracker's crash came from a build made before symbols were kept."""
    if proc != "WeatherGlance":
        return crash_symbols.make_result(
            "none", "no-build", f"No build on this computer matches {proc} in this report (UUID "
            "8d1e4c2a-3b5f-4a61-9c7e-2f0b6d5a4e13). It was built before iOS Studio kept symbols, on another "
            "computer, or the project was rebuilt since.", crash_symbols.HINT_REBUILD)
    project = str(Path.home() / "Developer" / proc)

    def at(fn: str, file: str | None = None, line: int | None = None, col: int | None = None) -> dict:
        out = {"image": proc, "function": fn, "line": line, "column": col}
        if file:
            out.update(file=f"Sources/{proc}/{file}", path=f"{project}/Sources/{proc}/{file}")
        return out
    return crash_symbols.make_result(
        "full", "ok", "Resolved the app's frames to file:line.", project=project,
        frames={"0:4": at("ForecastStore.load(from:)", "ForecastStore.swift", 57, 31),
                "0:5": at("closure #1 in ContentView.body.getter", "ContentView.swift", 24, 13),
                "0:9": at("main")})


def register(reg):
    reg.command("crash.list", lambda ctx, a: crash_items())
    reg.command("crash.pull", lambda ctx, a: crash_pull(str(a.get("name") or "")))
