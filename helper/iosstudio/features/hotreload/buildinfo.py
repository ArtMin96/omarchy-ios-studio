"""What a hot-reload patch needs from the last `xtool dev build` of a project, captured once as a Baseline.

xtool builds with SwiftBuild, which leaves everything in `<project>/.build/out/`:

  Intermediates.noindex/XCBuildData/<hash>.xcbuilddata/task-store.msgpack   the module's swiftc driver arguments
      (a run of msgpack strings "--", ".../swiftc", "-parse-as-library", …, with -module-name <Module>)
  …/manifest.json         the app's link command (a command whose key has ":Ld " and whose args[0] is swiftc)
  …/<Module>-OutputFileMap.json   where each source's object (.o) went

From those, one `swiftc … -###` call prints the per-file frontend commands (compile one file against the rest of
the module), and two more `-###` calls turn the patch link (the app's link as a dylib) into the plain ld64.lld
command it ends in (≈0.8 s instead of ≈2 s through the driver).

    snap = snapshot_sources(project)        # BEFORE the build: the watched files as the compiler will read them
    capture_baseline(project, module, cache_root=None, snapshot=snap, cancel=None) -> Baseline
                                            after the build; copies into the hot-reload cache:
      <cache_root>/<key>/baseline.json   the Baseline (compact JSON: one shared frontend argv + per-file changes)
                         sources/        the module's Swift files as built (from the snapshot; by project path)
                         objects/        their baseline .o files (<File>.o)
                         app.bin         the built app executable (xtool/<App>.app/<exe>)
                         patches/        written by patch.py (newest 10 kept)
    load_baseline(project, cache_root=None) -> Baseline | None

The snapshot is what makes a save during the build safe: the baseline holds the sources as they were when the
build started, so a file saved meanwhile differs from it and the next patch includes it (copying the live tree
after the build would make that save look built). The new baseline is written into a staging folder
(<cache_root>/.<key>-XXXX, the snapshot's) and swapped in only once it is complete, so a failed capture leaves the
previous one intact. Frontend files the snapshot missed (over its caps) are copied live; those changed since the
snapshot are listed in Baseline.late (the session asks for a Run).

key = the first 12 hex digits of sha1(project path); the whole cache is kept under 300 MB (oldest projects go
first; projects with a live hot reload session never). Anything missing fails with HelperError('no-baseline').
The `-###` driver calls run in their own process group, killed whole on a timeout or a cancel.
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
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
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
MAX_TOOL_OUTPUT = 16 * 1024 * 1024   # stdout / stderr read from one `-###` call
SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024  # source copies in one snapshot (files past it are copied after the build)
STAGE_STALE = 6 * 3600       # seconds after which a leftover staging folder (a killed session's) is deleted
BASELINE_VERSION = 2

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
    late: list[str] = field(default_factory=list)   # copied after the build and changed since it started
    working_dir: str | None = None        # the driver's -working-directory (the frontend commands' cwd)
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
        """baseline.json: `frontend` packed (each file's argv lists every module file: O(files²) unpacked)."""
        out = asdict(self)
        out["frontendShared"], out["frontendFiles"] = pack_frontends(out.pop("frontend"))
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Baseline":
        """to_json() back (also takes a plain `frontend`). TypeError / ValueError on a malformed document."""
        data = dict(data)
        if "frontendFiles" in data:
            data["frontend"] = unpack_frontends(data.pop("frontendShared", None), data.pop("frontendFiles"))
        names = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in names})


MAX_PACK_DIFFS = 8           # a file whose argv differs from the shared one in more places is stored whole


def pack_frontends(frontend: dict[str, list[str]]) -> tuple[list[str], dict[str, dict]]:
    """(shared argv, {file: {"primary": k, "from": j, "set": {pos: arg}} | {"argv": argv}}). The frontend commands
    of one module list the same inputs and flags; they differ in which input follows -primary-file (at k; `from`:
    where that input sits in the shared argv when the driver moved it) and in their -o, so each file stores that."""
    shared: list[str] | None = None
    files: dict[str, dict] = {}
    for rel, argv in frontend.items():
        k = argv.index("-primary-file") if "-primary-file" in argv else -1
        if k < 0 or k + 1 >= len(argv):
            files[rel] = {"argv": list(argv)}
            continue
        flat = argv[:k] + argv[k + 1:]
        if shared is None:
            shared = flat
        if len(flat) != len(shared):
            files[rel] = {"argv": list(argv)}
            continue
        j = shared.index(flat[k]) if flat[k] in shared else k
        base = _moved(shared, j, k)
        diff = {str(i): a for i, (a, b) in enumerate(zip(flat, base)) if a != b}
        files[rel] = {"argv": list(argv)} if len(diff) > MAX_PACK_DIFFS else \
            {"primary": k, **({"from": j} if j != k else {}), "set": diff}
    return shared or [], files


