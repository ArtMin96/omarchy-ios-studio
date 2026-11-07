"""swift and git for the package commands: runs (captured or streamed), their progress lines and their errors."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from iosstudio.core import output, subproc
from iosstudio.core.output import HelperError
from iosstudio.features.packages import manifest as M

QUICK_TIMEOUT = 60
LS_REMOTE_TIMEOUT = 45
EXIT_GRACE = 60          # seconds swift may linger after closing its output
SWIFT_LINES = 5000       # output lines of one swift command kept for errors

HINT_FETCH = "Check the URL: it must be a git repository that can be cloned without a password."
HINT_RESOLVE = "No version matches the rule: pick another version or rule (or a branch)."
HINT_NETWORK = "Check the internet connection and try again."
HINT_MANIFEST = "Fix the lines listed in Package.swift, then try again."


# ================================================================ errors

def package_error(code: str, message: str, hint: str | None = None, **extra: Any) -> HelperError:
    """A HelperError with a hint and extra fields (diagnostics, products, …) for the JSON error."""
    err = HelperError(code, message)
    err.extra = {k: v for k, v in {"hint": hint, **extra}.items() if v is not None}   # type: ignore[attr-defined]
    return err


def _missing_swift() -> HelperError:
    return HelperError("missing-swift", "Swift isn't installed yet.", output.fix("Open Setup", "setup", step="swift"))


def _error_diagnostics(lines: list[str]) -> list[dict]:
    from iosstudio.features import diagnostics
    return [d for d in diagnostics.parse(lines, failed=True) if d.get("severity") == "error"][:10]


def manifest_error(lines: list[str], what: str = "Package.swift has errors.") -> HelperError:
    """dump-package failed: manifest-invalid with the first error's line in the message."""
    from iosstudio.features import diagnostics
    diags = _error_diagnostics(lines)
    first = next((d for d in diags if d.get("line")), None)
    msg = what
    if first:
        msg = f"{what.rstrip('.')}: line {first['line']}: {diagnostics.clip(first['message'], 200)}"
    err = HelperError("manifest-invalid", msg, detail="\n".join(lines[-40:]))
    err.extra = {"diagnostics": diags, "hint": HINT_MANIFEST}   # type: ignore[attr-defined]
    return err


def swift_failure(lines: list[str], what: str) -> HelperError:
    """A failed resolve/update/edit -> an error with package diagnostics and a hint in app wording."""
    from iosstudio.features import diagnostics
    diags = _error_diagnostics(lines)
    text = "\n".join(lines)
    first = diags[0]["message"] if diags else next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    code, hint = "package-failed", None
    if re.search(r"could not resolve host|failed to connect|timed out|network is unreachable|"
                 r"temporary failure in name resolution|connection (refused|reset)", text, re.I):
        code, hint = "network", HINT_NETWORK
    elif re.search(r"failed to clone|repository .* not found|couldn.t fetch|failed to fetch|"
                   r"could not read username|authentication failed|permission denied \(publickey", text, re.I):
        code, hint = "fetch-failed", HINT_FETCH
    elif re.search(r"could not be resolved|no versions of|depends on", text, re.I):
        code, hint = "resolve-failed", HINT_RESOLVE
    elif re.search(r"invalid manifest|manifest parse|error: .*Package\.swift", text, re.I):
        code, hint = "manifest-invalid", HINT_MANIFEST
    for d in diags:   # hints in app wording; the MCP server adds its own next steps
        d["hint"] = hint or d.get("hint")
    if not diags and first:
        diags = [{"severity": "error", "category": "package", "message": diagnostics.clip(first),
                  **({"hint": hint} if hint else {})}]
    msg = f"{what}: {diagnostics.clip(first, 240)}" if first else f"{what}."
    lines = [ln for ln in lines if not _GIT_COUNT.match(ln.strip())]
    return package_error(code, msg, hint, diagnostics=diags, detail_lines=[ln[:300] for ln in lines[-20:]])


