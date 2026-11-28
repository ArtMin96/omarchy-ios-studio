"""How Setup acts: the dry-run switch, log/progress events, user and root (pkexec) commands, guarded file ops."""
from __future__ import annotations

import functools
import os
import re
import shutil
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

from iosstudio.core import appdirs, fileio, output, subproc
from iosstudio.core.appdirs import HOME, REPO
from iosstudio.core.output import HelperError, event, fix

from .pins import ROOT_SCRIPT

DRY = False   # --dry-run: every mutating command or file op is logged instead (set by @streaming)

# ------------------------------------------------------------------ output (output._write is thread-safe)

def log(step_id: str | None, text: str) -> None:
    event("log", text=text.rstrip("\n")[:4000], **({"step": step_id} if step_id else {}))


def progress(step_id: str | None, fraction: float | None, detail: str = "", eta: float | None = None) -> None:
    if fraction is not None:
        fraction = round(max(0.0, min(1.0, float(fraction))), 4)
    event("progress", fraction=fraction, detail=detail, eta=None if eta is None else int(eta),
          **({"step": step_id} if step_id else {}))


def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} s"
    m = round(sec / 60)
    return f"{m} min"


# ------------------------------------------------------------------ processes

def probe(argv: list, timeout: float = 8, env: dict | None = None, cwd: str | Path | None = None) -> tuple[int, str, str]:
    """Quick read-only run: (rc, stdout, stderr). Never raises (127 not found, 124 timeout, 126 other)."""
    r = subproc.run_cmd(argv, timeout=timeout, env=env or subproc.tool_env("setup"), cwd=cwd, check=False)
    return r.returncode, r.stdout, r.stderr


def _raise_nofile() -> None:
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard if hard > 0 else soft, hard))
    except Exception:
        pass


def _on_term(_signum, _frame) -> None:
    sys.exit(130)   # raised inside run_stream too: its Runner kills the running command on the way out


BUILD_FRAC = re.compile(r"^\[(\d+)\s*/\s*(\d+)\]")    # [n/m] and swift-build's [n / m] (thin spaces)
SUBTASK = re.compile(r"^\[\d+\s*/\s*\d+\]\s+(?:Fetching|Downloading)\s")   # SwiftPM's per-package counters
PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
EXIT_GRACE = 60          # seconds a command may linger after closing its output


class StreamProgress:
    """What run_stream shows for one command. Real numbers when the output has them: [n/m] build tasks, else NN %
    lines (SwiftPM's per-package fetch and download counters are not the build's and are only drawn, never logged).
    Before them, elapsed vs. estimate, up to `lead` (95 %). When real numbers start they fill what is left of the bar
    from where the estimate got to, so it never jumps back, and the ETA follows their rate.

    `st` is what a parse callback sees: it may set "detail", or "frac" (None goes back to the estimate)."""

    def __init__(self, label: str = "", estimate: float | None = None,
                 parse: Callable[[str, dict], str | None] | None = None, start: float | None = None,
                 lead: float = 0.95):
        self.label, self.estimate, self.parse, self.lead = label, estimate, parse, lead
        self.start = time.time() if start is None else start
        self.st: dict = {"frac": None, "detail": label, "count": None}
        self.shown = 0.0                 # bar position last shown (0..1), never lower
        self.base: float | None = None   # bar position when the current real numbers started …
        self.t0 = self.f0 = 0.0          # … and when, at which fraction
        self.last_logged = ""

    def line(self, line: str) -> bool:
        """Read one output line; True when it belongs in the log."""
        st = self.st
        quiet = bool(SUBTASK.match(line))
        m = None if quiet else BUILD_FRAC.match(line)
        if m and int(m.group(2)) > 0:
            n, total = int(m.group(1)), int(m.group(2))
            st["frac"], st["count"] = min(1.0, n / total), (n, total)
        if self.parse:
            quiet = self.parse(line, st) == "quiet" or quiet
        elif not quiet and (pm := PCT.search(line)):
            if line.split("%")[0][:-6] == self.last_logged.split("%")[0][:-6]:
                quiet = True  # a \r-redrawn percentage line: progress only
            if st["count"] is None:
                st["frac"] = min(1.0, float(pm.group(1)) / 100)
        if not quiet:
            self.last_logged = line
        return not quiet

    def reading(self, now: float) -> tuple[float | None, str, float | None]:
        """(fraction 0..1 or None, detail, eta seconds or None) for a heartbeat at `now`."""
        st, el = self.st, now - self.start
        what, frac = st["detail"] or self.label, st["frac"]
        if frac is None:
            self.base = None
            if not self.estimate:
                return None, f"{what} · {fmt_dur(el)}", None
            f, eta = min(self.lead, el / self.estimate), max(0.0, self.estimate - el)
            detail = f"{what} · {fmt_dur(el)} so far, usually about {fmt_dur(self.estimate)}"
        else:
            if self.base is None:
                self.base, self.t0, self.f0 = self.shown, now, frac
            f = self.base + (1 - self.base) * frac
            done = frac - self.f0
            eta = (now - self.t0) * (1 - frac) / done if done >= 0.02 else None
            count = st["count"]
            of = f" · {count[0]} of {count[1]}" if count and count[0] / count[1] == frac else ""
            detail = f"{what}{of} · {fmt_dur(el)}"
        self.shown = max(self.shown, f)
        return self.shown, detail, eta


