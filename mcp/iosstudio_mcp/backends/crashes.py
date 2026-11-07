"""Crash reports (.ips) from the iPhone: look one up, parse it, render a section, symbolicate frames.

    await crash_detail(state, name, udid, section)   CrashDetail of one report (used by crash, log and
                                                     camera-test tools)
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ResourceLink

from .. import helper
from ..core import budget as B
from ..core.job_status import job_result, settle, shared_job
from ..core.jobs import Job
from ..core.registry import MAX_WAIT
from ..models import CrashDetail, CrashFrame
from ..state import AppState
from .device import dcall

FRAMES = 15
REPORT_CAP = 8 * 1024 * 1024    # bytes of a .ips (or its .symbols.json) read into memory
PULL_TIMEOUT = 120              # crash.pull: copy off the iPhone + symbolicate, in a job (kind 'crash-pull')
PULL_REUSE = 60                 # seconds a finished pull answers repeat calls for the same report
PULL_RUNNING = ("Copying and symbolicating {name} takes longer than one call; it keeps going in the background "
                "(job {job}). Call ios_crash_get again in a few seconds [running].")


async def pull(state: AppState, name: str, udid: str | None = None) -> dict:
    """crash.pull as a shared job: a call that runs out of time leaves the copy running, and the next call for
    the same report joins it (or takes its result) instead of pulling and symbolicating it again. Outside a
    tool call (a job needs the report) it waits until the pull ends."""
    async def run(job: Job) -> dict:
        job.report(None, "copying and symbolicating the crash report")
        return await dcall(state, "crash.pull", {"name": name}, udid=udid, timeout=PULL_TIMEOUT,
                           hints={"not-found": _not_found(name)}) or {}
    job = shared_job(state, "crash-pull", f"{udid or ''}|{name}", run, title=f"Pull crash report {name}",
                     reuse_seconds=PULL_REUSE)
    await settle(state, None, job)
    if not job.finished:
        raise ToolError(PULL_RUNNING.format(name=name, job=job.id))
    return job_result(job) or {}


def _not_found(name: str) -> str:
    return f"No crash report '{name}'. Call ios_crash_list for valid names."


async def _lookup_name(state: AppState, name: str, udid: str | None) -> str:
    """Accept the device path from ios_crash_list, or just the file name."""
    if name.startswith("/"):
        return name
    items = await dcall(state, "crash.list", {"flush": False}, udid=udid, timeout=MAX_WAIT) or []
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


def _resolved(n: int, f: dict, images: list, info: dict | None) -> str:
    """A frame line, with the function and file:line from crash symbolication when it resolved."""
    if not info or not (info.get("function") or info.get("file")):
        return f"{n} {_frame(f, images)}"
    where = ""
    if info.get("file"):
        where = f" ({info['file']}:{info.get('line')}" + (f":{info['column']}" if info.get("column") else "") + ")"
    return f"{n} {info.get('image') or ''} {info.get('function') or ''}".rstrip() + where


def read_report(path: str, cap: int = REPORT_CAP) -> tuple[str, int | None]:
    """(text, size in bytes) of a pulled .ips, at most `cap` bytes of it (a report is rarely over 1 MB).
    Blocking: call it with asyncio.to_thread."""
    try:
        with open(path, "rb") as f:
            data = f.read(cap)
            size = os.fstat(f.fileno()).st_size
    except OSError:
        return "", None
    return data.decode(errors="replace"), size


def load_symbols(path: str) -> dict:
    """Crash symbolication's saved result for a pulled report (<report>.symbols.json, written by the device
    daemon's crash.pull): {"<thread>:<frame>": {function, file, line, ...}}. Blocking: use asyncio.to_thread."""
    try:
        p = Path(path + ".symbols.json")
        if p.stat().st_size > REPORT_CAP:
            return {}
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    frames = data.get("frames") if isinstance(data, dict) else None
    return frames if isinstance(frames, dict) else {}


def render_section(raw: str, section: str, frames_per_thread: int = 25, symbols: dict | None = None) -> list[str]:
    """Readable lines for one part of a .ips report: every thread's frames (app frames with file:line when
    `symbols` resolved them), or the binary images. Falls back to the raw lines when the body isn't the
    iOS 15+ JSON format."""
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
            out += [f"  {_resolved(n, f, images, (symbols or {}).get(f'{i}:{n}'))}"
                    for n, f in enumerate(frames[:frames_per_thread])]
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
    res = await pull(state, name, udid)
    s = res.get("summary") or {}
    path = str(res.get("path") or "")
    file = Path(path).name if path else None
    size = None
    raw = ""
    if path:
        raw, size = await asyncio.to_thread(read_report, path)
    nxt = []
    frames = [B.clip(f, 300) for f in (s.get("frames") or [])][:FRAMES]
    proc = s.get("process")
    sym = s.get("symbolication") if isinstance(s.get("symbolication"), dict) else {}
    app_frames = [CrashFrame(index=n, function=B.clip(i.get("function"), 300) if i.get("function") else None,
                             file=i.get("file"), line=i.get("line"), column=i.get("column"))
                  for n, i in enumerate(sym.get("frames") or []) if isinstance(i, dict)][:FRAMES]
    status = sym.get("status") if sym.get("status") in ("full", "partial", "none", "not-needed") else None
    sym_note = " ".join(x for x in (sym.get("message"), sym.get("hint")) if x) or None
    top = next((f for f in app_frames if f.file), None)
    if top:
        nxt.append(f"The crash is in {top.function or 'your code'} at {top.file}:{top.line} (frame {top.index}"
                   + (f", project {helper.tilde(sym['project'])}" if sym.get("project") else "") + "); open that line.")
    elif frames and proc:
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
                      symbolicated=status, symbolication_note=B.clip(sym_note, 600) if sym_note else None,
                      project_path=helper.tilde(sym["project"]) if sym.get("project") else None,
                      app_frames=app_frames, next_steps=nxt, full_output_path=helper.tilde(path) or None).with_budget(max_bytes, links)
    if section != "summary" and raw:
        symbols = {} if section == "raw" else await asyncio.to_thread(load_symbols, path)
        lines = raw.splitlines() if section == "raw" else render_section(raw, section, symbols=symbols)
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
        out.app_frames = []
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
