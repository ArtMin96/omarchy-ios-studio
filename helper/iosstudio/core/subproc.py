"""Child processes for the helper (stdlib only): run with a timeout, stream lines, kill whole process groups."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path

from iosstudio.core import appdirs, output
from iosstudio.core.output import HelperError, fix


def demo_delay(seconds: float) -> float:
    """A demo-mode pause scaled by IOSSTUDIO_DEMO_DELAY (a multiplier, default 1; 0 makes the demo instant, which
    the tests use). Only for pretend work: never for timeouts, polling or rates."""
    try:
        factor = float(os.environ.get("IOSSTUDIO_DEMO_DELAY", "1"))
    except ValueError:
        factor = 1.0
    return max(0.0, seconds * factor)


def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    cand = appdirs.LOCAL_BIN / name
    return str(cand) if cand.exists() and os.access(cand, os.X_OK) else None


def run_cmd(argv: list[str], timeout: float = 60, input_text: str | None = None,
            env: dict | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run and capture. Never raises on non-zero; raises HelperError on missing binary/timeout."""
    full_env = os.environ.copy()
    full_env["PATH"] = f"{appdirs.LOCAL_BIN}:{full_env.get('PATH', '/usr/bin')}"
    full_env.setdefault("NO_COLOR", "1")
    if env:
        full_env.update(env)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              input=input_text, env=full_env, cwd=cwd)
    except FileNotFoundError:
        raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed yet.",
                          fix("Open Setup", "setup"))
    except subprocess.TimeoutExpired:
        raise HelperError("timeout", f"{Path(argv[0]).name} took too long to respond.")


def kill_proc(proc: subprocess.Popen, reap: float = 5.0) -> None:
    """SIGKILL a child — its whole process group when it leads one (started with start_new_session) — and reap it
    (bounded: a process stuck in the kernel is left to init rather than hanging us)."""
    if proc.poll() is not None:
        return
    try:
        if os.getpgid(proc.pid) == proc.pid:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=reap)
    except subprocess.TimeoutExpired:
        pass


def wait_or_kill(proc: subprocess.Popen, timeout: float) -> int:
    """proc.wait() with a ceiling: past `timeout` the child (and its group) is killed and TimeoutExpired raised."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_proc(proc)
        raise


def run_group(argv: list[str], timeout: float, *, input: str | bytes | None = None, text: bool = True,
              cwd: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True) in its own process group: on timeout (or any exception) the whole group
    is killed — a driver like `swift format` leaves no tool running behind it — and TimeoutExpired re-raised."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, cwd=cwd, env=env,
                            start_new_session=True)
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except BaseException:
        kill_proc(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def stream_cmd(argv: list[str], env: dict | None = None, cwd: str | None = None,
               on_line=None, timeout: float | None = None) -> int:
    """Run, forwarding each output line as a log event (or to on_line). Returns exit code.

    The child gets its own process group, killed whole on timeout, on an exception (cancel/SIGINT) and if it closes
    its output but doesn't exit. Raises HelperError('timeout') past `timeout` seconds."""
    full_env = os.environ.copy()
    full_env["PATH"] = f"{appdirs.LOCAL_BIN}:{full_env.get('PATH', '/usr/bin')}"
    full_env.setdefault("NO_COLOR", "1")
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                bufsize=1, env=full_env, cwd=cwd, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    except FileNotFoundError:
        raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed yet.",
                          fix("Open Setup", "setup"))
    assert proc.stdout is not None
    timed_out = threading.Event()
    timer = None
    if timeout is not None:
        def expire() -> None:
            timed_out.set()
            kill_proc(proc, reap=0)
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
    try:
        for line in proc.stdout:
            if on_line:
                on_line(line.rstrip("\n"))
            else:
                output.log(line)
        rc = wait_or_kill(proc, 30)
    except subprocess.TimeoutExpired:
        rc = -9
    except BaseException:
        kill_proc(proc)
        raise
    finally:
        if timer:
            timer.cancel()
        proc.stdout.close()
    if timed_out.is_set():
        raise HelperError("timeout", f"{Path(argv[0]).name} took longer than {int(timeout or 0)} s and was stopped.")
    return rc


def pymd3_python() -> str | None:
    """Python interpreter that has pymobiledevice3, in preference order: ours, then
    ~/pymobile3-venv (the venv path omarchy-apple-dev's scripts create and expect)."""
    for cand in (appdirs.VENV_DIR / "bin/python", appdirs.HOME / "pymobile3-venv/bin/python"):
        if cand.exists():
            return str(cand)
    return None
