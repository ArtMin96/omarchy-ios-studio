"""Edit other programs' config files without losing anything (editor setup, AI agents).

JSON with comments (JSONC, what VS Code, Zed, opencode and Gemini use) is edited as text on top of
core/jsonc's spans: a change touches only the members it adds, sets or removes, and the rest of the file
(order, indentation, comments, trailing commas) stays byte for byte. Plus the file side: reading with a size
limit, a unified diff for previews, backups, and writing atomically through symlinks with the file's mode
kept, verified by reading back and restored when that fails.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, fileio, jsonc
from iosstudio.core.jsonc import JsoncError, Node
from iosstudio.core.output import HelperError

# ================================================================ layout helpers


def line_indent(text: str, pos: int) -> tuple[str, bool]:
    """Indentation of the line holding pos, and whether only whitespace precedes pos on it."""
    ls = text.rfind("\n", 0, pos) + 1
    before = text[ls:pos]
    indent = before[:len(before) - len(before.lstrip(" \t"))]
    return indent, before.strip() == ""


def render(value: Any, indent: str, unit: str | None) -> str:
    """value as JSON, laid out with `unit` per level (a tab or spaces) continuing at `indent`; unit None: one line."""
    if unit is None:
        return json.dumps(value, ensure_ascii=False)
    out = json.dumps(value, indent=1 if unit == "\t" else len(unit), ensure_ascii=False)
    if unit == "\t":
        out = "\n".join(("\t" * (len(ln) - len(ln.lstrip(" ")))) + ln.lstrip(" ") for ln in out.split("\n"))
    return out.replace("\n", "\n" + indent)


def key_label(path: list[str]) -> str:
    return " › ".join(path)


# ================================================================ merge: add what's missing, change nothing

def _file_indent_unit(text: str) -> str:
    """The file's indent step: a tab, or the smallest run of leading spaces (default 2)."""
    sizes = []
    for line in text.splitlines():
        m = re.match(r"^([ \t]+)\S", line)
        if m:
            if "\t" in m.group(1):
                return "\t"
            sizes.append(len(m.group(1)))
    return " " * (min(sizes) if sizes else 2)


@dataclass
class _Merge:
    edits: list[tuple] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    kept: list[dict] = field(default_factory=list)


def _append_edits(text: str, node: Node, new: list[tuple[str | None, Any]], unit: str) -> list[tuple]:
    """Text edits that add `new` members (key, value) to an object node, or items (None, value) to a list node,
    in the file's own layout."""
    close = node.end - 1
    children = [m.node for m in node.members] if node.kind == "obj" else node.items
    multiline = "\n" in text[node.start:node.end]

    def entry(k: str | None, v: Any, ind: str) -> str:
        return (json.dumps(k, ensure_ascii=False) + ": " if k is not None else "") + render(v, ind, unit)

    if children:
        first = node.members[0].kstart if node.kind == "obj" else children[0].start   # a member's line starts with its key
        own_ind, own_line = line_indent(text, first)
        ind = own_ind if own_line else line_indent(text, node.start)[0] + unit
    else:
        ind = line_indent(text, node.start)[0] + unit
    if not multiline:
        if not children and node.start == jsonc.skip(text, 0):   # an empty root {}: lay it out
            outer = line_indent(text, node.start)[0]
            return [(close, "\n" + ",\n".join(ind + entry(k, v, ind) for k, v in new) + "\n" + outer)]

        def compact(k: str | None, v: Any) -> str:   # stays on the one line
            return (json.dumps(k, ensure_ascii=False) + ": " if k is not None else "") + \
                json.dumps(v, ensure_ascii=False, separators=(", ", ": "))
        if not children:
            return [(close, ", ".join(compact(k, v) for k, v in new))]
        return [(children[-1].end, "".join(", " + compact(k, v) for k, v in new))]
    had_trailing = bool(children) and children[-1].comma >= 0
    lines = [ind + entry(k, v, ind) for k, v in new]
    ls = text.rfind("\n", 0, close) + 1
    edits: list[tuple] = []
    if children and not had_trailing:
        edits.append((children[-1].end, ","))
    if not children and text[node.start + 1:close].strip() == "":
        # an empty list or object spread over lines: replace the blank inside
        outer = line_indent(text, node.start)[0]
        return [(node.start + 1, close, "\n" + ",\n".join(lines) + "\n" + outer)]
    if text[ls:close].strip() == "":
        block = ",\n".join(lines) + ("," if had_trailing else "") + "\n"
        edits.append((ls, block))
    else:
        edits.append((close, "\n" + ",\n".join(lines) + "\n" + line_indent(text, node.start)[0]))
    return edits


