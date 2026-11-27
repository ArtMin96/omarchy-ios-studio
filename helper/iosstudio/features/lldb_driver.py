"""LLDB session driver. Runs under the Python that the Swift toolchain's lldb links (python3.12),
started by helper/iosstudio/features/debugger.py, never by the user.

    python3.12 -I lldb_driver.py '<config json>'

It drives lldb through its Python API (SBDebugger), with the steps omarchy-ios-kit / omarchy-apple-dev
use for a phone reached over a userspace tunnel:
  1. settings set target.memory-module-load-level minimal   (otherwise every image not in the
     sysroot is read from process memory in 512-byte reads, minutes over Wi-Fi),
     settings set target.process.memory-cache-line-size 16384 on an iPhone, 4096 elsewhere (the device's page
     size: fewer round trips, see MEMORY_CACHE_LINE), and
     settings set target.sdk-path "<iPhoneOS.sdk>" when the helper runs the shadow lldb
     (lldb_shadow.py), so Swift expressions find the SDK's modules (SwiftShims, CoreFoundation)
  2. platform select remote-ios --sysroot "<DeviceSupport/<ios> (<build)>"
  3. a target for the local .app's executable, its platform path set to the installed one
  4. `process connect connect://127.0.0.1:<port>` in ASYNC mode (synchronous, lldb waits forever
     for a stop from a debugserver that has no process yet), then poll until the process is valid
  5. `process attach --pid <pid>` (sync: returns once the app is stopped)

Output: JSON lines on stdout (`{"event": ...}`); anything lldb itself prints goes to stderr, which
the helper shows as console text. Input: JSON lines on stdin, `{"cmd": "continue"}` etc. (see
COMMANDS), `{"cancel": true}`, or a prompt answer `{"prompt": id, "value": "<cmd>"}`.
Whatever happens, the app is detached from (or killed, when asked) before this exits.

Slow reads (a stop's locals, frame_variables, select_frame, evaluate, backtrace) run on a worker thread (Reads), so
the main loop keeps answering stdin and signals while lldb reads phone memory: `stopped` goes out with the frames
as soon as the process stops, and its locals follow as a `variables` event with the same stopId when they take
longer than QUICK_LOCALS (`localsPending: true` on the stop says so). A step, continue or detach cancels a read.

Evaluate takes Swift (`days.count`) or lldb's spelling of it: `p x`, `expression -- x`, and `po x` (or
`expression -O -- x`) for the object's description, as on a Mac.

Config keys: mode ("backtrace" | "session"), lldbPythonPath, sdkPath, port, host, pid, remotePath,
localExe, sysroot, platform ("remote-ios"), loadLevel ("minimal"), breakpoints [spec], commands
[lldb command], maxFrames, stopFrames, localsDepth, connectTimeout, symbolsReady, projectPath (for fixes), quickLocals (tests: QUICK_LOCALS).
"""
from __future__ import annotations

import json
import os
import re
import select
import signal
import sys
import threading
import time
from collections import deque

_OUT = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)                       # lldb's own output never corrupts the JSON lines
sys.stdout = sys.stderr

CFG = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
if CFG.get("lldbPythonPath"):
    sys.path.insert(0, CFG["lldbPythonPath"])

import lldb  # noqa: E402

COMMANDS = ("continue", "step", "next", "finish", "pause", "detach", "kill", "frame_variables", "evaluate",
            "select_frame", "backtrace", "breakpoint_add", "breakpoint_delete", "status")
VALUE_CUT = 400
CHILD_CAP = 20
VAR_CAP = 40
DESCRIPTION_CUT = 2000
# A stop's locals ready within QUICK_LOCALS ride along in `stopped` (as before); slower ones follow as a `variables`
# event. LOCALS_BUDGET caps them: what isn't read by then is listed by name and type (`unread`). Over Wi-Fi without
# the iPhone's debug symbols lldb reads the Swift runtime's metadata from phone memory: four locals of a CrashLab
# frame took 530 s before this cap (and the stop waited for them).
QUICK_LOCALS = 0.5
LOCALS_BUDGET = 20.0
READ_BUDGET = 40.0          # frame_variables / select_frame asked for explicitly
SLOW_BUDGET = 120.0         # evaluate (the first one loads the app's Swift modules) and backtrace
STUCK_AFTER = 2.0           # past its budget by this much, one SB call is stuck: ask lldb to interrupt it
JOIN_WAIT = 5.0             # how long a step, continue or detach waits for a read it cancelled
# lldb caches phone memory in lines of target.process.memory-cache-line-size bytes (default 512), one gdb-remote round
# trip per line. Measured on a CrashLab session over Wi-Fi without debug symbols (2026-11, gdb-remote packet log): one
# stop's locals read 9279 512-byte lines at ~53 ms each (latency-bound: 131 KB reads took ~46 ms too), 530 s in all,
# yet they touched only 1807 distinct 4 KB and 711 distinct 16 KB blocks. 16 KB lines (the iPhone's page size, so a
# line never straddles a page) cut that to ~711 round trips (~40 s) for ~2.4x the bytes (11.6 MB instead of 4.8 MB).
MEMORY_CACHE_LINE = 16384
# A line is one page of the target, never more: lldb reads a line as a whole, and one that reaches into an unmapped page
# fails even for the mapped bytes in it. On a 4 KB-page target (the tests' x86_64 Linux process under lldb-server)
# 16 KB lines broke 1 in 4 expression results ("Couldn't dematerialize a result variable: couldn't read its memory").
PAGE_SIZES = {"remote-ios": MEMORY_CACHE_LINE}   # arm64 iOS: 16 KB pages


