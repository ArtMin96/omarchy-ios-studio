"""Helper group `debug`: LLDB on your own apps on the iPhone (iPhone page › Debug, MCP ios_debug_*).

  debug check                        is lldb usable? {ready, lldb, lldbVersion, python, error?}
  debug sources --bundle-id B        the matching project's Swift files, for file:line breakpoints
  debug backtrace --bundle-id B      stream: attach, read every thread, detach (the app pauses briefly)
  debug session --bundle-id B [--breakpoints '<json list>'] [--launch attach|suspended|auto]
                [--commands '<json list>'] [--stop-at-attach]
                                     stream: an interactive session; commands arrive on stdin
  debug editor-config --project P [--editor vscode|zed|neovim] [--dry-run]
                                     the editor's attach configuration (.vscode/launch.json, .zed/debug.json)
  debug editor --project P | --bundle-id B [--launch auto|attach|suspended] [--udid U] [--editor E] [--no-config]
                                     stream: hold the forward for the editor's lldb-dap until cancelled
  debug editor-status [--bundle-id B]  the "Debug in editor" bridges running now
  (editor_debug.py, editor_debug_bridge.py; docs/features/editor-debugging.md)

How it works: the device daemon forwards the phone's debugserver to a loopback port over its own
tunnel (`debug.start`), and helper/iosstudio/features/lldb_driver.py runs lldb against that port
through lldb's Python API, under the Python lldb links (python3.12 for Swift 6.4; Setup's
"Debugger support" step installs it). The lldb is the toolchain's, seen through a shadow toolchain
(lldb_shadow.py) whose Swift resource dir is the darwin SDK's, with target.sdk-path set to the
iPhoneOS SDK, so Swift expressions (p, po) work; without the SDK it is the plain toolchain lldb and
the session carries a `notice` (id lldb-plain) that expressions may fail. This process relays the
driver's JSON events to stdout and stdin commands to the driver, and releases the forward
(`debug.stop`) however the session ends.

Stream events (besides step/notice/log/done): `debug {mode, bundleId}`, `started {port, pid,
launched, ...}`, then the driver's `console {text}`, `attached`, `backtrace`, `breakpoints`,
`running`, `stopped` (`localsPending`: its locals follow as `variables {stopId}`), `variables`, `evaluated`,
`ack`, `exited`. stdin: `{"cmd": "continue"}` (continue, step, next, finish, pause, detach, kill,
frame_variables {frame?, depth?}, evaluate {expr}, select_frame {frame}, backtrace, breakpoint_add {spec},
breakpoint_delete {id}, status), or `{"cancel": true}` (= detach). Protocol: docs/deviced-protocol.md#debugging.

This process never hangs on the driver: it leads its own process group, and one that doesn't end DETACH_GRACE after
being asked to gets SIGTERM, then SIGKILL for the group. lldb's console spam (HostInfoError, missing .pcm warnings) is
summarized by ConsoleFilter; a build that lost its precompiled modules gets the "stale-modules" notice (Stale,
projects/modules.py, which also makes the next build recompile).
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

from iosstudio.core import appdirs, fileio, output
from iosstudio.core.output import HelperError, fix
from iosstudio.features import lldb_shadow
from iosstudio.features.projects import modules

DRIVER = Path(__file__).resolve().parent / "lldb_driver.py"
SETUP_SWIFT = fix("Install Swift", "setup", step="swift")
SETUP_DEBUGGER = fix("Install debugger support", "setup", step="debugger")
ATTACH_TIMEOUT = 240        # seconds until the driver must have attached (minimal load level: ~30 s on Wi-Fi)
DETACH_GRACE = 10           # seconds the driver gets to detach after we ask it to end, then SIGTERM
KILL_GRACE = 3              # seconds after SIGTERM before its whole process group gets SIGKILL
PUMP_GRACE = 5              # seconds to drain the driver's output once it has exited
# The iPhone's debug symbols (iPhone › Debug, DeviceDebugView.runAction("symbols") downloads or updates them).
SYMBOLS_FIX = {"label": "Download symbols", "page": "device", "tab": "debug", "action": "symbols"}
SYMBOLS_UPDATE_FIX = {**SYMBOLS_FIX, "label": "Update symbols"}
# One notice (id no-symbols) for everything missing symbols affects, with one fix: sessions, the editor bridge, demo.
SYMBOLS_NOTE = ("This iPhone's debug symbols aren't downloaded yet. Until they are, system frames show only library "
                "names, some Swift values (String, Array) may read wrong, and variables are slow over Wi-Fi. Download "
                "them once under iPhone › Debug › Debug symbols.")
SYMBOLS_UPDATE_NOTE = ("Attaching may take minutes over Wi-Fi until the debug symbols are updated (iPhone › Debug › "
                       "Debug symbols › Update). The app stays paused meanwhile.")
STALE_NOTE = "Swift expressions need a fresh build: Run the app again (it rebuilds what's missing)."
MAX_SOURCES = 400
DEBUG_BUILDS = appdirs.CACHE_DIR / "debug-builds"   # executables unpacked from .ipa files, per project
DEBUG_BUILDS_KEEP = 2


@dataclass
class Toolchain:
    lldb: str
    version: str
    python: str
    pythonpath: str
    env: dict


TOOLCHAIN_CACHE = appdirs.CACHE_DIR / "lldb-toolchain.json"


def _lldb_stamp(lldb: str) -> dict | None:
    try:
        real = os.path.realpath(lldb)
        st = os.stat(real)
    except OSError:
        return None
    return {"real": real, "mtime": st.st_mtime_ns, "size": st.st_size}


def _env(lldb: str) -> dict:
    root = Path(lldb).parent.parent.parent   # <root>/usr/bin/lldb
    libs = [root / "usr/lib/swift/host/compiler", root / "usr/lib"]   # what lldb's own rpath points at
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join([str(x) for x in libs if x.is_dir()] +
                                      ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def toolchain() -> Toolchain:
    """The Swift toolchain's lldb and the Python it links. Raises HelperError with a Setup fix.

    Asking lldb (-P, --version) takes about 0.4 s, so a working answer is cached on disk and reused
    while /usr/bin/swift points at the same toolchain and lldb is the same file."""
    c = fileio.load_json(TOOLCHAIN_CACHE)
    if isinstance(c, dict) and c.get("swift") == os.path.realpath("/usr/bin/swift") \
            and c.get("stamp") == _lldb_stamp(str(c.get("lldb") or "")) and Path(str(c.get("python"))).is_file():
        return Toolchain(c["lldb"], c["version"], c["python"], c["pythonpath"], _env(c["lldb"]))
    tc = _toolchain()
    with contextlib.suppress(OSError):
        fileio.save_json(TOOLCHAIN_CACHE, {"swift": os.path.realpath("/usr/bin/swift"), "lldb": tc.lldb,
                                           "stamp": _lldb_stamp(tc.lldb), "version": tc.version,
                                           "python": tc.python, "pythonpath": tc.pythonpath})
    return tc


def _toolchain() -> Toolchain:
    from iosstudio.features.setup import toolchain_root  # the same lookup Setup uses (realpath of /usr/bin/swift)
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
    try:
        v = subprocess.run([str(lldb), "--version"], capture_output=True, text=True, timeout=20).stdout
        version = (v.strip().splitlines() or [""])[0]
    except (OSError, subprocess.TimeoutExpired):
        version = ""
    return Toolchain(str(lldb), version, str(python), pypath, _env(str(lldb)))


@dataclass
class SessionLldb:
    """What a session's driver runs under: the shadow lldb's Python package and libraries, or the plain ones."""
    python: str
    pythonpath: str
    env: dict
    sdk: str | None             # for `settings set target.sdk-path`; None with the plain toolchain lldb
    note: str = ""              # why Swift expressions may fail ("" = they should work)
    shadow: lldb_shadow.Shadow | None = None


def session_lldb(tc: Toolchain) -> SessionLldb:
    """The shadow lldb for this toolchain and the installed iOS SDK (built once, then a stat check), else the
    toolchain's own lldb with a plain-language note."""
    sh, why = lldb_shadow.for_lldb(tc.lldb, tc.pythonpath)
    if sh is None or not sh.pythonpath:
        why = why or "The debugger couldn't be prepared for iOS."
        return SessionLldb(tc.python, tc.pythonpath, tc.env, None, note="Swift expressions (p, po) may not work in this "
                           f"session: {why[0].lower() + why[1:]} Variables still show.")
    return SessionLldb(tc.python, sh.pythonpath, _env(str(sh.lldb)), str(sh.sdk), shadow=sh)


