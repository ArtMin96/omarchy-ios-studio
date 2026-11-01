"""Demo mode for the helper's `debug` group: a made-up backtrace and breakpoint session on the demo
WeatherGlance app, with no lldb and no phone. It still asks the demo device daemon for
debug.start / debug.stop, so its errors (an app that isn't running, an App Store app) are the
same as in the real flow. Everything here is invented.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import select
import sys
import time

import common

HOME = str(common.HOME)


def _proj(bid: str) -> tuple[str, str]:
    name = {"weatherglance": "WeatherGlance", "habits": "HabitTracker", "recipebox": "RecipeBox"}.get(
        bid.rsplit(".", 1)[-1], "WeatherGlance")
    return name, f"{HOME}/Developer/{name}"


def _src(name: str, file: str) -> str:
    return f"{HOME}/Developer/{name}/Sources/{name}/{file}"


SOURCES = {
    "WeatherGlance": [("WeatherGlanceApp.swift", 18), ("ContentView.swift", 64), ("ForecastView.swift", 88),
                      ("ForecastModel.swift", 112), ("Forecast.swift", 40), ("LocationService.swift", 75)],
    "HabitTracker": [("HabitTrackerApp.swift", 16), ("Habit.swift", 30), ("StreakView.swift", 52), ("HabitStore.swift", 90)],
    "RecipeBox": [("RecipeBoxApp.swift", 14), ("RecipeList.swift", 70), ("Recipe.swift", 28)],
}


def F(i, fn, mod, file=None, line=None, app="WeatherGlance", name="WeatherGlance"):
    f = {"index": i, "function": fn, "module": mod, "pc": "0x%x" % (0x1a2b30000 + i * 0x1c4 + len(fn) * 16),
         "inApp": mod == app}
    if file:
        f.update(file=_src(name, file), line=line, column=9)
    return f


def _threads(name: str) -> list[dict]:
    exe = name
    main = [F(0, "mach_msg2_trap", "libsystem_kernel.dylib"), F(1, "mach_msg2_internal", "libsystem_kernel.dylib"),
            F(2, "mach_msg_overwrite", "libsystem_kernel.dylib"), F(3, "mach_msg", "libsystem_kernel.dylib"),
            F(4, "__CFRunLoopServiceMachPort", "CoreFoundation"), F(5, "__CFRunLoopRun", "CoreFoundation"),
            F(6, "_CFRunLoopRunSpecificWithOptions", "CoreFoundation"), F(7, "GSEventRunModal", "GraphicsServices"),
            F(8, "-[UIApplication _run]", "UIKitCore"), F(9, "UIApplicationMain", "UIKitCore"),
            F(10, "closure #1 in KitRendererCommon(_:)", "SwiftUI"), F(11, "runApp<A>(_:)", "SwiftUI"),
            F(12, "static App.main()", "SwiftUI"),
            F(13, f"static {exe}App.$main()", exe, f"{exe}App.swift", 4, exe, name),
            F(14, "main", exe, f"{exe}App.swift", 3, exe, name), F(15, "start", "dyld")]
    worker = [F(0, "Swift runtime failure: Index out of range", exe, "ForecastModel.swift", 58, exe, name),
              F(1, "ForecastModel.dailyHigh(for:)", exe, "ForecastModel.swift", 58, exe, name),
              F(2, "closure #1 in ForecastModel.refresh()", exe, "ForecastModel.swift", 41, exe, name),
              F(3, "partial apply for closure #1 in ForecastModel.refresh()", exe),
              F(4, "swift::runJobInEstablishedExecutorContext(swift::Job*)", "libswift_Concurrency.dylib"),
              F(5, "swift_job_runImpl(swift::Job*, swift::SerialExecutorRef)", "libswift_Concurrency.dylib"),
              F(6, "_dispatch_root_queue_drain", "libdispatch.dylib"), F(7, "_dispatch_worker_thread2", "libdispatch.dylib"),
              F(8, "_pthread_wqthread", "libsystem_pthread.dylib"), F(9, "start_wqthread", "libsystem_pthread.dylib")]
    idle = [F(0, "__workq_kernreturn", "libsystem_kernel.dylib"), F(1, "_pthread_wqthread", "libsystem_pthread.dylib"),
            F(2, "start_wqthread", "libsystem_pthread.dylib")]
    fetch = main[:7] + [F(7, "-[NSRunLoop(NSRunLoop) runMode:beforeDate:]", "Foundation"),
                        F(8, "-[UIEventFetcher threadMain]", "UIKitCore"), F(9, "__NSThread__start__", "Foundation"),
                        F(10, "_pthread_start", "libsystem_pthread.dylib"), F(11, "thread_start", "libsystem_pthread.dylib")]

    def T(i, frames, name_=None, queue=None, reason="signal", desc="signal SIGSTOP", crashed=False):
        return {"index": i, "tid": 0x2a1b0 + i, "name": name_, "queue": queue, "reason": reason, "numFrames": len(frames),
                "frames": frames, "description": desc, "crashed": crashed}
    return [T(1, main, queue="com.apple.main-thread"),
            T(2, fetch, "com.apple.uikit.eventfetch-thread"),
            T(3, worker, queue="com.apple.root.user-initiated-qos.cooperative", reason="exception",
              desc="EXC_BREAKPOINT (code=1, subcode=0x1a2b3d8f4)", crashed=True),
            T(4, idle), T(5, idle), T(6, idle), T(7, idle)]


def V(name, type_, value=None, children=None):
    v = {"name": name, "type": type_, "childCount": len(children or [])}
    if value is not None:
        v["value"] = value
    if children:
        v["children"] = children
    return v


def _days():
    return [V(f"[{i}]", "Day", f"{d} · {h}°", None) for i, (d, h) in
            enumerate([("Mon", 24.5), ("Tue", 23.0), ("Wed", 21.5), ("Thu", 22.0), ("Fri", 25.5), ("Sat", 26.0), ("Sun", 24.0)])]


def _states(name: str) -> list[dict]:
    """The three stops a demo session walks through: breakpoint, step over, step in."""
    exe = name
    view = [F(0, "ForecastView.body.getter", exe, "ForecastView.swift", 42, exe, name),
            F(1, "protocol witness for View.body.getter in conformance ForecastView", exe),
            F(2, "ViewBodyAccessor.updateBody(of:changed:)", "SwiftUI"), F(3, "DynamicBody.updateValue()", "SwiftUI"),
            F(4, "AG::Graph::UpdateStack::update()", "AttributeGraph"), F(5, "-[UIApplication _run]", "UIKitCore"),
            F(6, "UIApplicationMain", "UIKitCore"), F(7, f"static {exe}App.$main()", exe, f"{exe}App.swift", 4, exe, name),
            F(8, "main", exe, f"{exe}App.swift", 3, exe, name)]
    view2 = [dict(view[0], line=43, pc="0x1a2b30a10")] + view[1:]
    model = [F(0, "ForecastModel.dailyHigh(for:)", exe, "ForecastModel.swift", 56, exe, name),
             dict(view[0], line=43, index=1, function="ForecastView.body.getter")] + \
        [dict(f, index=f["index"] + 1) for f in view[1:]]
    day = V("day", "Day", "Mon · 24.5°", [V("date", "Date", "2026-11-02 00:00:00 UTC"), V("high", "Double", "24.5"),
                                          V("low", "Double", "15"), V("symbol", "String", "\"sun.max\"")])
    selfv = V("self", "ForecastView", None, [V("forecast", "Forecast", None, [V("city", "String", "\"Lisbon\""),
                                                                              V("days", "[Day]", "7 values")]),
                                             V("unit", "TemperatureUnit", ".celsius")])
    loc1 = [selfv, day]
    loc2 = [selfv, day, V("high", "Double", "24.5"), V("label", "String", "\"24.5°\"")]
    loc3 = [V("self", "ForecastModel", None, [V("days", "[Day]", "7 values", _days()), V("isLoading", "Bool", "false")]),
            V("index", "Int", "7"), V("count", "Int", "7")]

    def T(frames, reason, desc):
        return {"index": 1, "tid": 0x2a1b1, "name": None, "queue": "com.apple.main-thread", "reason": reason,
                "numFrames": len(frames), "frames": frames, "description": desc, "crashed": False}
    threads = [{"index": i, "name": n, "reason": "breakpoint" if i == 1 else "none"} for i, n in
               [(1, None), (2, "com.apple.uikit.eventfetch-thread"), (3, None), (4, None)]]
    return [
        {"reason": "breakpoint", "description": "breakpoint 1.1", "thread": T(view, "breakpoint", "breakpoint 1.1"),
         "threads": threads, "threadCount": 4, "breakpoint": {"id": 1, "spec": "ForecastView.swift:42"},
         "locals": {"thread": 1, "frame": 0, "function": "ForecastView.body.getter", "variables": loc1, "more": False}},
        {"reason": "step", "description": "step over", "thread": T(view2, "step", "step over"), "threads": threads,
         "threadCount": 4,
         "locals": {"thread": 1, "frame": 0, "function": "ForecastView.body.getter", "variables": loc2, "more": False}},
        {"reason": "step", "description": "step in", "thread": T(model, "step", "step in"), "threads": threads,
         "threadCount": 4,
         "locals": {"thread": 1, "frame": 0, "function": "ForecastModel.dailyHigh(for:)", "variables": loc3, "more": False}},
    ]


def _daemon(cmd: str, args: dict) -> tuple[dict | None, dict | None]:
    """(data, error) from the demo device daemon; (None, None) when it isn't reachable."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from device_client import DeviceClient, DeviceError

    async def go():
        async with DeviceClient(name="debug demo", events=[]) as dev:
            return await dev.request(cmd, args, timeout=15)
    try:
        return asyncio.run(go()), None
    except DeviceError as exc:
        if exc.code in ("daemon-unavailable", "timeout", "disconnected"):
            return None, None
        return None, exc.to_json()
    except Exception:  # noqa: BLE001
        return None, None


