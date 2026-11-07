"""`packages <action>` handlers: flags in, JSON (or step events and `done`) out; the work is in the other modules."""
from __future__ import annotations

import json
from typing import Any

from iosstudio.core import output, subproc
from iosstudio.core.output import HelperError

DEFAULT_MAX_BYTES = 12_000
MAX_MAX_BYTES = 200_000


# ================================================================ output

def error_json(exc: HelperError) -> dict:
    """The JSON error with the extra fields package errors carry (hint, diagnostics, …)."""
    out = exc.to_json()
    out.update(getattr(exc, "extra", None) or {})
    return out


def fail(exc: HelperError) -> int:
    output._write({"ok": False, "error": error_json(exc)})
    return 1


def done_fail(exc: HelperError) -> int:
    output.event("done", ok=False, error=error_json(exc))
    return 1


def done_ok(data: dict) -> int:
    output.progress(1.0, "Done")
    output.event("done", ok=True, data=data)
    return 0


# ================================================================ flags

def opt_str(flags: dict, key: str) -> str | None:
    """A flag's value as text; None when absent or given without a value."""
    v = flags.get(key)
    return None if v in (None, True) else str(v)


def max_bytes_flag(flags: dict) -> int:
    """--max-bytes, clamped to 500 … MAX_MAX_BYTES."""
    v = flags.get("max_bytes")
    n = DEFAULT_MAX_BYTES
    if v is not None and v is not True:
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise HelperError("usage", "--max-bytes must be a number.") from None
    return max(500, min(MAX_MAX_BYTES, n))


def products_flag(flags: dict) -> list[str]:
    """--product X [Y …] and/or --products '<json list>' (or a comma list), without duplicates."""
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


def _add_request(flags: dict) -> Any:
    from iosstudio.features.packages.add import AddRequest
    version = opt_str(flags, "version")
    return AddRequest(url=str(flags.get("url") or ""), rule=str(flags.get("rule") or "from"),
                      version=version.strip() if version is not None else None,
                      target=opt_str(flags, "target"), products=products_flag(flags))


# ================================================================ commands

def cmd_list(flags: dict) -> int:
    from iosstudio.features.packages import project
    root = project.project_root(flags)
    try:
        return output.ok(project.list_data(root, use_cache=not flags.get("fresh")))
    except HelperError as exc:
        return fail(exc)


def cmd_add(flags: dict) -> int:
    from iosstudio.features.packages import add, project, swift
    if flags.get("dry_run"):
        try:
            root = project.project_root(flags)
            req = _add_request(flags)
            output.event("done", ok=True, data=add.add_dry_run(req, root, max_bytes_flag(flags)))
            return 0
        except HelperError as exc:
            return done_fail(exc)
    stream = swift.Stream(add.STEPS_ADD)
    runner = swift.SwiftRunner()
    try:
        root = project.project_root(flags)
        with runner, project.project_lock(root):
            req = _add_request(flags)
            return done_ok(add.add(req, root, stream, runner, max_bytes_flag(flags)))
    except subproc.Cancelled:
        stream.close("cancelled")
        return done_fail(swift.package_error("cancelled", "Cancelled; Package.swift is as it was.", restored=True))
    except HelperError as exc:
        stream.close("failed")
        return done_fail(exc)


def cmd_add_product(flags: dict) -> int:
    from iosstudio.features.packages import add, project
    try:
        root = project.project_root(flags)
        wanted = products_flag(flags)
        if not wanted:
            raise HelperError("usage", "Missing --product.")
        return output.ok(add.add_product(root, flags.get("package"), wanted, opt_str(flags, "target"),
                                         max_bytes_flag(flags)))
    except HelperError as exc:
        return fail(exc)


def _resolve_like(flags: dict, action: str) -> int:
    from iosstudio.features.packages import project, swift, update
    stream = swift.Stream(update.STEPS_UPDATE if action == "update" else update.STEPS_RESOLVE)
    runner = swift.SwiftRunner()
    try:
        root = project.project_root(flags)
        with runner, project.project_lock(root):
            return done_ok(update.resolve_like(root, action, opt_str(flags, "package"), stream, runner))
    except subproc.Cancelled:
        stream.close("cancelled")
        return done_fail(swift.package_error("cancelled", "Cancelled; Package.resolved is as it was.",
                                             restored=True))
    except HelperError as exc:
        stream.close("failed")
        return done_fail(exc)


def cmd_update(flags: dict) -> int:
    return _resolve_like(flags, "update")


def cmd_resolve(flags: dict) -> int:
    return _resolve_like(flags, "resolve")


def cmd_remove(flags: dict) -> int:
    from iosstudio.features.packages import project, update
    try:
        root = project.project_root(flags)
        wanted = flags.get("package")
        if not wanted or wanted is True:
            raise HelperError("usage", "Missing --package.")
        return output.ok(update.remove(root, str(wanted), bool(flags.get("confirm")), max_bytes_flag(flags)))
    except HelperError as exc:
        return fail(exc)


def demo(action: str, flags: dict) -> int:
    """Demo mode (IOSSTUDIO_DEMO): canned packages, nothing written."""
    from iosstudio.features.packages import demo_mode
    return demo_mode.demo(action, flags)


GROUPS = {"packages": {"list": cmd_list, "add": cmd_add, "add-product": cmd_add_product, "update": cmd_update,
                       "resolve": cmd_resolve, "remove": cmd_remove}}
