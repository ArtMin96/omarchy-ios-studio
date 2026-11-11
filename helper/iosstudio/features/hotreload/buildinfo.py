"""What a hot-reload patch needs from the last `xtool dev build` of a project, captured once as a Baseline.

xtool builds with SwiftBuild, which leaves everything in `<project>/.build/out/`:

  Intermediates.noindex/XCBuildData/<hash>.xcbuilddata/task-store.msgpack   the module's swiftc driver arguments
      (a run of msgpack strings "--", ".../swiftc", "-parse-as-library", …, with -module-name <Module>)
  …/manifest.json         the app's link command (a command whose key has ":Ld " and whose args[0] is swiftc)
  …/<Module>-OutputFileMap.json   where each source's object (.o) went

From those, one `swiftc … -###` call prints the per-file frontend commands (compile one file against the rest of
the module), and two more `-###` calls turn the patch link (the app's link as a dylib) into the plain ld64.lld
command it ends in (≈0.8 s instead of ≈2 s through the driver).

    capture_baseline(project, module, cache_root=None) -> Baseline     copies into the hot-reload cache:
      <cache_root>/<key>/baseline.json   the Baseline (JSON)
                         sources/        the module's Swift files under the project, as built (by project path)
                         objects/        their baseline .o files (<File>.o)
                         app.bin         the built app executable (xtool/<App>.app/<exe>)
                         patches/        written by patch.py (newest 10 kept)
    load_baseline(project, cache_root=None) -> Baseline | None

key = the first 12 hex digits of sha1(project path); the whole cache is kept under 300 MB (oldest projects go
first). Anything missing fails with HelperError('no-baseline').
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import struct
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, fileio, subproc
from iosstudio.core.output import HelperError, fix
from iosstudio.features.hotreload import macho

CACHE_MAX_BYTES = 300 * 1024 * 1024
HASH_TIMEOUT = 30            # every `-###` call
MAX_STORE = 64 * 1024 * 1024  # task-store.msgpack / manifest.json read cap
MAX_ARGS = 4096              # longest driver argument run accepted
BASELINE_VERSION = 1

# Placeholders in the direct ld64.lld template (ld_command fills them in).
OBJECTS, OUTPUT, INSTALL_NAME = "{{objects}}", "{{output}}", "{{install_name}}"
NAME_SENTINEL = "@rpath/__iosstudio_hot_reload__.dylib"

# Driver flags dropped before asking for the frontend commands (they're about the whole build, not one file).
DROP_ONE = {"-incremental", "-enable-batch-mode", "-explicit-module-build", "-emit-module",
            "-experimental-emit-module-separately", "-emit-objc-header", "-save-temps", "-emit-const-values",
            "-emit-dependencies", "-serialize-diagnostics", "-color-diagnostics", "-c", "-validate-clang-modules-once",
            "-whole-module-optimization", "-wmo", "-parseable-output", "-use-frontend-parseable-output"}
DROP_TWO = {"-emit-module-path", "-emit-objc-header-path", "-index-store-path", "-const-gather-protocols-list",
            "-dependency-scan-serialize-diagnostics-path", "-output-file-map", "-clang-scanner-module-cache-path",
            "-sdk-module-cache-path", "-clang-build-session-file", "-num-threads"}
# Link flags dropped from the app's link command for the patch: "-Xlinker X" pairs, and pairs with one value.
LINK_DROP_X1 = {"-all_load", "-dead_strip", "-no_deduplicate"}
LINK_DROP_X2 = {"-object_path_lto", "-dependency_info"}


def no_baseline(message: str, detail: str | None = None) -> HelperError:
    return HelperError("no-baseline", message, fix("Run again", "projects", action="hot-reload-restart"), detail)


@dataclass
class Baseline:
    project: str                          # absolute project folder
    module: str                           # the app's Swift module (target)
    key: str
    cache_dir: str                        # <cache_root>/<key>
    swiftc: str                           # the driver
    frontend: dict[str, list[str]]        # project-relative source -> its frontend argv (its -o is replaced)
    link: list[str]                       # the patch link through the driver, without objects/-o/install name
    ld: list[str] | None                  # the same as a direct ld64.lld argv template (ld_command fills it)
    app_binary: str                       # the cached copy (app.bin)
    app_path: str                         # xtool/<App>.app/<exe> as built
    bundle_id: str | None
    team: str | None                      # from the app's embedded.mobileprovision (None: an unsigned dev build)
    built_at: float                       # mtime of the app executable
    captured_at: float
    objects: dict[str, str] = field(default_factory=dict)    # project-relative source -> cached baseline .o
    interposable: int = 0                 # the app's own exported symbols bound through pointer entries
    version: int = BASELINE_VERSION

    # ---- paths
    @property
    def root(self) -> Path:
        return Path(self.cache_dir)

    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    @property
    def patches_dir(self) -> Path:
        return self.root / "patches"

    def source_copy(self, rel: str) -> Path:
        return self.sources_dir / rel

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Baseline":
        names = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in names})


# ---------------------------------------------------------------- cache folders

def cache_root() -> Path:
    return appdirs.cache_dir() / "hot-reload"


def project_key(project: str | Path) -> str:
    return hashlib.sha1(str(Path(project)).encode()).hexdigest()[:12]


def project_cache(project: str | Path, root: Path | None = None) -> Path:
    return (root or cache_root()) / project_key(project)


def load_baseline(project: str | Path, root: Path | None = None) -> Baseline | None:
    data = fileio.load_json(project_cache(project, root) / "baseline.json")
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        return None
    try:
        return Baseline.from_json(data)
    except TypeError:
        return None


def prune_cache(root: Path | None = None, protect: Path | None = None, max_bytes: int = CACHE_MAX_BYTES) -> int:
    """Keep the whole hot-reload cache under max_bytes; the oldest projects' folders go first."""
    root = root or cache_root()
    try:
        dirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return 0
    return fileio.prune(dirs, keep=10_000, max_bytes=max_bytes, min_keep=1, protect=[protect] if protect else [])


