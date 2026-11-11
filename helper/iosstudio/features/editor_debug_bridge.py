"""`debug editor`: hold the debugserver forward open for the editor's lldb-dap (see editor_debug.py).

  debug editor --project P | --bundle-id B [--launch auto|attach|suspended] [--udid U] [--editor E] [--no-config]

A stream: writes the editor config (unless --no-config), asks the device daemon for `debug.start` (auto: attach
when the app runs, else launch it suspended, waiting for the debugger), writes the attach file with the port and
pid, emits `ready`, then stays open until cancelled ({"cancel": true} or EOF on stdin, SIGTERM), the app exits,
or MAX_LIFETIME. Meanwhile `editor {state: attached|detached}` follows the editor's connection to the forward
(seen in /proc/net/tcp, once a second), and the forward is kept alive (`debug.start` again every KEEPALIVE s,
since the daemon closes an untouched one after an hour). At the end: `debug.stop` (a suspended launch that no
editor ever attached to is killed), the attach file goes back to the stub, `done {ok, data: {ended, ...}}`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from pathlib import Path

from iosstudio.core import output
from iosstudio.core.output import HelperError, fix
from iosstudio.features import editor_debug as ed

MAX_LIFETIME = 2 * 3600       # seconds a bridge stays open
KEEPALIVE = 15 * 60           # seconds between debug.start refreshes of the forward
POLL = 1.0                    # seconds between looks at the editor's connection
APP_POLL = 10.0               # seconds between checks that the app still runs
PROJECTS_FIX = fix("Open Projects", "projects")


def _lifetime() -> float:
    with contextlib.suppress(ValueError):
        return float(os.environ.get("IOSSTUDIO_EDITOR_DEBUG_LIFETIME") or MAX_LIFETIME)
    return MAX_LIFETIME


def _resolve(flags: dict) -> tuple[str, Path | None, dict | None, bool]:
    """(bundle id, project path, project info, the id is the installed one) from --project or --bundle-id."""
    from iosstudio.features import debugger
    if flags.get("project") and flags.get("project") is not True:
        from iosstudio.features.editor_setup.commands import project_flag
        from iosstudio.features.projects import model
        path = project_flag(flags)
        info = model.project_info(path)
        if not info.get("bundleId"):
            raise HelperError("no-bundle-id", "This project has no bundle ID yet (xtool.yml sets it). Build it once "
                              "in iOS Studio first.", PROJECTS_FIX)
        return str(info["bundleId"]), path, info, False
    bid = str(flags.get("bundle_id") or "")
    if not bid or bid == "True":
        raise HelperError("bad-args", "Which app? (--project or --bundle-id missing)")
    path, info = debugger.find_project(bid)
    return bid, path, info, True


def match_installed(apps: list, want: str) -> str | None:
    """The installed bundle id for `want`: exact, or a free team's XTL-<TEAM>.<id>."""
    ids = [str(a.get("bundleId")) for a in apps if isinstance(a, dict) and a.get("bundleId")]
    if want in ids:
        return want
    plain = ed.plain_bundle_id(want)
    return next((b for b in ids if b == plain or (b.startswith("XTL-") and b.endswith("." + plain))), None)


def cmd_editor(flags: dict) -> int:
    from iosstudio.features import debugger
    try:
        launch = str(flags.get("launch") or "auto")
        if launch not in ("auto", "attach", "suspended"):
            raise HelperError("bad-args", "--launch must be auto, attach or suspended.")
        bid, path, info, installed = _resolve(flags)
    except HelperError as exc:
        return output.done(False, error=exc)
    output.event("debug", mode="editor", bundleId=bid)
    output.step("lldb", "Checking the debugger", "running")
    try:
        tc = debugger.toolchain()
        if not Path(tc.lldb).with_name("lldb-dap").is_file():
            raise HelperError("needs-lldb", "This Swift toolchain has no lldb-dap, which editors debug with. "
                              "Install a newer Swift in Setup.", debugger.SETUP_SWIFT)
    except HelperError as exc:
        output.step("lldb", "Checking the debugger", "failed", error=exc.to_json())
        return output.done(False, error=exc)
    output.step("lldb", "Checking the debugger", "done", detail=tc.version)
    app = str((info or {}).get("name") or (info or {}).get("product") or ed.plain_bundle_id(bid).rsplit(".", 1)[-1])
    exe = debugger.local_executable(path, info) if path else None
    configured: list[str] = []
    if path and not flags.get("no_config"):
        output.step("config", "Writing the editor's debug configuration", "running")
        try:
            cfg = ed.editor_config(path, flags.get("editor") or None)
        except HelperError as exc:
            output.step("config", "Writing the editor's debug configuration", "failed", error=exc.to_json())
            return output.done(False, error=exc)
        configured = [e["id"] for e in cfg["editors"] if e["state"] == "present" or e["action"].get("done")]
        output.step("config", "Writing the editor's debug configuration", "done",
                    detail=", ".join(e["name"] for e in cfg["editors"]) or "no editor")
        for n in cfg["notes"]:
            output.event("notice", id="editor-config", level="info", message=n)
    if not exe:
        output.event("notice", id="no-local-build", level="warn",
                     message="No build of this app was found on this computer, so breakpoints in your code won't "
                             "resolve. Build and run the app from iOS Studio first.")
    plain = ed.plain_bundle_id((info or {}).get("bundleId") or bid)
    return asyncio.run(_run(flags, launch, bid, installed, plain, app, path, exe, configured))


