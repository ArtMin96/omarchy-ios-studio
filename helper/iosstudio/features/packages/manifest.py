"""Package.swift (as `dump-package` data) and Package.resolved, read as data (stdlib only).

    parse_resolved(text)              Package.resolved v1, v2 or v3 -> {identity: {identity, url, version?, branch?,
                                      revision?, kind}}
    requirement(dep)                  one `dump-package` dependency's requirement -> {rule, version?, lower?, upper?,
                                      branch?, revision?, text}
    declared(dump) / target_deps(dump)  the declared packages / each target's dependencies
    identity_of(url_or_path)          SwiftPM's package identity (last path component, no .git, lowercased)
    resolved_changes(before, after)   [{identity, from, to}] between two parse_resolved() results
    drop_pins(text, identities)       Package.resolved without some pins

Editing Package.swift as text is manifest_edit.py.
"""
from __future__ import annotations

import json
import re
from typing import Any

# ================================================================ identities and requirements

SEMVER = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")


def identity_of(location: str) -> str:
    """SwiftPM's identity of a URL or path: its last path component without `.git`, lowercased."""
    s = str(location or "").strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    s = re.split(r"[/:]", s)[-1] if s else s
    return s.lower()


def normalize_location(loc: str) -> str:
    """A URL or path for comparing: no scheme, user, `.git` or trailing slash; `:` as `/`; lowercased."""
    s = str(loc).strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    s = re.sub(r"^(https?|ssh|git)://", "", s)
    s = re.sub(r"^[^@/]+@", "", s)
    return s.replace(":", "/").lower()


