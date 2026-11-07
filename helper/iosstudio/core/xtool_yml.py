"""xtool.yml (stdlib only): one reader for its flat YAML and a key update that keeps the rest of the file.

xtool.yml is small flat YAML: top-level `key: scalar` lines plus an `extensions:` list of mappings. Anything
fancier is left alone. Every key keeps its 1-based line, so checks can point at it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_TOP = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")
_EXT_KEY = re.compile(r"^\s*(-\s+)?([A-Za-z_][\w-]*)\s*:(\s|$)")
_TRUE = ("true", "yes", "on")


@dataclass(frozen=True)
class Key:
    name: str
    line: int
    value: str      # the scalar without quotes or a trailing comment; '' for none (or a nested block)


@dataclass
class XtoolYml:
    keys: list[Key] = field(default_factory=list)                         # top-level, in file order
    extensions: list[list[tuple[str, int]]] = field(default_factory=list)  # (key, line) of each item
    generated: bool = False     # "GENERATED" near the top: a script writes this file

    @property
    def values(self) -> dict[str, str]:
        """Top-level scalars; a repeated key: the last one wins."""
        return {k.name: k.value for k in self.keys}

    @property
    def lines(self) -> dict[str, int]:
        return {k.name: k.line for k in self.keys}

    def get(self, name: str) -> str | None:
        """A top-level scalar, None when it's missing or empty."""
        return self.values.get(name) or None

    def flag(self, name: str) -> bool:
        return (self.values.get(name) or "") in _TRUE


def scalar(raw: str) -> str:
    v = raw.strip()
    if v and v[0] in "'\"":
        q = v[0]
        end = v.find(q, 1)
        return v[1:end] if end > 0 else v[1:]
    return re.sub(r"(^|\s+)#.*$", "", v).strip()


def parse(text: str) -> XtoolYml:
    out = XtoolYml(generated="GENERATED" in text[:400])
    in_ext = False
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#") or line.startswith("---"):
            continue
        m = _TOP.match(line)
        if m:
            out.keys.append(Key(m.group(1), i, scalar(m.group(2))))
            in_ext = m.group(1) == "extensions"
            continue
        if line[:1] not in (" ", "\t", "-"):
            in_ext = False
            continue
        if in_ext:
            e = _EXT_KEY.match(line)
            if e:
                if e.group(1) or not out.extensions:
                    out.extensions.append([])
                out.extensions[-1].append((e.group(2), i))
            elif re.match(r"^\s*-\s*$", line):
                out.extensions.append([])
    return out


def read(path: Path) -> XtoolYml:
    """The project's xtool.yml parsed; empty when it's missing or unreadable."""
    try:
        return parse(path.read_text(errors="replace"))
    except OSError:
        return XtoolYml()


def set_key(text: str, name: str, value: str) -> str:
    """`text` with top-level `name: value`: an existing line is rewritten in place (its trailing comment
    kept), otherwise the key is appended. Everything else stays byte for byte."""
    rx = re.compile(rf"^{re.escape(name)}\s*:(.*)$", re.M)

    def repl(m: re.Match) -> str:
        c = re.search(r"\s+#.*$", m.group(1)) if m.group(1).strip()[:1] not in ("'", '"') else None
        return f"{name}: {value}" + (c.group(0) if c else "")
    if rx.search(text):
        return rx.sub(repl, text)
    return text.rstrip("\n") + f"\n{name}: {value}\n"
