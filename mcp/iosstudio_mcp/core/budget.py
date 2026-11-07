"""Output budget: keeps every tool result small enough for an agent's context window.

The rules (docs/mcp.md, "Output size"):
- A result (text block + structuredContent together) is at most DEFAULT_BYTES (~3k tokens) unless the
  caller passes max_bytes (≤ MAX_BYTES). to_result() in core/registry.py enforces the ceiling for every
  tool by shrinking lists, never by erroring.
- Bulk lines (logs, crash text) are in structuredContent like every other field, and in the text block as
  plain lines after a compact JSON header of the other fields. A model lists them in its BULK class
  variable. Since they go out twice, a tool spends at most room(max_bytes), a bit under half, on them.
- Every cut says what was cut and how to get more (`note`), and large artifacts are written to a file
  (spill) whose path and resource link come back with a budgeted excerpt.

Helpers here are pure (no MCP calls), so future tools can reuse them:
    clip(s, n)                       "abc…[+N]"
    take(lines, max_lines, max_bytes, from_end)  -> Page
    paginate(items, cursor, limit, render, max_bytes) -> Page with an opaque next_cursor
    grep(lines, pattern, context, max_matches)
    fold_repeats(items, key)          consecutive duplicates -> (item, count)
    group_diags(diags, root)          build diagnostics grouped by severity + category + message
    spill(data, kind, name)           -> (path, size, ResourceLink)
    fit_image(src, max_dim, max_bytes)
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Sequence

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ResourceLink
from pydantic import BaseModel, PrivateAttr

DEFAULT_BYTES = 12_000      # default result size (~3k tokens)
MAX_BYTES = 48_000          # what max_bytes may raise it to
LINE_CUT = 300              # default characters per log line
IMG_MAX_DIM = 1568          # long edge Claude's vision downsamples to anyway
IMG_MAX_BYTES = 350_000     # inline image cap (the API limit is ~5 MB; this keeps tokens and latency sane)
LIST_CAP = 20               # account/signing lists, ActionResult data, dry-run `would`
MAX_PATTERN = 200           # grep pattern length
RESERVE = 1_000             # bytes kept for the other fields of a result (per copy)
ERROR_CUT = 300             # characters of error detail

CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ios-studio" / "mcp"


def nbytes(s: str) -> int:
    return len(s.encode("utf-8", "replace"))


def clip(s: Any, n: int = LINE_CUT) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + f"…[+{len(s) - n}]"


def budget(max_bytes: int | None) -> int:
    """The byte budget for one call, text block and structuredContent together: max_bytes clamped to
    [DEFAULT_BYTES/4, MAX_BYTES], default DEFAULT_BYTES."""
    if not max_bytes:
        return DEFAULT_BYTES
    return max(DEFAULT_BYTES // 4, min(int(max_bytes), MAX_BYTES))


def room(max_bytes: int | None, reserve: int = RESERVE) -> int:
    """Bytes a tool may spend on its bulk lines or list items. Everything is sent twice (text block and
    structuredContent, where JSON quoting adds a little), so that is a bit under half of the budget left
    after `reserve` bytes for the other fields in both copies."""
    return max(1_000, int((budget(max_bytes) - 2 * reserve) * 0.47))


def cap_list(items: Sequence, n: int = LIST_CAP) -> tuple[list, int]:
    """(first n items, how many were left out)."""
    items = list(items)
    return items[:n], max(0, len(items) - n)


def clip_data(v: Any, n: int = LIST_CAP, depth: int = 0) -> Any:
    """Bound free-form data (ActionResult.data): lists to n items plus "+N more", strings to ERROR_CUT chars."""
    if depth > 4:
        return clip(json.dumps(v, default=str), ERROR_CUT)
    if isinstance(v, dict):
        keys = list(v)
        out = {k: clip_data(v[k], n, depth + 1) for k in keys[:n * 2]}
        if len(keys) > n * 2:
            out["…"] = f"+{len(keys) - n * 2} more keys"
        return out
    if isinstance(v, (list, tuple)):
        out = [clip_data(x, n, depth + 1) for x in list(v)[:n]]
        if len(v) > n:
            out.append(f"+{len(v) - n} more")
        return out
    if isinstance(v, str):
        return clip(v, ERROR_CUT)
    return v


def clip_error(msg: str) -> str:
    """One line of message (≤ ERROR_CUT chars) plus the 'Next:' step, never a raw multi-line dump."""
    msg = str(msg or "").strip()
    head, sep, nxt = msg.partition(" Next: ")
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
    out = clip(" ".join((lines[0] if lines else "").split()), ERROR_CUT)
    if len(lines) > 1:
        out += " […]"
    if sep:
        out += " Next: " + clip(" ".join(nxt.split()), ERROR_CUT)
    return out


# ------------------------------------------------------------------ pages

@dataclass
class Page:
    items: list
    truncated: bool = False
    omitted: int = 0
    next_cursor: Any = None     # take(): index after the last item; paginate(): opaque string
    prev_cursor: Any = None     # take(): index of the first item (pass as `before`)
    start: int = 0              # index of the first item in the input
    total: int = 0


def take(lines: Sequence[str], max_lines: int, max_bytes: int, from_end: bool = False) -> Page:
    """Up to max_lines items from the start (or end) of `lines` whose joined size stays within max_bytes."""
    n = len(lines)
    picked: list[int] = []
    used = 0
    order = range(n - 1, -1, -1) if from_end else range(n)
    for i in order:
        if len(picked) >= max_lines:
            break
        b = nbytes(lines[i]) + 1
        if picked and used + b > max_bytes:
            break
        picked.append(i)
        used += b
    picked.sort()
    items = [lines[i] for i in picked]
    start = picked[0] if picked else (n if from_end else 0)
    end = picked[-1] + 1 if picked else start
    return Page(items=items, truncated=len(items) < n, omitted=n - len(items),
                next_cursor=end if end < n else None, prev_cursor=start if start > 0 else None,
                start=start, total=n)


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: Any) -> int:
    """Opaque cursor (or a plain offset) -> offset."""
    if cursor in (None, ""):
        return 0
    if isinstance(cursor, int) or (isinstance(cursor, str) and cursor.isdigit()):
        return max(0, int(cursor))
    try:
        raw = base64.urlsafe_b64decode(str(cursor) + "=" * (-len(str(cursor)) % 4)).decode()
        if raw.startswith("o:"):
            return max(0, int(raw[2:]))
    except (ValueError, UnicodeDecodeError):
        pass
    raise ToolError("Invalid cursor: pass next_cursor from the previous call unchanged, or omit it.")


def paginate(items: Sequence, cursor: Any, limit: int, render: Callable[[Any], str] = str,
             max_bytes: int = DEFAULT_BYTES) -> Page:
    """One page of `items` from `cursor`: at most `limit` items whose rendered size fits max_bytes."""
    off = decode_cursor(cursor)
    n = len(items)
    chosen, used = [], 0
    for x in items[off:off + limit]:
        b = nbytes(render(x)) + 1
        if chosen and used + b > max_bytes:
            break
        chosen.append(x)
        used += b
    end = off + len(chosen)
    return Page(items=chosen, truncated=end < n or off > 0, omitted=n - len(chosen),
                next_cursor=encode_cursor(end) if end < n else None,
                prev_cursor=encode_cursor(max(0, off - limit)) if off > 0 else None, start=off, total=n)


# ------------------------------------------------------------------ filtering and folding

def compile_pattern(pattern: str) -> re.Pattern:
    if len(pattern) > MAX_PATTERN:
        raise ToolError(f"grep pattern is {len(pattern)} characters; keep it under {MAX_PATTERN}.")
    try:
        return re.compile(pattern, re.I)
    except re.error as exc:
        raise ToolError(f"Invalid grep regex ({exc}). Use a plain word or escape special characters.")


def grep(lines: Sequence[str], pattern: str | re.Pattern, context: int = 0,
         max_matches: int = 50) -> tuple[list[tuple[int, bool]], int]:
    """([(index, is_match)] for the first max_matches matches plus `context` lines around each, total matches).
    Indexes are into `lines`, ascending and unique; a gap between them means skipped lines."""
    rx = pattern if isinstance(pattern, re.Pattern) else compile_pattern(pattern)
    hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
    total = len(hits)
    hits = hits[:max_matches]
    keep: dict[int, bool] = {}
    for i in hits:
        for j in range(max(0, i - context), min(len(lines), i + context + 1)):
            keep[j] = keep.get(j, False) or j == i
    return sorted(keep.items()), total


def fold_repeats(items: Iterable, key: Callable[[Any], Any] = lambda x: x) -> list[tuple[Any, int]]:
    """Consecutive items with the same key -> (first item, count)."""
    out: list[list] = []
    last = object()
    for it in items:
        k = key(it)
        if out and k == last:
            out[-1][1] += 1
        else:
            out.append([it, 1])
            last = k
    return [(a, b) for a, b in out]


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
_RANK = {"error": 0, "warning": 1, "note": 2, "remark": 3}


def rel_path(path: str, root: str | None) -> str:
    p = str(path or "")
    if root:
        r = str(root).rstrip("/") + "/"
        if p.startswith(r):
            return p[len(r):]
    home = str(Path.home())
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


DIAG_DETAILS = 8            # detail lines per diagnostic group (symbols of a linker block, …)
DIAG_DETAIL_CUT = 200


def diag_category(d: dict) -> str:
    """A diagnostic's category; builds from before categories existed only had compiler file:line ones."""
    return str(d.get("category") or ("compiler" if d.get("file") else "other"))


