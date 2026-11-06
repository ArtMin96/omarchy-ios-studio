"""Format and lint a project's Swift code with swift-format (and SwiftLint when it is installed). Stdlib only.

    projects format --project P [--file F [F …] | --files '<json list>'] [--apply] [--max-bytes N]
    projects format-lint --project P [--file F …] [--max N] [--last]

`format` is a dry run unless --apply: it runs `swift-format format <file>` (stdout, the file stays as it is) on
every Swift file of the project, or the given ones, and returns a unified diff (paths relative to the project,
a/ b/ prefixes) bounded to --max-bytes; the full diff goes to a file under ~/.cache/ios-studio/format/ (the
newest DIFF_KEEP are kept) when it doesn't fit. --apply writes the files whose content changes, atomically,
and never a file outside the project. swift-format finds .swift-format files itself (the file's folder, then
its parents), so the project's config and nested ones apply exactly as on the command line.

`format-lint` runs `swift-format lint` and, when it runs, `swiftlint lint --quiet --reporter json` (which
reads .swiftlint.yml itself), and returns diagnostics in the shape of build diagnostics (helper/diagnostics.py)
with category 'style' and an absolute file path. The latest result per project is kept in
~/.local/state/ios-studio/lint/ so the app can show it again after a restart (--last reads it).

Which files: Package.swift (and Package@swift-*.swift), and the .swift files under Sources/, Tests/ and
Plugins/, skipping hidden folders, .build/, xtool/ and other build output, symlinks that leave the project,
and files over MAX_FILE_BYTES (generated). Long runs stop at a deadline (DEADLINE s) with timed_out and the
results so far.
"""
from __future__ import annotations

import concurrent.futures as cf
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import common
from common import HelperError

TOOLCHAIN_FALLBACK = Path("/usr/lib/swift/usr/bin/swift-format")
SOURCE_DIRS = ("Sources", "Tests", "Plugins")
SKIP_DIRS = {".build", "build", "xtool", "DerivedData", "Pods", "Carthage", "node_modules", ".swiftpm"}
MAX_FILES = 3000
MAX_FILE_BYTES = 1_000_000
DEADLINE = 20.0                 # seconds for a whole run (the MCP server waits 25)
DEFAULT_MAX_BYTES = 12_000      # diff bytes returned inline
MAX_MAX_BYTES = 200_000
FILES_CAP = 200                 # files_changed entries
ERRORS_CAP = 50
DEFAULT_MAX_DIAGS = 200
STORED_DIAGS = 500              # diagnostics kept in the stored lint result
DIFF_DIR = common.CACHE_DIR / "format"
DIFF_KEEP = 10
LINT_DIR = common.STATE_DIR / "lint"
LINT_CHUNK = 200                # files per `swift-format lint` call
WORKERS = max(1, min(8, os.cpu_count() or 2))
MESSAGE_CUT = 300

# swift-format's pretty-printer findings, which `format` fixes.
FIXABLE_RULES = {"Indentation", "Spacing", "AddLines", "RemoveLine", "TrailingWhitespace", "TrailingComma",
                 "LineLength", "NewlineAtEndOfFile"}
HINT_FIXABLE = "Format project fixes this."
HINT_PARSE = "swift-format can't read this file until its syntax is fixed; build it to see the compiler's error."
HINT_MISSING = "Install the Swift toolchain in Setup (swift-format comes with it)."

LINT_RX = re.compile(r"^(?P<file>[^\n]+?):(?P<line>\d+):(?P<col>\d+): (?P<sev>warning|error|note): "
                     r"(?:\[(?P<rule>[^\]]+)\] )?(?P<msg>.*)$")


# ================================================================ tools

