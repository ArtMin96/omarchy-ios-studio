"""Shared state of the demo device daemon (the made-up iPhone), for helper/deviced_demo.py and its
feature modules in helper/deviced_demo_features/. Stdlib only; never talks to a real device.

Demo feature modules import this (never `deviced_demo`, which runs as __main__).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_art  # noqa: E402
from device_client import runtime_dir  # noqa: E402

SCENARIO = os.environ.get("IOSSTUDIO_DEMO", "ready").lower()
FRESH = SCENARIO == "fresh"
TEAM = "A1B2C3D4E5"
UDID = "00008150-000000000000001C"
STARTED = time.time()


def _demo_dir() -> Path:
    if os.environ.get("IOSSTUDIO_RUNTIME_DIR"):   # private daemon: keep its files next to its socket
        d = runtime_dir("demo")
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d
    return demo_art.demo_dir()


DIR = _demo_dir()
BROKER = None   # device_broker.Broker in --serve mode (set by deviced_demo.serve)


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
FILE_SHARING = {"com.example.podcasts", "com.example.dailysketch"}   # App Store apps that share Documents
