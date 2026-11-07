"""Swift package dependencies of a project: list, add, update, resolve and remove (stdlib only).

    packages list --project P
    packages add --project P --url U [--rule from|upToNextMinor|exact|branch|revision] [--version V]
                 [--target T] [--product X [Y …] | --products '<json list>'] [--dry-run]          (streams)
    packages add-product --project P --package ID --product X [Y …] [--target T]
    packages update --project P [--package ID]                                                    (streams)
    packages resolve --project P                                                                  (streams)
    packages remove --project P --package ID [--confirm] [--max-bytes N]

`list` reads Package.swift through `swift package dump-package` (cached by the manifest's mtime and size, run
with its own scratch folder so a running build's lock on .build/ never blocks it) and Package.resolved directly
(formats 1, 2 and 3, see package_manifest.py).

`add` edits the manifest with `swift package add-dependency`, fetches with `swift package resolve`, reads the
package's library products from its checkout, and links one with `swift package add-target-dependency` (default
target: the app's). With several products and no --product it stops after fetching with needs_product and the
product list; `add-product` then links the chosen one(s). A failure after the first edit puts Package.swift and
Package.resolved back as they were. Without --version, rule from/upToNextMinor/exact uses the newest release tag
(`git ls-remote --tags`).

`update` and `resolve` run `swift package update [ID]` / `swift package resolve` and report `changes`, the pins
that moved, from Package.resolved before and after.

`remove` has no SwiftPM command, so it edits Package.swift as text (package_manifest.remove_package): the
.package entry and every target dependency on the package. Without --confirm it's a dry run: the diff and
whether the result is valid (checked with `dump-package` on a copy in a temp folder). With --confirm the file is
written atomically, checked again in place (put back if it fails), and the package's pins (and those only it
needed) are dropped from Package.resolved.

Streaming commands send `step` events (ids below), `progress` (fraction, detail), `log` (each line of swift's
output) and `done`. They stop on {"cancel": true} on stdin or SIGTERM: swift's process group is killed and the
manifest and Package.resolved are put back. One mutating command per project at a time (`busy` otherwise).

    add      check, add, fetch, products, link, validate
    update   update, changes          resolve   resolve, changes

Errors are {code, message, hint?, diagnostics?, …}; diagnostics have the build diagnostics' shape
(category 'package', diagnostics.py parses swift's output).
"""
from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import common
import package_manifest as M
from common import HelperError

CACHE = common.CACHE_DIR / "packages"
LOCKS = common.STATE_DIR / "packages"
DIFF_KEEP = 10
CACHE_KEEP = 100              # per kind of cached file (manifest descriptions, product lists, scratch)
DEFAULT_MAX_BYTES = 12_000
MAX_MAX_BYTES = 200_000
MAX_PACKAGES = 200
MAX_PRODUCTS = 100
DUMP_TIMEOUT = 120
QUICK_TIMEOUT = 60
LS_REMOTE_TIMEOUT = 45
EXIT_GRACE = 60          # seconds swift may linger after closing its output
MAX_LINE = 64 * 1024     # longest output line kept whole
RULES = ("from", "upToNextMinor", "exact", "branch", "revision")
TARGET_TYPES_FIRST = ("executable", "regular")

STEPS_ADD = [("check", "Checking the package"), ("add", "Adding to Package.swift"), ("fetch", "Fetching"),
             ("products", "Reading products"), ("link", "Linking to the target"), ("validate", "Checking Package.swift")]
STEPS_UPDATE = [("update", "Updating packages"), ("changes", "Comparing versions")]
STEPS_RESOLVE = [("resolve", "Resolving packages"), ("changes", "Comparing versions")]

HINT_FETCH = "Check the URL: it must be a git repository that can be cloned without a password."
HINT_RESOLVE = "No version matches the rule: pick another version or rule (or a branch)."
HINT_NETWORK = "Check the internet connection and try again."
HINT_MANIFEST = "Fix the lines listed in Package.swift, then try again."
HINT_BUILD_AFTER = "Check build compiles the app with the new package."
HINT_BUILD_REMOVED = "Check build shows any code that still imports the removed package."


class Cancelled(Exception):
    pass


# ================================================================ swift

def find_swift() -> str:
    env = os.environ.get("IOSSTUDIO_SWIFT")
    if env:
        return env
    found = shutil.which("swift") or common.which("swift")
    if found:
        return found
    if os.access("/usr/lib/swift/usr/bin/swift", os.X_OK):
        return "/usr/lib/swift/usr/bin/swift"
    raise HelperError("missing-swift", "Swift isn't installed yet.", common.fix("Open Setup", "setup", step="swift"))


def swift_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = f"{common.LOCAL_BIN}:{env.get('PATH', '/usr/bin:/bin')}"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    env["GIT_TERMINAL_PROMPT"] = "0"          # a private repository fails instead of waiting for a password
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def _pdeathsig() -> None:  # pragma: no cover - runs in the child
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        pass


def run_swift(args: list[str], cwd: Path | str, timeout: float = QUICK_TIMEOUT,
              stdout_only: bool = False) -> subprocess.CompletedProcess:
    """`swift <args>` captured (own process group; killed whole on timeout)."""
    argv = [find_swift(), *args]
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=swift_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, preexec_fn=_pdeathsig)
    except FileNotFoundError:
        raise HelperError("missing-swift", "Swift isn't installed yet.", common.fix("Open Setup", "setup", step="swift"))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid)
        common.kill_proc(proc)
        raise HelperError("timeout", f"swift {args[-1] if args else ''} took longer than {int(timeout)} s.")
    except BaseException:
        _kill_group(proc.pid)
        common.kill_proc(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, "" if stdout_only else err)


def _kill_group(pgid: int, grace: float = 2.0) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    end = time.time() + grace
    while time.time() < end:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Runner:
    """Runs swift commands for a streaming command: forwards each output line, and stops (killing swift's whole
    process group) on {"cancel": true} on stdin or SIGTERM/SIGINT/SIGHUP."""

    def __init__(self) -> None:
        self.stdin_open = True
        self.stdin_buf = b""
        self._old: dict = {}

    def __enter__(self) -> "Runner":
        def on_signal(_signum, _frame):
            raise Cancelled()
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self._old[s] = signal.signal(s, on_signal)
        return self

    def __exit__(self, *exc) -> None:
        for s, h in self._old.items():
            try:
                signal.signal(s, h)
            except (ValueError, TypeError):
                pass
        self._old = {}

    def quiet(self) -> None:
        """Ignore further stop signals while files are put back (__exit__ restores the old handlers)."""
        for s in self._old:
            try:
                signal.signal(s, signal.SIG_IGN)
            except (ValueError, TypeError):
                pass

    def poll_cancel(self) -> None:
        """Read pending stdin without blocking; raise Cancelled on a cancel message."""
        if not self.stdin_open:
            return
        try:
            fd = sys.stdin.fileno()
            while True:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    return
                self._read_stdin(fd)
                if not self.stdin_open:
                    return
        except (OSError, ValueError):
            self.stdin_open = False

    def _read_stdin(self, fd: int) -> None:
        chunk = os.read(fd, 4096)
        if not chunk:
            self.stdin_open = False
            return
        self.stdin_buf += chunk
        while b"\n" in self.stdin_buf:
            ln, self.stdin_buf = self.stdin_buf.split(b"\n", 1)
            try:
                msg = json.loads(ln.decode() or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict) and msg.get("cancel"):
                raise Cancelled()

    def run(self, args: list[str], cwd: Path, on_line: Callable[[str], None]) -> tuple[int, list[str]]:
        argv = [find_swift(), *args]
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=swift_env(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                                    start_new_session=True, preexec_fn=_pdeathsig)
        except FileNotFoundError:
            raise HelperError("missing-swift", "Swift isn't installed yet.",
                              common.fix("Open Setup", "setup", step="swift"))
        assert proc.stdout is not None
        out_fd = proc.stdout.fileno()
        lines: list[str] = []
        buf = b""
        try:
            while True:
                fds = [out_fd]
                in_fd = -1
                if self.stdin_open:
                    try:
                        in_fd = sys.stdin.fileno()
                        fds.append(in_fd)
                    except (OSError, ValueError):
                        self.stdin_open = False
                try:
                    ready, _, _ = select.select(fds, [], [], 0.5)
                except InterruptedError:
                    continue
                if in_fd in ready:
                    try:
                        self._read_stdin(in_fd)
                    except OSError:
                        self.stdin_open = False
                if out_fd in ready:
                    chunk = os.read(out_fd, 65536)
                    if not chunk:
                        break
                    buf += chunk
                    parts = re.split(rb"\r\n|\r|\n", buf)
                    buf = parts.pop()
                    if len(buf) > MAX_LINE:     # a newline-less flood: pass it on in pieces
                        parts.append(buf[:MAX_LINE])
                        buf = buf[MAX_LINE:]
                    for p in parts:
                        text = p.decode(errors="replace")
                        lines.append(text)
                        on_line(text)
            if buf:
                text = buf.decode(errors="replace")
                lines.append(text)
                on_line(text)
            try:    # output closed: it's exiting; one that lingers is stopped rather than waited on forever
                rc = proc.wait(timeout=EXIT_GRACE)
            except subprocess.TimeoutExpired:
                raise HelperError("timeout", f"swift {args[0] if args else ''} closed its output but didn't exit.")
        except BaseException:
            _kill_group(proc.pid)
            common.kill_proc(proc)
            raise
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)     # anything it left behind
            except (ProcessLookupError, PermissionError):
                pass
        return rc, lines


