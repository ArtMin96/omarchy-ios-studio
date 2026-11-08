"""Screen tools: short screen recordings for bug reports and the state of the live screen view.

Both go through the shared device daemon (record.* and mirror.status, docs/deviced-protocol.md#screen),
so the agent and the iOS Studio window share one connection and one mirror. A recording runs as a job of
this server (core/jobs.py, kind 'recording'): ios_screen_record starts it and waits up to wait_seconds; if it
isn't saved yet, the result has a job_id to call again with (ios_job_cancel stops it early and keeps the file). ios_screenshot (device.py) stays the tool for
one still image.
"""
from __future__ import annotations

import asyncio
import base64
import shutil
import time
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from .. import helper
from ..backends.device import dcall
from ..core import budget as B
from ..core.job_status import wait_with_progress
from ..core.jobs import Job
from ..core.params import OptJobId, Udid, WaitSeconds
from ..core.registry import MAX_WAIT, tool
from ..models import Out
from ..state import AppState, get_state

THUMB_DIM = 320          # long edge of the inline thumbnail
THUMB_BYTES = 60_000     # it's a preview; ios_screenshot is the tool for a sharp still


# ------------------------------------------------------------------ models

class RecordingInfo(Out):
    job_id: str | None = Field(None, description="The recording's job: call ios_screen_record(job_id=...) to wait")
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
    """A recording's own state, kept in its job's data["rec"]."""
    def __init__(self, started: dict, seconds: float):
        self.path = started.get("path")
        self.max_seconds = seconds
        self.mode = started.get("mode")
        self.t0 = time.monotonic()
        self.last: dict = dict(started)
        self.result: dict | None = None
        self.error: str | None = None


async def _watch(state: AppState, job: Job, udid: str | None) -> dict:
    """Follow record.status until our file is saved (the daemon stops at max_seconds by itself)."""
    rec: Rec = job.data["rec"]

    def fail(msg: str) -> ToolError:
        rec.error = msg
        return ToolError(msg)

    deadline = rec.t0 + rec.max_seconds + 120
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        done = min(rec.max_seconds, time.monotonic() - rec.t0)
        job.report(done / max(1.0, rec.max_seconds), f"recording {done:.0f}/{rec.max_seconds:.0f} s")
        try:
            st = await dcall(state, "record.status", udid=udid, timeout=20) or {}
        except ToolError as exc:
            raise fail(str(exc)) from None
        if st.get("path") and rec.path and st.get("path") != rec.path:
            raise fail("The recording was replaced by a newer one before it was saved.")
        rec.last = st
        if st.get("state") in ("done", "failed"):
            rec.result = st
            return st
        if st.get("state") == "idle":
            raise fail("The recording ended without a file (the device service restarted?).")
    try:   # overdue: ask the daemon to stop and save it
        rec.result = await dcall(state, "record.stop", udid=udid, timeout=120)
    except ToolError as exc:
        raise fail(str(exc)) from None
    return rec.result or {}


def _thumb(path: str | None) -> tuple[bytes | None, str | None]:
    if not path or not Path(path).is_file():
        return None, None
    try:
        img = B.fit_image(Path(path), THUMB_DIM, THUMB_BYTES, "jpeg", (70, 55, 40))
    except Exception:  # noqa: BLE001
        return None, None
    return (img.data, img.format) if img.fits else (None, None)


def _info(job: Job) -> RecordingInfo:
    rec: Rec = job.data["rec"]
    if job.state == "cancelled" and not rec.result:
        rec.error = rec.error or "Stopped before the file was saved."
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
        steps.append(f"Still {state} (about {left:.0f} s to go). Call ios_screen_record(job_id='{job.id}', "
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
        job_id=job.id, state=state, path=helper.tilde(r.get("path") or rec.path),
        seconds=float(r.get("seconds") or 0), max_seconds=rec.max_seconds, bytes=int(r.get("bytes") or 0),
        fps=r.get("fps"), frames=r.get("frames"), mode=r.get("mode") or rec.mode,
        stop_reason=r.get("reason"), thumbnail=helper.tilde(r.get("thumbnail")), note=note,
        error=err, next_steps=steps)


