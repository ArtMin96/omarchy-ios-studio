"""Demo mode of the `hotreload` group: answers for the demo projects as if done; nothing is written.

enable/disable build their diff from a made-up Package.swift with the real block (project_setup.block), so the
UI shows what a real project gets. WeatherGlance's views `import HotReload`, so turning it off keeps the stub.
"""
from __future__ import annotations

from iosstudio.core import output
from iosstudio.core.output import HelperError

IMPORTING = {"WeatherGlance": ["Sources/WeatherGlance/ContentView.swift", "Sources/WeatherGlance/ForecastView.swift"]}


def _project(flags: dict) -> tuple[str, str]:
    raw = str(flags.get("project") or flags.get("path") or "")
    if not raw or raw == "True":
        raise HelperError("usage", "Missing --project.")
    return raw.rstrip("/").split("/")[-1], raw.rstrip("/")


def _manifest(name: str) -> str:
    return ('// swift-tools-version: 6.0\nimport PackageDescription\n\nlet package = Package(\n'
            f'    name: "{name}",\n    platforms: [.iOS(.v17)],\n    products: [\n'
            f'        .library(name: "{name}", targets: ["{name}"]),\n    ],\n    targets: [\n'
            f'        .target(name: "{name}"),\n    ]\n)\n')


def _diff(before: str, after: str) -> str:
    from iosstudio.features import format as swift_format
    return swift_format.unified("Package.swift", before, after)


def enable(flags: dict) -> int:
    from iosstudio.features.hotreload import project_setup as S
    name, path = _project(flags)
    base = _manifest(name)
    return output.ok({"enabled": True, "mode": "on", "appTarget": name, "changed": True,
                      "diff": _diff(base, S.append_block(base, name)), "truncated": False,
                      "packagePath": f"{path}/Package.swift", "dryRun": bool(flags.get("dry_run")),
                      "runtimeUpdated": True, "demo": True})


def disable(flags: dict) -> int:
    from iosstudio.features.hotreload import project_setup as S
    name, path = _project(flags)
    base = _manifest(name)
    before = S.append_block(base, name)
    users = IMPORTING.get(name, [])
    after = S.append_block(base, name, linker=False) if users else base
    return output.ok({"enabled": False, "mode": "stub" if users else "off", "importedBy": users, "changed": True,
                      "diff": _diff(before, after), "truncated": False, "packagePath": f"{path}/Package.swift",
                      "dryRun": bool(flags.get("dry_run")), "runtimeUpdated": True, "demo": True})


ACTIONS = {"enable": enable, "disable": disable}


def run(action: str, flags: dict) -> int:
    fn = ACTIONS.get(action)
    if fn is None:
        output.fail("usage", f"Unknown command 'hotreload {action}'.")
        return output.EXIT_USAGE
    try:
        return fn(flags)
    except HelperError as exc:
        return output.fail(exc.code, exc.message, exc.fix, exc.detail)
