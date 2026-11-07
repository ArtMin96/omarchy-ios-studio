"""Adding a package (add-dependency, resolve, link a product) and linking more of its products (add-product)."""
from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from iosstudio.core import output
from iosstudio.features.packages import manifest as M
from iosstudio.features.packages import project as P
from iosstudio.features.packages.swift import (
    LineSink,
    Stream,
    SwiftRunner,
    latest_version,
    package_error,
    run_swift,
    swift_failure,
)

RULES = ("from", "upToNextMinor", "exact", "branch", "revision")
VERSION_RULES = ("from", "upToNextMinor", "exact")
STEPS_ADD = [("check", "Checking the package"), ("add", "Adding to Package.swift"), ("fetch", "Fetching"),
             ("products", "Reading products"), ("link", "Linking to the target"), ("validate", "Checking Package.swift")]
HINT_BUILD_AFTER = "Check build compiles the app with the new package."

URL_RX = re.compile(r"^(https?://[^\s/]+/\S+|ssh://\S+|git@[^\s:]+:\S+|file://\S+)$")
VERSION_RX = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
REVISION_RX = re.compile(r"^[0-9a-fA-F]{7,40}$")
BRANCH_RX = re.compile(r"^[^\s~^:?*\[\\]+$")
RULE_FLAGS = {"from": "--from", "upToNextMinor": "--up-to-next-minor-from", "exact": "--exact",
              "branch": "--branch", "revision": "--revision"}

SwiftRun = Callable[[list[str]], tuple[int, list[str]]]


@dataclass
class AddRequest:
    """What `packages add` was asked for (from its flags)."""
    url: str
    rule: str = "from"
    version: str | None = None
    target: str | None = None
    products: list[str] = field(default_factory=list)


# ================================================================ checks

def check_location(raw: str, root: Path) -> tuple[str, str, str]:
    """(kind 'url'|'path', the location to write, identity)."""
    s = str(raw or "").strip()
    if not s:
        raise package_error("bad-url", "Enter the package's URL.", "Paste a git URL such as "
                                                                    "https://github.com/apple/swift-collections.")
    if s.startswith(("/", "./", "../", "~")) or s in (".", ".."):
        p = Path(os.path.expanduser(s))
        full = p if p.is_absolute() else root / p
        if not (full / "Package.swift").is_file():
            raise package_error("bad-url", f"There's no Swift package (Package.swift) in {s}.",
                                "Pick the folder that contains the package's Package.swift.")
        if Path(os.path.realpath(full)) == root:
            raise package_error("bad-url", "A project can't depend on itself.")
        return "path", s, M.identity_of(str(Path(os.path.realpath(full))))
    s = s.rstrip("/")
    if s.startswith("http://"):
        raise package_error("bad-url", "Use the https:// URL of the package.",
                            "SwiftPM fetches packages over https or ssh.")
    if not URL_RX.match(s) or any(c in s for c in "\"'\\`$"):
        raise package_error("bad-url", "That doesn't look like a package URL.",
                            "Paste a git URL such as https://github.com/apple/swift-collections "
                            "(or git@host:owner/repo.git).")
    if re.match(r"^https://(www\.)?github\.com/[^/]+/[^/]+/(tree|blob|releases)/", s):
        s = "/".join(s.split("/")[:5])        # a page inside the repository -> the repository
    return "url", s, M.identity_of(s)


def check_requirement(rule: str, version: str | None) -> None:
    """Raise bad-rule / bad-version unless `version` fits `rule`."""
    if rule not in RULES:
        raise package_error("bad-rule", f"Unknown version rule '{rule}'.", f"Use one of: {', '.join(RULES)}.")
    v = (version or "").strip()
    if rule in VERSION_RULES and not VERSION_RX.match(v):
        raise package_error("bad-version", f"'{v}' isn't a version like 1.2.0.", "Versions have three numbers: 1.2.0.")
    if rule == "branch" and not BRANCH_RX.match(v):
        raise package_error("bad-version", "Enter the branch name.", "For example main.")
    if rule == "revision" and not REVISION_RX.match(v):
        raise package_error("bad-version", f"'{v}' isn't a commit hash.", "Use 7 to 40 hexadecimal characters.")


def check_target(dump: dict, root: Path, target: str | None) -> str:
    """`target` if the package has it, else the app's target when none was asked for."""
    names = [str(t.get("name")) for t in dump.get("targets") or [] if isinstance(t, dict)]
    if target:
        if target not in names:
            raise package_error("unknown-target", f"There's no target named {target}.",
                                f"Targets: {', '.join(names[:20]) or 'none'}.", targets=names[:50])
        return target
    app = P.app_target(root, dump)
    if not app:
        raise package_error("unknown-target", "This package has no target to link the package to.",
                            "Add a target to Package.swift first.")
    return app


