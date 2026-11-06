"""iOS Studio helper entry point: `iosstudio-helper <group> <action> [--flags]`.

Each slice module exposes `GROUPS = {"group": {"action": handler}}` where
handler(flags: dict) -> int (exit code; it must print via common.ok/fail or stream events).
Modules are imported lazily so a broken slice never takes the others down.
"""
from __future__ import annotations

import importlib
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402

# group -> module name (slices add their groups here; keep it to one line each)
MODULES = {
    "setup": "setup",
    "toolchain": "setup",
    "account": "account",
    "projects": "projects",
    "build": "projects",
    "app": "appconfig",
    "agents": "agents",
    "debug": "debugger",
    "screenshots": "screenshots",
    "editor": "editor_setup",
}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return common.fail("usage", "usage: iosstudio-helper <group> <action> [--flags]; groups: " + ", ".join(MODULES)) or 2
    group, action, rest = argv[0], argv[1], argv[2:]
    if os.environ.get("IOSSTUDIO_DEMO"):   # screenshots / walkthroughs: made-up data, no side effects
        import demo
        try:
            return int(demo.run(group, action, common.parse_flags(rest)) or 0)
        except Exception:
            return common.fail("internal", "Demo mode failed.", detail=traceback.format_exc())
    mod_name = MODULES.get(group)
    if not mod_name:
        common.fail("usage", f"Unknown command group '{group}'.")
        return 2
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:  # broken/missing slice module
        return common.fail("internal", "This part of iOS Studio failed to load.", detail=repr(exc))
    handler = getattr(mod, "GROUPS", {}).get(group, {}).get(action)
    if handler is None:
        common.fail("usage", f"Unknown command '{group} {action}'.")
        return 2
    try:
        return int(handler(common.parse_flags(rest)) or 0)
    except common.HelperError as exc:
        return common.fail(exc.code, exc.message, exc.fix, exc.detail)
    except KeyboardInterrupt:
        return common.fail("cancelled", "Cancelled.")
    except Exception:
        return common.fail("internal", "Something unexpected went wrong.", detail=traceback.format_exc())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
