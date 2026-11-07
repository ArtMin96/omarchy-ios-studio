"""Child processes for the helper (stdlib only): one environment recipe per kind of tool (tool_env), one-shot runs
with a timeout (run_cmd, run_group), streaming runs (Runner, stream_cmd) and killing whole process groups."""
from __future__ import annotations

import ctypes
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable

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


# ---------------------------------------------------------------- environment

def tool_env(kind: str = "plain", extra: dict | None = None) -> dict:
    """os.environ prepared for a tool, plus `extra`. The kinds differ only where the tools need it:

    plain  ~/.local/bin (xtool, rcodesign, ipsw) first on PATH; NO_COLOR unless set. run_cmd/Runner default.
    swift  plain + TERM=dumb, NO_COLOR=1, and git never asks for a password (a private repository fails
           instead of waiting): SwiftPM commands (packages).
    xtool  builds: ~/.local/bin, the real swift's directory, the system dirs, then the rest of PATH without mise
           shims (a shim swift breaks SDK builds); XDG_CONFIG_HOME defaulted (SwiftPM keeps SDKs under it, Omarchy
           sets it); TERM=dumb, NO_COLOR=1.
    setup  installs: /usr/bin first (swift-bin's swift, never a mise/asdf shim), then ~/.local/bin;
           XDG_CONFIG_HOME defaulted; NO_COLOR=1; LANG defaulted to C.UTF-8.
    """
    env = os.environ.copy()
    local = str(appdirs.LOCAL_BIN)
    if kind == "plain":
        env["PATH"] = f"{local}:{env.get('PATH', '/usr/bin')}"
        env.setdefault("NO_COLOR", "1")
    elif kind == "swift":
        env["PATH"] = f"{local}:{env.get('PATH', '/usr/bin:/bin')}"
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    elif kind == "xtool":
        env.setdefault("XDG_CONFIG_HOME", str(appdirs.HOME / ".config"))
        paths = [local]
        swift = shutil.which("swift")
        if swift:
            paths.append(str(Path(os.path.realpath(swift)).parent))
        paths += ["/usr/local/bin", "/usr/bin", "/bin"]
        rest = [p for p in env.get("PATH", "").split(":") if p and p not in paths and "mise" not in p]
        env["PATH"] = ":".join(paths + rest)
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
    elif kind == "setup":
        env["PATH"] = f"/usr/bin:{local}:{env.get('PATH', '/usr/bin')}"
        env.setdefault("XDG_CONFIG_HOME", str(appdirs.HOME / ".config"))
        env["NO_COLOR"] = "1"
        env.setdefault("LANG", "C.UTF-8")
    else:
        raise ValueError(f"unknown tool_env kind {kind!r}")
    if extra:
        env.update(extra)
    return env


def pdeathsig_term() -> None:  # pragma: no cover - runs in the child
    """preexec_fn: the child gets SIGTERM when we die (PR_SET_PDEATHSIG)."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        pass


# ---------------------------------------------------------------- one-shot runs

def run_cmd(argv: list, timeout: float = 60, input_text: str | None = None,
            env: dict | None = None, cwd: str | Path | None = None, *, check: bool = True) -> subprocess.CompletedProcess:
    """Run and capture (stdin is /dev/null unless input_text). Never raises on non-zero.

    check=True: raises HelperError on a missing binary or timeout. check=False never raises: returncode 127
    (not found, stderr "not found"), 124 (timeout, stderr "timeout") or 126 (other OSError)."""
    argv = [str(a) for a in argv]
    full_env = tool_env("plain", env)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input_text,
                              stdin=subprocess.DEVNULL if input_text is None else None, env=full_env, cwd=cwd)
    except FileNotFoundError:
        if not check:
            return subprocess.CompletedProcess(argv, 127, "", "not found")
        raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed yet.",
                          fix("Open Setup", "setup"))
    except subprocess.TimeoutExpired:
        if not check:
            return subprocess.CompletedProcess(argv, 124, "", "timeout")
        raise HelperError("timeout", f"{Path(argv[0]).name} took too long to respond.")
    except OSError as exc:
        if not check:
            return subprocess.CompletedProcess(argv, 126, "", str(exc))
        raise


def kill_group(pgid: int, grace: float = 2.0) -> None:
    """SIGTERM process group `pgid`, then SIGKILL whatever is left after `grace` s (grace 0: SIGKILL at once)."""
    if grace > 0:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        end = time.monotonic() + grace
        while time.monotonic() < end:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def start_reaper(pgid: int) -> tuple[int, int]:
    """Fork a watchdog that kills process group `pgid` once our end of a pipe closes.

    The pipe closes when this helper exits for any reason (SIGKILL included), so a build's whole
    tree (xtool -> swift-build -> swift-frontend...) never outlives us. Returns (reaper pid, write fd);
    stop_reaper() ends it."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the reaper
        try:
            os.close(w)
            for fd in (0, 1):
                try:
                    os.dup2(os.open(os.devnull, os.O_RDWR), fd)
                except OSError:
                    pass
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            while True:
                try:
                    if not os.read(r, 1):
                        break
                except InterruptedError:
                    continue
                except OSError:
                    break
            kill_group(pgid)
        finally:
            os._exit(0)
    os.close(r)
    os.set_inheritable(w, False)
    return pid, w


