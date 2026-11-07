"""Checks an xtool project for config mistakes before it is built (stdlib only; never writes a file).

xtool ignores misspelled xtool.yml keys without a word, and a missing NSCameraUsageDescription only shows up
as a crash the first time the app opens the camera. These checks take well under a second and catch such
mistakes first:

  xtool-keys          unknown or misspelled xtool.yml keys (closest valid key suggested), files it points to
  usage-descriptions  NS*UsageDescription missing from Info.plist while the sources use that feature
  icon                iconPath: exists, is a PNG, 1024×1024, no alpha
  entitlements        entitlements a free Apple ID can't get (xtool drops them when signing)
  bundle-id           the bundle id is reverse-DNS with allowed characters
  version             CFBundleShortVersionString / CFBundleVersion format (when Info.plist sets them)

An issue has the shape of a build diagnostic (helper/iosstudio/features/diagnostics.py) with category 'config':
{severity: warning|note, category, check, message, file (relative to the project) | None, line | None, hint}.
Warnings only: nothing here ever blocks a build.

    result = check(Path("/path/to/App"), deadline=time.monotonic() + 0.8)
"""
from __future__ import annotations

import difflib
import os
import plistlib
import re
import struct
import time
from pathlib import Path

CHECKS = ["xtool-keys", "usage-descriptions", "icon", "entitlements", "bundle-id", "version"]
DEFAULT_CACHE = Path.home() / ".cache/omarchy-apple-dev"

# xtool's PackSchemaBase (Sources/PackLib/PackSchema.swift at the pinned commit f300489), used when the
# cached xtool source isn't there. The cache wins when present, so a newer xtool's keys are picked up.
BUILTIN_KEYS = ["version", "orgID", "bundleID", "product", "infoPath", "entitlementsPath", "iconPath",
                "resources", "extensions", "skipLSP"]
BUILTIN_EXTENSION_KEYS = ["product", "bundleID", "infoPath", "resources", "entitlementsPath"]

MAX_SOURCE_FILES = 3000
MAX_SOURCE_BYTES = 1_000_000          # bigger .swift files are generated; skip them

