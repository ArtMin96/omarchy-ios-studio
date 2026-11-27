"""`debug session` end to end under the real toolchain lldb, against a process under a local lldb-server (no phone):
a stop goes out with its frames at once and the locals follow as a `variables` event with the stop's stopId (forced
with IOSSTUDIO_DEBUG_QUICK_LOCALS=0, as over a slow Wi-Fi link), reads and steps still work, and a detach lets the
process run on. Skipped without the toolchain's lldb-server, lldb's Python (Setup's debugger support) or cc.

Deterministic on purpose: lldb-server picks its own port and says so before the session starts
(support.lldb_target), the target runs until killed, every wait has a timeout, and a failing wait prints what the
stream said (Stream.story) and lldb-server's log."""
from __future__ import annotations

import json
import time
import unittest

import test_editor_debug_cli as ed

from support.fake_deviced import FakeDeviced
from support.lldb_target import BREAK_LINE, build, start_gdbserver, start_target


class Stream(ed.Stream):
    def send(self, msg: dict) -> None:
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()


@unittest.skipUnless(ed._tools(), "needs the Swift toolchain's lldb-server, lldb's Python and cc")
class LateLocals(ed.Base):
    def test_stop_first_then_locals_then_step_and_detach(self):
        tools = ed._tools()
        proj = ed.make_project(self.home / "Developer")    # a project root `debug session` scans
        exe = build(tools["cc"], proj / "Sources/Target/target.c", proj / ".build/debug/Target-App")
        target, pid = start_target(self, exe)
        log = self.tmp / "lldb-server.log"
        _server, port = start_gdbserver(self, tools["server"], log)
        installed = "XTL-TEAM123456.com.example.target"
        replies = {
            "debug.start": {"bundleId": installed, "port": port, "host": "127.0.0.1", "pid": pid, "launched": False,
                            "remotePath": str(exe), "symbolsReady": False},
            "debug.stop": lambda a: {"bundleId": a["bundleId"], "stopped": True, "killed": None},
        }
        env = {**self.env, "IOSSTUDIO_DEBUG_PLATFORM": "remote-linux", "IOSSTUDIO_DEBUG_QUICK_LOCALS": "0"}
        with FakeDeviced(self.run_dir, replies) as daemon:
            s = Stream(["debug", "session", "--bundle-id", installed,
                        "--breakpoints", json.dumps([f"target.c:{BREAK_LINE}"])], env)
            self.addCleanup(s.close)
            s.logs.append(log)                                # a failing wait shows lldb-server's side too
            stop = s.event("stopped", timeout=120)
            self.assertEqual(stop["reason"], "breakpoint")
            self.assertEqual((stop["thread"]["frames"][0]["function"], stop["thread"]["frames"][0]["line"]),
                             ("tick", BREAK_LINE))
            self.assertTrue(stop["localsPending"])
            self.assertNotIn("locals", stop)
            late = s.wait(lambda e: e.get("event") == "variables" and e.get("stopId") == stop["stopId"]
                          and not e.get("requestId"), 60)
            self.assertIn("n", [v["name"] for v in late["variables"]])
            self.assertFalse(late["partial"])
            self.assertIsInstance(late["elapsedMs"], int)
            # memory reads go through cache lines of the target's page size: 4 KB on this x86_64 Linux process
            # (16 KB on an iPhone, tests/helper/test_debugger_hardening.py)
            self.assertTrue(any("memory-cache-line-size 4096" in (e.get("text") or "") for e in s.lines), s.story())

            s.send({"cmd": "frame_variables", "requestId": "fv"})
            fv = s.event("variables", requestId="fv")
            self.assertIn("n", [v["name"] for v in fv["variables"]])
            s.send({"cmd": "evaluate", "expr": "n + 1", "requestId": "e1"})
            ev = s.event("evaluated", requestId="e1")
            self.assertTrue(ev["ok"], ev)
            self.assertEqual(ev["result"]["value"], str(int(next(v for v in fv["variables"] if v["name"] == "n")
                                                            ["value"]) + 1))

            s.send({"cmd": "next", "requestId": "n1"})
            nxt = s.wait(lambda e: e.get("event") == "stopped" and e.get("stopId", 0) > stop["stopId"], 60)
            self.assertEqual(nxt["reason"], "step")
            s.send({"cmd": "status", "requestId": "st"})
            self.assertIn("reading", s.event("ack", requestId="st"))

            t0 = time.monotonic()
            s.send({"cmd": "detach"})
            done = s.event("done", timeout=60)
            self.assertTrue(done["ok"], done)
            self.assertEqual(done["data"]["ended"], "detached")
            self.assertLess(time.monotonic() - t0, 30)
            self.assertEqual(daemon.sent("debug.stop")[0]["bundleId"], installed)
            self.assertIsNone(target.poll())                 # detaching let the process run on
            self.assertEqual(s.p.wait(10), 0)


if __name__ == "__main__":
    unittest.main()
