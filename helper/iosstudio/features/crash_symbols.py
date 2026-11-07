"""Crash symbolication: file:line and function names for the app's own frames in a crash report (.ips).

A crash report names each frame as '<image> + 0xOFFSET'. To turn that into Sources/App/File.swift:42 you
need the exact binary that crashed (matched by its Mach-O LC_UUID) and its debug info. Builds overwrite
.build, so on every successful build we keep the app's executable plus a dSYM (dsymutil collects the DWARF
from the .o files while they still exist) in a cache keyed by the UUID:

    ~/.cache/ios-studio/symbols/<uuid>/{<Exe>, <Exe>.dSYM/, meta.json}
    ~/.cache/ios-studio/symbols/index.json      every UUID ever kept, so a pruned build can say so

Pruned to the newest KEEP_PER_PROJECT builds per project and MAX_BYTES in total.

Reading a crash, each app image's UUID is looked up in the cache, then in the project's current build
products (xtool/<App>.app, .build/<config>/<App>-App, the signed .ipa), and llvm-symbolizer resolves the
offsets. Swift names are demangled with swift-demangle. Pure stdlib plus those two toolchain tools.

CLI (for checks by hand):  python3 helper/run.py iosstudio.features.crash_symbols remember <project> [product] [config]
                           python3 helper/run.py iosstudio.features.crash_symbols symbolicate <report.ips>
"""
from __future__ import annotations

import io
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from iosstudio.core import appdirs, fileio

CACHE = appdirs.CACHE_DIR / "symbols"
INDEX = CACHE / "index.json"
KEEP_PER_PROJECT = 10
MAX_BYTES = 1_000_000_000
MAX_INDEX = 2000
RESULT_VERSION = 1
SIDE = ".symbols.json"          # the result next to the local .ips copy
MAX_FRAMES = 400                 # app frames resolved per report (all threads)

MH_MAGIC_64, FAT_MAGIC, FAT_MAGIC_64 = 0xFEEDFACF, 0xCAFEBABE, 0xCAFEBABF
CPU_ARM64 = 0x0100000C
LC_SEGMENT_64, LC_UUID = 0x19, 0x1B
SYSTEM_PREFIXES = ("/System/", "/usr/lib/", "/usr/libexec/", "/Developer/", "/private/preboot/",
                   "/Library/", "/AppleInternal/")


# ------------------------------------------------------------------ Mach-O

def _thin(head: bytes) -> dict | None:
    """{uuid, cputype, cpusubtype, text_vmaddr} of one 64-bit Mach-O header (+ load commands)."""
    if len(head) < 32 or struct.unpack_from("<I", head, 0)[0] != MH_MAGIC_64:
        return None
    cputype, cpusub, _ft, ncmds, _size = struct.unpack_from("<iIIII", head, 4)
    uuid, text = None, None
    off = 32
    for _ in range(ncmds):
        if off + 8 > len(head):
            break
        cmd, size = struct.unpack_from("<II", head, off)
        if size < 8:
            break
        if cmd == LC_UUID and off + 24 <= len(head):
            uuid = head[off + 8:off + 24].hex()
        elif cmd == LC_SEGMENT_64 and off + 32 <= len(head):
            if head[off + 8:off + 24].rstrip(b"\0") == b"__TEXT":
                text = struct.unpack_from("<Q", head, off + 24)[0]
        off += size
    if not uuid:
        return None
    return {"uuid": norm_uuid(uuid), "cputype": cputype, "cpusubtype": cpusub & 0x00FFFFFF,
            "text_vmaddr": text if text is not None else 0x100000000}


def _read_slice(f, offset: int) -> dict | None:
    f.seek(offset)
    head = f.read(32)
    if len(head) < 32 or struct.unpack_from("<I", head, 0)[0] != MH_MAGIC_64:
        return None
    sizeofcmds = struct.unpack_from("<I", head, 20)[0]
    return _thin(head + f.read(min(sizeofcmds, 4 << 20)))


