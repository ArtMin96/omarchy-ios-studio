"""The Setup steps: each one's state, detail and action, built from the gathered facts, and their summary."""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from iosstudio.core import appdirs
from iosstudio.core.appdirs import HOME, REPO, VENV_DIR

from .facts import (
    arch,
    first_version,
    gather,
    is_omarchy,
    last_device_developer_mode,
    needed_xcode,
    parse_swift_version,
    sdk_caches,
    sdk_extras_missing,
    sdk_ios_versions,
    sdk_stamp,
    swift_hold_hook_present,
    xcode_for,
)
from .pins import APPLE_DEV_COMMIT, DESKTOP_FILE, DIRECT_XIP, DOWNLOAD_PAGE, DOWNLOADS, LAUNCHER, PYMD3_VERSION
from .runtime import fmt_bytes, probe
from .xip import best_xip


def _row(id_: str, title: str, group: str, description: str, state: str, detail: str = "", *, size: str = "",
         time_: str = "", root: bool = False, interactive: bool = False, auto: bool = True, essential: bool = False,
         action: dict | None = None, **extra) -> dict:
    """One step as `setup check` reports it."""
    if action is None:
        action = {"kind": "install", "label": "Repair" if state == "broken" else "Install"}
    d = {"id": id_, "title": title, "group": group, "description": description, "state": state, "detail": detail,
         "size": size, "time": time_, "needsRoot": root, "interactive": interactive,
         "canAutoInstall": auto and state in ("missing", "broken"), "essential": essential, "action": action}
    d.update(extra)
    return d