# Privacy strings: (keys that satisfy it, the first is the one to add), what it is, source patterns.
# Only APIs that really make iOS ask for permission: PhotosPicker, PHPickerViewController, CNContactPicker and
# CMMotionManager (raw accelerometer) need no usage description, so they aren't listed.
USAGE: list[tuple[tuple[str, ...], str, list[str]]] = [
    (("NSCameraUsageDescription",), "the camera",
     [r"\bAVCapture(Session|Device|PhotoOutput|VideoDataOutput|MovieFileOutput|VideoPreviewLayer|DeviceInput)\b",
      r"sourceType\s*=\s*\.camera\b", r"\bDataScannerViewController\b", r"\bVNDocumentCameraViewController\b"]),
    (("NSMicrophoneUsageDescription",), "the microphone",
     [r"\bAVAudioRecorder\b", r"\brequestRecordPermission\b", r"\.inputNode\b",
      r"AVCaptureDevice\.default\(\s*for:\s*\.audio\b", r"setCategory\(\s*\.(playAndRecord|record)\b"]),
    (("NSLocationWhenInUseUsageDescription",), "location",
     [r"\bCLLocationManager\b", r"\bCLLocationUpdate\b", r"\bCLMonitor\b"]),
    (("NSLocationAlwaysAndWhenInUseUsageDescription",), "location in the background",
     [r"\brequestAlwaysAuthorization\b"]),
    (("NSPhotoLibraryUsageDescription",), "the photo library",
     [r"\bPHAsset(Collection)?\.fetch", r"\bPH(Caching)?ImageManager\b",
      r"\bPHPhotoLibrary\.requestAuthorization\(\s*(for:\s*\.readWrite)?\s*[){]",
      r"\bPHPhotoLibrary\.authorizationStatus\(\s*(for:\s*\.readWrite)?\s*\)"]),
    (("NSPhotoLibraryAddUsageDescription", "NSPhotoLibraryUsageDescription"), "saving to the photo library",
     [r"\bUIImageWriteToSavedPhotosAlbum\b", r"\bUISaveVideoAtPathToSavedPhotosAlbum\b",
      r"\bPHAssetCreationRequest\b", r"\bPHAssetChangeRequest\.creationRequest",
      r"\.requestAuthorization\(\s*for:\s*\.addOnly\b"]),
    (("NSContactsUsageDescription",), "contacts", [r"\bCNContactStore\b"]),
    (("NSCalendarsFullAccessUsageDescription", "NSCalendarsWriteOnlyAccessUsageDescription",
      "NSCalendarsUsageDescription"), "the calendar",
     [r"\brequest(Full|WriteOnly)AccessToEvents\b", r"requestAccess\(\s*to:\s*\.event\b", r"\bEKEvent\(\s*eventStore:"]),
    (("NSRemindersFullAccessUsageDescription", "NSRemindersUsageDescription"), "reminders",
     [r"\brequestFullAccessToReminders\b", r"requestAccess\(\s*to:\s*\.reminder\b", r"\bEKReminder\(\s*eventStore:"]),
    (("NSBluetoothAlwaysUsageDescription",), "Bluetooth", [r"\bCB(Central|Peripheral)Manager\b"]),
    (("NSHealthShareUsageDescription",), "Health data", [r"\bHKHealthStore\b"]),
    (("NSHealthUpdateUsageDescription",), "writing Health data", [r"toShare:\s*(\[[^\]\s]|[A-Za-z_(])"]),
    (("NSSpeechRecognitionUsageDescription",), "speech recognition", [r"\bSFSpeechRecognizer\b"]),
    (("NSFaceIDUsageDescription",), "Face ID", [r"\bLAContext\b"]),
    (("NFCReaderUsageDescription",), "NFC", [r"\bNFC(NDEF|Tag|VAS)?ReaderSession\b"]),
    (("NSMotionUsageDescription",), "motion and fitness data", [r"\bCMMotionActivityManager\b", r"\bCMPedometer\b"]),
    (("NSUserTrackingUsageDescription",), "tracking (App Tracking Transparency)", [r"\bATTrackingManager\b"]),
    (("NSAppleMusicUsageDescription",), "the music library",
     [r"\bMPMediaLibrary\b", r"\bMPMediaQuery\b", r"\bMusicAuthorization\b"]),
    (("NSLocalNetworkUsageDescription",), "the local network",
     [r"\bNWBrowser\b", r"\bNetServiceBrowser\b", r"\bMCNearbyService(Browser|Advertiser)\b"]),
    (("NSHomeKitUsageDescription",), "HomeKit", [r"\bHMHomeManager\b"]),
    (("NSSiriUsageDescription",), "Siri", [r"\brequestSiriAuthorization\b"]),
]


def _needle(pattern: str) -> str:
    """The literal word a pattern starts with (`\\bAVCapture(…)` -> AVCapture): a plain `in` test on it skips
    the regex for files that can't match, which keeps the scan of a big project well under the time budget."""
    m = re.match(r"(?:\\[b.])*([A-Za-z]+)", pattern)
    return m.group(1) if m else ""


_USAGE_RX = [(keys, what, [(_needle(p), re.compile(p)) for p in pats]) for keys, what, pats in USAGE]