def _moved(items: list[str], a: int, b: int) -> list[str]:
    """items with the element at a moved to index b."""
    if a == b:
        return list(items)
    out = items[:a] + items[a + 1:]
    out.insert(b, items[a])
    return out


def unpack_frontends(shared: Any, files: Any) -> dict[str, list[str]]:
    if not isinstance(files, dict) or not isinstance(shared, list):
        raise ValueError("bad frontend table")
    out: dict[str, list[str]] = {}
    for rel, f in files.items():
        if "argv" in f:
            out[rel] = [str(a) for a in f["argv"]]
            continue
        k, j = int(f["primary"]), int(f.get("from", f["primary"]))
        if not (0 <= k < len(shared) and 0 <= j < len(shared)):
            raise ValueError("bad primary index")
        flat = _moved([str(a) for a in shared], j, k)
        for i, a in f.get("set", {}).items():
            flat[int(i)] = str(a)
        out[rel] = [*flat[:k], "-primary-file", *flat[k:]]
    return out


# ---------------------------------------------------------------- cache folders

def cache_root() -> Path:
    return appdirs.cache_dir() / "hot-reload"


def project_key(project: str | Path) -> str:
    return hashlib.sha1(str(Path(project)).encode()).hexdigest()[:12]


def project_cache(project: str | Path, root: Path | None = None) -> Path:
    return (root or cache_root()) / project_key(project)


def load_baseline(project: str | Path, root: Path | None = None) -> Baseline | None:
    data = fileio.load_json(project_cache(project, Path(root) if root else None) / "baseline.json")
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        return None
    try:
        return Baseline.from_json(data)
    except (TypeError, ValueError, KeyError, AttributeError, IndexError):
        return None


def _live_session_keys() -> list[str]:
    try:
        from iosstudio.features.hotreload import state
        return state.live_keys()
    except Exception:  # noqa: BLE001 - pruning never fails a capture
        return []


def prune_cache(root: Path | None = None, protect: Path | Iterable[Path] | None = None,
                max_bytes: int = CACHE_MAX_BYTES) -> int:
    """Keep the whole hot-reload cache under max_bytes; the oldest projects' folders go first. Never deleted:
    `protect` and the folder of every project with a live hot reload session. Staging folders (.<key>-…) older
    than STAGE_STALE are removed."""
    root = root or cache_root()
    keep = [protect] if isinstance(protect, Path) else list(protect or [])
    keep += [root / k for k in _live_session_keys()]
    try:
        entries = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return 0
    now = time.time()
    for p in entries:
        if p.name.startswith("."):
            try:
                if now - p.stat().st_mtime > STAGE_STALE:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    dirs = [p for p in entries if not p.name.startswith(".")]
    return fileio.prune(dirs, keep=10_000, max_bytes=max_bytes, min_keep=1, protect=keep)


# ---------------------------------------------------------------- the sources before the build

@dataclass
class SourceSnapshot:
    """The project's watched files when the build started (snapshot_sources). capture_baseline turns its folder into
    the baseline; discard_snapshot removes it when the start failed before that."""
    project: str
    dir: str                                  # <cache_root>/.<key>-XXXX (staging folder)
    digests: dict[str, str | None]            # project-relative -> fswatch.file_digest(), every watched file
    copied: set[str] = field(default_factory=set)   # files with a copy under dir/sources/
    started: float = 0.0                      # time.time() when it was taken


def snapshot_sources(project: str | Path, cache_dir: Path | None = None) -> SourceSnapshot:
    """Read every watched file (fswatch.scan_files: root files + Sources/) once: its digest, and for Swift files
    under Sources/ a copy (≤ SNAPSHOT_MAX_BYTES in all), taken from the same bytes. OSError if the cache folder
    can't be made."""
    from iosstudio.features.watch import fswatch
    proj = Path(project).resolve()
    root = cache_dir or cache_root()
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{project_key(proj)}-", dir=root))
    snap = SourceSnapshot(project=str(proj), dir=str(stage), digests={}, started=time.time())
    total = 0
    try:
        for rel in fswatch.scan_files(proj, fswatch.PRIME_MAX_FILES):
            path = proj / rel
            try:
                size = path.stat().st_size
                if size > fswatch.HASH_MAX:
                    snap.digests[rel] = fswatch.file_digest(path)
                    continue
                data = path.read_bytes()
            except OSError:
                snap.digests[rel] = None
                continue
            snap.digests[rel] = hashlib.sha1(data).hexdigest()
            if rel.startswith("Sources/") and rel.endswith(".swift") and total + len(data) <= SNAPSHOT_MAX_BYTES:
                dst = stage / "sources" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
                snap.copied.add(rel)
                total += len(data)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return snap