# ================================================================ project files

def project_root(flags: dict) -> Path:
    raw = flags.get("project") or flags.get("path")
    if not raw or raw is True:
        raise HelperError("usage", "Missing --project.")
    p = Path(os.path.realpath(os.path.expanduser(str(raw))))
    if not p.is_dir():
        raise HelperError("not-found", "That project folder doesn't exist anymore.")
    if not (p / "Package.swift").is_file():
        raise HelperError("not-a-project", "That folder has no Package.swift.")
    return p


def _key(p: Path | str) -> str:
    return hashlib.sha1(str(p).encode()).hexdigest()[:16]


@contextmanager
def project_lock(root: Path):
    """One mutating package command per project (the app and the MCP server share it)."""
    LOCKS.mkdir(parents=True, exist_ok=True)
    fh = open(LOCKS / f"{_key(root)}.lock", "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise HelperError("package-busy", "Another package change for this project is still running. Wait for "
                                              "it to finish, then try again.")
        yield
    finally:
        fh.close()


class Backup:
    """Package.swift and Package.resolved as they were before the first edit."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest = (root / "Package.swift").read_bytes()
        self.mode = (root / "Package.swift").stat().st_mode & 0o7777
        rp = root / "Package.resolved"
        self.resolved = rp.read_bytes() if rp.is_file() else None

    def changed(self) -> bool:
        rp = self.root / "Package.resolved"
        now = rp.read_bytes() if rp.is_file() else None
        return (self.root / "Package.swift").read_bytes() != self.manifest or now != self.resolved

    def restore(self, resolved_only: bool = False) -> bool:
        """Put the files back if they changed. Returns True when something was restored."""
        did = False
        mp = self.root / "Package.swift"
        if not resolved_only and _read_bytes(mp) != self.manifest:
            _write_bytes(mp, self.manifest, self.mode)
            did = True
        rp = self.root / "Package.resolved"
        if _read_bytes(rp) != self.resolved:
            if self.resolved is None:
                rp.unlink(missing_ok=True)
            else:
                _write_bytes(rp, self.resolved, 0o644)
            did = True
        if did:
            _forget_dump(self.root)
        return did


def _read_bytes(p: Path) -> bytes | None:
    try:
        return p.read_bytes()
    except OSError:
        return None


def _write_bytes(p: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ================================================================ dump-package

def _stamp(p: Path) -> list | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _dump_cache(root: Path) -> Path:
    return CACHE / "dump" / f"{_key(root)}.json"


def _forget_dump(root: Path) -> None:
    _dump_cache(root).unlink(missing_ok=True)


def _manifest_error(lines: list[str], what: str = "Package.swift has errors.") -> HelperError:
    import diagnostics
    diags = [d for d in diagnostics.parse(lines, failed=True) if d.get("severity") == "error"][:10]
    first = next((d for d in diags if d.get("line")), None)
    msg = what
    if first:
        msg = f"{what.rstrip('.')}: line {first['line']}: {diagnostics.clip(first['message'], 200)}"
    err = HelperError("manifest-invalid", msg, detail="\n".join(lines[-40:]))
    err.extra = {"diagnostics": diags, "hint": HINT_MANIFEST}   # type: ignore[attr-defined]
    return err


def dump_package(root: Path, use_cache: bool = True) -> dict:
    """`swift package dump-package` of a project, cached by Package.swift's mtime and size."""
    stamp = _stamp(root / "Package.swift")
    cache = _dump_cache(root)
    if use_cache:
        entry = common.load_json(cache)
        if isinstance(entry, dict) and entry.get("stamp") == stamp and isinstance(entry.get("data"), dict):
            return entry["data"]
    data = _dump_dir(root, CACHE / "scratch" / _key(root))
    try:
        common.save_json(cache, {"stamp": stamp, "data": data})
    except OSError:
        pass
    prune_cache()
    return data


def prune_cache() -> None:
    """Keep the newest CACHE_KEEP manifest descriptions, product lists and manifest scratch folders."""
    for sub in ("dump", "products", "scratch"):
        d = CACHE / sub
        if d.is_dir():
            common.prune(list(d.iterdir()), keep=CACHE_KEEP)


def _dump_dir(pkg_dir: Path, scratch: Path) -> dict:
    scratch.mkdir(parents=True, exist_ok=True)
    r = run_swift(["package", "--package-path", str(pkg_dir), "--scratch-path", str(scratch), "dump-package"],
                  pkg_dir, timeout=DUMP_TIMEOUT)
    if r.returncode != 0:
        raise _manifest_error((r.stderr or r.stdout or "").splitlines())
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise HelperError("manifest-invalid", "swift package dump-package didn't print the package description.",
                          detail=(r.stdout + r.stderr)[-2000:])
    return data if isinstance(data, dict) else {}


def validate_text(root: Path, text: str) -> tuple[dict | None, HelperError | None]:
    """dump-package of `text` as Package.swift in a temp folder: (description, None) or (None, the error)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(CACHE), prefix="validate-") as tmp:
        d = Path(tmp) / root.name
        d.mkdir()
        (d / "Package.swift").write_text(text)
        try:
            return _dump_dir(d, Path(tmp) / "scratch"), None
        except HelperError as exc:
            if exc.code != "manifest-invalid":
                raise
            return None, exc


# ================================================================ checkouts and products

def checkouts(root: Path) -> dict[str, dict]:
    """{identity: {path, name}} of fetched packages, from .build/workspace-state.json."""
    data = common.load_json(root / ".build" / "workspace-state.json") or {}
    out: dict[str, dict] = {}
    for dep in (data.get("object") or {}).get("dependencies") or []:
        ref = dep.get("packageRef") or {}
        ident = str(ref.get("identity") or "")
        if not ident:
            continue
        kind = ref.get("kind")
        if kind == "fileSystem":
            path = str(ref.get("location") or "")
        else:
            sub = dep.get("subpath") or ref.get("name") or ident
            state = (dep.get("state") or {}).get("name")
            base = "checkouts" if state != "edited" else "../Packages"
            path = str(root / ".build" / base / str(sub))
        out[ident] = {"path": path, "name": ref.get("name")}
    return out


def products_of(pkg_dir: str | Path) -> list[str]:
    """Library product names of a package folder (its manifest dumped with a throwaway scratch folder, cached by
    the manifest's stamp). Underscored products (`_RopeModule`) come last."""
    p = Path(pkg_dir)
    stamp = _stamp(p / "Package.swift")
    if stamp is None:
        return []
    cache = CACHE / "products" / f"{_key(p)}.json"
    entry = common.load_json(cache)
    if isinstance(entry, dict) and entry.get("stamp") == stamp and isinstance(entry.get("products"), list):
        return entry["products"]
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(CACHE), prefix="products-") as tmp:
        data = _dump_dir(p, Path(tmp))
    names = [str(x.get("name")) for x in data.get("products") or []
             if isinstance(x, dict) and "library" in (x.get("type") or {})]
    names = sorted(dict.fromkeys(names), key=lambda n: (n.startswith("_"), 0))
    try:
        common.save_json(cache, {"stamp": stamp, "products": names, "name": data.get("name")})
    except OSError:
        pass
    return names[:MAX_PRODUCTS]


def pick_product(products: list[str]) -> str | None:
    """The product to link without asking: the only one (ignoring underscored ones), else None."""
    public = [p for p in products if not p.startswith("_")] or products
    return public[0] if len(public) == 1 else None


# ================================================================ app target

