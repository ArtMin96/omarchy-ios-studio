"""Editor setup: Swift completion and errors in VS Code, Zed or Neovim for xtool projects.

SourceKit-LSP (the Swift language server) understands an xtool project through xtool's build server: the
file `.bsp/xtool.json` in the project tells it to start `xtool dev build-server`, which compiles for the
iPhone (arm64-apple-ios) instead of for Linux. Two parts:

1. Machine-wide, once per editor (`editor setup`): the editor's Swift support. Neovim: LazyVim's
   `lang.swift` extra in lazyvim.json when the installed LazyVim has one (backed up first), else a new plugin
   file `lua/plugins/ios-studio-swift.lua`; other Neovim configs get a snippet (docs/features/editor-setup.md).
   Zed and VS Code: their Swift extension (a step you do; status says whether it's there), plus
   sourcekit-lsp's path in the editor's user settings only when it isn't on PATH. Also remembers the editor
   in iOS Studio's settings.json (`editorSetup`).
2. Per project, automatic (auto_project, called after a successful build and on create / add / convert):
   writes `.bsp/xtool.json` when it's missing (the same file xtool writes on its first build) and, when the
   preferred editor is VS Code and iOS Studio created the project from a template, merges
   `.vscode/settings.json` and `.vscode/extensions.json` (added or converted projects get them only through
   `--fix`). Zed and Neovim need nothing in the project.

  editor status
      -> {swift, xtool, sourcekit_lsp, preferred, editors: [{id, name, installed, binary, state, extension,
          configured, files, note, notes}]}
  editor setup --editor vscode|zed|neovim [--confirm] [--prefer no]
      -> {editor, name, dryRun, actions: [{kind, path, scope, detail, keys?, kept?, preview?, done?}], applied,
          state, next, notes}
  editor status --project P [--fix [--editor E] [--confirm]]
      -> {project, bsp: {present, stale, reason, path, kind, built, note}, ready, hint, preferred, created,
          auto: {at, wrote: [{kind, path}]} | null, pending: [actions], fix?: {dryRun, actions, applied, next}}

state: ready, needs-setup (setup can do the rest), partial (a step only you can do remains, see notes:
install the Swift extension, a file it couldn't read, a non-LazyVim Neovim config), not-installed.

Action kinds: write (a new file), merge (missing keys added to an existing file; every value already
there is kept, and differing ones are listed in `kept`), skip (nothing to do, or a file it can't change
safely), backup (a copy of a user-level file before it's changed). scope: "project" or "user" (a file in
your home folder: editor config, iOS Studio's settings).

Files are never overwritten. JSON with comments and trailing commas (JSONC, what VS Code and Zed use) is
edited as text: new keys and list items are inserted, everything else stays byte for byte.

Demo mode (IOSSTUDIO_DEMO): made-up status; setup and --fix list their plan and write nothing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, fileio, output
from iosstudio.core.output import HelperError

EDITORS = {"vscode": "VS Code", "zed": "Zed", "neovim": "Neovim"}

# Exactly what xtool writes (Sources/XToolSupport/LSPConfig.swift): the BSP file for Swift 6.4+, the older
# SourceKit-LSP config before that. xtool only writes the one that's missing, so matching it keeps a later
# `xtool dev build` quiet.
BSP_REL = ".bsp/xtool.json"
BSP_TEXT = """{
    "name": "xtool",
    "version": "1.0",
    "bspVersion": "2.2.0",
    "languages": ["c", "cpp", "objective-c", "objective-cpp", "swift"],
    "argv": ["/usr/bin/env", "xtool", "dev", "build-server"]
}
"""
LEGACY_REL = ".sourcekit-lsp/config.json"
LEGACY_TEXT = """{
    "swiftPM": {
        "swiftSDK": "arm64-apple-ios"
    }
}
"""
BSP_DETAIL = "Lets the Swift language server build this project for the iPhone (xtool writes the same file)."
BSP_MIN_SWIFT = (6, 4)   # `xtool dev build-server` needs Swift 6.4 (swift-build)

VSCODE_EXT = "swiftlang.swift-vscode"
VSCODE_OLD_EXT = "sswg.swift-lang"
VSCODE_SETTINGS = ".vscode/settings.json"
VSCODE_EXTENSIONS = ".vscode/extensions.json"
LAZY_EXTRA = "lazyvim.plugins.extras.lang.swift"
NVIM_PLUGIN_REL = "lua/plugins/ios-studio-swift.lua"
DOCS = "docs/features/editor-setup.md"
NVIM_PLUGIN_DETAIL = "Turns on Swift completion (sourcekit-lsp) for every project. Delete the file to undo."

MAX_JSON = 512 * 1024        # bigger editor settings files are skipped, not parsed
NVIM_SCAN_FILES = 300        # lua files read when looking for a sourcekit setup
NVIM_SCAN_BYTES = 256 * 1024


# ================================================================ environment

@dataclass
class Env:
    """Where to look: home, XDG config/data dirs and PATH. Tests pass fake ones; the CLI uses the real ones."""
    home: Path
    config: Path
    data: Path
    path: str
    state: Path | None = None          # iOS Studio's state dir (default: <config>/../.local/state/ios-studio)
    swift_version: tuple[int, int] | None = None
    probe_swift: bool = True
    _probed: bool = field(default=False, repr=False)

    @classmethod
    def from_os(cls) -> "Env":
        home = Path(os.environ.get("HOME") or Path.home())
        return cls(home=home, config=Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config"),
                   data=Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share"),
                   path=os.environ.get("PATH", "/usr/bin"),
                   state=Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / "ios-studio")

    def __post_init__(self) -> None:
        if self.state is None:
            self.state = self.home / ".local/state/ios-studio"

    @property
    def settings_file(self) -> Path:
        return self.config / "ios-studio/settings.json"

    def which(self, name: str) -> str | None:
        return shutil.which(name, path=self.path)

    def swift(self) -> tuple[int, int] | None:
        if self.swift_version is None and self.probe_swift and not self._probed:
            self._probed = True
            self.swift_version = _swift_version(self.which("swift"))
        return self.swift_version


def _swift_version(swift: str | None) -> tuple[int, int] | None:
    """(major, minor) from `swift --version`, cached per binary (it takes ~0.5 s)."""
    if not swift:
        return None
    try:
        st = os.stat(swift)
        key = f"{os.path.realpath(swift)}:{int(st.st_mtime)}"
    except OSError:
        return None
    hit = appdirs.cache_get("editor-swift-version")
    if hit and isinstance(hit.get("data"), dict) and hit["data"].get("key") == key and hit["data"].get("v"):
        return tuple(hit["data"]["v"])[:2]  # type: ignore[return-value]
    try:
        out = subprocess.run([swift, "--version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Swift version (\d+)\.(\d+)", out)
    if not m:
        return None
    v = (int(m.group(1)), int(m.group(2)))
    try:
        appdirs.cache_put("editor-swift-version", {"key": key, "v": list(v)})
    except OSError:
        pass
    return v


def _flatpak(env: Env, app_id: str) -> bool:
    return any((d / app_id).is_dir() for d in (env.data / "flatpak/app", Path("/var/lib/flatpak/app")))


# ================================================================ JSONC: parse and edit as text

class JsoncError(ValueError):
    pass


def _line_of(text: str, i: int) -> int:
    return text.count("\n", 0, max(0, i)) + 1


def strip_jsonc(text: str) -> tuple[str, bool, set[int]]:
    """(clean, had_comments, trailing_comma_offsets). `clean` has the same length as `text`: comments,
    trailing commas and a BOM become spaces (newlines are kept), so offsets in it are offsets in `text`."""
    out = list(text)
    n = len(text)
    comments = False
    if text.startswith("﻿"):
        out[0] = " "
    i = 0
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            comments = True
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end < 0:
                raise JsoncError(f"a /* comment */ that never ends (line {_line_of(text, i)})")
            comments = True
            for j in range(i, end + 2):
                if text[j] != "\n":
                    out[j] = " "
            i = end + 2
            continue
        i += 1
    clean = "".join(out)
    # trailing commas: a comma (outside strings) whose next non-space character closes the object or list
    trailing: set[int] = set()
    in_str = False
    i = 0
    while i < n:
        c = clean[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == ",":
            j = i + 1
            while j < n and clean[j] in " \t\r\n":
                j += 1
            if j < n and clean[j] in "}]":
                trailing.add(i)
        i += 1
    if trailing:
        out = list(clean)
        for i in trailing:
            out[i] = " "
        clean = "".join(out)
    return clean, comments, trailing


@dataclass
class Node:
    kind: str                 # obj | arr | val
    start: int
    end: int                  # one past the closing brace / bracket / value
    value: Any = None
    members: list[tuple[str, "Node"]] = field(default_factory=list)   # obj
    items: list["Node"] = field(default_factory=list)                  # arr


_DECODER = json.JSONDecoder()
_WS = " \t\r\n"


def _skip(s: str, i: int) -> int:
    while i < len(s) and s[i] in _WS:
        i += 1
    return i


def _parse(s: str, i: int, depth: int = 0) -> Node:
    if depth > 64:
        raise JsoncError("nested too deeply")
    i = _skip(s, i)
    if i >= len(s):
        raise JsoncError("ends too early")
    c = s[i]
    if c == "{":
        node = Node("obj", i, i, {})
        j = _skip(s, i + 1)
        if j < len(s) and s[j] == "}":
            node.end = j + 1
            return node
        while True:
            j = _skip(s, j)
            if j >= len(s) or s[j] != '"':
                raise JsoncError(f"expected a \"key\" (line {_line_of(s, j)})")
            try:
                key, j = _DECODER.raw_decode(s, j)
            except json.JSONDecodeError as exc:
                raise JsoncError(f"{exc.msg} (line {exc.lineno})") from None
            j = _skip(s, j)
            if j >= len(s) or s[j] != ":":
                raise JsoncError(f"expected ':' (line {_line_of(s, j)})")
            child = _parse(s, j + 1, depth + 1)
            node.members.append((key, child))
            node.value[key] = child.value
            j = _skip(s, child.end)
            if j < len(s) and s[j] == ",":
                j += 1
                continue
            if j < len(s) and s[j] == "}":
                node.end = j + 1
                return node
            raise JsoncError(f"expected ',' or '}}' (line {_line_of(s, j)})")
    if c == "[":
        node = Node("arr", i, i, [])
        j = _skip(s, i + 1)
        if j < len(s) and s[j] == "]":
            node.end = j + 1
            return node
        while True:
            child = _parse(s, j, depth + 1)
            node.items.append(child)
            node.value.append(child.value)
            j = _skip(s, child.end)
            if j < len(s) and s[j] == ",":
                j += 1
                continue
            if j < len(s) and s[j] == "]":
                node.end = j + 1
                return node
            raise JsoncError(f"expected ',' or ']' (line {_line_of(s, j)})")
    try:
        value, end = _DECODER.raw_decode(s, i)
    except json.JSONDecodeError as exc:
        raise JsoncError(f"{exc.msg} (line {exc.lineno})") from None
    if isinstance(value, (dict, list)):   # raw_decode only sees containers through the branches above
        raise JsoncError(f"unexpected value (line {_line_of(s, i)})")
    return Node("val", i, end, value)


def parse_jsonc(text: str) -> tuple[Node, str, bool, set[int]]:
    """Parse JSON with comments and trailing commas. Returns (root node, clean text, had_comments,
    trailing comma offsets). Raises JsoncError."""
    clean, comments, trailing = strip_jsonc(text)
    root = _parse(clean, 0)
    rest = _skip(clean, root.end)
    if rest < len(clean):
        raise JsoncError(f"extra text after the end (line {_line_of(clean, rest)})")
    return root, clean, comments, trailing


def _indent_unit(text: str) -> str:
    """The file's indent step: a tab, or the smallest run of leading spaces (default 2)."""
    sizes = []
    for line in text.splitlines():
        m = re.match(r"^([ \t]+)\S", line)
        if m:
            if "\t" in m.group(1):
                return "\t"
            sizes.append(len(m.group(1)))
    return " " * (min(sizes) if sizes else 2)


