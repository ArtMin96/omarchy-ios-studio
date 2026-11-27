"""A local process under the toolchain's lldb-server, for the tests that drive the real lldb (tests/cli: `debug session`,
`debug editor` + lldb-dap) instead of a phone.

- TARGET_C: a C program that prints its pid, then calls tick(i) every 50 ms until it is killed (capped at 10 min, so a
  run that loses track of it can't leave it behind for long). Its BREAK line is where tests put a breakpoint.
- build(): compiles it with cc -g -O0.
- start_target(): runs it and reads its pid, with a timeout.
- start_gdbserver(): `lldb-server gdbserver 127.0.0.1:0`: lldb-server picks a free port itself and writes it to a pipe,
  so the test knows the port is listening before anything connects (no bind-close-reuse race with other runs), and
  its output goes to a log file a failing test can show.
Every process is killed (and reaped) by the test's cleanup.
"""
from __future__ import annotations

import os
import select
import subprocess
import time
from pathlib import Path

TARGET_C = r"""
#include <stdio.h>
#include <unistd.h>
#include <sys/prctl.h>
static volatile int counter = 0;
int tick(int n) {
    counter += n;          /* BREAK */
    return counter;
}
int main(void) {
    prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0);   /* let lldb-server attach (Yama ptrace_scope 1) */
    printf("%d\n", getpid());
    fflush(stdout);
    for (int i = 0; i < 12000; i++) { tick(i); usleep(50000); }   /* until killed (at most 10 min) */
    return 0;
}
"""
BREAK_LINE = next(i for i, x in enumerate(TARGET_C.splitlines(), 1) if "BREAK" in x)
START_TIMEOUT = 30.0


def build(cc: str, src: Path, exe: Path) -> Path:
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(TARGET_C)
    exe.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([cc, "-g", "-O0", "-o", str(exe), str(src)], check=True, timeout=60, capture_output=True)
    return exe


def _stop(p: subprocess.Popen) -> None:
    if p.poll() is None:
        p.kill()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        pass
    for f in (p.stdin, p.stdout, p.stderr):
        if f:
            f.close()


def _read_line(fd: int, timeout: float, what: str) -> str:
    deadline, buf = time.monotonic() + timeout, b""
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not r:
            break
        chunk = os.read(fd, 64)
        if not chunk:
            break
        buf += chunk
        if b"\n" in buf or b"\0" in buf:
            break
    text = buf.replace(b"\0", b"\n").decode(errors="replace").strip()
    if not text.split("\n")[0].strip().isdigit():
        raise AssertionError(f"{what} didn't start within {timeout:g} s (got {text!r})")
    return text.split("\n")[0].strip()


def start_target(test, exe: Path) -> tuple[subprocess.Popen, int]:
    """The built target running; (process, pid). Killed at the test's cleanup."""
    p = subprocess.Popen([str(exe)], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    test.addCleanup(_stop, p)
    return p, int(_read_line(p.stdout.fileno(), START_TIMEOUT, "the target"))


def start_gdbserver(test, server: str, log: Path) -> tuple[subprocess.Popen, int]:
    """lldb-server listening on a port of its own choosing; (process, port). Killed at the test's cleanup."""
    r, w = os.pipe()
    try:
        with open(log, "wb") as out:
            p = subprocess.Popen([server, "gdbserver", "127.0.0.1:0", "--pipe", str(w)], pass_fds=(w,),
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        test.addCleanup(_stop, p)
        os.close(w)
        w = -1
        port = int(_read_line(r, START_TIMEOUT, "lldb-server"))
    finally:
        os.close(r)
        if w >= 0:
            os.close(w)
    return p, port