def group_diags(diags: Iterable[dict], root: str | None = None, include_notes: bool = False,
                max_locations: int = 3, message_cut: int = LINE_CUT) -> list[dict]:
    """Build diagnostics -> groups keyed by severity, category and normalised message (quoted names masked),
    errors first: {severity, category, message, count, locations: ['Sources/App/File.swift:12:5', … first 3],
    more_locations, hint?, details?}. Location-less diagnostics (linker, package, signing, …) have no locations."""
    groups: dict[tuple, dict] = {}
    for d in diags:
        sev = str(d.get("severity") or "error")
        if sev in ("note", "remark") and not include_notes:
            continue
        cat = diag_category(d)
        msg = " ".join(str(d.get("message") or "").split())
        key = (sev, cat, _QUOTED.sub("'…'", msg))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"severity": sev, "category": cat, "message": msg, "count": 0, "locations": [],
                               "variants": set(), "located": 0}
            if d.get("hint"):
                g["hint"] = clip(" ".join(str(d["hint"]).split()), ERROR_CUT)
            if isinstance(d.get("details"), list) and d["details"]:
                g["details"] = [clip(str(x), DIAG_DETAIL_CUT) for x in d["details"][:DIAG_DETAILS]]
        g["count"] += 1
        g["variants"].add(msg)
        if not d.get("file"):
            continue
        g["located"] += 1
        loc = rel_path(str(d.get("file")), root)
        if d.get("line"):
            loc += f":{d.get('line')}" + (f":{d.get('col')}" if d.get("col") else "")
        if loc not in g["locations"] and len(g["locations"]) < max_locations:
            g["locations"].append(loc)
    out = []
    for g in sorted(groups.values(), key=lambda g: _RANK.get(g["severity"], 4)):
        extra = len(g.pop("variants")) - 1
        g["message"] = clip(g["message"], message_cut) + (f" (and {extra} similar)" if extra > 0 else "")
        g["more_locations"] = max(0, g.pop("located") - len(g["locations"]))
        out.append(g)
    return out