def version_tuple(v: str | None) -> tuple[int, int, int] | None:
    m = SEMVER.match(str(v or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def requirement_text(req: dict) -> str:
    rule = req.get("rule")
    if rule == "from":
        return f"from {req.get('version')}"
    if rule == "upToNextMinor":
        return f"up to next minor from {req.get('version')}"
    if rule == "exact":
        return f"exactly {req.get('version')}"
    if rule == "range":
        return f"{req.get('lower')} ..< {req.get('upper')}"
    if rule == "branch":
        return f"branch {req.get('branch')}"
    if rule == "revision":
        return f"revision {str(req.get('revision') or '')[:7]}"
    if rule == "local":
        return "local"
    return "unknown"


def _first(v: Any) -> Any:
    return v[0] if isinstance(v, list) and v else v


def requirement(raw: Any, local: bool = False) -> dict:
    """`dump-package`'s requirement object -> {rule, version?, lower?, upper?, branch?, revision?, text}."""
    if local:
        out: dict[str, Any] = {"rule": "local"}
    elif not isinstance(raw, dict) or not raw:
        out = {"rule": "unknown"}
    elif "range" in raw:
        r = _first(raw["range"]) or {}
        lo, up = str(r.get("lowerBound") or ""), str(r.get("upperBound") or "")
        lt, ut = version_tuple(lo), version_tuple(up)
        if lt and ut and ut == (lt[0] + 1, 0, 0):
            out = {"rule": "from", "version": lo, "lower": lo, "upper": up}
        elif lt and ut and ut == (lt[0], lt[1] + 1, 0):
            out = {"rule": "upToNextMinor", "version": lo, "lower": lo, "upper": up}
        else:
            out = {"rule": "range", "lower": lo, "upper": up}
    elif "exact" in raw:
        out = {"rule": "exact", "version": str(_first(raw["exact"]))}
    elif "branch" in raw:
        out = {"rule": "branch", "branch": str(_first(raw["branch"]))}
    elif "revision" in raw:
        out = {"rule": "revision", "revision": str(_first(raw["revision"]))}
    else:
        out = {"rule": "unknown"}
    out["text"] = requirement_text(out)
    return out


def declared(dump: dict) -> list[dict]:
    """`dump-package` dependencies -> [{identity, kind, url|path, requirement, name?}] in manifest order."""
    out = []
    for dep in dump.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        for kind, items in dep.items():
            item = _first(items) or {}
            if not isinstance(item, dict):
                continue
            ident = str(item.get("identity") or "")
            name = item.get("nameForTargetDependencyResolutionOnly")
            if kind == "fileSystem":
                path = str(item.get("path") or "")
                out.append({"identity": ident or identity_of(path), "kind": "local", "path": path, "url": None,
                            "requirement": requirement(None, local=True), "name": name})
            elif kind == "sourceControl":
                loc = item.get("location") or {}
                url = _first(loc.get("remote")) if isinstance(loc, dict) and "remote" in loc else None
                if isinstance(url, dict):
                    url = url.get("urlString")
                path = _first(loc.get("local")) if isinstance(loc, dict) and "local" in loc else None
                out.append({"identity": ident or identity_of(url or path or ""), "kind": "remote",
                            "url": str(url or path or ""), "path": None,
                            "requirement": requirement(item.get("requirement")), "name": name})
            elif kind == "registry":
                out.append({"identity": ident, "kind": "registry", "url": ident, "path": None,
                            "requirement": requirement(item.get("requirement")), "name": name})
    return out


def target_deps(dump: dict) -> list[dict]:
    """[{target, type, deps: [{kind: product|byName|target, name, package?}]}] from `dump-package`."""
    out = []
    for t in dump.get("targets") or []:
        if not isinstance(t, dict):
            continue
        deps = []
        for d in t.get("dependencies") or []:
            if not isinstance(d, dict):
                continue
            for kind, v in d.items():
                v = v if isinstance(v, list) else [v]
                if kind == "product":
                    deps.append({"kind": "product", "name": str(v[0]), "package": v[1] if len(v) > 1 else None})
                elif kind in ("byName", "target"):
                    deps.append({"kind": kind, "name": str(v[0])})
        out.append({"target": str(t.get("name")), "type": str(t.get("type") or "regular"), "deps": deps})
    return out


# ================================================================ Package.resolved

def parse_resolved(text: str) -> dict[str, dict]:
    """Package.resolved (format 1, 2 or 3) -> {identity: {identity, url, kind, version?, branch?, revision?}}.
    Raises ValueError for text that isn't a Package.resolved."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Package.resolved isn't valid JSON ({exc.msg}, line {exc.lineno}).") from None
    if not isinstance(data, dict):
        raise ValueError("Package.resolved isn't a JSON object.")
    version = data.get("version")
    if version == 1:
        pins = (data.get("object") or {}).get("pins") or []
    elif version in (2, 3) or "pins" in data:
        pins = data.get("pins") or []
    else:
        raise ValueError(f"Unknown Package.resolved format (version {version!r}).")
    out: dict[str, dict] = {}
    for pin in pins:
        if not isinstance(pin, dict):
            continue
        if version == 1:
            url = str(pin.get("repositoryURL") or "")
            ident = identity_of(url) or str(pin.get("package") or "").lower()
            kind = "remoteSourceControl"
        else:
            url = str(pin.get("location") or "")
            ident = str(pin.get("identity") or identity_of(url))
            kind = str(pin.get("kind") or "remoteSourceControl")
        st = pin.get("state") or {}
        entry: dict[str, Any] = {"identity": ident, "url": url, "kind": kind}
        for k in ("version", "branch", "revision"):
            if st.get(k):
                entry[k] = str(st[k])
        out[ident] = entry
    return out


def pin_label(pin: dict | None) -> str | None:
    """1.2.3 / main@abc1234 / abc1234."""
    if not pin:
        return None
    rev = str(pin.get("revision") or "")[:7]
    if pin.get("version"):
        return str(pin["version"])
    if pin.get("branch"):
        return f"{pin['branch']}@{rev}" if rev else str(pin["branch"])
    return rev or None


def resolved_changes(before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
    """Pins that changed between two parse_resolved() results: [{identity, from, to}] (from/to null when a pin
    was added or removed), sorted by identity."""
    out = []
    for ident in sorted(set(before) | set(after)):
        a, b = before.get(ident), after.get(ident)
        if a and b and a.get("version") == b.get("version") and a.get("branch") == b.get("branch") \
                and a.get("revision") == b.get("revision"):
            continue
        out.append({"identity": ident, "from": pin_label(a), "to": pin_label(b)})
    return out


def drop_pins(text: str, identities: set[str]) -> str | None:
    """Package.resolved text without the given pins, in SwiftPM's own layout (sorted keys, `"key" : value`).
    None when no pin is left (SwiftPM deletes the file then)."""
    data = json.loads(text)
    lower = {i.lower() for i in identities}
    if data.get("version") == 1:
        obj = data.get("object") or {}
        obj["pins"] = [p for p in obj.get("pins") or [] if identity_of(p.get("repositoryURL") or "") not in lower]
        left = obj["pins"]
    else:
        data["pins"] = [p for p in data.get("pins") or [] if str(p.get("identity") or "").lower() not in lower]
        left = data["pins"]
    if not left:
        return None
    return json.dumps(data, indent=2, sort_keys=True, separators=(",", " : "), ensure_ascii=False) + "\n"