def _xtool_product(root: Path) -> str | None:
    try:
        text = (root / "xtool.yml").read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"^product:\s*['\"]?([^'\"\n#]+?)['\"]?\s*(#.*)?$", text, re.M)
    return m.group(1).strip() if m else None


def app_target(root: Path, dump: dict) -> str | None:
    """The app's target: xtool.yml's product (a library product) -> its first target; else the first library
    product's first target; else the first executable or regular target."""
    targets = [t for t in dump.get("targets") or [] if isinstance(t, dict)]
    names = {str(t.get("name")): str(t.get("type") or "regular") for t in targets}
    libs = [p for p in dump.get("products") or [] if isinstance(p, dict) and "library" in (p.get("type") or {})]
    wanted = _xtool_product(root)
    for prod in ([p for p in libs if p.get("name") == wanted] if wanted else []) + libs:
        for t in prod.get("targets") or []:
            if names.get(str(t)) in TARGET_TYPES_FIRST:
                return str(t)
    if wanted and names.get(wanted) in TARGET_TYPES_FIRST:
        return wanted
    for kind in TARGET_TYPES_FIRST:
        for n, k in names.items():
            if k == kind:
                return n
    return None


def target_list(root: Path, dump: dict) -> list[dict]:
    app = app_target(root, dump)
    out = [{"name": str(t.get("name")), "type": str(t.get("type") or "regular"), "app": str(t.get("name")) == app}
           for t in dump.get("targets") or [] if isinstance(t, dict)]
    rank = {"executable": 1, "regular": 1, "test": 3}
    out.sort(key=lambda t: (not t["app"], rank.get(t["type"], 2)))
    return out


# ================================================================ list

def read_resolved(root: Path) -> tuple[bool, dict[str, dict], str | None]:
    """(file exists, pins, error message)."""
    rp = root / "Package.resolved"
    if not rp.is_file():
        return False, {}, None
    try:
        return True, M.parse_resolved(rp.read_text(errors="replace")), None
    except (ValueError, OSError) as exc:
        return True, {}, str(exc)


def _short_pin(pin: dict | None) -> dict | None:
    if not pin:
        return None
    out = {}
    if pin.get("version"):
        out["version"] = pin["version"]
    if pin.get("branch"):
        out["branch"] = pin["branch"]
    if pin.get("revision"):
        out["revision"] = str(pin["revision"])[:7]
    return out or None


def build_list(root: Path, dump: dict, pins: dict[str, dict], co: dict[str, dict] | None = None,
               products: Callable[[str], list[str]] | None = None) -> list[dict]:
    """Merge declared packages, pins and target dependencies into the list entries."""
    co = co if co is not None else {}
    tdeps = M.target_deps(dump)
    local_targets = {t["target"] for t in tdeps}
    by_name = [(t["target"], d["name"]) for t in tdeps for d in t["deps"]
               if d["kind"] == "byName" and d["name"] not in local_targets]
    out = []
    for d in M.declared(dump)[:MAX_PACKAGES]:
        ident = d["identity"]
        names = {ident.lower()} | ({str(d["name"]).lower()} if d.get("name") else set())
        used_by: list[str] = []
        used: list[str] = []
        for t in tdeps:
            for dep in t["deps"]:
                if dep["kind"] == "product" and dep.get("package") and str(dep["package"]).lower() in names:
                    used_by.append(t["target"])
                    used.append(dep["name"])
        if by_name and products:
            prods = set(products(ident))
            for tgt, n in by_name:
                if n in prods:
                    used_by.append(tgt)
                    used.append(n)
        info = co.get(ident) or {}
        entry = {
            "identity": ident,
            "name": d.get("name") or info.get("name") or ident,
            "kind": d["kind"],
            "requirement": d["requirement"],
            "resolved": None if d["kind"] == "local" else _short_pin(pins.get(ident)),
            "used_by": list(dict.fromkeys(used_by)),
            "products_used": list(dict.fromkeys(used)),
        }
        if d["kind"] == "local":
            entry["path"] = d["path"]
        else:
            entry["url"] = d["url"]
        out.append(entry)
    return out


def list_data(root: Path, use_cache: bool = True) -> dict:
    t0 = time.monotonic()
    dump = dump_package(root, use_cache)
    exists, pins, perr = read_resolved(root)
    co = checkouts(root)

    def prods(ident: str) -> list[str]:
        path = (co.get(ident) or {}).get("path")
        if not path:
            return []
        try:
            return products_of(path)
        except HelperError:
            return []

    pkgs = build_list(root, dump, pins, co, prods)
    declared_total = len(M.declared(dump))
    unresolved = [p["identity"] for p in pkgs if p["kind"] != "local" and not p["resolved"]]
    out: dict[str, Any] = {
        "path": str(root), "package": dump.get("name"), "packages": pkgs, "total": declared_total,
        "truncated": declared_total > len(pkgs), "targets": target_list(root, dump),
        "app_target": app_target(root, dump), "resolved_file": exists, "unresolved": unresolved,
        "manifest_mtime": (root / "Package.swift").stat().st_mtime,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }
    if perr:
        out["resolved_error"] = perr
    variants = sorted(p.name for p in root.glob("Package@swift-*.swift"))
    if variants:
        out["note"] = (f"This project also has {', '.join(variants)}; newer Swift versions read that file instead of "
                       "Package.swift, and only Package.swift is changed here.")
    return out


def find_declared(dump: dict, wanted: str) -> dict | None:
    """A declared package by identity, name, URL or path (case-insensitive)."""
    w = str(wanted or "").strip()
    if not w:
        return None
    wl = w.lower()
    for d in M.declared(dump):
        if wl in {d["identity"].lower(), str(d.get("name") or "").lower()}:
            return d
    for d in M.declared(dump):
        loc = d.get("url") or d.get("path") or ""
        if loc and (M._norm_loc(loc) == M._norm_loc(w) or M.identity_of(w) == d["identity"].lower()
                    and ("/" in w or ":" in w)):
            return d
    return None


# ================================================================ events

class Stream:
    """Step/progress events of one streaming command."""

    def __init__(self, steps: list[tuple[str, str]]) -> None:
        self.titles = dict(steps)
        self.current: str | None = None
        for sid, title in steps:
            common.step(sid, title, "pending")

    def start(self, sid: str, title: str | None = None, detail: str | None = None, fraction: float | None = None):
        if title:
            self.titles[sid] = title
        self.current = sid
        extra = {"detail": detail} if detail else {}
        common.step(sid, self.titles.get(sid, sid), "running", **extra)
        if fraction is not None:
            common.progress(fraction, detail or self.titles.get(sid, sid))

    def detail(self, detail: str, fraction: float | None = None) -> None:
        if self.current:
            common.step(self.current, self.titles.get(self.current, self.current), "running", detail=detail)
        common.progress(fraction, detail)

    def finish(self, sid: str, state: str = "done", detail: str | None = None) -> None:
        extra = {"detail": detail} if detail else {}
        common.step(sid, self.titles.get(sid, sid), state, **extra)
        if self.current == sid:
            self.current = None

    def close(self, state: str) -> None:
        """Mark the running step (and every pending one) as failed/cancelled/skipped."""
        if self.current:
            self.finish(self.current, state)


_PROGRESS = [
    (re.compile(r"^Fetching (\S+?)(?: from cache)?$"), "Fetching {n}", 0),
    (re.compile(r"^Fetched (\S+)"), "Fetched {n}", 1),
    (re.compile(r"^Cloning (?!into\b)(\S+)"), "Cloning {n}", 0),
    (re.compile(r"^Updating (\S+)$"), "Updating {n}", 0),
    (re.compile(r"^Updated (\S+)"), "Updated {n}", 1),
    (re.compile(r"^Computing version for (\S+)"), "Computing the version of {n}", 0),
    (re.compile(r"^Computed (\S+) at (\S+)"), "{n} {v}", 1),
    (re.compile(r"^Creating working copy for (\S+)"), "Checking out {n}", 0),
    (re.compile(r"^Working copy of (\S+) resolved at (.+)$"), "{n} {v}", 1),
]
_WAITING = re.compile(r"Another instance of SwiftPM is already running", re.I)
_GIT_COUNT = re.compile(r"^\[(\d+)/(\d+)\] (\w+) (\S+)")      # "[120/29848] Fetching swift-collections"