def _console(text: str) -> None:
    common.event("console", text=text)


def _start(mode: str, flags: dict) -> tuple[dict | None, str, str]:
    bid = str(flags.get("bundle_id") or "")
    name, path = _proj(bid)
    common.event("debug", mode=mode, bundleId=bid)
    for sid, title, detail in (("lldb", "Checking the debugger", "lldb version 21.0.0 (demo)"),
                               ("project", "Finding the app's build on this computer", name)):
        common.step(sid, title, "running")
        time.sleep(0.25)
        common.step(sid, title, "done", detail=detail)
    common.step("forward", "Starting the debugger on your iPhone", "running")
    launch = str(flags.get("launch") or ("attach" if mode == "backtrace" else "auto"))
    st, e = _daemon("debug.start", {"bundleId": bid, "launch": launch})
    if e:
        common.step("forward", "Starting the debugger on your iPhone", "failed", error=e)
        common.event("done", ok=False, error=e)
        return None, name, path
    st = st or {"port": 52917, "pid": 400, "launched": False, "remotePath": f"/private/var/containers/Bundle/Application/DEMO/{name}.app/{name}",
                "executable": name, "ios": "27.2", "build": "24B115", "symbolsReady": True}
    time.sleep(0.4)
    common.step("forward", "Starting the debugger on your iPhone", "done", detail=f"port {st.get('port')}")
    exe = f"{path}/xtool/{name}.app/{name}"
    common.event("started", **{k: st.get(k) for k in ("port", "pid", "launched", "remotePath", "executable", "ios",
                                                    "build", "symbolsReady")}, localBuild=exe, project=name)
    if not st.get("symbolsReady"):
        common.event("notice", id="no-symbols", level="info",
                     message="This iPhone's debug symbols aren't downloaded, so Swift values (String, Array) may read "
                             "wrong and system frames show only library names. Download them once under iPhone › Debug › Debug symbols.")
    elif (st.get("symbolsUpdate") or {}).get("needed"):
        common.event("notice", id="symbols-update", level="warn",
                     message="Attaching may take minutes over Wi-Fi until the debug symbols are updated "
                             "(iPhone › Debug › Debug symbols › Update). The app stays paused meanwhile.")
    common.step("connect", "Connecting to the debugger on your iPhone", "running")
    _console("(lldb) settings set target.memory-module-load-level minimal")
    _console("(lldb) platform select remote-ios --sysroot \"~/.cache/ios-studio/DeviceSupport/27.2 (24B115)\"\n"
             "  Platform: remote-ios\n Connected: no\n  SDK Path: \"~/.cache/ios-studio/DeviceSupport/27.2 (24B115)\"")
    _console(f"(lldb) process connect connect://127.0.0.1:{st.get('port')}")
    time.sleep(0.6)
    common.step("connect", "Connecting to the debugger on your iPhone", "done")
    common.step("attach", "Attaching to the app (it pauses)", "running")
    time.sleep(1.2)
    _console(f"(lldb) process attach --pid {st.get('pid')}\nProcess {st.get('pid')} stopped\n"
             f"* thread #1, queue = 'com.apple.main-thread', stop reason = signal SIGSTOP\n"
             f"    frame #0: 0x00000001a2b30000 libsystem_kernel.dylib`mach_msg2_trap + 8")
    common.step("attach", "Attaching to the app (it pauses)", "done")
    common.event("attached", pid=st.get("pid"), process=name, threads=7, appSymbols=True, module=name)
    return st, name, path


