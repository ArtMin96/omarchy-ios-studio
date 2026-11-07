"""tools/debug.py: backtrace job, breakpoint sessions and the symbols status, against fakes that behave like the real
backends (tests/support/mcp_harness.py).

- The iPhone is the fake device daemon (apps.list, debug.symbols) talking to the real DeviceClient.
- `debug backtrace` is a FakeHelper stream shaped like helper/iosstudio/features/debugger.py's (step events, then
  `done` with the backtrace data); `debug session` is an interactive fake helper below that speaks the session
  protocol (started/breakpoints/running/stopped/variables/evaluated/ack/done on stdout, JSON commands on stdin).

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_cov_debug.py
"""
from __future__ import annotations

import asyncio
import json
import os
import textwrap

from iosstudio_mcp.tools import debug as DEBUG
from iosstudio_mcp.tools import jobs as JOBS
from support.mcp_harness import FAKE_HELPER, ToolCase, err, size_of

BID = "com.example.Weather"
APPS = [{"bundleId": BID, "name": "Weather", "developer": True, "running": True},
        {"bundleId": "com.example.Idle", "name": "Idle", "developer": True, "running": False},
        {"bundleId": "com.apple.mobilesafari", "name": "Safari", "developer": False, "running": True}]
HOME = os.path.expanduser("~")
LOCAL = f"{HOME}/Developer/Weather/xtool/Weather.app/Weather"
SRC = f"{HOME}/Developer/Weather/Sources/Weather/ForecastModel.swift"


def frame(i, fn, mod, file=None, line=None):
    f = {"index": i, "function": fn, "module": mod, "pc": f"0x{0x1000 + i:x}", "inApp": mod == "Weather"}
    if file:
        f.update(file=file, line=line)
    return f


def thread(i, frames, crashed=False, name=None, queue=None):
    return {"index": i, "name": name, "queue": queue, "reason": "exception" if crashed else "signal",
            "description": "EXC_BREAKPOINT" if crashed else "signal SIGSTOP", "numFrames": len(frames),
            "frames": frames, "crashed": crashed}


IDLE = [frame(0, "__workq_kernreturn", "libsystem_kernel.dylib"), frame(1, "start_wqthread", "libsystem_pthread.dylib")]
MAIN = [frame(0, "mach_msg2_trap", "libsystem_kernel.dylib"), frame(1, "UIApplicationMain", "UIKitCore")]
CRASH = [frame(0, "ForecastModel.dailyHigh(for:)", "Weather", SRC, 58), frame(1, "_dispatch_worker", "libdispatch.dylib")]
# Many threads with identical stacks (grouped) plus some with very long frames: the result must stay in budget.
THREADS = [thread(1, MAIN, queue="com.apple.main-thread"), thread(2, CRASH, crashed=True)] + \
    [thread(i, IDLE) for i in range(3, 60)] + \
    [thread(100 + i, [frame(j, f"f{i}_{j}_" + "x" * 300, f"Lib{i}") for j in range(30)]) for i in range(20)]
BT_DATA = {"bundleId": BID, "pid": 401, "launched": False, "localBuild": LOCAL, "project": "Weather",
           "symbolsReady": False, "killed": None, "threads": THREADS, "selectedThread": 1, "crashedThread": 2,
           "ended": "detached"}
STEPS = [{"event": "step", "id": "project", "title": "Finding the app's build on this computer", "state": "done"},
         {"event": "step", "id": "forward", "title": "Starting the debugger on your iPhone", "state": "done"}]

