"""Test helpers for hot reload: synthetic Mach-O files, throwaway signing identities, a fake build tree.

build_macho(...)      a small, valid arm64 Mach-O (object, dylib or executable) with the sections, symbols,
                      relocations and indirect symbols a test asks for; fat=True wraps it in a fat header
make_identity(dir)    an xtool-style data folder with a throwaway RSA key (made with openssl) in the key format
                      asked for; point IOSSTUDIO_XTOOL_DATA at it (never the owner's real identity)
msgpack_str(s), task_store(args)   msgpack bytes like SwiftBuild's task store
"""
from __future__ import annotations

import plistlib
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ARM64 = 0x0100000C
MH_OBJECT, MH_EXECUTE, MH_DYLIB = 0x1, 0x2, 0x6
N_EXT, N_SECT, N_UNDF, N_PEXT = 0x1, 0xE, 0x0, 0x10
S_NON_LAZY_SYMBOL_POINTERS, S_SYMBOL_STUBS = 0x6, 0x8
OPENSSL = shutil.which("openssl")


@dataclass
class Sect:
    seg: str
    name: str
    data: bytes = b""
    flags: int = 0
    reserved1: int = 0
    reserved2: int = 0
    relocs: list[tuple[int, int, bool, int, bool, int]] = field(default_factory=list)
    # (address, symbolnum, pcrel, length, extern, type)
    addr: int = 0


@dataclass
class Sym:
    name: str
    type: int = N_SECT | N_EXT
    sect: int = 1
    value: int = 0
    desc: int = 0


def _pad(b: bytes, n: int = 8) -> bytes:
    return b + b"\0" * (-len(b) % n)


