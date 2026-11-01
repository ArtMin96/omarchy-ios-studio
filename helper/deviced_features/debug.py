"""debug.start, debug.stop, debug.symbols (+ debugSymbols events): LLDB on your own apps.

debug.start forwards the phone's debugserver (com.apple.internal.dt.remote.debugproxy) to a
loopback port through the daemon's own tunnel (Session.port_forward, never a second tunnel) and
finds or launches the app's process over the shared DVT connection. The helper (helper/debugger.py)
then runs lldb against that port. The forward is a reference-counted resource: it closes on
debug.stop, when the client that asked for it disconnects, when the tunnel drops, and after an
hour without a new debug.start.

debug.symbols downloads the phone's dyld shared cache once per iOS build (about 6 GB, over the
tunnel) and extracts the Swift runtime and libobjc with ipsw into
~/.cache/ios-studio/DeviceSupport/<ios> (<build>)/Symbols, so Linux lldb reads Swift values right.
The cache itself is deleted afterwards; only the extracted libraries (a few hundred MB) stay.
"""
from __future__ import annotations

import asyncio
import contextlib
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
DONE_MARK = ".ios-studio-complete"
SPACE_MARGIN = 1 << 30          # free space kept on top of the download size
WORKERS = 4                     # concurrent file streams, as pymobiledevice3 uses
ESTIMATE = 6 << 30              # shown before the phone has been asked for the file list
IPSW_FIX = fix("Install ipsw", "setup", step="ipsw")


def symbols_dir(ios: str | None, build: str | None) -> Path | None:
    if not ios or not build:
        return None
    safe = re.sub(r"[^A-Za-z0-9.]", "", f"{ios}") + " (" + re.sub(r"[^A-Za-z0-9]", "", f"{build}") + ")"
    return SYMBOLS_ROOT / safe


def symbols_ready(base: Path | None) -> bool:
    return bool(base) and (base / "Symbols" / DONE_MARK).is_file()


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


def _size_on_disk(p: Path) -> int:
    return common.dir_size(p) if p.exists() else 0


