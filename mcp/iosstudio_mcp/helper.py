"""Async access to `bin/iosstudio-helper`, the app's stdlib-only backend.

    data = await call("projects", "list")                     # one JSON object -> its "data"
    proc = await spawn_stream("build", "compile", {"path": p})  # JSON lines (see jobs.py)

Every helper failure becomes a `HelperFailure` (a ToolError), whose text is the helper's
user-facing message plus the MCP tool an agent should call next. argv lists only, never a shell.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

log = logging.getLogger("iosstudio.helper")

REPO = Path(os.environ.get("IOSSTUDIO_REPO") or Path(__file__).resolve().parents[2])
HELPER = REPO / "bin" / "iosstudio-helper"
APP_LAUNCHER = REPO / "bin" / "ios-studio"
HOME = Path.home()
if str(REPO / "helper") not in sys.path:      # the helper's stdlib-only `iosstudio.core` (one set of path rules)
    sys.path.append(str(REPO / "helper"))
from iosstudio.core import appdirs  # noqa: E402,F401  (tools use helper.appdirs)


def demo_mode() -> bool:
    return bool(os.environ.get("IOSSTUDIO_DEMO"))


# ------------------------------------------------------------------ error mapping

# helper error code -> what the agent should do next (tool names, human-only steps)
NEXT_BY_CODE: dict[str, str] = {
    "not-signed-in": "Signing in needs the human (password and 2FA never go through MCP): ask them to sign in, "
                     "or call ios_open_app(page='account') to open the sign-in screen. Then retry.",
    "key-mode": "This Apple ID sign-in mode can't list account details. Ask the user to sign in with a password "
                "via ios_open_app(page='account').",
    "missing-xtool": "Run ios_toolchain_check, then ios_toolchain_install (step='xtool').",
    "missing-tool": "Run ios_toolchain_check to see what's missing, then ios_toolchain_install.",
    "missing-sdk": "Run ios_toolchain_check; the iOS SDK step may need the Xcode .xip (ios_open_app(page='setup')).",
    "needs-step": "Run ios_toolchain_check to see which setup step is missing.",
    "needs-xcode": "The SDK needs an Xcode .xip download from Apple, which the human has to fetch: "
                   "ios_open_app(page='setup').",
    "no-device": "No iPhone is selected. Ask the user to connect it (USB cable or Wi-Fi) and unlock it; "
                 "ios_open_app(page='device') shows pairing. Then pass its udid or retry.",
    "device-not-found": "The iPhone didn't show up. Ask the user to plug it in, unlock it and tap Trust; "
                        "ios_open_app(page='device').",
    "device-not-on-usb": "Retry without via='usb' to install over Wi-Fi, or ask the user to plug in the cable.",
    "device-unreachable": "Ask the user to unlock the iPhone and keep it on the same Wi-Fi, then retry.",
    "not-found": "Call ios_project_list to see valid project paths.",
    "not-a-project": "Call ios_project_list, or make the folder an app with ios_project_convert / ios_project_create.",
    "needs-conversion": "Call ios_project_convert(project_path=...) to add an xtool.yml.",
    "not-convertible": "Create a fresh app with ios_project_create and move the code into it.",
    "generated-project": "Call ios_project_info to see the suggested build settings, then (after asking the user) "
                         "ios_project_build_settings(project_path, prebuild=<script that regenerates xtool.yml>, "
                         "allow_generated=true, confirm=true); or build it with the project's own script.",
    "prebuild-failed": "Read ios_build_log for the prebuild's output, fix the script or change the command with "
                       "ios_project_build_settings, then rebuild.",
    "open-files-limit": "Lower open_files with ios_project_build_settings, or ask the user to raise the hard "
                        "open-file limit (it needs a new login session).",
    "invalid-setting": "Fix the value and call ios_project_build_settings again.",
    "unknown-preset": "Use one of the preset names listed in the message (ios_project_presets(project_path) shows "
                      "them with their args), or save it first with ios_project_preset_set(action='save').",
    "invalid-preset": "Fix the preset (a name, args as a list of strings, env with string values, language like 'de', "
                      "region like 'de_DE') and call ios_project_preset_set(action='save') again.",
    "no-build": "Call ios_run (or ios_build_start action='ipa') to produce a signed build first.",
    "build-failed": "Call ios_build_diagnostics and ios_build_log(grep='error') for the failing lines.",
    "compile-errors": "Call ios_build_diagnostics for file:line errors, fix them, then rebuild.",
    "needs-certificate": "The signing certificate must be replaced; this needs the human's decision. "
                         "Retry the build and answer its prompt with ios_job_respond only after asking them.",
    "signing-failed": "Call ios_signing_status and ios_build_log(grep='error') to see why signing failed.",
    "busy": "Another build of this project is running. Check ios_job_status, or wait and retry.",
    "pacman-locked": "A package manager is running. Wait for it to finish, then retry.",
    "network": "Check the internet connection, then retry.",
    "timeout": "Retry; if it keeps happening, check ios_status.",
    "interactive": "This step is done by the human in the app: ios_open_app(page='setup').",
    "no-pkexec": "Polkit isn't available, so system packages can't be installed from here. Ask the user to run "
                 "Setup in the app.",
    "missing-swift-format": "swift-format comes with the Swift toolchain: run ios_toolchain_check, then "
                            "ios_toolchain_install (step='swift').",
    "bad-config": "The project's .swift-format isn't valid JSON: fix it (swift-format dump-configuration prints a "
                  "valid one), then retry.",
    "disk-full": "Free disk space (ios_toolchain_info shows what can be cleaned), then retry.",
    "already-added": "The package is already in Package.swift: call ios_package_add with the same url and "
                     "product='<name>' to link a product, or see ios_package_list.",
    "fetch-failed": "Check the URL (a git repository that clones without a password), then call ios_package_add "
                    "again.",
    "resolve-failed": "No version matches the rule: call ios_package_add again with another version or rule "
                      "(rule='branch', version='main').",
    "no-versions": "The repository has no release tags: call ios_package_add with rule='branch' (version='main') "
                   "or rule='revision'.",
    "no-products": "The package has no library products, so an app can't use it. Remove it with "
                   "ios_package_remove.",
    "unknown-product": "Use one of the product names in the message (ios_package_add(product=...)).",
    "unknown-package": "Use an identity from ios_package_list.",
    "unknown-target": "Use a target name from ios_package_list (targets).",
    "bad-url": "Pass the package's git URL (https://github.com/owner/repo) or a local package folder.",
    "bad-version": "Pass a version like 1.2.0 (from/upToNextMinor/exact), a branch name or a commit hash.",
    "bad-rule": "rule is one of from, upToNextMinor, exact, branch, revision.",
    "manifest-invalid": "Package.swift has errors: fix the lines listed (ios_build_diagnostics after a compile "
                        "shows them too), then retry.",
    "edit-invalid": "Nothing was changed. Package.swift needs this edit by hand: show the user the diff and "
                    "problems.",
    "changed": "Package.swift changed meanwhile: call ios_package_remove again without confirm for a new diff.",
    "package-busy": "Another package change of this project (from the app or another agent) is running: wait a "
                    "minute and retry.",
    "package-failed": "Read the message; fix Package.swift or the request, then retry.",
    "missing-swift": "Run ios_toolchain_check, then ios_toolchain_install (step='swift').",
}

NEXT_BY_PAGE: dict[str, str] = {
    "setup": "Call ios_toolchain_check, then ios_toolchain_install, or ios_open_app(page='setup') for human steps.",
    "account": "Call ios_account_status; for sign-in ask the user and use ios_open_app(page='account').",
    "device": "Ask the user to connect and unlock the iPhone (ios_open_app(page='device')).",
    "projects": "Call ios_project_list.",
    "toolchain": "Call ios_toolchain_info.",
}


def next_step_for(err: dict) -> str | None:
    code = str(err.get("code") or "")
    if code in NEXT_BY_CODE:
        return NEXT_BY_CODE[code]
    page = (err.get("fix") or {}).get("page")
    return NEXT_BY_PAGE.get(page) if page else None


def format_error(err: dict) -> str:
    msg = str(err.get("message") or "The iOS Studio helper failed.").strip()
    code = err.get("code")
    nxt = next_step_for(err)
    out = f"{msg} [{code}]" if code else msg
    if nxt:
        out += f" Next: {nxt}"
    return out


class HelperFailure(ToolError):
    """A handled helper error ({code, message, fix, detail}) rendered for an agent."""

    def __init__(self, err: dict):
        self.error = err
        self.code = str(err.get("code") or "failed")
        super().__init__(format_error(err))


# ------------------------------------------------------------------ argv

def build_argv(group: str, action: str, flags: dict[str, Any] | None = None) -> list[str]:
    """--key value pairs from a dict. True -> bare flag, False/None -> omitted. Keys use dashes."""
    argv = [str(HELPER), group, action]
    for key, val in (flags or {}).items():
        if val is None or val is False:
            continue
        name = "--" + key.replace("_", "-")
        if val is True:
            argv.append(name)
            continue
        sval = str(val)
        if sval.startswith("--") or "\x00" in sval:
            raise ToolError(f"Invalid value for {key}: {sval!r}.")
        argv += [name, sval]
    return argv


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("HOME", str(HOME))
    env.setdefault("NO_COLOR", "1")
    return env


def _preexec() -> None:  # pragma: no cover - runs in the child
    """Die with the server: PR_SET_PDEATHSIG(SIGTERM)."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        pass


