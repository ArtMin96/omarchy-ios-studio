"""Builds the watcher starts: one `build compile|run` helper child (HelperBuild), its summary, and auto-run (RunJob)."""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from iosstudio.core import appdirs, fileio, subproc

from .runtime import CANCEL_GRACE, HELPER_MAIN, killpg, python_argv, runtime_dir

if TYPE_CHECKING:
    from .watcher import Watcher

EVENT_LINE_MAX = 1 << 20  # bytes; a build event line longer than this is dropped, not buffered forever
FIRST_DIAGS = 5


class HelperBuild:
    """One `build <kind>` helper process, the same command the Build/Run buttons use: it writes the build
    history, the log and the diagnostics. Its own session; it dies with the watcher (PDEATHSIG)."""

    def __init__(self, kind: str, path: str, build_id: str, extra: list[str] | None = None,
                 argv: list[str] | None = None):
        self.kind = kind
        self.build_id = build_id
        self.argv = argv or [*python_argv(), *HELPER_MAIN, "build", kind, "--path", path,
                             "--build-id", build_id, *(extra or [])]
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=subproc.pdeathsig_term,
                                     close_fds=True)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.buf = b""
        self.eof = False
        self.done: dict | None = None
        self.log_file: str | None = None
        self.cancel_at: float | None = None
        self.started = time.monotonic()

    @property
    def pgid(self) -> int:
        return self.proc.pid

    def fileno(self) -> int | None:
        return None if self.eof else self.proc.stdout.fileno()

    def read_events(self) -> list[dict]:
        out: list[dict] = []
        while not self.eof:
            try:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            if not chunk:
                self.eof = True
                break
            self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        if len(self.buf) > EVENT_LINE_MAX:
            self.buf = b""
        if self.eof and self.buf:
            lines.append(self.buf)
            self.buf = b""
        for ln in lines:
            try:
                ev = json.loads(ln.decode(errors="replace"))
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("event") == "build" and ev.get("logFile"):
                self.log_file = ev["logFile"]
            if ev.get("event") == "done":
                self.done = ev
            out.append(ev)
        return out

    def finished(self) -> bool:
        if not self.eof:
            return False
        return self.proc.poll() is not None or self._reap()

    def _reap(self) -> bool:
        try:
            self.proc.wait(timeout=0.2)
            return True
        except subprocess.TimeoutExpired:
            return False

    def cancel(self) -> None:
        """SIGTERM: the helper cancels xtool, kills its process tree and records the build as cancelled."""
        if self.cancel_at is None:
            self.cancel_at = time.monotonic()
            try:
                self.proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

    def enforce(self) -> None:
        """A cancelled build that didn't end within CANCEL_GRACE: kill its whole group."""
        if self.cancel_at is not None and time.monotonic() - self.cancel_at > CANCEL_GRACE:
            killpg(self.pgid)

    def kill(self) -> None:
        killpg(self.pgid)
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        try:
            self.proc.stdout.close()
        except OSError:
            pass

    def result(self) -> dict:
        """{ok, data | error}: the done event, or a synthetic error when the helper died without one."""
        if self.done:
            return self.done
        rc = self.proc.poll()
        code = "cancelled" if self.cancel_at is not None else "build-failed"
        return {"event": "done", "ok": False, "error": {"code": code, "message": "Build cancelled." if
                                                        code == "cancelled" else f"The build helper exited ({rc})."}}


def short_diags(diags: list, n: int = FIRST_DIAGS) -> list[dict]:
    """Errors first, then warnings: ≤ n of {severity, category, file, line, col, message}."""
    ds = [d for d in diags or [] if isinstance(d, dict) and d.get("severity") in ("error", "warning")]
    ds = [d for d in ds if d.get("severity") == "error"] + [d for d in ds if d.get("severity") != "error"]
    out = []
    for d in ds[:n]:
        out.append({k: v for k, v in {
            "severity": d.get("severity"), "category": d.get("category") or ("compiler" if d.get("file") else "other"),
            "file": d.get("file"), "line": d.get("line"), "col": d.get("col"),
            "message": " ".join(str(d.get("message") or "").split())[:300]}.items() if v is not None})
    return out


