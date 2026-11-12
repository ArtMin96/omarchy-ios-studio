"""One hot reload session (`hotreload run`): build, install and launch the app once, then reload each save.

    Session(root, udid=…, preset=…, debounce_ms=…).run()    streams `hotreload` snapshots, ends with `done`

Start (a worker thread; the main loop meanwhile answers stdin, signals and pause/resume):
  prechecks     hot reload on (else not-enabled; an outdated runtime copy is refreshed), xtool and rcodesign
                (missing-tool), an Apple ID (not-signed-in), an iPhone (no-device)
  watch mode    stopped if it watches this project (snapshot `notice`)
  snapshot      buildinfo.snapshot_sources: the watched files (digests, Swift copies) as the build will read them
  build         `build run` as a helper child (history, log, signing records like the Run button), its progress
                forwarded as building / installing; a Wi-Fi build is installed through the device daemon. With a
                prebuild command the snapshot is taken again when the prebuild step is done (before xtool starts):
                the build compiles what the prebuild wrote, so its files aren't saves made during the build
  launch        the installed bundle id (apps.list, free teams' XTL-…), `hotreload.clean`, `app.launch` with the
                console (the runtime's [HotReload] lines show in Logs › Console)
  baseline      buildinfo.capture_baseline from the snapshot (interposable == 0 -> not-interposable), warm_up in
                the background
  runtime       `hotreload.runtime` until runtime.json is from this launch (≤ RUNTIME_WAIT); if it never shows,
                lastError app-not-running and the session keeps watching
A failed build ends the session (stopReason build-failed); any other start failure too (stopReason error).

Then Sources/ is watched (fswatch). Files saved while the app was building (their digest differs from the snapshot)
reload right away (notice `changed-while-building`); a module file the snapshot couldn't hold that changed
meanwhile gets notice `changed-during-build` (press Run). Each settled save (debounce, default 150 ms) makes one
cumulative patch (patch.make_patch(baseline, None, seq)) and pushes it (`hotreload.push`, waits for the ack). Only
one reload runs at a time: saves during a reload are collected and make exactly one more after it. Paused, saves
are only collected (`pending`, `pendingCount`); resume makes one cumulative patch at once. Files back to the build
(`no-changes`) push nothing: "Back to the built version", with a notice to press Run when the app still runs an
earlier reload. Package.swift, xtool.yml or resource changes get a notice (they need Run), and so do Swift files
outside the app module's folders (notice `other-target`: hot reload patches the app target only). A reload's
failure is `failed` with lastError (PatchError.to_json(); ack.ok false -> load-failed with the dlerror text; no ack
-> app-not-running); the session keeps watching. The app gone from the phone (not-installed) ends it (app-exited).

Stop: {"cancel": true} or {"cmd": "stop"} on stdin ({"cmd": "pause" | "resume"} also work), SIGTERM/SIGINT/SIGHUP,
`hotreload stop` (SIGTERM), the parent process ending (checked every step, so `hotreload run` must run under a
process that stays alive; the app and the MCP server do). The build is cancelled (its process group killed after
the grace period), running compiles are killed, the final `stopped` snapshot is written, then `done`; all of it
within SHUTDOWN_BUDGET (state.STOP_WAIT waits longer before it kills the session).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import select
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iosstudio.core import output, subproc
from iosstudio.core.output import HelperError, fix
from iosstudio.features.hotreload import state as rt

DEFAULT_DEBOUNCE_MS = 150
MIN_DEBOUNCE_MS, MAX_DEBOUNCE_MS = 50, 5000
MAX_DELAY = 2.0            # seconds: saves that never pause still reload at least this often
PUSH_WAIT_MS = 5000        # how long hotreload.push waits for the app's ack
RUNTIME_WAIT = 10.0        # seconds after the launch for the runtime's runtime.json
INSTALL_TIMEOUT = 600
REQUEST_TIMEOUT = 60
RELOADED_SHOWN = 1.5       # seconds `reloaded` shows before `watching`
PROGRESS_EVERY = 1.0       # seconds between build progress snapshots
SHUTDOWN_BUDGET = 9.0      # seconds the whole shutdown may take (state.STOP_WAIT, 10 s, covers it)
JOIN_WAIT = 6.0            # of that, for the start / reload threads (a cancelled build gets CANCEL_GRACE, 5 s)
WARM_WAIT = 1.0            # for the warm-up compile (killed on cancel)
CLOSE_WAIT = 2.0           # for the device daemon connection to close
FILES_SHOWN = 5            # pending / history files
RELOAD_FILES = 10          # lastReload files
STEP_TEXT = {"check": "Checking the change", "compile": "Compiling", "link": "Linking", "sign": "Signing",
             "push": "Sending it to your iPhone"}
RUN_FIX = fix("Run again", "projects", action="hot-reload-restart")
DEVICE_FIX = fix("Open iPhone", "device")


def clamp_debounce(v: Any) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise HelperError("usage", "--debounce-ms must be a number of milliseconds.") from None
    return max(MIN_DEBOUNCE_MS, min(MAX_DEBOUNCE_MS, n))


def _name(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def _files_text(files: list[str]) -> str:
    if not files:
        return "your changes"
    return _name(files[0]) + (f" and {len(files) - 1} more" if len(files) > 1 else "")


# ================================================================ scheduling (pure; driven by a clock)

class ReloadScheduler:
    """Debounce, one reload at a time, pause. changed() on saves, tick() says when a reload starts (and marks it
    running), finished() when it ended. Saves during a reload stay pending and make one more reload after it."""

    def __init__(self, debounce: float, max_delay: float = MAX_DELAY):
        self.debounce = debounce
        self.max_delay = max(max_delay, debounce)
        self.files: list[str] = []      # saved since the last reload started
        self.first: float | None = None
        self.due_at: float | None = None
        self.running = False
        self.paused = False

    def changed(self, files: list[str], now: float) -> None:
        for f in files:
            if f not in self.files:
                self.files.append(f)
        if self.first is None:
            self.first = now
        self.due_at = min(now + self.debounce, self.first + self.max_delay)

    def pause(self) -> None:
        self.paused = True

    def resume(self, now: float) -> None:
        self.paused = False
        if self.files:
            self.due_at = now                  # one cumulative patch at once
            self.first = now

    def due_in(self, now: float) -> float | None:
        if not self.files or self.running or self.paused or self.due_at is None:
            return None
        return max(0.0, self.due_at - now)

    def tick(self, now: float) -> list[str] | None:
        if self.due_in(now) != 0.0:
            return None
        files, self.files = self.files, []
        self.first = self.due_at = None
        self.running = True
        return files

    def finished(self) -> None:
        self.running = False


# ================================================================ the device daemon, synchronously

class DeviceLink:
    """One DeviceClient on a private asyncio loop thread; request() blocks with a timeout and a cancel check."""

    def __init__(self, name: str = "hot reload"):
        from iosstudio.device.client import DeviceClient
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="hotreload-device", daemon=True)
        self.thread.start()
        self.client = DeviceClient(name=name, events=["installProgress"])   # the Wi-Fi install's waiting message

    def request(self, cmd: str, args: dict, timeout: float, cancel: Callable[[], bool] | None = None) -> Any:
        args = {k: v for k, v in args.items() if v is not None}
        fut = asyncio.run_coroutine_threadsafe(self.client.request(cmd, args, timeout=timeout), self.loop)
        deadline = time.monotonic() + timeout + 35      # connecting (maybe starting the daemon) comes on top
        while True:
            try:
                return fut.result(0.2)
            except concurrent.futures.TimeoutError:
                if cancel and cancel():
                    fut.cancel()
                    raise subproc.Cancelled() from None
                if time.monotonic() > deadline:
                    fut.cancel()
                    raise HelperError("timeout", "Your iPhone didn't answer in time.", DEVICE_FIX) from None
            except concurrent.futures.CancelledError:
                raise subproc.Cancelled() from None

    def on_event(self, callback: Callable[[str, Any], None], names: list[str]) -> Callable[[], None]:
        """callback(name, data) for these daemon events, on the link's loop thread. Returns the unsubscribe."""
        return self.client.on_event(callback, names)

    def close(self, timeout: float = CLOSE_WAIT) -> None:
        end = time.monotonic() + timeout
        try:
            asyncio.run_coroutine_threadsafe(self.client.close(), self.loop).result(timeout)
        except Exception:  # noqa: BLE001 - closing never fails the session
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(max(0.1, end - time.monotonic()))