def build_macho(sections: list[Sect], symbols: list[Sym], *, filetype: int = MH_OBJECT, indirect: list[int] = (),
                id_dylib: str | None = None, code_signature: bool = False, cputype: int = ARM64,
                fat: bool = False, fat_other: bool = True) -> bytes:
    """An object file keeps its sections in one unnamed segment; a dylib or executable gets one segment per
    segment name (in order, the first one starting at the file's start like __TEXT) and a __LINKEDIT segment
    holding the symbol, string and indirect tables, so rcodesign can sign it."""
    linked = filetype != MH_OBJECT
    groups: list[tuple[str, list[Sect]]] = []
    for sect in sections:
        name = sect.seg if linked else ""
        if not groups or groups[-1][0] != name:
            groups.append((name, []))
        groups[-1][1].append(sect)
    nseg = len(groups) + (1 if linked else 0)
    cmds_size = sum(72 + 80 * len(g) for _n, g in groups) + (72 if linked else 0) + 24 + 80
    id_cmd = b""
    if id_dylib:
        name = _pad(id_dylib.encode() + b"\0")
        id_cmd = struct.pack("<IIIIII", 0xD, 24 + len(name), 24, 0, 0x10000, 0x10000) + name
        cmds_size += len(id_cmd)
    if code_signature:
        cmds_size += 16
    ncmds = nseg + 2 + (1 if id_dylib else 0) + (1 if code_signature else 0)
    data_off = 32 + cmds_size + (256 if linked else 0)    # room for the LC_CODE_SIGNATURE a signer adds
    blob = b""
    offsets = {}
    for sect in sections:
        offsets[id(sect)] = data_off + len(blob)
        if linked:
            sect.addr = offsets[id(sect)]
        blob += _pad(sect.data)
    reloc_offs = {}
    for sect in sections:
        reloc_offs[id(sect)] = data_off + len(blob)
        for (addr, symnum, pcrel, length, extern, rtype) in sect.relocs:
            info = (symnum & 0xFFFFFF) | (int(pcrel) << 24) | (length << 25) | (int(extern) << 27) | (rtype << 28)
            blob += struct.pack("<iI", addr, info)
    if linked:
        blob += b"\0" * (-(data_off + len(blob)) % 0x4000)
    linkedit = data_off + len(blob)
    strtab = b"\0"
    nl = b""
    for sym in symbols:
        strx = len(strtab)
        strtab += sym.name.encode() + b"\0"
        value = sym.value + (sections[sym.sect - 1].addr if linked and 0 < sym.sect <= len(sections) else 0)
        nl += struct.pack("<IBBHQ", strx, sym.type, sym.sect, sym.desc, value)
    symoff = data_off + len(blob)
    blob += nl
    stroff = data_off + len(blob)
    blob += _pad(strtab)
    indoff = data_off + len(blob)
    blob += struct.pack(f"<{len(indirect)}I", *indirect)
    sigoff = data_off + len(blob)
    if code_signature:
        blob += b"\xfa\xde\x0c\xc0" + b"\0" * 12
    total = data_off + len(blob)
    cmds = b""
    for k, (segname, sects) in enumerate(groups):
        if linked:
            first = offsets[id(sects[0])] if k else 0
            last = max(offsets[id(x)] + len(x.data) for x in sects)
            fileoff, filesize = first, (linkedit if k == len(groups) - 1 else last) - first
        else:
            fileoff, filesize = 0, total
        cmds += struct.pack("<II16sQQQQiiII", 0x19, 72 + 80 * len(sects), segname.encode(), fileoff, filesize,
                            fileoff, filesize, 7, 7, len(sects), 0)
        for sect in sects:
            cmds += struct.pack("<16s16sQQIIIIIIII", sect.name.encode(), sect.seg.encode(), sect.addr, len(sect.data),
                                offsets[id(sect)], 2, reloc_offs[id(sect)] if sect.relocs else 0, len(sect.relocs),
                                sect.flags, sect.reserved1, sect.reserved2, 0)
    if linked:
        cmds += struct.pack("<II16sQQQQiiII", 0x19, 72, b"__LINKEDIT", linkedit, total - linkedit, linkedit,
                            total - linkedit, 1, 1, 0, 0)
    cmds += struct.pack("<IIIIII", 0x2, 24, symoff, len(symbols), stroff, len(strtab))
    cmds += struct.pack("<II18I", 0xB, 80, *([0] * 12), indoff, len(indirect), 0, 0, 0, 0)
    cmds += id_cmd
    if code_signature:
        cmds += struct.pack("<IIII", 0x1D, 16, sigoff, 16)
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, cputype, 0, filetype, ncmds, len(cmds), 0, 0)
    thin = header + cmds + b"\0" * (data_off - 32 - len(cmds)) + blob
    if not fat:
        return thin
    slices = [(0x01000007, b"\xcf\xfa\xed\xfe" + b"\0" * 60)] if fat_other else []   # an x86_64 stand-in first
    slices.append((cputype, thin))
    out = struct.pack(">II", 0xCAFEBABE, len(slices))
    off = 4096
    body = b""
    entries = b""
    for cpu, data in slices:
        entries += struct.pack(">iiIII", cpu, 0, off + len(body), len(data), 12)
        body += data + b"\0" * (-len(data) % 4096)
    return (out + entries).ljust(off, b"\0") + body


def app_and_patch(bound: list[str], app_funcs: list[str], patch_funcs: list[str]) -> tuple[bytes, bytes]:
    """An app executable that defines app_funcs and calls `bound` of them through __got, and a patch dylib that
    defines patch_funcs."""
    text = Sect("__TEXT", "__text", b"\0" * (4 * max(1, len(app_funcs))), addr=0x1000)
    got = Sect("__DATA_CONST", "__got", b"\0" * (8 * len(bound)), flags=S_NON_LAZY_SYMBOL_POINTERS, addr=0x4000)
    syms = [Sym(n, value=0x1000 + 4 * i) for i, n in enumerate(app_funcs)]
    indirect = [app_funcs.index(n) for n in bound]
    app = build_macho([text, got], syms, filetype=MH_EXECUTE, indirect=indirect)
    ptext = Sect("__TEXT", "__text", b"\0" * (4 * max(1, len(patch_funcs))))
    patch = build_macho([ptext], [Sym(n, value=4 * i) for i, n in enumerate(patch_funcs)], filetype=MH_DYLIB,
                        id_dylib="@rpath/patch-1.dylib")
    return app, patch