def summarize(done: dict, build_id: str, log_file: str | None) -> dict:
    """A finished build's done event -> lastBuild."""
    if done.get("ok"):
        b = done.get("data") or {}
        err = None
    else:
        err = done.get("error") or {}
        b = err.get("build") or {}
    diags = b.get("diagnostics") or []
    errors = b.get("errors")
    if errors is None:
        errors = sum(1 for d in diags if isinstance(d, dict) and d.get("severity") == "error")
    warnings = b.get("warnings")
    if warnings is None:
        warnings = sum(1 for d in diags if isinstance(d, dict) and d.get("severity") == "warning")
    out = {"id": build_id, "result": "ok" if done.get("ok") else "failed", "duration": b.get("duration"),
           "time": b.get("time") or time.time(), "errors": errors, "warnings": warnings,
           "logFile": b.get("logFile") or log_file, "diagnostics": short_diags(diags)}
    if err:
        out["errorCode"] = err.get("code")
        out["errorMessage"] = str(err.get("message") or "")[:400]
    return out


def device_for_run() -> tuple[str | None, str | None]:
    """(udid, None) when an iPhone is connected (the device daemon's last-known state is 'connected' and the
    daemon is running), else (None, why not)."""
    st = fileio.load_json(appdirs.STATE_DIR / "device.json", None)
    if not isinstance(st, dict) or not st.get("udid"):
        return None, "no iPhone has been connected yet"
    if st.get("connection") != "connected":
        return None, f"{st.get('name') or 'the iPhone'} isn't connected right now"
    if not (runtime_dir() / "deviced.sock").exists():
        return None, "the iPhone service isn't running (open iOS Studio or the iPhone page)"
    return str(st["udid"]), None



class RunJob(threading.Thread):
    """Auto-run after a clean compile: check the iPhone and Apple ID, `build run`, then (Wi-Fi) install and
    launch through the device daemon. phase: checking | building | installing | launching | done."""

    def __init__(self, w: Watcher, build_id: str, for_build: str | None = None):
        super().__init__(daemon=True, name="watch-run")
        self.w = w
        self.build_id = build_id      # the run's own build (w-N, in the history)
        self.for_build = for_build    # the clean compile it follows
        self.phase = "checking"
        self.cancelled: str | None = None
        self.build: Any = None
        self.once: subprocess.Popen | None = None

    def alive(self) -> bool:
        return self.is_alive()

    def report(self, state: str, message: str, **extra: Any) -> None:
        self.w.publish(reason="run", run={"state": state, "message": message, "buildId": self.build_id,
                                          "forBuild": self.for_build, "time": time.time(), **extra})

    def cancel(self, why: str) -> None:
        self.cancelled = why
        if self.build:
            self.build.cancel()
        if self.once and self.once.poll() is None:
            killpg(self.once.pid, signal.SIGTERM)

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001  a failed run never takes the watcher down
            self.report("failed", f"Auto-run failed: {exc}")
        finally:
            self.phase = "done"

    def _run(self) -> None:
        self.report("checking", "Checking the iPhone and Apple ID")
        udid, why = self.w.device_check()
        if not udid:
            self.report("skipped", f"Not run: {why}.")
            return
        auth = self.w.auth_check
        if auth is None:
            from iosstudio.features.projects.build import auth_ok
            auth = auth_ok
        if not auth():
            self.report("skipped", "Not run: sign in with your Apple ID first (Account page).")
            return
        if self.cancelled:
            self.report("skipped", self.cancelled)
            return
        self.phase = "building"
        self.report("building", "Building, signing and installing on the iPhone")
        extra = ["--udid", udid]
        preset = _selected_preset(self.w.root)
        if preset:
            extra += ["--preset", preset]
        self.build = self.w.build_factory("run", self.build_id, extra)
        while not self.build.finished():
            if self.cancelled:          # also a cancel that came before self.build was set
                self.build.cancel()
            fd = self.build.fileno()
            if fd is not None:
                try:
                    select.select([fd], [], [], 0.5)
                except InterruptedError:
                    pass
            else:
                time.sleep(0.1)
            self.build.read_events()
            self.build.enforce()
        self.build.read_events()
        done = self.build.result()
        if self.cancelled or (done.get("error") or {}).get("code") == "cancelled":
            self.report("skipped", self.cancelled or "The run was cancelled.")
            return
        if not done.get("ok"):
            err = done.get("error") or {}
            self.report("failed", str(err.get("message") or "The run failed.")[:400], errorCode=err.get("code"))
            return
        data = done.get("data") or {}
        steps = self.w.run_steps or DeviceSteps(self)
        bundle = data.get("bundleId")
        if data.get("needsInstall") and data.get("ipa"):
            self.phase = "installing"
            self.report("installing", "Installing on the iPhone over Wi-Fi")
            res = steps.install(udid, data["ipa"])
            if self.cancelled:
                self.report("skipped", self.cancelled)
                return
            if not res.get("ok"):
                self.report("failed", "Built and signed, but installing failed: " +
                            str((res.get("error") or {}).get("message") or "the iPhone wasn't reachable")[:300])
                return
            bundle = (res.get("data") or {}).get("bundleId") or bundle
            try:
                from iosstudio.features.projects.store import record_signing
                record_signing(str(self.w.root), udid, bundle, "wifi", data.get("profile"))
            except Exception:  # noqa: BLE001
                pass
        self.phase = "launching"
        launch = data.get("launch") or {}
        res = steps.launch(udid, bundle, launch.get("args") or [], launch.get("env") or {})
        if not res.get("ok"):
            self.report("failed", "Installed, but it didn't open: " +
                        str((res.get("error") or {}).get("message") or "launch failed")[:300], bundleId=bundle)
            return
        self.report("launched", "Running on the iPhone", bundleId=(res.get("data") or {}).get("bundleId") or bundle)