def cache_line(platform: str) -> int:
    """target.process.memory-cache-line-size for the platform: its page size (4 KB unless known otherwise)."""
    return PAGE_SIZES.get(platform, 4096)

# `p x`, `po x`, `expression -O -- x`: lldb's commands around a Swift expression. Only followed by something that
# starts an expression, so `p + 1` (a variable named p) stays Swift.
_EXPR_CMD = re.compile(r"^(?P<cmd>po|p|print|e|expr|expression)\s+(?P<obj>-O\s+)?(?:--\s+)?(?=[\w$\"'(\[.@#\\])")
# What lldb says when it can't load the iOS Swift modules (the toolchain's Linux ones shadow the SDK's).
_MODULE_TROUBLE = re.compile(r"SwiftShims|HostInfoError|malformed or corrupted precompiled file|"
                             r"Could not import Swift modules|failed to get module .* from AST context", re.I)
# ... when it can't rebuild the frame's Swift types from the app's build: its precompiled module (.pcm) files are gone
# (SwiftPM prunes them from .build/out/SDKExplicitPrecompiledModules; objects compiled earlier still point at them).
_STALE_TYPES = re.compile(r"type for self cannot be reconstructed|Couldn't realize Swift AST type|"
                          r"\.pcm' does not exist|Unable to locate module needed for external types", re.I)
# ... when the frame has no Swift context to compile in (main, system code).
_NO_JIT = re.compile(r"no JIT compiled function", re.I)
# The Swift plugin looking for Xcode on Linux: noise around the real error.
_HOSTINFO_LINE = re.compile(r"^.*Error while searching for (?:Xcode )?SDK: HostInfoError.*(?:\n|$)", re.M)

_quit: str | None = None            # "detach" | "kill" once a signal or stdin EOF asks to end
_NOT_CONNECTED = (lldb.eStateInvalid, lldb.eStateUnloaded, lldb.eStateExited, lldb.eStateDetached)


def emit(_event: str, **fields) -> None:
    try:
        _OUT.write(json.dumps({"event": _event, **fields}, default=str, separators=(",", ":")) + "\n")
        _OUT.flush()
    except (BrokenPipeError, ValueError, OSError):
        pass


def console(text: str) -> None:
    text = (text or "").rstrip()
    if text:
        emit("console", text=text[-8000:])


def clip(s, n: int = VALUE_CUT) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + "…"


def parse_expr(text: str) -> tuple[str, bool]:
    """(the Swift expression, whether to return its description as `po` does)."""
    text = text.strip()
    m = _EXPR_CMD.match(text)
    if not m:
        return text, False
    return text[m.end():].strip(), m.group("cmd") == "po" or bool(m.group("obj"))


def strip_noise(error: str) -> str:
    """lldb's error without the HostInfoError lines (unless they are all there is)."""
    cleaned = _HOSTINFO_LINE.sub("", error or "").strip()
    return cleaned or (error or "").strip()


def run_fix(project: str | None) -> dict:
    """Run the project again (Projects page): the build regenerates the module files lldb needs."""
    if project:
        return {"label": "Run the app", "page": "projects", "action": "run", "path": project}
    return {"label": "Open Projects", "page": "projects"}


def module_trouble(error: str, sdk: bool, project: str | None = None) -> dict | None:
    """{code, message, fix} in plain words when an expression failed for a reason that isn't the Swift itself (lldb
    couldn't load the iOS modules, rebuild this frame's types, or run code in this frame), else None."""
    text = strip_noise(error)
    if _STALE_TYPES.search(text):
        return {"code": "stale-build", "fix": run_fix(project),
                "message": "Swift expressions need a fresh build: lldb couldn't rebuild this frame's Swift types because "
                           "files from the app's last build are missing. Run the app again (it rebuilds what's "
                           "missing), then debug again. The variables list still works."}
    if _NO_JIT.search(text):
        return {"code": "frame-no-swift", "fix": None,
                "message": "Expressions can't run in this frame: it isn't your Swift code (main and system frames have "
                           "no Swift context). Select one of your app's frames and try again."}
    if not _MODULE_TROUBLE.search(text):
        return None
    if sdk:
        return {"code": "swift-modules", "fix": run_fix(project),
                "message": "lldb couldn't load the app's Swift modules for this expression. The variables list still "
                           "works; try again after the app has been built on this computer."}
    return {"code": "swift-modules", "fix": {"label": "Set up the iOS SDK", "page": "setup", "step": "sdk"},
            "message": "Swift expressions don't work in this session: the debugger couldn't be prepared for iOS (see "
                       "the note above). The variables list still works."}