def discard_snapshot(snap: SourceSnapshot | None) -> None:
    """Remove a snapshot that never became a baseline (after a capture its folder is gone: nothing happens)."""
    if snap and Path(snap.dir).name.startswith("."):
        shutil.rmtree(snap.dir, ignore_errors=True)


# ---------------------------------------------------------------- running the driver

def run_tool(argv: list[str], cwd: str | None = None, cancel: Callable[[], bool] | None = None,
             timeout: float = HASH_TIMEOUT) -> subprocess.CompletedProcess:
    """argv to its end in its own process group, stdout and stderr through temp files (each ≤ MAX_TOOL_OUTPUT
    read): a frontend line lists every module file, far longer than a streamed line may be. cancel() (checked every
    0.2 s) kills the group and raises subproc.Cancelled; past `timeout` the group is killed and returncode is 124.
    A missing program is 127, another OSError 126 (like run_cmd(check=False))."""
    argv = [str(a) for a in argv]
    if cancel and cancel():
        raise subproc.Cancelled()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, cwd=cwd,
                                    env=subproc.tool_env("plain"), start_new_session=True)
        except FileNotFoundError:
            return subprocess.CompletedProcess(argv, 127, "", "not found")
        except OSError as exc:
            return subprocess.CompletedProcess(argv, 126, "", str(exc))
        deadline = time.monotonic() + timeout
        rc: int | None = None
        try:
            while rc is None:
                try:
                    rc = proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    if cancel and cancel():
                        raise subproc.Cancelled() from None
                    if time.monotonic() >= deadline:
                        rc = 124
        finally:
            subproc.kill_group(proc.pid, 0)      # on a cancel or timeout, and anything it left behind
            subproc.kill_proc(proc)

        def text(fh) -> str:
            fh.seek(0)
            return fh.read(MAX_TOOL_OUTPUT).decode("utf-8", "replace")
        return subprocess.CompletedProcess(argv, rc, text(out), "timeout" if rc == 124 else text(err))


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


def link_args(build_data: Path, module: str | None = None, exe_name: str | None = None) -> list[str]:
    """The app's link command (swiftc … -emit-executable … -o …) from manifest.json. With several (an app
    extension links its own executable): the one of the app's target (key `target-<module>-App:`), else the one
    whose -o is the app's executable (`exe_name`, or `<module>-App`); else the first."""
    try:
        manifest = json.loads(_read(build_data / "manifest.json"))
    except ValueError:
        raise no_baseline("The last build's records can't be read: build the app again.") from None
    found: list[tuple[str, list[str]]] = []
    for key, cmd in (manifest.get("commands") or {}).items():
        args = cmd.get("args") if isinstance(cmd, dict) else None
        if ":Ld " in key and isinstance(args, list) and args and str(args[0]).endswith("swiftc") \
                and "-emit-executable" in args:
            found.append((str(key), [str(a) for a in args]))
    if not found:
        raise no_baseline("The last build has no app link step.")
    if len(found) == 1 or not module:
        return found[0][1]

    def out_name(args: list[str]) -> str:
        return Path(args[args.index("-o") + 1]).name if "-o" in args[:-1] else ""
    for match in (lambda k, a: f"target-{module}-App:" in k,
                  lambda k, a: bool(exe_name) and out_name(a) == exe_name,
                  lambda k, a: out_name(a) == f"{module}-App"):
        hits = [a for k, a in found if match(k, a)]
        if hits:
            return hits[0]
    return found[0][1]


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


def direct_ld(link: list[str], cwd: str | None = None, cancel: Callable[[], bool] | None = None) -> list[str] | None:
    """The ld64.lld argv the driver link ends in (driver -### -> clang, clang -### -> ld64.lld), with the
    OBJECTS / OUTPUT / INSTALL_NAME placeholders; None when it can't be worked out (patch.py then links through
    the driver)."""
    with tempfile.TemporaryDirectory(prefix="iosstudio-ld-") as tmp:
        obj, out = os.path.join(tmp, "patch.o"), os.path.join(tmp, "patch.dylib")
        Path(obj).write_bytes(b"")        # clang -### wants its inputs to exist
        cmd = link_command(link, [obj], out, NAME_SENTINEL)
        r = run_tool([*cmd, "-###"], cwd, cancel)
        lines = [ln for ln in r.stdout.splitlines() if obj in ln] if r.returncode == 0 else []
        if not lines:
            return None
        try:
            clang = shlex.split(lines[-1])
        except ValueError:
            return None
        r = run_tool([*clang, "-###"], cwd, cancel)
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


