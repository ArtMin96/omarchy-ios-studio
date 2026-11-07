"""Screenshot baselines and visual diffs: `iosstudio-helper screenshots <action>` (stdlib only; Pillow when present).

Save a screenshot of a screen as its baseline, then compare later screenshots against it: the result is the share
of pixels that changed, a verdict against a threshold, the changed regions as boxes and a diff image (the current
screenshot dimmed, changed pixels bright pink, regions outlined in yellow). The status bar (clock, battery, signal)
is masked so it never counts, and a small per-channel tolerance keeps compression noise out.

Storage (per project; the key is the folder name plus a hash of its path, so two checkouts never mix):
  DATA_DIR/screenshots/<key>/baselines/<name>.png + <name>.json     kept until deleted
  DATA_DIR/screenshots/<key>/compare/<stamp>-<name>/{current,diff}.png   newest 30 runs / 200 MB kept
  DATA_DIR/screenshots/<key>/matrix/<set>/<config>/<name>.png + manifest.json   newest 10 sets / 500 MB kept
A failed compare leaves nothing behind; `projects remove` deletes the project's folder here (forget_project).
In demo mode (IOSSTUDIO_DEMO) the same layout lives in the demo dir, seeded with made-up baselines.

Images: Pillow when it can be imported. The helper runs on the system python, which usually has no Pillow, so
`compare` (and `save` of a non-PNG file) re-runs itself once with the daemon's venv python (subproc.pymd3_python())
when that has it. Otherwise a pure-Python PNG codec does the work (8-bit, non-interlaced; gray, RGB, palette, RGBA):
correct, but a full-size iPhone screenshot takes several seconds that way. IOSSTUDIO_SCREENSHOTS_PURE=1 forces it.

Actions (JSON on stdout, one object; see docs/features/screenshot-compare.md):
  list    --project P
  save    --project P --name N --shot PNG [--device D] [--bundle B] [--preset NAME]
  compare --project P --name N --shot PNG [--threshold 0.1] [--tolerance 24] [--mask-top auto|PX] [--mask JSON]
  delete  --project P --name N [--confirm]                                  dry run without --confirm
  matrix-plan --project P --configs JSON --names JSON [--bundle B]          used by ios_screenshot_matrix
  matrix-save --project P --set ID --config KEY --name SLUG (--shot PNG | --error MSG)
  matrix-finish --project P --set ID --state done|cancelled|failed [--error MSG] [--restored TEXT]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, fileio, output, subproc
from iosstudio.core.output import HelperError

THRESHOLD = 0.1          # percent of compared pixels that may change before the verdict is "changed"
TOLERANCE = 24           # a pixel counts as changed when a channel differs by more than this (0-255)
CELL_PT = 8              # region grid cell, in points
MIN_CELL_PIXELS = 2      # a lone cell with fewer changed pixels is noise, not a region
MAX_REGIONS = 50
KEEP_COMPARES = 30       # compare runs (current + diff image) kept per project ...
COMPARES_MAX_BYTES = 200 << 20   # ... and at most this much of them
KEEP_SETS = 10           # matrix sets kept per project ...
SETS_MAX_BYTES = 500 << 20       # ... and at most this much of them
MAX_NAME = 60
MAX_CONFIGS = 12
MAX_NAMES = 12

PINK = (255, 46, 160)    # changed pixels in the diff image
YELLOW = (255, 214, 10)  # region outlines
DIM = bytes(min(255, int(22 + v * 0.36)) for v in range(256))   # what the unchanged rest of the screen becomes


# ================================================================ images

class Img:
    """An 8-bit RGB image: width, height and w*h*3 bytes, row by row."""
    __slots__ = ("w", "h", "rgb")

    def __init__(self, w: int, h: int, rgb: bytes | bytearray):
        self.w, self.h, self.rgb = w, h, rgb


def _pil():
    """PIL.Image, or None (not installed, or IOSSTUDIO_SCREENSHOTS_PURE=1)."""
    if os.environ.get("IOSSTUDIO_SCREENSHOTS_PURE"):
        return None
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def backend() -> str:
    return "pillow" if _pil() else "python"


def _bad_image(path: Path, why: str) -> HelperError:
    return HelperError("bad-image", f"{path.name} can't be read as a screenshot: {why}.")


def png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24 and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) without decoding: the PNG header, else Pillow."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return None
    size = png_size(head)
    if size:
        return size
    Image = _pil()
    if Image:
        try:
            with Image.open(path) as im:
                return im.size
        except Exception:  # noqa: BLE001
            return None
    return None


def _png_decode(data: bytes, path: Path) -> Img:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise _bad_image(path, "it isn't a PNG (Pillow is needed for other formats)")
    pos, ihdr, plte, idat = 8, None, b"", []
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"PLTE":
            plte = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if ihdr is None:
        raise _bad_image(path, "the PNG has no header")
    w, h, depth, ctype, _comp, _filt, interlace = ihdr
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if depth != 8 or channels is None or interlace:
        raise _bad_image(path, "only 8-bit, non-interlaced PNGs can be read without Pillow")
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        raise _bad_image(path, "the PNG data is damaged") from None
    bpp, stride = channels, w * channels
    if len(raw) < h * (stride + 1):
        raise _bad_image(path, "the PNG data is cut short")
    out = bytearray(h * stride)
    lo, hi = int.from_bytes(b"\x7f" * stride, "big"), int.from_bytes(b"\x80" * stride, "big")
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ft = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:      # bytewise add without carries between bytes (SWAR)
            a, b = int.from_bytes(line, "big"), int.from_bytes(prev, "big")
            line = bytearray((((a & lo) + (b & lo)) ^ ((a ^ b) & hi)).to_bytes(stride, "big"))
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif ft != 0:
            raise _bad_image(path, f"unknown PNG row filter {ft}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    if ctype == 2:
        return Img(w, h, bytes(out))
    rgb = bytearray(w * h * 3)
    if ctype == 6:
        for c in range(3):
            rgb[c::3] = out[c::4]
    elif ctype == 0:
        for c in range(3):
            rgb[c::3] = out
    elif ctype == 4:
        for c in range(3):
            rgb[c::3] = out[0::2]
    else:   # palette
        pal = plte + bytes(768 - len(plte)) if len(plte) < 768 else plte
        for c in range(3):
            rgb[c::3] = bytes(out).translate(bytes(pal[i * 3 + c] for i in range(256)))
    return Img(w, h, bytes(rgb))


def load(path: Path) -> Img:
    """Any screenshot file -> Img (RGB; alpha is dropped)."""
    Image = _pil()
    if Image:
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                return Img(im.size[0], im.size[1], im.tobytes())
        except OSError as exc:
            raise _bad_image(path, str(exc) or "unknown format") from None
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HelperError("no-shot", f"Can't read {path}: {exc.strerror or exc}.") from None
    return _png_decode(data, path)


def _png_encode(img: Img) -> bytes:
    stride = img.w * 3
    raw = b"".join(b"\x00" + img.rgb[y * stride:(y + 1) * stride] for y in range(img.h))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", img.w, img.h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def save_png(path: Path, img: Img) -> None:
    Image = _pil()
    with fileio.replacing(path) as tmp:
        if Image:
            Image.frombytes("RGB", (img.w, img.h), bytes(img.rgb)).save(tmp, "PNG", compress_level=6)
        else:
            tmp.write_bytes(_png_encode(img))


def _copy_png(src: Path, dst: Path) -> tuple[int, int]:
    """Keep a copy of a screenshot as PNG (copied as-is when it already is one). Returns its size."""
    try:
        with open(src, "rb") as fh:
            head = fh.read(32)
    except OSError as exc:
        raise HelperError("no-shot", f"Can't read the screenshot {src}: {exc.strerror or exc}.") from None
    dst.parent.mkdir(parents=True, exist_ok=True)
    size = png_size(head)
    if size:
        with fileio.replacing(dst) as tmp:
            shutil.copyfile(src, tmp)
        return size
    img = load(src)          # JPEG and friends: needs Pillow
    save_png(dst, img)
    return img.w, img.h


# ================================================================ comparing

def screen_scale(w: int, h: int) -> int:
    """Points -> pixels factor of an iPhone screenshot, from its short side (1206 -> 3, 750 -> 2, 390 -> 1)."""
    short = min(w, h)
    return 3 if short >= 1000 else 2 if short >= 600 else 1


def status_bar_px(w: int, h: int) -> int:
    """Height of the status bar band to ignore. Portrait only (iPhones hide it in landscape): 54 pt on Face ID
    phones (notch or Dynamic Island, taller than 2:1), 20 pt on Home-button ones, times the screen scale."""
    if w > h:
        return 0
    pt = 54 if h / max(1, w) > 2.0 else 20
    return min(h, pt * screen_scale(w, h))


def parse_masks(raw: Any) -> list[tuple[int, int, int, int]]:
    """[{x, y, w, h}, ...] or [[x, y, w, h], ...] (pixels; also a JSON string) -> rectangles."""
    if raw in (None, "", True):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise HelperError("usage", "--mask must be JSON: [{\"x\": 0, \"y\": 0, \"w\": 100, \"h\": 40}].") from None
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for r in raw if isinstance(raw, list) else []:
        try:
            x, y, w, h = (r.get(k) for k in ("x", "y", "w", "h")) if isinstance(r, dict) else r
            x, y, w, h = int(x), int(y), int(w), int(h)
        except (TypeError, ValueError):
            raise HelperError("usage", "Each mask needs x, y, w and h in pixels.") from None
        if w > 0 and h > 0:
            out.append((x, y, w, h))
    return out


def _mask_top(value: Any, w: int, h: int) -> int:
    if value in (None, "", "auto", True):
        return status_bar_px(w, h)
    try:
        return max(0, min(h, int(float(value))))
    except (TypeError, ValueError):
        raise HelperError("usage", "--mask-top must be 'auto' or a number of pixels.") from None


def _ignored(w: int, h: int, top: int, masks: list) -> list[tuple[int, int, int, int]]:
    """Ignored rectangles as (x0, y0, x1, y1), clipped to the image."""
    rects = [(0, 0, w, top)] if top else []
    for x, y, rw, rh in masks:
        x0, y0, x1, y1 = max(0, x), max(0, y), min(w, x + rw), min(h, y + rh)
        if x1 > x0 and y1 > y0:
            rects.append((x0, y0, x1, y1))
    return rects


def _ignored_count(w: int, h: int, rects: list) -> int:
    """Pixels covered by the ignored rectangles (overlaps counted once)."""
    if not rects:
        return 0
    n = 0
    for y in range(h):
        spans = sorted((x0, x1) for x0, y0, x1, y1 in rects if y0 <= y < y1)
        end = -1
        for x0, x1 in spans:
            if x1 <= end:
                continue
            n += x1 - max(x0, end) if end > x0 else x1 - x0
            end = max(end, x1)
    return n


class _Cells:
    """Changed-pixel counts and exact bounds per grid cell."""

    def __init__(self, w: int, h: int, cell: int):
        self.cell = cell
        self.gw, self.gh = (w + cell - 1) // cell, (h + cell - 1) // cell
        self.count: dict[int, int] = {}
        self.bounds: dict[int, list[int]] = {}

    def add(self, i: int, n: int, x0: int, y0: int, x1: int, y1: int) -> None:
        self.count[i] = self.count.get(i, 0) + n
        b = self.bounds.get(i)
        if b is None:
            self.bounds[i] = [x0, y0, x1, y1]
        else:
            b[0], b[1], b[2], b[3] = min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)


def _diff_python(base: Img, cur: Img, tol: int, rects: list, cell: int) -> tuple[int, _Cells, list[int]]:
    """(changed pixel count, cells, changed pixel offsets) without Pillow; identical rows are skipped quickly."""
    w, h = cur.w, cur.h
    stride = w * 3
    cells = _Cells(w, h, cell)
    changed: list[int] = []
    a, b = base.rgb, cur.rgb
    for y in range(h):
        o = y * stride
        ra, rb = a[o:o + stride], b[o:o + stride]
        if ra == rb:
            continue
        skip = [(x0, x1) for x0, y0, x1, y1 in rects if y0 <= y < y1]
        cy = (y // cell) * cells.gw
        for x in range(w):
            i = x * 3
            if ra[i:i + 3] == rb[i:i + 3]:
                continue
            if max(abs(ra[i] - rb[i]), abs(ra[i + 1] - rb[i + 1]), abs(ra[i + 2] - rb[i + 2])) <= tol:
                continue
            if skip and any(x0 <= x < x1 for x0, x1 in skip):
                continue
            changed.append(y * w + x)
            cells.add(cy + x // cell, 1, x, y, x, y)
    return len(changed), cells, changed


def _diff_pillow(Image, base: Img, cur: Img, tol: int, rects: list, cell: int):
    """Same result as _diff_python, with Pillow doing the per-pixel work. Returns (count, cells, mask image)."""
    from PIL import ImageChops, ImageDraw
    w, h = cur.w, cur.h
    ia = Image.frombytes("RGB", (w, h), bytes(base.rgb))
    ib = Image.frombytes("RGB", (w, h), bytes(cur.rgb))
    r, g, b = ImageChops.difference(ia, ib).split()
    m = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > tol else 0)
    if rects:
        d = ImageDraw.Draw(m)
        for x0, y0, x1, y1 in rects:
            d.rectangle((x0, y0, x1 - 1, y1 - 1), fill=0)
    n = m.histogram()[255]
    cells = _Cells(w, h, cell)
    box = m.getbbox()
    if n and box:
        gx0, gy0 = box[0] // cell, box[1] // cell
        gx1, gy1 = (box[2] - 1) // cell, (box[3] - 1) // cell
        for gy in range(gy0, gy1 + 1):
            strip = m.crop((0, gy * cell, w, min(h, (gy + 1) * cell)))
            if not strip.getbbox():
                continue
            for gx in range(gx0, gx1 + 1):
                x0 = gx * cell
                part = strip.crop((x0, 0, min(w, x0 + cell), strip.size[1]))
                bb = part.getbbox()
                if not bb:
                    continue
                k = part.histogram()[255]
                cells.add(gy * cells.gw + gx, k, x0 + bb[0], gy * cell + bb[1], x0 + bb[2] - 1, gy * cell + bb[3] - 1)
    return n, cells, m


def _regions(cells: _Cells, w: int, h: int) -> list[dict]:
    """Changed cells grouped into regions: cells within one empty cell of each other join (so the glyphs of one
    label make one box), lone cells under MIN_CELL_PIXELS are dropped. Exact pixel bounds, biggest first."""
    gw = cells.gw
    seen: set[int] = set()
    out = []
    for start in sorted(cells.count):
        if start in seen:
            continue
        seen.add(start)
        stack, members = [start], []
        while stack:
            i = stack.pop()
            members.append(i)
            cx, cy = i % gw, i // gw
            for dy in (-2, -1, 0, 1, 2):
                for dx in (-2, -1, 0, 1, 2):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < gw and 0 <= ny < cells.gh:
                        j = ny * gw + nx
                        if j in cells.count and j not in seen:
                            seen.add(j)
                            stack.append(j)
        px = sum(cells.count[i] for i in members)
        if len(members) == 1 and px < MIN_CELL_PIXELS:
            continue
        bs = [cells.bounds[i] for i in members]
        x0, y0 = min(b[0] for b in bs), min(b[1] for b in bs)
        x1, y1 = max(b[2] for b in bs), max(b[3] for b in bs)
        out.append({"x": x0, "y": y0, "w": x1 - x0 + 1, "h": y1 - y0 + 1, "pixels": px})
    out.sort(key=lambda r: (-r["w"] * r["h"], r["y"], r["x"]))
    return out


def compare_images(base: Img, cur: Img, threshold: float = THRESHOLD, tolerance: int = TOLERANCE,
                   mask_top: Any = "auto", masks: Any = None) -> dict:
    """Pure comparison of two images. Returns the result fields plus '_diff' (what render_diff needs)."""
    if (base.w, base.h) != (cur.w, cur.h):
        return {"verdict": "size-mismatch", "diff_percent": None, "threshold": threshold, "regions": [],
                "regions_total": 0, "width": cur.w, "height": cur.h, "baseline_width": base.w,
                "baseline_height": base.h, "masked_top": 0, "changed_pixels": None, "compared_pixels": None,
                "tolerance": tolerance, "backend": backend(), "_diff": None}
    w, h = cur.w, cur.h
    scale = screen_scale(w, h)
    top = _mask_top(mask_top, w, h)
    rects = _ignored(w, h, top, parse_masks(masks))
    cell = CELL_PT * scale
    Image = _pil()
    if Image:
        n, cells, extra = _diff_pillow(Image, base, cur, tolerance, rects, cell)
    else:
        n, cells, extra = _diff_python(base, cur, tolerance, rects, cell)
    compared = max(1, w * h - _ignored_count(w, h, rects))
    regions = _regions(cells, w, h)
    pct = round(100.0 * n / compared, 3)
    if n and pct == 0:
        pct = 0.001          # a real change never reads as "0 %"
    verdict = "changed" if pct > threshold or (threshold <= 0 and n) else "same"
    return {"verdict": verdict, "diff_percent": pct, "threshold": threshold, "regions": regions[:MAX_REGIONS],
            "regions_total": len(regions), "width": w, "height": h, "masked_top": top, "changed_pixels": n,
            "compared_pixels": compared, "tolerance": tolerance, "backend": "pillow" if Image else "python",
            "_diff": {"rects": rects, "extra": extra, "scale": scale}}


def render_diff(cur: Img, res: dict) -> Img:
    """The diff image: the current screenshot dimmed, changed pixels pink, regions outlined yellow, ignored
    areas striped."""
    d = res["_diff"]
    w, h, scale = cur.w, cur.h, d["scale"]
    pad, stroke = 3 * scale, max(2, scale + 1)
    boxes = [(max(0, r["x"] - pad), max(0, r["y"] - pad), min(w - 1, r["x"] + r["w"] - 1 + pad),
              min(h - 1, r["y"] + r["h"] - 1 + pad)) for r in res["regions"]]
    Image = _pil()
    if Image and not isinstance(d["extra"], list):
        from PIL import ImageDraw
        im = Image.frombytes("RGB", (w, h), bytes(cur.rgb)).point(list(DIM) * 3)
        draw = ImageDraw.Draw(im)
        for x0, y0, x1, y1 in d["rects"]:
            _stripes_pil(draw, x0, y0, x1, y1, scale)
        im.paste(PINK, (0, 0, w, h), d["extra"])
        for b in boxes:
            draw.rectangle(b, outline=YELLOW, width=stroke)
        return Img(w, h, im.tobytes())
    out = bytearray(bytes(cur.rgb).translate(DIM))
    for x0, y0, x1, y1 in d["rects"]:
        step = 6 * scale
        for y in range(y0, y1):
            for x in range(x0, x1):
                if (x + y) % step < scale:
                    i = (y * w + x) * 3
                    out[i:i + 3] = b"\x5a\x5a\x5a"
    extra = d["extra"]
    if not isinstance(extra, list):    # a Pillow mask but Pillow went away: unlikely; draw no pixels
        extra = []
    pink = bytes(PINK)
    for off in extra:
        out[off * 3:off * 3 + 3] = pink
    yel = bytes(YELLOW)
    for x0, y0, x1, y1 in boxes:
        for t in range(stroke):
            for x in range(x0, x1 + 1):
                for y in (y0 + t, y1 - t):
                    if y0 <= y <= y1:
                        out[(y * w + x) * 3:(y * w + x) * 3 + 3] = yel
            for y in range(y0, y1 + 1):
                for x in (x0 + t, x1 - t):
                    if x0 <= x <= x1:
                        out[(y * w + x) * 3:(y * w + x) * 3 + 3] = yel
    return Img(w, h, bytes(out))


def _stripes_pil(draw, x0: int, y0: int, x1: int, y1: int, scale: int) -> None:
    """Diagonal stripes like the pure-Python ones: pixels where (x + y) % step < scale."""
    step = 6 * scale
    for y in range(y0, y1):
        first = x0 + ((-(x0 + y)) % step)
        for xs in range(first, x1, step):
            draw.line((xs, y, min(x1 - 1, xs + scale - 1), y), fill=(0x5a, 0x5a, 0x5a))
        k = (x0 + y) % step     # the run that started left of x0
        if 0 < k < scale:
            draw.line((x0, y, min(x1 - 1, x0 + scale - k - 1), y), fill=(0x5a, 0x5a, 0x5a))


# ================================================================ storage

def root() -> Path:
    if os.environ.get("IOSSTUDIO_DEMO"):
        from iosstudio.demo import demo_art
        return demo_art.demo_dir() / "screenshots"
    return appdirs.DATA_DIR / "screenshots"


def slug(name: Any, what: str = "screen name") -> str:
    """A screen or config name -> its file name: letters, digits, '.', '_' and '-' (others become '-')."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "").strip()).strip("-.")
    s = re.sub(r"-{2,}", "-", s)[:MAX_NAME].strip("-.")
    if not s:
        raise HelperError("invalid-name", f"The {what} needs at least one letter or digit, e.g. 'Home' or "
                                          "'settings-dark'.")
    return s