class LineSink:
    """Forwards swift's lines as log events and progress, but sends git's object counters ("[120/29848]
    Fetching x", thousands of them) at most every 0.5 s and never as log lines."""

    def __init__(self, on_progress: Callable[[tuple[str, str | None, bool]], None]) -> None:
        self.on_progress = on_progress
        self.last_count = 0.0

    def __call__(self, line: str) -> None:
        counter = _GIT_COUNT.match(line.strip())
        if counter:
            now = time.monotonic()
            if now - self.last_count < 0.5:
                return
            self.last_count = now
        else:
            common.log(line)
        pd = progress_detail(line)
        if pd:
            self.on_progress(pd)


def progress_detail(line: str) -> tuple[str, str | None, bool] | None:
    """A swift output line -> (detail, package name, finished one) or None."""
    s = line.strip()
    m = _GIT_COUNT.match(s)
    if m:
        pct = int(m.group(1)) * 100 // max(1, int(m.group(2)))
        return f"{m.group(3)} {m.group(4)} {pct}%", None, False
    if _WAITING.search(s):
        return "Waiting for another Swift command on this project (a build?) to finish", None, False
    for rx, fmt, doneflag in _PROGRESS:
        m = rx.match(s)
        if m:
            name = M.identity_of(m.group(1)) or m.group(1)
            v = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
            return fmt.format(n=name, v=v).strip(), name, bool(doneflag)
    return None


# ================================================================ errors

def _error(code: str, message: str, hint: str | None = None, **extra: Any) -> HelperError:
    err = HelperError(code, message)
    err.extra = {k: v for k, v in {"hint": hint, **extra}.items() if v is not None}   # type: ignore[attr-defined]
    return err


def error_json(exc: HelperError) -> dict:
    out = exc.to_json()
    out.update(getattr(exc, "extra", None) or {})
    return out


def fail(exc: HelperError) -> int:
    common._write({"ok": False, "error": error_json(exc)})
    return 1


def done_fail(exc: HelperError) -> int:
    common.event("done", ok=False, error=error_json(exc))
    return 1


def swift_failure(lines: list[str], what: str) -> HelperError:
    """A failed resolve/update/edit -> an error with package diagnostics and a hint in app wording."""
    import diagnostics
    diags = [d for d in diagnostics.parse(lines, failed=True) if d.get("severity") == "error"][:10]
    text = "\n".join(lines)
    first = diags[0]["message"] if diags else next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    code, hint = "package-failed", None
    if re.search(r"could not resolve host|failed to connect|timed out|network is unreachable|"
                 r"temporary failure in name resolution|connection (refused|reset)", text, re.I):
        code, hint = "network", HINT_NETWORK
    elif re.search(r"failed to clone|repository .* not found|couldn.t fetch|failed to fetch|"
                   r"could not read username|authentication failed|permission denied \(publickey", text, re.I):
        code, hint = "fetch-failed", HINT_FETCH
    elif re.search(r"could not be resolved|no versions of|depends on", text, re.I):
        code, hint = "resolve-failed", HINT_RESOLVE
    elif re.search(r"invalid manifest|manifest parse|error: .*Package\.swift", text, re.I):
        code, hint = "manifest-invalid", HINT_MANIFEST
    for d in diags:   # hints in app wording; the MCP server adds its own next steps
        d["hint"] = hint or d.get("hint")
    if not diags and first:
        diags = [{"severity": "error", "category": "package", "message": diagnostics.clip(first),
                  **({"hint": hint} if hint else {})}]
    msg = f"{what}: {diagnostics.clip(first, 240)}" if first else f"{what}."
    lines = [ln for ln in lines if not _GIT_COUNT.match(ln.strip())]
    return _error(code, msg, hint, diagnostics=diags, detail_lines=[ln[:300] for ln in lines[-20:]])


# ================================================================ diff

def manifest_diff(root: Path, before: str, after: str, max_bytes: int) -> dict:
    import format as swift_format
    full = swift_format.unified("Package.swift", before, after)
    shown, cut = swift_format.bound_diff(full, max_bytes)
    out = {"diff": shown, "diff_bytes": len(full.encode()), "truncated": cut, "full_output_path": None}
    if cut:
        out["full_output_path"] = save_diff(root, full)
    return out


def save_diff(root: Path, text: str) -> str:
    d = CACHE / "diffs"
    d.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", root.name)[:60] or "project"
    p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}-{os.getpid()}.diff"
    common.atomic_write(p, text)
    common.prune(list(d.glob("*.diff")), keep=DIFF_KEEP)
    return str(p)


def _int(flags: dict, key: str, default: int) -> int:
    v = flags.get(key)
    if v is None or v is True:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise HelperError("usage", f"--{key.replace('_', '-')} must be a number.") from None


def _max_bytes(flags: dict) -> int:
    return max(500, min(MAX_MAX_BYTES, _int(flags, "max_bytes", DEFAULT_MAX_BYTES)))


def _products_flag(flags: dict) -> list[str]:
    out: list[str] = []
    p = flags.get("product")
    if p and p is not True:
        out.append(str(p))
        out += [str(x) for x in flags.get("_") or []]
    ps = flags.get("products")
    if ps and ps is not True:
        try:
            v = json.loads(ps)
        except json.JSONDecodeError:
            v = [x.strip() for x in str(ps).split(",")]
        if not isinstance(v, list):
            raise HelperError("usage", "--products must be a JSON list of names.")
        out += [str(x) for x in v]
    return list(dict.fromkeys(x.strip() for x in out if x and x.strip()))


# ================================================================ URL and version checks

URL_RX = re.compile(r"^(https?://[^\s/]+/\S+|ssh://\S+|git@[^\s:]+:\S+|file://\S+)$")
VERSION_RX = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
REVISION_RX = re.compile(r"^[0-9a-fA-F]{7,40}$")
BRANCH_RX = re.compile(r"^[^\s~^:?*\[\\]+$")


def check_location(raw: str, root: Path) -> tuple[str, str, str]:
    """(kind 'url'|'path', the location to write, identity)."""
    s = str(raw or "").strip()
    if not s:
        raise _error("bad-url", "Enter the package's URL.", "Paste a git URL such as "
                                                              "https://github.com/apple/swift-collections.")
    if s.startswith(("/", "./", "../", "~")) or s in (".", ".."):
        p = Path(os.path.expanduser(s))
        full = p if p.is_absolute() else root / p
        if not (full / "Package.swift").is_file():
            raise _error("bad-url", f"There's no Swift package (Package.swift) in {s}.",
                         "Pick the folder that contains the package's Package.swift.")
        if Path(os.path.realpath(full)) == root:
            raise _error("bad-url", "A project can't depend on itself.")
        return "path", s, M.identity_of(str(Path(os.path.realpath(full))))
    s = s.rstrip("/")
    if s.startswith("http://"):
        raise _error("bad-url", "Use the https:// URL of the package.", "SwiftPM fetches packages over https or ssh.")
    if not URL_RX.match(s) or any(c in s for c in "\"'\\`$"):
        raise _error("bad-url", "That doesn't look like a package URL.",
                     "Paste a git URL such as https://github.com/apple/swift-collections (or git@host:owner/repo.git).")
    if re.match(r"^https://(www\.)?github\.com/[^/]+/[^/]+/(tree|blob|releases)/", s):
        s = "/".join(s.split("/")[:5])        # a page inside the repository -> the repository
    return "url", s, M.identity_of(s)


def check_requirement(rule: str, version: str | None) -> None:
    if rule not in RULES:
        raise _error("bad-rule", f"Unknown version rule '{rule}'.", f"Use one of: {', '.join(RULES)}.")
    v = (version or "").strip()
    if rule in ("from", "upToNextMinor", "exact") and not VERSION_RX.match(v):
        raise _error("bad-version", f"'{v}' isn't a version like 1.2.0.", "Versions have three numbers: 1.2.0.")
    if rule == "branch" and not BRANCH_RX.match(v):
        raise _error("bad-version", "Enter the branch name.", "For example main.")
    if rule == "revision" and not REVISION_RX.match(v):
        raise _error("bad-version", f"'{v}' isn't a commit hash.", "Use 7 to 40 hexadecimal characters.")


