"""What a project's tests need, cheaply: package facts from `swift package dump-package` (cached on disk) and the
imports of Apple-only frameworks in the code the tests build (a line scan, never cached: it is milliseconds).

    root = project_root(flags)              # HelperError "not_project"
    facts = package_facts(root)             # cached per project path + Package.swift mtime/size + swift binary
    blockers = host_blockers(root, facts)   # [{"target", "file", "line", "module"}] (capped)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from iosstudio.core import appdirs, fileio, output
from iosstudio.core.output import HelperError

DUMP_TIMEOUT = 30
CACHE_NAME = "test-packages.json"
CACHE_MAX = 200                 # projects remembered
FACTS_VERSION = 1               # bump when PackageFacts changes shape

MAX_BLOCKERS = 20
SCAN_FILES = 2000               # .swift files read per info
SCAN_FILE_BYTES = 64 * 1024     # bytes read from the start of each file
SCAN_TOTAL_BYTES = 16 * 1024 * 1024

# Frameworks that only exist on Apple platforms: code importing them can't be built for a test run on Linux.
# (Foundation, Dispatch, XCTest, Testing, Observation, RegexBuilder, Synchronization exist on Linux.)
APPLE_ONLY = frozenset("""
    UIKit SwiftUI AppKit WatchKit WidgetKit ActivityKit AppIntents Intents IntentsUI MapKit CoreLocation
    CoreLocationUI CoreData SwiftData CloudKit Combine CoreGraphics CoreImage CoreText CoreAnimation QuartzCore
    CoreML CreateML Vision VisionKit NaturalLanguage Speech SoundAnalysis CoreMotion CoreBluetooth
    CoreHaptics CoreTelephony CoreNFC CoreMedia CoreVideo CoreAudio AudioToolbox AVFoundation AVKit
    AVFAudio MediaPlayer Photos PhotosUI PhotoKit ImageIO UniformTypeIdentifiers StoreKit GameKit
    GameController SpriteKit SceneKit RealityKit ARKit Metal MetalKit MetalPerformanceShaders HealthKit
    HealthKitUI HomeKit EventKit EventKitUI Contacts ContactsUI MessageUI Messages WebKit SafariServices
    AuthenticationServices LocalAuthentication Security CryptoKit Network NetworkExtension UserNotifications
    UserNotificationsUI PassKit QuickLook QuickLookThumbnailing PDFKit Charts TipKit MultipeerConnectivity
    LinkPresentation BackgroundTasks SystemConfiguration DeviceCheck AdSupport AppTrackingTransparency
    ClockKit CarPlay CallKit PushKit FamilyControls ManagedSettings DeviceActivity os OSLog Accelerate
    ExtensionKit DeveloperToolsSupport Translation