def capture_baseline(project: str | Path, module: str, cache_dir: Path | None = None, *,
                     snapshot: SourceSnapshot | None = None, cancel: Callable[[], bool] | None = None) -> Baseline:
    """Read the last build of `project` and copy what patches need into the hot-reload cache (see the module doc).
    cache_dir is the hot-reload cache root (default ~/.cache/ios-studio/hot-reload). snapshot: the sources from
    before the build (snapshot_sources), used as the built ones; without it the live files are copied. cancel():
    the driver calls are killed and subproc.Cancelled raised. The previous baseline is replaced only on success."""
    proj = Path(project).resolve()
    root = cache_dir or cache_root()
    args, build_data = driver_args(proj, module)
    ofm = output_file_map(args)
    app = find_app(proj, module)
    exe, bundle_id = app_info(app)
    app_link = link_args(build_data, module, exe.name)
    try:
        binary = macho.MachO.load(exe)
    except macho.MachOError as exc:
        raise no_baseline("The built app's executable can't be read: build the app again.", str(exc)) from None

    cwd = _working_dir(args)
    r = run_tool(frontend_request(args), cwd, cancel)
    if r.returncode != 0:
        raise no_baseline("The Swift compiler couldn't plan a patch for this project.", (r.stderr or r.stdout)[-2000:])
    frontends = parse_frontends(r.stdout)
    if not frontends:
        raise no_baseline("The Swift compiler couldn't plan a patch for this project.", r.stdout[-2000:])

    key = project_key(proj)
    dest = root / key
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(snapshot.dir) if snapshot and Path(snapshot.dir).is_dir() else \
        Path(tempfile.mkdtemp(prefix=f".{key}-", dir=root))
    try:
        frontend: dict[str, list[str]] = {}
        objects: dict[str, str] = {}
        late: list[str] = []
        for src, argv in frontends.items():
            sp = Path(src)
            try:
                rel = str(sp.resolve().relative_to(proj))
            except ValueError:
                continue                     # generated files (resource accessors): compiled along, never patched
            frontend[rel] = argv
            if not (snapshot and rel in snapshot.copied) and sp.is_file():
                _copy(sp, stage / "sources" / rel)
                if snapshot and _changed_since(sp, snapshot.started):
                    late.append(rel)
            obj = (ofm.get(src) or {}).get("object") if isinstance(ofm.get(src), dict) else None
            if obj and Path(obj).is_file():
                _copy(Path(obj), stage / "objects" / Path(obj).name)
                objects[rel] = str(dest / "objects" / Path(obj).name)
        if not frontend:
            raise no_baseline(f"The module {module} has no Swift files in this project.")
        _drop_unlisted(stage / "sources", set(frontend))
        _copy(exe, stage / "app.bin")

        link = patch_link_args(app_link)
        base = Baseline(
            project=str(proj), module=module, key=key, cache_dir=str(dest), swiftc=args[0], frontend=frontend,
            link=link, ld=direct_ld(link, cwd, cancel), app_binary=str(dest / "app.bin"), app_path=str(exe),
            bundle_id=bundle_id, team=team_of(app), built_at=exe.stat().st_mtime, captured_at=time.time(),
            objects=objects, interposable=self_bound(binary), late=sorted(late), working_dir=cwd)
        fileio.atomic_write(stage / "baseline.json", json.dumps(base.to_json(), separators=(",", ":")))
        _swap_in(stage, dest)                # patches of an older build don't apply to this one: they go with it
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    os.utime(dest)                       # this project is the newest in the cache
    prune_cache(root, protect=dest)
    return base


def _changed_since(path: Path, t: float) -> bool:
    try:
        return path.stat().st_mtime >= t
    except OSError:
        return True


def _drop_unlisted(sources: Path, keep: set[str]) -> None:
    """Snapshot copies of files outside the module (other targets) aren't part of the baseline."""
    if not sources.is_dir():
        return
    for p in sorted(sources.rglob("*"), reverse=True):
        rel = str(p.relative_to(sources))
        try:
            if p.is_file() and rel not in keep:
                p.unlink()
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass


def _swap_in(stage: Path, dest: Path) -> None:
    """Replace dest by the complete stage folder (two renames; the old one is put back if the second fails)."""
    old = None
    if dest.exists():
        old = dest.with_name(f".{dest.name}-old-{os.getpid()}")
        shutil.rmtree(old, ignore_errors=True)
        os.rename(dest, old)
    try:
        os.rename(stage, dest)
    except OSError:
        if old:
            os.rename(old, dest)
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)


def _working_dir(args: list[str]) -> str | None:
    try:
        wd = args[args.index("-working-directory") + 1]
    except (ValueError, IndexError):
        return None
    return wd if Path(wd).is_dir() else None
