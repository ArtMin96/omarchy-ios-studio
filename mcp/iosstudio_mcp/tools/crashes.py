"""Crash reports from the iPhone (.ips): list, parsed summary, full text, and a resource template."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ResourceLink
from pydantic import Field

from .. import budget as B
from .. import helper
from ..models import CrashBrief, CrashDetail, CrashList
from ..state import AppState, get_state
from . import READ_REMOTE, page_of, tool
from .device import Udid, apps, dcall, match_app

RESOURCE_CAP = 100_000
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


Section = Literal["summary", "threads", "images", "raw"]


def _frame(f: dict, images: list) -> str:
    idx = f.get("imageIndex")
    img = "?"
    if isinstance(idx, int) and 0 <= idx < len(images):
        img = images[idx].get("name") or Path(str(images[idx].get("path") or "?")).name
    sym = f.get("symbol")
    if sym:
        off = f.get("symbolLocation")
        return f"{img} {sym}" + (f" + {off}" if off else "")
    return f"{img} + 0x{int(f.get('imageOffset') or 0):x}"


def render_section(raw: str, section: str, frames_per_thread: int = 25) -> list[str]:
    """Readable lines for one part of a .ips report: every thread's frames, or the binary images. Falls back
    to the raw lines when the body isn't the iOS 15+ JSON format."""
    _, _, body = raw.partition("\n")
    try:
        b = json.loads(body)
    except json.JSONDecodeError:
        b = None
    if not isinstance(b, dict):
        return raw.splitlines()
    images = b.get("usedImages") or []
    out: list[str] = []
    if section == "threads":
        threads = b.get("threads") or []
        if not threads and b.get("frames"):        # simplified reports: one frame list
            return [str(f) for f in b["frames"]]
        for i, t in enumerate(threads):
            name = " ".join(x for x in (t.get("name"), t.get("queue")) if x)
            out.append(f"Thread {i}{' CRASHED' if t.get('triggered') else ''}{(': ' + name) if name else ''}")
            frames = t.get("frames") or []
            out += [f"  {n} {_frame(f, images)}" for n, f in enumerate(frames[:frames_per_thread])]
            if len(frames) > frames_per_thread:
                out.append(f"  … {len(frames) - frames_per_thread} more frames")
    elif section == "images":
        for im in images:
            base = im.get("base")
            out.append(f"{im.get('name') or Path(str(im.get('path') or '?')).name} "
                       f"{hex(base) if isinstance(base, int) else base or ''} {im.get('arch') or ''} "
                       f"{im.get('uuid') or ''} {im.get('path') or ''}".rstrip())
    return out


async def crash_detail(state: AppState, name: str, udid: str | None = None, section: str = "summary",
                       start_line: int = 0, max_lines: int = 400, max_bytes: int | None = None) -> CrashDetail:
    name = await _lookup_name(state, name, udid)
    res = await dcall(state, "crash.pull", {"name": name}, udid=udid, timeout=60, not_found=_not_found(name)) or {}
    s = res.get("summary") or {}
    path = str(res.get("path") or "")
    file = Path(path).name if path else None
    size = None
    raw = ""
    if path:
        try:
            raw = Path(path).read_text(errors="replace")
            size = len(raw.encode())
        except OSError:
            raw = ""
    nxt = []
    frames = [B.clip(f, 300) for f in (s.get("frames") or [])][:FRAMES]
    proc = s.get("process")
    if frames and proc:
        own = [f for f in frames if f" {proc} " in f" {f} "]
        if own:
            nxt.append(f"Frames in {proc} point at your code (e.g. '{own[0]}'); find those functions in the project's "
                       "Sources/ folder.")
    if s.get("exceptionType") is None and s.get("terminationReason"):
        nxt.append("No exception: this is a resource/jetsam/hang report; the termination reason explains it.")
    if section == "summary":
        nxt.append("section='threads' lists every thread's frames, 'images' the binary images, 'raw' the .ips "
                   "text (paged with start_line).")
    links = []
    if file:
        links.append(ResourceLink(type="resource_link", uri=f"iosstudio://crashes/{file}", name=file,
                                  mime_type="text/plain", size=size, description=f"Raw crash report ({size} bytes)"))
    out = CrashDetail(name=name, path=helper.tilde(path) or "", resource_uri=f"iosstudio://crashes/{file}" if file else None,
                      process=proc, bundle_id=s.get("bundleId"), app_version=s.get("appVersion"),
                      os_version=s.get("osVersion"), date=s.get("date"), exception_type=s.get("exceptionType"),
                      signal=s.get("signal"),
                      termination_reason=B.clip(s.get("terminationReason"), 600) if s.get("terminationReason") else None,
                      crashed_thread=s.get("crashedThread"), frames=frames, size=size, section=section,
                      next_steps=nxt, full_output_path=helper.tilde(path) or None).with_budget(max_bytes, links)
    if section != "summary" and raw:
        lines = raw.splitlines() if section == "raw" else render_section(raw, section)
        window = [B.clip(ln, 1000) for ln in lines[start_line:start_line + max_lines]]

        def page_note(end: int) -> str | None:
            if end >= len(lines) and start_line == 0:
                return None
            return (f"Showing {section} lines {start_line}–{end - 1} of {len(lines)}"
                    + (f"; call again with start_line={end} for more" if end < len(lines) else "")
                    + f". The whole report is {helper.tilde(path)}.")

        # Size the window by what the other fields (frames, reason, the note itself) leave of the budget, so
        # the result never needs cutting: a cut would leave end_line past lines the caller never saw.
        out.frames = []                     # the summary's; a section page is about its own lines
        if out.termination_reason:
            out.termination_reason = B.clip(out.termination_reason, 200)
        out.start_line, out.end_line, out.total_lines = start_line, len(lines), len(lines)
        out.note, out.truncated = page_note(start_line), True
        others = B.result_size(B.dump(out), CrashDetail.BULK)
        avail = max(500, int((B.ceiling(out) - others - 200) * 0.47))
        page = B.take(window, len(window), avail, from_end=False)
        end = start_line + len(page.items)
        out.text, out.end_line = "\n".join(page.items), end
        out.note = page_note(end)
        out.truncated = out.note is not None
    return out


def cap_text(text: str, tool_hint: str, cap: int = RESOURCE_CAP) -> str:
    """Resources are capped at 100 KB, with a pointer to the tool that pages through the rest."""
    if len(text.encode()) <= cap:
        return text
    head = text.encode()[:cap].decode(errors="ignore")
    head = head[:head.rfind("\n") + 1] or head
    return head + f"\n…[cut at {cap // 1000} KB of {len(text.encode()) // 1000} KB; read the rest with {tool_hint}]\n"


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

    @tool(mcp, annotations=READ_REMOTE("Crash report"))
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
        thread, plus the local .ips path (and a resource link). App frames without symbols read
        '<image> + 0xOFFSET'. Use the frames in the app's own module to find the failing function in the
        project. For more, pick a section (threads, images, raw); its lines come as plain text after the
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

        def capped(p: Path) -> str:
            return cap_text(p.read_text(errors="replace"), f"ios_crash_get(name='{file}', section='raw', "
                                                          "start_line=...)")
        for d in _crash_dirs():
            for p in d.glob(f"*/{file}") if d.is_dir() else []:
                return capped(p)
            if (d / file).is_file():
                return capped(d / file)
        state = get_state(ctx)
        try:
            det = await crash_detail(state, file)
        except ToolError as exc:
            raise ResourceError(B.clip_error(str(exc)))
        try:
            return capped(Path(helper.expand(det.path)))
        except OSError:
            raise ResourceError(f"Couldn't read {det.path}.")