def _line_indent(text: str, i: int) -> str:
    ls = text.rfind("\n", 0, i) + 1
    m = re.match(r"[ \t]*", text[ls:i])
    return m.group(0) if m else ""


def _render(value: Any, indent: str, unit: str) -> str:
    s = json.dumps(value, indent=unit, ensure_ascii=False)
    return s.replace("\n", "\n" + indent)


def _key_label(path: list[str]) -> str:
    return " › ".join(path)


@dataclass
class _Merge:
    edits: list[tuple] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    kept: list[dict] = field(default_factory=list)


def _append(text: str, clean: str, trailing: set[int], node: Node, new: list[tuple[str | None, Any]],
            unit: str) -> list[tuple]:
    """Text edits that add `new` members (key, value) to an object node, or items (None, value) to a list node,
    in the file's own layout."""
    close = node.end - 1
    children = [c for _, c in node.members] if node.kind == "obj" else node.items
    multiline = "\n" in text[node.start:node.end]

    def entry(k: str | None, v: Any, ind: str) -> str:
        return (json.dumps(k, ensure_ascii=False) + ": " if k is not None else "") + _render(v, ind, unit)

    if children:
        first = node.members[0][1].start if node.kind == "obj" else children[0].start
        if node.kind == "obj":   # the key starts the member's line, not its value
            first = clean.rfind('"', node.start, first)
            first = clean.rfind('"', node.start, first)
        ind = _line_indent(text, first) if text[text.rfind("\n", 0, first) + 1:first].strip() == "" \
            else _line_indent(text, node.start) + unit
    else:
        ind = _line_indent(text, node.start) + unit
    if not multiline:
        if not children and node.start == _skip(clean, 0):   # an empty root {}: lay it out
            outer = _line_indent(text, node.start)
            return [(close, "\n" + ",\n".join(ind + entry(k, v, ind) for k, v in new) + "\n" + outer)]

        def compact(k: str | None, v: Any) -> str:   # stays on the one line
            return (json.dumps(k, ensure_ascii=False) + ": " if k is not None else "") + \
                json.dumps(v, ensure_ascii=False, separators=(", ", ": "))
        if not children:
            return [(close, ", ".join(compact(k, v) for k, v in new))]
        return [(children[-1].end, "".join(", " + compact(k, v) for k, v in new))]
    had_trailing = bool(children) and any(p in trailing for p in range(children[-1].end, close))
    lines = [ind + entry(k, v, ind) for k, v in new]
    ls = text.rfind("\n", 0, close) + 1
    edits: list[tuple] = []
    if children and not had_trailing:
        edits.append((children[-1].end, ","))
    if not children and text[node.start + 1:close].strip() == "":
        # an empty list or object spread over lines: replace the blank inside
        outer = _line_indent(text, node.start)
        return [(node.start + 1, close, "\n" + ",\n".join(lines) + "\n" + outer)]
    if text[ls:close].strip() == "":
        block = ",\n".join(lines) + ("," if had_trailing else "") + "\n"
        edits.append((ls, block))
    else:
        edits.append((close, "\n" + ",\n".join(lines) + "\n" + _line_indent(text, node.start)))
    return edits