def project_key(path: str) -> str:
    p = os.path.abspath(os.path.expanduser(path)).rstrip("/") or "/"
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(p) or "project").strip("-.")[:40] or "project"
    return f"{base}-{hashlib.sha1(p.encode()).hexdigest()[:8]}"


def _demo_projects() -> list[str]:
    from iosstudio.demo import demo
    return [p["name"] for p in demo.PROJECTS] if not demo.FRESH else []


def project_dir(flags: dict) -> tuple[str, Path]:
    raw = flags.get("project")
    if not isinstance(raw, str) or not raw.strip():
        raise HelperError("usage", "--project needs the project folder.")
    path = os.path.abspath(os.path.expanduser(raw.strip()))
    if os.environ.get("IOSSTUDIO_DEMO"):
        if os.path.basename(path.rstrip("/")) not in _demo_projects():
            raise HelperError("not-found", "That isn't one of the demo projects.")
    elif not os.path.isdir(path):
        raise HelperError("not-found", "That project folder doesn't exist anymore.")
    d = root() / project_key(path)
    if os.environ.get("IOSSTUDIO_DEMO"):
        _seed_demo(path, d)
    return path, d


def _meta_path(d: Path, name: str) -> Path:
    return d / "baselines" / f"{name}.json"


def _baseline(d: Path, name: str) -> dict | None:
    png = d / "baselines" / f"{name}.png"
    if not png.is_file():
        return None
    meta = fileio.load_json(_meta_path(d, name), {}) or {}
    size = (meta.get("width"), meta.get("height")) if meta.get("width") else image_size(png) or (None, None)
    try:
        st = png.stat()
    except OSError:
        return None
    return {"name": name, "path": str(png), "created": meta.get("created") or st.st_mtime,
            "width": size[0], "height": size[1], "bytes": st.st_size, "device": meta.get("device"),
            "bundle": meta.get("bundle"), "preset": meta.get("preset")}


