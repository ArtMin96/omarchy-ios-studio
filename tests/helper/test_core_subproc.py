"""core.subproc: tool_env kinds, run_cmd(check=False) and the streaming Runner (lines, partial-line prompts with
answers to the child, stdin cancel, timeout, bounded buffers, whole-group kills) against fake executables.
    python -m pytest tests/helper/test_core_subproc.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.core import appdirs, subproc
from iosstudio.core.output import HelperError
from support import write_exe

# Starts a long-lived grandchild (its pid goes to $1), prints one line, then hangs.
HANGS = """#!/bin/sh
sleep 30 &
echo $! > "$1"
echo started
sleep 30
"""
# Asks a question without a newline, then echoes the answer it reads.
ASKS = """#!/bin/sh
echo hello
printf 'Password: '
read answer
echo "got $answer"
"""


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def gone_soon(pid: int, within: float = 3.0) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)

    def exe(self, name: str, text: str) -> str:
        return str(write_exe(self.tmp / name, text))

    def grandchild(self) -> int:
        pidfile = self.tmp / "pid"
        end = time.monotonic() + 3
        while time.monotonic() < end and not (pidfile.exists() and pidfile.read_text().strip()):
            time.sleep(0.02)
        return int(pidfile.read_text())

    def stdin_pipe(self):
        """Replace sys.stdin with a pipe; returns the write fd (closed on cleanup)."""
        r, w = os.pipe()
        rf = os.fdopen(r, "r")
        self.addCleanup(rf.close)
        patcher = mock.patch.object(sys, "stdin", rf)
        patcher.start()
        self.addCleanup(patcher.stop)

        def close_w():
            try:
                os.close(w)
            except OSError:
                pass
        self.addCleanup(close_w)
        return w


class ToolEnv(unittest.TestCase):
    def test_kinds(self):
        with mock.patch.dict(os.environ, {"PATH": "/home/u/.local/share/mise/shims:/opt/x"}, clear=True):
            plain = subproc.tool_env()
            swift = subproc.tool_env("swift")
            xtool = subproc.tool_env("xtool")
            setup = subproc.tool_env("setup", {"A": "1"})
        local = str(appdirs.LOCAL_BIN)
        self.assertTrue(plain["PATH"].startswith(local + ":"))
        self.assertEqual(plain["NO_COLOR"], "1")
        self.assertEqual((swift["TERM"], swift["GIT_TERMINAL_PROMPT"]), ("dumb", "0"))
        self.assertNotIn("mise", xtool["PATH"])
        self.assertIn("/opt/x", xtool["PATH"].split(":"))
        self.assertTrue(xtool["PATH"].startswith(local + ":"))
        self.assertTrue(setup["PATH"].startswith(f"/usr/bin:{local}:"))
        self.assertEqual((setup["LANG"], setup["A"]), ("C.UTF-8", "1"))
        with self.assertRaises(ValueError):
            subproc.tool_env("nope")


class RunCmd(Base):
    def test_check_false_never_raises(self):
        r = subproc.run_cmd([self.tmp / "missing"], check=False)
        self.assertEqual((r.returncode, r.stderr), (127, "not found"))
        r = subproc.run_cmd([self.exe("slow", "#!/bin/sh\nsleep 5\n")], timeout=0.2, check=False)
        self.assertEqual(r.returncode, 124)
        with self.assertRaises(HelperError):
            subproc.run_cmd([self.tmp / "missing"])

    def test_stdin_is_not_inherited(self):
        r = subproc.run_cmd([self.exe("cat", "#!/bin/sh\ncat\necho done\n")], timeout=5)
        self.assertEqual(r.stdout, "done\n")


class Streaming(Base):
    def test_lines_crlf_and_tail(self):
        exe = self.exe("out", "#!/bin/sh\nprintf 'a\\r\\nb\\rc\\nlast'\nexit 4\n")
        lines: list[str] = []
        runner = subproc.Runner(lines.append, tail_lines=2)
        self.assertEqual(runner.run([exe]), 4)
        self.assertEqual(lines, ["a", "b", "c", "last"])
        self.assertEqual(list(runner.tail), ["c", "last"])

    def test_flood_comes_in_pieces(self):
        exe = self.exe("flood", "#!/bin/sh\nhead -c 300000 /dev/zero | tr '\\0' x\n")
        sizes: list[int] = []
        self.assertEqual(subproc.Runner(lambda t: sizes.append(len(t))).run([exe]), 0)
        self.assertEqual(sum(sizes), 300000)
        self.assertLessEqual(max(sizes), subproc.MAX_LINE)

    def test_partial_line_prompt_and_answer(self):
        self.stdin_pipe()
        lines: list[str] = []
        runner = subproc.Runner(lines.append)

        def on_partial(text: str) -> bool:
            if text.endswith("Password: "):
                runner.write("secret\n")
                return True
            return False
        runner.on_partial = on_partial
        self.assertEqual(runner.run([self.exe("ask", ASKS)], child_stdin=True), 0)
        self.assertEqual(lines, ["hello", "got secret"])

    def test_next_message_answers_a_prompt(self):
        w = self.stdin_pipe()
        os.write(w, b'not json\n{"other": 1}\n{"prompt": "p1", "value": "42"}\n')
        runner = subproc.Runner(watch_stdin=True)
        self.assertEqual(runner.next_message(), {"other": 1})
        self.assertEqual(runner.next_message(), {"prompt": "p1", "value": "42"})
        os.close(w)
        self.assertIsNone(runner.next_message())

    def test_stdin_cancel_kills_grandchildren(self):
        w = self.stdin_pipe()
        seen: list[str] = []

        def on_line(line):
            seen.append(line)
            os.write(w, b'{"cancel": true}\n')
        t0 = time.monotonic()
        with self.assertRaises(subproc.Cancelled):
            subproc.Runner(on_line, watch_stdin=True).run([self.exe("hang", HANGS), self.tmp / "pid"])
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(seen, ["started"])
        self.assertTrue(gone_soon(self.grandchild()))

    def test_other_messages_go_to_on_message(self):
        w = self.stdin_pipe()
        got: list[dict] = []
        os.write(w, b'{"hello": 1}\n')
        exe = self.exe("nap", "#!/bin/sh\nsleep 0.6\necho x\n")
        self.assertEqual(subproc.Runner(on_message=got.append, watch_stdin=True).run([exe]), 0)
        self.assertEqual(got, [{"hello": 1}])

    def test_timeout_kills_grandchildren(self):
        t0 = time.monotonic()
        with self.assertRaises(HelperError) as cm:
            subproc.Runner().run([self.exe("hang", HANGS), self.tmp / "pid"], timeout=0.5)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(gone_soon(self.grandchild()))

    def test_tick_can_stop_the_run(self):
        ticks = []

        def on_tick():
            ticks.append(1)
            if len(ticks) > 2:
                raise HelperError("device-not-found", "no phone")
        with self.assertRaises(HelperError):
            subproc.Runner(on_tick=on_tick).run([self.exe("hang", HANGS), self.tmp / "pid"], kill_grace=0.5)
        self.assertTrue(gone_soon(self.grandchild()))

    def test_leftovers_killed_after_normal_exit(self):
        exe = self.exe("bg", f"#!/bin/sh\nsleep 30 >/dev/null 2>&1 &\necho $! > {self.tmp / 'pid'}\necho ok\n")
        self.assertEqual(subproc.Runner().run([exe]), 0)
        self.assertTrue(gone_soon(self.grandchild()))

    def test_lingering_child(self):
        exe = self.exe("linger", "#!/bin/sh\nexec >/dev/null 2>&1\nsleep 30\n")
        with self.assertRaises(HelperError) as cm:
            subproc.Runner().run([exe], exit_grace=0.3)
        self.assertEqual(cm.exception.code, "timeout")

    def test_signal_becomes_cancelled(self):
        def on_line(_line):
            os.kill(os.getpid(), 15)
        with self.assertRaises(subproc.Cancelled):
            subproc.Runner(on_line).run([self.exe("hang", HANGS), self.tmp / "pid"], signals=True)
        self.assertTrue(gone_soon(self.grandchild()))

    def test_missing_program(self):
        with self.assertRaises(FileNotFoundError):
            subproc.Runner().run([self.tmp / "missing"])


class SetupRunStream(Base):
    """setup.run_stream on the Runner: heartbeat progress while silent, tail, [n/m] progress, kill on interrupt."""

    def capture(self):
        from iosstudio.core import output
        events: list[dict] = []
        patcher = mock.patch.object(output, "_write", events.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        return events

    def test_heartbeat_tail_and_progress(self):
        from iosstudio.features import setup
        events = self.capture()
        exe = self.exe("slow", "#!/bin/sh\necho '[1/2] one'\nsleep 2.3\nprintf '\\n\\n[2/2] two'\n")
        tail: list[str] = []
        self.assertEqual(setup.run_stream([exe], "s", mutating=False, tail=tail), 0)
        self.assertEqual(tail, ["[1/2] one", "[2/2] two"])
        progress = [e for e in events if e.get("event") == "progress"]
        self.assertTrue(progress)           # the heartbeat fired during the silent 2.3 s

    def test_interrupt_kills_the_command(self):
        """run_stream keeps the command in our process group (pkexec keeps its session), so it is the command
        itself that is killed."""
        from iosstudio.features import setup
        self.capture()
        exe = self.exe("self", f"#!/bin/sh\necho $$ > {self.tmp / 'pid'}\necho started\nexec sleep 30\n")

        def parse(line, _st):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            setup.run_stream([exe], "s", mutating=False, parse=parse)
        self.assertTrue(gone_soon(self.grandchild()))

if __name__ == "__main__":
    unittest.main()