def _merge_node(text: str, clean: str, trailing: set[int], node: Node, want: Any, path: list[str],
                unit: str, m: _Merge) -> None:
    if node.kind == "obj" and isinstance(want, dict):
        have = dict(node.members)
        new: list[tuple[str | None, Any]] = []
        for k, v in want.items():
            if k not in have:
                new.append((k, v))
                m.added.append(_key_label(path + [k]))
            else:
                _merge_node(text, clean, trailing, have[k], v, path + [k], unit, m)
        if new:
            m.edits.extend(_append(text, clean, trailing, node, new, unit))
        return
    if node.kind == "arr" and isinstance(want, list):
        new = [(None, v) for v in want if v not in node.value]
        if new:
            m.added.extend(_key_label(path) + " + " + json.dumps(v, ensure_ascii=False) for _, v in new)
            m.edits.extend(_append(text, clean, trailing, node, new, unit))
        return
    if node.value != want:
        m.kept.append({"key": _key_label(path), "yours": node.value, "ours": want})


def _apply_edits(text: str, edits: list[tuple]) -> str:
    """Apply (offset, text) insertions and (start, end, text) replacements, all given in offsets of `text`.
    Insertions at one offset keep their order (a comma, then the new lines)."""
    joined: dict[int, list] = {}
    for e in edits:
        start, end, ins = (e[0], e[0], e[1]) if len(e) == 2 else e
        cur = joined.get(start)
        joined[start] = [max(end, cur[0]) if cur else end, (cur[1] if cur else "") + ins]
    out = text
    for start in sorted(joined, reverse=True):
        end, ins = joined[start]
        out = out[:start] + ins + out[end:]
    return out


def merge_text(text: str, want: dict) -> dict:
    """Merge `want` into a JSON/JSONC document without overwriting anything.

    Returns {status, text?, added, kept, reason?}: status 'unchanged' (everything is already there; `kept`
    lists values that differ and stay), 'merged' (`text` is the new document: only insertions), or 'skip'
    (`reason`: not valid JSON, not an object). An empty file counts as {}."""
    if not text.strip():
        return {"status": "merged", "text": json.dumps(want, indent=2, ensure_ascii=False) + "\n",
                "added": [_key_label([k]) for k in want], "kept": []}
    try:
        root, clean, _comments, trailing = parse_jsonc(text)
    except JsoncError as exc:
        return {"status": "skip", "reason": f"isn't valid JSON: {exc}", "added": [], "kept": []}
    if root.kind != "obj":
        return {"status": "skip", "reason": "isn't a JSON object ({ … })", "added": [], "kept": []}
    m = _Merge()
    _merge_node(text, clean, trailing, root, want, [], _indent_unit(text), m)
    if not m.edits:
        return {"status": "unchanged", "added": [], "kept": m.kept}
    new_text = _apply_edits(text, m.edits)
    try:   # belt and braces: the result must parse and contain everything we meant to add
        check, _, _, _ = parse_jsonc(new_text)
    except JsoncError as exc:
        return {"status": "skip", "reason": f"couldn't be merged safely ({exc}); add the keys by hand",
                "added": [], "kept": m.kept}
    if _missing(check.value, want, root.value):
        return {"status": "skip", "reason": "couldn't be merged safely; add the keys by hand", "added": [],
                "kept": m.kept}
    return {"status": "merged", "text": new_text, "added": m.added, "kept": m.kept}


def _missing(got: Any, want: Any, before: Any) -> bool:
    """True when a key or item we wanted to add isn't in `got`, or a value that was there changed."""
    if isinstance(want, dict) and isinstance(got, dict):
        b = before if isinstance(before, dict) else {}
        for k, v in want.items():
            if k not in got:
                return True
            if k in b and not isinstance(v, (dict, list)) and got[k] != b[k]:
                return True
            if _missing(got[k], v, b.get(k)):
                return True
        return any(got.get(k) != v for k, v in b.items() if k not in want)
    if isinstance(want, list) and isinstance(got, list):
        return any(v not in got for v in want) or (isinstance(before, list) and got[:len(before)] != before)
    return False


def _read_json_file(path: Path) -> tuple[str | None, str | None]:
    """(text, problem). text None + problem None: the file doesn't exist."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"can't be read ({exc.strerror})"
    if not path.is_file():
        return None, "isn't a file"
    if st.st_size > MAX_JSON:
        return None, f"is larger than {MAX_JSON // 1024} KB"
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"can't be read ({exc})"


def json_action(root: Path, rel: str, want: dict, scope: str = "project", what: str = "") -> dict:
    """The write/merge/skip action that puts `want` into root/rel."""
    path = root / rel
    act: dict[str, Any] = {"path": str(path), "scope": scope}
    text, problem = _read_json_file(path)
    if problem:
        return {**act, "kind": "skip", "detail": f"The file {problem}, so it's left alone."}
    if text is None:
        body = json.dumps(want, indent=2, ensure_ascii=False) + "\n"
        return {**act, "kind": "write", "detail": what or "New file.",
                "keys": [_key_label([k]) for k in want], "preview": body, "_text": body}
    res = merge_text(text, want)
    kept = res.get("kept") or []
    if res["status"] == "skip":
        return {**act, "kind": "skip", "detail": f"The file {res['reason']}, so it's left alone.", "kept": kept}
    if res["status"] == "unchanged":
        return {**act, "kind": "skip", "detail": "Already set up." + _kept_note(kept), "kept": kept,
                "configured": True}
    n = len(res["added"])
    d = f"Adds {n} missing setting{'s' if n != 1 else ''}; everything already there stays." + _kept_note(kept)
    return {**act, "kind": "merge", "detail": d, "keys": res["added"], "kept": kept, "_text": res["text"]}


def _kept_note(kept: list) -> str:
    n = len(kept)
    return f" Your own value for {n} setting{'s' if n != 1 else ''} is kept." if n else ""


# ================================================================ the build server file (.bsp)

def _xtool_yml_skip_lsp(project: Path) -> bool:
    try:
        text = (project / "xtool.yml").read_text(errors="replace")
    except OSError:
        return False
    return bool(re.search(r"(?m)^skipLSP:\s*(true|yes|on)\s*(#.*)?$", text))


SDK_REASON = "xtool's iOS SDK isn't set up: finish it on the Toolchain page"


def ios_sdk(env: Env) -> str | None:
    """xtool's Darwin Swift SDK bundle (the same places setup.sdk_bundle_candidates checks), or None."""
    for d in (env.config / "swiftpm/swift-sdks/darwin.artifactbundle",
              env.home / ".config/swiftpm/swift-sdks/darwin.artifactbundle",
              env.home / ".swiftpm/swift-sdks/darwin.artifactbundle"):
        if d.is_dir():
            return str(d)
    return None


