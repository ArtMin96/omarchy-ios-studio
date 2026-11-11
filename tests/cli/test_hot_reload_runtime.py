"""The hot reload runtime package (templates/hot-reload/): its layout, that full and stub declare the same public
API, and (with the Darwin Swift SDK installed) that both build for iOS as a dependency of a Swift 6 client, with the
loader in the debug build only."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "templates" / "hot-reload"
FULL, STUB = ROOT / "full", ROOT / "stub"

LAYOUT = [
    "version.txt", "README.md",
    "full/Package.swift", "full/Sources/HotReloadLoader/loader.c",
    "full/Sources/HotReloadLoader/include/HotReloadLoader.h", "full/Sources/HotReload/HotReload.swift",
    "stub/Package.swift", "stub/Sources/HotReload/HotReload.swift",
]


def public_api(source: str) -> list[str]:
    """The public declarations of a Swift file, normalized (one line each, without bodies)."""
    decls = []
    lines = source.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("public "):
            continue
        attrs = []   # attribute lines just above (@MainActor, @propertyWrapper, @discardableResult)
        j = i - 1
        while j >= 0 and lines[j].strip().startswith("@"):
            attrs.insert(0, lines[j].strip())
            j -= 1
        decl = re.sub(r"\s*\{.*$", "", s)   # drop a body or accessor block
        decls.append(" ".join([*attrs, decl]))
    return decls


def test_layout_and_version():
    for rel in LAYOUT:
        assert (ROOT / rel).is_file(), rel
    assert re.fullmatch(r"[1-9][0-9]*", (ROOT / "version.txt").read_text().strip())
    assert not (STUB / "Sources" / "HotReloadLoader").exists()
    for pkg in (FULL, STUB):
        manifest = (pkg / "Package.swift").read_text()
        assert manifest.startswith("// swift-tools-version: 5.9")
        assert 'name: "hot-reload"' in manifest
        assert '.library(name: "HotReload", targets: ["HotReload"])' in manifest
        assert ".iOS(.v17)" in manifest
    assert "unsafeFlags" not in (FULL / "Package.swift").read_text() + (STUB / "Package.swift").read_text()
    loader = (FULL / "Sources/HotReloadLoader/loader.c").read_text()
    gate = loader.index("#if defined(IOSSTUDIO_HOT_RELOAD)")   # everything but the exported constant is debug-only
    assert loader.index("const int iosstudio_hot_reload_loader") < gate < loader.index("__attribute__((constructor))")
    assert loader.rstrip().endswith("#endif")
    for needle in ("const int iosstudio_hot_reload_loader", "__attribute__((constructor))",
                   "DISPATCH_SOURCE_TYPE_VNODE", "VM_PROT_COPY", '"dev.iosstudio.hot-reload"', "RTLD_MAIN_ONLY",
                   "Library/Caches", "runtime.json"):
        assert needle in loader, needle


def test_full_and_stub_declare_the_same_public_api():
    full = public_api((FULL / "Sources/HotReload/HotReload.swift").read_text())
    stub = public_api((STUB / "Sources/HotReload/HotReload.swift").read_text())
    assert full == stub
    joined = "\n".join(full)
    for needle in ("@MainActor @propertyWrapper public struct ObserveHotReload: DynamicProperty", "public init()",
                   "public var wrappedValue: Int", "public func hotReloadable() -> some View", "public enum HotReload",
                   "public static var isActive: Bool", "public static var generation: Int",
                   "@discardableResult public static func onReload(_ body: @escaping @MainActor () -> Void) "
                   "-> AnyObject"):
        assert needle in joined, needle


# --- building for iOS (needs the Darwin SDK; a few minutes) ---

CLIENT_MANIFEST = """// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Client",
    platforms: [.iOS(.v17)],
    dependencies: [.package(path: "../hot-reload")],
    targets: [.target(name: "Client", dependencies: [.product(name: "HotReload", package: "hot-reload")])]
)
"""

CLIENT_SOURCE = """import SwiftUI
import HotReload

struct Probe: View {
    @ObserveHotReload var reload
    @State private var taps = 0

    var body: some View {
        Text("reload \\(reload), taps \\(taps)")
            .hotReloadable()
    }
}

