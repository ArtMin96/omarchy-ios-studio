"""iOS Studio helper, groups `setup` and `toolchain` (the setup slice).

setup check                      one JSON object: every Setup step with its state (read-only, fast)
setup install --step ID          JSON lines: install or repair one step
setup install --all              JSON lines: every missing auto-installable step, in order
      [--dry-run] [--force] [--yes] [--include-optional]
setup xip --watch                JSON lines: watch ~/Downloads for the Xcode .xip and validate it
setup xip --use PATH             JSON lines: validate a .xip the user picked
setup xip --open                 one JSON object: open Apple's download page for the needed Xcode
toolchain info                   one JSON object: versions of every piece
toolchain disk [--cached]        one JSON object: disk usage per piece, which are safe to clean
toolchain clean --item ID        JSON lines: delete one allow-listed item  [--dry-run]
toolchain remove-all             JSON lines: remove the iOS toolchain  [--remove-auth] [--keep-swift] [--dry-run] [--yes]

Root work goes through ONE `pkexec helper/root/root-tasks.sh ...` per step (one password prompt).
yay/makepkg never run as root; AUR packages are built as the user (runuser inside the root script).
--dry-run prints every command that would run as log events and changes nothing.
"""
from __future__ import annotations

import hashlib
import mmap
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from iosstudio.core import appdirs, fileio, output, subproc
from iosstudio.core.appdirs import CACHE_DIR, HOME, LOCAL_BIN, REPO, STATE_DIR, VENV_DIR
from iosstudio.core.output import HelperError, event, fix, ok

# ------------------------------------------------------------------ pins (one place)

# omarchy-apple-dev: the commit proven on the author's machine (2026-10-31): xtool fork pin f300489.
APPLE_DEV_REPO = "https://github.com/joshuaswarren/omarchy-apple-dev"
APPLE_DEV_COMMIT = "79bda716bf207924efc25bce923bda261dc67837"
# Where a fresh machine gets it: the same path as Omarchy PR #14255 (Install > Development > iOS).
APPLE_DEV_HOME = HOME / ".local/share/omarchy-apple-dev"
APPLE_DEV_CANDIDATES = (APPLE_DEV_HOME,)

# Fallbacks when no checkout is present yet (the values at APPLE_DEV_COMMIT).
DEFAULT_XTOOL_REPO = "https://github.com/joshuaswarren/xtool"
DEFAULT_XTOOL_SHA = "f300489f3a3fddbd9fb598ba6cf69eb24a494527"
DEFAULT_SDK_STAMP = "epoch=4,darwinTools=1.1.0,oam=1.4.1"

# xtool-org/xtool PR #406 (GSA user agent). Without it `xtool auth` gets HTTP 503 (#404). A binary that
# still contains the old user agent string lacks the fix.
XTOOL_FIX_REPO = "https://github.com/xtool-org/xtool"
XTOOL_FIX_SHA = "70e1568fdfcb20b560774057cd783f02361293b9"
XTOOL_OLD_UA = b"akd/1.0 CFNetwork/808.1.4"
XTOOL_KNOWN_BASE = "f300489f3a3fddbd9fb598ba6cf69eb24a494527"
XTOOL_KNOWN_TREE = "bb93e00eaa35bca365d8625749b912648d665fae"

# swift-bin from the AUR, pinned to a commit (6.4.2-2), built as the user.
SWIFT_AUR_COMMIT = "f78c5ec6f5be1052fa6ffd3bf78ee44b3fb607a0"
AUR_SNAPSHOT = "https://aur.archlinux.org/cgit/aur.git/snapshot/aur-{ref}.tar.gz"
AUR_SNAPSHOT_HEAD = "https://aur.archlinux.org/cgit/aur.git/snapshot/{pkg}.tar.gz"

PYMD3_VERSION = "11.49.0"

RCODESIGN_VERSION = "0.29.0"
RCODESIGN_SHA = {
    "aarch64": "4af92c87ddf52f5f2d1258a3b4e56c7dcb8f1b2468df744976c5f139e031961f",
    "x86_64": "dbe85cedd8ee4217b64e9a0e4c2aef92ab8bcaaa41f20bde99781ff02e600002",
}
IPSW_VERSION = "3.1.731"
IPSW_SHA = {
    "aarch64": ("arm64", "a8d373966d79d24537aa17ec8b9a61bbc73964e6200383f7708c53400ad9f42a"),
    "x86_64": ("x86_64", "98e5b127641be3511242a5ba9c5872ef34010e5171318b616e5aa80a607749ff"),
}

PACKAGES = ["usbmuxd", "zip", "base-devel", "git", "libimobiledevice", "openssl", "poppler", "libheif",
            "python", "curl", "ffmpeg"]
# GStreamer pieces UxPlay's mirror needs at run time: the H.264/H.265 parsers (bad) and hardware decode (va)
UXPLAY_RUNTIME = ["gst-plugins-bad", "gst-plugin-va"]

# Swift major.minor -> (Xcode we recommend, Xcode major that works, approx .xip bytes)
XCODE_FOR_SWIFT = {"6.4": ("27.1", "27", 2_027_369_056), "6.3": ("26", "26", 3_000_000_000)}
XIP_MIN_BYTES = 1_000_000_000

SDK_CACHE = HOME / ".cache/xtool"
APPLE_DEV_CACHE = HOME / ".cache/omarchy-apple-dev"
APPLE_DEV_LIB = HOME / ".local/lib/omarchy-apple-dev"
LEGACY_VENV = HOME / "pymobile3-venv"   # omarchy-apple-dev's venv path (its scripts expect it)
AUR_DIR = CACHE_DIR / "aur"
SETUP_STATE = STATE_DIR / "setup.json"
OMARCHY_HOOK = HOME / ".config/omarchy/hooks/pre-refresh-pacman.d/ios-studio-hold-swift-bin"
DESKTOP_FILE = HOME / ".local/share/applications/ios-studio.desktop"
LAUNCHER = LOCAL_BIN / "ios-studio"
ROOT_SCRIPT = REPO / "helper/root/root-tasks.sh"
XCSPEC_PY = REPO / "helper/root/xcspec.py"
HOOK_SRC = REPO / "helper/root/hold-swift-bin-hook.sh"
DOWNLOADS = Path(os.environ.get("XDG_DOWNLOAD_DIR", HOME / "Downloads"))
XTOOL_CONFIG = HOME / ".config/xtool"
DOWNLOAD_PAGE = "https://developer.apple.com/download/all/?q=Xcode%20{v}"
DIRECT_XIP = "https://download.developer.apple.com/Developer_Tools/Xcode_{v}/Xcode_{v}.xip"

DRY = False

# ------------------------------------------------------------------ output (output._write is thread-safe)

def slog(step_id: str | None, text: str) -> None:
    event("log", text=text.rstrip("\n")[:4000], **({"step": step_id} if step_id else {}))


def sprog(step_id: str | None, fraction, detail: str = "", eta=None) -> None:
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

def q(argv: list, timeout: float = 8, env: dict | None = None, cwd=None) -> tuple[int, str, str]:
    """Quick read-only run: (rc, stdout, stderr). Never raises (127 not found, 124 timeout, 126 other)."""
    r = subproc.run_cmd(argv, timeout=timeout, env=env or subproc.tool_env("setup"), cwd=cwd, check=False)
    return r.returncode, r.stdout, r.stderr


def _raise_nofile():
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard if hard > 0 else soft, hard))
    except Exception:
        pass


def _on_term(signum, _frame):
    sys.exit(130)   # raised inside run_stream too: its Runner kills the running command on the way out


BUILD_FRAC = re.compile(r"^\[(\d+)/(\d+)\]")
PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")


MAX_LINE = subproc.MAX_LINE     # longest output line kept whole
EXIT_GRACE = 60          # seconds a command may linger after closing its output


def run_stream(argv: list, step_id: str | None, *, label: str = "", estimate: float | None = None,
               env: dict | None = None, cwd=None, parse=None, frac_range=(0.0, 1.0),
               mutating: bool = True, tail: list | None = None) -> int:
    """Run a command, forwarding output as log events, with a heartbeat progress event every 2 s.

    Long silent builds (xtool, the macro server) still move the bar: elapsed / estimate, capped at
    95 %, until real numbers arrive ([n/m] build lines or NN% lines)."""
    argv = [str(a) for a in argv]
    if DRY and mutating:
        slog(step_id, "would run: " + " ".join(_shq(a) for a in argv) + (f"   (in {cwd})" if cwd else ""))
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
        sprog(step_id, None if frac is None else lo + (hi - lo) * frac, detail, eta)

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
            slog(step_id, line)
            last_logged = line

    # Same process group as us (a pkexec/sudo child keeps working as before); killed on any exception.
    try:
        return subproc.Runner(on_line, on_tick=beat).run(argv, env=env or subproc.tool_env("setup"), cwd=cwd,
                                                         new_session=False, preexec_fn=_raise_nofile,
                                                         exit_grace=EXIT_GRACE)
    except FileNotFoundError:
        raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed yet.", fix("Open Setup", "setup"))


def _shq(a: str) -> str:
    return a if re.fullmatch(r"[\w@%+=:,./-]+", a) else "'" + a.replace("'", "'\\''") + "'"


def must(rc: int, code: str, message: str, step_id: str | None = None, detail_tail: list | None = None):
    if rc != 0:
        raise HelperError(code, message, fix("Try again", "setup", step=step_id) if step_id else None,
                          detail="\n".join(detail_tail or [])[-3000:] or None)


# ------------------------------------------------------------------ file ops (dry-run aware)

def do_mkdir(p: Path, step_id=None):
    if DRY:
        if not p.exists():
            slog(step_id, f"would create {p}")
        return
    p.mkdir(parents=True, exist_ok=True)


def do_symlink(target: Path, link: Path, step_id=None):
    if DRY:
        slog(step_id, f"would link {link} -> {target}")
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


def do_rmtree(p: Path, step_id=None, inside: tuple = (), exact: tuple = ()) -> int:
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
        slog(step_id, f"would delete {appdirs.tilde(p)} ({fmt_bytes(size)})")
        return size
    slog(step_id, f"deleting {appdirs.tilde(p)} ({fmt_bytes(size)})")
    if p.is_symlink() or p.is_file():
        p.unlink()
    else:
        shutil.rmtree(p, ignore_errors=True)
    return size


def do_unlink(p: Path, step_id=None):
    if not os.path.lexists(p):
        return
    if DRY:
        slog(step_id, f"would delete {p}")
        return
    slog(step_id, f"deleting {p}")
    p.unlink()


def du(p: Path) -> int:
    if not os.path.lexists(p):
        return 0
    if p.is_file() and not p.is_symlink():
        return p.stat().st_blocks * 512
    rc, out, _ = q(["du", "-s", "-B1", "-x", str(p)], timeout=60)
    try:
        return int(out.split()[0])
    except (IndexError, ValueError):
        return fileio.dir_size(p)


def free_bytes(p: Path) -> int:
    while not p.exists():
        p = p.parent
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize


def need_space(path: Path, need: int, what: str):
    free = free_bytes(path)
    if free < need:
        raise HelperError("disk-full", f"Not enough free disk space for {what}: it needs about {fmt_bytes(need)}, "
                          f"and only {fmt_bytes(free)} is free. Free some space, then try again.",
                          fix("See disk usage", "toolchain"))


# ------------------------------------------------------------------ downloads

def download(url: str, dest: Path, step_id: str | None, *, sha256: str | None = None, label: str = "",
             frac_range=(0.0, 1.0)) -> Path:
    if DRY:
        slog(step_id, f"would download {url} -> {dest}" + (f" (sha256 {sha256[:12]}…)" if sha256 else ""))
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and sha256 and _sha256(dest) == sha256:
        slog(step_id, f"already downloaded: {dest.name}")
        return dest
    part = dest.with_name(dest.name + ".part")
    lo, hi = frac_range
    slog(step_id, f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "iOS-Studio/1.0"})
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            got, last, start = 0, 0.0, time.time()
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                got += len(chunk)
                now = time.time()
                if now - last > 0.5:
                    last = now
                    rate = got / max(0.1, now - start)
                    frac = got / total if total else None
                    eta = (total - got) / rate if total and rate else None
                    sprog(step_id, None if frac is None else lo + (hi - lo) * frac,
                          f"{label or 'Downloading'} · {fmt_bytes(got)}" + (f" of {fmt_bytes(total)}" if total else "")
                          + f" · {fmt_bytes(rate)}/s", eta)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        try:
            part.unlink()
        except OSError:
            pass
        raise HelperError("network", f"Couldn't download {label or Path(url).name}. Check your internet "
                          "connection, then try again.", fix("Try again", "setup", step=step_id) if step_id else None,
                          detail=f"{url}: {exc}")
    if sha256 and h.hexdigest() != sha256:
        part.unlink()
        raise HelperError("checksum", f"The download of {label or Path(url).name} was damaged (checksum mismatch). "
                          "Try again.", fix("Try again", "setup", step=step_id) if step_id else None)
    os.replace(part, dest)
    return dest


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ small readers