def macho_slices(path: str | Path) -> list[dict]:
    """The UUID, CPU and __TEXT address of each arm64 slice of a Mach-O file (thin or universal).
    [] when it isn't one (or isn't readable)."""
    try:
        with open(path, "rb") as f:
            return slices_of(f)
    except OSError:
        return []


def slices_of(f) -> list[dict]:
    """macho_slices() of an open binary file object."""
    try:
        magic = f.read(4)
        if len(magic) < 4:
            return []
        if struct.unpack("<I", magic)[0] == MH_MAGIC_64:
            s = _read_slice(f, 0)
            return [s] if s else []
        be = struct.unpack(">I", magic)[0]
        if be not in (FAT_MAGIC, FAT_MAGIC_64):
            return []
        n = struct.unpack(">I", f.read(4))[0]
        out = []
        for _ in range(min(n, 16)):
            if be == FAT_MAGIC:
                cpu, _sub, off, _size, _align = struct.unpack(">iIIII", f.read(20))
            else:
                cpu, _sub, off, _size, _align, _r = struct.unpack(">iIQQII", f.read(32))
            pos = f.tell()
            if cpu == CPU_ARM64:
                s = _read_slice(f, off)
                if s:
                    out.append(s)
            f.seek(pos)
        return out
    except (OSError, struct.error):
        return []


def has_line_info(path: str | Path) -> bool:
    """Whether a thin Mach-O carries DWARF line tables (a __DWARF,__debug_line section with data). A dSYM made
    from a binary whose .o files are gone has the UUID but no lines."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if len(head) < 32 or struct.unpack_from("<I", head, 0)[0] != MH_MAGIC_64:
                return False
            ncmds, sizeofcmds = struct.unpack_from("<II", head, 16)
            cmds = f.read(min(sizeofcmds, 4 << 20))
    except (OSError, struct.error):
        return False
    off = 0
    for _ in range(ncmds):
        if off + 72 > len(cmds):
            break
        cmd, size = struct.unpack_from("<II", cmds, off)
        if size < 8:
            break
        if cmd == LC_SEGMENT_64 and cmds[off + 8:off + 24].rstrip(b"\0") == b"__DWARF":
            for s in range(struct.unpack_from("<I", cmds, off + 64)[0]):
                so = off + 72 + 80 * s
                if so + 80 > len(cmds):
                    break
                if cmds[so:so + 16].rstrip(b"\0") == b"__debug_line" and struct.unpack_from("<Q", cmds, so + 40)[0]:
                    return True
        off += size
    return False


def norm_uuid(u: str) -> str:
    """'4C4C4436555531...' or '4c4c4436-5555-...' -> '4c4c4436-5555-3144-a1ff-3313a3a53a5d'."""
    h = re.sub(r"[^0-9a-fA-F]", "", str(u or "")).lower()
    if len(h) != 32:
        return ""
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _uuids(path: Path) -> set[str]:
    return {s["uuid"] for s in macho_slices(path)}


# ------------------------------------------------------------------ tools

def _toolchain_bin() -> Path | None:
    swift = shutil.which("swift")
    return Path(os.path.realpath(swift)).parent if swift else None


def find_tool(name: str) -> str | None:
    """A tool from the Swift toolchain (next to `swift`), else from PATH, else xtool's Swift SDK toolset."""
    tc = _toolchain_bin()
    if tc and (tc / name).is_file():
        return str(tc / name)
    found = shutil.which(name)
    if found:
        return found
    sdk = Path(os.environ.get("XDG_CONFIG_HOME", appdirs.HOME / ".config")) / "swiftpm/swift-sdks/darwin.artifactbundle"
    cand = sdk / "toolset/bin" / name
    return str(cand) if cand.is_file() else None


def demangle(names: list[str]) -> dict[str, str]:
    try:
        from iosstudio.features import diagnostics
        return diagnostics.demangle(names)
    except Exception:  # noqa: BLE001 - names stay mangled
        return {}


# ------------------------------------------------------------------ the cache