def kill_group(pid: int, sig: int = signal.SIGKILL) -> None:
    if pid <= 1:
        return
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# ------------------------------------------------------------------ one-shot

def _clamp(timeout: float, what: str) -> float:
    """The call's time budget (core/deadline.py; imported here to keep this module's imports to the stdlib/mcp)."""
    from .core import deadline
    return deadline.clamp(timeout, what)


async def call(group: str, action: str, flags: dict[str, Any] | None = None, *,
               timeout: float = 25, stdin: str | None = None) -> Any:
    """Run one helper command and return its `data`. Raises HelperFailure / ToolError. Inside a tool call the
    timeout is cut to what is left of the call's budget (core/deadline.py)."""
    timeout = _clamp(timeout, f"'{group} {action}'")
    argv = build_argv(group, action, flags)
    log.debug("helper: %s", argv[1:])
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_child_env(), start_new_session=True, preexec_fn=_preexec)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        kill_group(proc.pid)
        await proc.wait()
        raise ToolError(f"iOS Studio's helper took longer than {timeout:.0f} s for '{group} {action}' [timeout]. "
                        "Next: retry, or check ios_status.")
    except BaseException:  # cancelled by the client: never leave the child behind
        kill_group(proc.pid)
        raise
    if err:
        log.debug("helper stderr (%s %s): %s", group, action, err.decode(errors="replace")[-2000:])
    obj = parse_result(out.decode(errors="replace"))
    if obj is None:
        raise ToolError(f"iOS Studio's helper returned no result for '{group} {action}' (exit {proc.returncode}).")
    if obj.get("ok"):
        return obj.get("data")
    raise HelperFailure(obj.get("error") or {})