async def _run(flags: dict, launch: str, bid: str, installed: bool, plain: str, app: str, path: Path | None,
               exe: str | None, configured: list[str]) -> int:
    from iosstudio.device.client import DeviceClient, DeviceError
    udid = flags.get("udid") or None
    title = "Starting the debugger on your iPhone"
    async with DeviceClient(name="debug editor", events=[]) as dev:
        output.step("forward", title, "running")
        try:
            target = bid
            if not installed:
                apps = (await dev.request("apps.list", {"udid": udid, "icons": False}, timeout=60) or {}).get("apps") or []
                target = match_installed(apps, bid) or ""
                if not target:
                    raise HelperError("not-installed", f"{app} isn't installed on your iPhone. Press Run on its card "
                                      "first, then Debug in editor.")
            st = await dev.request("debug.start", {"bundleId": target, "launch": launch, "udid": udid}, timeout=90)
        except (DeviceError, HelperError) as exc:
            output.step("forward", title, "failed", error=exc.to_json())
            return output.done(False, error=exc)
        output.step("forward", title, "done", detail=f"port {st.get('port')}")
        bridge = Bridge(dev, flags, target, plain, app, path, exe, configured, st)
        return await bridge.run()


class Bridge:
    def __init__(self, dev, flags, target, plain, app, path, exe, configured, st):
        self.dev, self.udid = dev, flags.get("udid") or None
        self.target, self.plain, self.app, self.path, self.exe = target, plain, app, path, exe
        self.configured, self.st = configured, st
        self.helper_pid, self.helper_start = ed.helper_identity()
        self.editor = "waiting"
        self.ever_attached = False
        self.stop = asyncio.Event()
        self.ended = "cancelled"
        self.since = ed.now_iso()

    def attach_kw(self) -> dict:
        st = self.st
        return {"port": int(st["port"]), "host": st.get("host") or "127.0.0.1", "pid": int(st.get("pid") or 0),
                "helper_pid": self.helper_pid, "helper_start": self.helper_start, "local_exe": self.exe,
                "remote_path": st.get("remotePath"), "sysroot": st.get("sysroot") if st.get("symbolsReady") else None,
                # tests drive a local lldb-server instead of a phone (IOSSTUDIO_DEBUG_PLATFORM=remote-linux)
                "platform": os.environ.get("IOSSTUDIO_DEBUG_PLATFORM") or "remote-ios"}

    def save(self) -> None:
        st = self.st
        with contextlib.suppress(OSError):
            ed.save_state(self.plain, {
                "bundleId": self.plain, "installedBundleId": self.target, "appName": self.app,
                "project": str(self.path) if self.path else None, "port": st.get("port"), "host": st.get("host"),
                "pid": st.get("pid"), "launched": bool(st.get("launched")), "editor": self.editor,
                "since": self.since, "helperPid": self.helper_pid, "helperStart": self.helper_start,
                "scriptPath": str(ed.script_path(self.plain))})

    def finish(self, why: str) -> None:
        if not self.stop.is_set():
            self.ended = why
            self.stop.set()

    async def run(self) -> int:
        st = self.st
        try:
            script = ed.write_attach(self.plain, self.app, **self.attach_kw())
        except OSError as exc:
            await self._release(kill=bool(st.get("launched")))
            return output.done(False, error=HelperError("write-failed", "Couldn't write the editor's attach file.",
                                                        detail=str(exc)))
        self.save()
        ed.prune()
        if not st.get("symbolsReady"):
            output.event("notice", id="no-symbols", level="info",
                         message="This iPhone's debug symbols aren't downloaded, so Swift values (String, Array) may "
                                 "read wrong. Download them once under iPhone › Debug › Debug symbols.")
        elif (st.get("symbolsUpdate") or {}).get("needed"):
            output.event("notice", id="symbols-update", level="warn",
                         message="Attaching may take minutes over Wi-Fi until the debug symbols are updated "
                                 "(iPhone › Debug › Debug symbols › Update). The app stays paused meanwhile.")
        cfg = ed.config_name(self.app)
        if st.get("launched"):
            output.event("notice", id="waiting", level="info",
                         message=f"{self.app} is started and waits for the debugger: start “{cfg}” in your editor now.")
        output.event("ready", bundleId=self.target, appName=self.app, port=st.get("port"),
                     host=st.get("host") or "127.0.0.1", pid=st.get("pid"), launched=bool(st.get("launched")),
                     scriptPath=str(script), editors=self.configured, configName=cfg,
                     symbolsReady=bool(st.get("symbolsReady")), localBuild=self.exe)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.finish, "cancelled")
        tasks = [asyncio.create_task(x()) for x in (self._stdin, self._watch_editor, self._watch_app, self._keepalive)]
        try:
            await asyncio.wait_for(self.stop.wait(), _lifetime())
        except asyncio.TimeoutError:
            self.ended = "timeout"
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.editor == "attached":
            output.event("notice", id="editor-cut", level="warn",
                         message="Your editor was still attached; its debugging session ends now.")
        kill = bool(st.get("launched")) and not self.ever_attached and self.ended != "app-exited"
        stopped = await self._release(kill=kill)
        data = {"ended": self.ended, "bundleId": self.target, "appName": self.app, "pid": st.get("pid"),
                "launched": bool(st.get("launched")), "attached": self.ever_attached,
                "killed": (stopped or {}).get("killed"), "scriptPath": str(ed.script_path(self.plain))}
        return output.done(True, data)

    async def _release(self, kill: bool) -> dict | None:
        """debug.stop, then the attach file back to the stub and the bridge's state file gone."""
        from iosstudio.features.debugger import _stop
        stopped = await _stop(self.dev, self.target, self.st, kill)
        with contextlib.suppress(OSError):
            ed.write_stub(self.plain, self.app)
        with contextlib.suppress(OSError):
            ed.clear_state(self.plain)
        return stopped

    async def _stdin(self) -> None:
        from iosstudio.features.debugger import _stdin_reader
        reader = await _stdin_reader()
        if reader is None:
            return
        while True:
            raw = await reader.readline()
            if not raw:
                self.finish("cancelled")
                return
            with contextlib.suppress(ValueError):
                msg = json.loads(raw)
                if isinstance(msg, dict) and (msg.get("cancel") or msg.get("cmd") in ("stop", "detach")):
                    self.finish("cancelled")
                    return

    async def _watch_editor(self) -> None:
        while True:
            n = await asyncio.to_thread(ed.tcp_peers, int(self.st["port"]))
            state = "attached" if n else ("detached" if self.ever_attached else "waiting")
            if state != self.editor:
                self.editor = state
                if state == "attached":
                    self.ever_attached = True
                output.event("editor", state=state, connections=n)
                self.save()
            await asyncio.sleep(POLL)

    async def _watch_app(self) -> None:
        pid = self.st.get("pid")
        if not pid:
            return
        misses = 0
        while True:
            await asyncio.sleep(APP_POLL)
            try:
                procs = await self.dev.request("processes.list", {"udid": self.udid, "appsOnly": True}, timeout=20)
            except Exception:  # noqa: BLE001 - a phone that's briefly away isn't an app that exited
                continue
            if isinstance(procs, list) and not any(isinstance(p, dict) and p.get("pid") == pid for p in procs):
                misses += 1
                if misses >= 2:      # twice in a row: a list taken mid-launch can miss it
                    self.finish("app-exited")
                    return
            else:
                misses = 0

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE)
            try:
                st = await self.dev.request("debug.start", {"bundleId": self.target, "launch": "none",
                                                            "udid": self.udid}, timeout=60)
            except Exception as exc:  # noqa: BLE001
                output.event("notice", id="keepalive", level="warn",
                             message="Couldn't refresh the connection to the debugger on your iPhone.",
                             detail=str(exc)[:300])
                continue
            if st.get("port") and st.get("port") != self.st.get("port"):
                self.st = {**self.st, "port": st["port"], "host": st.get("host") or self.st.get("host")}
                with contextlib.suppress(OSError):
                    ed.write_attach(self.plain, self.app, **self.attach_kw())
                self.save()
                output.event("forward", port=st["port"], host=self.st.get("host") or "127.0.0.1")


def cmd_editor_config(flags: dict) -> int:
    from iosstudio.features.editor_setup.commands import project_flag
    p = project_flag(flags)
    return output.ok(ed.editor_config(p, flags.get("editor") or None, dry_run=bool(flags.get("dry_run"))))


def cmd_editor_status(flags: dict) -> int:
    bid = flags.get("bundle_id")
    return output.ok({"bridges": ed.bridges(str(bid) if bid and bid is not True else None),
                      "checkedAt": time.time()})
