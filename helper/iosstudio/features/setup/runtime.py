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
from iosstudio.core.appdirs import HOME
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


BUILD_FRAC = re.compile(r"^\[(\d+)/(\d+)\]")
PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
EXIT_GRACE = 60          # seconds a command may linger after closing its output


def run_stream(argv: list, step_id: str | None, *, label: str = "", estimate: float | None = None,
               env: dict | None = None, cwd: str | Path | None = None,
               parse: Callable[[str, dict], str | None] | None = None, frac_range: tuple[float, float] = (0.0, 1.0),
               mutating: bool = True, tail: list | None = None) -> int:
    """Run a command, forwarding output as log events, with a heartbeat progress event every 2 s.

    Long silent builds (xtool, the macro server) still move the bar: elapsed / estimate, capped at
    95 %, until real numbers arrive ([n/m] build lines or NN% lines)."""
    argv = [str(a) for a in argv]
    if DRY and mutating:
        log(step_id, "would run: " + " ".join(shell_quote(a) for a in argv) + (f"   (in {cwd})" if cwd else ""))
        return 0
    lo, hi = frac_range
    start = time.time()
    st = {"frac": None, "detail": label, "t": start, "beat": start}
    last_logged = ""

    def beat():     # a heartbeat progress event every 2 s
        now = time.time()
        if now - st["beat"] < 2.0:
            return
        st["beat"] = now
        el = now - start
        frac = st["frac"]
        eta = None
        if frac is None and estimate:
            frac = min(0.95, el / estimate)
            eta = max(0, estimate - el)
            detail = f"{st['detail'] or label} · {fmt_dur(el)} so far, usually about {fmt_dur(estimate)}"
        else:
            if frac and frac > 0.02:
                eta = el / frac - el
            detail = f"{st['detail'] or label} · {fmt_dur(el)}"
        progress(step_id, None if frac is None else lo + (hi - lo) * frac, detail, eta)

    def on_line(line: str) -> None:
        nonlocal last_logged
        line = line.rstrip()
        if not line:
            return
        if tail is not None:
            tail.append(line)
            del tail[:-60]
        quiet = False
        m = BUILD_FRAC.match(line)
        if m and int(m.group(2)) > 0:
            st["frac"] = int(m.group(1)) / int(m.group(2))
        if parse:
            res = parse(line, st)
            quiet = res == "quiet"
        elif PCT.search(line) and line.split("%")[0][:-6] == last_logged.split("%")[0][:-6]:
            quiet = True  # a \r-redrawn percentage line: progress only
        if PCT.search(line) and st["frac"] is None and parse is None:
            try:
                st["frac"] = float(PCT.search(line).group(1)) / 100
            except ValueError:
                pass
        if not quiet:
            log(step_id, line)
            last_logged = line

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


def run_root(args: list[str], step_id: str, label: str) -> None:
    """One pkexec call to root-tasks.sh. Lines '@@task X' become progress detail."""
    if not args:
        return
    if Path("/var/lib/pacman/db.lck").exists() and any(a in args for a in ("--packages", "--deps", "--aur-build", "--remove-swift")):
        raise HelperError("pacman-locked", "Another program is installing system packages right now (maybe a system "
                          "update). Wait for it to finish, then try again.", fix("Try again", "setup", step=step_id))
    argv = ["pkexec", str(ROOT_SCRIPT), *args]
    if DRY:
        log(step_id, "would ask for your password once, then run: " + " ".join(shell_quote(a) for a in argv))
        rc = run_stream(["bash", str(ROOT_SCRIPT), *args, "--dry-run"], step_id, label=label, mutating=False)
        must(rc, "root-failed", "The root script rejected these arguments (dry run).", step_id)
        return
    if not shutil.which("pkexec"):
        raise HelperError("no-pkexec", "This computer has no way to ask for your password (polkit is missing).")
    log(step_id, "Asking for your password…")
    progress(step_id, None, "Waiting for your password")
    tail: list = []

    def parse(line, st):
        if line.startswith("@@task "):
            st["detail"] = {"packages": "Installing system packages", "deps": "Installing build tools",
                            "hold": "Holding Swift", "xcspec": "Fixing Swift build settings", "usbmuxd": "Configuring usbmuxd",
                            "udev": "Refreshing USB devices", "avahi": "Starting network discovery",
                            "firewall": "Opening the firewall for mirroring", "remove-swift": "Removing Swift",
                            "unhold": "Releasing the Swift hold", "done": "Finishing"}.get(line[7:].split()[0], line[7:])
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