def _load_index() -> dict:
    data = fileio.load_json(INDEX, {}) or {}
    return data if isinstance(data, dict) else {}


def _record(uuid: str, meta: dict) -> None:
    idx = _load_index()
    idx[uuid] = {k: meta.get(k) for k in ("project", "product", "config", "builtAt")}
    if len(idx) > MAX_INDEX:
        for k in sorted(idx, key=lambda k: idx[k].get("builtAt") or 0)[:len(idx) - MAX_INDEX]:
            idx.pop(k, None)
    try:
        fileio.save_json(INDEX, idx)
    except OSError:
        pass


def _app_executable(app: Path) -> Path | None:
    try:
        exe = plistlib.loads((app / "Info.plist").read_bytes()).get("CFBundleExecutable")
    except Exception:  # noqa: BLE001
        return None
    return app / exe if exe and (app / exe).is_file() else None


def build_products(project: Path, product: str | None = None) -> list[Path]:
    """The app executables a build leaves in a project, newest first: .build/<config>/<Product>-App (what
    xtool signs), .build/out/Products/*-iphoneos/<Product>-App, and xtool/<App>.app/<Exe>."""
    found: list[Path] = []
    names = [f"{product}-App"] if product else []
    for cfg in ("debug", "release"):
        d = project / ".build" / cfg
        for exe in [d / n for n in names] if names else d.glob("*-App"):
            if exe.is_file():
                found.append(exe)
    for exe in (project / ".build").glob("out/Products/*-iphoneos/*-App"):
        if not product or exe.name == f"{product}-App":
            found.append(exe)
    for app in sorted((project / "xtool").glob("*.app")):
        exe = _app_executable(app)
        if exe:
            found.append(exe)

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0
    return sorted(set(found), key=mtime, reverse=True)


def cached(uuid: str) -> dict | None:
    """meta.json of the kept build with this UUID (with 'dir'), or None."""
    uuid = norm_uuid(uuid)
    d = CACHE / uuid
    meta = fileio.load_json(d / "meta.json", None) if uuid else None
    if not isinstance(meta, dict) or not (d / str(meta.get("exe") or "")).is_file():
        return None
    meta["dir"] = str(d)
    return meta


def _dsym_dwarf(d: Path, exe: str) -> Path | None:
    p = d / f"{exe}.dSYM" / "Contents" / "Resources" / "DWARF" / exe
    return p if p.is_file() else None


