"""debug.start, debug.stop, debug.symbols (+ debugSymbols events): LLDB on your own apps.

debug.start forwards the phone's debugserver (com.apple.internal.dt.remote.debugproxy) to a
loopback port through the daemon's own tunnel (Session.port_forward, never a second tunnel) and
finds or launches the app's process over the shared DVT connection. The helper (helper/debugger.py)
then runs lldb against that port. The forward is a reference-counted resource: it closes on
debug.stop, when the client that asked for it disconnects, when the tunnel drops, and after an
hour without a new debug.start.

debug.symbols downloads the phone's dyld shared cache once per iOS build (about 7 GB, over the
tunnel) into ~/.cache/ios-studio/DeviceSupport/<ios> (<build>)/dsc and unpacks the Swift runtime and
the system libraries lldb needs (SWIFT_PREFIX, SYSTEM_LIBS) with ipsw into .../Symbols, so Linux
lldb reads Swift values right and attaching doesn't look every system symbol up over the
connection. The download is deleted afterwards unless the user keeps it (keepDownload), so the set
can be unpacked again without downloading. Each set is marked with its FORMAT; an older one still
works but reports `outdated` so the UI offers an update.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
from pymobiledevice3.services.remote_fetch_symbols import RemoteFetchSymbolsService

import common
from common import fix
from deviced_core import dlog, emit, err

DEBUGPROXY = "com.apple.internal.dt.remote.debugproxy"
FORWARD_TIMEOUT = 3600          # seconds a forward lives without a new debug.start
SYMBOLS_ROOT = common.CACHE_DIR / "DeviceSupport"
SETTINGS = common.STATE_DIR / "debug-symbols.json"   # {"keepDownload": bool}
DONE_MARK = ".ios-studio-complete"   # Symbols/<this>: JSON {format, libs, failed, ios, build, created}
DSC_MARK = ".ios-studio-downloaded"  # dsc/<this>: JSON {bytes, files, ios, build, created}, the download is whole
SPACE_MARGIN = 1 << 30          # free space kept on top of the download size (and all an unpack needs)
WORKERS = 4                     # concurrent file streams, as pymobiledevice3 uses
UNPACKERS = 4                   # concurrent `ipsw dyld extract` runs (each takes about 2 s)
ESTIMATE = 7 << 30              # shown before the phone has been asked for the file list
IPSW_FIX = fix("Install ipsw", "setup", step="ipsw")

# Bump FORMAT whenever what gets unpacked changes. A set with a lower format (or the plain-text
# marker of format 1) still works, but status reports it `outdated` so the UI offers an update.
#   1: the Swift runtime and libobjc only
#   2: + the system libraries below
FORMAT = 2

# Unpacked out of the dyld shared cache into Symbols/. lldb reads these from the sysroot instead of
# out of the phone's memory, which over Wi-Fi is slow. Paths as `ipsw dyld info <cache> --dylibs`
# prints them.
SWIFT_PREFIX = "/usr/lib/swift/"         # the Swift runtime and overlays: String, Array, ... read right
SYSTEM_LIBS = (
    "/usr/lib/libobjc.A.dylib",                  # Objective-C runtime: class names, Swift/ObjC bridging
    # While attaching, debugserver asks lldb for dispatch_queue_offsets once per loaded image (about
    # a thousand qSymbol round trips). Without libdispatch here lldb can't answer and attaching takes
    # minutes over Wi-Fi.
    "/usr/lib/system/libdispatch.dylib",
    "/usr/lib/system/libsystem_pthread.dylib",   # thread start frames, thread names
    "/usr/lib/system/libsystem_kernel.dylib",    # syscalls: the top frame of every waiting thread
    "/usr/lib/system/libsystem_platform.dylib",  # memcpy, locks, atomics
    "/usr/lib/system/libsystem_c.dylib",         # abort, printf, malloc callers
    "/usr/lib/system/libdyld.dylib",             # the loader: the bottom frame of the main thread
    "/usr/lib/libc++.1.dylib",                   # the C++ runtime under many frameworks
    "/usr/lib/libc++abi.dylib",                  # C++ exceptions (__cxa_throw)
)
# Without these the set is incomplete. libswiftCore failing to unpack fails the whole run.
REQUIRED = ("usr/lib/swift/libswiftCore.dylib", "usr/lib/system/libdispatch.dylib")
SLOW_ATTACH = "so attaching the debugger can take minutes over Wi-Fi and the app stays paused that long"


def symbols_dir(ios: str | None, build: str | None) -> Path | None:
    if not ios or not build:
        return None
    safe = re.sub(r"[^A-Za-z0-9.]", "", f"{ios}") + " (" + re.sub(r"[^A-Za-z0-9]", "", f"{build}") + ")"
    return SYMBOLS_ROOT / safe


def ipsw_path() -> str | None:
    local = common.HOME / ".local/bin/ipsw"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("ipsw")


def free_bytes(p: Path) -> int:
    for d in (p, *p.parents):
        if d.exists():
            with contextlib.suppress(OSError):
                return shutil.disk_usage(d).free
    return 0


def keep_download() -> bool:
    return bool((common.load_json(SETTINGS, {}) or {}).get("keepDownload"))


def set_keep_download(on: bool) -> None:
    common.save_json(SETTINGS, {**(common.load_json(SETTINGS, {}) or {}), "keepDownload": bool(on)})


def wanted_libs(cache_paths) -> list[str]:
    """The paths of one cache's dylib list that get unpacked."""
    return sorted({p for p in cache_paths if p.startswith(SWIFT_PREFIX) or p in SYSTEM_LIBS})