def baselines(d: Path) -> list[dict]:
    out = []
    for png in sorted((d / "baselines").glob("*.png"), key=lambda p: p.stem.casefold()):
        b = _baseline(d, png.stem)
        if b:
            out.append(b)
    return out


def _find(d: Path, raw: Any) -> str:
    """The stored name for `raw` (exact, else ignoring case), or its slug when nothing matches."""
    name = slug(raw)
    if (d / "baselines" / f"{name}.png").is_file():
        return name
    for png in (d / "baselines").glob("*.png"):
        if png.stem.casefold() == name.casefold():
            return png.stem
    return name


def save_baseline(project: str, d: Path, raw_name: Any, shot: Path, meta: dict | None = None) -> dict:
    name = _find(d, raw_name)
    dst = d / "baselines" / f"{name}.png"
    replaced = dst.is_file()
    w, h = _copy_png(shot, dst)
    info = {"name": name, "created": time.time(), "width": w, "height": h, "project": project,
            **{k: v for k, v in (meta or {}).items() if isinstance(v, str) and v.strip()}}
    fileio.save_json(_meta_path(d, name), info)
    return {"name": name, "baseline_path": str(dst), "width": w, "height": h, "replaced": replaced,
            "created": info["created"]}


def compare_shot(d: Path, raw_name: Any, shot: Path, threshold: float = THRESHOLD, tolerance: int = TOLERANCE,
                 mask_top: Any = "auto", masks: Any = None) -> dict:
    """Keep a copy of `shot`, compare it with the baseline `raw_name` and write the diff image."""
    name = _find(d, raw_name)
    if not shot.is_file():
        raise HelperError("no-shot", f"There's no screenshot at {shot}.")
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    run = d / "compare" / f"{stamp}-{name}"
    current = run / "current.png"
    t0 = time.monotonic()
    base_info = _baseline(d, name)
    out: dict = {"name": name, "threshold": threshold, "baseline_path": base_info["path"] if base_info else None,
                 "current_path": str(current), "diff_path": None, "regions": [], "regions_total": 0}
    try:
        if base_info is None:
            w, h = _copy_png(shot, current)
            out.update(verdict="no-baseline", diff_percent=None, width=w, height=h, masked_top=0)
        else:
            cur = load(shot)
            save_png(current, cur) if png_size(_head(shot)) is None else _copy_png(shot, current)
            base = load(Path(base_info["path"]))
            res = compare_images(base, cur, threshold, tolerance, mask_top, masks)
            out.update({k: v for k, v in res.items() if k != "_diff"})
            out["baseline_created"] = base_info["created"]
            if res["_diff"] is not None:
                diff = run / "diff.png"
                save_png(diff, render_diff(cur, res))
                out["diff_path"] = str(diff)
    except BaseException:
        shutil.rmtree(run, ignore_errors=True)    # a failed compare leaves no half-written run behind
        raise
    out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    prune_compares(d, protect=run)
    return out