# ---------------------------------------------------------------- signing identities

_KEYS: Path | None = None


def make_identity(folder: Path, team: str = "ABCDE12345", key_format: str = "pkcs8-pem",
                  wrap_key: bool = True, cert_format: str = "der") -> Path:
    """<folder>/<team>: a binary plist {privateKey: {data}, certificate} with a throwaway key. Returns folder."""
    folder.mkdir(parents=True, exist_ok=True)
    global _KEYS
    if _KEYS is None:
        _KEYS = Path(tempfile.mkdtemp(prefix="hot-reload-keys-"))     # one throwaway key per test run
    work = _KEYS / team
    work.mkdir(exist_ok=True)
    key, cert = work / "k.pem", work / "c.pem"
    if not key.exists():
        subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                        str(cert), "-days", "2", "-subj", f"/CN=Apple Development: Test ({team})/OU={team}"],
                       check=True, capture_output=True)

    def ossl(*args: str) -> bytes:
        return subprocess.run([OPENSSL, *args], check=True, capture_output=True).stdout

    if key_format == "pkcs8-pem":
        kb = key.read_bytes()
    elif key_format == "pkcs1-pem":
        kb = ossl("rsa", "-in", str(key), "-traditional")
    elif key_format == "pkcs1-der":
        kb = ossl("rsa", "-in", str(key), "-traditional", "-outform", "DER")
    elif key_format == "pkcs8-der":
        kb = ossl("pkcs8", "-topk8", "-nocrypt", "-in", str(key), "-outform", "DER")
    else:
        raise ValueError(key_format)
    cb = cert.read_bytes() if cert_format == "pem" else ossl("x509", "-in", str(cert), "-outform", "DER")
    pl = {"privateKey": {"data": kb} if wrap_key else kb, "certificate": cb}
    (folder / team).write_bytes(plistlib.dumps(pl, fmt=plistlib.FMT_BINARY))
    return folder


# ---------------------------------------------------------------- msgpack

def msgpack_str(s: str) -> bytes:
    b = s.encode()
    if len(b) < 32:
        return bytes([0xA0 | len(b)]) + b
    if len(b) < 256:
        return b"\xd9" + bytes([len(b)]) + b
    return b"\xda" + struct.pack(">H", len(b)) + b


def task_store(*runs: list[str], noise: bytes = b"\x93\x01\xc3\xa3abc") -> bytes:
    """Bytes with each run as `array header, "--", args…` between some unrelated msgpack values."""
    out = noise
    for args in runs:
        n = len(args) + 1
        out += (bytes([0x90 | n]) if n < 16 else b"\xdc" + struct.pack(">H", n)) + msgpack_str("--")
        out += b"".join(msgpack_str(a) for a in args) + noise
    return out


# ---------------------------------------------------------------- a fake toolchain and xtool build

FAKE_SWIFTC = r'''#!/usr/bin/env python3
import os, shlex, shutil, sys
a = sys.argv[1:]
d = os.path.dirname(os.path.abspath(__file__))
def files():
    out = []
    for x in a:
        if x.startswith("@"):
            out += [ln.strip() for ln in open(x[1:]) if ln.strip()]
        elif x.endswith(".swift"):
            out.append(x)
    return out
def val(flag):
    return a[a.index(flag) + 1] if flag in a else ""
if "-###" in a and "-emit-library" in a:
    objs = [x for x in a if x.endswith(".o")]
    print(shlex.join([os.path.join(d, "clang"), "-dynamiclib", *objs, "-Xlinker", "-install_name", "-Xlinker",
                      a[a.index("-install_name") + 2] if "-install_name" in a else "x", "-o", val("-o")]))
elif "-###" in a:
    fs = files()
    for f in fs:
        wd = ["-Xcc", "-working-directory", "-Xcc", val("-working-directory")] if "-working-directory" in a else []
        print(shlex.join([os.path.join(d, "swift-frontend"), "-frontend", "-c", "-primary-file", f,
                          *[o for o in fs if o != f], "-module-name", val("-module-name"), *wd, "-o", "/nowhere/x.o"]))
elif "-emit-library" in a:
    if os.environ.get("FAKE_LINK_FAIL"):
        print("ld64.lld: error: undefined symbol: _missing"); sys.exit(1)
    shutil.copy(os.environ["FAKE_PATCH"], val("-o"))
else:
    sys.exit(3)
'''

