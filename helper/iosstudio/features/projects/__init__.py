"""Projects and builds: command groups `projects` and `build` (stdlib only).

One-shot commands print one JSON object; build/renew commands stream JSON lines (see docs/development.md
"Helper protocol").

  commands.py  GROUPS and the handlers
  model.py     what a project is: metadata, the folder scan, templates, new apps
  store.py     the state files below (+ provisioning profiles read from built apps)
  runner.py    BuildRunner: one xtool child -> step/progress/log/diagnostic/prompt events
  build.py     the build flow (checks, lock, prebuild, xtool, history, signing record)
  renew.py     auto-renew of free 7-day signing (systemd user timer)

State (all under ~/.local/state/ios-studio/):
  projects.json   {"added": [path], "hidden": [path], "meta": {path: {"autoRenew": bool, "udid": str,
                    "build": {prebuild, env, openFiles, allowGenerated},     (build: see build_settings.py)
                    "runPresets": [{name, args, env, language, region, uiTestMode}], "runPreset": name}}}
                                                                              (see run_presets.py)
  builds.json     [{"project", "path", "name", "kind", "config", "result", "duration", "time",
                    "ipa", "errors", "warnings", "logFile", "udid", "bundleId", "buildId" (watch mode: w-<n>), "trigger" ("watch"),
                    "diagnostics": [{severity, message, category, file?, line?, col?, hint?, details?}]
                                   (≤ 200, errors first; see diagnostics.py)}]   newest last
  renewals.json   {path: {"udid", "bundleId", "signedAt", "expires", "via", "estimated"}}
  build-logs/     one full raw log per build (<time>-<name>-<kind>.log)
"""
from __future__ import annotations

import importlib
from typing import Any

from iosstudio.features.projects.commands import GROUPS

__all__ = ["GROUPS"]

# Names other feature modules (watch, debugger, screenshots, app_data) still use as `projects.<name>`:
# resolved on first use, so importing this package doesn't load the build machinery.
_COMPAT = {
    "_state": ("store", "load_state"),
    "_history": ("store", "load_history"),
    "_record_signing": ("store", "record_signing"),
    "_scan": ("model", "scan"),
    "_project_info": ("model", "project_info"),
    "_is_project": ("model", "is_project"),
    "_auth_ok": ("build", "auth_ok"),
}


def __getattr__(name: str) -> Any:
    try:
        module, attr = _COMPAT[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(importlib.import_module(f"{__name__}.{module}"), attr)
