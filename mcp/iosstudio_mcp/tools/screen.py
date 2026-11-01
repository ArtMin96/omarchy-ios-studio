"""Screen tools: short screen recordings for bug reports and the state of the live screen view.

Both go through the shared device daemon (record.* and mirror.status, docs/deviced-protocol.md#screen),
so the agent and the iOS Studio window share one connection and one mirror. A recording runs as a small
background job inside this server: ios_screen_record starts it and waits up to wait_seconds; if it isn't
saved yet, the result has a recording_id to call again with. ios_screenshot (device.py) stays the tool for
one still image.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import shutil
import time
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from .. import budget as B
from .. import helper
from ..models import Out
from ..state import AppState, get_state
from . import MAX_WAIT, READ_REMOTE, ann, tool
from .device import Udid, dcall

THUMB_DIM = 320          # long edge of the inline thumbnail
THUMB_BYTES = 60_000     # it's a preview; ios_screenshot is the tool for a sharp still
KEEP = 20                # finished recordings remembered per server


# ------------------------------------------------------------------ models

class RecordingInfo(Out):
    recording_id: str
    state: Literal["recording", "finishing", "done", "failed"]
    path: str | None = Field(None, description="The MP4 (H.264 from screenshots, or HEVC from the stream)")
    seconds: float = Field(0, description="Length so far, or of the saved video")
    max_seconds: float | None = None
    bytes: int = 0
    fps: float | None = Field(None, description="Average pictures per second in the video")
    frames: int | None = None
    mode: str | None = Field(None, description="stream (smooth, iOS 27+) or screenshot (a few per second)")
    stop_reason: str | None = Field(None, description="timeout = reached seconds; stopped, mode, disconnected, …")
    thumbnail: str | None = Field(None, description="Small JPEG of the last picture (also sent inline)")
    inline_thumbnail: bool = False
    note: str | None = None
    error: str | None = None
    next_steps: list[str] = Field(default_factory=list)


class ModeAvailability(Out):
    mode: Literal["stream", "screenshot", "airplay"]
    available: bool
    reason: str | None = Field(None, description="Why not (e.g. media-in-use, unsupported, not-installed)")


class MirrorStatus(Out):
    running: bool
    mode: str | None = Field(None, description="What the live view is using now: stream or screenshot")
    requested_mode: str | None = None
    fps: float | None = Field(None, description="Requested frame rate cap")
    measured_fps: float | None = Field(None, description="Frames per second actually delivered (last 2 s)")
    viewers: int = 0
    notice: str | None = Field(None, description="Why it fell back to a lower frame rate, if it did")
    modes: list[ModeAvailability]
    uxplay_installed: bool = Field(description="The AirPlay receiver (UxPlay) for the smoothest mirror")
    ffmpeg_installed: bool
    recording: RecordingInfo | None = Field(None, description="The recording running now, or the last one")
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ background recordings

class Rec:
    def __init__(self, rid: str, started: dict, seconds: float):
        self.id = rid
        self.path = started.get("path")
        self.max_seconds = seconds
        self.mode = started.get("mode")
        self.t0 = time.monotonic()
        self.last: dict = dict(started)
        self.result: dict | None = None
        self.error: str | None = None
        self.task: asyncio.Task | None = None


def _hub(state: AppState) -> dict:
    return state.extras.setdefault("screen.recordings", {"recs": {}, "ids": itertools.count(1)})


async def _watch(state: AppState, rec: Rec, udid: str | None) -> None:
    """Follow record.status until our file is saved (the daemon stops at max_seconds by itself)."""
    deadline = rec.t0 + rec.max_seconds + 120
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        try:
            st = await dcall(state, "record.status", udid=udid, timeout=20) or {}
        except ToolError as exc:
            rec.error = str(exc)
            return
        if st.get("path") and rec.path and st.get("path") != rec.path:
            rec.error = "The recording was replaced by a newer one before it was saved."
            return
        rec.last = st
        if st.get("state") in ("done", "failed"):
            rec.result = st
            return
        if st.get("state") == "idle":
            rec.error = "The recording ended without a file (the device service restarted?)."
            return
    try:   # overdue: ask the daemon to stop and save it
        rec.result = await dcall(state, "record.stop", udid=udid, timeout=120)
    except ToolError as exc:
        rec.error = str(exc)


def _thumb(path: str | None) -> tuple[bytes | None, str | None]:
    if not path or not Path(path).is_file():
        return None, None
    try:
        img = B.fit_image(Path(path), THUMB_DIM, THUMB_BYTES, "jpeg", (70, 55, 40))
    except Exception:  # noqa: BLE001
        return None, None
    return (img.data, img.format) if img.fits else (None, None)


def _info(rec: Rec) -> RecordingInfo:
    r = rec.result or rec.last or {}
    state = r.get("state") or "recording"
    if rec.error and not rec.result:
        state = "failed"
    if state not in ("recording", "finishing", "done", "failed"):
        state = "recording"
    err = rec.error
    if state == "failed" and not err:
        e = r.get("error") or {}
        err = B.clip_error(str(e.get("message") or "The recording couldn't be saved."))
    note = r.get("notice")
    steps: list[str] = []
    if state in ("recording", "finishing"):
        left = max(0, rec.max_seconds - (time.monotonic() - rec.t0))
        steps.append(f"Still {state} (about {left:.0f} s to go). Call ios_screen_record(recording_id='{rec.id}', "
                     "wait_seconds=25) to wait for the file.")
    elif state == "done":
        if r.get("placeholder"):
            steps.append("Demo mode without ffmpeg: no video file was written.")
        else:
            steps.append("Attach the MP4 at `path` to the bug report. For a sharp still of one moment, use ios_screenshot.")
        if r.get("mode") == "screenshot":
            steps.append("This video is made of screenshots (a few per second). ios_mirror_status says why smooth "
                         "streaming wasn't used.")
    else:
        steps.append("Check ios_mirror_status; make sure the iPhone is unlocked with the screen on, then retry.")
    return RecordingInfo(
        recording_id=rec.id, state=state, path=helper.tilde(r.get("path") or rec.path),
        seconds=float(r.get("seconds") or 0), max_seconds=rec.max_seconds, bytes=int(r.get("bytes") or 0),
        fps=r.get("fps"), frames=r.get("frames"), mode=r.get("mode") or rec.mode,
        stop_reason=r.get("reason"), thumbnail=helper.tilde(r.get("thumbnail")), note=note,
        error=err, next_steps=steps)


def _brief(d: dict | None) -> RecordingInfo | None:
    if not d or d.get("state") in (None, "idle"):
        return None
    st = d.get("state") if d.get("state") in ("recording", "finishing", "done", "failed") else "recording"
    return RecordingInfo(recording_id="(daemon)", state=st, path=helper.tilde(d.get("path")),
                         seconds=float(d.get("seconds") or 0), max_seconds=d.get("maxSeconds"),
                         bytes=int(d.get("bytes") or 0), fps=d.get("fps"), frames=d.get("frames"),
                         mode=d.get("mode"), stop_reason=d.get("reason"), thumbnail=helper.tilde(d.get("thumbnail")))


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:

    @mcp.tool(annotations=ann("Record the screen", read_only=True, idempotent=False, open_world=True))
    async def ios_screen_record(
        ctx: Context,
        seconds: Annotated[int, Field(ge=1, le=60, description="Length of the recording")] = 10,
        recording_id: Annotated[str | None, Field(description="Wait for a recording started earlier instead of "
                                                              "starting a new one")] = None,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT, description="How long this call waits for the "
                                                                            "file before returning")] = MAX_WAIT,
        mode: Annotated[Literal["auto", "stream", "screenshot"], Field(
            description="auto: smooth stream on iOS 27+ (falls back to screenshots); only used when the iPhone "
                        "isn't already being mirrored")] = "auto",
        thumbnail: Annotated[bool, Field(description="Include a small preview image of the last picture")] = True,
        udid: Udid = None,
    ) -> Annotated[CallToolResult, RecordingInfo]:
        """Record the iPhone's screen for a few seconds (at most 60) into an MP4 for a bug report, while you
        or the user reproduce the problem. Returns the file path, length, size, frame rate and mode, plus one
        small preview image. Runs in the background: if it isn't saved within wait_seconds, call again with
        the returned recording_id. The phone must be unlocked with the screen on. In stream mode the phone
        shows a screen-sharing indicator and apps can't use the camera or microphone until it ends."""
        state = get_state(ctx)
        hub = _hub(state)
        if recording_id:
            rec = hub["recs"].get(recording_id.strip())
            if rec is None:
                raise ToolError(f"Unknown recording_id '{recording_id}'. Start one with ios_screen_record(seconds=...).")
        else:
            started = await dcall(state, "record.start", {"maxSeconds": seconds, "mode": mode}, udid=udid, timeout=45) or {}
            rec = Rec(f"rec-{next(hub['ids'])}", started, float(seconds))
            rec.task = asyncio.create_task(_watch(state, rec, udid))
            hub["recs"][rec.id] = rec
            for old in list(hub["recs"])[:-KEEP]:
                hub["recs"].pop(old, None)
        if rec.task is not None and not rec.task.done() and wait_seconds > 0:
            end = time.monotonic() + wait_seconds
            while not rec.task.done() and time.monotonic() < end:
                done = min(rec.max_seconds, time.monotonic() - rec.t0)
                await ctx.report_progress(round(100 * done / max(1.0, rec.max_seconds), 1), 100,
                                          message=f"recording {done:.0f}/{rec.max_seconds:.0f} s")
                await asyncio.wait({rec.task}, timeout=min(1.0, max(0.05, end - time.monotonic())))
        info = _info(rec)
        img, fmt = (None, None)
        if thumbnail and info.state == "done":
            img, fmt = await asyncio.to_thread(_thumb, (rec.result or {}).get("thumbnail"))
        info.inline_thumbnail = img is not None
        text = (f"Recording {info.recording_id}: {info.state}" +
                (f", {info.seconds:g} s, {info.bytes // 1000} KB, {info.fps or 0:g} fps ({info.mode}) at {info.path}"
                 if info.state == "done" else f" ({info.seconds:g} s so far)" if info.state != "failed" else
                 f": {info.error}") + "." + (f" {info.note}" if info.note else "") +
                (" " + info.next_steps[0] if info.next_steps else ""))
        content: list = [TextContent(type="text", text=text)]
        if img is not None:
            content.insert(0, ImageContent(type="image", data=base64.b64encode(img).decode(), mime_type=f"image/{fmt}"))
        return CallToolResult(content=content, structured_content=B.dump(info))

    @tool(mcp, annotations=READ_REMOTE("Screen mirror status"))
    async def ios_mirror_status(ctx: Context, udid: Udid = None) -> MirrorStatus:
        """How the live screen view of the iPhone works right now: whether it runs, in which mode (stream =
        smooth, iOS 27+; screenshot = a few frames a second), the frame rate it actually reaches, which modes
        are available and why not, whether the AirPlay receiver (UxPlay) and ffmpeg are installed, and the
        current or last screen recording. Read-only: it never starts the mirror."""
        state = get_state(ctx)
        st = await dcall(state, "mirror.status", udid=udid, timeout=30) or {}
        av = st.get("available") or {}
        stream = av.get("stream") or {}
        shot = av.get("screenshot") or {}
        ux = bool(shutil.which("uxplay"))
        modes = [ModeAvailability(mode="stream", available=bool(stream.get("available")), reason=stream.get("reason")),
                 ModeAvailability(mode="screenshot", available=bool(shot.get("available")), reason=shot.get("reason")),
                 ModeAvailability(mode="airplay", available=ux, reason=None if ux else "not-installed")]
        steps: list[str] = []
        if not ux:
            steps.append("For the smoothest mirror the user can install UxPlay (AirPlay receiver) in Setup: "
                         "ios_open_app(page='setup').")
        if not av.get("ffmpeg"):
            steps.append("Recordings made from screenshots need ffmpeg; the user can install it in Setup "
                         "(ios_open_app(page='setup')).")
        if stream.get("reason") == "media-in-use":
            steps.append("An app on the iPhone is using the camera or microphone, so smooth streaming is off until "
                         "it's closed.")
        steps.append("ios_screen_record(seconds=10) records a short MP4; ios_screenshot captures one still.")
        return MirrorStatus(
            running=bool(st.get("running")), mode=st.get("mode"), requested_mode=st.get("requestedMode"),
            fps=st.get("fps"), measured_fps=st.get("measuredFps"), viewers=int(st.get("viewers") or 0),
            notice=st.get("notice"), modes=modes, uxplay_installed=ux, ffmpeg_installed=bool(av.get("ffmpeg")),
            recording=_brief(st.get("record")), next_steps=steps)
