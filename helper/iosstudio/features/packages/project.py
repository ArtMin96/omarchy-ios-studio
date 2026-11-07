"""A project's package files: the folder and its lock, backups, cached dump-package, checkouts, products and the list."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from iosstudio.core import appdirs, fileio, xtool_yml
from iosstudio.core.output import HelperError
from iosstudio.features.packages import manifest as M
from iosstudio.features.packages.swift import manifest_error, run_swift

CACHE = appdirs.CACHE_DIR / "packages"
LOCKS = appdirs.STATE_DIR / "packages"
DIFF_KEEP = 10
CACHE_KEEP = 100              # per kind of cached file (manifest descriptions, product lists, scratch)
MAX_PACKAGES = 200
MAX_PRODUCTS = 100
DUMP_TIMEOUT = 120
TARGET_TYPES_FIRST = ("executable", "regular")


# ================================================================ project files

def project_root(flags: dict) -> Path:
    """--project (or --path) as a real path to a folder with a Package.swift."""
    raw = flags.get("project") or flags.get("path")
    if not raw or raw is True:
        raise HelperError("usage", "Missing --project.")
    p = Path(os.path.realpath(os.path.expanduser(str(raw))))
    if not p.is_dir():
        raise HelperError("not-found", "That project folder doesn't exist anymore.")
    if not (p / "Package.swift").is_file():
        raise HelperError("not-a-project", "That folder has no Package.swift.")
    return p


@contextmanager
def project_lock(root: Path) -> Iterator[None]:
    """One mutating package command per project (the app and the MCP server share it)."""
    with ExitStack() as stack:
        try:
            stack.enter_context(fileio.lock(LOCKS / f"{fileio.path_key(root)}.lock", blocking=False))
        except OSError:
            raise HelperError("package-busy", "Another package change for this project is still running. Wait for "
                                              "it to finish, then try again.") from None
        yield


def _read_or_none(p: Path) -> bytes | None:
    try:
        return p.read_bytes()
    except OSError:
        return None


class Backup:
    """Package.swift and Package.resolved as they were before the first edit."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest = (root / "Package.swift").read_bytes()
        self.mode = (root / "Package.swift").stat().st_mode & 0o7777
        rp = root / "Package.resolved"
        self.resolved = rp.read_bytes() if rp.is_file() else None

    def restore(self, resolved_only: bool = False) -> bool:
        """Put the files back if they changed. Returns True when something was restored."""
        did = False
        mp = self.root / "Package.swift"
        if not resolved_only and _read_or_none(mp) != self.manifest:
            fileio.atomic_write(mp, self.manifest, self.mode)
            did = True
        rp = self.root / "Package.resolved"
        if _read_or_none(rp) != self.resolved:
            if self.resolved is None:
                rp.unlink(missing_ok=True)
            else:
                fileio.atomic_write(rp, self.resolved, 0o644)
            did = True
        if did:
            forget_dump(self.root)
        return did


# ================================================================ dump-package

def _stamp(p: Path) -> list | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _dump_cache(root: Path) -> Path:
    return CACHE / "dump" / f"{fileio.path_key(root)}.json"


def forget_dump(root: Path) -> None:
    """Drop the cached dump-package of a project (after editing its Package.swift)."""
    _dump_cache(root).unlink(missing_ok=True)


def dump_package(root: Path, use_cache: bool = True) -> dict:
    """`swift package dump-package` of a project, cached by Package.swift's mtime and size."""
    stamp = _stamp(root / "Package.swift")
    cache = _dump_cache(root)
    if use_cache:
        entry = fileio.load_json(cache)
        if isinstance(entry, dict) and entry.get("stamp") == stamp and isinstance(entry.get("data"), dict):
            return entry["data"]
    data = _dump_dir(root, CACHE / "scratch" / fileio.path_key(root))
    try:
        fileio.save_json(cache, {"stamp": stamp, "data": data})
    except OSError:
        pass
    _prune_cache()
    return data


def _prune_cache() -> None:
    """Keep the newest CACHE_KEEP manifest descriptions, product lists and manifest scratch folders."""
    for sub in ("dump", "products", "scratch"):
        d = CACHE / sub
        if d.is_dir():
            fileio.prune(list(d.iterdir()), keep=CACHE_KEEP)


