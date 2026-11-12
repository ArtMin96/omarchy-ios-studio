"""The hot reload runtime package (templates/hot-reload/): its layout, that full and stub declare the same public
API, and (with the Darwin Swift SDK installed) that both build for iOS as a dependency of a Swift 6 client, with the
loader in the debug build only (with SwiftPM's native build system and with SwiftBuild, what xtool uses).

The loader's symbol rules (full/Sources/HotReloadLoader/hr_rules.h) are compiled for this machine and checked
against names and async function pointer records of a small app and patch compiled and linked for arm64-apple-ios
the way hot reload builds them: global addressors are kept, async functions whose context grew are caught, and an
async method's vtable slot holds its `…Tu` record. What dyld, dlopen and the rewrite do on a phone isn't covered."""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from iosstudio.features.hotreload.macho import MachO

ROOT = Path(__file__).resolve().parents[2] / "templates" / "hot-reload"
FULL, STUB = ROOT / "full", ROOT / "stub"

LAYOUT = [
    "version.txt", "README.md",
    "full/Package.swift", "full/Sources/HotReloadLoader/loader.c",
    "full/Sources/HotReloadLoader/include/HotReloadLoader.h", "full/Sources/HotReloadLoader/hr_rules.h",
    "full/Sources/HotReload/HotReload.swift",
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
                   "Library/Caches", "runtime.json", '#include "hr_rules.h"', "hr_classify(",
                   "hr_async_context_grew(", "HR_MAX_LOADED"):
        assert needle in loader, needle
    assert loader.index('#include "hr_rules.h"') > gate   # the release object keeps only the constant