# ---------------------------------------------------------------- projects and local builds

def find_project(bundle_id: str) -> tuple[Path | None, dict | None]:
    """The scanned project whose bundle id the installed one is (XTL-<TEAM>.<id> or <id>)."""
    from iosstudio.features.projects import model
    found, _ = model.scan()
    for p in found:
        info = model.project_info(p)
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
            proj = ipa.parent.parent.name
            out = DEBUG_BUILDS / f"{proj}-{int(st.st_mtime)}-{st.st_size}" / str(exe)
            if not out.is_file():
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_suffix(".part")
                with z.open(member) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                tmp.replace(out)
                # one folder per rebuilt .ipa: keep this project's newest few
                mine = re.compile(re.escape(proj) + r"-\d+-\d+")
                fileio.prune((d for d in DEBUG_BUILDS.iterdir() if mine.fullmatch(d.name)), DEBUG_BUILDS_KEEP,
                             min_keep=1, protect=[out.parent])
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
    """Also builds the shadow lldb the first time (so a session start only checks its stamp)."""
    try:
        tc = toolchain()
    except HelperError as exc:
        return output.ok({"ready": False, "error": exc.to_json()})
    sl = session_lldb(tc)
    return output.ok({"ready": True, "lldb": tc.lldb, "lldbVersion": tc.version, "python": tc.python,
                      "swiftExpressions": not sl.note, "expressionsNote": sl.note or None,
                      "shadowLldb": str(sl.shadow.lldb) if sl.shadow else None})


