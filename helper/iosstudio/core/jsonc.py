"""JSON with comments (stdlib only), span-preserving.

Parses JSON plus // and /* */ comments, trailing commas and a BOM (VS Code, Zed, opencode and Gemini accept
those). Every node records where it sits in the text (and every object member where its key starts and the
comma after it), so callers can edit one member's text and leave the rest of the file (order, indentation,
comments) byte for byte.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MAX_DEPTH = 64


class JsoncError(ValueError):
    pass


@dataclass
class Node:
    kind: str                  # obj | arr | str | lit
    start: int
    end: int = 0               # one past the closing brace / bracket / value
    value: Any = None
    members: list[Member] = field(default_factory=list)   # obj
    items: list[Node] = field(default_factory=list)       # arr
    comma: int = -1            # offset of the comma after this node in its container, -1 if none


@dataclass
class Member:
    key: str
    kstart: int                # offset of the key's opening quote
    node: Node

    @property
    def comma(self) -> int:
        return self.node.comma


def line_of(text: str, i: int) -> int:
    return text.count("\n", 0, max(0, i)) + 1


def skip(text: str, i: int) -> int:
    """The offset of the first character at or after `i` that isn't whitespace, a BOM or a comment."""
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n﻿":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise JsoncError(f"a /* comment */ that never ends (line {line_of(text, i)})")
            i = j + 2
        else:
            break
    return i


class _Parser:
    def __init__(self, text: str):
        self.t, self.i = text, 0

    def err(self, what: str) -> JsoncError:
        return JsoncError(f"{what} (line {line_of(self.t, self.i)})")

    def value(self, depth: int = 0) -> Node:
        if depth > MAX_DEPTH:
            raise self.err("nested too deeply")
        self.i = skip(self.t, self.i)
        if self.i >= len(self.t):
            raise self.err("ends too early")
        c = self.t[self.i]
        if c == "{":
            return self.obj(depth)
        if c == "[":
            return self.arr(depth)
        if c == '"':
            return self.string()
        return self.literal()

    def string(self) -> Node:
        node = Node("str", self.i)
        try:
            node.value, node.end = json.JSONDecoder().raw_decode(self.t, self.i)
        except json.JSONDecodeError as exc:
            raise self.err(f"bad string ({exc.msg})") from None
        self.i = node.end
        return node

    def literal(self) -> Node:
        node = Node("lit", self.i)
        j = self.i
        while j < len(self.t) and self.t[j] not in " \t\r\n,]}/":
            j += 1
        raw = self.t[self.i:j]
        try:
            node.value = json.loads(raw)
        except json.JSONDecodeError:
            raise self.err(f"unexpected '{raw[:20]}'") from None
        if isinstance(node.value, (dict, list, str)):
            raise self.err(f"unexpected '{raw[:20]}'")
        node.end = self.i = j
        return node

    def _after(self, child: Node, close: str) -> bool:
        """Past a child: True at the container's end (consumed), False after a comma."""
        self.i = skip(self.t, self.i)
        if self.i < len(self.t) and self.t[self.i] == ",":
            child.comma = self.i
            self.i += 1
            return False
        if self.i < len(self.t) and self.t[self.i] == close:
            return False
        raise self.err(f"expected ',' or '{close}'")

    def obj(self, depth: int) -> Node:
        node = Node("obj", self.i, value={})
        self.i += 1
        while True:
            self.i = skip(self.t, self.i)
            if self.i < len(self.t) and self.t[self.i] == "}":
                self.i += 1
                node.end = self.i
                return node
            if self.i >= len(self.t) or self.t[self.i] != '"':
                raise self.err('expected a "key"')
            kstart = self.i
            key = self.string().value
            self.i = skip(self.t, self.i)
            if self.i >= len(self.t) or self.t[self.i] != ":":
                raise self.err("expected ':'")
            self.i += 1
            m = Member(key, kstart, self.value(depth + 1))
            node.members.append(m)
            node.value[key] = m.node.value
            self._after(m.node, "}")

    def arr(self, depth: int) -> Node:
        node = Node("arr", self.i, value=[])
        self.i += 1
        while True:
            self.i = skip(self.t, self.i)
            if self.i < len(self.t) and self.t[self.i] == "]":
                self.i += 1
                node.end = self.i
                return node
            child = self.value(depth + 1)
            node.items.append(child)
            node.value.append(child.value)
            self._after(child, "]")


def parse(text: str) -> Node:
    """The document's root node. Raises JsoncError (with a line number) when it isn't valid JSONC."""
    p = _Parser(text)
    root = p.value()
    p.i = skip(text, p.i)
    if p.i != len(text):
        raise p.err("extra text after the end")
    return root


def load(text: str) -> Any:
    """The document's value; an empty (or blank) document is {}."""
    return parse(text).value if text.strip() else {}
