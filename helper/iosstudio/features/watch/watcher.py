"""The watcher process: file changes -> Scheduler (debounce/supersede) -> builds, snapshots to watch.json/events."""
from __future__ import annotations

import json
import os
import re
import select
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from iosstudio.core import fileio

from .builds import HelperBuild, RunJob, device_for_run, summarize
from .fswatch import ContentFilter, FileWatcher, make_watcher
from .runtime import (
    CANCEL_GRACE,
    DEFAULT_DEBOUNCE_MS,
    MAX_DELAY,
    clamp_debounce,
    control_path,
    events_path,
    live_owners,
    owners_path,
    runtime_dir,
    save_json,
    status_path,
)

OWNER_CHECK = 1.0        # seconds between owner liveness checks
BUSY_RETRY = 2.0         # another build of the project holds its lock: try again after this
CHANGED_SHOWN = 5
SUPERSEDE_LIMIT = 2      # after this many builds in a row were cancelled by saves, the next one is let finish
EVENTS_MAX = 1_000_000   # bytes; the events file is cut back to the latest snapshot beyond this
PROGRESS_EVERY = 1.0     # seconds between progress snapshots


# ================================================================ scheduling (pure; driven by a clock)

class Scheduler:
    """Debounce and supersede, without I/O. Call changed() on file changes, tick() to learn whether a build
    should start now, finished() when it ended. Never two builds at once: a change during a build asks for
    it to be cancelled (changed() returns True) and one more build runs after it."""

    def __init__(self, debounce: float, max_delay: float = MAX_DELAY, supersede_limit: int = SUPERSEDE_LIMIT):
        self.debounce = debounce
        self.supersede_limit = supersede_limit
        self.streak = 0          # builds cancelled by saves in a row
        self.protected = False   # the running build finishes whatever is saved meanwhile
        self.max_delay = max(max_delay, debounce)
        self.pending = False
        self.first: float | None = None
        self.due_at: float | None = None
        self.building = False
        self.cancelling = False
        self.files: list[str] = []
        self.superseded = 0

    def changed(self, files: Iterable[str], now: float) -> bool:
        for f in files:
            if f not in self.files:
                self.files.append(f)
        if not self.pending:
            self.pending = True
            self.first = now
        first = self.first if self.first is not None else now
        self.due_at = min(now + self.debounce, first + self.max_delay)
        if self.building and not self.cancelling and not self.protected:
            self.cancelling = True
            self.superseded += 1
            return True
        return False

    def request(self, now: float, delay: float = 0.0) -> None:
        """Ask for a build without a file change (the first build, a retry after `busy`)."""
        if not self.pending:
            self.pending = True
            self.first = now
        self.due_at = now + delay if self.due_at is None else min(self.due_at, now + delay)

    def due_in(self, now: float) -> float | None:
        if not self.pending or self.building or self.due_at is None:
            return None
        return max(0.0, self.due_at - now)

    def tick(self, now: float) -> list[str] | None:
        """The changed files when a build should start now (and it is marked running), else None."""
        if not self.pending or self.building or self.due_at is None or now < self.due_at:
            return None
        files, self.files = self.files, []
        self.pending, self.first, self.due_at = False, None, None
        self.building = True
        # Saves that keep coming faster than a build takes would cancel every build: after
        # supersede_limit cancelled builds in a row, this one finishes, so there's always a result.
        self.protected = self.streak >= self.supersede_limit
        return files

    def finished(self, superseded: bool | None = None) -> None:
        """The build ended; superseded: it was cancelled by a save (default: a cancel was asked)."""
        was = self.cancelling if superseded is None else superseded
        self.streak = self.streak + 1 if was else 0
        self.building = False
        self.cancelling = False
        self.protected = False



# ================================================================ the watcher