def bsp_status(project: Path, env: Env) -> dict:
    """Is the file SourceKit-LSP needs there, and does it still work? kind: 'bsp' (.bsp/xtool.json, Swift
    6.4+) or 'legacy' (.sourcekit-lsp/config.json, older Swift)."""
    ver = env.swift()
    kind = "legacy" if ver is not None and ver < BSP_MIN_SWIFT else "bsp"
    rel = BSP_REL if kind == "bsp" else LEGACY_REL
    path = project / rel
    xtool = env.which("xtool")
    out: dict[str, Any] = {"present": path.is_file(), "path": str(path), "kind": kind, "stale": False,
                           "reason": None, "built": (project / ".build").is_dir(), "xtoolOnPath": bool(xtool),
                           "skipLSP": _xtool_yml_skip_lsp(project), "note": None}
    if out["present"]:
        text, problem = _read_json_file(path)
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            data = None
        if kind == "bsp":
            argv = data.get("argv") if isinstance(data, dict) else None
            if problem or not isinstance(argv, list) or not argv:
                out.update(stale=True, reason=f"{rel} isn't a build server file iOS Studio can read")
            elif "build-server" not in [str(a) for a in argv]:
                out.update(stale=True, reason=f"{rel} starts {' '.join(map(str, argv))[:80]}, not xtool's "
                                              "build server")
            elif "xtool" in [str(a) for a in argv] and not xtool:
                if (env.home / ".local/bin/xtool").is_file():
                    out["note"] = ("xtool is in ~/.local/bin, which isn't on this PATH: start your editor from a "
                                   "shell that has it, or it can't start xtool's build server.")
                else:
                    out.update(stale=True, reason="xtool isn't on PATH, so the editor can't start xtool's build "
                                                  "server")
        elif problem or not isinstance(data, dict):
            out.update(stale=True, reason=f"{rel} isn't valid JSON")
        if kind == "bsp" and not out["stale"] and not ios_sdk(env):
            # without it `xtool dev build-server` exits at once and editors only say "Failed to initialize
            # build server: JSON-RPC connection closed"
            out.update(stale=True, sdkMissing=True, reason=SDK_REASON)
    if kind == "legacy" and (project / BSP_REL).is_file():
        out["note"] = (f"Swift {ver[0]}.{ver[1]} is older than 6.4, so xtool's build server can't run; "  # type: ignore[index]
                       f"SourceKit-LSP uses {LEGACY_REL} instead.")
    elif not out["built"] and not out["note"]:
        out["note"] = "Build once so the Swift packages are fetched; until then completion for them is missing."
    return out


def bsp_action(project: Path, env: Env) -> dict:
    st = bsp_status(project, env)
    act = {"path": st["path"], "scope": "project"}
    if st["skipLSP"] and not st["present"]:
        return {**act, "kind": "skip", "detail": "Turned off by skipLSP: true in xtool.yml. Remove that line to "
                "get completion."}
    if st["present"] and st.get("sdkMissing"):
        return {**act, "kind": "skip", "detail": f"Not usable yet: {SDK_REASON}."}
    if st["present"] and st["stale"]:
        return {**act, "kind": "skip", "detail": f"Stale: {st['reason']}. It's left as it is; delete it "
                "and build again."}
    if st["present"]:
        return {**act, "kind": "skip", "detail": "Already there.", "configured": True}
    text = BSP_TEXT if st["kind"] == "bsp" else LEGACY_TEXT
    return {**act, "kind": "write", "detail": BSP_DETAIL, "preview": text, "_text": text}


# ================================================================ editors

def sourcekit(env: Env) -> str | None:
    return env.which("sourcekit-lsp")


def _sourcekit_fallback(env: Env) -> str | None:
    """sourcekit-lsp next to the real swift binary (a toolchain that isn't on PATH as a whole)."""
    sw = env.which("swift")
    if not sw:
        return None
    cand = Path(os.path.realpath(sw)).parent / "sourcekit-lsp"
    return str(cand) if cand.is_file() and os.access(cand, os.X_OK) else None


def _sourcekit_for_settings(env: Env) -> str | None:
    """A sourcekit-lsp path to write into editor settings: only when it isn't on PATH (on PATH, every editor
    finds it, and a machine path in a shared settings file breaks it for others)."""
    return None if sourcekit(env) else _sourcekit_fallback(env)


# ---------------------------------------------------------------- VS Code

def vscode_installed(env: Env) -> str | None:
    for b in ("code", "code-oss", "codium", "vscodium", "code-insiders"):
        p = env.which(b)
        if p:
            return p
    for app in ("com.visualstudio.code", "com.vscodium.codium"):
        if _flatpak(env, app):
            return "flatpak:" + app
    return None


def vscode_extension(env: Env) -> str | None:
    for d in (env.home / ".vscode/extensions", env.home / ".vscode-oss/extensions",
              env.home / ".var/app/com.visualstudio.code/data/vscode/extensions",
              env.home / ".var/app/com.vscodium.codium/data/codium/extensions"):
        for prefix in (VSCODE_EXT, VSCODE_OLD_EXT):
            try:
                hit = next(iter(sorted(d.glob(prefix + "-*"))), None)
            except OSError:
                hit = None
            if hit:
                return str(hit)
    return None


def vscode_user_settings(env: Env) -> Path:
    """VS Code's own (user) settings.json for the installed flavour."""
    b = Path(vscode_installed(env) or "code").name
    folder = {"code-oss": "Code - OSS", "codium": "VSCodium", "vscodium": "VSCodium",
              "code-insiders": "Code - Insiders"}.get(b, "Code")
    return env.config / folder / "User/settings.json"


