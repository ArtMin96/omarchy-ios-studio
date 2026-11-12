"""Make one hot-reload patch: compile the changed files, link them into a dylib, sign it (stdlib only).

    changes = source_changes(baseline)                       # which Swift files differ from the build
    result = make_patch(baseline, None, seq, cancel=…, on_step=…)   # -> PatchResult (or raises PatchError)
    warm_up(baseline)                                        # once after the baseline: fills the module cache

make_patch is cumulative: it takes every file that differs from the baseline (or the files given), so the newest
patch alone holds every change. Steps, each timed in ms in PatchResult.steps:

  check     structural_blockers() per file (before compiling); after compiling, the opaque-type check per file and,
            after linking, the interposable check (both counted here too)
  compile   each file's frontend command (from the baseline), output in a work folder; in parallel (≤4)
  link      one dylib `patch-<seq>.dylib` (install name @rpath/patch-<seq>.dylib): ld64.lld directly (≈0.8 s),
            falling back to the swiftc driver (≈2 s) if that fails
  sign      signing.sign_dylib() with the baseline's team

The dylib lands in <cache>/<key>/patches/ (newest 10 kept). Errors are PatchError (a HelperError) with the
`lastError` fields of the session snapshot: code (unsupported-change, new-file, compile-failed, link-failed,
not-interposable, sign-failed, not-signed-in, no-baseline), message, fix, file, line, blockers (≤10) with
blockersTotal, diagnostics (≤5, errors first) with diagnosticsTotal. `cancel()` returning True stops at the next
step or while a tool runs (killed; subproc.Cancelled raised); the work folder is removed either way.

Every tool runs in the build's working directory (work_dir()), in its own process group with a reaper and
PR_SET_PDEATHSIG, so a compile or link never outlives a killed session. A changed file whose baseline object is
missing is refused (its opaque types can't be checked). New files are Swift files under the module's folders that
aren't in the build and were created or moved there after it (older ones are excluded on purpose: `exclude:`,
resources); hidden folders, nested packages and Package.swift manifests are never sources.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from iosstudio.core import fileio, subproc
from iosstudio.core.output import HelperError, fix
from iosstudio.features import diagnostics
from iosstudio.features.hotreload import buildinfo, checks, macho, signing
from iosstudio.features.hotreload.buildinfo import Baseline

COMPILE_TIMEOUT = 120
LINK_TIMEOUT = 60
WARM_UP_TIMEOUT = 300       # the first compile builds the implicit module cache (12 s idle, minutes on a busy machine)
KEEP_PATCHES = 10
MAX_DIAGNOSTICS = 5
MAX_OUTPUT_LINES = 2000
MAX_SOURCES = 5000
MAX_PARALLEL = 4

RunFn = Callable[[list[str], float], tuple[int, list[str]]]


def restart_fix() -> dict:
    return fix("Run again", "projects", action="hot-reload-restart")


class PatchError(HelperError):
    """A HelperError with the extra fields of a hot-reload lastError."""

    def __init__(self, code: str, message: str, fix_: dict | None = None, *, file: str | None = None,
                 line: int | None = None, blockers: list[dict] | None = None, diagnostics_: list[dict] | None = None,
                 detail: str | None = None):
        super().__init__(code, message, fix_, detail)
        self.file = file
        self.line = line
        self.blockers_total = len(blockers or [])
        self.blockers = (blockers or [])[:checks.MAX_BLOCKERS]
        self.diagnostics_total = len(diagnostics_ or [])
        self.diagnostics = (diagnostics_ or [])[:MAX_DIAGNOSTICS]

    def to_json(self) -> dict:
        out = super().to_json()
        if self.file:
            out["file"] = self.file
        if self.line:
            out["line"] = self.line
        if self.blockers:
            out["blockers"] = self.blockers
            out["blockersTotal"] = self.blockers_total
        if self.diagnostics:
            out["diagnostics"] = self.diagnostics
            out["diagnosticsTotal"] = self.diagnostics_total
        return out


@dataclass
class SourceChanges:
    changed: list[str] = field(default_factory=list)    # project-relative, in the baseline and different now
    added: list[str] = field(default_factory=list)      # Swift files in the module's folders made after the build
    removed: list[str] = field(default_factory=list)    # baseline files that are gone

    @property
    def empty(self) -> bool:
        return not (self.changed or self.added or self.removed)


@dataclass
class PatchResult:
    seq: int
    dylib: str                     # <cache>/<key>/patches/patch-<seq>.dylib
    files: list[str]               # project-relative sources in the patch
    bytes: int
    steps: dict[str, int]          # ms: check, compile, link, sign
    link_mode: str                 # "ld" (ld64.lld directly) or "driver"
    swappable: list[str]           # functions the app will call through the patch
    missing: list[str]             # functions the app defines but calls directly (their change won't show)
    signed: bool

    def to_json(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- what changed

def module_roots(baseline: Baseline) -> list[Path]:
    """The folders the module's Swift files live in (Sources/<Target>/ for SwiftPM)."""
    proj = Path(baseline.project)
    roots: set[Path] = set()
    for rel in baseline.frontend:
        parts = Path(rel).parts
        roots.add(proj.joinpath(*parts[:2]) if len(parts) > 2 and parts[0] == "Sources" else (proj / rel).parent)
    return sorted(roots)


