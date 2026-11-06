"""Demo mode for screenshots and walkthroughs: `IOSSTUDIO_DEMO=ready|expiring|fresh`.

bin/iosstudio-helper routes every command here when IOSSTUDIO_DEMO is set. Everything personal
(Apple ID, team, iPhone, projects, builds) is made up. Setup and toolchain wording comes from the
real read-only checks, with personal strings scrubbed and states adjusted to the scenario.
Nothing writes to Hyprland, the Apple account, the iPhone or your projects.

Scenarios:
  ready     everything set up, signed in, iPhone connected, three apps (default for "1")
  expiring  like ready, but app signing expires in about 20 hours
  fresh     a new machine: tools missing, not signed in, no iPhone

Builds: HabitTracker always fails with two compiler errors. IOSSTUDIO_DEMO_FAIL=linker or =package makes
every other project's build fail too, with an undefined-symbol link error or a package version conflict;
that output goes through the real parser (diagnostics.py), so its categories and hints are the real ones.
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


# Run presets per demo project (saves, deletes and selections answer as if done but change nothing).
DEMO_PRESETS = {
    "HabitTracker": [
        {"name": "German", "language": "de", "region": "de_DE"},
        {"name": "Debug flags", "args": ["-ShowDebugMenu", "YES"], "env": {"LOG_LEVEL": "debug"}},
    ],
    "WeatherGlance": [
        {"name": "Japanese", "language": "ja", "region": "ja_JP"},
        {"name": "UI tests", "args": ["-ResetData"], "env": {"WEATHER_STUB": "sunny"}, "uiTestMode": True},
    ],
}
DEMO_SELECTED = {"HabitTracker": "German"}


def _demo_presets(name: str) -> tuple[list, str | None]:
    import run_presets
    if FRESH:
        return [], None
    presets = [run_presets.validate(run_presets.normalize(p)) for p in DEMO_PRESETS.get(name, [])]
    return presets, DEMO_SELECTED.get(name)


def _presets_cmd(flags: dict) -> int:
    import run_presets
    path = str(flags.get("path") or "")
    name = path.rstrip("/").split("/")[-1]
    if not any(p["name"] == name for p in PROJECTS):
        return common.fail("not-a-project", "That folder isn't an xtool app (it needs Package.swift and xtool.yml).")
    presets, sel = _demo_presets(name)
    try:
        presets, sel, extra, _changed = run_presets.act(presets, sel, flags)
    except common.HelperError as exc:
        return common.fail(exc.code, exc.message, exc.fix, exc.detail)
    out = run_presets.result(path, presets, sel, extra)
    return ok({**out, "demo": True} if "presets" in out else out)


def _demo_launch_preset(path: str, flags: dict) -> dict | None:
    import run_presets
    want = flags.get("preset")
    if not isinstance(want, str) or not want.strip():
        return None
    presets, _sel = _demo_presets(path.rstrip("/").split("/")[-1])
    p = run_presets.find(presets, want)
    return {"name": p["name"], **run_presets.resolve(p)}


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
        **_demo_presets_info(p["name"]), "lastBuild": last, "signing": signing, "modified": NOW - 3600 * (2 + PROJECTS.index(p) * 20), "ipa": None,
    }


def _demo_presets_info(name: str) -> dict:
    import run_presets
    presets, sel = _demo_presets(name)
    return {"runPresets": [run_presets.with_resolved(x) for x in presets], "runPreset": sel}


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
    try:   # like the real build: an unknown preset fails before anything builds
        preset = _demo_launch_preset(path, flags) if kind == "run" else None
    except common.HelperError as exc:
        return common.done(False, error=exc)
    common.event("build", project=name, bundleId=bundle, logFile=str(demo_art.demo_dir() / "build-logs" / "demo.log"), estimate=12)
    steps = [("resolve", "Resolving packages"), ("compile", "Compiling"), ("link", "Linking"), ("sign", "Signing"), ("package", "Packaging")]
    lines = ["Fetching https://github.com/example/swift-collections", "Compiling " + name + " ContentView.swift",
             "Compiling " + name + " Model.swift", "Linking " + name, "Signing with Apple Development: " + NAME, "Packaging " + name + ".ipa"]
    pre = _demo_prebuild_diags(path, name)
    if name == "HabitTracker":   # the demo project with a broken branch: fails to compile, like its history
        return _failing_build(kind, path, name, bundle, steps[:2], lines)
    fail = os.environ.get("IOSSTUDIO_DEMO_FAIL", "").strip().lower()
    if fail in DEMO_FAILURES:
        return _parsed_failure(fail, kind, path, name, bundle, steps)
    for i, (sid, title) in enumerate(steps):
        common.step(sid, title, "running", detail=title + "…")
        for j in range(3):
            common.progress((i * 3 + j + 1) / (len(steps) * 3), title + "…", max(1, 12 - (i * 3 + j) * 0.8))
            common.log(lines[min(i + j // 2, len(lines) - 1)])
            time.sleep(0.45)
        common.step(sid, title, "done")
    data = {"project": name, "name": name, "path": path, "kind": kind, "config": "debug", "result": "ok", "duration": 11.8,
            "time": NOW, "errors": 0, "warnings": len(pre), "udid": flags.get("udid"), "via": "wifi", "bundleId": bundle,
            "ipa": path + "/xtool/" + name + ".ipa", "diagnostics": pre, "installed": False}
    if kind in ("run", "renew", "install"):
        data["needsInstall"] = True
        data["launch"] = {"bundleId": bundle, "udid": flags.get("udid"), "args": preset["args"] if preset else [],
                          "env": preset["env"] if preset else {},
                          "preset": preset["name"] if preset else None} if kind == "run" else None
    data["preset"] = preset["name"] if preset else None
    return common.done(True, data)


def _config_issues(name: str) -> list[dict]:
    """Made-up `projects check` findings (same shape and wording as helper/project_check.py)."""
    import project_check as pc
    h = pc.HINTS
    return [
        pc._issue("xtool-keys", "xtool.yml has an unknown key 'bundleId': did you mean 'bundleID'?",
                  "Rename it to 'bundleID'. " + h["unknown-key"], "xtool.yml", 3),
        pc._issue("usage-descriptions", "The code uses the camera (ScannerView.swift:24) but Info.plist has no "
                  "NSCameraUsageDescription, so the app crashes when it asks for access.",
                  h["usage"].format(key="NSCameraUsageDescription", what="the camera"),
                  f"Sources/{name}/ScannerView.swift", 24),
        pc._issue("icon", "The icon has transparency (an alpha channel).", h["icon-alpha"], "Resources/AppIcon.png"),
        pc._issue("entitlements", "aps-environment (push notifications) needs a paid Apple Developer account.",
                  h["entitlement"], f"{name}.entitlements", 5),
    ]


def _demo_check(flags: dict) -> dict:
    import project_check as pc
    path = str(flags.get("path") or "")
    name = path.rstrip("/").split("/")[-1] or "App"
    try:
        cap = max(0, int(flags.get("max") or 50))
    except (TypeError, ValueError):
        cap = 50
    return pc.summarize(path, _config_issues(name), "builtin", 14, cap)


def _demo_prebuild_diags(path: str, name: str) -> list[dict]:
    """The pre-build check's warnings, shown before the build output like a real build."""
    out = []
    for i in _config_issues(name)[:2]:
        d = {k: v for k, v in i.items() if v is not None}
        if d.get("file"):
            d["file"] = path.rstrip("/") + "/" + d["file"]
        common.event("diagnostic", **d)
        out.append(d)
    return out


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