# Entitlements a free (personal team) Apple ID can't get. xtool marks the first group isFree: false
# (XKit DeveloperServicesCapability.swift) and leaves them out when signing for a free team; Apple's portal
# doesn't offer the rest (iCloud, Apple Pay, Sign in with Apple, Wallet) to personal teams at all.
PAID_ENTITLEMENTS: list[tuple[str, list[str]]] = [
    ("push notifications", ["aps-environment"]),
    ("associated domains (universal links)", ["com.apple.developer.associated-domains"]),
    ("app groups", ["com.apple.security.application-groups"]),
    ("iCloud", ["com.apple.developer.icloud-container-identifiers", "com.apple.developer.icloud-services",
                "com.apple.developer.icloud-container-environment", "com.apple.developer.ubiquity-container-identifiers",
                "com.apple.developer.ubiquity-kvstore-identifier"]),
    ("Apple Pay", ["com.apple.developer.in-app-payments"]),
    ("Sign in with Apple", ["com.apple.developer.applesignin"]),
    ("Wallet passes", ["com.apple.developer.pass-type-identifiers"]),
    ("network extensions", ["com.apple.developer.networking.networkextension"]),
    ("personal VPN", ["com.apple.developer.networking.vpn.api", "com.apple.networking.vpn.configuration"]),
    ("multipath networking", ["com.apple.developer.networking.multipath"]),
    ("SiriKit", ["com.apple.developer.siri"]),
    ("Wi-Fi Aware", ["com.apple.developer.wifi-aware"]),
]

PNG_SIG = b"\x89PNG\r\n\x1a\n"
VERSION_RX = re.compile(r"^\d+(\.\d+){0,2}$")
BUNDLE_RX = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")

HINTS = {
    "unknown-key": "xtool ignores keys it doesn't know without a word, so this setting does nothing: fix the "
                   "spelling (keys are case-sensitive).",
    "missing-file": "Fix the path in xtool.yml (it is relative to the project folder) or add the file.",
    "plist": "Fix the file so it is a valid property list with a dictionary at the top (plutil -lint shows where).",
    "id": "Add bundleID: com.yourname.AppName (or orgID: com.yourname) to xtool.yml.",
    "schema": "Set version: 1 at the top of xtool.yml.",
    "usage": "Add {key} to Info.plist with one sentence saying why the app needs {what}; without it iOS ends the "
             "app the first time it asks.",
    "usage-noplist": "Add an Info.plist with {key} (one sentence saying why the app needs {what}) and point "
                     "infoPath in xtool.yml at it; without it iOS ends the app the first time it asks.",
    "usage-empty": "Write one sentence saying why the app needs {what}; iOS shows it in the permission prompt.",
    "icon-missing": "Point iconPath in xtool.yml at a 1024×1024 PNG, or add the file.",
    "icon-format": "Export the icon as a PNG: xtool only takes .png files.",
    "icon-size": "Resize the icon to exactly 1024×1024 pixels.",
    "icon-alpha": "Save the icon without transparency (flatten it onto a solid background): iOS fills "
                  "transparent parts with black.",
    "icon-none": "Add a 1024×1024 PNG without transparency and set iconPath in xtool.yml; until then the "
                 "iPhone shows a blank icon.",
    "entitlement": "A free Apple ID can't get this, so xtool leaves it out when signing and the feature won't "
                   "work on the iPhone. Remove it from the entitlements file, or sign with a paid Apple Developer "
                   "account. (Only applies to free accounts.)",
    "bundle-id": "Use reverse-DNS like com.yourname.AppName: letters, numbers, dashes and dots only, at least "
                 "two parts, no spaces or underscores.",
    "short-version": "Use one to three numbers separated by dots, like 1.0 or 1.2.3.",
    "build-version": "Use one to three numbers separated by dots, like 1 or 1.0.7.",
    "build-var": "xtool doesn't fill in Xcode build variables like $(MARKETING_VERSION): write the value itself.",
}


# ================================================================ helpers

def _issue(check: str, message: str, hint: str, file: str | None = None, line: int | None = None,
           severity: str = "warning") -> dict:
    return {"severity": severity, "category": "config", "check": check, "message": message,
            "file": file, "line": line, "hint": hint}