def arch() -> str:
    return os.uname().machine


def swift_hold_hook_present() -> bool:
    """Ours, or any other pre-refresh-pacman hook that already re-holds swift-bin."""
    if OMARCHY_HOOK.exists():
        return True
    try:
        for f in OMARCHY_HOOK.parent.iterdir():
            if f.is_file() and not f.name.endswith(".sample") and os.access(f, os.X_OK):
                text = f.read_text(errors="replace")
                if "swift-bin" in text and "IgnorePkg" in text:
                    return True
    except OSError:
        pass
    return False


def is_omarchy() -> bool:
    return Path("/usr/share/omarchy/bin/omarchy-hook").exists() or shutil.which("omarchy-hook") is not None


def xtool_bin() -> Path | None:
    cand = LOCAL_BIN / "xtool"
    if cand.exists():
        return cand
    w = shutil.which("xtool")
    return Path(w) if w else None


def binary_has_old_ua(path: Path) -> bool | None:
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return mm.find(XTOOL_OLD_UA) != -1
    except (OSError, ValueError):
        return None


def find_apple_dev() -> Path | None:
    s = appdirs.settings().get("appleDevDir")
    cands = ([Path(os.path.expanduser(s))] if s else []) + list(APPLE_DEV_CANDIDATES)
    for c in cands:
        if (c / "install-toolchain.sh").is_file() and (c / "tools/sdk-state.sh").is_file():
            return c
    return None


def apple_dev_pins(d: Path | None) -> dict:
    pins = {"XTOOL_REPO": DEFAULT_XTOOL_REPO, "XTOOL_SHA": DEFAULT_XTOOL_SHA, "XTOOL_SDK_VERSION": DEFAULT_SDK_STAMP}
    if d:
        try:
            text = (d / "install-toolchain.sh").read_text(errors="replace")
            for k in list(pins):
                m = re.search(rf'^{k}="?([^"\n]*)"?\s*$', text, re.M)
                if m and m.group(1):
                    pins[k] = m.group(1)
        except OSError:
            pass
    return pins


def toolchain_root() -> Path | None:
    sw = Path("/usr/bin/swift")
    if not sw.exists():
        return None
    real = os.path.realpath(sw)
    m = re.match(r"^(.*)/usr/bin/[^/]+$", real)
    return Path(m.group(1)) if m else None


def xcspec_check(root: Path | None) -> bool | None:
    if root is None:
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ios_studio_xcspec", XCSPEC_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        for rel, setting, value in mod.PATCHES:
            path = root / "usr/share/pm" / rel
            try:
                text = path.read_text()
            except OSError:
                return False
            if mod.patched(text, setting, value) != text:
                return False
        return True
    except Exception:
        return None


def sdk_stamp(bundle: Path) -> str:
    try:
        return (bundle / "darwin-sdk-version.txt").read_text().strip()
    except OSError:
        return ""


def sdk_ios_versions(bundle: Path) -> list[str]:
    base = bundle / "Developer/Platforms/iPhoneOS.platform/Developer/SDKs"
    out = []
    for p in sorted(base.glob("iPhoneOS[0-9]*.sdk")):
        out.append(p.name[len("iPhoneOS"):-len(".sdk")])
    return out


def sdk_extras_missing(bundle: Path) -> list[str]:
    b = bundle / "Developer/Platforms/iPhoneOS.platform/Developer/usr/bin"
    miss = [t for t in ("actool", "xcstringstool", "ibtool", "momc") if not os.access(b / t, os.X_OK)]
    if not os.access(bundle / "OpenAppleMacrosServer", os.X_OK):
        miss.append("macro server")
    return miss


