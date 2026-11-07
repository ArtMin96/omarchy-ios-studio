"""BuildRunner: runs one xtool command and turns its output into step/progress/log/diagnostic/prompt events.

Every child runs in its own process group with a reaper process (core.subproc.Runner), so killing this helper
(even with SIGKILL) also kills xtool, swift-build and swift-frontend."""
from __future__ import annotations

import ctypes
import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from iosstudio.core import output, subproc
from iosstudio.core.output import HelperError, fix
from iosstudio.features import build_settings, diagnostics

EXIT_GRACE = 60          # seconds a build may linger after closing its output

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
TASK = re.compile(r"^\[(\d+)\s*/\s*(\d+)\]\s*(.*)$")
STAGE = re.compile(r"^\[(?P<stage>[A-Za-z][A-Za-z ]+)\]\s*(?:(?P<pct>\d+)%)?")
WROTE = re.compile(r"^Wrote to (?P<path>/.+)$")
STAGE_TEXT = {
    "unpacking app": "Preparing the app",
    "preparing device": "Connecting to your iPhone",
    "provisioning": "Getting a provisioning profile from Apple",
    "signing": "Signing the app",
    "logging in": "Signing in to Apple",
    "packaging": "Packaging",
    "connecting": "Connecting to your iPhone",
    "installing": "Sending the app to your iPhone",
    "verifying": "Your iPhone is verifying the app",
}
BUILD_DONE = re.compile(r"^Build complete! \((?P<secs>[\d.]+) secs?\)")


def _child_setup(open_files: int | None = None) -> None:
    """preexec: own process group, die with our parent, big open-file limit (SDK module builds; a project's
    openFiles build setting, else 65536 capped at the hard limit). Only the child's limit changes."""
    os.setsid()
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass
    build_settings.raise_nofile(open_files)


