"""Demo: location.set, location.clear."""
from __future__ import annotations

from iosstudio.device.deviced_demo_core import INFO, UDID, event


def register(reg):
    @reg.command("location.set")
    def location_set(ctx, a):
        INFO["location"] = {"lat": a.get("lat"), "lon": a.get("lon")}
        event("deviceInfo", {"udid": UDID, "location": INFO["location"]})
        return {"lat": a.get("lat"), "lon": a.get("lon")}

    @reg.command("location.clear")
    def location_clear(ctx, a):
        INFO["location"] = None
        event("deviceInfo", {"udid": UDID, "location": None})
        return {}