# Project settings: portable (no machine paths), so .vscode/ can be committed.
VSCODE_PROJECT_SETTINGS = {
    # Host executables don't exist for an iPhone app; a debug launch.json is a separate step (lldb-dap)
    "swift.autoGenerateLaunchConfigurations": False,
    # .build holds tens of thousands of files that change on every build
    "files.watcherExclude": {"**/.build/**": True},
}
VSCODE_PROJECT_EXTENSIONS = {"recommendations": [VSCODE_EXT]}


def vscode_project_actions(project: Path) -> list[dict]:
    acts = [json_action(project, VSCODE_SETTINGS, VSCODE_PROJECT_SETTINGS,
                        what="Swift settings for this project (no machine paths, safe to commit).")]
    rel = VSCODE_EXTENSIONS
    text, _ = _read_json_file(project / rel)
    unwanted: Any = []
    if text:
        try:
            v = parse_jsonc(text)[0].value
            unwanted = (v.get("unwantedRecommendations") or []) if isinstance(v, dict) else []
        except JsoncError:
            pass
    if isinstance(unwanted, list) and VSCODE_EXT in unwanted:
        acts.append({"path": str(project / rel), "scope": "project", "kind": "skip",
                     "detail": "The Swift extension is on this project's unwanted list; that's kept.",
                     "kept": [{"key": "unwantedRecommendations", "yours": unwanted, "ours": None}],
                     "configured": True})
    else:
        acts.append(json_action(project, rel, VSCODE_PROJECT_EXTENSIONS,
                                what="VS Code offers the Swift extension when the folder opens."))
    return acts


def _user_json(path: Path, want: dict, what: str) -> list[dict]:
    """A merge into a user-level JSON file, preceded by a backup action."""
    act = json_action(path.parent, path.name, want, scope="user", what=what)
    if act["kind"] != "merge":
        return [act]
    bak = f"{path}.bak.{int(time.time())}"
    return [{"path": bak, "scope": "user", "kind": "backup", "detail": f"A copy of {path.name} before it changes.",
             "_from": str(path)}, act]


def vscode_machine_actions(env: Env) -> list[dict]:
    sk = _sourcekit_for_settings(env)
    if not sk:
        return []
    return _user_json(vscode_user_settings(env), {"swift.sourcekit-lsp.serverPath": sk},
                      "Tells the editor where sourcekit-lsp is (it isn't on PATH).")


# ---------------------------------------------------------------- Zed
# Zed needs no project file: its Swift extension starts the sourcekit-lsp it finds on PATH (else
# lsp.sourcekit-lsp.binary.path), and sourcekit-lsp finds .bsp/xtool.json in the opened folder itself.

def zed_installed(env: Env) -> str | None:
    for b in ("zeditor", "zed", "zedit", "zed-editor"):
        p = env.which(b)
        if p:
            return p
    return "flatpak:dev.zed.Zed" if _flatpak(env, "dev.zed.Zed") else None


def zed_extension(env: Env) -> str | None:
    for d in (env.data / "zed/extensions/installed/swift",
              env.home / ".var/app/dev.zed.Zed/data/zed/extensions/installed/swift"):
        if d.is_dir():
            return str(d)
    return None


def zed_machine_actions(env: Env) -> list[dict]:
    sk = _sourcekit_for_settings(env)
    if not sk:
        return []
    return _user_json(env.config / "zed/settings.json", {"lsp": {"sourcekit-lsp": {"binary": {"path": sk}}}},
                      "Tells the editor where sourcekit-lsp is (it isn't on PATH).")


# ---------------------------------------------------------------- Neovim

def nvim_dirs(env: Env) -> tuple[Path, Path]:
    """(config dir, LazyVim checkout) honouring NVIM_APPNAME."""
    app = os.environ.get("NVIM_APPNAME") or "nvim"
    return env.config / app, env.data / app / "lazy/LazyVim"


def _nvim_lua_files(cfg: Path) -> list[Path]:
    files: list[Path] = []
    for name in ("init.lua", "init.vim"):
        if (cfg / name).is_file():
            files.append(cfg / name)
    for sub in ("lua", "plugin", "after", "lsp"):
        d = cfg / sub
        if not d.is_dir():
            continue
        for root, dirs, names in os.walk(d):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for n in sorted(names):
                if n.endswith((".lua", ".vim")):
                    files.append(Path(root) / n)
                    if len(files) >= NVIM_SCAN_FILES:
                        return files
    return files


_SOURCEKIT_RX = re.compile(r"sourcekit|lang\.swift")


def nvim_status(env: Env) -> dict:
    cfg, lazy = nvim_dirs(env)
    lazyvim_json = cfg / "lazyvim.json"
    is_lazy = lazyvim_json.is_file() or (lazy / "lua/lazyvim").is_dir()
    extra_file = lazy / "lua/lazyvim/plugins/extras/lang/swift.lua"
    extras: list = []
    lj_problem = None
    if lazyvim_json.is_file():
        text, lj_problem = _read_json_file(lazyvim_json)
        if text is not None:
            try:
                data = json.loads(text)
                extras = data.get("extras") if isinstance(data, dict) and isinstance(data.get("extras"), list) else []
            except json.JSONDecodeError:
                lj_problem = "isn't valid JSON"
    found = None
    if LAZY_EXTRA in extras:
        found = str(lazyvim_json)
    else:
        for f in _nvim_lua_files(cfg):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(NVIM_SCAN_BYTES)
            except OSError:
                continue
            for line in text.splitlines():
                code = line.split("--", 1)[0] if f.suffix == ".lua" else line.split('"', 1)[0]
                if _SOURCEKIT_RX.search(code):
                    found = str(f)
                    break
            if found:
                break
    return {"config": str(cfg), "configExists": cfg.is_dir(), "lazyvim": is_lazy,
            "lazyvimJson": str(lazyvim_json) if lazyvim_json.is_file() else None, "lazyvimJsonProblem": lj_problem,
            "extraAvailable": extra_file.is_file(), "extraEnabled": LAZY_EXTRA in extras, "swiftLsp": found,
            "pluginFile": str(cfg / NVIM_PLUGIN_REL)}


def nvim_plugin_text(env: Env) -> str:
    sk = _sourcekit_for_settings(env)
    cmd = f"\n          cmd = {{ {json.dumps(sk)} }}," if sk else ""
    return f"""-- Added by iOS Studio (editor setup): Swift completion and errors through sourcekit-lsp.
-- sourcekit-lsp finds an xtool project through its .bsp/xtool.json (xtool's build server), so it
-- compiles for the iPhone. Delete this file to undo. See {DOCS} in iOS Studio.
return {{
  {{
    "nvim-treesitter/nvim-treesitter",
    opts = {{ ensure_installed = {{ "swift" }} }},
  }},
  {{
    "neovim/nvim-lspconfig",
    opts = {{
      servers = {{
        sourcekit = {{
          mason = false, -- comes with Swift, not with Mason{cmd}
        }},
      }},
    }},
  }},
}}
"""