def latest_version(url: str) -> str | None:
    """Newest release tag (no pre-releases) of a git repository, or None."""
    git = shutil.which("git")
    if not git:
        return None
    # Own session, killed whole on timeout (git-remote-https / ssh children too); never prompts for a password.
    try:
        proc = subprocess.Popen([git, "ls-remote", "--tags", "--refs", url], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env={**swift_env(), "GIT_TERMINAL_PROMPT": "0"},
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    try:
        out, err = proc.communicate(timeout=LS_REMOTE_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid, grace=0.5)
        common.kill_proc(proc)
        return None
    except BaseException:
        _kill_group(proc.pid, grace=0.5)
        common.kill_proc(proc)
        raise
    r = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    if r.returncode != 0:
        raise swift_failure((r.stderr or "").splitlines() or ["error: Failed to fetch " + url],
                            "Couldn't read the package's versions")
    return newest_tag(r.stdout.splitlines())


def newest_tag(lines: list[str]) -> str | None:
    best: tuple[tuple[int, int, int], str] | None = None
    for ln in lines:
        ref = ln.split("\t")[-1].strip()
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/"):]
        m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", tag)
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if best is None or key > best[0]:
            best = (key, tag.lstrip("v"))
    return best[1] if best else None


# ================================================================ commands

def cmd_list(flags: dict) -> int:
    root = project_root(flags)
    try:
        return common.ok(list_data(root, use_cache=not flags.get("fresh")))
    except HelperError as exc:
        return fail(exc)


def _entry(root: Path, ident: str) -> dict | None:
    try:
        data = list_data(root, use_cache=False)
    except HelperError:
        return None
    return next((p for p in data["packages"] if p["identity"] == ident), None)


def _check_target(dump: dict, root: Path, target: str | None) -> str:
    names = [str(t.get("name")) for t in dump.get("targets") or [] if isinstance(t, dict)]
    if target:
        if target not in names:
            raise _error("unknown-target", f"There's no target named {target}.",
                         f"Targets: {', '.join(names[:20]) or 'none'}.", targets=names[:50])
        return target
    app = app_target(root, dump)
    if not app:
        raise _error("unknown-target", "This package has no target to link the package to.",
                     "Add a target to Package.swift first.")
    return app


def _link(root: Path, dump: dict, ident: str, products: list[str], target: str, run=None) -> list[str]:
    """add-target-dependency for each product not linked yet. Returns the products added."""
    present = set()
    for t in M.target_deps(dump):
        if t["target"] == target:
            present = {d["name"] for d in t["deps"] if d["kind"] in ("product", "byName")}
    added = []
    for prod in products:
        if prod in present:
            continue
        args = ["package", "add-target-dependency", prod, target, "--package", ident]
        if run:
            rc, lines = run(args)
        else:
            r = run_swift(args, root)
            rc, lines = r.returncode, (r.stdout + r.stderr).splitlines()
        if rc != 0:
            raise swift_failure(lines, f"Couldn't link {prod} to {target}")
        added.append(prod)
    return added


def _verify_link(root: Path, ident: str, products: list[str], target: str) -> dict:
    dump = dump_package(root, use_cache=False)
    have = set()
    for t in M.target_deps(dump):
        if t["target"] == target:
            have = {d["name"] for d in t["deps"] if d["kind"] in ("product", "byName")}
    missing = [p for p in products if p not in have]
    if missing:
        raise _error("edit-invalid", f"Package.swift doesn't list {', '.join(missing)} for {target} after the edit.",
                     "Add the product to the target's dependencies by hand.")
    return dump


def cmd_add(flags: dict) -> int:
    if flags.get("dry_run"):
        return _add_dry_run(flags)
    stream = Stream(STEPS_ADD)
    runner = Runner()
    try:
        root = project_root(flags)
        with runner, project_lock(root):
            return _add(flags, root, stream, runner)
    except Cancelled:
        stream.close("cancelled")
        return done_fail(_error("cancelled", "Cancelled; Package.swift is as it was.", restored=True))
    except HelperError as exc:
        stream.close("failed")
        return done_fail(exc)


def _add(flags: dict, root: Path, stream: Stream, runner: Runner) -> int:
    t0 = time.monotonic()
    rule = str(flags.get("rule") or "from")
    version = flags.get("version")
    version = None if version in (None, True) else str(version).strip()
    wanted = _products_flag(flags)
    max_bytes = _max_bytes(flags)

    stream.start("check", fraction=0.02)
    kind, loc, ident = check_location(str(flags.get("url") or ""), root)
    dump = dump_package(root, use_cache=False)
    existing = find_declared(dump, ident) or find_declared(dump, loc)
    if existing:
        raise _error("already-added", f"{existing['identity']} is already in Package.swift.",
                     "Link one of its products to a target instead.", identity=existing["identity"],
                     package=_entry(root, existing["identity"]))
    target = _check_target(dump, root, str(flags["target"]) if flags.get("target") not in (None, True) else None)
    if kind == "path":
        rule = "local"
    elif not version and rule in ("from", "upToNextMinor", "exact"):
        stream.detail("Finding the newest version", 0.04)
        runner.poll_cancel()
        version = latest_version(loc)
        if not version:
            raise _error("no-versions", f"{ident} has no release versions.",
                         "Pick Branch (for example main) or Revision instead.")
    if kind != "path":
        check_requirement(rule, version)
    req = M.requirement_text({"rule": rule, "version": version, "branch": version, "revision": version})
    stream.finish("check", detail=f"{ident} {req}")
    runner.poll_cancel()

    backup = Backup(root)
    before = backup.manifest.decode("utf-8", "replace")
    try:
        stream.start("add", fraction=0.1)
        args = ["package", "add-dependency", loc]
        if kind == "path":
            args += ["--type", "path"]
        else:
            args += {"from": ["--from"], "upToNextMinor": ["--up-to-next-minor-from"], "exact": ["--exact"],
                     "branch": ["--branch"], "revision": ["--revision"]}[rule] + [str(version)]
        rc, lines = runner.run(args, root, common.log)
        if rc != 0:
            raise swift_failure(lines, "Couldn't add the package to Package.swift")
        _forget_dump(root)
        stream.finish("add")

        stream.start("fetch", f"Fetching {ident}", fraction=0.15)
        fetched = [0]

        def on_progress(pd: tuple[str, str | None, bool]) -> None:
            fetched[0] += 1 if pd[2] else 0
            stream.detail(pd[0], round(0.15 + 0.6 * (1 - 0.8 ** fetched[0]), 3))

        rc, lines = runner.run(["package", "resolve"], root, LineSink(on_progress))
        if rc != 0:
            raise swift_failure(lines, f"Couldn't fetch {ident}")
        stream.finish("fetch")

        stream.start("products", fraction=0.78)
        runner.poll_cancel()
        co = checkouts(root)
        pkg_dir = (co.get(ident) or {}).get("path") or (str((root / loc).resolve()) if kind == "path" else None)
        products = products_of(pkg_dir) if pkg_dir else []
        if not products:
            raise _error("no-products", f"{ident} has no library products to link.",
                         "Only packages with library products can be used by an app.")
        stream.finish("products", detail=", ".join(products[:8]) + ("…" if len(products) > 8 else ""))

        chosen = wanted or ([pick_product(products)] if pick_product(products) else [])
        unknown = [p for p in chosen if p not in products]
        if unknown:
            raise _error("unknown-product", f"{ident} has no product named {', '.join(unknown)}.",
                         f"Products: {', '.join(products[:20])}.", products=products)
        added: list[str] = []
        if chosen:
            stream.start("link", f"Linking {', '.join(chosen)} to {target}", fraction=0.88)
            added = _link(root, dump_package(root, use_cache=False), ident, chosen, target,
                          run=lambda a: runner.run(a, root, common.log))
            _forget_dump(root)
            stream.finish("link")
        else:
            stream.finish("link", "skipped", detail="Choose a product")
        stream.start("validate", fraction=0.95)
        runner.poll_cancel()
        if chosen:
            _verify_link(root, ident, chosen, target)
        else:
            dump_package(root, use_cache=False)
        stream.finish("validate")
    except BaseException:
        runner.quiet()
        backup.restore()
        raise
    after = (root / "Package.swift").read_text(errors="replace")
    data = {
        "package": _entry(root, ident), "identity": ident, "target": target, "products": products,
        "product_added": added[0] if added else None, "products_added": added, "needs_product": not chosen,
        "requirement": req, **manifest_diff(root, before, after, max_bytes),
        "next": "Check build" if chosen else "Choose a product",
        "hint": HINT_BUILD_AFTER if chosen else f"{ident} has {len(products)} products: choose which to link.",
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }
    common.progress(1.0, "Done")
    common.event("done", ok=True, data=data)
    return 0


def _add_dry_run(flags: dict) -> int:
    """What `add` would write, from add-dependency on a copy of the manifest; nothing is fetched or changed."""
    try:
        root = project_root(flags)
        rule = str(flags.get("rule") or "from")
        version = flags.get("version")
        version = None if version in (None, True) else str(version).strip()
        kind, loc, ident = check_location(str(flags.get("url") or ""), root)
        dump = dump_package(root)
        existing = find_declared(dump, ident) or find_declared(dump, loc)
        if existing:
            raise _error("already-added", f"{existing['identity']} is already in Package.swift.",
                         "Link one of its products to a target instead.", identity=existing["identity"])
        target = _check_target(dump, root, str(flags["target"]) if flags.get("target") not in (None, True) else None)
        if kind == "path":
            rule = "local"
        elif not version and rule in ("from", "upToNextMinor", "exact"):
            version = latest_version(loc)
            if not version:
                raise _error("no-versions", f"{ident} has no release versions.",
                             "Pick Branch (for example main) or Revision instead.")
        if kind != "path":
            check_requirement(rule, version)
        before = (root / "Package.swift").read_text(errors="replace")
        CACHE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(CACHE), prefix="dry-") as tmp:
            d = Path(tmp) / root.name
            d.mkdir()
            (d / "Package.swift").write_text(before)
            args = ["package", "add-dependency", loc]
            if kind == "path":
                args += ["--type", "path"]
            else:
                args += {"from": ["--from"], "upToNextMinor": ["--up-to-next-minor-from"], "exact": ["--exact"],
                         "branch": ["--branch"], "revision": ["--revision"]}[rule] + [str(version)]
            r = run_swift(args, d)
            if r.returncode != 0:
                raise swift_failure((r.stdout + r.stderr).splitlines(), "Couldn't add the package to Package.swift")
            after = (d / "Package.swift").read_text(errors="replace")
        req = M.requirement_text({"rule": rule, "version": version, "branch": version, "revision": version})
        data = {"dry_run": True, "identity": ident, "url": loc if kind == "url" else None,
                "path": loc if kind == "path" else None, "requirement": req, "target": target,
                **manifest_diff(root, before, after, _max_bytes(flags)),
                "hint": "Adding fetches the package and links its product to " + target + "."}
        common.event("done", ok=True, data=data)
        return 0
    except HelperError as exc:
        return done_fail(exc)


