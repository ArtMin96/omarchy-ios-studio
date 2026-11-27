"""`debug session` end to end under the real toolchain lldb, against a process under a local lldb-server (no phone):
a stop goes out with its frames at once and the locals follow as a `variables` event with the stop's stopId (forced
with IOSSTUDIO_DEBUG_QUICK_LOCALS=0, as over a slow Wi-Fi link), reads and steps still work, and a detach lets the
process run on. Skipped without the toolchain's lldb-server, lldb's Python (Setup's debugger support) or cc."""
from __future__ import annotations

import json
import socket
import subprocess
import time
import unittest

import test_editor_debug_cli as ed

from support.fake_deviced import FakeDeviced


class Stream(ed.Stream):
    def send(self, msg: dict) -> None:
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()


@unittest.skipUnless(ed._tools(), "needs the Swift toolchain's lldb-server, lldb's Python and cc")
class LateLocals(ed.Base):
    def test_stop_first_then_locals_then_step_and_detach(self):
        tools = ed._tools()
        proj = ed.make_project(self.home / "Developer")    # a project root `debug session` scans
        src = proj / "Sources/Target/target.c"
        src.write_text(ed.TARGET_C)
        exe = proj / ".build/debug/Target-App"
        exe.parent.mkdir(parents=True)
        subprocess.run([tools["cc"], "-g", "-O0", "-o", str(exe), str(src)], check=True, timeout=60)
        line = next(i for i, x in enumerate(ed.TARGET_C.splitlines(), 1) if "BREAK" in x)
        target = subprocess.Popen([str(exe)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(target.kill)
        pid = int(target.stdout.readline())
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = subprocess.Popen([tools["server"], "gdbserver", f"127.0.0.1:{port}"], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        self.addCleanup(server.kill)
        installed = "XTL-TEAM123456.com.example.target"
        replies = {
            "debug.start": {"bundleId": installed, "port": port, "host": "127.0.0.1", "pid": pid, "launched": False,
                            "remotePath": str(exe), "symbolsReady": False},
            "debug.stop": lambda a: {"bundleId": a["bundleId"], "stopped": True, "killed": None},
        }
        env = {**self.env, "IOSSTUDIO_DEBUG_PLATFORM": "remote-linux", "IOSSTUDIO_DEBUG_QUICK_LOCALS": "0"}
        with FakeDeviced(self.run_dir, replies) as daemon:
            s = Stream(["debug", "session", "--bundle-id", installed,
                        "--breakpoints", json.dumps([f"target.c:{line}"])], env)
            self.addCleanup(s.close)
            stop = s.event("stopped", timeout=120)
            self.assertEqual(stop["reason"], "breakpoint")
            self.assertEqual((stop["thread"]["frames"][0]["function"], stop["thread"]["frames"][0]["line"]), ("tick", line))
            self.assertTrue(stop["localsPending"])
            self.assertNotIn("locals", stop)
            late = s.wait(lambda e: e.get("event") == "variables" and e.get("stopId") == stop["stopId"]
                          and not e.get("requestId"), 60)
            self.assertIn("n", [v["name"] for v in late["variables"]])
            self.assertFalse(late["partial"])
            self.assertIsInstance(late["elapsedMs"], int)
            # memory reads go through 16 KB cache lines (fewer round trips over Wi-Fi)
            self.assertTrue(any("memory-cache-line-size 16384" in (e.get("text") or "") for e in s.lines))

            s.send({"cmd": "frame_variables", "requestId": "fv"})
            fv = s.event("variables", requestId="fv")
            self.assertIn("n", [v["name"] for v in fv["variables"]])
            s.send({"cmd": "evaluate", "expr": "n + 1", "requestId": "e1"})
            ev = s.event("evaluated", requestId="e1")
            self.assertTrue(ev["ok"], ev)

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
