"""Fake device daemon for demo mode (IOSSTUDIO_DEMO set): same protocol as deviced.py
(docs/deviced-protocol.md) with a made-up iPhone. Stdlib only; never talks to a real device.

`--serve` runs it as the shared broker (helper/device_broker.py) on the demo socket
($XDG_RUNTIME_DIR/ios-studio-demo/deviced.sock), so the GUI and MCP evaluations can share one fake
iPhone. `--once <cmd> '<json>'` answers one request and exits.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_art  # noqa: E402
import device_broker  # noqa: E402

SCENARIO = os.environ.get("IOSSTUDIO_DEMO", "ready").lower()
FRESH = SCENARIO == "fresh"
TEAM = "A1B2C3D4E5"
UDID = "00008150-000000000000001C"
DIR = demo_art.demo_dir()
BROKER: device_broker.Broker | None = None


def event(name: str, data) -> None:
    """Device-wide event to every subscribed client (thread-safe)."""
    if BROKER is not None:
        BROKER.emit(name, data)


DEVICE = {"udid": UDID, "name": "Alex's iPhone", "productType": "iPhone18,1", "model": "iPhone 17 Pro", "ios": "27.2",
          "build": "24B115", "transport": "wifi", "usb": False, "wifiPaired": True, "usbPaired": True,
          "state": "connected", "active": True}
INFO = {**{k: DEVICE[k] for k in ("udid", "name", "productType", "model", "ios", "build", "transport")},
        "hardwareModel": "V53AP", "cpu": "arm64e", "deviceClass": "iPhone", "serial": "", "wifiAddress": "02:00:00:00:00:00",
        "timeZone": "Europe/Lisbon", "address": "fe80::1%wlan0",
        "battery": {"level": 78, "charging": False, "external": False, "cycleCount": 212, "health": 96},
        "storage": {"total": 256000000000, "free": 118400000000, "used": 137600000000},
        "developerMode": True, "ddiMounted": True, "locked": False, "passcodeSet": True,
        "wifiPaired": True, "usbPaired": True, "pymobiledevice3": "demo"}

# name, bundle id, hue, shape, developer, running, size MB
APPS = [
    ("WeatherGlance", f"XTL-{TEAM}.com.example.weatherglance", 0.57, "circle", True, True, 38),
    ("HabitTracker", f"XTL-{TEAM}.com.example.habits", 0.36, "ring", True, False, 24),
    ("City Transit", "com.example.citytransit", 0.95, "bars", False, False, 182),
    ("Daily Sketch", "com.example.dailysketch", 0.12, "drop", False, True, 96),
    ("Focus Timer", "com.example.focustimer", 0.0, "ring", False, False, 31),
    ("Lingo Cards", "com.example.lingocards", 0.78, "square", False, False, 144),
    ("Plant Care", "com.example.plantcare", 0.30, "drop", False, False, 58),
    ("Pocket Budget", "com.example.pocketbudget", 0.45, "bars", False, True, 77),
    ("Podcast Player", "com.example.podcasts", 0.83, "circle", False, False, 212),
    ("Trail Runner", "com.example.trailrunner", 0.08, "circle", False, False, 129),
]
running = {b for _, b, _, _, _, r, _ in APPS if r}
pids = {b: 400 + i for i, (_, b, *_) in enumerate(APPS)}   # one pid per app, as apps.list and processes.list report it
installed = {b for _, b, *_ in APPS}


def apps_list() -> list:
    out = []
    for name, bid, hue, shape, dev, _, mb in APPS:
        if bid not in installed:
            continue
        out.append({"bundleId": bid, "name": name, "version": "1.%d.0" % (len(name) % 7), "build": str(10 + len(name)),
                    "developer": dev, "removable": True, "hidden": False, "system": False, "executable": name.replace(" ", ""),
                    "path": "/private/var/containers/Bundle/Application/DEMO/" + name.replace(" ", "") + ".app",
                    "icon": str(demo_art.icon(DIR / "icons" / (bid + ".png"), hue, shape)), "running": bid in running,
                    "pid": pids[bid] if bid in running else None, "size": mb * 1_000_000})
    return sorted(out, key=lambda a: a["name"].lower())


def crash_items(now: float | None = None) -> list:
    # Anchored to a 15-minute step so a name from crash.list still resolves in a later crash.pull.
    now = (time.time() // 900) * 900 if now is None else now
    out = []
    for i, ago in enumerate((3.2 * 3600, 27 * 3600, 30 * 3600)):
        ts = now - ago
        stamp = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(ts))
        proc = "WeatherGlance" if i != 1 else "HabitTracker"
        out.append({"name": f"/{proc}-{stamp}.ips", "file": f"{proc}-{stamp}.ips", "process": proc,
                    "date": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)), "ts": ts, "kind": "crash", "retired": False})
    ts = now - 50 * 3600
    out.append({"name": "/JetsamEvent-demo.ips", "file": "JetsamEvent-demo.ips", "process": "JetsamEvent",
                "date": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)), "ts": ts, "kind": "jetsam", "retired": False})
    return out


def crash_pull(name: str) -> dict:
    proc = "HabitTracker" if "Habit" in name else ("JetsamEvent" if "Jetsam" in name else "WeatherGlance")
    base = (time.time() // 900) * 900
    item = next((c for c in crash_items(base) + crash_items(base - 900) if c["name"] == name), None)
    when = time.strftime("%Y-%m-%d %H:%M:%S.00", time.localtime(item["ts"] if item else time.time()))
    path = DIR / "crashes" / Path(name).name
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = ["0 libsystem_kernel.dylib __pthread_kill + 8", "1 libsystem_pthread.dylib pthread_kill + 268",
              "2 libsystem_c.dylib abort + 148", "3 libswiftCore.dylib _assertionFailure(_:_:file:line:flags:) + 1240",
              f"4 {proc} ForecastStore.load(from:) + 412" if proc == "WeatherGlance" else f"4 {proc} StreakView.body.getter + 96",
              f"5 {proc} closure #1 in ContentView.body.getter + 88", "6 SwiftUI ViewGraph.updateOutputs() + 220",
              "7 UIKitCore -[UIApplication _run] + 888", "8 UIKitCore UIApplicationMain + 340", f"9 {proc} main + 64"]
    summary = {"process": proc, "bundleId": f"XTL-{TEAM}.com.example." + ("habits" if proc == "HabitTracker" else "weatherglance"),
               "appVersion": "1.2.0 (14)", "osVersion": "iPhone OS 27.2 (24B115)", "date": when,
               "exceptionType": "EXC_BREAKPOINT" if proc == "WeatherGlance" else "EXC_CRASH",
               "signal": "SIGTRAP" if proc == "WeatherGlance" else "SIGABRT",
               "terminationReason": "Fatal error: Unexpectedly found nil while unwrapping an Optional value" if proc == "WeatherGlance" else "Abort trap: 6",
               "crashedThread": 0, "frames": frames, "bugType": "309", "incident": "11111111-2222-4333-8444-555555555555"}
    if proc == "JetsamEvent":
        summary.update(exceptionType=None, signal=None, terminationReason="Memory pressure", frames=[], bugType="298")
    header = {"app_name": proc, "timestamp": summary["date"], "app_version": "1.2.0", "bug_type": summary["bugType"],
              "os_version": summary["osVersion"], "incident_id": summary["incident"]}
    path.write_text(json.dumps(header) + "\n" + json.dumps({"exception": {"type": summary["exceptionType"], "signal": summary["signal"]},
                                                            "frames": frames}, indent=2) + "\n")
    return {"path": str(path), "summary": summary}


# ---------------------------------------------------------------- syslog / mirror

mirror_on = threading.Event()
mirror_fps = [1.0]

LOG_LINES = [
    ("WeatherGlance", "default", "com.example.weatherglance", "network", "Fetching forecast for 3 saved places"),
    ("WeatherGlance", "info", "com.example.weatherglance", "location", "Location authorization: authorizedWhenInUse"),
    ("WeatherGlance", "default", "com.example.weatherglance", "network", "Forecast updated in 412 ms (24 hourly, 10 daily)"),
    ("WeatherGlance", "debug", "com.example.weatherglance", "ui", "ForecastView appeared"),
    ("WeatherGlance", "error", "com.example.weatherglance", "network", "Request timed out for place 'Lisbon', retrying in 2 s"),
    ("WeatherGlance", "default", "com.apple.UIKit", "scene", "Scene did become active"),
    ("SpringBoard", "default", "com.apple.springboard", "app", "Application launched: com.example.weatherglance"),
    ("WeatherGlance", "fault", "com.example.weatherglance", "store", "Cache file is corrupt, rebuilding"),
    ("WeatherGlance", "info", "com.example.weatherglance", "widgets", "Reloaded 2 widget timelines"),
]


def mirror_loop() -> None:
    seq = 0
    while True:
        if not mirror_on.is_set():
            time.sleep(0.2)
            continue
        seq += 1
        p = DIR / "frames" / f"frame-{seq % 3}.png"
        demo_art.phone_screen(p, seq)
        event("frame", {"path": str(p), "seq": seq, "ts": time.time(), "width": 390, "height": 844})
        time.sleep(1.0 / max(0.2, mirror_fps[0]))


# ---------------------------------------------------------------- requests

def handle(cmd: str, a: dict):
    if cmd == "daemon.ping":
        return {"pong": True, "ts": time.time()}
    if cmd in ("devices.list",):
        return [] if FRESH else [DEVICE]
    if cmd == "device.select":
        return DEVICE
    if cmd == "device.reconnect":
        return {}
    if cmd == "device.info":
        return INFO
    if cmd == "apps.list":
        return apps_list()
    if cmd == "app.launch":
        running.add(a.get("bundleId"))
        pids[a.get("bundleId")] = 812
        return {"bundleId": a.get("bundleId"), "pid": 812}
    if cmd == "app.kill":
        running.discard(a.get("bundleId"))
        return {"bundleId": a.get("bundleId"), "killed": [812]}
    if cmd == "app.uninstall":
        installed.discard(a.get("bundleId"))
        return {"bundleId": a.get("bundleId")}
    if cmd == "app.install":
        for pct in (0, 35, 70, 100):
            event("installProgress", {"path": a.get("path"), "percent": pct, "status": "done" if pct == 100 else ("uploading" if pct == 0 else "installing")})
            time.sleep(0.5)
        name = Path(str(a.get("path") or "App.ipa")).stem
        bid = next((b for n, b, *_ in APPS if n == name), f"XTL-{TEAM}.com.example." + name.lower())
        installed.add(bid)
        return {"path": a.get("path"), "bundleId": bid, "packageBundleId": bid.split(f"XTL-{TEAM}.")[-1], "seconds": 2.0}
    if cmd == "processes.list":
        return [{"pid": pids.get(b, 812), "name": n.replace(" ", ""), "path": "", "isApp": True, "bundleId": b}
                for n, b, *_ in APPS if b in running]
    if cmd == "screenshot":
        p = demo_art.phone_screen(DIR / "screens" / f"screenshot-{int(time.time())}.png")
        return {"path": str(p), "width": 390, "height": 844, "bytes": p.stat().st_size, "ts": time.time()}
    if cmd == "mirror.start":
        mirror_fps[0] = max(0.2, min(4.0, float(a.get("fps") or 1.0)))
        mirror_on.set()
        event("mirror", {"running": True, "fps": mirror_fps[0]})
        return {"running": True, "fps": mirror_fps[0]}
    if cmd == "mirror.stop":
        mirror_on.clear()
        event("mirror", {"running": False, "fps": mirror_fps[0]})
        return {"running": False}
    if cmd == "location.set":
        INFO["location"] = {"lat": a.get("lat"), "lon": a.get("lon")}
        event("deviceInfo", {"udid": UDID, "location": INFO["location"]})
        return {"lat": a.get("lat"), "lon": a.get("lon")}
    if cmd == "location.clear":
        INFO["location"] = None
        event("deviceInfo", {"udid": UDID, "location": None})
        return {}
    if cmd in ("devmode.reveal", "pair.cancel", "pair.code"):
        return {}
    if cmd in ("ddi.status", "ddi.mount"):
        return {"mounted": True}
    if cmd == "devmode.status":
        return {"enabled": True}
    if cmd == "profiles.list":
        now = time.time()
        return [{"name": "XTL profile " + n, "uuid": "00000000-0000-4000-8000-00000000000%d" % i, "appId": f"{TEAM}.{b}",
                 "team": TEAM, "teamName": "Alex Rivera", "created": now - (2 + i) * 86400, "expires": now + (5 - i) * 86400,
                 "expired": False, "devices": 1, "isXcodeManaged": False}
                for i, (n, b, *_) in enumerate(APPS[:2])]
    if cmd == "pair.status":
        return {"records": [{"udid": UDID, "wifi": True, "usb": True, "path": ""}], "usb": []}
    if cmd == "pair.start":
        return {"started": True}
    if cmd == "crash.list":
        return crash_items()
    if cmd == "crash.pull":
        return crash_pull(str(a.get("name") or ""))
    raise LookupError(f"Unknown command {cmd!r}.")


class DemoBackend:
    """device_broker backend for the fake iPhone."""

    def __init__(self, broker: device_broker.Broker):
        self.b = broker
        self.syslog_task: asyncio.Task | None = None

    async def welcome(self):
        out = [("ready", {"version": "1", "pymobiledevice3": "demo", "python": sys.version.split()[0], "pid": os.getpid(),
                          "demo": SCENARIO})]
        if FRESH:
            out += [("devices", []), ("connection", {"udid": None, "state": "idle", "transport": None,
                                                     "message": "No iPhone selected", "detail": "", "retryIn": 0,
                                                     "since": time.time()})]
        else:
            out += [("devices", [DEVICE]), ("connection", {"udid": UDID, "state": "connected", "transport": "wifi",
                                                           "message": "Connected over Wi-Fi", "detail": "", "retryIn": 0,
                                                           "since": STARTED - 900}), ("deviceInfo", INFO)]
        if mirror_on.is_set():
            out.append(("mirror", {"running": True, "fps": mirror_fps[0]}))
        return out

    async def dispatch(self, cmd: str, a: dict):
        return await asyncio.to_thread(handle, cmd, a)

    def error_json(self, exc: BaseException, cmd: str) -> dict:
        if isinstance(exc, LookupError):
            return {"code": "bad-args", "message": str(exc).strip("'\"")}
        return {"code": "failed", "message": "Demo mode can't do that.", "detail": str(exc)}

    # syslog: one fake stream, filtered per client by the broker
    def syslog_target(self, args):
        if FRESH:
            raise RuntimeError("no device")
        return UDID

    def syslog_open(self, pid: int):
        if self.syslog_task is None or self.syslog_task.done():
            self.syslog_task = asyncio.create_task(self._syslog())

    async def syslog_close(self):
        t, self.syslog_task = self.syslog_task, None
        if t:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t

    async def syslog_resolve(self, bid: str):
        for name, b, *_ in APPS:
            if b == bid or b.endswith("." + bid):
                return name.replace(" ", ""), b
        return None, bid

    async def _syslog(self):
        i = 0
        hub = self.b.syslog
        while True:
            await asyncio.sleep(0.7)
            for _ in range(random.randint(1, 3)):
                proc, lvl, sub, cat, msg = LOG_LINES[i % len(LOG_LINES)]
                i += 1
                hub.feed({"ts": time.time(), "process": proc, "pid": 812 if proc != "SpringBoard" else 54, "level": lvl,
                          "subsystem": sub, "category": cat, "message": msg, "image": proc})

    async def shutdown(self):
        mirror_on.clear()
        await self.syslog_close()


STARTED = time.time()


async def serve() -> int:
    global BROKER
    BROKER = device_broker.Broker(demo=SCENARIO)
    if not BROKER.acquire():
        return 0   # another demo daemon is live
    BROKER.redirect_stderr()
    threading.Thread(target=mirror_loop, daemon=True).start()

    async def make(b):
        return DemoBackend(b)
    return await BROKER.run(make)


def once(cmd: str, raw: str) -> int:
    try:
        data = handle(cmd, json.loads(raw or "{}"))
        print(json.dumps({"ok": True, "data": data}))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": DemoBackend(None).error_json(exc, cmd)}))
        return 1


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--once":
        return once(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "{}")
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        code = asyncio.run(serve())
        os._exit(code)
    print("usage: deviced_demo.py --serve | --once <cmd> '<json>'  (normally started by bin/iosstudio-deviced)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
