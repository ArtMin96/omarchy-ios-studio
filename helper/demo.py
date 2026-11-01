"""Demo mode for screenshots and walkthroughs: `IOSSTUDIO_DEMO=ready|expiring|fresh`.

bin/iosstudio-helper routes every command here when IOSSTUDIO_DEMO is set. Everything personal
(Apple ID, team, iPhone, projects, builds) is made up. Setup and toolchain wording comes from the
real read-only checks, with personal strings scrubbed and states adjusted to the scenario.
Nothing writes to Hyprland, the Apple account, the iPhone or your projects.

Scenarios:
  ready     everything set up, signed in, iPhone connected, three apps (default for "1")
  expiring  like ready, but app signing expires in about 20 hours
  fresh     a new machine: tools missing, not signed in, no iPhone
"""
from __future__ import annotations

import os
import re
import time

import common
import demo_art
from common import ok

NOW = time.time()
HOME = str(common.HOME)
SCENARIO = {"1": "ready", "true": "ready", "yes": "ready"}.get(os.environ.get("IOSSTUDIO_DEMO", "").lower(),
                                                               os.environ.get("IOSSTUDIO_DEMO", "ready").lower())
FRESH = SCENARIO == "fresh"
EXPIRING = SCENARIO == "expiring"

APPLE_ID = "alex@example.com"
NAME = "Alex Rivera"
TEAM = "A1B2C3D4E5"
UDID = "00008150-000000000000001C"
DEVICE = "Alex's iPhone"
PROJ_ROOT = HOME + "/Developer"

SIGN_EXPIRES = NOW + (20 * 3600 if EXPIRING else 5.3 * 86400)


