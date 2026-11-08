"""Changing what a project depends on: update and resolve (with the pins that moved) and remove (a text edit)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from iosstudio.core import fileio, output
from iosstudio.core.output import HelperError
from iosstudio.features.packages import manifest as M
from iosstudio.features.packages import manifest_edit
from iosstudio.features.packages import project as P
from iosstudio.features.packages.swift import LineSink, Stream, SwiftRunner, package_error, run_swift, swift_failure

STEPS_UPDATE = [("update", "Updating packages"), ("changes", "Comparing versions")]
STEPS_RESOLVE = [("resolve", "Resolving packages"), ("changes", "Comparing versions")]
HINT_BUILD_REMOVED = "Check build shows any code that still imports the removed package."
SHOW_DEPS_TIMEOUT = 20


# ================================================================ update / resolve

def resolve_like(root: Path, action: str, package: str | None, stream: Stream, runner: SwiftRunner) -> dict:
    """`swift package update [ID]` or `swift package resolve`, with step events; the pins that moved.
    Package.resolved is put back on failure or cancel."""
    steps = STEPS_UPDATE if action == "update" else STEPS_RESOLVE
    t0 = time.monotonic()
    dump = P.dump_package(root)
    args = ["package", action]
    only = None
    if action == "update" and package is not None:
        pkg = P.find_declared(dump, package)
        if not pkg:
            raise package_error("unknown-package", f"Package.swift has no package named {package}.",
                                "Pick one from the list.")
        only = pkg["identity"]
        args.append(only)
    remote = [d for d in M.declared(dump) if d["kind"] != "local"]
    _exists, before, _ = P.read_resolved(root)
    total = max(1, len(before) or len(remote))
    backup = P.Backup(root)
    sid = steps[0][0]
    stream.start(sid, f"Updating {only}" if only else None, fraction=0.02)
    seen: set[str] = set()

    def on_progress(pd: tuple[str, str | None, bool]) -> None:
        if pd[2] and pd[1]:
            seen.add(pd[1])
        stream.detail(pd[0], round(min(0.9, 0.05 + 0.85 * len(seen) / total), 3))

    try:
        rc, lines = runner.swift(args, root, LineSink(on_progress))
        if rc != 0:
            raise swift_failure(lines, "Updating failed" if action == "update" else "Resolving failed")
    except BaseException:
        runner.quiet()
        if backup.restore(resolved_only=True):
            output.log("Package.resolved was put back as it was.")
        raise
    stream.finish(sid)
    stream.start("changes", fraction=0.95)
    _exists, after, perr = P.read_resolved(root)
    changes = M.resolved_changes(before, after)
    if only:
        changes = [c for c in changes if c["identity"] == only] + [c for c in changes if c["identity"] != only]
    up_to_date = any("Everything is already up-to-date" in ln for ln in lines)
    stream.finish("changes", detail=f"{len(changes)} changed" if changes else "No changes")
    data = P.list_data(root)
    out = {"changes": changes[:P.MAX_PACKAGES], "changed": len(changes), "up_to_date": not changes,
           "swift_up_to_date": up_to_date, "package": only, "packages": data["packages"],
           "unresolved": data["unresolved"], "resolved_file": data["resolved_file"],
           "elapsed_ms": int((time.monotonic() - t0) * 1000), "summary": summary(changes, action)}
    if perr:
        out["resolved_error"] = perr
    return out


def summary(changes: list[dict], action: str) -> str:
    """One line about the pins that moved: '2 packages updated: a 1.0.0 → 1.1.0, b 2.0.0 (new)'."""
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


# ================================================================ remove

def _transitive_only(root: Path, ident: str) -> set[str]:
    """Identities only `ident` brings in (from show-dependencies; empty when it can't be read offline)."""
    try:
        r = run_swift(["package", "--disable-automatic-resolution", "show-dependencies", "--format", "json"], root,
                      timeout=SHOW_DEPS_TIMEOUT, stdout_only=True)
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
    dump = P.dump_package(root, use_cache=False)
    pkg = P.find_declared(dump, wanted)
    if not pkg:
        names = ", ".join(d["identity"] for d in M.declared(dump)[:20]) or "none"
        raise package_error("unknown-package", f"Package.swift has no package named {wanted}.", f"Packages: {names}.")
    ident = pkg["identity"]
    local_targets = [t["target"] for t in M.target_deps(dump)]
    pkg_dir = P.package_dir(root, ident, pkg.get("path"))
    products: list[str] = []
    if pkg_dir and Path(pkg_dir).is_dir():
        try:
            products = P.products_of(pkg_dir)
        except HelperError:
            products = []
    try:
        edit = manifest_edit.remove_package(text, ident, url=pkg.get("url") or pkg.get("path"),
                                            names=[pkg["name"]] if pkg.get("name") else [], products=products,
                                            local_targets=local_targets)
    except manifest_edit.LexError as exc:
        edit = manifest_edit.RemoveEdit(text, problems=[f"Package.swift couldn't be read as Swift ({exc})."])
    problems = list(edit.problems)
    if not edit.removed_package and not problems:
        problems.append(f"The .package entry for {ident} wasn't found as a plain entry in Package.swift.")
    new_dump, verr = (None, None)
    if edit.text != text:
        new_dump, verr = P.validate_text(root, edit.text)
    if new_dump is not None:
        if P.find_declared(new_dump, ident):
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


def _drop_resolved_pins(root: Path, identities: set[str]) -> list[str]:
    """Remove those pins from Package.resolved (the file goes when none is left); the ones removed."""
    rp = root / "Package.resolved"
    if not rp.is_file():
        return []
    try:
        old_pins = M.parse_resolved(rp.read_text())
        drop = identities & set(old_pins)
        if not drop:
            return []
        new = M.drop_pins(rp.read_text(), drop)
        if new is None:
            rp.unlink()
        else:
            fileio.atomic_write(rp, new)
        return sorted(drop)
    except (ValueError, OSError):
        return []


def remove(root: Path, wanted: str, confirm: bool, max_bytes: int) -> dict:
    """Without confirm the dry run (diff, valid); with it the edit written, checked in place (put back if it
    fails) and the package's pins (and those only it needed) dropped from Package.resolved."""
    plan = plan_remove(root, wanted)
    diff = P.manifest_diff(root, plan["text"], plan["new_text"], max_bytes)
    would = {"package": plan["identity"], "target_deps": plan["target_deps"]}
    if not plan["valid"]:
        raise package_error("edit-invalid", f"Removing {plan['identity']} can't be done safely: {plan['problems'][0]}",
                            "Nothing was changed. Edit Package.swift by hand.", problems=plan["problems"],
                            diagnostics=plan["diagnostics"], would_remove=would, valid=False, **diff)
    base = {"identity": plan["identity"], "would_remove": would, "valid": True, **diff}
    if not confirm:
        return {**base, "dry_run": True, "removed": False,
                "hint": f"Removing changes Package.swift as shown and drops the pins of "
                        f"{plan['identity']} from Package.resolved."}
    with P.project_lock(root):
        if (root / "Package.swift").read_text(errors="replace") != plan["text"]:
            raise package_error("changed", "Package.swift changed while this was open.", "Look at the change again.")
        transitive = _transitive_only(root, plan["identity"])
        backup = P.Backup(root)
        try:
            fileio.atomic_write(root / "Package.swift", plan["new_text"].encode("utf-8"), backup.mode)
            P.forget_dump(root)
            P.dump_package(root, use_cache=False)
        except HelperError as exc:
            backup.restore()
            raise package_error("edit-invalid", f"Package.swift didn't check out after the edit, so it was put back: "
                                                f"{exc.message}", "Nothing was changed. Edit Package.swift by hand.",
                                diagnostics=(getattr(exc, "extra", {}) or {}).get("diagnostics"), valid=False,
                                **diff) from None
        except BaseException:
            backup.restore()
            raise
        pins_removed = _drop_resolved_pins(root, {plan["identity"]} | transitive)
        return {**base, "dry_run": False, "removed": True, "pins_removed": pins_removed,
                "next": "Check build", "hint": HINT_BUILD_REMOVED}