def cmd_add_product(flags: dict) -> int:
    try:
        root = project_root(flags)
        wanted = _products_flag(flags)
        if not wanted:
            raise HelperError("usage", "Missing --product.")
        with project_lock(root):
            dump = dump_package(root, use_cache=False)
            pkg = find_declared(dump, str(flags.get("package") or ""))
            if not pkg:
                raise _error("unknown-package", f"Package.swift has no package named {flags.get('package')}.",
                             "Add it first.")
            ident = pkg["identity"]
            target = _check_target(dump, root, str(flags["target"]) if flags.get("target") not in (None, True)
                                   else None)
            co = checkouts(root)
            pkg_dir = (co.get(ident) or {}).get("path") or pkg.get("path")
            products = products_of(pkg_dir) if pkg_dir and Path(pkg_dir).is_dir() else []
            if products:
                unknown = [p for p in wanted if p not in products]
                if unknown:
                    raise _error("unknown-product", f"{ident} has no product named {', '.join(unknown)}.",
                                 f"Products: {', '.join(products[:20])}.", products=products)
            backup = Backup(root)
            before = backup.manifest.decode("utf-8", "replace")
            try:
                added = _link(root, dump, ident, wanted, target)
                _forget_dump(root)
                _verify_link(root, ident, wanted, target)
            except BaseException:
                backup.restore()
                raise
            after = (root / "Package.swift").read_text(errors="replace")
            return common.ok({
                "package": _entry(root, ident), "identity": ident, "target": target, "products": products,
                "product_added": added[0] if added else None, "products_added": added, "needs_product": False,
                "already_linked": [p for p in wanted if p not in added],
                **manifest_diff(root, before, after, _max_bytes(flags)), "next": "Check build",
                "hint": HINT_BUILD_AFTER})
    except HelperError as exc:
        return fail(exc)


def _resolve_like(flags: dict, action: str) -> int:
    steps = STEPS_UPDATE if action == "update" else STEPS_RESOLVE
    stream = Stream(steps)
    runner = Runner()
    try:
        root = project_root(flags)
        with runner, project_lock(root):
            t0 = time.monotonic()
            dump = dump_package(root)
            args = ["package", action]
            only = None
            if action == "update" and flags.get("package") not in (None, True):
                pkg = find_declared(dump, str(flags["package"]))
                if not pkg:
                    raise _error("unknown-package", f"Package.swift has no package named {flags['package']}.",
                                 "Pick one from the list.")
                only = pkg["identity"]
                args.append(only)
            remote = [d for d in M.declared(dump) if d["kind"] != "local"]
            _exists, before, _ = read_resolved(root)
            total = max(1, len(before) or len(remote))
            backup = Backup(root)
            sid = steps[0][0]
            stream.start(sid, f"Updating {only}" if only else None, fraction=0.02)
            seen: set[str] = set()

            def on_progress(pd: tuple[str, str | None, bool]) -> None:
                if pd[2] and pd[1]:
                    seen.add(pd[1])
                stream.detail(pd[0], round(min(0.9, 0.05 + 0.85 * len(seen) / total), 3))

            try:
                rc, lines = runner.run(args, root, LineSink(on_progress))
                if rc != 0:
                    raise swift_failure(lines, "Updating failed" if action == "update" else "Resolving failed")
            except BaseException:
                runner.quiet()
                restored = backup.restore(resolved_only=True)
                if restored:
                    common.log("Package.resolved was put back as it was.")
                raise
            stream.finish(sid)
            stream.start("changes", fraction=0.95)
            _exists, after, perr = read_resolved(root)
            changes = M.resolved_changes(before, after)
            if only:
                changes = [c for c in changes if c["identity"] == only] + \
                          [c for c in changes if c["identity"] != only]
            up_to_date = any("Everything is already up-to-date" in ln for ln in lines)
            stream.finish("changes", detail=f"{len(changes)} changed" if changes else "No changes")
            data = list_data(root)
            out = {"changes": changes[:MAX_PACKAGES], "changed": len(changes), "up_to_date": not changes,
                   "swift_up_to_date": up_to_date, "package": only, "packages": data["packages"],
                   "unresolved": data["unresolved"], "resolved_file": data["resolved_file"],
                   "elapsed_ms": int((time.monotonic() - t0) * 1000),
                   "summary": summary(changes, action)}
            if perr:
                out["resolved_error"] = perr
            common.progress(1.0, "Done")
            common.event("done", ok=True, data=out)
            return 0
    except Cancelled:
        stream.close("cancelled")
        return done_fail(_error("cancelled", "Cancelled; Package.resolved is as it was.", restored=True))
    except HelperError as exc:
        stream.close("failed")
        return done_fail(exc)


def summary(changes: list[dict], action: str) -> str:
    if not changes:
        return "Everything is up to date" if action == "update" else "All packages are resolved"
    parts = []
    for c in changes[:3]:
        if c["from"] and c["to"]:
            parts.append(f"{c['identity']} {c['from']} → {c['to']}")
        elif c["to"]:
            parts.append(f"{c['identity']} {c['to']} (new)")
        else:
            parts.append(f"{c['identity']} removed")
    more = f" and {len(changes) - 3} more" if len(changes) > 3 else ""
    n = len(changes)
    return f"{n} package{'s' if n != 1 else ''} {'updated' if action == 'update' else 'changed'}: " \
           f"{', '.join(parts)}{more}"


def cmd_update(flags: dict) -> int:
    return _resolve_like(flags, "update")


def cmd_resolve(flags: dict) -> int:
    return _resolve_like(flags, "resolve")


def _transitive_only(root: Path, ident: str) -> set[str]:
    """Identities only `ident` brings in (from show-dependencies; empty when it can't be read offline)."""
    try:
        r = run_swift(["package", "--disable-automatic-resolution", "show-dependencies", "--format", "json"], root,
                      timeout=20, stdout_only=True)
        tree = json.loads(r.stdout) if r.returncode == 0 else None
    except (HelperError, json.JSONDecodeError):
        return set()
    if not isinstance(tree, dict):
        return set()

    def walk(node: dict, acc: set[str]) -> None:
        for d in node.get("dependencies") or []:
            i = str(d.get("identity") or "")
            if i and i not in acc:
                acc.add(i)
                walk(d, acc)

    under: set[str] = set()
    others: set[str] = set()
    for d in tree.get("dependencies") or []:
        i = str(d.get("identity") or "")
        if i == ident:
            walk(d, under)
        else:
            others.add(i)
            walk(d, others)
    return under - others - {ident}