# ---------------------------------------------------------------- msgpack (enough of it for task-store)

class MsgpackError(ValueError):
    pass


def mp_decode(b: bytes, i: int, depth: int = 0) -> tuple[Any, int]:
    """One msgpack value at b[i:] -> (value, next index). Bounded: raises MsgpackError on truncated input or
    nesting deeper than 64."""
    if depth > 64:
        raise MsgpackError("nested too deep")
    n = len(b)
    if i >= n:
        raise MsgpackError("truncated")
    c = b[i]
    i += 1

    def take(k: int) -> bytes:
        if i + k > n:
            raise MsgpackError("truncated")
        return b[i:i + k]

    def uint(k: int) -> int:
        return int.from_bytes(take(k), "big")

    if c <= 0x7F:
        return c, i
    if c >= 0xE0:
        return c - 256, i
    if 0xA0 <= c <= 0xBF:
        k = c & 0x1F
        return take(k).decode("utf-8", "replace"), i + k
    if 0x90 <= c <= 0x9F:
        return _mp_array(b, i, c & 0x0F, depth)
    if 0x80 <= c <= 0x8F:
        return _mp_map(b, i, c & 0x0F, depth)
    if c == 0xC0:
        return None, i
    if c == 0xC2:
        return False, i
    if c == 0xC3:
        return True, i
    if c in (0xD9, 0xDA, 0xDB):          # str 8/16/32
        w = {0xD9: 1, 0xDA: 2, 0xDB: 4}[c]
        k = uint(w)
        i += w
        return take(k).decode("utf-8", "replace"), i + k
    if c in (0xC4, 0xC5, 0xC6):          # bin 8/16/32
        w = {0xC4: 1, 0xC5: 2, 0xC6: 4}[c]
        k = uint(w)
        i += w
        return take(k), i + k
    if c in (0xC7, 0xC8, 0xC9):          # ext 8/16/32
        w = {0xC7: 1, 0xC8: 2, 0xC9: 4}[c]
        k = uint(w)
        i += w
        return ("ext", take(k + 1)), i + k + 1
    if 0xD4 <= c <= 0xD8:                # fixext
        k = 1 << (c - 0xD4)
        return ("ext", take(k + 1)), i + k + 1
    if c == 0xCA:
        return struct.unpack(">f", take(4))[0], i + 4
    if c == 0xCB:
        return struct.unpack(">d", take(8))[0], i + 8
    if 0xCC <= c <= 0xCF:
        k = 1 << (c - 0xCC)
        return uint(k), i + k
    if 0xD0 <= c <= 0xD3:
        k = 1 << (c - 0xD0)
        return int.from_bytes(take(k), "big", signed=True), i + k
    if c in (0xDC, 0xDD):
        w = 2 if c == 0xDC else 4
        k = uint(w)
        return _mp_array(b, i + w, k, depth)
    if c in (0xDE, 0xDF):
        w = 2 if c == 0xDE else 4
        k = uint(w)
        return _mp_map(b, i + w, k, depth)
    raise MsgpackError(f"bad type byte 0x{c:02x}")