# ================================================================ one reload's result

@dataclass
class Outcome:
    seq: int
    files: list[str]
    ok: bool = False
    unchanged: bool = False      # the files are back to the build (no-changes): nothing pushed
    cancelled: bool = False
    gone: bool = False           # the app isn't installed anymore
    error: dict | None = None    # lastError
    reload: dict | None = None   # lastReload

    def summary(self) -> dict:
        """The history entry."""
        out: dict[str, Any] = {"seq": self.seq, "ok": self.ok, "time": time.time(), "files": self.files[:FILES_SHOWN]}
        if self.reload:
            out.update(ms=self.reload.get("ms"), swapped=self.reload.get("swapped"))
        if self.unchanged:
            out["unchanged"] = True
        if self.error:
            out.update(code=self.error.get("code"), message=str(self.error.get("message") or "")[:200])
        return out


class StartFailed(Exception):
    def __init__(self, reason: str, error: dict):
        super().__init__(error.get("message"))
        self.reason = reason
        self.error = error


# ================================================================ prechecks

def prechecks(root: Path, udid: str | None) -> tuple[str, list[dict]]:
    """(the iPhone's udid, notices) or HelperError not-enabled / missing-tool / not-signed-in / no-device."""
    from iosstudio.features.hotreload import project_setup, signing
    from iosstudio.features.projects.build import auth_ok, xtool_path
    from iosstudio.features.watch.builds import device_for_run
    notices: list[dict] = []
    st = project_setup.status(root)
    if not st["enabled"]:
        raise HelperError("not-enabled", "Hot reload is off for this project. Turn it on first (it adds a few lines "
                          "to Package.swift that iOS Studio manages).",
                          fix("Turn on hot reload", "projects", action="hot-reload-enable"))
    if not st["current"] and project_setup.install_runtime(root, "full"):
        notices.append({"id": "runtime-updated", "level": "info",
                        "message": "Updated the hot reload runtime in .iosstudio/hot-reload."})
    if not xtool_path():
        raise HelperError("missing-tool", "xtool, the tool that builds iPhone apps on Linux, isn't installed yet.",
                          fix("Install xtool", "setup", step="xtool"))
    if not signing.rcodesign_path():
        raise HelperError("missing-tool", "rcodesign, which signs each reload for your iPhone, isn't installed yet.",
                          fix("Install rcodesign", "setup", step="rcodesign"))
    if not auth_ok():
        raise HelperError("not-signed-in", "Sign in with your Apple ID so iOS Studio can sign the app and its reloads.",
                          fix("Sign in", "account", action="signin"))
    if not udid:
        udid, why = device_for_run()
        if not udid:
            raise HelperError("no-device", f"Connect your iPhone first: {why}.", DEVICE_FIX)
    return udid, notices


