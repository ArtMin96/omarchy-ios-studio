"""Package.swift edited as text: removing a package (there is no `swift package remove-dependency`; stdlib only).

    remove_package(text, identity, url=None, names=(), products=(), local_targets=())
        -> RemoveEdit(text, removed_package, target_deps, problems)
    Removes the `.package(...)` entry of that package from whatever array holds it, every
    `.product(name:…, package: "<identity or name>")` from target dependency arrays, and bare `"Product"` /
    `.byName(name: "Product")` dependencies naming one of the package's products. It works on tokens from a small
    Swift lexer (string literals including multi-line and raw ones with interpolation, // and nested /* */
    comments, balanced brackets), so commas, comments and parentheses inside strings never confuse it. An entry
    on its own line(s) is removed with its line(s) and a comment after it on the same line; an entry sharing a
    line is removed with its comma; an array left with no entries becomes `[]`. Entries this can't find as plain
    array elements (built in a loop, from variables, inside .append(...)) are reported in `problems`, and the
    caller refuses the edit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

from iosstudio.features.packages.manifest import identity_of, normalize_location

# ================================================================ Swift lexer

class Tok(NamedTuple):
    kind: str       # ws nl comment string ident number punct
    start: int
    end: int


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|`[^`\n]+`")
_NUMBER = re.compile(r"[0-9][0-9A-Za-z_.]*")
_WS = re.compile(r"[ \t\f\v]+")


class LexError(ValueError):
    pass


def _skip_comment(text: str, i: int) -> int:
    """i at '/*' -> index after the matching '*/' (comments nest in Swift)."""
    depth, j, n = 0, i, len(text)
    while j < n:
        if text.startswith("/*", j):
            depth += 1
            j += 2
        elif text.startswith("*/", j):
            depth -= 1
            j += 2
            if depth == 0:
                return j
        else:
            j += 1
    raise LexError("A /* comment isn't closed.")


def _string_end(text: str, i: int) -> int:
    """i at the first '#' or '"' of a string literal -> index after its closing delimiter."""
    n = len(text)
    hashes = 0
    while i + hashes < n and text[i + hashes] == "#":
        hashes += 1
    j = i + hashes
    if not text.startswith('"', j):
        raise LexError("Not a string literal.")
    multi = text.startswith('"""', j)
    j += 3 if multi else 1
    close = ('"""' if multi else '"') + "#" * hashes
    esc = "\\" + "#" * hashes
    while j < n:
        if text.startswith(close, j):
            return j + len(close)
        if text.startswith(esc, j):
            k = j + len(esc)
            if k < n and text[k] == "(":
                j = _expr_end(text, k)       # interpolation: a balanced expression
                continue
            j = k + 1                        # an escaped character
            continue
        if text[j] == "\n" and not multi:
            raise LexError("A string literal isn't closed on its line.")
        j += 1
    raise LexError("A string literal isn't closed.")


def _expr_end(text: str, i: int) -> int:
    """i at '(' of an interpolation -> index after its matching ')', skipping strings and comments inside."""
    depth, j, n = 0, i, len(text)
    while j < n:
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        elif c == '"' or (c == "#" and _raw_string_at(text, j)):
            j = _string_end(text, j)
            continue
        elif text.startswith("//", j):
            nl = text.find("\n", j)
            j = n if nl < 0 else nl
            continue
        elif text.startswith("/*", j):
            j = _skip_comment(text, j)
            continue
        j += 1
    raise LexError("An interpolation \\( isn't closed.")


def _raw_string_at(text: str, i: int) -> bool:
    j = i
    while j < len(text) and text[j] == "#":
        j += 1
    return j > i and text.startswith('"', j)


def tokenize(text: str) -> list[Tok]:
    out: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\n" or c == "\r":
            j = i + (2 if text.startswith("\r\n", i) else 1)
            out.append(Tok("nl", i, j))
            i = j
            continue
        m = _WS.match(text, i)
        if m:
            out.append(Tok("ws", i, m.end()))
            i = m.end()
            continue
        if text.startswith("//", i):
            nl = text.find("\n", i)
            j = n if nl < 0 else nl
            out.append(Tok("comment", i, j))
            i = j
            continue
        if text.startswith("/*", i):
            j = _skip_comment(text, i)
            out.append(Tok("comment", i, j))
            i = j
            continue
        if c == '"' or (c == "#" and _raw_string_at(text, i)):
            j = _string_end(text, i)
            out.append(Tok("string", i, j))
            i = j
            continue
        m = _IDENT.match(text, i)
        if m:
            out.append(Tok("ident", i, m.end()))
            i = m.end()
            continue
        m = _NUMBER.match(text, i)
        if m:
            out.append(Tok("number", i, m.end()))
            i = m.end()
            continue
        out.append(Tok("punct", i, i + 1))
        i += 1
    return out