""".split())

IMPORT = re.compile(r"^\s*(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:(?:public|package|internal|fileprivate|private)\s+)?"
                    r"import\s+(?:(?:typealias|struct|class|enum|protocol|let|var|func)\s+)?(?P<module>[A-Za-z_]\w*)")
IF_OPEN = re.compile(r"^\s*#if\b")
IF_CLOSE = re.compile(r"^\s*#endif\b")
SOURCE_DIRS = ("Sources", "Source", "src", "srcs")
SKIP_DIRS = {".build", ".git", ".swiftpm", "xtool", "node_modules", "Resources"}


def toolchain_error() -> HelperError:
    return HelperError("toolchain", "Swift for this computer isn't installed yet, so tests can't run here.",
                       output.fix("Install Swift", "setup", step="swift"))


def open_fix() -> dict:
    return output.fix("Open in editor", "projects", action="open")


# ================================================================ the project

def project_root(flags: dict) -> Path:
    """The SwiftPM package folder named by --project (or --path)."""
    raw = flags.get("project") or flags.get("path")
    if not raw or raw is True:
        raise HelperError("not_project", "Choose a project first.", output.fix("Open Projects", "projects"))
    p = Path(os.path.expanduser(str(raw))).resolve()
    if not (p / "Package.swift").is_file():
        raise HelperError("not_project", "That folder isn't a Swift package (it has no Package.swift).",
                          output.fix("Open Projects", "projects"))
    return p


def find_swift() -> str:
    """The host swift (IOSSTUDIO_SWIFT, PATH, /usr/lib/swift); HelperError "toolchain" when there is none."""
    from iosstudio.features.packages import swift
    try:
        path = swift.find_swift()
    except HelperError:
        raise toolchain_error() from None
    if not os.access(path, os.X_OK):
        raise toolchain_error()
    return path


def swift_signature(swift: str) -> str:
    """Which swift this is, without running it: its resolved path, mtime and size (an upgrade changes them)."""
    real = os.path.realpath(swift)
    try:
        st = os.stat(real)
    except OSError:
        return real
    return f"{real}:{st.st_mtime_ns}:{st.st_size}"


# ================================================================ package facts

@dataclass
class PackageFacts:
    name: str
    test_targets: list[dict] = field(default_factory=list)      # [{"name", "path"}] (path relative to the project)
    tested_targets: list[str] = field(default_factory=list)     # local non-test targets the tests depend on
    paths: dict[str, str] = field(default_factory=dict)         # target -> folder (relative), test + tested ones

    @property
    def product(self) -> str:
        return f"{self.name}PackageTests"


def parse_dump(dump: dict, root: Path) -> PackageFacts:
    """`swift package dump-package` JSON -> PackageFacts (test targets, the local targets they need, folders)."""
    targets = {t["name"]: t for t in dump.get("targets") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)}
    facts = PackageFacts(str(dump.get("name") or root.name))
    tests = [n for n, t in targets.items() if t.get("type") == "test"]
    seen: set[str] = set()
    todo = [d for n in tests for d in _local_deps(targets[n], targets)]
    while todo:
        n = todo.pop(0)
        if n in seen or targets[n].get("type") == "test":
            continue
        seen.add(n)
        todo += _local_deps(targets[n], targets)
    for n in tests:
        facts.test_targets.append({"name": n, "path": _target_path(targets[n], root)})
        facts.paths[n] = facts.test_targets[-1]["path"]
    for n in targets:          # manifest order
        if n in seen:
            facts.tested_targets.append(n)
            facts.paths[n] = _target_path(targets[n], root)
    return facts


def _local_deps(target: dict, targets: dict) -> list[str]:
    out = []
    for dep in target.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        for kind in ("byName", "target"):
            v = dep.get(kind)
            if isinstance(v, list) and v and isinstance(v[0], str) and v[0] in targets:
                out.append(v[0])
    return out


def _target_path(target: dict, root: Path) -> str:
    if isinstance(target.get("path"), str) and target["path"]:
        return target["path"].rstrip("/")
    name = target["name"]
    if target.get("type") == "test":
        return f"Tests/{name}"
    for base in SOURCE_DIRS:
        if (root / base / name).is_dir():
            return f"{base}/{name}"
    return f"Sources/{name}"


def _cache_file() -> Path:
    return appdirs.cache_dir() / CACHE_NAME


def _manifest_signature(root: Path, swift_sig: str) -> str | None:
    try:
        st = (root / "Package.swift").stat()
    except OSError:
        return None
    return f"{FACTS_VERSION}:{st.st_mtime_ns}:{st.st_size}:{swift_sig}"


def _load_cache() -> dict:
    data = fileio.load_json(_cache_file(), {})
    return data if isinstance(data, dict) else {}


def _save_cache(update) -> None:
    """Read-modify-write the cache under its lock, keeping the newest CACHE_MAX projects."""
    f = _cache_file()
    try:
        with fileio.lock(f.with_name(f".{f.name}.lock")):
            data = _load_cache()
            update(data)
            projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
            data["projects"] = dict(list(projects.items())[-CACHE_MAX:])
            fileio.save_json(f, data)
    except OSError:
        pass


def package_facts(root: Path, swift: str | None = None) -> PackageFacts:
    """Cached facts; `swift package dump-package` (about a second) only when Package.swift or swift changed."""
    swift = swift or find_swift()
    sig = _manifest_signature(root, swift_signature(swift))
    key = str(root)
    entry = (_load_cache().get("projects") or {}).get(key)
    if sig and isinstance(entry, dict) and entry.get("sig") == sig and isinstance(entry.get("facts"), dict):
        try:
            return PackageFacts(**entry["facts"])
        except TypeError:
            pass
    facts = parse_dump(_dump_package(root, swift), root)
    if sig:
        def put(data: dict) -> None:
            projects = data.setdefault("projects", {})
            if not isinstance(projects, dict):
                projects = data["projects"] = {}
            projects.pop(key, None)
            projects[key] = {"sig": sig, "facts": asdict(facts)}
        _save_cache(put)
    return facts


def _dump_package(root: Path, swift: str) -> dict:
    from iosstudio.features.packages import swift as S
    # Own scratch path: SwiftPM's lock on the default .build would make this wait for an xtool build.
    argv = ["package", "--scratch-path", str(root / ".build" / "host-tests"), "dump-package"]
    try:
        r = S.run_swift(argv, cwd=root, timeout=DUMP_TIMEOUT)
    except HelperError as exc:
        if exc.code == "missing-swift":
            raise toolchain_error() from None
        if exc.code == "timeout":
            raise HelperError("timeout", f"Reading Package.swift took longer than {DUMP_TIMEOUT} s.") from None
        raise
    if r.returncode != 0:
        raise S.manifest_error((r.stderr or r.stdout or "").splitlines())
    try:
        dump = json.loads(r.stdout)
    except ValueError:
        raise HelperError("manifest-invalid", "Swift couldn't read Package.swift.", open_fix(),
                          detail=(r.stderr or r.stdout)[-2000:]) from None
    if not isinstance(dump, dict):
        raise HelperError("manifest-invalid", "Swift couldn't read Package.swift.", open_fix())
    return dump


# ---------------------------------------------------------------- remembered build-system choice

def build_system(swift_sig: str) -> str | None:
    """"native" or "default" when a run already found out for this swift, else None (try native first)."""
    v = (_load_cache().get("build_system") or {}).get(swift_sig)
    return v if v in ("native", "default") else None


def remember_build_system(swift_sig: str, choice: str) -> None:
    def put(data: dict) -> None:
        bs = data.get("build_system") if isinstance(data.get("build_system"), dict) else {}
        bs.pop(swift_sig, None)
        bs[swift_sig] = choice
        data["build_system"] = dict(list(bs.items())[-10:])
    _save_cache(put)


# ================================================================ Apple-only imports

def host_blockers(root: Path, facts: PackageFacts, limit: int = MAX_BLOCKERS) -> list[dict]:
    """Imports of Apple-only frameworks (UIKit, SwiftUI, …) in the tested targets and the test targets, outside
    #if blocks (`#if canImport(UIKit)` guards are fine). Reads only the first SCAN_FILE_BYTES of each file."""
    out: list[dict] = []
    budget = [SCAN_FILES, SCAN_TOTAL_BYTES]
    names = facts.tested_targets + [t["name"] for t in facts.test_targets]
    for name in names:
        folder = root / facts.paths.get(name, f"Sources/{name}")
        for f in _swift_files(folder, budget):
            for line, module in scan_imports(f, budget):
                out.append({"target": name, "file": str(f), "line": line, "module": module})
                if len(out) >= limit:
                    return out
    return out


