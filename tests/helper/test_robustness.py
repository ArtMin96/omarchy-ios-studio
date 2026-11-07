"""Robustness of the helper's process and file handling: bounded subprocess waits that kill the whole process group
(fake slow executables), tail reads of growing logs, capped line buffers, the build-lock context manager and
thread-safe JSON output.
    python -m pytest tests/helper/test_robustness.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.core import fileio, output, subproc
from iosstudio.core.output import HelperError
from iosstudio.features.packages import swift as package_swift
from iosstudio.features.projects import build as projects_build
from iosstudio.features.projects import renew as projects_renew
from iosstudio.features.projects import runner as projects_runner
from iosstudio.features.projects import store as projects_store
from iosstudio.features.setup import runtime as setup_runtime
from iosstudio.features.setup import sources as setup_sources
from iosstudio.features.watch import fswatch, lifecycle
from support import write_exe

# A fake tool that starts a long-lived grandchild (its pid goes to $1), prints one line, then hangs.
HANGS = """#!/bin/sh
sleep 30 &
echo $! > "$1"
echo started
sleep 30
"""
# One that closes its output at once but keeps running.
LINGERS = """#!/bin/sh
exec >/dev/null 2>&1
sleep 30
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


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def exe(self, name: str, text: str) -> str:
        return str(write_exe(self.tmp / name, text))

    def grandchild(self) -> int:
        pidfile = self.tmp / "pid"
        end = time.monotonic() + 3
        while time.monotonic() < end and not (pidfile.exists() and pidfile.read_text().strip()):
            time.sleep(0.02)
        return int(pidfile.read_text())


class Captured:
    """Collect output._write output instead of printing it."""

    def __enter__(self):
        self.events: list[dict] = []
        self._old = output._write
        output._write = self.events.append
        return self.events

    def __exit__(self, *exc):
        output._write = self._old


class Timeouts(Tmp):
    def test_wait_or_kill_kills_the_group(self):
        p = subprocess.Popen([self.exe("hang", HANGS), str(self.tmp / "pid")], stdout=subprocess.DEVNULL,
                             start_new_session=True)
        gc = self.grandchild()
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            subproc.wait_or_kill(p, 0.3)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIsNotNone(p.returncode)
        self.assertTrue(gone_soon(gc))

    def test_stream_cmd_timeout(self):
        lines: list[str] = []
        t0 = time.monotonic()
        with self.assertRaises(HelperError) as cm:
            subproc.stream_cmd([self.exe("hang", HANGS), str(self.tmp / "pid")], on_line=lines.append, timeout=0.5)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(lines, ["started"])
        self.assertTrue(gone_soon(self.grandchild()))

    def test_stream_cmd_cancel_kills_the_child(self):
        def on_line(_line):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            subproc.stream_cmd([self.exe("hang", HANGS), str(self.tmp / "pid")], on_line=on_line)
        self.assertTrue(gone_soon(self.grandchild()))

    def test_stream_cmd_normal_exit(self):
        lines: list[str] = []
        rc = subproc.stream_cmd([self.exe("ok", "#!/bin/sh\necho a\necho b\nexit 3\n")], on_line=lines.append,
                               timeout=10)
        self.assertEqual((rc, lines), (3, ["a", "b"]))

    def test_run_group_timeout_kills_grandchild(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            subproc.run_group([self.exe("hang", HANGS), str(self.tmp / "pid")], 0.5)
        self.assertTrue(gone_soon(self.grandchild()))

    def test_run_group_captures(self):
        r = subproc.run_group([self.exe("cat", "#!/bin/sh\ncat; echo err >&2\n")], 5, input="hi\n")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "hi\n", "err\n"))

    def test_setup_run_stream_lingering_child(self):
        with Captured(), mock.patch.object(setup_runtime, "EXIT_GRACE", 0.3):
            t0 = time.monotonic()
            with self.assertRaises(HelperError) as cm:
                setup_runtime.run_stream([self.exe("linger", LINGERS)], None, mutating=False)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - t0, 5)

    def test_setup_git_timeout(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        write_exe(bindir / "git", "#!/bin/sh\nsleep 30\n")
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, env), mock.patch.object(setup_sources, "GIT_TIMEOUT", 0.3):
            with self.assertRaises(HelperError) as cm:
                setup_sources.git_patch_id(self.tmp, "a", "b")
        self.assertEqual(cm.exception.code, "timeout")

    def test_systemctl_timeout(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        write_exe(bindir / "systemctl", "#!/bin/sh\nsleep 30\n")
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, env), mock.patch.object(projects_renew, "SYSTEMCTL_TIMEOUT", 0.3):
            with self.assertRaises(HelperError) as cm:
                projects_renew.systemctl("daemon-reload")
            self.assertEqual(cm.exception.code, "systemd-timeout")
            st = projects_renew.timer_status()      # status stays best-effort
        self.assertNotIn("enabled", st)

    def test_ls_remote_timeout(self):
        git = self.exe("git", "#!/bin/sh\nsleep 30 &\necho $! > " + str(self.tmp / "pid") + "\nsleep 30\n")
        with mock.patch.object(package_swift.shutil, "which", return_value=git), \
                mock.patch.object(package_swift, "LS_REMOTE_TIMEOUT", 0.5):
            t0 = time.monotonic()
            self.assertIsNone(package_swift.latest_version("https://example.invalid/x.git"))
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(gone_soon(self.grandchild()))