def run_stream(argv: list, step_id: str | None, *, label: str = "", estimate: float | None = None,
               env: dict | None = None, cwd: str | Path | None = None,
               parse: Callable[[str, dict], str | None] | None = None, frac_range: tuple[float, float] = (0.0, 1.0),
               mutating: bool = True, tail: list | None = None, lead: float = 0.95) -> int:
    """Run a command, forwarding output as log events, with a heartbeat progress event every 2 s
    (StreamProgress: real numbers when the output has them, else elapsed vs. estimate up to `lead`)."""
    argv = [str(a) for a in argv]
    if DRY and mutating:
        log(step_id, "would run: " + " ".join(shell_quote(a) for a in argv) + (f"   (in {cwd})" if cwd else ""))
        return 0
    lo, hi = frac_range
    bar = StreamProgress(label, estimate, parse, lead=lead)
    last_beat = bar.start

    def beat():     # a heartbeat progress event every 2 s
        nonlocal last_beat
        now = time.time()
        if now - last_beat < 2.0:
            return
        last_beat = now
        frac, detail, eta = bar.reading(now)
        progress(step_id, None if frac is None else lo + (hi - lo) * frac, detail, eta)

    def on_line(line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        if tail is not None:
            tail.append(line)
            del tail[:-60]
        if bar.line(line):
            log(step_id, line)

    # Same process group as us (a pkexec/sudo child keeps working as before); killed on any exception.
    try:
        return subproc.Runner(on_line, on_tick=beat).run(argv, env=env or subproc.tool_env("setup"), cwd=cwd,
                                                         new_session=False, preexec_fn=_raise_nofile,
                                                         exit_grace=EXIT_GRACE)
    except FileNotFoundError:
        raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed yet.", fix("Open Setup", "setup"))


def shell_quote(a: str) -> str:
    return a if re.fullmatch(r"[\w@%+=:,./-]+", a) else "'" + a.replace("'", "'\\''") + "'"


def must(rc: int, code: str, message: str, step_id: str | None = None, detail_tail: list | None = None) -> None:
    if rc != 0:
        raise HelperError(code, message, fix("Try again", "setup", step=step_id) if step_id else None,
                          detail="\n".join(detail_tail or [])[-3000:] or None)


# ------------------------------------------------------------------ file ops (dry-run aware)

def do_mkdir(p: Path, step_id: str | None = None) -> None:
    if DRY:
        if not p.exists():
            log(step_id, f"would create {p}")
        return
    p.mkdir(parents=True, exist_ok=True)


def do_symlink(target: Path, link: Path, step_id: str | None = None) -> None:
    if DRY:
        log(step_id, f"would link {link} -> {target}")
        return
    with fileio.replacing(link) as tmp:
        os.symlink(target, tmp)


ALLOWED_DELETE_ROOTS = (
    HOME / ".cache/omarchy-apple-dev", HOME / ".local/lib/omarchy-apple-dev", HOME / ".cache/xtool",
    HOME / ".cache/ios-studio", HOME / ".local/share/ios-studio", HOME / ".local/share/omarchy-apple-dev",
    HOME / ".config/swiftpm/swift-sdks", HOME / ".swiftpm/swift-sdks",
)


def _inside(child: Path, parent: Path) -> bool:
    return str(child).startswith(str(parent).rstrip(os.sep) + os.sep)


def do_rmtree(p: Path, step_id: str | None = None, inside: tuple = (), exact: tuple = ()) -> int:
    """Delete p only if it is one of our own folders (ALLOWED_DELETE_ROOTS or inside one), strictly inside
    one of `inside`, or exactly one of `exact`. Symlinks are removed, never followed. Returns bytes freed."""
    p = Path(p)
    if not os.path.lexists(p):
        return 0
    real = Path(os.path.realpath(p.parent)) / p.name
    rp = lambda x: Path(os.path.realpath(x))  # noqa: E731
    allowed = (any(real == rp(r) or _inside(real, rp(r)) for r in ALLOWED_DELETE_ROOTS)
               or any(_inside(real, rp(r)) for r in inside)
               or any(real == rp(r) for r in exact))
    if not allowed or real in (rp(HOME), Path("/")):
        raise HelperError("unsafe-path", "iOS Studio refused to delete a path outside its own folders.", detail=str(p))
    size = 0 if p.is_symlink() else du(p)
    if DRY:
        log(step_id, f"would delete {appdirs.tilde(p)} ({fmt_bytes(size)})")
        return size
    log(step_id, f"deleting {appdirs.tilde(p)} ({fmt_bytes(size)})")
    if p.is_symlink() or p.is_file():
        p.unlink()
    else:
        shutil.rmtree(p, ignore_errors=True)
    return size


def do_unlink(p: Path, step_id: str | None = None) -> None:
    if not os.path.lexists(p):
        return
    if DRY:
        log(step_id, f"would delete {p}")
        return
    log(step_id, f"deleting {p}")
    p.unlink()


def du(p: Path) -> int:
    if not os.path.lexists(p):
        return 0
    if p.is_file() and not p.is_symlink():
        return p.stat().st_blocks * 512
    rc, out, _ = probe(["du", "-s", "-B1", "-x", str(p)], timeout=60)
    try:
        return int(out.split()[0])
    except (IndexError, ValueError):
        return fileio.dir_size(p)


def free_bytes(p: Path) -> int:
    while not p.exists():
        p = p.parent
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize


def need_space(path: Path, need: int, what: str) -> None:
    free = free_bytes(path)
    if free < need:
        raise HelperError("disk-full", f"Not enough free disk space for {what}: it needs about {fmt_bytes(need)}, "
                          f"and only {fmt_bytes(free)} is free. Free some space, then try again.",
                          fix("See disk usage", "toolchain"))


def confirm(message: str, yes_label: str, flags: dict) -> None:
    if flags.get("yes") or DRY:
        if DRY:
            log(None, f"would ask: {message}")
        return
    ans = output.prompt("confirm", message, [{"value": "yes", "label": yes_label}, {"value": "no", "label": "Cancel"}])
    if ans != "yes":
        raise HelperError("cancelled", "Cancelled. Nothing was changed.")


def _untrusted(p: Path, app: Path) -> str | None:
    """Why `p` (a real path) can't be trusted to run as root, or None. Only this user or root may own it, nobody
    else may write to it, and it must be inside the app folder."""
    if p != app and not _inside(p, app):
        return "is a link to a file outside the iOS Studio folder"
    try:
        st = os.stat(p)
    except OSError:
        return "is missing"
    if st.st_uid not in (os.getuid(), 0):
        return "belongs to another user"
    if st.st_mode & 0o022:
        return "can be changed by other users"
    return None


def trusted_root_script() -> Path:
    """The real path of root-tasks.sh, after checking it, the xcspec.py it runs (the one next to it) and the
    folders above them up to the app folder: a file there that another user could change would run as root."""
    app = Path(os.path.realpath(REPO))
    script = Path(os.path.realpath(ROOT_SCRIPT))
    xcspec = Path(os.path.realpath(script.parent / "xcspec.py"))
    checks = [(script, script.name), (xcspec, xcspec.name)]
    d = script.parent
    while d != app and _inside(d, app):
        checks.append((d, f"folder {d.name}"))
        d = d.parent
    checks.append((app, "folder"))
    for p, name in checks:
        why = _untrusted(p, app)
        if why:
            what = "iOS Studio's folder" if name == "folder" else f"iOS Studio's {name}"
            raise HelperError("untrusted-root-script",
                              f"iOS Studio didn't ask for your password: {what} {why}, so another program could "
                              "change what runs as administrator. Reinstall iOS Studio from a fresh download, then "
                              "try again.", fix("Open Setup", "setup"), detail=str(p))
    return script


def run_root(args: list[str], step_id: str, label: str) -> None:
    """One pkexec call to root-tasks.sh (its real path, after trusted_root_script()). Lines '@@task X' become
    progress detail."""
    if not args:
        return
    if Path("/var/lib/pacman/db.lck").exists() and any(a in args for a in ("--packages", "--deps", "--aur-build", "--remove-swift")):
        raise HelperError("pacman-locked", "Another program is installing system packages right now (maybe a system "
                          "update). Wait for it to finish, then try again.", fix("Try again", "setup", step=step_id))
    if DRY:
        argv = ["pkexec", str(ROOT_SCRIPT), *args]
        log(step_id, "would ask for your password once, then run: " + " ".join(shell_quote(a) for a in argv))
        rc = run_stream(["bash", str(ROOT_SCRIPT), *args, "--dry-run"], step_id, label=label, mutating=False)
        must(rc, "root-failed", "The root script rejected these arguments (dry run).", step_id)
        return
    if not shutil.which("pkexec"):
        raise HelperError("no-pkexec", "This computer has no way to ask for your password (polkit is missing).")
    argv = ["pkexec", str(trusted_root_script()), *args]
    log(step_id, "Asking for your password…")
    progress(step_id, None, "Waiting for your password")
    tail: list = []

    def parse(line, st):
        if line.startswith("@@task "):
            st["detail"] = {"packages": "Installing system packages", "deps": "Installing build tools",
                            "hold": "Holding Swift", "xcspec": "Fixing Swift build settings", "usbmuxd": "Configuring usbmuxd",
                            "udev": "Refreshing USB devices", "avahi": "Starting network discovery",
                            "firewall": "Opening the firewall for mirroring", "remove-swift": "Removing Swift",
                            "unhold": "Releasing the Swift hold", "avahi-off": "Stopping network discovery",
                            "firewall-off": "Closing the firewall for mirroring", "done": "Finishing"}.get(line[7:].split()[0], line[7:])
            if line[7:].startswith("aur"):
                st["detail"] = f"Building and installing {line[11:]}"
            return "quiet"
        return None
    rc = run_stream(argv, step_id, label=label, parse=parse, tail=tail)
    if rc in (126, 127) and not any("@@task" in t for t in tail):
        raise HelperError("auth-cancelled", "The password prompt was closed, so nothing was installed.",
                          fix("Try again", "setup", step=step_id))
    if rc == 3:
        raise HelperError("pacman-locked", "Another program is installing system packages right now. Wait for it to "
                          "finish, then try again.", fix("Try again", "setup", step=step_id))
    must(rc, "root-failed", f"{label} failed. The log has the details.", step_id, tail)


def streaming(fn: Callable[[dict], int]) -> Callable[[dict], int]:
    """Run a streaming handler: every HelperError becomes a done(false) event (never a bare fail object)."""
    @functools.wraps(fn)
    def wrapper(flags: dict) -> int:
        global DRY
        DRY = bool(flags.get("dry_run"))
        signal.signal(signal.SIGTERM, _on_term)
        try:
            return fn(flags)
        except HelperError as exc:
            return output.done(False, error=exc)
        except KeyboardInterrupt:
            return output.done(False, error=HelperError("cancelled", "Cancelled."))
        except Exception:  # noqa: BLE001
            import traceback
            return output.done(False, error=HelperError("internal", "Something unexpected went wrong.",
                                                         detail=traceback.format_exc()))
    return wrapper
