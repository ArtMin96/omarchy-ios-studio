"""Helper group `debug`: LLDB on your own apps on the iPhone (iPhone page › Debug, MCP ios_debug_*).

  debug check                        is lldb usable? {ready, lldb, lldbVersion, python, error?}
  debug sources --bundle-id B        the matching project's Swift files, for file:line breakpoints
  debug backtrace --bundle-id B      stream: attach, read every thread, detach (the app pauses briefly)
  debug session --bundle-id B [--breakpoints '<json list>'] [--launch attach|suspended|auto]
                [--commands '<json list>'] [--stop-at-attach]
                                     stream: an interactive session; commands arrive on stdin

How it works: the device daemon forwards the phone's debugserver to a loopback port over its own
tunnel (`debug.start`), and helper/lldb_driver.py runs the toolchain's lldb against that port
through lldb's Python API, under the Python lldb links (python3.12 for Swift 6.4; Setup's
"Debugger support" step installs it). This process relays the driver's JSON events to stdout and
stdin commands to the driver, and releases the forward (`debug.stop`) however the session ends.

Stream events (besides step/notice/log/done): `debug {mode, bundleId}`, `started {port, pid,
launched, ...}`, then the driver's `console {text}`, `attached`, `backtrace`, `breakpoints`,
`running`, `stopped`, `variables`, `evaluated`, `ack`, `exited`. stdin: `{"cmd": "continue"}`
(continue, step, next, finish, pause, detach, kill, frame_variables {frame?, depth?}, evaluate
{expr}, select_frame {frame}, backtrace, breakpoint_add {spec}, breakpoint_delete {id}, status),
or `{"cancel": true}` (= detach). Protocol: docs/deviced-protocol.md#debugging.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import common
from common import HelperError, fix

DRIVER = Path(__file__).resolve().parent / "lldb_driver.py"
SETUP_SWIFT = fix("Install Swift", "setup", step="swift")
SETUP_DEBUGGER = fix("Install debugger support", "setup", step="debugger")
ATTACH_TIMEOUT = 240        # seconds until the driver must have attached (minimal load level: ~30 s on Wi-Fi)
DETACH_GRACE = 10           # seconds the driver gets to detach after we ask it to end
MAX_SOURCES = 400


@dataclass
class Toolchain:
    lldb: str
    version: str
    python: str
    pythonpath: str
    env: dict


def toolchain() -> Toolchain:
    """The Swift toolchain's lldb and the Python it links. Raises HelperError with a Setup fix."""
    from setup import toolchain_root   # the same lookup Setup uses (realpath of /usr/bin/swift)
    root = toolchain_root()
    lldb = root / "usr/bin/lldb" if root else None
    if lldb is None or not lldb.is_file():
        raise HelperError("needs-lldb", "The debugger (lldb) comes with the Swift toolchain, which isn't installed "
                          "yet. Install it in Setup.", SETUP_SWIFT)
    try:
        p = subprocess.run([str(lldb), "-P"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperError("needs-lldb", "The debugger (lldb) didn't start.", SETUP_SWIFT, str(exc))
    text = (p.stdout + p.stderr).strip()
    if p.returncode != 0 or not p.stdout.strip():
        m = re.search(r"libpython(\d+\.\d+)", text)
        if m:
            raise HelperError("needs-python", f"The debugger needs Python {m.group(1)}, which isn't installed. "
                              "Install “Debugger support” in Setup (it takes a while: it's built from source).",
                              SETUP_DEBUGGER, text[-400:])
        raise HelperError("needs-lldb", "The debugger (lldb) didn't start.", SETUP_SWIFT, text[-400:])
    pypath = p.stdout.strip().splitlines()[-1]
    m = re.search(r"python(\d+\.\d+)", pypath)
    ver = m.group(1) if m else "3.12"
    python = Path(f"/usr/bin/python{ver}")
    if not python.is_file():
        raise HelperError("needs-python", f"The debugger needs Python {ver}, which isn't installed. Install "
                          "“Debugger support” in Setup (it takes a while: it's built from source).", SETUP_DEBUGGER)
    libs = [root / "usr/lib/swift/host/compiler", root / "usr/lib"]   # what lldb's own rpath points at
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join([str(x) for x in libs if x.is_dir()] +
                                      ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    try:
        v = subprocess.run([str(lldb), "--version"], capture_output=True, text=True, timeout=20).stdout
        version = (v.strip().splitlines() or [""])[0]
    except (OSError, subprocess.TimeoutExpired):
        version = ""
    return Toolchain(str(lldb), version, str(python), pypath, env)


# ---------------------------------------------------------------- projects and local builds

def find_project(bundle_id: str) -> tuple[Path | None, dict | None]:
    """The scanned project whose bundle id the installed one is (XTL-<TEAM>.<id> or <id>)."""
    import projects
    found, _ = projects._scan()
    for p in found:
        info = projects._project_info(p)
        b = info.get("bundleId")
        if b and (bundle_id == b or bundle_id.endswith("." + b)):
            return p, info
    return None, None


def _app_executable(app: Path) -> Path | None:
    try:
        exe = plistlib.loads((app / "Info.plist").read_bytes()).get("CFBundleExecutable")
    except Exception:  # noqa: BLE001
        return None
    return app / exe if exe and (app / exe).is_file() else None


def _ipa_executable(ipa: Path) -> Path | None:
    """The executable inside an .ipa, unpacked once into the cache (refreshed when the .ipa changes)."""
    import zipfile
    try:
        st = ipa.stat()
        with zipfile.ZipFile(ipa) as z:
            names = z.namelist()
            plists = [n for n in names if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
            if not plists:
                return None
            exe = plistlib.loads(z.read(plists[0])).get("CFBundleExecutable")
            member = plists[0].rsplit("/", 1)[0] + "/" + str(exe)
            if not exe or member not in names:
                return None
            out = common.CACHE_DIR / "debug-builds" / f"{ipa.parent.parent.name}-{int(st.st_mtime)}-{st.st_size}" / str(exe)
            if not out.is_file():
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_suffix(".part")
                with z.open(member) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                tmp.replace(out)
            return out
    except (OSError, ValueError, KeyError, Exception):  # noqa: BLE001  (a broken zip must not break the debugger)
        return None


def local_executable(path: Path, info: dict) -> str | None:
    """The app's executable from its newest build on this computer: xtool/<App>.app, xtool's build
    folder (.build/<config>/<Product>-App, which is what gets signed and installed) or the
    executable inside xtool/<App>.ipa. None when the project was never built here."""
    product = str(info.get("product") or "")
    found: list[Path] = []
    xt = path / "xtool"
    for app in ([xt / f"{product}.app"] if product else []) + sorted(xt.glob("*.app")):
        exe = _app_executable(app)
        if exe:
            found.append(exe)
    if product:
        for cfg in ("debug", "release"):
            exe = path / ".build" / cfg / f"{product}-App"
            if exe.is_file():
                found.append(exe)
        for exe in sorted((path / ".build").glob(f"out/Products/*-iphoneos/{product}-App")):
            found.append(exe)
    if not found:
        for ipa in sorted(xt.glob("*.ipa")):
            exe = _ipa_executable(ipa)
            if exe:
                found.append(exe)
    if not found:
        return None
    return str(max(found, key=lambda p: p.stat().st_mtime))


_PATH_ARG = re.compile(r'\bpath\s*:\s*"([^"]+)"')
_SKIP_SOURCE_DIRS = {".build", ".git", ".swiftpm", "xtool", "DerivedData", "Pods", "node_modules", "build"}


def _is_test_dir(name: str) -> bool:
    return name.lower() in ("tests", "test") or name.endswith("Tests")


def source_roots(path: Path, depth: int = 2) -> list[Path]:
    """Folders holding the app's Swift code: Sources/, the target paths in Package.swift (projects
    often point the target somewhere else, e.g. a generated folder of symlinks) and the local
    packages it depends on (`.package(path: …)`), up to `depth` levels of packages."""
    roots: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        try:
            r = p.resolve()
        except OSError:
            return
        if r.is_dir() and r not in seen and not _is_test_dir(r.name):
            seen.add(r)
            roots.append(p)

    def visit(pkg: Path, level: int) -> None:
        add(pkg / "Sources")
        manifest = pkg / "Package.swift"
        try:
            text = manifest.read_text(errors="replace")
        except OSError:
            return
        for rel in _PATH_ARG.findall(text):
            target = (pkg / rel)
            if (target / "Package.swift").is_file():           # a local package dependency
                if level < depth and target.resolve() not in seen:
                    visit(target, level + 1)
            else:                                              # a target's own path
                add(target)

    visit(path, 0)
    return roots


def swift_sources(path: Path) -> list[dict]:
    out = []
    seen: set[str] = set()
    for root in source_roots(path):
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and d not in _SKIP_SOURCE_DIRS and not _is_test_dir(d))
            for name in sorted(filenames):
                if not name.endswith(".swift"):
                    continue
                f = Path(dirpath) / name
                real = os.path.realpath(f)
                if real in seen:
                    continue
                seen.add(real)
                try:
                    lines = sum(1 for _ in open(real, "rb"))
                except OSError:
                    lines = None
                out.append({"path": real, "rel": os.path.relpath(real, path), "name": name, "lines": lines})
                if len(out) >= MAX_SOURCES:
                    return out
    return out


# ---------------------------------------------------------------- commands

def cmd_check(flags: dict) -> int:
    try:
        tc = toolchain()
    except HelperError as exc:
        return common.ok({"ready": False, "error": exc.to_json()})
    return common.ok({"ready": True, "lldb": tc.lldb, "lldbVersion": tc.version, "python": tc.python})


def cmd_sources(flags: dict) -> int:
    bid = str(flags.get("bundle_id") or "")
    if not bid:
        return common.fail("bad-args", "Which app? (--bundle-id missing)")
    path, info = find_project(bid)
    if path is None:
        return common.ok({"project": None, "files": [], "localBuild": None})
    return common.ok({"project": {"name": info.get("name"), "path": str(path), "displayPath": info.get("displayPath")},
                      "files": swift_sources(path), "localBuild": local_executable(path, info)})


def _json_list(flags: dict, key: str) -> list:
    raw = flags.get(key)
    if not raw or raw is True:
        return []
    try:
        v = json.loads(raw)
    except ValueError:
        raise HelperError("bad-args", f"--{key.replace('_', '-')} must be a JSON list.")
    if not isinstance(v, list):
        raise HelperError("bad-args", f"--{key.replace('_', '-')} must be a JSON list.")
    return [str(x) for x in v if str(x).strip()][:50]


def cmd_backtrace(flags: dict) -> int:
    return _stream("backtrace", flags)


def cmd_session(flags: dict) -> int:
    return _stream("session", flags)


def _stream(mode: str, flags: dict) -> int:
    bid = str(flags.get("bundle_id") or "")
    if not bid:
        return common.done(False, error=HelperError("bad-args", "Which app? (--bundle-id missing)"))
    try:
        bps = _json_list(flags, "breakpoints")
        cmds = _json_list(flags, "commands")
        common.event("debug", mode=mode, bundleId=bid)
        common.step("lldb", "Checking the debugger", "running")
        tc = toolchain()
        common.step("lldb", "Checking the debugger", "done", detail=tc.version)
    except HelperError as exc:
        common.step("lldb", "Checking the debugger", "failed", error=exc.to_json())
        return common.done(False, error=exc)
    return asyncio.run(_run(mode, bid, flags, tc, bps, cmds))


def _write(line: str) -> None:
    sys.stdout.write(line.rstrip("\n") + "\n")
    sys.stdout.flush()


async def _stdin_reader() -> asyncio.StreamReader | None:
    import stat
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError, AttributeError):
        return None
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or sys.stdin.isatty()):
        return None             # /dev/null or a file: nobody will send commands
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=1 << 20)
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (OSError, ValueError):
        return None
    return reader


