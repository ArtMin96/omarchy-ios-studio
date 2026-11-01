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


def phone_screen(path: Path, seq: int = 0, w: int = 390, h: int = 844) -> Path:
    """A plausible weather-app screen for the mirror / screenshot views."""
    top, bottom = _rgb(0.58, 0.55, 0.85), _rgb(0.70, 0.60, 0.45)
    # cards: (x0, y0, x1, y1, alpha)
    cards = [(24, 300, 366, 470, 0.20), (24, 490, 366, 640, 0.16), (24, 660, 366, 790, 0.16)]
    lines = [(48, 330, 220, 344), (48, 360, 300, 370), (48, 380, 260, 390),
             (48, 520, 180, 532), (48, 548, 330, 558), (48, 690, 200, 702), (48, 718, 310, 728)]
    temp_r = 70
    sun_x = 195 + int(20 * ((seq % 10) / 10.0))
    rows = []
    for y in range(h):
        t = y / h
        base = [int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)]
        row = bytearray()
        for x in range(w):
            px = list(base)
            # status bar pill + "time" dots
            if 14 <= y <= 26 and (30 <= x <= 70 or 300 <= x <= 360):
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
