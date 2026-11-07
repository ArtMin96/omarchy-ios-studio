"""The AI tools iOS Studio knows (where each keeps its MCP servers, how to find it), the entry it registers in
each format, and reading what's registered there now (never changes anything)."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import tomllib
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, jsonc
from iosstudio.core.jsonc import JsoncError
from iosstudio.core.output import HelperError
from iosstudio.features import config_files

SERVER = "iosstudio"     # Gemini CLI doesn't allow underscores in server names
LAUNCHER = appdirs.REPO / "bin" / "iosstudio-mcp"
HOME = appdirs.HOME
DEMO_ENV = {"IOSSTUDIO_DEMO": "1"}

# Where AI tools usually install their command, besides PATH. The app is often started from the desktop,
# where PATH lacks version-manager shims, so these are checked too.
EXTRA_BIN_DIRS = (".local/bin", ".local/share/mise/shims", ".asdf/shims", ".volta/bin", ".bun/bin",
                  ".npm-global/bin", ".local/share/pnpm", ".cargo/bin", ".opencode/bin", ".deno/bin")


def launcher() -> str:
    return str(LAUNCHER)


def which(names: tuple[str, ...]) -> str | None:
    """The first of `names` on PATH or in EXTRA_BIN_DIRS."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        for d in EXTRA_BIN_DIRS:
            cand = HOME / d / name
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
    return None


def same_path(a: str, b: str) -> bool:
    try:
        return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(b)
    except (OSError, ValueError):
        return a == b


# ================================================================ the entry, per format

def entry_json(kind: str, cmd: str, demo: bool) -> dict:
    """The server's entry in a JSON config, in the shape client `kind` expects."""
    env = dict(DEMO_ENV) if demo else {}
    if kind == "opencode":
        e: dict = {"type": "local", "command": [cmd], "enabled": True, "timeout": 120000}
        if demo:
            e["environment"] = env
        return e
    if kind == "zed":
        return {"command": cmd, "args": [], "env": env}
    if kind == "crush":
        e = {"type": "stdio", "command": cmd, "args": [], "timeout": 120}
    elif kind == "gemini":
        e = {"command": cmd, "args": [], "timeout": 600000}
    else:   # cursor, claude
        e = {"command": cmd, "args": []}
    if demo:
        e["env"] = env
    return e


def toml_block(cmd: str, demo: bool) -> str:
    """Codex's [mcp_servers.iosstudio] table (a JSON string is a valid TOML basic string)."""
    lines = [f"[mcp_servers.{SERVER}]", f"command = {json.dumps(cmd, ensure_ascii=False)}", "args = []",
             "startup_timeout_sec = 30", "tool_timeout_sec = 120"]
    if demo:
        lines.append('env = { IOSSTUDIO_DEMO = "1" }')
    return "\n".join(lines) + "\n"


def crush_line(cmd: str, demo: bool) -> str:
    """Crush's `mcp add` line for a crushrc."""
    line = f"mcp add {SERVER} --type stdio --command {shlex.quote(cmd)} --timeout 120"
    if demo:
        line += " --env IOSSTUDIO_DEMO 1"
    return line


# ================================================================ the client table

def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or HOME / ".codex")


def clients() -> list[dict]:
    """id, name, how to detect it, where it keeps MCP servers and how iOS Studio registers there."""
    xdg_config = appdirs.config_home()
    crush_dir = xdg_config / "crush"
    opencode_dir = xdg_config / "opencode"
    oc_file = next((opencode_dir / n for n in ("opencode.jsonc", "opencode.json")
                    if (opencode_dir / n).exists()), opencode_dir / "opencode.json")
    zed_dir = xdg_config / "zed"
    return [
        {"id": "claude", "name": "Claude Code", "bins": ("claude",), "dirs": (HOME / ".claude",),
         "file": HOME / ".claude.json", "format": "json", "path": ["mcpServers", SERVER], "cli": "claude",
         "restart": "Start a new Claude Code session to use it."},
        {"id": "codex", "name": "Codex", "bins": ("codex",), "dirs": (_codex_home(),),
         "file": _codex_home() / "config.toml", "format": "toml",
         "restart": "Start a new Codex session to use it."},
        {"id": "opencode", "name": "opencode", "bins": ("opencode",), "dirs": (opencode_dir,),
         "file": oc_file, "format": "json", "path": ["mcp", SERVER], "entry": "opencode",
         "restart": "Restart opencode to use it."},
        {"id": "cursor", "name": "Cursor", "bins": ("cursor", "cursor-agent"), "dirs": (HOME / ".cursor",),
         "file": HOME / ".cursor/mcp.json", "format": "json", "path": ["mcpServers", SERVER], "entry": "cursor",
         "restart": "Cursor picks it up in a moment; turn it on under Settings › MCP if asked."},
        {"id": "gemini", "name": "Gemini CLI", "bins": ("gemini",), "dirs": (HOME / ".gemini",),
         "file": HOME / ".gemini/settings.json", "format": "json", "path": ["mcpServers", SERVER],
         "entry": "gemini", "cli": "gemini", "restart": "Start a new Gemini CLI session to use it."},
        {"id": "crush", "name": "Crush", "bins": ("crush",), "dirs": (crush_dir,),
         "file": crush_dir / "crushrc", "json_file": crush_dir / "crush.json", "format": "crushrc",
         "restart": "Restart Crush to use it."},
        {"id": "zed", "name": "Zed", "bins": ("zed", "zeditor", "zedit"), "dirs": (zed_dir,),
         "file": zed_dir / "settings.json", "format": "json", "path": ["context_servers", SERVER],
         "entry": "zed", "restart": "Zed picks it up right away; it's listed in the Agent panel's settings."},
    ]