def _end(bid: str, st: dict, how: str, data: dict) -> int:
    _console("(lldb) process kill" if how == "killed" else "(lldb) process detach\nProcess detached")
    stopped, _ = _daemon("debug.stop", {"bundleId": bid, "kill": how == "killed", "pid": st.get("pid")})
    return common.done(True, {"bundleId": bid, "pid": st.get("pid"), "launched": bool(st.get("launched")),
                              "symbolsReady": bool(st.get("symbolsReady")), "killed": (stopped or {}).get("killed"),
                              "ended": how, **data})


def backtrace(flags: dict) -> int:
    st, name, _ = _start("backtrace", flags)
    if st is None:
        return 1
    common.step("read", "Reading every thread", "running")
    time.sleep(0.7)
    threads = _threads(name)
    bt = {"pid": st.get("pid"), "threads": threads, "selectedThread": 1, "crashedThread": 3, "module": name}
    common.step("read", "Reading every thread", "done")
    common.event("backtrace", **bt)
    common.step("detach", "Letting the app run again", "done")
    return _end(str(flags.get("bundle_id") or ""), st, "detached", bt)


class Stdin:
    def __init__(self):
        self.buf = b""
        self.eof = False

    def poll(self, timeout: float) -> list[dict]:
        if self.eof:
            time.sleep(timeout)
            return []
        try:
            r, _, _ = select.select([0], [], [], timeout)
        except (OSError, ValueError):
            return []
        if not r:
            return []
        chunk = os.read(0, 65536)
        if not chunk:
            self.eof = True
            return [{"cmd": "detach"}]
        self.buf += chunk
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            with contextlib.suppress(ValueError):
                m = json.loads(line)
                if isinstance(m, dict):
                    if m.get("cancel"):
                        m = {"cmd": "detach"}
                    elif "prompt" in m and "cmd" not in m:
                        m = {"cmd": m.get("value")}
                    out.append(m)
        return out


