"""Crash reports from the iPhone (.ips): list, parsed summary, full text, and a resource template."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from pydantic import Field

from .. import helper
from ..models import CrashBrief, CrashDetail, CrashList
from ..state import AppState, get_state
from . import READ_REMOTE, tool
from .device import Udid, apps, dcall, match_app

MAX_TEXT = 200_000
FRAMES = 15


def _not_found(name: str) -> str:
    return f"No crash report '{name}'. Call ios_crash_list for valid names."


async def _lookup_name(state: AppState, name: str, udid: str | None) -> str:
    """Accept the device path from ios_crash_list, or just the file name."""
    if name.startswith("/"):
        return name
    items = await dcall(state, "crash.list", {"flush": False}, udid=udid, timeout=30) or []
    hit = next((c for c in items if c.get("file") == name or str(c.get("name", "")).endswith("/" + name)), None)
    if hit is None:
        known = ", ".join(str(c.get("file") or c.get("name")) for c in items[:5])
        raise ToolError(_not_found(name) + (f" Newest: {known}." if known else " The iPhone has none."))
    return str(hit["name"])


async def crash_detail(state: AppState, name: str, udid: str | None = None, full_text: bool = False,
                       max_chars: int = 50_000) -> CrashDetail:
    name = await _lookup_name(state, name, udid)
    res = await dcall(state, "crash.pull", {"name": name}, udid=udid, timeout=60, not_found=_not_found(name)) or {}
    s = res.get("summary") or {}
    path = str(res.get("path") or "")
    text, cut = None, False
    if full_text and path:
        try:
            raw = Path(path).read_text(errors="replace")
        except OSError:
            raw = ""
        cut = len(raw) > max_chars
        text = raw[:max_chars] + (f"\n…[truncated; {len(raw) - max_chars} more characters in {helper.tilde(path)}]"
                                  if cut else "")
    nxt = []
    frames = [str(f) for f in (s.get("frames") or [])][:FRAMES]
    proc = s.get("process")
    if frames and proc:
        own = [f for f in frames if f" {proc} " in f" {f} "]
        if own:
            nxt.append(f"Frames in {proc} point at your code (e.g. '{own[0]}'); find those functions in the project's "
                       "Sources/ folder.")
    if s.get("exceptionType") is None and s.get("terminationReason"):
        nxt.append("No exception: this is a resource/jetsam/hang report; the termination reason explains it.")
    if not full_text:
        nxt.append("full_text=true returns the whole report (all threads, binary images).")
    file = Path(path).name if path else None
    return CrashDetail(name=name, path=helper.tilde(path) or "", resource_uri=f"iosstudio://crashes/{file}" if file else None,
                       process=proc, bundle_id=s.get("bundleId"), app_version=s.get("appVersion"),
                       os_version=s.get("osVersion"), date=s.get("date"), exception_type=s.get("exceptionType"),
                       signal=s.get("signal"), termination_reason=s.get("terminationReason"),
                       crashed_thread=s.get("crashedThread"), frames=frames, text=text, text_truncated=cut,
                       next_steps=nxt)


def _crash_dirs() -> list[Path]:
    dirs = [Path.home() / ".cache/ios-studio/crashes"]
    if helper.demo_mode():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("iosstudio_demo_art", helper.REPO / "helper" / "demo_art.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            dirs.insert(0, Path(mod.demo_dir()) / "crashes")
        except Exception:  # noqa: BLE001
            pass
    return dirs


def register(mcp: MCPServer) -> None:

    @tool(mcp, annotations=READ_REMOTE("List crash reports"))
    async def ios_crash_list(
        ctx: Context,
        process: Annotated[str | None, Field(description="Only reports of this process (the app's executable "
                                                         "name, e.g. 'MyApp')")] = None,
        bundle_id: Annotated[str | None, Field(description="Only this app's reports (resolved to its process "
                                                           "name)")] = None,
        kind: Annotated[Literal["any", "crash", "fault", "resource", "jetsam", "hang", "other"],
                        Field(description="crash = the process died (exception/signal); fault = a non-fatal "
                                          "user-space fault report; jetsam = killed for memory; resource = too much "
                                          "CPU/disk/wakeups; hang = froze")] = "any",
        limit: Annotated[int, Field(ge=1, le=200, description="Max reports to return, newest first")] = 20,
        udid: Udid = None,
    ) -> CrashList:
        """Crash and diagnostic reports on the iPhone, newest first: name (pass it to ios_crash_get),
        process, date and kind. Collects reports still pending on the phone first. Use when an app
        closed by itself, or after ios_app_run_and_capture."""
        state = get_state(ctx)
        if bundle_id and not process:
            a = match_app(await apps(state, udid), bundle_id)
            if a is None:
                raise ToolError(f"No app '{bundle_id}' is installed; pass process (its executable name) instead.")
            process = a.get("executable") or a.get("name")
        args: dict = {"flush": True}
        if process:
            args["process"] = process
        items = await dcall(state, "crash.list", args, udid=udid, timeout=45) or []
        if process:
            items = [c for c in items if c.get("process") == process]
        if kind != "any":
            items = [c for c in items if c.get("kind") == kind]
        items.sort(key=lambda c: c.get("ts") or 0, reverse=True)
        out = [CrashBrief(name=str(c.get("name")), process=c.get("process"), date=c.get("date"), kind=c.get("kind"))
               for c in items[:limit]]
        nxt = []
        if len(items) > limit:
            nxt.append(f"Showing the newest {limit} of {len(items)}; narrow with process, bundle_id or kind, or raise limit.")
        if out:
            nxt.append(f"ios_crash_get(name='{out[0].name}') for the exception, reason and crashed-thread frames.")
        else:
            nxt.append("No reports" + (f" for {process}" if process else "") + ". If the app quits, reproduce it with "
                       "ios_app_run_and_capture.")
        return CrashList(total=len(items), count=len(out), has_more=len(items) > len(out), crashes=out, next_steps=nxt)

    @tool(mcp, annotations=READ_REMOTE("Crash report"))
    async def ios_crash_get(
        ctx: Context,
        name: Annotated[str, Field(description="name from ios_crash_list (e.g. '/MyApp-2026-11-01-142233.ips'); "
                                               "the bare file name also works")],
        full_text: Annotated[bool, Field(description="Also return the raw .ips text")] = False,
        max_chars: Annotated[int, Field(ge=1000, le=MAX_TEXT, description="Cap for full_text")] = 50_000,
        udid: Udid = None,
    ) -> CrashDetail:
        """One crash report, parsed: process, bundle id, app and iOS version, exception type, signal,
        termination reason (e.g. the Swift fatal error message), and the top 15 frames of the crashed
        thread, plus the local .ips path. App frames without symbols read '<image> + 0xOFFSET'. Use the
        frames in the app's own module to find the failing function in the project."""
        state = get_state(ctx)
        return await crash_detail(state, name, udid, full_text, max_chars)

    @mcp.resource("iosstudio://crashes/{file}", name="crash-report", mime_type="text/plain")
    async def crash_report(file: str, ctx: Context) -> str:
        """Raw text of an iPhone crash report (.ips) by file name, e.g. MyApp-2026-11-01-142233.ips.
        Reads the local copy if it was already pulled, else pulls it from the active iPhone."""
        if "/" in file or not file.endswith(".ips"):
            raise ResourceError("Expected a .ips file name from ios_crash_list.")
        for d in _crash_dirs():
            for p in d.glob(f"*/{file}") if d.is_dir() else []:
                return p.read_text(errors="replace")[:MAX_TEXT]
            if (d / file).is_file():
                return (d / file).read_text(errors="replace")[:MAX_TEXT]
        state = get_state(ctx)
        try:
            det = await crash_detail(state, file)
        except ToolError as exc:
            raise ResourceError(str(exc))
        p = Path(helper.expand(det.path))
        try:
            return p.read_text(errors="replace")[:MAX_TEXT]
        except OSError:
            raise ResourceError(f"Couldn't read {det.path}.")
