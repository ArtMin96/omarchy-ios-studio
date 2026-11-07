"""Screen recordings and the mirror's command replies (features/mirror/real.py and demo.py): the recorder's caps
(time, pictures, bytes counted, clamping), finishing (ffmpeg mocked, never run), and the reply shapes of
mirror.start / mirror.status / record.start / record.stop / record.status in real and demo mode side by side.

Not repeated here (see test_daemon_loop_perf.py and test_short_recording.py): Depacketizer, RateWindow, the
max-seconds clamp with a frame cap of 3, and the wait for the first picture.

    python -m pytest tests/helper/test_dev_mirror.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_mirror
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import devtest

devtest.isolate()

from iosstudio.device import backend, protocol  # noqa: E402
from iosstudio.device.features.mirror import demo as MD  # noqa: E402
from iosstudio.device.features.mirror import real as M  # noqa: E402

SNAPSHOT_KEYS = {"state", "mode", "seconds", "bytes", "frames", "maxSeconds", "path", "started"}
DONE_KEYS = {"state", "path", "seconds", "bytes", "fps", "frames", "mode", "thumbnail"}
START_KEYS = {"running", "fps", "mode", "requestedMode", "scale", "quality"}
STATE_KEYS = {"running", "fps", "mode", "requestedMode", "measuredFps", "dropped", "scale", "quality", "recording"}


def quiet():
    return mock.patch.object(M, "dlog", lambda *a, **k: None)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mirror-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- real recorder

class RecorderCaps(TmpCase):
    def recorder(self, mode="screenshot", max_s=10.0):
        r = M.Recorder(mode, max_s, None, folder=self.tmp)
        self.addCleanup(r.cleanup)
        if r.raw is not None:
            self.addCleanup(r.raw.close)
        return r

    def test_max_seconds_lower_clamp_and_frame_cap_formula(self):
        for given, want in ((0, 1.0), (-5, 1.0), (0.2, 1.0), (2.5, 2.5), (M.REC_MAX_S + 1, M.REC_MAX_S)):
            r = self.recorder(max_s=given)
            self.assertEqual(r.max_seconds, want)
            self.assertEqual(r.max_frames, min(M.REC_MAX_FRAMES, int(want * 60) + 600))
        self.assertEqual(self.recorder(max_s=M.REC_MAX_S).max_frames, M.REC_MAX_FRAMES)

    def test_a_recording_in_the_same_second_gets_its_own_file(self):
        fake_time = devtest.FakeClock().module()
        fake_time.strftime = lambda *_a: "20260101-120000"
        with mock.patch.object(M, "time", fake_time):
            a = self.recorder()
            a.path.write_bytes(b"done")                       # the first one was saved
            b = self.recorder()
        self.assertEqual(b.path.name, a.path.stem + "-2.mp4")
        self.assertNotEqual(a.tmp, b.tmp)

    def test_frames_count_bytes_and_files(self):
        r = self.recorder()
        r.add_frame(b"a" * 10, "jpg", 1.0)
        r.add_frame(b"b" * 5, "png", 1.5)
        self.assertEqual((r.frames, r.bytes, r.files, r.times), (2, 15, ["00000.jpg", "00001.png"], [1.0, 1.5]))
        self.assertEqual((r.tmp / "00001.png").read_bytes(), b"b" * 5)
        self.assertEqual(r.last_frame, b"b" * 5)
        self.assertTrue(r.first.is_set())

    def test_time_cap_is_max_seconds_plus_slack(self):
        clock = devtest.FakeClock()
        with mock.patch.object(M, "time", clock.module()):
            r = self.recorder(max_s=5)
            clock.advance(5 + 10)
            r.add_frame(b"x", "jpg", clock())
            self.assertEqual(r.frames, 1)                     # at the edge: still kept
            clock.advance(0.01)
            r.add_frame(b"x", "jpg", clock())
            self.assertEqual(r.frames, 1)                     # past it: dropped
            self.assertEqual(r.seconds, 15.0)

    def test_nothing_is_added_after_finishing_starts(self):
        r = self.recorder()
        r.add_frame(b"x", "jpg", 1.0)
        r._close_input()
        r.add_frame(b"y", "jpg", 2.0)
        self.assertEqual((r.state, r.frames, r.bytes), ("finishing", 1, 1))

    def test_stream_waits_for_a_keyframe_and_is_capped(self):
        r = self.recorder("stream")
        r.add_au(b"delta", False, 1.0)
        self.assertEqual(r.frames, 0)                         # a file can't start on a delta frame
        self.assertFalse(r.first.is_set())
        r.add_au(b"key", True, 2.0)
        r.add_au(b"d2", False, 3.0)
        self.assertEqual((r.frames, r.bytes, r.times), (2, 5, [2.0, 3.0]))
        r.max_frames = 2
        r.add_au(b"more", True, 4.0)
        self.assertEqual(r.frames, 2)
        r._close_input()
        self.assertEqual((r.tmp / "video.hevc").read_bytes(), b"keyd2")

    def test_screenshot_recorder_ignores_access_units(self):
        r = self.recorder("screenshot")
        r.add_au(b"key", True, 1.0)
        self.assertEqual(r.frames, 0)

    def test_snapshot_shape(self):
        r = self.recorder(max_s=7)
        snap = r.snapshot()
        self.assertEqual(set(snap), SNAPSHOT_KEYS)
        self.assertEqual((snap["state"], snap["mode"], snap["maxSeconds"], snap["frames"]), ("recording", "screenshot", 7.0, 0))
        r.result = {"state": "done", "seconds": 3.3, "fps": 2.0}
        r.state = "done"
        snap = r.snapshot()
        self.assertEqual((snap["seconds"], snap["fps"]), (3.3, 2.0))   # the result's numbers win


class RecorderFinish(TmpCase):
    def recorder(self, mode="screenshot"):
        r = M.Recorder(mode, 10, None, folder=self.tmp)
        self.addCleanup(r.cleanup)
        if r.raw is not None:
            self.addCleanup(r.raw.close)
        return r

    def fake_ffmpeg(self):
        """run_ffmpeg stand-in: records the command and writes the output file it names."""
        cmds: list[list[str]] = []

        def run(cmd):
            cmds.append(cmd)
            Path(cmd[-1]).write_bytes(b"mp4" * 100)
        return cmds, mock.patch.multiple(M, run_ffmpeg=run, ffmpeg_path=lambda: "/usr/bin/ffmpeg")

    def test_empty_recording_fails(self):
        r = self.recorder()
        r._close_input()
        with self.assertRaises(Exception) as cm:
            r.finalize(None)
        self.assertEqual(cm.exception.code, "record-empty")

    def test_concat_result_and_command(self):
        r = self.recorder()
        frame = tiny_png(31, 17)
        for t in (10.0, 10.5, 11.0, 12.0):
            r.add_frame(frame, "png", t)
        r._close_input()
        cmds, patch = self.fake_ffmpeg()
        with patch, mock.patch.object(M, "Image", None):          # no Pillow: size from the PNG header
            out = r.finalize(None)
        self.assertEqual(set(out), DONE_KEYS)
        self.assertEqual((out["state"], out["frames"], out["seconds"], out["fps"], out["bytes"]),
                         ("done", 4, 2.0, 1.5, 300))
        self.assertIsNone(out["thumbnail"])
        vf = cmds[0][cmds[0].index("-vf") + 1]
        self.assertTrue(vf.startswith("scale=30:16:"), vf)              # even dimensions for H.264
        listing = (r.tmp / "frames.ffconcat").read_text().splitlines()
        self.assertEqual(listing[0], "ffconcat version 1.0")
        self.assertEqual(listing[-1], "file '00003.png'")

    def test_one_picture(self):
        r = self.recorder()
        r.add_frame(tiny_png(4, 4), "png", 5.0)
        r._close_input()
        _cmds, patch = self.fake_ffmpeg()
        with patch:
            out = r.finalize(None)
        self.assertEqual((out["frames"], out["seconds"], out["fps"]), (1, 0.1, 1.0))

    def test_no_ffmpeg(self):
        r = self.recorder()
        r.add_frame(tiny_png(4, 4), "png", 1.0)
        r._close_input()
        with mock.patch.object(M, "ffmpeg_path", lambda: None):
            with self.assertRaises(Exception) as cm:
                r.finalize(None)
        self.assertEqual(cm.exception.code, "no-ffmpeg")
        self.assertEqual(cm.exception.fix, M.FFMPEG_FIX)

    def test_stream_remux_falls_back_to_ffmpeg_copy(self):
        r = self.recorder("stream")
        for i in range(3):
            r.add_au(b"\0\0\0\1" + bytes([i]), i == 0, 1.0 + i * 0.5)
        r._close_input()
        cmds, patch = self.fake_ffmpeg()
        with patch, mock.patch.object(M, "av", None), quiet():
            out = r.finalize(None)
        cmd = cmds[0]
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")             # no re-encode
        self.assertEqual(cmd[cmd.index("-r") + 1], "2.000")            # 2 intervals in 1 s
        self.assertEqual((out["mode"], out["frames"], out["seconds"]), ("stream", 3, 1.0))

    def test_ffmpeg_failure_is_record_failed_with_stderr(self):
        done = subprocess.CompletedProcess([], 1, "", "x" * 2000 + "codec not found")
        with mock.patch.object(M.subprocess, "run", return_value=done) as run:
            with self.assertRaises(Exception) as cm:
                M.run_ffmpeg(["ffmpeg"])
        self.assertEqual(cm.exception.code, "record-failed")
        self.assertTrue(cm.exception.detail.endswith("codec not found"))
        self.assertLessEqual(len(cm.exception.detail), 800)
        self.assertEqual(run.call_args.kwargs["timeout"], 600)


def tiny_png(w: int, h: int) -> bytes:
    import struct
    import zlib
    raw = b"".join(b"\0" + b"\0\0\0" * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class ConcatList(TmpCase):
    def test_durations_follow_capture_times(self):
        p = self.tmp / "l.ffconcat"
        M.write_concat(p, ["a.jpg", "b.jpg", "c.jpg"], [1.0, 1.0, 1.25])
        self.assertEqual(p.read_text().splitlines(), [
            "ffconcat version 1.0", "file 'a.jpg'", "duration 0.001",     # same instant: the minimum
            "file 'b.jpg'", "duration 0.250", "file 'c.jpg'", "duration 0.100", "file 'c.jpg'"])


class Clamp(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(M.clamp("5", 1, 300, 60.0), 5.0)
        self.assertEqual(M.clamp(None, 1, 300, 60.0), 60.0)
        self.assertEqual(M.clamp("abc", 1, 300, 60.0), 60.0)
        self.assertEqual(M.clamp(1e9, 1, 300, 60.0), 300)
        self.assertEqual(M.clamp(-3, 1, 300, 60.0), 1)
        self.assertEqual(M.clamp(float("inf"), 1, 300, 60.0), 300)
        self.assertEqual(MD._clamp(float("-inf"), 1, 300, 60.0), 1)

    @unittest.expectedFailure
    def test_nan_gives_the_default(self):
        # BUG: features/mirror/real.py clamp() (and demo _clamp): float("nan") passes float() and min(hi, nan)
        # returns hi, so {"maxSeconds": NaN} (Python's json accepts NaN) records the full 300 s, not the default.
        self.assertEqual(M.clamp(float("nan"), 1, 300, 60.0), 60.0)


# ---------------------------------------------------------------- demo recording

class DemoRecording(TmpCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(MD, "DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def test_seconds_never_pass_max_seconds(self):
        clock = devtest.FakeClock()
        with mock.patch.object(MD, "time", clock.module()):
            rec = MD.Recording("stream", 3.0)
            clock.advance(10)
            self.assertEqual(rec.seconds, 3.0)
            self.assertEqual(set(rec.snapshot()), SNAPSHOT_KEYS)

    def test_bytes_grow_like_a_phone_recording(self):
        rec = MD.Recording("screenshot", 3.0)
        rec.add("a.png")
        rec.add("b.png")
        self.assertEqual((rec.bytes, rec.snapshot()["frames"]), (180_000, 2))
        rec.state = "finishing"
        rec.add("c.png")
        self.assertEqual(len(rec.files), 2)

    def test_empty_and_no_ffmpeg_results(self):
        rec = MD.Recording("screenshot", 3.0)
        out = rec.finalize()
        self.assertEqual((out["state"], out["error"]["code"]), ("failed", "record-empty"))
        src = self.tmp / "f.png"
        src.write_bytes(tiny_png(4, 4))
        rec.add(src)
        with mock.patch.object(MD.shutil, "which", lambda _n: None):
            out = rec.finalize()
        self.assertEqual((out["state"], out["placeholder"], out["bytes"]), ("done", True, 0))
        self.assertLessEqual(DONE_KEYS, set(out))                 # the same keys as a real result

    def test_ffmpeg_command_mocked(self):
        rec = MD.Recording("screenshot", 3.0)
        src = self.tmp / "f.png"
        src.write_bytes(tiny_png(4, 4))
        rec.add(src)
        rec.add(src)

        def run(cmd, **kw):
            Path(cmd[-1]).write_bytes(b"v" * 42)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(MD.shutil, "which", lambda _n: "/usr/bin/ffmpeg"), \
                mock.patch.object(MD.subprocess, "run", run):
            out = rec.finalize()
        self.assertEqual((out["state"], out["bytes"], out["frames"]), ("done", 42, 2))
        self.assertEqual(set(out) - {"reason"}, DONE_KEYS)
        self.assertFalse(rec.path.with_suffix(".ffconcat").exists())   # list file cleaned up


# ---------------------------------------------------------------- commands, real vs demo

class FakeSession:
    udid = "00008150-MIRROR"
    rsd = None
    info: dict = {}
    closing = False


class RealCtx(backend.Backend):
    """The real daemon's ctx as far as the mirror feature uses it: need_session, session, resources."""
    features = None

    def __init__(self):
        super().__init__(log=lambda *_: None)
        mod = self.registry.module("mirror")
        with mock.patch.object(M.threading, "Thread"):           # no sweep threads over the real cache
            M.register(mod)
        mod.commit()
        self.session = None
        self.fake = FakeSession()

    def need_session(self, a):
        return self.fake