def prune_compares(d: Path, protect: Path | None = None) -> int:
    runs = d / "compare"
    if not runs.is_dir():
        return 0
    return fileio.prune([p for p in runs.iterdir() if p.is_dir()], KEEP_COMPARES, COMPARES_MAX_BYTES,
                        protect=[protect] if protect else ())


def forget_project(project: str) -> int:
    """Delete everything kept for a project (baselines, compare runs, matrix sets). Returns the number of
    baselines that went with it; 0 when there was nothing."""
    d = root() / project_key(project)
    if not d.is_dir():
        return 0
    n = len(list((d / "baselines").glob("*.png")))
    shutil.rmtree(d, ignore_errors=True)
    return n


def _head(p: Path) -> bytes:
    try:
        with open(p, "rb") as fh:
            return fh.read(32)
    except OSError:
        return b""


STAMP_LEN = len("20261106-213226-563")   # compare/<stamp>-<name>


def compare_runs(d: Path, name: str) -> list[Path]:
    """The compare runs kept for screen `name` (matched ignoring case, like baseline names)."""
    runs = d / "compare"
    if not runs.is_dir():
        return []
    want = name.casefold()
    return sorted(p for p in runs.iterdir()
                  if p.is_dir() and p.name[STAMP_LEN:STAMP_LEN + 1] == "-" and p.name[STAMP_LEN + 1:].casefold() == want)


