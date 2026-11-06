"""Writes the screenshot-compare fixtures (stdlib only): python3 tests/fixtures/screenshots/make_fixtures.py

A 120x260 "phone screen" (scale 1, taller than 2:1, so the auto status-bar mask is 54 px) and variants:
  base.png        RGB, no row filters
  same-rgba.png   the same pixels as RGBA with Sub/Up/Average/Paeth rows (decoder check, compares as 0 %)
  label.png       the label at x 20-60, y 120-131 is wider (to x 90): one changed region x 61-90, y 120-131
  status.png      only the clock in the status bar differs (0 % with the mask, changed without)
  noise.png       every pixel off by up to 8 per channel (under the tolerance: 0 %)
  wide.png        130x260 (size-mismatch)
"""
from __future__ import annotations

import random
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
W, H = 120, 260


def paint(w=W, h=H, label_end=60, clock_end=40, noise=0):
    rnd = random.Random(7)
    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            px = [30 + y * 100 // h, 60 + x * 60 // w, 160]
            if 8 <= y <= 16 and 10 <= x <= clock_end:          # clock
                px = [240, 240, 240]
            if 8 <= y <= 16 and 90 <= x <= 110:                 # battery
                px = [240, 240, 240]
            if 70 <= y <= 100 and 20 <= x <= 100:               # sun
                px = [250, 200, 90]
            if 120 <= y <= 131 and 20 <= x <= label_end:        # label
                px = [250, 250, 250]
            if 180 <= y <= 230 and 10 <= x <= 110 and not (190 <= y <= 220 and 20 <= x <= 100):   # card border
                px = [200, 210, 230]
            if noise:
                px = [max(0, min(255, c + rnd.randint(-noise, noise))) for c in px]
            row.append(px)
        rows.append(row)
    return rows


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def write(path, rows, alpha=False, filters=(0,)):
    h, w = len(rows), len(rows[0])
    bpp = 4 if alpha else 3
    raw = bytearray()
    prev = bytes(w * bpp)
    for y, row in enumerate(rows):
        line = bytes(b for px in row for b in (px + [255] if alpha else px))
        ft = filters[y % len(filters)]
        out = bytearray(len(line))
        for i, v in enumerate(line):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ft == 0:
                p = 0
            elif ft == 1:
                p = a
            elif ft == 2:
                p = b
            elif ft == 3:
                p = (a + b) >> 1
            else:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                p = a if pa <= pb and pa <= pc else b if pb <= pc else c
            out[i] = (v - p) & 255
        raw += bytes([ft]) + out
        prev = line
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if alpha else 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


if __name__ == "__main__":
    write(HERE / "base.png", paint())
    write(HERE / "same-rgba.png", paint(), alpha=True, filters=(1, 2, 3, 4, 0))
    write(HERE / "label.png", paint(label_end=90))
    write(HERE / "status.png", paint(clock_end=60))
    write(HERE / "noise.png", paint(noise=8))
    write(HERE / "wide.png", paint(w=130))