def _selected_preset(root: Path) -> str | None:
    """The run preset selected for the project in the app (what its Run button uses)."""
    try:
        from iosstudio.features.projects.store import load_state
        name = (load_state()["meta"].get(str(root)) or {}).get("runPreset")
    except Exception:  # noqa: BLE001
        return None
    return name if isinstance(name, str) and name.strip() else None


class DeviceSteps:
    """Install and launch through `bin/iosstudio-deviced --once` (the shared device daemon), killable."""

    def __init__(self, job: RunJob):
        self.job = job

    def _once(self, cmd: str, args: dict, timeout: float) -> dict:
        exe = appdirs.REPO / "bin/iosstudio-deviced"
        try:
            p = subprocess.Popen([str(exe), "--once", cmd, json.dumps(args)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 start_new_session=True, preexec_fn=subproc.pdeathsig_term)
        except OSError as exc:
            return {"ok": False, "error": {"message": str(exc)}}
        self.job.once = p
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            killpg(p.pid)
            subproc.kill_proc(p)
            return {"ok": False, "error": {"message": "timed out"}}
        finally:
            self.job.once = None
        for line in reversed(out.strip().splitlines()):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and "ok" in obj and "event" not in obj:
                return obj
        return {"ok": False, "error": {"message": "the iPhone service gave no answer"}}

    def install(self, udid: str, ipa: str) -> dict:
        return self._once("app.install", {"udid": udid, "path": ipa}, 600)

    def launch(self, udid: str, bundle: str | None, args: list, env: dict) -> dict:
        if not bundle:
            return {"ok": False, "error": {"message": "no bundle id"}}
        apps = self._once("apps.list", {"udid": udid, "icons": False, "sizes": False}, 60)
        data = apps.get("data") if apps.get("ok") else None
        if isinstance(data, dict):  # {apps, stale, fetchedAt}
            data = data.get("apps")
        items = data if isinstance(data, list) else []
        plain = bundle.split(".", 1)[-1] if bundle.startswith("XTL-") else bundle
        real = next((a.get("bundleId") for a in items if a.get("bundleId") == bundle), None) or \
            next((a.get("bundleId") for a in items if str(a.get("bundleId") or "").endswith("." + plain)), bundle)
        a: dict = {"udid": udid, "bundleId": real}
        if args:
            a["args"] = args
        if env:
            a["env"] = env
        return self._once("app.launch", a, 60)