def neovim_actions(env: Env) -> list[dict]:
    st = nvim_status(env)
    cfg = Path(st["config"])
    if st["swiftLsp"]:
        return [{"path": st["swiftLsp"], "scope": "user", "kind": "skip", "configured": True,
                 "detail": "Swift is already on in your Neovim config."}]
    if not st["lazyvim"]:
        where = str(cfg / "init.lua") if st["configExists"] else str(cfg)
        return [{"path": where, "scope": "user", "kind": "skip",
                 "detail": "Not a LazyVim config, so it's left alone. Add the snippet from the docs to it."}]
    if st["extraAvailable"] and st["lazyvimJson"]:
        lj = cfg / "lazyvim.json"
        if st["lazyvimJsonProblem"]:
            return [{"path": str(lj), "scope": "user", "kind": "skip",
                     "detail": f"The file {st['lazyvimJsonProblem']}, so it's left alone. Turn on Swift with "
                               ":LazyExtras instead."}]
        bak = f"{lj}.bak.{int(time.time())}"
        data = json.loads(lj.read_text(encoding="utf-8"))
        data["extras"] = sorted(set(data.get("extras") or []) | {LAZY_EXTRA})
        acts = [{"path": bak, "scope": "user", "kind": "backup", "detail": "A copy of lazyvim.json before it changes.",
                 "_from": str(lj)}]
        acts.append({"path": str(lj), "scope": "user", "kind": "merge",
                     "detail": "Turns on LazyVim's Swift extra for every project. Undo with :LazyExtras.",
                     "keys": [f"extras + {LAZY_EXTRA}"], "_text": json.dumps(data, indent=2) + "\n"})
        return acts
    plugin = cfg / NVIM_PLUGIN_REL
    if plugin.exists():
        return [{"path": str(plugin), "scope": "user", "kind": "skip",
                 "detail": "A file with this name is already there; it's left as it is."}]
    body = nvim_plugin_text(env)
    return [{"path": str(plugin), "scope": "user", "kind": "write",
             "detail": NVIM_PLUGIN_DETAIL, "preview": body, "_text": body}]


# ================================================================ settings: the preferred editor

SETTING_KEY = "editorSetup"   # in iOS Studio's settings.json, next to editorCommand ("Open in editor")
_OPEN_TO_EDITOR = {"zeditor": "zed", "zed": "zed", "zedit": "zed", "zed-editor": "zed", "code": "vscode",
                   "code-oss": "vscode", "codium": "vscode", "vscodium": "vscode", "code-insiders": "vscode",
                   "nvim": "neovim"}


def _settings(env: Env) -> dict:
    data = fileio.load_json(env.settings_file, {})
    return data if isinstance(data, dict) else {}


def preferred(env: Env) -> str | None:
    """The editor set up last (settings.editorSetup); without one, the one "Open in editor" uses when it's
    one of ours (settings.editorCommand)."""
    s = _settings(env)
    v = s.get(SETTING_KEY)
    if v in EDITORS:
        return v
    cmd = str(s.get("editorCommand") or "").split()
    if cmd:
        return _OPEN_TO_EDITOR.get(Path(cmd[0]).name)
    return None


def _preference_action(editor: str, env: Env) -> dict:
    path = env.settings_file
    if _settings(env).get(SETTING_KEY) == editor:
        return {"path": str(path), "scope": "user", "kind": "skip", "configured": True,
                "detail": f"{EDITORS[editor]} is already your editor."}
    extra = " Projects get its settings files when they're built." if editor == "vscode" else ""
    return {"path": str(path), "scope": "user", "kind": "merge" if path.is_file() else "write",
            "detail": f"Remember {EDITORS[editor]} as your editor.{extra}", "keys": [SETTING_KEY],
            "_setting": editor}


# ================================================================ machine-wide status and setup

def _project(flags: dict) -> Path:
    raw = flags.get("project") or flags.get("path")
    if not raw or raw is True:
        raise HelperError("usage", "Missing --project.")
    p = Path(os.path.expanduser(str(raw))).resolve()
    if not p.is_dir():
        raise HelperError("not-found", "That project folder doesn't exist anymore.")
    if not (p / "Package.swift").is_file():
        raise HelperError("not-a-project", "That folder isn't a Swift package (it has no Package.swift).")
    return p


def machine_actions(editor: str, env: Env) -> list[dict]:
    if editor == "vscode":
        return vscode_machine_actions(env)
    if editor == "zed":
        return zed_machine_actions(env)
    if editor == "neovim":
        return neovim_actions(env)
    raise HelperError("usage", "--editor must be vscode, zed or neovim.")


def editor_info(editor: str, env: Env, acts: list[dict] | None = None) -> dict:
    """One editor's machine-wide state. state: ready | needs-setup (setup can do the rest) | partial (a step
    only you can do remains; `notes` says which) | not-installed."""
    acts = machine_actions(editor, env) if acts is None else acts
    pending = [a for a in acts if a["kind"] in ("write", "merge", "backup")]
    blocked = [a for a in acts if a["kind"] == "skip" and not a.get("configured")]
    notes: list[str] = []
    ext = None
    if editor == "vscode":
        installed = vscode_installed(env)
        ext = vscode_extension(env)
        if not ext:
            notes.append(f"Install the Swift extension ({VSCODE_EXT}): VS Code offers it when you open a project "
                         "set up for it, or search Swift in Extensions (ctrl-shift-x).")
    elif editor == "zed":
        installed = zed_installed(env)
        ext = zed_extension(env)
        if not ext:
            notes.append("Install Zed's Swift extension: open Extensions (ctrl-shift-x), search for Swift, click "
                         "Install.")
    else:
        installed = env.which("nvim")
    if not sourcekit(env) and not _sourcekit_fallback(env):
        notes.append("sourcekit-lsp isn't installed: install Swift on iOS Studio's Setup page.")
    notes += [a["detail"] for a in blocked]
    if not installed:
        state, notes = "not-installed", []
    elif pending:
        state = "needs-setup"
    elif notes:
        state = "partial"
    else:
        state = "ready"
    files: list[str] = []
    extra: dict[str, Any] = {}
    if editor == "neovim":
        st = nvim_status(env)
        files = [st["swiftLsp"]] if st["swiftLsp"] else []
        extra["lazyvim"] = bool(st["lazyvim"])
    return {"id": editor, "name": EDITORS[editor], "installed": bool(installed), "binary": installed or None,
            "state": state, "extension": ext, "configured": not pending and not blocked, "files": files,
            "note": notes[0] if notes else (_pending_note(pending, env) if pending else None), "notes": notes,
            **extra}


def _pending_note(pending: list[dict], env: Env) -> str:
    return "Set up changes " + ", ".join(_tilde(a["path"], env) for a in pending if a["kind"] != "backup") + "."


def _tilde(path: str, env: Env) -> str:
    try:
        return "~/" + str(Path(path).relative_to(env.home))
    except ValueError:
        return path


def machine_status(env: Env | None = None) -> dict:
    env = env or Env.from_os()
    ver = env.swift()
    return {"swift": f"{ver[0]}.{ver[1]}" if ver else None, "xtool": env.which("xtool"),
            "sourcekit_lsp": sourcekit(env) or _sourcekit_fallback(env), "preferred": preferred(env),
            "editors": [editor_info(ed, env) for ed in EDITORS]}


