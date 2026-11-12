"""Turning hot reload on and off for a project: the managed Package.swift block and the runtime package copy.

On (`hotreload enable`): templates/hot-reload/full/ is copied into <project>/.iosstudio/hot-reload/ (with
<project>/.iosstudio/.gitignore = `*`), and this block is appended to Package.swift (APP = the app's target, found
like the packages feature finds it):

    // >>> iOS Studio hot reload (managed by iOS Studio: turn hot reload off there to remove this)
    package.dependencies.append(.package(path: ".iosstudio/hot-reload"))
    for target in package.targets where target.name == "APP" {
        target.dependencies.append(.product(name: "HotReload", package: "hot-reload"))
        target.linkerSettings = (target.linkerSettings ?? []) + [
            .unsafeFlags([...], .when(configuration: .debug)),
        ]
    }
    // <<< iOS Studio hot reload

Off (`hotreload disable`): when no Swift file under Sources/ has `import HotReload`, the block and the copy are
removed (mode `off`); when some do, the block loses its linkerSettings lines and the copy becomes the no-op stub
(mode `stub`), so the project still builds without the loader or the flag.

Byte for byte: the block goes after one newline (a blank line when the file ends with one; the file's own CRLF or
LF), and removing it gives back exactly the file it was added to. Every change is checked with
`swift package dump-package` on a temp copy before Package.swift is written (atomically); a dry run returns the
diff and writes nothing. The copy is made again when templates/hot-reload/version.txt (or the kind, full or stub)
differs from the copy's marker file.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from iosstudio.core import appdirs, fileio
from iosstudio.core.output import HelperError
from iosstudio.features.packages import manifest as M
from iosstudio.features.packages import project as P

BEGIN = "// >>> iOS Studio hot reload (managed by iOS Studio: turn hot reload off there to remove this)"
END = "// <<< iOS Studio hot reload"
BEGIN_RE = re.compile(r"(?m)^// >>> iOS Studio hot reload[^\n]*")
END_RE = re.compile(r"(?m)^// <<< iOS Studio hot reload[^\r\n]*")
TARGET_RE = re.compile(r'target\.name == "((?:[^"\\]|\\.)*)"')
LINKER_FLAGS = '["-Xlinker", "-interposable", "-Xlinker", "-u", "-Xlinker", "_iosstudio_hot_reload_loader"]'

RUNTIME_REL = ".iosstudio/hot-reload"
MARKER = ".iosstudio-runtime.json"      # {version, kind} of the copy
PRODUCT, PACKAGE = "HotReload", "hot-reload"
IMPORT_RE = re.compile(rb"(?m)^[ \t]*(?:@[A-Za-z_]+(?:\([^)\n]*\))?[ \t]+)*import[ \t]+"
                       rb"(?:(?:struct|class|enum|protocol|typealias|func|var|let)[ \t]+)?HotReload\b")
SCAN_FILES = 10_000                     # Swift files looked at under Sources/
SCAN_BYTES = 1 << 20                    # bytes read of each
IMPORTED_MAX = 50                       # files listed in importedBy
SKIP_DIRS = {".build", ".swiftpm", ".git", "xtool", ".iosstudio", "node_modules"}
DEFAULT_MAX_BYTES = 12_000

DAMAGED = ("The hot reload lines at the end of Package.swift were changed by hand, so iOS Studio can't update "
           "them. Put back the line “// <<< iOS Studio hot reload” after them (or delete the lines from "
           "“// >>> iOS Studio hot reload” on), then try again.")


def templates_dir() -> Path:
    """templates/hot-reload/ of this checkout ($IOSSTUDIO_HOT_RELOAD_TEMPLATES in tests)."""
    env = os.environ.get("IOSSTUDIO_HOT_RELOAD_TEMPLATES")
    return Path(env) if env else appdirs.REPO / "templates" / "hot-reload"


def template_version() -> str:
    try:
        return (templates_dir() / "version.txt").read_text().strip()
    except OSError:
        raise _missing_templates() from None


def _missing_templates() -> HelperError:
    return HelperError("missing-runtime", "The hot reload files that come with iOS Studio are missing. Update "
                                          "iOS Studio, then try again.")


# ================================================================ the Package.swift block

def swift_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def block(target: str, linker: bool = True, nl: str = "\n") -> str:
    """The managed block for the app's target, without a trailing newline (linker=False: the stub's)."""
    lines = [BEGIN,
             f'package.dependencies.append(.package(path: "{RUNTIME_REL}"))',
             f'for target in package.targets where target.name == "{swift_string(target)}" {{',
             f'    target.dependencies.append(.product(name: "{PRODUCT}", package: "{PACKAGE}"))']
    if linker:
        lines += ["    target.linkerSettings = (target.linkerSettings ?? []) + [",
                  f"        .unsafeFlags({LINKER_FLAGS},",
                  "                     .when(configuration: .debug)),",
                  "    ]"]
    lines += ["}", END]
    return nl.join(lines)


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _span(text: str) -> tuple[int, int] | None:
    """(start, end) of the last block's lines: BEGIN's first char to END's last (no newline); damaged raises.
    The last one first, so stripping several gives back the text the first one was appended to."""
    ms = list(BEGIN_RE.finditer(text))
    if not ms:
        return None
    m = ms[-1]
    e = END_RE.search(text, m.end())
    if not e:
        raise HelperError("block-damaged", DAMAGED)
    return m.start(), e.end()


def find_block(text: str) -> str | None:
    """The block's text (BEGIN … END), or None."""
    sp = _span(text)
    return text[sp[0]:sp[1]] if sp else None