# ================================================================ runs

def find_swift() -> str:
    """$IOSSTUDIO_SWIFT, else swift on PATH (or the toolchain's PATH), else /usr/lib/swift's."""
    env = os.environ.get("IOSSTUDIO_SWIFT")
    if env:
        return env
    found = shutil.which("swift") or subproc.which("swift")
    if found:
        return found
    if os.access("/usr/lib/swift/usr/bin/swift", os.X_OK):
        return "/usr/lib/swift/usr/bin/swift"
    raise _missing_swift()


def _communicate(proc: subprocess.Popen, timeout: float, grace: float) -> tuple[str, str] | None:
    """proc.communicate(); None on timeout. On timeout or any exception the child's whole group is killed."""
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        subproc.kill_group(proc.pid, grace=grace)
        subproc.kill_proc(proc)
        return None
    except BaseException:
        subproc.kill_group(proc.pid, grace=grace)
        subproc.kill_proc(proc)
        raise


def run_swift(args: list[str], cwd: Path | str, timeout: float = QUICK_TIMEOUT,
              stdout_only: bool = False) -> subprocess.CompletedProcess:
    """`swift <args>` captured (own process group; killed whole on timeout)."""
    argv = [find_swift(), *args]
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=subproc.tool_env("swift"), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, preexec_fn=subproc.pdeathsig_term)
    except FileNotFoundError:
        raise _missing_swift() from None
    res = _communicate(proc, timeout, grace=2.0)
    if res is None:
        raise HelperError("timeout", f"swift {args[-1] if args else ''} took longer than {int(timeout)} s.")
    out, err = res
    return subprocess.CompletedProcess(argv, proc.returncode, out, "" if stdout_only else err)


class SwiftRunner(subproc.Runner):
    """Runs swift commands for a streaming command: forwards each output line, and stops (killing swift's whole
    process group) on {"cancel": true} on stdin or, inside its with-block, SIGTERM/SIGINT/SIGHUP."""

    def __init__(self) -> None:
        super().__init__(watch_stdin=True, tail_lines=SWIFT_LINES)

    def swift(self, args: list[str], cwd: Path, on_line: Callable[[str], None]) -> tuple[int, list[str]]:
        """`swift <args>` streamed to on_line; (exit code, its last SWIFT_LINES lines)."""
        self.on_line = on_line
        self.tail.clear()
        try:
            rc = self.run([find_swift(), *args], cwd=cwd, env=subproc.tool_env("swift"),
                          preexec_fn=subproc.pdeathsig_term, exit_grace=EXIT_GRACE, kill_grace=2.0)
        except FileNotFoundError:
            raise _missing_swift() from None
        return rc, list(self.tail)


# ================================================================ git

