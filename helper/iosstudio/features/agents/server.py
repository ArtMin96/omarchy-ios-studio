"""iOS Studio's MCP server itself: its launcher and Python environment (status), and setting that
environment up or repairing it (`agents prepare`, streaming)."""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from iosstudio.core import appdirs, output, subproc
from iosstudio.core.output import HelperError
from iosstudio.features import config_files

from .clients import LAUNCHER, launcher, which

PYPROJECT = appdirs.REPO / "mcp" / "pyproject.toml"
PREPARE_TIMEOUT = 900    # seconds for the MCP server's first-time venv setup (downloads its Python packages)
REPAIR_FIX = {"page": "settings", "action": "agents-repair"}


def venv_dir() -> Path:
    env = os.environ.get("IOSSTUDIO_MCP_VENV")
    return Path(env) if env else appdirs.data_dir() / "mcp-venv"


def server_status() -> dict:
    venv = venv_dir()
    want = ""
    try:
        want = hashlib.sha256(PYPROJECT.read_bytes()).hexdigest()[:16]
    except OSError:
        pass
    have = config_files.read_text(venv / ".iosstudio-deps").strip()
    py_ok = os.access(venv / "bin/python", os.X_OK)
    state = "ready" if py_ok and have == want else ("outdated" if py_ok else "missing")
    return {"state": state, "ready": state == "ready", "launcher": launcher(), "launcherShort": appdirs.tilde(launcher()),
            "venv": str(venv), "venvShort": appdirs.tilde(venv), "uv": bool(which(("uv",))),
            "launcherOk": os.access(LAUNCHER, os.X_OK)}


def _install(st: dict) -> int:
    """Run `iosstudio-mcp --prepare` (creates or updates the venv), streaming its progress."""
    output.step("venv", "Setting up the server's Python environment", "running",
                detail="Downloads about 30 MB the first time" if st["state"] == "missing" else "Updating")
    output.progress(None, "Starting…")
    lines: list[str] = []

    def on_line(line: str) -> None:
        text = line.replace("iosstudio-mcp: ", "").strip()
        if text.startswith("setting up"):
            text = "Downloading and installing the server's Python packages…"
        elif text == "ready.":
            text = "Installed"
        if text:
            lines.append(text)
            del lines[:-30]
            output.progress(None, text[:200])
            output.log(text)
    try:    # a stalled download can't hang setup: the launcher (and pip under it) is stopped after this long
        code = subproc.stream_cmd([launcher(), "--prepare"], on_line=on_line, timeout=PREPARE_TIMEOUT)
    except HelperError as exc:
        if exc.code != "timeout":
            raise
        lines.append(exc.message)
        code = -1
    if code != 0:
        output.step("venv", "Setting up the server's Python environment", "failed")
        return output.done(False, error=HelperError(
            "prepare-failed", "Couldn't set up the server's Python environment. Check your internet "
            "connection and try again.", fix={"label": "Try again", **REPAIR_FIX}, detail="\n".join(lines)))
    output.step("venv", "Setting up the server's Python environment", "done")
    return 0


def _check_starts() -> int:
    """Import the server once in its venv."""
    output.step("check", "Checking the server starts", "running")
    output.progress(None, "Starting the server once…")
    env = {"PYTHONPATH": str(appdirs.REPO / "mcp"), "PYTHONSAFEPATH": "1", "IOSSTUDIO_REPO": str(appdirs.REPO),
           "PYTHONPYCACHEPREFIX": str(appdirs.CACHE_DIR / "pycache")}
    try:
        r = subproc.run_cmd([str(venv_dir() / "bin/python"), "-c", "import iosstudio_mcp.server"], timeout=60, env=env)
    except HelperError as exc:
        return output.done(False, error=exc)
    if r.returncode != 0:
        output.step("check", "Checking the server starts", "failed")
        return output.done(False, error=HelperError(
            "server-broken", "The server's environment is set up but the server doesn't start.",
            fix={"label": "Rebuild it", **REPAIR_FIX}, detail=(r.stderr or r.stdout)[-3000:]))
    output.step("check", "Checking the server starts", "done")
    return 0


def prepare(repair: bool = False) -> int:
    """(streaming) Create or update the server's Python environment, then check the server starts. repair:
    remove the old environment first (only ever our own venv)."""
    st = server_status()
    if not st["launcherOk"]:
        return output.done(False, error=HelperError("no-launcher", "iOS Studio's MCP server is missing from the app folder."))
    if repair:
        venv = Path(st["venv"])
        if venv.is_dir() and (venv / "pyvenv.cfg").exists():
            output.step("clean", "Removing the old environment", "running")
            shutil.rmtree(venv, ignore_errors=True)
            output.step("clean", "Removing the old environment", "done")
        st = server_status()
    if st["ready"]:
        output.step("venv", "Server environment", "done", detail="Already set up")
    else:
        code = _install(st)
        if code:
            return code
    code = _check_starts()
    if code:
        return code
    return output.done(True, server_status())