class _Budget:
    def __init__(self, deadline: float | None):
        self.deadline = deadline
        self.partial = False

    def over(self) -> bool:
        if self.deadline is not None and time.monotonic() > self.deadline:
            self.partial = True
            return True
        return False


def _rel(root: Path, p: Path) -> str:
    """Relative to the project, without resolving symlinks (a symlinked source keeps its path in the project);
    ../ for files of a local package next to it."""
    try:
        return os.path.relpath(os.path.abspath(p), os.path.abspath(root))
    except ValueError:
        return str(p)


def _resolve(root: Path, value: str) -> Path:
    p = Path(os.path.expanduser(value))
    return p if p.is_absolute() else root / p


def _key_line(text: str, rx: str) -> int | None:
    m = re.search(rx, text, re.M)
    return text.count("\n", 0, m.start()) + 1 if m else None


def _plist_line(text: str, key: str) -> int | None:
    return _key_line(text, r"<key>\s*" + re.escape(key) + r"\s*</key>")


# ================================================================ xtool.yml

def _scalar(v: str) -> str:
    v = v.strip()
    if v and v[0] in "'\"":
        q = v[0]
        end = v.find(q, 1)
        return v[1:end] if end > 0 else v[1:]
    return re.sub(r"\s+#.*$", "", v).strip()


def parse_yml(text: str) -> dict:
    """The parts of xtool.yml the checks need: top-level keys (with line and scalar value) and the keys of each
    `extensions:` item. xtool.yml is small flat YAML; anything fancier than that is left alone."""
    top: list[tuple[str, int, str]] = []
    ext_items: list[list[tuple[str, int]]] = []
    in_ext = False
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#") or line.startswith("---"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:(.*)$", line)
        if m:
            top.append((m.group(1), i, _scalar(m.group(2))))
            in_ext = m.group(1) == "extensions"
            continue
        if line[:1] not in (" ", "\t", "-"):
            in_ext = False
            continue
        if in_ext:
            e = re.match(r"^\s*(-\s+)?([A-Za-z_][\w-]*)\s*:(\s|$)", line)
            if e:
                if e.group(1) or not ext_items:
                    ext_items.append([])
                ext_items[-1].append((e.group(2), i))
            elif re.match(r"^\s*-\s*$", line):
                ext_items.append([])
    return {"top": top, "values": {k: v for k, _ln, v in top}, "lines": {k: ln for k, ln, _v in top},
            "extensions": ext_items}


def _swift_struct_keys(text: str, name: str) -> list[str] | None:
    """Stored-property names (or CodingKeys raw values) declared directly in `struct <name>`."""
    m = re.search(r"\bstruct\s+" + re.escape(name) + r"\b[^{]*\{", text)
    if not m:
        return None
    depth, i, start = 1, m.end(), m.end()
    while i < len(text) and depth:
        c = text[i]
        depth += 1 if c == "{" else -1 if c == "}" else 0
        i += 1
    body, flat, d = text[start:i - 1], [], 0
    for line in body.splitlines():          # keep only the lines at the struct's own depth
        if d == 0:
            flat.append(line)
        d += line.count("{") - line.count("}")
    own = "\n".join(flat)
    names = re.findall(r"^\s*(?:public\s+|internal\s+)?(?:var|let)\s+(\w+)\s*:", own, re.M)
    ck = re.search(r"enum\s+CodingKeys\b[^{]*\{(.*?)\}", body, re.S) if re.search(r"enum\s+CodingKeys\b", own) else None
    if ck:
        keys = []
        for case in re.findall(r"^\s*case\s+(.+?)\s*$", ck.group(1), re.M):
            for part in case.split(","):
                raw = re.match(r'\s*(\w+)\s*(?:=\s*"([^"]+)")?', part)
                if raw:
                    keys.append(raw.group(2) or raw.group(1))
        return keys or names
    return names