def source_changes(baseline: Baseline) -> SourceChanges:
    proj = Path(baseline.project)
    out = SourceChanges()
    known = set(baseline.frontend)
    for rel in sorted(known):
        cur = proj / rel
        try:
            now = cur.read_bytes()
        except OSError:
            out.removed.append(rel)
            continue
        try:
            then = baseline.source_copy(rel).read_bytes()
        except OSError:
            then = None
        if now != then:
            out.changed.append(rel)
    added: set[str] = set()
    for path in _module_swift_files(baseline):
        rel = str(path.relative_to(proj))
        if rel in known or rel in added:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        # A file that was there at build time but wasn't compiled is excluded on purpose (`exclude:`, a resource).
        # ctime too: a file moved in keeps its mtime but gets a new ctime.
        if max(st.st_mtime, st.st_ctime) > baseline.built_at:
            added.add(rel)
    out.added = sorted(added)
    return out


MANIFEST = re.compile(r"^Package(@swift-[\d.]+)?\.swift$")


def _module_swift_files(baseline: Baseline) -> list[Path]:
    """The Swift files under the module's folders (≤ MAX_SOURCES): never in hidden folders (.build, .iosstudio,
    .git, .swiftpm), nested packages (a folder with its own Package.swift) or Package.swift manifests."""
    proj = Path(baseline.project)
    found: list[Path] = []
    for root in module_roots(baseline):
        if not root.is_dir():
            continue
        for dirpath, dirs, names in os.walk(root):
            here = Path(dirpath)
            if here not in (proj, root) and "Package.swift" in names:
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for n in sorted(names):
                if n.endswith(".swift") and not n.startswith(".") and not MANIFEST.match(n):
                    found.append(here / n)
                    if len(found) >= MAX_SOURCES:
                        return found
    return found


# ---------------------------------------------------------------- running tools

def work_dir(baseline: Baseline) -> str | None:
    """The working directory the build ran its compiles in (relative paths in the captured commands are relative
    to it): the baseline's, else the one in a frontend command (`-Xcc -working-directory -Xcc D`,
    `-file-compilation-dir D`), else the project folder."""
    wd = getattr(baseline, "working_dir", None)
    if wd and Path(wd).is_dir():
        return str(wd)
    for argv in baseline.frontend.values():
        for i, a in enumerate(argv[:-1]):
            if a == "-working-directory":
                cand = argv[i + 2] if argv[i + 1] == "-Xcc" and i + 2 < len(argv) else argv[i + 1]
            elif a == "-file-compilation-dir":
                cand = argv[i + 1]
            else:
                continue
            if Path(cand).is_dir():
                return cand
        break                  # every file's command has the same flags
    return baseline.project if Path(baseline.project).is_dir() else None


