"""Demo: debug.start, debug.stop, debug.symbols (a made-up debugserver forward and symbol download).

Nothing listens on the demo port: in demo mode the helper's `debug` group never runs lldb
(helper/debugger.py has its own demo), so these only keep the protocol and the symbols card honest.

The symbols set lives only in memory. Its starting state follows the scenario (ready: up to date,
expiring: an outdated set, fresh: none) or IOSSTUDIO_DEMO_SYMBOLS, and `debug.symbols {action:
"demo", state}` switches it to missing | outdated | incomplete | failed | ready | kept (ready with
the download kept), for screenshots and for trying each button.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from deviced_demo_core import APPS, SCENARIO, event, installed, pids, running

GB = 1_000_000_000
TOTAL = int(7.3 * GB)
SET_SIZE = 214_000_000
OLD_SIZE = 168_500_000
LIBS, OLD_LIBS = 89, 80
FORMAT = 2
KEY = "debug.symbols"
SLOW = "so attaching the debugger can take minutes over Wi-Fi and the app stays paused that long"
OUTDATED = ("These symbols were unpacked by an older version of iOS Studio and miss system libraries, " + SLOW + ".")
STATES = ("missing", "outdated", "incomplete", "failed", "ready", "kept")


class Symbols:
    def __init__(self):
        self.state: dict = {"phase": "idle"}
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.keep = False
        self.seed(os.environ.get("IOSSTUDIO_DEMO_SYMBOLS") or
                  {"expiring": "outdated", "fresh": "missing"}.get(SCENARIO, "ready"))

    def seed(self, name: str):
        """The made-up disk: mark (None | format), missing libs, a whole download kept, last error."""
        self.mark = {"missing": None, "failed": None}.get(name, 1 if name == "outdated" else FORMAT)
        self.missing = ["usr/lib/system/libdispatch.dylib"] if name == "incomplete" else []
        self.dsc = name in ("kept", "failed")
        self.keep = name == "kept" or (self.keep and name != "missing")
        self.state = ({"phase": "failed", "ios": "27.2", "build": "24B115",
                       "message": "Unpacking the debug symbols failed.",
                       "error": {"code": "failed", "message": "Unpacking the debug symbols failed.",
                                 "detail": "ipsw: exit status 1"}} if name == "failed" else {"phase": "idle"})

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def publish(self, **fields):
        self.state = {**self.state, **fields, "ts": time.time()}
        event("debugSymbols", dict(self.state))

    def loop(self, download: bool):
        got = 0
        rate = 140_000_000
        while download and got < TOTAL:
            if self.stop_flag.wait(0.4):
                self.dsc = False
                self.publish(phase="cancelled", message="Cancelled. The symbols you had before are unchanged."
                             if self.mark else "Cancelled.", ready=bool(self.mark))
                return
            got = min(TOTAL, got + int(rate * 0.4 * 6))   # six times faster than a real phone
            self.publish(phase="downloading", received=got, total=TOTAL, fraction=got / TOTAL, rate=rate,
                         eta=int((TOTAL - got) / rate / 6), message="Copying system libraries from your iPhone…")
        self.dsc = True
        for n in range(1, 9):
            if self.stop_flag.wait(0.3):
                self.publish(phase="cancelled", message="Cancelled. The symbols you had before are unchanged."
                             if self.mark else "Cancelled.", ready=bool(self.mark))
                return
            done = min(LIBS, n * 12)
            self.publish(phase="extracting", done=done, count=LIBS, fraction=n / 8,
                         message=f"Unpacking system libraries ({done} of {LIBS})…")
        self.mark, self.missing = FORMAT, []
        self.dsc = self.keep
        self.publish(phase="done", fraction=1.0, message="Debug symbols are ready.", ready=True,
                     sizeOnDisk=SET_SIZE, libs=LIBS)

    def start(self, download: bool = True):
        if not self.running():
            self.stop_flag.clear()
            self.state = {"phase": "starting", "ios": "27.2", "build": "24B115"}
            self.thread = threading.Thread(target=self.loop, args=(download,), daemon=True)
            self.thread.start()
        return self

    def stop(self, *_):
        self.stop_flag.set()

    def assess(self) -> dict:
        upd = {"needed": False, "reason": None, "action": "unpack" if self.dsc else "download"}
        out = {"state": "missing", "ready": False, "update": upd, "libs": 0, "format": self.mark,
               "downloadKept": TOTAL if self.dsc else 0, "canUnpack": self.dsc, "sizeOnDisk": 0}
        if self.mark is None:
            return out
        out.update(ready=True, libs=OLD_LIBS if self.mark < FORMAT else LIBS - len(self.missing),
                   sizeOnDisk=OLD_SIZE if self.mark < FORMAT else SET_SIZE)
        if self.mark < FORMAT:
            out["state"] = "outdated"
            upd.update(needed=True, reason=OUTDATED)
        elif self.missing:
            out.update(state="incomplete", missing=list(self.missing))
            upd.update(needed=True, reason="libdispatch is missing from these symbols, " + SLOW + ".")
        else:
            out["state"] = "ready"
        return out


def register(reg):
    sym = Symbols()

    def status() -> dict:
        base = str(Path.home() / ".cache/ios-studio/DeviceSupport/27.2 (24B115)")   # never created in demo mode
        out = {"ios": "27.2", "build": "24B115", "dir": base, "sysroot": base, **sym.assess(),
               "downloading": sym.running(), "estimatedBytes": TOTAL, "estimated": False, "freeBytes": 182 * GB,
               "ipsw": True, "keepDownload": sym.keep, "currentFormat": FORMAT}
        phase = sym.state.get("phase")
        if sym.running():
            out["state"] = "extracting" if phase == "extracting" else "downloading"
        elif phase == "failed" and out["state"] in ("missing", "outdated", "incomplete"):
            out["error"] = sym.state.get("error")
            if out["state"] == "missing":
                out["state"] = "failed"
        if sym.running() or phase in ("failed", "cancelled"):
            out["progress"] = dict(sym.state)
        return out

    @reg.command("debug.start")
    async def start(ctx, a):
        bid = str(a.get("bundleId") or "")
        app = next((x for x in APPS if x[1] == bid and bid in installed), None)
        if app is None:
            raise_error("not-found", "That app isn't installed on your iPhone.")
        name, _, _, _, dev, *_ = app
        if not dev:
            raise_error("not-debuggable", "Only your own apps can be debugged: apps you built and signed for "
                        "development. App Store and system apps can't be.")
        launch = str(a.get("launch") or "auto")
        launched = launch == "suspended" or (launch == "auto" and bid not in running)
        if launch == "none":
            await ctx.resources.acquire(f"debug:{bid}", ctx.client(), lambda: object(), None, scope="tunnel", timeout=3600)
            return {"bundleId": bid, "port": 52917, "host": "127.0.0.1", "pid": None, "launched": False}
        if launch == "attach" and bid not in running:
            raise_error("not-running", "That app isn't running. Open it on your iPhone, or start it from the "
                        "debugger (it then waits for the debugger before it runs).")
        await ctx.resources.acquire(f"debug:{bid}", ctx.client(), lambda: object(), None, scope="tunnel", timeout=3600)
        if launched:
            running.add(bid)
            pids[bid] = 913
        exe = name.replace(" ", "")
        return {"bundleId": bid, "port": 52917, "host": "127.0.0.1", "pid": pids.get(bid, 913), "launched": launched,
                "remotePath": f"/private/var/containers/Bundle/Application/DEMO/{exe}.app/{exe}", "executable": exe,
                "ios": "27.2", "build": "24B115", "sysroot": status()["sysroot"], "symbolsReady": sym.assess()["ready"],
                "symbolsState": sym.assess()["state"], "symbolsUpdate": sym.assess()["update"]}

    @reg.command("debug.stop")
    async def stop(ctx, a):
        bid = str(a.get("bundleId") or "")
        left = await ctx.resources.release(f"debug:{bid}", ctx.client())
        killed = None
        if a.get("kill") and a.get("pid"):
            running.discard(bid)
            killed = int(a["pid"])
        return {"bundleId": bid, "stopped": left == 0, "sharedWith": left, "killed": killed}

    @reg.command("debug.symbols")
    async def symbols(ctx, a):
        action = str(a.get("action") or "status")
        if "keepDownload" in a:
            sym.keep = bool(a["keepDownload"])
            if not sym.keep and not sym.running() and sym.assess()["state"] == "ready":
                sym.dsc = False
        if action in ("download", "unpack"):
            if sym.running():
                return status()
            st = sym.assess()
            if action == "download" and st["state"] == "ready" and not a.get("force"):
                return status()
            if action == "unpack" and not st["canUnpack"]:
                raise_error("no-download", "There's no kept download to unpack. Download the debug symbols again.")
            await ctx.resources.acquire(KEY, ctx.client(), lambda: sym.start(action == "download"), sym.stop,
                                        scope="tunnel" if action == "download" else "session")
            return {**status(), "started": True, **({"total": TOTAL} if action == "download" else {})}
        elif action == "cancel":
            await ctx.resources.close(KEY, "released")
            sym.stop()
        elif action == "remove":
            if sym.running():
                raise_error("busy", "The debug symbols are being downloaded or unpacked. Cancel that first.")
            sym.seed("missing")
        elif action == "demo":
            name = str(a.get("state") or "")
            if name not in STATES:
                raise_error("bad-args", "state must be one of " + ", ".join(STATES) + ".")
            sym.stop()
            sym.seed(name)
        elif action != "status":
            raise_error("bad-args", "action must be status, download, unpack, cancel or remove.")
        return status()

    @reg.info
    def info(ctx, s):
        st = sym.assess()
        return {"debugSymbols": st["ready"], "debugSymbolsState": st["state"]}

    @reg.on_shutdown
    def shutdown(ctx):
        sym.stop()


def raise_error(code: str, message: str):
    import common
    raise common.HelperError(code, message)