class BuildRunner:
    """Runs one xtool command, turning its output into step/progress/log/diagnostic/prompt events."""

    WEIGHTS = {"prebuild": 0.04, "resolve": 0.06, "compile": 0.62, "link": 0.08, "sign": 0.12, "package": 0.04,
               "install": 0.12}
    TITLES = {"prebuild": "Running the prebuild command", "resolve": "Resolving packages", "compile": "Compiling", "link": "Linking",
              "sign": "Signing", "package": "Packaging", "install": "Installing", "launch": "Launching"}

    def __init__(self, plan: list[str], interactive: bool, log_file: Path, estimate: float | None,
                 ui_steps: list[str] | None = None):
        self.plan = plan
        self.ui_steps = ui_steps or []
        self.interactive = interactive
        self.estimate = estimate
        self.state = {s: "pending" for s in plan}
        self.current: str | None = None
        self.sub = 0.0
        self.diags: list[dict] = []
        self.parser = diagnostics.Parser()
        self.wrote: str | None = None
        self.build_secs: float | None = None
        self.revoke_certs: list[str] = []
        self._collect_revoke = False
        self.revoke_answer: str | None = None
        self.tail: list[str] = []
        self.started = time.time()
        self.last_progress = 0.0
        self._frac = 0.0            # progress never goes backwards
        self.waiting_device_since: float | None = None
        self.log_fh = open(log_file, "w", buffering=1)
        self._skip_cmdline = False
        self._prompts = 0
        # stdin carries the UI's prompt answers and cancel; the child's stdin gets ours (write_child)
        self.runner = subproc.Runner(lambda text: self.on_line(text), on_partial=lambda text: self.on_partial(text),
                                     on_tick=self._check_device_wait, watch_stdin=True, tail_lines=1)
        self.raw = False            # True while the prebuild runs: log its output, parse nothing
        self.env: dict | None = None
        self.open_files: int | None = None
        self.device_timeout = 90.0

    # ---- events
    def announce(self) -> None:
        total = len(self.plan) + len(self.ui_steps)
        i = 0
        for s in self.plan:
            output.step(s, self.TITLES[s], "pending", index=i, total=total)
            i += 1
        for s in self.ui_steps:
            output.step(s, self.TITLES[s], "pending", index=i, total=total, owner="ui")
            i += 1

    def enter(self, sid: str, detail: str = "") -> None:
        if sid not in self.state or self.state[sid] in ("done",):
            return
        if self.current == sid:
            return
        idx = self.plan.index(sid)
        for prev in self.plan[:idx]:
            if self.state[prev] in ("pending", "running"):
                self.state[prev] = "done"
                output.step(prev, self.TITLES[prev], "done")
        self.current = sid
        self.sub = 0.0
        self.state[sid] = "running"
        output.step(sid, self.TITLES[sid], "running", detail=detail)
        self.emit_progress(detail)

    STEP_OF = {"compiler": "compile", "linker": "link", "package": "resolve"}

    def finish_all(self, ok: bool) -> None:
        # Compiler, linker or package errors name the step that failed; later steps never really ran.
        failed = None if ok else next((self.STEP_OF[c] for d in self.diags if d["severity"] == "error"
                                       for c in [d.get("category") or "compiler"]
                                       if self.STEP_OF.get(c) in self.state), None)
        if not ok and self.state.get("prebuild") in ("running", "failed"):
            failed = "prebuild"
        if failed:
            idx = self.plan.index(failed)
            for earlier in self.plan[:idx]:
                if self.state[earlier] == "running":
                    self.state[earlier] = "done"
                    output.step(earlier, self.TITLES[earlier], "done")
            self.state[failed] = "failed"
            output.step(failed, self.TITLES[failed], "failed")
            for later in self.plan[idx + 1:]:
                if self.state[later] != "pending":
                    self.state[later] = "skipped"
                    output.step(later, self.TITLES[later], "skipped")
            return
        for s in self.plan:
            if self.state[s] == "running" or (ok and self.state[s] == "pending"):
                self.state[s] = "done" if ok else "failed"
                output.step(s, self.TITLES[s], self.state[s])

    def fraction(self) -> float:
        total = sum(self.WEIGHTS.get(s, 0.05) for s in self.plan) or 1.0
        acc = 0.0
        for s in self.plan:
            w = self.WEIGHTS.get(s, 0.05)
            if self.state[s] == "done":
                acc += w
            elif s == self.current:
                acc += w * min(1.0, self.sub)
        self._frac = max(self._frac, min(0.99, acc / total))
        return self._frac

    def emit_progress(self, detail: str = "", force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_progress < 0.25:
            return
        self.last_progress = now
        frac = self.fraction()
        eta = None
        elapsed = now - self.started
        if self.estimate:
            eta = max(1.0, self.estimate - elapsed)
        elif frac > 0.15:
            eta = elapsed / frac - elapsed
        output.progress(round(frac, 3), detail, round(eta) if eta else None)

    # ---- line parsing
    def on_line(self, raw: str) -> None:
        line = ANSI.sub("", raw).replace(" ", " ").rstrip()
        self.log_fh.write(line + "\n")
        if not line.strip():
            return
        self.tail.append(line)
        if len(self.tail) > 200:
            self.tail = self.tail[-200:]
        # Swift's "Failed frontend command:" is followed by one gigantic command line: keep it in the
        # file log only.
        if self._skip_cmdline:
            self._skip_cmdline = False
            if len(line) > 600:
                output.log(line[:300] + " …")
                return
        if line.startswith("Failed frontend command:") or line.endswith("Command line:"):
            self._skip_cmdline = True
        output.log(line)
        if not self.raw:
            self.classify(line)

    def classify(self, line: str) -> None:
        s = line.strip()
        found = self.parser.feed(line)
        for d in found:
            self.add_diag(d)
        if diagnostics.DIAG.match(s):
            if (self.current is None or self.current == "resolve") and found and found[-1]["category"] == "compiler":
                self.enter("compile")
            return
        if self._collect_revoke:
            if s.startswith("- "):
                self.revoke_certs.append(s[2:])
                return
            self._collect_revoke = False
        if s.startswith("The following certificates must be revoked"):
            self._collect_revoke = True
            if "sign" in self.state:
                self.enter("sign")
            return
        if re.match(r"^(Fetching|Updating|Cloning|Resolving|Computing version|Working copy|Creating working copy)", s):
            self.enter("resolve", s)
            return
        if s.startswith("Building for") or s.startswith("[Computing dependencies") or s.startswith("[Pre-planning") \
                or s.startswith("[Planning"):
            self.enter("compile", "Preparing")
            return
        t = TASK.match(s)
        if t:
            n, total, what = int(t.group(1)), int(t.group(2)), t.group(3)
            if what.endswith("-product") or what.startswith("Linking"):
                self.enter("link", "Linking the app")
                self.sub = 0.5
            else:
                self.enter("compile")
                if self.current == "compile":
                    self.sub = max(self.sub, n / max(total, 1))
            self.emit_progress(f"{n} of {total} tasks" + (f" · {what}" if what else ""))
            return
        if s.startswith("Compiling") or s.startswith("Emitting module"):
            self.enter("compile", s)
            return
        if s.startswith("Linking"):
            self.enter("link", s)
            return
        b = BUILD_DONE.match(s)
        if b:
            self.build_secs = float(b.group("secs"))
            if "link" in self.state:
                self.enter("link")
                self.sub = 1.0
            return
        st = STAGE.match(s)
        if st and not TASK.match(s):
            stage = st.group("stage").strip()
            pct = st.group("pct")
            low = stage.lower()
            target = None
            if low in ("unpacking app", "preparing device", "provisioning", "signing", "logging in"):
                target = "sign"
            elif low in ("connecting", "installing", "verifying", "uploading"):
                target = "install"
            elif low == "packaging":
                target = "package" if "package" in self.state else ("install" if "install" in self.state else "sign")
            nice = STAGE_TEXT.get(low, stage)
            if target and target in self.state:
                self.enter(target, nice)
                if pct is not None:
                    self.sub = int(pct) / 100.0
                self.emit_progress(f"{nice} · {pct}%" if pct else nice)
            return
        if s.startswith("Planning...") and "sign" in self.state and self.build_secs is not None:
            return
        if s.startswith("Applying entitlements"):
            return
        if s.startswith("Packaging"):
            if "package" in self.state:
                self.enter("package", "Packaging")
                pm = re.search(r"(\d+)%", s)
                if pm:
                    self.sub = int(pm.group(1)) / 100
                    self.emit_progress(s)
            return
        if s.startswith("Installing to device"):
            return
        if s.startswith("Waiting for device to be connected"):
            self.waiting_device_since = time.time()
            return
        w = WROTE.match(s)
        if w:
            self.wrote = w.group("path")
            return

    def add_diag(self, d: dict) -> None:
        """One parsed diagnostic (already de-duplicated by the parser) -> the list and a `diagnostic` event."""
        self.diags.append(d)
        output.event("diagnostic", **d)

    def flush_diags(self, failed: bool) -> None:
        """End of output: an open linker/package/crash block becomes a diagnostic, and a failed build with
        no error at all gets one last-resort diagnostic from the tool's final words."""
        for d in self.parser.finish(failed):
            self.add_diag(d)

    # ---- prompts
    def on_partial(self, text: str) -> bool:
        """Called with the unterminated tail of xtool's output. Returns True if it was a prompt we answered."""
        if self.raw:
            return False
        clean = ANSI.sub("", text)
        if "Continue? (yes/no)" in clean:
            self.log_fh.write(clean + "\n")
            output.log(clean.strip())
            self.answer_revoke()
            return True
        if re.search(r"Choice \(\d+-\d+\):\s*$", clean):
            output.log(clean.strip())
            self.write_child("\n")  # can't happen with --udid; answer anything rather than hang
            return True
        if clean.rstrip().endswith("Password:") or clean.rstrip().endswith("Code:"):
            # xtool wants to sign in: never from a build. Stop and send the user to the Account page.
            raise HelperError("not-signed-in", "Your Apple ID sign-in has expired. Sign in again, then build.",
                              fix("Sign in", "account", action="signin"))
        return False

    def answer_revoke(self) -> None:
        certs = self.revoke_certs or ["your current development certificate"]
        answer = "no"
        if self.interactive:
            message = (
                "Apple only lets a free account keep one development certificate, and this computer doesn't have "
                "the private key for the one you already have (it was probably made on another computer or in a "
                "previous install).\n\n"
                "To sign this app here, iOS Studio has to revoke (cancel) it and create a new one. If you do:\n"
                "• apps you signed on other computers stop opening until you re-install them from there,\n"
                "• on your iPhone you'll need to trust the new certificate once (Settings › General › VPN & Device "
                "Management),\n"
                "• your Apple ID itself is not affected.\n\n"
                "Certificates to revoke:\n" + "\n".join(f"• {c}" for c in certs)
            )
            value = self.ask("confirm", message, [
                {"value": "no", "label": "Don't revoke"},
                {"value": "yes", "label": "Revoke and continue", "destructive": True},
            ], title="Replace your signing certificate?", default="no", certificates=certs)
            answer = "yes" if value.strip().lower() in ("yes", "y", "true", "1") else "no"
        self.revoke_answer = answer
        output.log(f"Answered: {answer}")
        self.write_child(answer + "\n")

    def ask(self, kind: str, message: str, options: list[dict], **extra: Any) -> str:
        """Prompt the UI and wait for {"prompt": id, "value": ...} on our stdin (raw reads, one buffer)."""
        self._prompts += 1
        pid = f"p{self._prompts}"
        output.event("prompt", id=pid, kind=kind, message=message, options=options, **extra)
        while True:
            msg = self.runner.next_message()
            if msg is None:
                return str(extra.get("default", ""))
            if msg.get("prompt") == pid:
                return str(msg.get("value", ""))

    @property
    def proc(self) -> subprocess.Popen | None:
        return self.runner.proc

    def write_child(self, text: str) -> None:
        self.runner.write(text)

    def _check_device_wait(self) -> None:
        if self.waiting_device_since and time.time() - self.waiting_device_since > self.device_timeout \
                and self.state.get("install") != "running":
            raise HelperError("device-not-found",
                              "Your iPhone didn't show up over the USB cable. Check the cable, unlock the "
                              "phone and tap Trust if it asks.", fix("Open iPhone", "device"))

    # ---- run
    def run(self, argv: list[str], cwd: Path, device_timeout: float = 90) -> int:
        """Run one command to the end: own process group (setsid in _child_setup) with a reaper, so the whole
        tree dies with us; {"cancel": true} on stdin or SIGTERM/SIGINT/SIGHUP raise subproc.Cancelled. A prebuild
        command (raw) gets no input: a script that asks something reads EOF, not a hang."""
        env = self.env if self.env is not None else subproc.tool_env("xtool")
        self.log_fh.write(f"$ {' '.join(shlex.quote(a) for a in argv)}\n  (in {cwd})\n")
        open_files = self.open_files
        self.device_timeout = device_timeout
        return self.runner.run(argv, cwd=cwd, env=env, child_stdin=not self.raw, new_session=False,
                               preexec_fn=lambda: _child_setup(open_files), reaper=True, signals=True,
                               exit_grace=EXIT_GRACE, kill_grace=2.0)

    def close(self) -> None:
        try:
            self.log_fh.close()
        except OSError:
            pass


def classify_failure(runner: BuildRunner, rc: int) -> HelperError:
    """The user-facing error of a build that exited non-zero: from its diagnostics, else its last output lines."""
    text = "\n".join(runner.tail[-120:])
    low = text.lower()
    errors = [d for d in runner.diags if d["severity"] == "error" and d.get("file")
              and (d.get("category") or "compiler") == "compiler"]
    other = next((d for d in runner.diags if d["severity"] == "error" and d not in errors), None)
    if runner.revoke_answer == "no" or re.search(r"revok|continue\?", low) and runner.revoke_answer is None:
        return HelperError("needs-certificate",
                           "Signing stopped because a new certificate is needed and it wasn't replaced. "
                           "Nothing was changed on your Apple account.",
                           {"label": "Review certificates", "page": "account", "action": "certificates"}, detail=text)
    if errors:
        n = len(errors)
        first = errors[0]
        return HelperError("compile-errors",
                           f"Your code has {n} error{'s' if n != 1 else ''}. The first is in "
                           f"{Path(first['file']).name}, line {first['line']}: {first['message']}",
                           {"label": "Open in editor", "page": "projects", "action": "open-diagnostic"}, detail=text)
    # Linker, package, config and compiler-crash problems are named by their diagnostic; signing and device
    # ones go through the checks below, which have the right fix buttons.
    lead = {"linker": "Linking failed", "package": "Swift packages couldn't be resolved",
            "config": "The project settings are invalid"}
    manifest = next((d for d in runner.diags if d["severity"] == "error" and d.get("category") == "package"
                     and str(d.get("file") or "").endswith("Package.swift")), None)
    if manifest:
        return HelperError("build-failed", f"Package.swift has errors. The first is on line {manifest['line']}: "
                           f"{manifest['message']}",
                           {"label": "Open in editor", "page": "projects", "action": "open-diagnostic"}, detail=text)
    if other and other.get("category") in lead:
        return HelperError("build-failed", f"{lead[other['category']]}: {other['message']}",
                           {"label": "Show log", "page": "projects", "action": "log"}, detail=text)
    if other and other["message"].startswith("The Swift compiler crashed"):
        return HelperError("build-failed", other["message"] + ".",
                           {"label": "Show log", "page": "projects", "action": "log"}, detail=text)
    if re.search(r"requires logging in|unauthori[sz]ed|not logged in|log ?in again|invalid.*token|session.*expired|"
                 r"(http|status|code)[ :]*401", low):
        return HelperError("not-signed-in", "Apple didn't accept your sign-in. Sign in again, then build.",
                           fix("Sign in", "account", action="signin"), detail=text)
    if re.search(r"anisette|timed out|could not connect|couldn.t connect|network connection|internet|"
                 r"(http|status|code)[ :]*5\d\d|server busy", low):
        return HelperError("apple-unreachable",
                           "Apple's signing service (or the helper server xtool uses) didn't answer. This is usually "
                           "temporary; try again in a minute.", {"label": "Try again", "page": "projects", "action": "retry"},
                           detail=text)
    if re.search(r"maximum number of (installed )?apps|free develop\w* (provisioning )?profiles? (limit|has been)",
                 low):
        return HelperError("app-limit",
                           "A free Apple account can have 3 of your apps on an iPhone at once. Delete one of them from "
                           "the iPhone, then try again.", fix("Open iPhone", "device"), detail=text)
    if re.search(r"maximum number of app ids|max(imum)? app ids|app id limit|up to 10 app ids|too many app ids|"
                 r"no more app ids", low):
        return HelperError("app-id-limit",
                           "A free Apple account can register at most 10 app IDs per week. Reuse an existing bundle "
                           "ID, or wait a few days.", fix("See app IDs", "account", action="identifiers"), detail=text)
    if re.search(r"darwin sdk|swift-sdks|no swift sdk|could not find.*sdk|sdk.*not (found|installed)|unknown sdk", low):
        return HelperError("missing-sdk", "The iOS SDK isn't installed (or needs rebuilding).",
                           fix("Set up the iOS SDK", "setup", step="sdk"), detail=text)
    if "too many open files" in low:
        return HelperError("open-files", "The build hit the system's open-file limit.",
                           {"label": "Try again", "page": "projects", "action": "retry"}, detail=text)
    if re.search(r"device not found|no devices|could not connect to (the )?device|usbmux|lockdown|pairing", low):
        return HelperError("device-unreachable", "Couldn't reach your iPhone. Unlock it, keep it nearby and check the cable.",
                           fix("Open iPhone", "device"), detail=text)
    if re.search(r"no current ios devices|devices on this team", low):
        return HelperError("signing-failed",
                           "Apple has no iPhone registered on your account yet. Connect the iPhone with a USB cable "
                           "once so xtool can register it, then build again.", fix("Open iPhone", "device"),
                           detail=text)
    if re.search(r"provision|profile|entitlement|not registered|device.*not.*(included|registered)", low):
        return HelperError("signing-failed",
                           "Signing failed. If this is a new iPhone, connect it with a cable and choose Run once so it "
                           "gets registered with your account.", fix("Open iPhone", "device"), detail=text)
    last = other["message"] if other else \
        next((ln for ln in reversed(runner.tail) if ln.strip().lower().startswith(("error", "fatal"))), None)
    return HelperError("build-failed", "The build failed." + (f" {last.strip()[:300]}" if last else ""),
                       {"label": "Show log", "page": "projects", "action": "log"}, detail=text)