FAKE_CLANG = r'''#!/usr/bin/env python3
import os, shlex, sys
a = sys.argv[1:]
d = os.path.dirname(os.path.abspath(__file__))
objs = [x for x in a if x.endswith(".o")]
k = a.index("-install_name") + 1 if "-install_name" in a else -1
name = (a[k + 1] if a[k] == "-Xlinker" else a[k]) if k > 0 else "x"
out = a[a.index("-o") + 1]
sys.stderr.write("clang version 0 (fake)\n")
sys.stderr.write(" " + " ".join(shlex.quote(x) for x in [os.path.join(d, "ld64.lld"), "-dylib", *objs, "-o", out,
                                                         "-install_name", name]) + "\n")
'''

FAKE_LD = r'''#!/usr/bin/env python3
import os, shutil, sys
a = sys.argv[1:]
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls"), "a") as f:
    f.write("ld " + " ".join(a) + "\n")
print("ld64.lld: warning: This version of lld does not support linking for platform iOS. Proceed at your own risk.")
if os.environ.get("FAKE_LD_FAIL"):
    sys.exit(1)
shutil.copy(os.environ["FAKE_PATCH"], a[a.index("-o") + 1])
'''

FAKE_FRONTEND = r'''#!/usr/bin/env python3
import os, shutil, sys, time
a = sys.argv[1:]
src = a[a.index("-primary-file") + 1]
d = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(d, "calls"), "a") as f:
    f.write("frontend " + os.path.basename(src) + "\n")
with open(os.path.join(d, "cwds"), "a") as f:
    f.write(os.getcwd() + "\n")
if os.environ.get("FAKE_FRONTEND_HANG"):        # write our pid, then hang (a compile a killed session must not orphan)
    with open(os.environ["FAKE_FRONTEND_HANG"], "w") as f:
        f.write(str(os.getpid()))
    time.sleep(60)
text = open(src).read()
if "COMPILE_ERROR" in text:
    line = text[:text.index("COMPILE_ERROR")].count("\n") + 1
    print(f"{src}:{line}:5: error: cannot find 'COMPILE_ERROR' in scope")
    print(f"{src}:{line}:5: note: did you mean 'compileError'?")
    sys.exit(1)
objs = os.environ["FAKE_OBJ_DIR"]
stem = os.path.splitext(os.path.basename(src))[0]
pick = os.path.join(objs, stem + ".o")
shutil.copy(pick if os.path.exists(pick) else os.path.join(objs, "default.o"), a[a.index("-o") + 1])
'''