def string_value(text: str, tok: Tok) -> str | None:
    """The value of a string literal token without interpolation, else None."""
    s = text[tok.start:tok.end]
    hashes = len(s) - len(s.lstrip("#"))
    body = s[hashes:len(s) - hashes]
    if body.startswith('"""'):
        lines = body[3:-3].split("\n")[1:]          # the text starts on the line after the opening quotes
        indent = lines[-1] if lines and not lines[-1].strip() else ""
        body = "\n".join(ln[len(indent):] if ln.startswith(indent) else ln.lstrip() for ln in lines[:-1])
    else:
        body = body[1:-1]
    esc = "\\" + "#" * hashes
    if esc + "(" in body:
        return None
    if not hashes:
        body = re.sub(r"\\u\{([0-9A-Fa-f]{1,8})\}", lambda m: chr(int(m.group(1), 16)), body)
        body = re.sub(r'\\(.)', lambda m: {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(m.group(1), m.group(1)),
                      body)
    return body


# ================================================================ structure

@dataclass
class Code:
    """Significant tokens (no whitespace, newlines or comments) with bracket matches and parents."""
    text: str
    toks: list[Tok]                      # every token
    sig: list[int] = field(default_factory=list)          # indices into toks
    match: dict[int, int] = field(default_factory=dict)   # sig index of an opener <-> its closer
    parent: list[int] = field(default_factory=list)       # sig index of the innermost opener holding each, or -1

    @classmethod
    def parse(cls, text: str) -> "Code":
        toks = tokenize(text)
        code = cls(text, toks)
        code.sig = [i for i, t in enumerate(toks) if t.kind not in ("ws", "nl", "comment")]
        stack: list[int] = []
        pairs = {")": "(", "]": "[", "}": "{"}
        for si in range(len(code.sig)):
            code.parent.append(stack[-1] if stack else -1)
            s = code.s(si)
            if s in ("(", "[", "{"):
                stack.append(si)
            elif s in pairs:
                if not stack or code.s(stack[-1]) != pairs[s]:
                    raise LexError(f"Unbalanced '{s}' in Package.swift.")
                op = stack.pop()
                code.match[op] = si
                code.match[si] = op
                code.parent[si] = code.parent[op]
        if stack:
            raise LexError(f"Unclosed '{code.s(stack[-1])}' in Package.swift.")
        return code

    def tok(self, si: int) -> Tok:
        return self.toks[self.sig[si]]

    def s(self, si: int) -> str:
        t = self.tok(si)
        return self.text[t.start:t.end]

    def kind(self, si: int) -> str:
        return self.tok(si).kind

    def items(self, open_si: int) -> list[tuple[int, int, int | None]]:
        """Top-level items between an opener and its closer: [(first sig, last sig, comma sig or None)]."""
        close = self.match[open_si]
        out, start, i = [], open_si + 1, open_si + 1
        while i < close:
            s = self.s(i)
            if s in ("(", "[", "{"):
                i = self.match[i] + 1
                continue
            if s == ",":
                if i > start:
                    out.append((start, i - 1, i))
                start = i + 1
            i += 1
        if start < close:
            out.append((start, close - 1, None))
        return out

    def args(self, open_si: int) -> list[tuple[str | None, int, int]]:
        """Arguments of a call: [(label or None, first value sig, last value sig)]."""
        out = []
        for a, b, _ in self.items(open_si):
            if b > a and self.kind(a) == "ident" and self.s(a + 1) == ":":
                out.append((self.s(a).strip("`"), a + 2, b))
            else:
                out.append((None, a, b))
        return out

    def arg_string(self, open_si: int, label: str) -> str | None:
        for lab, a, b in self.args(open_si):
            if lab == label and a == b and self.kind(a) == "string":
                return string_value(self.text, self.tok(a))
        return None

    def has_arg(self, open_si: int, label: str) -> bool:
        return any(lab == label for lab, _a, _b in self.args(open_si))

    def calls(self, name: str) -> list[tuple[int, int]]:
        """`.name(` member calls: [(sig of the '.', sig of '(')]."""
        out = []
        for i in range(len(self.sig) - 2):
            if self.s(i) == "." and self.s(i + 1) == name and self.s(i + 2) == "(" and self.kind(i + 1) == "ident":
                out.append((i, i + 2))
        return out

    def element_of(self, first: int, last: int) -> tuple[int, int] | None:
        """(array opener sig, item index) when the expression first..last is a whole item of an array literal,
        allowing a type qualifier before it (`Package.Dependency.package(...)`)."""
        op = self.parent[first]
        if op < 0 or self.s(op) != "[":
            return None
        for idx, (a, b, _c) in enumerate(self.items(op)):
            if a <= first and last == b:
                if all(self.kind(k) == "ident" or self.s(k) == "." for k in range(a, first)):
                    return op, idx
                return None
        return None


# ================================================================ remove

@dataclass
class RemoveEdit:
    text: str
    removed_package: bool = False
    target_deps: list[dict] = field(default_factory=list)   # [{target, product}]
    problems: list[str] = field(default_factory=list)


TARGET_CALLS = {"target", "executableTarget", "testTarget", "plugin", "macro", "binaryTarget", "systemLibrary"}


def _line_no(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _enclosing_call(code: Code, si: int) -> str | None:
    """Name of the call whose parentheses hold sig index si (`.target(`, `Package(`), if any."""
    op = code.parent[si]
    while op >= 0 and code.s(op) != "(":
        op = code.parent[op]
    if op < 1:
        return None
    return code.s(op - 1) if code.kind(op - 1) == "ident" else None


def _target_name(code: Code, si: int) -> str | None:
    op = code.parent[si]
    while op >= 0:
        if code.s(op) == "(" and op >= 1 and code.s(op - 1) in TARGET_CALLS:
            return code.arg_string(op, "name")
        op = code.parent[op]
    return None


def remove_package(text: str, identity: str, url: str | None = None, names: tuple[str, ...] | list[str] = (),
                   products: tuple[str, ...] | list[str] = (), local_targets: tuple[str, ...] | list[str] = ()
                   ) -> RemoveEdit:
    """Package.swift text without the package `identity` and the target dependencies on it (see module doc).
    `url`: the declared URL or path (matched too); `names`: other names product references may use (a legacy
    `.package(name:)`); `products`: the package's product names, for bare-string dependencies; `local_targets`:
    this package's own target names (a bare string naming one of them is a target, never removed)."""
    code = Code.parse(text)
    ident = identity.lower()
    pkg_names = {ident} | {n.lower() for n in names if n}
    product_set = {p for p in products if p} - set(local_targets)
    url_norm = normalize_location(url) if url else None
    removals: dict[int, set[int]] = {}       # array opener sig -> item indices to drop
    edit = RemoveEdit(text)

    def drop(first: int, last: int, what: str) -> bool:
        el = code.element_of(first, last)
        if el is None:
            line = _line_no(text, code.tok(first).start)
            edit.problems.append(f"{what} on line {line} isn't a plain array entry, so it can't be removed safely.")
            return False
        removals.setdefault(el[0], set()).add(el[1])
        return True

    # .package(...) entries
    opaque: list[int] = []
    for dot, op in code.calls("package"):
        if _enclosing_call(code, dot) in TARGET_CALLS:
            continue
        loc = code.arg_string(op, "url") or code.arg_string(op, "path") or code.arg_string(op, "id")
        legacy = code.arg_string(op, "name")
        if loc is not None:
            hit = identity_of(loc) == ident or (url_norm is not None and normalize_location(loc) == url_norm) \
                or (code.has_arg(op, "id") and loc.lower() == ident)
        else:
            hit = legacy is not None and legacy.lower() in pkg_names
            if not hit and any(code.has_arg(op, k) for k in ("url", "path", "id")):
                opaque.append(_line_no(text, code.tok(dot).start))
        if hit:
            if legacy:
                pkg_names.add(legacy.lower())
            if drop(dot, code.match[op], "The .package entry"):
                edit.removed_package = True
    if not edit.removed_package and opaque:
        many = len(opaque) > 1
        edit.problems.append(f"The .package entr{'ies on lines' if many else 'y on line'} "
                             f"{', '.join(map(str, opaque))} use{'' if many else 's'} a computed URL or path, so "
                             f"{'they' if many else 'it'} can't be matched.")

    # .product(name:, package:) anywhere
    for dot, op in code.calls("product"):
        pkg = code.arg_string(op, "package")
        name = code.arg_string(op, "name")
        if pkg is None and code.has_arg(op, "package"):
            continue
        if (pkg is not None and pkg.lower() in pkg_names) or (pkg is None and name in product_set):
            if drop(dot, code.match[op], f".product(name: \"{name}\")"):
                edit.target_deps.append({"target": _target_name(code, dot), "product": name})

    # bare "Product" and .byName(name: "Product") in a target's dependencies: [...]
    if product_set:
        for i in range(len(code.sig) - 2):
            if not (code.s(i) == "dependencies" and code.s(i + 1) == ":" and code.s(i + 2) == "["):
                continue
            if _enclosing_call(code, i) not in TARGET_CALLS:
                continue
            arr = i + 2
            for idx, (a, b, _c) in enumerate(code.items(arr)):
                name = None
                if a == b and code.kind(a) == "string":
                    name = string_value(text, code.tok(a))
                elif code.s(a) == "." and code.s(a + 1) == "byName" and code.s(a + 2) == "(" \
                        and code.match.get(a + 2) == b:
                    name = code.arg_string(a + 2, "name")
                if name and name in product_set:
                    removals.setdefault(arr, set()).add(idx)
                    edit.target_deps.append({"target": _target_name(code, a), "product": name})

    edits: list[tuple[int, int, str]] = []
    for arr, idxs in removals.items():
        edits += _array_edits(code, arr, sorted(idxs))
    edits.sort(key=lambda e: e[0], reverse=True)
    out, last_start = text, len(text) + 1
    for start, end, rep in edits:
        if end > last_start:     # overlapping edits can't happen for distinct arrays, but never corrupt the text
            edit.problems.append("Two edits overlapped; nothing was changed.")
            return RemoveEdit(text, problems=edit.problems)
        out = out[:start] + rep + out[end:]
        last_start = start
    edit.text = out
    return edit


def _array_edits(code: Code, arr: int, idxs: list[int]) -> list[tuple[int, int, str]]:
    """Character edits dropping items `idxs` of the array opened at sig `arr`."""
    text = code.text
    items = code.items(arr)
    close = code.match[arr]
    if len(idxs) == len(items):
        # nothing left: `[]` (comments between the entries go with them)
        return [(code.tok(arr).end, code.tok(close).start, "")]
    runs: list[list[int]] = []
    for i in idxs:
        if runs and runs[-1][-1] == i - 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    out = []
    for run in runs:
        first, last = items[run[0]], items[run[-1]]
        start = code.tok(first[0]).start
        end = code.tok(last[1]).end
        if last[2] is not None:
            end = code.tok(last[2]).end
        line_start = text.rfind("\n", 0, start) + 1
        nl = text.find("\n", end)
        rest = text[end:nl if nl >= 0 else len(text)]
        rest_s = rest.strip()
        rest_ok = not rest_s or rest_s.startswith("//") or (rest_s.startswith("/*") and rest_s.endswith("*/")
                                                              and rest_s.count("*/") == 1)
        prefix = re.sub(r"/\*.*?\*/", "", text[line_start:start])     # a `/* note */` before the entry goes with it
        if not prefix.strip() and rest_ok and nl >= 0:
            out.append((line_start, nl + 1, ""))           # whole lines
            continue
        if last[2] is not None:                              # `a, X, b` -> `a, b`
            m = re.match(r"[ \t]*", text[end:])
            out.append((start, end + (m.end() if m else 0), ""))
        else:                                                # last item `a, X]` -> `a]`
            prev_comma = items[run[0] - 1][2]
            assert prev_comma is not None
            out.append((code.tok(prev_comma).start, code.tok(last[1]).end, ""))
    return out
