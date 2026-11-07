"""Demo: pair.status, pair.start, pair.code, pair.cancel (same replies and error codes as the real ones)."""
from __future__ import annotations

from iosstudio.core.output import HelperError
from iosstudio.device.demo.core import DEVICE, UDID


def _usb() -> list[str]:
    return [UDID] if DEVICE.get("usb") else []   # demo.transport plugs the made-up cable in


def register(reg):
    @reg.command("pair.status")
    def status(ctx, a):
        return {"records": [{"udid": UDID, "wifi": True, "usb": True, "path": ""}], "usb": _usb()}

    @reg.command("pair.start")
    def start(ctx, a):
        usb = _usb()
        method = a.get("method") or ("usb" if usb else "wifi")
        if method not in ("usb", "wifi"):
            raise HelperError("bad-args", "Pairing method must be usb or wifi.")
        if method == "usb" and (a.get("udid") or UDID) not in usb:
            raise HelperError("unreachable", "Plug your iPhone in with a USB cable and unlock it, then try again.")
        return {"started": True, "method": method}

    @reg.command("pair.code")
    def code(ctx, a):
        # No demo flow asks for a code, like the real daemon today.
        raise HelperError("bad-args", "No pairing code was asked for.")

    reg.command("pair.cancel", lambda ctx, a: {})
