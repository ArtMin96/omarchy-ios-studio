"""What a hot-reload patch must not change (stdlib only). A patch swaps functions into the running app; it can't
change how values are laid out in memory or which slots a class's vtable has, so those edits need a full build.

1. structural_blockers(old_source, new_source, file)   a Swift scanner compares the baseline and the new text of
   one file: new or removed types, stored properties (property wrappers such as @State included), enum cases,
   overridable class members (vtable layout: `class func`/`class var`/`class subscript` too), protocol requirements
   (static ones too), generic signatures / superclasses / conformances, changed or removed type aliases, and static
   or global stored properties whose type changes or that go away (the app's storage stays in use; new ones are
   fine). Function bodies, computed properties, new functions and extensions without conformances are fine.
   Conservative: anything the scanner can't read is a blocker (kind `unparsed`) rather than a guess.
2. opaque_blockers(baseline_o, patch_o, new_source, file)   a `some View` body fixes its concrete type in an opaque
   type descriptor (`…QOMQ`); its `_symbolic` relocation targets name that type. A different type would corrupt
   memory, so any mismatch is a blocker; the message says to end `body` with `.hotReloadable()` (which must return
   AnyView: a `-> some View` wrapper would still carry the inner type) when the new source doesn't, and that the
   app must be built once with it when it does.
3. interposable(patch, app)   the functions the patch defines that the app exports, split into those the app calls
   through an indirect pointer (swappable) and those it doesn't; none swappable -> HelperError('not-interposable').

A blocker is {kind, type?, name?, file, line?, message}.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from iosstudio.core.output import HelperError, fix
from iosstudio.features.hotreload import macho

MAX_SOURCE = 4 * 1024 * 1024
MAX_TOKENS = 2_000_000
MAX_DEPTH = 200
MAX_BLOCKERS = 10


class ScanError(ValueError):
    """Source the scanner can't follow (an unterminated string or comment, unbalanced brackets)."""

    def __init__(self, message: str, line: int = 0):
        super().__init__(message)
        self.line = line


# ---------------------------------------------------------------- tokens

@dataclass(frozen=True)
class Tok:
    kind: str      # id, num, str, op, punct, attr, pound
    text: str
    line: int
    nl: bool       # a newline (or the file start) before it
    sp: bool       # whitespace or a comment before it


OP_CHARS = set("/=-+!*%<>&|^~?")
PUNCT = set("()[]{},;:.")
ID_START = re.compile(r"[A-Za-z_$\u0080-\U0010ffff]")
ID_CHARS = re.compile(r"[A-Za-z0-9_$\u0080-\U0010ffff]*")
NUM = re.compile(r"0[xX][0-9a-fA-F_]*(?:\.[0-9a-fA-F_]+)?(?:[pP][+-]?[0-9_]+)?|0[bB][01_]+|0[oO][0-7_]+|"
                 r"[0-9][0-9_]*(?:\.[0-9][0-9_]*)?(?:[eE][+-]?[0-9_]+)?")