def sdk_caches(stamp: str) -> list[dict]:
    out = []
    for p in sorted(SDK_CACHE.glob("darwin-*.xtoolsdk"), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_dir():
            continue
        ver = p.name[len("darwin-iPhoneOS"):-len(".xtoolsdk")] if p.name.startswith("darwin-iPhoneOS") else ""
        st = sdk_stamp(p)
        out.append({"path": str(p), "ios": ver, "stamp": st, "current": st == stamp,
                    "complete": bool(sdk_ios_versions(p))})
    return out


def load_state() -> dict:
    return fileio.load_json(SETUP_STATE, {}) or {}


def save_state(upd: dict):
    if DRY:
        return
    st = load_state()
    st.update(upd)
    fileio.save_json(SETUP_STATE, st)


def parse_swift_version(text: str) -> str | None:
    m = re.search(r"Swift version (\d+\.\d+(?:\.\d+)?)", text)
    return m.group(1) if m else None


def xcode_for(swift_ver: str | None) -> tuple[str, str, int] | None:
    if not swift_ver:
        return None
    mm = ".".join(swift_ver.split(".")[:2])
    return XCODE_FOR_SWIFT.get(mm)


def needed_xcode(swift_ver: str | None) -> tuple[str, str, int]:
    """(recommended version, required major, approx bytes). Defaults to the pinned swift-bin's."""
    override = appdirs.settings().get("xcodeVersion")
    rec = xcode_for(swift_ver) or XCODE_FOR_SWIFT["6.4"]
    if override:
        return (str(override), str(override).split(".")[0], rec[2])
    return rec


# ------------------------------------------------------------------ Xcode .xip

def xip_quick(path: Path) -> dict:
    """Cheap checks only (magic + size). Version is read separately (about a minute)."""
    info: dict = {"path": str(path), "name": path.name}
    try:
        stt = path.stat()
        info["size"] = stt.st_size
        info["mtime"] = stt.st_mtime
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError as exc:
        info.update(valid=False, problem="unreadable", message=f"Can't read that file ({exc.strerror}).")
        return info
    if magic != b"xar!" or info["size"] < XIP_MIN_BYTES:
        small = info["size"] < 5_000_000
        info.update(valid=False, problem="stub" if small else "not-xip",
                    message=("This file is only " + fmt_bytes(info["size"]) + ": it's Apple's sign-in page, not Xcode. "
                             "Sign in on the download page and download it again.") if small else
                    "This file isn't an Xcode .xip archive (or the download is incomplete). Download it again.")
        return info
    info["valid"] = True
    cached = (load_state().get("xipInfo") or {})
    if cached.get("path") == str(path) and cached.get("size") == info["size"] and cached.get("mtime") == info["mtime"]:
        info["version"] = cached.get("version")
        info["build"] = cached.get("build")
    else:
        m = re.search(r"Xcode[_ -]?(\d+(?:\.\d+)*)", path.name)
        if m:
            info["nameVersion"] = m.group(1)
    return info


def xip_read_version(path: Path, step_id: str | None = "xcode") -> tuple[str, str]:
    from iosstudio.features import xip_version  # vendored, stdlib only
    result: dict = {}

    def work():
        try:
            _data, v, b = xip_version.read_version(str(path))
            result["v"], result["b"] = v, b
        except Exception as exc:  # noqa: BLE001 - any parse failure means "not a usable xip"
            result["err"] = str(exc)

    th = threading.Thread(target=work, daemon=True)
    start = time.time()
    th.start()
    while th.is_alive():
        th.join(1.5)
        el = time.time() - start
        sprog(step_id, min(0.95, el / 110), f"Checking the Xcode version inside the file · {fmt_dur(el)}, usually about 2 min",
              max(0, 110 - el))
    if "err" in result:
        raise HelperError("bad-xip", "This file isn't a complete Xcode download. Download it again.",
                          fix("Open download page", "setup", step="xcode"), detail=result["err"])
    st = path.stat()
    save_state({"xip": str(path), "xipInfo": {"path": str(path), "size": st.st_size, "mtime": st.st_mtime,
                                              "version": result["v"], "build": result["b"]}})
    return result["v"], result["b"]


def find_xips() -> list[Path]:
    seen, out = set(), []
    st = load_state().get("xip")
    cands = ([Path(st)] if st else []) + sorted(DOWNLOADS.glob("Xcode*.xip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        if p.is_file() and str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


def best_xip(major: str) -> dict | None:
    """The most promising local .xip for the needed Xcode major: valid, matching version first."""
    infos = [xip_quick(p) for p in find_xips()]
    if not infos:
        return None

    def score(i):
        ver = i.get("version") or i.get("nameVersion") or ""
        return (bool(i.get("valid")), ver.split(".")[0] == major, i.get("mtime", 0))
    return sorted(infos, key=score, reverse=True)[0]


# ------------------------------------------------------------------ facts (parallel, read-only)

def gather() -> dict:
    xt = xtool_bin()
    ad = find_apple_dev()
    pins = apple_dev_pins(ad)
    py_cands = [p for p in (VENV_DIR / "bin/python", LEGACY_VENV / "bin/python") if p.exists()]
    pycode = ("import importlib.metadata as m, cryptography, pymobiledevice3, sys; "
              "print(m.version('pymobiledevice3')); print(sys.version.split()[0])")
    jobs = {
        "pacman_T": lambda: q(["pacman", "-T", *PACKAGES]),
        "swift_qi": lambda: q(["pacman", "-Qi", "swift-bin"]),
        "swift_ver": lambda: q(["/usr/bin/swift", "--version"], timeout=10),
        "ignore": lambda: q(["pacman-conf", "IgnorePkg"]),
        "xtool_ver": (lambda: q([xt, "--version"])) if xt else (lambda: (127, "", "")),
        "sdk_status": (lambda: q([xt, "sdk", "status"])) if xt else (lambda: (127, "", "")),
        "auth": (lambda: q([xt, "auth", "status"])) if xt else (lambda: (127, "", "")),
        "rcodesign": lambda: q([LOCAL_BIN / "rcodesign", "--version"]) if (LOCAL_BIN / "rcodesign").exists() else q(["rcodesign", "--version"]),
        "ipsw": lambda: q([LOCAL_BIN / "ipsw", "version"]) if (LOCAL_BIN / "ipsw").exists() else q(["ipsw", "version"]),
        "pymd3": lambda: [(str(p), q([p, "-I", "-c", pycode], timeout=15)) for p in py_cands],
        "ad_head": (lambda: q(["git", "-C", ad, "rev-parse", "HEAD"])) if ad else (lambda: (127, "", "")),
        "ad_dirty": (lambda: q(["git", "-C", ad, "status", "--porcelain", "--untracked-files=no"])) if ad else (lambda: (127, "", "")),
        "xtool_old_ua": (lambda: binary_has_old_ua(Path(os.path.realpath(xt)))) if xt else (lambda: None),
        "xcspec": lambda: xcspec_check(toolchain_root()),
        "idevice": lambda: q(["idevice_id", "-l"], timeout=3),
        "avahi": lambda: q(["systemctl", "is-active", "avahi-daemon"], timeout=3),
        "py312": lambda: q(["pacman", "-Q", "python312"]),
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {k: ex.submit(fn) for k, fn in jobs.items()}
        F = {k: f.result() for k, f in futs.items()}
    F["xtool"] = xt
    F["apple_dev"] = ad
    F["pins"] = pins
    return F


# ------------------------------------------------------------------ the steps

def S(id_, title, group, description, state, detail="", *, size="", time_="", root=False, interactive=False,
      auto=True, essential=False, action=None, **extra) -> dict:
    if action is None:
        action = {"kind": "install", "label": "Repair" if state == "broken" else "Install"}
    d = {"id": id_, "title": title, "group": group, "description": description, "state": state, "detail": detail,
         "size": size, "time": time_, "needsRoot": root, "interactive": interactive,
         "canAutoInstall": auto and state in ("missing", "broken"), "essential": essential, "action": action}
    d.update(extra)
    return d


def build_steps(F: dict) -> tuple[list[dict], dict]:
    steps: list[dict] = []
    ctx: dict = {}

    # 1 Platform
    a = arch()
    plat_ok = a in ("x86_64", "aarch64") and shutil.which("pacman") is not None
    steps.append(S("platform", "This computer", "System",
                   "iOS Studio needs an Arch-based Linux (like Omarchy) on an Intel/AMD or ARM 64-bit computer.",
                   "done" if plat_ok else "broken",
                   f"{'Omarchy' if is_omarchy() else 'Arch Linux'} on {a}" if plat_ok else f"Unsupported: {a}, pacman {'found' if shutil.which('pacman') else 'missing'}",
                   auto=False, essential=True, action={"kind": "none"}))

    # 2 System packages
    missing_pkgs = [l.strip() for l in F["pacman_T"][1].splitlines() if l.strip()] if F["pacman_T"][0] != 0 else []
    ctx["missing_pkgs"] = missing_pkgs
    steps.append(S("packages", "System packages", "System",
                   "Tools for talking to an iPhone over USB (usbmuxd, libimobiledevice), building software (base-devel, git), packaging apps and saving screen recordings (ffmpeg).",
                   "missing" if missing_pkgs else "done",
                   ("Missing: " + ", ".join(missing_pkgs)) if missing_pkgs else "All installed",
                   size="about 50–150 MB", time_="under a minute", root=True, essential=True))

    # 3 usbmuxd
    unit = Path("/usr/lib/systemd/system/usbmuxd.service").exists()
    rule = Path("/usr/lib/udev/rules.d/39-usbmuxd.rules").exists()
    dropin = Path("/etc/systemd/system/usbmuxd.service.d/ios-studio-restart.conf").exists() or \
        Path("/etc/systemd/system/usbmuxd.service.d/restart.conf").exists()
    if not (unit and rule):
        st, det = "missing", "Comes with the system packages"
    elif not dropin:
        st, det = "optional", "Works. Optional: restart it automatically if it ever crashes"
    else:
        st, det = "done", "Starts by itself when an iPhone is plugged in, and restarts after a crash"
    steps.append(S("usbmuxd", "USB connection service", "System",
                   "usbmuxd carries the connection to an iPhone plugged in with a cable. It starts on its own when you plug the phone in.",
                   st, det, size="tiny", time_="seconds", root=True,
                   auto=bool(unit and rule), action={"kind": "install", "label": "Improve" if st == "optional" else "Install"}))

    # 4 Swift
    qi_rc, qi, _ = F["swift_qi"]
    swift_pkg = re.search(r"^Version\s*:\s*(\S+)", qi, re.M).group(1) if qi_rc == 0 and re.search(r"^Version\s*:\s*(\S+)", qi, re.M) else None
    swift_ver = parse_swift_version(F["swift_ver"][1]) if F["swift_ver"][0] == 0 else None
    ctx["swift_ver"] = swift_ver
    xc = xcode_for(swift_ver)
    if not swift_pkg and not swift_ver:
        st, det = "missing", "Not installed"
    elif not swift_ver:
        st, det = "broken", f"swift-bin {swift_pkg} is installed but swift doesn't run"
    elif not xc:
        st, det = "broken", f"Swift {swift_ver} doesn't match any iOS SDK iOS Studio knows (needs Swift 6.4)"
    else:
        st, det = "done", f"Swift {swift_ver}" + (f" (swift-bin {swift_pkg})" if swift_pkg else "") + f", matches Xcode {xc[0]}"
    steps.append(S("swift", "Swift compiler", "Toolchain",
                   "The Swift language toolchain that compiles your iPhone apps (the official swift.org build, packaged for Arch).",
                   st, det, size="about 1 GB download, 3.5 GB on disk", time_="about 5–10 minutes", root=True, essential=True))

    # 5 Hold swift-bin
    held = "swift-bin" in F["ignore"][1].split()
    hook_ok = (not is_omarchy()) or swift_hold_hook_present()
    if not swift_pkg:
        st, det = "missing", "Needs Swift first"
    elif held and hook_ok:
        st, det = "done", "System updates won't change your Swift version"
    elif held:
        st, det = "broken", "Held now, but “omarchy refresh pacman” would drop the hold"
    else:
        st, det = "missing", "A system update could replace Swift with a version the iOS SDK doesn't support"
    steps.append(S("swift-hold", "Keep Swift on this version", "Toolchain",
                   "Swift must match the iOS SDK. This stops system updates from upgrading it behind your back.",
                   st, det, size="none", time_="seconds", root=not held, essential=False, depends=["swift"]))

    # 6 xcspec
    xs = F["xcspec"]
    if xs is None:
        st, det = "missing", "Needs Swift first"
    elif xs:
        st, det = "done", "Text resources (.strings) build correctly"
    else:
        st, det = "broken", "Swift's build settings need a small fix for .strings files (reset by every Swift update)"
    steps.append(S("xcspec", "Swift build settings fix", "Toolchain",
                   "A small adjustment inside the Swift toolchain so localized text files in your apps build on Linux.",
                   st, det, size="none", time_="seconds", root=True, essential=True, depends=["swift"]))

    # 7 omarchy-apple-dev
    ad = F["apple_dev"]
    head = F["ad_head"][1].strip() if F["ad_head"][0] == 0 else ""
    ctx["apple_dev"], ctx["apple_dev_head"] = ad, head
    if not ad:
        st, det = "missing", "Not downloaded yet"
    elif not head:
        st, det = "done", f"Found at {appdirs.tilde(ad)}"
    elif head == APPLE_DEV_COMMIT:
        st, det = "done", f"At the tested version {head[:7]} ({appdirs.tilde(ad)})"
    else:
        st, det = "done", f"At {head[:7]}, not the tested {APPLE_DEV_COMMIT[:7]} ({appdirs.tilde(ad)})"
    if ad and F["ad_dirty"][0] == 0 and F["ad_dirty"][1].strip():
        det += " · has local changes"
    steps.append(S("apple-dev", "iOS build scripts", "Toolchain",
                   "omarchy-apple-dev: the open-source scripts that turn Xcode's iOS SDK into something Linux can build with.",
                   st, det, size="a few MB", time_="seconds", essential=True))

    # 8 xtool
    xt = F["xtool"]
    xv_rc, xv, _ = F["xtool_ver"]
    old_ua = F["xtool_old_ua"]
    ver_line = (xv.strip().splitlines() or [""])[0]
    if not xt:
        st, det = "missing", "Not installed"
    elif xv_rc != 0:
        st, det = "broken", "Installed, but it doesn't start (often after a Swift update). Repair rebuilds it"
    elif old_ua:
        st, det = "broken", "This build can't sign in to Apple (HTTP 503). Repair rebuilds it with the fix"
    else:
        st, det = "done", (("Build " + ver_line.split()[-1][:7]) if ver_line else "Works") + " · has the Apple sign-in fix"
    ncpu = os.cpu_count() or 4
    est_min = max(5, min(25, round(7 * 16 / ncpu)))
    steps.append(S("xtool", "xtool", "Toolchain",
                   "The tool that builds, signs and installs iPhone apps from Linux. It's compiled on your computer.",
                   st, det, size="1.8 GB while building, 410 MB installed", time_=f"about {est_min} minutes (no output while it compiles)",
                   essential=True, depends=["packages", "swift", "apple-dev"]))

    # 9 rcodesign
    rc, out, _ = F["rcodesign"]
    st = "done" if rc == 0 else "missing"
    steps.append(S("rcodesign", "rcodesign", "Toolchain",
                   "Signs apps for TestFlight and the App Store uploads.", st,
                   f"rcodesign {_first_ver(out)}" if rc == 0 else "Not installed",
                   size="about 10 MB", time_="seconds"))

    # 10 pymobiledevice3
    best = None
    for path, (prc, pout, _e) in F["pymd3"]:
        lines = pout.strip().splitlines()
        if prc == 0 and lines:
            best = (path, lines[0], lines[1] if len(lines) > 1 else "")
            break
    ctx["pymd3"] = best
    if best:
        st = "done"
        det = f"pymobiledevice3 {best[1]}" + ("" if best[1] == PYMD3_VERSION else f" (tested: {PYMD3_VERSION})") + \
            ("" if best[0].startswith(str(VENV_DIR)) else f" · {appdirs.tilde(Path(best[0]).parent.parent)}")
    elif F["pymd3"]:
        st, det = "broken", "Installed, but broken (usually after a Python update). Repair recreates it"
    else:
        st, det = "missing", "Not installed"
    steps.append(S("pymobiledevice3", "iPhone connection tools", "Toolchain",
                   "pymobiledevice3 talks to your iPhone over USB or Wi-Fi: app installs, logs, screenshots, Developer Mode.",
                   st, det, size="about 335 MB", time_="1–2 minutes", essential=True))

    # 11 ipsw (optional)
    rc, out, _ = F["ipsw"]
    steps.append(S("ipsw", "ipsw (debugger helper)", "Toolchain",
                   "Optional. Extracts system libraries from your iPhone so the debugger can show readable stack traces.",
                   "done" if rc == 0 else "optional",
                   (f"ipsw {_first_ver(out) or ''}".strip()) if rc == 0 else "Not installed (optional)",
                   size="about 40 MB", time_="seconds", optional=True, action={"kind": "install", "label": "Install"}))

    # 12 python312 for lldb (optional)
    pydep = None
    m = re.search(r"(python3\d+)", qi) if qi_rc == 0 else None
    if m:
        pydep = m.group(1)
    if pydep:
        have = F["py312"][0] == 0 if pydep == "python312" else q(["pacman", "-Q", pydep])[0] == 0
        steps.append(S("debugger", "Debugger support", "Toolchain",
                       f"Optional. lldb, Swift's debugger, needs {pydep}. It's compiled from source, which takes a while.",
                       "done" if have else "optional", f"{pydep} installed" if have else f"{pydep} not installed (optional)",
                       size="about 100 MB", time_="10–20 minutes", root=True, pkg=pydep, optional=True,
                       action={"kind": "install", "label": "Install"}))

    # 13 Xcode .xip, 14 SDK
    stamp = F["pins"]["XTOOL_SDK_VERSION"]
    rec_ver, major, approx = needed_xcode(swift_ver)
    ctx.update(stamp=stamp, xcode=rec_ver, xcode_major=major)
    sdk_rc, sdk_out, _ = F["sdk_status"]
    bundle = None
    m = re.search(r"Path:\s*(\S.*)$", sdk_out, re.M)
    if sdk_rc == 0 and "is installed" in sdk_out and m:
        bundle = Path(m.group(1).strip())
    else:
        for c in appdirs.sdk_bundles():
            if c.is_dir():
                bundle = c
                break
    installed_stamp = sdk_stamp(bundle) if bundle else ""
    ios_vers = sdk_ios_versions(bundle) if bundle else []
    extras_missing = sdk_extras_missing(bundle) if bundle else []
    matching = [v for v in ios_vers if v.split(".")[0] == major]
    caches = sdk_caches(stamp)
    good_cache = next((c for c in caches if c["current"] and c["complete"] and c["ios"].split(".")[0] == major), None)
    ctx.update(bundle=bundle, caches=caches, good_cache=good_cache)
    if bundle and installed_stamp == stamp and matching and not extras_missing:
        sdk_state, sdk_det = "done", f"iOS {matching[-1]} SDK" + (", spare copy in the cache" if good_cache else "")
    elif bundle and installed_stamp and installed_stamp != stamp:
        sdk_state, sdk_det = "broken", "Built by an older xtool; it needs rebuilding"
    elif bundle and ios_vers and not matching:
        sdk_state, sdk_det = "broken", f"Built from Xcode {ios_vers[-1]}, but Swift {swift_ver or '?'} needs Xcode {major}"
    elif bundle and extras_missing:
        sdk_state, sdk_det = "broken", "Missing extras: " + ", ".join(extras_missing)
    else:
        sdk_state, sdk_det = "missing", "Not installed"

    xip = best_xip(major)
    ctx["xip"] = xip
    if sdk_state == "done":
        xstate = "done"
        xdet = "Not needed any more: the iOS SDK is already built" + (f" (you can delete {xip['name']} to free {fmt_bytes(xip['size'])})" if xip and xip.get("valid") and good_cache else "")
    elif good_cache:
        xstate, xdet = "done", f"Not needed: a ready-made SDK copy (iOS {good_cache['ios']}) is cached"
    elif xip and xip.get("valid"):
        v = xip.get("version") or xip.get("nameVersion")
        if xip.get("version") and xip["version"].split(".")[0] != major:
            xstate, xdet = "broken", f"{xip['name']} is Xcode {xip['version']}, but you need Xcode {rec_ver}"
        else:
            xstate = "done"
            xdet = f"{xip['name']} ({fmt_bytes(xip['size'])})" + (f", Xcode {v}" if v else "") + ("" if xip.get("version") else ", will be checked before use")
    elif xip:
        xstate, xdet = "broken", xip.get("message", "That file isn't usable")
    else:
        xstate, xdet = "missing", f"Download Xcode {rec_ver} from Apple (about {fmt_bytes(approx)})"
    steps.append(S("xcode", f"Xcode {rec_ver} download", "Apple",
                   "The iOS SDK only comes inside Apple's Xcode download. You download it once in your browser with a free Apple Account; iOS Studio finds it in Downloads by itself.",
                   xstate, xdet, size=f"about {fmt_bytes(approx)} download", time_="depends on your connection",
                   interactive=True, auto=False, essential=sdk_state != "done",
                   action={"kind": "open-url", "label": "Open download page", "url": DIRECT_XIP.format(v=rec_ver) if "." in rec_ver else DOWNLOAD_PAGE.format(v=rec_ver),
                           "pageUrl": DOWNLOAD_PAGE.format(v=rec_ver), "watch": True},
                   xcode={"needed": rec_ver, "major": major, "swift": swift_ver, "expectedSize": approx,
                          "downloadUrl": DIRECT_XIP.format(v=rec_ver) if "." in rec_ver else None,
                          "pageUrl": DOWNLOAD_PAGE.format(v=rec_ver), "file": xip, "downloads": str(DOWNLOADS)}))
    sdk_auto = bool(good_cache or (xip and xip.get("valid")))
    steps.append(S("sdk", "iOS SDK", "Apple",
                   "Apple's iOS frameworks (SwiftUI, UIKit…) unpacked from Xcode and prepared for Linux. Needed to build any app.",
                   sdk_state, sdk_det + ("" if sdk_state == "done" or sdk_auto else " · needs the Xcode download first"),
                   size="about 7 GB on disk (15 GB free needed while building)", time_="about 20–30 minutes",
                   auto=sdk_auto, essential=True, depends=["xtool", "xcode", "pymobiledevice3"]))

    # 15 Apple ID
    arc, aout, _ = F["auth"]
    first = (aout.strip().splitlines() or [""])[0]
    if not xt:
        st, det = "missing", "Needs xtool first"
    elif first == "Logged in.":
        aid = re.search(r"Apple ID:\s*(\S+)", aout)
        exp = re.search(r"Token expiry:\s*(.+)$", aout, re.M)
        st = "done"
        det = (f"Signed in as {aid.group(1)}" if aid else "Signed in") + (f" · token valid until {exp.group(1).strip()}" if exp else "")
    else:
        st, det = "missing", "Not signed in"
    steps.append(S("apple-id", "Apple Account", "Apple",
                   "Sign in with your Apple Account (a free one works) so iOS Studio can sign apps for your iPhone. You'll need the 6-digit code from one of your Apple devices.",
                   st, det, time_="a minute", interactive=True, auto=False, essential=True,
                   action={"kind": "page", "label": "Sign in", "page": "account", "action": "signin"}))

    # 16 iPhone pairing
    wifi = sorted(p.name[len("remote_"):-len(".plist")] for p in (HOME / ".local/share/pymobiledevice3").glob("remote_*.plist"))
    try:
        usb = sorted(p.stem for p in Path("/var/lib/lockdown").glob("*.plist") if re.fullmatch(r"[0-9A-Fa-f-]{20,}", p.stem))
    except OSError:
        usb = []
    plugged = [l.strip() for l in F["idevice"][1].splitlines() if l.strip()] if F["idevice"][0] == 0 else []
    if usb or wifi:
        how = " and ".join(x for x in (("USB" if usb else ""), ("Wi-Fi" if wifi else "")) if x)
        st, det = "done", f"Trusted over {how}" + (" · plugged in now" if plugged else "")
    elif plugged:
        st, det = "missing", "Plugged in, but not trusted yet: unlock it and tap Trust"
    else:
        st, det = "missing", "No iPhone paired yet"
    steps.append(S("iphone", "Pair your iPhone", "iPhone",
                   "Connect your iPhone with a cable (or over Wi-Fi), unlock it and tap Trust, so this computer can install apps on it.",
                   st, det, time_="a minute", interactive=True, auto=False, essential=True,
                   udids=sorted(set(usb) | set(wifi)), plugged=plugged,
                   action={"kind": "page", "label": "Pair iPhone", "page": "device", "action": "pair"}))

    # 17 Developer Mode (only the device daemon can tell; we read its last report)
    dm = last_device_developer_mode()
    if dm is True:
        st, det = "done", "On"
    elif dm is False:
        st, det = "missing", "Off: Settings › Privacy & Security › Developer Mode"
    else:
        st, det = "unknown", "Connect your iPhone to check"
    steps.append(S("developer-mode", "Developer Mode", "iPhone",
                   "iPhones only run apps you build yourself with Developer Mode on: Settings › Privacy & Security › Developer Mode, then restart the phone.",
                   st, det, time_="a minute (the phone restarts)", interactive=True, auto=False, essential=False,
                   action={"kind": "page", "label": "Show me how", "page": "device", "action": "developer-mode"}))

    # 18 UxPlay (optional)
    ux = shutil.which("uxplay")
    avahi = F["avahi"][1].strip() == "active"
    if ux and avahi:
        st, det = "done", "Installed: pick this computer in Control Center › Screen Mirroring"
    elif ux:
        st, det = "broken", "Installed, but the network discovery service (avahi) isn't running"
    else:
        st, det = "optional", "Not installed (optional)"
    steps.append(S("uxplay", "Screen mirroring (UxPlay)", "iPhone",
                   "Optional. Shows your iPhone's screen live on this computer over Wi-Fi (AirPlay), smooth and with sound.",
                   st, det, size="about 20 MB", time_="2–5 minutes", root=True, optional=True,
                   action={"kind": "install", "label": "Repair" if st == "broken" else "Install"}))

    # 18b Wireshark (optional): opens the packet captures from iPhone › Network
    ws = shutil.which("wireshark")
    steps.append(S("wireshark", "Wireshark (packet viewer)", "iPhone",
                   "Optional. Opens the network recordings you make on the iPhone page's Network tab, packet by packet.",
                   "done" if ws else "optional", "Installed" if ws else "Not installed (optional)",
                   size="about 100 MB", time_="about a minute", root=True, optional=True,
                   action={"kind": "install", "label": "Install"}))

    # 19 App integration
    have_desktop = DESKTOP_FILE.exists()
    have_launcher = LAUNCHER.exists()
    if have_desktop and have_launcher:
        st, det = "done", "In the app launcher"
    else:
        st, det = "missing", "Not in the app launcher yet" if not have_desktop else "The ios-studio command is missing"
    steps.append(S("app-integration", "App launcher entry", "System",
                   "Adds iOS Studio to the app launcher with its icon, so you can open it like any other app.",
                   st, det, size="tiny", time_="seconds", auto=(REPO / "install.sh").exists()))

    return steps, ctx


def last_device_developer_mode():
    for name in ("device.json", "last-device.json"):
        d = fileio.load_json(STATE_DIR / name)
        if isinstance(d, dict):
            d = d.get("data", d)
            for k in ("developerMode", "developer_mode"):
                if isinstance(d.get(k), bool):
                    return d[k]
    return None


def summarize(steps: list[dict]) -> dict:
    counted = [s for s in steps if not s.get("optional") and s["state"] != "optional" and s["id"] != "platform"]
    done_n = sum(1 for s in counted if s["state"] == "done")
    essential_missing = [s["id"] for s in steps if s.get("essential") and s["state"] in ("missing", "broken")]
    broken = [s["id"] for s in steps if s["state"] == "broken"]
    auto = [s["id"] for s in steps if s["canAutoInstall"] and s["state"] in ("missing", "broken")]
    ready = not essential_missing
    if ready:
        level, title = "ok", "Everything is ready"
    elif broken and any(b in essential_missing for b in broken):
        level, title = "error", "Something needs repair"
    else:
        level, title = "warn", f"{len(essential_missing)} step{'s' if len(essential_missing) != 1 else ''} left"
    return {"total": len(counted), "done": done_n, "readiness": f"{done_n} of {len(counted)} ready",
            "ready": ready, "essentialMissing": essential_missing, "broken": broken, "autoInstallable": auto,
            "level": level, "title": title, "badge": "" if ready else str(len(essential_missing))}


def cmd_check(flags: dict) -> int:
    fixture = os.environ.get("IOS_STUDIO_SETUP_FIXTURE")  # UI development only: a saved `setup check` data object
    if fixture:
        return ok(fileio.load_json(Path(fixture), {}))
    t0 = time.time()
    F = gather()
    steps, ctx = build_steps(F)
    data = {"steps": steps, "summary": summarize(steps), "xcode": next(s["xcode"] for s in steps if s["id"] == "xcode"),
            "groups": ["System", "Toolchain", "Apple", "iPhone"], "elapsed": round(time.time() - t0, 2)}
    return ok(data)


# ------------------------------------------------------------------ installers

class Ctx:
    def __init__(self, flags: dict):
        self.flags = flags
        self.F = gather()
        self.steps, self.c = build_steps(self.F)
        self.by_id = {s["id"]: s for s in self.steps}

    def refresh(self):
        self.F = gather()
        self.steps, self.c = build_steps(self.F)
        self.by_id = {s["id"]: s for s in self.steps}

    def state(self, sid: str) -> str:
        return self.by_id.get(sid, {}).get("state", "missing")


def confirm(message: str, yes_label: str, flags: dict) -> None:
    if flags.get("yes") or DRY:
        if DRY:
            slog(None, f"would ask: {message}")
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
        slog(step_id, "would ask for your password once, then run: " + " ".join(_shq(a) for a in argv))
        rc = run_stream(["bash", str(ROOT_SCRIPT), *args, "--dry-run"], step_id, label=label, mutating=False)
        must(rc, "root-failed", "The root script rejected these arguments (dry run).", step_id)
        return
    if not shutil.which("pkexec"):
        raise HelperError("no-pkexec", "This computer has no way to ask for your password (polkit is missing).")
    slog(step_id, "Asking for your password…")
    sprog(step_id, None, "Waiting for your password")
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


def aur_fetch(pkg: str, ref: str | None, step_id: str) -> Path:
    """Unpack an AUR snapshot (pinned commit or HEAD) into ~/.cache/ios-studio/aur/<pkg> as the user."""
    dest = AUR_DIR / pkg
    url = AUR_SNAPSHOT.format(ref=ref) if ref else AUR_SNAPSHOT_HEAD.format(pkg=pkg)
    if DRY:
        slog(step_id, f"would fetch the AUR recipe {url} into {dest}")
        return dest
    marker = dest / ".ios-studio-ref"
    if ref and marker.exists() and marker.read_text().strip() == ref and (dest / "PKGBUILD").exists():
        return dest
    tmpdir = Path(tempfile.mkdtemp(prefix=f".{pkg}.", dir=_mk(AUR_DIR)))
    tgz = download(url, tmpdir / "snap.tar.gz", step_id, label=f"{pkg} recipe")
    with tarfile.open(tgz) as tf:
        members = [m for m in tf.getmembers() if not (m.name.startswith("/") or ".." in Path(m.name).parts)]
        tf.extractall(tmpdir, members=members, filter="data") if sys.version_info >= (3, 12) else tf.extractall(tmpdir, members=members)
    top = next((p for p in tmpdir.iterdir() if p.is_dir() and (p / "PKGBUILD").exists()), None)
    if not top:
        raise HelperError("aur", f"The {pkg} recipe from the AUR looks wrong.", detail=url)
    # keep downloaded sources between attempts
    old_sources = [p for p in dest.glob("*") if p.is_file() and p.suffix in (".gz", ".xz", ".zst", ".tar")] if dest.exists() else []
    for p in old_sources:
        os.replace(p, top / p.name)
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(top, dest)
    shutil.rmtree(tmpdir, ignore_errors=True)
    if ref:
        marker.write_text(ref + "\n")
    return dest


def _mk(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def srcinfo(dir_: Path) -> dict:
    out: dict = {}
    try:
        for line in (dir_ / ".SRCINFO").read_text().splitlines():
            if "=" in line:
                k, v = line.strip().split("=", 1)
                out.setdefault(k.strip(), []).append(v.strip())
    except OSError:
        pass
    return out


def strip_ver(dep: str) -> str:
    return re.split(r"[<>=]", dep, 1)[0]


def prefetch_swift_sources(d: Path, step_id: str) -> None:
    """Download swift.org's tarball ourselves (real progress), so makepkg only verifies it."""
    info = srcinfo(d)
    a = arch()
    srcs = info.get(f"source_{a}", [])
    sums = info.get(f"sha256sums_{a}", [])
    for i, src in enumerate(srcs):
        url = src.split("::", 1)[-1]
        name = src.split("::", 1)[0] if "::" in src else url.rsplit("/", 1)[-1]
        if not url.startswith("https://"):
            continue
        sha = sums[i] if i < len(sums) and sums[i] != "SKIP" else None
        download(url, d / name, step_id, sha256=sha, label="Swift", frac_range=(0.02, 0.6))


def i_root_bundle(ctx: Ctx, ids: list[str], step_id: str, label: str) -> None:
    """Collect the root work of several steps and run it with one password prompt."""
    args: list[str] = []
    a = arch()
    if "packages" in ids:
        miss = ctx.c.get("missing_pkgs") or []
        if miss or ctx.flags.get("force"):
            args += ["--packages", " ".join(miss or PACKAGES)]
    if "swift" in ids and (ctx.state("swift") != "done" or ctx.flags.get("force")):
        d = aur_fetch("swift-bin", SWIFT_AUR_COMMIT, step_id)
        info = srcinfo(d) if not DRY else {}
        mk = sorted({strip_ver(x) for x in info.get("makedepends", []) + info.get(f"makedepends_{a}", [])} | {"base-devel"})
        if DRY:
            mk = ["base-devel", "patchelf"]
        need_space(HOME, 5_500_000_000, "Swift")
        prefetch_swift_sources(d, step_id) if not DRY else slog(step_id, "would download the Swift toolchain (about 1 GB) from swift.org")
        args += ["--deps", " ".join(mk), "--aur-build", str(d)]
        ids = list(ids) + ["swift-hold", "xcspec"]
    if "swift-hold" in ids and ("swift-bin" not in ctx.F["ignore"][1].split() or ctx.flags.get("force")):
        args += ["--hold-swift"]
    if "xcspec" in ids and (ctx.state("xcspec") != "done" or ctx.flags.get("force") or "--aur-build" in args):
        args += ["--xcspec", str(XCSPEC_PY)]
    if "usbmuxd" in ids and ctx.state("usbmuxd") != "done":
        args += ["--usbmuxd-dropin", "--udev-trigger"]
    if "uxplay" in ids:
        if not shutil.which("uxplay") or ctx.flags.get("force"):
            d = aur_fetch("uxplay", None, step_id)
            info = srcinfo(d) if not DRY else {}
            deps = sorted({strip_ver(x) for x in info.get("depends", []) + info.get("makedepends", []) +
                           info.get(f"depends_{a}", []) + info.get(f"makedepends_{a}", [])} | {"base-devel"})
            args += ["--deps", " ".join(deps), "--aur-build", str(d)]
        ux_miss = UXPLAY_RUNTIME if DRY else [x for x in UXPLAY_RUNTIME if q(["pacman", "-T", x])[0] != 0]
        if ux_miss:
            if "--packages" in args:
                i = args.index("--packages") + 1
                args[i] = " ".join(dict.fromkeys(args[i].split() + ux_miss))
            else:
                args += ["--packages", " ".join(ux_miss)]
        args += ["--avahi", "--ufw-uxplay"]
    if "wireshark" in ids and (not shutil.which("wireshark") or ctx.flags.get("force")):
        if "--packages" in args:   # one --packages flag: root-tasks.sh keeps only the last one
            i = args.index("--packages")
            args[i + 1] += " wireshark-qt"
        else:
            args += ["--packages", "wireshark-qt"]
    if "debugger" in ids:
        pkg = ctx.by_id.get("debugger", {}).get("pkg")
        if pkg and (ctx.state("debugger") != "done" or ctx.flags.get("force")):
            d = aur_fetch(pkg, None, step_id)
            info = srcinfo(d) if not DRY else {}
            deps = sorted({strip_ver(x) for x in info.get("depends", []) + info.get("makedepends", [])} | {"base-devel"})
            args += ["--deps", " ".join(deps), "--aur-build", str(d)]
    if not args:
        slog(step_id, "Nothing needs your password here.")
        return
    run_root(args, step_id, label)


def i_swift_hook(ctx: Ctx, step_id: str) -> None:
    if not is_omarchy() or swift_hold_hook_present():
        return
    if DRY:
        slog(step_id, f"would install the Omarchy hook {OMARCHY_HOOK}")
        return
    OMARCHY_HOOK.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HOOK_SRC, OMARCHY_HOOK)
    os.chmod(OMARCHY_HOOK, 0o755)
    slog(step_id, f"installed the Omarchy hook {appdirs.tilde(OMARCHY_HOOK)} (keeps the hold through “omarchy refresh pacman”)")


def git(args: list, step_id: str, cwd=None, mutating=True, tail=None) -> int:
    return run_stream(["git", *args], step_id, mutating=mutating, cwd=cwd, tail=tail,
                      env=subproc.tool_env("setup", {"GIT_TERMINAL_PROMPT": "0"}))


def i_apple_dev(ctx: Ctx, step_id: str) -> Path:
    ad = ctx.c.get("apple_dev")
    if ad and not ctx.flags.get("force"):
        return ad
    d = APPLE_DEV_HOME
    if d.exists() and not (d / ".git").is_dir():
        raise HelperError("apple-dev-path", f"{appdirs.tilde(d)} exists but isn't a download of the iOS build scripts. "
                          "Move it away, then try again.")
    do_mkdir(d.parent, step_id)
    tail: list = []
    if not (d / ".git").is_dir():
        must(git(["init", "-q", str(d)], step_id), "git", "Couldn't prepare the iOS build scripts folder.", step_id)
        must(git(["-C", str(d), "remote", "add", "origin", APPLE_DEV_REPO], step_id), "git", "Couldn't prepare the iOS build scripts folder.", step_id)
    sprog(step_id, 0.3, "Downloading the iOS build scripts")
    must(git(["-C", str(d), "fetch", "-q", "--depth", "1", APPLE_DEV_REPO, APPLE_DEV_COMMIT], step_id, tail=tail),
         "network", "Couldn't download the iOS build scripts from GitHub. Check your internet connection, then try again.", step_id, tail)
    must(git(["-C", str(d), "checkout", "-q", "--detach", APPLE_DEV_COMMIT], step_id, tail=tail),
         "git", "Couldn't check out the iOS build scripts.", step_id, tail)
    if not DRY:
        rc, out, _ = q(["git", "-C", d, "rev-parse", "HEAD"])
        if out.strip() != APPLE_DEV_COMMIT:
            raise HelperError("git", "The iOS build scripts aren't at the expected version after download.", detail=out)
    return d


def i_xtool(ctx: Ctx, step_id: str) -> None:
    for dep in ("packages", "swift"):
        if ctx.state(dep) not in ("done",) and not DRY:
            t = ctx.by_id[dep]["title"]
            raise HelperError("needs-step", f"Install “{t}” first.", fix(f"Install {t}", "setup", step=dep))
    ad = ctx.c.get("apple_dev") or i_apple_dev(ctx, step_id)
    pins = apple_dev_pins(ad if ad and ad.exists() else None)
    sha, repo = pins["XTOOL_SHA"], pins["XTOOL_REPO"]
    fix_dir = APPLE_DEV_LIB / f"xtool-{sha[:7]}-gsa406"
    pin_dir = APPLE_DEV_LIB / f"xtool-{sha}"
    src = APPLE_DEV_CACHE / f"xtool-{sha[:7]}-gsa406"
    link = LOCAL_BIN / "xtool"

    def complete(d: Path) -> bool:
        return os.access(d / "xtool", os.X_OK) and (d / "libswiftCore.so").exists()

    for cand in (pin_dir, fix_dir):
        if complete(cand) and binary_has_old_ua(cand / "xtool") is False and not ctx.flags.get("force"):
            rc, _, _ = q([cand / "xtool", "--version"])
            if rc == 0:
                slog(step_id, f"A working xtool with the sign-in fix is already built: {appdirs.tilde(cand)}")
                do_symlink(cand / "xtool", link, step_id)
                return
    need_space(APPLE_DEV_CACHE, 3_000_000_000, "building xtool")
    sprog(step_id, 0.02, "Downloading xtool's source code")
    do_mkdir(src, step_id)
    tail: list = []
    if not (src / ".git").is_dir():
        must(git(["init", "-q", str(src)], step_id), "git", "Couldn't prepare xtool's source folder.", step_id)

    def have(c: str) -> bool:
        return not DRY and q(["git", "-C", src, "cat-file", "-e", f"{c}^{{commit}}"])[0] == 0
    if not have(sha):
        must(git(["-C", str(src), "fetch", "-q", "--depth", "1", repo, sha], step_id, tail=tail), "network",
             "Couldn't download xtool's source code. Check your internet connection, then try again.", step_id, tail)
    if not have(XTOOL_FIX_SHA + "^"):
        must(git(["-C", str(src), "fetch", "-q", "--depth", "2", XTOOL_FIX_REPO, XTOOL_FIX_SHA], step_id, tail=tail), "network",
             "Couldn't download the xtool sign-in fix. Check your internet connection, then try again.", step_id, tail)
    sprog(step_id, 0.05, "Applying the Apple sign-in fix")
    build_commit = sha
    dest = fix_dir
    if not DRY:
        if base_has_fix(src, sha):
            slog(step_id, f"xtool {sha[:7]} already contains the sign-in fix; building it as is")
            dest = pin_dir
            must(git(["-C", str(src), "checkout", "-q", "-f", "--detach", sha], step_id), "git", "Couldn't prepare xtool's source.", step_id)
        elif not fix_source_ok(src, sha):
            must(git(["-C", str(src), "checkout", "-q", "-f", "--detach", sha], step_id), "git", "Couldn't prepare xtool's source.", step_id)
            rc, date, _ = q(["git", "-C", src, "log", "-1", "--format=%aI", XTOOL_FIX_SHA])
            env = subproc.tool_env("setup", {"GIT_COMMITTER_NAME": "iOS Studio", "GIT_COMMITTER_EMAIL": "setup@ios-studio.invalid",
                            "GIT_COMMITTER_DATE": date.strip()})
            rc = run_stream(["git", "-C", str(src), "-c", "commit.gpgsign=false", "cherry-pick", XTOOL_FIX_SHA], step_id, env=env, tail=tail)
            must(rc, "git", "Couldn't apply the xtool sign-in fix.", step_id, tail)
            if not fix_source_ok(src, sha):
                raise HelperError("git", "xtool's source doesn't match the expected fix after applying it.")
        build_commit = q(["git", "-C", src, "rev-parse", "HEAD"])[1].strip()
    else:
        slog(step_id, f"would cherry-pick xtool-org/xtool {XTOOL_FIX_SHA[:7]} onto {sha[:7]} in {src}")
    ncpu = os.cpu_count() or 4
    est = max(300, min(1500, 7 * 60 * 16 / ncpu))
    swift_real = os.path.realpath("/usr/bin/swift") if Path("/usr/bin/swift").exists() else "/usr/bin/swift"
    benv = subproc.tool_env("setup", {"PATH": f"{os.path.dirname(swift_real)}:/usr/bin:{LOCAL_BIN}:/bin"})
    tail = []
    rc = run_stream(["swift", "build", "-c", "release", "--product", "xtool", "-Xswiftc", "-no-toolchain-stdlib-rpath"],
                    step_id, label="Compiling xtool", estimate=est, env=benv, cwd=src, frac_range=(0.08, 0.92), tail=tail)
    must(rc, "build-failed", "xtool didn't compile. This usually means Swift and xtool versions don't match; "
         "repairing Swift first can help.", step_id, tail)
    if DRY:
        slog(step_id, f"would install the built xtool and its Swift runtime into {dest}, then link {link}")
        return
    sprog(step_id, 0.94, "Installing xtool")
    rc, binp, _ = q(["swift", "build", "-c", "release", "--show-bin-path"], env=benv, cwd=src, timeout=120)
    binp = Path(binp.strip())
    root = toolchain_root() or Path("/usr/lib/swift")
    runtime = root / "usr/lib/swift/linux"
    stage = dest.with_name(dest.name + ".partial")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copy2(binp / "xtool", stage / "xtool")
    os.chmod(stage / "xtool", 0o755)
    rc, ldd, _ = q(["ldd", stage / "xtool"], env=subproc.tool_env("setup", {"LD_LIBRARY_PATH": str(runtime)}))
    for line in ldd.splitlines():
        m = re.search(r"=>\s*(\S+)", line)
        if m and m.group(1).startswith(str(runtime) + "/"):
            shutil.copy2(m.group(1), stage / Path(m.group(1)).name)
            os.chmod(stage / Path(m.group(1)).name, 0o644)
    if not complete(stage) or binary_has_old_ua(stage / "xtool") is not False:
        raise HelperError("build-failed", "The new xtool build is incomplete or still lacks the sign-in fix.")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(stage, dest)
    do_symlink(dest / "xtool", link, step_id)
    rc, out, _ = q([link, "--version"])
    must(rc, "build-failed", "The new xtool doesn't start.", step_id, [out])
    slog(step_id, f"Installed {appdirs.tilde(dest)} ({out.strip()}, built from {build_commit[:7]})")


GIT_TIMEOUT = 60


def _git(src: Path, *args: str, input: bytes | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    """A local git command on the xtool checkout, bounded (a locked or huge repo can't hang setup)."""
    try:
        return subprocess.run(["git", "-C", str(src), *args], input=input, env=env, capture_output=True,
                              timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise HelperError("timeout", f"git {args[0]} took longer than {GIT_TIMEOUT} s in {appdirs.tilde(src)}.",
                          fix("Try again", "setup", step="xtool"))


def base_has_fix(src: Path, sha: str) -> bool:
    fd, index = tempfile.mkstemp()
    os.close(fd)
    try:
        env = subproc.tool_env("setup", {"GIT_INDEX_FILE": index})
        if _git(src, "read-tree", sha, env=env).returncode != 0:
            return False
        diff = _git(src, "diff", f"{XTOOL_FIX_SHA}^", XTOOL_FIX_SHA).stdout
        r = _git(src, "apply", "--cached", "--reverse", "--check", input=diff, env=env)
        return r.returncode == 0
    finally:
        os.unlink(index)


def _patch_id(src: Path, a: str, b: str) -> str:
    diff = _git(src, "diff", a, b).stdout
    r = _git(src, "patch-id", "--stable", input=diff)
    return (r.stdout.decode().split() or [""])[0]


def fix_source_ok(src: Path, sha: str) -> bool:
    if q(["git", "-C", src, "rev-parse", "-q", "--verify", "HEAD^"])[1].strip() != sha:
        return False
    if q(["git", "-C", src, "diff", "--quiet", "HEAD"])[0] != 0:
        return False
    if sha == XTOOL_KNOWN_BASE and q(["git", "-C", src, "rev-parse", "HEAD^{tree}"])[1].strip() != XTOOL_KNOWN_TREE:
        return False
    return _patch_id(src, sha, "HEAD") == _patch_id(src, f"{XTOOL_FIX_SHA}^", XTOOL_FIX_SHA)


def i_rcodesign(ctx: Ctx, step_id: str) -> None:
    a = arch()
    sha = RCODESIGN_SHA.get(a)
    if not sha:
        raise HelperError("unsupported", f"rcodesign has no build for {a}.")
    name = f"apple-codesign-{RCODESIGN_VERSION}-{a}-unknown-linux-musl"
    url = f"https://github.com/indygreg/apple-platform-rs/releases/download/apple-codesign%2F{RCODESIGN_VERSION}/{name}.tar.gz"
    _install_tarball_binary(url, sha, f"{name}/rcodesign", LOCAL_BIN / "rcodesign", step_id, "rcodesign")


def i_ipsw(ctx: Ctx, step_id: str) -> None:
    a = arch()
    if a not in IPSW_SHA:
        raise HelperError("unsupported", f"ipsw has no build for {a}.")
    ia, sha = IPSW_SHA[a]
    url = f"https://github.com/blacktop/ipsw/releases/download/v{IPSW_VERSION}/ipsw_{IPSW_VERSION}_linux_{ia}.tar.gz"
    _install_tarball_binary(url, sha, "ipsw", LOCAL_BIN / "ipsw", step_id, "ipsw")


def _install_tarball_binary(url, sha, member, dest: Path, step_id, label):
    if DRY:
        download(url, Path("/nonexistent"), step_id, sha256=sha, label=label)
        slog(step_id, f"would install {member} -> {dest}")
        return
    with tempfile.TemporaryDirectory(prefix="ios-studio-") as td:
        tgz = download(url, Path(td) / "pkg.tar.gz", step_id, sha256=sha, label=label, frac_range=(0, 0.9))
        with tarfile.open(tgz) as tf:
            try:
                m = tf.getmember(member)
            except KeyError:
                raise HelperError("bad-download", f"The {label} download doesn't contain the program.")
            f = tf.extractfile(m)
            with fileio.replacing(dest, 0o755) as tmp, open(tmp, "xb") as out:
                shutil.copyfileobj(f, out)
    slog(step_id, f"installed {appdirs.tilde(dest)}")


def i_pymd3(ctx: Ctx, step_id: str) -> None:
    py = "/usr/bin/python3"
    if not Path(py).exists():
        raise HelperError("needs-step", "Install “System packages” first.", fix("Install System packages", "setup", step="packages"))
    venv = VENV_DIR
    if venv.exists():
        do_rmtree(venv, step_id)
    do_mkdir(venv.parent, step_id)
    sprog(step_id, 0.05, "Creating a private Python environment")
    tail: list = []
    must(run_stream([py, "-m", "venv", str(venv)], step_id, tail=tail), "venv", "Couldn't create the Python environment.", step_id, tail)
    sprog(step_id, 0.15, f"Downloading pymobiledevice3 {PYMD3_VERSION}")

    def parse(line, st):
        if line.startswith(("Collecting", "Downloading")):
            st["detail"] = line.split(" (")[0][:80]
        if line.startswith("Installing collected"):
            st["detail"] = "Installing"
        return None
    rc = run_stream([venv / "bin/pip", "install", "--disable-pip-version-check", "--no-input", "--progress-bar", "off",
                     f"pymobiledevice3=={PYMD3_VERSION}"], step_id, label="Installing pymobiledevice3", estimate=90,
                    parse=parse, frac_range=(0.15, 0.97), tail=tail)
    must(rc, "network", "Couldn't install pymobiledevice3. Check your internet connection, then try again.", step_id, tail)
    # omarchy-apple-dev's scripts (device-run.sh, install-toolchain.sh --repair) look for ~/pymobile3-venv.
    if not os.path.lexists(LEGACY_VENV):
        do_symlink(venv, LEGACY_VENV, step_id)
    if not DRY:
        rc, out, err = q([venv / "bin/python", "-I", "-c", "import pymobiledevice3, cryptography"], timeout=30)
        must(rc, "venv", "pymobiledevice3 was installed but doesn't load.", step_id, [err])


def ensure_legacy_venv(ctx: Ctx, step_id: str) -> None:
    """install-toolchain.sh --repair runs venv_ensure on ~/pymobile3-venv; make sure it is healthy so it
    never pip-installs a second, unpinned copy."""
    best = ctx.c.get("pymd3")
    if os.path.lexists(LEGACY_VENV):
        ok_ = q([LEGACY_VENV / "bin/python", "-I", "-c", "import cryptography, pymobiledevice3"], timeout=20)[0] == 0
        if ok_:
            return
        if LEGACY_VENV.is_symlink():
            do_unlink(LEGACY_VENV, step_id)
        else:
            raise HelperError("venv", "The older Python environment at ~/pymobile3-venv is broken. Repair "
                              "“iPhone connection tools” first.", fix("Repair", "setup", step="pymobiledevice3"))
    if not (best and best[0].startswith(str(VENV_DIR))):
        i_pymd3(ctx, step_id)
    else:
        do_symlink(VENV_DIR, LEGACY_VENV, step_id)


def i_sdk(ctx: Ctx, step_id: str) -> None:
    for dep in ("xtool", "pymobiledevice3"):
        if ctx.state(dep) != "done" and not DRY:
            t = ctx.by_id[dep]["title"]
            raise HelperError("needs-step", f"Install “{t}” first.", fix(f"Install {t}", "setup", step=dep))
    ad = ctx.c.get("apple_dev") or i_apple_dev(ctx, step_id)
    major = ctx.c["xcode_major"]
    cache = ctx.c.get("good_cache")
    xip_path = ctx.flags.get("xip")
    xt = str(LOCAL_BIN / "xtool")
    benv = subproc.tool_env("setup", {"PATH": f"{os.path.dirname(os.path.realpath('/usr/bin/swift'))}:/usr/bin:{LOCAL_BIN}:/bin"})
    tail: list = []
    if not cache or ctx.flags.get("rebuild"):
        xip = Path(xip_path) if xip_path else (Path(ctx.c["xip"]["path"]) if ctx.c.get("xip") and ctx.c["xip"].get("valid") else None)
        if not xip:
            raise HelperError("needs-xcode", f"Download Xcode {ctx.c['xcode']} first; iOS Studio builds the SDK from it.",
                              fix("Get Xcode", "setup", step="xcode"))
        info = xip_quick(xip)
        if not info.get("valid"):
            raise HelperError("bad-xip", info.get("message", "That Xcode file can't be used."), fix("Get Xcode", "setup", step="xcode"))
        if not info.get("version") and not DRY:
            v, b = xip_read_version(xip, step_id)
            info["version"] = v
        if info.get("version") and info["version"].split(".")[0] != major:
            raise HelperError("xcode-mismatch", f"That file is Xcode {info['version']}, but your Swift needs Xcode {ctx.c['xcode']}.",
                              fix("Get Xcode", "setup", step="xcode"))
        need_space(SDK_CACHE, 15_000_000_000, "building the iOS SDK")
        confirm(f"Build the iOS SDK from {xip.name}? It takes about 20–30 minutes and needs about 15 GB of free space "
                f"while it works (about 7 GB stays). You can keep using your computer meanwhile.", "Build SDK", ctx.flags)
        do_mkdir(SDK_CACHE, step_id)
        tmpd = Path(tempfile.mkdtemp(prefix=".build.", dir=SDK_CACHE)) if not DRY else SDK_CACHE / ".build.XXXX"

        def parse(line, st):
            m = re.match(r"^\[([^\]]+)\]\s*(\d+(?:\.\d+)?)%", line)
            if m:
                st["detail"] = f"Unpacking Xcode: {m.group(1)}"
                st["frac"] = float(m.group(2)) / 100
                return "quiet"
            return None
        rc = run_stream([xt, "sdk", "build", str(xip), str(tmpd)], step_id, label="Unpacking Xcode and building the SDK",
                        estimate=900, env=benv, parse=parse, frac_range=(0.03, 0.6), tail=tail)
        must(rc, "sdk-build", "Building the SDK from Xcode failed. The log has the details.", step_id, tail)
        if not DRY:
            built = tmpd / "darwin.xtoolsdk"
            vers = sdk_ios_versions(built)
            ver = f"iPhoneOS{vers[-1]}" if vers else "unknown"
            cached = SDK_CACHE / f"darwin-{ver}.xtoolsdk"
            if cached.exists():
                shutil.rmtree(cached)
            os.replace(built, cached)
            shutil.rmtree(tmpd, ignore_errors=True)
            plist = Path(str(cached) + ".version.plist")
            try:
                from iosstudio.features import xip_version
                data, _v, _b = xip_version.read_version(str(xip))
                plist.write_bytes(data)
            except Exception:  # noqa: BLE001
                slog(step_id, "couldn't record the Xcode version next to the SDK (only matters for App Store uploads)")
            slog(step_id, f"SDK cache kept at {appdirs.tilde(cached)}")
            cache = {"path": str(cached)}
        else:
            cache = {"path": str(SDK_CACHE / f"darwin-iPhoneOS{ctx.c['xcode']}.xtoolsdk")}
    # --repair re-registers the newest current cache and (re)installs the darwin tools and the macro server.
    if not DRY:
        os.utime(cache["path"])  # make ours the newest so --repair picks it
    ensure_legacy_venv(ctx, step_id)
    sprog(step_id, 0.62, "Registering the SDK and building its extras")

    def parse2(line, st):
        if "Building OpenAppleMacrosServer" in line:
            st["detail"], st["frac"] = "Building the Swift macro server (about 5 min, quiet)", None
        elif line.startswith("== ") or "sdk install" in line:
            st["detail"] = line.strip("= ")[:80]
        return None
    rc = run_stream([str(Path(ad) / "install-toolchain.sh"), "--mode", "full", "--repair"], step_id,
                    label="Registering the SDK and building its extras", estimate=420, env=benv, cwd=str(ad),
                    parse=parse2, frac_range=(0.62, 0.99), tail=tail)
    must(rc, "sdk-install", "Installing the SDK failed. The log has the details.", step_id, tail)


def i_app_integration(ctx: Ctx, step_id: str) -> None:
    inst = REPO / "install.sh"
    if not inst.exists():
        raise HelperError("missing", "The app's installer (install.sh) is missing from this copy of iOS Studio.")
    must(run_stream(["bash", str(inst)], step_id), "install", "Adding iOS Studio to the launcher failed.", step_id)


INSTALLERS = {
    "packages": lambda c, s: i_root_bundle(c, ["packages"], s, "Installing system packages"),
    "usbmuxd": lambda c, s: i_root_bundle(c, ["usbmuxd"], s, "Configuring the USB service"),
    "swift": lambda c, s: (i_root_bundle(c, ["packages", "swift", "swift-hold", "xcspec"], s, "Installing Swift"), i_swift_hook(c, s)),
    "swift-hold": lambda c, s: (i_root_bundle(c, ["swift-hold"], s, "Holding Swift"), i_swift_hook(c, s)),
    "xcspec": lambda c, s: i_root_bundle(c, ["xcspec"], s, "Fixing Swift's build settings"),
    "apple-dev": lambda c, s: i_apple_dev(c, s),
    "xtool": i_xtool,
    "rcodesign": i_rcodesign,
    "ipsw": i_ipsw,
    "pymobiledevice3": i_pymd3,
    "sdk": i_sdk,
    "uxplay": lambda c, s: i_root_bundle(c, ["uxplay"], s, "Installing UxPlay"),
    "debugger": lambda c, s: i_root_bundle(c, ["debugger"], s, "Installing debugger support"),
    "wireshark": lambda c, s: i_root_bundle(c, ["wireshark"], s, "Installing Wireshark"),
    "app-integration": i_app_integration,
}

ORDER = ["packages", "usbmuxd", "swift", "swift-hold", "xcspec", "apple-dev", "xtool", "rcodesign", "pymobiledevice3",
         "ipsw", "debugger", "sdk", "uxplay", "wireshark", "app-integration"]
ROOT_STEPS = {"packages", "usbmuxd", "swift", "swift-hold", "xcspec", "uxplay", "wireshark", "debugger"}
OPTIONAL_IN_ALL = {"ipsw", "debugger", "uxplay", "wireshark"}


def _streaming(fn):
    """Run a streaming handler: every HelperError becomes a done(false) event (never a bare fail object)."""
    def wrapper(flags):
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


def _run_one(ctx: Ctx, sid: str) -> None:
    s = ctx.by_id.get(sid)
    title = s["title"] if s else sid
    output.step(sid, title, "running")
    t0 = time.time()
    try:
        INSTALLERS[sid](ctx, sid)
    except HelperError as exc:
        output.step(sid, title, "failed", error=exc.to_json())
        raise
    sprog(sid, 1.0, "Done")
    output.step(sid, title, "done", elapsed=round(time.time() - t0))


@_streaming
def cmd_install(flags: dict) -> int:
    ctx = Ctx(flags)
    if DRY:
        slog(None, "Dry run: nothing will be changed; every command is printed instead.")
    if flags.get("all"):
        todo = [sid for sid in ORDER if sid in ctx.by_id and
                (ctx.by_id[sid]["canAutoInstall"] or (flags.get("force") and sid in INSTALLERS)) and
                (sid not in OPTIONAL_IN_ALL or flags.get("include_optional"))]
        if not todo:
            return output.done(True, {"installed": [], "summary": summarize(ctx.steps), "message": "Nothing to install."})
        big = [ctx.by_id[s]["title"] for s in todo if s in ("swift", "xtool", "sdk")]
        if big:
            confirm("Install " + ", ".join(ctx.by_id[s]["title"] for s in todo) + "? This downloads up to about "
                    f"{'2' if 'swift' in todo else '1'} GB and takes {'30–45' if 'sdk' in todo else '15–25'} minutes. "
                    "You'll be asked for your password once.", "Install everything", flags)
        # All root work first, with ONE password prompt.
        root_ids = [s for s in todo if s in ROOT_STEPS]
        if root_ids:
            for sid in root_ids:
                output.step(sid, ctx.by_id[sid]["title"], "running")
            try:
                i_root_bundle(ctx, root_ids, root_ids[0], "Installing system pieces")
            except HelperError as exc:
                for sid in root_ids:
                    output.step(sid, ctx.by_id[sid]["title"], "failed", error=exc.to_json())
                raise
            if "swift" in root_ids or "swift-hold" in root_ids:
                i_swift_hook(ctx, "swift-hold")
            for sid in root_ids:
                output.step(sid, ctx.by_id[sid]["title"], "done")
            if not DRY:
                ctx.refresh()
        installed = list(root_ids)
        for sid in [s for s in todo if s not in ROOT_STEPS]:
            if sid == "sdk" and not DRY and not ctx.by_id["sdk"]["canAutoInstall"]:
                output.step(sid, ctx.by_id[sid]["title"], "skipped", reason="needs the Xcode download")
                continue
            _run_one(ctx, sid)
            installed.append(sid)
            if not DRY:
                ctx.refresh()
        return output.done(True, {"installed": installed, "summary": summarize(ctx.steps), "dryRun": DRY})
    sid = flags.get("step")
    if not sid or sid is True:
        raise HelperError("usage", "Which step? Use --step <id> or --all.")
    if sid not in INSTALLERS:
        s = ctx.by_id.get(sid)
        if s and s["action"].get("kind") == "page":
            raise HelperError("interactive", f"“{s['title']}” is done by hand.", fix(s["action"].get("label", "Open"),
                              s["action"]["page"], action=s["action"].get("action")))
        raise HelperError("usage", f"There's no installer for '{sid}'.")
    if ctx.state(sid) == "done" and not flags.get("force"):
        output.step(sid, ctx.by_id[sid]["title"], "done", already=True)
        return output.done(True, {"installed": [], "step": ctx.by_id[sid], "message": "Already done."})
    if sid in ("swift", "xtool"):
        s = ctx.by_id[sid]
        confirm(f"Install {s['title']}? It needs {s['size']} and takes {s['time']}.", "Install", flags)
    _run_one(ctx, sid)
    if not DRY:
        ctx.refresh()
    return output.done(True, {"installed": [sid], "step": ctx.by_id.get(sid), "summary": summarize(ctx.steps), "dryRun": DRY})


# ------------------------------------------------------------------ setup xip

def cmd_xip(flags: dict) -> int:
    if flags.get("open"):
        F = gather()
        _steps, c = build_steps(F)
        rec = c["xcode"]
        url = DIRECT_XIP.format(v=rec) if "." in rec and not flags.get("page") else DOWNLOAD_PAGE.format(v=rec)
        if flags.get("dry_run"):
            return ok({"url": url, "opened": False})
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return ok({"url": url, "pageUrl": DOWNLOAD_PAGE.format(v=rec), "opened": True, "xcode": rec})
    if flags.get("use"):
        return _xip_use(flags)
    if flags.get("watch"):
        return _xip_watch(flags)
    raise HelperError("usage", "usage: setup xip --watch | --use PATH | --open")


def _xip_validate_event(path: Path, major: str, rec: str) -> dict:
    info = xip_quick(path)
    if not info.get("valid"):
        return info
    if not info.get("version"):
        v, b = xip_read_version(path, "xcode")
        info["version"], info["build"] = v, b
    info["matches"] = info["version"].split(".")[0] == major
    if not info["matches"]:
        info["valid"] = False
        info["problem"] = "wrong-version"
        info["message"] = f"This is Xcode {info['version']}, but your Swift needs Xcode {rec}. Download Xcode {rec} instead."
    return info


@_streaming
def _xip_use(flags: dict) -> int:
    p = Path(os.path.expanduser(str(flags["use"])))
    if not p.is_file():
        raise HelperError("not-found", "That file doesn't exist.")
    F = gather()
    _s, c = build_steps(F)
    output.step("xcode", "Checking Xcode", "running")
    info = _xip_validate_event(p, c["xcode_major"], c["xcode"])
    if not info.get("valid"):
        raise HelperError(info.get("problem", "bad-xip"), info.get("message", "That file can't be used."),
                          fix("Open download page", "setup", step="xcode"))
    save_state({"xip": str(p)})
    output.step("xcode", "Checking Xcode", "done")
    return output.done(True, {"xip": info})


@_streaming
def _xip_watch(flags: dict) -> int:
    F = gather()
    _s, c = build_steps(F)
    major, rec = c["xcode_major"], c["xcode"]
    expected = needed_xcode(c.get("swift_ver"))[2]
    interval = float(flags.get("interval") or 1.5)
    event("watching", dir=str(DOWNLOADS), needed=rec, expectedSize=expected)
    checked: dict[str, tuple] = {}
    sizes: dict[str, tuple[int, float]] = {}
    last_emit = 0.0
    # A file already there counts right away.
    while True:
        now = time.time()
        partials = []
        for pat in ("Xcode*.xip.part", "Xcode*.xip.crdownload", "*.crdownload", "Xcode*.xip.download", "Xcode*.xip.tmp"):
            partials += [p for p in DOWNLOADS.glob(pat) if p.is_file()]
        partials = sorted(set(partials), key=lambda p: p.stat().st_mtime, reverse=True)
        if partials:
            p = partials[0]
            size = p.stat().st_size
            prev = sizes.get(str(p))
            rate = (size - prev[0]) / max(0.1, now - prev[1]) if prev else 0
            sizes[str(p)] = (size, now)
            if now - last_emit > 1:
                last_emit = now
                eta = (expected - size) / rate if rate > 0 and expected > size else None
                event("download", file=p.name, size=size, expected=expected, fraction=min(0.99, size / expected) if expected else None,
                      rate=rate, eta=None if eta is None else int(eta))
                sprog("xcode", min(0.99, size / expected) if expected else None,
                      f"Downloading {p.name.split('.xip')[0]}: {fmt_bytes(size)} of about {fmt_bytes(expected)}", eta)
        for p in find_xips():
            st = p.stat()
            key = (st.st_size, st.st_mtime)
            if checked.get(str(p)) == key:
                continue
            # wait until the size is stable (browser finished writing)
            prev = sizes.get("final:" + str(p))
            sizes["final:" + str(p)] = (st.st_size, now)
            if not prev or prev[0] != st.st_size or now - st.st_mtime < 2:
                continue
            checked[str(p)] = key
            event("found", path=str(p), file=p.name, size=st.st_size)
            output.step("xcode", "Checking Xcode", "running")
            try:
                info = _xip_validate_event(p, major, rec)
            except HelperError as exc:
                info = {"path": str(p), "name": p.name, "valid": False, "problem": "bad-xip", "message": exc.message}
            if info.get("valid"):
                save_state({"xip": str(p)})
                output.step("xcode", "Checking Xcode", "done")
                return output.done(True, {"xip": info})
            output.step("xcode", "Checking Xcode", "failed", error={"code": info.get("problem"), "message": info.get("message")})
            event("rejected", xip=info, message=info.get("message"))
        time.sleep(interval)


# ------------------------------------------------------------------ toolchain info

def cmd_info(flags: dict) -> int:
    F = gather()
    steps, c = build_steps(F)
    by = {s["id"]: s for s in steps}

    def item(id_, name, version, detail, status, **kw):
        d = {"id": id_, "name": name, "version": version, "detail": detail, "status": status}
        d.update(kw)
        return d

    def st_of(sid):
        return {"done": "ok", "missing": "missing", "broken": "error", "optional": "missing", "unknown": "warn"}[by[sid]["state"]]
    xv = (F["xtool_ver"][1].strip().splitlines() or [""])[0]
    xt_real = os.path.realpath(F["xtool"]) if F["xtool"] else None
    swift_ver = c.get("swift_ver")
    qi = F["swift_qi"][1]
    pkgv = re.search(r"^Version\s*:\s*(\S+)", qi, re.M)
    items = [
        item("xtool", "xtool", xv.split()[-1][:7] if xv else None, appdirs.tilde(xt_real) if xt_real else "Not installed",
             st_of("xtool"), hasFix=F["xtool_old_ua"] is False, step="xtool"),
        item("swift", "Swift", swift_ver, f"swift-bin {pkgv.group(1)}" if pkgv else "Not installed", st_of("swift"),
             held="swift-bin" in F["ignore"][1].split(), step="swift"),
        item("sdk", "iOS SDK", (sdk_ios_versions(c["bundle"]) or [None])[-1] if c.get("bundle") else None,
             by["sdk"]["detail"], st_of("sdk"), path=appdirs.tilde(c["bundle"]) if c.get("bundle") else None,
             stamp=sdk_stamp(c["bundle"]) if c.get("bundle") else None, wantedStamp=c["stamp"], step="sdk"),
        item("pymobiledevice3", "pymobiledevice3", c["pymd3"][1] if c.get("pymd3") else None,
             (f"Python {c['pymd3'][2]} · {appdirs.tilde(Path(c['pymd3'][0]).parent.parent)}" if c.get("pymd3") else by["pymobiledevice3"]["detail"]),
             st_of("pymobiledevice3"), pinned=PYMD3_VERSION, step="pymobiledevice3"),
        item("rcodesign", "rcodesign", _first_ver(F["rcodesign"][1]) if F["rcodesign"][0] == 0 else None,
             "Signs App Store builds", st_of("rcodesign"), pinned=RCODESIGN_VERSION, step="rcodesign"),
        item("ipsw", "ipsw", _first_ver(F["ipsw"][1]) if F["ipsw"][0] == 0 else None, "Debugger helper (optional)",
             "ok" if F["ipsw"][0] == 0 else "missing", pinned=IPSW_VERSION, step="ipsw", optional=True),
        item("apple-dev", "omarchy-apple-dev", (c.get("apple_dev_head") or "")[:7] or None,
             appdirs.tilde(c["apple_dev"]) if c.get("apple_dev") else "Not downloaded", st_of("apple-dev"),
             pinned=APPLE_DEV_COMMIT[:7], step="apple-dev"),
    ]
    caches = [dict(x, path=appdirs.tilde(x["path"])) for x in c.get("caches", [])]
    return ok({"items": items, "sdks": {"installed": sdk_ios_versions(c["bundle"]) if c.get("bundle") else [],
                                        "caches": caches, "xcodeNeeded": c["xcode"]},
               "summary": summarize(steps)})


def _first_ver(text: str) -> str | None:
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", text)
    return m.group(1) if m else (text.strip().splitlines() or [None])[0]


# ------------------------------------------------------------------ toolchain disk / clean

PRUNE = {"node_modules", "Pods", "vendor", "build", "dist", "target", "DerivedData", "venv", "__pycache__"}


def project_build_dirs(max_depth: int = 5) -> list[Path]:
    """`.build` folders of xtool projects (Package.swift + xtool.yml) under the configured project roots."""
    roots = appdirs.project_roots()
    out: set[Path] = set()
    deadline = time.time() + 3
    for r in roots:
        r = Path(os.path.expanduser(r))
        base = len(r.parts)
        for dirpath, dirnames, filenames in os.walk(r):
            if time.time() > deadline:
                break
            here = Path(dirpath)
            if "xtool.yml" in filenames and "Package.swift" in filenames and (here / ".build").is_dir():
                out.add(here / ".build")
            if len(here.parts) - base >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in PRUNE]
    return sorted(out)


def _stale_pid_dirs() -> list[Path]:
    out = []
    for p in SDK_CACHE.glob("pid-*"):
        m = re.match(r"pid-(\d+)-", p.name)
        if m and not Path(f"/proc/{m.group(1)}").exists():
            out.append(p)
    return out


def disk_items() -> list[dict]:
    """Every piece with its paths. `clean` deletes only items with cleanable=True, and only their paths."""
    xt_real = Path(os.path.realpath(LOCAL_BIN / "xtool")) if (LOCAL_BIN / "xtool").exists() else None
    active_lib = xt_real.parent if xt_real else None
    ad = find_apple_dev()
    stamp = apple_dev_pins(ad)["XTOOL_SDK_VERSION"]
    bundle = appdirs.sdk_bundle()
    reg_vers = sdk_ios_versions(bundle) if bundle else []
    caches = sdk_caches(stamp)
    good_caches = [Path(x["path"]) for x in caches if x["current"] and x["complete"] and (not reg_vers or x["ios"] in reg_vers)]
    old_caches = [Path(x["path"]) for x in caches if Path(x["path"]) not in good_caches]
    old_caches += _stale_pid_dirs() + [p for p in SDK_CACHE.glob(".build.*")]
    sdk_ok_now = bool(bundle and sdk_stamp(bundle) == stamp and reg_vers)
    xips = [p for p in find_xips()]
    superseded_libs = [p for p in APPLE_DEV_LIB.glob("xtool-*") if p.is_dir() and p != active_lib]
    # source trees of superseded builds (same naming: lib/xtool-X <-> cache/xtool-X)
    superseded_src = [APPLE_DEV_CACHE / p.name for p in superseded_libs if (APPLE_DEV_CACHE / p.name).is_dir()]
    build_trees = [p / ".build" for p in APPLE_DEV_CACHE.glob("*") if (p / ".build").is_dir() and p not in superseded_src]
    if ad and (ad / "tools/darwin-tools/.build").is_dir():
        build_trees.append(ad / "tools/darwin-tools/.build")
    venvs = [p for p in (VENV_DIR,) if p.exists()] + ([LEGACY_VENV] if LEGACY_VENV.exists() and not LEGACY_VENV.is_symlink() else [])
    items = [
        dict(id="sdk", name="iOS SDK (in use)", paths=[bundle] if bundle else [], cleanable=False,
             note="Needed to build apps. Remove it with “Remove everything”."),
        dict(id="sdk-cache", name="Spare SDK copy", paths=good_caches, cleanable=bool(good_caches),
             safe=False, note="Lets iOS Studio repair the SDK without the Xcode download. Deleting it is safe, "
             "but a later repair needs Xcode again."),
        dict(id="sdk-cache-old", name="Old SDK copies and leftovers", paths=old_caches, cleanable=True, safe=True,
             note="Built for another Xcode or by an older xtool. Not used."),
        dict(id="xip", name="Xcode download (.xip)", paths=xips, cleanable=bool(xips), safe=sdk_ok_now and bool(good_caches),
             note=("Not needed any more: the SDK is built and a spare copy exists." if sdk_ok_now and good_caches else
                   "Still needed until the SDK is built." if not sdk_ok_now else
                   "The SDK is built, but there's no spare copy: a repair would need this file.")),
        dict(id="build-trees", name="Build leftovers (xtool, macro server)", paths=build_trees, cleanable=bool(build_trees), safe=True,
             note="Compiler output kept from installing xtool. Only needed to rebuild faster."),
        dict(id="xtool-old", name="Old xtool builds", paths=superseded_libs + superseded_src, cleanable=True, safe=True,
             note="xtool versions that are no longer used."),
        dict(id="aur", name="Swift download and package", paths=[AUR_DIR] if AUR_DIR.exists() else [], cleanable=True, safe=True,
             note="Kept from installing Swift. Not needed once Swift is installed."),
        dict(id="project-builds", name="Project build folders", paths=project_build_dirs(), cleanable=True, safe=True,
             note="Your apps' .build folders. Safe to delete; the next build takes a few minutes longer."),
        dict(id="xtool", name="xtool", paths=[p for p in (active_lib,) if p], cleanable=False,
             note="In use. (Its source code and build leftovers are listed separately.)"),
        dict(id="venv", name="pymobiledevice3", paths=venvs, cleanable=False, note="In use."),
        dict(id="tools", name="rcodesign and ipsw", paths=[p for p in (LOCAL_BIN / "rcodesign", LOCAL_BIN / "ipsw") if p.exists()],
             cleanable=False, note="Small helper programs."),
        dict(id="swift", name="Swift toolchain", paths=[toolchain_root()] if toolchain_root() else [], cleanable=False,
             note="System package (swift-bin)."),
    ]
    return items


def cmd_disk(flags: dict) -> int:
    if flags.get("cached"):
        c = appdirs.cache_get("toolchain-disk")
        if c:
            return ok(dict(c["data"], cachedAt=c["at"], stale=True))
    items = disk_items()
    all_paths = {str(p) for it in items for p in it["paths"]}
    with ThreadPoolExecutor(max_workers=8) as ex:
        sizes = dict(zip(all_paths, ex.map(lambda s: du(Path(s)), all_paths)))
    out = []
    for it in items:
        ps = [{"path": appdirs.tilde(p), "size": sizes.get(str(p), 0)} for p in it["paths"]]
        out.append({"id": it["id"], "name": it["name"], "size": sum(x["size"] for x in ps), "paths": ps,
                    "cleanable": bool(it["cleanable"] and ps and sum(x["size"] for x in ps) > 0), "safe": it.get("safe", False),
                    "note": it["note"]})
    data = {"items": out, "total": sum(i["size"] for i in out),
            "reclaimable": sum(i["size"] for i in out if i["cleanable"] and i["safe"]),
            "free": free_bytes(HOME)}
    appdirs.cache_put("toolchain-disk", data)
    return ok(data)


@_streaming
def cmd_clean(flags: dict) -> int:
    item_id = flags.get("item")
    items = {i["id"]: i for i in disk_items()}
    it = items.get(item_id)
    if not it or not it["cleanable"]:
        raise HelperError("usage", "That item can't be cleaned.")
    if item_id == "xip":
        extra_roots = (DOWNLOADS,) + tuple(p.parent for p in it["paths"])
    elif item_id == "project-builds":
        extra_roots = tuple(p.parent for p in it["paths"])
    elif item_id == "build-trees":
        extra_roots = tuple(p.parent for p in it["paths"])
    else:
        extra_roots = ()
    if DRY:
        slog(None, "Dry run: nothing will be deleted.")
    output.step(item_id, it["name"], "running")
    freed = 0
    for i, p in enumerate(it["paths"]):
        # each path must still match its item's shape
        if item_id == "xip" and not (p.suffix == ".xip" and p.name.lower().startswith("xcode")):
            continue
        if item_id in ("project-builds", "build-trees") and p.name != ".build":
            continue
        sprog(item_id, i / max(1, len(it["paths"])), f"Deleting {appdirs.tilde(p)}")
        freed += do_rmtree(p, item_id, inside=extra_roots)
    if item_id == "xip" and not DRY:
        st = load_state()
        if st.get("xip") and not Path(st["xip"]).exists():
            save_state({"xip": None, "xipInfo": None})
    output.step(item_id, it["name"], "done")
    try:
        (CACHE_DIR / "toolchain-disk.json").unlink()
    except OSError:
        pass
    return output.done(True, {"item": item_id, "freed": freed, "dryRun": DRY})


@_streaming
def cmd_remove_all(flags: dict) -> int:
    """Mirror of `omarchy-remove-dev-env ios` (PR #14255), complete: also ipsw, our venv, caches, the hold,
    the hook and the usbmuxd drop-in. Keeps Apple sign-in (~/.config/xtool) unless --remove-auth, keeps
    pairing records always, keeps system packages that other software uses."""
    if DRY:
        slog(None, "Dry run: nothing will be removed.")
    confirm("Remove the whole iOS toolchain (Swift, xtool, the iOS SDK, caches)? Your projects, your iPhone pairing"
            + ("" if flags.get("remove_auth") else " and your Apple sign-in") + " stay.", "Remove everything", flags)
    sid = "remove-all"
    output.step(sid, "Removing the iOS toolchain", "running")
    xt = LOCAL_BIN / "xtool"
    sprog(sid, 0.05, "Unregistering the iOS SDK")
    if Path("/usr/bin/swift").exists():
        run_stream(["/usr/bin/swift", "sdk", "remove", "darwin"], sid)
    for b in appdirs.sdk_bundles():
        do_rmtree(b, sid)
    steps = [
        ("SDK copies", [SDK_CACHE]),
        ("xtool builds", [APPLE_DEV_LIB, APPLE_DEV_CACHE]),
        ("iOS build scripts", [APPLE_DEV_HOME]),
        ("pymobiledevice3", [VENV_DIR]),
        ("iOS Studio downloads", [AUR_DIR]),
    ]
    for i, (label, paths) in enumerate(steps):
        sprog(sid, 0.1 + 0.5 * i / len(steps), f"Removing {label}")
        for p in paths:
            do_rmtree(p, sid)
    # ~/pymobile3-venv (omarchy-apple-dev's venv path): its uninstall removes it too. Our symlink always; a real one only if it isn't someone else's.
    if LEGACY_VENV.is_symlink():
        do_unlink(LEGACY_VENV, sid)
    elif LEGACY_VENV.is_dir():
        do_rmtree(LEGACY_VENV, sid, exact=(LEGACY_VENV,))
    for b in ("xtool", "rcodesign", "ipsw"):
        p = LOCAL_BIN / b
        if p.is_symlink() or p.is_file():
            do_unlink(p, sid)
    do_unlink(OMARCHY_HOOK, sid)
    if flags.get("remove_auth"):
        sprog(sid, 0.65, "Signing out of Apple")
        if xt.exists():
            run_stream([xt, "auth", "logout"], sid)
        do_rmtree(XTOOL_CONFIG, sid, exact=(XTOOL_CONFIG,))
    root_args = ["--remove-usbmuxd-dropin"]
    if not flags.get("keep_swift"):
        root_args = ["--unhold-swift", "--remove-swift"] + root_args
    sprog(sid, 0.7, "Removing system pieces (asks for your password)")
    run_root(root_args, sid, "Removing Swift")
    save_state({"xip": None, "xipInfo": None})
    output.step(sid, "Removing the iOS toolchain", "done")
    return output.done(True, {"removed": True, "keptAuth": not flags.get("remove_auth"), "dryRun": DRY})


GROUPS = {
    "setup": {"check": cmd_check, "install": cmd_install, "xip": cmd_xip},
    "toolchain": {"info": cmd_info, "disk": cmd_disk, "clean": cmd_clean, "remove-all": cmd_remove_all},
}