def keep(exe: Path, project: str | Path, product: str | None = None, config: str | None = None,
         source: str = "build", dsym_timeout: float = 120) -> dict | None:
    """Copy one executable (+ a dSYM made with dsymutil) into the cache under its UUID. Reuses an entry
    that's already there (and bumps it to newest). Never raises; None when exe isn't an arm64 Mach-O."""
    try:
        slices = macho_slices(exe)
        if not slices:
            return None
        uuid = slices[0]["uuid"]
        d = CACHE / uuid
        meta = cached(uuid)
        if meta:
            os.utime(d)
            return meta
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = CACHE / f".{uuid}.{os.getpid()}.part"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        shutil.copy2(exe, tmp / exe.name)
        dsym = False
        tool = find_tool("dsymutil")
        if tool:
            try:
                # From the original path: its debug map points at .o files that are still there right after the build.
                subprocess.run([tool, str(exe), "-o", str(tmp / f"{exe.name}.dSYM")], capture_output=True,
                               timeout=dsym_timeout, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
            dw = _dsym_dwarf(tmp, exe.name)
            dsym = bool(dw and uuid in _uuids(dw) and has_line_info(dw))
            if not dsym:
                shutil.rmtree(tmp / f"{exe.name}.dSYM", ignore_errors=True)
        dsym = dsym or has_line_info(exe)       # an executable that carries its own DWARF
        meta = {"uuid": uuid, "exe": exe.name, "project": str(project), "product": product, "config": config,
                "builtAt": exe.stat().st_mtime, "keptAt": time.time(), "dsym": dsym, "source": source,
                "arch": "arm64e" if slices[0]["cpusubtype"] == 2 else "arm64",
                "textVmaddr": slices[0]["text_vmaddr"]}
        fileio.save_json(tmp / "meta.json", meta)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        tmp.rename(d)
        _record(uuid, meta)
        prune(protect=d)
        meta["dir"] = str(d)
        return meta
    except Exception:  # noqa: BLE001 - symbol caching must never break a build
        return None


def remember_build(project: str | Path, product: str | None = None, config: str | None = None) -> dict | None:
    """Called once after a successful build: keep the executable it produced. Best effort."""
    try:
        p = Path(project)
        products = build_products(p, product)
        if config:
            products.sort(key=lambda x: 0 if f"/.build/{config}/" in str(x) else 1)
        return keep(products[0], p, product, config) if products else None
    except Exception:  # noqa: BLE001
        return None


def prune(protect: Path | None = None) -> int:
    """Newest KEEP_PER_PROJECT builds per project, MAX_BYTES in total (fileio.prune; the newest stays)."""
    try:
        dirs = [d for d in CACHE.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except OSError:
        return 0
    groups: dict[str, list[Path]] = {}
    for d in dirs:
        meta = fileio.load_json(d / "meta.json", {}) or {}
        groups.setdefault(str(meta.get("project") or ""), []).append(d)
    keep_ = [protect] if protect else []
    n = 0
    for paths in groups.values():
        n += fileio.prune(paths, KEEP_PER_PROJECT, min_keep=1, protect=keep_)
    left = [d for d in dirs if d.exists()]
    n += fileio.prune(left, len(left) + 1, max_bytes=MAX_BYTES, min_keep=1, protect=keep_)
    return n


def _known_projects() -> list[Path]:
    """Projects that were built here (newest first) and the ones added by hand."""
    out: list[str] = []
    hist = fileio.load_json(appdirs.STATE_DIR / "builds.json", []) or []
    for e in reversed(hist if isinstance(hist, list) else []):
        p = e.get("path") if isinstance(e, dict) else None
        if p and p not in out:
            out.append(p)
    st = fileio.load_json(appdirs.STATE_DIR / "projects.json", {}) or {}
    for p in (st.get("added") or []) if isinstance(st, dict) else []:
        if isinstance(p, str) and p not in out:
            out.append(p)
    return [Path(p) for p in out[:30]]


def _ipa_match(ipa: Path, uuid: str, tmpdir: Path) -> Path | None:
    """Unpack the .ipa's executable when its UUID matches (reads the header first)."""
    try:
        with zipfile.ZipFile(ipa) as z:
            names = z.namelist()
            plists = [n for n in names if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
            if not plists:
                return None
            exe = plistlib.loads(z.read(plists[0])).get("CFBundleExecutable")
            member = plists[0].rsplit("/", 1)[0] + "/" + str(exe)
            if not exe or member not in names:
                return None
            tmpdir.mkdir(parents=True, exist_ok=True)
            out = tmpdir / str(exe)
            with z.open(member) as src:
                head = src.read(1 << 16)
                if uuid not in {s["uuid"] for s in slices_of(io.BytesIO(head))}:
                    return None
                with open(out, "wb") as dst:
                    dst.write(head)
                    shutil.copyfileobj(src, dst)
            return out
    except Exception:  # noqa: BLE001
        return None


def find_build(uuid: str, name: str | None = None, projects: list[Path] | None = None) -> dict | None:
    """The kept build with this UUID; else look in the projects' current build products (and their last
    .ipa) and keep a match. None when no build here has this UUID."""
    uuid = norm_uuid(uuid)
    if not uuid:
        return None
    meta = cached(uuid)
    if meta:
        return meta
    for proj in projects if projects is not None else _known_projects():
        for exe in build_products(proj):
            if name and exe.name not in (name, f"{name}-App") and not exe.name.startswith(name):
                continue
            if uuid in _uuids(exe):
                return keep(exe, proj, source="found")
        for ipa in sorted((proj / "xtool").glob("*.ipa")):
            tmp = CACHE / f".ipa-{os.getpid()}"
            exe = _ipa_match(ipa, uuid, tmp)
            if exe:
                meta = keep(exe, proj, source="ipa")
                shutil.rmtree(tmp, ignore_errors=True)
                return meta
            shutil.rmtree(tmp, ignore_errors=True)
    return None


# ------------------------------------------------------------------ symbolication

def is_app_image(image: dict) -> bool:
    """An image that belongs to the app bundle (its executable, app extensions, embedded frameworks)."""
    path = str(image.get("path") or "")
    if not image.get("uuid") or not path or path.startswith(SYSTEM_PREFIXES):
        return False
    return "/Bundle/Application/" in path or ".app/" in path


def _image_name(image: dict) -> str:
    return image.get("name") or Path(str(image.get("path") or "?")).name


def run_symbolizer(obj: Path, addrs: list[int], arch: str | None = None, timeout: float = 20) -> dict[int, dict]:
    """addr -> {function (mangled), file, line, column} from llvm-symbolizer (innermost inlined frame)."""
    tool = find_tool("llvm-symbolizer")
    if not tool or not addrs:
        return {}
    argv = [tool, f"--obj={obj}", "--output-style=JSON", "--functions=linkage", "--inlines", "--no-demangle"]
    if arch:
        argv.append(f"--default-arch={arch}")
    try:
        r = subprocess.run(argv, input="\n".join(hex(a) for a in addrs) + "\n", capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict[int, dict] = {}
    for line in r.stdout.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in rec if isinstance(rec, list) else [rec]:
            try:
                addr = int(str(item.get("Address")), 16)
            except (TypeError, ValueError):
                continue
            syms = item.get("Symbol") or []
            if not syms:
                continue
            s = syms[0]
            fn = s.get("FunctionName") or ""
            file = s.get("FileName") or ""
            line_no = int(s.get("Line") or 0)
            out[addr] = {"function": fn if fn and fn != "??" else None,
                         "file": file if file and not file.startswith("/<") and line_no > 0 else None,
                         "line": line_no or None, "column": int(s.get("Column") or 0) or None}
    return out


def _rel(path: str, project: str | None) -> str:
    if project and path.startswith(project.rstrip("/") + "/"):
        return path[len(project.rstrip("/")) + 1:]
    parts = path.split("/")
    return "/".join(parts[-3:]) if len(parts) > 3 else path


def _body(text: str) -> dict | None:
    _, _, body = text.partition("\n")
    try:
        b = json.loads(body)
    except json.JSONDecodeError:
        return None
    return b if isinstance(b, dict) else None


def _crashed(b: dict) -> int | None:
    ft = b.get("faultingThread")
    if isinstance(ft, int):
        return ft
    for i, t in enumerate(b.get("threads") or []):
        if isinstance(t, dict) and t.get("triggered"):
            return i
    return None


def make_result(status: str, reason: str, message: str, hint: str | None = None, **extra) -> dict:
    return {"version": RESULT_VERSION, "status": status, "reason": reason, "message": message, "hint": hint,
            "images": [], "project": None, "frames": {}, **extra}


HINT_REBUILD = ("Rebuild that exact version of the app (same code and settings), install it and reproduce the "
                "crash; new crashes then resolve to file:line.")


def symbolicate(text: str, finder=find_build) -> dict:
    """Resolve the app's frames in a .ips report. Returns
    {status: full|partial|none|not-needed, reason, message, hint, project, images: [...],
     frames: {"<thread>:<frame>": {image, function, file, path, line, column}}}.
    full = every app frame of the crashed thread has file:line; partial = some (or names only);
    none = no matching build (reason says why); not-needed = nothing of the app's own to resolve."""
    b = _body(text)
    if b is None:
        return make_result("none", "unsupported", "This report isn't in the JSON format (iOS 15 or later) that can be "
                                              "symbolicated.")
    images = b.get("usedImages") or []
    threads = [t for t in (b.get("threads") or []) if isinstance(t, dict)]
    crashed = _crashed(b)
    want: dict[int, list[tuple[int, int, int]]] = {}     # image index -> [(thread, frame, offset)]
    n = 0
    order = ([crashed] if isinstance(crashed, int) and 0 <= crashed < len(threads) else []) + \
        [i for i in range(len(threads)) if i != crashed]
    for ti in order:
        for fi, f in enumerate(threads[ti].get("frames") or []):
            idx = f.get("imageIndex") if isinstance(f, dict) else None
            if not isinstance(idx, int) or not 0 <= idx < len(images) or not is_app_image(images[idx]):
                continue
            if n >= MAX_FRAMES:
                break
            want.setdefault(idx, []).append((ti, fi, int(f.get("imageOffset") or 0)))
            n += 1
    if not want:
        return make_result("not-needed", "no-app-frames", "No frames in the app's own code to resolve.")
    if not find_tool("llvm-symbolizer"):
        return make_result("none", "no-symbolizer", "llvm-symbolizer isn't installed, so app frames can't be resolved.",
                       "Install the Swift toolchain (iOS Studio › Setup), or the llvm package.")
    index = _load_index()
    frames: dict[str, dict] = {}
    imgs: list[dict] = []
    project = None
    for idx, items in want.items():
        im = images[idx]
        uuid = norm_uuid(im.get("uuid"))
        name = _image_name(im)
        meta = finder(uuid, name)
        info = {"name": name, "uuid": uuid, "found": bool(meta)}
        if not meta:
            info["pruned"] = uuid in index
            imgs.append(info)
            continue
        project = project or meta.get("project")
        info.update(project=meta.get("project"), builtAt=meta.get("builtAt"), dsym=bool(meta.get("dsym")))
        d = Path(meta["dir"])
        obj = _dsym_dwarf(d, str(meta["exe"])) or d / str(meta["exe"])
        base = int(meta.get("textVmaddr") or 0x100000000)
        # Frame 0 is the program counter; every other frame is a return address, which points at the
        # instruction after the call: step back one byte so the line is the call's.
        addr = {(t, f): base + off - (1 if f > 0 else 0) for t, f, off in items}
        res = run_symbolizer(obj, sorted(set(addr.values())), str(meta.get("arch") or "arm64"))
        names = demangle([r["function"] for r in res.values() if r.get("function")])
        for (t, f), a in addr.items():
            r = res.get(a)
            if not r:
                continue
            fn = r.get("function")
            entry = {"image": name, "function": names.get(fn, fn) if fn else None, "line": r.get("line"),
                     "column": r.get("column")}
            if r.get("file"):
                entry["path"] = r["file"]
                entry["file"] = _rel(r["file"], meta.get("project"))
            frames[f"{t}:{f}"] = entry
        imgs.append(info)
    crashed_keys = [f"{t}:{f}" for idx, items in want.items() for t, f, _ in items if t == crashed]
    missing = [i for i in imgs if not i["found"]]
    if not any(i["found"] for i in imgs):
        pruned = any(i.get("pruned") for i in missing)
        who = ", ".join(sorted({i["name"] for i in missing}))
        if pruned:
            return make_result("none", "pruned", f"The build of {who} that crashed was kept once but has been pruned "
                                             f"(iOS Studio keeps the last {KEEP_PER_PROJECT} builds per project).",
                           HINT_REBUILD, images=imgs)
        return make_result("none", "no-build", f"No build on this computer matches {who} in this report (UUID "
                                           f"{missing[0]['uuid'] or '?'}). It was built before iOS Studio kept "
                                           "symbols, on another computer, or the project was rebuilt since.",
                       HINT_REBUILD, images=imgs)
    # Judge by the crashed thread's app frames (or every app frame when it has none). A frame with a name
    # but no line is compiler-generated code (the synthesized `main`, thunks): resolved as far as it goes.
    keys = crashed_keys or list(frames) or [f"{t}:{f}" for items in want.values() for t, f, _ in items]
    with_line = [k for k in keys if frames.get(k, {}).get("file")]
    named = [k for k in keys if frames.get(k, {}).get("file") or frames.get(k, {}).get("function")]
    no_dsym = [i for i in imgs if i["found"] and not i.get("dsym")]
    if not missing and with_line and len(named) == len(keys):
        status, reason, msg, hint = "full", "ok", "Resolved the app's frames to file:line.", None
    elif no_dsym and not with_line:
        status, reason = "partial", "no-debug-info"
        msg = "Found the build, but it has no debug info, so only function names are shown."
        hint = "Debug builds keep line numbers; build with config 'debug' to get file:line."
    else:
        status, reason = "partial", "partial"
        parts = []
        if missing:
            parts.append("no matching build for " + ", ".join(sorted({i["name"] for i in missing})))
        if len(named) < len(keys):
            parts.append(f"{len(named)} of {len(keys)} app frames resolved")
        msg = "Resolved some frames: " + ("; ".join(parts) or "some have no line information") + "."
        hint = HINT_REBUILD if missing else None
    return make_result(status, reason, msg, hint, images=imgs, project=project, frames=frames)


def symbolicate_file(ips: str | Path, finder=find_build) -> dict:
    """symbolicate() with the result kept next to the report (<file>.symbols.json). A full result is reused;
    anything less is redone (a rebuild of the right version may have come along since)."""
    ips = Path(ips)
    side = Path(str(ips) + SIDE)
    old = fileio.load_json(side, None)
    if isinstance(old, dict) and old.get("version") == RESULT_VERSION and old.get("status") in ("full", "not-needed"):
        return old
    try:
        text = ips.read_text(errors="replace")
    except OSError:
        return make_result("none", "unreadable", "The report couldn't be read.")
    res = symbolicate(text, finder)
    try:
        fileio.atomic_write(side, json.dumps(res))
    except OSError:
        pass
    return res


def load_result(ips: str | Path) -> dict | None:
    """The saved result for a report, if any (the MCP server uses it to annotate every thread)."""
    data = fileio.load_json(Path(str(ips) + SIDE), None)
    return data if isinstance(data, dict) else None


def frame_text(n: int, info: dict, fallback: str) -> str:
    """'4 MyApp Forecast.first() (Sources/MyApp/Forecast.swift:6:17)' for a resolved frame."""
    if not info or not (info.get("function") or info.get("file")):
        return fallback
    where = ""
    if info.get("file"):
        where = f" ({info['file']}:{info['line']}" + (f":{info['column']}" if info.get("column") else "") + ")"
    return f"{n} {info.get('image') or ''} {info.get('function') or ''}".rstrip() + where


def apply(summary: dict, res: dict, crashed: int | None = None, limit: int = 15) -> dict:
    """Add the result to a parse_ips summary: `symbolication` (status, reason, message, hint, project and
    `frames`, aligned with summary.frames: a dict for each resolved app frame, else None), and resolved frames
    rewritten as '<n> <image> <function> (<file>:<line>)'."""
    ct = summary.get("crashedThread") if crashed is None else crashed
    texts = list(summary.get("frames") or [])
    aligned = []
    for n in range(min(len(texts), limit)):
        info = (res.get("frames") or {}).get(f"{ct}:{n}")
        aligned.append(info if info and (info.get("file") or info.get("function")) else None)
        if info:
            texts[n] = frame_text(n, info, texts[n])
    summary["frames"] = texts
    summary["symbolication"] = {k: res.get(k) for k in ("status", "reason", "message", "hint", "project")}
    summary["symbolication"]["frames"] = aligned
    return summary


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "remember":
        print(json.dumps(remember_build(argv[1], argv[2] if len(argv) > 2 else None,
                                        argv[3] if len(argv) > 3 else None), indent=1))
        return 0
    if len(argv) == 2 and argv[0] == "symbolicate":
        print(json.dumps(symbolicate(Path(argv[1]).read_text(errors="replace")), indent=1))
        return 0
    print(__doc__.split("CLI", 1)[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