def build_steps(F: dict) -> tuple[list[dict], dict]:
    """The Setup steps from gather()'s facts, plus a context dict the installers use (missing packages, xip…)."""
    steps: list[dict] = []
    ctx: dict = {}

    # 1 Platform
    a = arch()
    plat_ok = a in ("x86_64", "aarch64") and shutil.which("pacman") is not None
    steps.append(_row("platform", "This computer", "System",
                   "iOS Studio needs an Arch-based Linux (like Omarchy) on an Intel/AMD or ARM 64-bit computer.",
                   "done" if plat_ok else "broken",
                   f"{'Omarchy' if is_omarchy() else 'Arch Linux'} on {a}" if plat_ok else f"Unsupported: {a}, pacman {'found' if shutil.which('pacman') else 'missing'}",
                   auto=False, essential=True, action={"kind": "none"}))

    # 2 System packages
    missing_pkgs = [l.strip() for l in F["pacman_T"][1].splitlines() if l.strip()] if F["pacman_T"][0] != 0 else []
    ctx["missing_pkgs"] = missing_pkgs
    steps.append(_row("packages", "System packages", "System",
                   "Tools for talking to an iPhone over USB (usbmuxd, libimobiledevice), building software (base-devel, git), packaging apps and saving screen recordings (ffmpeg).",
                   "missing" if missing_pkgs else "done",
                   ("Missing: " + ", ".join(missing_pkgs)) if missing_pkgs else "All installed",
                   size="about 50–150 MB", time_="under a minute", root=True, essential=True))

    # 3 usbmuxd
    unit = Path("/usr/lib/systemd/system/usbmuxd.service").exists()
    rule = Path("/usr/lib/udev/rules.d/39-usbmuxd.rules").exists()
    dropin = Path("/etc/systemd/system/usbmuxd.service.d/ios-studio-restart.conf").exists() or \
        Path("/etc/systemd/system/usbmuxd.service.d/restart.conf").exists()
    if not (unit and rule):
        st, det = "missing", "Comes with the system packages"
    elif not dropin:
        st, det = "optional", "Works. Optional: restart it automatically if it ever crashes"
    else:
        st, det = "done", "Starts by itself when an iPhone is plugged in, and restarts after a crash"
    steps.append(_row("usbmuxd", "USB connection service", "System",
                   "usbmuxd carries the connection to an iPhone plugged in with a cable. It starts on its own when you plug the phone in.",
                   st, det, size="tiny", time_="seconds", root=True,
                   auto=bool(unit and rule), action={"kind": "install", "label": "Improve" if st == "optional" else "Install"}))

    # 4 Swift
    qi_rc, qi, _ = F["swift_qi"]
    swift_pkg = re.search(r"^Version\s*:\s*(\S+)", qi, re.M).group(1) if qi_rc == 0 and re.search(r"^Version\s*:\s*(\S+)", qi, re.M) else None
    swift_ver = parse_swift_version(F["swift_ver"][1]) if F["swift_ver"][0] == 0 else None
    ctx["swift_ver"] = swift_ver
    xc = xcode_for(swift_ver)
    if not swift_pkg and not swift_ver:
        st, det = "missing", "Not installed"
    elif not swift_ver:
        st, det = "broken", f"swift-bin {swift_pkg} is installed but swift doesn't run"
    elif not xc:
        st, det = "broken", f"Swift {swift_ver} doesn't match any iOS SDK iOS Studio knows (needs Swift 6.4)"
    else:
        st, det = "done", f"Swift {swift_ver}" + (f" (swift-bin {swift_pkg})" if swift_pkg else "") + f", matches Xcode {xc[0]}"
    steps.append(_row("swift", "Swift compiler", "Toolchain",
                   "The Swift language toolchain that compiles your iPhone apps (the official swift.org build, packaged for Arch).",
                   st, det, size="about 1 GB download, 3.5 GB on disk", time_="about 5–10 minutes", root=True, essential=True))

    # 5 Hold swift-bin
    held = "swift-bin" in F["ignore"][1].split()
    hook_ok = (not is_omarchy()) or swift_hold_hook_present()
    if not swift_pkg:
        st, det = "missing", "Needs Swift first"
    elif held and hook_ok:
        st, det = "done", "System updates won't change your Swift version"
    elif held:
        st, det = "broken", "Held now, but “omarchy refresh pacman” would drop the hold"
    else:
        st, det = "missing", "A system update could replace Swift with a version the iOS SDK doesn't support"
    steps.append(_row("swift-hold", "Keep Swift on this version", "Toolchain",
                   "Swift must match the iOS SDK. This stops system updates from upgrading it behind your back.",
                   st, det, size="none", time_="seconds", root=not held, essential=False, depends=["swift"]))

    # 6 xcspec
    xs = F["xcspec"]
    if xs is None:
        st, det = "missing", "Needs Swift first"
    elif xs:
        st, det = "done", "Text resources (.strings) build correctly"
    else:
        st, det = "broken", "Swift's build settings need a small fix for .strings files (reset by every Swift update)"
    steps.append(_row("xcspec", "Swift build settings fix", "Toolchain",
                   "A small adjustment inside the Swift toolchain so localized text files in your apps build on Linux.",
                   st, det, size="none", time_="seconds", root=True, essential=True, depends=["swift"]))

    # 7 omarchy-apple-dev
    ad = F["apple_dev"]
    head = F["ad_head"][1].strip() if F["ad_head"][0] == 0 else ""
    ctx["apple_dev"], ctx["apple_dev_head"] = ad, head
    if not ad:
        st, det = "missing", "Not downloaded yet"
    elif not head:
        st, det = "done", f"Found at {appdirs.tilde(ad)}"
    elif head == APPLE_DEV_COMMIT:
        st, det = "done", f"At the tested version {head[:7]} ({appdirs.tilde(ad)})"
    else:
        st, det = "done", f"At {head[:7]}, not the tested {APPLE_DEV_COMMIT[:7]} ({appdirs.tilde(ad)})"
    if ad and F["ad_dirty"][0] == 0 and F["ad_dirty"][1].strip():
        det += " · has local changes"
    steps.append(_row("apple-dev", "iOS build scripts", "Toolchain",
                   "omarchy-apple-dev: the open-source scripts that turn Xcode's iOS SDK into something Linux can build with.",
                   st, det, size="a few MB", time_="seconds", essential=True))

    # 8 xtool
    xt = F["xtool"]
    xv_rc, xv, _ = F["xtool_ver"]
    old_ua = F["xtool_old_ua"]
    ver_line = (xv.strip().splitlines() or [""])[0]
    if not xt:
        st, det = "missing", "Not installed"
    elif xv_rc != 0:
        st, det = "broken", "Installed, but it doesn't start (often after a Swift update). Repair rebuilds it"
    elif old_ua:
        st, det = "broken", "This build can't sign in to Apple (HTTP 503). Repair rebuilds it with the fix"
    else:
        st, det = "done", (("Build " + ver_line.split()[-1][:7]) if ver_line else "Works") + " · has the Apple sign-in fix"
    ncpu = os.cpu_count() or 4
    est_min = max(5, min(25, round(7 * 16 / ncpu)))
    steps.append(_row("xtool", "xtool", "Toolchain",
                   "The tool that builds, signs and installs iPhone apps from Linux. It's compiled on your computer.",
                   st, det, size="1.8 GB while building, 410 MB installed", time_=f"about {est_min} minutes (no output while it compiles)",
                   essential=True, depends=["packages", "swift", "apple-dev"]))

    # 9 rcodesign
    rc, out, _ = F["rcodesign"]
    st = "done" if rc == 0 else "missing"
    steps.append(_row("rcodesign", "rcodesign", "Toolchain",
                   "Signs apps for TestFlight and the App Store uploads.", st,
                   f"rcodesign {first_version(out)}" if rc == 0 else "Not installed",
                   size="about 10 MB", time_="seconds"))

    # 10 pymobiledevice3
    best = None
    for path, (prc, pout, _e) in F["pymd3"]:
        lines = pout.strip().splitlines()
        if prc == 0 and lines:
            best = (path, lines[0], lines[1] if len(lines) > 1 else "")
            break
    ctx["pymd3"] = best
    if best:
        st = "done"
        det = f"pymobiledevice3 {best[1]}" + ("" if best[1] == PYMD3_VERSION else f" (tested: {PYMD3_VERSION})") + \
            ("" if best[0].startswith(str(VENV_DIR)) else f" · {appdirs.tilde(Path(best[0]).parent.parent)}")
    elif F["pymd3"]:
        st, det = "broken", "Installed, but broken (usually after a Python update). Repair recreates it"
    else:
        st, det = "missing", "Not installed"
    steps.append(_row("pymobiledevice3", "iPhone connection tools", "Toolchain",
                   "pymobiledevice3 talks to your iPhone over USB or Wi-Fi: app installs, logs, screenshots, Developer Mode.",
                   st, det, size="about 335 MB", time_="1–2 minutes", essential=True))

    # 11 ipsw (optional)
    rc, out, _ = F["ipsw"]
    steps.append(_row("ipsw", "ipsw (debugger helper)", "Toolchain",
                   "Optional. Extracts system libraries from your iPhone so the debugger can show readable stack traces.",
                   "done" if rc == 0 else "optional",
                   (f"ipsw {first_version(out) or ''}".strip()) if rc == 0 else "Not installed (optional)",
                   size="about 40 MB", time_="seconds", optional=True, action={"kind": "install", "label": "Install"}))

    # 12 python312 for lldb (optional)
    pydep = None
    m = re.search(r"(python3\d+)", qi) if qi_rc == 0 else None
    if m:
        pydep = m.group(1)
    if pydep:
        have = F["py312"][0] == 0 if pydep == "python312" else probe(["pacman", "-Q", pydep])[0] == 0
        steps.append(_row("debugger", "Debugger support", "Toolchain",
                       f"Optional. lldb, Swift's debugger, needs {pydep}. It's compiled from source, which takes a while.",
                       "done" if have else "optional", f"{pydep} installed" if have else f"{pydep} not installed (optional)",
                       size="about 100 MB", time_="10–20 minutes", root=True, pkg=pydep, optional=True,
                       action={"kind": "install", "label": "Install"}))

    # 13 Xcode .xip, 14 SDK
    stamp = F["pins"]["XTOOL_SDK_VERSION"]
    rec_ver, major, approx = needed_xcode(swift_ver)
    ctx.update(stamp=stamp, xcode=rec_ver, xcode_major=major)
    sdk_rc, sdk_out, _ = F["sdk_status"]
    bundle = None
    m = re.search(r"Path:\s*(\S.*)$", sdk_out, re.M)
    if sdk_rc == 0 and "is installed" in sdk_out and m:
        bundle = Path(m.group(1).strip())
    else:
        for c in appdirs.sdk_bundles():
            if c.is_dir():
                bundle = c
                break
    installed_stamp = sdk_stamp(bundle) if bundle else ""
    ios_vers = sdk_ios_versions(bundle) if bundle else []
    extras_missing = sdk_extras_missing(bundle) if bundle else []
    matching = [v for v in ios_vers if v.split(".")[0] == major]
    caches = sdk_caches(stamp)
    good_cache = next((c for c in caches if c["current"] and c["complete"] and c["ios"].split(".")[0] == major), None)
    ctx.update(bundle=bundle, caches=caches, good_cache=good_cache)
    if bundle and installed_stamp == stamp and matching and not extras_missing:
        sdk_state, sdk_det = "done", f"iOS {matching[-1]} SDK" + (", spare copy in the cache" if good_cache else "")
    elif bundle and installed_stamp and installed_stamp != stamp:
        sdk_state, sdk_det = "broken", "Built by an older xtool; it needs rebuilding"
    elif bundle and ios_vers and not matching:
        sdk_state, sdk_det = "broken", f"Built from Xcode {ios_vers[-1]}, but Swift {swift_ver or '?'} needs Xcode {major}"
    elif bundle and extras_missing:
        sdk_state, sdk_det = "broken", "Missing extras: " + ", ".join(extras_missing)
    else:
        sdk_state, sdk_det = "missing", "Not installed"

    xip = best_xip(major)
    ctx["xip"] = xip
    if sdk_state == "done":
        xstate = "done"
        xdet = "Not needed any more: the iOS SDK is already built" + (f" (you can delete {xip['name']} to free {fmt_bytes(xip['size'])})" if xip and xip.get("valid") and good_cache else "")
    elif good_cache:
        xstate, xdet = "done", f"Not needed: a ready-made SDK copy (iOS {good_cache['ios']}) is cached"
    elif xip and xip.get("valid"):
        v = xip.get("version") or xip.get("nameVersion")
        if xip.get("version") and xip["version"].split(".")[0] != major:
            xstate, xdet = "broken", f"{xip['name']} is Xcode {xip['version']}, but you need Xcode {rec_ver}"
        else:
            xstate = "done"
            xdet = f"{xip['name']} ({fmt_bytes(xip['size'])})" + (f", Xcode {v}" if v else "") + ("" if xip.get("version") else ", will be checked before use")
    elif xip:
        xstate, xdet = "broken", xip.get("message", "That file isn't usable")
    else:
        xstate, xdet = "missing", f"Download Xcode {rec_ver} from Apple (about {fmt_bytes(approx)})"
    steps.append(_row("xcode", f"Xcode {rec_ver} download", "Apple",
                   "The iOS SDK only comes inside Apple's Xcode download. You download it once in your browser with a free Apple Account; iOS Studio finds it in Downloads by itself.",
                   xstate, xdet, size=f"about {fmt_bytes(approx)} download", time_="depends on your connection",
                   interactive=True, auto=False, essential=sdk_state != "done",
                   action={"kind": "open-url", "label": "Open download page", "url": DIRECT_XIP.format(v=rec_ver) if "." in rec_ver else DOWNLOAD_PAGE.format(v=rec_ver),
                           "pageUrl": DOWNLOAD_PAGE.format(v=rec_ver), "watch": True},
                   xcode={"needed": rec_ver, "major": major, "swift": swift_ver, "expectedSize": approx,
                          "downloadUrl": DIRECT_XIP.format(v=rec_ver) if "." in rec_ver else None,
                          "pageUrl": DOWNLOAD_PAGE.format(v=rec_ver), "file": xip, "downloads": str(DOWNLOADS)}))
    sdk_auto = bool(good_cache or (xip and xip.get("valid")))
    steps.append(_row("sdk", "iOS SDK", "Apple",
                   "Apple's iOS frameworks (SwiftUI, UIKit…) unpacked from Xcode and prepared for Linux. Needed to build any app.",
                   sdk_state, sdk_det + ("" if sdk_state == "done" or sdk_auto else " · needs the Xcode download first"),
                   size="about 7 GB on disk (15 GB free needed while building)", time_="about 20–30 minutes",
                   auto=sdk_auto, essential=True, depends=["xtool", "xcode", "pymobiledevice3"]))

    # 15 Apple ID
    arc, aout, _ = F["auth"]
    first = (aout.strip().splitlines() or [""])[0]
    if not xt:
        st, det = "missing", "Needs xtool first"
    elif first == "Logged in.":
        aid = re.search(r"Apple ID:\s*(\S+)", aout)
        exp = re.search(r"Token expiry:\s*(.+)$", aout, re.M)
        st = "done"
        det = (f"Signed in as {aid.group(1)}" if aid else "Signed in") + (f" · token valid until {exp.group(1).strip()}" if exp else "")
    else:
        st, det = "missing", "Not signed in"
    steps.append(_row("apple-id", "Apple Account", "Apple",
                   "Sign in with your Apple Account (a free one works) so iOS Studio can sign apps for your iPhone. You'll need the 6-digit code from one of your Apple devices.",
                   st, det, time_="a minute", interactive=True, auto=False, essential=True,
                   action={"kind": "page", "label": "Sign in", "page": "account", "action": "signin"}))

    # 16 iPhone pairing
    wifi = sorted(p.name[len("remote_"):-len(".plist")] for p in (HOME / ".local/share/pymobiledevice3").glob("remote_*.plist"))
    try:
        usb = sorted(p.stem for p in Path("/var/lib/lockdown").glob("*.plist") if re.fullmatch(r"[0-9A-Fa-f-]{20,}", p.stem))
    except OSError:
        usb = []
    plugged = [l.strip() for l in F["idevice"][1].splitlines() if l.strip()] if F["idevice"][0] == 0 else []
    if usb or wifi:
        how = " and ".join(x for x in (("USB" if usb else ""), ("Wi-Fi" if wifi else "")) if x)
        st, det = "done", f"Trusted over {how}" + (" · plugged in now" if plugged else "")
    elif plugged:
        st, det = "missing", "Plugged in, but not trusted yet: unlock it and tap Trust"
    else:
        st, det = "missing", "No iPhone paired yet"
    steps.append(_row("iphone", "Pair your iPhone", "iPhone",
                   "Connect your iPhone with a cable (or over Wi-Fi), unlock it and tap Trust, so this computer can install apps on it.",
                   st, det, time_="a minute", interactive=True, auto=False, essential=True,
                   udids=sorted(set(usb) | set(wifi)), plugged=plugged,
                   action={"kind": "page", "label": "Pair iPhone", "page": "device", "action": "pair"}))

    # 17 Developer Mode (only the device daemon can tell; we read its last report)
    dm = last_device_developer_mode()
    if dm is True:
        st, det = "done", "On"
    elif dm is False:
        st, det = "missing", "Off: Settings › Privacy & Security › Developer Mode"
    else:
        st, det = "unknown", "Connect your iPhone to check"
    steps.append(_row("developer-mode", "Developer Mode", "iPhone",
                   "iPhones only run apps you build yourself with Developer Mode on: Settings › Privacy & Security › Developer Mode, then restart the phone.",
                   st, det, time_="a minute (the phone restarts)", interactive=True, auto=False, essential=False,
                   action={"kind": "page", "label": "Show me how", "page": "device", "action": "developer-mode"}))

    # 18 UxPlay (optional)
    ux = shutil.which("uxplay")
    avahi = F["avahi"][1].strip() == "active"
    if ux and avahi:
        st, det = "done", "Installed: pick this computer in Control Center › Screen Mirroring"
    elif ux:
        st, det = "broken", "Installed, but the network discovery service (avahi) isn't running"
    else:
        st, det = "optional", "Not installed (optional)"
    steps.append(_row("uxplay", "Screen mirroring (UxPlay)", "iPhone",
                   "Optional. Shows your iPhone's screen live on this computer over Wi-Fi (AirPlay), smooth and with sound.",
                   st, det, size="about 20 MB", time_="2–5 minutes", root=True, optional=True,
                   action={"kind": "install", "label": "Repair" if st == "broken" else "Install"}))

    # 18b Wireshark (optional): opens the packet captures from iPhone › Network
    ws = shutil.which("wireshark")
    steps.append(_row("wireshark", "Wireshark (packet viewer)", "iPhone",
                   "Optional. Opens the network recordings you make on the iPhone page's Network tab, packet by packet.",
                   "done" if ws else "optional", "Installed" if ws else "Not installed (optional)",
                   size="about 100 MB", time_="about a minute", root=True, optional=True,
                   action={"kind": "install", "label": "Install"}))

    # 19 App integration
    have_desktop = DESKTOP_FILE.exists()
    have_launcher = LAUNCHER.exists()
    if have_desktop and have_launcher:
        st, det = "done", "In the app launcher"
    else:
        st, det = "missing", "Not in the app launcher yet" if not have_desktop else "The ios-studio command is missing"
    steps.append(_row("app-integration", "App launcher entry", "System",
                   "Adds iOS Studio to the app launcher with its icon, so you can open it like any other app.",
                   st, det, size="tiny", time_="seconds", auto=(REPO / "install.sh").exists()))

    return steps, ctx