def _public(a: dict) -> dict:
    return {k: v for k, v in a.items() if not k.startswith("_")}


def _apply(acts: list[dict], env: Env) -> None:
    for a in acts:
        if a["kind"] == "backup":
            shutil.copy2(a["_from"], a["path"])
            a["done"] = True
        elif "_setting" in a:
            s = _settings(env)
            s[SETTING_KEY] = a["_setting"]
            fileio.save_json(env.settings_file, s)
            a["done"] = True
        elif a["kind"] in ("write", "merge"):
            path = Path(a["path"])
            if a["kind"] == "write" and path.exists():   # appeared since the plan: never overwrite
                a["kind"], a["detail"] = "skip", "The file appeared meanwhile; it's left as it is."
                continue
            mode = 0o644
            try:
                mode = path.stat().st_mode & 0o777
            except OSError:
                pass
            fileio.atomic_write(path, a["_text"], mode)
            a["done"] = True


def _apply_safely(acts: list[dict], env: Env) -> None:
    try:
        _apply(acts, env)
    except OSError as exc:
        raise HelperError("write-failed", f"Couldn't write {exc.filename or 'a file'}: {exc.strerror}.") from None


def machine_setup(editor: str, confirm: bool = False, env: Env | None = None, prefer: bool = True) -> dict:
    """prefer=False leaves the remembered editor as it is (setting up several editors at once)."""
    env = env or Env.from_os()
    if editor not in EDITORS:
        raise HelperError("usage", "--editor must be vscode, zed or neovim.")
    acts = machine_actions(editor, env) + ([_preference_action(editor, env)] if prefer else [])
    if confirm:
        _apply_safely(acts, env)
    info = editor_info(editor, env, None if confirm else [a for a in acts if "_setting" not in a])
    pending = any(a["kind"] in ("write", "merge") for a in acts)
    if not confirm and pending:
        nxt = None
    elif info["notes"]:
        nxt = info["notes"][0]
    elif editor == "neovim":
        nxt = "Restart Neovim; Swift files in your projects then get completion and errors."
    else:
        nxt = f"Open a project in {EDITORS[editor]}; completion starts once sourcekit-lsp has indexed it."
    return {"editor": editor, "name": EDITORS[editor], "dryRun": not confirm,
            "actions": [_public(a) for a in acts], "applied": any(a.get("done") for a in acts),
            "state": info["state"], "next": nxt, "notes": info["notes"]}


# ================================================================ per project: automatic, status, --fix

def created_here(project: Path, env: Env) -> bool:
    """True for a project iOS Studio created from a template (projects.json meta createdFrom). Only these get
    editor files (.vscode/) written by themselves; added or converted projects get them through --fix."""
    data = fileio.load_json(env.state / "projects.json", {})
    meta = (data.get("meta") or {}).get(str(project)) if isinstance(data, dict) else None
    return isinstance(meta, dict) and bool(meta.get("createdFrom"))


def project_actions(project: Path, env: Env, editors: list[str] | None = None) -> list[dict]:
    """What a project needs: the build server file, plus VS Code's project files when VS Code is the
    preferred editor (or asked for). Zed and Neovim need nothing in the project."""
    editors = [preferred(env)] if editors is None else editors
    acts = [bsp_action(project, env)]
    if "vscode" in editors:
        acts += vscode_project_actions(project)
    return acts


def _auto_actions(project: Path, env: Env) -> list[dict]:
    """What the automatic part writes: .bsp for every project, .vscode/ only for projects created here."""
    acts = project_actions(project, env)
    return acts if created_here(project, env) else acts[:1]


def _auto_file(env: Env) -> Path:
    return env.state / "editor-auto.json"


def auto_project(project: Path, env: Env | None = None) -> dict:
    """The automatic part (after a successful build, on create / add / convert): write what's missing,
    quietly, never overwriting. A file it can't merge is left alone. Remembers what it wrote per project."""
    env = env or Env.from_os()
    acts = [a for a in _auto_actions(project, env) if a["kind"] in ("write", "merge")]
    if not acts:
        return {"wrote": []}
    _apply(acts, env)
    wrote = [{"kind": a["kind"], "path": a["path"]} for a in acts if a.get("done")]
    if wrote:
        log = fileio.load_json(_auto_file(env), {})
        log = log if isinstance(log, dict) else {}
        log[str(project)] = {"at": time.time(), "wrote": wrote}
        if len(log) > 200:   # keep the newest
            log = dict(sorted(log.items(), key=lambda kv: kv[1].get("at", 0) if isinstance(kv[1], dict) else 0)[-200:])
        fileio.save_json(_auto_file(env), log)
    return {"wrote": wrote}


def project_status(project: Path, env: Env | None = None) -> dict:
    """{project, bsp: {present, stale, reason, path, kind}, ready, hint, preferred, auto, pending}.
    ready: an editor gets completion and errors here once the machine-wide part is done."""
    env = env or Env.from_os()
    b = bsp_status(project, env)
    pref = preferred(env)
    acts = project_actions(project, env)
    pending = [_public(a) for a in acts[1:] if a["kind"] in ("write", "merge")]
    created = created_here(project, env)
    hint = None
    if not b["present"]:
        hint = ("xtool.yml has skipLSP: true, so editors get no completion" if b["skipLSP"]
                else "Build once to enable completion")
    elif b["stale"]:
        hint = b["reason"]
    elif pending and created:
        hint = "VS Code's Swift settings for this project are added on the next build"
    elif pending:
        hint = "VS Code settings for this project: set up from Settings › Code editor"
    auto = fileio.load_json(_auto_file(env), {})
    auto = auto.get(str(project)) if isinstance(auto, dict) else None
    return {"project": str(project),
            "bsp": {k: b[k] for k in ("present", "stale", "reason", "path", "kind", "built", "note")},
            "ready": b["present"] and not b["stale"] and not pending, "hint": hint, "preferred": pref,
            "created": created,
            "auto": auto if isinstance(auto, dict) else None, "pending": pending}


def brief(project: Path, env: Env | None = None) -> dict:
    """The `editor` field of `projects info`: project_status without the pending list."""
    s = project_status(project, env)
    return {"bsp": {k: s["bsp"][k] for k in ("present", "stale", "reason")}, "ready": s["ready"], "hint": s["hint"],
            "preferred": s["preferred"], "auto": s["auto"], "created": s["created"]}


def project_fix(project: Path, confirm: bool = False, env: Env | None = None, editor: str | None = None) -> dict:
    """--fix: write the project's missing files now (dry run unless confirm)."""
    env = env or Env.from_os()
    if editor and editor not in EDITORS:
        raise HelperError("usage", "--editor must be vscode, zed or neovim.")
    acts = project_actions(project, env, [editor] if editor else None)
    if confirm:
        _apply_safely(acts, env)
    pending = any(a["kind"] in ("write", "merge") for a in acts)
    nxt = (None if not confirm and pending else "Nothing left to write for this project." if not pending
           else "Reopen the folder in your editor if it was open.")
    return {"dryRun": not confirm, "actions": [_public(a) for a in acts],
            "applied": any(a.get("done") for a in acts), "next": nxt}