def _linker_output(path: str, name: str) -> list[str]:
    pkg = f"{path}/xtool/.xtool-tmp/Package.swift: {name}-App-product: ld64.lld"
    return ["Building for debugging...", f"[41 / 44] Compiling {name} ContentView.swift", f"[43 / 44] Linking {name}",
            f"{pkg}: warning: This version of lld does not support linking for platform iOS. Proceed at your own risk.",
            f"{pkg}: error: undefined symbol: $s{len(name)}{name}13ShareExporterC6exportyyF",
            f">>> referenced by ContentView.swift:31 ({path}/Sources/{name}/ContentView.swift:31)",
            f">>>               {path}/.build/arm64-apple-ios/debug/{name}.build/ContentView.swift.o:"
            f"($s{len(name)}{name}11ContentViewV4bodyQrvg)",
            f"{pkg}: error: undefined symbol: _OBJC_CLASS_$_CKContainer",
            f">>> referenced by CloudSync.swift ({path}/.build/arm64-apple-ios/debug/{name}.build/CloudSync.swift.o)",
            f"{path}/xtool/.xtool-tmp/Package.swift: {name}-App-product: clang: error: linker command failed "
            "with exit code 1 (use -v to see invocation)",
            "error: fatalError", "Error: exited(1)"]


def _package_output(path: str, name: str) -> list[str]:
    return ["Fetching https://github.com/apple/swift-collections.git from cache",
            "Fetched https://github.com/apple/swift-collections.git from cache (1.34s)",
            "Computing version for https://github.com/apple/swift-collections.git",
            "error: Dependencies could not be resolved because no versions of 'swift-collections' match the "
            "requirement 99.0.0..<100.0.0 and root depends on 'swift-collections' 99.0.0..<100.0.0.",
            "Error: exited(1)"]