# ------------------------------------------------------------------ files

def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")[:120] or "output"


def spill(data: str | bytes, kind: str, name: str, mime_type: str = "text/plain",
          keep: int = 30) -> tuple[str, int, ResourceLink]:
    """Write a large artifact to ~/.cache/ios-studio/mcp/<kind>/<name> (keeping the newest `keep` files of that
    kind) and return (path, size in bytes, ResourceLink to iosstudio://cache/<kind>/<name>)."""
    kind, name = _safe_name(kind), _safe_name(name)
    d = CACHE / kind
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
    p.write_bytes(raw)
    try:
        old = sorted((f for f in d.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in old[keep:]:
            f.unlink(missing_ok=True)
    except OSError:
        pass
    link = ResourceLink(type="resource_link", uri=f"iosstudio://cache/{kind}/{name}", name=name,
                        mime_type=mime_type, size=len(raw),
                        description=f"Full output ({len(raw)} bytes); also on disk at {rel_path(str(p), None)}")
    return str(p), len(raw), link


def cache_file(kind: str, name: str) -> Path | None:
    p = CACHE / _safe_name(kind) / _safe_name(name)
    return p if p.is_file() else None


# ------------------------------------------------------------------ images

@dataclass
class FittedImage:
    data: bytes
    format: str          # jpeg | png
    width: int
    height: int
    original_width: int
    original_height: int
    quality: int | None = None
    fits: bool = True    # within max_bytes
    notes: list[str] = field(default_factory=list)


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    return 0, 0


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return 0, 0


def image_size(data: bytes) -> tuple[int, int]:
    return png_size(data) if data[:4] == b"\x89PNG" else _jpeg_size(data) if data[:2] == b"\xff\xd8" else (0, 0)


def fit_image(src: Path, max_dim: int = IMG_MAX_DIM, max_bytes: int = IMG_MAX_BYTES,
              fmt: str = "jpeg", qualities: Sequence[int] = (80, 65, 50)) -> FittedImage:
    """Downscale `src` so the long edge is ≤ max_dim and encode it under max_bytes: JPEG at the given
    qualities in turn; PNG only if it fits, else JPEG. Pillow, else ImageMagick, else the file as-is."""
    max_dim = min(max_dim, IMG_MAX_DIM)
    try:
        from PIL import Image as PILImage
    except ImportError:
        PILImage = None
    if PILImage is not None:
        with PILImage.open(src) as im0:
            ow, oh = im0.size
            im = im0.copy()
        if max(ow, oh) > max_dim:
            s = max_dim / max(ow, oh)
            im = im.resize((max(1, round(ow * s)), max(1, round(oh * s))), PILImage.LANCZOS)
        notes = []
        if fmt == "png":
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            if buf.tell() <= max_bytes:
                return FittedImage(buf.getvalue(), "png", im.size[0], im.size[1], ow, oh)
            notes.append(f"PNG was {buf.tell() // 1000} KB, over the {max_bytes // 1000} KB cap; sent JPEG instead.")
        rgb = im.convert("RGB")
        data, q = b"", None
        for q in qualities:
            buf = io.BytesIO()
            rgb.save(buf, "JPEG", quality=q, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                break
        return FittedImage(data, "jpeg", rgb.size[0], rgb.size[1], ow, oh, q, len(data) <= max_bytes, notes)
    raw = src.read_bytes()
    ow, oh = image_size(raw)
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        data, q = b"", None
        for q in qualities:
            data = subprocess.run([magick, str(src), "-resize", f"{max_dim}x{max_dim}>", "-quality", str(q),
                                   "jpeg:-"], capture_output=True, timeout=30, check=True).stdout
            if len(data) <= max_bytes:
                break
        w, h = image_size(data)
        return FittedImage(data, "jpeg", w, h, ow or w, oh or h, q, len(data) <= max_bytes)
    fmt0 = "png" if raw[:4] == b"\x89PNG" else "jpeg"
    ok = len(raw) <= max_bytes and max(ow, oh) <= max_dim
    return FittedImage(raw, fmt0, ow, oh, ow, oh, None, ok,
                       [] if ok else ["Neither Pillow nor ImageMagick is available to shrink the image."])


# ------------------------------------------------------------------ models

class Budgeted(BaseModel):
    """Mixin for results that can be cut. BULK names the fields (log lines, crash text) that the text block
    shows as plain lines after the JSON header; structuredContent carries them as ordinary fields."""
    BULK: ClassVar[tuple[str, ...]] = ()
    model_config = {"extra": "ignore"}

    truncated: bool = False
    omitted: int = 0
    note: str | None = None
    next_cursor: str | None = None
    full_output_path: str | None = None

    _budget: int = PrivateAttr(default=DEFAULT_BYTES)
    _links: list = PrivateAttr(default_factory=list)

    def with_budget(self, max_bytes: int | None, links: list | None = None):
        self._budget = budget(max_bytes)
        if links:
            self._links.extend(links)
        return self


def dump(model: BaseModel) -> dict:
    """model_dump(json) without None values, and without empty lists/dicts or False/0 in fields that have a
    default (so the result still validates against the schema)."""
    out: dict = {}
    fields = type(model).model_fields
    for name, f in fields.items():
        v = getattr(model, name)
        if v is None:
            continue
        optional = not f.is_required()
        if optional and (v == [] or v == {}):
            continue
        if optional and name in ("truncated", "omitted") and not v:
            continue
        out[name] = _dump_value(v)
    return out


def _dump_value(v: Any) -> Any:
    if isinstance(v, BaseModel):
        return dump(v)
    if isinstance(v, (list, tuple)):
        return [_dump_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _dump_value(x) for k, x in v.items()}
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return json.loads(json.dumps(v, default=str))


def _json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def render_text(data: dict, bulk_fields: Sequence[str] = ()) -> str:
    """The text block: compact JSON of everything except the bulk fields, then the bulk as plain lines."""
    head = {k: v for k, v in data.items() if k not in bulk_fields}
    lines: list[str] = []
    for f in bulk_fields:
        lines += bulk_text(data.get(f))
    return _json(head) if not lines else _json(head) + "\n" + "\n".join(lines)


def result_size(data: dict, bulk_fields: Sequence[str] = ()) -> int:
    """What a result costs on the wire: the text block plus structuredContent, which both carry the bulk."""
    return nbytes(render_text(data, bulk_fields)) + nbytes(_json(data))


WALK_DEPTH = 8              # how deep enforce() looks for lists and strings to cut
WALK_NODES = 5_000          # and how many values it visits per pass
STR_FLOOR = 200             # strings this short are never clipped (longer ones are halved)
# Keys whose string values must come back unchanged (cursors, ids, paths a later call passes back).
_KEEP_KEY = re.compile(r"(cursor|^ids?$|_id$|path$|^uri$|^url$|^note$|^file$|_file$)", re.I)


def _walk(data: dict, skip: Sequence[str]):
    """(container, key, value, protected) for every value under `data`, breadth first, up to WALK_DEPTH levels
    and WALK_NODES values. Top-level keys in `skip` (the bulk fields) are left out."""
    queue: list[tuple[Any, Any, Any, int, bool]] = [(data, k, v, 0, bool(_KEEP_KEY.search(str(k))))
                                                    for k, v in data.items() if k not in skip]
    seen = 0
    while queue and seen < WALK_NODES:
        nxt: list = []
        for cont, key, val, depth, prot in queue:
            seen += 1
            if seen > WALK_NODES:
                break
            yield cont, key, val, prot
            if depth >= WALK_DEPTH:
                continue
            if isinstance(val, dict):
                nxt += [(val, k, v, depth + 1, bool(_KEEP_KEY.search(str(k)))) for k, v in val.items()]
            elif isinstance(val, list):
                nxt += [(val, i, v, depth + 1, prot) for i, v in enumerate(val)]
        queue = nxt


def _largest_list(data: dict, skip: Sequence[str] = ()) -> tuple[Any, Any, int] | None:
    """(container, key, size) of the biggest list with more than one item anywhere in `data` (bounded
    walk), not under a top-level key in `skip`."""
    best = None
    for cont, key, val, _ in _walk(data, skip):
        if isinstance(val, list) and len(val) > 1:
            size = len(json.dumps(val, separators=(",", ":"), default=str))
            if best is None or size > best[2]:
                best = (cont, key, size)
    return best


def _longest_string(data: dict, skip: Sequence[str] = ()) -> tuple[Any, Any, int] | None:
    """(container, key, length) of the longest clippable string anywhere in `data` (bounded walk)."""
    best = None
    for cont, key, val, prot in _walk(data, skip):
        if isinstance(val, str) and not prot and len(val) > STR_FLOOR and (best is None or len(val) > best[2]):
            best = (cont, key, len(val))
    return best


def enforce(data: dict, bulk_fields: Sequence[str], limit: int) -> tuple[dict, str]:
    """Shrink `data` until text block + structuredContent together fit `limit` bytes: first the bulk fields
    (lines kept from the start, with a marker line), then the biggest lists (halved), then the longest
    strings (halved), at any depth. Cursors, ids, paths and the note are never cut. Marks the result
    truncated with a note on what was cut. Returns (structuredContent, text)."""
    if result_size(data, bulk_fields) <= limit:
        return data, render_text(data, bulk_fields)
    cuts: list[str] = []
    for f in bulk_fields:
        v = data.get(f)
        if not v:
            continue
        lines = bulk_text(v)
        others = result_size({k: x for k, x in data.items() if k != f}, bulk_fields)
        page = take(lines, len(lines), max(500, int((limit - others - 400) * 0.47)), from_end=False)
        if page.omitted:
            cuts.append(f"{page.omitted} of {len(lines)} lines of {f}")
            kept = page.items + [f"…[{page.omitted} more lines cut to fit the output budget]"]
            data[f] = "\n".join(kept) if isinstance(v, str) else kept
    for _ in range(80):
        if result_size(data, bulk_fields) <= limit:
            break
        hit = _largest_list(data, bulk_fields)
        lng = _longest_string(data, bulk_fields)
        # halve a list unless one string alone is bigger than the biggest list
        if hit is not None and (lng is None or hit[2] >= lng[2]):
            cont, key, _ = hit
            n = len(cont[key])
            keep = max(1, n // 2)
            cont[key] = cont[key][:keep]
            cuts.append(f"{key} {n}→{keep}" if isinstance(key, str) else f"a nested list {n}→{keep}")
            continue
        if lng is None:
            break
        cont, key, n = lng
        cont[key] = clip(cont[key], n // 2)
        cuts.append(f"field {key}" if isinstance(key, str) else "a long string in a list")
    if cuts:
        data["truncated"] = True
        why = "Cut to fit the output budget (" + ", ".join(dict.fromkeys(cuts)) + "); narrow the request " \
              "(filters, smaller limit) or pass max_bytes (≤48000) where the tool has it."
        data["note"] = (str(data.get("note")) + " " if data.get("note") else "") + why
    return data, render_text(data, bulk_fields)


def bulk_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return value.splitlines() or [value]
    return [str(x) for x in value]


def ceiling(model: BaseModel) -> int:
    return getattr(model, "_budget", None) or DEFAULT_BYTES


def links(model: BaseModel) -> list:
    return list(getattr(model, "_links", None) or [])