def fake_toolchain(folder: Path) -> Path:
    """swiftc, swift-frontend, clang and ld64.lld fakes in `folder` (calls logged to folder/calls, each compile's
    working directory to folder/cwds). The compiled object comes from $FAKE_OBJ_DIR/<stem>.o (or default.o), the
    linked dylib from $FAKE_PATCH. $FAKE_FRONTEND_HANG=<file>: a compile writes its pid there and hangs."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in (("swiftc", FAKE_SWIFTC), ("clang", FAKE_CLANG), ("ld64.lld", FAKE_LD),
                       ("swift-frontend", FAKE_FRONTEND)):
        p = folder / name
        p.write_text(text)
        p.chmod(0o755)
    return folder


def fake_profile(team: str) -> bytes:
    pl = plistlib.dumps({"TeamIdentifier": [team], "Name": "fake", "Entitlements": {}})
    return b"\x30\x80fake-cms" + pl + b"\0\0"


def fake_build(project: Path, tools: Path, module: str = "App", files: dict[str, str] | None = None,
               app_binary: bytes | None = None, objects: dict[str, bytes] | None = None,
               team: str | None = "ABCDE12345", bundle_id: str = "dev.example.app") -> Path:
    """A project folder as `xtool dev build` leaves it: Sources/<module>/*.swift, .build/out/… with a task store
    (the driver run uses the fake swiftc), manifest.json (the app link), the output file map and the objects,
    and xtool/<module>.app with Info.plist, the executable and (with a team) an embedded.mobileprovision."""
    files = files or {"ContentView.swift": "struct ContentView {}\n", "App.swift": "struct App {}\n"}
    src_dir = project / "Sources" / module
    src_dir.mkdir(parents=True, exist_ok=True)
    out = project / ".build/out"
    inter = out / f"Intermediates.noindex/{module}.build/Debug-iphoneos/{module}-t.build/Objects-normal/arm64"
    inter.mkdir(parents=True, exist_ok=True)
    ofm: dict = {"": {"diagnostics": str(inter / "x.dia")}}
    for name, text in files.items():
        (src_dir / name).write_text(text)
        obj = inter / (Path(name).stem + ".o")
        obj.write_bytes((objects or {}).get(name, b"obj"))
        ofm[str(src_dir / name)] = {"object": str(obj)}
    (inter / f"{module}-OutputFileMap.json").write_text(__import__("json").dumps(ofm))
    filelist = inter / f"{module}.SwiftFileList"
    filelist.write_text("".join(f"{src_dir / n}\n" for n in files))
    driver = [str(tools / "swiftc"), "-parse-as-library", "-module-name", module, "-Onone", f"@{filelist}",
              "-sdk", "/sdk", "-c", "-j8", "-enable-batch-mode", "-incremental", "-output-file-map",
              str(inter / f"{module}-OutputFileMap.json"), "-module-cache-path", str(out / "ModuleCache.noindex"),
              "-explicit-module-build", "-module-cache-path", str(out / "Explicit"), "-emit-module",
              "-emit-module-path", str(inter / f"{module}.swiftmodule"), "-working-directory", str(project / "xtool")]
    bd = out / "Intermediates.noindex/XCBuildData/abc.xcbuilddata"
    bd.mkdir(parents=True, exist_ok=True)
    other = [str(tools / "swiftc"), "-module-name", "Other", "-c"]
    (bd / "task-store.msgpack").write_bytes(task_store(other, driver))
    link = [str(tools / "swiftc"), "-target", "arm64-apple-ios17.0", "-emit-executable", "-sdk", "/sdk",
            f"@{inter}/{module}-App.LinkFileList", "-Xlinker", "-dead_strip", "-Xlinker", "-object_path_lto",
            "-Xlinker", str(inter / "lto.o"), "-Xlinker", "-interposable", "-o", str(out / f"Products/{module}-App")]
    manifest = {"commands": {
        f"P2:target-{module}:Ld {out}/Products/{module}.o normal": {"args": ["/usr/bin/clang", "-r"]},
        f"P2:target-{module}-App:Ld {out}/Products/{module}-App normal": {"args": link}}}
    (bd / "manifest.json").write_text(__import__("json").dumps(manifest))
    app = project / "xtool" / f"{module}.app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": module, "CFBundleIdentifier": bundle_id}))
    (app / module).write_bytes(app_binary if app_binary is not None else app_and_patch([], ["_x"], [])[0])
    if team:
        (app / "embedded.mobileprovision").write_bytes(fake_profile(team))
    return project


def pack_ipa(app: Path, ipa: Path | None = None, remove: bool = True,
             extra: dict[str, bytes] | None = None) -> Path:
    """A Wi-Fi build's xtool/<App>.ipa from the .app fake_build made (Payload/<App>.app/<files>), the .app removed
    (as `xtool dev build --ipa` leaves it) unless remove=False. `extra`: more entries, by their name in the zip."""
    import zipfile
    ipa = ipa or app.with_suffix(".ipa")
    with zipfile.ZipFile(ipa, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"Payload/{app.name}/", b"")
        for p in sorted(app.rglob("*")):
            if p.is_file():
                z.write(p, f"Payload/{app.name}/{p.relative_to(app)}")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    if remove:
        shutil.rmtree(app)
    return ipa