def _next_build_number() -> int:
    """w-<n> ids keep counting across watchers: one more than the highest in the build history."""
    try:
        from iosstudio.features.projects.store import load_history
        hist = load_history()
    except Exception:  # noqa: BLE001
        return 1
    best = 0
    for e in hist:
        m = re.fullmatch(r"w-(\d+)", str(e.get("buildId") or "")) if isinstance(e, dict) else None
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


class Watcher:
    """The watcher process: file events -> Scheduler -> HelperBuild, snapshots to watch.json and
    watch-events.jsonl. `build_factory(kind, build_id, extra)` makes the builds (tests pass a fake)."""

    def __init__(self, root: Path, auto_run: bool = False, debounce_ms: int = DEFAULT_DEBOUNCE_MS,
                 initial_build: bool = True, build_factory: Callable[..., Any] | None = None,
                 watcher: FileWatcher | None = None, clock: Callable[[], float] = time.monotonic,
                 owner_check: bool = True,
                 device_check: Callable[[], tuple[str | None, str | None]] | None = None,
                 auth_check: Callable[[], bool] | None = None, run_steps: Any = None):
        self.root = root
        self.auto_run = auto_run
        self.debounce_ms = debounce_ms
        self.initial_build = initial_build
        self.build_factory = build_factory or (lambda kind, bid, extra: HelperBuild(
            kind, str(root), bid, [*extra, "--trigger", "watch"]))
        self.watcher = watcher
        self.mode_reason: str | None = None
        self.clock = clock
        self.owner_check = owner_check
        self.check_project = True
        self.device_check = device_check or device_for_run
        self.auth_check = auth_check
        self.run_steps = run_steps            # install/launch over the device daemon (tests replace it)
        self.sched = Scheduler(debounce_ms / 1000.0)
        self.filter = ContentFilter(root)
        self.build: Any = None
        self.build_files: list[str] = []
        self.run_job: RunJob | None = None    # the latest auto-run
        self.run_jobs: list[RunJob] = []      # every auto-run thread still to be joined (an older one can still be
                                              # reporting while a newer one runs)
        self.stop_reason: str | None = None
        self.stop_message: str | None = None
        self.reload = False
        self.lock = threading.RLock()
        self.seq = 0
        self.number = 0
        self.last_owner_check = 0.0
        self.last_progress = 0.0
        self.snap: dict = {}

    # ---- snapshots
    def publish(self, state: str | None = None, reason: str = "", message: str | None = None, **fields: Any) -> dict:
        with self.lock:
            self.seq += 1
            if state:
                self.snap["state"] = state
            if message is not None:
                self.snap["message"] = message
            self.snap.update(fields)
            for k in [k for k, v in self.snap.items() if v is None]:
                self.snap.pop(k)
            self.snap.update(event="watch", seq=self.seq, reason=reason, updatedAt=time.time(),
                             autoRun=self.auto_run, debounceMs=self.debounce_ms,
                             builds=self.number, superseded=self.sched.superseded)
            snap = dict(self.snap)
            save_json(status_path(), snap)
            line = json.dumps(snap, separators=(",", ":"), default=str) + "\n"
            ev = events_path()
            try:
                if ev.exists() and ev.stat().st_size > EVENTS_MAX:
                    ev.write_text(line)
                else:
                    with open(ev, "a") as fh:
                        fh.write(line)
            except OSError:
                pass
            return snap

    def _new_id(self) -> str:
        if not self.number:
            self.number = _next_build_number() - 1
        self.number += 1
        return f"w-{self.number}"

    # ---- builds
    def start_compile(self, files: list[str]) -> None:
        bid = self._new_id()
        self.build_files = files
        try:
            self.build = self.build_factory("compile", bid, [])
        except OSError as exc:
            self.sched.finished()
            self.publish("failed", "build-started", f"Couldn't start the build: {exc}")
            return
        self.publish("compiling", "build-started",
                     f"Compiling ({len(files)} changed file{'s' if len(files) != 1 else ''})" if files
                     else "Compiling", buildId=bid, logFile=None, progress=None,
                     changed=files[:CHANGED_SHOWN], changedCount=len(files))

    def on_build_events(self, events: list[dict]) -> None:
        for ev in events:
            if ev.get("event") == "build" and ev.get("logFile"):
                self.publish(reason="build-log", logFile=ev["logFile"])
            elif ev.get("event") == "progress" and self.clock() - self.last_progress >= PROGRESS_EVERY:
                self.last_progress = self.clock()
                frac = ev.get("fraction")
                self.publish(reason="progress", progress={"fraction": frac, "detail": str(ev.get("detail") or "")[:200]})

    def on_build_done(self) -> None:
        b, self.build = self.build, None
        done = b.result()
        getattr(b, "close", lambda: None)()
        code = (done.get("error") or {}).get("code") if not done.get("ok") else None
        self.sched.finished(superseded=code == "cancelled" and self.sched.cancelling)
        if code == "cancelled":
            if self.stop_reason:
                return
            self.publish("changed", "superseded", "Files changed during the build; compiling again",
                         progress=None)
            return
        if code == "busy":
            self.sched.request(self.clock(), BUSY_RETRY)
            self.publish("changed", "busy", "Another build of this project is running; compiling after it",
                         progress=None)
            return
        last = summarize(done, b.build_id, b.log_file)
        if last["result"] == "ok":
            w = last["warnings"]
            msg = "Compiled cleanly" + (f", {w} warning{'s' if w != 1 else ''}" if w else "")
            self.publish("ok", "build-finished", msg, lastBuild=last, progress=None, logFile=last["logFile"])
            if self.auto_run and not self.sched.pending:
                self.maybe_run(last["id"])
        else:
            n = last["errors"]
            msg = (f"{n} error{'s' if n != 1 else ''}" if n else (last.get("errorMessage") or "The build failed"))
            self.publish("failed", "build-finished", msg, lastBuild=last, progress=None, logFile=last["logFile"])

    # ---- auto-run
    def maybe_run(self, for_build: str | None = None) -> None:
        """Auto-run after the clean compile `for_build` (its id goes in run.forBuild)."""
        if self.run_job and self.run_job.alive() and self.run_job.phase in ("installing", "launching"):
            self.publish(reason="run", run={"state": "skipped", "message": "The previous run is still installing, "
                                            "so this clean build wasn't run.", "time": time.time(),
                                            "forBuild": for_build})
            return
        self.run_jobs = [j for j in self.run_jobs if j.alive()]
        self.run_job = RunJob(self, self._new_id(), for_build)
        self.run_jobs.append(self.run_job)
        self.run_job.start()

    def run_blocks(self) -> bool:
        """A run that is still checking or building holds the project: compiles wait for it."""
        return bool(self.run_job and self.run_job.alive() and self.run_job.phase in ("checking", "building"))

    # ---- main loop
    def on_changes(self, files: list[str]) -> None:
        now = self.clock()
        cancel = self.sched.changed(files, now)
        if self.run_job and self.run_job.alive() and self.run_job.phase in ("checking", "building"):
            self.run_job.cancel("A newer change came in before the run started; compiling that first.")
        if cancel and self.build:
            self.build.cancel()
            self.publish(reason="superseding", message="Files changed during the build; restarting it",
                         changed=self.sched.files[:CHANGED_SHOWN], changedCount=len(self.sched.files))
        elif self.build and self.sched.protected:
            self.publish(reason="changed", message="Files changed; this build finishes first (the last "
                         f"{self.sched.supersede_limit} were restarted), then it compiles again",
                         changed=self.sched.files[:CHANGED_SHOWN], changedCount=len(self.sched.files))
        elif not self.build:
            n = len(self.sched.files)
            self.publish("changed", "changed", f"{n} file{'s' if n != 1 else ''} changed",
                         changed=self.sched.files[:CHANGED_SHOWN], changedCount=n)

    def check_owners(self) -> None:
        now = self.clock()
        if not self.owner_check or now - self.last_owner_check < OWNER_CHECK:
            return
        self.last_owner_check = now
        owners = live_owners()
        if not owners:
            self.stop_reason = "no-owner"
            self.stop_message = "Stopped: the app or MCP session that used the watcher has ended"
            return
        if len(owners) != self.snap.get("owners"):
            self.publish(reason="owners", owners=len(owners))
        if self.check_project and not (self.root / "Package.swift").is_file():
            self.stop_reason = "project-gone"
            self.stop_message = "Stopped: the project folder or its Package.swift is gone"

    def apply_control(self) -> None:
        c = fileio.load_json(control_path(), {}) or {}
        if c.get("path") not in (None, str(self.root)):
            return
        if "autoRun" in c:
            self.auto_run = bool(c["autoRun"])
        if c.get("debounceMs"):
            self.debounce_ms = clamp_debounce(c["debounceMs"])
            self.sched.debounce = self.debounce_ms / 1000.0
            self.sched.max_delay = max(MAX_DELAY, self.sched.debounce)
        self.publish(reason="settings", message=("Watching; auto-run on" if self.auto_run else "Watching")
                     if self.snap.get("state") in ("watching", None) else None)

    def step(self, timeout: float = 0.5) -> None:
        """One turn of the loop (tests call it directly)."""
        now = self.clock()
        d = self.sched.due_in(now)
        if d is not None:
            timeout = min(timeout, d)
        fds = [f for f in (self.watcher.fileno() if self.watcher else None,
                           self.build.fileno() if self.build else None) if f is not None]
        try:
            if fds:
                select.select(fds, [], [], max(timeout, 0))
            elif timeout > 0:
                time.sleep(timeout)     # polling mode, idle: the scheduler's due time already bounds timeout
        except InterruptedError:
            pass
        if self.reload:
            self.reload = False
            self.apply_control()
        raw = self.watcher.read(self.clock()) if self.watcher else []
        changes = self.filter.changed(raw) if raw else []
        if changes:
            self.on_changes(changes)
        if self.build:
            self.on_build_events(self.build.read_events())
            self.build.enforce()
            if self.build.finished():
                self.on_build_events(self.build.read_events())
                self.on_build_done()
        if not self.build and not self.run_blocks():
            files = self.sched.tick(self.clock())
            if files is not None:
                self.start_compile(files)
        self.check_owners()

    def setup(self) -> None:
        if self.watcher is None:
            self.watcher, self.mode_reason = make_watcher(self.root)
        self.filter.prime()
        name = self.root.name
        self.snap = {"project": name, "path": str(self.root), "pid": os.getpid(), "mode": self.watcher.mode,
                     "modeReason": self.mode_reason, "startedAt": time.time(), "owners": len(live_owners())}
        self.publish("watching", "started", "Watching for changes" + (" (checking files every second)"
                                                                       if self.watcher.mode == "poll" else ""))
        if self.initial_build:
            self.sched.request(self.clock())

    def shutdown(self) -> None:
        if self.build:
            self.build.cancel()
        for job in self.run_jobs:
            if job.alive():
                job.cancel("The watcher stopped.")
        deadline = time.monotonic() + CANCEL_GRACE
        while self.build and not self.build.finished() and time.monotonic() < deadline:
            self.build.read_events()
            fd = self.build.fileno()
            if fd is not None:      # wake on its output (or EOF) instead of a fixed tick
                try:
                    select.select([fd], [], [], 0.25)
                except InterruptedError:
                    pass
            else:
                time.sleep(0.1)
        if self.build:
            self.build.kill()
            self.build = None
        deadline = time.monotonic() + CANCEL_GRACE + 1
        for job in self.run_jobs:     # no thread may still write to the runtime dir once shutdown returns
            job.join(max(0.0, deadline - time.monotonic()))
        self.run_jobs = [j for j in self.run_jobs if j.alive()]
        if self.watcher:
            self.watcher.close()
        reason = self.stop_reason or "stopped"
        self.publish("stopped", "stopped", self.stop_message or "Stopped", stopReason=reason, stoppedAt=time.time(),
                     progress=None, owners=0)
        try:
            owners_path().unlink()
        except OSError:
            pass

    def loop(self) -> None:
        self.setup()
        try:
            while not self.stop_reason:
                self.step()
        finally:
            self.shutdown()


