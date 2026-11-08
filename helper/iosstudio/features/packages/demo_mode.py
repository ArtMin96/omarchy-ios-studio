"""Demo mode of the package commands: canned packages for the demo projects; progress is made up, nothing written."""
from __future__ import annotations

import time
from pathlib import Path

from iosstudio.core import output, subproc
from iosstudio.core.output import HelperError
from iosstudio.features.packages import manifest as M
from iosstudio.features.packages.add import HINT_BUILD_AFTER, RULES, STEPS_ADD, check_location
from iosstudio.features.packages.commands import done_fail, fail, products_flag
from iosstudio.features.packages.swift import package_error
from iosstudio.features.packages.update import HINT_BUILD_REMOVED, STEPS_RESOLVE, STEPS_UPDATE, summary

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
        output.step(sid, title, "pending")
    frac = 0.0
    for i, (sid, title) in enumerate(steps):
        output.step(sid, title, "running")
        for d in details.get(sid, []):
            time.sleep(subproc.demo_delay(pause))
            frac = min(0.95, frac + 0.12)
            output.step(sid, title, "running", detail=d)
            output.progress(round(frac, 2), d)
            output.log(d)
        time.sleep(subproc.demo_delay(pause))
        output.step(sid, title, "done")


def _demo_diff(before: str, after: str) -> dict:
    from iosstudio.features import format as swift_format
    full = swift_format.unified("Package.swift", before, after)
    return {"diff": full, "diff_bytes": len(full.encode()), "truncated": False, "full_output_path": None}


def demo(action: str, flags: dict) -> int:
    """`packages <action>` in demo mode."""
    try:
        name, path = _demo_project(flags)
    except HelperError as exc:
        return fail(exc)
    pkgs = DEMO_PACKAGES.get(name, [])
    manifest = ("    dependencies: [\n" + "".join(f'        .package(url: "{p.get("url")}", from: "1.0.0"),\n'
                                                   for p in pkgs if p["kind"] == "remote") + "    ],\n")
    if action == "list":
        return output.ok(_demo_list(name, path))
    if action == "add":
        url = str(flags.get("url") or "")
        try:
            kind, loc, ident = check_location(url, Path(path)) if not url.startswith(("/", ".", "~")) \
                else ("path", url, M.identity_of(url))
        except HelperError as exc:
            return done_fail(exc)
        if any(p["identity"] == ident for p in pkgs):
            return done_fail(package_error("already-added", f"{ident} is already in Package.swift.",
                                    "Link one of its products to a target instead.", identity=ident,
                                    package=next(p for p in pkgs if p["identity"] == ident)))
        rule = str(flags.get("rule") or "from")
        version = str(flags.get("version") or "1.2.0") if flags.get("version") not in (None, True) else "1.2.0"
        req = M.requirement_text({"rule": rule if kind == "url" else "local", "version": version, "branch": version,
                                  "revision": version})
        products = DEMO_PRODUCTS.get(ident, [ident.replace("-", " ").title().replace(" ", "")])
        wanted = products_flag(flags)
        chosen = wanted or ([products[0]] if len(products) == 1 else [])
        target = str(flags.get("target") or name)
        flag = rule if rule in RULES else "from"
        after = manifest.replace("    ],\n", f'        .package(url: "{loc}", {flag}: "{version}"),\n    ],\n')
        diff = _demo_diff(manifest, after)
        if flags.get("dry_run"):
            output.event("done", ok=True, data={"dry_run": True, "identity": ident, "url": loc, "requirement": req,
                                                "target": target, **diff, "demo": True})
            return 0
        _demo_steps(STEPS_ADD if chosen else STEPS_ADD[:4],
                    {"check": [f"{ident} {req}"], "fetch": [f"Fetching {ident}", f"Fetched {ident}",
                                                           f"{ident} {version}"],
                     "products": [", ".join(products)]})
        if not chosen:
            output.step("link", "Linking to the target", "skipped", detail="Choose a product")
            output.step("validate", "Checking Package.swift", "done")
        entry = {"identity": ident, "name": ident, "kind": "remote" if kind == "url" else "local",
                 "requirement": {"rule": rule, "version": version, "text": req}, "resolved": {"version": version},
                 "used_by": [target] if chosen else [], "products_used": chosen, "url": loc}
        output.event("done", ok=True, data={
            "package": entry, "identity": ident, "target": target, "products": products,
            "product_added": chosen[0] if chosen else None, "products_added": chosen, "needs_product": not chosen,
            "requirement": req, **diff, "next": "Check build" if chosen else "Choose a product",
            "hint": HINT_BUILD_AFTER if chosen else f"{ident} has {len(products)} products: choose which to link.",
            "elapsed_ms": 2100, "demo": True, "note": "Demo mode: nothing was written."})
        return 0
    if action == "add-product":
        ident = str(flags.get("package") or "")
        products = DEMO_PRODUCTS.get(ident, [])
        wanted = products_flag(flags)
        target = str(flags.get("target") or name)
        line = "".join(f'                .product(name: "{p}", package: "{ident}"),\n' for p in wanted)
        diff = _demo_diff("            dependencies: [\n            ]\n",
                          f"            dependencies: [\n{line}            ]\n")
        return output.ok({"package": {"identity": ident, "name": ident, "kind": "remote", "used_by": [target],
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
        output.event("done", ok=True, data={
            "changes": changes, "changed": len(changes), "up_to_date": not changes, "package": only,
            "packages": pkgs, "unresolved": [], "resolved_file": True, "elapsed_ms": 1800,
            "summary": summary(changes, action), "demo": True, "note": "Demo mode: nothing was written."})
        return 0
    if action == "remove":
        ident = str(flags.get("package") or "")
        pkg = next((p for p in pkgs if p["identity"] == ident or p["name"] == ident), None)
        if not pkg:
            return fail(package_error("unknown-package", f"Package.swift has no package named {ident}.",
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
            return output.ok({**base, "dry_run": True, "removed": False})
        return output.ok({**base, "dry_run": False, "removed": True, "pins_removed": [pkg["identity"]],
                          "next": "Check build", "hint": HINT_BUILD_REMOVED, "note": "Demo mode: nothing was written."})
    return fail(HelperError("usage", f"Unknown command 'packages {action}'."))
