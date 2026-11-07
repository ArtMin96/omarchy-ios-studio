"""Tiny synthetic Xcode .xip archives (xar -> pbzx -> odc cpio) for helper/iosstudio/features/xip_version.py."""
from __future__ import annotations

import lzma
import plistlib
import struct
import zlib

VERSION_PLIST = plistlib.dumps({"CFBundleShortVersionString": "27.1", "ProductBuildVersion": "27A123"})


def cpio_entry(name: str, data: bytes, magic: bytes = b"070707") -> bytes:
    """One odc cpio member (76-byte ASCII octal header, NUL-terminated name, data)."""
    raw = name.encode() + b"\0"
    head = (magic + b"000000" * 7 + b"%011o" % 0 + b"%06o" % len(raw) + b"%011o" % len(data))
    assert len(head) == 76
    return head + raw + data


def cpio(members: list[tuple[str, bytes]], trailer: bool = True) -> bytes:
    out = b"".join(cpio_entry(n, d) for n, d in members)
    return out + (cpio_entry("TRAILER!!!", b"") if trailer else b"")


def pbzx(payload: bytes, chunk: int = 100, compress_every_other: bool = True) -> bytes:
    """A pbzx stream: magic, 8-byte flags, then (flags, size, data) chunks; every other chunk xz-compressed."""
    out = [b"pbzx", struct.pack(">Q", 1 << 24)]
    for i in range(0, len(payload), chunk):
        piece = payload[i:i + chunk]
        data = lzma.compress(piece, format=lzma.FORMAT_XZ) if compress_every_other and (i // chunk) % 2 == 0 else piece
        out.append(struct.pack(">QQ", 1 << 24, len(data)) + data)
    return b"".join(out)


def xar(content: bytes, name: str = "Content", magic: bytes = b"xar!") -> bytes:
    """A xar archive whose heap holds one member `name` = content."""
    toc = (f"<?xml version='1.0'?><xar><toc><file id='1'><name>{name}</name><data><offset>0</offset>"
           f"<length>{len(content)}</length></data></file></toc></xar>").encode()
    packed = zlib.compress(toc)
    header = struct.pack(">4sHHQQI", magic, 28, 1, len(packed), len(toc), 1)
    return header + packed + content


def xcode_xip(plist: bytes = VERSION_PLIST, app: str = "Xcode.app") -> bytes:
    """A small but structurally real .xip whose cpio holds a filler file and <app>/Contents/version.plist."""
    members = [("./" + app, b""), ("./" + app + "/Contents/Info.plist", b"x" * 333),
               ("./" + app + "/Contents/version.plist", plist)]
    return xar(pbzx(cpio(members)))