def _remove_if_empty(d: Path) -> bool:
    """Remove the project's folder (and its empty sub-folders) when nothing is left in it."""
    for sub in ("baselines", "compare", "matrix"):
        try:
            (d / sub).rmdir()          # only succeeds when empty
        except OSError:
            pass
    try:
        d.rmdir()
        return True
    except OSError:
        return False


def delete_baseline(d: Path, raw_name: Any, confirm: bool) -> dict:
    """Delete a baseline (image + details) and that screen's compare runs; then the project's folder if it's
    empty. Without confirm only says what would go."""
    name = _find(d, raw_name)
    b = _baseline(d, name)
    if b is None:
        have = ", ".join(x["name"] for x in baselines(d)) or "none yet"
        raise HelperError("no-baseline", f"No baseline named '{name}'. This project has: {have}.")
    runs = compare_runs(d, name)
    run_bytes = sum(fileio.dir_size(r) for r in runs)
    removed_dir = False
    if confirm:
        Path(b["path"]).unlink(missing_ok=True)
        _meta_path(d, name).unlink(missing_ok=True)
        for r in runs:
            shutil.rmtree(r, ignore_errors=True)
        removed_dir = _remove_if_empty(d)
    return {"name": name, "baseline_path": b["path"], "deleted": bool(confirm), "dryRun": not confirm,
            "width": b["width"], "height": b["height"], "created": b["created"],
            "compare_runs": len(runs), "compare_bytes": run_bytes, "project_dir": str(d),
            "project_dir_removed": removed_dir}