def valid_keys(cache_dir: Path | None = None) -> tuple[list[str], list[str], str]:
    """(top-level keys, extension keys, 'xtool-cache' | 'builtin') from the newest cached xtool source."""
    root = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE
    try:
        cands = []
        for d in root.glob("xtool-*"):
            for f in (d / "Sources/PackLib/PackSchema.swift", d / "Sources/PackLib/Planner.swift"):
                if f.is_file():
                    cands.append(f)
        cands.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        for f in cands:
            text = f.read_text(errors="replace")
            top = _swift_struct_keys(text, "PackSchemaBase")
            if top and "version" in top:
                ext = _swift_struct_keys(text, "Extension") or BUILTIN_EXTENSION_KEYS
                return top, ext, "xtool-cache"
    except OSError:
        pass
    return list(BUILTIN_KEYS), list(BUILTIN_EXTENSION_KEYS), "builtin"


def suggest(key: str, valid: list[str]) -> str | None:
    low = {v.lower(): v for v in valid}
    if key.lower() in low:
        return low[key.lower()]
    norm = {re.sub(r"[^a-z0-9]", "", v.lower()): v for v in valid}
    k = re.sub(r"[^a-z0-9]", "", key.lower())
    if k in norm:
        return norm[k]
    for v in valid:                           # bundleIdentifier -> bundleID, iconFile -> iconPath
        vl = v.lower()
        base = vl[:-2] if vl.endswith("id") else vl[:-4] if vl.endswith("path") else ""
        if len(base) >= 3 and k.startswith(base) and k != base and k[len(base):] in (
                "identifier", "ident", "name", "file", "filename", "dir", "url", "location", "ids"):
            return v
    m = difflib.get_close_matches(key.lower(), list(low), n=1, cutoff=0.6)
    return low[m[0]] if m else None


def _check_keys(root: Path, yml: dict, top_keys: list[str], ext_keys: list[str]) -> list[dict]:
    out: list[dict] = []
    for key, line, _v in yml["top"]:
        if key not in top_keys:
            s = suggest(key, top_keys)
            out.append(_issue("xtool-keys", f"xtool.yml has an unknown key '{key}'" +
                              (f": did you mean '{s}'?" if s else "."),
                              (f"Rename it to '{s}'. " if s else "") + HINTS["unknown-key"], "xtool.yml", line))
    for item in yml["extensions"]:
        for key, line in item:
            if key not in ext_keys:
                s = suggest(key, ext_keys)
                out.append(_issue("xtool-keys", f"xtool.yml has an unknown key '{key}' in extensions" +
                                  (f": did you mean '{s}'?" if s else "."),
                                  (f"Rename it to '{s}'. " if s else "") + HINTS["unknown-key"], "xtool.yml", line))
    vals, lines = yml["values"], yml["lines"]
    if "version" not in vals:
        out.append(_issue("xtool-keys", "xtool.yml has no 'version' key, so xtool refuses it.", HINTS["schema"],
                          "xtool.yml", 1))
    elif vals["version"] not in ("1", ""):
        out.append(_issue("xtool-keys", f"xtool.yml says version: {vals['version']}; xtool only reads version 1.",
                          HINTS["schema"], "xtool.yml", lines["version"]))
    typo_id = any(f"did you mean '{k}'" in i["message"] for i in out for k in ("bundleID", "orgID"))
    if not vals.get("bundleID") and not vals.get("orgID") and not typo_id:
        out.append(_issue("xtool-keys", "xtool.yml sets neither bundleID nor orgID, so xtool refuses it.",
                          HINTS["id"], "xtool.yml", 1))
    for key in ("infoPath", "entitlementsPath"):       # iconPath has its own check
        v = vals.get(key)
        if v and not _resolve(root, v).is_file():
            out.append(_issue("xtool-keys", f"{key} points to {v}, which doesn't exist.", HINTS["missing-file"],
                              "xtool.yml", lines.get(key)))
    return out


# ================================================================ Info.plist / sources