@MainActor final class Controller {
    var token: AnyObject?
    func start() -> (Bool, Int) {
        token = HotReload.onReload { [weak self] in self?.reloaded() }
        return (HotReload.isActive, HotReload.generation)
    }
    func reloaded() {}
}
"""


def _sdks_path() -> Path | None:
    """The folder holding darwin.artifactbundle. Looked up in the real home, since the test run points
    XDG_CONFIG_HOME (where SwiftPM looks by default) at a temp folder."""
    for p in (Path.home() / ".config/swiftpm/swift-sdks", Path.home() / ".swiftpm/swift-sdks"):
        if (p / "darwin.artifactbundle").is_dir():
            return p
    return None


def _has_darwin_sdk() -> bool:
    sdks = _sdks_path()
    if not sdks or not shutil.which("swift"):
        return False
    try:
        out = subprocess.run(["swift", "sdk", "list", "--swift-sdks-path", str(sdks)],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "darwin" in out.split()


needs_sdk = pytest.mark.skipif(not _has_darwin_sdk(), reason="the Darwin Swift SDK (swift sdk list: darwin) is missing")


def _build_client(tmp: Path, package: Path, config: str) -> Path:
    """Builds a Swift 6 client of `package` for iOS; returns the build folder of that configuration."""
    shutil.copytree(package, tmp / "hot-reload")
    client = tmp / "client"
    (client / "Sources/Client").mkdir(parents=True)
    (client / "Package.swift").write_text(CLIENT_MANIFEST)
    (client / "Sources/Client/Probe.swift").write_text(CLIENT_SOURCE)
    # The default build system (SwiftBuild) can't find the iphoneos platform in the Darwin SDK on Linux.
    cmd = ["swift", "build", "-c", config, "--swift-sdks-path", str(_sdks_path()), "--swift-sdk", "arm64-apple-ios",
           "--build-system", "native"]
    run = subprocess.run(cmd, cwd=client, capture_output=True, text=True, timeout=900)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    warnings = [line for line in (run.stdout + run.stderr).splitlines()
                if "warning:" in line and "deprecated" not in line]
    assert not warnings, warnings
    return client / ".build" / "arm64-apple-ios" / config


def _symbols(obj: Path) -> str:
    nm = shutil.which("llvm-nm")
    if not nm:
        pytest.skip("llvm-nm is missing")
    return subprocess.run([nm, "-m", str(obj)], capture_output=True, text=True, timeout=60, check=True).stdout


@needs_sdk
def test_full_debug_builds_with_loader(tmp_path):
    out = _build_client(tmp_path, FULL, "debug")
    syms = _symbols(out / "HotReloadLoader.build" / "loader.c.o")
    assert "external [no dead strip] _iosstudio_hot_reload_loader" in syms
    assert "_iosstudio_hot_reload_init" in syms
    assert "_dispatch_source_create" in syms
    sections = subprocess.run(["llvm-objdump", "--macho", "--section-headers", str(out / "HotReloadLoader.build"
                                                                                     / "loader.c.o")],
                              capture_output=True, text=True, timeout=60).stdout
    assert "__mod_init_func" in sections


@needs_sdk
def test_full_release_has_no_loader(tmp_path):
    out = _build_client(tmp_path, FULL, "release")
    syms = _symbols(out / "HotReloadLoader.build" / "loader.c.o")
    # Only the constant the app's `-u` needs (SwiftPM passes the block's debug linker flags to release too).
    assert [line.split()[-1] for line in syms.splitlines() if "iosstudio" in line] == ["_iosstudio_hot_reload_loader"]
    assert "_iosstudio_hot_reload_init" not in syms
    assert "dispatch" not in syms
    assert "__mod_init_func" not in subprocess.run(
        ["llvm-objdump", "--macho", "--section-headers", str(out / "HotReloadLoader.build" / "loader.c.o")],
        capture_output=True, text=True, timeout=60).stdout
    assert "CFNotification" not in _symbols(out / "HotReload.build" / "HotReload.swift.o")


@needs_sdk
def test_stub_builds(tmp_path):
    out = _build_client(tmp_path, STUB, "debug")
    assert not (out / "HotReloadLoader.build").exists()
    assert "iosstudio" not in _symbols(out / "HotReload.build" / "HotReload.swift.o")