class _Lexer:
    def __init__(self, src: str):
        self.s = src
        self.n = len(src)
        self.line = 1

    def _nl_count(self, a: int, b: int) -> None:
        self.line += self.s.count("\n", a, b)

    def skip_block_comment(self, i: int) -> int:
        """i at '/*' -> index after the matching '*/' (Swift block comments nest)."""
        depth, j = 0, i
        s, n = self.s, self.n
        while j < n:
            if s.startswith("/*", j):
                depth += 1
                j += 2
            elif s.startswith("*/", j):
                depth -= 1
                j += 2
                if depth == 0:
                    self._nl_count(i, j)
                    return j
            else:
                j += 1
        raise ScanError("An unterminated /* comment.", self.line)

    def skip_string(self, i: int) -> int:
        """i at the first '#' or '"' of a string literal -> index after it (raw, multi-line, interpolation)."""
        s, n = self.s, self.n
        start_line = self.line
        hashes = 0
        while i < n and s[i] == "#":
            hashes += 1
            i += 1
        if i >= n or s[i] != '"':
            raise ScanError("A malformed string literal.", start_line)
        multi = s.startswith('"""', i)
        i += 3 if multi else 1
        close = ('"""' if multi else '"') + "#" * hashes
        escape = "\\" + "#" * hashes
        j = i
        while j < n:
            c = s[j]
            if s.startswith(escape, j):
                k = j + len(escape)
                if k < n and s[k] == "(":
                    self._nl_count(i, j)
                    i = j = self.skip_parens(k)
                    continue
                j = k + 1          # an escaped character
                continue
            if s.startswith(close, j):
                j += len(close)
                self._nl_count(i, j)
                return j
            if c == "\n" and not multi:
                raise ScanError("An unterminated string literal.", start_line)
            j += 1
        raise ScanError("An unterminated string literal.", start_line)

    def skip_parens(self, i: int) -> int:
        """i at '(' inside an interpolation -> index after the matching ')' (strings and comments skipped)."""
        s, n = self.s, self.n
        depth = 0
        j = i
        while j < n:
            c = s[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return j + 1
            elif c == "\n":
                self.line += 1
            elif c == '"' or (c == "#" and _raw_string_at(s, j)):
                j = self.skip_string(j)
                continue
            elif s.startswith("//", j):
                k = s.find("\n", j)
                j = n if k < 0 else k
                continue
            elif s.startswith("/*", j):
                j = self.skip_block_comment(j)
                continue
            j += 1
        raise ScanError("An unterminated string interpolation.", self.line)

    def tokens(self) -> list[Tok]:
        s, n = self.s, self.n
        out: list[Tok] = []
        i = 0
        nl, sp = True, True
        while i < n:
            if len(out) > MAX_TOKENS:
                raise ScanError("The file is too long to check.")
            c = s[i]
            if c == "\n":
                self.line += 1
                nl = sp = True
                i += 1
                continue
            if c in " \t\r\f\v﻿":
                sp = True
                i += 1
                continue
            if s.startswith("//", i):
                k = s.find("\n", i)
                i = n if k < 0 else k
                sp = True
                continue
            if s.startswith("/*", i):
                i = self.skip_block_comment(i)
                sp = True
                continue
            line = self.line
            if c == '"' or (c == "#" and _raw_string_at(s, i)):
                j = self.skip_string(i)
                out.append(Tok("str", s[i:j] if j - i < 200 else s[i:i + 200], line, nl, sp))
                i = j
            elif c == "#" and _regex_at(s, i):
                j = _skip_regex(s, i)
                self._nl_count(i, j)
                out.append(Tok("str", s[i:j][:200], line, nl, sp))
                i = j
            elif c == "#":
                m = ID_CHARS.match(s, i + 1)
                j = m.end() if m else i + 1
                out.append(Tok("pound", s[i:j], line, nl, sp))
                i = j
            elif c == "@":
                m = ID_CHARS.match(s, i + 1)
                j = m.end() if m else i + 1
                out.append(Tok("attr", s[i:j], line, nl, sp))
                i = j
            elif c == "`":
                k = s.find("`", i + 1)
                if k < 0 or "\n" in s[i:k]:
                    raise ScanError("An unterminated `identifier`.", line)
                out.append(Tok("id", s[i + 1:k], line, nl, sp))
                i = k + 1
            elif c.isdigit():
                m = NUM.match(s, i)
                j = m.end() if m and m.end() > i else i + 1
                out.append(Tok("num", s[i:j], line, nl, sp))
                i = j
            elif ID_START.match(c):
                m = ID_CHARS.match(s, i + 1)
                j = m.end() if m else i + 1
                out.append(Tok("id", s[i:j], line, nl, sp))
                i = j
            elif c == "." and s.startswith("..", i):
                j = i
                while j < n and (s[j] == "." or s[j] in OP_CHARS):
                    j += 1
                out.append(Tok("op", s[i:j], line, nl, sp))
                i = j
            elif c in PUNCT:
                out.append(Tok("punct", c, line, nl, sp))
                i += 1
            elif c == "/" and _regex_position(out, nl) and (j := _skip_bare_regex(s, i)) > 0:
                out.append(Tok("str", s[i:j][:200], line, nl, sp))
                i = j
            elif c in OP_CHARS:
                j = i
                while j < n and s[j] in OP_CHARS and not s.startswith("//", j) and not s.startswith("/*", j):
                    j += 1
                j = max(j, i + 1)
                out.append(Tok("op", s[i:j], line, nl, sp))
                i = j
            elif c == "\\":
                out.append(Tok("op", "\\", line, nl, sp))      # a key path
                i += 1
            else:
                out.append(Tok("op", c, line, nl, sp))
                i += 1
            nl = sp = False
        return out


def _raw_string_at(s: str, i: int) -> bool:
    j = i
    while j < len(s) and s[j] == "#":
        j += 1
    return j > i and j < len(s) and s[j] == '"'


def _regex_at(s: str, i: int) -> bool:
    j = i
    while j < len(s) and s[j] == "#":
        j += 1
    return j > i and j < len(s) and s[j] == "/"


def _skip_regex(s: str, i: int) -> int:
    hashes = 0
    while s[i + hashes] == "#":
        hashes += 1
    close = "/" + "#" * hashes
    k = s.find(close, i + hashes + 1)
    if k < 0:
        raise ScanError("An unterminated regex literal.")
    return k + len(close)


# keywords after which an expression (so a `/regex/`) starts; any other identifier is an operand (`a / b`)
EXPR_KW = {"return", "case", "in", "where", "if", "guard", "while", "throw", "try", "await", "switch", "yield", "then"}


def _regex_position(out: list[Tok], nl: bool) -> bool:
    """Can a `/` here start a bare regex literal (Swift 5.7: only where an expression starts, not after an operand)?"""
    if not out or nl:
        return True
    p = out[-1]
    if p.kind == "id":
        return p.text in EXPR_KW
    if p.kind == "punct":
        return p.text in "([{,:;"
    if p.kind == "op":
        return p.sp            # a binary or prefix operator (`= /x/`), not a postfix one (`a! / b`)
    return False


def _skip_bare_regex(s: str, i: int) -> int:
    """i at the '/' of a bare regex literal -> index after its closing '/', or -1 when it isn't one: it can't start
    or end with a space, can't span lines and can't hold an unbalanced ')' (Swift's own rules for `/.../`)."""
    n = len(s)
    j = i + 1
    if j >= n or s[j] in " \t\r\n":
        return -1
    depth = 0
    while j < n:
        c = s[j]
        if c == "\n" or (c == "\\" and s.startswith("\n", j + 1)):
            return -1
        if c == "\\":
            j += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return -1
        elif c == "/":
            return -1 if s[j - 1] in " \t" else j + 1
        j += 1
    return -1


def tokenize(src: str) -> list[Tok]:
    if len(src) > MAX_SOURCE:
        raise ScanError("The file is too long to check.")
    return _Lexer(src).tokens()


# ---------------------------------------------------------------- declarations

TYPE_KW = {"struct", "class", "enum", "actor", "protocol"}
MODIFIERS = {"public", "private", "fileprivate", "internal", "open", "package", "final", "static", "class", "override",
             "mutating", "nonmutating", "dynamic", "lazy", "weak", "unowned", "required", "convenience", "indirect",
             "nonisolated", "isolated", "consuming", "borrowing", "distributed", "optional", "prefix", "postfix",
             "infix", "__consuming", "unsafe", "sending"}
ACCESS = {"public", "private", "fileprivate", "internal", "open", "package"}
LAYOUT_MODS = {"lazy", "weak", "unowned"}
DECL_KW = {"func", "init", "deinit", "subscript", "var", "let", "case", "typealias", "associatedtype", "import",
           "extension", "operator", "precedencegroup", "macro"} | TYPE_KW
ACCESSOR_KW = {"get", "set", "_read", "_modify", "read", "modify", "unsafeAddress", "unsafeMutableAddress", "init"}
OBSERVER_KW = {"willSet", "didSet"}
CONTINUE_KW = {"where", "throws", "rethrows", "async", "as", "is"}


@dataclass
class Member:
    name: str
    sig: str
    line: int


@dataclass
class TypeDecl:
    name: str                    # qualified (Outer.Inner)
    kind: str                    # struct, class, enum, actor, protocol
    line: int
    header: str                  # generic parameters, inheritance and where clause, normalized
    final: bool = False
    stored: list[Member] = field(default_factory=list)
    cases: list[Member] = field(default_factory=list)
    vtable: list[Member] = field(default_factory=list)      # overridable class members, in declaration order
    requirements: list[Member] = field(default_factory=list)  # protocol requirements
    tails: dict[str, list[str]] = field(default_factory=dict)  # computed property -> its block's last tokens


@dataclass
class Scoped:
    """A declaration that belongs to a type (or extension) by name, or to the file when owner is ''."""
    owner: str
    name: str
    sig: str
    line: int


@dataclass
class FileModel:
    types: dict[str, TypeDecl] = field(default_factory=dict)
    conformances: list[tuple[str, str, int]] = field(default_factory=list)   # (extended type, inheritance, line)
    statics: dict[str, Scoped] = field(default_factory=dict)    # static and global stored properties
    aliases: dict[str, Scoped] = field(default_factory=dict)    # type aliases

    def add(self, table: dict[str, Scoped], item: Scoped) -> None:
        key = f"{item.owner}.{item.name}"
        k = 2
        while key in table:              # the same name in two #if branches
            key = f"{item.owner}.{item.name}#{k}"
            k += 1
        table[key] = item


def _norm(toks: list[Tok]) -> str:
    """Tokens as text with single spaces, so formatting never counts as a change."""
    return " ".join(t.text for t in toks)


class _Parser:
    def __init__(self, toks: list[Tok]):
        self.t = toks
        self.n = len(toks)
        self.match = self._match_brackets()
        self.model = FileModel()

    def _match_brackets(self) -> dict[int, int]:
        pairs = {")": "(", "]": "[", "}": "{"}
        stack: list[int] = []
        match: dict[int, int] = {}
        for i, tok in enumerate(self.t):
            if tok.kind != "punct":
                continue
            if tok.text in "([{":
                stack.append(i)
                if len(stack) > MAX_DEPTH:
                    raise ScanError("The code is nested too deeply to check.", tok.line)
            elif tok.text in ")]}":
                if not stack or self.t[stack[-1]].text != pairs[tok.text]:
                    raise ScanError(f"An unbalanced '{tok.text}'.", tok.line)
                match[stack.pop()] = i
        if stack:
            raise ScanError(f"An unclosed '{self.t[stack[-1]].text}'.", self.t[stack[-1]].line)
        return match

    def is_p(self, i: int, text: str) -> bool:
        return i < self.n and self.t[i].kind == "punct" and self.t[i].text == text

    def is_id(self, i: int, text: str | None = None) -> bool:
        return i < self.n and self.t[i].kind == "id" and (text is None or self.t[i].text == text)

    def skip_group(self, i: int) -> int:
        """i at an opening bracket -> index after its match; otherwise i + 1."""
        if i < self.n and self.t[i].kind == "punct" and self.t[i].text in "([{":
            return self.match[i] + 1
        return i + 1

    def _continues(self, prev: Tok, tok: Tok) -> bool:
        if prev.kind == "op" and not prev.sp and prev.text != "=":
            return False           # left-bound before a newline: a postfix operator (`Crash?`, `value!`)
        if prev.kind == "op" or (prev.kind == "punct" and prev.text in ",:.([") or prev.kind == "attr":
            return True
        if tok.kind == "op" and tok.text not in ("!",):
            return True
        if tok.kind == "punct" and tok.text in ".{":
            return True
        return tok.kind == "id" and tok.text in CONTINUE_KW

    def decl_end(self, i: int, end: int) -> int:
        """The end (exclusive) of the declaration or statement starting at i, within [i, end)."""
        j = i + 1 if i < end else i
        if i < end and self.t[i].kind == "punct" and self.t[i].text in "([{":
            j = self.skip_group(i)
        while j < end:
            tok = self.t[j]
            if tok.kind == "punct" and tok.text == ";":
                return j + 1
            if tok.kind == "punct" and tok.text in ")]}":
                return j
            if tok.nl and not self._continues(self.t[j - 1], tok):
                return j
            j = self.skip_group(j)
        return end

    # ---- a body of declarations: [i, end)
    def members(self, i: int, end: int, owner: TypeDecl | None, prefix: str, ext: bool = False) -> None:
        while i < end:
            tok = self.t[i]
            if tok.kind == "punct" and tok.text == ";":
                i += 1
                continue
            if tok.kind == "pound" and tok.text in ("#if", "#elseif"):
                i = self._line_end(i, end)
                continue
            if tok.kind == "pound" and tok.text in ("#else", "#endif"):
                i += 1
                continue
            start = i
            attrs: list[str] = []
            mods: list[str] = []
            while i < end:
                tok = self.t[i]
                if tok.kind == "attr":
                    text = tok.text
                    i += 1
                    if self.is_p(i, "(") and not self.t[i].sp:
                        text += _norm(self.t[i:self.match[i] + 1]).replace(" ", "")
                        i = self.match[i] + 1
                    attrs.append(text)
                elif tok.kind == "id" and tok.text in MODIFIERS and self._is_modifier(i):
                    mods.append(tok.text)
                    i += 1
                    if self.is_p(i, "(") and tok.text in ACCESS | {"unowned", "nonisolated"}:
                        i = self.match[i] + 1      # private(set), unowned(unsafe)
                else:
                    break
            if i >= end:
                break
            tok = self.t[i]
            kw = tok.text if tok.kind == "id" else None
            if kw in TYPE_KW and self.is_id(i + 1):
                i = self.type_decl(i, end, prefix, mods)
            elif kw == "extension":
                i = self.extension(i, end, prefix)
            elif kw in ("var", "let"):
                i = self.binding(i, end, owner, attrs, mods, ext, prefix)
            elif kw == "case" and owner is not None and owner.kind == "enum":
                i = self.enum_case(i, end, owner, mods)
            elif kw in ("func", "init", "subscript", "deinit"):
                i = self.function(i, end, owner, attrs, mods, ext)
            elif kw == "associatedtype" and owner is not None and owner.kind == "protocol":
                e = self.decl_end(i, end)
                owner.requirements.append(Member(_norm(self.t[i:e]), _norm(self.t[i:e]), tok.line))
                i = e
            elif kw == "typealias" and self.is_id(i + 1):
                e = self.decl_end(i, end)
                stop = e - 1 if self.is_p(e - 1, ";") else e
                self.model.add(self.model.aliases, Scoped(prefix[:-1], self.t[i + 1].text,
                                                          _norm(self.t[i + 1:stop]), tok.line))
                i = e
            elif tok.kind == "pound":
                i += 1                              # #warning(…), #Preview { … }, macro expansions
                if self.is_p(i, "(") and not self.t[i].sp:
                    i = self.match[i] + 1
                if self.is_p(i, "{"):
                    i = self.match[i] + 1
            else:
                i = self.decl_end(i, end)
            if i <= start:
                i = start + 1

    def _line_end(self, i: int, end: int) -> int:
        line = self.t[i].line
        j = i + 1
        while j < end and self.t[j].line == line and not self.t[j].nl:
            j += 1
        return j

    def _is_modifier(self, i: int) -> bool:
        """`class` and friends are modifiers only when another modifier or a declaration keyword follows."""
        if i + 1 >= self.n:
            return False
        nxt = self.t[i + 1]
        if self.t[i].text in ("class",):
            return nxt.kind == "id" and (nxt.text in DECL_KW - TYPE_KW or nxt.text in MODIFIERS)
        if nxt.kind == "punct" and nxt.text == "(":
            return True
        return nxt.kind in ("id", "attr") or (nxt.kind == "pound")

    def _header(self, i: int, end: int) -> tuple[list[Tok], int]:
        """Tokens from i up to the body's '{' (exclusive) and the '{' index (or the declaration end)."""
        j = i
        while j < end:
            tok = self.t[j]
            if tok.kind == "punct" and tok.text == "{":
                return self.t[i:j], j
            if tok.kind == "punct" and tok.text in "([":
                j = self.match[j] + 1
                continue
            if tok.kind == "punct" and tok.text in ";}":
                break
            j += 1
        return self.t[i:j], -1

    def type_decl(self, i: int, end: int, prefix: str, mods: list[str]) -> int:
        kw, name_tok = self.t[i], self.t[i + 1]
        name = prefix + name_tok.text
        header, brace = self._header(i + 2, end)
        decl = TypeDecl(name, kw.text, kw.line, _norm(header), final="final" in mods or kw.text == "actor")
        key = name
        k = 2
        while key in self.model.types:
            key = f"{name}#{k}"
            k += 1
        self.model.types[key] = decl
        if brace < 0:
            return self.decl_end(i, end)
        self.members(brace + 1, self.match[brace], decl, name + ".")
        return self.match[brace] + 1

    def extension(self, i: int, end: int, prefix: str) -> int:
        j = i + 1
        parts = []
        while j < end and (self.t[j].kind == "id" or self.is_p(j, ".")):
            parts.append(self.t[j].text)
            j += 1
        name = "".join(parts)
        header, brace = self._header(j, end)
        inherit = []
        if header and header[0].kind == "punct" and header[0].text == ":":
            for tok in header[1:]:
                if tok.kind == "id" and tok.text == "where":
                    break
                inherit.append(tok)
        if inherit:
            self.model.conformances.append((name, _norm(inherit), self.t[i].line))
        if brace < 0:
            return self.decl_end(i, end)
        self.members(brace + 1, self.match[brace], None, name + ".", ext=True)
        return self.match[brace] + 1

    def binding(self, i: int, end: int, owner: TypeDecl | None, attrs: list[str], mods: list[str],
                ext: bool, prefix: str = "") -> int:
        kw = self.t[i]
        e = self.decl_end(i, end)
        static = "static" in mods
        class_member = "class" in mods          # `class var`: computed, overridable unless final
        j = i + 1
        while j < e:
            name_toks: list[Tok]
            if self.is_p(j, "("):
                name_toks = self.t[j:self.match[j] + 1]
                j = self.match[j] + 1
            elif j < e and self.t[j].kind == "id":
                name_toks = [self.t[j]]
                j += 1
            else:
                return e
            name, line = _norm(name_toks), name_toks[0].line
            type_toks: list[Tok] = []
            init_toks: list[Tok] = []
            block: tuple[int, int] | None = None
            if self.is_p(j, ":"):
                j += 1
                while j < e and not (self.t[j].kind == "op" and self.t[j].text == "=") and not self.is_p(j, ",") \
                        and not self.is_p(j, "{"):
                    k = self.skip_group(j)
                    type_toks += self.t[j:k]
                    j = k
            if j < e and self.t[j].kind == "op" and self.t[j].text == "=":
                j += 1
                while j < e and not self.is_p(j, ","):
                    if self.is_p(j, "{") and self._observer_block(j):
                        break
                    k = self.skip_group(j)
                    init_toks += self.t[j:k]
                    j = k
            if self.is_p(j, "{"):
                block = (j, self.match[j])
                j = self.match[j] + 1
            computed = block is not None and not self._observer_block(block[0])
            layout = [m for m in mods if m in LAYOUT_MODS]
            typ = f": {_norm(type_toks)}" if type_toks else " = " + _inferred(init_toks)
            if owner is not None and owner.kind == "protocol":
                owner.requirements.append(Member(name, f"{'static ' if static else ''}{kw.text} {name}: "
                                                       f"{_norm(type_toks)} "
                                                       f"{_norm(self.t[block[0]:block[1] + 1]) if block else ''}",
                                                 line))
            elif class_member:
                if owner is not None and owner.kind == "class" and not ext and (
                        "override" in mods or (not owner.final and "final" not in mods)):
                    owner.vtable.append(Member(name, ("override " if "override" in mods else "") +
                                               f"class var {name}: {_norm(type_toks)}", line))
            elif static or (owner is None and not ext):
                if not computed:        # the app's storage stays in use, so its type can't change
                    self.model.add(self.model.statics, Scoped(
                        prefix[:-1], name, " ".join([*attrs, *layout, kw.text, f"{name}{typ}"]), line))
            elif owner is not None:
                if computed:
                    owner.tails[name] = [t.text for t in self.t[max(block[0] + 1, block[1] - 8):block[1]]]
                    if owner.kind == "class" and not owner.final and "final" not in mods:
                        owner.vtable.append(Member(name, f"var {name}: {_norm(type_toks)}", line))
                    elif "override" in mods and owner.kind == "class":
                        owner.vtable.append(Member(name, f"override var {name}: {_norm(type_toks)}", line))
                elif not ext:
                    sig = " ".join([*attrs, *layout, kw.text, f"{name}{typ}"])
                    owner.stored.append(Member(name, sig, line))
                    if owner.kind == "class" and "override" in mods:
                        owner.vtable.append(Member(name, f"override var {name}", line))
            if self.is_p(j, ","):
                j += 1
                continue
            break
        return e

    def _observer_block(self, brace: int) -> bool:
        k = brace + 1
        while k < self.match[brace] and self.t[k].kind == "attr":
            k += 1
        return k < self.n and self.t[k].kind == "id" and self.t[k].text in OBSERVER_KW

    def enum_case(self, i: int, end: int, owner: TypeDecl, mods: list[str]) -> int:
        e = self.decl_end(i, end)
        j = i + 1
        indirect = "indirect " if "indirect" in mods else ""
        while j < e:
            if self.t[j].kind != "id":
                break
            name, line = self.t[j].text, self.t[j].line
            j += 1
            payload = ""
            if self.is_p(j, "("):
                payload = _norm(self.t[j:self.match[j] + 1])
                j = self.match[j] + 1
            if j < e and self.t[j].kind == "op" and self.t[j].text == "=":
                j += 1
                while j < e and not self.is_p(j, ","):
                    j = self.skip_group(j)
            owner.cases.append(Member(name, f"{indirect}case {name}{payload}", line))
            if self.is_p(j, ","):
                j += 1
                continue
            break
        return e

    def function(self, i: int, end: int, owner: TypeDecl | None, attrs: list[str], mods: list[str],
                 ext: bool) -> int:
        kw = self.t[i]
        e = self.decl_end(i, end)
        header, brace = self._header(i, e)      # a requirement without a body ends at its line
        stop = brace if brace >= 0 else e
        sig_toks = self.t[i:stop]
        name = self._selector(i, stop)
        sig = _norm(sig_toks)
        if owner is not None and kw.text != "deinit":
            if owner.kind == "protocol":
                owner.requirements.append(Member(name, sig, kw.line))
            elif owner.kind == "class" and not ext:
                static = "static" in mods
                overridable = not owner.final and "final" not in mods and not static
                if overridable or "override" in mods:
                    owner.vtable.append(Member(name, ("override " if "override" in mods else "") + sig, kw.line))
        if brace >= 0:
            return self.match[brace] + 1
        return stop


    def _selector(self, i: int, stop: int) -> str:
        """`func f(_ a: Int, b c: String)` -> 'f(_:b:)' (overloads differ by their argument labels)."""
        kw = self.t[i]
        j = i + 1
        name = kw.text
        if kw.text == "func" and j < stop:
            name = self.t[j].text
            j += 1
        while j < stop and not self.is_p(j, "("):
            j = self.skip_group(j) if self.t[j].kind == "punct" and self.t[j].text == "[" else j + 1
        if j >= stop:
            return name
        labels, seg, k, close = [], [], j + 1, self.match[j]
        while k <= close:
            if k == close or self.is_p(k, ","):
                ids = []
                for t in seg:
                    if t.kind == "punct" and t.text == ":":
                        break
                    if t.kind == "id":
                        ids.append(t.text)
                if ids:
                    labels.append(ids[0] + ":")
                seg = []
                k += 1
                continue
            seg.append(self.t[k])
            k = self.skip_group(k)
        return f"{name}({''.join(labels)})"


def _inferred(toks: list[Tok]) -> str:
    """What decides an unannotated stored property's type: a literal's kind, a call's callee, else the text."""
    if not toks:
        return "?"
    body = toks[1:] if toks[0].kind == "op" and toks[0].text == "-" and len(toks) > 1 else toks
    if len(body) == 1:
        t = body[0]
        if t.kind == "num":
            low = t.text.lower()
            is_float = ("." in t.text or ("e" in low and not low.startswith("0x")) or
                        (low.startswith("0x") and "p" in low))
            return "<float literal>" if is_float else "<integer literal>"
        if t.kind == "str":
            return "<string literal>"
        if t.kind == "id" and t.text in ("true", "false"):
            return "<bool literal>"
    head: list[Tok] = []
    for k, t in enumerate(toks):
        if t.kind == "punct" and t.text in "({" and head:
            return _norm(head) + t.text
        if t.kind == "id" or (t.kind == "punct" and t.text == "." and head):
            head.append(t)
            continue
        break
    else:
        return _norm(head)
    return _norm(toks)


def scan(source: str) -> FileModel:
    """The declarations of one Swift file (ScanError when it can't be followed)."""
    p = _Parser(tokenize(source))
    p.members(0, p.n, None, "")
    return p.model


# ---------------------------------------------------------------- comparing

def _blocker(kind: str, file: str, message: str, line: int | None = None, type_: str | None = None,
             name: str | None = None) -> dict:
    b: dict = {"kind": kind}
    if type_:
        b["type"] = type_
    if name:
        b["name"] = name
    b["file"] = file
    if line:
        b["line"] = line
    b["message"] = message
    return b


def _display(name: str) -> str:
    return name.split("#", 1)[0]


def _keyed(members: list[Member]) -> dict[str, Member]:
    """Members by name, overloads with the same labels (`f(_:)` taking Int, then String) numbered in order."""
    out: dict[str, Member] = {}
    for m in members:
        key, k = m.name, 2
        while key in out:
            key = f"{m.name}#{k}"
            k += 1
        out[key] = m
    return out


def _compare_members(old: list[Member], new: list[Member], kind: str, tname: str, file: str, what: str,
                     out: list[dict]) -> None:
    olds, news = _keyed(old), _keyed(new)
    old_keys, new_keys = list(olds), list(news)
    for key, m in news.items():
        if key not in olds:
            out.append(_blocker(kind, file, f"Adds {what} {m.name} to {_display(tname)}.", m.line, tname, m.name))
        elif olds[key].sig != m.sig:
            out.append(_blocker(kind, file, f"Changes {what} {m.name} of {_display(tname)}.", m.line, tname, m.name))
    for key, m in olds.items():
        if key not in news:
            out.append(_blocker(kind, file, f"Removes {what} {m.name} from {_display(tname)}.", None, tname, m.name))
    common_old = [k for k in old_keys if k in news]
    common_new = [k for k in new_keys if k in olds]
    if common_old != common_new:
        first = next(news[b] for a, b in zip(common_old, common_new) if a != b)
        out.append(_blocker(kind, file, f"Reorders the {what.split(' ', 1)[-1]}s of {_display(tname)}.", first.line,
                            tname))


def _compare_scoped(old: dict[str, Scoped], new: dict[str, Scoped], kind: str, file: str, out: list[dict],
                    of: Callable[[Scoped], str], from_: Callable[[Scoped], str]) -> None:
    """Changed or removed declarations are blockers; new ones are fine (the patch brings its own)."""
    for key, d in new.items():
        o = old.get(key)
        if o is not None and o.sig != d.sig:
            out.append(_blocker(kind, file, f"Changes the {of(d)}.", d.line, d.owner or None, d.name))
    for key, o in old.items():
        if key not in new:
            out.append(_blocker(kind, file, f"Removes the {from_(o)}.", None, o.owner or None, o.name))


def structural_blockers(old_source: str, new_source: str, file: str) -> list[dict]:
    """Blockers for the edits between the baseline and the new text of one file (see the module doc)."""
    try:
        old = scan(old_source)
    except ScanError as exc:
        return [_blocker("unparsed", file, f"iOS Studio couldn't read the baseline of this file ({exc}).", exc.line)]
    try:
        new = scan(new_source)
    except ScanError as exc:
        return [_blocker("unparsed", file, f"iOS Studio couldn't read this file: {exc}", exc.line)]
    out: list[dict] = []
    for key, t in new.types.items():
        if key not in old.types:
            out.append(_blocker("new-type", file, f"Adds the {t.kind} {_display(key)}.", t.line, _display(key)))
    for key, t in old.types.items():
        if key not in new.types:
            out.append(_blocker("removed-type", file, f"Removes the {t.kind} {_display(key)}.", None, _display(key)))
    for key, t in new.types.items():
        o = old.types.get(key)
        if o is None:
            continue
        if o.kind != t.kind or o.header != t.header or o.final != t.final:
            out.append(_blocker("generic-signature", file,
                                f"Changes the declaration of {_display(key)} (its kind, generic parameters, superclass "
                                f"or conformances).", t.line, _display(key)))
        _compare_members(o.stored, t.stored, "stored-property", key, file, "the stored property", out)
        _compare_members(o.cases, t.cases, "enum-case", key, file, "the case", out)
        if t.kind == "class":
            _compare_members(o.vtable, t.vtable, "class-method", key, file, "the overridable member", out)
        if t.kind == "protocol":
            _compare_members(o.requirements, t.requirements, "class-method", key, file, "the requirement", out)
    _compare_scoped(old.statics, new.statics, "stored-property", file, out,
                    lambda d: f"static property {d.name} of {d.owner}" if d.owner else f"global variable {d.name}",
                    lambda d: f"static property {d.name} from {d.owner}" if d.owner else f"global variable {d.name}")
    _compare_scoped(old.aliases, new.aliases, "type-alias", file, out,
                    lambda d: f"type alias {d.name} of {d.owner}" if d.owner else f"type alias {d.name}",
                    lambda d: f"type alias {d.name} from {d.owner}" if d.owner else f"type alias {d.name}")
    old_conf = sorted((n, h) for n, h, _ in old.conformances)
    new_conf = sorted((n, h) for n, h, _ in new.conformances)
    if old_conf != new_conf:
        for n, h, line in new.conformances:
            if (n, h) not in old_conf:
                out.append(_blocker("generic-signature", file, f"Adds the conformance of {n} to {h}.", line, n))
        for n, h, _line in old.conformances:
            if (n, h) not in new_conf:
                out.append(_blocker("generic-signature", file, f"Removes the conformance of {n} to {h}.", None, n))
    return out


def ends_with_call(source: str, type_name: str, prop: str = "body", call: str = "hotReloadable") -> bool:
    """Does `prop` of `type_name` end with `.call()` in this source?"""
    try:
        model = scan(source)
    except ScanError:
        return False
    t = model.types.get(type_name)
    if t is None:
        t = next((x for k, x in model.types.items() if k.rsplit(".", 1)[-1] == type_name), None)
    tail = t.tails.get(prop) if t else None
    return bool(tail) and tail[-4:] == [".", call, "(", ")"]


# ---------------------------------------------------------------- opaque result types

DESCRIPTOR_SUFFIX = "QOMQ"
ARM64_RELOC_SUBTRACTOR = 1
ANYVIEW = "7SwiftUI7AnyViewV"


def opaque_descriptors(obj: macho.MachO) -> dict[str, list[str]]:
    """{descriptor symbol: the names its relocations point at, in address order}: what each `some T` resolves to."""
    out: dict[str, list[str]] = {}
    by_sect: dict[int, list[macho.Relocation]] = {}
    for sym in obj.symbols:
        if not sym.name.endswith(DESCRIPTOR_SUFFIX) or sym.kind != macho.N_SECT:
            continue
        sect = obj.section_by_index(sym.sect)
        rng = obj.symbol_range(sym)
        if sect is None or rng is None:
            continue
        if sect.index not in by_sect:
            by_sect[sect.index] = obj.relocations(sect)
        lo, hi = rng
        targets = sorted((r.address, _canon(r.symbol)) for r in by_sect[sect.index]
                         if lo <= r.address < hi and r.extern and r.type != ARM64_RELOC_SUBTRACTOR and r.symbol
                         and r.symbol != sym.name)
        out[sym.name] = [name for _a, name in targets]
    return out


def _canon(name: str | None) -> str:
    """A relocation target without the numbering the compiler appends to private copies (`….1`)."""
    return re.sub(r"\.\d+$", "", name or "")


def underlying_is_anyview(targets: list[str]) -> bool:
    """Is the underlying type in these descriptor targets exactly SwiftUI.AnyView (`_symbolic _____ 7SwiftUI7AnyViewV`)?"""
    for t in targets:
        if not t.startswith("_symbolic "):
            continue
        parts = t.split(" ")[1:]
        if not parts or parts == ["x"]:
            continue                     # `_symbolic x`: the generic parameter, not the underlying type
        return parts in (["_____", ANYVIEW], [f"$s{ANYVIEW}"], [ANYVIEW])
    return False


def _identifiers(s: str, limit: int = 8) -> list[str]:
    """The leading identifiers of a Swift mangling (after `$s`), with word substitutions (`0A3Row` -> `CrashRow`
    after `8CrashLab`) and private discriminators (`33_…LL`) handled, stopping at the first other operator."""
    words: list[str] = []
    names: list[str] = []
    i = 0

    def add_words(lit: str) -> None:
        start = -1
        for k in range(len(lit) + 1):
            c = lit[k] if k < len(lit) else ""
            if start >= 0 and (c in ("_", "") or (c.isupper() and not lit[k - 1].isupper())):
                if k - start >= 2 and len(words) < 26:
                    words.append(lit[start:k])
                start = -1
            if start < 0 and c and not c.isdigit() and c != "_":
                start = k

    while i < len(s) and len(names) < limit:
        if not s[i].isdigit():
            if s.startswith("LL", i) and names and names[-1].startswith("_"):
                names.pop()                  # a private discriminator: not a name
                i += 2
                continue
            if s[i] in "VCOE" and names:
                i += 1                       # the kind after a nominal type's name
                continue
            break
        subst = s[i] == "0" and i + 1 < len(s) and s[i + 1] != "0"
        if s.startswith("00", i):
            break                            # punycode: not worth decoding for a message
        if subst:
            i += 1
        ident = ""
        while True:
            while subst and i < len(s) and s[i].isalpha():
                c = s[i]
                i += 1
                idx = ord(c.lower()) - ord("a")
                if idx >= len(words):
                    return names
                ident += words[idx]
                if c.isupper():
                    subst = False
            if i < len(s) and s[i] == "0" and ident:
                i += 1
                break
            m = re.match(r"\d+", s[i:])
            if not m:
                if ident:
                    break
                return names
            n = int(m.group(0))
            i += len(m.group(0))
            lit = s[i:i + n]
            if len(lit) < n:
                return names
            ident += lit
            add_words(lit)
            i += n
            if not subst:
                break
        names.append(ident)
    return names


def describe_opaque(symbol: str) -> tuple[str | None, str | None]:
    """(type name, property/function name) from an opaque descriptor's mangling, best effort:
    `_$s8HotSpike11ContentViewV4bodyQrvpQOMQ` -> ('ContentView', 'body')."""
    m = re.match(r"_?\$s(.*)", symbol)
    if not m:
        return None, None
    names = _identifiers(m.group(1))
    if len(names) >= 3:
        return names[-2], names[-1]
    if len(names) == 2:
        return None, names[-1]
    return None, None


def opaque_blockers(baseline_o: str | Path | macho.MachO, patch_o: str | Path | macho.MachO, new_source: str,
                    file: str) -> list[dict]:
    """A blocker for each opaque result type whose underlying type differs between the baseline and the patch."""
    old = baseline_o if isinstance(baseline_o, macho.MachO) else macho.MachO.load(baseline_o)
    new = patch_o if isinstance(patch_o, macho.MachO) else macho.MachO.load(patch_o)
    od, nd = opaque_descriptors(old), opaque_descriptors(new)
    out: list[dict] = []
    for sym in sorted(set(od) | set(nd)):
        if sym in od and sym in nd and od[sym] == nd[sym]:
            continue
        if sym not in od:
            continue         # a new `some` function: nothing in the app uses its descriptor yet
        tname, prop = describe_opaque(sym)
        label = f"{tname}.{prop}" if tname and prop else (prop or "A function")
        if sym not in nd:
            out.append(_blocker("opaque-type", file, f"{label} no longer returns an opaque type (some …).",
                                None, tname, prop))
            continue
        has = bool(tname and prop and ends_with_call(new_source, tname, prop))
        if has and underlying_is_anyview(nd[sym]) and not underlying_is_anyview(od[sym]):
            msg = (f"{label} now ends with .hotReloadable(): the app has to be built once with it before reloads "
                   f"can change its views.")
        elif has and not underlying_is_anyview(nd[sym]):
            msg = (f"{label} ends with .hotReloadable(), but its view type still changed (.hotReloadable() has to "
                   f"return AnyView). Build the app again to apply this change.")
        else:
            msg = (f"{label} now returns a different view type. End {prop or 'it'} with .hotReloadable() (and build "
                   f"the app once) so its structure can change during reloads.")
        out.append(_blocker("opaque-type", file, msg, None, tname, prop))
    return out


# ---------------------------------------------------------------- interposable functions

@dataclass
class Interposable:
    swappable: list[str]       # patch functions the app defines and calls through an indirect pointer
    missing: list[str]         # patch functions the app defines but calls directly (the change won't show)
    new: list[str]             # patch functions the app doesn't have (only patch code calls them)


def interposable(patch: macho.MachO, app: macho.MachO) -> Interposable:
    funcs = patch.exported("__TEXT", "__text")
    app_defined = app.defined()
    bound = app.indirect_names()
    swappable, missing, new = [], [], []
    for name in sorted(funcs):
        if name not in app_defined:
            new.append(name)
        elif name.endswith("Ma"):          # metadata accessors are kept, never swapped
            continue
        elif name in bound:
            swappable.append(name)
        else:
            missing.append(name)
    return Interposable(swappable, missing, new)


def require_interposable(patch: macho.MachO, app: macho.MachO) -> Interposable:
    """interposable(), raising not-interposable when the patch has functions to swap but none can be."""
    res = interposable(patch, app)
    if not res.swappable and res.missing:
        raise HelperError("not-interposable",
                          "The app on the iPhone wasn't built for hot reload, so its functions can't be swapped. "
                          "Turn hot reload on and run the app again.",
                          fix("Run again", "projects", action="hot-reload-restart"))
    return res