def _load_plist(root: Path, rel: str | None) -> tuple[dict | None, str, str | None]:
    """(dict or None, raw text for line lookups, error message)."""
    if not rel:
        return None, "", None
    p = _resolve(root, rel)
    try:
        data = p.read_bytes()
    except OSError:
        return None, "", None                 # reported by the xtool-keys check
    text = data.decode("utf-8", "replace") if not data.startswith(b"bplist") else ""
    try:
        obj = plistlib.loads(data)
    except Exception:  # noqa: BLE001  any parse error
        return None, text, "isn't a valid property list"
    if not isinstance(obj, dict):
        return None, text, "doesn't have a dictionary at the top"
    return obj, text, None


def source_dirs(root: Path) -> list[Path]:
    """Where the app's Swift code lives: Sources/, target `path:`s in Package.swift, and the Sources/ of local
    `.package(path:)` dependencies (their code ends up in the app too)."""
    dirs = [root / "Sources"]
    try:
        manifest = (root / "Package.swift").read_text(errors="replace")
    except OSError:
        manifest = ""
    manifest = re.sub(r"//[^\n]*", "", manifest)
    for m in re.finditer(r'(\.package\s*\(\s*)?path:\s*"([^"]+)"', manifest):
        d = _resolve(root, m.group(2))
        dirs.append(d / "Sources" if m.group(1) else d)
    out, seen = [], set()
    for d in dirs:
        try:
            real = d.resolve()
        except OSError:
            continue
        if real not in seen and d.is_dir():
            seen.add(real)
            out.append(d)
    return out


def _source_files(root: Path, budget: _Budget):
    n = 0
    seen: set[str] = set()
    for src in source_dirs(root):
        for dirpath, dirnames, filenames in os.walk(src, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in seen:                       # a symlink loop or a folder reached twice
                dirnames[:] = []
                continue
            seen.add(real)
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in ("xtool", "build"))
            for f in sorted(filenames):
                if not f.endswith(".swift"):
                    continue
                n += 1
                if n > MAX_SOURCE_FILES or budget.over():
                    return
                yield Path(dirpath) / f


def _code_match(pats: list[tuple[str, re.Pattern]], text: str) -> int | None:
    """Offset of the first match (of any pattern) outside a // or /* comment line."""
    best = None
    for needle, rx in pats:
        if needle not in text:
            continue
        pos = _first_code(rx, text)
        if pos is not None and (best is None or pos < best):
            best = pos
    return best


def _first_code(rx: re.Pattern, text: str) -> int | None:
    for m in rx.finditer(text):
        ls = text.rfind("\n", 0, m.start()) + 1
        head = text[ls:m.start()]
        stripped = head.lstrip()
        if "//" in head or stripped.startswith(("*", "/*")):
            continue
        return m.start()
    return None


def _check_usage(root: Path, info: dict | None, info_text: str, info_rel: str | None,
                 budget: _Budget) -> list[dict]:
    have = info or {}
    todo = [(keys, what, rx) for keys, what, rx in _USAGE_RX if not any(k in have for k in keys)]
    found: dict[str, tuple[str, int]] = {}
    for f in _source_files(root, budget) if todo else ():
        try:
            if f.stat().st_size > MAX_SOURCE_BYTES:
                continue
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for keys, _what, rx in todo:
            if keys[0] in found:
                continue
            pos = _code_match(rx, text)
            if pos is not None:
                found[keys[0]] = (_rel(root, f), text.count("\n", 0, pos) + 1)
        if len(found) == len(todo):
            break
    out = []
    for keys, what, _rx in todo:
        if keys[0] not in found:
            continue
        file, line = found[keys[0]]
        hint = HINTS["usage" if info_rel else "usage-noplist"].format(key=keys[0], what=what)
        where = "Info.plist has no" if info_rel else "the project has no Info.plist with"
        out.append(_issue("usage-descriptions",
                          f"The code uses {what} ({Path(file).name}:{line}) but {where} {keys[0]}, so the "
                          f"app crashes when it asks for access.", hint, file, line))
    for keys, what, _rx in _USAGE_RX:            # present but empty
        for k in keys:
            v = have.get(k)
            if k in have and (not isinstance(v, str) or not v.strip()):
                out.append(_issue("usage-descriptions", f"{k} in Info.plist is empty.",
                                  HINTS["usage-empty"].format(what=what), info_rel, _plist_line(info_text, k)))
    return out