# ================================================================ CLI

def cmd_status(flags: dict) -> int:
    """editor status                         -> machine_status
    editor status --project P               -> project_status
    editor status --project P --fix [--confirm] [--editor E] -> project_status + fix"""
    if not flags.get("project") and not flags.get("path"):
        return output.ok(machine_status())
    p = _project(flags)
    fix = project_fix(p, bool(flags.get("confirm")), editor=flags.get("editor") or None) if flags.get("fix") else None
    out = project_status(p)
    if fix is not None:
        out["fix"] = fix
    return output.ok(out)


def cmd_setup(flags: dict) -> int:
    editor = str(flags.get("editor") or "")
    if editor not in EDITORS:
        raise HelperError("usage", "--editor must be vscode, zed or neovim.")
    prefer = str(flags.get("prefer", "yes")).lower() not in ("no", "0", "false")
    return output.ok(machine_setup(editor, bool(flags.get("confirm")), prefer=prefer))


GROUPS = {"editor": {"status": cmd_status, "setup": cmd_setup}}


# ================================================================ demo mode
# Made-up answers: Zed ready, Neovim (LazyVim) needs setup, VS Code not installed; setup and --fix answer
# as if done but change nothing. HabitTracker has never been built (no .bsp yet).

DEMO_NO_BSP = {"HabitTracker"}


def _demo_machine() -> dict:
    home = os.path.expanduser("~")
    plugin = home + "/.config/nvim/" + NVIM_PLUGIN_REL
    return {"swift": "6.4", "xtool": home + "/.local/bin/xtool", "sourcekit_lsp": "/usr/bin/sourcekit-lsp",
            "preferred": "zed", "demo": True, "editors": [
                {"id": "vscode", "name": "VS Code", "installed": False, "binary": None, "state": "not-installed",
                 "extension": None, "configured": True, "files": [], "note": None, "notes": []},
                {"id": "zed", "name": "Zed", "installed": True, "binary": "/usr/bin/zeditor", "state": "ready",
                 "extension": home + "/.local/share/zed/extensions/installed/swift", "configured": True,
                 "files": [], "note": None, "notes": []},
                {"id": "neovim", "name": "Neovim", "installed": True, "binary": "/usr/bin/nvim",
                 "state": "needs-setup", "extension": None, "configured": False, "files": [], "lazyvim": True,
                 "note": f"Set up changes ~/.config/nvim/{NVIM_PLUGIN_REL}.", "notes": [], "_plugin": plugin}]}


def _demo_setup(editor: str, confirm: bool, prefer: bool = True) -> dict:
    m = _demo_machine()
    info = next(e for e in m["editors"] if e["id"] == editor)
    acts: list[dict] = []
    if editor == "neovim":
        body = nvim_plugin_text(Env(Path("/"), Path("/"), Path("/"), "/usr/bin", probe_swift=False))
        acts.append({"path": info["_plugin"], "scope": "user", "kind": "write",
                     "detail": NVIM_PLUGIN_DETAIL, "preview": body})
    settings = os.path.expanduser("~/.config/ios-studio/settings.json")
    if not prefer:
        pass
    elif editor == "zed":
        acts.append({"path": settings, "scope": "user", "kind": "skip", "configured": True,
                     "detail": "Zed is already your editor."})
    else:
        acts.append({"path": settings, "scope": "user", "kind": "merge", "keys": [SETTING_KEY],
                     "detail": f"Remember {EDITORS[editor]} as your editor."})
    pending = any(a["kind"] in ("write", "merge") for a in acts)
    if confirm:
        for a in acts:
            if a["kind"] in ("write", "merge"):
                a["done"] = True
    state = info["state"] if not confirm or info["state"] == "not-installed" else "ready"
    nxt = (None if not confirm and pending
           else "Restart Neovim; Swift files in your projects then get completion and errors." if editor == "neovim"
           else f"Open a project in {EDITORS[editor]}; completion starts once sourcekit-lsp has indexed it.")
    return {"editor": editor, "name": EDITORS[editor], "dryRun": not confirm, "actions": acts,
            "applied": confirm and pending, "state": state, "next": nxt, "notes": info["notes"], "demo": True}


def _demo_project(path: str, fix: bool = False, confirm: bool = False) -> dict:
    name = path.rstrip("/").split("/")[-1] or "App"
    root = path.rstrip("/")
    has = name not in DEMO_NO_BSP
    out = {"project": path,
           "bsp": {"present": has, "stale": False, "reason": None, "path": root + "/" + BSP_REL, "kind": "bsp",
                   "built": has, "note": None if has else "Build once so the Swift packages are fetched; until "
                                                          "then completion for them is missing."},
           "ready": has, "hint": None if has else "Build once to enable completion", "preferred": "zed",
           "auto": {"at": time.time() - 3600, "wrote": [{"kind": "write", "path": root + "/" + BSP_REL}]}
           if name == "RecipeBox" else None, "pending": [], "created": name != "HabitTracker", "demo": True}
    if fix:
        act = {"path": root + "/" + BSP_REL, "scope": "project", "kind": "skip" if has else "write",
               "detail": "Already there." if has else BSP_DETAIL}
        if not has:
            act["preview"] = BSP_TEXT
            if confirm:
                act["done"] = True
        out["fix"] = {"dryRun": not confirm, "actions": [act], "applied": confirm and not has,
                      "next": "Nothing left to write for this project." if has else
                      ("Reopen the folder in your editor if it was open." if confirm else None)}
    return out


def demo_brief(path: str) -> dict:
    s = _demo_project(path)
    return {"bsp": {k: s["bsp"][k] for k in ("present", "stale", "reason")}, "ready": s["ready"], "hint": s["hint"],
            "preferred": s["preferred"], "auto": s["auto"], "created": s["created"]}


def demo(action: str, flags: dict) -> int:
    """Demo mode: made-up status; setup and --fix list their plan and write nothing."""
    path = flags.get("project") or flags.get("path")
    if action == "status":
        if not path or path is True:
            m = _demo_machine()
            for e in m["editors"]:
                e.pop("_plugin", None)
            return output.ok(m)
        return output.ok(_demo_project(str(path), bool(flags.get("fix")), bool(flags.get("confirm"))))
    if action == "setup":
        editor = str(flags.get("editor") or "")
        if editor not in EDITORS:
            return output.fail("usage", "--editor must be vscode, zed or neovim.")
        prefer = str(flags.get("prefer", "yes")).lower() not in ("no", "0", "false")
        return output.ok(_demo_setup(editor, bool(flags.get("confirm")), prefer))
    return output.fail("usage", f"Unknown command 'editor {action}'.")