def plan_remove(root: Path, wanted: str) -> dict:
    """The remove edit and its check, nothing written."""
    text = (root / "Package.swift").read_text(errors="replace")
    dump = dump_package(root, use_cache=False)
    pkg = find_declared(dump, wanted)
    if not pkg:
        names = ", ".join(d["identity"] for d in M.declared(dump)[:20]) or "none"
        raise _error("unknown-package", f"Package.swift has no package named {wanted}.", f"Packages: {names}.")
    ident = pkg["identity"]
    tdeps = M.target_deps(dump)
    local_targets = [t["target"] for t in tdeps]
    co = checkouts(root)
    pkg_dir = (co.get(ident) or {}).get("path") or pkg.get("path")
    products: list[str] = []
    if pkg_dir and Path(pkg_dir).is_dir():
        try:
            products = products_of(pkg_dir)
        except HelperError:
            products = []
    try:
        edit = M.remove_package(text, ident, url=pkg.get("url") or pkg.get("path"),
                                names=[pkg["name"]] if pkg.get("name") else [], products=products,
                                local_targets=local_targets)
    except M.LexError as exc:
        edit = M.RemoveEdit(text, problems=[f"Package.swift couldn't be read as Swift ({exc})."])
    problems = list(edit.problems)
    if not edit.removed_package and not problems:
        problems.append(f"The .package entry for {ident} wasn't found as a plain entry in Package.swift.")
    new_dump, verr = (None, None)
    if edit.text != text:
        new_dump, verr = validate_text(root, edit.text)
    if new_dump is not None:
        still = find_declared(new_dump, ident)
        if still:
            problems.append(f"{ident} is still declared after the edit.")
        names = {ident, str(pkg.get("name") or "").lower()}
        refs = [f"{t['target']}: {d['name']}" for t in M.target_deps(new_dump) for d in t["deps"]
                if (d["kind"] == "product" and str(d.get("package") or "").lower() in names)
                or (d["kind"] == "byName" and d["name"] in products and d["name"] not in local_targets)]
        if refs:
            problems.append("Still used after the edit by " + ", ".join(refs[:10]) + ".")
    diags = (getattr(verr, "extra", {}) or {}).get("diagnostics", []) if verr else []
    if verr:
        problems.append("Package.swift wouldn't compile after the edit: " + verr.message)
    return {"text": text, "new_text": edit.text, "identity": ident, "pkg": pkg,
            "target_deps": edit.target_deps, "problems": problems, "valid": not problems, "diagnostics": diags}


def cmd_remove(flags: dict) -> int:
    try:
        root = project_root(flags)
        wanted = flags.get("package")
        if not wanted or wanted is True:
            raise HelperError("usage", "Missing --package.")
        max_bytes = _max_bytes(flags)
        plan = plan_remove(root, str(wanted))
        diff = manifest_diff(root, plan["text"], plan["new_text"], max_bytes)
        would = {"package": plan["identity"], "target_deps": plan["target_deps"]}
        if not plan["valid"]:
            raise _error("edit-invalid", f"Removing {plan['identity']} can't be done safely: {plan['problems'][0]}",
                         "Nothing was changed. Edit Package.swift by hand.", problems=plan["problems"],
                         diagnostics=plan["diagnostics"], would_remove=would, valid=False, **diff)
        base = {"identity": plan["identity"], "would_remove": would, "valid": True, **diff}
        if not flags.get("confirm"):
            return common.ok({**base, "dry_run": True, "removed": False,
                              "hint": f"Removing changes Package.swift as shown and drops the pins of "
                                      f"{plan['identity']} from Package.resolved."})
        with project_lock(root):
            if (root / "Package.swift").read_text(errors="replace") != plan["text"]:
                raise _error("changed", "Package.swift changed while this was open.", "Look at the change again.")
            transitive = _transitive_only(root, plan["identity"])
            backup = Backup(root)
            try:
                _write_bytes(root / "Package.swift", plan["new_text"].encode("utf-8"), backup.mode)
                _forget_dump(root)
                dump_package(root, use_cache=False)
            except HelperError as exc:
                backup.restore()
                raise _error("edit-invalid", f"Package.swift didn't check out after the edit, so it was put back: "
                                             f"{exc.message}", "Nothing was changed. Edit Package.swift by hand.",
                             diagnostics=(getattr(exc, "extra", {}) or {}).get("diagnostics"), valid=False, **diff)
            except BaseException:
                backup.restore()
                raise
            pins_removed: list[str] = []
            rp = root / "Package.resolved"
            if rp.is_file():
                try:
                    old_pins = M.parse_resolved(rp.read_text())
                    drop = ({plan["identity"]} | transitive) & set(old_pins)
                    if drop:
                        new = M.drop_pins(rp.read_text(), drop)
                        if new is None:
                            rp.unlink()
                        else:
                            common.atomic_write(rp, new)
                        pins_removed = sorted(drop)
                except (ValueError, OSError):
                    pass
            return common.ok({**base, "dry_run": False, "removed": True, "pins_removed": pins_removed,
                              "next": "Check build", "hint": HINT_BUILD_REMOVED})
    except HelperError as exc:
        return fail(exc)


GROUPS = {"packages": {"list": cmd_list, "add": cmd_add, "add-product": cmd_add_product, "update": cmd_update,
                       "resolve": cmd_resolve, "remove": cmd_remove}}


# ================================================================ demo

DEMO_PACKAGES = {
    "WeatherGlance": [
        {"identity": "swift-collections", "name": "swift-collections", "kind": "remote",
         "url": "https://github.com/apple/swift-collections",
         "requirement": {"rule": "from", "version": "1.1.0", "lower": "1.1.0", "upper": "2.0.0", "text": "from 1.1.0"},
         "resolved": {"version": "1.1.4", "revision": "671108c"}, "used_by": ["WeatherGlance"],
         "products_used": ["OrderedCollections"]},
        {"identity": "swift-algorithms", "name": "swift-algorithms", "kind": "remote",
         "url": "https://github.com/apple/swift-algorithms",
         "requirement": {"rule": "upToNextMinor", "version": "1.2.0", "lower": "1.2.0", "upper": "1.3.0",
                         "text": "up to next minor from 1.2.0"},
         "resolved": {"version": "1.2.0", "revision": "f6919dc"}, "used_by": ["WeatherGlance"],
         "products_used": ["Algorithms"]},
        {"identity": "weatherkit-mock", "name": "WeatherKitMock", "kind": "local", "path": "../WeatherKitMock",
         "requirement": {"rule": "local", "text": "local"}, "resolved": None, "used_by": ["WeatherGlance"],
         "products_used": ["WeatherKitMock"]},
    ],
    "HabitTracker": [
        {"identity": "kingfisher", "name": "Kingfisher", "kind": "remote", "url": "https://github.com/onevcat/Kingfisher",
         "requirement": {"rule": "from", "version": "8.0.0", "lower": "8.0.0", "upper": "9.0.0", "text": "from 8.0.0"},
         "resolved": {"version": "8.1.3", "revision": "3ec0ab0"}, "used_by": ["HabitTracker"],
         "products_used": ["Kingfisher"]},
        {"identity": "swift-algorithms", "name": "swift-algorithms", "kind": "remote",
         "url": "https://github.com/apple/swift-algorithms",
         "requirement": {"rule": "branch", "branch": "main", "text": "branch main"},
         "resolved": {"branch": "main", "revision": "b14b7f4"}, "used_by": ["HabitTracker"],
         "products_used": ["Algorithms"]},
        {"identity": "confetti-swiftui", "name": "ConfettiSwiftUI", "kind": "remote",
         "url": "https://github.com/simibac/ConfettiSwiftUI",
         "requirement": {"rule": "exact", "version": "1.1.0", "text": "exactly 1.1.0"},
         "resolved": None, "used_by": [], "products_used": []},
    ],
}
DEMO_PRODUCTS = {
    "swift-collections": ["Collections", "DequeModule", "OrderedCollections", "HeapModule", "BitCollections"],
    "swift-algorithms": ["Algorithms"], "kingfisher": ["Kingfisher"], "swift-argument-parser": ["ArgumentParser"],
}
DEMO_UPDATES = {"WeatherGlance": [{"identity": "swift-collections", "from": "1.1.4", "to": "1.2.1"}],
                "HabitTracker": [{"identity": "kingfisher", "from": "8.1.3", "to": "8.3.2"},
                                 {"identity": "swift-algorithms", "from": "main@b14b7f4", "to": "main@2e0b8a7"}]}