# ================================================================ icon

def png_info(path: Path) -> dict:
    """{png, width, height, alpha} read from the header chunks (IHDR color type 4/6, or a tRNS chunk)."""
    with open(path, "rb") as fh:
        if fh.read(8) != PNG_SIG:
            return {"png": False}
        info = {"png": True, "width": 0, "height": 0, "alpha": False}
        for _ in range(64):
            head = fh.read(8)
            if len(head) < 8:
                break
            length, ctype = struct.unpack(">I4s", head)
            if ctype == b"IHDR":
                body = fh.read(13)
                w, h, _depth, color = struct.unpack(">IIBB", body[:10])
                info.update(width=w, height=h, alpha=color in (4, 6))
                fh.seek(length - 13 + 4, 1)
                continue
            if ctype == b"tRNS":
                info["alpha"] = True
            if ctype in (b"IDAT", b"IEND"):
                break
            fh.seek(length + 4, 1)
        return info


def _check_icon(root: Path, yml: dict) -> list[dict]:
    rel = yml["values"].get("iconPath")
    line = yml["lines"].get("iconPath")
    if not rel:
        return [_issue("icon", "The app has no icon (xtool.yml has no iconPath).", HINTS["icon-none"],
                       "xtool.yml", None, severity="note")]
    p = _resolve(root, rel)
    if not p.is_file():
        return [_issue("icon", f"iconPath points to {rel}, which doesn't exist.", HINTS["icon-missing"],
                       "xtool.yml", line)]
    try:
        info = png_info(p)
    except (OSError, struct.error):
        info = {"png": False}
    shown = _rel(root, p)
    if not info["png"] or p.suffix.lower() != ".png":
        return [_issue("icon", f"The icon {shown} isn't a PNG file.", HINTS["icon-format"], shown)]
    out = []
    if (info["width"], info["height"]) != (1024, 1024):
        out.append(_issue("icon", f"The icon is {info['width']}×{info['height']}; it should be 1024×1024.",
                          HINTS["icon-size"], shown))
    if info["alpha"]:
        out.append(_issue("icon", "The icon has transparency (an alpha channel).", HINTS["icon-alpha"], shown))
    return out


# ================================================================ entitlements

def _check_entitlements(root: Path, yml: dict) -> list[dict]:
    rels = [yml["values"].get("entitlementsPath")]
    out = []
    for rel in [r for r in rels if r]:
        ents, text, err = _load_plist(root, rel)
        shown = _rel(root, _resolve(root, rel))
        if err:
            out.append(_issue("entitlements", f"The entitlements file {shown} {err}.", HINTS["plist"], shown))
            continue
        for what, keys in PAID_ENTITLEMENTS:
            hit = next((k for k in keys if k in (ents or {})), None)
            if hit:
                out.append(_issue("entitlements", f"{hit} ({what}) needs a paid Apple Developer account.",
                                  HINTS["entitlement"], shown, _plist_line(text, hit)))
    return out


# ================================================================ bundle id / versions

def _product(root: Path, yml: dict) -> str:
    if yml["values"].get("product"):
        return yml["values"]["product"]
    try:
        libs = re.findall(r'\.library\s*\(\s*name:\s*"([^"]+)"', (root / "Package.swift").read_text(errors="replace"))
    except OSError:
        libs = []
    return libs[0] if len(libs) == 1 else root.name


