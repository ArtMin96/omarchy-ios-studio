"""location.set, location.clear (DVT LocationSimulation). The simulated location is global state:
it stays until cleared or until the daemon exits, whichever client set it."""
from __future__ import annotations

from iosstudio.device.real.core import emit, err

KEY = "location"


def register(reg):
    @reg.command("location.set")
    async def c_location_set(d, a):
        try:
            lat, lon = float(a["lat"]), float(a["lon"])
        except (KeyError, TypeError, ValueError):
            raise err("bad-args", "Enter a latitude and longitude (numbers).")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise err("bad-args", "Latitude must be between -90 and 90, longitude between -180 and 180.")
        s, _ = await d.rsd(a)
        loc = await s.dvt_instrument(KEY, s.pmd3.LocationSimulation)
        async with s.dvt_lock:
            await loc.set(lat, lon)
        s.data[KEY] = {"lat": lat, "lon": lon}
        emit("deviceInfo", {"udid": s.udid, "location": s.data[KEY]})
        return {"lat": lat, "lon": lon}

    @reg.command("location.clear")
    async def c_location_clear(d, a):
        s, _ = await d.rsd(a)
        loc = await s.dvt_instrument(KEY, s.pmd3.LocationSimulation)
        async with s.dvt_lock:
            await loc.clear()
        s.data[KEY] = None
        emit("deviceInfo", {"udid": s.udid, "location": None})
        return {}

    @reg.info
    def info(d, s):
        return {"location": s.data.get(KEY)}
