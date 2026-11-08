"""LLDB session driver. Runs under the Python that the Swift toolchain's lldb links (python3.12),
started by helper/iosstudio/features/debugger.py, never by the user.

    python3.12 -I lldb_driver.py '<config json>'

It drives lldb through its Python API (SBDebugger), with the steps omarchy-ios-kit / omarchy-apple-dev
use for a phone reached over a userspace tunnel:
  1. settings set target.memory-module-load-level minimal   (otherwise every image not in the
     sysroot is read from process memory in 512-byte reads, minutes over Wi-Fi)
  2. platform select remote-ios --sysroot "<DeviceSupport/<ios> (<build)>"
  3. a target for the local .app's executable, its platform path set to the installed one
  4. `process connect connect://127.0.0.1:<port>` in ASYNC mode (synchronous, lldb waits forever
     for a stop from a debugserver that has no process yet), then poll until the process is valid
  5. `process attach --pid <pid>` (sync: returns once the app is stopped)

Output: JSON lines on stdout (`{"event": ...}`); anything lldb itself prints goes to stderr, which
the helper shows as console text. Input: JSON lines on stdin, `{"cmd": "continue"}` etc. (see
COMMANDS), `{"cancel": true}`, or a prompt answer `{"prompt": id, "value": "<cmd>"}`.
Whatever happens, the app is detached from (or killed, when asked) before this exits.

Config keys: mode ("backtrace" | "session"), lldbPythonPath, port, host, pid, remotePath,
localExe, sysroot, platform ("remote-ios"), loadLevel ("minimal"), breakpoints [spec], commands
[lldb command], maxFrames, stopFrames, localsDepth, connectTimeout.
"""
from __future__ import annotations

import json
import os
import re
import select
import signal
import sys
import time

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

_quit: str | None = None            # "detach" | "kill" once a signal or stdin EOF asks to end


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
            self.drain()
            p = self.target.GetProcess()
            if p.IsValid() and p.GetState() not in (lldb.eStateInvalid, lldb.eStateExited, lldb.eStateDetached):
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

    def value_json(self, v, depth: int) -> dict:
        out = {"name": v.GetName() or "", "type": clip(v.GetDisplayTypeName() or v.GetTypeName() or "", 200)}
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
            out["children"] = [self.value_json(v.GetChildAtIndex(i), depth - 1) for i in range(min(n, CHILD_CAP))]
            if n > CHILD_CAP:
                out["more"] = True
        return out

    def variables(self, frame_index: int | None = None, depth: int | None = None) -> dict:
        t = self.process.GetSelectedThread()
        f = t.GetFrameAtIndex(frame_index) if frame_index is not None else t.GetSelectedFrame()
        if not f.IsValid():
            raise Fail("bad-args", "There's no such frame.")
        d = int(self.cfg.get("localsDepth", 1) if depth is None else depth)
        vs = f.GetVariables(True, True, False, True)
        items = [self.value_json(vs.GetValueAtIndex(i), max(0, min(d, 3))) for i in range(min(vs.GetSize(), VAR_CAP))]
        return {"thread": t.GetIndexID(), "frame": f.GetFrameID(), "function": f.GetDisplayFunctionName(),
                "variables": items, "more": vs.GetSize() > VAR_CAP}

    def stop_state(self) -> dict:
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
        if t.IsValid():
            try:
                out["locals"] = self.variables()
            except Exception as exc:  # noqa: BLE001 - locals are best effort
                out["localsError"] = clip(str(exc), 200)
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
    drv.dbg.SetAsync(True)
    running = False
    # Start the app (or let it carry on) unless asked to stay paused.
    if drv.cfg.get("stopAtAttach"):
        emit("stopped", **drv.stop_state())
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
                    emit("ack", cmd=cmd, ok=True, requestId=rid, running=running, breakpoints=drv.breakpoint_list())
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
                t = p.GetSelectedThread()
                if cmd == "continue":
                    p.Continue()
                elif cmd == "step":
                    t.StepInto()
                elif cmd == "next":
                    t.StepOver()
                elif cmd == "finish":
                    t.StepOut()
                elif cmd == "frame_variables":
                    emit("variables", requestId=rid, **drv.variables(msg.get("frame"), msg.get("depth")))
                    continue
                elif cmd == "select_frame":
                    t.SetSelectedFrame(int(msg.get("frame") or 0))
                    emit("variables", requestId=rid, **drv.variables(None, msg.get("depth")))
                    continue
                elif cmd == "backtrace":
                    emit("backtrace", requestId=rid, **drv.backtrace(int(msg.get("maxFrames") or 64)))
                    continue
                elif cmd == "evaluate":
                    expr = str(msg.get("expr") or "").strip()
                    if not expr:
                        raise Fail("bad-args", "Nothing to evaluate.")
                    opts = lldb.SBExpressionOptions()
                    opts.SetTimeoutInMicroSeconds(10_000_000)
                    opts.SetTryAllThreads(False)
                    v = t.GetSelectedFrame().EvaluateExpression(expr, opts)
                    res = drv.value_json(v, 1)
                    emit("evaluated", requestId=rid, expr=clip(expr, 300), ok=not v.GetError().Fail(), result=res)
                    continue
                running = True
                emit("running", cmd=cmd)
            except Fail as exc:
                emit("ack", cmd=cmd, ok=False, requestId=rid, code=exc.code, message=exc.message)
        if end:
            break
        for state, restarted in drv.drain():
            if state == lldb.eStateStopped and not restarted:
                running = False
                emit("stopped", **drv.stop_state())
            elif state == lldb.eStateRunning:
                running = True
            elif state in (lldb.eStateExited, lldb.eStateCrashed, lldb.eStateDetached):
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