def _demo_project(flags: dict) -> tuple[str, str]:
    raw = str(flags.get("project") or flags.get("path") or "")
    if not raw or raw == "True":
        raise HelperError("usage", "Missing --project.")
    return raw.rstrip("/").split("/")[-1], raw.rstrip("/")


def _demo_list(name: str, path: str) -> dict:
    pkgs = DEMO_PACKAGES.get(name, [])
    return {"path": path, "package": name, "packages": pkgs, "total": len(pkgs), "truncated": False,
            "targets": [{"name": name, "type": "regular", "app": True},
                        {"name": f"{name}Tests", "type": "test", "app": False}],
            "app_target": name, "resolved_file": bool(pkgs),
            "unresolved": [p["identity"] for p in pkgs if p["kind"] != "local" and not p["resolved"]],
            "manifest_mtime": time.time() - 3600, "elapsed_ms": 140, "demo": True}


def _demo_steps(steps: list[tuple[str, str]], details: dict[str, list[str]], pause: float = 0.35) -> None:
    for sid, title in steps:
        common.step(sid, title, "pending")
    frac = 0.0
    for i, (sid, title) in enumerate(steps):
        common.step(sid, title, "running")
        for d in details.get(sid, []):
            time.sleep(common.demo_delay(pause))
            frac = min(0.95, frac + 0.12)
            common.step(sid, title, "running", detail=d)
            common.progress(round(frac, 2), d)
            common.log(d)
        time.sleep(common.demo_delay(pause))
        common.step(sid, title, "done")


def _demo_diff(before: str, after: str) -> dict:
    import format as swift_format
    full = swift_format.unified("Package.swift", before, after)
    return {"diff": full, "diff_bytes": len(full.encode()), "truncated": False, "full_output_path": None}


def demo(action: str, flags: dict) -> int:
    """Demo mode: canned packages for the demo projects; progress is made up and nothing is written."""
    try:
        name, path = _demo_project(flags)
    except HelperError as exc:
        return fail(exc)
    pkgs = DEMO_PACKAGES.get(name, [])
    manifest = ("    dependencies: [\n" + "".join(f'        .package(url: "{p.get("url")}", from: "1.0.0"),\n'
                                                   for p in pkgs if p["kind"] == "remote") + "    ],\n")
    if action == "list":
        return common.ok(_demo_list(name, path))
    if action == "add":
        url = str(flags.get("url") or "")
        try:
            kind, loc, ident = check_location(url, Path(path)) if not url.startswith(("/", ".", "~")) \
                else ("path", url, M.identity_of(url))
        except HelperError as exc:
            return done_fail(exc) if not flags.get("dry_run") else done_fail(exc)
        if any(p["identity"] == ident for p in pkgs):
            return done_fail(_error("already-added", f"{ident} is already in Package.swift.",
                                    "Link one of its products to a target instead.", identity=ident,
                                    package=next(p for p in pkgs if p["identity"] == ident)))
        rule = str(flags.get("rule") or "from")
        version = str(flags.get("version") or "1.2.0") if flags.get("version") not in (None, True) else "1.2.0"
        req = M.requirement_text({"rule": rule if kind == "url" else "local", "version": version, "branch": version,
                                  "revision": version})
        products = DEMO_PRODUCTS.get(ident, [ident.replace("-", " ").title().replace(" ", "")])
        wanted = _products_flag(flags)
        chosen = wanted or ([products[0]] if len(products) == 1 else [])
        target = str(flags.get("target") or name)
        flag = {"from": "from", "upToNextMinor": "upToNextMinor", "exact": "exact", "branch": "branch",
                "revision": "revision"}.get(rule, "from")
        after = manifest.replace("    ],\n", f'        .package(url: "{loc}", {flag}: "{version}"),\n    ],\n')
        diff = _demo_diff(manifest, after)
        if flags.get("dry_run"):
            common.event("done", ok=True, data={"dry_run": True, "identity": ident, "url": loc, "requirement": req,
                                                "target": target, **diff, "demo": True})
            return 0
        _demo_steps(STEPS_ADD if chosen else STEPS_ADD[:4],
                    {"check": [f"{ident} {req}"], "fetch": [f"Fetching {ident}", f"Fetched {ident}",
                                                           f"{ident} {version}"],
                     "products": [", ".join(products)]})
        if not chosen:
            common.step("link", "Linking to the target", "skipped", detail="Choose a product")
            common.step("validate", "Checking Package.swift", "done")
        entry = {"identity": ident, "name": ident, "kind": "remote" if kind == "url" else "local",
                 "requirement": {"rule": rule, "version": version, "text": req}, "resolved": {"version": version},
                 "used_by": [target] if chosen else [], "products_used": chosen, "url": loc}
        common.event("done", ok=True, data={
            "package": entry, "identity": ident, "target": target, "products": products,
            "product_added": chosen[0] if chosen else None, "products_added": chosen, "needs_product": not chosen,
            "requirement": req, **diff, "next": "Check build" if chosen else "Choose a product",
            "hint": HINT_BUILD_AFTER if chosen else f"{ident} has {len(products)} products: choose which to link.",
            "elapsed_ms": 2100, "demo": True, "note": "Demo mode: nothing was written."})
        return 0
    if action == "add-product":
        ident = str(flags.get("package") or "")
        products = DEMO_PRODUCTS.get(ident, [])
        wanted = _products_flag(flags)
        target = str(flags.get("target") or name)
        line = "".join(f'                .product(name: "{p}", package: "{ident}"),\n' for p in wanted)
        diff = _demo_diff("            dependencies: [\n            ]\n",
                          f"            dependencies: [\n{line}            ]\n")
        return common.ok({"package": {"identity": ident, "name": ident, "kind": "remote", "used_by": [target],
                                      "products_used": wanted, "requirement": {"rule": "from", "text": "from 1.2.0"},
                                      "resolved": {"version": "1.2.0"}},
                          "identity": ident, "target": target, "products": products,
                          "product_added": wanted[0] if wanted else None, "products_added": wanted,
                          "needs_product": False, "already_linked": [], **diff, "next": "Check build",
                          "hint": HINT_BUILD_AFTER, "demo": True, "note": "Demo mode: nothing was written."})
    if action in ("update", "resolve"):
        only = flags.get("package") if flags.get("package") not in (None, True) else None
        changes = DEMO_UPDATES.get(name, []) if action == "update" else []
        if only:
            changes = [c for c in changes if c["identity"] == only]
        steps = STEPS_UPDATE if action == "update" else STEPS_RESOLVE
        names = [only] if only else [p["identity"] for p in pkgs if p["kind"] == "remote"]
        _demo_steps(steps, {steps[0][0]: [f"Updating {n}" for n in names] + [f"Updated {n}" for n in names]})
        common.event("done", ok=True, data={
            "changes": changes, "changed": len(changes), "up_to_date": not changes, "package": only,
            "packages": pkgs, "unresolved": [], "resolved_file": True, "elapsed_ms": 1800,
            "summary": summary(changes, action), "demo": True, "note": "Demo mode: nothing was written."})
        return 0
    if action == "remove":
        ident = str(flags.get("package") or "")
        pkg = next((p for p in pkgs if p["identity"] == ident or p["name"] == ident), None)
        if not pkg:
            return fail(_error("unknown-package", f"Package.swift has no package named {ident}.",
                               f"Packages: {', '.join(p['identity'] for p in pkgs) or 'none'}."))
        target_deps = [{"target": t, "product": pr} for t in pkg["used_by"] for pr in pkg["products_used"]]
        before = manifest + "".join(f'                .product(name: "{d["product"]}", package: "{ident}"),\n'
                                    for d in target_deps)
        after = before
        for ln in before.splitlines(keepends=True):
            if ident in ln or (pkg.get("url") and pkg["url"] in ln):
                after = after.replace(ln, "", 1)
        diff = _demo_diff(before, after)
        base = {"identity": pkg["identity"], "would_remove": {"package": pkg["identity"], "target_deps": target_deps},
                "valid": True, **diff, "demo": True}
        if not flags.get("confirm"):
            return common.ok({**base, "dry_run": True, "removed": False})
        return common.ok({**base, "dry_run": False, "removed": True, "pins_removed": [pkg["identity"]],
                          "next": "Check build", "hint": HINT_BUILD_REMOVED, "note": "Demo mode: nothing was written."})
    return fail(HelperError("usage", f"Unknown command 'packages {action}'."))
