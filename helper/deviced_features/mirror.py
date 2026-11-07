"""mirror.start / mirror.stop / mirror.ack / mirror.status and record.start / record.stop / record.status:
the live screen view on the iPhone › Screen tab and short screen recordings for bug reports.

Two engines feed one FrameSink (rotating JPEG files + `frame` events, with backpressure):

* StreamEngine, mode "stream" (iOS 27+): DisplayService pushes RTP/HEVC to a UDP socket on the
  daemon's own userspace tunnel stack (s.userspace_udp()). This module depacketizes it, keeps the
  encoder alive with RTCP receiver reports, asks for keyframes (PLI) after loss, decodes with PyAV in
  a worker thread and writes downscaled JPEGs. While a stream runs the phone shows its
  screen-sharing indicator and blocks the camera and microphone, so every way out of a stream
  (stop, release, tunnel drop, device switch, shutdown, a crash of the loop) ends with
  DisplayService.stop_all_streams(); when the tunnel is down at that moment, the stop is sent as
  soon as it's back.
* ShotEngine, mode "screenshot": DVT screenshots in a loop on the shared DVT connection. The
  capture RPC of frame n+1 overlaps the PNG -> JPEG conversion of frame n.

"auto" tries the stream and falls back to screenshots (older iOS, camera/microphone in use (9022),
missing decoder, or a stream that won't start) with a plain-language `notice`.

Recording (Recorder) takes whatever the mirror is producing: in stream mode the HEVC access units
are remuxed into MP4 without re-encoding (PyAV; ffmpeg -c copy as fallback); in screenshot mode the
JPEG frames are joined with ffmpeg's concat demuxer (per-frame durations, so the timing is real)
into H.264. Files: ~/.cache/ios-studio/recordings/rec-<YYYYmmdd-HHMMSS>.mp4.

Protocol: docs/deviced-protocol.md#screen.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import struct
import subprocess
import threading
import time
from collections import deque
from fractions import Fraction
from pathlib import Path

import common
from common import fix
from deviced_core import SCREENS_DIR, classify, dlog, emit, err, png_size, safe_name, write_bytes

# Pillow, PyAV and the stream classes are imported by load_media() when the screen view or a recording
# first needs them: together they take about a third of a second, which the daemon shouldn't pay at
# start for a screen view that may never open. None means missing (that mode is then unavailable).
Image = ImageChops = av = DisplayService = None
STREAM_IMPORT_ERROR: str | None = None
_media_loaded = False


def load_media() -> None:
    global Image, ImageChops, av, Version, DeviceVersionNotSupportedError, VIDEO_STREAM_MIN_VERSION
    global DisplayService, is_media_in_use_error, depacketize_hevc, STREAM_IMPORT_ERROR, _media_loaded
    if _media_loaded:
        return
    _media_loaded = True
    try:
        from PIL import Image, ImageChops
    except Exception:  # noqa: BLE001 - JPEG conversion is optional: PNG frames still work
        Image = ImageChops = None
    try:
        import av
        import av.error  # noqa: F401
    except Exception:  # noqa: BLE001
        av = None
    try:
        from packaging.version import Version
        from pymobiledevice3.exceptions import DeviceVersionNotSupportedError
        from pymobiledevice3.remote.core_device.display_service import (
            VIDEO_STREAM_MIN_VERSION,
            DisplayService,
            is_media_in_use_error,
        )
        from pymobiledevice3.remote.core_device.screen_stream import depacketize_hevc
    except Exception as exc:  # noqa: BLE001 - only the stream mode is lost
        DisplayService = None
        STREAM_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


REC_DIR = common.CACHE_DIR / "recordings"
REC_KEEP = 20                   # recordings kept (newest first), with their .jpg thumbnails
REC_MAX_BYTES = 2 << 30
REC_STALE_S = 900               # .rec-* work files untouched this long are from a daemon that died
FRAME_FILES = 4                 # rotating frame files; the UI holds at most ACK_WINDOW of them
ACK_WINDOW = 3                  # frames in flight to an acknowledging UI (+1 shown = FRAME_FILES)
ACK_STALE_S = 1.0               # no ack for this long: send anyway (the UI missed one or went away)
SHOT_FPS = (0.2, 8.0)           # screenshot mode: as fast as the capture RPC allows, up to 8/s
STREAM_FPS = (0.2, 30.0)        # stream mode: frame events to the UI, at most 30/s
REC_SHOT_FPS = 8.0              # screenshot mode captures at least this fast while recording
REC_MAX_S = 300
STALL_S = 5.0                   # stream: no picture for this long -> restart the stream
MAX_RESTARTS = 3
DECODE_QUEUE = 45               # access units waiting for the decoder before we drop to the next key
REC_HOLDER = "recording"        # the mirror holder a recording uses (not a client)

NOTICE = {
    "media-in-use": "Screen streaming isn't available while the camera or microphone is in use, so "
                    "mirroring at a lower frame rate.",
    "unsupported": "Smooth streaming needs iOS 27 or later, so mirroring at a lower frame rate.",
    "no-decoder": "Smooth streaming needs the video decoder from the iPhone tools, so mirroring at a "
                  "lower frame rate. Repairing the iPhone tools in Setup adds it.",
    "no-udp": "Smooth streaming isn't available on this connection, so mirroring at a lower frame rate.",
    "failed": "Screen streaming didn't start, so mirroring at a lower frame rate.",
}
REPAIR_FIX = fix("Repair iPhone tools", "setup", step="pymobiledevice3")
FFMPEG_FIX = fix("Install ffmpeg", "setup", step="packages")


class StreamUnavailable(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    def to_error(self):
        msgs = {
            "media-in-use": "Screen streaming can't start while an app on your iPhone is using the camera or "
                            "microphone. Close that app, or mirror with screenshots.",
            "unsupported": "Smooth streaming needs iOS 27 or later. Mirror with screenshots instead.",
            "no-decoder": "The video decoder for smooth streaming is missing. Repair the iPhone tools in Setup, "
                          "or mirror with screenshots.",
            "no-udp": "Smooth streaming isn't available on this connection. Mirror with screenshots instead.",
            "failed": "Screen streaming stopped working. Mirror with screenshots, or try again.",
        }
        return err(f"stream-{self.code}" if self.code != "media-in-use" else "media-in-use",
                   msgs.get(self.code, msgs["failed"]), REPAIR_FIX if self.code == "no-decoder" else None,
                   self.detail[-600:] or None)


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


# ====================================================================== images

def to_jpeg(img, scale: float, quality: int):
    """PIL image -> (jpeg bytes, w, h), downscaled by `scale` (reduce() for whole factors: cheap)."""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    tw, th = max(1, round(w * scale)), max(1, round(h * scale))
    if (tw, th) != (w, h):
        factor = int(1 / scale + 1e-6)
        if factor >= 2:
            img = img.reduce(factor)
        if abs(img.size[0] - tw) > 2 or abs(img.size[1] - th) > 2:
            img = img.resize((tw, th), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=int(quality))
    return buf.getvalue(), img.size[0], img.size[1]


class PngConverter:
    """PNG screenshot -> downscaled JPEG, timed per stage. Decodes with libavcodec's PNG decoder through
    PyAV (about 30 % faster than Pillow on a busy 6.7 MB frame: ~160 vs ~230 ms, and it scales in the
    same pass); falls back to Pillow, and to the PNG unchanged without Pillow. Both decoders release
    the GIL, so the event loop keeps receiving the next screenshot meanwhile. Not thread-safe: the
    screenshot engine runs one conversion at a time."""

    def __init__(self):
        load_media()
        self.codec = None
        self.use_av = av is not None and Image is not None
        self.backend = "pyav" if self.use_av else ("pillow" if Image is not None else "none")

    def convert(self, png: bytes, scale: float, quality: int):
        """-> (data, ext, w, h, decode_s, encode_s)."""
        if Image is None:
            w, h = png_size(png)
            return png, "png", w, h, 0.0, 0.0
        if self.use_av:
            try:
                return self._av(png, scale, quality)
            except Exception as exc:  # noqa: BLE001 - odd PNG or PyAV trouble: Pillow from now on
                dlog(f"mirror (screenshot): PyAV PNG decode failed ({exc!r}); using Pillow")
                self.use_av, self.codec, self.backend = False, None, "pillow"
        t0 = time.perf_counter()
        with Image.open(io.BytesIO(png)) as im:
            im.load()
            t1 = time.perf_counter()
            data, w, h = to_jpeg(im, scale, quality)
        return data, "jpg", w, h, t1 - t0, time.perf_counter() - t1

    def _av(self, png: bytes, scale: float, quality: int):
        if self.codec is None:
            self.codec = av.CodecContext.create("png", "r")
        t0 = time.perf_counter()
        frames = self.codec.decode(av.Packet(png))
        if not frames:
            raise ValueError("no picture in the PNG")
        fr = frames[-1]
        t1 = time.perf_counter()
        tw, th = max(2, round(fr.width * scale)), max(2, round(fr.height * scale))
        img = fr.reformat(width=tw, height=th, format="rgb24", interpolation="AREA").to_image()
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=int(quality))
        return buf.getvalue(), "jpg", tw, th, t1 - t0, time.perf_counter() - t1


def uncollapse(img):
    """Under fast motion the phone's encoder shrinks the picture into the top-left corner and pads the
    rest with mid-gray. Stretch that corner back to the full frame (pymobiledevice3's viewer does the same)."""
    if ImageChops is None:
        return img
    w, h = img.size
    small = img.reduce(4) if w >= 64 and h >= 64 else img
    diff = ImageChops.difference(small, Image.new(small.mode, small.size, (128, 128, 128)))
    box = diff.convert("L").point(lambda v: 255 if v > 8 else 0).getbbox()
    if not box:
        return img
    sw, sh = small.size
    cw, ch = box[2] / sw, box[3] / sh
    if 0.2 < cw < 0.92 and 0.2 < ch < 0.92:
        return img.crop((0, 0, max(1, round(w * cw)), max(1, round(h * ch)))).resize((w, h), Image.BILINEAR)
    return img


# ====================================================================== frame sink

class FrameSink:
    """Rotating frame files and `frame` events, throttled to the requested rate and to what the UI has
    acknowledged (mirror.ack). Thread-safe: the stream decoder publishes from its worker thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.udid = ""
        self.reset("")

    def reset(self, udid: str):
        with self.lock:
            self.udid = safe_name(udid)
            self.seq = 0
            self.acked = 0
            self.ack_t = 0.0
            self.acking = False
            self.last_emit = 0.0
            self.times: deque = deque(maxlen=120)
            self.dropped = 0
            self.last: tuple | None = None    # (data, ext, w, h) of the newest frame

    def ack(self, seq: int):
        with self.lock:
            if seq > self.acked:
                self.acked = min(seq, self.seq)
                self.ack_t = time.monotonic()
                self.acking = True

    def ready(self, min_interval: float) -> bool:
        """May a new frame go out now? Rate cap, then backpressure (counts a drop when it may not)."""
        now = time.monotonic()
        with self.lock:
            if now - self.last_emit < min_interval * 0.9:
                return False
            if self.acking and self.seq - self.acked >= ACK_WINDOW and now - self.ack_t < ACK_STALE_S:
                self.dropped += 1
                return False
            return True

    def publish(self, data: bytes, ext: str, w: int, h: int, mode: str) -> int:
        with self.lock:
            self.seq += 1
            seq = self.seq
            now = time.monotonic()
            self.last_emit = now
            self.times.append(now)
            self.last = (data, ext, w, h)
            if self.acking and now - self.ack_t >= ACK_STALE_S:
                self.ack_t = now   # one frame per stale period, then wait for the UI again
        path = SCREENS_DIR / f"mirror-{self.udid}-{seq % FRAME_FILES}.{ext}"
        write_bytes(path, data)
        emit("frame", {"path": str(path), "seq": seq, "ts": time.time(), "width": w, "height": h,
                       "bytes": len(data), "mode": mode})
        return seq

    def measured_fps(self) -> float:
        now = time.monotonic()
        with self.lock:
            recent = [t for t in self.times if now - t <= 2.0]
        if len(recent) < 2:
            return float(len(recent)) / 2.0
        return round((len(recent) - 1) / max(0.001, recent[-1] - recent[0]), 1)


# ====================================================================== screenshot engine

async def capture_png(s) -> bytes:
    """One PNG over the shared DVT connection (rebuilt once if it broke); same as screen.screenshot."""
    async def take():
        shot = await s.dvt_instrument("screenshot", s.pmd3.Screenshot)
        return await asyncio.wait_for(shot.get_screenshot(), 15)
    return await s.dvt_retry(take, err("failed", "Couldn't take a screenshot."))


class ShotStats:
    """Per-stage timings of the screenshot pipeline, logged to deviced.log every 5 s:
    mirror: 5.0 s, 19 frames (3.8/s, gap 263 ms) | rpc 251 ms (19 shots) | wait 3 | decode 160 | scale+encode 21
            | write 1 ms (pyav) | skipped 1 (converter busy), held 0 (acks)"""

    EVERY = 5.0

    def __init__(self, backend: str):
        self.backend = backend
        self.t0 = time.monotonic()
        self.reset()

    def reset(self):
        self.rpc, self.wait, self.dec, self.enc, self.wr = [], [], [], [], []
        self.frames = 0
        self.skipped = 0
        self.held = 0
        self.gaps: list[float] = []
        self.last_pub: float | None = None

    def published(self, now: float):
        if self.last_pub is not None:
            self.gaps.append(now - self.last_pub)
        self.last_pub = now
        self.frames += 1

    def maybe_log(self):
        now = time.monotonic()
        span = now - self.t0
        if span < self.EVERY:
            return
        def ms(xs):
            return f"{1000 * sum(xs) / len(xs):.0f}" if xs else "-"
        gap = sorted(self.gaps)[len(self.gaps) // 2] if self.gaps else 0
        dlog(f"mirror: {span:.1f} s, {self.frames} frames ({self.frames / span:.1f}/s, gap {1000 * gap:.0f} ms) "
             f"| rpc {ms(self.rpc)} ms ({len(self.rpc)} shots) | wait {ms(self.wait)} "
             f"| decode {ms(self.dec)} | scale+encode {ms(self.enc)} | write {ms(self.wr)} ms ({self.backend}) "
             f"| skipped {self.skipped} (converter busy), held {self.held} (acks)")
        self.t0 = now
        last = self.last_pub
        self.reset()
        self.last_pub = last


class ShotEngine:
    """Two loops: the capture loop starts the next screenshot RPC as soon as the previous one returns
    (paced only by the fps cap), and the converter turns the newest PNG into a JPEG in a worker thread,
    one at a time. A PNG that arrives while the converter is busy replaces the one waiting (newest
    wins), so the frame rate is min(1/rpc, 1/convert) instead of 1/(rpc + convert)."""
    mode = "screenshot"

    def __init__(self, m: "Mirror", s):
        self.m, self.s = m, s
        self.conv = PngConverter()
        self.slot: tuple | None = None                     # (png, rpc start, rpc end): newest PNG waiting
        self.ready = asyncio.Event()
        self.stats = ShotStats(self.conv.backend)

    async def _capture_loop(self) -> None:
        fails = 0
        while True:
            t0 = time.monotonic()
            try:
                png = await capture_png(self.s)
                fails = 0
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                fails += 1
                e = classify(exc)
                dlog(f"mirror (screenshot): {exc!r}")
                if e.code == "unreachable" and fails < 30:
                    await asyncio.sleep(2)
                    continue
                raise e from None
            t1 = time.monotonic()
            self.stats.rpc.append(t1 - t0)
            if self.slot is not None:
                self.stats.skipped += 1        # the converter is still on an older one: drop the waiting PNG
            self.slot = (png, t0, t1)
            self.ready.set()
            self.stats.maybe_log()
            delay = self.m.shot_interval() - (time.monotonic() - t0)
            if delay > 0:
                await asyncio.sleep(delay)

    # -- convert (one at a time)
    def _convert(self, png: bytes, t: float, got: float) -> None:
        m, st = self.m, self.stats
        st.wait.append(time.monotonic() - got)      # from the RPC's return to the converter picking it up
        rec = m.recorder if m.recorder is not None and m.recorder.mode == self.mode else None
        show = bool(m.viewers)
        if show and not m.sink.ready(0):
            st.held += 1
            show = False
        if not show and rec is None:
            return
        data, ext, w, h, dec, enc = self.conv.convert(png, m.scale, m.quality)
        st.dec.append(dec)
        st.enc.append(enc)
        t2 = time.monotonic()
        if show:
            m.sink.publish(data, ext, w, h, self.mode)
        if rec is not None:
            rec.add_frame(data, ext, t)
        st.wr.append(time.monotonic() - t2)
        st.published(time.monotonic())

    async def _convert_loop(self) -> None:
        while True:
            await self.ready.wait()
            self.ready.clear()
            item, self.slot = self.slot, None
            if item is None:
                continue
            png, t, got = item
            try:
                await asyncio.to_thread(self._convert, png, t, got)
            except Exception as exc:  # noqa: BLE001 - one bad frame mustn't stop the mirror
                dlog(f"mirror (screenshot): frame conversion failed {exc!r}")

    async def run(self) -> None:
        dlog(f"mirror (screenshot): capture via dvt, decode via {self.conv.backend}")
        conv = asyncio.create_task(self._convert_loop(), name="mirror-convert")
        try:
            await self._capture_loop()
        finally:
            conv.cancel()
            with contextlib.suppress(BaseException):
                await conv


# ====================================================================== stream engine

_RTP_KEY_NALS = (19, 20, 21)       # IDR_W_RADL, IDR_N_LP, CRA
_VPS, _SPS, _PPS = 32, 33, 34


def build_pli(local_ssrc: int, remote_ssrc: int) -> bytes:
    """RTCP Picture Loss Indication (RFC 4585): ask the encoder for a fresh keyframe."""
    return struct.pack("!BBHII", 0x81, 0xCE, 2, local_ssrc & 0xFFFFFFFF, remote_ssrc & 0xFFFFFFFF)


def build_rr(local_ssrc: int, remote_ssrc: int, highest_seq: int) -> bytes:
    """RTCP receiver report + empty-CNAME SDES, as Xcode sends it. Without RRs the encoder stops after
    ~25 s (RTCPTimeoutEnabled in the stream config)."""
    rr = struct.pack("!BBHII BBBB IIII", 0x81, 0xC9, 7, local_ssrc & 0xFFFFFFFF, remote_ssrc & 0xFFFFFFFF,
                     0, 0, 0, 0, highest_seq & 0xFFFFFFFF, 0, 0, 0)
    sdes = struct.pack("!BBHI BBBB", 0x81, 0xCA, 2, local_ssrc & 0xFFFFFFFF, 0x01, 0x00, 0x00, 0x00)
    return rr + sdes


class Decoder:
    """PyAV HEVC decoder on a worker thread. Every access unit is decoded (deltas need their
    references); only the frames the sink wants are scaled and encoded to JPEG."""

    def __init__(self, engine: "StreamEngine", loop: asyncio.AbstractEventLoop):
        import queue
        self.engine = engine
        self.loop = loop
        self.q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=DECODE_QUEUE)
        self.latest = None               # newest decoded av.VideoFrame (thumbnails)
        self.decoded = 0
        self.errors = 0
        self.encode_errors = 0
        self._stop = threading.Event()
        self.codec = av.CodecContext.create("hevc", "r")
        with contextlib.suppress(Exception):
            self.codec.thread_type = "SLICE"     # frame threading adds latency (and waits for more input)
            self.codec.thread_count = 0
        self.thread = threading.Thread(target=self._run, name="mirror-hevc", daemon=True)
        self.thread.start()

    def feed(self, annexb: bytes) -> bool:
        """False when the decoder is too far behind (the caller then waits for the next keyframe)."""
        import queue
        try:
            self.q.put_nowait(annexb)
            return True
        except queue.Full:
            with contextlib.suppress(Exception):
                while True:
                    self.q.get_nowait()
            return False

    def close(self):
        self._stop.set()
        with contextlib.suppress(Exception):
            self.q.put_nowait(None)
        self.thread.join(timeout=2)

    def _key_please(self):
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self.engine.request_key, "decode")

    def _run(self):
        import queue
        m = self.engine.m
        while not self._stop.is_set():
            try:
                item = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                frames = self.codec.decode(av.Packet(item))
            except Exception as exc:  # noqa: BLE001 - concealed / rejected AU: ask for a key
                self.errors += 1
                if self.errors <= 3 or self.errors % 50 == 0:
                    dlog(f"mirror (stream): decode error {exc!r}")
                self._key_please()
                continue
            if not frames:
                continue
            fr = frames[-1]
            self.latest = fr
            self.decoded += 1
            if not m.viewers or not m.sink.ready(m.stream_interval()):
                continue
            try:
                tw = max(2, round(fr.width * m.scale) // 2 * 2)
                th = max(2, round(fr.height * m.scale) // 2 * 2)
                img = fr.reformat(width=tw, height=th, format="rgb24", interpolation="AREA").to_image()
                img = uncollapse(img)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=int(m.quality))
                m.sink.publish(buf.getvalue(), "jpg", tw, th, "stream")
            except Exception as exc:  # noqa: BLE001
                self.encode_errors += 1
                if self.encode_errors <= 3 or self.encode_errors % 100 == 0:
                    dlog(f"mirror (stream): frame encode failed {exc!r} ({self.encode_errors} so far)")

    def thumbnail(self, max_dim: int = 320):
        fr = self.latest
        if fr is None:
            return None
        s = max_dim / max(fr.width, fr.height)
        return fr.reformat(width=max(2, round(fr.width * s)), height=max(2, round(fr.height * s)),
                           format="rgb24").to_image()


class StreamEngine:
    mode = "stream"

    def __init__(self, m: "Mirror", s):
        self.m, self.s = m, s
        self.svc = None
        self.transport = None
        self.decoder: Decoder | None = None
        self.tasks: list[asyncio.Task] = []
        self.vps = self.sps = self.pps = None
        self.local_ssrc = self.remote_ssrc = 0
        self.rtcp_dest = None
        self.highest = 0
        self.packets = 0
        self.last_au_t = 0.0
        self.last_key_req = 0.0
        self.need_key = True
        self.au_bytes: deque = deque()

    # -- checks
    @staticmethod
    def unavailable_reason(s) -> str | None:
        """Why stream mode can't run on this session (None: it may; the phone has the last word)."""
        load_media()
        if DisplayService is None:
            return "no-decoder"
        if av is None or Image is None:
            return "no-decoder"
        ios = str((s.info or {}).get("ios") or "")
        if ios:
            with contextlib.suppress(Exception):
                if Version(ios) < VIDEO_STREAM_MIN_VERSION:
                    return "unsupported"
        if s.rsd is not None and not s.udp_available():
            return "no-udp"
        return None

    # -- one stream session
    async def open(self) -> None:
        s = self.s
        reason = self.unavailable_reason(s)
        if reason:
            raise StreamUnavailable(reason, STREAM_IMPORT_ERROR or "")
        rsd = await s.get_rsd()
        with contextlib.suppress(Exception):
            if Version(rsd.product_version) < VIDEO_STREAM_MIN_VERSION:
                raise StreamUnavailable("unsupported", f"iOS {rsd.product_version}")
        if not s.udp_available():
            raise StreamUnavailable("no-udp", "the userspace tunnel isn't active")
        if s in self.m._owed:                # a previous stream may still be up on the phone
            await self.m.stop_streams(s)
        svc = DisplayService(rsd)
        await asyncio.wait_for(svc.connect(), 15)
        transport = None
        try:
            transport = s.userspace_udp()
            self.m.stream_used(s)          # from here on a stop_all_streams is owed
            answer = await asyncio.wait_for(svc.start_video_stream(
                receiver_ip=transport.local_ip, receiver_port=transport.port,
                sender_ip=rsd.service.address[0], display_id=1), 25)
        except BaseException as exc:
            if transport is not None:
                transport.close()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(svc.close(), 2)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if is_media_in_use_error(exc):
                raise StreamUnavailable("media-in-use", str(exc)) from None
            if isinstance(exc, DeviceVersionNotSupportedError):
                raise StreamUnavailable("unsupported", str(exc)) from None
            raise
        cfg = (answer.get("connection") or {}).get("streamConfig") or {}
        port = int(cfg.get("SourcePort", 0) or 0)
        # streamConfig names are from the phone's side: RemoteSSRC is ours, LocalSSRC the phone's.
        self.local_ssrc = int(cfg.get("RemoteSSRC", 0) or 0)
        self.remote_ssrc = int(cfg.get("LocalSSRC", 0) or 0)
        self.rtcp_dest = (rsd.service.address[0], port) if port else None
        self.svc, self.transport = svc, transport
        self.highest = self.packets = 0
        self.need_key = True
        self.last_au_t = time.monotonic()
        loop = asyncio.get_running_loop()
        if self.decoder is None:
            self.decoder = Decoder(self, loop)
        self.tasks = [asyncio.create_task(self._recv(transport), name="mirror-rtp"),
                      asyncio.create_task(self._rtcp(transport), name="mirror-rtcp"),
                      asyncio.create_task(self._refresh(), name="mirror-refresh")]
        dlog(f"mirror (stream): started, rtcp={self.rtcp_dest} ssrc={self.local_ssrc}/{self.remote_ssrc}")

    async def close(self) -> None:
        tasks, self.tasks = self.tasks, []
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        tr, self.transport = self.transport, None
        if tr is not None:
            tr.close()
        svc, self.svc = self.svc, None
        if svc is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(svc.close(), 2)

    async def run(self) -> None:
        restarts = 0
        first = True
        try:
            while True:
                try:
                    await self.open()
                except StreamUnavailable:
                    raise
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001
                    e = classify(exc)
                    dlog(f"mirror (stream): start failed {exc!r}")
                    if first or e.code != "unreachable":
                        raise StreamUnavailable("failed", f"{type(exc).__name__}: {exc}") from None
                    await asyncio.sleep(2)     # tunnel reconnecting: try again
                    continue
                first = False
                got = False
                while True:
                    await asyncio.sleep(0.5)
                    if not self.tasks or self.tasks[0].done():
                        break
                    if self.m.au_count_since_open:
                        got = True
                    if time.monotonic() - self.last_au_t > STALL_S:
                        dlog(f"mirror (stream): no picture for {STALL_S:g} s, restarting")
                        break
                await self.close()
                restarts = 0 if got else restarts + 1
                if restarts > MAX_RESTARTS:
                    raise StreamUnavailable("failed", "the stream stopped sending pictures")
                self.m.au_count_since_open = 0
                await self.m.stop_streams(self.s)   # clear the old session before starting a new one
                await asyncio.sleep(1)
        finally:
            await self.close()
            if self.decoder is not None:
                dec, self.decoder = self.decoder, None
                await asyncio.to_thread(dec.close)

    # -- keyframes
    def request_key(self, reason: str = "") -> None:
        now = time.monotonic()
        if now - self.last_key_req < 0.7 or self.transport is None or self.rtcp_dest is None:
            return
        if not (self.local_ssrc and self.remote_ssrc):
            return
        self.last_key_req = now
        tr, dest = self.transport, self.rtcp_dest

        async def send():
            with contextlib.suppress(OSError, Exception):
                await tr.sendto(build_pli(self.local_ssrc, self.remote_ssrc), *dest)
        asyncio.ensure_future(send())

    # -- RTP in
    def _track_seq(self, seq: int) -> None:
        cur = self.highest
        cycles = (cur >> 16) & 0xFFFF
        last16 = cur & 0xFFFF
        if seq < last16 and (last16 - seq) > 0x8000:
            cycles = (cycles + 1) & 0xFFFF
        new = (cycles << 16) | seq
        if cur == 0 or ((new - cur) & 0xFFFFFFFF) < 0x80000000:
            self.highest = new

    async def _recv(self, transport) -> None:
        fu = bytearray()
        au: list[bytes] = []
        key = False
        corrupt = False
        last_seq = None
        nals: list[bytes] = []
        while True:
            try:
                data = await transport.recv(65535)
            except (OSError, asyncio.CancelledError):
                return
            except Exception as exc:  # noqa: BLE001
                dlog(f"mirror (stream): receive failed {exc!r}")
                return
            if len(data) < 12:
                continue
            pt = data[1] & 0x7F
            if 64 <= pt <= 95:                      # RTCP from the phone
                continue
            marker = data[1] >> 7
            hl = 12 + (data[0] & 0x0F) * 4
            if data[0] & 0x10:                      # header extension
                if len(data) < hl + 4:
                    continue
                hl += 4 + int.from_bytes(data[hl + 2:hl + 4], "big") * 4
            seq = int.from_bytes(data[2:4], "big")
            self.packets += 1
            self._track_seq(seq)
            if last_seq is not None and seq != ((last_seq + 1) & 0xFFFF):
                fu.clear()                          # loss or reorder: this access unit is broken
                corrupt = True
            if last_seq is None or ((seq - last_seq) & 0xFFFF) < 0x8000:
                last_seq = seq
            nals.clear()
            depacketize_hevc(data[hl:], fu, nals)
            for nal in nals:
                if not nal:
                    continue
                nt = (nal[0] >> 1) & 0x3F
                if nt == _VPS:
                    self.vps = nal
                elif nt == _SPS:
                    self.sps = nal
                elif nt == _PPS:
                    self.pps = nal
                if nt in _RTP_KEY_NALS:
                    key = True
                au.append(nal)
            if marker:
                if corrupt:
                    self.need_key = True
                    self.request_key("loss")
                elif au:
                    self._on_au(au, key)
                au = []
                key = False
                corrupt = False

    def _on_au(self, nals: list[bytes], key: bool) -> None:
        now = time.monotonic()
        size = sum(len(n) for n in nals)
        if not key:
            self.au_bytes.append((now, size))
        while self.au_bytes and self.au_bytes[0][0] < now - 1.0:
            self.au_bytes.popleft()
        if self.need_key and not key:
            self.request_key("waiting for key")
            return
        if key:
            self.need_key = False
            types = {(n[0] >> 1) & 0x3F for n in nals}
            if _SPS not in types and self.vps and self.sps and self.pps:
                nals = [self.vps, self.sps, self.pps] + nals
        self.last_au_t = now
        self.m.au_count_since_open += 1
        annexb = b"".join(b"\x00\x00\x00\x01" + n for n in nals)
        rec = self.m.recorder
        if rec is not None and rec.mode == self.mode:
            rec.add_au(annexb, key, now)
        if self.decoder is not None and not self.decoder.feed(annexb):
            self.need_key = True
            self.request_key("decoder behind")

    # -- RTCP out
    async def _rtcp(self, transport) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.rtcp_dest is None or not self.packets or not (self.local_ssrc and self.remote_ssrc):
                continue
            try:
                await transport.sendto(build_rr(self.local_ssrc, self.remote_ssrc, self.highest), *self.rtcp_dest)
            except OSError:
                return

    async def _refresh(self) -> None:
        """One keyframe after motion settles (so a still screen is sharp again after the encoder dropped
        resolution) and a heartbeat one every 10 s while idle. Never during motion: forced keyframes
        under load make the encoder shrink the picture further."""
        quiet_since = None
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            moving = sum(n for _, n in self.au_bytes) >= 200_000
            if moving:
                quiet_since = None
                continue
            if quiet_since is None:
                quiet_since = now
            if (now - quiet_since >= 1.5 and last < quiet_since) or now - last >= 10:
                self.last_key_req = 0
                self.request_key("settle")
                last = now


# ====================================================================== recorder

class Recorder:
    """One recording. Thread-safe adds; finish() writes the MP4 and returns the result dict."""

    def __init__(self, mode: str, max_seconds: float, client):
        load_media()
        self.mode = mode
        self.max_seconds = max_seconds
        self.client = client
        self.state = "recording"
        self.t0 = time.monotonic()
        self.started = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        REC_DIR.mkdir(parents=True, exist_ok=True)
        self.path = REC_DIR / f"rec-{stamp}.mp4"
        n = 1
        while self.path.exists():
            n += 1
            self.path = REC_DIR / f"rec-{stamp}-{n}.mp4"
        self.thumb = self.path.with_suffix(".jpg")
        self.tmp = REC_DIR / f".{self.path.stem}.parts"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.frames = 0
        self.bytes = 0
        self.times: list[float] = []
        self.files: list[str] = []
        self.raw = open(self.tmp / "video.hevc", "wb") if mode == "stream" else None
        self.got_key = False
        self.last_frame: bytes | None = None
        self.result: dict | None = None
        self.task: asyncio.Task | None = None
        self.stop_reason = ""

    @property
    def seconds(self) -> float:
        return round(time.monotonic() - self.t0, 1)

    def add_au(self, annexb: bytes, key: bool, t: float) -> None:
        with self.lock:
            if self.state != "recording" or self.raw is None:
                return
            if not self.got_key and not key:
                return
            self.got_key = True
            self.raw.write(annexb)
            self.times.append(t)
            self.frames += 1
            self.bytes += len(annexb)

    def add_frame(self, data: bytes, ext: str, t: float) -> None:
        with self.lock:
            if self.state != "recording":
                return
            name = f"{len(self.files):05d}.{ext}"
            (self.tmp / name).write_bytes(data)
            self.files.append(name)
            self.times.append(t)
            self.frames += 1
            self.bytes += len(data)
            self.last_frame = data

    def snapshot(self) -> dict:
        out = {"state": self.state, "mode": self.mode, "seconds": self.seconds if self.state == "recording"
               else (self.result or {}).get("seconds", self.seconds), "bytes": self.bytes,
               "frames": self.frames, "maxSeconds": self.max_seconds, "path": str(self.path),
               "started": self.started}
        if self.result:
            out.update(self.result)
        return out

    # -- finishing
    def _close_input(self) -> float:
        with self.lock:
            self.state = "finishing"
            if self.raw is not None:
                self.raw.close()
            return self.seconds

    def finalize(self, thumb_img) -> dict:
        """Blocking: write the MP4 (and a thumbnail); returns {path, seconds, bytes, fps, mode, thumbnail}."""
        seconds = round((self.times[-1] - self.times[0]) if len(self.times) > 1 else 0.0, 2)
        if not self.frames:
            raise err("record-empty", "The recording has no pictures. Make sure your iPhone is unlocked with "
                      "the screen on, then try again.")
        if self.mode == "stream":
            self._remux()
        else:
            self._concat()
        thumb = None
        with contextlib.suppress(Exception):
            if thumb_img is None and self.last_frame is not None and Image is not None:
                thumb_img = Image.open(io.BytesIO(self.last_frame))
            if thumb_img is not None:
                thumb_img = thumb_img.convert("RGB")
                thumb_img.thumbnail((320, 320))
                thumb_img.save(self.thumb, "JPEG", quality=75)
                thumb = str(self.thumb)
        size = self.path.stat().st_size
        fps = round((self.frames - 1) / seconds, 1) if seconds > 0 else float(self.frames)
        return {"state": "done", "path": str(self.path), "seconds": max(seconds, 0.1), "bytes": size,
                "fps": fps, "frames": self.frames, "mode": self.mode, "thumbnail": thumb}

    def _remux(self) -> None:
        raw = self.tmp / "video.hevc"
        if av is not None:
            try:
                n = remux_hevc(raw, self.times, self.path)
                dlog(f"record: remuxed {n} pictures into {self.path.name}")
                return
            except Exception as exc:  # noqa: BLE001
                dlog(f"record: PyAV remux failed ({exc!r}), trying ffmpeg")
        ff = ffmpeg_path()
        if not ff:
            raise err("no-ffmpeg", "Saving the recording needs ffmpeg. Install it in Setup, then record again.",
                      FFMPEG_FIX)
        span = (self.times[-1] - self.times[0]) if len(self.times) > 1 else 1.0
        rate = max(1.0, min(60.0, (len(self.times) - 1) / span if span > 0 else 30.0))
        run_ffmpeg([ff, "-hide_banner", "-loglevel", "error", "-y", "-r", f"{rate:.3f}", "-f", "hevc", "-i", str(raw),
                    "-c", "copy", "-tag:v", "hvc1", "-movflags", "+faststart", str(self.path)])

    def _concat(self) -> None:
        ff = ffmpeg_path()
        if not ff:
            raise err("no-ffmpeg", "Saving the recording needs ffmpeg. Install it in Setup, then record again.",
                      FFMPEG_FIX)
        write_concat(self.tmp / "frames.ffconcat", self.files, self.times)
        w, h = 0, 0
        first = (self.tmp / self.files[0]).read_bytes()
        if Image is not None:
            with Image.open(io.BytesIO(first)) as im:
                w, h = im.size
        else:
            w, h = png_size(first)
        w, h = max(2, w // 2 * 2), max(2, h // 2 * 2)
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
              "format=yuv420p")
        run_ffmpeg([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(self.tmp / "frames.ffconcat"), "-vf", vf, "-fps_mode", "vfr", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart", str(self.path)])

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def sweep_stale_parts() -> None:
    """Work files a recording leaves behind when the daemon dies mid-recording (.rec-*.parts folders,
    half-written .rec-*.tmp.mp4). Only old ones: another daemon (a parallel checkout) may be recording."""
    now = time.time()
    for p in REC_DIR.glob(".rec-*"):
        try:
            times = [p.lstat().st_mtime]
            if p.is_dir():
                times += [q.lstat().st_mtime for q in p.iterdir()]
            if now - max(times) < REC_STALE_S:
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            dlog(f"record: removed leftover {p.name}")
        except OSError:
            pass


def write_concat(listfile: Path, files: list[str], times: list[float]) -> None:
    """ffconcat list with each frame shown until the next one was captured (real, variable timing)."""
    lines = ["ffconcat version 1.0"]
    for i, name in enumerate(files):
        dur = (times[i + 1] - times[i]) if i + 1 < len(times) else 0.1
        lines += [f"file '{name}'", f"duration {max(0.001, dur):.3f}"]
    lines.append(f"file '{files[-1]}'")    # the concat demuxer ignores the last entry's duration
    listfile.write_text("\n".join(lines) + "\n")


def run_ffmpeg(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise err("record-failed", "Couldn't save the recording.", None, (p.stderr or "")[-800:])


def remux_hevc(raw: Path, times: list[float], out: Path) -> int:
    """Annex-B HEVC + per-picture arrival times -> MP4 (no re-encode), with the real (variable) timing."""
    with av.open(str(raw), format="hevc") as inp:
        ins = inp.streams.video[0]
        tmp = out.with_name(f".{out.name}.tmp.mp4")
        with av.open(str(tmp), "w", format="mp4", options={"movflags": "+faststart"}) as o:
            ost = o.add_stream_from_template(ins)
            with contextlib.suppress(Exception):
                ost.codec_tag = "hvc1"     # plays in more players than hev1
            ost.time_base = Fraction(1, 1000)
            t0 = times[0] if times else 0.0
            n, last = 0, -1
            for pkt in inp.demux(ins):
                if pkt.size == 0:
                    continue
                t = times[min(n, len(times) - 1)] if times else n / 30
                ms = max(last + 1, round((t - t0) * 1000))
                nxt = times[n + 1] if n + 1 < len(times) else t + 1 / 30
                pkt.pts = pkt.dts = ms
                pkt.duration = max(1, round((nxt - t) * 1000))
                pkt.time_base = Fraction(1, 1000)
                pkt.stream = ost
                o.mux(pkt)
                last = ms
                n += 1
        if not n:
            tmp.unlink(missing_ok=True)
            raise ValueError("no pictures in the stream")
        os.replace(tmp, out)
    return n


# ====================================================================== mirror

def clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


class Mirror:
    KEY = "mirror"
    REC_KEY = "record"

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.stats_task: asyncio.Task | None = None
        self.requested = "auto"          # auto | stream | screenshot
        self.mode: str | None = None     # what's running: stream | screenshot
        self.fps: float | None = None    # None: as fast as the mode allows
        self.scale = 0.5
        self.quality = 70
        self.sink = FrameSink()
        self.viewers: set = set()
        self.notice: str | None = None
        self.notice_code: str | None = None
        self.engine = None
        self.session = None
        self.recorder: Recorder | None = None
        self.last_record: dict | None = None
        self.au_count_since_open = 0
        self._d = None
        self._owed: set = set()          # sessions that may still have a stream open on the phone
        self._bg: set = set()

    # -- rates
    def shot_interval(self) -> float:
        fps = self.fps or SHOT_FPS[1]
        if self.recorder is not None and self.recorder.mode == "screenshot":
            fps = max(fps, REC_SHOT_FPS)
        return 1.0 / max(SHOT_FPS[0], min(SHOT_FPS[1], fps))

    def stream_interval(self) -> float:
        return 1.0 / max(STREAM_FPS[0], min(STREAM_FPS[1], self.fps or STREAM_FPS[1]))

    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def state(self, **extra) -> dict:
        out = {"running": self.running(), "fps": self.fps or (STREAM_FPS[1] if self.mode == "stream" else SHOT_FPS[1]),
               "mode": self.mode if self.running() else None, "requestedMode": self.requested,
               "measuredFps": self.sink.measured_fps() if self.running() else 0.0,
               "dropped": self.sink.dropped, "scale": self.scale, "quality": self.quality,
               "recording": self.recorder is not None and self.recorder.state == "recording"}
        if self.notice and self.running():
            out["notice"] = self.notice
            out["noticeCode"] = self.notice_code
        out.update(extra)
        return out

    def announce(self, **extra):
        emit("mirror", self.state(**extra))

    # -- stop_all_streams bookkeeping
    def stream_used(self, s):
        self._owed.add(s)

    async def stop_streams(self, s) -> None:
        """DisplayService.stop_all_streams on its own connection (bounded, shielded from cancellation).
        If the tunnel is down, it's sent as soon as the session reconnects."""
        if s not in self._owed or DisplayService is None:
            return
        rsd = s.rsd
        if rsd is None:
            self._defer_stop(s)
            return

        async def go():
            try:
                await asyncio.wait_for(DisplayService.stop_all_streams(rsd), 6)
                self._owed.discard(s)
                dlog("mirror (stream): stop_all_streams sent")
            except BaseException as exc:  # noqa: BLE001
                dlog(f"mirror (stream): stop_all_streams failed {exc!r}")
                self._defer_stop(s)
        t = asyncio.ensure_future(go())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        with contextlib.suppress(BaseException):
            await asyncio.shield(t)

    def _defer_stop(self, s):
        if s.closing:
            self._owed.discard(s)          # its tunnel is gone for good; don't keep the session alive
            return
        if getattr(s, "_mirror_stop_pending", False):
            return
        s._mirror_stop_pending = True

        async def later():
            try:
                for _ in range(40):            # up to ~20 min of reconnect attempts
                    if s.closing or s not in self._owed:
                        self._owed.discard(s)
                        return
                    if self.running() and self.session is s and isinstance(self.engine, StreamEngine):
                        return               # the stream engine clears it itself before restarting
                    try:
                        rsd = await s.get_rsd(timeout=30)
                    except BaseException:  # noqa: BLE001
                        continue
                    if self.running() and self.session is s and isinstance(self.engine, StreamEngine):
                        return
                    try:
                        await asyncio.wait_for(DisplayService.stop_all_streams(rsd), 6)
                        self._owed.discard(s)
                        dlog("mirror (stream): stop_all_streams sent after reconnect")
                        return
                    except BaseException as exc:  # noqa: BLE001
                        dlog(f"mirror (stream): deferred stop_all_streams failed {exc!r}")
                        await asyncio.sleep(5)
                self._owed.discard(s)          # gave up: don't pin the session forever
            finally:
                s._mirror_stop_pending = False
        t = asyncio.ensure_future(later())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # -- lifecycle
    def start_task(self, d, s):
        self.session = s
        self.sink.reset(s.udid)
        self.task = asyncio.create_task(self._run(d, s), name="mirror")
        if self.stats_task is None or self.stats_task.done():
            self.stats_task = asyncio.create_task(self._stats(), name="mirror-stats")

    async def _stats(self):
        while self.running():
            await asyncio.sleep(2)
            if self.running():
                self.announce()
                rec = self.recorder
                if rec is not None and rec.state == "recording":
                    emit("record", rec.snapshot())

    async def _run(self, d, s):
        req = self.requested
        self.notice = self.notice_code = None
        try:
            if req in ("auto", "stream"):
                eng = StreamEngine(self, s)
                self.engine, self.mode = eng, "stream"
                self.announce()
                try:
                    await eng.run()
                except StreamUnavailable as e:
                    dlog(f"mirror (stream): unavailable: {e}")
                    if req == "stream":
                        raise e.to_error() from None
                    self.notice, self.notice_code = NOTICE.get(e.code, NOTICE["failed"]), e.code
                finally:
                    await self.stop_streams(s)
                self._mode_changed("screenshot")
            eng = ShotEngine(self, s)
            self.engine, self.mode = eng, "screenshot"
            self.announce()
            await eng.run()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            e = classify(exc)
            dlog(f"mirror: stopped by {exc!r}")
            self.task = None
            d.resources.forget(self.KEY)      # ended by itself: every holder is released
            self.viewers.clear()
            await self._end_recording("error")
            self.mode = None
            emit("mirror", self.state(running=False, error=e.to_json()))
        finally:
            self.engine = None

    def _mode_changed(self, new_mode: str):
        rec = self.recorder
        if rec is not None and rec.state == "recording" and rec.mode != new_mode:
            rec.stop_reason = "mode"
            asyncio.ensure_future(self._end_recording("mode"))

    async def stop(self, *_):
        await self._end_recording("stopped")
        t, self.task = self.task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        if self.session is not None:
            await self.stop_streams(self.session)
        self.mode = None
        self.viewers.clear()
        emit("mirror", self.state(running=False))

    async def restart(self, d, s):
        """Settings that need a new engine (mode) changed while running."""
        await self._end_recording("mode")
        t, self.task = self.task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        await self.stop_streams(s)
        self.start_task(d, s)

    async def ensure(self, d, s, holder):
        async def open_():
            if not self.running():
                await s.get_rsd()
                self.start_task(d, s)
            return self
        # Reference counted per holder: the mirror stops when the last one stops or disconnects.
        await d.resources.acquire(self.KEY, holder, open_, self.stop, scope="session")

    # -- recording
    async def _end_recording(self, reason: str):
        rec = self.recorder
        if rec is None:
            return None
        if rec.task is None:
            rec.stop_reason = rec.stop_reason or reason
            rec._close_input()
            thumb = None
            eng = self.engine
            if rec.mode == "stream" and isinstance(eng, StreamEngine) and eng.decoder is not None:
                with contextlib.suppress(Exception):
                    thumb = eng.decoder.thumbnail()
            rec.task = asyncio.ensure_future(self._finish(rec, thumb))
        return rec.task

    async def _finish(self, rec: Recorder, thumb_img) -> dict:
        emit("record", rec.snapshot())
        try:
            result = await asyncio.to_thread(rec.finalize, thumb_img)
            result["reason"] = rec.stop_reason
        except BaseException as exc:  # noqa: BLE001
            e = classify(exc)
            dlog(f"record: failed {exc!r}")
            result = {"state": "failed", "error": e.to_json(), "mode": rec.mode, "seconds": rec.seconds,
                      "bytes": 0, "reason": rec.stop_reason}
        finally:
            await asyncio.to_thread(rec.cleanup)
            await asyncio.to_thread(common.prune, REC_DIR.glob("rec-*.mp4"), REC_KEEP, REC_MAX_BYTES,
                                    also=(".jpg",))
        rec.result = result
        rec.state = result["state"]
        self.last_record = rec.snapshot()
        if self.recorder is rec:
            self.recorder = None
        emit("record", self.last_record)
        if self.mode is not None or self.running():
            self.announce()
        # the recording's own holds: its resource and its reference on the mirror
        d = getattr(self, "_d", None)
        if d is not None:
            await d.resources.close(self.REC_KEY, "released")
            await d.resources.release(self.KEY, REC_HOLDER)
        return self.last_record

    def availability(self, s) -> dict:
        load_media()
        stream_reason = StreamEngine.unavailable_reason(s) if s is not None else "no-device"
        if stream_reason is None and self.notice_code == "media-in-use":
            stream_reason = "media-in-use"
        return {"stream": {"available": stream_reason is None, "reason": stream_reason,
                           "message": NOTICE.get(stream_reason) if stream_reason else None},
                "screenshot": {"available": s is not None, "reason": None if s is not None else "no-device"},
                "jpeg": Image is not None, "ffmpeg": bool(ffmpeg_path())}


# ====================================================================== commands

def register(reg):
    m = Mirror()
    threading.Thread(target=sweep_stale_parts, name="record-sweep", daemon=True).start()

    def apply_args(a) -> bool:
        """Settings from mirror.start; True when the engine has to restart (mode changed)."""
        changed = False
        if "mode" in a and a.get("mode") is not None:
            mode = str(a.get("mode"))
            if mode not in ("auto", "stream", "screenshot"):
                raise err("bad-args", "mode must be auto, stream or screenshot.")
            changed = mode != m.requested
            m.requested = mode
        if a.get("fps") is not None:
            m.fps = clamp(a.get("fps"), SHOT_FPS[0], STREAM_FPS[1], 1.0)
        if a.get("scale") is not None:
            m.scale = clamp(a.get("scale"), 0.25, 1.0, 0.5)
        if a.get("quality") is not None:
            m.quality = int(clamp(a.get("quality"), 30, 95, 70))
        return changed

    @reg.command("mirror.start")
    async def c_mirror_start(d, a):
        s = d.need_session(a)
        m._d = d
        restart = apply_args(a)
        client = d.client()
        if m.running() and restart:
            await m.restart(d, s)
        await m.ensure(d, s, client)
        m.viewers.add(client)
        m.announce()
        return {"running": True, "fps": m.fps or (STREAM_FPS[1] if m.mode == "stream" else SHOT_FPS[1]),
                "mode": m.mode or "starting", "requestedMode": m.requested, "scale": m.scale, "quality": m.quality}

    @reg.command("mirror.stop")
    async def c_mirror_stop(d, a):
        client = d.client()
        m.viewers.discard(client)
        left = await d.resources.release(Mirror.KEY, client)
        if left:
            return {"running": False, "sharedWith": left}
        if m.running():
            await m.stop()
        return {"running": False}

    @reg.command("mirror.ack")
    async def c_mirror_ack(d, a):
        try:
            m.sink.ack(int(a.get("seq") or 0))
        except (TypeError, ValueError):
            raise err("bad-args", "seq must be a number.") from None
        return {}

    @reg.command("mirror.status")
    async def c_mirror_status(d, a):
        s = d.session
        return {**m.state(), "available": m.availability(s), "viewers": len(m.viewers),
                "record": m.recorder.snapshot() if m.recorder else m.last_record}

    @reg.command("record.start")
    async def c_record_start(d, a):
        s = d.need_session(a)
        m._d = d
        if m.recorder is not None:
            if m.recorder.state == "recording":
                raise err("busy", "A recording is already running. Stop it first.")
            raise err("busy", "The last recording is still being saved. Try again in a moment.")
        max_s = clamp(a.get("maxSeconds"), 1, REC_MAX_S, 60.0)
        if a.get("mode") is not None and not m.running():
            apply_args({"mode": a.get("mode")})
        await m.ensure(d, s, REC_HOLDER)
        for _ in range(100):                    # the engine settles on a mode (stream may fall back)
            if m.mode == "screenshot" or (m.mode == "stream" and m.au_count_since_open) or not m.running():
                break
            await asyncio.sleep(0.1)
        if not m.running():
            await d.resources.release(Mirror.KEY, REC_HOLDER)
            raise err("failed", "The screen view couldn't start, so there's nothing to record.")
        mode = m.mode or "screenshot"
        if mode == "screenshot" and not ffmpeg_path():
            await d.resources.release(Mirror.KEY, REC_HOLDER)
            raise err("no-ffmpeg", "Recording needs ffmpeg, which isn't installed. Install it in Setup.", FFMPEG_FIX)
        rec = Recorder(mode, max_s, d.client())
        m.recorder = rec
        if mode == "stream" and isinstance(m.engine, StreamEngine):
            m.engine.need_key = True            # start the file on a keyframe
            m.engine.last_key_req = 0
            m.engine.request_key("record")

        async def open_():
            return rec

        async def close_(handle, reason):       # client gone / tunnel / device switch / shutdown / timeout
            if handle is m.recorder:
                handle.stop_reason = handle.stop_reason or reason
                await m._end_recording(reason)
        # Auto-stop: the resource times out at maxSeconds; it also ends with the client or the tunnel.
        await d.resources.acquire(Mirror.REC_KEY, d.client(), open_, close_, scope="tunnel", timeout=max_s)
        emit("record", rec.snapshot())
        return rec.snapshot()

    @reg.command("record.stop")
    async def c_record_stop(d, a):
        rec = m.recorder
        if rec is None:
            if m.last_record:
                return m.last_record
            raise err("not-recording", "Nothing is being recorded.")
        rec.stop_reason = rec.stop_reason or "stopped"
        task = await m._end_recording("stopped")
        await d.resources.close(Mirror.REC_KEY, "released")
        result = await asyncio.shield(task)
        if result.get("state") == "failed" and result.get("error"):
            e = result["error"]
            raise err(e.get("code", "record-failed"), e.get("message", "Couldn't save the recording."),
                      e.get("fix"), e.get("detail"))
        return result

    @reg.command("record.status")
    async def c_record_status(d, a):
        if m.recorder is not None:
            return m.recorder.snapshot()
        return m.last_record or {"state": "idle"}

    @reg.on_client_gone
    def client_gone(d, client):
        m.viewers.discard(client)

    @reg.on_tunnel_drop
    async def tunnel_drop(d, s):
        rec = m.recorder
        if rec is not None and rec.state == "recording":
            rec.stop_reason = "disconnected"   # (the record resource is tunnel-scoped; this is a backstop)
            await m._end_recording("disconnected")

    @reg.on_shutdown
    async def shutdown(d):
        if m.recorder is not None:
            task = await m._end_recording("shutdown")
            if task is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(asyncio.shield(task), 20)
        if m.running():
            await m.stop()
        for s in list(m._owed):
            await m.stop_streams(s)

    @reg.welcome
    def snapshot(d):
        out = [("mirror", m.state())] if m.running() else []
        if m.recorder is not None:
            out.append(("record", m.recorder.snapshot()))
        return out