def find_swift_format() -> list[str] | None:
    """argv prefix that runs swift-format: $IOSSTUDIO_SWIFT_FORMAT, the toolchain /usr/bin/swift belongs to,
    /usr/lib/swift, PATH, then `swift format`."""
    env = os.environ.get("IOSSTUDIO_SWIFT_FORMAT")
    if env:
        return [env] if os.access(env, os.X_OK) else None
    cands = []
    swift = shutil.which("swift")
    if swift:
        cands.append(Path(os.path.realpath(swift)).parent / "swift-format")
    cands.append(TOOLCHAIN_FALLBACK)
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            return [str(c)]
    on_path = common.which("swift-format")
    if on_path:
        return [on_path]
    if swift:
        try:
            r = subprocess.run([swift, "format", "--version"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return [swift, "format"]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def tool_version(argv: list[str]) -> str | None:
    try:
        r = subprocess.run([*argv, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (r.stdout or r.stderr or "").strip().splitlines()
    return out[0][:80] if r.returncode == 0 and out else None


def find_swiftlint() -> tuple[str | None, str | None, str | None]:
    """(path, version, why not) — a swiftlint on PATH that actually starts (a mise shim without a version
    set is on PATH but fails)."""
    path = os.environ.get("IOSSTUDIO_SWIFTLINT") or common.which("swiftlint")
    if not path:
        return None, None, None
    try:
        r = subprocess.run([path, "version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, None, f"swiftlint is on PATH but didn't start ({exc.__class__.__name__})."
    if r.returncode != 0:
        first = next((ln.strip() for ln in (r.stderr or r.stdout or "").splitlines() if ln.strip()), "")
        return None, None, "swiftlint is on PATH but didn't start" + (f": {first[:160].rstrip('.')}." if first else ".")
    return path, (r.stdout or "").strip()[:40] or None, None


def missing_tool_error() -> HelperError:
    err = HelperError("missing-swift-format", "swift-format isn't installed (it comes with the Swift toolchain).",
                      common.fix("Open Setup", "setup", step="swift"))
    err.hint = HINT_MISSING    # type: ignore[attr-defined]
    return err


# ================================================================ files

def _inside(root: Path, p: Path) -> bool:
    try:
        Path(os.path.realpath(p)).relative_to(root)
        return True
    except ValueError:
        return False


def discover(root: Path, max_files: int = MAX_FILES) -> tuple[list[Path], int]:
    """(Swift files of the project, how many were left out by the cap or size limit), sorted. `root` is real."""
    out: list[Path] = []
    skipped = 0
    for name in sorted(os.listdir(root)) if root.is_dir() else []:
        if name == "Package.swift" or (name.startswith("Package@swift-") and name.endswith(".swift")):
            p = root / name
            if p.is_file() and not p.is_symlink():
                out.append(p)
    for d in SOURCE_DIRS:
        base = root / d
        if not base.is_dir() or base.is_symlink():
            continue
        for cur, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS)
            for f in sorted(files):
                if not f.endswith(".swift") or f.startswith("."):
                    continue
                p = Path(cur) / f
                if p.is_symlink() and not _inside(root, p):
                    continue
                try:
                    if p.stat().st_size > MAX_FILE_BYTES:
                        skipped += 1
                        continue
                except OSError:
                    continue
                out.append(p)
    if len(out) > max_files:
        skipped += len(out) - max_files
        out = out[:max_files]
    return out, skipped


def select(root: Path, wanted: list[str]) -> tuple[list[Path], list[dict]]:
    """--file values (relative to the project or absolute) -> (files inside the project, errors)."""
    files, errors, seen = [], [], set()
    for w in wanted:
        raw = os.path.expanduser(str(w).strip())
        if not raw:
            continue
        p = Path(raw) if os.path.isabs(raw) else root / raw
        real = Path(os.path.realpath(p))
        if not _inside(root, p):
            errors.append({"file": str(w), "message": "Outside the project: left alone."})
        elif not real.is_file():
            errors.append({"file": str(w), "message": "No such file."})
        elif real.suffix != ".swift":
            errors.append({"file": str(w), "message": "Not a .swift file."})
        elif real not in seen:
            seen.add(real)
            files.append(real)
    return files, errors


def rel(root: Path, p: Path) -> str:
    try:
        return str(Path(os.path.realpath(p)).relative_to(root))
    except ValueError:
        return str(p)


def find_config(root: Path) -> tuple[str | None, list[str]]:
    """(the .swift-format that applies at the project root: relative when inside the project, absolute above
    it, or None; nested .swift-format files in the source folders, relative)."""
    top = None
    d = root
    while True:
        c = d / ".swift-format"
        if c.is_file():
            top = ".swift-format" if d == root else str(c)
            break
        if d.parent == d:
            break
        d = d.parent
    nested = []
    for sd in SOURCE_DIRS:
        base = root / sd
        if not base.is_dir():
            continue
        for cur, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS)
            if ".swift-format" in files:
                nested.append(rel(root, Path(cur) / ".swift-format"))
            if len(nested) >= 20:
                break
    return top, nested


def check_config(root: Path, config: str | None) -> None:
    """A .swift-format that isn't JSON makes swift-format fail on every file: say so once."""
    if not config:
        return
    p = Path(config) if os.path.isabs(config) else root / config
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        err = HelperError("bad-config", f"{config} isn't valid JSON, so swift-format can't use it.",
                          detail=str(exc))
        err.hint = "Fix the JSON in .swift-format (swift-format dump-configuration prints a valid one)."  # type: ignore[attr-defined]
        raise err


# ================================================================ diff

def unified(rel_path: str, before: str, after: str) -> str:
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    out = []
    for line in difflib.unified_diff(a, b, f"a/{rel_path}", f"b/{rel_path}", n=3):
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")
    return "".join(out)


def bound_diff(text: str, max_bytes: int) -> tuple[str, bool]:
    """(text cut to at most max_bytes UTF-8 bytes, truncated). The cut falls before a hunk (`@@`) or file
    (`--- a/`) header when one is in the second half of the kept part, else at a line end."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    head = raw[:max(0, max_bytes)].decode("utf-8", "ignore")
    cut = head.rfind("\n")
    head = head[:cut + 1] if cut >= 0 else ""
    best = -1
    for m in re.finditer(r"(?m)^(@@ |--- a/)", head):
        if m.group(1) == "@@ " and _prev_line(head, m.start()).startswith("+++ "):
            continue        # never leave a file header without its first hunk
        best = m.start()
    if best > len(head) // 2:
        head = head[:best]
    return head, True


def _prev_line(text: str, pos: int) -> str:
    """The line before the one starting at `pos` (pos is just after a newline)."""
    start = text.rfind("\n", 0, max(0, pos - 1)) + 1
    return text[start:max(0, pos - 1)]


def save_diff(root: Path, text: str) -> str:
    DIFF_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", root.name)[:60] or "project"
    p = DIFF_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}-{os.getpid()}.diff"
    common.atomic_write(p, text)
    common.prune([x for x in DIFF_DIR.glob("*.diff")], keep=DIFF_KEEP)
    return str(p)


# ================================================================ format

def _first_error(text: str, path: Path) -> str:
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        for prefix in (str(path) + ":", path.name + ":"):
            if ln.startswith(prefix):
                ln = ln[len(prefix):]
        return common_clip(ln.strip())
    return "swift-format failed on this file."


def common_clip(s: str, n: int = MESSAGE_CUT) -> str:
    return s if len(s) <= n else s[:n] + "…"


def format_one(sf: list[str], root: Path, p: Path, timeout: float) -> dict:
    """{file, before, after} or {file, error}. Reads the file as bytes; swift-format prints the result."""
    r_path = rel(root, p)
    try:
        raw = p.read_bytes()
        before = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"file": r_path, "error": "Not UTF-8 text: left alone."}
    except OSError as exc:
        return {"file": r_path, "error": f"Can't read it ({exc.strerror or exc})."}
    try:
        r = subprocess.run([*sf, "format", str(p)], capture_output=True, timeout=max(1.0, timeout),
                           cwd=str(root), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"file": r_path, "error": "swift-format took too long on this file.", "timeout": True}
    except OSError as exc:
        return {"file": r_path, "error": f"swift-format didn't start ({exc})."}
    if r.returncode != 0:
        return {"file": r_path, "error": _first_error(r.stderr.decode("utf-8", "replace"), p)}
    try:
        after = r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return {"file": r_path, "error": "swift-format printed something that isn't UTF-8."}
    return {"file": r_path, "path": p, "raw": raw, "before": before, "after": after}


def write_atomic(p: Path, data: bytes, expect: bytes) -> str | None:
    """Replace p with data unless it changed since it was read (an editor saved it). Keeps the mode."""
    try:
        if p.read_bytes() != expect:
            return "Changed while formatting: left alone, run Format again."
        mode = p.stat().st_mode & 0o7777
    except OSError as exc:
        return f"Can't read it ({exc.strerror or exc})."
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".fmt")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return f"Couldn't write it ({exc.strerror or exc})."
    return None


def run_format(root: Path, files: list[Path] | None = None, apply: bool = False,
               max_bytes: int = DEFAULT_MAX_BYTES, deadline: float = DEADLINE,
               sf: list[str] | None = None, pre_errors: list[dict] | None = None) -> dict:
    """The `projects format` result for real folder `root` (see the module docstring)."""
    t0 = time.monotonic()
    sf = sf or find_swift_format()
    if not sf:
        raise missing_tool_error()
    config, nested = find_config(root)
    check_config(root, config)
    skipped = 0
    if files is None:
        files, skipped = discover(root)
    errors: list[dict] = list(pre_errors or [])
    results: list[dict] = []
    end = t0 + deadline
    timed_out = False
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {}
        for p in files:
            futs[pool.submit(_format_if_time, sf, root, p, end)] = p
        for fut in cf.as_completed(futs):
            res = fut.result()
            if res is None or res.get("timeout"):
                timed_out = True
                if res is None:
                    continue
            results.append(res)
    results.sort(key=lambda r: r["file"])
    changed, parts = [], []
    for r in results:
        if "error" in r:
            errors.append({"file": r["file"], "message": r["error"]})
            continue
        if r["before"] == r["after"]:
            continue
        if apply:
            why = write_atomic(r["path"], r["after"].encode("utf-8"), r["raw"])
            if why:
                errors.append({"file": r["file"], "message": why})
                continue
        changed.append(r["file"])
        parts.append(unified(r["file"], r["before"], r["after"]))
    full = "".join(parts)
    max_bytes = max(500, min(int(max_bytes or DEFAULT_MAX_BYTES), MAX_MAX_BYTES))
    shown, truncated = bound_diff(full, max_bytes)
    full_path = save_diff(root, full) if truncated else None
    checked = len(results)
    unchecked = len(files) - checked
    notes = []
    if truncated:
        notes.append(f"Diff cut at {len(shown.encode())} of {len(full.encode())} bytes; the whole diff is in "
                     f"full_output_path (or pass --file for fewer files, or a larger --max-bytes).")
    if timed_out:
        notes.append(f"Stopped after {int(deadline)} s: {unchecked} of {len(files)} files weren't checked. Run it "
                     f"again on fewer files (--file).")
    if skipped:
        notes.append(f"{skipped} files were left out (over {MAX_FILE_BYTES // 1000} KB, likely generated, or past "
                     f"{MAX_FILES} files).")
    if not files and pre_errors:
        notes.append("None of the given files can be formatted (see errors).")
    elif not files:
        notes.append("No Swift files found in Package.swift, Sources/, Tests/ or Plugins/.")
    elif not changed and not errors and not timed_out:
        notes.append("Already formatted.")
    if apply and changed:
        notes.append(f"Formatted {len(changed)} file{'s' if len(changed) != 1 else ''}.")
    return {
        "path": str(root),
        "apply": bool(apply),
        "files_checked": checked,
        "files_changed": changed[:FILES_CAP],
        "files_changed_total": len(changed),
        "diff": shown,
        "diff_bytes": len(full.encode()),
        "truncated": truncated,
        "full_output_path": full_path,
        "config_path": config,
        "nested_configs": nested,
        "tool": "swift-format",
        "tool_version": tool_version(sf),
        "errors": errors[:ERRORS_CAP],
        "errors_total": len(errors),
        "timed_out": timed_out,
        "files_skipped": skipped,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "note": " ".join(notes) or None,
    }


def _format_if_time(sf: list[str], root: Path, p: Path, end: float) -> dict | None:
    left = end - time.monotonic()
    if left <= 0:
        return None
    return format_one(sf, root, p, left)


# ================================================================ lint

def parse_swift_format_lint(text: str, root: Path | str) -> list[dict]:
    """`swift-format lint` output -> diagnostics with category 'style' and absolute file paths."""
    root = Path(root)
    out = []
    for line in (text or "").splitlines():
        m = LINT_RX.match(line.strip())
        if not m:
            continue
        f = m.group("file")
        path = f if os.path.isabs(f) else str(root / f)
        rule = m.group("rule")
        sev = m.group("sev")
        d = {"severity": "error" if sev == "error" else "warning", "category": "style",
             "message": common_clip(m.group("msg").strip()), "file": os.path.normpath(path),
             "line": int(m.group("line")), "col": int(m.group("col")), "source": "swift-format"}
        if rule:
            d["rule"] = rule
            if rule in FIXABLE_RULES:
                d["hint"] = HINT_FIXABLE
        elif sev == "error":
            d["rule"] = "syntax"
            d["hint"] = HINT_PARSE
        out.append(d)
    return out


def parse_swiftlint_json(text: str, root: Path | str) -> list[dict]:
    """`swiftlint lint --reporter json` output -> diagnostics (category 'style', absolute paths)."""
    root = Path(root)
    text = (text or "").strip()
    if not text:
        return []
    start = text.find("[")
    try:
        items = json.loads(text[start:] if start > 0 else text)
    except json.JSONDecodeError:
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        f = str(it.get("file") or "")
        if f and not os.path.isabs(f):
            f = str(root / f)
        d = {"severity": "error" if str(it.get("severity", "")).lower() == "error" else "warning",
             "category": "style", "message": common_clip(" ".join(str(it.get("reason") or "").split())),
             "file": os.path.normpath(f) if f else None, "line": it.get("line") if isinstance(it.get("line"), int) else None,
             "col": it.get("character") if isinstance(it.get("character"), int) else None, "source": "swiftlint"}
        if it.get("rule_id"):
            d["rule"] = str(it["rule_id"])
        out.append(d)
    return out


def _sort_key(d: dict):
    return (0 if d["severity"] == "error" else 1, d.get("file") or "", d.get("line") or 0, d.get("col") or 0)


def run_lint(root: Path, files: list[Path] | None = None, max_items: int = DEFAULT_MAX_DIAGS,
             deadline: float = DEADLINE, sf: list[str] | None = None, use_swiftlint: bool = True,
             pre_errors: list[dict] | None = None) -> dict:
    """The `projects format-lint` result for real folder `root`. Also stores it (see last_lint)."""
    t0 = time.monotonic()
    end = t0 + deadline
    sf = sf or find_swift_format()
    if not sf:
        raise missing_tool_error()
    config, _nested = find_config(root)
    check_config(root, config)
    explicit = files is not None
    skipped = 0
    if files is None:
        files, skipped = discover(root)
    diags: list[dict] = []
    tools: list[str] = []
    notes: list[str] = []
    timed_out = False
    if files:
        tools.append("swift-format")
        for i in range(0, len(files), LINT_CHUNK):
            left = end - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            chunk = [str(p) for p in files[i:i + LINT_CHUNK]]
            try:
                r = subprocess.run([*sf, "lint", "--parallel", *chunk], capture_output=True, text=True,
                                   timeout=left, cwd=str(root), stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                timed_out = True
                break
            except OSError as exc:
                raise HelperError("missing-swift-format", f"swift-format didn't start ({exc}).") from None
            diags += parse_swift_format_lint((r.stdout or "") + "\n" + (r.stderr or ""), root)
    lint_path, lint_version, why_not = (None, None, None)
    if use_swiftlint:
        lint_path, lint_version, why_not = find_swiftlint()
    if why_not:
        notes.append(why_not)
    if lint_path and files and not timed_out:
        if explicit:
            targets = [str(p) for p in files]
        else:
            targets = [n for n in ("Package.swift", *SOURCE_DIRS) if (root / n).exists()]
        left = end - time.monotonic()
        try:
            r = subprocess.run([lint_path, "lint", "--quiet", "--reporter", "json", "--force-exclude", *targets],
                               capture_output=True, text=True, timeout=max(1.0, left), cwd=str(root),
                               stdin=subprocess.DEVNULL)
            tools.append("swiftlint")
            found = parse_swiftlint_json(r.stdout, root)
            if not found and r.returncode not in (0, 2) and (r.stderr or "").strip():
                notes.append("swiftlint failed: " + common_clip(r.stderr.strip().splitlines()[-1], 160).rstrip(".") + ".")
            diags += found
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as exc:
            notes.append(f"swiftlint didn't start ({exc}).")
    # Never report a file outside the project (a config could point SwiftLint elsewhere).
    diags = [d for d in diags if not d.get("file") or _inside(root, Path(d["file"]))]
    diags.sort(key=_sort_key)
    counts = {"error": 0, "warning": 0}
    by_source: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for d in diags:
        counts[d["severity"]] = counts.get(d["severity"], 0) + 1
        by_source[d["source"]] = by_source.get(d["source"], 0) + 1
        if d.get("rule"):
            by_rule[d["rule"]] = by_rule.get(d["rule"], 0) + 1
    max_items = max(0, int(max_items))
    if timed_out:
        notes.append(f"Stopped after {int(deadline)} s; results are partial. Lint fewer files with --file.")
    if skipped:
        notes.append(f"{skipped} files were left out (over {MAX_FILE_BYTES // 1000} KB or past {MAX_FILES} files).")
    if len(diags) > max_items:
        notes.append(f"Showing {max_items} of {len(diags)}; pass a larger --max or lint fewer files with --file.")
    top_rules = dict(sorted(by_rule.items(), key=lambda kv: -kv[1])[:20])
    result = {
        "path": str(root),
        "diagnostics": diags[:max_items],
        "total": len(diags),
        "truncated": len(diags) > max_items,
        "counts": counts,
        "by_source": by_source,
        "by_rule": top_rules,
        "files_checked": len(files),
        "config_path": config,
        "tools": tools,
        "tool_version": tool_version(sf),
        "swiftlint": bool(lint_path),
        "swiftlint_version": lint_version,
        "errors": list(pre_errors or [])[:ERRORS_CAP],
        "timed_out": timed_out,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "at": time.time(),
        "cached": False,
        "note": " ".join(notes) or None,
    }
    if not explicit:     # a lint of a few files isn't the project's state
        store_lint(root, {**result, "diagnostics": diags[:STORED_DIAGS], "truncated": len(diags) > STORED_DIAGS})
    return result


def _lint_file(root: Path) -> Path:
    return LINT_DIR / (hashlib.sha1(str(root).encode()).hexdigest()[:16] + ".json")


def store_lint(root: Path, result: dict) -> None:
    try:
        common.save_json(_lint_file(root), result)
    except OSError:
        pass


def last_lint(root: Path, max_items: int = DEFAULT_MAX_DIAGS) -> dict:
    """The stored result of the last whole-project lint, or an empty one with cached=false and at=null."""
    data = common.load_json(_lint_file(root))
    if not isinstance(data, dict) or data.get("path") != str(root):
        return {"path": str(root), "diagnostics": [], "total": 0, "truncated": False,
                "counts": {"error": 0, "warning": 0}, "by_source": {}, "by_rule": {}, "files_checked": 0,
                "config_path": None, "tools": [], "swiftlint": False, "errors": [], "timed_out": False,
                "elapsed_ms": 0, "at": None, "cached": False, "note": "This project hasn't been linted yet."}
    diags = data.get("diagnostics") or []
    total = int(data.get("total") or len(diags))
    data["cached"] = True
    data["diagnostics"] = diags[:max(0, max_items)]
    data["truncated"] = total > len(data["diagnostics"])
    return data


# ================================================================ CLI

def _root(flags: dict) -> Path:
    raw = flags.get("project") or flags.get("path")
    if not raw or raw is True:
        raise HelperError("usage", "Missing --project.")
    p = Path(os.path.realpath(os.path.expanduser(str(raw))))
    if not p.is_dir():
        raise HelperError("not-found", "That project folder doesn't exist anymore.")
    return p


def _wanted(flags: dict) -> list[str] | None:
    """--file A [B …] (extra positional values count) and/or --files '<json list>'; None for the whole project."""
    out: list[str] = []
    f = flags.get("file")
    if f and f is not True:
        out.append(str(f))
        out += [str(x) for x in flags.get("_") or []]
    fs = flags.get("files")
    if fs and fs is not True:
        try:
            v = json.loads(fs)
        except json.JSONDecodeError:
            v = [x for x in str(fs).split("\n") if x.strip()]
        if not isinstance(v, list):
            raise HelperError("usage", "--files must be a JSON list of paths.")
        out += [str(x) for x in v]
    return out or None


def _int(flags: dict, key: str, default: int) -> int:
    v = flags.get(key)
    if v is None or v is True:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise HelperError("usage", f"--{key.replace('_', '-')} must be a number.") from None


def _fail(exc: HelperError) -> int:
    err = exc.to_json()
    if getattr(exc, "hint", None):
        err["hint"] = exc.hint    # type: ignore[attr-defined]
    common._write({"ok": False, "error": err})
    return 1


def cmd_format(flags: dict) -> int:
    root = _root(flags)
    wanted = _wanted(flags)
    files, errs = (select(root, wanted) if wanted is not None else (None, []))
    try:
        return common.ok(run_format(root, files, bool(flags.get("apply")),
                                    _int(flags, "max_bytes", DEFAULT_MAX_BYTES), pre_errors=errs))
    except HelperError as exc:
        return _fail(exc)


def cmd_lint(flags: dict) -> int:
    root = _root(flags)
    cap = max(0, _int(flags, "max", DEFAULT_MAX_DIAGS))
    if flags.get("last"):
        return common.ok(last_lint(root, cap))
    wanted = _wanted(flags)
    files, errs = (select(root, wanted) if wanted is not None else (None, []))
    try:
        return common.ok(run_lint(root, files, cap, pre_errors=errs))
    except HelperError as exc:
        return _fail(exc)


GROUPS = {"projects": {"format": cmd_format, "format-lint": cmd_lint}}


# ================================================================ demo

DEMO_DIFF = {
    "WeatherGlance": [
        ("Sources/WeatherGlance/ForecastView.swift",
         "struct ForecastView: View {\n    let days: [Day]\n    var body: some View {\n        List(days){ day in\n"
         "            HStack {\n                Text(day.name)\n                Spacer()\n"
         "                Text(\"\\(day.high)°\")\n            }\n        }\n    }\n}\n",
         "struct ForecastView: View {\n  let days: [Day]\n  var body: some View {\n    List(days) { day in\n"
         "      HStack {\n        Text(day.name)\n        Spacer()\n        Text(\"\\(day.high)°\")\n"
         "      }\n    }\n  }\n}\n"),
        ("Sources/WeatherGlance/WeatherClient.swift",
         "func fetch(city:String) async throws -> Forecast {\n  let url = base.appending(path:city)\n"
         "  return try await load(url)\n}\n",
         "func fetch(city: String) async throws -> Forecast {\n  let url = base.appending(path: city)\n"
         "  return try await load(url)\n}\n"),
    ],
    "HabitTracker": [
        ("Sources/HabitTracker/StreakView.swift",
         "struct StreakView: View {\n  var habit: Habit\n  var body: some View {\n    Text(\"\\(habit.streak) days\")"
         ".font(.headline )\n  }\n}\n",
         "struct StreakView: View {\n  var habit: Habit\n  var body: some View {\n    Text(\"\\(habit.streak) days\")"
         ".font(.headline)\n  }\n}\n"),
    ],
}

DEMO_LINT = {
    "WeatherGlance": [
        ("Sources/WeatherGlance/ForecastView.swift", 2, 1, "warning", "Indentation", "unindent by 2 spaces", "swift-format"),
        ("Sources/WeatherGlance/ForecastView.swift", 4, 19, "warning", "Spacing", "add 1 space", "swift-format"),
        ("Sources/WeatherGlance/WeatherClient.swift", 1, 16, "warning", "Spacing", "add 1 space", "swift-format"),
        ("Sources/WeatherGlance/WeatherClient.swift", 9, 5, "warning", "force_unwrapping",
         "Force unwrapping should be avoided", "swiftlint"),
        ("Sources/WeatherGlance/WeatherGlanceApp.swift", 14, 121, "warning", "line_length",
         "Line should be 120 characters or less; currently it has 134 characters", "swiftlint"),
    ],
    "HabitTracker": [
        ("Sources/HabitTracker/StreakView.swift", 4, 54, "warning", "Spacing", "remove 1 space", "swift-format"),
        ("Sources/HabitTracker/Habit.swift", 12, 9, "warning", "identifier_name",
         "Variable name 'd' should be between 2 and 40 characters long", "swiftlint"),
    ],
}


def demo(action: str, flags: dict) -> int:
    """Demo mode: canned diffs and style warnings for the demo projects; nothing is read or written."""
    raw = str(flags.get("project") or flags.get("path") or "")
    if not raw or raw == "True":
        return common.fail("usage", "Missing --project.")
    name = raw.rstrip("/").split("/")[-1]
    path = raw.rstrip("/")
    if action == "format":
        entries = DEMO_DIFF.get(name, [])
        full = "".join(unified(r, a, b) for r, a, b in entries)
        shown, truncated = bound_diff(full, max(500, _int(flags, "max_bytes", DEFAULT_MAX_BYTES)))
        changed = [r for r, _a, _b in entries]
        return common.ok({
            "path": path, "apply": bool(flags.get("apply")), "files_checked": {"WeatherGlance": 9,
                                                                                "HabitTracker": 7}.get(name, 5),
            "files_changed": changed, "files_changed_total": len(changed), "diff": shown,
            "diff_bytes": len(full.encode()), "truncated": truncated, "full_output_path": None,
            "config_path": ".swift-format" if name == "WeatherGlance" else None, "nested_configs": [],
            "tool": "swift-format", "tool_version": "6.4.2", "errors": [], "errors_total": 0, "timed_out": False,
            "files_skipped": 0, "elapsed_ms": 412, "demo": True,
            "note": ("Demo mode: nothing was written." if flags.get("apply") and changed else
                     None if changed else "Already formatted."),
        })
    if action == "format-lint":
        diags = []
        for f, line, col, sev, rule, msg, src in DEMO_LINT.get(name, []):
            d = {"severity": sev, "category": "style", "message": msg, "file": f"{path}/{f}", "line": line,
                 "col": col, "rule": rule, "source": src}
            if rule in FIXABLE_RULES:
                d["hint"] = HINT_FIXABLE
            diags.append(d)
        diags.sort(key=_sort_key)
        cap = max(0, _int(flags, "max", DEFAULT_MAX_DIAGS))
        by_source: dict[str, int] = {}
        by_rule: dict[str, int] = {}
        for d in diags:
            by_source[d["source"]] = by_source.get(d["source"], 0) + 1
            by_rule[d["rule"]] = by_rule.get(d["rule"], 0) + 1
        return common.ok({
            "path": path, "diagnostics": diags[:cap], "total": len(diags), "truncated": len(diags) > cap,
            "counts": {"error": 0, "warning": len(diags)}, "by_source": by_source, "by_rule": by_rule,
            "files_checked": 9, "config_path": ".swift-format" if name == "WeatherGlance" else None,
            "tools": ["swift-format", "swiftlint"], "tool_version": "6.4.2", "swiftlint": True,
            "swiftlint_version": "0.65.1", "errors": [], "timed_out": False, "elapsed_ms": 380,
            "at": time.time() - 120, "cached": bool(flags.get("last")), "demo": True, "note": None,
        })
    return common.fail("usage", f"Unknown command 'projects {action}'.")