# ================================================================ matrix (ios_screenshot_matrix)

def _load_json_flag(flags: dict, key: str, example: str) -> Any:
    raw = flags.get(key)
    if raw in (None, True, ""):
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        raise HelperError("usage", f"--{key.replace('_', '-')} must be JSON, e.g. {example}.") from None


def _project_presets(project: str) -> list[dict]:
    from iosstudio.features import run_presets
    if os.environ.get("IOSSTUDIO_DEMO"):
        from iosstudio.demo import demo
        return demo._demo_presets(os.path.basename(project.rstrip("/")))[0]
    from iosstudio.features.projects.store import load_state
    return run_presets.stored(load_state()["meta"].get(project))


def plan_configs(project: str, raw: Any) -> list[dict]:
    """Matrix configs -> [{key, label, preset, language, region, args, env}]. Each item is a preset name
    ("German" or {"preset": "German"}) or an inline config {name, language, region, args, env, uiTestMode};
    no configs means one launch with the app's defaults."""
    from iosstudio.features import run_presets
    items = raw if isinstance(raw, list) else [] if raw is None else [raw]
    if len(items) > MAX_CONFIGS:
        raise HelperError("usage", f"At most {MAX_CONFIGS} launch configs per matrix ({len(items)} given).")
    presets = None
    out, keys = [], set()
    for n, item in enumerate(items or [{"name": "default"}], 1):
        if isinstance(item, str) or (isinstance(item, dict) and item.get("preset")):
            want = item if isinstance(item, str) else str(item["preset"])
            presets = presets if presets is not None else _project_presets(project)
            p = run_presets.find(presets, want)
            label, preset = p["name"], p["name"]
        elif isinstance(item, dict):
            p = run_presets.normalize({**item, "name": item.get("name") or item.get("language") or f"config-{n}"})
            p = run_presets.validate(p)
            label, preset = p["name"], None
        else:
            raise HelperError("usage", "Each config is a preset name or an object such as {\"name\": \"German\", "
                                       "\"language\": \"de\"}.")
        key = base = slug(label, "config name")
        i = 2
        while key.casefold() in keys:
            key, i = f"{base}-{i}", i + 1
        keys.add(key.casefold())
        out.append({"key": key, "label": label, "preset": preset, "language": p["language"], "region": p["region"],
                    **run_presets.resolve(p)})
    return out


