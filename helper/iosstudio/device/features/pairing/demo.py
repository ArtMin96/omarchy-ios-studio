"""Demo: pair.status, pair.start, pair.code, pair.cancel."""
from __future__ import annotations

from iosstudio.device.demo.core import UDID


def register(reg):
    reg.command("pair.status", lambda ctx, a: {"records": [{"udid": UDID, "wifi": True, "usb": True, "path": ""}], "usb": []})
    reg.command("pair.start", lambda ctx, a: {"started": True})
    reg.command("pair.code", lambda ctx, a: {})
    reg.command("pair.cancel", lambda ctx, a: {})
