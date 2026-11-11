"""A stand-in for ImageMagick 7's `magick` (set IOSSTUDIO_MAGICK to it; write it with write_fake_magick).

It appends each argv as a JSON line to $FAKE_MAGICK_LOG.
  magick identify -format F FILE[0]   prints "W H FORMAT OPAQUE sRGB " read from a PNG or JPEG header
  magick FILE[0] … PNG24:OUT          writes a valid 1024×1024 8-bit RGB PNG to OUT, painted $FAKE_MAGICK_COLOR
$FAKE_MAGICK_MODE: "bad" writes a 512×512 RGBA PNG instead, "fail" exits 1 with an error, "unreadable" makes
identify fail like a file that isn't a picture.
"""
from __future__ import annotations

from pathlib import Path

from support import write_exe

SCRIPT = r'''#!/usr/bin/env python3
import json, os, struct, sys, zlib
args = sys.argv[1:]
with open(os.environ["FAKE_MAGICK_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
mode = os.environ.get("FAKE_MAGICK_MODE", "")

def header(path):
    data = open(path, "rb").read()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h, _d, color = struct.unpack(">IIBB", data[16:26])
        return w, h, "PNG", color not in (4, 6) and b"tRNS" not in data
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data):
            marker, n = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xC3:
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h, "JPEG", True
            i += 2 + n
    return None

def png(w, h, color, rgb):
    def chunk(k, b):
        return struct.pack(">I", len(b)) + k + b + struct.pack(">I", zlib.crc32(k + b) & 0xFFFFFFFF)
    px = bytes(color) if rgb else bytes(color) + b"\xff"
    raw = b"".join(b"\x00" + px * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2 if rgb else 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

if args[:1] == ["identify"]:
    info = None if mode == "unreadable" else header(args[-1].rsplit("[", 1)[0])
    if not info:
        sys.stderr.write("identify: no decode delegate for this image format\n"); sys.exit(1)
    w, h, fmt, opaque = info
    print(f"{w} {h} {fmt} {'True' if opaque else 'False'} sRGB ")
    sys.exit(0)
if mode == "fail":
    sys.stderr.write("magick: unable to open image\n"); sys.exit(1)
out = args[-1].split(":", 1)[1]
color = tuple(int(os.environ.get("FAKE_MAGICK_COLOR", "#336699")[i:i + 2], 16) for i in (1, 3, 5))
with open(out, "wb") as fh:
    fh.write(png(512, 512, color, False) if mode == "bad" else png(1024, 1024, color, True))
'''


def write_fake_magick(path: Path) -> Path:
    return write_exe(path, SCRIPT)