class Symbols:
    """One download at a time (the active iPhone's iOS build)."""
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

    async def run(self, d, s, base: Path, files: list, total: int):
        dsc = base / "dsc"
        sym = base / "Symbols"
        try:
            await self._download(s, dsc, files, total)
            await self._extract(dsc, sym)
            await asyncio.to_thread(shutil.rmtree, dsc, True)
            (sym / DONE_MARK).write_text(time.strftime("%Y-%m-%dT%H:%M:%S\n"))
            self.publish(phase="done", fraction=1.0, message="Debug symbols are ready.",
                         sizeOnDisk=_size_on_disk(sym), ready=True)
        except asyncio.CancelledError:
            await asyncio.to_thread(shutil.rmtree, dsc, True)
            await asyncio.to_thread(shutil.rmtree, sym, True)
            self.publish(phase="cancelled", message="Download cancelled.", ready=False)
            raise
        except BaseException as exc:  # noqa: BLE001
            dlog(f"symbols: {exc!r}")
            e = exc if isinstance(exc, common.HelperError) else err(
                "failed", "Downloading the debug symbols failed.", None, f"{type(exc).__name__}: {exc}")
            await asyncio.to_thread(shutil.rmtree, sym, True)
            self.publish(phase="failed", message=e.message, error=e.to_json(), ready=False)
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
                    got[0] += f.file_size       # kept from an interrupted run
                    continue
                queue.put_nowait(i)
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

    async def _extract(self, dsc: Path, sym: Path):
        ipsw = ipsw_path()
        if not ipsw:
            raise err("needs-ipsw", "ipsw is needed to unpack the symbols. Install it in Setup, then try again.", IPSW_FIX)
        cache = main_cache(dsc)
        if cache is None:
            raise err("failed", "The iPhone's system library cache wasn't in the download.")
        self.publish(phase="extracting", fraction=None, message="Unpacking the Swift runtime…", done=0)
        proc = await asyncio.create_subprocess_exec(ipsw, "dyld", "info", str(cache), "--dylibs",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        paths = sorted({m.group(0) for m in re.finditer(r"/\S+$", out.decode(errors="replace"), re.M)
                        if re.match(r"^/usr/lib/(swift/|libobjc)", m.group(0))})
        if not paths:
            raise err("failed", "ipsw couldn't read the iPhone's system library cache.", None,
                      out.decode(errors="replace")[-600:])
        for n, p in enumerate(paths, 1):
            dest = sym / p.lstrip("/").rsplit("/", 1)[0]
            dest.mkdir(parents=True, exist_ok=True)
            x = await asyncio.create_subprocess_exec(ipsw, "dyld", "extract", str(cache), p.rsplit("/", 1)[1], "--slide",
                                                     "-o", str(dest), stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.PIPE)
            _, e = await x.communicate()
            if x.returncode != 0:
                dlog(f"symbols: ipsw extract {p}: {e.decode(errors='replace')[-300:]}")
            self.publish(False, phase="extracting", done=n, count=len(paths), fraction=n / len(paths),
                         message=f"Unpacking the Swift runtime ({n} of {len(paths)})…")
        if not (sym / "usr/lib/swift/libswiftCore.dylib").is_file():
            raise err("failed", "Unpacking the Swift runtime failed.", None, "libswiftCore.dylib missing after extract")


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


def register(reg):
    sym = Symbols()

    def status(s) -> dict:
        ios, build = s.info.get("ios"), s.info.get("build")
        base = symbols_dir(ios, build)
        label = f"{ios} ({build})" if ios and build else None
        need = sym.sizes.get(label or "", 0)
        out = {"ios": ios, "build": build, "dir": str(base) if base else None, "sysroot": str(base) if base else None,
               "ready": symbols_ready(base), "downloading": sym.running(),
               "estimatedBytes": need or ESTIMATE, "estimated": not need,
               "freeBytes": free_bytes(SYMBOLS_ROOT), "ipsw": bool(ipsw_path())}
        if out["ready"] and base:
            out["sizeOnDisk"] = _size_on_disk(base / "Symbols")
        if sym.running() or sym.state.get("phase") in ("failed", "cancelled"):
            out["progress"] = dict(sym.state)
        return out

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
        return {"bundleId": bid, "port": fw.port, "host": "127.0.0.1", "pid": pid or None, "launched": launched,
                "remotePath": remote, "executable": exe, "ios": s.info.get("ios"), "build": s.info.get("build"),
                "sysroot": str(base) if base else None, "symbolsReady": symbols_ready(base)}

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
        s = d.need_session(a)
        if action == "status":
            return status(s)
        if action == "cancel":
            if sym.running():
                await d.resources.close(Symbols.KEY, "released")
                await sym.stop()
            return status(s)
        if action != "download":
            raise err("bad-args", "action must be status, download or cancel.")
        if sym.running():
            await d.resources.acquire(Symbols.KEY, d.client(), lambda: sym, sym.stop, scope="tunnel")
            return status(s)
        rsd = await s.get_rsd()
        base = symbols_dir(s.info.get("ios"), s.info.get("build"))
        if base is None:
            raise err("unreachable", "Your iPhone's iOS version isn't known yet. Wait for it to connect.")
        if symbols_ready(base) and not a.get("force"):
            return status(s)
        if not ipsw_path():
            raise err("needs-ipsw", "Unpacking the symbols needs ipsw. Install it in Setup first.", IPSW_FIX)
        async with s.open_service(RemoteFetchSymbolsService) as svc:
            files = await svc.get_dsc_file_list()
        total = sum(f.file_size for f in files)
        sym.sizes[f"{s.info.get('ios')} ({s.info.get('build')})"] = total
        have = sum((base / "dsc" / f.file_path.lstrip("/")).stat().st_size for f in files
                   if (base / "dsc" / f.file_path.lstrip("/")).is_file())
        free = free_bytes(SYMBOLS_ROOT)
        if free < total - have + SPACE_MARGIN:
            gb = (total - have + SPACE_MARGIN) / 1e9
            raise err("no-space", f"The debug symbols need about {gb:.1f} GB free while they download; this computer "
                      f"has {free / 1e9:.1f} GB. Free some space (Toolchain shows what can go), then try again.",
                      fix("Free space", "toolchain"))
        base.mkdir(parents=True, exist_ok=True)

        def open_():
            sym.state = {"phase": "starting", "ios": s.info.get("ios"), "build": s.info.get("build")}
            sym.task = asyncio.create_task(sym.run(d, s, base, files, total))
            return sym
        await d.resources.acquire(Symbols.KEY, d.client(), open_, sym.stop, scope="tunnel")
        sym.publish(phase="downloading", received=have, total=total, fraction=have / total if total else 0,
                    message="Copying system libraries from your iPhone…")
        return {**status(s), "started": True, "total": total}

    @reg.info
    def info(d, s):
        base = symbols_dir(s.info.get("ios"), s.info.get("build"))
        return {"debugSymbols": symbols_ready(base)}
