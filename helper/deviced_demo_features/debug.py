"""Demo: debug.start, debug.stop, debug.symbols (a made-up debugserver forward and symbol download).

Nothing listens on the demo port: in demo mode the helper's `debug` group never runs lldb
(helper/debugger.py has its own demo), so these only keep the protocol and the symbols card honest.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from deviced_demo_core import APPS, event, installed, pids, running

GB = 1_000_000_000
TOTAL = int(5.8 * GB)
KEY = "debug.symbols"


class Symbols:
    def __init__(self):
        self.ready = False
        self.state: dict = {"phase": "idle"}
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def publish(self, **fields):
        self.state = {**self.state, **fields, "ts": time.time()}
        event("debugSymbols", dict(self.state))

    def loop(self):
        got = 0
        rate = 140_000_000
        while got < TOTAL:
            if self.stop_flag.wait(0.4):
                self.publish(phase="cancelled", message="Download cancelled.", ready=False)
                return
            got = min(TOTAL, got + int(rate * 0.4 * 6))   # six times faster than a real phone
            self.publish(phase="downloading", received=got, total=TOTAL, fraction=got / TOTAL, rate=rate,
                         eta=int((TOTAL - got) / rate / 6), message="Copying system libraries from your iPhone…")
        for n in range(1, 9):
            if self.stop_flag.wait(0.3):
                self.publish(phase="cancelled", message="Download cancelled.", ready=False)
                return
            self.publish(phase="extracting", done=n * 14, count=112, fraction=n / 8,
                         message=f"Unpacking the Swift runtime ({n * 14} of 112)…")
        self.ready = True
        self.publish(phase="done", fraction=1.0, message="Debug symbols are ready.", ready=True, sizeOnDisk=286_000_000)

    def start(self):
        if not self.running():
            self.stop_flag.clear()
            self.state = {"phase": "starting", "ios": "27.2", "build": "24B115"}
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()
        return self

    def stop(self, *_):
        self.stop_flag.set()


def register(reg):
    sym = Symbols()

    def status() -> dict:
        base = str(Path.home() / ".cache/ios-studio/DeviceSupport/27.2 (24B115)")   # never created in demo mode
        out = {"ios": "27.2", "build": "24B115", "dir": base, "sysroot": base, "ready": sym.ready,
               "downloading": sym.running(), "estimatedBytes": TOTAL, "estimated": False, "freeBytes": 182 * GB,
               "ipsw": True}
        if sym.ready:
            out["sizeOnDisk"] = 286_000_000
        if sym.running() or sym.state.get("phase") in ("failed", "cancelled"):
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
                "ios": "27.2", "build": "24B115", "sysroot": status()["sysroot"], "symbolsReady": sym.ready}

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
        if action == "download":
            if not sym.ready or a.get("force"):
                if a.get("force"):
                    sym.ready = False
                await ctx.resources.acquire(KEY, ctx.client(), sym.start, sym.stop, scope="tunnel")
                return {**status(), "started": True, "total": TOTAL}
        elif action == "cancel":
            await ctx.resources.close(KEY, "released")
            sym.stop()
        elif action != "status":
            raise_error("bad-args", "action must be status, download or cancel.")
        return status()

    @reg.on_shutdown
    def shutdown(ctx):
        sym.stop()


def raise_error(code: str, message: str):
    import common
    raise common.HelperError(code, message)