def _mp_array(b: bytes, i: int, k: int, depth: int) -> tuple[list, int]:
    if k > len(b) - i:                   # every item is at least one byte
        raise MsgpackError("truncated")
    out = []
    for _ in range(k):
        v, i = mp_decode(b, i, depth + 1)
        out.append(v)
    return out, i


def _mp_map(b: bytes, i: int, k: int, depth: int) -> tuple[dict, int]:
    if 2 * k > len(b) - i:
        raise MsgpackError("truncated")
    out: dict = {}
    for _ in range(k):
        key, i = mp_decode(b, i, depth + 1)
        v, i = mp_decode(b, i, depth + 1)
        out[key if isinstance(key, (str, int, float, bool, type(None))) else repr(key)] = v
    return out, i


def _is_str_header(c: int) -> bool:
    return 0xA0 <= c <= 0xBF or c in (0xD9, 0xDA, 0xDB)


def swiftc_runs(data: bytes) -> list[list[str]]:
    """Every `"--", "<…>/swiftc", args…` run of msgpack strings in a task store: the driver invocations."""
    runs: list[list[str]] = []
    for m in re.finditer(rb"\xa2--", data):
        i = m.end()
        try:
            v, j = mp_decode(data, i)
        except MsgpackError:
            continue
        if not (isinstance(v, str) and v.endswith("/swiftc")):
            continue
        args = [v]
        i = j
        while i < len(data) and _is_str_header(data[i]) and len(args) < MAX_ARGS:
            try:
                v, i = mp_decode(data, i)
            except MsgpackError:
                break
            args.append(v)
        runs.append(args)
    return runs


def pick_compile_run(runs: list[list[str]], module: str) -> list[str] | None:
    """The run that compiles `module` (has -module-name <module> and -c)."""
    for args in runs:
        try:
            k = args.index("-module-name")
        except ValueError:
            continue
        if k + 1 < len(args) and args[k + 1] == module and "-c" in args:
            return args
    return None


# ---------------------------------------------------------------- the build's files

def _read(path: Path, cap: int = MAX_STORE) -> bytes:
    try:
        if path.stat().st_size > cap:
            raise no_baseline(f"{path.name} from the last build is unexpectedly large.")
        return path.read_bytes()
    except OSError:
        raise no_baseline("The last build's records are missing: build the app again.") from None


