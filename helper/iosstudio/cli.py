"""iOS Studio helper entry point: `iosstudio-helper <group> <action> [--flags]` (`python -m iosstudio`).

Each slice module (iosstudio/features/) exposes `GROUPS = {"group": {"action": handler}}` where
handler(flags: dict) -> int (exit code; it must print via output.ok/fail or stream events).
Modules are imported lazily so a broken slice never takes the others down.
"""
from __future__ import annotations

import importlib
import os
import sys
import traceback

from iosstudio.core import flags, output

# group -> module name in iosstudio.features (slices add their groups here; keep it to one line each)
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
    "watch": "watch",
    "packages": "packages",
    "hotreload": "hotreload",
}
# one-word commands -> group + action
ALIASES = {"format": ["projects", "format"], "format-lint": ["projects", "format-lint"]}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        output.fail("usage", "usage: iosstudio-helper <group> <action> [--flags]; groups: " + ", ".join(MODULES))
        return output.EXIT_USAGE
    if argv[0] in ALIASES:   # `format --project P` == `projects format --project P`
        argv = ALIASES[argv[0]] + argv[1:]
    group, action, rest = argv[0], argv[1], argv[2:]
    if os.environ.get("IOSSTUDIO_DEMO"):   # screenshots / walkthroughs: made-up data, no side effects
        from iosstudio.demo import demo
        try:
            return int(demo.run(group, action, flags.parse_flags(rest)) or 0)
        except Exception:
            return output.fail("internal", "Demo mode failed.", detail=traceback.format_exc())
    mod_name = MODULES.get(group)
    if not mod_name:
        output.fail("usage", f"Unknown command group '{group}'.")
        return output.EXIT_USAGE
    try:
        mod = importlib.import_module(f"iosstudio.features.{mod_name}")
    except Exception as exc:  # broken/missing slice module
        return output.fail("internal", "This part of iOS Studio failed to load.", detail=repr(exc))
    handler = getattr(mod, "GROUPS", {}).get(group, {}).get(action)
    if handler is None:
        output.fail("usage", f"Unknown command '{group} {action}'.")
        return output.EXIT_USAGE
    try:
        return int(handler(flags.parse_flags(rest)) or 0)
    except output.HelperError as exc:
        return output.fail(exc.code, exc.message, exc.fix, exc.detail)
    except KeyboardInterrupt:
        return output.fail("cancelled", "Cancelled.")
    except Exception:
        return output.fail("internal", "Something unexpected went wrong.", detail=traceback.format_exc())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