def _brief(d: dict | None) -> RecordingInfo | None:
    if not d or d.get("state") in (None, "idle"):
        return None
    st = d.get("state") if d.get("state") in ("recording", "finishing", "done", "failed") else "recording"
    return RecordingInfo(state=st, path=helper.tilde(d.get("path")),
                         seconds=float(d.get("seconds") or 0), max_seconds=d.get("maxSeconds"),
                         bytes=int(d.get("bytes") or 0), fps=d.get("fps"), frames=d.get("frames"),
                         mode=d.get("mode"), stop_reason=d.get("reason"), thumbnail=helper.tilde(d.get("thumbnail")))


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:

    @tool(mcp, "Record the screen", kind="read_remote", idempotent=False)
    async def ios_screen_record(
        ctx: Context,
        seconds: Annotated[int, Field(ge=1, le=60, description="Length")] = 10,
        job_id: OptJobId = None,
        wait_seconds: WaitSeconds = MAX_WAIT,
        mode: Annotated[Literal["auto", "stream", "screenshot"], Field(
            description="auto: smooth stream on iOS 27+, else screenshots")] = "auto",
        thumbnail: Annotated[bool, Field(description="Include a small preview image")] = True,
        udid: Udid = None,
    ) -> Annotated[CallToolResult, RecordingInfo]:
        """Record the iPhone's screen (≤ 60 s) into an MP4 for a bug report while the problem is reproduced.
        Returns path, length, fps and a small preview. A job: state='running' + job_id, call again with job_id
        (ios_job_cancel stops early, keeping the video). The phone must be unlocked; stream mode blocks the
        camera and microphone while it runs."""
        state = get_state(ctx)
        if job_id:
            job = state.jobs.get(job_id, "recording")
        else:
            started = await dcall(state, "record.start", {"maxSeconds": seconds, "mode": mode}, udid=udid, timeout=45) or {}

            async def stop(j: Job) -> None:
                j.data["rec"].result = await dcall(state, "record.stop", udid=udid, timeout=60)

            job = state.jobs.run(lambda j: _watch(state, j, udid), "recording", title=f"Record the screen ({seconds} s)",
                                 tool="ios_screen_record", data={"rec": Rec(started, float(seconds))}, on_cancel=stop)
        await wait_with_progress(state, job, wait_seconds, ctx)
        rec: Rec = job.data["rec"]
        info = _info(job)
        img, fmt = (None, None)
        if thumbnail and info.state == "done":
            img, fmt = await asyncio.to_thread(_thumb, (rec.result or {}).get("thumbnail"))
        info.inline_thumbnail = img is not None
        text = (f"Recording {info.job_id}: {info.state}" +
                (f", {info.seconds:g} s, {info.bytes // 1000} KB, {info.fps or 0:g} fps ({info.mode}) at {info.path}"
                 if info.state == "done" else f" ({info.seconds:g} s so far)" if info.state != "failed" else
                 f": {info.error}") + "." + (f" {info.note}" if info.note else "") +
                (" " + info.next_steps[0] if info.next_steps else ""))
        content: list = [TextContent(type="text", text=text)]
        if img is not None:
            content.insert(0, ImageContent(type="image", data=base64.b64encode(img).decode(), mime_type=f"image/{fmt}"))
        return CallToolResult(content=content, structured_content=B.dump(info))

    @tool(mcp, "Screen mirror status", kind="read_remote")
    async def ios_mirror_status(ctx: Context, udid: Udid = None) -> MirrorStatus:
        """The live screen view right now: running, mode (stream: smooth, iOS 27+; screenshot: a few fps), real
        frame rate, which modes are available and why not, UxPlay/ffmpeg installed, and the current or last
        recording. Read-only."""
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