def _on_signal(signum, _frame):
    global _quit
    _quit = _quit or "detach"


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGHUP, _on_signal)


class Fail(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


# ---------------------------------------------------------------- slow reads off the main loop

class Budget:
    """How long a read may take; `cancel` ends it early (a step, continue or detach came in)."""

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds
        self.cancel = threading.Event()
        self.skipped = False            # some values were left unread

    def spent(self) -> bool:
        return self.cancel.is_set() or time.monotonic() >= self.deadline


class Job:
    """One read on a worker thread. lldb's Python releases the GIL inside SB calls, so the main loop keeps reading
    stdin and handling signals meanwhile."""

    def __init__(self, kind: str, cmd: str, fn, seconds: float, rid=None, stop_id: int | None = None,
                 frame: int | None = None):
        self.kind, self.cmd, self.fn = kind, cmd, fn
        self.budget = Budget(seconds)
        self.rids = [rid] if rid is not None else []
        self.stop_id, self.frame = stop_id, frame
        self.result: dict | None = None
        self.error: Fail | None = None
        self.started = time.monotonic()
        self.interrupted = False
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"lldb-{kind}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            self.result = self.fn(self.budget)
        except Fail as exc:
            self.error = exc
        except Exception as exc:  # noqa: BLE001 - a read never takes the session down
            self.error = Fail("lldb-failed", "The debugger couldn't read that.", f"{type(exc).__name__}: {exc}")
        finally:
            self.done.set()


READS = ("frame_variables", "select_frame", "evaluate", "backtrace")


class Reads:
    """The session's slow reads, one at a time: a stop's locals (kind "locals"), then requests in order. Driven from
    the main loop (on_stop, submit, tick, cancel); only the job's own thread calls into lldb while it runs."""

    def __init__(self, drv: Driver):
        self.drv = drv
        self.job: Job | None = None
        self.queue: deque = deque()

    def busy(self) -> bool:
        return self.job is not None

    def on_stop(self) -> None:
        """Emit `stopped` now; its locals too when they come within QUICK_LOCALS, else as a `variables` event later."""
        st = self.drv.stop_state()
        thread = self.drv.process.GetSelectedThread()
        if st.get("thread") is None or not thread.IsValid():
            emit("stopped", localsPending=False, **st)
            return
        frame = thread.GetSelectedFrame().GetFrameID()
        job = Job("locals", "frame_variables", lambda b: self.drv.variables(None, None, b), LOCALS_BUDGET,
                  stop_id=st["stopId"], frame=frame)
        quick = self.drv.cfg.get("quickLocals")
        if job.done.wait(QUICK_LOCALS if quick is None else float(quick)):
            self._uninterrupt(job)
            if job.error:
                st["localsError"] = clip(job.error.message + (f" ({job.error.detail})" if job.error.detail else ""), 200)
            else:
                st["locals"] = job.result
            emit("stopped", localsPending=False, **st)
            return
        emit("stopped", localsPending=True, **st)
        self.job = job

    def submit(self, msg: dict) -> None:
        """A read request (READS) while the process is stopped: answered when its turn comes."""
        cmd, j = str(msg.get("cmd")), self.job
        if j and j.kind == "locals":
            same = msg.get("frame") in (None, j.frame) if cmd == "frame_variables" else \
                cmd == "select_frame" and int(msg.get("frame") or 0) == j.frame
            if same and msg.get("depth") is None:
                j.rids.append(msg.get("requestId"))         # the same locals: answered with the stop's
                return
            if cmd in ("frame_variables", "select_frame"):  # another frame: the stop's locals aren't wanted any more
                self._cancel_job()
        if self.job:
            self.queue.append(msg)
            return
        self._begin(msg)

    def tick(self) -> None:
        j = self.job
        if j is None:
            return
        if not j.done.is_set():
            if time.monotonic() > j.budget.deadline + STUCK_AFTER:
                self._interrupt(j)          # one SB call is stuck past the budget
            return
        self.job = None
        self._settle(j)
        while self.queue and self.job is None:
            self._begin(self.queue.popleft())

    def cancel(self, wait: float = JOIN_WAIT) -> bool:
        """Drop queued requests and cancel the running read (the app is about to move on). True when nothing runs."""
        while self.queue:
            msg = self.queue.popleft()
            emit("ack", cmd=msg.get("cmd"), ok=False, requestId=msg.get("requestId"), code="cancelled",
                 message="The app moved on before that was read.")
        return self._cancel_job(wait)

    # -- inside
    def _cancel_job(self, wait: float = JOIN_WAIT) -> bool:
        j = self.job
        if j is None:
            return True
        j.budget.cancel.set()
        self._interrupt(j)
        if not j.done.wait(wait):
            return False
        self.job = None
        self._settle(j)
        return True

    def _interrupt(self, j: Job) -> None:
        if not j.interrupted and hasattr(self.drv.dbg, "RequestInterrupt"):
            j.interrupted = True
            self.drv.dbg.RequestInterrupt()

    def _uninterrupt(self, j: Job) -> None:
        if j.interrupted and hasattr(self.drv.dbg, "CancelInterruptRequest"):
            j.interrupted = False
            self.drv.dbg.CancelInterruptRequest()

    def _settle(self, j: Job) -> None:
        """Emit a finished job's answer (nothing for a cancelled one: nobody waits for it)."""
        self._uninterrupt(j)
        if j.budget.cancel.is_set():
            for rid in j.rids:
                emit("ack", cmd=j.cmd, ok=False, requestId=rid, code="cancelled",
                     message="The app moved on before that was read.")
            return
        name = {"locals": "variables", "frame_variables": "variables", "select_frame": "variables",
                "evaluate": "evaluated", "backtrace": "backtrace"}[j.kind]
        if j.error:
            if j.kind == "locals":
                emit("variables", stopId=j.stop_id, frame=j.frame, variables=[], more=False, partial=False,
                     error=clip(j.error.message, 200))
            for rid in j.rids:
                emit("ack", cmd=j.cmd, ok=False, requestId=rid, code=j.error.code, message=j.error.message)
            return
        if j.kind == "locals":
            emit(name, stopId=j.stop_id, **j.result)
        for rid in j.rids:
            emit(name, requestId=rid, **({"stopId": j.stop_id} if j.stop_id else {}), **j.result)

    def _begin(self, msg: dict) -> None:
        drv, cmd, rid = self.drv, str(msg.get("cmd")), msg.get("requestId")
        t = drv.process.GetSelectedThread()
        if cmd == "frame_variables":
            fn, seconds = (lambda b: drv.variables(msg.get("frame"), msg.get("depth"), b)), READ_BUDGET
        elif cmd == "select_frame":
            t.SetSelectedFrame(int(msg.get("frame") or 0))
            fn, seconds = (lambda b: drv.variables(None, msg.get("depth"), b)), READ_BUDGET
        elif cmd == "evaluate":
            frame, expr = t.GetSelectedFrame(), str(msg.get("expr") or "")
            fn, seconds = (lambda b: drv.evaluate(frame, expr)), SLOW_BUDGET
        else:
            fn, seconds = (lambda b: drv.backtrace(int(msg.get("maxFrames") or 64))), SLOW_BUDGET
        self.job = Job(cmd, cmd, fn, seconds, rid=rid)


# ---------------------------------------------------------------- lldb plumbing

class Driver:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        lldb.SBDebugger.Initialize()
        self.dbg = lldb.SBDebugger.Create(False)
        self.dbg.SetAsync(False)
        self.ci = self.dbg.GetCommandInterpreter()
        self.target: lldb.SBTarget | None = None
        self.process: lldb.SBProcess | None = None
        self.app_module = ""
        self.breakpoints: dict[int, str] = {}
        self.stop_id = 0
        self.ended = False
        self.reads = Reads(self)

    def run(self, cmd: str, show: bool = True) -> tuple[bool, str, str]:
        r = lldb.SBCommandReturnObject()
        self.ci.HandleCommand(cmd, r)
        out, errs = r.GetOutput() or "", r.GetError() or ""
        if show:
            console(f"(lldb) {cmd}\n{out}{errs}")
        return r.Succeeded(), out, errs

    # -- connect and attach
    def connect(self) -> None:
        c = self.cfg
        emit("step", id="connect", title="Connecting to the debugger on your iPhone", state="running")
        self.run(f"settings set target.memory-module-load-level {c.get('loadLevel') or 'minimal'}")
        plat = c.get("platform") or "remote-ios"
        self.run(f"settings set target.process.memory-cache-line-size {cache_line(plat)}")
        if c.get("sdkPath"):     # before the target exists: its Swift context is set up from it
            self.run(f'settings set target.sdk-path "{c["sdkPath"]}"')
        sysroot = c.get("sysroot")
        self.run(f'platform select {plat}' + (f' --sysroot "{sysroot}"' if sysroot else ""))
        local = c.get("localExe")
        err = lldb.SBError()
        self.target = self.dbg.CreateTarget(local or "", None, None, True, err) if local else self.dbg.CreateTarget("")
        if not self.target or not self.target.IsValid():
            raise Fail("lldb-failed", "lldb couldn't load the app's executable.", err.GetCString() or "")
        self.dbg.SetSelectedTarget(self.target)     # `process connect` / `process attach` act on it
        if local and c.get("remotePath"):
            m = self.target.GetModuleAtIndex(0)
            if m.IsValid():
                m.SetPlatformFileSpec(lldb.SBFileSpec(c["remotePath"]))
                self.app_module = m.GetFileSpec().GetFilename() or ""
        if not self.app_module and c.get("remotePath"):
            self.app_module = os.path.basename(c["remotePath"])
        # Async: a synchronous `process connect` waits forever for a stop that never comes.
        self.dbg.SetAsync(True)
        url = f"connect://{c.get('host') or '127.0.0.1'}:{int(c['port'])}"
        ok, out, errs = self.run(f"process connect {url}")
        if not ok and errs.strip():
            raise Fail("connect-failed", "Couldn't reach the debugger on your iPhone.", (errs or out).strip())
        if not ok and not self.target.GetProcess().IsValid():
            # Some platforms fail the command without a message; the API call does the same connect.
            e = lldb.SBError()
            self.target.ConnectRemote(self.dbg.GetListener(), url, "gdb-remote", e)
            if e.Fail():
                raise Fail("connect-failed", "Couldn't reach the debugger on your iPhone.", e.GetCString() or "")
        deadline = time.monotonic() + float(c.get("connectTimeout") or 15)
        while time.monotonic() < deadline:
            states = [s for s, _ in self.drain()]
            p = self.target.GetProcess()
            # Connected once we have pulled the process's state event (lldb sets the public state as it's pulled): the
            # state alone can read "stopped" a moment before, while the event is still on its way, and an attach then
            # hung for good (1 in ~50 local sessions under load).
            if p.IsValid() and (p.GetState() == lldb.eStateConnected or any(s not in _NOT_CONNECTED for s in states)):
                break
            if _quit:
                raise Fail("cancelled", "Cancelled.")
            time.sleep(0.1)
        else:
            raise Fail("connect-failed", "The debugger on your iPhone didn't answer.", "process connect: no connection")
        self.dbg.SetAsync(False)
        emit("step", id="connect", title="Connecting to the debugger on your iPhone", state="done")

    def attach(self) -> None:
        pid = int(self.cfg["pid"])
        emit("step", id="attach", title="Attaching to the app (it pauses)", state="running")
        ok, out, errs = self.run(f"process attach --pid {pid}")
        p = self.target.GetProcess()
        if not ok or not p.IsValid() or p.GetState() != lldb.eStateStopped:
            text = (errs or out).strip()
            low = text.lower()
            if "not allowed" in low or "permission" in low or "attach failed" in low and "denied" in low:
                raise Fail("attach-denied", "Your iPhone didn't allow attaching. Only apps you built for development "
                           "can be debugged.", text)
            raise Fail("attach-failed", "Couldn't attach to the app.", text)
        self.process = p
        emit("step", id="attach", title="Attaching to the app (it pauses)", state="done")
        app = self.app_mod()
        emit("attached", pid=p.GetProcessID(), process=app.GetFileSpec().GetFilename() if app else None,
             threads=p.GetNumThreads(), appSymbols=bool(app and app.GetNumCompileUnits() > 0),
             module=self.app_module)
        for cmd in self.cfg.get("commands") or []:
            self.run(str(cmd))

    def app_mod(self):
        if not self.target:
            return None
        for i in range(self.target.GetNumModules()):
            m = self.target.GetModuleAtIndex(i)
            if self.app_module and m.GetFileSpec().GetFilename() == self.app_module:
                return m
        return self.target.GetModuleAtIndex(0) if self.target.GetNumModules() else None

    def drain(self) -> list:
        """Pull pending events (so lldb updates the public process state) and return the process ones."""
        out = []
        lst = self.dbg.GetListener()
        ev = lldb.SBEvent()
        while lst.GetNextEvent(ev):
            if lldb.SBProcess.EventIsProcessEvent(ev):
                out.append((lldb.SBProcess.GetStateFromEvent(ev), lldb.SBProcess.GetRestartedFromEvent(ev)))
            ev = lldb.SBEvent()
        return out

    # -- views
    def frame_json(self, f) -> dict:
        mod = f.GetModule().GetFileSpec().GetFilename() if f.GetModule().IsValid() else None
        fn = f.GetDisplayFunctionName() or f.GetFunctionName()
        out = {"index": f.GetFrameID(), "function": clip(fn, 300) if fn else None, "module": mod,
               "pc": "0x%x" % f.GetPC(), "inApp": bool(mod and mod == self.app_module)}
        sym = f.GetSymbol()
        if sym.IsValid():
            start = sym.GetStartAddress().GetLoadAddress(self.target)
            if start not in (lldb.LLDB_INVALID_ADDRESS, 0) and f.GetPC() >= start:
                out["offset"] = f.GetPC() - start
        le = f.GetLineEntry()
        if le.IsValid() and le.GetLine() > 0:
            fs = le.GetFileSpec()
            out.update(file=fs.fullpath or fs.GetFilename(), line=le.GetLine(), column=le.GetColumn() or None)
        return out

    @staticmethod
    def reason(t) -> str:
        r = t.GetStopReason()
        return {lldb.eStopReasonBreakpoint: "breakpoint", lldb.eStopReasonPlanComplete: "step",
                lldb.eStopReasonTrace: "step", lldb.eStopReasonSignal: "signal",
                lldb.eStopReasonException: "exception", lldb.eStopReasonWatchpoint: "watchpoint",
                lldb.eStopReasonNone: "none"}.get(r, "other")

    def thread_json(self, t, max_frames: int) -> dict:
        n = t.GetNumFrames()
        reason = self.reason(t)
        out = {"index": t.GetIndexID(), "tid": t.GetThreadID(), "name": t.GetName() or None,
               "queue": t.GetQueueName() or None, "reason": reason, "numFrames": n,
               "frames": [self.frame_json(t.GetFrameAtIndex(i)) for i in range(min(n, max_frames))]}
        if reason not in ("none",):
            out["description"] = clip(t.GetStopDescription(256), 300)
        out["crashed"] = reason == "exception" or (reason == "signal" and "SIGSTOP" not in (out.get("description") or ""))
        return out

    def backtrace(self, max_frames: int) -> dict:
        p = self.process
        sel = p.GetSelectedThread()
        threads = [self.thread_json(p.GetThreadAtIndex(i), max_frames) for i in range(p.GetNumThreads())]
        crashed = next((t["index"] for t in threads if t["crashed"]), None)
        return {"pid": p.GetProcessID(), "threads": threads, "selectedThread": sel.GetIndexID() if sel.IsValid() else None,
                "crashedThread": crashed, "module": self.app_module}

    def value_json(self, v, depth: int, budget: Budget | None = None) -> dict:
        out = {"name": v.GetName() or "", "type": clip(v.GetDisplayTypeName() or v.GetTypeName() or "", 200)}
        if budget is not None and budget.spent():
            budget.skipped = True
            out.update(childCount=0, unread=True)
            return out
        summary, value = v.GetSummary(), v.GetValue()
        text = summary if summary else value
        if text is not None:
            out["value"] = clip(text)
        err = v.GetError()
        if err.Fail() and err.GetCString():
            out["error"] = clip(err.GetCString(), 200)
        n = v.GetNumChildren(CHILD_CAP + 1)
        out["childCount"] = n
        if depth > 0 and n:
            out["children"] = [self.value_json(v.GetChildAtIndex(i), depth - 1, budget) for i in range(min(n, CHILD_CAP))]
            if n > CHILD_CAP:
                out["more"] = True
        return out

    def evaluate(self, frame, text: str) -> dict:
        """Evaluate a Swift expression (or `p`/`po`/`expression` …) in `frame`: {expr, ok, result, message?}."""
        expr, describe = parse_expr(text)
        if not expr:
            raise Fail("bad-args", "Nothing to evaluate.")
        opts = lldb.SBExpressionOptions()
        opts.SetTimeoutInMicroSeconds(10_000_000)
        opts.SetTryAllThreads(False)
        v = frame.EvaluateExpression(expr, opts)
        res = self.value_json(v, 1)
        ok = not v.GetError().Fail()
        out = {"expr": clip(text.strip(), 300), "ok": ok, "result": res}
        if ok and describe:
            desc = v.GetObjectDescription()
            if desc:
                res["description"] = clip(desc.rstrip("\n"), DESCRIPTION_CUT)
        if not ok:
            raw = v.GetError().GetCString() or ""
            if res.get("error"):
                res["error"] = clip(strip_noise(raw), 200)
            trouble = module_trouble(raw, bool(self.cfg.get("sdkPath")), self.cfg.get("projectPath"))
            if trouble:
                out.update({k: val for k, val in trouble.items() if val is not None})
        return out

    def variables(self, frame_index: int | None = None, depth: int | None = None, budget: Budget | None = None) -> dict:
        """The frame's locals; with a budget, the ones not read in time come back by name and type (`unread`)."""
        t0 = time.monotonic()
        t = self.process.GetSelectedThread()
        f = t.GetFrameAtIndex(frame_index) if frame_index is not None else t.GetSelectedFrame()
        if not f.IsValid():
            raise Fail("bad-args", "There's no such frame.")
        d = int(self.cfg.get("localsDepth", 1) if depth is None else depth)
        vs = f.GetVariables(True, True, False, True)
        items = [self.value_json(vs.GetValueAtIndex(i), max(0, min(d, 3)), budget)
                 for i in range(min(vs.GetSize(), VAR_CAP))]
        return {"thread": t.GetIndexID(), "frame": f.GetFrameID(), "function": f.GetDisplayFunctionName(),
                "variables": items, "more": vs.GetSize() > VAR_CAP, "partial": bool(budget and budget.skipped),
                "elapsedMs": int((time.monotonic() - t0) * 1000)}

    def stop_state(self) -> dict:
        """Where the process stopped, without the locals (Reads.on_stop adds them)."""
        p = self.process
        t = p.GetSelectedThread()
        if not t.IsValid() or self.reason(t) == "none":
            for i in range(p.GetNumThreads()):
                c = p.GetThreadAtIndex(i)
                if self.reason(c) != "none":
                    t = c
                    p.SetSelectedThread(t)
                    break
        self.stop_id += 1
        reason = self.reason(t) if t.IsValid() else "none"
        desc = (t.GetStopDescription(64) or "") if t.IsValid() else ""
        if reason == "none" or (reason == "signal" and "SIGSTOP" in desc):
            reason = "paused"
        out = {"stopId": self.stop_id, "reason": reason,
               "description": clip(t.GetStopDescription(256), 300) if t.IsValid() else None,
               "thread": self.thread_json(t, int(self.cfg.get("stopFrames") or 40)) if t.IsValid() else None,
               "threads": [{"index": p.GetThreadAtIndex(i).GetIndexID(), "name": p.GetThreadAtIndex(i).GetName() or None,
                            "reason": self.reason(p.GetThreadAtIndex(i))} for i in range(min(p.GetNumThreads(), 64))],
               "threadCount": p.GetNumThreads()}
        if t.IsValid() and t.GetStopReason() == lldb.eStopReasonBreakpoint:
            bid = t.GetStopReasonDataAtIndex(0)
            out["breakpoint"] = {"id": bid, "spec": self.breakpoints.get(bid)}
        return out

    # -- breakpoints
    def add_breakpoint(self, spec: str) -> dict:
        spec = str(spec).strip()
        m = re.match(r"^(.+?):(\d+)$", spec)
        if m:
            bp = self.target.BreakpointCreateByLocation(m.group(1), int(m.group(2)))
        else:
            bp = self.target.BreakpointCreateByName(spec)
        if not bp.IsValid():
            raise Fail("bad-breakpoint", f"lldb couldn't set a breakpoint at {spec}.")
        self.breakpoints[bp.GetID()] = spec
        return {"id": bp.GetID(), "spec": spec, "locations": bp.GetNumLocations(), "resolved": bp.GetNumResolvedLocations()}

    def breakpoint_list(self) -> list:
        out = []
        for i in range(self.target.GetNumBreakpoints()):
            bp = self.target.GetBreakpointAtIndex(i)
            out.append({"id": bp.GetID(), "spec": self.breakpoints.get(bp.GetID(), "?"), "locations": bp.GetNumLocations(),
                        "resolved": bp.GetNumResolvedLocations(), "hits": bp.GetHitCount()})
        return out

    # -- the end
    def finish(self, how: str = "detach") -> str:
        """Detach (or kill) whatever state the process is in. Safe to call twice."""
        if self.ended:
            return how
        self.ended = True
        if not self.reads.cancel():
            console("A read from the iPhone is still running; detaching anyway.")
        p = self.process or (self.target.GetProcess() if self.target else None)
        if p is None or not p.IsValid():
            return "none"
        st = p.GetState()
        if st in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateInvalid, lldb.eStateUnloaded):
            return "exited"
        try:
            if st in (lldb.eStateRunning, lldb.eStateStepping):
                # Interrupt asynchronously and wait for the stop ourselves: a synchronous Stop() can
                # miss a stop event that is already queued and wait forever.
                self.dbg.SetAsync(True)
                p.SendAsyncInterrupt()
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and p.GetState() in (lldb.eStateRunning, lldb.eStateStepping):
                    self.drain()
                    time.sleep(0.05)
                self.drain()
            self.dbg.SetAsync(False)
            if how == "kill":
                e = p.Kill()
                console("(lldb) process kill" + (f"\n{e.GetCString()}" if e.Fail() else ""))
                return "killed"
            e = p.Detach()
            console("(lldb) process detach" + (f"\n{e.GetCString()}" if e.Fail() else ""))
            return "detached" if e.Success() else "detach-failed"
        except Exception as exc:  # noqa: BLE001
            console(f"detach: {exc}")
            return "detach-failed"