def _runner(cancel: Callable[[], bool] | None, cwd: str | None = None) -> RunFn:
    def run(argv: list[str], timeout: float) -> tuple[int, list[str]]:
        lines: list[str] = []

        def on_line(text: str) -> None:
            if len(lines) < MAX_OUTPUT_LINES:
                lines.append(text)

        def on_tick() -> None:
            if cancel and cancel():
                raise subproc.Cancelled()
        try:
            # own process group + reaper + PDEATHSIG: a compile never outlives a killed session
            rc = subproc.Runner(on_line=on_line, on_tick=on_tick).run(
                argv, cwd=cwd, timeout=timeout, env=subproc.tool_env("plain"), preexec_fn=subproc.pdeathsig_term,
                reaper=True)
        except FileNotFoundError:
            raise HelperError("missing-tool", f"{Path(argv[0]).name} isn't installed yet.", fix("Open Setup", "setup"))
        return rc, lines
    return run


def _check_cancel(cancel: Callable[[], bool] | None) -> None:
    if cancel and cancel():
        raise subproc.Cancelled()


def frontend_command(argv: list[str], out: str) -> list[str]:
    cmd = list(argv)
    k = cmd.index("-o")
    cmd[k + 1] = out
    return cmd


def _diagnostics(lines: list[str]) -> list[dict]:
    """Every diagnostic, errors first (PatchError keeps MAX_DIAGNOSTICS and the total)."""
    found = diagnostics.parse(lines, failed=True, demangler=None)
    errors = [d for d in found if d.get("severity") == "error"]
    rest = [d for d in found if d.get("severity") != "error"]
    return errors + rest


def _rel(baseline: Baseline, path: str | None) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(Path(baseline.project)))
    except (ValueError, OSError):
        return path


# ---------------------------------------------------------------- the app binary (cached: read once per session)

_app_cache: dict[tuple[str, float, int], macho.MachO] = {}
_app_lock = threading.Lock()


def app_binary(baseline: Baseline) -> macho.MachO:
    path = Path(baseline.app_binary)
    try:
        st = path.stat()
    except OSError:
        raise buildinfo.no_baseline("The copy of the built app is missing: run the app again.") from None
    key = (str(path), st.st_mtime, st.st_size)
    with _app_lock:
        if key not in _app_cache:
            _app_cache.clear()
            try:
                _app_cache[key] = macho.MachO.load(path)
            except macho.MachOError as exc:
                raise buildinfo.no_baseline("The copy of the built app can't be read: run the app again.",
                                            str(exc)) from None
        return _app_cache[key]


# ---------------------------------------------------------------- the patch