def test_full_and_stub_declare_the_same_public_api():
    full = public_api((FULL / "Sources/HotReload/HotReload.swift").read_text())
    stub = public_api((STUB / "Sources/HotReload/HotReload.swift").read_text())
    assert full == stub
    joined = "\n".join(full)
    for needle in ("@MainActor @propertyWrapper public struct ObserveHotReload: DynamicProperty", "public init()",
                   "public var wrappedValue: Int", "public func hotReloadable() -> AnyView", "public enum HotReload",
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


def _build_client(tmp: Path, package: Path, config: str, build_system: str = "native") -> Path:
    """Builds a Swift 6 client of `package` for iOS; returns the build folder of that configuration (native) or the
    client folder (swiftbuild)."""
    shutil.copytree(package, tmp / "hot-reload")
    client = tmp / "client"
    (client / "Sources/Client").mkdir(parents=True)
    (client / "Package.swift").write_text(CLIENT_MANIFEST)
    (client / "Sources/Client/Probe.swift").write_text(CLIENT_SOURCE)
    cmd = ["swift", "build", "-c", config, "--swift-sdks-path", str(_sdks_path()), "--swift-sdk", "arm64-apple-ios",
           "--build-system", build_system]
    env = dict(os.environ)
    if build_system == "swiftbuild":
        # How xtool builds: SwiftBuild finds the iphoneos platform only through this variable, and takes the
        # bundle's SwiftBuild toolset.
        bundle = _sdks_path() / "darwin.artifactbundle"
        env["XCODE_EXTRA_PLATFORM_FOLDERS"] = str(bundle / "Developer" / "Platforms")
        cmd += ["--toolset", str(bundle / "toolset-swb.json")]
    run = subprocess.run(cmd, cwd=client, capture_output=True, text=True, timeout=900, env=env)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    warnings = [line for line in (run.stdout + run.stderr).splitlines()
                if "warning:" in line and "deprecated" not in line]
    assert not warnings, warnings
    if build_system == "swiftbuild":
        return client
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
def test_full_release_with_swiftbuild_has_no_loader(tmp_path):
    """The build system xtool uses honors the package's `IOSSTUDIO_HOT_RELOAD` (debug-only) defines too."""
    client = _build_client(tmp_path, FULL, "release", "swiftbuild")
    objects = client / ".build" / "out" / "Intermediates.noindex" / "hot-reload.build" / "Release-iphoneos"
    loader = next(objects.glob("HotReloadLoader*.build/Objects-normal/arm64/loader.o"))
    syms = _symbols(loader)
    assert [line.split()[-1] for line in syms.splitlines() if "iosstudio" in line] == ["_iosstudio_hot_reload_loader"]
    assert "dispatch" not in syms
    swift = next(objects.glob("HotReload*.build/Objects-normal/arm64/HotReload.o"))
    assert "CFNotification" not in _symbols(swift)


@needs_sdk
def test_stub_builds(tmp_path):
    out = _build_client(tmp_path, STUB, "debug")
    assert not (out / "HotReloadLoader.build").exists()
    assert "iosstudio" not in _symbols(out / "HotReload.build" / "HotReload.swift.o")


# --- the loader's symbol rules (hr_rules.h, compiled for this machine) ---

RULES = FULL / "Sources/HotReloadLoader/hr_rules.h"

# Reads "<name> <in_text> <in_app>" lines (prints the kind) and "@ <app record hex> <patch record hex>" lines (prints
# both context sizes and whether the patch's grew).
HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include "hr_rules.h"

static void unhex(const char *s, unsigned char *out) {
    for (int i = 0; i < 8; i++) { char b[3] = {s[2 * i], s[2 * i + 1], 0}; out[i] = (unsigned char)strtoul(b, 0, 16); }
}

int main(void) {
    static const char *kinds[] = {"new", "swap", "async", "keep"};
    char name[4096], a[32], b[32];
    int t, p;
    for (;;) {
        if (scanf("%4095s", name) != 1) break;
        if (name[0] == '@') {
            unsigned char ra[8], rb[8];
            if (scanf("%31s %31s", a, b) != 2) return 1;
            unhex(a, ra);
            unhex(b, rb);
            printf("%u %u %d\n", hr_async_context_size(ra), hr_async_context_size(rb), hr_async_context_grew(ra, rb));
        } else {
            if (scanf("%d %d", &t, &p) != 2) return 1;
            printf("%s\n", kinds[hr_classify(name, t, p)]);
        }
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def rules(tmp_path_factory):
    """A function running the compiled harness: rules(lines) -> output lines."""
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler")
    tmp = tmp_path_factory.mktemp("rules")
    (tmp / "harness.c").write_text(HARNESS)
    exe = tmp / "harness"
    run = subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Wconversion", "-Werror", "-I", str(RULES.parent),
                          str(tmp / "harness.c"), "-o", str(exe)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr

    def call(lines: list[str]) -> list[str]:
        out = subprocess.run([str(exe)], input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=60,
                             check=True)
        return out.stdout.splitlines()
    return call


def test_rules_sort_swift_symbols(rules):
    cases = {   # name, in the patch's __text, the app has it -> kind
        ("_$s3Fix5useItSiyF", 1, 1): "swap",
        ("_$s3Fix5useItSiyF", 1, 0): "new",                               # only in the patch
        ("_$s3Fix7GlobalsO6sharedAA5StoreCvgZ", 1, 1): "swap",            # static getter: calls the addressor
        ("_$s3Fix5StoreC1nSivg", 1, 1): "swap",                           # instance property getter
        ("_$s3Fix5StoreC1nSivs", 1, 1): "swap",
        ("_$s3Fix7GlobalsO6sharedAA5StoreCvau", 1, 1): "keep",            # static let shared: its addressor
        ("_$s3Fix7counterSivau", 1, 1): "keep",                           # a global var
        ("_$s3Fix1SV1xSivauZ", 1, 1): "keep",                             # static marker
        ("_$s3Fix1xSivlu", 1, 1): "keep",                                 # immutable addressor
        ("_$s3Fix5StoreCMa", 1, 1): "keep",                               # metadata accessor
        ("_$s3Fix5StoreCN", 0, 1): "keep",                                # metadata (data)
        ("_$s3Fix5StoreCMn", 0, 1): "keep",
        ("_$s3Fix4loadSiyYaFTu", 0, 1): "async",                          # async function pointer
        ("_$s3Fix4loadSiyYaF", 1, 1): "swap",
        ("_$s3Fix4tickyyYaFTu", 0, 0): "new",
        ("_somefunc_vau", 1, 1): "swap",                                  # not Swift: no Swift rules
        ("_cstateMa", 1, 1): "swap",
    }
    out = rules([f"{n} {t} {a}" for n, t, a in cases])
    assert dict(zip(cases, out)) == cases


def test_rules_compare_async_context_sizes(rules):
    record = lambda rel, size: struct.pack("<iI", rel, size).hex()   # noqa: E731
    out = rules([f"@ {record(-100, 0x10)} {record(-200, 0xc0)}", f"@ {record(-100, 0x30)} {record(-8, 0x30)}",
                 f"@ {record(-100, 0x40)} {record(-8, 0x10)}"])
    assert out == ["16 192 1", "48 48 0", "64 16 0"]   # grew, same, smaller (callers allocate more: fine)


# A small module like a SwiftUI app's model: a shared instance, a global, an async function and an async method.
APP_SWIFT = """final class Store { var n = 0; init() { print("init") } }
enum Globals { static let shared = Store() }
var counter: Int = 0
func useIt() -> Int { counter += 1; return Globals.shared.n + counter }
func load() async -> Int { 42 }
func caller() async -> Int { await load() + 1 }
class K { func m() async -> Int { 1 } }
"""
APP_MAIN = "@main struct M { static func main() async { print(useIt(), await caller(), await K().m()) } }\n"
# The same file after an edit: load() and K.m() keep values across awaits (a bigger async context), caller() doesn't.
PATCH_SWIFT = """final class Store { var n = 0; init() { print("init") } }
enum Globals { static let shared = Store() }
var counter: Int = 0
func useIt() -> Int { counter += 2; return Globals.shared.n + counter }
func tick() async {}
func load() async -> Int {
    let a = [1, 2, 3]; let s = "x\\(a)"; await tick(); let t = (a, s, a, s, 1, 2, 3); await tick()
    return t.0.count + t.1.count + t.4
}
func caller() async -> Int { await load() + 1 }
class K { func m() async -> Int { let a = [1]; await tick(); return a.count + 2 } }
"""


def _ios_swiftc(tmp: Path) -> list[str]:
    bundle = _sdks_path() / "darwin.artifactbundle"
    sdk = bundle / "Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk"
    if not (bundle / "toolset/bin/ld64.lld").exists() or not sdk.is_dir():
        pytest.skip("the Darwin SDK has no iPhoneOS.sdk or ld64.lld")
    # What a debug build compiles with (-Onone -enable-testing) and what hot reload links with (-interposable).
    return ["swiftc", "-target", "arm64-apple-ios17.0", "-sdk", str(sdk),
            "-resource-dir", str(bundle / "Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift"),
            "-use-ld=lld", "-Xclang-linker", f"-B{bundle / 'toolset/bin'}", "-Onone", "-enable-testing",
            "-module-name", "Fix", "-parse-as-library", "-Xlinker", "-interposable",
            "-module-cache-path", str(tmp / "modules")]


def _swiftc(cmd: list[str], cwd: Path) -> None:
    run = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]


def _demangle(names: list[str]) -> list[str]:
    out = subprocess.run(["swift", "demangle", "--compact"], input="\n".join(n[1:] for n in names) + "\n",
                         capture_output=True, text=True, timeout=60, check=True).stdout.splitlines()
    assert len(out) == len(names)
    return out


def _records(m: MachO) -> dict[str, tuple[bytes, int]]:
    """{name: (8-byte record, the address its relative pointer reaches)} of the async function pointers."""
    out = {}
    for name, sym in m.defined().items():
        if name.endswith("Tu"):
            sect = m.section_by_index(sym.sect)
            data = m.section_data(sect)[sym.value - sect.addr:sym.value - sect.addr + 8]
            out[name] = (data, sym.value + struct.unpack("<i", data[:4])[0])
    return out


@needs_sdk
def test_rules_on_a_compiled_app_and_patch(tmp_path, rules):
    for name, text in {"app.swift": APP_SWIFT, "main.swift": APP_MAIN, "patch.swift": PATCH_SWIFT}.items():
        (tmp_path / name).write_text(text)
    swiftc = _ios_swiftc(tmp_path)
    _swiftc([*swiftc, "-c", "app.swift", "main.swift"], tmp_path)                         # app.o, main.o
    _swiftc([*swiftc, "app.o", "main.o", "-o", "app"], tmp_path)
    _swiftc([*swiftc, "-emit-library", "patch.swift", "-Xlinker", "-undefined", "-Xlinker", "dynamic_lookup",
             "-o", "patch.dylib"], tmp_path)
    app, patch = MachO.load(tmp_path / "app"), MachO.load(tmp_path / "patch.dylib")
    in_app = app.defined()
    text = patch.section("__TEXT", "__text").index

    # Every symbol the patch exports, sorted as the loader sorts it.
    exported = patch.defined()
    syms = sorted(exported.values(), key=lambda s: s.name)
    kinds = dict(zip((s.name for s in syms),
                     rules([f"{s.name} {int(s.sect == text)} {int(s.name in in_app)}" for s in syms])))
    readable = dict(zip((s.name for s in syms), _demangle([s.name for s in syms])))
    by_text = {readable[n]: k for n, k in kinds.items()}
    assert by_text["Fix.Globals.shared.unsafeMutableAddressor : Fix.Store"] == "keep"
    assert by_text["Fix.counter.unsafeMutableAddressor : Swift.Int"] == "keep"
    assert by_text["static Fix.Globals.shared.getter : Fix.Store"] == "swap"
    assert by_text["Fix.useIt() -> Swift.Int"] == "swap"
    assert by_text["Fix.load() async -> Swift.Int"] == "swap"
    assert by_text["async function pointer to Fix.load() async -> Swift.Int"] == "async"
    assert by_text["Fix.tick() async -> ()"] == "new"
    for name, kind in kinds.items():   # the rules agree with the demangler on every symbol
        if name not in in_app:
            assert kind == "new", readable[name]
        elif "unsafeMutableAddressor" in readable[name] or readable[name].startswith("type metadata accessor"):
            assert kind == "keep", readable[name]
        elif readable[name].startswith("async function pointer to"):
            assert kind == "async", readable[name]
        elif exported[name].sect == text:
            assert kind == "swap", readable[name]

    # The patch reaches the addressors only through its symbol pointers, which keep points at the app's: new code
    # gets the app's storage (the running `shared` instance, the current `counter`).
    pointers = patch.indirect_names()
    for name, kind in kinds.items():
        if "unsafeMutableAddressor" in readable[name]:
            assert name in pointers, readable[name]

    # Async function pointer records: { relative pointer to the function, context size }. load() and K.m() need
    # a bigger context in the patch, so the loader refuses the patch; caller() is unchanged.
    app_rec, patch_rec = _records(app), _records(patch)
    for recs, m in ((app_rec, app), (patch_rec, patch)):
        for name, (_data, target) in recs.items():
            assert m.defined()[name[:-2]].value == target, name   # the layout hr_rules.h reads
    common = sorted(set(app_rec) & set(patch_rec))
    sizes = dict(zip(common, rules([f"@ {app_rec[n][0].hex()} {patch_rec[n][0].hex()}" for n in common])))
    grew = {readable[n].removeprefix("async function pointer to "): s.split()[2] == "1" for n, s in sizes.items()}
    assert grew == {"Fix.load() async -> Swift.Int": True, "Fix.caller() async -> Swift.Int": False,
                    "Fix.K.m() async -> Swift.Int": True}

    # K's vtable slot for m() holds m()'s async function pointer, not the function: the vtable rewrite must
    # replace the app's `…Tu` with the patch's.
    obj = MachO.load(tmp_path / "app.o")
    data = obj.section("__DATA", "__data")
    targets = {r.symbol for r in obj.relocations(data) if r.extern and r.length == 3}   # 8-byte pointers
    method = next(n for n in kinds if readable[n] == "Fix.K.m() async -> Swift.Int")
    assert method + "Tu" in targets and method not in targets


DLERROR_HARNESS = r"""
#include <stdio.h>
#include <string.h>
%s
int main(int argc, char **argv) {
    char out[%s];
    if (argc != 4) return 2;
    hr_short_dlerror(out, sizeof out, argv[1], argv[2], argv[3]);
    printf("%%s", out);
    return 0;
}
"""


def test_loader_keeps_the_dlopen_reason(tmp_path):
    """The loader cuts dyld's long container paths and the repeated `tried:` entries, so the reason
    ("code signature invalid") fits the error cap; hr_short_dlerror is compiled for this machine."""
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler")
    loader = (FULL / "Sources/HotReloadLoader/loader.c").read_text()
    fn = loader[loader.index("static void hr_short_dlerror("):loader.index("// END hr_short_dlerror")]
    cap = re.search(r"#define HR_ERROR_MAX (\d+)", loader).group(1)
    (tmp_path / "h.c").write_text(DLERROR_HARNESS % (fn, cap))
    exe = tmp_path / "h"
    run = subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Wconversion", "-Werror", str(tmp_path / "h.c"),
                          "-o", str(exe)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr

    def short(e: str, path: str, file: str) -> str:
        return subprocess.run([str(exe), e, path, file], capture_output=True, text=True, timeout=30,
                              check=True).stdout

    d = "/var/mobile/Containers/Data/Application/0B1C2D3E-4F5A-6B7C-8D9E-0F1A2B3C4D5E/Library/Caches/HotReload"
    p = f"{d}/patch-1.dylib"
    e = (f"dlopen({p}, 0x0002): tried: '{p}' (code signature invalid in <1A2B3C4D-1234-5678-9ABC-DEF012345678> "
         f"'{p}' (errno=1) sliceOffset=0x00000000, codeBlobOffset=0x0000C450, codeBlobSize=0x00004A10), "
         f"'/System/Volumes/Preboot/Cryptexes/OS{p}' (no such file), '/private{p}' (code signature invalid in "
         f"<1A2B> '/private{p}' (errno=1))")
    assert short(e, p, "patch-1.dylib") == (
        "dlopen(patch-1.dylib, 0x0002): tried: 'patch-1.dylib' (code signature invalid in "
        "<1A2B3C4D-1234-5678-9ABC-DEF012345678> 'patch-1.dylib' (errno=1) sliceOffset=0x00000000, "
        "codeBlobOffset=0x0000C450, codeBlobSize=0x00004A10), …")
    assert short("symbol not found: _foo", p, "patch-1.dylib") == "symbol not found: _foo"
    assert short(f"dlopen({p}): tried: '{p}' (no such file)", p, "patch-1.dylib") == \
        "dlopen(patch-1.dylib): tried: 'patch-1.dylib' (no such file)"
    assert len(short("x" * 2000, p, "patch-1.dylib").encode()) < int(cap)