# ---------------------------------------------------------------- input

class Input:
    def __init__(self):
        self.buf = b""
        self.eof = False

    def poll(self, timeout: float) -> list[dict]:
        if self.eof:
            time.sleep(timeout)
            return []
        try:
            r, _, _ = select.select([0], [], [], timeout)
        except (InterruptedError, OSError):
            return []
        if not r:
            return []
        chunk = os.read(0, 65536)
        if not chunk:
            self.eof = True
            return [{"cmd": "detach", "reason": "eof"}]
        self.buf += chunk
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                msg = {"cmd": line.decode(errors="replace")}
            if not isinstance(msg, dict):
                continue
            if msg.get("cancel"):
                msg = {"cmd": "detach", "reason": "cancel"}
            elif "prompt" in msg and "cmd" not in msg:
                msg = {**msg, "cmd": msg.get("value")}
            out.append(msg)
        return out


# ---------------------------------------------------------------- modes

def mode_backtrace(drv: Driver) -> dict:
    drv.connect()
    drv.attach()
    emit("step", id="read", title="Reading every thread", state="running")
    bt = drv.backtrace(int(drv.cfg.get("maxFrames") or 128))
    emit("step", id="read", title="Reading every thread", state="done")
    emit("backtrace", **bt)
    how = drv.finish("detach")
    emit("step", id="detach", title="Letting the app run again", state="done")
    return {**bt, "ended": how}