def plan_names(raw: Any) -> list[dict]:
    """Screens to capture after each launch, in order -> [{name, press, wait}]. A name is a string, or
    {name, press: "<label or identifier to press first>", wait: seconds}; presses chain from screen to screen."""
    items = raw if isinstance(raw, list) else [] if raw is None else [raw]
    if len(items) > MAX_NAMES:
        raise HelperError("usage", f"At most {MAX_NAMES} screen names per matrix ({len(items)} given).")
    out, seen = [], set()
    for item in items or ["launch"]:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise HelperError("usage", "Each name is a string or {\"name\": \"Settings\", \"press\": \"Settings\"}.")
        name = slug(item.get("name"))
        if name.casefold() in seen:
            raise HelperError("usage", f"The screen name '{name}' is listed twice.")
        seen.add(name.casefold())
        press = item.get("press")
        press = str(press).strip()[:200] if isinstance(press, str) and press.strip() else None
        try:
            wait = max(0.0, min(30.0, float(item.get("wait") if item.get("wait") is not None else (1.5 if press else 0))))
        except (TypeError, ValueError):
            raise HelperError("usage", f"wait for '{name}' must be a number of seconds.") from None
        out.append({"name": name, "press": press, "wait": wait})
    return out


def plan_matrix(project: str, d: Path, configs: Any, names: Any, bundle: str | None = None) -> dict:
    cfgs, nms = plan_configs(project, configs), plan_names(names)
    sets = d / "matrix"
    sets.mkdir(parents=True, exist_ok=True)
    fileio.prune([p for p in sets.iterdir() if p.is_dir()], KEEP_SETS - 1, SETS_MAX_BYTES)
    set_id = time.strftime("%Y%m%d-%H%M%S")
    i = 2
    while (sets / set_id).exists():
        set_id, i = f"{time.strftime('%Y%m%d-%H%M%S')}-{i}", i + 1
    sd = sets / set_id
    sd.mkdir(parents=True)
    manifest = {"set_id": set_id, "set_dir": str(sd), "project": project, "bundle": bundle, "created": time.time(),
                "state": "running", "configs": cfgs, "names": nms, "shots": []}
    fileio.save_json(sd / "manifest.json", manifest)
    return {**manifest, "manifest_path": str(sd / "manifest.json")}


def matrix_save(d: Path, set_id: Any, config: Any, name: Any, shot: Path | None, error: str | None = None) -> dict:
    sid = slug(set_id, "set id")
    sd = d / "matrix" / sid
    mpath = sd / "manifest.json"
    manifest = fileio.load_json(mpath)
    if not isinstance(manifest, dict):
        raise HelperError("not-found", f"No screenshot set '{sid}' for this project (sets older than the newest "
                                       f"{KEEP_SETS} are removed).")
    key, nm = slug(config, "config name"), slug(name)
    entry: dict = {"config": key, "name": nm}
    if shot is not None:
        dst = sd / key / f"{nm}.png"
        w, h = _copy_png(shot, dst)       # cleans up its temp file if the copy fails
        entry.update(path=str(dst), width=w, height=h)
    else:
        entry["error"] = str(error or "Not captured.")[:500]
    shots = [s for s in manifest.get("shots") or [] if not (s.get("config") == key and s.get("name") == nm)]
    shots.append(entry)
    manifest["shots"] = shots
    fileio.save_json(mpath, manifest)
    return {**entry, "set_id": sid, "manifest_path": str(mpath), "count": len(shots)}


MATRIX_STATES = ("done", "cancelled", "failed")


def matrix_finish(d: Path, set_id: Any, state: Any, error: Any = None, restored: Any = None) -> dict:
    """Record how a matrix ended in its manifest: state (done | cancelled | failed), error, app_restored,
    finished (Unix time), and counts, so a reader of manifest.json can tell an incomplete set."""
    if state not in MATRIX_STATES:
        raise HelperError("usage", "--state must be done, cancelled or failed.")
    sid = slug(set_id, "set id")
    mpath = d / "matrix" / sid / "manifest.json"
    manifest = fileio.load_json(mpath)
    if not isinstance(manifest, dict):
        raise HelperError("not-found", f"No screenshot set '{sid}' for this project.")
    shots = manifest.get("shots") or []
    captured = sum(1 for x in shots if x.get("path"))
    expected = len(manifest.get("configs") or []) * len(manifest.get("names") or [])
    manifest.update(state=state, error=str(error)[:500] if isinstance(error, str) and error else None,
                    app_restored=str(restored)[:300] if isinstance(restored, str) and restored else None,
                    finished=time.time(), expected=expected, captured=captured, failed=len(shots) - captured,
                    complete=state == "done" and captured == expected)
    fileio.save_json(mpath, manifest)
    return {"set_id": sid, "manifest_path": str(mpath), **{k: manifest[k] for k in (
        "state", "error", "app_restored", "finished", "expected", "captured", "failed", "complete")}}


# ================================================================ demo

DEMO_BASELINES = {   # project -> [(name, demo_art variant, age in days)]
    "WeatherGlance": [("Home", "older", 2.5), ("Today", "clock", 0.8)],
    "HabitTracker": [("Streaks", "", 4.1)],
}


