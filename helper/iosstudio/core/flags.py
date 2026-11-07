"""Command-line flags of the helper (stdlib only)."""
from __future__ import annotations

from typing import Any, Iterable


def parse_flags(args: Iterable[str]) -> dict[str, Any]:
    """--key value / --flag  ->  {"key": "value", "flag": True}; positional -> "_": [...]"""
    out: dict[str, Any] = {"_": []}
    args = list(args)
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                out[key] = args[i + 1]
                i += 2
                continue
            out[key] = True
        else:
            out["_"].append(a)
        i += 1
    return out