def _check_bundle(root: Path, yml: dict) -> list[dict]:
    vals, lines = yml["values"], yml["lines"]
    if vals.get("bundleID"):
        bid, key = vals["bundleID"], "bundleID"
    elif vals.get("orgID"):
        bid, key = f"{vals['orgID']}.{_product(root, yml)}", "orgID"
    else:
        return []                                   # reported by xtool-keys
    if BUNDLE_RX.match(bid):
        return []
    why = ("has a space" if " " in bid else "has an underscore" if "_" in bid
           else "needs at least two parts separated by dots" if "." not in bid.strip(".")
           else "has an empty part" if ".." in bid or bid.startswith(".") or bid.endswith(".")
           else "has characters that aren't allowed")
    src = "" if key == "bundleID" else " (orgID + product name)"
    return [_issue("bundle-id", f"The bundle id {bid}{src} {why}.", HINTS["bundle-id"], "xtool.yml", lines.get(key))]


def _check_versions(info: dict | None, info_text: str, info_rel: str | None) -> list[dict]:
    out = []
    for key, hint in (("CFBundleShortVersionString", "short-version"), ("CFBundleVersion", "build-version")):
        if not info or key not in info:
            continue
        v = info[key]
        line = _plist_line(info_text, key)
        if isinstance(v, str) and "$(" in v:
            out.append(_issue("version", f"{key} is {v}, an Xcode build variable xtool leaves as it is.",
                              HINTS["build-var"], info_rel, line))
        elif not isinstance(v, str):
            out.append(_issue("version", f"{key} must be text (a <string>), not a {type(v).__name__}.",
                              HINTS[hint], info_rel, line))
        elif not VERSION_RX.match(v.strip()):
            out.append(_issue("version", f"{key} is '{v}', which isn't a valid version.", HINTS[hint], info_rel, line))
    return out


# ================================================================ entry point

def check(path: Path | str, deadline: float | None = None, cache_dir: Path | None = None) -> dict:
    """Every check on one project folder; stops early (partial=True) once `deadline` (time.monotonic()) passes.
    Returns {issues, keysSource, partial}. Never raises for a broken project; never writes."""
    root = Path(path)
    budget = _Budget(deadline)
    issues: list[dict] = []
    try:
        text = (root / "xtool.yml").read_text(errors="replace")
    except OSError:
        text = ""
    yml = parse_yml(text)
    top, ext, source = valid_keys(cache_dir)
    issues += _check_keys(root, yml, top, ext)
    info_rel = yml["values"].get("infoPath") or None
    info, info_text, info_err = _load_plist(root, info_rel)
    shown_info = _rel(root, _resolve(root, info_rel)) if info_rel else None
    if info_err:
        issues.append(_issue("xtool-keys", f"The Info.plist {shown_info} {info_err}.", HINTS["plist"], shown_info))
    steps = [
        lambda: _check_usage(root, info, info_text, shown_info, budget),
        lambda: _check_icon(root, yml),
        lambda: _check_entitlements(root, yml),
        lambda: _check_bundle(root, yml),
        lambda: _check_versions(info, info_text, shown_info),
    ]
    for step in steps:
        if budget.over():
            break
        issues += step()
    return {"issues": issues, "keysSource": source, "partial": budget.partial}


def summarize(path: Path | str, issues: list[dict], keys_source: str, elapsed_ms: int, max_issues: int = 50) -> dict:
    """The `projects check` result: issues capped at max_issues, with counts over all of them."""
    counts = {"error": 0, "warning": 0, "note": 0}
    for i in issues:
        counts[i["severity"]] = counts.get(i["severity"], 0) + 1
    shown = issues[:max(0, max_issues)]
    return {"path": str(path), "issues": shown, "total": len(issues), "shown": len(shown),
            "truncated": len(shown) < len(issues), "counts": counts, "elapsedMs": elapsed_ms,
            "keysSource": keys_source, "checks": list(CHECKS)}