def build_data_dirs(project: Path) -> list[Path]:
    """The project's .xcbuilddata folders, newest first."""
    base = project / ".build/out/Intermediates.noindex/XCBuildData"
    try:
        dirs = [p for p in base.iterdir() if p.suffix == ".xcbuilddata" and (p / "task-store.msgpack").is_file()]
    except OSError:
        return []
    return sorted(dirs, key=lambda p: (p / "task-store.msgpack").stat().st_mtime, reverse=True)


def driver_args(project: Path, module: str) -> tuple[list[str], Path]:
    """The newest build's swiftc driver arguments for `module`, and the .xcbuilddata folder they came from."""
    dirs = build_data_dirs(project)
    if not dirs:
        raise no_baseline("There's no finished build of this project to reload: build the app first.")
    for d in dirs[:5]:
        args = pick_compile_run(swiftc_runs(_read(d / "task-store.msgpack")), module)
        if args:
            return args, d
    raise no_baseline(f"The last build has no compile step for the module {module}.")


def link_args(build_data: Path) -> list[str]:
    """The app's link command (swiftc … -emit-executable … -o …) from manifest.json."""
    try:
        manifest = json.loads(_read(build_data / "manifest.json"))
    except ValueError:
        raise no_baseline("The last build's records can't be read: build the app again.") from None
    found = []
    for key, cmd in (manifest.get("commands") or {}).items():
        args = cmd.get("args") if isinstance(cmd, dict) else None
        if ":Ld " in key and isinstance(args, list) and args and str(args[0]).endswith("swiftc") \
                and "-emit-executable" in args:
            found.append([str(a) for a in args])
    if not found:
        raise no_baseline("The last build has no app link step.")
    return found[0]


def output_file_map(args: list[str]) -> dict[str, dict]:
    try:
        path = Path(args[args.index("-output-file-map") + 1])
    except (ValueError, IndexError):
        return {}
    data = fileio.load_json(path, {})
    return data if isinstance(data, dict) else {}


def frontend_request(args: list[str]) -> list[str]:
    """The driver args with the build-wide flags dropped, asking for per-file frontend commands (-###)."""
    out: list[str] = []
    i, seen_cache = 1, False
    while i < len(args):
        x = args[i]
        if x in DROP_ONE or re.fullmatch(r"-j\d+", x):
            i += 1
            continue
        if x in DROP_TWO:
            i += 2
            continue
        if x == "-module-cache-path":
            if seen_cache:       # SwiftBuild passes a second one (the explicit-modules folder): keep the first
                i += 2
                continue
            seen_cache = True
        out.append(x)
        i += 1
    return [args[0], *out, "-c", "-disable-batch-mode", "-###"]


def parse_frontends(stdout: str) -> dict[str, list[str]]:
    """`swift-frontend -frontend -c -primary-file F …` lines -> {F: argv}."""
    out: dict[str, list[str]] = {}
    for line in stdout.splitlines():
        if "-primary-file" not in line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        k = parts.index("-primary-file") if "-primary-file" in parts else -1
        if k < 0 or k + 1 >= len(parts) or "-o" not in parts:
            continue
        out[parts[k + 1]] = _without_side_outputs(parts)
    return out


SIDE_OUTPUT = re.compile(r"-emit-[a-z-]+-path|-serialize-diagnostics-path|-index-store-path|-index-unit-output-path|"
                         r"-supplementary-output-file-map")


def _without_side_outputs(argv: list[str]) -> list[str]:
    """A frontend command without outputs besides -o (a patch compile must never write into the build's folders)."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        if SIDE_OUTPUT.fullmatch(argv[i]):
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


def patch_link_args(app_link: list[str]) -> list[str]:
    """The app's link command turned into a dylib link without inputs or output (patch.py adds them)."""
    out: list[str] = []
    i = 0
    a = app_link
    while i < len(a):
        x = a[i]
        if x.startswith("@") and x.endswith("LinkFileList"):
            i += 1
            continue
        if x == "-o":
            i += 2
            continue
        if x == "-emit-executable":
            i += 1
            continue
        if x == "-Xlinker" and i + 1 < len(a) and a[i + 1] in LINK_DROP_X1:
            i += 2
            continue
        if x == "-Xlinker" and i + 1 < len(a) and a[i + 1] in LINK_DROP_X2:
            i += 4
            continue
        out.append(x)
        i += 1
    out.insert(1, "-emit-library")
    out += ["-Xlinker", "-undefined", "-Xlinker", "dynamic_lookup"]
    if not any(out[j] == "-Xlinker" and out[j + 1] == "-interposable" for j in range(len(out) - 1)):
        out += ["-Xlinker", "-interposable"]
    return out