async def code_of(coro) -> str | None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        return protocol.error_json(exc)["code"]
    return None


class Commands(unittest.IsolatedAsyncioTestCase):
    """The same commands against the real feature (engines replaced: no phone, no ffmpeg) and the demo one
    (frames not painted, no thread): same reply keys, same clamps, same error codes."""

    def setUp(self):
        M.load_media()      # Pillow/PyAV imports (~0.3 s once) outside the loop: no slow-callback warning

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mirror-cmd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        async def ensure(m, d, s, holder):           # real: the engine "settles" on screenshots at once
            m.mode = "screenshot"

        def demo_start(m):                           # demo: no frame thread
            m.mode, m.notice = m.pick_mode()
            m.on.set()
            return m
        patches = [
            mock.patch.object(M.Mirror, "ensure", ensure), mock.patch.object(M.Mirror, "running", lambda m: True),
            mock.patch.object(M, "REC_DIR", self.tmp), mock.patch.object(M, "REC_FIRST_WAIT_S", 0),
            mock.patch.object(M, "ffmpeg_path", lambda: "/usr/bin/ffmpeg"), quiet(),
            # (without a broker, real emit() writes to the daemon's private stdout, which a test doesn't have)
            mock.patch.object(M, "emit", lambda *a, **k: None),
            mock.patch.object(MD.Mirror, "start", demo_start),
            mock.patch.object(MD.Frames, "first", lambda f, timeout: None),
            mock.patch.object(MD, "DIR", self.tmp), mock.patch.object(MD, "MEDIA_BUSY", False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        from iosstudio.device.demo.daemon import DemoBackend
        self.real = RealCtx()
        self.demo = DemoBackend(None)
        self.addAsyncCleanup(self.demo.shutdown)
        self.addAsyncCleanup(self.real.shutdown)

    async def both(self, cmd, args=None):
        return (await self.real.dispatch(cmd, dict(args or {})), await self.demo.dispatch(cmd, dict(args or {})))

    async def test_idle_record_status_and_stop(self):
        real, demo = await self.both("record.status")
        self.assertEqual(real, demo)
        self.assertEqual(real, {"state": "idle"})
        for b in (self.real, self.demo):
            self.assertEqual(await code_of(b.dispatch("record.stop", {})), "not-recording")

    async def test_mirror_start_clamps_and_shape(self):
        for args, want in (({"fps": 1000, "scale": 5, "quality": 10}, (30.0, 1.0, 30)),
                           ({"fps": 0, "scale": 0, "quality": 1000}, (0.2, 0.25, 95)),
                           ({"fps": "x", "scale": "y", "quality": "z"}, (1.0, 0.5, 70))):
            real, demo = await self.both("mirror.start", args)
            self.assertEqual(set(real), START_KEYS)
            self.assertEqual(set(demo), START_KEYS)
            for r in (real, demo):
                self.assertEqual((r["fps"], r["scale"], r["quality"]), want, (args, r))
                self.assertIsInstance(r["quality"], int)

    async def test_bad_mode(self):
        for b in (self.real, self.demo):
            self.assertEqual(await code_of(b.dispatch("mirror.start", {"mode": "hdmi"})), "bad-args")

    async def test_ack_with_a_bad_seq_is_bad_args_in_real_mode(self):
        self.assertEqual(await self.real.dispatch("mirror.ack", {"seq": 3}), {})
        self.assertEqual(await code_of(self.real.dispatch("mirror.ack", {"seq": "x"})), "bad-args")

    @unittest.expectedFailure
    async def test_ack_with_a_bad_seq_parity(self):
        # BUG: features/mirror/demo.py mirror_ack does int(a.get("seq")) unguarded: a bad seq is a "failed"
        # (crash) reply in demo mode where the real daemon answers bad-args.
        self.assertEqual(await code_of(self.demo.dispatch("mirror.ack", {"seq": "x"})), "bad-args")

    async def test_status_shape(self):
        real, demo = await self.both("mirror.status")
        self.assertEqual(set(real), set(demo))
        self.assertEqual(set(real), STATE_KEYS | {"available", "viewers", "record"})
        self.assertEqual(set(real["available"]), set(demo["available"]))
        for k in ("stream", "screenshot"):
            self.assertLessEqual({"available", "reason"}, set(real["available"][k]))
            self.assertLessEqual(set(real["available"][k]), set(demo["available"][k]) | {"message"})
        self.assertEqual(real["available"]["screenshot"], {"available": False, "reason": "no-device"})
        for r in (real, demo):
            for k in STATE_KEYS:
                self.assertIn(k, r)
            self.assertIsInstance(r["available"]["ffmpeg"], bool)
            self.assertIsInstance(r["available"]["jpeg"], bool)
            self.assertIsInstance(r["viewers"], int)

    async def test_record_start_clamps_then_empty_stop_fails_the_same_way(self):
        for given, want in ((10_000, 300.0), (0, 1.0), ("abc", 60.0), (None, 60.0)):
            args = {"mode": "screenshot"} if given is None else {"mode": "screenshot", "maxSeconds": given}
            real, demo = await self.both("record.start", args)
            self.assertEqual(set(real), SNAPSHOT_KEYS)
            self.assertEqual(set(demo), SNAPSHOT_KEYS)
            for r in (real, demo):
                self.assertEqual((r["state"], r["mode"], r["maxSeconds"], r["frames"]),
                                 ("recording", "screenshot", want, 0), (given, r))
            for b in (self.real, self.demo):
                self.assertEqual(await code_of(b.dispatch("record.start", {})), "busy")
            for b in (self.real, self.demo):
                self.assertEqual(await code_of(b.dispatch("record.stop", {})), "record-empty")
            real, demo = await self.both("record.status")
            for r in (real, demo):
                self.assertEqual(r["state"], "failed")
                self.assertEqual(r["error"]["code"], "record-empty")
                self.assertLessEqual(SNAPSHOT_KEYS | {"error", "reason"}, set(r))
            self.assertEqual(real["reason"], demo["reason"])
            self.assertEqual(list(self.tmp.glob(".rec-*")), [])        # real work folder removed


if __name__ == "__main__":
    unittest.main()