def client(cid: str) -> dict:
    table = clients()
    for c in table:
        if c["id"] == cid:
            return c
    names = ", ".join(c["id"] for c in table)
    raise HelperError("usage", f"Unknown AI tool '{cid}'. Use one of: {names}.")


def method(c: dict) -> str:
    """'cli' when the tool's own CLI registers the server, else 'file' (iOS Studio edits its config)."""
    return "cli" if c.get("cli") and which((c["cli"],)) else "file"


# ================================================================ reading registrations

def crushrc_entries(text: str) -> list[dict]:
    """`mcp add iosstudio …` lines in a crushrc, parsed like the shell would split them."""
    out = []
    for i, line in enumerate(text.splitlines()):
        s = line.strip()
        if not s.startswith("mcp "):
            continue
        try:
            words = shlex.split(s, comments=True)
        except ValueError:
            continue
        if len(words) >= 3 and words[0] == "mcp" and words[1] == "add" and words[2] == SERVER:
            e: dict = {"line": i, "command": None, "env": {}}
            j = 3
            while j < len(words):
                w = words[j]
                if w == "--command" and j + 1 < len(words):
                    e["command"] = words[j + 1]
                    j += 2
                elif w == "--env" and j + 2 < len(words):
                    e["env"][words[j + 1]] = words[j + 2]
                    j += 3
                else:
                    j += 1
            out.append(e)
    return out


def registration(c: dict) -> dict:
    """{registered, command, demo, where} as the client would see it. Never changes anything."""
    fmt = c["format"]
    entry: dict | None = None
    where = c["file"]
    if fmt == "json":
        node: Any = jsonc.load(config_files.read_text(c["file"]))
        for k in c["path"]:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict):
            cmd = node.get("command")
            if isinstance(cmd, list):   # opencode: command is argv
                cmd = cmd[0] if cmd else None
            env = node.get("env") or node.get("environment") or {}
            entry = {"command": cmd, "env": env if isinstance(env, dict) else {}}
    elif fmt == "toml":
        text = config_files.read_text(c["file"])
        data = tomllib.loads(text) if text.strip() else {}
        e = (data.get("mcp_servers") or {}).get(SERVER)
        if isinstance(e, dict):
            entry = {"command": e.get("command"), "env": e.get("env") or {}}
    elif fmt == "crushrc":
        found = crushrc_entries(config_files.read_text(c["file"]))
        if found:
            entry = found[-1]
        else:
            data = jsonc.load(config_files.read_text(c["json_file"])) if c["json_file"].exists() else {}
            e = (data.get("mcp") or {}).get(SERVER) if isinstance(data, dict) else None
            if isinstance(e, dict):
                entry = {"command": e.get("command"), "env": e.get("env") or {}}
                where = c["json_file"]
    if not entry:
        return {"registered": False, "command": None, "demo": False, "where": str(where)}
    return {"registered": True, "command": entry.get("command"),
            "demo": str((entry.get("env") or {}).get("IOSSTUDIO_DEMO", "")) not in ("", "0"), "where": str(where)}


def snippet(c: dict, cmd: str) -> dict:
    """What to add by hand (Copy setup snippet)."""
    q = shlex.quote(cmd)
    cid = c["id"]
    if cid == "claude":
        return {"kind": "shell", "text": f"claude mcp add --scope user {SERVER} -- {q}"}
    if cid == "gemini":
        return {"kind": "shell", "text": f"gemini mcp add -s user --timeout 600000 {SERVER} {q}"}
    if cid == "codex":
        return {"kind": "toml", "where": appdirs.tilde(c["file"]), "text": toml_block(cmd, False)}
    if cid == "crush":
        return {"kind": "shell", "where": appdirs.tilde(c["file"]), "text": crush_line(cmd, False)}
    wrapped: Any = entry_json(c.get("entry", cid), cmd, False)
    for k in reversed(c["path"]):
        wrapped = {k: wrapped}
    return {"kind": "json", "where": appdirs.tilde(c["file"]), "text": json.dumps(wrapped, indent=2)}


def describe(c: dict) -> dict:
    """One row of `agents list`: installed, where, registered, current or stale, the snippet."""
    binary = which(c["bins"])
    dir_found = next((str(d) for d in c["dirs"] if d.is_dir()), None)
    out: dict = {"id": c["id"], "name": c["name"], "installed": bool(binary or dir_found),
                 "binary": binary, "configDir": dir_found, "file": str(c["file"]),
                 "fileShort": appdirs.tilde(c["file"]), "method": method(c), "restartHint": c["restart"]}
    try:
        reg = registration(c)
    except (JsoncError, tomllib.TOMLDecodeError, OSError) as exc:
        reg = {"registered": False, "command": None, "demo": False, "where": str(c["file"])}
        out["error"] = {"code": "config-unreadable",
                        "message": f"{appdirs.tilde(c['file'])} couldn't be read ({exc}). Fix it by hand, then try again."}
    out.update(reg)
    out["whereShort"] = appdirs.tilde(reg.get("where"))
    cmd = reg.get("command")
    out["current"] = bool(reg["registered"] and cmd and same_path(cmd, launcher()))
    out["stale"] = bool(reg["registered"] and not out["current"])
    out["snippet"] = snippet(c, launcher())
    return out