def _dump_dir(pkg_dir: Path, scratch: Path) -> dict:
    """dump-package of a package folder, with its own scratch folder (a build's lock on .build/ never blocks it)."""
    scratch.mkdir(parents=True, exist_ok=True)
    r = run_swift(["package", "--package-path", str(pkg_dir), "--scratch-path", str(scratch), "dump-package"],
                  pkg_dir, timeout=DUMP_TIMEOUT)
    if r.returncode != 0:
        raise manifest_error((r.stderr or r.stdout or "").splitlines())
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise HelperError("manifest-invalid", "swift package dump-package didn't print the package description.",
                          detail=(r.stdout + r.stderr)[-2000:]) from None
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
    data = fileio.load_json(root / ".build" / "workspace-state.json") or {}
    out: dict[str, dict] = {}
    for dep in (data.get("object") or {}).get("dependencies") or []:
        ref = dep.get("packageRef") or {}
        ident = str(ref.get("identity") or "")
        if not ident:
            continue
        if ref.get("kind") == "fileSystem":
            path = str(ref.get("location") or "")
        else:
            sub = dep.get("subpath") or ref.get("name") or ident
            state = (dep.get("state") or {}).get("name")
            base = "checkouts" if state != "edited" else "../Packages"
            path = str(root / ".build" / base / str(sub))
        out[ident] = {"path": path, "name": ref.get("name")}
    return out


def package_dir(root: Path, ident: str, fallback: str | None = None) -> str | None:
    """The folder of a package: its checkout (or edited copy), else `fallback` (its declared local path)."""
    return (checkouts(root).get(ident) or {}).get("path") or fallback


def products_of(pkg_dir: str | Path) -> list[str]:
    """Library product names of a package folder (its manifest dumped with a throwaway scratch folder, cached by
    the manifest's stamp). Underscored products (`_RopeModule`) come last."""
    p = Path(pkg_dir)
    stamp = _stamp(p / "Package.swift")
    if stamp is None:
        return []
    cache = CACHE / "products" / f"{fileio.path_key(p)}.json"
    entry = fileio.load_json(cache)
    if isinstance(entry, dict) and entry.get("stamp") == stamp and isinstance(entry.get("products"), list):
        return entry["products"]
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(CACHE), prefix="products-") as tmp:
        data = _dump_dir(p, Path(tmp))
    names = [str(x.get("name")) for x in data.get("products") or []
             if isinstance(x, dict) and "library" in (x.get("type") or {})]
    names = sorted(dict.fromkeys(names), key=lambda n: n.startswith("_"))
    try:
        fileio.save_json(cache, {"stamp": stamp, "products": names, "name": data.get("name")})
    except OSError:
        pass
    return names[:MAX_PRODUCTS]


# ================================================================ targets

def app_target(root: Path, dump: dict) -> str | None:
    """The app's target: xtool.yml's product (a library product) -> its first target; else the first library
    product's first target; else the first executable or regular target."""
    targets = [t for t in dump.get("targets") or [] if isinstance(t, dict)]
    names = {str(t.get("name")): str(t.get("type") or "regular") for t in targets}
    libs = [p for p in dump.get("products") or [] if isinstance(p, dict) and "library" in (p.get("type") or {})]
    wanted = xtool_yml.read(root / "xtool.yml").get("product")
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
    """[{name, type, app}] with the app's target first and test targets last."""
    app = app_target(root, dump)
    out = [{"name": str(t.get("name")), "type": str(t.get("type") or "regular"), "app": str(t.get("name")) == app}
           for t in dump.get("targets") or [] if isinstance(t, dict)]
    rank = {"executable": 1, "regular": 1, "test": 3}
    out.sort(key=lambda t: (not t["app"], rank.get(t["type"], 2)))
    return out


def linked_products(dump: dict, target: str) -> set[str]:
    """The product (and bare-name) dependencies of one target."""
    have: set[str] = set()
    for t in M.target_deps(dump):
        if t["target"] == target:
            have = {d["name"] for d in t["deps"] if d["kind"] in ("product", "byName")}
    return have


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
    """The `packages list` result: declared packages with their pins and users, the targets and the app's."""
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


def list_entry(root: Path, ident: str) -> dict | None:
    """One package's list entry, read fresh; None when it can't be read."""
    try:
        data = list_data(root, use_cache=False)
    except HelperError:
        return None
    return next((p for p in data["packages"] if p["identity"] == ident), None)


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
        if loc and (M.normalize_location(loc) == M.normalize_location(w)
                    or M.identity_of(w) == d["identity"].lower() and ("/" in w or ":" in w)):
            return d
    return None


# ================================================================ diff

def manifest_diff(root: Path, before: str, after: str, max_bytes: int) -> dict:
    """{diff, diff_bytes, truncated, full_output_path} of a Package.swift edit (the whole diff saved when cut)."""
    from iosstudio.features import format as swift_format
    full = swift_format.unified("Package.swift", before, after)
    shown, cut = swift_format.bound_diff(full, max_bytes)
    out = {"diff": shown, "diff_bytes": len(full.encode()), "truncated": cut, "full_output_path": None}
    if cut:
        out["full_output_path"] = _save_diff(root, full)
    return out


def _save_diff(root: Path, text: str) -> str:
    d = CACHE / "diffs"
    d.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", root.name)[:60] or "project"
    p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}-{os.getpid()}.diff"
    fileio.atomic_write(p, text)
    fileio.prune(list(d.glob("*.diff")), keep=DIFF_KEEP)
    return str(p)