def _t(ts: float) -> dict:
    lt = time.localtime(ts)
    return {"text": time.strftime("%d/%m/%Y, %H:%M", lt), "ts": ts, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", lt)}


def scrub(obj):
    """Replace anything that could identify the real user or device."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        s = obj.replace(HOME, "~")
        s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", APPLE_ID, s)
        s = re.sub(r"\b[0-9A-F]{8}-[0-9A-F]{16}\b", UDID, s)
        s = re.sub(r"\b[0-9A-Z]{10}\b(?=[^a-z]|$)", lambda m: TEAM if re.search(r"\d", m.group(0)) and re.search(r"[A-Z]", m.group(0)) else m.group(0), s)
        return s
    return obj


def _real(mod_name: str, group: str, action: str, flags: dict) -> dict | None:
    """Run a real read-only command in-process and capture its data."""
    import contextlib
    import importlib
    import io
    import json
    buf = io.StringIO()
    try:
        mod = importlib.import_module(mod_name)
        with contextlib.redirect_stdout(buf):
            mod.GROUPS[group][action](flags)
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        return out.get("data") if out.get("ok") else None
    except Exception:
        return None


# ================================================================ projects

def _icon(name: str, hue: float, shape: str) -> str:
    return str(demo_art.icon(demo_art.demo_dir() / "icons" / f"{name}.png", hue, shape))


PROJECTS = [
    {"name": "WeatherGlance", "bundleId": "com.example.weatherglance", "hue": 0.57, "shape": "circle", "branch": "main",
     "signedDaysAgo": 1.7},
    {"name": "HabitTracker", "bundleId": "com.example.habits", "hue": 0.36, "shape": "ring", "branch": "feature/streaks",
     "signedDaysAgo": 3.2},
    {"name": "RecipeBox", "bundleId": "com.example.recipebox", "hue": 0.04, "shape": "square", "branch": "main",
     "signedDaysAgo": None},
]


def _history() -> list:
    if FRESH:
        return []
    wg, ht, rb = (HOME + "/Developer/" + p["name"] for p in PROJECTS)
    log_dir = str(demo_art.demo_dir() / "build-logs")
    return [
        {"project": "WeatherGlance", "name": "WeatherGlance", "path": wg, "kind": "run", "config": "debug", "result": "ok",
         "duration": 48.2, "time": NOW - 2.1 * 3600, "errors": 0, "warnings": 1, "logFile": log_dir + "/weather-run.log",
         "udid": UDID, "via": "wifi", "bundleId": "com.example.weatherglance", "diagnostics": [
             {"severity": "warning", "file": wg + "/Sources/WeatherGlance/ForecastView.swift", "line": 42, "col": 9,
              "message": "initialization of immutable value 'unused' was never used"}]},
        {"project": "HabitTracker", "name": "HabitTracker", "path": ht, "kind": "run", "config": "debug", "result": "failed",
         "duration": 21.7, "time": NOW - 26 * 3600, "errors": 2, "warnings": 0, "logFile": log_dir + "/habits-run.log",
         "udid": UDID, "via": "wifi", "bundleId": "com.example.habits", "errorCode": "build-failed",
         "errorMessage": "The build failed.", "diagnostics": [
             {"severity": "error", "file": ht + "/Sources/HabitTracker/StreakView.swift", "line": 18, "col": 27,
              "message": "cannot find 'streakCount' in scope"},
             {"severity": "error", "file": ht + "/Sources/HabitTracker/Habit.swift", "line": 7, "col": 5,
              "message": "type 'Habit' does not conform to protocol 'Identifiable'"}]},
        {"project": "RecipeBox", "name": "RecipeBox", "path": rb, "kind": "ipa", "config": "release", "result": "ok",
         "duration": 96.4, "time": NOW - 3.4 * 86400, "errors": 0, "warnings": 0, "logFile": log_dir + "/recipes-ipa.log",
         "bundleId": "com.example.recipebox", "ipa": rb + "/xtool/RecipeBox.ipa", "diagnostics": []},
        {"project": "WeatherGlance", "name": "WeatherGlance", "path": wg, "kind": "renew", "config": "debug", "result": "ok",
         "duration": 39.0, "time": NOW - 4.9 * 86400, "errors": 0, "warnings": 0, "logFile": log_dir + "/weather-renew.log",
         "udid": UDID, "via": "usb", "bundleId": "com.example.weatherglance", "diagnostics": []},
    ]


def _project(p: dict) -> dict:
    path = PROJ_ROOT + "/" + p["name"]
    last = next((h for h in _history() if h["path"] == path), None)
    signing = None
    if p["signedDaysAgo"] is not None and not FRESH:
        exp = SIGN_EXPIRES if p["name"] == "WeatherGlance" else NOW + (7 - p["signedDaysAgo"]) * 86400
        left = (exp - NOW) / 86400
        signing = {"udid": UDID, "bundleId": p["bundleId"], "signedAt": exp - 7 * 86400, "expires": exp, "via": "wifi",
                   "estimated": False, "daysLeft": round(left, 2), "level": "error" if left <= 0 else ("warn" if left < 2 else "ok")}
    return {
        "name": p["name"], "package": p["name"], "path": path, "displayPath": "~/Developer/" + p["name"],
        "bundleId": p["bundleId"], "product": p["name"], "icon": _icon(p["name"], p["hue"], p["shape"]),
        "iosMin": "17", "branch": p["branch"], "generated": False, "added": False, "autoRenew": p["name"] == "WeatherGlance",
        "lastBuild": last, "signing": signing, "modified": NOW - 3600 * (2 + PROJECTS.index(p) * 20), "ipa": None,
    }


# ================================================================ account

def _account_status() -> dict:
    if FRESH:
        return {"signedIn": False, "mode": None, "appleId": None, "teamId": None, "tokenExpiry": None,
                "tokenDaysLeft": None, "hasLocalSigningKey": False}
    return {"signedIn": True, "mode": "password", "appleId": APPLE_ID, "teamId": TEAM,
            "tokenExpiry": _t(NOW + 300 * 86400), "tokenDaysLeft": 300.0, "hasLocalSigningKey": True}


def _section(items: list) -> dict:
    return {"ok": True, "items": items, "seconds": 2.1, "attempts": 1, "stale": False, "fetchedAt": NOW - 120}


def _account_details() -> dict:
    team = {"name": NAME, "status": "active", "id": TEAM, "memberships": [{"name": "Xcode Free Provisioning Program", "platform": "iOS"}],
            "type": "free", "typeLabel": "Free", "active": True}
    profiles = []
    for p in PROJECTS[:2]:
        exp = SIGN_EXPIRES if p["name"] == "WeatherGlance" else NOW + (7 - p["signedDaysAgo"]) * 86400
        profiles.append({"id": "P" + p["name"][:4].upper() + "0001", "name": "XTL profile " + p["name"], "uuid": "00000000-0000-4000-8000-00000000000" + str(len(profiles) + 1),
                         "platform": "IOS", "type": "IOS_APP_DEVELOPMENT", "state": "ACTIVE", "active": True,
                         "created": _t(exp - 7 * 86400), "expires": _t(exp), "daysLeft": round((exp - NOW) / 86400, 1),
                         "appIdentifier": f"{TEAM}.XTL-{TEAM}.{p['bundleId']}", "bundleId": f"XTL-{TEAM}.{p['bundleId']}",
                         "displayBundleId": p["bundleId"], "devices": [UDID], "devicesCount": 1, "certificates": ["C0FFEE"],
                         "teamIds": [TEAM], "entitlements": ["application-identifier", "get-task-allow"]})
    profiles.sort(key=lambda x: x["expires"]["ts"])
    soon = profiles[0]
    left_h = (soon["expires"]["ts"] - NOW) / 3600
    health = {"level": "warn" if left_h < 48 else "ok",
              "title": "Signing expires soon" if left_h < 48 else "Signing is healthy",
              "detail": f"{soon['displayBundleId']} " + ("stops opening in less than a day. Renew it now." if left_h < 24 else f"keeps working for {int(left_h // 24)} days."),
              "profileId": soon["id"], "bundleId": soon["bundleId"], "expires": soon["expires"], "daysLeft": round(left_h / 24, 1)}
    certs = [{"id": "CERT000001", "name": "Apple Development: " + NAME, "displayName": NAME, "kind": "DEVELOPMENT", "kindLabel": "Development",
              "serial": "0A1B2C3D4E5F60718293A4B5C6D7E8F9", "serialShort": "0A1B…D7E8F9", "commonName": f"Apple Development: {APPLE_ID} (Z9Y8X7W6V5)",
              "teamId": TEAM, "expires": _t(NOW + 300 * 86400), "daysLeft": 300.0}]
    idents = [{"id": "ID" + p["name"][:6].upper(), "name": "XTool app " + p["name"], "bundleId": f"XTL-{TEAM}.{p['bundleId']}",
               "displayBundleId": p["bundleId"], "platform": "UNIVERSAL", "seedId": TEAM} for p in PROJECTS]
    devices = [{"id": "DEV0000001", "name": DEVICE, "udid": UDID, "udidShort": "0000…00001C", "deviceClass": "IPHONE", "model": "iPhone",
                "platform": "IOS", "status": "ENABLED", "enabled": True, "added": _t(NOW - 40 * 86400)}]
    return {
        "status": _account_status(), "errors": [], "cached": False, "empty": False, "stale": False, "fetchedAt": NOW - 120, "ageSeconds": 120,
        "sections": {"teams": _section([team]), "profiles": _section(profiles), "certificates": _section(certs),
                     "identifiers": _section(idents), "devices": _section(devices)},
        "summary": {"team": team, "teamType": "free", "free": True,
                    "limits": {"profileDays": 7, "appIdsPerWeek": 10, "devices": 3, "appsPerDevice": 3},
                    "counts": {"certificates": 1, "appIds": 3, "appIdsThisWeek": 1, "devices": 1, "profiles": len(profiles)},
                    "health": health},
    }


# ================================================================ setup / toolchain

FRESH_MISSING = {"swift", "swift-hold", "xcspec", "apple-dev", "xtool", "rcodesign", "pymobiledevice3", "ipsw", "debugger",
                 "xcode", "sdk", "apple-id", "iphone", "developer-mode", "uxplay", "wireshark"}


def _setup_check(flags: dict) -> dict:
    d = scrub(_real("setup", "setup", "check", flags) or {"steps": [], "groups": ["System", "Toolchain", "Apple", "iPhone"]})
    for s in d.get("steps", []):
        if s["id"] == "apple-id":
            s["detail"] = f"Signed in as {APPLE_ID}"
        if s["id"] == "iphone":
            s["detail"] = "Trusted over USB and Wi-Fi"
            s["udids"], s["plugged"] = [UDID], []
        if FRESH and s["id"] in FRESH_MISSING:
            s["state"] = "optional" if s["id"] in ("ipsw", "uxplay", "debugger", "wireshark") else "missing"
            s["detail"] = ""
    if FRESH:
        steps = d.get("steps", [])
        counted = [s for s in steps if s["id"] != "platform" and s["state"] != "optional"]
        miss = [s["id"] for s in steps if s.get("essential") and s["state"] == "missing"]
        d["summary"] = {**d.get("summary", {}), "done": sum(1 for s in counted if s["state"] == "done"), "total": len(counted),
                        "ready": False, "essentialMissing": miss, "level": "warn", "title": f"{len(miss)} steps left", "badge": str(len(miss))}
        if isinstance(d.get("xcode"), dict):
            d["xcode"]["file"] = None
    return d


def _toolchain_info(flags: dict) -> dict:
    d = scrub(_real("setup", "toolchain", "info", flags) or {"items": [], "sdks": {}})
    for it in d.get("items", []):
        if it.get("id") == "apple-dev" and it.get("detail"):   # a personal checkout path: show the standard one
            it["detail"] = "~/.local/share/omarchy-apple-dev"
    if FRESH:
        for it in d.get("items", []):
            if it["id"] not in ("apple-dev",):
                it["status"], it["version"], it["detail"] = "missing", None, "Not installed yet"
        d["sdks"] = {"installed": [], "caches": [], "xcodeNeeded": (d.get("sdks") or {}).get("xcodeNeeded")}
    return d


def _toolchain_disk(flags: dict) -> dict:
    d = scrub(_real("setup", "toolchain", "disk", {**flags, "cached": True}) or {"items": [], "total": 0, "reclaimable": 0, "free": 0})
    if FRESH:
        d.update(items=[], total=0, reclaimable=0)
    return d


# ================================================================ streams

def _build_stream(kind: str, flags: dict) -> int:
    path = str(flags.get("path") or "")
    name = path.rstrip("/").split("/")[-1] or "App"
    bundle = next((p["bundleId"] for p in PROJECTS if p["name"] == name), "com.example.app")
    common.event("build", project=name, bundleId=bundle, logFile=str(demo_art.demo_dir() / "build-logs" / "demo.log"), estimate=12)
    steps = [("resolve", "Resolving packages"), ("compile", "Compiling"), ("link", "Linking"), ("sign", "Signing"), ("package", "Packaging")]
    lines = ["Fetching https://github.com/example/swift-collections", "Compiling " + name + " ContentView.swift",
             "Compiling " + name + " Model.swift", "Linking " + name, "Signing with Apple Development: " + NAME, "Packaging " + name + ".ipa"]
    if name == "HabitTracker":   # the demo project with a broken branch: fails to compile, like its history
        return _failing_build(kind, path, name, bundle, steps[:2], lines)
    for i, (sid, title) in enumerate(steps):
        common.step(sid, title, "running", detail=title + "…")
        for j in range(3):
            common.progress((i * 3 + j + 1) / (len(steps) * 3), title + "…", max(1, 12 - (i * 3 + j) * 0.8))
            common.log(lines[min(i + j // 2, len(lines) - 1)])
            time.sleep(0.45)
        common.step(sid, title, "done")
    data = {"project": name, "name": name, "path": path, "kind": kind, "config": "debug", "result": "ok", "duration": 11.8,
            "time": NOW, "errors": 0, "warnings": 0, "udid": flags.get("udid"), "via": "wifi", "bundleId": bundle,
            "ipa": path + "/xtool/" + name + ".ipa", "diagnostics": [], "installed": False}
    if kind in ("run", "renew", "install"):
        data["needsInstall"] = True
        data["launch"] = {"bundleId": bundle, "udid": flags.get("udid")} if kind == "run" else None
    return common.done(True, data)


def _failing_build(kind: str, path: str, name: str, bundle: str, steps: list, lines: list) -> int:
    diags = [{"file": path + "/Sources/HabitTracker/StreakView.swift", "line": 18, "col": 27, "severity": "error",
              "message": "cannot find 'streakCount' in scope"},
             {"file": path + "/Sources/HabitTracker/Habit.swift", "line": 7, "col": 5, "severity": "error",
              "message": "type 'Habit' does not conform to protocol 'Identifiable'"}]
    for i, (sid, title) in enumerate(steps):
        common.step(sid, title, "running", detail=title + "…")
        for j in range(2):
            common.progress((i * 2 + j + 1) / 6, title + "…", 6)
            common.log(lines[min(i + j, len(lines) - 1)])
            time.sleep(0.4)
        if sid == "compile":
            for d in diags:
                common.log(f"{d['file']}:{d['line']}:{d['col']}: error: {d['message']}")
                common.event("diagnostic", **d)
            common.step(sid, title, "failed")
            break
        common.step(sid, title, "done")
    build = {"project": name, "name": name, "path": path, "kind": kind, "config": "debug", "result": "failed",
             "duration": 4.1, "time": NOW, "errors": 2, "warnings": 0, "bundleId": bundle, "diagnostics": diags,
             "errorCode": "compile-errors", "errorMessage": "The app has 2 errors in its code."}
    common.event("done", ok=False, error={"code": "compile-errors", "message": "The app has 2 errors in its code.",
                                          "build": build})
    return 1


def _fake_log(flags: dict) -> dict:
    f = str(flags.get("file") or "")
    failed = "habits" in f
    text = "\n".join([
        "$ xtool dev build --triple arm64-apple-ios",
        "Building for debugging...",
        "[1/12] Compiling Habit.swift" if failed else "[1/12] Compiling Forecast.swift",
        HOME + "/Developer/HabitTracker/Sources/HabitTracker/Habit.swift:7:5: error: type 'Habit' does not conform to protocol 'Identifiable'" if failed else "[6/12] Compiling ContentView.swift",
        HOME + "/Developer/HabitTracker/Sources/HabitTracker/StreakView.swift:18:27: error: cannot find 'streakCount' in scope" if failed else "[12/12] Linking WeatherGlance",
        "error: build failed (2 errors)" if failed else "Signed and packaged in 48.2 s",
    ])
    return {"text": text.replace(HOME, "~"), "file": f, "truncated": False}


# ================================================================ router

def run(group: str, action: str, flags: dict) -> int:
    key = f"{group} {action}"
    if group == "agents":
        import agents
        return agents.demo(action, flags)
    if group == "debug":
        import debugger
        return debugger.demo(action, flags)
    if key == "setup check":
        return ok(_setup_check(flags))
    if key == "toolchain info":
        return ok(_toolchain_info(flags))
    if key == "toolchain disk":
        return ok(_toolchain_disk(flags))
    if key == "account status":
        return ok(_account_status())
    if key == "account details":
        if FRESH:
            return common.fail("not-signed-in", "You're not signed in to your Apple ID.",
                               common.fix("Sign in", "account", action="signin"))
        return ok(_account_details())
    if key == "projects list":
        if FRESH:
            return ok({"projects": [], "convertible": [], "missing": [], "roots": [PROJ_ROOT], "defaultRoots": [PROJ_ROOT], "defaultDir": PROJ_ROOT, "xtool": False})
        return ok({"projects": [_project(p) for p in PROJECTS], "convertible": [], "missing": [], "roots": [PROJ_ROOT], "defaultRoots": [PROJ_ROOT], "defaultDir": PROJ_ROOT, "xtool": True})
    if key == "projects info":
        want = str(flags.get("path") or "").rstrip("/").split("/")[-1]
        match = next((p for p in PROJECTS if p["name"] == want), None)
        if not match:
            return common.fail("not-found", "That project folder doesn't exist anymore.")
        return ok(_project(match))
    if key == "projects summary":
        h = sorted(_history(), key=lambda e: e["time"])
        return ok({"lastBuild": h[-1] if h else None, "recentBuilds": list(reversed(h))[:8], "expiring": []})
    if key == "projects templates":
        return ok(_real("projects", "projects", "templates", flags) or {"templates": []})
    if key == "projects autorenew":
        return ok({"installed": False, "unit": "ios-studio-renew.timer", "enabled": False, "next": None, "last": None, "projects": []})
    if key == "build history":
        return ok({"builds": list(reversed(_history()))[: int(flags.get("limit") or 50)]})
    if key == "build log":
        return ok(_fake_log(flags))
    if key in ("build run", "build ipa", "build compile", "build install", "projects renew"):
        return _build_stream(action if group == "build" else "renew", flags)
    if key == "app status":
        return ok({"keybinding": {"enabled": False, "key": "SUPER + SHIFT + I", "label": "Super + Shift + I", "conflict": ""},
                   "windowRule": {"mode": "tile"}, "opaque": True,
                   "paths": {"bindings": HOME + "/.config/hypr/bindings.lua", "hyprland": HOME + "/.config/hypr/hyprland.lua"}})
    if key == "app about":
        return ok(scrub(_real("appconfig", "app", "about", flags) or {}))
    # Anything that would change something real is a harmless no-op in demo mode.
    if group == "setup" and action in ("install", "xip"):
        common.event("notice", id="demo", level="info", message="Demo mode: nothing is installed.")
        return common.done(True, {"installed": [], "dryRun": True})
    if group == "toolchain" and action in ("clean", "remove-all"):
        return common.done(True, {"demo": True})
    return ok({"demo": True})