def _merge_node(text: str, node: Node, want: Any, path: list[str], unit: str, m: _Merge) -> None:
    if node.kind == "obj" and isinstance(want, dict):
        have = {mem.key: mem.node for mem in node.members}
        new: list[tuple[str | None, Any]] = []
        for k, v in want.items():
            if k not in have:
                new.append((k, v))
                m.added.append(key_label(path + [k]))
            else:
                _merge_node(text, have[k], v, path + [k], unit, m)
        if new:
            m.edits.extend(_append_edits(text, node, new, unit))
        return
    if node.kind == "arr" and isinstance(want, list):
        new = [(None, v) for v in want if v not in node.value]
        if new:
            m.added.extend(key_label(path) + " + " + json.dumps(v, ensure_ascii=False) for _, v in new)
            m.edits.extend(_append_edits(text, node, new, unit))
        return
    if node.value != want:
        m.kept.append({"key": key_label(path), "yours": node.value, "ours": want})


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


def _lost_something(got: Any, want: Any, before: Any) -> bool:
    """True when a key or item we wanted to add isn't in `got`, or a value that was there changed."""
    if isinstance(want, dict) and isinstance(got, dict):
        b = before if isinstance(before, dict) else {}
        for k, v in want.items():
            if k not in got:
                return True
            if k in b and not isinstance(v, (dict, list)) and got[k] != b[k]:
                return True
            if _lost_something(got[k], v, b.get(k)):
                return True
        return any(got.get(k) != v for k, v in b.items() if k not in want)
    if isinstance(want, list) and isinstance(got, list):
        return any(v not in got for v in want) or (isinstance(before, list) and got[:len(before)] != before)
    return False


def merge_missing(text: str, want: dict) -> dict:
    """Merge `want` into a JSON/JSONC document without overwriting anything.

    Returns {status, text?, added, kept, reason?}: status 'unchanged' (everything is already there; `kept`
    lists values that differ and stay), 'merged' (`text` is the new document: only insertions), or 'skip'
    (`reason`: not valid JSON, not an object). An empty file counts as {}."""
    if not text.strip():
        return {"status": "merged", "text": json.dumps(want, indent=2, ensure_ascii=False) + "\n",
                "added": [key_label([k]) for k in want], "kept": []}
    try:
        root = jsonc.parse(text)
    except JsoncError as exc:
        return {"status": "skip", "reason": f"isn't valid JSON: {exc}", "added": [], "kept": []}
    if root.kind != "obj":
        return {"status": "skip", "reason": "isn't a JSON object ({ … })", "added": [], "kept": []}
    m = _Merge()
    _merge_node(text, root, want, [], _file_indent_unit(text), m)
    if not m.edits:
        return {"status": "unchanged", "added": [], "kept": m.kept}
    new_text = _apply_edits(text, m.edits)
    try:   # belt and braces: the result must parse and contain everything we meant to add
        check = jsonc.parse(new_text)
    except JsoncError as exc:
        return {"status": "skip", "reason": f"couldn't be merged safely ({exc}); add the keys by hand",
                "added": [], "kept": m.kept}
    if _lost_something(check.value, want, root.value):
        return {"status": "skip", "reason": "couldn't be merged safely; add the keys by hand", "added": [],
                "kept": m.kept}
    return {"status": "merged", "text": new_text, "added": m.added, "kept": m.kept}


# ================================================================ set / remove one member

def _insert_member(text: str, obj: Node, key: str, value: Any, unit_hint: str) -> str:
    """Add "key": value as the last member of obj, matching the surrounding layout."""
    k = json.dumps(key, ensure_ascii=False)
    base, _ = line_indent(text, obj.start)
    if obj.members:
        last = obj.members[-1]
        mindent, own_line = line_indent(text, last.kstart)
        if own_line:   # one member per line
            unit = mindent[len(base):] if mindent.startswith(base) and len(mindent) > len(base) else unit_hint
            piece = f",\n{mindent}{k}: {render(value, mindent, unit)}"
        else:          # everything on one line
            piece = f", {k}: {render(value, '', None)}"
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
        body = f"{{\n{mindent}{k}: {render(value, mindent, unit_hint)}\n{base}}}"
    else:
        body = f"{{{k}: {render(value, '', None)}}}"
    if inner.strip():   # only comments inside: keep them
        body = body[:-1] + inner.rstrip() + ("\n" + base if multiline else "") + "}"
    return text[:obj.start] + body + text[obj.end:]