def strip_block(text: str) -> str:
    """text without the managed block(s): exactly the file the block was appended to."""
    while (sp := _span(text)) is not None:
        start, end = sp
        nl = "\r\n" if "\r\n" in text[start:end] else "\n"
        head, tail = text[:start], text[end:]
        if head.endswith(nl):
            head = head[:-len(nl)]
        elif head.endswith("\n"):
            head = head[:-1]
        if tail in (nl, "\n"):      # the block was the end of the file
            tail = ""
        text = head + tail
    return text


def append_block(text: str, target: str, linker: bool = True) -> str:
    """text (without a block) + the block: after a blank line when text ends with a newline, else after one."""
    nl = _newline(text)
    return text + nl + block(target, linker, nl) + (nl if text.endswith("\n") else "")


def block_mode(text: str) -> str:
    """"on" (block with the linker flags), "stub" (block without them) or "off" (no block)."""
    b = find_block(text)
    if b is None:
        return "off"
    return "on" if "linkerSettings" in b else "stub"


def block_target(text: str) -> str | None:
    b = find_block(text)
    m = TARGET_RE.search(b) if b else None
    return re.sub(r"\\(.)", r"\1", m.group(1)) if m else None


# ================================================================ sources

def imported_by(root: Path) -> list[str]:
    """Project-relative paths of the Swift files under Sources/ that `import HotReload` (sorted, at most 50)."""
    src = root / "Sources"
    found: list[str] = []
    seen = 0
    if not src.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(".swift"):
                continue
            seen += 1
            if seen > SCAN_FILES:
                return sorted(found)[:IMPORTED_MAX]
            p = Path(dirpath) / name
            try:
                with open(p, "rb") as fh:
                    data = fh.read(SCAN_BYTES)
            except OSError:
                continue
            if b"HotReload" in data and IMPORT_RE.search(data):
                found.append(p.relative_to(root).as_posix())
    return sorted(found)[:IMPORTED_MAX]


# ================================================================ the runtime copy

def runtime_dir(root: Path) -> Path:
    return root / RUNTIME_REL


def runtime_marker(root: Path) -> dict | None:
    """{version, kind} of the project's copy, or None when there's none (or it's incomplete)."""
    d = runtime_dir(root)
    data = fileio.load_json(d / MARKER)
    if not isinstance(data, dict) or not (d / "Package.swift").is_file():
        return None
    return {"version": str(data.get("version")), "kind": str(data.get("kind"))}


def runtime_current(root: Path, kind: str) -> bool:
    """The copy is this kind (full or stub) at the templates' version, and the folder is ignored by git."""
    gi = root / ".iosstudio" / ".gitignore"
    try:
        ignored = gi.read_text() == "*\n"
    except OSError:
        ignored = False
    return ignored and runtime_marker(root) == {"version": template_version(), "kind": kind}