def parse_result(text: str) -> dict | None:
    """Last JSON object with "ok" in the output (one-shot), or a streaming `done` event."""
    for line in reversed(text.strip().splitlines()):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if "ok" in obj and "event" not in obj:
                return obj
            if obj.get("event") == "done":
                return {"ok": obj.get("ok"), "data": obj.get("data"), "error": obj.get("error")}
    return None


async def call_stream_collect(group: str, action: str, flags: dict[str, Any] | None = None, *,
                              timeout: float = 25) -> tuple[Any, list[dict]]:
    """Run a short *streaming* command to completion (e.g. a --dry-run). Returns (done data, events)."""
    timeout = _clamp(timeout, f"'{group} {action}'")
    argv = build_argv(group, action, flags)
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=_child_env(), start_new_session=True, preexec_fn=_preexec)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        kill_group(proc.pid)
        await proc.wait()
        raise ToolError(f"'{group} {action}' took longer than {int(timeout)} s.")
    except BaseException:
        kill_group(proc.pid)
        raise
    events = []
    for line in out.decode(errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    res = parse_result(out.decode(errors="replace"))
    if res is None:
        raise ToolError(f"'{group} {action}' returned no result (exit {proc.returncode}).")
    if not res.get("ok"):
        raise HelperFailure(res.get("error") or {})
    return res.get("data"), events


async def spawn_stream(group: str, action: str, flags: dict[str, Any] | None = None) -> asyncio.subprocess.Process:
    """Start a streaming helper command (stdin kept open for prompt answers) in its own process group."""
    argv = build_argv(group, action, flags)
    log.info("job: %s", " ".join(argv[1:]))
    return await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_child_env(), start_new_session=True, preexec_fn=_preexec, limit=4 * 1024 * 1024)


# ------------------------------------------------------------------ device client

_device_client_mod = None


def device_client_module():
    """helper/iosstudio/device/device_client.py (the stdlib asyncio client for the shared device daemon), loaded under a
    private module name so helper/'s other modules never shadow anything on sys.path."""
    global _device_client_mod
    if _device_client_mod is None:
        import importlib.util
        import sys
        path = REPO / "helper" / "iosstudio" / "device" / "device_client.py"
        spec = importlib.util.spec_from_file_location("iosstudio_device_client", path)
        if spec is None or spec.loader is None:
            raise ToolError("The device client (helper/iosstudio/device/device_client.py) is missing from this install.")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _device_client_mod = mod
    return _device_client_mod


# ------------------------------------------------------------------ paths

def tilde(path: str | None) -> str | None:
    """~-relative display form (keeps outputs short and free of the user's home path)."""
    if not path:
        return path
    h = str(HOME)
    return "~" + path[len(h):] if path == h or path.startswith(h + "/") else path


def expand(path: str) -> Path:
    if "\x00" in path:
        raise ToolError("Invalid path.")
    return Path(os.path.expanduser(path.strip())).resolve()


def allowed_roots() -> list[Path]:
    """Where project folders may live: the home folder, plus IOSSTUDIO_MCP_ALLOW_PATHS (colon-separated)."""
    roots = [HOME.resolve()]
    for extra in (os.environ.get("IOSSTUDIO_MCP_ALLOW_PATHS") or "").split(":"):
        if extra.strip():
            roots.append(Path(os.path.expanduser(extra.strip())).resolve())
    return roots


def inside_home(p: Path) -> bool:
    """True if `p` is inside an allowed root (the home folder by default)."""
    for root in allowed_roots():
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def is_project_dir(p: Path) -> bool:
    return (p / "Package.swift").is_file() and (p / "xtool.yml").is_file()