def cmd_sources(flags: dict) -> int:
    bid = str(flags.get("bundle_id") or "")
    if not bid:
        return output.fail("bad-args", "Which app? (--bundle-id missing)")
    path, info = find_project(bid)
    if path is None:
        return output.ok({"project": None, "files": [], "localBuild": None})
    return output.ok({"project": {"name": info.get("name"), "path": str(path), "displayPath": info.get("displayPath")},
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
        return output.done(False, error=HelperError("bad-args", "Which app? (--bundle-id missing)"))
    try:
        bps = _json_list(flags, "breakpoints")
        cmds = _json_list(flags, "commands")
        output.event("debug", mode=mode, bundleId=bid)
        output.step("lldb", "Checking the debugger", "running")
        tc = toolchain()
        sl = session_lldb(tc)
        output.step("lldb", "Checking the debugger", "done", detail=tc.version)
    except HelperError as exc:
        output.step("lldb", "Checking the debugger", "failed", error=exc.to_json())
        return output.done(False, error=exc)
    if sl.note and mode == "session":
        output.event("notice", id="lldb-plain", level="info", message=sl.note)
    return asyncio.run(_run(mode, bid, flags, sl, bps, cmds))


def symbols_notice(st: dict) -> None:
    """The session's one notice about the iPhone's debug symbols (`debug.start` says whether they're ready): missing
    (no-symbols) or out of date (symbols-update), each with the fix that gets them; nothing when they're fine."""
    if not st.get("symbolsReady"):
        output.event("notice", id="no-symbols", level="info", message=SYMBOLS_NOTE, fix=SYMBOLS_FIX)
    elif (st.get("symbolsUpdate") or {}).get("needed"):
        output.event("notice", id="symbols-update", level="warn", message=SYMBOLS_UPDATE_NOTE, fix=SYMBOLS_UPDATE_FIX)


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


async def _run(mode: str, bid: str, flags: dict, sl: SessionLldb, bps: list, cmds: list) -> int:
    from iosstudio.device.client import DeviceClient, DeviceError

    output.step("project", "Finding the app's build on this computer", "running")
    path, info = await asyncio.to_thread(find_project, bid)
    exe = local_executable(path, info) if path else None
    output.step("project", "Finding the app's build on this computer", "done",
                detail=(info or {}).get("name") or "no matching project")
    if not exe:
        output.event("notice", id="no-local-build", level="warn",
                     message="No build of this app was found on this computer, so only system symbols are available: "
                             "frames in your own code show as addresses, and file:line breakpoints won't resolve. "
                             "Build and run the app from iOS Studio first.")
    launch = str(flags.get("launch") or ("attach" if mode == "backtrace" else "auto"))
    output.step("forward", "Starting the debugger on your iPhone", "running")
    async with DeviceClient(name=f"debug {mode}", events=[]) as dev:
        try:
            st = await dev.request("debug.start", {"bundleId": bid, "launch": launch, "udid": flags.get("udid") or None},
                                   timeout=90)
        except DeviceError as exc:
            output.step("forward", "Starting the debugger on your iPhone", "failed", error=exc.to_json())
            return output.done(False, error=exc)
        output.step("forward", "Starting the debugger on your iPhone", "done", detail=f"port {st.get('port')}")
        output.event("started", **{k: st.get(k) for k in ("port", "pid", "launched", "remotePath", "executable",
                                                        "ios", "build", "symbolsReady")},
                     localBuild=exe, project=(info or {}).get("name"), swiftExpressions=not sl.note)
        stale = Stale(path)
        if mode == "session" and path and await asyncio.to_thread(modules.pruned, path):
            stale.notice()
        symbols_notice(st)
        cfg = {"mode": mode, "lldbPythonPath": sl.pythonpath, "sdkPath": sl.sdk, "port": st.get("port"),
               "host": st.get("host"), "pid": st.get("pid"), "remotePath": st.get("remotePath"), "localExe": exe,
               "sysroot": st.get("sysroot") if st.get("symbolsReady") else None, "breakpoints": bps,
               "commands": cmds, "stopAtAttach": bool(flags.get("stop_at_attach")),
               "maxFrames": 128, "stopFrames": 40, "localsDepth": 1,
               "symbolsReady": bool(st.get("symbolsReady")), "projectPath": str(path) if path else None,
               # tests drive a local lldb-server instead of a phone (IOSSTUDIO_DEBUG_PLATFORM=remote-linux), and
               # IOSSTUDIO_DEBUG_QUICK_LOCALS=0 makes every stop's locals follow it as over a slow link
               "platform": os.environ.get("IOSSTUDIO_DEBUG_PLATFORM") or "remote-ios",
               "quickLocals": float(os.environ["IOSSTUDIO_DEBUG_QUICK_LOCALS"])
               if os.environ.get("IOSSTUDIO_DEBUG_QUICK_LOCALS") else None}
        final: dict | None = None
        attached = asyncio.Event()
        try:
            # its own process group, so a driver stuck inside lldb can be ended with everything it started
            proc = await asyncio.create_subprocess_exec(
                sl.python, "-I", str(DRIVER), json.dumps(cfg), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=sl.env, limit=16 << 20,
                start_new_session=True)
        except OSError as exc:
            await _stop(dev, bid, st, kill=bool(st.get("launched")))
            return output.done(False, error=HelperError("needs-python", "The debugger's Python didn't start.",
                                                        SETUP_DEBUGGER, str(exc)))

        def ask_end(how: str = "detach") -> None:
            with contextlib.suppress(Exception):
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.write((json.dumps({"cmd": how}) + "\n").encode())

        async def stop_soon(how: str = "detach") -> None:
            """Ask the driver to detach; one that doesn't end (lldb busy inside a call) gets SIGTERM, then SIGKILL."""
            ask_end(how)
            if await _exited(proc, DETACH_GRACE):
                return
            output.event("console", text="The debugger didn't stop in time; ending it.")
            _signal_group(proc, signal.SIGTERM)
            if not await _exited(proc, KILL_GRACE):
                _signal_group(proc, signal.SIGKILL)

        ending: list[asyncio.Task] = []

        def end_soon(how: str = "detach") -> None:
            if not ending:      # one escalation per session, whoever asks first (signal, stdin EOF, watchdog)
                ending.append(asyncio.ensure_future(stop_soon(how)))

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, end_soon)
        lines = ConsoleFilter()

        def show_console(text: str) -> None:
            shown = lines.text(text)
            if lines.missing_modules:
                stale.notice(noted=True)
            if shown:
                output.event("console", text=shown[:4000])

        async def pump_out():
            nonlocal final
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    show_console(line)
                    continue
                if ev.get("event") == "done":
                    final = ev
                    continue
                if ev.get("event") == "attached":
                    attached.set()
                if ev.get("event") == "console":
                    show_console(str(ev.get("text") or ""))
                    continue
                _write(line)

        async def pump_err():
            async for raw in proc.stderr:
                text = raw.decode(errors="replace").rstrip()
                if text:
                    show_console(text)

        async def pump_in():
            reader = await _stdin_reader()
            if reader is None:
                return
            while True:
                raw = await reader.readline()
                if not raw:
                    if mode == "session":   # the controller went away: let the app go
                        end_soon()
                    return
                with contextlib.suppress(Exception):
                    proc.stdin.write(raw if raw.endswith(b"\n") else raw + b"\n")
                    await proc.stdin.drain()

        async def watchdog():
            try:
                await asyncio.wait_for(attached.wait(), ATTACH_TIMEOUT)
            except asyncio.TimeoutError:
                output.event("notice", id="attach-timeout", level="warn",
                             message="Attaching took too long; giving up.")
                end_soon()

        tasks = [asyncio.create_task(x()) for x in (pump_out, pump_err, pump_in, watchdog)]
        await proc.wait()
        _signal_group(proc, signal.SIGKILL)     # anything the driver left behind holding its pipes
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(tasks[0], tasks[1], return_exceptions=True), PUMP_GRACE)
        for t in tasks + ending:
            t.cancel()
        if lines.summary():
            output.event("console", text=lines.summary())
        ended = (final or {}).get("data", {}).get("ended") if (final or {}).get("ok") else (final or {}).get("ended")
        # A suspended launch that never got attached would stay frozen: end it.
        kill = bool(st.get("launched")) and ended in (None, "none", "detach-failed")
        stopped = await _stop(dev, bid, st, kill=kill)
    base = {"bundleId": bid, "pid": st.get("pid"), "launched": bool(st.get("launched")), "localBuild": exe,
            "project": (info or {}).get("name"), "symbolsReady": bool(st.get("symbolsReady")),
            "killed": (stopped or {}).get("killed")}
    if final and final.get("ok"):
        return output.done(True, {**base, **(final.get("data") or {})})
    e = (final or {}).get("error") or {"code": "lldb-failed", "message": "The debugger stopped unexpectedly.",
                                       "detail": f"exit {proc.returncode}"}
    out = HelperError(e.get("code") or "lldb-failed", e.get("message") or "The debugger stopped.", e.get("fix"),
                      e.get("detail"))
    output.event("done", ok=False, error=out.to_json(), data=base)
    return 1