def read_mark(sym: Path) -> dict | None:
    """The set's marker: None when there's no finished set, {"format": 1} for the old plain-text one."""
    p = sym / DONE_MARK
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = None
    return d if isinstance(d, dict) else {"format": 1}


def lib_names(rels: list[str], limit: int = 3) -> str:
    """'libdispatch, libsystem_c and 4 more libraries' from relative dylib paths."""
    names = [r.rsplit("/", 1)[-1].removesuffix(".dylib") for r in rels]
    if len(names) <= limit:
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more libraries"


def recover(base: Path) -> None:
    """Finish a swap that stopped between its two renames (see replace_set)."""
    sym, old = base / "Symbols", base / "Symbols.old"
    if old.exists() and not sym.exists():
        old.rename(sym)
    elif old.exists():
        shutil.rmtree(old, True)


def replace_set(new: Path, sym: Path) -> None:
    """Put the freshly unpacked set in place. The old one stays usable until the moment of the swap."""
    old = sym.with_name("Symbols.old")
    shutil.rmtree(old, True)
    if sym.exists():
        sym.rename(old)
    new.rename(sym)
    shutil.rmtree(old, True)


def assess(base: Path | None) -> dict:
    """What's on disk for one iOS build: the unpacked set and any download. Only reads."""
    out = {"state": "missing", "ready": False, "update": {"needed": False, "reason": None, "action": None},
           "libs": 0, "format": None, "downloadKept": 0, "canUnpack": False, "sizeOnDisk": 0}
    if base is None:
        return out
    sym, dsc = base / "Symbols", base / "dsc"
    if not sym.exists() and (base / "Symbols.old").exists():
        sym = base / "Symbols.old"          # a swap that didn't finish
    if dsc.is_dir():
        dm = common.load_json(dsc / DSC_MARK)
        whole = isinstance(dm, dict) and main_cache(dsc) is not None
        out["downloadKept"] = int(dm.get("bytes") or 0) if whole else common.dir_size(dsc)
        out["canUnpack"] = whole
    out["update"]["action"] = "unpack" if out["canUnpack"] else "download"
    mark = read_mark(sym)
    if mark is None:
        return out
    libs = [x for x in mark.get("libs") or [] if isinstance(x, str)]
    fmt = int(mark.get("format") or 1)
    out["format"] = fmt
    out["libs"] = sum(1 for x in libs if (sym / x).is_file()) if libs else sum(1 for _ in sym.rglob("*.dylib"))
    out["sizeOnDisk"] = common.dir_size(sym)
    out["ready"] = (sym / "usr/lib/swift/libswiftCore.dylib").is_file()
    if fmt < FORMAT:
        out["state"] = "outdated"
        out["update"].update(needed=True, reason="These symbols were unpacked by an older version of iOS Studio "
                             f"and miss system libraries, {SLOW_ATTACH}.")
        return out
    missing = [x for x in libs if not (sym / x).is_file()]
    missing += [x for x in REQUIRED if x not in libs and not (sym / x).is_file()]
    missing += [x for x in mark.get("failed") or [] if isinstance(x, str) and x not in missing]
    if missing:
        out["state"] = "incomplete"
        verb = "is" if len(missing) == 1 else "are"
        out["update"].update(needed=True, reason=f"{lib_names(missing)} {verb} missing from these symbols, "
                             f"{SLOW_ATTACH}." if any("dispatch" in m for m in missing) else
                             f"{lib_names(missing)} {verb} missing from these symbols, so frames in "
                             f"{'it' if len(missing) == 1 else 'them'} show only the library name.")
        out["missing"] = missing
        return out
    out["state"] = "ready"
    return out