def _member_indent_unit(text: str, root: Node) -> str:
    """The indent step of the root's members (default 2 spaces)."""
    if root.members:
        ind, own = line_indent(text, root.members[0].kstart)
        base, _ = line_indent(text, root.start)
        if own and len(ind) > len(base):
            return ind[len(base):]
    return "  "


def set_member(text: str, path: list[str], value: Any) -> str:
    """Set path (object keys) to value, creating parent objects as needed."""
    if not text.strip():
        out: Any = value
        for k in reversed(path):
            out = {k: out}
        return json.dumps(out, indent=2, ensure_ascii=False) + "\n"
    root = jsonc.parse(text)
    if root.kind != "obj":
        raise JsoncError("the file isn't a JSON object")
    unit = _member_indent_unit(text, root)
    node = root
    for depth, key in enumerate(path):
        m = next((m for m in node.members if m.key == key), None)
        if m is None:
            rest: Any = value
            for k in reversed(path[depth + 1:]):
                rest = {k: rest}
            return _insert_member(text, node, key, rest, unit)
        if depth == len(path) - 1:
            base, _ = line_indent(text, m.kstart)
            single = "\n" not in text[node.start:node.end]
            rendered = render(value, base, None if single else unit)
            return text[:m.node.start] + rendered + text[m.node.end:]
        if m.node.kind != "obj":
            raise JsoncError(f"\"{key}\" isn't an object")
        node = m.node
    return text


def remove_member(text: str, path: list[str]) -> str:
    """Remove the member at path. A parent object left empty by that goes too (not the root)."""
    if not text.strip():
        return text
    root = jsonc.parse(text)
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
        return remove_member(text, path[:-1])
    m = node.members[idx]
    if m.comma >= 0:
        # "key": value, plus the comma; when the member had its own line, that whole line
        _, own_line = line_indent(text, m.kstart)
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


# ================================================================ files

def read_text(path: Path) -> str:
    """The file's text, '' when it doesn't exist."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def read_limited(path: Path, max_bytes: int) -> tuple[str | None, str | None]:
    """(text, problem) for a config file read only when it's a readable UTF-8 file of at most max_bytes.
    text None + problem None: the file doesn't exist."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"can't be read ({exc.strerror})"
    if not path.is_file():
        return None, "isn't a file"
    if st.st_size > max_bytes:
        return None, f"is larger than {max_bytes // 1024} KB"
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"can't be read ({exc})"


def unified_diff(path: Path, old: str, new: str) -> str:
    import difflib  # only plans show a diff; keeps `editor status` / `agents list` startup lean
    name = appdirs.tilde(path)
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), name, name + " (after)"))


def write_target(path: Path) -> Path:
    """The file a write to `path` changes: a symlink's target (dotfile managers link config files), else path."""
    return path.resolve() if path.is_symlink() else path


def write_keeping_mode(path: Path, text: str) -> Path:
    """Write atomically through a symlink, keeping the file's permissions (new files: 0644). Returns the
    file written."""
    target = write_target(path)
    try:
        mode = target.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fileio.atomic_write(target, text, mode)
    return target


def backup_into(folder: Path, prefix: str, path: Path, keep: int = 10) -> Path | None:
    """Copy `path` to folder/<prefix><name>.<timestamp> (the newest `keep` per file stay); None when it doesn't
    exist."""
    if not path.exists():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = folder / f"{prefix}{path.name}.{stamp}"
    n = 1
    while dest.exists():
        n += 1
        dest = folder / f"{prefix}{path.name}.{stamp}-{n}"
    shutil.copy2(path, dest)
    for old in sorted(folder.glob(f"{prefix}{path.name}.*"))[:-keep]:
        old.unlink(missing_ok=True)
    return dest


def commit(path: Path, old: str, new: str, verify: Callable[[str], None],
           backup: Callable[[Path], Path | None]) -> dict:
    """Back up (backup(target) returns the copy or None), write atomically through symlinks keeping the mode,
    re-read and verify(text); put the original back if anything goes wrong.
    -> {changed, file, backup?}"""
    if new == old:
        return {"changed": False, "file": str(path)}
    target = write_target(path)
    existed = target.exists()
    saved = backup(target)
    try:
        write_keeping_mode(target, new)
        verify(target.read_text())
    except Exception as exc:
        if saved:
            shutil.copy2(saved, target)
        elif not existed:
            target.unlink(missing_ok=True)
        if isinstance(exc, HelperError):
            raise
        raise HelperError("config-write", f"Couldn't update {appdirs.tilde(path)}, so it was left as it was.",
                          detail=repr(exc))
    return {"changed": True, "file": str(path), "backup": str(saved) if saved else None}