class Buffers(Tmp):
    def test_setup_run_stream_splits_a_newline_less_flood(self):
        exe = self.exe("flood", "#!/bin/sh\nhead -c 300000 /dev/zero | tr '\\0' x\n")
        with Captured() as events:
            rc = setup_runtime.run_stream([exe], "s", mutating=False)
        self.assertEqual(rc, 0)
        logs = [e for e in events if e.get("event") == "log"]
        self.assertGreaterEqual(len(logs), 300000 // subproc.MAX_LINE)

    def test_build_runner_caps_its_line_buffer(self):
        exe = self.exe("flood", "#!/bin/sh\nhead -c 300000 /dev/zero | tr '\\0' x\n")
        got: list[int] = []
        with Captured(), open(os.devnull) as null, mock.patch.object(sys, "stdin", null):
            runner = projects_runner.BuildRunner(["compile"], False, self.tmp / "b.log", None)
            runner.on_line = lambda text: got.append(len(text))
            runner.on_partial = lambda text: False
            try:
                rc = runner.run([exe], self.tmp)
            finally:
                runner.close()
        self.assertEqual(rc, 0)
        self.assertEqual(sum(got), 300000)
        self.assertLessEqual(max(got), subproc.MAX_LINE)

    def test_build_runner_lingering_child(self):
        with Captured(), open(os.devnull) as null, mock.patch.object(sys, "stdin", null), \
                mock.patch.object(projects_runner, "EXIT_GRACE", 0.3):
            runner = projects_runner.BuildRunner(["compile"], False, self.tmp / "b.log", None)
            try:
                with self.assertRaises(HelperError) as cm:
                    runner.run([self.exe("linger", LINGERS)], self.tmp)
            finally:
                runner.close()
        self.assertEqual(cm.exception.code, "timeout")
        self.assertIsNotNone(runner.proc.returncode)


class Tails(Tmp):
    def test_tail_text_reads_only_the_end(self):
        log = self.tmp / "big.log"
        with open(log, "wb") as fh:
            fh.write(b"a" * 5_000_000 + b"THE END")
        real_open = open
        sizes: list[int] = []

        def spy_open(path, mode="r", *a, **k):
            fh = real_open(path, mode, *a, **k)
            if str(path) == str(log):
                orig = fh.read

                def read(n=-1):
                    data = orig(n)
                    sizes.append(len(data))
                    return data
                fh.read = read
            return fh
        with mock.patch("builtins.open", spy_open):
            self.assertEqual(fileio.tail_text(log, 7), "THE END")
        self.assertEqual(sizes, [7])
        self.assertEqual(fileio.tail_text(log, 10_000_000)[-7:], "THE END")
        self.assertEqual(fileio.tail_text(self.tmp / "missing.log", 100), "")

    def test_watch_log_is_trimmed(self):
        log = self.tmp / "watch.log"
        log.write_bytes(b"x" * (lifecycle.WATCH_LOG_MAX + 10) + b"\nlast line\n")
        lifecycle._trim_log(log, keep=1000)
        self.assertEqual(log.stat().st_size, 1000)
        self.assertTrue(log.read_text().endswith("last line\n"))
        small = self.tmp / "small.log"
        small.write_text("keep me\n")
        lifecycle._trim_log(small)
        self.assertEqual(small.read_text(), "keep me\n")


class Locks(Tmp):
    def test_build_lock_is_a_context_manager(self):
        with mock.patch.object(projects_store, "LOCK_DIR", self.tmp / "locks"):
            with projects_build.project_lock(self.tmp):
                with self.assertRaises(HelperError) as cm, projects_build.project_lock(self.tmp):
                    pass
                self.assertEqual(cm.exception.code, "busy")
                with projects_build.project_lock(self.tmp / "other"):     # another project isn't blocked
                    pass
            with projects_build.project_lock(self.tmp):     # released on exit
                pass
            with self.assertRaises(RuntimeError):
                with projects_build.project_lock(self.tmp):
                    raise RuntimeError
            with projects_build.project_lock(self.tmp):     # and on an exception
                pass


class Output(unittest.TestCase):
    def test_setup_does_not_patch_write(self):
        self.assertEqual(output._write.__module__, "iosstudio.core.output")
        self.assertFalse(hasattr(setup_runtime, "_locked_write"))

    def test_write_is_thread_safe(self):
        buf = io.StringIO()
        chunks: list[str] = []

        class Slow:
            """A stdout whose writes yield mid-way, so unlocked writers would interleave."""

            def write(self, s):
                half = len(s) // 2
                chunks.append(s[:half])
                time.sleep(0)
                chunks.append(s[half:])

            def flush(self):
                pass

        with mock.patch.object(sys, "stdout", Slow()):
            threads = [threading.Thread(target=lambda i=i: [output.event("log", text=f"{i}-{n}" * 20)
                                                             for n in range(50)]) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        buf.write("".join(chunks))
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 400)
        for ln in lines:
            self.assertEqual(json.loads(ln)["event"], "log")


class PollCap(Tmp):
    def test_poll_watcher_caps_files(self):
        src = self.tmp / "Sources" / "App"
        src.mkdir(parents=True)
        for i in range(30):
            (src / f"F{i}.swift").write_text("")
        with mock.patch.object(fswatch, "POLL_MAX_FILES", 10):
            w = fswatch.PollWatcher(self.tmp)
        self.assertLessEqual(len(w.snap), 10)


if __name__ == "__main__":
    unittest.main()