def install_runtime(root: Path, kind: str) -> bool:
    """Copy templates/hot-reload/<kind>/ to .iosstudio/hot-reload/ unless it's already current. The new copy is
    made next to it and swapped in with renames, so a build never sees half a package. True when it changed."""
    if runtime_current(root, kind):
        return False
    src = templates_dir() / kind
    version = template_version()
    if not (src / "Package.swift").is_file():
        raise _missing_templates()
    base = root / ".iosstudio"
    base.mkdir(exist_ok=True)
    gi = base / ".gitignore"
    if not gi.is_file() or gi.read_text(errors="replace") != "*\n":
        fileio.atomic_write(gi, "*\n")
    if runtime_marker(root) == {"version": version, "kind": kind}:
        return True
    dest = runtime_dir(root)
    tag = secrets.token_hex(4)
    tmp = base / f".hot-reload.new-{tag}"
    old = base / f".hot-reload.old-{tag}"
    try:
        shutil.copytree(src, tmp, symlinks=False, ignore=shutil.ignore_patterns(".build", ".swiftpm"))
        (tmp / MARKER).write_text(json.dumps({"version": version, "kind": kind}) + "\n")
        if dest.exists() or dest.is_symlink():
            os.rename(dest, old)
        os.rename(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
    return True


def remove_runtime(root: Path) -> bool:
    """Delete .iosstudio/hot-reload/ (and .iosstudio/ when only its .gitignore is left). True when it removed it."""
    dest = runtime_dir(root)
    if not dest.exists():
        return False
    shutil.rmtree(dest)
    base = dest.parent
    try:
        if {p.name for p in base.iterdir()} <= {".gitignore"}:
            shutil.rmtree(base)
    except OSError:
        pass
    return True


# ================================================================ enable / disable

@dataclass
class Plan:
    root: Path
    text: str           # Package.swift now
    new_text: str       # Package.swift after
    mode: str           # on / stub / off
    target: str | None
    kind: str | None    # runtime copy wanted: full / stub / None (removed)

    @property
    def manifest_changed(self) -> bool:
        return self.new_text != self.text

    def runtime_changes(self) -> bool:
        if self.kind is None:
            return runtime_dir(self.root).exists()
        return not runtime_current(self.root, self.kind)


def read_manifest(root: Path) -> str:
    data = (root / "Package.swift").read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise HelperError("manifest-invalid", "Package.swift isn't UTF-8 text, so iOS Studio can't change it.") \
            from None


def _app_target(root: Path) -> str:
    target = P.app_target(root, P.dump_package(root))
    if not target:
        raise HelperError("no-app-target", "Package.swift has no app target to add hot reload to.")
    return target


def plan_enable(root: Path) -> Plan:
    text = read_manifest(root)
    base = strip_block(text)
    target = _app_target(root)
    return Plan(root, text, append_block(base, target, linker=True), "on", target, "full")


def plan_disable(root: Path) -> tuple[Plan, list[str]]:
    text = read_manifest(root)
    base = strip_block(text)
    users = imported_by(root)
    if not users:
        return Plan(root, text, base, "off", block_target(text), None), users
    target = block_target(text) or _app_target(root)
    return Plan(root, text, append_block(base, target, linker=False), "stub", target, "stub"), users


def validate(plan: Plan) -> None:
    """dump-package of the new Package.swift on a temp copy; with a block, the app target must link HotReload."""
    dump, exc = P.validate_text(plan.root, plan.new_text)
    if exc is not None:
        what = "turn hot reload on" if plan.mode == "on" else "turn hot reload off"
        err = HelperError("manifest-invalid", f"Package.swift wouldn't work after the change to {what}, so "
                                              f"Package.swift was left as it is: {exc.message}", exc.fix, exc.detail)
        err.extra = getattr(exc, "extra", None) or {}   # type: ignore[attr-defined]
        raise err
    if plan.mode == "off" or dump is None:
        return
    linked = any(t["target"] == plan.target and any(d["kind"] == "product" and d["name"] == PRODUCT
                                                    for d in t["deps"]) for t in M.target_deps(dump))
    if not linked:
        raise HelperError("manifest-unsupported", "iOS Studio couldn't add hot reload to this Package.swift (its "
                                                  "package isn't declared as `let package = Package(…)`), so "
                                                  "nothing was changed.")


def _write(plan: Plan) -> None:
    mp = plan.root / "Package.swift"
    fileio.atomic_write(mp, plan.new_text.encode("utf-8"), mp.stat().st_mode & 0o7777)
    P.forget_dump(plan.root)


def _run(plan: Plan, dry_run: bool, max_bytes: int) -> dict:
    """Validate, then (unless dry_run) apply: the runtime copy first when the block needs it, the manifest last
    when it removes the block, so Package.swift never points at a missing folder."""
    runtime = plan.runtime_changes()
    if plan.manifest_changed:
        validate(plan)
    diff = P.manifest_diff(plan.root, plan.text, plan.new_text, max_bytes)
    out = {"changed": plan.manifest_changed or runtime, "diff": diff["diff"], "truncated": diff["truncated"],
           "packagePath": str(plan.root / "Package.swift"), "dryRun": dry_run, "runtimeUpdated": runtime}
    if diff["truncated"]:
        out["fullDiffPath"] = diff["full_output_path"]
    if dry_run:
        return out
    with P.project_lock(plan.root):
        if read_manifest(plan.root) != plan.text:
            raise HelperError("changed", "Package.swift changed while hot reload was being set up. Try again.")
        if plan.kind is not None:
            install_runtime(plan.root, plan.kind)
        if plan.manifest_changed:
            _write(plan)
        if plan.kind is None:
            remove_runtime(plan.root)
    return out


def enable(root: Path, dry_run: bool = False, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    plan = plan_enable(root)
    return {"enabled": True, "mode": "on", "appTarget": plan.target, **_run(plan, dry_run, max_bytes)}


def disable(root: Path, dry_run: bool = False, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    plan, users = plan_disable(root)
    return {"enabled": False, "mode": plan.mode, "importedBy": users, **_run(plan, dry_run, max_bytes)}


def status(root: Path) -> dict:
    """{enabled, mode, runtime: {version, kind} | None, current}: cheap (no swift), for `hotreload status`."""
    mode = block_mode(read_manifest(root))
    marker = runtime_marker(root)
    try:
        current = mode != "off" and runtime_current(root, "full" if mode == "on" else "stub")
    except HelperError:
        current = False
    return {"enabled": mode == "on", "mode": mode, "runtime": marker, "current": current}