def make_patch(baseline: Baseline, changed_files: list[str] | None, seq: int, out_dir: Path | None = None, *,
               cancel: Callable[[], bool] | None = None, on_step: Callable[[str], None] | None = None,
               run: RunFn | None = None, sign: bool = True, data_dir: Path | None = None) -> PatchResult:
    """Build patch `seq` from the files that differ from the baseline (or `changed_files`, project-relative or
    absolute). See the module doc for the steps and errors."""
    run = run or _runner(cancel, work_dir(baseline))
    step = on_step or (lambda _name: None)
    out_dir = Path(out_dir) if out_dir else baseline.patches_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    steps = {"check": 0, "compile": 0, "link": 0, "sign": 0}
    proj = Path(baseline.project)

    # ---- which files
    changes = source_changes(baseline)
    if changed_files is None:
        if changes.added:
            raise PatchError("new-file", f"{Path(changes.added[0]).name} is a new file: new files need a full build.",
                             restart_fix(), file=changes.added[0],
                             blockers=[_file_blocker("new-file", r) for r in changes.added])
        if changes.removed:
            raise PatchError("unsupported-change", f"{Path(changes.removed[0]).name} was deleted: removing files "
                             "needs a full build.", restart_fix(), file=changes.removed[0],
                             blockers=[_file_blocker("removed-file", r) for r in changes.removed])
        files = changes.changed
    else:
        files = []
        for f in changed_files:
            rel = _rel(baseline, str(proj / f) if not os.path.isabs(f) else f) or f
            if rel not in baseline.frontend:
                raise PatchError("new-file", f"{Path(rel).name} is a new file: new files need a full build.",
                                 restart_fix(), file=rel, blockers=[_file_blocker("new-file", rel)])
            if rel not in files:
                files.append(rel)
    if not files:
        raise HelperError("no-changes", "Nothing changed since the app was built.")

    # ---- check: the structure of each file
    step("check")
    t0 = time.monotonic()
    sources: dict[str, str] = {}
    blockers: list[dict] = []
    unparsed: list[dict] = []
    for rel in files:
        try:
            new_src = (proj / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise PatchError("unsupported-change", f"{Path(rel).name} can't be read.", restart_fix(), file=rel)
        try:
            old_src = baseline.source_copy(rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise buildinfo.no_baseline("The copy of the built sources is missing: run the app again.") from None
        sources[rel] = new_src
        for b in checks.structural_blockers(old_src, new_src, rel):
            (unparsed if b["kind"] == "unparsed" else blockers).append(b)
    steps["check"] += _ms(t0)
    if blockers:
        raise _unsupported(blockers)
    unchecked = [rel for rel in files if not _baseline_object(baseline, rel)]
    if unchecked:      # its opaque types can't be compared: refuse rather than risk a body of another type
        raise _unchecked(unchecked)
    _check_cancel(cancel)

    work = Path(tempfile.mkdtemp(prefix=f".patch-{seq}-", dir=out_dir))
    try:
        # ---- compile
        step("compile")
        t0 = time.monotonic()
        objects = _compile(baseline, files, work, run)
        steps["compile"] = _ms(t0)
        if unparsed:          # it compiles, but the scanner couldn't follow it: don't guess
            raise _unsupported(unparsed)
        _check_cancel(cancel)

        # ---- check: opaque result types
        t0 = time.monotonic()
        for rel, obj in zip(files, objects):
            base_o = _baseline_object(baseline, rel)
            if not base_o:
                raise _unchecked([rel])
            try:
                blockers += checks.opaque_blockers(base_o, obj, sources[rel], rel)
            except macho.MachOError as exc:
                raise PatchError("unsupported-change", f"The compiled {Path(rel).name} can't be checked.",
                                 restart_fix(), file=rel, detail=str(exc)) from None
        steps["check"] += _ms(t0)
        if blockers:
            raise _unsupported(blockers)

        # ---- link
        step("link")
        t0 = time.monotonic()
        tmp_dylib = work / f"patch-{seq}.dylib"
        link_mode = _link(baseline, objects, tmp_dylib, seq, run)
        steps["link"] = _ms(t0)
        _check_cancel(cancel)

        # ---- check: can the app's calls be redirected?
        t0 = time.monotonic()
        try:
            patch_bin = macho.MachO.load(tmp_dylib)
        except macho.MachOError as exc:
            raise PatchError("link-failed", "The linked patch can't be read.", restart_fix(), detail=str(exc)) from None
        swap = checks.require_interposable(patch_bin, app_binary(baseline))
        steps["check"] += _ms(t0)

        # ---- sign
        signed = False
        if sign:
            step("sign")
            t0 = time.monotonic()
            signing.sign_dylib(tmp_dylib, baseline.team, data_dir)
            steps["sign"] = _ms(t0)
            signed = True
        _check_cancel(cancel)

        final = out_dir / f"patch-{seq}.dylib"
        os.replace(tmp_dylib, final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    prune_patches(out_dir)
    return PatchResult(seq=seq, dylib=str(final), files=files, bytes=final.stat().st_size, steps=steps,
                       link_mode=link_mode, swappable=swap.swappable, missing=swap.missing, signed=signed)


def _ms(t0: float) -> int:
    return int(round((time.monotonic() - t0) * 1000))


def _file_blocker(kind: str, rel: str) -> dict:
    verb = "Adds" if kind == "new-file" else "Removes"
    return {"kind": kind, "file": rel, "message": f"{verb} the file {Path(rel).name}."}


def _baseline_object(baseline: Baseline, rel: str) -> str | None:
    base_o = baseline.objects.get(rel)
    return base_o if base_o and Path(base_o).is_file() else None


def _unchecked(files: list[str]) -> PatchError:
    """Changed files without a baseline object: the opaque-type check can't run, so the patch is refused."""
    more = f" (and {len(files) - 1} more)" if len(files) > 1 else ""
    blockers = [{"kind": "unchecked", "file": rel,
                 "message": f"Can't check {Path(rel).name} against the running app."} for rel in files]
    return PatchError("unsupported-change", f"Can't check {Path(files[0]).name}{more} against the running app. "
                      "Press Run to rebuild.", restart_fix(), file=files[0], blockers=blockers)


def _unsupported(blockers: list[dict]) -> PatchError:
    first = blockers[0]
    more = f" (and {len(blockers) - 1} more)" if len(blockers) > 1 else ""
    return PatchError("unsupported-change", f"{first['message']}{more} This change needs a full build.",
                      restart_fix(), file=first.get("file"), line=first.get("line"), blockers=blockers)


def _compile(baseline: Baseline, files: list[str], work: Path, run: RunFn) -> list[str]:
    def one(rel: str) -> tuple[str, int, list[str]]:
        out = str(work / f"{Path(rel).stem}.o")
        rc, lines = run(frontend_command(baseline.frontend[rel], out), COMPILE_TIMEOUT)
        return out, rc, lines

    if len(files) == 1:
        results = [one(files[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(files))) as pool:
            results = list(pool.map(one, files))
    for rel, (out, rc, lines) in zip(files, results):
        if rc != 0 or not Path(out).is_file():
            diags = _diagnostics(lines)
            first = next((d for d in diags if d.get("severity") == "error"), diags[0] if diags else None)
            msg = f"{Path(rel).name} doesn't compile"
            msg += f": {first['message']}" if first else "."
            raise PatchError("compile-failed", msg, None, file=_rel(baseline, first.get("file")) if first else rel,
                             line=first.get("line") if first else None, diagnostics_=diags,
                             detail="\n".join(lines[-40:]))
    return [r[0] for r in results]


def _link(baseline: Baseline, objects: list[str], out: Path, seq: int, run: RunFn) -> str:
    install = f"@rpath/patch-{seq}.dylib"
    lines: list[str] = []
    if baseline.ld:
        rc, lines = run(buildinfo.ld_command(baseline.ld, objects, str(out), install), LINK_TIMEOUT)
        if rc == 0 and out.is_file():
            return "ld"
        out.unlink(missing_ok=True)
    rc, lines = run(buildinfo.link_command(baseline.link, objects, str(out), install), LINK_TIMEOUT)
    if rc == 0 and out.is_file():
        return "driver"
    diags = _diagnostics(lines)
    first = diags[0] if diags else None
    raise PatchError("link-failed", "The patch couldn't be linked" + (f": {first['message']}" if first else "."),
                     restart_fix(), diagnostics_=diags, detail="\n".join(lines[-40:]))


def prune_patches(out_dir: Path, keep: int = KEEP_PATCHES) -> int:
    try:
        dylibs = [p for p in out_dir.iterdir() if p.name.startswith("patch-") and p.suffix == ".dylib"]
    except OSError:
        return 0
    removed = fileio.prune(dylibs, keep=keep, min_keep=1)
    now = time.time()
    for p in out_dir.glob(".patch-*"):       # work folders a killed helper left behind
        try:
            if now - p.stat().st_mtime > 3600:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    return removed


def warm_up(baseline: Baseline, *, cancel: Callable[[], bool] | None = None, run: RunFn | None = None,
            timeout: float = WARM_UP_TIMEOUT) -> float | None:
    """Compile one file to a throwaway output so the implicit module cache is warm (≈12 s cold, then ≈0.5 s per
    patch compile). Returns the seconds it took, or None if it failed (patches then just pay the cost once)."""
    run = run or _runner(cancel, work_dir(baseline))
    rel = next(iter(sorted(baseline.frontend)), None)
    if rel is None:
        return None
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="iosstudio-warm-") as tmp:
        rc, _lines = run(frontend_command(baseline.frontend[rel], os.path.join(tmp, "warm.o")), timeout)
    return time.monotonic() - t0 if rc == 0 else None