# ================================================================ demo

DEMO_ERROR = {"severity": "error", "category": "compiler", "line": 42, "col": 17,
              "message": "cannot find 'habbits' in scope"}


class DemoWatcher(Watcher):
    """Demo mode: no files are watched and nothing is built. A scripted cycle repeats: watching -> changed
    -> compiling -> failed (1 error) -> watching -> changed -> compiling -> ok (auto-run: launched)."""

    SCRIPT = [(2.0, "changed"), (0.6, "compiling"), (3.0, "failed"), (3.0, "changed"), (0.6, "compiling"),
              (3.0, "ok"), (6.0, "changed")]

    def __init__(self, root: Path, auto_run: bool = False, speed: float = 1.0):
        super().__init__(root, auto_run=auto_run, initial_build=False, watcher=None)
        self.speed = speed
        self.check_project = False   # demo projects are made up
        self.i = 0
        self.next_at = 0.0
        self.t0 = 0.0

    def setup(self) -> None:
        self.snap = {"project": self.root.name, "path": str(self.root), "pid": os.getpid(), "mode": "inotify",
                     "startedAt": time.time(), "owners": len(live_owners())}
        self.number = 0
        self.publish("watching", "started", "Watching for changes")
        self.next_at = self.clock() + self.SCRIPT[0][0] * self.speed

    def step(self, timeout: float = 0.5) -> None:
        if self.reload:
            self.reload = False
            self.apply_control()
        now = self.clock()
        if now < self.next_at:
            time.sleep(min(timeout, self.next_at - now))
            self.check_owners()
            return
        _, what = self.SCRIPT[self.i % len(self.SCRIPT)]
        f = f"Sources/{self.root.name}/ContentView.swift"
        if what == "changed":
            self.publish("changed", "changed", "1 file changed", changed=[f], changedCount=1)
        elif what == "compiling":
            self.number += 1
            self.t0 = time.time()
            self.publish("compiling", "build-started", "Compiling (1 changed file)", buildId=f"w-{self.number}",
                         progress={"fraction": 0.4, "detail": "Compiling"})
        else:
            bad = what == "failed"
            diag = [{**DEMO_ERROR, "file": str(self.root / f)}] if bad else []
            last = {"id": f"w-{self.number}", "result": "failed" if bad else "ok", "duration": 3.1,
                    "time": self.t0, "errors": 1 if bad else 0, "warnings": 0, "diagnostics": diag,
                    "logFile": str(runtime_dir() / "demo-build.log")}
            if bad:
                last.update(errorCode="build-failed", errorMessage="The build failed.")
            self.publish(what, "build-finished", "1 error" if bad else "Compiled cleanly", lastBuild=last,
                         progress=None)
            if not bad and self.auto_run:
                self.publish(reason="run", run={"state": "launched", "message": "Running on the iPhone (demo)",
                                                "buildId": f"w-{self.number}", "forBuild": f"w-{self.number}",
                                                "time": time.time(), "bundleId": "com.example.habits"})
        self.i += 1
        self.next_at = self.clock() + self.SCRIPT[self.i % len(self.SCRIPT)][0] * self.speed
        self.check_owners()

    def shutdown(self) -> None:
        self.publish("stopped", "stopped", self.stop_message or "Stopped", stopReason=self.stop_reason or "stopped",
                     stoppedAt=time.time(), progress=None, owners=0)
        try:
            owners_path().unlink()
        except OSError:
            pass

