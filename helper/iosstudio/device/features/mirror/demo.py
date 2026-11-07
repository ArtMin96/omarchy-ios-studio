"""Demo: mirror.start / mirror.stop / mirror.ack / mirror.status and record.start / record.stop /
record.status, with painted frames (demo_art) instead of a phone.

"stream" sends ~24 frames a second from a small set of pre-rendered frames; "screenshot" about 3.
IOSSTUDIO_DEMO_MEDIA_BUSY=1 pretends an app holds the camera, so "auto" falls back to screenshots
with the same notice as the real daemon (and "stream" fails with media-in-use). A recording is a
real MP4 made with ffmpeg from the frames shown; without ffmpeg it returns a placeholder path and a
notice. Files stay in the demo directory, never in the real cache.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time

from iosstudio.core import fileio
from iosstudio.demo import demo_art
from iosstudio.device.demo.core import DIR, event

KEY = "mirror"
REC_KEY = "record"
REC_HOLDER = "recording"
FRAME_FILES = 4
ACK_WINDOW = 3
CACHE = DIR / "frames-cache"
VARIANTS = 10                       # demo_art.phone_screen moves the sun over 10 steps
MEDIA_BUSY = bool(os.environ.get("IOSSTUDIO_DEMO_MEDIA_BUSY"))
NOTICE_BUSY = ("Screen streaming isn't available while the camera or microphone is in use, so "
               "mirroring at a lower frame rate.")
RATE = {"stream": 24.0, "screenshot": 3.0}


class Frames:
    """Pre-rendered demo frames (pure-Python painting is slow, so they're made once, in the background)."""

    def __init__(self):
        self.ready: list = []
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def ensure(self):
        with self.lock:
            if self.thread is not None:
                return
            self.thread = threading.Thread(target=self._render, daemon=True)
            self.thread.start()

    def _render(self):
        CACHE.mkdir(parents=True, exist_ok=True)
        for i in range(VARIANTS):
            p = CACHE / f"sun-{i}.png"
            if not p.exists() or p.stat().st_size < 1000:
                tmp = CACHE / f".sun-{i}.png"
                demo_art.phone_screen(tmp, i)
                os.replace(tmp, p)
            with self.lock:
                self.ready.append(p)

    def pick(self, n: int):
        with self.lock:
            if not self.ready:
                return None
            # ping-pong over the variants so the sun drifts back and forth
            k = len(self.ready)
            i = n % (2 * k - 2) if k > 1 else 0
            return self.ready[i if i < k else 2 * k - 2 - i]


class Mirror:
    def __init__(self):
        self.on = threading.Event()
        self.requested = "auto"
        self.mode: str | None = None
        self.fps: float | None = None
        self.scale = 0.5
        self.quality = 70
        self.notice: str | None = None
        self.viewers: set = set()
        self.frames = Frames()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.seq = 0
        self.acked = 0
        self.ack_t = 0.0
        self.acking = False
        self.dropped = 0
        self.times: list[float] = []
        self.rec: "Recording | None" = None
        self.last_record: dict | None = None
        self.n = 0

    # -- state
    def rate(self) -> float:
        r = RATE.get(self.mode or "screenshot", 3.0)
        return max(0.2, min(r, self.fps)) if self.fps else r

    def measured(self) -> float:
        now = time.monotonic()
        with self.lock:
            t = [x for x in self.times if now - x <= 2]
        return round((len(t) - 1) / max(0.001, t[-1] - t[0]), 1) if len(t) > 1 else 0.0

    def state(self, **extra) -> dict:
        on = self.on.is_set()
        out = {"running": on, "fps": self.fps or RATE.get(self.mode or "stream", 24.0), "mode": self.mode if on else None,
               "requestedMode": self.requested, "measuredFps": self.measured() if on else 0.0,
               "dropped": self.dropped, "scale": self.scale, "quality": self.quality,
               "recording": self.rec is not None and self.rec.state == "recording"}
        if self.notice and on:
            out["notice"] = self.notice
            out["noticeCode"] = "media-in-use"
        out.update(extra)
        return out

    def pick_mode(self):
        if self.requested == "screenshot":
            return "screenshot", None
        if MEDIA_BUSY:
            if self.requested == "stream":
                return None, {"code": "media-in-use", "message": "Screen streaming can't start while an app on your "
                              "iPhone is using the camera or microphone. Close that app, or mirror with screenshots."}
            return "screenshot", NOTICE_BUSY
        return "stream", None

    # -- loop
    def loop(self):
        last_stats = 0.0
        while True:
            if not self.on.is_set():
                time.sleep(0.1)
                continue
            t0 = time.monotonic()
            self.frames.ensure()
            src = self.frames.pick(self.n)
            self.n += 1
            if src is not None:
                rec = self.rec
                if rec is not None and rec.state == "recording":
                    rec.add(src)
                mode = self.mode
                if mode and self.viewers and self._ready():
                    self._publish(src, mode)
            if t0 - last_stats > 2:
                last_stats = t0
                event("mirror", self.state())
                if self.rec is not None and self.rec.state == "recording":
                    event("record", self.rec.snapshot())
            time.sleep(max(0.0, 1.0 / self.rate() - (time.monotonic() - t0)))

    def _ready(self) -> bool:
        now = time.monotonic()
        with self.lock:
            if self.acking and self.seq - self.acked >= ACK_WINDOW and now - self.ack_t < 1.0:
                self.dropped += 1
                return False
            return True

    def _publish(self, src, mode: str):
        with self.lock:
            self.seq += 1
            seq = self.seq
            now = time.monotonic()
            self.times = [t for t in self.times if now - t <= 2] + [now]
            if self.acking and now - self.ack_t >= 1.0:
                self.ack_t = now
        p = DIR / "frames" / f"mirror-{seq % FRAME_FILES}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}")
        shutil.copyfile(src, tmp)
        os.replace(tmp, p)
        event("frame", {"path": str(p), "seq": seq, "ts": time.time(), "width": 390, "height": 844,
                        "bytes": p.stat().st_size, "mode": mode})

    # -- lifecycle
    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()
        mode, notice = self.pick_mode()
        if mode is None:
            raise DemoError(notice)
        self.mode, self.notice = mode, notice
        with self.lock:
            self.seq = self.acked = 0
            self.acking = False
            self.dropped = 0
            self.times = []
        self.on.set()
        return self

    def stop(self, *_):
        if self.rec is not None and self.rec.state == "recording":
            self.rec.stop_reason = self.rec.stop_reason or "stopped"
            threading.Thread(target=self.finish_recording, daemon=True).start()
        self.on.clear()
        self.viewers.clear()
        self.mode = None
        event("mirror", self.state(running=False))

    # -- recording
    def finish_recording(self) -> dict | None:
        rec = self.rec
        if rec is None:
            return self.last_record
        with rec.lock:
            if rec.state != "recording":
                pass
            else:
                rec.state = "finishing"
        if rec.done.is_set() or rec.claimed:
            rec.done.wait(60)
            return self.last_record
        rec.claimed = True
        event("record", rec.snapshot())
        result = rec.finalize()
        fileio.prune((DIR / "recordings").glob("rec-*.mp4"), 20, 2 << 30, also=(".png",))   # as the real daemon
        rec.result = result
        rec.state = result["state"]
        self.last_record = rec.snapshot()
        self.rec = None
        rec.done.set()
        event("record", self.last_record)
        return self.last_record