def link_command(link: list[str], objects: list[str], out: str, install_name: str) -> list[str]:
    return [*link, "-Xlinker", "-install_name", "-Xlinker", install_name, *objects, "-o", out]


def direct_ld(link: list[str], cwd: str | None = None) -> list[str] | None:
    """The ld64.lld argv the driver link ends in (driver -### -> clang, clang -### -> ld64.lld), with the
    OBJECTS / OUTPUT / INSTALL_NAME placeholders; None when it can't be worked out (patch.py then links through
    the driver)."""
    with tempfile.TemporaryDirectory(prefix="iosstudio-ld-") as tmp:
        obj, out = os.path.join(tmp, "patch.o"), os.path.join(tmp, "patch.dylib")
        Path(obj).write_bytes(b"")        # clang -### wants its inputs to exist
        cmd = link_command(link, [obj], out, NAME_SENTINEL)
        r = subproc.run_cmd([*cmd, "-###"], timeout=HASH_TIMEOUT, cwd=cwd, check=False)
        lines = [ln for ln in r.stdout.splitlines() if obj in ln] if r.returncode == 0 else []
        if not lines:
            return None
        try:
            clang = shlex.split(lines[-1])
        except ValueError:
            return None
        r = subproc.run_cmd([*clang, "-###"], timeout=HASH_TIMEOUT, cwd=cwd, check=False)
        if r.returncode != 0:
            return None
        for line in reversed((r.stdout + "\n" + r.stderr).splitlines()):
            if obj not in line or out not in line:
                continue
            try:
                ld = shlex.split(line)
            except ValueError:
                continue
            if ld and "ld" in Path(ld[0]).name and ld.count(obj) == 1:
                return [OBJECTS if a == obj else OUTPUT if a == out else INSTALL_NAME if a == NAME_SENTINEL else a
                        for a in ld]
    return None


def ld_command(ld: list[str], objects: list[str], out: str, install_name: str) -> list[str]:
    """A direct_ld() template filled in."""
    argv: list[str] = []
    for a in ld:
        if a == OBJECTS:
            argv += objects
        elif a == OUTPUT:
            argv.append(out)
        elif a == INSTALL_NAME:
            argv.append(install_name)
        else:
            argv.append(a)
    return argv


# ---------------------------------------------------------------- the app bundle

