"""Fake device daemon for demo mode (IOSSTUDIO_DEMO set): same protocol as deviced.py
(docs/deviced-protocol.md) with a made-up iPhone. Stdlib only; never talks to a real device.
"""
from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_art  # noqa: E402

SCENARIO = os.environ.get("IOSSTUDIO_DEMO", "ready").lower()
FRESH = SCENARIO == "fresh"
TEAM = "A1B2C3D4E5"
UDID = "00008150-000000000000001C"
DIR = demo_art.demo_dir()
_out = threading.Lock()


def send(obj: dict) -> None:
    with _out:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def event(name: str, data) -> None:
    send({"event": name, "data": data})


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
                    "pid": 400 + len(name) if bid in running else None, "size": mb * 1_000_000})
    return sorted(out, key=lambda a: a["name"].lower())


def crash_items() -> list:
    now = time.time()
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
    item = next((c for c in crash_items() if c["name"][-11:] == name[-11:] or c["name"] == name), None)
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

syslog_on = threading.Event()
syslog_filters: dict = {}
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


def syslog_loop() -> None:
    i = 0
    while True:
        time.sleep(0.7)
        if not syslog_on.is_set():
            continue
        batch = []
        for _ in range(random.randint(1, 3)):
            proc, lvl, sub, cat, msg = LOG_LINES[i % len(LOG_LINES)]
            i += 1
            procs = syslog_filters.get("processes") or []
            if procs and proc not in procs:
                continue
            batch.append({"ts": time.time(), "process": proc, "pid": 812 if proc != "SpringBoard" else 54, "level": lvl,
                          "subsystem": sub, "category": cat, "message": msg, "image": proc})
        if batch:
            event("syslog", batch)


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
        return {"path": a.get("path"), "bundleId": bid, "packageBundleId": bid.split(f"XTL-{TEAM}.")[-1], "seconds": 2.0}
    if cmd == "processes.list":
        return [{"pid": 400 + i, "name": n.replace(" ", ""), "path": "", "isApp": True, "bundleId": b}
                for i, (n, b, *_) in enumerate(APPS) if b in running]
    if cmd == "screenshot":
        p = demo_art.phone_screen(DIR / "screens" / f"screenshot-{int(time.time())}.png")
        return {"path": str(p), "width": 390, "height": 844, "bytes": p.stat().st_size, "ts": time.time()}
    if cmd == "mirror.start":
        mirror_fps[0] = float(a.get("fps") or 1.0)
        mirror_on.set()
        event("mirror", {"running": True, "fps": mirror_fps[0]})
        return {"running": True, "fps": mirror_fps[0]}
    if cmd == "mirror.stop":
        mirror_on.clear()
        event("mirror", {"running": False, "fps": mirror_fps[0]})
        return {"running": False}
    if cmd == "location.set":
        return {"lat": a.get("lat"), "lon": a.get("lon")}
    if cmd in ("location.clear", "devmode.reveal", "pair.cancel", "pair.code"):
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
    if cmd == "syslog.start" or cmd == "syslog.filter":
        syslog_filters.clear()
        syslog_filters.update(a.get("filters") or {})
        syslog_on.set()
        event("syslogState", {"running": True, "udid": UDID, "filters": syslog_filters, "dropped": 0, "reconnecting": False})
        return {"running": True, "filters": syslog_filters}
    if cmd == "syslog.stop":
        syslog_on.clear()
        event("syslogState", {"running": False, "udid": UDID, "filters": syslog_filters, "dropped": 0, "reconnecting": False})
        return {"running": False}
    if cmd == "crash.list":
        return crash_items()
    if cmd == "crash.pull":
        return crash_pull(str(a.get("name") or ""))
    if cmd == "daemon.shutdown":
        send({"event": "log", "data": {"level": "info", "text": "bye"}})
        os._exit(0)
    raise ValueError(f"Unknown command {cmd}")


def serve(req: dict) -> None:
    rid = req.get("id")
    try:
        send({"id": rid, "ok": True, "data": handle(str(req.get("cmd")), req.get("args") or {})})
    except Exception as exc:  # noqa: BLE001
        send({"id": rid, "ok": False, "error": {"code": "failed", "message": "Demo mode can't do that.", "detail": str(exc)}})


def main() -> int:
    event("ready", {"version": "1", "pymobiledevice3": "demo", "python": sys.version.split()[0], "pid": os.getpid()})
    if FRESH:
        event("devices", [])
        event("connection", {"udid": None, "state": "idle", "transport": None, "message": "No iPhone selected", "detail": "",
                             "retryIn": 0, "since": time.time()})
    else:
        event("devices", [DEVICE])
        event("connection", {"udid": UDID, "state": "connected", "transport": "wifi", "message": "Connected over Wi-Fi",
                             "detail": "", "retryIn": 0, "since": time.time() - 900})
        event("deviceInfo", INFO)
    threading.Thread(target=syslog_loop, daemon=True).start()
    threading.Thread(target=mirror_loop, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if isinstance(req, dict) and "cmd" in req:
            threading.Thread(target=serve, args=(req,), daemon=True).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