def pick_product(products: list[str]) -> str | None:
    """The product to link without asking: the only one (ignoring underscored ones), else None."""
    public = [p for p in products if not p.startswith("_")] or products
    return public[0] if len(public) == 1 else None


def _unknown_products(ident: str, wanted: list[str], products: list[str]) -> None:
    unknown = [p for p in wanted if p not in products]
    if unknown:
        raise package_error("unknown-product", f"{ident} has no product named {', '.join(unknown)}.",
                            f"Products: {', '.join(products[:20])}.", products=products)


def _plan(req: AddRequest, root: Path, load_dump: Callable[[], dict], on_lookup: Callable[[], None] | None = None,
          entry: bool = True) -> tuple[str, str, str, str, str | None, str]:
    """Checks shared by add and its dry run: (kind, location, identity, rule, version, target)."""
    kind, loc, ident = check_location(req.url, root)
    dump = load_dump()
    existing = P.find_declared(dump, ident) or P.find_declared(dump, loc)
    if existing:
        extra = {"package": P.list_entry(root, existing["identity"])} if entry else {}
        raise package_error("already-added", f"{existing['identity']} is already in Package.swift.",
                            "Link one of its products to a target instead.", identity=existing["identity"], **extra)
    target = check_target(dump, root, req.target)
    rule, version = req.rule, req.version
    if kind == "path":
        rule = "local"
    elif not version and rule in VERSION_RULES:
        if on_lookup:
            on_lookup()
        version = latest_version(loc)
        if not version:
            raise package_error("no-versions", f"{ident} has no release versions.",
                                "Pick Branch (for example main) or Revision instead.")
    if kind != "path":
        check_requirement(rule, version)
    return kind, loc, ident, rule, version, target


def _add_dependency_args(kind: str, loc: str, rule: str, version: str | None) -> list[str]:
    args = ["package", "add-dependency", loc]
    if kind == "path":
        return args + ["--type", "path"]
    return args + [RULE_FLAGS[rule], str(version)]


def requirement_text(rule: str, version: str | None) -> str:
    """'from 1.2.0', 'branch main', … for the requested rule."""
    return M.requirement_text({"rule": rule, "version": version, "branch": version, "revision": version})


# ================================================================ linking

def link(root: Path, dump: dict, ident: str, products: list[str], target: str, run: SwiftRun | None = None
         ) -> list[str]:
    """add-target-dependency for each product not linked yet. Returns the products added."""
    present = P.linked_products(dump, target)
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


def _verify_link(root: Path, products: list[str], target: str) -> dict:
    dump = P.dump_package(root, use_cache=False)
    have = P.linked_products(dump, target)
    missing = [p for p in products if p not in have]
    if missing:
        raise package_error("edit-invalid",
                            f"Package.swift doesn't list {', '.join(missing)} for {target} after the edit.",
                            "Add the product to the target's dependencies by hand.")
    return dump


# ================================================================ add