def find_app(project: Path, module: str) -> Path:
    """xtool/<App>.app: the one named for the module, else the only (or newest) .app there."""
    base = project / "xtool"
    named = base / f"{module}.app"
    if named.is_dir():
        return named
    try:
        apps = sorted((p for p in base.iterdir() if p.suffix == ".app" and p.is_dir()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        apps = []
    if not apps:
        raise no_baseline("The built app is missing: build the app first.")
    return apps[0]


def app_info(app: Path) -> tuple[Path, str | None]:
    """(executable, bundle id) from the .app's Info.plist."""
    try:
        info = plistlib.loads((app / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        raise no_baseline("The built app has no readable Info.plist: build the app again.") from None
    exe = app / str(info.get("CFBundleExecutable") or app.stem)
    if not exe.is_file():
        raise no_baseline("The built app has no executable: build the app again.")
    bid = info.get("CFBundleIdentifier")
    return exe, bid if isinstance(bid, str) else None


def team_of(app: Path) -> str | None:
    """The team id in the app's embedded.mobileprovision (None for an unsigned dev build)."""
    from iosstudio.features.projects.store import profile_from_bytes
    try:
        data = (app / "embedded.mobileprovision").read_bytes()
    except OSError:
        return None
    prof = profile_from_bytes(data) or {}
    team = prof.get("team")
    return team if isinstance(team, str) and team else None


def self_bound(binary: macho.MachO) -> int:
    """How many of the binary's own exported functions it calls through pointer entries (-interposable)."""
    own = binary.exported("__TEXT", "__text")
    return sum(1 for n in binary.indirect_names() if n in own)


# ---------------------------------------------------------------- capture

def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def capture_baseline(project: str | Path, module: str, cache_dir: Path | None = None) -> Baseline:
    """Read the last build of `project` and copy what patches need into the hot-reload cache (see the module doc).
    cache_dir is the hot-reload cache root (default ~/.cache/ios-studio/hot-reload)."""
    proj = Path(project).resolve()
    root = cache_dir or cache_root()
    args, build_data = driver_args(proj, module)
    ofm = output_file_map(args)
    app_link = link_args(build_data)
    app = find_app(proj, module)
    exe, bundle_id = app_info(app)
    try:
        binary = macho.MachO.load(exe)
    except macho.MachOError as exc:
        raise no_baseline("The built app's executable can't be read: build the app again.", str(exc)) from None

    cwd = _working_dir(args)
    r = subproc.run_cmd(frontend_request(args), timeout=HASH_TIMEOUT, cwd=cwd, check=False)
    if r.returncode != 0:
        raise no_baseline("The Swift compiler couldn't plan a patch for this project.", (r.stderr or r.stdout)[-2000:])
    frontends = parse_frontends(r.stdout)
    if not frontends:
        raise no_baseline("The Swift compiler couldn't plan a patch for this project.", r.stdout[-2000:])

    key = project_key(proj)
    dest = root / key
    dest.mkdir(parents=True, exist_ok=True)
    for sub in ("sources", "objects"):
        shutil.rmtree(dest / sub, ignore_errors=True)
    shutil.rmtree(dest / "patches", ignore_errors=True)   # patches of an older build don't apply to this one

    frontend: dict[str, list[str]] = {}
    objects: dict[str, str] = {}
    for src, argv in frontends.items():
        sp = Path(src)
        try:
            rel = str(sp.resolve().relative_to(proj))
        except ValueError:
            continue                     # generated files (resource accessors): compiled along, never patched
        frontend[rel] = argv
        if sp.is_file():
            _copy(sp, dest / "sources" / rel)
        obj = (ofm.get(src) or {}).get("object") if isinstance(ofm.get(src), dict) else None
        if obj and Path(obj).is_file():
            cached = dest / "objects" / Path(obj).name
            _copy(Path(obj), cached)
            objects[rel] = str(cached)
    if not frontend:
        raise no_baseline(f"The module {module} has no Swift files in this project.")
    _copy(exe, dest / "app.bin")

    link = patch_link_args(app_link)
    base = Baseline(
        project=str(proj), module=module, key=key, cache_dir=str(dest), swiftc=args[0], frontend=frontend,
        link=link, ld=direct_ld(link, cwd), app_binary=str(dest / "app.bin"), app_path=str(exe),
        bundle_id=bundle_id, team=team_of(app), built_at=exe.stat().st_mtime, captured_at=time.time(),
        objects=objects, interposable=self_bound(binary))
    fileio.save_json(dest / "baseline.json", base.to_json())
    os.utime(dest)                       # this project is the newest in the cache
    prune_cache(root, protect=dest)
    return base


def _working_dir(args: list[str]) -> str | None:
    try:
        wd = args[args.index("-working-directory") + 1]
    except (ValueError, IndexError):
        return None
    return wd if Path(wd).is_dir() else None