def stop_watch_mode(root: Path) -> dict | None:
    """Stop watch mode when it watches this project (its auto-run would reinstall the app); a notice if it did."""
    from iosstudio.features.watch import lifecycle
    try:
        res = lifecycle.stop(str(root))
    except HelperError:
        return None
    if not res.get("stopped"):
        return None
    return {"id": "watch-stopped", "level": "info",
            "message": "Watch mode was stopped for this project: hot reload rebuilds on save instead."}


def match_installed(apps: Any, want: str) -> str | None:
    from iosstudio.features.editor_debug_bridge import match_installed as match
    if isinstance(apps, dict):
        apps = apps.get("apps")
    return match(apps if isinstance(apps, list) else [], want)


# ================================================================ the session

class Session:
    """See the module doc. Everything with side effects is a parameter, so tests swap in fakes:
    device (.request(cmd, args, timeout, cancel) / .close()), build_factory(extra, build_id) -> HelperBuild,
    patcher = patch.make_patch, baseliner = buildinfo.capture_baseline(root, module, snapshot=, cancel=),
    snapshotter = buildinfo.snapshot_sources, warmer = patch.warm_up, watcher_factory = fswatch.make_watcher,
    checks = prechecks, stop_watch = stop_watch_mode."""

    def __init__(self, root: Path, *, udid: str | None = None, preset: str | None = None,
                 debounce_ms: int = DEFAULT_DEBOUNCE_MS, device: Any = None,
                 build_factory: Callable[[list[str], str], Any] | None = None, patcher: Callable | None = None,
                 baseliner: Callable | None = None, snapshotter: Callable | None = None,
                 warmer: Callable | None = None,
                 watcher_factory: Callable | None = None, checks: Callable | None = None,
                 stop_watch: Callable | None = None, clock: Callable[[], float] = time.monotonic,
                 write: Callable[[dict], None] = output._write, check_parent: bool = True):
        from iosstudio.features.watch.fswatch import ContentFilter
        self.root = root
        self.key = rt.key(root)
        self.udid = udid
        self.preset = preset
        self.debounce_ms = debounce_ms
        self._device = device
        self.build_factory = build_factory or self._helper_build
        self.patcher = patcher
        self.baseliner = baseliner
        self.snapshotter = snapshotter
        self.warmer = warmer
        self.watcher_factory = watcher_factory
        self.checks = checks or prechecks
        self.stop_watch = stop_watch or stop_watch_mode
        self.clock = clock
        self.write = write
        self.check_parent = check_parent
        self.sched = ReloadScheduler(debounce_ms / 1000.0)
        self.filter = ContentFilter(root)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.snap: dict[str, Any] = {}
        self.seq = 0                    # snapshot counter
        self.patch_seq = 0              # the last reload's seq (continues the previous session's)
        self.history: list[dict] = []
        self.reloads = 0                # applied reloads
        self.swapped_any = False        # the app runs code of a reload (a Run brings back the build)
        self.baseline: Any = None
        self.snapshot: Any = None       # buildinfo.SourceSnapshot from before the build (until the baseline has it)
        self.built_digests: dict | None = None   # the snapshot's digests: what the build read
        self.late: list[str] = []       # module files copied after the build that changed while it ran
        self.app_roots: list[str] | None = None  # project-relative folders of the app module ("Sources/App/")
        self.bundle_id: str | None = None
        self.build: Any = None
        self.watcher: Any = None
        self.ready = False              # started: watching for saves
        self.start_thread: threading.Thread | None = None
        self.start_failure: StartFailed | None = None
        self.start_done = threading.Event()
        self.worker: threading.Thread | None = None
        self.outcome: Outcome | None = None
        self.warm_thread: threading.Thread | None = None
        self.reloaded_at: float | None = None
        self.last_progress = 0.0
        self.stop_reason: str | None = None
        self.stop_message: str | None = None
        self.stop_request: tuple[str, str] | None = None
        self.control = False
        self.ppid = os.getppid()
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)

    # ---------------------------------------------------------------- plumbing
    @property
    def device(self) -> Any:
        if self._device is None:
            self._device = DeviceLink()
        return self._device

    def wake(self) -> None:
        if self.wake_w < 0:
            return
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass

    def cancelled(self) -> bool:
        return self.stopping.is_set()

    def request(self, cmd: str, args: dict, timeout: float = REQUEST_TIMEOUT) -> Any:
        return self.device.request(cmd, {**args, "udid": self.udid}, timeout, self.cancelled)

    def idle_state(self) -> str:
        return "paused" if self.sched.paused else "watching"

    def idle_message(self) -> str:
        if self.sched.paused:
            n = len(self.sched.files)
            return f"Paused: {n} saved file{'s' if n != 1 else ''} wait for resume" if n else \
                "Paused: saves wait until you resume"
        return "Watching for saves"

    def publish(self, state: str | None = None, message: str | None = None, **fields: Any) -> dict:
        """A new snapshot: the `hotreload` event on stdout and the state file (with history)."""
        with self.lock:
            self.seq += 1
            if state:
                self.snap["state"] = state
            if message is not None:
                self.snap["message"] = message
            self.snap.update(fields)
            self.snap.update(event="hotreload", seq=self.seq, updatedAt=time.time(), reloads=self.reloads,
                             paused=self.sched.paused, pending=self.sched.files[:FILES_SHOWN],
                             pendingCount=len(self.sched.files), patchSeq=self.patch_seq,
                             bundleId=self.bundle_id)
            snap = dict(self.snap)
            try:
                rt.write_state(self.key, {**snap, "history": self.history})
            except OSError:
                pass
            self.write(snap)
            return snap

    def notice(self, n: dict | None) -> None:
        if n:
            self.publish(notice={**n, "time": time.time()})

    # ---------------------------------------------------------------- run
    def run(self) -> int:
        """The whole session in this process (the `hotreload run` command). Returns the exit code."""
        try:
            lock = rt.acquire(self.key, str(self.root))
        except HelperError as exc:
            return output.done(False, error=exc)
        try:
            self._signals()
            prev = rt.read_state(self.key) or {}
            self.patch_seq = int(prev.get("patchSeq") or 0)
            rt.clear_control(self.key)
            self.snap = {"event": "hotreload", "seq": 0, "state": "starting", "message": "", "project": self.root.name, "path": str(self.root), "pid": os.getpid(),
                         "startedAt": time.time(), "udid": self.udid, "debounceMs": self.debounce_ms,
                         "notice": None, "progress": None, "lastReload": None, "lastError": None,
                         "stopReason": None}
            self.publish("starting", "Starting hot reload")
            threading.Thread(target=self._stdin, name="hotreload-stdin", daemon=True).start()
            self.start_thread = threading.Thread(target=self._start_thread, name="hotreload-start", daemon=True)
            self.start_thread.start()
            while not self.stop_reason:
                self.step()
        finally:
            self.shutdown()
            rt.release(lock)
        return self.finish()

    def _signals(self) -> None:
        def on_stop(_signum, _frame):
            self.stop_request = ("stopped", "Stopped")

        def on_control(_signum, _frame):
            self.control = True

        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, on_stop)
        signal.signal(signal.SIGUSR1, on_control)
        signal.set_wakeup_fd(self.wake_w, warn_on_full_buffer=False)

    def _stdin(self) -> None:
        """{"cancel": true} / {"cmd": "stop"} stop; {"cmd": "pause" | "resume"} toggle. EOF only ends reading."""
        try:
            for line in sys.stdin:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("cancel") or msg.get("cmd") == "stop":
                    self.stop_request = ("stopped", "Stopped")
                elif msg.get("cmd") in ("pause", "resume"):
                    rt.write_control(self.key, str(self.root), msg["cmd"] == "pause")
                    self.control = True
                else:
                    continue
                self.wake()
        except (OSError, ValueError):
            pass

    # ---------------------------------------------------------------- the main loop
    def step(self, timeout: float = 0.5) -> None:
        """One turn of the loop (tests call it directly)."""
        now = self.clock()
        if self.ready and not self.worker:
            d = self.sched.due_in(now)
            if d is not None:
                timeout = min(timeout, d)
        if self.reloaded_at is not None:
            timeout = min(timeout, max(0.0, self.reloaded_at + RELOADED_SHOWN - now))
        fds = [self.wake_r]
        wfd = self.watcher.fileno() if self.watcher else None
        if wfd is not None:
            fds.append(wfd)
        try:
            select.select(fds, [], [], max(0.0, timeout))
        except (InterruptedError, OSError, ValueError):
            pass
        try:
            while os.read(self.wake_r, 512):
                pass
        except (BlockingIOError, OSError):
            pass
        if self.stop_request:
            self.stop_reason, self.stop_message = self.stop_request
            return
        if self.control:
            self.control = False
            self.apply_control()
        if self.start_thread and self.start_done.is_set():
            self.start_thread.join(1)
            self.start_thread = None
            self.on_started()
            if self.stop_reason:
                return
        if self.ready:
            changes = self.poll_changes()
            if changes:
                self.on_changes(changes)
        if self.worker and (self.outcome is not None or not self.worker.is_alive()):
            self.worker.join(1)
            self.worker = None
            if self.outcome:
                self.on_reload_done(self.outcome)
            if self.stop_reason:
                return
        if self.ready and not self.worker:
            files = self.sched.tick(self.clock())
            if files is not None:
                self.start_reload(files)
        if (self.reloaded_at is not None and not self.worker
                and self.clock() - self.reloaded_at >= RELOADED_SHOWN):
            self.reloaded_at = None
            if self.snap.get("state") == "reloaded":
                self.publish(self.idle_state(), self.idle_message())
        if self.check_parent and os.getppid() != self.ppid:
            self.stop_reason = "stopped"
            self.stop_message = "Stopped: the app or tool that started hot reload has closed"

    def apply_control(self) -> None:
        c = rt.read_control(self.key)
        if c.get("path") not in (None, str(self.root)) or "paused" not in c:
            return
        want = bool(c["paused"])
        if want == self.sched.paused:
            self.publish()                   # the caller waits for a snapshot either way
            return
        idle = self.ready and not self.worker
        if want:
            self.sched.pause()
            busy = "Paused: this reload finishes, then saves wait until you resume" if self.worker else None
            self.publish(self.idle_state() if idle else None, self.idle_message() if idle else busy)
        else:
            n = len(self.sched.files)
            self.sched.resume(self.clock())
            msg = f"Resumed: reloading {_files_text(self.sched.files)}" if n else "Watching for saves"
            self.publish(self.idle_state() if idle else None, msg if idle else ("Resumed" if self.worker else None))

    def poll_changes(self) -> list[str]:
        raw = self.watcher.read(self.clock()) if self.watcher else []
        return self.filter.changed(raw) if raw else []

    def in_app(self, rel: str) -> bool:
        """rel is in (or a folder above) the app module's folders; True while they aren't known."""
        if self.app_roots is None:
            return True
        return any(not r or rel.startswith(r) or r.startswith(rel.rstrip("/") + "/") for r in self.app_roots)

    def on_changes(self, rels: list[str], during_build: bool = False) -> None:
        code = [r for r in rels if r.startswith("Sources/") and (r.endswith(".swift") or "." not in _name(r))]
        other = [r for r in rels if r not in code]
        elsewhere = [r for r in code if not self.in_app(r)]
        code = [r for r in code if r not in elsewhere]
        if other:
            self.notice({"id": "needs-run", "level": "warn", "fix": RUN_FIX,
                         "message": f"{_name(other[0])} changed. Hot reload swaps Swift code only: press Run again "
                                    "to use this change."})
        if elsewhere:
            parts = elsewhere[0].split("/")
            target = parts[1] if len(parts) > 2 else _name(elsewhere[0])
            self.notice({"id": "other-target", "level": "warn", "fix": RUN_FIX, "file": elsewhere[0],
                         "message": f"Hot reload only updates the app target. Changes in {target} need a full Run."})
        if not code:
            return
        if during_build:
            self.notice({"id": "changed-while-building", "level": "info",
                         "message": f"{_files_text(code)} changed while the app was building: reloading it now."})
        self.sched.changed(code, self.clock())
        if self.sched.paused:
            self.publish(message=self.idle_message())
        elif self.worker:
            self.publish(message=f"Saved again: {_files_text(self.sched.files)} reloads after this one")
        else:
            self.publish()

    # ---------------------------------------------------------------- start
    def _start_thread(self) -> None:
        try:
            self.start()
        except subproc.Cancelled:
            pass
        except StartFailed as exc:
            self.start_failure = exc
        except HelperError as exc:
            self.start_failure = StartFailed("error", exc.to_json())
        except Exception as exc:  # noqa: BLE001 - reported as the session's error, never a traceback on stdout
            self.start_failure = StartFailed("error", {"code": "internal", "message": "Hot reload couldn't start.",
                                                       "detail": repr(exc)[:2000]})
        finally:
            self.start_done.set()
            self.wake()

    def start(self) -> None:
        """Everything before watching (the module doc, "Start")."""
        self.udid, notices = self.checks(self.root, self.udid)
        self.publish(udid=self.udid)
        for n in notices:
            self.notice(n)
        self.notice(self.stop_watch(self.root))
        self._prune()
        self.snapshot = self.take_snapshot()      # before the build: what it compiles (see the module doc)
        try:
            data = self.run_build()
            if data.get("needsInstall") and data.get("ipa"):
                self.install(data)
            self.bundle_id = self.installed_id(data)
            try:
                self.request("hotreload.clean", {"bundleId": self.bundle_id}, 30)
            except HelperError:
                pass                              # not installed yet / no folder: nothing to clean
            pid = self.launch(data)
            self.publish("launching", "Getting hot reload ready", progress=None)
            self.baseline = self.capture()
        finally:
            from iosstudio.features.hotreload import buildinfo
            if self.snapshot is not None:
                self.built_digests = dict(self.snapshot.digests)
                buildinfo.discard_snapshot(self.snapshot)    # a capture took its folder already
                self.snapshot = None
        self.late = list(getattr(self.baseline, "late", None) or [])
        self.warm_thread = threading.Thread(target=self._warm, name="hotreload-warm", daemon=True)
        self.warm_thread.start()
        self.confirm_runtime(pid)

    def _prune(self) -> None:
        from iosstudio.features.hotreload import buildinfo
        try:
            rt.prune()
            buildinfo.prune_cache(protect=buildinfo.project_cache(self.root))
        except OSError:
            pass

    def take_snapshot(self) -> Any:
        from iosstudio.features.hotreload import buildinfo
        try:
            return (self.snapshotter or buildinfo.snapshot_sources)(self.root)
        except OSError:
            return None                       # no cache folder: the baseline copies the live files instead

    def resnapshot(self) -> None:
        """The prebuild is done and the compile hasn't started: what it generated or rewrote (Sources/, xtool.yml,
        a plist) is what the build reads, so the snapshot is taken again (kept as it was if that fails)."""
        from iosstudio.features.hotreload import buildinfo
        if self.snapshot is None:
            return
        new = self.take_snapshot()
        if new is None:
            return
        old, self.snapshot = self.snapshot, new
        buildinfo.discard_snapshot(old)

    def _helper_build(self, extra: list[str], build_id: str) -> Any:
        from iosstudio.features.watch.builds import HelperBuild
        return HelperBuild("run", str(self.root), build_id, extra)

    def run_build(self) -> dict:
        """`build run` as a helper child; its progress as building / installing. Returns its done data."""
        from iosstudio.features.watch.builds import _selected_preset, short_diags
        extra = ["--udid", str(self.udid), "--trigger", "hot-reload"]
        preset = self.preset or _selected_preset(self.root)
        if preset:
            extra += ["--preset", preset]
        build_id = f"hr-{int(time.time())}"
        self.publish("building", "Building the app", buildId=build_id, progress={"fraction": 0.0,
                                                                                 "detail": "Starting the build"})
        if self.cancelled():
            raise subproc.Cancelled()
        b = self.build = self.build_factory(extra, build_id)
        try:
            while not b.finished():
                if self.cancelled():
                    b.cancel()
                fd = b.fileno()
                if fd is not None:
                    try:
                        select.select([fd], [], [], 0.25)
                    except (InterruptedError, ValueError, OSError):
                        pass
                else:
                    time.sleep(0.1)
                for ev in b.read_events():
                    self.on_build_event(ev)
                b.enforce()
            for ev in b.read_events():
                self.on_build_event(ev)
            done = b.result()
        finally:
            getattr(b, "close", lambda: None)()
            self.build = None
        err = done.get("error") or {}
        if self.cancelled() or (not done.get("ok") and err.get("code") == "cancelled"):
            raise subproc.Cancelled()
        if not done.get("ok"):
            build = err.get("build") or {}
            last = {k: v for k, v in {"code": err.get("code") or "build-failed",
                                      "message": str(err.get("message") or "The build failed.")[:400],
                                      "fix": err.get("fix"), "logFile": build.get("logFile"),
                                      "diagnostics": short_diags(build.get("diagnostics") or [])}.items() if v}
            raise StartFailed("build-failed", last)
        return done.get("data") or {}

    def on_build_event(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "step" and ev.get("id") == "prebuild" and ev.get("state") == "done":
            self.resnapshot()
        if kind == "build" and ev.get("logFile"):
            self.publish(logFile=ev["logFile"])
        elif kind == "step" and ev.get("state") == "running":
            sid = ev.get("id")
            if sid == "install":
                self.publish("installing", "Installing on your iPhone")
            elif sid in ("sign", "package"):
                self.publish("building", "Signing the app")
        elif kind == "progress" and time.monotonic() - self.last_progress >= PROGRESS_EVERY:
            self.last_progress = time.monotonic()
            self.publish(progress={"fraction": ev.get("fraction"), "detail": str(ev.get("detail") or "")[:200]})

    def install(self, data: dict) -> None:
        """A Wi-Fi build: the device daemon installs the .ipa (like the Run button does). Its installProgress
        `message` (the installer waits for the phone or the app to close) replaces the bar's text meanwhile."""
        ipa, base = data["ipa"], "Installing on your iPhone over Wi-Fi"
        self.publish("installing", base, progress=None)
        gate, live = threading.Lock(), [True]

        def on_progress(_name: str, ev: Any) -> None:
            if not isinstance(ev, dict) or ev.get("path") != ipa or ev.get("status") == "done":
                return
            text = str(ev.get("message") or "")[:200] or base
            with gate:
                if live[0] and text != self.snap.get("message"):
                    self.publish(message=text)

        on_event = getattr(self.device, "on_event", None)
        off = on_event(on_progress, ["installProgress"]) if on_event else (lambda: None)
        try:
            self.request("app.install", {"path": ipa}, INSTALL_TIMEOUT)
        except HelperError as exc:
            raise StartFailed("error", {**exc.to_json(), "message": "Built and signed, but installing failed: "
                                        + exc.message}) from None
        finally:
            with gate:
                live[0] = False
            off()
        try:
            from iosstudio.features.projects.store import record_signing
            record_signing(str(self.root), self.udid, data.get("bundleId"), "wifi", data.get("profile"))
        except Exception:  # noqa: BLE001 - bookkeeping only
            pass

    def installed_id(self, data: dict) -> str:
        want = str((data.get("launch") or {}).get("bundleId") or data.get("bundleId") or "")
        if not want:
            raise StartFailed("error", {"code": "no-bundle-id", "message": "This project has no bundle ID (xtool.yml "
                                        "sets it).", "fix": fix("Open Projects", "projects")})
        apps = self.request("apps.list", {"icons": False, "sizes": False})
        found = match_installed(apps, want)
        if not found:
            raise StartFailed("error", {"code": "not-installed", "message": "The app was built, but it isn't on "
                                        "your iPhone. Press Run again.", "fix": RUN_FIX})
        return found

    def launch(self, data: dict) -> int | None:
        launch = data.get("launch") or {}
        self.publish("launching", "Opening the app on your iPhone", progress=None)
        args: dict[str, Any] = {"bundleId": self.bundle_id, "console": True}
        if launch.get("args"):
            args["args"] = launch["args"]
        if launch.get("env"):
            args["env"] = launch["env"]
        try:
            reply = self.request("app.launch", args)
        except HelperError as exc:
            self.publish(lastError={"code": "app-not-running", "message": "Installed, but the app didn't open: "
                                    f"{exc.message} Open it on your iPhone; saves reload once it runs.",
                                    "fix": RUN_FIX, "time": time.time()})
            return None
        pid = reply.get("pid") if isinstance(reply, dict) else None
        return int(pid) if isinstance(pid, int) else None

    def capture(self) -> Any:
        from iosstudio.features.hotreload import buildinfo, project_setup
        try:
            module = project_setup.block_target(project_setup.read_manifest(self.root))
        except (OSError, HelperError):
            module = None
        if not module:
            from iosstudio.features.projects.model import project_info
            module = project_info(self.root)["product"]
        base = (self.baseliner or buildinfo.capture_baseline)(self.root, module, snapshot=self.snapshot,
                                                               cancel=self.cancelled)
        if not base.interposable:
            raise StartFailed("error", {"code": "not-interposable", "fix": RUN_FIX,
                                        "message": "The app was built without hot reload, so its functions can't be "
                                                   "swapped. Run it again with hot reload on."})
        return base

    def _warm(self) -> None:
        from iosstudio.features.hotreload import patch
        try:
            (self.warmer or patch.warm_up)(self.baseline, cancel=self.cancelled)
        except Exception:  # noqa: BLE001 - warming is an optimisation; the first patch pays otherwise
            pass

    def confirm_runtime(self, pid: int | None) -> None:
        """Wait (≤ RUNTIME_WAIT) for runtime.json from this launch; if it never shows, say so and keep going."""
        deadline = time.monotonic() + (RUNTIME_WAIT if pid else 2.0)
        present = False
        while not self.cancelled():
            try:
                reply = self.request("hotreload.runtime", {"bundleId": self.bundle_id}, 20) or {}
            except HelperError:
                reply = {}
            present = bool(reply.get("present"))
            rpid = (reply.get("runtime") or {}).get("pid")
            if present and (pid is None or rpid == pid):
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if present or self.cancelled():
            return
        self.publish(lastError={"code": "app-not-running", "fix": RUN_FIX, "time": time.time(),
                                "message": "The app on your iPhone isn't running with hot reload. Open it there; if "
                                           "it's open, press Run again so the hot reload build is installed."})

    def on_started(self) -> None:
        if self.start_failure:
            f = self.start_failure
            self.stop_reason = f.reason
            self.stop_message = ("Stopped: the build failed" if f.reason == "build-failed" else
                                 f"Stopped: {f.error.get('message') or 'hot reload could not start'}")
            self.publish(lastError={**f.error, "time": time.time()})
            return
        if self.cancelled() or self.stop_reason:
            return
        if self.watcher is None:
            if self.watcher_factory is None:
                from iosstudio.features.watch.fswatch import make_watcher
                self.watcher_factory = make_watcher
            self.watcher, why = self.watcher_factory(self.root)
            if why and why != "forced":
                self.notice({"id": "polling", "level": "info", "message": f"Checking files every second: {why}."})
        self.filter.prime()
        self.app_roots = self.module_folders()
        self.ready = True
        self.publish(self.idle_state(), self.idle_message(), progress=None,
                     mode=getattr(self.watcher, "mode", None))
        self.catch_up()

    def module_folders(self) -> list[str] | None:
        """The app module's folders, project-relative with a trailing slash ("" for the project itself)."""
        if not hasattr(self.baseline, "frontend") or not hasattr(self.baseline, "project"):
            return None
        from iosstudio.features.hotreload import patch
        proj = Path(self.baseline.project)
        out = []
        for root in patch.module_roots(self.baseline):
            try:
                rel = root.relative_to(proj).as_posix()
            except ValueError:
                continue
            out.append("" if rel == "." else rel + "/")
        return out or None

    def catch_up(self) -> None:
        """Saves made while the app was building: the watcher never saw them, so compare the files now with what
        the build read (the snapshot's digests) and reload what differs."""
        if self.built_digests is not None:
            before, now = self.built_digests, self.filter.known
            changed = sorted(r for r in set(before) | set(now) if before.get(r) != now.get(r) and r not in self.late)
            if changed:
                self.on_changes(changed, during_build=True)
        if self.late:                         # copied after the build: a patch can't tell what the build read
            self.notice({"id": "changed-during-build", "level": "warn", "fix": RUN_FIX, "file": self.late[0],
                         "message": f"{_files_text(self.late)} changed while building. Press Run to include it."})

    # ---------------------------------------------------------------- reloads
    def start_reload(self, files: list[str]) -> None:
        self.patch_seq += 1
        seq = self.patch_seq
        self.outcome = None
        self.reloaded_at = None
        self.publish("reloading", f"Reloading {_files_text(files)}", progress={"step": "check",
                                                                              "detail": STEP_TEXT["check"]})

        def work() -> None:
            try:
                self.outcome = self.reload_once(seq, files)
            except Exception as exc:  # noqa: BLE001 - one failed reload never takes the session down
                self.outcome = Outcome(seq, files, error={"code": "internal", "message": "The reload failed.",
                                                          "detail": repr(exc)[:2000]})
            finally:
                self.wake()

        self.worker = threading.Thread(target=work, name=f"hotreload-{seq}", daemon=True)
        self.worker.start()

    def on_step(self, name: str) -> None:
        self.publish(progress={"step": name, "detail": STEP_TEXT.get(name, name)})

    def reload_once(self, seq: int, files: list[str]) -> Outcome:
        """make_patch -> hotreload.push -> the app's ack (runs in the worker thread)."""
        from iosstudio.features.hotreload import patch
        t0 = time.monotonic()
        out = Outcome(seq, files)
        try:
            res = (self.patcher or patch.make_patch)(self.baseline, None, seq, cancel=self.cancelled,
                                                     on_step=self.on_step)
        except subproc.Cancelled:
            out.cancelled = True
            return out
        except HelperError as exc:
            if exc.code == "no-changes":
                out.ok = out.unchanged = True
            else:
                out.error = exc.to_json()
            return out
        out.files = list(res.files)
        self.on_step("push")
        try:
            push = {"bundleId": self.bundle_id, "src": os.path.realpath(res.dylib), "seq": seq, "waitMs": PUSH_WAIT_MS}
            if getattr(self.baseline, "key", None):
                push["key"] = self.baseline.key       # the daemon checks the patch is in this project's store
            reply = self.request("hotreload.push", push, PUSH_WAIT_MS / 1000 + 60)
        except subproc.Cancelled:
            out.cancelled = True
            return out
        except HelperError as exc:
            out.gone = exc.code == "not-installed"
            out.error = exc.to_json()
            return out
        reply = reply if isinstance(reply, dict) else {}
        ack = reply.get("ack") if isinstance(reply.get("ack"), dict) else None
        if not reply.get("acked") or not ack:
            out.error = {"code": "app-not-running", "fix": RUN_FIX,
                         "message": "Your app didn't take the change: it isn't running on your iPhone. Open it, then "
                                    "save again (each reload has all your changes)."}
            return out
        if not ack.get("ok"):
            why = str(ack.get("error") or "unknown error")
            out.error = {"code": "load-failed", "fix": RUN_FIX, "detail": why[:2000],
                         "message": f"The app couldn't load the change: {why[:300]}"}
            return out
        steps = {**res.steps, "push": int(reply.get("pushMs") or 0) + int(reply.get("ackMs") or 0),
                 "apply": round(float(ack.get("ms") or 0), 1)}
        out.ok = True
        out.reload = {"seq": seq, "ok": True, "files": out.files[:RELOAD_FILES], "fileCount": len(out.files),
                      "ms": int(round((time.monotonic() - t0) * 1000)), "steps": steps,
                      "swapped": int(ack.get("swapped") or 0), "reverted": int(ack.get("reverted") or 0),
                      "vtable": int(ack.get("vtable") or 0), "notSwapped": len(getattr(res, "missing", []) or []),
                      "time": time.time()}
        return out

    def on_reload_done(self, out: Outcome) -> None:
        self.sched.finished()
        if out.cancelled:
            return
        self.history = (self.history + [out.summary()])[-rt.HISTORY_MAX:]
        if out.gone:
            self.stop_reason = "app-exited"
            self.stop_message = "Stopped: the app isn't on your iPhone anymore"
            self.publish(lastError={**(out.error or {}), "seq": out.seq, "time": time.time()}, progress=None)
            return
        if out.unchanged:
            if self.swapped_any:
                self.notice({"id": "back-to-build", "level": "info", "fix": RUN_FIX,
                             "message": "Your files match the build again, but the app still runs the last reload. "
                                        "Press Run to start it fresh."})
            self.publish(self.idle_state(), "Back to the built version", lastError=None, progress=None)
            return
        if out.error:
            self.publish("failed", str(out.error.get("message") or "The reload failed."),
                         lastError={**out.error, "seq": out.seq, "time": time.time()}, progress=None)
            return
        r = out.reload or {}
        self.reloads += 1
        if r.get("swapped") or r.get("vtable"):
            self.swapped_any = True
        msg = f"Reloaded {_files_text(out.files)} in {r.get('ms', 0) / 1000:.1f} s"
        self.reloaded_at = self.clock()
        self.publish("reloaded", msg, lastReload=r, lastError=None, progress=None)

    # ---------------------------------------------------------------- the end
    def shutdown(self) -> None:
        if self.stopping.is_set() and self.wake_r < 0:
            return                            # already shut down
        self.stopping.set()
        self.ready = False
        t0 = time.monotonic()
        if self.build:
            self.build.cancel()
        for t in (self.start_thread, self.worker):
            if t:
                t.join(max(0.0, t0 + JOIN_WAIT - time.monotonic()))
        if self.build:
            self.build.kill()
            self.build = None
        left = lambda cap: max(0.0, min(cap, t0 + SHUTDOWN_BUDGET - time.monotonic()))  # noqa: E731
        if self.warm_thread:
            self.warm_thread.join(left(WARM_WAIT))
        if self.watcher:
            self.watcher.close()
            self.watcher = None
        if self._device is not None:
            try:
                close = self._device.close
                if isinstance(self._device, DeviceLink):
                    close(max(0.5, left(CLOSE_WAIT)))
                else:
                    close()
            except Exception:  # noqa: BLE001
                pass
        reason = self.stop_reason or "stopped"
        self.sched.files.clear()
        self.publish("stopped", self.stop_message or "Stopped", stopReason=reason, stoppedAt=time.time(),
                     progress=None)
        rt.clear_control(self.key)
        try:
            signal.set_wakeup_fd(-1)
        except ValueError:                    # not the main thread (tests)
            pass
        for fd in (self.wake_r, self.wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self.wake_r = self.wake_w = -1

    def finish(self) -> int:
        """The final `done`: ok with the last snapshot, or (a start failure, an error) not ok with lastError."""
        final = {**self.snap, "history": self.history}
        reason = final.get("stopReason")
        if reason in ("stopped", "app-exited"):
            return output.done(True, final)
        err = final.get("lastError") or {"code": "failed", "message": final.get("message") or "Hot reload stopped."}
        output.event("done", ok=False, error={k: v for k, v in err.items() if k != "time"}, data=final)
        return 1