# The interactive `debug session`: a stop right away when breakpoints were given, else running until 'pause'.
SESSION = textwrap.dedent("""\
    def session(argv):
        import select
        flags = dict(zip(argv[3::2], argv[4::2]))
        bid = flags.get("--bundle-id")
        bps = json.loads(flags.get("--breakpoints") or "[]")
        def out(name, **kw):
            print(json.dumps({"event": name, **kw}), flush=True)
        if bid == "com.example.Idle":
            out("done", ok=False, error={"code": "missing-tool", "message": "lldb isn't installed."})
            return
        out("started", pid=401, launched=flags.get("--launch") == "suspended", localBuild=LOCAL)
        out("breakpoints", items=[{"id": i + 1, "spec": s, "locations": 0 if "Nope" in s else 1}
                                  for i, s in enumerate(bps)])
        out("running")
        stops = [0]
        def stop(reason="breakpoint"):
            stops[0] += 1
            frames = [{"index": 0, "function": "ForecastView.body.getter", "module": "Weather", "inApp": True,
                       "file": SRC, "line": 40 + stops[0]}]
            out("stopped", stopId=stops[0], reason=reason, description=reason,
                breakpoint={"id": 1, "spec": bps[0]} if bps else None,
                thread={"index": 1, "numFrames": 1, "frames": frames},
                locals={"variables": [{"name": "day", "type": "Day", "value": "Mon", "childCount": 1,
                                       "children": [{"name": "high", "type": "Double", "value": "24.5"}]}]})
        if bps and not any("Nope" in s for s in bps):
            out("console", text="Process 401 stopped")
            stop()
        for line in sys.stdin:
            m = json.loads(line)
            cmd, rid = m.get("cmd"), m.get("requestId")
            if cmd in ("detach", "kill"):
                out("done", ok=True, data={"ended": "killed" if cmd == "kill" else "detached", "stops": stops[0]})
                return
            if cmd == "finish":
                out("ack", cmd=cmd, ok=False, requestId=rid, message="Can't finish out of the outermost frame.")
            elif cmd in ("next", "step"):
                out("ack", cmd=cmd, ok=True, requestId=rid)
                out("running")
                stop("step")
            elif cmd == "continue":
                out("ack", cmd=cmd, ok=True, requestId=rid)
                out("running")
            elif cmd == "pause":
                stop("paused")
                out("ack", cmd=cmd, ok=True, requestId=rid)
            elif cmd == "frame_variables":
                out("variables", requestId=rid, frame=m.get("frame", 0), function="ForecastView.body.getter",
                    variables=[{"name": f"v{i}", "type": "Int", "value": str(i) * 400} for i in range(50)])
            elif cmd == "evaluate":
                out("evaluated", requestId=rid, expr=m.get("expr"), ok=True,
                    result={"name": "$R0", "type": "Int", "value": "7"})
        out("done", ok=True, data={"ended": "detached", "stops": stops[0]})
""")