def symbols_ready(base: Path | None) -> bool:
    """A usable set (also an outdated or incomplete one: lldb uses whatever is there)."""
    return assess(base)["ready"]


class Symbols:
    """One download or unpack at a time (the active iPhone's iOS build)."""
    KEY = "debug.symbols"

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.state: dict = {"phase": "idle"}
        self.sizes: dict[str, int] = {}     # "<ios> (<build>)" -> bytes the phone reported
        self._last_emit = 0.0

    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def publish(self, force: bool = True, **fields):
        self.state = {**self.state, **fields, "ts": time.time()}
        now = time.monotonic()
        if force or now - self._last_emit >= 0.5:
            self._last_emit = now
            emit("debugSymbols", dict(self.state))

    async def stop(self, *_):
        t, self.task = self.task, None
        if t and not t.done():
            t.cancel()
            with contextlib.suppress(BaseException):
                await t

    async def run(self, d, s, base: Path, files: list | None, total: int):
        """files None: unpack the download already in dsc/ again."""
        dsc, sym, new = base / "dsc", base / "Symbols", base / "Symbols.new"
        ios, build = s.info.get("ios"), s.info.get("build")
        try:
            await asyncio.to_thread(recover, base)
            if files is not None:
                await self._download(s, dsc, files, total)
                common.save_json(dsc / DSC_MARK, {"bytes": common.dir_size(dsc), "files": len(files), "ios": ios,
                                                  "build": build, "created": time.strftime("%Y-%m-%dT%H:%M:%S")})
            await asyncio.to_thread(shutil.rmtree, new, True)
            mark = await self._extract(dsc, new)
            common.save_json(new / DONE_MARK, {**mark, "ios": ios, "build": build,
                                               "created": time.strftime("%Y-%m-%dT%H:%M:%S")})
            await asyncio.to_thread(replace_set, new, sym)
            if not keep_download():
                await asyncio.to_thread(shutil.rmtree, dsc, True)
            a = assess(base)
            self.publish(phase="done", fraction=1.0, ready=True, sizeOnDisk=a["sizeOnDisk"], libs=a["libs"],
                         message="Debug symbols are ready." if a["state"] == "ready" else a["update"]["reason"])
        except asyncio.CancelledError:
            await asyncio.to_thread(shutil.rmtree, new, True)
            if not (dsc / DSC_MARK).is_file():      # a cancelled download frees its space; a whole one stays
                await asyncio.to_thread(shutil.rmtree, dsc, True)
            self.publish(phase="cancelled", message="Cancelled. The symbols you had before are unchanged."
                         if read_mark(sym) else "Cancelled.", ready=symbols_ready(base))
            raise
        except BaseException as exc:  # noqa: BLE001
            dlog(f"symbols: {exc!r}")
            e = exc if isinstance(exc, common.HelperError) else err(
                "failed", "Downloading the debug symbols failed." if files is not None and not (dsc / DSC_MARK).is_file()
                else "Unpacking the debug symbols failed.", None, f"{type(exc).__name__}: {exc}")
            # The download stays: a partial one resumes, a whole one can be unpacked again.
            await asyncio.to_thread(shutil.rmtree, new, True)
            self.publish(phase="failed", message=e.message, error=e.to_json(), ready=symbols_ready(base))
        finally:
            self.task = None
            d.resources.forget(self.KEY)

    async def _download(self, s, out: Path, files: list, total: int):
        queue: asyncio.Queue = asyncio.Queue()
        got = [0]
        t0 = time.monotonic()
        async with s.open_service(RemoteFetchSymbolsService) as svc:
            files = await svc.get_dsc_file_list()   # the file streams belong to the listing on this connection
            total = sum(f.file_size for f in files) or total
            for i, f in enumerate(files):
                target = out / f.file_path.lstrip("/")
                if target.is_file() and target.stat().st_size == f.file_size:
                    got[0] += f.file_size       # kept, or left by an interrupted run
                    continue
                queue.put_nowait(i)
            if not queue.empty():
                (out / DSC_MARK).unlink(missing_ok=True)
            self.publish(phase="downloading", received=got[0], total=total, fraction=got[0] / total if total else 0,
                         message="Copying system libraries from your iPhone…")
            start = got[0]

            async def worker():
                while True:
                    try:
                        i = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    f = files[i]
                    target = (out / f.file_path.lstrip("/")).resolve()
                    if out.resolve() not in target.parents:
                        raise err("failed", "The iPhone sent an unexpected file name.", None, f.file_path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "wb") as fh:
                        async for chunk in svc.service.iter_file_chunks(f.file_size, file_idx=i):
                            fh.write(chunk)
                            got[0] += len(chunk)
                            el = max(0.001, time.monotonic() - t0)
                            rate = (got[0] - start) / el
                            self.publish(False, received=got[0], total=total,
                                         fraction=min(1.0, got[0] / total) if total else None, rate=int(rate),
                                         eta=int((total - got[0]) / rate) if rate > 0 else None)
            await asyncio.gather(*(worker() for _ in range(min(WORKERS, max(1, queue.qsize())))))

    async def _extract(self, dsc: Path, sym: Path) -> dict:
        """Unpack wanted_libs() into sym. Returns the marker's {format, libs, failed}."""
        ipsw = ipsw_path()
        if not ipsw:
            raise err("needs-ipsw", "ipsw is needed to unpack the symbols. Install it in Setup, then try again.", IPSW_FIX)
        cache = main_cache(dsc)
        if cache is None:
            raise err("failed", "The iPhone's system library cache wasn't in the download.")
        self.publish(phase="extracting", fraction=None, message="Unpacking system libraries…", done=0)
        proc = await asyncio.create_subprocess_exec(ipsw, "dyld", "info", str(cache), "--dylibs",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        listed = {m.group(0) for m in re.finditer(r"/\S+$", out.decode(errors="replace"), re.M)}
        paths = wanted_libs(listed)
        if not any(p.startswith(SWIFT_PREFIX) for p in paths):
            raise err("failed", "ipsw couldn't read the iPhone's system library cache.", None,
                      out.decode(errors="replace")[-600:])
        absent = [p for p in SYSTEM_LIBS if p not in listed]
        if absent:
            dlog(f"symbols: not in this cache: {', '.join(absent)}")
        done, failed, n = [], [], [0]
        gate = asyncio.Semaphore(UNPACKERS)

        async def one(p: str):
            rel = p.lstrip("/")
            dest = sym / rel.rsplit("/", 1)[0]
            dest.mkdir(parents=True, exist_ok=True)
            async with gate:
                # one dylib per run: `ipsw dyld extract` takes a single name (or --all, ~3,000 of them)
                x = await asyncio.create_subprocess_exec(ipsw, "dyld", "extract", str(cache), p.rsplit("/", 1)[1],
                                                         "--slide", "-o", str(dest), stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.PIPE)
                _, e = await x.communicate()
            if (sym / rel).is_file():
                done.append(rel)
            else:
                failed.append(rel)
                dlog(f"symbols: ipsw extract {p} ({x.returncode}): {e.decode(errors='replace')[-300:]}")
            n[0] += 1
            self.publish(False, phase="extracting", done=n[0], count=len(paths), fraction=n[0] / len(paths),
                         message=f"Unpacking system libraries ({n[0]} of {len(paths)})…")
        await asyncio.gather(*(one(p) for p in paths))
        if "usr/lib/swift/libswiftCore.dylib" not in done:
            raise err("failed", "Unpacking the Swift runtime failed.", None, "libswiftCore.dylib missing after extract")
        return {"format": FORMAT, "libs": sorted(done), "failed": sorted(failed)}


def main_cache(dsc: Path) -> Path | None:
    """The iOS system library cache among the downloaded ones.

    Since iOS 16 the download also holds smaller caches with the same file name (DriverKit and,
    on newer phones, ExclaveKit). They have no Swift runtime, so pick the one under
    System/Library/Caches/com.apple.dyld, else the one with the most subcache files next to it."""
    found = [p for p in dsc.rglob("dyld_shared_cache_arm64*") if p.is_file() and "." not in p.name]
    if not found:
        return None
    def rank(p: Path):
        s = p.as_posix()
        main = "/System/Library/Caches/com.apple.dyld/" in s and "DriverKit" not in s and "Exclave" not in s
        subcaches = sum(1 for q in p.parent.glob(p.name + ".*"))
        return (main, p.name.endswith("arm64e"), subcaches)
    return max(found, key=rank)


async def find_pid(s, bid: str) -> int:
    async def go():
        async with s.dvt_channel(ProcessControl) as pc:
            return int(await pc.process_identifier_for_bundle_identifier(bid) or 0)
    return await s.dvt_retry(go)


async def launch_suspended(s, bid: str, args: list, env: dict) -> int:
    async def go():
        async with s.dvt_channel(ProcessControl) as pc:
            return int(await pc.launch(bid, arguments=args, kill_existing=True, start_suspended=True,
                                       environment=env))
    return await s.dvt_retry(go)


def symbols_status(sym: Symbols, ios: str | None, build: str | None) -> dict:
    """The debug.symbols status reply (see docs/deviced-protocol.md#debugging)."""
    base = symbols_dir(ios, build)
    label = f"{ios} ({build})" if ios and build else None
    need = sym.sizes.get(label or "", 0)
    out = {"ios": ios, "build": build, "dir": str(base) if base else None, "sysroot": str(base) if base else None,
           **assess(base), "downloading": sym.running(), "estimatedBytes": need or ESTIMATE, "estimated": not need,
           "freeBytes": free_bytes(SYMBOLS_ROOT), "ipsw": bool(ipsw_path()), "keepDownload": keep_download(),
           "currentFormat": FORMAT}
    mine = sym.state.get("ios") == ios and sym.state.get("build") == build
    phase = sym.state.get("phase") if mine else None
    if sym.running():
        out["state"] = "extracting" if phase == "extracting" else "downloading"
    elif phase == "failed" and out["state"] in ("missing", "outdated", "incomplete"):
        out["error"] = sym.state.get("error")
        if out["state"] == "missing":
            out["state"] = "failed"
    if sym.running() or phase in ("failed", "cancelled"):
        out["progress"] = dict(sym.state)
    return out


def register(reg):
    sym = Symbols()

    def status(s) -> dict:
        return symbols_status(sym, s.info.get("ios"), s.info.get("build"))

    @reg.command("debug.start")
    async def c_start(d, a):
        bid = str(a.get("bundleId") or "")
        if not bid:
            raise err("bad-args", "Which app? (bundleId missing)")
        launch = str(a.get("launch") or "auto")
        if launch not in ("attach", "suspended", "auto", "none"):   # none: the forward only (diagnostics)
            raise err("bad-args", "launch must be attach, suspended, auto or none.")
        s, rsd = await d.rsd(a)
        apps = await d.pmd3.InstallationProxyService(lockdown=rsd).get_apps(
            application_type="Any", bundle_identifiers=[bid])
        info = apps.get(bid)
        if not info:
            raise err("not-found", "That app isn't installed on your iPhone.")
        signer = str(info.get("SignerIdentity") or "")
        if info.get("ApplicationType") == "System" or (signer and not signer.startswith(("Apple Development", "iPhone Developer"))):
            raise err("not-debuggable", "Only your own apps can be debugged: apps you built and signed for "
                      "development. App Store and system apps can't be.")
        exe = info.get("CFBundleExecutable") or ""
        remote = (info.get("Path") or "").rstrip("/") + ("/" + exe if exe else "")
        key = f"debug:{bid}"
        client = d.client()
        if d.resources.held(key) and not d.resources.holds(key, client):
            raise err("busy", "A debugger is already attached to this app (in another window or an AI agent). "
                      "Detach it there first.")
        await s.ensure_ddi()

        async def open_():
            return await s.port_forward(service=DEBUGPROXY)

        async def close(fw, reason):
            dlog(f"debug {bid}: forward closed ({reason})")
            await fw.stop()

        # The forward first, so a suspended launch is attached before the launch watchdog fires.
        fw = await d.resources.acquire(key, client, open_, close, scope="tunnel", timeout=FORWARD_TIMEOUT)
        d.resources.touch(key)
        try:
            pid, launched = 0, False
            if launch in ("attach", "auto"):
                pid = await find_pid(s, bid)
            if pid <= 0 and launch != "none":
                if launch == "attach":
                    raise err("not-running", "That app isn't running. Open it on your iPhone, or start it from "
                              "the debugger (it then waits for the debugger before it runs).")
                pid = await launch_suspended(s, bid, [str(x) for x in a.get("args") or []],
                                             {str(k): str(v) for k, v in (a.get("env") or {}).items()})
                launched = True
        except BaseException:
            await d.resources.release(key, client)
            raise
        base = symbols_dir(s.info.get("ios"), s.info.get("build"))
        have = assess(base)
        return {"bundleId": bid, "port": fw.port, "host": "127.0.0.1", "pid": pid or None, "launched": launched,
                "remotePath": remote, "executable": exe, "ios": s.info.get("ios"), "build": s.info.get("build"),
                "sysroot": str(base) if base else None, "symbolsReady": have["ready"],
                "symbolsState": have["state"], "symbolsUpdate": have["update"]}

    @reg.command("debug.stop")
    async def c_stop(d, a):
        bid = str(a.get("bundleId") or "")
        if not bid:
            raise err("bad-args", "Which app? (bundleId missing)")
        killed = None
        if a.get("kill") and a.get("pid"):
            s = d.need_session(a)
            with contextlib.suppress(Exception):
                async def go():
                    async with s.dvt_channel(ProcessControl) as pc:
                        await pc.kill(int(a["pid"]))
                await s.dvt_retry(go)
                killed = int(a["pid"])
        left = await d.resources.release(f"debug:{bid}", d.client())
        return {"bundleId": bid, "stopped": left == 0, "sharedWith": left, "killed": killed}

    @reg.command("debug.symbols")
    async def c_symbols(d, a):
        action = str(a.get("action") or "status")
        if action not in ("status", "download", "unpack", "cancel", "remove"):
            raise err("bad-args", "action must be status, download, unpack, cancel or remove.")
        s = d.need_session(a)
        ios, build = s.info.get("ios"), s.info.get("build")
        base = symbols_dir(ios, build)
        if "keepDownload" in a:
            set_keep_download(bool(a["keepDownload"]))
            # Turning it off frees the kept download once the set doesn't need it any more.
            if not a["keepDownload"] and not sym.running() and base and assess(base)["state"] == "ready":
                await asyncio.to_thread(shutil.rmtree, base / "dsc", True)
        if action == "status":
            return status(s)
        if action == "cancel":
            if sym.running():
                await d.resources.close(Symbols.KEY, "released")
                await sym.stop()
            return status(s)
        if action == "remove":
            if sym.running():
                raise err("busy", "The debug symbols are being downloaded or unpacked. Cancel that first.")
            if base:
                for sub in ("Symbols", "Symbols.new", "Symbols.old", "dsc"):
                    await asyncio.to_thread(shutil.rmtree, base / sub, True)
                with contextlib.suppress(OSError):
                    base.rmdir()
            sym.state = {"phase": "idle"}
            return status(s)
        if sym.running():
            await d.resources.acquire(Symbols.KEY, d.client(), lambda: sym, sym.stop, scope="tunnel")
            return status(s)
        if base is None:
            raise err("unreachable", "Your iPhone's iOS version isn't known yet. Wait for it to connect.")
        have = assess(base)
        if action == "download" and have["state"] == "ready" and not a.get("force"):
            return status(s)
        if not ipsw_path():
            raise err("needs-ipsw", "Unpacking the symbols needs ipsw. Install it in Setup first.", IPSW_FIX)

        if action == "unpack":
            if not have["canUnpack"]:
                raise err("no-download", "There's no kept download to unpack. Download the debug symbols again.")
            free = free_bytes(SYMBOLS_ROOT)
            if free < SPACE_MARGIN:
                raise err("no-space", f"Unpacking the debug symbols needs about {SPACE_MARGIN / 1e9:.1f} GB free; this "
                          f"computer has {free / 1e9:.1f} GB. Free some space (Toolchain shows what can go), then "
                          "try again.", fix("Free space", "toolchain"))

            def open_unpack():
                sym.state = {"phase": "starting", "ios": ios, "build": build}
                sym.task = asyncio.create_task(sym.run(d, s, base, None, 0))
                return sym
            await d.resources.acquire(Symbols.KEY, d.client(), open_unpack, sym.stop, scope="session")
            sym.publish(phase="extracting", fraction=None, done=0, message="Unpacking system libraries…")
            return {**status(s), "started": True}

        await s.get_rsd()
        async with s.open_service(RemoteFetchSymbolsService) as svc:
            files = await svc.get_dsc_file_list()
        total = sum(f.file_size for f in files)
        sym.sizes[f"{ios} ({build})"] = total
        have_bytes = sum((base / "dsc" / f.file_path.lstrip("/")).stat().st_size for f in files
                         if (base / "dsc" / f.file_path.lstrip("/")).is_file())
        free = free_bytes(SYMBOLS_ROOT)
        if free < total - have_bytes + SPACE_MARGIN:
            gb = (total - have_bytes + SPACE_MARGIN) / 1e9
            raise err("no-space", f"The debug symbols need about {gb:.1f} GB free while they download; this computer "
                      f"has {free / 1e9:.1f} GB. Free some space (Toolchain shows what can go), then try again.",
                      fix("Free space", "toolchain"))
        base.mkdir(parents=True, exist_ok=True)

        def open_():
            sym.state = {"phase": "starting", "ios": ios, "build": build}
            sym.task = asyncio.create_task(sym.run(d, s, base, files, total))
            return sym
        await d.resources.acquire(Symbols.KEY, d.client(), open_, sym.stop, scope="tunnel")
        sym.publish(phase="downloading", received=have_bytes, total=total, fraction=have_bytes / total if total else 0,
                    message="Copying system libraries from your iPhone…")
        return {**status(s), "started": True, "total": total}

    @reg.info
    def info(d, s):
        have = assess(symbols_dir(s.info.get("ios"), s.info.get("build")))
        return {"debugSymbols": have["ready"], "debugSymbolsState": have["state"]}