def add(req: AddRequest, root: Path, stream: Stream, runner: SwiftRunner, max_bytes: int) -> dict:
    """Add, fetch and link a package, with step events; files are put back on any failure or cancel."""
    t0 = time.monotonic()
    stream.start("check", fraction=0.02)

    def on_lookup() -> None:
        stream.detail("Finding the newest version", 0.04)
        runner.poll()

    kind, loc, ident, rule, version, target = _plan(req, root, lambda: P.dump_package(root, use_cache=False), on_lookup)
    req_text = requirement_text(rule, version)
    stream.finish("check", detail=f"{ident} {req_text}")
    runner.poll()

    backup = P.Backup(root)
    before = backup.manifest.decode("utf-8", "replace")
    try:
        stream.start("add", fraction=0.1)
        rc, lines = runner.swift(_add_dependency_args(kind, loc, rule, version), root, output.log)
        if rc != 0:
            raise swift_failure(lines, "Couldn't add the package to Package.swift")
        P.forget_dump(root)
        stream.finish("add")

        stream.start("fetch", f"Fetching {ident}", fraction=0.15)
        fetched = 0

        def on_progress(pd: tuple[str, str | None, bool]) -> None:
            nonlocal fetched
            fetched += 1 if pd[2] else 0
            stream.detail(pd[0], round(0.15 + 0.6 * (1 - 0.8 ** fetched), 3))

        rc, lines = runner.swift(["package", "resolve"], root, LineSink(on_progress))
        if rc != 0:
            raise swift_failure(lines, f"Couldn't fetch {ident}")
        stream.finish("fetch")

        stream.start("products", fraction=0.78)
        runner.poll()
        pkg_dir = P.package_dir(root, ident, str((root / loc).resolve()) if kind == "path" else None)
        products = P.products_of(pkg_dir) if pkg_dir else []
        if not products:
            raise package_error("no-products", f"{ident} has no library products to link.",
                                "Only packages with library products can be used by an app.")
        stream.finish("products", detail=", ".join(products[:8]) + ("…" if len(products) > 8 else ""))

        only = pick_product(products)
        chosen = req.products or ([only] if only else [])
        _unknown_products(ident, chosen, products)
        added: list[str] = []
        if chosen:
            stream.start("link", f"Linking {', '.join(chosen)} to {target}", fraction=0.88)
            added = link(root, P.dump_package(root, use_cache=False), ident, chosen, target,
                         run=lambda a: runner.swift(a, root, output.log))
            P.forget_dump(root)
            stream.finish("link")
        else:
            stream.finish("link", "skipped", detail="Choose a product")
        stream.start("validate", fraction=0.95)
        runner.poll()
        if chosen:
            _verify_link(root, chosen, target)
        else:
            P.dump_package(root, use_cache=False)
        stream.finish("validate")
    except BaseException:
        runner.quiet()
        backup.restore()
        raise
    after = (root / "Package.swift").read_text(errors="replace")
    return {
        "package": P.list_entry(root, ident), "identity": ident, "target": target, "products": products,
        "product_added": added[0] if added else None, "products_added": added, "needs_product": not chosen,
        "requirement": req_text, **P.manifest_diff(root, before, after, max_bytes),
        "next": "Check build" if chosen else "Choose a product",
        "hint": HINT_BUILD_AFTER if chosen else f"{ident} has {len(products)} products: choose which to link.",
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }


def add_dry_run(req: AddRequest, root: Path, max_bytes: int) -> dict:
    """What `add` would write, from add-dependency on a copy of the manifest; nothing is fetched or changed."""
    kind, loc, ident, rule, version, target = _plan(req, root, lambda: P.dump_package(root), entry=False)
    before = (root / "Package.swift").read_text(errors="replace")
    P.CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(P.CACHE), prefix="dry-") as tmp:
        d = Path(tmp) / root.name
        d.mkdir()
        (d / "Package.swift").write_text(before)
        r = run_swift(_add_dependency_args(kind, loc, rule, version), d)
        if r.returncode != 0:
            raise swift_failure((r.stdout + r.stderr).splitlines(), "Couldn't add the package to Package.swift")
        after = (d / "Package.swift").read_text(errors="replace")
    return {"dry_run": True, "identity": ident, "url": loc if kind == "url" else None,
            "path": loc if kind == "path" else None, "requirement": requirement_text(rule, version),
            "target": target, **P.manifest_diff(root, before, after, max_bytes),
            "hint": "Adding fetches the package and links its product to " + target + "."}


def add_product(root: Path, package: str | None, wanted: list[str], target: str | None, max_bytes: int) -> dict:
    """Link more products of a declared package to a target (Package.swift put back on failure)."""
    with P.project_lock(root):
        dump = P.dump_package(root, use_cache=False)
        pkg = P.find_declared(dump, str(package or ""))
        if not pkg:
            raise package_error("unknown-package", f"Package.swift has no package named {package}.", "Add it first.")
        ident = pkg["identity"]
        target = check_target(dump, root, target)
        pkg_dir = P.package_dir(root, ident, pkg.get("path"))
        products = P.products_of(pkg_dir) if pkg_dir and Path(pkg_dir).is_dir() else []
        if products:
            _unknown_products(ident, wanted, products)
        backup = P.Backup(root)
        before = backup.manifest.decode("utf-8", "replace")
        try:
            added = link(root, dump, ident, wanted, target)
            P.forget_dump(root)
            _verify_link(root, wanted, target)
        except BaseException:
            backup.restore()
            raise
        after = (root / "Package.swift").read_text(errors="replace")
        return {
            "package": P.list_entry(root, ident), "identity": ident, "target": target, "products": products,
            "product_added": added[0] if added else None, "products_added": added, "needs_product": False,
            "already_linked": [p for p in wanted if p not in added],
            **P.manifest_diff(root, before, after, max_bytes), "next": "Check build", "hint": HINT_BUILD_AFTER}