async def _exited(proc, seconds: float) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), seconds)
        return True
    except asyncio.TimeoutError:
        return False


def _signal_group(proc, sig: int) -> None:
    """Signal the driver's process group (it leads its own session)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, sig)


class Stale:
    """The session's "stale-modules" notice (once): the app's build lost the module files Swift expressions need.
    Seen at the start (modules.pruned, stat only) or in lldb's own warnings; the latter is noted for the next build."""

    def __init__(self, path: Path | None):
        self.path, self.shown, self.noted = path, False, False

    def notice(self, noted: bool = False) -> None:
        if noted and self.path and not self.noted:
            self.noted = True
            modules.note_missing(self.path)
        if self.shown:
            return
        self.shown = True
        fx = ({"label": "Run the app", "page": "projects", "action": "run", "path": str(self.path)} if self.path
              else fix("Open Projects", "projects"))
        output.event("notice", id="stale-modules", level="warn", message=STALE_NOTE, fix=fx)


_HOSTINFO = re.compile(r"Error while searching for (?:Xcode )?SDK: HostInfoError")
_MISSING_PCM = re.compile(r"'([^']+\.pcm)' does not exist")
_MISSING_TYPES = re.compile(r"Unable to locate module needed for external types|"
                            r"Debugging will be degraded due to missing types")