def mode_session(drv: Driver) -> dict:
    global _quit
    drv.connect()
    drv.attach()
    for spec in drv.cfg.get("breakpoints") or []:
        try:
            drv.add_breakpoint(spec)
        except Fail as exc:
            emit("ack", cmd="breakpoint_add", ok=False, message=exc.message, spec=spec)
    emit("breakpoints", items=drv.breakpoint_list())
    inp = Input()
    p = drv.process
    reads = drv.reads
    drv.dbg.SetAsync(True)
    running = False
    # Start the app (or let it carry on) unless asked to stay paused.
    if drv.cfg.get("stopAtAttach"):
        reads.on_stop()
    else:
        p.Continue()
        running = True
        emit("running")
    end = None
    while end is None:
        if _quit:
            end = _quit
            break
        for msg in inp.poll(0.15):
            cmd = str(msg.get("cmd") or "")
            rid = msg.get("requestId")
            try:
                if cmd not in COMMANDS:
                    raise Fail("bad-command", f"Unknown debugger command '{clip(cmd, 40)}'.")
                if cmd in ("detach", "kill"):
                    end = cmd
                    break
                if cmd == "pause":
                    if running:
                        p.SendAsyncInterrupt()
                    emit("ack", cmd=cmd, ok=True, requestId=rid)
                    continue
                if cmd == "status":
                    emit("ack", cmd=cmd, ok=True, requestId=rid, running=running, breakpoints=drv.breakpoint_list(),
                         reading=reads.busy())
                    continue
                if cmd == "breakpoint_add":
                    item = drv.add_breakpoint(str(msg.get("spec") or ""))
                    emit("ack", cmd=cmd, ok=True, requestId=rid, breakpoint=item)
                    emit("breakpoints", items=drv.breakpoint_list())
                    continue
                if cmd == "breakpoint_delete":
                    ok = drv.target.BreakpointDelete(int(msg.get("id")))
                    drv.breakpoints.pop(int(msg.get("id")), None)
                    emit("ack", cmd=cmd, ok=bool(ok), requestId=rid)
                    emit("breakpoints", items=drv.breakpoint_list())
                    continue
                if running:
                    raise Fail("running", "The app is running. Pause it first.")
                if cmd in READS:
                    if cmd == "evaluate" and not str(msg.get("expr") or "").strip():
                        raise Fail("bad-args", "Nothing to evaluate.")
                    reads.submit(msg)
                    continue
                # continue / step / next / finish: what's still being read is for a stop that's about to end
                if not reads.cancel():
                    raise Fail("busy", "lldb is still reading from the iPhone. Try again in a few seconds.")
                t = p.GetSelectedThread()
                if cmd == "continue":
                    p.Continue()
                elif cmd == "step":
                    t.StepInto()
                elif cmd == "next":
                    t.StepOver()
                elif cmd == "finish":
                    t.StepOut()
                running = True
                emit("running", cmd=cmd)
            except Fail as exc:
                emit("ack", cmd=cmd, ok=False, requestId=rid, code=exc.code, message=exc.message)
        if end:
            break
        reads.tick()
        for state, restarted in drv.drain():
            if state == lldb.eStateStopped and not restarted:
                running = False
                reads.on_stop()
            elif state == lldb.eStateRunning:
                running = True
            elif state in (lldb.eStateExited, lldb.eStateCrashed, lldb.eStateDetached):
                reads.cancel(0)
                status = p.GetExitStatus() if state == lldb.eStateExited else None
                emit("exited", status=status, description=clip(p.GetExitDescription() or "", 300) or None)
                drv.ended = True
                end = "exited"
                break
    how = drv.finish("kill" if end == "kill" else "detach")
    return {"ended": how, "stops": drv.stop_id}


def main() -> int:
    drv = None
    try:
        drv = Driver(CFG)
        data = (mode_backtrace if CFG.get("mode") == "backtrace" else mode_session)(drv)
        emit("done", ok=True, data=data)
        return 0
    except Fail as exc:
        how = drv.finish("detach") if drv else "none"
        emit("done", ok=False, error={"code": exc.code, "message": exc.message, "detail": clip(exc.detail, 600) or None},
             ended=how)
        return 1
    except BaseException as exc:  # noqa: BLE001 - always detach, whatever went wrong
        how = drv.finish("detach") if drv else "none"
        emit("done", ok=False, error={"code": "lldb-failed", "message": "The debugger stopped unexpectedly.",
                                      "detail": clip(f"{type(exc).__name__}: {exc}", 600)}, ended=how)
        return 1
    finally:
        try:
            lldb.SBDebugger.Terminate()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