def latest_version(url: str) -> str | None:
    """Newest release tag (no pre-releases) of a git repository, or None."""
    git = shutil.which("git")
    if not git:
        return None
    # Own session, killed whole on timeout (git-remote-https / ssh children too); never prompts for a password.
    try:
        proc = subprocess.Popen([git, "ls-remote", "--tags", "--refs", url], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env={**subproc.tool_env("swift"), "GIT_TERMINAL_PROMPT": "0"},
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    res = _communicate(proc, LS_REMOTE_TIMEOUT, grace=0.5)
    if res is None:
        return None
    out, err = res
    if proc.returncode != 0:
        raise swift_failure((err or "").splitlines() or ["error: Failed to fetch " + url],
                            "Couldn't read the package's versions")
    return newest_tag(out.splitlines())


def newest_tag(lines: list[str]) -> str | None:
    """The highest x.y.z (or vx.y.z) tag in `git ls-remote --tags` output, without the v."""
    best: tuple[tuple[int, int, int], str] | None = None
    for ln in lines:
        ref = ln.split("\t")[-1].strip()
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/"):]
        m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", tag)
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if best is None or key > best[0]:
            best = (key, tag.lstrip("v"))
    return best[1] if best else None


# ================================================================ progress events

class Stream:
    """Step/progress events of one streaming command."""

    def __init__(self, steps: list[tuple[str, str]]) -> None:
        self.titles = dict(steps)
        self.current: str | None = None
        for sid, title in steps:
            output.step(sid, title, "pending")

    def start(self, sid: str, title: str | None = None, detail: str | None = None,
              fraction: float | None = None) -> None:
        if title:
            self.titles[sid] = title
        self.current = sid
        extra = {"detail": detail} if detail else {}
        output.step(sid, self.titles.get(sid, sid), "running", **extra)
        if fraction is not None:
            output.progress(fraction, detail or self.titles.get(sid, sid))

    def detail(self, detail: str, fraction: float | None = None) -> None:
        if self.current:
            output.step(self.current, self.titles.get(self.current, self.current), "running", detail=detail)
        output.progress(fraction, detail)

    def finish(self, sid: str, state: str = "done", detail: str | None = None) -> None:
        extra = {"detail": detail} if detail else {}
        output.step(sid, self.titles.get(sid, sid), state, **extra)
        if self.current == sid:
            self.current = None

    def close(self, state: str) -> None:
        """Mark the running step, if any, as failed/cancelled."""
        if self.current:
            self.finish(self.current, state)


_PROGRESS = [
    (re.compile(r"^Fetching (\S+?)(?: from cache)?$"), "Fetching {n}", 0),
    (re.compile(r"^Fetched (\S+)"), "Fetched {n}", 1),
    (re.compile(r"^Cloning (?!into\b)(\S+)"), "Cloning {n}", 0),
    (re.compile(r"^Updating (\S+)$"), "Updating {n}", 0),
    (re.compile(r"^Updated (\S+)"), "Updated {n}", 1),
    (re.compile(r"^Computing version for (\S+)"), "Computing the version of {n}", 0),
    (re.compile(r"^Computed (\S+) at (\S+)"), "{n} {v}", 1),
    (re.compile(r"^Creating working copy for (\S+)"), "Checking out {n}", 0),
    (re.compile(r"^Working copy of (\S+) resolved at (.+)$"), "{n} {v}", 1),
]
_WAITING = re.compile(r"Another instance of SwiftPM is already running", re.I)
_GIT_COUNT = re.compile(r"^\[(\d+)/(\d+)\] (\w+) (\S+)")      # "[120/29848] Fetching swift-collections"


class LineSink:
    """Forwards swift's lines as log events and progress, but sends git's object counters ("[120/29848]
    Fetching x", thousands of them) at most every 0.5 s and never as log lines."""

    def __init__(self, on_progress: Callable[[tuple[str, str | None, bool]], None]) -> None:
        self.on_progress = on_progress
        self.last_count = 0.0

    def __call__(self, line: str) -> None:
        counter = _GIT_COUNT.match(line.strip())
        if counter:
            now = time.monotonic()
            if now - self.last_count < 0.5:
                return
            self.last_count = now
        else:
            output.log(line)
        pd = progress_detail(line)
        if pd:
            self.on_progress(pd)


def progress_detail(line: str) -> tuple[str, str | None, bool] | None:
    """A swift output line -> (detail, package name, finished one) or None."""
    s = line.strip()
    m = _GIT_COUNT.match(s)
    if m:
        pct = int(m.group(1)) * 100 // max(1, int(m.group(2)))
        return f"{m.group(3)} {m.group(4)} {pct}%", None, False
    if _WAITING.search(s):
        return "Waiting for another Swift command on this project (a build?) to finish", None, False
    for rx, fmt, doneflag in _PROGRESS:
        m = rx.match(s)
        if m:
            name = M.identity_of(m.group(1)) or m.group(1)
            v = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
            return fmt.format(n=name, v=v).strip(), name, bool(doneflag)
    return None