def _swift_files(folder: Path, budget: list[int]):
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP_DIRS:
                continue
            if e.is_dir(follow_symlinks=False):
                subdirs.append(Path(e.path))
            elif e.name.endswith(".swift") and e.is_file():
                if budget[0] <= 0:
                    return
                budget[0] -= 1
                yield Path(e.path)
        stack.extend(reversed(subdirs))


def scan_imports(path: Path, budget: list[int] | None = None) -> list[tuple[int, str]]:
    """(line, module) for each unguarded import of an APPLE_ONLY module in a file's first SCAN_FILE_BYTES."""
    n = SCAN_FILE_BYTES if budget is None else max(0, min(SCAN_FILE_BYTES, budget[1]))
    try:
        with open(path, "rb") as fh:
            data = fh.read(n)
    except OSError:
        return []
    if budget is not None:
        budget[1] -= len(data)
    out: list[tuple[int, str]] = []
    depth = 0
    in_comment = False
    for i, raw in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        s = raw.strip()
        if in_comment:
            if "*/" in s:
                in_comment = False
                s = s.split("*/", 1)[1].strip()
            else:
                continue
        if s.startswith("/*") and "*/" not in s:
            in_comment = True
            continue
        if not s or s.startswith("//"):
            continue
        if IF_OPEN.match(s):
            depth += 1
        elif IF_CLOSE.match(s):
            depth = max(0, depth - 1)
        elif depth == 0 and "import" in s and (m := IMPORT.match(s)) and m["module"] in APPLE_ONLY:
            out.append((i, m["module"]))
    return out
