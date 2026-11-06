"""Tiny stdlib-only PNG painter for demo mode: app icons and a fake iPhone screen.

Nothing here touches a real device or account. Files go to demo_dir() (under $XDG_RUNTIME_DIR).
"""
from __future__ import annotations

import colorsys
import os
import struct
import zlib
from pathlib import Path


def demo_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    d = Path(base) / "ios-studio-demo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_png(path: Path, w: int, h: int, rows: list[bytes]) -> None:
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(png)
    tmp.replace(path)


def _rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _inside_round(x: int, y: int, x0: int, y0: int, x1: int, y1: int, r: int) -> bool:
    if x < x0 or x >= x1 or y < y0 or y >= y1:
        return False
    cx = min(max(x, x0 + r), x1 - r - 1)
    cy = min(max(y, y0 + r), y1 - r - 1)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def icon(path: Path, hue: float, shape: str = "circle", size: int = 120) -> Path:
    """Rounded-square app icon: diagonal gradient plus a simple white mark."""
    if path.exists():
        return path
    c0, c1 = _rgb(hue, 0.55, 0.95), _rgb(hue + 0.08, 0.75, 0.70)
    r = int(size * 0.225)
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            if not _inside_round(x, y, 0, 0, size, size, r):
                row += b"\x00\x00\x00\x00"
                continue
            t = (x + y) / (2 * size)
            px = [int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)]
            dx, dy = x - size / 2, y - size / 2
            mark = False
            if shape == "circle":
                mark = dx * dx + dy * dy <= (size * 0.24) ** 2
            elif shape == "ring":
                d2 = dx * dx + dy * dy
                mark = (size * 0.17) ** 2 <= d2 <= (size * 0.27) ** 2
            elif shape == "bars":
                mark = any(abs(dx - off) < size * 0.06 and dy > -hh for off, hh in
                           ((-size * 0.2, size * 0.05), (0, size * 0.2), (size * 0.2, size * 0.12))) and dy < size * 0.22
            elif shape == "square":
                mark = abs(dx) < size * 0.2 and abs(dy) < size * 0.2
            elif shape == "drop":
                mark = (dx * dx + (dy - size * 0.06) ** 2 <= (size * 0.18) ** 2) or (abs(dx) < (size * 0.18) * (1 - (-(dy + size * 0.02)) / (size * 0.3)) and -size * 0.32 < dy < size * 0.0)
            if mark:
                px = [255, 255, 255]
            row += bytes(px) + b"\xff"
        rows.append(bytes(row))
    _write_png(path, size, size, rows)
    return path


def phone_screen(path: Path, seq: int = 0, w: int = 390, h: int = 844, variant: str = "") -> Path:
    """A plausible weather-app screen for the mirror / screenshot views.

    variant (demo screenshot baselines, helper/screenshots.py): "older" has different text lines, a badge in the
    first card and another clock width; "clock" differs from the default only in the status bar."""
    top, bottom = _rgb(0.58, 0.55, 0.85), _rgb(0.70, 0.60, 0.45)
    # cards: (x0, y0, x1, y1, alpha)
    cards = [(24, 300, 366, 470, 0.20), (24, 490, 366, 640, 0.16), (24, 660, 366, 790, 0.16)]
    lines = [(48, 330, 220, 344), (48, 360, 300, 370), (48, 380, 260, 390),
             (48, 520, 180, 532), (48, 548, 330, 558), (48, 690, 200, 702), (48, 718, 310, 728)]
    temp_r = 70
    sun_x = 195 + int(20 * ((seq % 10) / 10.0))
    clock = (30, 70)
    if variant == "older":
        lines[1], lines[3] = (48, 360, 232, 370), (48, 520, 252, 532)
        lines.append((292, 322, 342, 342))      # a "NEW" badge the current screen no longer has
        clock = (30, 62)
    elif variant == "clock":
        clock = (30, 58)
    rows = []
    for y in range(h):
        t = y / h
        base = [int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)]
        row = bytearray()
        for x in range(w):
            px = list(base)
            # status bar pill + "time" dots
            if 14 <= y <= 26 and (clock[0] <= x <= clock[1] or 300 <= x <= 360):
                px = [245, 245, 250]
            # sun disc
            dx, dy = x - sun_x, y - 170
            if dx * dx + dy * dy <= temp_r * temp_r:
                k = 1 - (dx * dx + dy * dy) / (temp_r * temp_r)
                px = [int(px[0] + (255 - px[0]) * (0.55 + 0.45 * k)), int(px[1] + (214 - px[1]) * (0.55 + 0.45 * k)),
                      int(px[2] + (120 - px[2]) * 0.6)]
            for x0, y0, x1, y1, a in cards:
                if _inside_round(x, y, x0, y0, x1, y1, 22):
                    px = [int(c + (255 - c) * a) for c in px]
            for x0, y0, x1, y1 in lines:
                if _inside_round(x, y, x0, y0, x1, y1, 5):
                    px = [240, 242, 248]
            # home indicator
            if h - 20 <= y <= h - 14 and 130 <= x <= 260:
                px = [250, 250, 250]
            row += bytes(px) + b"\xff"
        rows.append(bytes(row))
    _write_png(path, w, h, rows)
    return path


def photo(path: Path, seed: int = 0, w: int = 200, h: int = 200) -> Path:
    """A made-up camera roll picture for demo mode: sky, sun, two hill ridges and water, varied by seed."""
    if path.exists():
        return path
    import math
    import random
    rnd = random.Random(seed)
    hue = rnd.random()
    sky0, sky1 = _rgb(hue, 0.35 + rnd.random() * 0.3, 0.95), _rgb(hue + 0.07, 0.55, 0.65)
    hill = [_rgb(hue + 0.33 + rnd.random() * 0.1, 0.45, 0.45 + rnd.random() * 0.2),
            _rgb(hue + 0.30, 0.55, 0.28 + rnd.random() * 0.12)]
    water = _rgb(hue + 0.02, 0.4, 0.55)
    sx, sy, sr = rnd.uniform(0.2, 0.8) * w, rnd.uniform(0.15, 0.4) * h, rnd.uniform(0.06, 0.12) * w
    waves = [(rnd.uniform(0.45, 0.6), rnd.uniform(2, 5), rnd.uniform(0, 6), rnd.uniform(0.03, 0.07)),
             (rnd.uniform(0.6, 0.72), rnd.uniform(3, 7), rnd.uniform(0, 6), rnd.uniform(0.02, 0.05))]
    has_water = rnd.random() < 0.5
    rows = []
    for y in range(h):
        t = y / h
        base = [int(sky0[i] + (sky1[i] - sky0[i]) * t) for i in range(3)]
        row = bytearray()
        for x in range(w):
            px = base
            dx, dy = x - sx, y - sy
            d2 = dx * dx + dy * dy
            if d2 <= sr * sr:
                px = [255, 236, 190]
            elif d2 <= (sr * 2.2) ** 2:
                k = 1 - math.sqrt(d2) / (sr * 2.2)
                px = [int(c + (255 - c) * 0.35 * k) for c in base]
            for (lvl, freq, ph, amp), col in zip(waves, hill):
                if t > lvl + amp * math.sin(freq * x / w * math.pi + ph):
                    px = [int(c * (0.85 + 0.15 * (1 - t))) for c in col]
            if has_water and t > 0.82:
                k = 0.5 + 0.5 * math.sin(y * 0.9 + x * 0.05)
                px = [int(c * (0.8 + 0.2 * k)) for c in water]
            row += bytes(px) + b"\xff"
        rows.append(bytes(row))
    _write_png(path, w, h, rows)
    return path