def _seed_demo(project: str, d: Path) -> None:
    """Made-up baselines for a demo project, written once into the demo dir. "Home" differs from the demo
    screenshot (a changed text line and a badge), "Today" only in the status bar (so it compares as 0 %)."""
    marker = d / ".seeded"
    if marker.exists():
        return
    from iosstudio.demo import demo, demo_art
    for name, variant, age in DEMO_BASELINES.get(os.path.basename(project.rstrip("/")), []):
        png = d / "baselines" / f"{name}.png"
        demo_art.phone_screen(png, variant=variant)
        fileio.save_json(_meta_path(d, name), {"name": name, "created": demo.NOW - age * 86400, "width": 390,
                                               "height": 844, "project": project, "device": demo.DEVICE,
                                               "bundle": next((p["bundleId"] for p in demo.PROJECTS
                                                               if project.endswith("/" + p["name"])), None)})
    d.mkdir(parents=True, exist_ok=True)
    marker.write_text("")


# ================================================================ CLI

def _maybe_reexec() -> None:
    """Re-run this command with a python that has Pillow (the device daemon's venv), once."""
    if _pil() or os.environ.get("IOSSTUDIO_SCREENSHOTS_PURE") or os.environ.get("IOSSTUDIO_SCREENSHOTS_CHILD"):
        return
    py = subproc.pymd3_python()
    if not py or os.path.abspath(py) == os.path.abspath(sys.executable):
        return
    os.environ["IOSSTUDIO_SCREENSHOTS_CHILD"] = "1"
    run_py = str(appdirs.REPO / "helper" / "run.py")
    pycache = str(appdirs.CACHE_DIR / "pycache")
    try:
        sys.stdout.flush()
        os.execv(py, [py, "-I", "-X", f"pycache_prefix={pycache}", run_py, "iosstudio"] + sys.argv[1:])
    except OSError:
        pass      # carry on with the pure-Python codec


def _shot(flags: dict) -> Path:
    raw = flags.get("shot")
    if not isinstance(raw, str) or not raw.strip():
        raise HelperError("usage", "--shot needs the screenshot file (PNG).")
    p = Path(os.path.expanduser(raw.strip()))
    if not p.is_file():
        raise HelperError("no-shot", f"There's no screenshot at {p}.")
    return p


def _float(flags: dict, key: str, default: float, lo: float, hi: float) -> float:
    v = flags.get(key)
    if v in (None, True, ""):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HelperError("usage", f"--{key} must be a number.") from None
    if not lo <= f <= hi:
        raise HelperError("usage", f"--{key} must be between {lo:g} and {hi:g}.")
    return f


def cmd_list(flags: dict) -> int:
    project, d = project_dir(flags)
    return output.ok({"project": project, "dir": str(d), "baselines": baselines(d)})


def cmd_save(flags: dict) -> int:
    shot = _shot(flags)
    if png_size(_head(shot)) is None:
        _maybe_reexec()
    project, d = project_dir(flags)
    meta = {k: flags.get(k) for k in ("device", "bundle", "preset")}
    return output.ok(save_baseline(project, d, flags.get("name"), shot, meta))


def cmd_compare(flags: dict) -> int:
    _maybe_reexec()
    shot = _shot(flags)
    project, d = project_dir(flags)
    threshold = _float(flags, "threshold", THRESHOLD, 0, 100)
    tolerance = int(_float(flags, "tolerance", TOLERANCE, 0, 255))
    return output.ok(compare_shot(d, flags.get("name"), shot, threshold, tolerance,
                                  flags.get("mask_top", "auto"), flags.get("mask")))


def cmd_delete(flags: dict) -> int:
    _project, d = project_dir(flags)
    return output.ok(delete_baseline(d, flags.get("name"), bool(flags.get("confirm"))))


def cmd_matrix_plan(flags: dict) -> int:
    project, d = project_dir(flags)
    configs = _load_json_flag(flags, "configs", '[{"preset": "German"}, {"name": "French", "language": "fr"}]')
    names = _load_json_flag(flags, "names", '["Home", {"name": "Settings", "press": "Settings"}]')
    bundle = flags.get("bundle") if isinstance(flags.get("bundle"), str) else None
    return output.ok(plan_matrix(project, d, configs, names, bundle))


def cmd_matrix_save(flags: dict) -> int:
    _project, d = project_dir(flags)
    err = flags.get("error") if isinstance(flags.get("error"), str) else None
    shot = None if err else _shot(flags)
    return output.ok(matrix_save(d, flags.get("set"), flags.get("config"), flags.get("name"), shot, err))


def cmd_matrix_finish(flags: dict) -> int:
    _project, d = project_dir(flags)
    return output.ok(matrix_finish(d, flags.get("set"), flags.get("state"), flags.get("error"), flags.get("restored")))


GROUPS = {"screenshots": {"list": cmd_list, "save": cmd_save, "compare": cmd_compare, "delete": cmd_delete,
                          "matrix-plan": cmd_matrix_plan, "matrix-save": cmd_matrix_save,
                          "matrix-finish": cmd_matrix_finish}}


def demo(action: str, flags: dict) -> int:
    """Demo mode: the real actions on made-up baselines in the demo dir (see _seed_demo); nothing else changes."""
    handler = GROUPS["screenshots"].get(action)
    if handler is None:
        return output.fail("usage", f"Unknown command 'screenshots {action}'.")
    try:
        return handler(flags)
    except HelperError as exc:
        return output.fail(exc.code, exc.message, exc.fix, exc.detail)
