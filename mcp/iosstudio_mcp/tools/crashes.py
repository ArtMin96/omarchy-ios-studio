"""Crash reports from the iPhone (.ips): list, parsed summary, full text, and a resource template."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from pydantic import Field

from .. import helper
from ..backends.crashes import Section, crash_detail
from ..backends.device import apps, dcall, match_app
from ..core import budget as B
from ..core.params import Udid
from ..core.registry import MAX_WAIT, page_of, tool
from ..models import CrashBrief, CrashDetail, CrashList
from ..state import get_state

RESOURCE_CAP = 100_000


def capped(p: Path, tool_hint: str) -> str:
    """The first RESOURCE_CAP bytes of a file as text (blocking: call it in a thread)."""
    with open(p, "rb") as f:
        head = f.read(RESOURCE_CAP + 1)
        size = os.fstat(f.fileno()).st_size
    if size <= RESOURCE_CAP:
        return head.decode(errors="replace")
    text = head[:RESOURCE_CAP].decode(errors="ignore")
    text = text[:text.rfind("\n") + 1] or text
    return text + f"\n…[cut at {RESOURCE_CAP // 1000} KB of {size // 1000} KB; read the rest with {tool_hint}]\n"


def _crash_dirs() -> list[Path]:
    dirs = [helper.appdirs.cache_dir() / "crashes"]   # where the device daemon keeps pulled reports
    if helper.demo_mode():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("iosstudio_demo_art", helper.REPO / "helper" / "iosstudio" / "demo" / "demo_art.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            dirs.insert(0, Path(mod.demo_dir()) / "crashes")
        except Exception:  # noqa: BLE001
            pass
    return dirs


def register(mcp: MCPServer) -> None:

    @tool(mcp, "List crash reports", kind="read_remote")
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
        items = await dcall(state, "crash.list", args, udid=udid, timeout=MAX_WAIT) or []
        if process:
            items = [c for c in items if c.get("process") == process]
        if kind != "any":
            items = [c for c in items if c.get("kind") == kind]
        items.sort(key=lambda c: c.get("ts") or 0, reverse=True)
        out, _ = page_of(items, 0, limit, lambda c: CrashBrief(name=str(c.get("name")), process=c.get("process"),
                                                               date=c.get("date"), kind=c.get("kind")))
        nxt = []
        note = None
        if len(items) > len(out):
            note = (f"Showing the newest {len(out)} of {len(items)}; narrow with process, bundle_id or kind"
                    + (", or raise limit." if len(out) == limit else "."))
        if out:
            nxt.append(f"ios_crash_get(name='{out[0].name}') for the exception, reason and crashed-thread frames.")
        else:
            nxt.append("No reports" + (f" for {process}" if process else "") + ". If the app quits, reproduce it with "
                       "ios_app_run_and_capture.")
        return CrashList(total=len(items), count=len(out), has_more=len(items) > len(out), crashes=out, next_steps=nxt,
                         truncated=note is not None, omitted=len(items) - len(out), note=note)

    @tool(mcp, "Crash report", kind="read_remote")
    async def ios_crash_get(
        ctx: Context,
        name: Annotated[str, Field(description="name from ios_crash_list (e.g. '/MyApp-2026-11-01-142233.ips'); "
                                               "the bare file name also works")],
        section: Annotated[Section, Field(description="summary: parsed facts and crashed-thread frames. threads: "
                                                     "every thread's frames. images: binary images. raw: the "
                                                     ".ips text")] = "summary",
        start_line: Annotated[int, Field(ge=0, description="With a section other than summary: first line "
                                                           "(page with the result's end_line)")] = 0,
        max_lines: Annotated[int, Field(ge=1, le=5000, description="Lines of the section to return")] = 400,
        full_text: Annotated[bool, Field(description="Same as section='raw'")] = False,
        max_bytes: Annotated[int | None, Field(ge=1000, le=B.MAX_BYTES, description="Raise the size cap for text + "
                                                                                  "structuredContent together (default 12000 bytes)")] = None,
        udid: Udid = None,
    ) -> CrashDetail:
        """One crash report, parsed: process, bundle id, app and iOS version, exception type, signal,
        termination reason (e.g. the Swift fatal error message), and the top 15 frames of the crashed
        thread, plus the local .ips path (and a resource link). The app's own frames are resolved against
        the build that crashed (kept on every build): app_frames has function, file (relative to
        project_path) and line, and symbolicated says full/partial/none with symbolication_note saying why
        and what to do (usually: rebuild that exact version). Unresolved frames read '<image> + 0xOFFSET'. For more, pick a section (threads, images, raw); its lines come as plain text after the
        JSON, paged with start_line, under 12 KB unless max_bytes raises it."""
        state = get_state(ctx)
        return await crash_detail(state, name, udid, "raw" if full_text and section == "summary" else section,
                                  start_line, max_lines, max_bytes)

    @mcp.resource("iosstudio://crashes/{file}", name="crash-report", mime_type="text/plain")
    async def crash_report(file: str, ctx: Context) -> str:
        """Raw text of an iPhone crash report (.ips) by file name, e.g. MyApp-2026-11-01-142233.ips (first
        100 KB; ios_crash_get(section='raw', start_line=...) pages through the rest).
        Reads the local copy if it was already pulled, else pulls it from the active iPhone."""
        if "/" in file or not file.endswith(".ips"):
            raise ResourceError("Expected a .ips file name from ios_crash_list.")

        hint = f"ios_crash_get(name='{file}', section='raw', start_line=...)"

        def local_copy() -> str | None:      # blocking file access: runs in a thread
            for d in _crash_dirs():
                for p in d.glob(f"*/{file}") if d.is_dir() else []:
                    return capped(p, hint)
                if (d / file).is_file():
                    return capped(d / file, hint)
            return None
        text = await asyncio.to_thread(local_copy)
        if text is not None:
            return text
        state = get_state(ctx)
        try:
            det = await crash_detail(state, file)
        except ToolError as exc:
            raise ResourceError(B.clip_error(str(exc)))
        try:
            return await asyncio.to_thread(capped, Path(helper.expand(det.path)), hint)
        except OSError:
            raise ResourceError(f"Couldn't read {det.path}.")