def session(flags: dict) -> int:
    bid = str(flags.get("bundle_id") or "")
    try:
        specs = json.loads(flags.get("breakpoints") or "[]") if isinstance(flags.get("breakpoints"), str) else []
    except ValueError:
        specs = []
    st, name, _ = _start("session", flags)
    if st is None:
        return 1
    specs = [str(s) for s in specs if str(s).strip()] or ["ForecastView.swift:42"]
    bps = [{"id": i + 1, "spec": s, "locations": 1, "resolved": 1, "hits": 0} for i, s in enumerate(specs)]
    common.event("breakpoints", items=bps)
    states = _states(name)
    for s in states:
        if s.get("breakpoint"):
            s["breakpoint"]["spec"] = specs[0]
    stop_id = 0
    idx = 0
    running = True
    resume_at = time.time() + 1.5
    common.event("running")
    inp = Stdin()

    def stop(i: int, reason: str | None = None):
        nonlocal stop_id, idx, running
        stop_id += 1
        idx = i
        running = False
        s = dict(states[i], stopId=stop_id)
        if reason == "paused":
            s.update(reason="paused", description="signal SIGSTOP")
            s.pop("breakpoint", None)
        if i == 0 and reason != "paused":
            bps[0]["hits"] += 1
        common.event("stopped", **s)
        f0 = s["thread"]["frames"][0]
        _console(f"Process {st.get('pid')} stopped\n* thread #1, queue = 'com.apple.main-thread', stop reason = "
                 f"{s['description']}\n    frame #0: {f0['pc']} {name}`{f0['function']} at "
                 f"{os.path.basename(f0.get('file') or '')}:{f0.get('line') or ''}")
    while True:
        if running and resume_at and time.time() >= resume_at:
            resume_at = 0
            stop(0)
        for m in inp.poll(0.15):
            cmd = str(m.get("cmd") or "")
            rid = m.get("requestId")
            if cmd in ("detach", "kill"):
                return _end(bid, st, "killed" if cmd == "kill" else "detached", {"stops": stop_id})
            if cmd == "pause":
                if running:
                    stop(idx, "paused")
                common.event("ack", cmd=cmd, ok=True, requestId=rid)
                continue
            if cmd == "status":
                common.event("ack", cmd=cmd, ok=True, requestId=rid, running=running, breakpoints=bps)
                continue
            if cmd == "breakpoint_add":
                spec = str(m.get("spec") or "").strip()
                if not spec:
                    common.event("ack", cmd=cmd, ok=False, requestId=rid, code="bad-args", message="Which line or function?")
                    continue
                item = {"id": max([b["id"] for b in bps] or [0]) + 1, "spec": spec, "locations": 1, "resolved": 1, "hits": 0}
                bps.append(item)
                _console(f"(lldb) breakpoint set {spec}\nBreakpoint {item['id']}: 1 location.")
                common.event("ack", cmd=cmd, ok=True, requestId=rid, breakpoint=item)
                common.event("breakpoints", items=bps)
                continue
            if cmd == "breakpoint_delete":
                bps[:] = [b for b in bps if b["id"] != int(m.get("id") or 0)]
                common.event("ack", cmd=cmd, ok=True, requestId=rid)
                common.event("breakpoints", items=bps)
                continue
            if running:
                common.event("ack", cmd=cmd, ok=False, requestId=rid, code="running", message="The app is running. Pause it first.")
                continue
            if cmd == "continue":
                running = True
                common.event("running", cmd=cmd)
                _console(f"(lldb) continue\nProcess {st.get('pid')} resuming")
                resume_at = time.time() + 2.0 if bps else 0
            elif cmd in ("next", "step", "finish"):
                common.event("running", cmd=cmd)
                time.sleep(0.5)
                stop({"next": [1, 2, 1], "step": [2, 2, 1], "finish": [1, 1, 1]}[cmd][idx])
            elif cmd in ("frame_variables", "select_frame"):
                fr = int(m.get("frame") or 0)
                loc = dict(states[idx]["locals"])
                if fr:
                    frames = states[idx]["thread"]["frames"]
                    f = frames[min(fr, len(frames) - 1)]
                    loc = {"thread": 1, "frame": fr, "function": f["function"],
                           "variables": [V("self", "ForecastView", None, [V("unit", "TemperatureUnit", ".celsius")])]
                           if f.get("inApp") else [], "more": False}
                common.event("variables", requestId=rid, **loc)
            elif cmd == "evaluate":
                expr = str(m.get("expr") or "").strip()
                known = {v["name"]: v for v in states[idx]["locals"]["variables"]}
                if expr in known:
                    common.event("evaluated", requestId=rid, expr=expr, ok=True, result=dict(known[expr], name="$R0"))
                elif expr in ("days.count", "self.days.count", "count"):
                    common.event("evaluated", requestId=rid, expr=expr, ok=True, result=V("$R0", "Int", "7"))
                else:
                    common.event("evaluated", requestId=rid, expr=expr, ok=False,
                                 result=V("$R0", "", None) | {"error": f"error: cannot find '{expr[:40]}' in scope"})
            elif cmd == "backtrace":
                th = [dict(states[idx]["thread"])] + _threads(name)[1:2] + _threads(name)[3:]
                common.event("backtrace", requestId=rid, pid=st.get("pid"), threads=th, selectedThread=1,
                             crashedThread=None, module=name)
            else:
                common.event("ack", cmd=cmd, ok=False, requestId=rid, code="bad-command",
                             message=f"Unknown debugger command '{cmd[:40]}'.")


def check(flags: dict) -> int:
    return common.ok({"ready": True, "lldb": "/usr/lib/swift/usr/bin/lldb", "lldbVersion": "lldb version 21.0.0 (demo)",
                      "python": "/usr/bin/python3.12"})


def sources(flags: dict) -> int:
    bid = str(flags.get("bundle_id") or "")
    name, path = _proj(bid)
    if not bid.endswith(("weatherglance", "habits", "recipebox")):
        return common.ok({"project": None, "files": [], "localBuild": None})
    files = [{"path": _src(name, f), "rel": f"Sources/{name}/{f}", "name": f, "lines": n} for f, n in SOURCES[name]]
    return common.ok({"project": {"name": name, "path": path, "displayPath": f"~/Developer/{name}"}, "files": files,
                      "localBuild": f"{path}/xtool/{name}.app/{name}"})


def run(action: str, flags: dict) -> int:
    fn = {"check": check, "sources": sources, "backtrace": backtrace, "session": session}.get(action)
    if fn is None:
        return common.fail("usage", f"Unknown command 'debug {action}'.")
    return fn(flags)