# failure -> (output, the step it fails in, steps before it, error message)
DEMO_FAILURES = {"linker": (_linker_output, "link", "Linking failed"),
                 "package": (_package_output, "resolve", "Swift packages couldn't be resolved")}


def _parsed_failure(fail: str, kind: str, path: str, name: str, bundle: str, steps: list) -> int:
    """A failed build whose output is parsed by the real diagnostics parser (IOSSTUDIO_DEMO_FAIL)."""
    import diagnostics
    make, fail_step, lead = DEMO_FAILURES[fail]
    out = make(path, name)
    parser = diagnostics.Parser(demangler=lambda names: {})   # no swift-demangle needed (or wanted) here
    diags: list[dict] = []
    for i, (sid, title) in enumerate(steps):
        common.step(sid, title, "running", detail=title + "…")
        common.progress((i + 1) / (len(steps) + 1), title + "…", 4)
        time.sleep(0.4)
        if sid != fail_step:
            common.log(f"[{i + 1}/{len(steps)}] {title}")
            common.step(sid, title, "done")
            continue
        for line in out:
            common.log(line)
            for d in parser.feed(line):
                diags.append(d)
                common.event("diagnostic", **d)
        for d in parser.finish(failed=True):
            diags.append(d)
            common.event("diagnostic", **d)
        common.step(sid, title, "failed")
        break
    errors = [d for d in diags if d.get("severity") == "error"]
    first = errors[0]["message"] if errors else "see the log"
    msg = f"{lead}: {first}"
    build = {"project": name, "name": name, "path": path, "kind": kind, "config": "debug", "result": "failed",
             "duration": 6.3, "time": NOW, "errors": len(errors), "warnings": len(diags) - len(errors),
             "bundleId": bundle, "diagnostics": diags, "errorCode": "build-failed", "errorMessage": msg}
    common.event("done", ok=False, error={"code": "build-failed", "message": msg, "build": build})
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


def _demo_install_steps(action: str, flags: dict) -> None:
    """Step events like the real `setup install`/`setup xip --use`, so the UI never keeps a step 'running'."""
    if action == "xip":
        if flags.get("use"):
            common.step("xcode", "Checking Xcode", "running")
            common.step("xcode", "Checking Xcode", "skipped", reason="Demo mode: nothing is installed.")
        return
    try:
        steps = _setup_check({}).get("steps", [])
    except Exception:  # noqa: BLE001
        steps = []
    titles = {s.get("id"): s.get("title") or s.get("id") for s in steps}
    sid = flags.get("step")
    if flags.get("all"):
        todo = [s.get("id") for s in steps if s.get("canAutoInstall")]
    elif sid and sid is not True:
        todo = [str(sid)]
    else:
        todo = []
    for sid in todo:
        common.step(sid, titles.get(sid) or sid, "running")
        common.step(sid, titles.get(sid) or sid, "skipped", reason="Demo mode: nothing is installed.")


# ================================================================ router

def run(group: str, action: str, flags: dict) -> int:
    key = f"{group} {action}"
    if group == "agents":
        import agents
        return agents.demo(action, flags)
    if group == "debug":
        import debugger
        return debugger.demo(action, flags)
    if group == "screenshots":
        import screenshots
        return screenshots.demo(action, flags)
    if group == "editor":
        import editor_setup
        return editor_setup.demo(action, flags)
    if group == "watch":
        import watch
        return watch.demo(action, flags)
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
        import editor_setup
        return ok({**_project(match), "editor": editor_setup.demo_brief(str(flags.get("path") or ""))})
    if key == "projects check":
        return ok(_demo_check(flags))
    if key == "projects summary":
        h = sorted(_history(), key=lambda e: e["time"])
        return ok({"lastBuild": h[-1] if h else None, "recentBuilds": list(reversed(h))[:8], "expiring": []})
    if key == "projects templates":
        return ok(_real("projects", "projects", "templates", flags) or {"templates": []})
    if key == "projects build-settings":   # nothing is saved in demo mode
        empty = {"prebuild": None, "env": {}, "openFiles": None, "allowGenerated": False}
        return ok({"demo": True, "path": flags.get("path"), "settings": empty, "before": empty, "changes": [],
                   "saved": False, "dryRun": True, "suggested": None, "openFilesError": None})
    if key == "projects presets":   # answers as if saved, but nothing changes in demo mode
        return _presets_cmd(flags)
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
        _demo_install_steps(action, flags)
        return common.done(True, {"installed": [], "dryRun": True})
    if group == "toolchain" and action in ("clean", "remove-all"):
        return common.done(True, {"demo": True})
    return ok({"demo": True})
