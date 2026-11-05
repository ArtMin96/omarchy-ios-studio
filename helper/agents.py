"""AI agents (group "agents"): connect iOS Studio's MCP server to AI coding tools.

  agents list                         which tools are installed and whether iosstudio is registered
  agents connect --client X [--demo]  register bin/iosstudio-mcp with that tool  (--dry-run: show the plan)
  agents disconnect --client X        remove it again                            (--dry-run: show the plan)
  agents test [--client X] [--demo]   start the server over stdio, initialize + tools/list, count the tools
  agents prepare [--repair]           (streaming) create or update the server's Python environment now

Tools with a reliable CLI of their own are driven through it (Claude Code, Gemini CLI). For the rest the
config file is edited: parsed first (JSON with comments, TOML, crushrc), changed in one place, checked by
parsing the result again and comparing it with the intended data, backed up to
~/.local/state/ios-studio/backups/, written atomically, and restored from the backup if anything fails.
The server is always called "iosstudio" (Gemini CLI doesn't allow underscores in server names).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

import common
from common import HelperError, ok

SERVER = "iosstudio"
LAUNCHER = common.REPO / "bin" / "iosstudio-mcp"
PYPROJECT = common.REPO / "mcp" / "pyproject.toml"
BACKUPS = common.STATE_DIR / "backups"
HOME = common.HOME
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
DEMO_ENV = {"IOSSTUDIO_DEMO": "1"}

MARK_START = "ios-studio: mcp server start"
MARK_END = "ios-studio: mcp server end"

# Where AI tools usually install their command, besides PATH. The app is often started from the desktop,
# where PATH lacks version-manager shims, so these are checked too.
EXTRA_BIN_DIRS = (".local/bin", ".local/share/mise/shims", ".asdf/shims", ".volta/bin", ".bun/bin",
                  ".npm-global/bin", ".local/share/pnpm", ".cargo/bin", ".opencode/bin", ".deno/bin")


def _venv() -> Path:
    env = os.environ.get("IOSSTUDIO_MCP_VENV")
    if env:
        return Path(env)
    data = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")
    return data / "ios-studio" / "mcp-venv"


def _launcher() -> str:
    return str(LAUNCHER)


def _tilde(p: str | Path | None) -> str:
    s = str(p or "")
    h = str(HOME)
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def _which(names: tuple[str, ...]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        for d in EXTRA_BIN_DIRS:
            cand = HOME / d / name
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
    return None


def _run(argv: list[str], timeout: float = 90) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    extra = [str(Path(argv[0]).parent)] + [str(HOME / d) for d in EXTRA_BIN_DIRS]
    env["PATH"] = ":".join(extra + [env.get("PATH", "/usr/bin:/bin")])
    env.setdefault("NO_COLOR", "1")
    env["CI"] = "1"   # no interactive prompts or spinners from node CLIs
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL, cwd=str(HOME))
    except FileNotFoundError:
        raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed.")
    except subprocess.TimeoutExpired:
        raise HelperError("timeout", f"{Path(argv[0]).name} took too long to respond.")


# ================================================================ JSON with comments, span-preserving
# A small parser for JSON plus // and /* */ comments and trailing commas (Zed, opencode and Gemini accept
# those). It records where every object member sits, so a change touches only that member's text and the
# rest of the file (order, indentation, comments) stays byte for byte.

class _Node:
    __slots__ = ("kind", "start", "end", "value", "members")

    def __init__(self, kind: str, start: int):
        self.kind, self.start, self.end, self.value, self.members = kind, start, start, None, []


class _Member:
    __slots__ = ("key", "kstart", "node", "comma")

    def __init__(self, key: str, kstart: int, node: _Node):
        self.key, self.kstart, self.node, self.comma = key, kstart, node, -1


class JsoncError(ValueError):
    pass


class _Parser:
    def __init__(self, text: str):
        self.t, self.i = text, 0

    def ws(self) -> None:
        t, n = self.t, len(self.t)
        while self.i < n:
            c = t[self.i]
            if c in " \t\r\n﻿":
                self.i += 1
            elif t.startswith("//", self.i):
                j = t.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            elif t.startswith("/*", self.i):
                j = t.find("*/", self.i + 2)
                if j < 0:
                    raise JsoncError("unterminated comment")
                self.i = j + 2
            else:
                break

    def err(self, what: str) -> JsoncError:
        line = self.t.count("\n", 0, self.i) + 1
        return JsoncError(f"{what} at line {line}")

    def value(self) -> _Node:
        self.ws()
        if self.i >= len(self.t):
            raise self.err("unexpected end")
        c = self.t[self.i]
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c == '"':
            return self.string()
        return self.scalar()

    def string(self) -> _Node:
        node = _Node("str", self.i)
        j = self.i + 1
        t = self.t
        while j < len(t):
            if t[j] == "\\":
                j += 2
                continue
            if t[j] == '"':
                break
            if t[j] == "\n":
                raise self.err("newline in string")
            j += 1
        else:
            raise self.err("unterminated string")
        node.end = j + 1
        try:
            node.value = json.loads(t[self.i:node.end])
        except json.JSONDecodeError as exc:
            raise self.err(f"bad string ({exc.msg})")
        self.i = node.end
        return node

    def scalar(self) -> _Node:
        node = _Node("lit", self.i)
        j = self.i
        while j < len(self.t) and self.t[j] not in " \t\r\n,]}/":
            j += 1
        raw = self.t[self.i:j]
        try:
            node.value = json.loads(raw)
        except json.JSONDecodeError:
            raise self.err(f"unexpected '{raw[:20]}'")
        node.end = self.i = j
        return node

    def obj(self) -> _Node:
        node = _Node("obj", self.i)
        node.value = {}
        self.i += 1
        while True:
            self.ws()
            if self.i < len(self.t) and self.t[self.i] == "}":
                self.i += 1
                node.end = self.i
                return node
            if self.i >= len(self.t) or self.t[self.i] != '"':
                raise self.err("expected a key")
            kstart = self.i
            key = self.string().value
            self.ws()
            if self.i >= len(self.t) or self.t[self.i] != ":":
                raise self.err("expected ':'")
            self.i += 1
            m = _Member(key, kstart, self.value())
            node.members.append(m)
            node.value[key] = m.node.value
            self.ws()
            if self.i < len(self.t) and self.t[self.i] == ",":
                m.comma = self.i
                self.i += 1
            elif self.i < len(self.t) and self.t[self.i] == "}":
                continue
            else:
                raise self.err("expected ',' or '}'")

    def arr(self) -> _Node:
        node = _Node("arr", self.i)
        node.value = []
        self.i += 1
        while True:
            self.ws()
            if self.i < len(self.t) and self.t[self.i] == "]":
                self.i += 1
                node.end = self.i
                return node
            node.value.append(self.value().value)
            self.ws()
            if self.i < len(self.t) and self.t[self.i] == ",":
                self.i += 1
            elif self.i < len(self.t) and self.t[self.i] == "]":
                continue
            else:
                raise self.err("expected ',' or ']'")


def jsonc_parse(text: str) -> _Node:
    p = _Parser(text)
    root = p.value()
    p.ws()
    if p.i != len(text):
        raise p.err("unexpected text after the end")
    return root


def jsonc_load(text: str) -> Any:
    return jsonc_parse(text).value if text.strip() else {}


def _line_indent(text: str, pos: int) -> tuple[str, bool]:
    """Indentation of the line holding pos, and whether only whitespace precedes pos on it."""
    ls = text.rfind("\n", 0, pos) + 1
    before = text[ls:pos]
    indent = before[:len(before) - len(before.lstrip(" \t"))]
    return indent, before.strip() == ""


def _render(value: Any, indent: str, unit: str | None) -> str:
    if unit is None:
        return json.dumps(value, ensure_ascii=False)
    out = json.dumps(value, indent=1 if unit == "\t" else len(unit), ensure_ascii=False)
    if unit == "\t":
        out = "\n".join(("\t" * (len(l) - len(l.lstrip(" ")))) + l.lstrip(" ") for l in out.split("\n"))
    return out.replace("\n", "\n" + indent)


def _insert_member(text: str, obj: _Node, key: str, value: Any, unit_hint: str) -> str:
    """Add "key": value as the last member of obj, matching the surrounding layout."""
    k = json.dumps(key, ensure_ascii=False)
    base, _ = _line_indent(text, obj.start)
    if obj.members:
        last = obj.members[-1]
        mindent, own_line = _line_indent(text, last.kstart)
        if own_line:   # one member per line
            unit = mindent[len(base):] if mindent.startswith(base) and len(mindent) > len(base) else unit_hint
            piece = f",\n{mindent}{k}: {_render(value, mindent, unit)}"
        else:          # everything on one line
            piece = f", {k}: {_render(value, '', None)}"
        if last.comma >= 0:   # an existing trailing comma: the new member goes after it, with its own comma
            eol = text.find("\n", last.comma)
            eol = len(text) if eol < 0 else eol
            rest = text[last.comma + 1:eol].strip()
            if own_line and (rest == "" or rest.startswith("//")):   # keep a line comment with its member
                at = eol - 1 if text[:eol].endswith("\r") else eol
                return text[:at] + "\n" + piece[2:] + "," + text[at:]
            return text[:last.comma] + piece + text[last.comma:]
        return text[:last.node.end] + piece + text[last.node.end:]
    # empty object
    inner = text[obj.start + 1:obj.end - 1]
    multiline = "\n" in text
    if multiline:
        mindent = base + unit_hint
        body = f"{{\n{mindent}{k}: {_render(value, mindent, unit_hint)}\n{base}}}"
    else:
        body = f"{{{k}: {_render(value, '', None)}}}"
    if inner.strip():   # only comments inside: keep them
        body = body[:-1] + inner.rstrip() + ("\n" + base if multiline else "") + "}"
    return text[:obj.start] + body + text[obj.end:]


def _detect_unit(text: str, root: _Node) -> str:
    if root.members:
        ind, own = _line_indent(text, root.members[0].kstart)
        base, _ = _line_indent(text, root.start)
        if own and len(ind) > len(base):
            return ind[len(base):]
    return "  "


def jsonc_set(text: str, path: list[str], value: Any) -> str:
    """Set path (object keys) to value, creating parent objects as needed."""
    if not text.strip():
        out: Any = value
        for k in reversed(path):
            out = {k: out}
        return json.dumps(out, indent=2, ensure_ascii=False) + "\n"
    root = jsonc_parse(text)
    if root.kind != "obj":
        raise JsoncError("the file isn't a JSON object")
    unit = _detect_unit(text, root)
    node = root
    for depth, key in enumerate(path):
        m = next((m for m in node.members if m.key == key), None)
        if m is None:
            rest: Any = value
            for k in reversed(path[depth + 1:]):
                rest = {k: rest}
            return _insert_member(text, node, key, rest, unit)
        if depth == len(path) - 1:
            base, _ = _line_indent(text, m.kstart)
            single = "\n" not in text[node.start:node.end]
            rendered = _render(value, base, None if single else unit)
            return text[:m.node.start] + rendered + text[m.node.end:]
        if m.node.kind != "obj":
            raise JsoncError(f"\"{key}\" isn't an object")
        node = m.node
    return text


def jsonc_remove(text: str, path: list[str]) -> str:
    """Remove the member at path. A parent object left empty by that goes too (not the root)."""
    if not text.strip():
        return text
    root = jsonc_parse(text)
    node = root
    for key in path[:-1]:
        m = next((m for m in node.members if m.key == key and m.node.kind == "obj"), None)
        if m is None:
            return text
        node = m.node
    idx = next((i for i, m in enumerate(node.members) if m.key == path[-1]), None)
    if idx is None:
        return text
    if len(node.members) == 1 and len(path) > 1:
        return jsonc_remove(text, path[:-1])
    m = node.members[idx]
    if m.comma >= 0:
        # "key": value, plus the comma; when the member had its own line, that whole line
        _, own_line = _line_indent(text, m.kstart)
        start, end = m.kstart, m.comma + 1
        while end < len(text) and text[end] in " \t":
            end += 1
        if own_line and end < len(text) and text[end] in "\r\n":
            start = text.rfind("\n", 0, m.kstart) + 1
            end += 2 if text.startswith("\r\n", end) else 1
        new = text[:start] + text[end:]
    elif idx > 0:   # last member without a trailing comma: drop the comma before it instead
        new = text[:node.members[idx - 1].comma] + text[m.node.end:]
    else:
        new = text[:m.kstart] + text[m.node.end:]
    if len(node.members) == 1:   # the root's only member: leave a tidy empty object
        new = text[:node.start] + "{}" + text[node.end:]
    return new


# ================================================================ client table
def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or HOME / ".codex")


def _entry_json(kind: str, cmd: str, demo: bool) -> dict:
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
        if demo:
            e["env"] = env
        return e
    if kind == "gemini":
        e = {"command": cmd, "args": [], "timeout": 600000}
        if demo:
            e["env"] = env
        return e
    e = {"command": cmd, "args": []}   # cursor, claude
    if demo:
        e["env"] = env
    return e


def _clients() -> list[dict]:
    """id, name, how to detect it, where it keeps MCP servers and how iOS Studio registers there."""
    crush_dir = XDG_CONFIG / "crush"
    opencode_dir = XDG_CONFIG / "opencode"
    oc_file = next((opencode_dir / n for n in ("opencode.jsonc", "opencode.json")
                    if (opencode_dir / n).exists()), opencode_dir / "opencode.json")
    zed_dir = XDG_CONFIG / "zed"
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


def _client(cid: str) -> dict:
    for c in _clients():
        if c["id"] == cid:
            return c
    names = ", ".join(c["id"] for c in _clients())
    raise HelperError("usage", f"Unknown AI tool '{cid}'. Use one of: {names}.")


# ---------------------------------------------------------------- reading registrations
def _read(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _crushrc_entries(text: str) -> list[dict]:
    """`mcp add iosstudio …` lines in a crushrc, parsed like the shell would split them."""
    out = []
    for i, line in enumerate(text.splitlines()):
        s = line.strip()
        if not s.startswith("mcp ") or s.startswith("#"):
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


def _registration(c: dict) -> dict:
    """{registered, command, env, where} as the client would see it. Never changes anything."""
    fmt = c["format"]
    entry: dict | None = None
    where = c["file"]
    if fmt == "json":
        data = jsonc_load(_read(c["file"]))
        node: Any = data
        for k in c["path"]:
            node = node.get(k) if isinstance(node, dict) else None
        entry = node if isinstance(node, dict) else None
        if entry is not None:
            cmd = entry.get("command")
            if isinstance(cmd, list):   # opencode: command is argv
                cmd = cmd[0] if cmd else None
            env = entry.get("env") or entry.get("environment") or {}
            entry = {"command": cmd, "env": env if isinstance(env, dict) else {}}
    elif fmt == "toml":
        text = _read(c["file"])
        data = tomllib.loads(text) if text.strip() else {}
        e = (data.get("mcp_servers") or {}).get(SERVER)
        if isinstance(e, dict):
            entry = {"command": e.get("command"), "env": e.get("env") or {}}
    elif fmt == "crushrc":
        found = _crushrc_entries(_read(c["file"]))
        if found:
            entry = found[-1]
        else:
            data = jsonc_load(_read(c["json_file"])) if c["json_file"].exists() else {}
            e = (data.get("mcp") or {}).get(SERVER) if isinstance(data, dict) else None
            if isinstance(e, dict):
                entry = {"command": e.get("command"), "env": e.get("env") or {}}
                where = c["json_file"]
    if not entry:
        return {"registered": False, "command": None, "demo": False, "where": str(where)}
    cmd = entry.get("command")
    return {"registered": True, "command": cmd, "demo": str((entry.get("env") or {}).get("IOSSTUDIO_DEMO", "")) not in ("", "0"),
            "where": str(where)}


def _snippet(c: dict, cmd: str) -> dict:
    q = shlex.quote(cmd)
    cid = c["id"]
    if cid == "claude":
        return {"kind": "shell", "text": f"claude mcp add --scope user {SERVER} -- {q}"}
    if cid == "gemini":
        return {"kind": "shell", "text": f"gemini mcp add -s user --timeout 600000 {SERVER} {q}"}
    if cid == "codex":
        return {"kind": "toml", "where": _tilde(c["file"]), "text": _toml_block(cmd, False)}
    if cid == "crush":
        return {"kind": "shell", "where": _tilde(c["file"]), "text": _crush_line(cmd, False)}
    entry = _entry_json(c.get("entry", cid), cmd, False)
    wrapped: Any = entry
    for k in reversed(c["path"]):
        wrapped = {k: wrapped}
    return {"kind": "json", "where": _tilde(c["file"]), "text": json.dumps(wrapped, indent=2)}


def _method(c: dict) -> str:
    if c.get("cli") and _which((c["cli"],)):
        return "cli"
    return "file"


def _describe(c: dict) -> dict:
    binary = _which(c["bins"])
    dir_found = next((str(d) for d in c["dirs"] if d.is_dir()), None)
    out: dict = {"id": c["id"], "name": c["name"], "installed": bool(binary or dir_found),
                 "binary": binary, "configDir": dir_found, "file": str(c["file"]),
                 "fileShort": _tilde(c["file"]), "method": _method(c), "restartHint": c["restart"]}
    try:
        reg = _registration(c)
    except (JsoncError, tomllib.TOMLDecodeError, OSError) as exc:
        reg = {"registered": False, "command": None, "demo": False, "where": str(c["file"])}
        out["error"] = {"code": "config-unreadable",
                        "message": f"{_tilde(c['file'])} couldn't be read ({exc}). Fix it by hand, then try again."}
    out.update(reg)
    out["whereShort"] = _tilde(reg.get("where"))
    cmd = reg.get("command")
    out["current"] = bool(reg["registered"] and cmd and _same_path(cmd, _launcher()))
    out["stale"] = bool(reg["registered"] and not out["current"])
    out["snippet"] = _snippet(c, _launcher())
    return out


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(b)
    except (OSError, ValueError):
        return a == b


def server_status() -> dict:
    venv = _venv()
    want = ""
    try:
        want = hashlib.sha256(PYPROJECT.read_bytes()).hexdigest()[:16]
    except OSError:
        pass
    have = _read(venv / ".iosstudio-deps").strip()
    py_ok = os.access(venv / "bin/python", os.X_OK)
    state = "ready" if py_ok and have == want else ("outdated" if py_ok else "missing")
    return {"state": state, "ready": state == "ready", "launcher": _launcher(), "launcherShort": _tilde(_launcher()),
            "venv": str(venv), "venvShort": _tilde(venv), "uv": bool(_which(("uv",))),
            "launcherOk": os.access(LAUNCHER, os.X_OK)}


def list_(flags: dict) -> int:
    return ok({"server": server_status(), "clients": [_describe(c) for c in _clients()]})


# ================================================================ writing
def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _backup(cid: str, path: Path) -> Path | None:
    if not path.exists():
        return None
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS / f"agents-{cid}-{path.name}.{_stamp()}"
    n = 1
    while dest.exists():
        n += 1
        dest = BACKUPS / f"agents-{cid}-{path.name}.{_stamp()}-{n}"
    shutil.copy2(path, dest)
    for old in sorted(BACKUPS.glob(f"agents-{cid}-{path.name}.*"))[:-10]:   # keep the 10 newest
        old.unlink(missing_ok=True)
    return dest


def _diff(path: Path, old: str, new: str) -> str:
    name = _tilde(path)
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), name, name + " (after)"))


def _commit(cid: str, path: Path, old: str, new: str, verify) -> dict:
    """Back up, write atomically (through symlinks, keeping the file mode), re-read and verify;
    put the original back if anything goes wrong."""
    if new == old:
        return {"changed": False, "file": str(path)}
    target = path.resolve() if path.is_symlink() else path
    existed = target.exists()
    mode = (target.stat().st_mode & 0o777) if existed else 0o644
    backup = _backup(cid, target)
    try:
        common.atomic_write(target, new, mode=mode)
        verify(target.read_text())
    except Exception as exc:
        if backup:
            shutil.copy2(backup, target)
        elif not existed:
            target.unlink(missing_ok=True)
        if isinstance(exc, HelperError):
            raise
        raise HelperError("config-write", f"Couldn't update {_tilde(path)}, so it was left as it was.",
                          detail=repr(exc))
    return {"changed": True, "file": str(path), "backup": str(backup) if backup else None}


# ---------------------------------------------------------------- JSON clients
def _json_plan(c: dict, connect: bool, demo: bool) -> tuple[Path, str, str, Any]:
    path = c["file"] if c["format"] == "json" else c["json_file"]
    keypath = c["path"] if c["format"] == "json" else ["mcp", SERVER]
    old = _read(path)
    try:
        before = jsonc_load(old)
    except JsoncError as exc:
        raise HelperError("config-unreadable", f"{_tilde(path)} has a mistake in it ({exc}), so iOS Studio "
                          "won't touch it. Fix it by hand, or use Copy setup snippet.")
    if not isinstance(before, dict):
        raise HelperError("config-unreadable", f"{_tilde(path)} isn't a settings object, so iOS Studio won't touch it.")
    expected = json.loads(json.dumps(before))
    node = expected
    for k in keypath[:-1]:
        if not isinstance(node.get(k), dict):
            if not connect:
                return path, old, old, before
            node[k] = {}
        node = node[k]
    if connect:
        value = _entry_json(c.get("entry", "crush" if c["format"] == "crushrc" else c["id"]), _launcher(), demo)
        node[keypath[-1]] = value
        new = jsonc_set(old, keypath, value)
    else:
        node.pop(keypath[-1], None)
        if not node and len(keypath) > 1:   # jsonc_remove drops a parent it leaves empty
            parent = expected
            for k in keypath[:-2]:
                parent = parent[k]
            parent.pop(keypath[-2], None)
        new = jsonc_remove(old, keypath)
    return path, old, new, expected


def _json_apply(c: dict, connect: bool, demo: bool, dry: bool) -> dict:
    path, old, new, expected = _json_plan(c, connect, demo)
    try:
        got = jsonc_load(new)
    except JsoncError as exc:
        raise HelperError("config-write", f"iOS Studio couldn't work out a safe change to {_tilde(path)}.",
                          detail=str(exc))
    if got != expected:
        raise HelperError("config-write", f"iOS Studio couldn't work out a safe change to {_tilde(path)}, "
                          "so it changed nothing. Use Copy setup snippet to add it by hand.")
    plan = {"method": "file", "file": str(path), "diff": _diff(path, old, new), "changed": new != old}
    if dry:
        return plan

    def verify(text: str) -> None:
        if jsonc_load(text) != expected:
            raise HelperError("config-write", f"{_tilde(path)} didn't read back as expected, so it was restored.")
    plan.update(_commit(c["id"], path, old, new, verify))
    return plan


# ---------------------------------------------------------------- Codex (TOML)
def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)   # a JSON string is a valid TOML basic string


def _toml_block(cmd: str, demo: bool) -> str:
    lines = [f"[mcp_servers.{SERVER}]", f"command = {_toml_str(cmd)}", "args = []",
             "startup_timeout_sec = 30", "tool_timeout_sec = 120"]
    if demo:
        lines.append('env = { IOSSTUDIO_DEMO = "1" }')
    return "\n".join(lines) + "\n"


def _strip_marked(lines: list[str]) -> tuple[list[str], bool]:
    """Remove our marker-delimited block, and the one blank line _append_marked put before it."""
    out: list[str] = []
    inside = found = False
    for line in lines:
        s = line.strip()
        if s == f"# {MARK_START}":
            inside = found = True
            if out and out[-1].strip() == "":
                out.pop()
            continue
        if inside:
            if s == f"# {MARK_END}":
                inside = False
            continue
        out.append(line)
    return out, found


def _append_marked(text: str, body: str) -> str:
    block = f"# {MARK_START}\n{body.rstrip(chr(10))}\n# {MARK_END}\n"
    if not text:
        return block
    return text + ("\n" if text.endswith("\n") else "\n\n") + block


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
    lines, _ = _strip_marked(text.splitlines(True))
    out, skipping = [], False
    for line in lines:
        hp = _toml_header_path(line)
        if hp is not None:
            skipping = hp[:2] == ["mcp_servers", SERVER]
        if not skipping:
            out.append(line)
    return "".join(out)


def _drop_empty_servers(d: dict, key: str) -> dict:
    if d.get(key) == {}:
        d = dict(d)
        d.pop(key)
    return d


def _toml_apply(c: dict, connect: bool, demo: bool, dry: bool) -> dict:
    path = c["file"]
    old = _read(path)
    try:
        before = tomllib.loads(old) if old.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise HelperError("config-unreadable", f"{_tilde(path)} has a mistake in it ({exc}), so iOS Studio "
                          "won't touch it. Fix it by hand, or use Copy setup snippet.")
    expected = json.loads(json.dumps(before, default=str))
    servers = expected.setdefault("mcp_servers", {})
    servers.pop(SERVER, None)
    new = _toml_remove(old)
    if connect:
        entry: dict = {"command": _launcher(), "args": [], "startup_timeout_sec": 30, "tool_timeout_sec": 120}
        if demo:
            entry["env"] = dict(DEMO_ENV)
        servers[SERVER] = entry
        new = _append_marked(new, _toml_block(_launcher(), demo))
    expected = _drop_empty_servers(expected, "mcp_servers")

    def check(text: str) -> None:
        try:
            got = json.loads(json.dumps(tomllib.loads(text), default=str))
        except tomllib.TOMLDecodeError as exc:
            raise HelperError("config-write", f"iOS Studio couldn't work out a safe change to {_tilde(path)}.",
                              detail=str(exc))
        if _drop_empty_servers(got, "mcp_servers") != expected:
            raise HelperError("config-write", f"iOS Studio couldn't work out a safe change to {_tilde(path)} "
                              "(the server may be set up in an unusual way there), so it changed nothing. "
                              "Edit it by hand, or use Copy setup snippet.")
    check(new)
    plan = {"method": "file", "file": str(path), "diff": _diff(path, old, new), "changed": new != old}
    if dry:
        return plan
    plan.update(_commit(c["id"], path, old, new, check))
    return plan


# ---------------------------------------------------------------- Crush (crushrc)
def _crush_line(cmd: str, demo: bool) -> str:
    line = f"mcp add {SERVER} --type stdio --command {shlex.quote(cmd)} --timeout 120"
    if demo:
        line += " --env IOSSTUDIO_DEMO 1"
    return line


def _crush_apply(c: dict, connect: bool, demo: bool, dry: bool) -> dict:
    path = c["file"]
    # Someone using the older crush.json (and no crushrc) keeps using it.
    if c["json_file"].exists() and not path.exists():
        return _json_apply(c, connect, demo, dry)
    old = _read(path)
    lines, _ = _strip_marked(old.splitlines(True))
    stray = {e["line"] for e in _crushrc_entries("".join(lines))}   # hand-written `mcp add iosstudio`
    new = "".join(l for i, l in enumerate(lines) if i not in stray)
    if connect:
        new = _append_marked(new, _crush_line(_launcher(), demo))

    def check(text: str) -> None:
        found = _crushrc_entries(text)
        if connect and (len(found) != 1 or not _same_path(found[0]["command"] or "", _launcher())):
            raise HelperError("config-write", f"{_tilde(path)} didn't read back as expected.")
        if not connect and found:
            raise HelperError("config-write", f"iOS Studio couldn't remove itself from {_tilde(path)}.")
        if shutil.which("bash"):
            r = subprocess.run(["bash", "-n"], input=text, capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                raise HelperError("config-write", f"The change would break {_tilde(path)}, so it wasn't made.",
                                  detail=r.stderr)
    check(new)
    plan = {"method": "file", "file": str(path), "diff": _diff(path, old, new), "changed": new != old}
    if dry:
        return plan
    plan.update(_commit(c["id"], path, old, new, check))
    return plan


# ---------------------------------------------------------------- CLI clients
def _cli_argv(c: dict, connect: bool, demo: bool) -> list[list[str]]:
    exe = _which((c["cli"],)) or c["cli"]
    cmd = _launcher()
    reg = _registration(c)
    steps: list[list[str]] = []
    if c["id"] == "claude":
        if reg["registered"]:
            steps.append([exe, "mcp", "remove", "--scope", "user", SERVER])
        if connect:
            env = ["-e", "IOSSTUDIO_DEMO=1"] if demo else []
            steps.append([exe, "mcp", "add", *env, "--scope", "user", SERVER, "--", cmd])
    elif c["id"] == "gemini":
        if reg["registered"]:
            steps.append([exe, "mcp", "remove", "-s", "user", SERVER])
        if connect:
            steps.append([exe, "mcp", "add", "-s", "user", "--timeout", "600000", SERVER, cmd]
                         + (["-e", "IOSSTUDIO_DEMO=1"] if demo else []))
    return steps


def _cli_apply(c: dict, connect: bool, demo: bool, dry: bool) -> dict:
    steps = _cli_argv(c, connect, demo)
    plan = {"method": "cli", "file": str(c["file"]), "commands": [shlex.join(s) for s in steps],
            "changed": bool(steps)}
    if dry or not steps:
        return plan
    for argv in steps:
        r = _run(argv)
        if r.returncode != 0:
            raise HelperError("agent-cli", f"{c['name']} didn't accept the change.",
                              detail=(shlex.join(argv) + "\n" + (r.stderr or r.stdout or "")).strip())
    reg = _registration(c)
    if reg["registered"] != connect:
        raise HelperError("agent-cli", f"{c['name']} said yes, but iOS Studio can't see the change in "
                          f"{_tilde(c['file'])}.")
    return plan


def _apply(c: dict, connect: bool, flags: dict) -> dict:
    demo = flags.get("demo") is True or str(flags.get("demo", "")).lower() in ("1", "true", "on", "yes")
    dry = bool(flags.get("dry_run"))
    via = str(flags.get("via") or "")   # "file" forces the config-file path (used for testing)
    if not connect and not _registration(c)["registered"]:
        return {"method": "none", "changed": False, "file": str(c["file"])}
    if c.get("cli") and via != "file" and _which((c["cli"],)):
        res = _cli_apply(c, connect, demo, dry)
    elif c["format"] == "toml":
        res = _toml_apply(c, connect, demo, dry)
    elif c["format"] == "crushrc":
        res = _crush_apply(c, connect, demo, dry)
    else:
        res = _json_apply(c, connect, demo, dry)
    res.update({"client": c["id"], "name": c["name"], "dryRun": dry, "connected": connect if not dry else None,
                "demo": demo, "command": _launcher(), "restartHint": c["restart"]})
    return res


def _need_client(flags: dict) -> dict:
    cid = str(flags.get("client") or "")
    if not cid:
        raise HelperError("usage", "Pass --client (claude, codex, opencode, cursor, gemini, crush or zed).")
    return _client(cid)


def connect(flags: dict) -> int:
    c = _need_client(flags)
    if not LAUNCHER.exists():
        raise HelperError("no-launcher", "iOS Studio's MCP server is missing from the app folder.")
    return ok(_apply(c, True, flags))


def disconnect(flags: dict) -> int:
    return ok(_apply(_need_client(flags), False, flags))


# ================================================================ test (stdio JSON-RPC)
class _Rpc:
    def __init__(self, argv: list[str], env: dict):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env, cwd=str(HOME), start_new_session=True)
        self.err: list[str] = []
        self.lines: list[bytes] = []
        self.cv = threading.Condition()
        threading.Thread(target=self._drain_err, daemon=True).start()
        threading.Thread(target=self._read_out, daemon=True).start()

    def _drain_err(self) -> None:
        assert self.p.stderr
        for raw in self.p.stderr:
            self.err.append(raw.decode(errors="replace").rstrip())
            del self.err[:-40]

    def _read_out(self) -> None:
        assert self.p.stdout
        for raw in self.p.stdout:
            with self.cv:
                self.lines.append(raw)
                self.cv.notify_all()
        with self.cv:
            self.cv.notify_all()

    def send(self, obj: dict) -> None:
        assert self.p.stdin
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def wait_for(self, rid: int, deadline: float) -> dict:
        while True:
            with self.cv:
                while not self.lines:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise HelperError("timeout", "The server didn't answer in time.", detail=self.stderr())
                    if self.p.poll() is not None:
                        raise HelperError("server-exited", "The server stopped before it answered.",
                                          detail=self.stderr())
                    self.cv.wait(min(left, 0.5))
                raw = self.lines.pop(0)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                raise HelperError("server-protocol", "The server printed something that isn't MCP.",
                                  detail=raw.decode(errors="replace")[:500])
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise HelperError("server-protocol", "The server rejected the request.",
                                      detail=json.dumps(msg["error"]))
                return msg["result"]

    def stderr(self) -> str:
        return "\n".join(self.err[-20:])

    def close(self) -> None:
        try:
            if self.p.stdin:
                self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            try:
                os.killpg(self.p.pid, 15)
                self.p.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(self.p.pid, 9)
                except Exception:
                    pass


def test(flags: dict) -> int:
    cmd, demo, who = _launcher(), bool(flags.get("demo")), None
    if flags.get("client"):
        c = _client(str(flags["client"]))
        reg = _registration(c)
        if not reg["registered"]:
            raise HelperError("not-connected", f"{c['name']} isn't connected to iOS Studio yet.")
        cmd, demo, who = os.path.expanduser(reg["command"] or ""), demo or reg["demo"], c["name"]
        if not os.access(cmd, os.X_OK):
            raise HelperError("stale", f"{c['name']} points to {_tilde(cmd)}, which doesn't exist anymore. "
                              "Connect it again to fix that.")
    env = os.environ.copy()
    env.pop("IOSSTUDIO_DEMO", None)
    if demo:
        env.update(DEMO_ENV)
    timeout = float(flags.get("timeout") or 180)
    t0 = time.monotonic()
    rpc = _Rpc([cmd], env)
    try:
        deadline = t0 + timeout
        rpc.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                             "clientInfo": {"name": "ios-studio-test", "version": "1"}}})
        init = rpc.wait_for(1, deadline)
        rpc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names: list[str] = []
        cursor, rid = None, 2
        while True:
            rpc.send({"jsonrpc": "2.0", "id": rid, "method": "tools/list",
                      "params": {"cursor": cursor} if cursor else {}})
            res = rpc.wait_for(rid, deadline)
            names += [t.get("name", "") for t in res.get("tools", [])]
            cursor = res.get("nextCursor")
            rid += 1
            if not cursor or rid > 50:
                break
    finally:
        rpc.close()
    info = init.get("serverInfo") or {}
    return ok({"tools": len(names), "names": names, "client": who, "demo": demo,
               "protocol": init.get("protocolVersion"), "server": info.get("name"), "version": info.get("version"),
               "seconds": round(time.monotonic() - t0, 1)})


# ================================================================ prepare (streaming)
def prepare(flags: dict) -> int:
    st = server_status()
    if not st["launcherOk"]:
        return common.done(False, error=HelperError("no-launcher", "iOS Studio's MCP server is missing from the app folder."))
    if flags.get("repair"):
        venv = Path(st["venv"])
        if venv.is_dir() and (venv / "pyvenv.cfg").exists():   # only ever our own venv
            common.step("clean", "Removing the old environment", "running")
            shutil.rmtree(venv, ignore_errors=True)
            common.step("clean", "Removing the old environment", "done")
        st = server_status()
    if st["ready"]:
        common.step("venv", "Server environment", "done", detail="Already set up")
    else:
        common.step("venv", "Setting up the server's Python environment", "running",
                    detail="Downloads about 30 MB the first time" if st["state"] == "missing" else "Updating")
        common.progress(None, "Starting…")
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
                common.progress(None, text[:200])
                common.log(text)
        code = common.stream_cmd([_launcher(), "--prepare"], on_line=on_line)
        if code != 0:
            common.step("venv", "Setting up the server's Python environment", "failed")
            return common.done(False, error=HelperError(
                "prepare-failed", "Couldn't set up the server's Python environment. Check your internet "
                "connection and try again.", fix={"label": "Try again", "page": "settings", "action": "agents-repair"},
                detail="\n".join(lines)))
        common.step("venv", "Setting up the server's Python environment", "done")
    common.step("check", "Checking the server starts", "running")
    common.progress(None, "Starting the server once…")
    venv = Path(server_status()["venv"])
    env = {"PYTHONPATH": str(common.REPO / "mcp"), "PYTHONSAFEPATH": "1", "IOSSTUDIO_REPO": str(common.REPO),
           "PYTHONPYCACHEPREFIX": str(common.CACHE_DIR / "pycache")}
    try:
        r = common.run_cmd([str(venv / "bin/python"), "-c", "import iosstudio_mcp.server"], timeout=60, env=env)
    except HelperError as exc:
        return common.done(False, error=exc)
    if r.returncode != 0:
        common.step("check", "Checking the server starts", "failed")
        return common.done(False, error=HelperError(
            "server-broken", "The server's environment is set up but the server doesn't start.",
            fix={"label": "Rebuild it", "page": "settings", "action": "agents-repair"},
            detail=(r.stderr or r.stdout)[-3000:]))
    common.step("check", "Checking the server starts", "done")
    return common.done(True, server_status())


# ================================================================ demo mode
def demo(action: str, flags: dict) -> int:
    """Made-up answers for `ios-studio --demo` (screenshots): nothing is read or changed."""
    launcher = "~/.local/share/omarchy-ios-studio/bin/iosstudio-mcp"
    state_file = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "ios-studio-demo" / "agents.json"
    connected = set(common.load_json(state_file, ["claude"]) or [])
    installed = {"claude", "codex", "cursor", "zed"}
    if action in ("connect", "disconnect"):
        cid = str(flags.get("client") or "")
        if not flags.get("dry_run"):
            (connected.add if action == "connect" else connected.discard)(cid)
            common.save_json(state_file, sorted(connected))
        return ok({"client": cid, "demo": True, "connected": action == "connect", "method": "demo",
                   "restartHint": "Demo mode: nothing was changed."})
    if action == "test":
        return ok({"tools": 34, "names": [], "demo": True, "seconds": 0.4})
    if action == "prepare":
        common.step("venv", "Server environment", "done", detail="Already set up")
        return common.done(True, {"state": "ready", "ready": True})
    clients = []
    for c in _clients():
        cid = c["id"]
        clients.append({"id": cid, "name": c["name"], "installed": cid in installed, "method": _method(c),
                        "registered": cid in connected, "current": cid in connected, "stale": False, "demo": False,
                        "command": launcher if cid in connected else None, "fileShort": _tilde(c["file"]),
                        "whereShort": _tilde(c["file"]), "restartHint": c["restart"],
                        "snippet": {"kind": "shell", "text": f"# demo mode\n{launcher}"}})
    return ok({"server": {"state": "ready", "ready": True, "launcherShort": launcher, "launcher": launcher,
                          "venvShort": "~/.local/share/ios-studio/mcp-venv", "uv": True, "launcherOk": True},
               "clients": clients})


GROUPS = {
    "agents": {
        "list": list_,
        "connect": connect,
        "disconnect": disconnect,
        "test": test,
        "prepare": prepare,
    }
}