def summarize(steps: list[dict]) -> dict:
    counted = [s for s in steps if not s.get("optional") and s["state"] != "optional" and s["id"] != "platform"]
    done_n = sum(1 for s in counted if s["state"] == "done")
    essential_missing = [s["id"] for s in steps if s.get("essential") and s["state"] in ("missing", "broken")]
    broken = [s["id"] for s in steps if s["state"] == "broken"]
    auto = [s["id"] for s in steps if s["canAutoInstall"] and s["state"] in ("missing", "broken")]
    ready = not essential_missing
    if ready:
        level, title = "ok", "Everything is ready"
    elif broken and any(b in essential_missing for b in broken):
        level, title = "error", "Something needs repair"
    else:
        level, title = "warn", f"{len(essential_missing)} step{'s' if len(essential_missing) != 1 else ''} left"
    return {"total": len(counted), "done": done_n, "readiness": f"{done_n} of {len(counted)} ready",
            "ready": ready, "essentialMissing": essential_missing, "broken": broken, "autoInstallable": auto,
            "level": level, "title": title, "badge": "" if ready else str(len(essential_missing))}


def check() -> dict:
    """`setup check`'s data: every step with its state."""
    t0 = time.time()
    steps, _ctx = build_steps(gather())
    return {"steps": steps, "summary": summarize(steps), "xcode": next(s["xcode"] for s in steps if s["id"] == "xcode"),
            "groups": ["System", "Toolchain", "Apple", "iPhone"], "elapsed": round(time.time() - t0, 2)}


class Ctx:
    """What an install run knows: its flags, the facts, the steps (by id) and build_steps' context dict `c`."""

    def __init__(self, flags: dict):
        self.flags = flags
        self.F = gather()
        self.steps, self.c = build_steps(self.F)
        self.by_id = {s["id"]: s for s in self.steps}

    def refresh(self) -> None:
        self.F = gather()
        self.steps, self.c = build_steps(self.F)
        self.by_id = {s["id"]: s for s in self.steps}

    def state(self, sid: str) -> str:
        return self.by_id.get(sid, {}).get("state", "missing")