async def _run(mode: str, bid: str, flags: dict, tc: Toolchain, bps: list, cmds: list) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from device_client import DeviceClient, DeviceError

    common.step("project", "Finding the app's build on this computer", "running")
    path, info = await asyncio.to_thread(find_project, bid)
    exe = local_executable(path, info) if path else None
    common.step("project", "Finding the app's build on this computer", "done",
                detail=(info or {}).get("name") or "no matching project")
    if not exe:
        common.event("notice", id="no-local-build", level="warn",
                     message="No build of this app was found on this computer, so only system symbols are available: "
                             "frames in your own code show as addresses, and file:line breakpoints won't resolve. "
                             "Build and run the app from iOS Studio first.")
    launch = str(flags.get("launch") or ("attach" if mode == "backtrace" else "auto"))
    common.step("forward", "Starting the debugger on your iPhone", "running")
    async with DeviceClient(name=f"debug {mode}", events=[]) as dev:
        try:
            st = await dev.request("debug.start", {"bundleId": bid, "launch": launch, "udid": flags.get("udid") or None},
                                   timeout=90)
        except DeviceError as exc:
            e = HelperError(exc.code, exc.message, exc.fix, exc.detail)
            common.step("forward", "Starting the debugger on your iPhone", "failed", error=e.to_json())
            return common.done(False, error=e)
        common.step("forward", "Starting the debugger on your iPhone", "done", detail=f"port {st.get('port')}")
        common.event("started", **{k: st.get(k) for k in ("port", "pid", "launched", "remotePath", "executable",
                                                        "ios", "build", "symbolsReady")},
                     localBuild=exe, project=(info or {}).get("name"))
        if not st.get("symbolsReady"):
            common.event("notice", id="no-symbols", level="info",
                         message="This iPhone's debug symbols aren't downloaded, so Swift values (String, Array) may "
                                 "read wrong and system frames show only library names. Download them once under "
                                 "iPhone › Debug › Debug symbols.")
        cfg = {"mode": mode, "lldbPythonPath": tc.pythonpath, "port": st.get("port"), "host": st.get("host"),
               "pid": st.get("pid"), "remotePath": st.get("remotePath"), "localExe": exe,
               "sysroot": st.get("sysroot") if st.get("symbolsReady") else None, "breakpoints": bps,
               "commands": cmds, "stopAtAttach": bool(flags.get("stop_at_attach")),
               "maxFrames": 128, "stopFrames": 40, "localsDepth": 1,
               # tests drive a local lldb-server instead of a phone (IOSSTUDIO_DEBUG_PLATFORM=remote-linux)
               "platform": os.environ.get("IOSSTUDIO_DEBUG_PLATFORM") or "remote-ios"}
        final: dict | None = None
        attached = asyncio.Event()
        try:
            proc = await asyncio.create_subprocess_exec(
                tc.python, "-I", str(DRIVER), json.dumps(cfg), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=tc.env, limit=16 << 20)
        except OSError as exc:
            await _stop(dev, bid, st, kill=bool(st.get("launched")))
            return common.done(False, error=HelperError("needs-python", "The debugger's Python didn't start.",
                                                        SETUP_DEBUGGER, str(exc)))

        def ask_end(how: str = "detach") -> None:
            with contextlib.suppress(Exception):
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.write((json.dumps({"cmd": how}) + "\n").encode())

        async def stop_soon(how: str = "detach") -> None:
            ask_end(how)
            try:
                await asyncio.wait_for(proc.wait(), DETACH_GRACE)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, lambda: asyncio.ensure_future(stop_soon()))

        async def pump_out():
            nonlocal final
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    common.event("console", text=line[:4000])
                    continue
                if ev.get("event") == "done":
                    final = ev
                    continue
                if ev.get("event") == "attached":
                    attached.set()
                _write(line)

        async def pump_err():
            async for raw in proc.stderr:
                text = raw.decode(errors="replace").rstrip()
                if text:
                    common.event("console", text=text[:4000])

        async def pump_in():
            reader = await _stdin_reader()
            if reader is None:
                return
            while True:
                raw = await reader.readline()
                if not raw:
                    if mode == "session":   # the controller went away: let the app go
                        await stop_soon()
                    return
                with contextlib.suppress(Exception):
                    proc.stdin.write(raw if raw.endswith(b"\n") else raw + b"\n")
                    await proc.stdin.drain()

        async def watchdog():
            try:
                await asyncio.wait_for(attached.wait(), ATTACH_TIMEOUT)
            except asyncio.TimeoutError:
                common.event("notice", id="attach-timeout", level="warn",
                             message="Attaching took too long; giving up.")
                await stop_soon()

        tasks = [asyncio.create_task(x()) for x in (pump_out, pump_err, pump_in, watchdog)]
        await proc.wait()
        await asyncio.gather(tasks[0], tasks[1], return_exceptions=True)
        for t in tasks[2:]:
            t.cancel()
        ended = (final or {}).get("data", {}).get("ended") if (final or {}).get("ok") else (final or {}).get("ended")
        # A suspended launch that never got attached would stay frozen: end it.
        kill = bool(st.get("launched")) and ended in (None, "none", "detach-failed")
        stopped = await _stop(dev, bid, st, kill=kill)
    base = {"bundleId": bid, "pid": st.get("pid"), "launched": bool(st.get("launched")), "localBuild": exe,
            "project": (info or {}).get("name"), "symbolsReady": bool(st.get("symbolsReady")),
            "killed": (stopped or {}).get("killed")}
    if final and final.get("ok"):
        return common.done(True, {**base, **(final.get("data") or {})})
    e = (final or {}).get("error") or {"code": "lldb-failed", "message": "The debugger stopped unexpectedly.",
                                       "detail": f"exit {proc.returncode}"}
    out = HelperError(e.get("code") or "lldb-failed", e.get("message") or "The debugger stopped.", e.get("fix"),
                      e.get("detail"))
    common.event("done", ok=False, error=out.to_json(), data=base)
    return 1


async def _stop(dev, bid: str, st: dict, kill: bool) -> dict | None:
    with contextlib.suppress(Exception):
        return await dev.request("debug.stop", {"bundleId": bid, "kill": kill, "pid": st.get("pid")}, timeout=30)
    return None


def demo(action: str, flags: dict) -> int:
    import debugger_demo
    return debugger_demo.run(action, flags)


GROUPS = {"debug": {"check": cmd_check, "sources": cmd_sources, "backtrace": cmd_backtrace, "session": cmd_session}}