class ConsoleFilter:
    """lldb's repetitive console lines, each kind once as a plain summary and counted after that (summary()):
    the Swift plugin looking for Xcode on Linux (HostInfoError, hundreds per session) and the missing precompiled
    module warnings of a build whose .pcm files were pruned (two or three lines per object file). Every other line
    passes unchanged."""

    def __init__(self):
        self.hidden: dict[str, int] = {}
        self.missing_modules = False

    def _summary(self, kind: str, line: str) -> str:
        if kind == "hostinfo":
            return ("lldb: “Error while searching for Xcode SDK: HostInfoError”. lldb looks for Xcode, which "
                    "Linux doesn't have; on its own this is harmless (iOS Studio gives lldb the iOS SDK). Repeats are "
                    "hidden.")
        m = _MISSING_PCM.search(line)
        example = f" (e.g. {os.path.basename(m.group(1))})" if m else ""
        return (f"lldb: module files from the app's build are missing{example}, so some Swift types can't be shown "
                "and expressions may fail. Run the app again to rebuild them. Repeats are hidden.")

    def text(self, text: str) -> str | None:
        """What to show of one console text (None: nothing)."""
        keep = []
        for ln in text.splitlines():
            kind = "hostinfo" if _HOSTINFO.search(ln) else \
                "modules" if (_MISSING_PCM.search(ln) or _MISSING_TYPES.search(ln)) else None
            if kind is None:
                keep.append(ln)
                continue
            if kind == "modules":
                self.missing_modules = True
            seen = self.hidden.get(kind, 0)
            self.hidden[kind] = seen + 1
            if not seen:
                keep.append(self._summary(kind, ln))
        out = "\n".join(keep).strip("\n")
        return out or None

    def summary(self) -> str | None:
        label = {"hostinfo": "HostInfoError lines", "modules": "missing-module warnings"}
        parts = [f"{n - 1} {label[k]}" for k, n in self.hidden.items() if n > 1]
        return f"Hidden repeats of lldb messages: {', '.join(parts)}." if parts else None


async def _stop(dev, bid: str, st: dict, kill: bool) -> dict | None:
    with contextlib.suppress(Exception):
        return await dev.request("debug.stop", {"bundleId": bid, "kill": kill, "pid": st.get("pid")}, timeout=30)
    return None


def demo(action: str, flags: dict) -> int:
    from iosstudio.demo import debugger_demo
    return debugger_demo.run(action, flags)


def cmd_editor_config(flags: dict) -> int:
    from iosstudio.features import editor_debug_bridge
    return editor_debug_bridge.cmd_editor_config(flags)


def cmd_editor(flags: dict) -> int:
    from iosstudio.features import editor_debug_bridge
    return editor_debug_bridge.cmd_editor(flags)


def cmd_editor_status(flags: dict) -> int:
    from iosstudio.features import editor_debug_bridge
    return editor_debug_bridge.cmd_editor_status(flags)


GROUPS = {"debug": {"check": cmd_check, "sources": cmd_sources, "backtrace": cmd_backtrace, "session": cmd_session,
                    "editor-config": cmd_editor_config, "editor": cmd_editor, "editor-status": cmd_editor_status}}