class DemoError(Exception):
    def __init__(self, e: dict):
        super().__init__(e.get("message"))
        self.e = e


class Recording:
    def __init__(self, mode: str, max_seconds: float):
        self.mode = mode
        self.max_seconds = max_seconds
        self.state = "recording"
        self.t0 = time.monotonic()
        self.started = time.time()
        self.lock = threading.Lock()
        self.files: list = []
        self.times: list[float] = []
        self.bytes = 0
        self.result: dict | None = None
        self.done = threading.Event()
        self.claimed = False
        self.stop_reason = ""
        self.task = None                 # asyncio task finishing it (demo resource close)
        d = DIR / "recordings"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"rec-{time.strftime('%Y%m%d-%H%M%S')}.mp4"

    @property
    def seconds(self) -> float:
        return round(min(time.monotonic() - self.t0, self.max_seconds), 1)

    def add(self, src):
        with self.lock:
            if self.state != "recording":
                return
            self.files.append(src)
            self.times.append(time.monotonic())
            self.bytes += 40_000 if self.mode == "stream" else 90_000   # what a phone recording grows by

    def snapshot(self) -> dict:
        out = {"state": self.state, "mode": self.mode, "seconds": self.seconds, "bytes": self.bytes,
               "frames": len(self.files), "maxSeconds": self.max_seconds, "path": str(self.path), "started": self.started}
        if self.result:
            out.update(self.result)
        return out

    def finalize(self) -> dict:
        seconds = round(max(0.1, (self.times[-1] - self.times[0]) if len(self.times) > 1 else 0.1), 2)
        frames = len(self.files)
        fps = round((frames - 1) / seconds, 1) if frames > 1 else float(frames)
        thumb = self.path.with_suffix(".png")
        if self.files:
            shutil.copyfile(self.files[-1], thumb)
        base = {"seconds": seconds, "fps": fps, "frames": frames, "mode": self.mode, "reason": self.stop_reason,
                "thumbnail": str(thumb) if self.files else None}
        ff = shutil.which("ffmpeg")
        if not frames:
            return {"state": "failed", **base, "bytes": 0, "error": {"code": "record-empty", "message":
                    "The recording has no pictures. Make sure your iPhone is unlocked with the screen on, then try again."}}
        if not ff:
            return {"state": "done", **base, "path": str(self.path), "bytes": 0, "placeholder": True,
                    "notice": "Demo mode: ffmpeg isn't installed, so no video file was made. The path is a placeholder."}
        lst = self.path.with_suffix(".ffconcat")
        lines = ["ffconcat version 1.0"]
        for i, f in enumerate(self.files):
            dur = (self.times[i + 1] - self.times[i]) if i + 1 < frames else 0.1
            lines += [f"file '{f}'", f"duration {max(0.001, dur):.3f}"]
        lines.append(f"file '{self.files[-1]}'")
        lst.write_text("\n".join(lines) + "\n")
        p = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-vf", "format=yuv420p", "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "28", "-movflags", "+faststart", str(self.path)], capture_output=True, text=True, timeout=300)
        lst.unlink(missing_ok=True)
        if p.returncode != 0 or not self.path.exists():
            return {"state": "failed", **base, "bytes": 0, "error": {"code": "record-failed", "message":
                    "Couldn't save the recording.", "detail": (p.stderr or "")[-400:]}}
        return {"state": "done", **base, "path": str(self.path), "bytes": self.path.stat().st_size}


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def register(reg):
    m = Mirror()

    def err(code, message, fix=None):
        from iosstudio.core.output import HelperError
        return HelperError(code, message, fix)

    async def ensure(ctx, holder):
        try:
            await ctx.resources.acquire(KEY, holder, m.start, m.stop, scope="session")
        except DemoError as e:
            event("mirror", m.state(running=False, error=e.e))
            raise err(e.e["code"], e.e["message"]) from None

    @reg.command("mirror.start")
    async def mirror_start(ctx, a):
        mode = a.get("mode")
        if mode is not None and mode not in ("auto", "stream", "screenshot"):
            raise err("bad-args", "mode must be auto, stream or screenshot.")
        if a.get("fps") is not None:
            m.fps = _clamp(a.get("fps"), 0.2, 30, 1.0)
        if a.get("scale") is not None:
            m.scale = _clamp(a.get("scale"), 0.25, 1, 0.5)
        if a.get("quality") is not None:
            m.quality = int(_clamp(a.get("quality"), 30, 95, 70))
        if mode and mode != m.requested:
            m.requested = mode
            if m.on.is_set():
                if m.rec is not None and m.rec.state == "recording":
                    m.rec.stop_reason = "mode"
                    task = m.rec.task
                    await ctx.resources.close(REC_KEY, "released")
                    if m.rec is not None and m.rec.task is not None:
                        await asyncio.shield(m.rec.task)
                    elif task is not None:
                        await asyncio.shield(task)
                new, notice = m.pick_mode()
                if new is None:
                    await ctx.resources.close(KEY, "error")
                    raise err(notice["code"], notice["message"])
                m.mode, m.notice = new, notice
        await ensure(ctx, ctx.client())
        m.viewers.add(ctx.client())
        event("mirror", m.state())
        return {"running": True, "fps": m.fps or RATE[m.mode], "mode": m.mode, "requestedMode": m.requested,
                "scale": m.scale, "quality": m.quality}

    @reg.command("mirror.stop")
    async def mirror_stop(ctx, a):
        m.viewers.discard(ctx.client())
        held = ctx.resources.held(KEY)
        left = await ctx.resources.release(KEY, ctx.client())
        if left:
            return {"running": False, "sharedWith": left}
        if not held:   # (when held, the release above already stopped it)
            m.stop()
        return {"running": False}

    @reg.command("mirror.ack")
    def mirror_ack(ctx, a):
        seq = int(a.get("seq") or 0)
        with m.lock:
            if seq > m.acked:
                m.acked = min(seq, m.seq)
                m.ack_t = time.monotonic()
                m.acking = True
        return {}

    @reg.command("mirror.status")
    def mirror_status(ctx, a):
        busy = MEDIA_BUSY
        return {**m.state(), "viewers": len(m.viewers),
                "available": {"stream": {"available": not busy, "reason": "media-in-use" if busy else None,
                                         "message": NOTICE_BUSY if busy else None},
                              "screenshot": {"available": True, "reason": None},
                              "jpeg": True, "ffmpeg": bool(shutil.which("ffmpeg"))},
                "record": m.rec.snapshot() if m.rec else m.last_record}

    @reg.command("record.start")
    async def record_start(ctx, a):
        if m.rec is not None:
            raise err("busy", "A recording is already running. Stop it first." if m.rec.state == "recording"
                      else "The last recording is still being saved. Try again in a moment.")
        max_s = _clamp(a.get("maxSeconds"), 1, 300, 60.0)
        if a.get("mode") in ("auto", "stream", "screenshot") and not m.on.is_set():
            m.requested = a["mode"]
        await ensure(ctx, REC_HOLDER)
        rec = Recording(m.mode or "screenshot", max_s)
        m.rec = rec

        async def finish():
            result = await asyncio.to_thread(m.finish_recording)
            await ctx.resources.release(KEY, REC_HOLDER)    # the recording's hold on the mirror
            return result

        def close_(handle, reason):     # stop, auto-stop (timeout = maxSeconds), client gone, shutdown
            if handle.task is None:
                handle.stop_reason = handle.stop_reason or reason
                handle.task = asyncio.ensure_future(finish())
        await ctx.resources.acquire(REC_KEY, ctx.client(), lambda: rec, close_, scope="tunnel", timeout=max_s)
        event("record", rec.snapshot())
        return rec.snapshot()

    @reg.command("record.stop")
    async def record_stop(ctx, a):
        rec = m.rec
        if rec is None:
            if m.last_record:
                return m.last_record
            raise err("not-recording", "Nothing is being recorded.")
        rec.stop_reason = rec.stop_reason or "stopped"
        await ctx.resources.close(REC_KEY, "released")
        result = await asyncio.shield(rec.task) if rec.task is not None else await asyncio.to_thread(m.finish_recording)
        if result and result.get("state") == "failed":
            e = result.get("error") or {}
            raise err(e.get("code", "record-failed"), e.get("message", "Couldn't save the recording."))
        return result

    @reg.command("record.status")
    def record_status(ctx, a):
        return m.rec.snapshot() if m.rec else (m.last_record or {"state": "idle"})

    @reg.on_client_gone
    def client_gone(ctx, client):
        m.viewers.discard(client)

    @reg.welcome
    def snapshot(ctx):
        out = [("mirror", m.state())] if m.on.is_set() else []
        if m.rec is not None:
            out.append(("record", m.rec.snapshot()))
        return out

    @reg.on_shutdown
    def stop(ctx):
        if m.rec is not None:
            m.finish_recording()
        m.on.clear()