class DebugCase(ToolCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.daemon.replies["apps.list"] = (0, APPS)
        script = FAKE_HELPER.replace(
            'key = " ".join(sys.argv[1:3])\n',
            'key = " ".join(sys.argv[1:3])\n'
            'if key == "debug session":\n    session(sys.argv)\n    sys.exit(0)\n')
        script = script.replace("me = sys.argv[0]\n", f"me = sys.argv[0]\nLOCAL = {LOCAL!r}\nSRC = {SRC!r}\n"
                                + SESSION)
        self.helper.exe.write_text(script)

    async def asyncTearDown(self):
        await DEBUG.on_shutdown(self.state)      # like the server: every open session is detached
        pumps = [t for t in asyncio.all_tasks() if t.get_name().startswith("debug-dbg-")]
        await asyncio.wait_for(asyncio.gather(*pumps, return_exceptions=True), 5)   # the helpers are reaped
        await super().asyncTearDown()


class Annotations(DebugCase):
    async def test_kinds(self):
        ann = self.rec(DEBUG).annotations
        for name in ("ios_debug_backtrace", "ios_debug_session_start", "ios_debug_session_command"):
            self.assertFalse(ann[name].read_only_hint, name)
            self.assertFalse(ann[name].destructive_hint, name)
            self.assertTrue(ann[name].open_world_hint, name)
        self.assertTrue(ann["ios_debug_session_wait"].read_only_hint)
        self.assertFalse(ann["ios_debug_session_wait"].idempotent_hint)
        self.assertTrue(ann["ios_debug_session_end"].idempotent_hint)
        self.assertTrue(ann["ios_debug_symbols_status"].read_only_hint)
        self.assertTrue(ann["ios_debug_symbols_status"].open_world_hint)


class Backtrace(DebugCase):
    async def test_without_confirm_it_only_explains(self):
        res = await self.call(DEBUG, "ios_debug_backtrace", bundle_id=BID)
        d = res.structured_content
        self.assertEqual(d["state"], "dry_run")
        self.assertIn("pauses", d["message"])
        self.assertIn("confirm=true", d["next_steps"][0])
        self.assertEqual(self.helper.calls("debug"), [])          # nothing attached

    async def test_a_job_polled_through_ios_job_status_then_summarised_in_budget(self):
        self.helper.stream("debug", "backtrace", STEPS + [{"event": "done", "ok": True, "data": BT_DATA}], delay=0.5)
        first = await self.data(DEBUG, "ios_debug_backtrace", bundle_id=BID, confirm=True, wait_seconds=0)
        self.assertEqual(first["state"], "running")
        self.assertTrue(first["job_id"].startswith("dbg-"))
        st = await self.data(JOBS, "ios_job_status", job_id=first["job_id"], wait_seconds=3)
        argv = self.helper.calls("debug", "backtrace")[0]
        self.assertEqual(self.helper.flag(argv, "bundle_id"), BID)
        self.assertEqual((st["state"], st["kind"]), ("succeeded", "debug"))
        res = await self.call(DEBUG, "ios_debug_backtrace", job_id=first["job_id"], wait_seconds=0)
        self.assertLessEqual(size_of(res), 12_000)
        d = res.structured_content
        self.assertEqual((d["state"], d["crashed_thread"], d["thread_count"]), ("succeeded", 2, len(THREADS)))
        self.assertEqual(d["focus"]["index"], 2)
        self.assertIn("Sources/Weather/ForecastModel.swift:58", d["focus"]["frames"][0]["text"])
        idle = next(g for g in d["other_threads"] if g["count"] == 57)
        self.assertEqual(len(idle["threads"]), 20)                # group thread lists are capped
        self.assertLessEqual(len(d["other_threads"]), DEBUG.MAX_GROUPS)
        self.assertTrue(d["truncated"])                            # more groups than shown: they're in the file
        self.assertTrue(os.path.isfile(os.path.expanduser(d["full_output_path"])))
        self.assertTrue(any("debug symbols" in s for s in d["next_steps"]))

    async def test_a_failed_attach_is_a_failed_result_with_a_next_step(self):
        self.helper.stream("debug", "backtrace", STEPS[:1] + [{
            "event": "done", "ok": False, "error": {"code": "missing-tool", "message": "lldb isn't installed."},
            "data": {"bundleId": BID}}])
        d = await self.data(DEBUG, "ios_debug_backtrace", bundle_id=BID, confirm=True, wait_seconds=3)
        self.assertEqual(d["state"], "failed")
        self.assertEqual(d["error"]["code"], "missing-tool")
        self.assertIn("ios_toolchain_check", d["next_steps"][-1])

    async def test_refusals_are_one_line_with_the_next_step(self):
        e = await self.error(DEBUG, "ios_debug_backtrace")
        self.assertIn("ios_app_list", e)
        e = await self.error(DEBUG, "ios_debug_backtrace", bundle_id="com.apple.mobilesafari", confirm=True)
        self.assertIn("ios_app_list(scope='own')", e)
        e = await self.error(DEBUG, "ios_debug_backtrace", bundle_id="com.example.Idle", confirm=True)
        self.assertIn("ios_app_launch", e)
        e = await self.error(DEBUG, "ios_debug_backtrace", bundle_id="com.example.Gone", confirm=True)
        self.assertIn("ios_run", e)
        self.daemon.replies["apps.list"] = (0, err("developer-mode-off", "Developer Mode is off."))
        e = await self.error(DEBUG, "ios_debug_backtrace", bundle_id=BID, confirm=True)
        self.assertIn("[developer-mode-off] Next: Call ios_device_prepare", e)


class Sessions(DebugCase):
    async def test_breakpoint_stop_step_inspect_and_end(self):
        res = await self.call(DEBUG, "ios_debug_session_start", bundle_id=BID, breakpoints=["ForecastView.swift:42"],
                              wait_seconds=3)
        self.assertLessEqual(size_of(res), 12_000)
        d = res.structured_content
        sid = d["session_id"]
        self.assertEqual((d["state"], d["new_stop"]), ("stopped", True))
        self.assertEqual(d["stop"]["location"], "ForecastModel.swift:41")
        self.assertEqual(d["stop"]["locals"][0]["children"], ["high: Double = 24.5"])
        self.assertEqual(d["breakpoints"], ["#1 ForecastView.swift:42 (1 location)"])
        argv = self.helper.calls("debug", "session")[0]
        self.assertEqual(json.loads(self.helper.flag(argv, "breakpoints")), ["ForecastView.swift:42"])

        d = await self.data(DEBUG, "ios_debug_session_command", session_id=sid, command="next", wait_seconds=3)
        self.assertEqual((d["state"], d["stop"]["stop_id"], d["new_stop"]), ("stopped", 2, True))
        d = await self.data(DEBUG, "ios_debug_session_wait", session_id=sid, wait_seconds=0)
        self.assertFalse(d["new_stop"])                            # the same stop isn't new twice

        res = await self.call(DEBUG, "ios_debug_session_command", session_id=sid, command="frame_variables", frame=0)
        self.assertLessEqual(size_of(res), 12_000)
        r = res.structured_content["result"]
        self.assertLessEqual(len(r["variables"]), DEBUG.MAX_LOCALS)    # 50 locals: capped, then cut to the budget
        self.assertTrue(r["more"])
        self.assertLessEqual(len(r["variables"][9]["value"]), DEBUG.VALUE_CUT + 10)
        d = await self.data(DEBUG, "ios_debug_session_command", session_id=sid, command="evaluate", expr="days.count")
        self.assertEqual((d["result"]["value"], d["result"]["ok"]), ("7", True))

        e = await self.error(DEBUG, "ios_debug_session_command", session_id=sid, command="finish", wait_seconds=3)
        self.assertIn("outermost frame", e)
        e = await self.error(DEBUG, "ios_debug_session_command", session_id=sid, command="evaluate", expr=" ")
        self.assertIn("expr=", e)

        d = await self.data(DEBUG, "ios_debug_session_end", session_id=sid)
        self.assertTrue(d["done"])
        self.assertEqual(d["data"]["ended"], "detached")
        self.assertFalse(d.get("next_steps"))
        e = await self.error(DEBUG, "ios_debug_session_wait", session_id=sid)
        self.assertIn("ios_debug_session_start", e)

    async def test_running_until_paused_and_kill(self):
        d = await self.data(DEBUG, "ios_debug_session_start", bundle_id=BID, breakpoints=["Nope.swift:1"],
                            wait_seconds=0.5)
        sid = d["session_id"]
        self.assertEqual(d["state"], "running")
        self.assertTrue(any("0 locations" in s for s in d["next_steps"]))
        e = await self.error(DEBUG, "ios_debug_session_command", session_id=sid, command="frame_variables")
        self.assertIn("command='pause'", e)
        d = await self.data(DEBUG, "ios_debug_session_command", session_id=sid, command="pause", wait_seconds=3)
        self.assertEqual((d["state"], d["stop"]["reason"]), ("stopped", "paused"))
        e = await self.error(DEBUG, "ios_debug_session_start", bundle_id=BID)
        self.assertIn("already open", e)
        d = await self.data(DEBUG, "ios_debug_session_end", session_id=sid, kill=True)
        self.assertEqual(d["data"]["ended"], "killed")

    async def test_a_session_that_fails_reports_the_next_step(self):
        d = await self.data(DEBUG, "ios_debug_session_start", bundle_id="com.example.Idle", wait_seconds=3)
        self.assertEqual((d["state"], d["error"]["code"]), ("failed", "missing-tool"))
        self.assertIn("ios_toolchain_check", d["next_steps"][0])
        d = await self.data(DEBUG, "ios_debug_session_end", session_id=d["session_id"])
        self.assertTrue(d["next_steps"])                          # it never detached cleanly

    async def test_refusals(self):
        e = await self.error(DEBUG, "ios_debug_session_start", bundle_id="com.apple.mobilesafari")
        self.assertIn("ios_app_list(scope='own')", e)
        e = await self.error(DEBUG, "ios_debug_session_command", session_id="dbg-s999", command="next")
        self.assertIn("ios_debug_session_start", e)
        e = await self.error(DEBUG, "ios_debug_session_end", session_id="dbg-s999")
        self.assertIn("ios_debug_session_start", e)


class Symbols(DebugCase):
    async def test_outdated_symbols_say_how_to_fix(self):
        self.helper.reply("debug", "check", {"ready": True, "lldb": "/usr/bin/lldb", "lldbVersion": "21.0"})
        self.daemon.replies["debug.symbols"] = (0, {
            "state": "outdated", "ready": True, "ios": "27.2", "build": "24B115", "canUnpack": False,
            "update": {"needed": True, "reason": "The iPhone was updated to 27.2."}, "estimatedBytes": 7_000_000_000,
            "freeBytes": 50_000_000_000, "libs": 900})
        res = await self.call(DEBUG, "ios_debug_symbols_status")
        self.assertLessEqual(size_of(res), 12_000)
        d = res.structured_content
        self.assertTrue(d["debugger_ready"] and d["symbols_ready"] and d["update_needed"])
        self.assertEqual(d["state"], "outdated")
        self.assertIn("press Update", d["how_to_fix"])
        self.assertIn("7.0 GB", d["how_to_fix"])
        self.assertEqual(self.daemon.sent("debug.symbols")[0]["action"], "status")

    async def test_missing_lldb_names_the_toolchain_step(self):
        self.helper.reply("debug", "check", {"ready": False, "error": {"code": "missing-tool", "message": "No lldb."}})
        self.daemon.replies["debug.symbols"] = (0, {"state": "ready", "ready": True})
        d = await self.data(DEBUG, "ios_debug_symbols_status")
        self.assertFalse(d["debugger_ready"])
        self.assertEqual(d["debugger_problem"], "No lldb.")
        self.assertIsNone(d.get("how_to_fix"))
        self.assertIn("ios_toolchain_check", d["next_steps"][0])

    async def test_a_device_error_is_one_line_with_next(self):
        self.helper.reply("debug", "check", {"ready": True})
        self.daemon.replies["debug.symbols"] = (0, err("not-paired", "The iPhone isn't paired."))
        e = await self.error(DEBUG, "ios_debug_symbols_status")
        self.assertIn("[not-paired] Next:", e)