def stop_reaper(reaper: tuple[int, int]) -> None:
    pid, fd = reaper
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    os.close(fd)


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

    A Runner: own process group, killed whole on timeout, on an exception (cancel/SIGINT) and if it closes its
    output but doesn't exit. Raises HelperError('timeout') past `timeout` seconds."""
    def forward(line: str) -> None:
        if on_line:
            on_line(line)
        else:
            output.log(line)
    try:
        return Runner(on_line=forward).run(argv, env=tool_env("plain", env), cwd=cwd, timeout=timeout,
                                           exit_grace=30)
    except FileNotFoundError:
        raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed yet.",
                          fix("Open Setup", "setup"))


# ---------------------------------------------------------------- streaming runs

MAX_LINE = 64 * 1024     # longest output line passed on whole; a newline-less flood comes in pieces this size
EXIT_GRACE = 60.0        # seconds a child may linger after closing its output
_EOL = re.compile(rb"\r\n|\r|\n")


class Cancelled(Exception):
    """{"cancel": true} on our stdin, or SIGTERM/SIGINT/SIGHUP while a Runner listens for them."""


class Runner:
    """Runs a child with stdout+stderr merged and streams its output line by line (\\r, \\n or \\r\\n end a line).

    Hooks (all optional):
      on_line(text)            each line; a line longer than MAX_LINE comes in MAX_LINE pieces.
      on_partial(text) -> bool the unfinished last line after each read (a prompt such as "Password:");
                               True consumes it. May call next_message() to wait for the user's answer.
      on_tick()                after every read and at least every 0.5 s (heartbeats, own deadlines); may raise.
      on_message(msg)          JSON objects read from our stdin other than {"cancel": true} (watch_stdin).
    watch_stdin: our stdin carries JSON lines from the UI while running; {"cancel": true} raises Cancelled.
    The last `tail_lines` lines are kept in self.tail. As a context manager, SIGTERM/SIGINT/SIGHUP raise
    Cancelled for the with-block (run(signals=True) does that for one run).
    On any exception (cancel, timeout, a hook raising) the child's process group is killed (SIGTERM, then
    SIGKILL after kill_grace s); after a normal exit anything left in the group is SIGKILLed."""

    def __init__(self, on_line: Callable[[str], None] | None = None, *,
                 on_partial: Callable[[str], bool] | None = None, on_tick: Callable[[], None] | None = None,
                 on_message: Callable[[dict], None] | None = None, watch_stdin: bool = False,
                 tail_lines: int = 200) -> None:
        self.on_line = on_line
        self.on_partial = on_partial
        self.on_tick = on_tick
        self.on_message = on_message
        self.stdin_open = watch_stdin
        self.tail: deque[str] = deque(maxlen=tail_lines)
        self.proc: subprocess.Popen | None = None
        self._in = b""
        self._old: dict = {}

    # ---- signals
    def __enter__(self) -> "Runner":
        def on_signal(_signum, _frame):
            raise Cancelled()
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self._old[s] = signal.signal(s, on_signal)
        return self

    def __exit__(self, *exc) -> None:
        for s, h in self._old.items():
            try:
                signal.signal(s, h)
            except (ValueError, TypeError):
                pass
        self._old = {}

    def quiet(self) -> None:
        """Ignore further stop signals until the with-block ends (while files are put back)."""
        for s in self._old:
            try:
                signal.signal(s, signal.SIG_IGN)
            except (ValueError, TypeError):
                pass

    # ---- our stdin (messages from the UI)
    def _stdin_fd(self) -> int | None:
        if not self.stdin_open:
            return None
        try:
            return sys.stdin.fileno()
        except (OSError, ValueError, AttributeError):
            self.stdin_open = False
            return None

    def _read_stdin(self, fd: int) -> None:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            chunk = b""
        if not chunk:
            self.stdin_open = False
            return
        self._in += chunk
        if b"\n" not in self._in and len(self._in) > MAX_LINE:    # no message is this long: drop it
            self._in = b""

    def _take_message(self) -> dict | None:
        """The next complete message in the buffer (Cancelled for a cancel), or None."""
        while b"\n" in self._in:
            ln, self._in = self._in.split(b"\n", 1)
            try:
                msg = json.loads(ln.decode() or "{}")
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("cancel"):
                raise Cancelled()
            return msg
        return None

    def _dispatch(self) -> None:
        while (msg := self._take_message()) is not None:
            if self.on_message:
                self.on_message(msg)

    def poll(self) -> None:
        """Handle pending stdin messages without blocking (Cancelled on a cancel)."""
        while (fd := self._stdin_fd()) is not None:
            try:
                ready, _, _ = select.select([fd], [], [], 0)
            except (OSError, ValueError):
                self.stdin_open = False
                return
            if not ready:
                return
            self._read_stdin(fd)
            self._dispatch()

    def next_message(self) -> dict | None:
        """Block for the next stdin message (Cancelled on a cancel); None once stdin is closed."""
        while True:
            msg = self._take_message()
            if msg is not None:
                return msg
            fd = self._stdin_fd()
            if fd is None:
                return None
            self._read_stdin(fd)

    # ---- the child
    def write(self, text: str) -> None:
        """Send text to the child's stdin (run(child_stdin=True)); a closed pipe is ignored."""
        proc = self.proc
        if proc and proc.stdin:
            try:
                proc.stdin.write(text.encode())
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def _emit(self, raw: bytes) -> None:
        text = raw.decode(errors="replace")
        self.tail.append(text)
        if self.on_line:
            self.on_line(text)

    def _feed(self, buf: bytes) -> bytes:
        parts = _EOL.split(buf)
        buf = parts.pop()
        for p in parts:
            self._emit(p)
        if buf and self.on_partial and self.on_partial(buf.decode(errors="replace")):
            return b""
        while len(buf) > MAX_LINE:     # a newline-less flood: hand it on in pieces, don't grow forever
            self._emit(buf[:MAX_LINE])
            buf = buf[MAX_LINE:]
        return buf

    def run(self, argv: list, *, cwd: str | Path | None = None, env: dict | None = None,
            child_stdin: bool = False, preexec_fn: Callable[[], None] | None = None, new_session: bool = True,
            reaper: bool = False, timeout: float | None = None, exit_grace: float = EXIT_GRACE,
            kill_grace: float = 0.0, signals: bool = False) -> int:
        """Run argv to the end and return its exit code. env defaults to tool_env(). Raises FileNotFoundError
        for a missing program, HelperError('timeout') past `timeout` s or when the child closes its output
        but doesn't exit within exit_grace s. new_session=False keeps the child in our process group (unless
        preexec_fn calls setsid). reaper: a watchdog kills the group even if we are SIGKILLed."""
        argv = [str(a) for a in argv]
        name = Path(argv[0]).name
        proc = subprocess.Popen(argv, cwd=None if cwd is None else str(cwd), env=env or tool_env(),
                                stdin=subprocess.PIPE if child_stdin else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                                start_new_session=new_session, preexec_fn=preexec_fn)
        self.proc = proc
        try:
            pgid = proc.pid if os.getpgid(proc.pid) == proc.pid else None
        except OSError:
            pgid = None
        watchdog = start_reaper(pgid) if reaper and pgid else None
        own_signals = signals and not self._old
        if own_signals:
            self.__enter__()
        deadline = None if timeout is None else time.monotonic() + timeout
        assert proc.stdout is not None
        out_fd = proc.stdout.fileno()
        buf = b""
        try:
            while True:
                in_fd = self._stdin_fd()
                wait = 0.5 if deadline is None else max(0.0, min(0.5, deadline - time.monotonic()))
                try:
                    ready, _, _ = select.select([out_fd] + ([in_fd] if in_fd is not None else []), [], [], wait)
                except InterruptedError:
                    ready = []
                if in_fd is not None and in_fd in ready:
                    self._read_stdin(in_fd)
                    self._dispatch()
                if out_fd in ready:
                    chunk = os.read(out_fd, 65536)
                    if not chunk:
                        break
                    buf = self._feed(buf + chunk)
                if self.on_tick:
                    self.on_tick()
                if deadline is not None and time.monotonic() >= deadline:
                    raise HelperError("timeout", f"{name} took longer than {int(timeout or 0)} s and was stopped.")
            if buf:
                self._emit(buf)
            if deadline is not None:
                exit_grace = max(0.0, min(exit_grace, deadline - time.monotonic()))
            try:    # output closed: it's exiting; one that lingers is stopped rather than waited on forever
                return proc.wait(timeout=exit_grace)
            except subprocess.TimeoutExpired:
                raise HelperError("timeout", f"{name} closed its output but didn't exit.")
        except BaseException:
            if pgid:
                kill_group(pgid, kill_grace)
            kill_proc(proc)
            raise
        finally:
            if own_signals:
                self.__exit__()
            if watchdog:
                stop_reaper(watchdog)
            if pgid:
                kill_group(pgid, 0)     # anything it left behind
            for f in (proc.stdout, proc.stdin):
                if f:
                    try:
                        f.close()
                    except OSError:
                        pass


def pymd3_python() -> str | None:
    """Python interpreter that has pymobiledevice3, in preference order: ours, then
    ~/pymobile3-venv (the venv path omarchy-apple-dev's scripts create and expect)."""
    for cand in (appdirs.VENV_DIR / "bin/python", appdirs.HOME / "pymobile3-venv/bin/python"):
        if cand.exists():
            return str(cand)
    return None
