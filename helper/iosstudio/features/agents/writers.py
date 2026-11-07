"""Registering iOS Studio's MCP server with an AI tool, or removing it: through the tool's own CLI (Claude
Code, Gemini CLI) or by editing its config file (JSON with comments, Codex's TOML, Crush's crushrc). A file
edit is planned, checked by parsing the result and comparing it with the intended data, shown as a diff
(--dry-run), backed up, written and verified (config_files.commit)."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, jsonc
from iosstudio.core.jsonc import JsoncError
from iosstudio.core.output import HelperError
from iosstudio.features import config_files

from .clients import (
    DEMO_ENV,
    EXTRA_BIN_DIRS,
    HOME,
    SERVER,
    crush_line,
    crushrc_entries,
    entry_json,
    launcher,
    registration,
    same_path,
    toml_block,
    which,
)

MARK_START = "ios-studio: mcp server start"
MARK_END = "ios-studio: mcp server end"
KEEP_BACKUPS = 10        # per config file
CLI_TIMEOUT = 90         # seconds for `claude mcp add` and the like


def _backups_dir() -> Path:
    return appdirs.STATE_DIR / "backups"


def _commit(cid: str, path: Path, old: str, new: str, verify: Callable[[str], None]) -> dict:
    return config_files.commit(path, old, new, verify,
                               lambda target: config_files.backup_into(_backups_dir(), f"agents-{cid}-", target,
                                                                       keep=KEEP_BACKUPS))


def _unsafe(path: Path, detail: str | None = None, why: str = "") -> HelperError:
    return HelperError("config-write", f"iOS Studio couldn't work out a safe change to {appdirs.tilde(path)}{why}",
                       detail=detail)


def _unreadable(path: Path, exc: Exception) -> HelperError:
    return HelperError("config-unreadable", f"{appdirs.tilde(path)} has a mistake in it ({exc}), so iOS Studio "
                       "won't touch it. Fix it by hand, or use Copy setup snippet.")


def _plan(path: Path, old: str, new: str) -> dict:
    return {"method": "file", "file": str(path), "diff": config_files.unified_diff(path, old, new),
            "changed": new != old}


# ================================================================ JSON with comments

def _json_change(c: dict, connect: bool, demo: bool) -> tuple[Path, str, str, Any]:
    """(file, old text, new text, the data the new text must parse to)."""
    path = c["file"] if c["format"] == "json" else c["json_file"]
    keypath = c["path"] if c["format"] == "json" else ["mcp", SERVER]
    old = config_files.read_text(path)
    try:
        before = jsonc.load(old)
    except JsoncError as exc:
        raise _unreadable(path, exc) from None
    if not isinstance(before, dict):
        raise HelperError("config-unreadable", f"{appdirs.tilde(path)} isn't a settings object, so iOS Studio won't touch it.")
    expected = json.loads(json.dumps(before))
    node = expected
    for k in keypath[:-1]:
        if not isinstance(node.get(k), dict):
            if not connect:
                return path, old, old, before
            node[k] = {}
        node = node[k]
    if connect:
        value = entry_json(c.get("entry", "crush" if c["format"] == "crushrc" else c["id"]), launcher(), demo)
        node[keypath[-1]] = value
        new = config_files.set_member(old, keypath, value)
    else:
        node.pop(keypath[-1], None)
        if not node and len(keypath) > 1:   # remove_member drops a parent it leaves empty
            parent = expected
            for k in keypath[:-2]:
                parent = parent[k]
            parent.pop(keypath[-2], None)
        new = config_files.remove_member(old, keypath)
    return path, old, new, expected


def _json_apply(c: dict, connect: bool, demo: bool, dry: bool) -> dict:
    path, old, new, expected = _json_change(c, connect, demo)
    try:
        got = jsonc.load(new)
    except JsoncError as exc:
        raise _unsafe(path, str(exc), ".") from None
    if got != expected:
        raise _unsafe(path, why=", so it changed nothing. Use Copy setup snippet to add it by hand.")
    plan = _plan(path, old, new)
    if dry:
        return plan

    def verify(text: str) -> None:
        if jsonc.load(text) != expected:
            raise HelperError("config-write", f"{appdirs.tilde(path)} didn't read back as expected, so it was restored.")
    plan.update(_commit(c["id"], path, old, new, verify))
    return plan


# ================================================================ marked blocks (TOML, crushrc)

def _strip_marked(lines: list[str]) -> list[str]:
    """Remove our marker-delimited block, and the one blank line _append_marked put before it."""
    out: list[str] = []
    inside = False
    for line in lines:
        s = line.strip()
        if s == f"# {MARK_START}":
            inside = True
            if out and out[-1].strip() == "":
                out.pop()
            continue
        if inside:
            if s == f"# {MARK_END}":
                inside = False
            continue
        out.append(line)
    return out


def _append_marked(text: str, body: str) -> str:
    block = f"# {MARK_START}\n{body.rstrip(chr(10))}\n# {MARK_END}\n"
    if not text:
        return block
    return text + ("\n" if text.endswith("\n") else "\n\n") + block


# ================================================================ Codex (TOML)

def _toml_header_path(line: str) -> list | None:
    s = line.strip()
    if not s.startswith("["):
        return None
    try:
        d = tomllib.loads(s)
    except tomllib.TOMLDecodeError:
        return None
    path: list = []
    while isinstance(d, (dict, list)):
        if isinstance(d, list):
            d = d[0] if d else None
            continue
        if len(d) != 1:
            break
        k = next(iter(d))
        path.append(k)
        d = d[k]
    return path


def _toml_remove(text: str) -> str:
    """Drop our marked block, or else a hand-written [mcp_servers.iosstudio] table and its sub-tables."""
    out, skipping = [], False
    for line in _strip_marked(text.splitlines(True)):
        hp = _toml_header_path(line)
        if hp is not None:
            skipping = hp[:2] == ["mcp_servers", SERVER]
        if not skipping:
            out.append(line)
    return "".join(out)


def _drop_empty_servers(d: dict) -> dict:
    if d.get("mcp_servers") == {}:
        d = dict(d)
        d.pop("mcp_servers")
    return d


def _toml_apply(c: dict, connect: bool, demo: bool, dry: bool) -> dict:
    path = c["file"]
    old = config_files.read_text(path)
    try:
        before = tomllib.loads(old) if old.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise _unreadable(path, exc) from None
    expected = json.loads(json.dumps(before, default=str))
    servers = expected.setdefault("mcp_servers", {})
    servers.pop(SERVER, None)
    new = _toml_remove(old)
    if connect:
        entry: dict = {"command": launcher(), "args": [], "startup_timeout_sec": 30, "tool_timeout_sec": 120}
        if demo:
            entry["env"] = dict(DEMO_ENV)
        servers[SERVER] = entry
        new = _append_marked(new, toml_block(launcher(), demo))
    expected = _drop_empty_servers(expected)

    def check(text: str) -> None:
        try:
            got = json.loads(json.dumps(tomllib.loads(text), default=str))
        except tomllib.TOMLDecodeError as exc:
            raise _unsafe(path, str(exc), ".") from None
        if _drop_empty_servers(got) != expected:
            raise _unsafe(path, why=" (the server may be set up in an unusual way there), so it changed nothing. "
                                    "Edit it by hand, or use Copy setup snippet.")
    check(new)
    plan = _plan(path, old, new)
    if dry:
        return plan
    plan.update(_commit(c["id"], path, old, new, check))
    return plan


# ================================================================ Crush (crushrc)

def _crush_apply(c: dict, connect: bool, demo: bool, dry: bool) -> dict:
    path = c["file"]
    # Someone using the older crush.json (and no crushrc) keeps using it.
    if c["json_file"].exists() and not path.exists():
        return _json_apply(c, connect, demo, dry)
    old = config_files.read_text(path)
    lines = _strip_marked(old.splitlines(True))
    stray = {e["line"] for e in crushrc_entries("".join(lines))}   # hand-written `mcp add iosstudio`
    new = "".join(ln for i, ln in enumerate(lines) if i not in stray)
    if connect:
        new = _append_marked(new, crush_line(launcher(), demo))

    def check(text: str) -> None:
        found = crushrc_entries(text)
        if connect and (len(found) != 1 or not same_path(found[0]["command"] or "", launcher())):
            raise HelperError("config-write", f"{appdirs.tilde(path)} didn't read back as expected.")
        if not connect and found:
            raise HelperError("config-write", f"iOS Studio couldn't remove itself from {appdirs.tilde(path)}.")
        if shutil.which("bash"):
            r = subprocess.run(["bash", "-n"], input=text, capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                raise HelperError("config-write", f"The change would break {appdirs.tilde(path)}, so it wasn't made.",
                                  detail=r.stderr)
    check(new)
    plan = _plan(path, old, new)
    if dry:
        return plan
    plan.update(_commit(c["id"], path, old, new, check))
    return plan


# ================================================================ through the tool's own CLI

def _run_cli(argv: list[str], timeout: float = CLI_TIMEOUT) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    extra = [str(Path(argv[0]).parent)] + [str(HOME / d) for d in EXTRA_BIN_DIRS]
    env["PATH"] = ":".join(extra + [env.get("PATH", "/usr/bin:/bin")])
    env.setdefault("NO_COLOR", "1")
    env["CI"] = "1"   # no interactive prompts or spinners from node CLIs
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL, cwd=str(HOME))
    except FileNotFoundError:
        raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed.") from None
    except subprocess.TimeoutExpired:
        raise HelperError("timeout", f"{Path(argv[0]).name} took too long to respond.") from None


def _cli_steps(c: dict, connect: bool, demo: bool) -> list[list[str]]:
    exe = which((c["cli"],)) or c["cli"]
    cmd = launcher()
    registered = registration(c)["registered"]
    steps: list[list[str]] = []
    if c["id"] == "claude":
        if registered:
            steps.append([exe, "mcp", "remove", "--scope", "user", SERVER])
        if connect:
            env = ["-e", "IOSSTUDIO_DEMO=1"] if demo else []
            steps.append([exe, "mcp", "add", *env, "--scope", "user", SERVER, "--", cmd])
    elif c["id"] == "gemini":
        if registered:
            steps.append([exe, "mcp", "remove", "-s", "user", SERVER])
        if connect:
            steps.append([exe, "mcp", "add", "-s", "user", "--timeout", "600000", SERVER, cmd]
                         + (["-e", "IOSSTUDIO_DEMO=1"] if demo else []))
    return steps


def _cli_apply(c: dict, connect: bool, demo: bool, dry: bool) -> dict:
    steps = _cli_steps(c, connect, demo)
    plan = {"method": "cli", "file": str(c["file"]), "commands": [shlex.join(s) for s in steps],
            "changed": bool(steps)}
    if dry or not steps:
        return plan
    for argv in steps:
        r = _run_cli(argv)
        if r.returncode != 0:
            raise HelperError("agent-cli", f"{c['name']} didn't accept the change.",
                              detail=(shlex.join(argv) + "\n" + (r.stderr or r.stdout or "")).strip())
    if registration(c)["registered"] != connect:
        raise HelperError("agent-cli", f"{c['name']} said yes, but iOS Studio can't see the change in "
                          f"{appdirs.tilde(c['file'])}.")
    return plan


# ================================================================ connect / disconnect

def change(c: dict, connect: bool, demo: bool = False, dry: bool = False, via: str = "") -> dict:
    """Register (connect) or remove the server for client c. via="file" edits the config file even when the
    tool has a CLI (tests). dry: only the plan (diff or commands)."""
    if not connect and not registration(c)["registered"]:
        return {"method": "none", "changed": False, "file": str(c["file"])}
    if c.get("cli") and via != "file" and which((c["cli"],)):
        res = _cli_apply(c, connect, demo, dry)
    elif c["format"] == "toml":
        res = _toml_apply(c, connect, demo, dry)
    elif c["format"] == "crushrc":
        res = _crush_apply(c, connect, demo, dry)
    else:
        res = _json_apply(c, connect, demo, dry)
    res.update({"client": c["id"], "name": c["name"], "dryRun": dry, "connected": connect if not dry else None,
                "demo": demo, "command": launcher(), "restartHint": c["restart"]})
    return res
