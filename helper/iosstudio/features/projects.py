"""Projects and builds slice: groups `projects` and `build` (stdlib only).

One-shot commands print one JSON object; build/renew commands stream JSON lines (see
docs/development.md "Helper protocol"). Every xtool child runs in its own process group
and is watched by a tiny reaper process, so killing this helper (even with SIGKILL) also kills
xtool, swift-build and swift-frontend.

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

import ctypes
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, fileio, output, subproc
from iosstudio.core.output import HelperError, fix
from iosstudio.features import build_settings, diagnostics, run_presets

PROJECTS_FILE = appdirs.STATE_DIR / "projects.json"
BUILDS_FILE = appdirs.STATE_DIR / "builds.json"
RENEWALS_FILE = appdirs.STATE_DIR / "renewals.json"
BUILD_LOGS = appdirs.STATE_DIR / "build-logs"
LOCK_DIR = appdirs.STATE_DIR / "locks"
MAX_LINE = subproc.MAX_LINE     # longest build-output line kept whole
EXIT_GRACE = 60          # seconds a build may linger after closing its output
TEMPLATES = appdirs.REPO / "templates"
SYSTEMD_DIR = Path(os.environ.get("XDG_CONFIG_HOME", appdirs.HOME / ".config")) / "systemd/user"
UNIT = "ios-studio-renew"

SKIP_DIRS = {"node_modules", ".build", ".git", "xtool", "DerivedData", "Pods", ".swiftpm", "build",
             "dist", "vendor", ".cache", "venv", ".venv", "__pycache__", "target", ".bsp", ".idea",
             ".vscode", "Carthage", ".gradle"}
MAX_HISTORY = 300
LOG_GRACE = 6 * 3600   # an unreferenced build log younger than this may belong to a running build
MAX_STORED_DIAGS = 200   # per builds.json entry (errors first), so Logs can show problems without re-parsing
FREE_PROFILE_DAYS = 7
EXIT_TEMP, EXIT_USER = 75, 78

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
TASK = re.compile(r"^\[(\d+)\s*/\s*(\d+)\]\s*(.*)$")
STAGE = re.compile(r"^\[(?P<stage>[A-Za-z][A-Za-z ]+)\]\s*(?:(?P<pct>\d+)%)?")
WROTE = re.compile(r"^Wrote to (?P<path>/.+)$")
STAGE_TEXT = {
    "unpacking app": "Preparing the app",
    "preparing device": "Connecting to your iPhone",
    "provisioning": "Getting a provisioning profile from Apple",
    "signing": "Signing the app",
    "logging in": "Signing in to Apple",
    "packaging": "Packaging",
    "connecting": "Connecting to your iPhone",
    "installing": "Sending the app to your iPhone",
    "verifying": "Your iPhone is verifying the app",
}
BUILD_DONE = re.compile(r"^Build complete! \((?P<secs>[\d.]+) secs?\)")


# ================================================================ small utilities

def _now() -> float:
    return time.time()


def _state() -> dict:
    data = fileio.load_json(PROJECTS_FILE, {}) or {}
    data.setdefault("added", [])
    data.setdefault("hidden", [])
    data.setdefault("meta", {})
    return data


def _save_state(data: dict) -> None:
    fileio.save_json(PROJECTS_FILE, data)


@contextmanager
def _file_lock(name: str):
    """Exclusive flock on STATE_DIR/.<name>.lock for a read-modify-write of one state file (the app,
    the MCP server and the renew timer each run their own helper processes)."""
    appdirs.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(appdirs.STATE_DIR / f".{name}.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


@contextmanager
def _edit_state():
    """Read projects.json under its lock and save it when the block ends without an exception."""
    with _file_lock("projects"):
        state = _state()
        yield state
        _save_state(state)


def _norm(path: str | Path) -> str:
    return str(Path(os.path.expanduser(str(path))).resolve())


def _require_path(flags: dict, key: str = "path") -> Path:
    raw = flags.get(key)
    if not raw or raw is True:
        raise HelperError("usage", f"Missing --{key}.")
    p = Path(_norm(raw))
    if not p.is_dir():
        raise HelperError("not-found", "That project folder doesn't exist anymore.",
                          {**fix("Remove from list", "projects", action="remove"), "path": str(p)})
    return p


def _roots() -> list[Path]:
    return appdirs.project_roots()


def _depth() -> int:
    try:
        return max(1, min(8, int(appdirs.settings().get("projectScanDepth", 4))))
    except (TypeError, ValueError):
        return 4


def _xtool_path() -> str | None:
    # IOS_STUDIO_XTOOL: test hook (a fake xtool for exercising prompts without an Apple account).
    return os.environ.get("IOS_STUDIO_XTOOL") or subproc.which("xtool")


def _xtool() -> str:
    x = _xtool_path()
    if not x:
        raise HelperError("missing-xtool", "xtool, the tool that builds iPhone apps on Linux, isn't installed yet.",
                          fix("Install xtool", "setup", step="xtool"))
    return x


def _notify(title: str, body: str = "", urgency: str = "normal") -> None:
    exe = subproc.which("omarchy-notification-send")
    argv = [exe, "--app-name", "iOS Studio", "-u", urgency, title] if exe else \
        (["notify-send", "-a", "iOS Studio", "-u", urgency, title] if shutil.which("notify-send") else None)
    if not argv:
        return
    if body:
        argv.append(body)
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


# ================================================================ project metadata

def _read_yml(path: Path) -> dict:
    """xtool.yml is flat YAML; we only need top-level scalar keys."""
    out: dict[str, Any] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    out["_generated"] = "GENERATED" in text[:400]
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z][A-Za-z0-9_]*):\s*(.*?)\s*$", line)
        if m and m.group(2) and not m.group(2).startswith("#"):
            out[m.group(1)] = m.group(2).strip("'\"")
    return out


def _package_info(pkg: Path) -> dict:
    try:
        text = pkg.read_text(errors="replace")
    except OSError:
        return {}
    name = re.search(r'Package\s*\(\s*name:\s*"([^"]+)"', text)
    libs = re.findall(r'\.library\s*\(\s*name:\s*"([^"]+)"', text)
    ios = re.search(r"\.iOS\s*\(\s*(?:\.v(\d+)|\"(\d+))", text)
    tools = re.search(r"swift-tools-version:\s*([\d.]+)", text)
    return {
        "package": name.group(1) if name else pkg.parent.name,
        "libraries": libs,
        "iosMin": (ios.group(1) or ios.group(2)) if ios else None,
        "toolsVersion": tools.group(1) if tools else None,
    }


def _git_branch(path: Path) -> str | None:
    for d in (path, *path.parents):
        head = d / ".git" / "HEAD"
        if head.is_file():
            try:
                ref = head.read_text().strip()
            except OSError:
                return None
            if ref.startswith("ref: refs/heads/"):
                return ref[len("ref: refs/heads/"):]
            return ref[:8]
        if (d / ".git").exists() or d == appdirs.HOME:
            break
    return None


def _last_build(path: str, history: list | None = None) -> dict | None:
    history = history if history is not None else _history()
    for entry in reversed(history):
        if entry.get("path") == path:
            return entry
    return None


def _project_info(path: Path, history: list | None = None, renewals: dict | None = None,
                  state: dict | None = None) -> dict:
    yml = _read_yml(path / "xtool.yml")
    pkg = _package_info(path / "Package.swift")
    product = yml.get("product") or (pkg.get("libraries") or [None])[0] or pkg.get("package") or path.name
    bundle = yml.get("bundleID") or (f"{yml['orgID']}.{product}" if yml.get("orgID") else None)
    icon = yml.get("iconPath")
    icon_abs = str((path / icon).resolve()) if icon and (path / icon).is_file() else None
    sp = str(path)
    state = state if state is not None else _state()
    renewals = renewals if renewals is not None else (fileio.load_json(RENEWALS_FILE, {}) or {})
    ren = renewals.get(sp)
    last = _last_build(sp, history)
    try:
        mtime = max((path / "Package.swift").stat().st_mtime, (path / "xtool.yml").stat().st_mtime)
    except OSError:
        mtime = None
    out = {
        "name": product,
        "package": pkg.get("package"),
        "path": sp,
        "displayPath": sp.replace(str(appdirs.HOME), "~", 1),
        "bundleId": bundle,
        "product": product,
        "icon": icon_abs,
        "iosMin": pkg.get("iosMin"),
        "branch": _git_branch(path),
        "generated": bool(yml.get("_generated")),
        "added": sp in state["added"],
        "autoRenew": bool(state["meta"].get(sp, {}).get("autoRenew")),
        "buildSettings": build_settings.current(state["meta"].get(sp)),
        **_presets_info(state["meta"].get(sp)),
        "lastBuild": last,
        "signing": _signing_status(ren),
        "modified": mtime,
        "ipa": _existing_artifact(path, product, ".ipa"),
    }
    if out["generated"] and not out["buildSettings"]["allowGenerated"]:
        out["note"] = ("This project's xtool.yml is generated by its own scripts, so iOS Studio doesn't build it. "
                       "To build it here, turn on \"Build generated project\" in its build settings and set the "
                       "script that regenerates xtool.yml as the prebuild command.")
    elif out["generated"] and not out["buildSettings"]["prebuild"]:
        out["note"] = ("This project's xtool.yml is generated, and no prebuild command regenerates it before a build, "
                       "so a build uses the file as it is now.")
    return out


def _existing_artifact(path: Path, product: str, ext: str) -> str | None:
    cand = path / "xtool" / f"{product}{ext}"
    return str(cand) if cand.exists() else None


def _signing_status(ren: dict | None) -> dict | None:
    if not ren:
        return None
    exp = ren.get("expires")
    left = (exp - _now()) / 86400 if exp else None
    level = "ok"
    if left is not None:
        level = "error" if left <= 0 else ("warn" if left < 2 else "ok")
    return {**ren, "daysLeft": round(left, 2) if left is not None else None, "level": level}


def _is_project(d: Path) -> bool:
    return (d / "Package.swift").is_file() and (d / "xtool.yml").is_file()


def _convertible(d: Path) -> dict | None:
    """A SwiftPM package that targets iOS with exactly one library could become an xtool app."""
    pkg = d / "Package.swift"
    if not pkg.is_file() or (d / "xtool.yml").exists():
        return None
    info = _package_info(pkg)
    if not info.get("iosMin") or len(info.get("libraries") or []) != 1:
        return None
    src = d / "Sources" / info["libraries"][0]
    has_main = False
    if src.is_dir():
        for f in list(src.rglob("*.swift"))[:200]:
            try:
                t = f.read_text(errors="replace")
            except OSError:
                continue
            if "@main" in t and "App" in t and "SwiftUI" in t:
                has_main = True
                break
    if not has_main:
        return None
    return {"name": info["libraries"][0], "path": str(d), "displayPath": str(d).replace(str(appdirs.HOME), "~", 1),
            "reason": "SwiftUI package without xtool.yml"}


def _scan() -> tuple[list[Path], list[dict]]:
    found: list[Path] = []
    conv: list[dict] = []
    max_depth = _depth()
    seen: set[str] = set()
    for root in _roots():
        stack = [(root, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                key = str(d.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            if _is_project(d):
                found.append(d.resolve())
                continue  # never descend into a project
            c = _convertible(d)
            if c:
                conv.append(c)
                continue
            if depth >= max_depth:
                continue
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS and not e.name.startswith("."):
                            stack.append((Path(e.path), depth + 1))
            except OSError:
                continue
    return found, conv


# ================================================================ projects group

def cmd_list(flags: dict) -> int:
    state = _state()
    found, conv = _scan()
    paths: dict[str, Path] = {}
    for p in found:
        paths[str(p)] = p
    for a in state["added"]:
        p = Path(a)
        if p.is_dir() and _is_project(p):
            paths[str(p)] = p
    hidden = set(state["hidden"])
    history = _history()
    renewals = fileio.load_json(RENEWALS_FILE, {}) or {}
    items = [_project_info(p, history, renewals, state) for k, p in paths.items() if k not in hidden]
    missing = [a for a in state["added"] if not Path(a).is_dir()]

    def sort_key(it):
        lb = (it.get("lastBuild") or {}).get("time") or 0
        return (-(lb or 0), -(it.get("modified") or 0), it["name"].lower())

    items.sort(key=sort_key)
    return output.ok({
        "projects": items,
        "convertible": [c for c in conv if c["path"] not in hidden],
        "missing": missing,
        "roots": [str(r) for r in _roots()],
        "defaultRoots": [str(r) for r in appdirs.default_project_roots()],
        "defaultDir": str(appdirs.new_app_dir()),
        "xtool": bool(subproc.which("xtool")),
    })


def cmd_info(flags: dict) -> int:
    p = _require_path(flags)
    if not _is_project(p):
        raise HelperError("not-a-project", "That folder isn't an xtool app (it needs Package.swift and xtool.yml).")
    info = _project_info(p)
    info["suggestedBuildSettings"] = build_settings.suggest(p, info["buildSettings"])
    try:   # editor support in this project (.bsp file, ready, hint); see editor_setup.py
        from iosstudio.features import editor_setup
        info["editor"] = editor_setup.brief(p)
    except Exception:  # noqa: BLE001 - never let it break project info
        info["editor"] = None
    return output.ok(info)


def cmd_check(flags: dict) -> int:
    """projects check --path P [--max N]: config mistakes found before building (warnings only; reads, never
    writes). See helper/iosstudio/features/project_check.py."""
    p = _require_path(flags)
    if not _is_project(p):
        raise HelperError("not-a-project", "That folder isn't an xtool app (it needs Package.swift and xtool.yml).")
    try:
        cap = max(0, int(flags.get("max") or 50))
    except (TypeError, ValueError):
        raise HelperError("usage", "--max must be a number.") from None
    from iosstudio.features import project_check
    t0 = time.monotonic()
    res = project_check.check(p)
    return output.ok(project_check.summarize(p, res["issues"], res["keysSource"],
                                             int((time.monotonic() - t0) * 1000), cap))


def cmd_build_settings(flags: dict) -> int:
    """projects build-settings --path P [--set '<json patch>' [--dry-run]]: show or change a project's build
    settings (prebuild, env, openFiles, allowGenerated). The patch is merged (see build_settings.apply_patch);
    --suggested starts the patch from what the project's own scripts imply. Returns
    {path, settings, before, changes, saved, suggested}."""
    p = _require_path(flags)
    raw = flags.get("set")
    patch: dict = {}
    if raw and raw is not True:
        try:
            patch = json.loads(raw)
        except json.JSONDecodeError:
            raise HelperError("usage", "--set must be a JSON object.") from None
        if not isinstance(patch, dict):
            raise HelperError("usage", "--set must be a JSON object.")
    sp = str(p)
    dry = bool(flags.get("dry_run"))
    with _edit_state() as state:
        meta = state["meta"].get(sp) or {}
        before = build_settings.current(meta)
        if flags.get("suggested"):
            sug = build_settings.suggest(p, before)
            base = dict((sug or {}).get("settings") or {})
            if isinstance(patch.get("env"), dict) and isinstance(base.get("env"), dict):
                patch = {**patch, "env": {**base["env"], **patch["env"]}}   # explicit names win, the rest stay
            patch = {**base, **patch}
        try:
            after = build_settings.apply_patch(before, patch)
        except build_settings.Invalid as exc:
            raise HelperError("invalid-setting", str(exc)) from None
        changes = build_settings.diff(before, after)
        saved = bool(changes) and not dry
        if saved:
            meta = state["meta"].setdefault(sp, {})
            st = build_settings.stored(after)
            if st:
                meta["build"] = st
            else:
                meta.pop("build", None)
    return output.ok({"path": sp, "settings": after if saved or dry else before, "before": before,
                      "changes": changes, "saved": saved, "dryRun": dry,
                      "suggested": build_settings.suggest(p, after if saved or dry else before),
                      "openFilesError": build_settings.check_nofile(after.get("openFiles"))})


def _presets_info(meta: dict | None) -> dict:
    """runPresets (each with its resolved args/env) and runPreset (the selected name or None) for project info."""
    presets = run_presets.stored(meta)
    return {"runPresets": [run_presets.with_resolved(p) for p in presets],
            "runPreset": run_presets.selected(meta, presets)}


def cmd_presets(flags: dict) -> int:
    """projects presets --path P [--save '<preset json>' [--rename-from OLD] | --delete NAME [--dry-run] |
    --select NAME|'' | --resolve NAME]: list or change a project's run presets (see run_presets.py).
    Returns {path, presets: [preset + resolved], selected} (+ saved / deleted, dryRun), or {name, args, env}
    for --resolve."""
    p = _require_path(flags)
    if not _is_project(p):
        raise HelperError("not-a-project", "That folder isn't an xtool app (it needs Package.swift and xtool.yml).")
    sp = str(p)
    with _edit_state() as state:
        meta = state["meta"].get(sp) or {}
        presets = run_presets.stored(meta)
        sel = run_presets.selected(meta, presets)
        presets, sel, extra, changed = run_presets.act(presets, sel, flags)
        if changed:
            run_presets.store(state["meta"].setdefault(sp, {}), presets, sel)
            if not state["meta"][sp]:
                state["meta"].pop(sp)
    return output.ok(run_presets.result(sp, presets, sel, extra))


def _launch_preset(p: Path, flags: dict) -> dict | None:
    """The --preset of a `build run`, resolved before anything builds (raises unknown-preset)."""
    name = flags.get("preset")
    if not isinstance(name, str) or not name.strip():
        return None
    preset = run_presets.find(run_presets.stored(_state()["meta"].get(str(p))), name)
    return {"name": preset["name"], **run_presets.resolve(preset)}


def cmd_add(flags: dict) -> int:
    p = _require_path(flags)
    if not _is_project(p):
        if (p / "Package.swift").is_file():
            raise HelperError("needs-conversion", "This Swift package has no xtool.yml yet, so it can't be built as an iPhone app.",
                              {"label": "Make it an app", "page": "projects", "action": "convert", "path": str(p)})
        raise HelperError("not-a-project", "That folder doesn't contain an iPhone app project (Package.swift and xtool.yml).",
                          fix("Create a new app", "projects", action="new"))
    sp = str(p)
    with _edit_state() as state:
        if sp not in state["added"]:
            state["added"].append(sp)
        if sp in state["hidden"]:
            state["hidden"].remove(sp)
    return output.ok(_project_info(p, state=state))


def cmd_remove(flags: dict) -> int:
    raw = flags.get("path")
    if not raw or raw is True:
        raise HelperError("usage", "Missing --path.")
    sp = _norm(raw)
    with _edit_state() as state:
        if sp in state["added"]:
            state["added"].remove(sp)
        if sp not in state["hidden"] and Path(sp).is_dir():
            state["hidden"].append(sp)
        state["meta"].pop(sp, None)
    from iosstudio.features import (
        screenshots,  # its saved screenshot baselines and diffs go with its settings (not the project's files)
    )
    shots = screenshots.forget_project(sp)
    return output.ok({"path": sp, "removed": True, "filesDeleted": False, "screenshotsDeleted": shots})


def cmd_unhide(flags: dict) -> int:
    sp = _norm(flags.get("path") or "")
    with _edit_state() as state:
        if sp in state["hidden"]:
            state["hidden"].remove(sp)
    return output.ok({"path": sp})


def cmd_set(flags: dict) -> int:
    p = _require_path(flags)
    with _edit_state() as state:
        meta = state["meta"].setdefault(str(p), {})
        if "auto_renew" in flags:
            meta["autoRenew"] = str(flags["auto_renew"]).lower() in ("1", "true", "yes", "on")
        if flags.get("udid") and flags["udid"] is not True:
            meta["udid"] = flags["udid"]
    return output.ok(_project_info(p, state=state))


def _templates() -> list[dict]:
    out = []
    if TEMPLATES.is_dir():
        for d in sorted(TEMPLATES.iterdir()):
            meta = fileio.load_json(d / "template.json")
            if isinstance(meta, dict) and (d / "Sources").is_dir():
                meta.setdefault("id", d.name)
                meta["files"] = sorted(f.name for f in (d / "Sources").iterdir() if f.is_file())
                meta["_dir"] = d
                out.append(meta)
    out.sort(key=lambda m: (not m.get("default"), m.get("title", "")))
    return out


def _template_dir(tmpl_id: Any) -> Path:
    """The folder of a listed template. Only ids from _templates() are accepted, never a path."""
    for meta in _templates():
        if meta.get("id") == tmpl_id:
            return meta["_dir"]
    ids = ", ".join(str(m.get("id")) for m in _templates())
    raise HelperError("bad-template", f"That template isn't available. Choose one of: {ids}.")


def cmd_templates(flags: dict) -> int:
    return output.ok({"templates": [{k: v for k, v in m.items() if k != "_dir"} for m in _templates()],
                      "bundlePrefix": appdirs.settings().get("bundlePrefix") or "com.example",
                      "defaultDir": str(appdirs.new_app_dir())})


NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,48}$")


def _fallback_skeleton(dest: Path, name: str) -> None:
    """What `xtool new NAME --skip-setup` writes, for when xtool isn't installed yet."""
    (dest / "Sources" / name).mkdir(parents=True, exist_ok=True)
    (dest / "Package.swift").write_text(f'''// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "{name}",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        // An xtool project should contain exactly one library product,
        // representing the main app.
        .library(
            name: "{name}",
            targets: ["{name}"]
        ),
    ],
    targets: [
        .target(
            name: "{name}"
        ),
    ]
)
''')
    (dest / "xtool.yml").write_text(f"version: 1\nbundleID: com.example.{name}\n")
    (dest / ".gitignore").write_text(".DS_Store\n/.build\n/Packages\nxcuserdata/\nDerivedData/\n.swiftpm/\n/xtool\n/.bsp\n")


def cmd_new(flags: dict) -> int:
    name = str(flags.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise HelperError("bad-name", "App names start with a letter and use only letters, numbers and _ (no spaces).")
    tmpl_id = flags.get("template") or "swiftui-app"
    tmpl = _template_dir(tmpl_id)
    parent = Path(os.path.expanduser(str(flags.get("dir") or appdirs.new_app_dir())))
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / name
    if dest.exists():
        raise HelperError("exists", f"A folder named {name} already exists in {parent}. Pick another name or location.")
    prefix = str(flags.get("bundle_prefix") or appdirs.settings().get("bundlePrefix") or "com.example").strip(". ")
    bundle = str(flags.get("bundle_id") or f"{prefix}.{name.replace('_', '-')}")
    if not re.match(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$", bundle):
        raise HelperError("bad-bundle-id", "The bundle ID must look like com.yourname.AppName (letters, numbers, dots and dashes).")

    xtool = subproc.which("xtool")
    used_xtool = False
    if xtool:
        r = subproc.run_cmd([xtool, "new", name, "--skip-setup"], timeout=60, cwd=str(parent), env=subproc.tool_env("xtool"))
        used_xtool = r.returncode == 0 and (dest / "Package.swift").is_file()
        if not used_xtool and dest.exists() and not any(dest.iterdir()):
            dest.rmdir()
    if not used_xtool:
        if dest.exists():
            raise HelperError("new-failed", "Creating the project didn't finish cleanly.", detail=(r.stdout + r.stderr) if xtool else None)
        _fallback_skeleton(dest, name)

    src_dir = dest / "Sources" / name
    for f in list(src_dir.glob("*.swift")):
        f.unlink()
    for f in (tmpl / "Sources").iterdir():
        if not f.is_file():
            continue
        target = f"{name}App.swift" if f.name == "App.swift" else f.name
        (src_dir / target).write_text(f.read_text().replace("{{NAME}}", name))
    yml = dest / "xtool.yml"
    text = yml.read_text() if yml.exists() else "version: 1\n"
    if re.search(r"^bundleID:.*$", text, re.M):
        text = re.sub(r"^bundleID:.*$", f"bundleID: {bundle}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\nbundleID: {bundle}\n"
    yml.write_text(text)

    git = shutil.which("git")
    if git and not (dest / ".git").exists() and flags.get("git", True) not in ("0", "false", "no"):
        if subproc.run_cmd([git, "init", "-q"], cwd=str(dest), timeout=20).returncode == 0:
            subproc.run_cmd([git, "add", "-A"], cwd=str(dest), timeout=20)
            subproc.run_cmd([git, "commit", "-q", "-m", f"New app {name} (iOS Studio template {tmpl_id})"],
                           cwd=str(dest), timeout=20)

    with _edit_state() as state:
        if str(dest.resolve()) not in state["added"]:
            state["added"].append(str(dest.resolve()))
        # marks it as made by iOS Studio: editor files (.vscode/) are only written by themselves into these
        state["meta"].setdefault(str(dest.resolve()), {})["createdFrom"] = tmpl_id
    _editor_auto(dest.resolve())
    return output.ok({**_project_info(dest.resolve(), state=state), "template": tmpl_id, "usedXtool": used_xtool})


def cmd_convert(flags: dict) -> int:
    p = _require_path(flags)
    if (p / "xtool.yml").exists():
        return cmd_add(flags)
    info = _package_info(p / "Package.swift")
    libs = info.get("libraries") or []
    if len(libs) != 1:
        raise HelperError("not-convertible", "To become an app, the package needs exactly one library product (the app).")
    prefix = appdirs.settings().get("bundlePrefix") or "com.example"
    bundle = flags.get("bundle_id") if isinstance(flags.get("bundle_id"), str) else f"{prefix}.{libs[0]}"
    (p / "xtool.yml").write_text(f"version: 1\nbundleID: {bundle}\nproduct: {libs[0]}\n")
    _editor_auto(p)
    return cmd_add({"path": str(p)})


# ---------------------------------------------------------------- editor / folder

def _editor_auto(p: Path) -> None:
    """Editor support files a project needs, written when missing, never overwriting (editor_setup.auto_project):
    .bsp/xtool.json for every project; .vscode/ only for projects created from a template here (meta
    createdFrom) when VS Code is the preferred editor. Best effort: never fails the caller."""
    try:
        from iosstudio.features import editor_setup
        editor_setup.auto_project(p)
    except Exception:  # noqa: BLE001
        pass


def _editor_cmd() -> list[str]:
    st = appdirs.settings()
    s = st.get("editorCommand") or st.get("editor")
    if s and s not in ("auto", True):
        return shlex.split(str(s))
    default_file = appdirs.HOME / ".local/state/omarchy/defaults/editor"
    try:
        ed = default_file.read_text().strip().split()[0]
        if ed and subproc.which(ed):
            return [ed]
    except (OSError, IndexError):
        pass
    for cand in ("zeditor", "zed", "code", "cursor", "codium", "nvim"):
        if subproc.which(cand):
            return [cand]
    return ["nvim"]


TUI_EDITORS = {"nvim", "vim", "vi", "nano", "micro", "hx", "helix", "emacs"}


def _spawn(argv: list[str], cwd: str | None = None) -> None:
    try:
        subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=subproc.tool_env("xtool"))
    except OSError as exc:
        raise HelperError("launch-failed", f"Couldn't open {Path(argv[0]).name}.", fix("Choose editor", "settings"),
                          detail=str(exc))


def cmd_open(flags: dict) -> int:
    p = _require_path(flags)
    file = flags.get("file") if isinstance(flags.get("file"), str) else None
    line = str(flags.get("line") or "") if flags.get("line") not in (None, True) else ""
    col = str(flags.get("col") or "") if flags.get("col") not in (None, True) else ""
    ed = _editor_cmd()
    exe = Path(ed[0]).name
    target_file = None
    if file:
        target_file = file if os.path.isabs(file) else str(p / file)
    if exe in ("zeditor", "zed"):
        argv = ed + [str(p)]
        if target_file:
            argv.append(target_file + (f":{line}" if line else "") + (f":{col}" if line and col else ""))
    elif exe in ("code", "cursor", "codium", "code-oss"):
        argv = ed + [str(p)]
        if target_file:
            argv += ["-g", target_file + (f":{line}" if line else "") + (f":{col}" if line and col else "")]
    elif exe in TUI_EDITORS:
        inner = ed + ([f"+{line}"] if line and target_file else []) + [target_file or str(p)]
        launcher = subproc.which("omarchy-launch-tui")
        argv = [launcher, *inner] if launcher else ["xdg-terminal-exec", *inner]
    else:
        argv = ed + [target_file or str(p)]
    gui = exe not in TUI_EDITORS and subproc.which("uwsm-app")
    _spawn(["setsid", "uwsm-app", "--", *argv] if gui else argv, cwd=str(p))
    return output.ok({"editor": exe, "argv": argv})


def cmd_reveal(flags: dict) -> int:
    p = _require_path(flags)
    opener = subproc.which("nautilus") or subproc.which("xdg-open")
    if not opener:
        raise HelperError("no-file-manager", "No file manager is installed.")
    _spawn([opener, str(p)])
    return output.ok({"path": str(p)})


def cmd_pick_folder(flags: dict) -> int:
    """Native folder chooser (zenity/kdialog). Returns {"path": str|None} (None = cancelled)."""
    start = os.path.expanduser(str(flags.get("start") or (_roots()[0] if _roots() else appdirs.HOME)))
    title = str(flags.get("title") or "Choose a folder")
    if subproc.which("zenity"):
        argv = ["zenity", "--file-selection", "--directory", f"--title={title}", f"--filename={start.rstrip('/')}/"]
    elif subproc.which("kdialog"):
        argv = ["kdialog", "--getexistingdirectory", start, "--title", title]
    else:
        raise HelperError("no-picker", "No folder chooser is installed. Type the folder path instead.")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return output.ok({"path": None})
    path = r.stdout.strip() if r.returncode == 0 else ""
    return output.ok({"path": path or None,
                      "isProject": bool(path) and _is_project(Path(path)),
                      "isPackage": bool(path) and (Path(path) / "Package.swift").is_file()})


# ================================================================ build history

def _history() -> list:
    h = fileio.load_json(BUILDS_FILE, [])
    return h if isinstance(h, list) else []


def _stored_diags(diags: list[dict]) -> list[dict]:
    """Errors first, then warnings/notes, in build order, capped at MAX_STORED_DIAGS."""
    errs = [d for d in diags if d.get("severity") == "error"]
    rest = [d for d in diags if d.get("severity") != "error"]
    return (errs + rest)[:MAX_STORED_DIAGS]


def _append_history(entry: dict) -> None:
    with _file_lock("builds"):
        h = _history()
        h.append(entry)
        h = h[-MAX_HISTORY:]
        fileio.save_json(BUILDS_FILE, h)
        _prune_logs(h)


def _prune_logs(history: list, min_age: float = LOG_GRACE) -> list[str]:
    """Delete build logs no history entry points to any more (history keeps MAX_HISTORY builds).

    A build writes its log before its history entry exists, so a log only goes once it is older than
    `min_age` seconds: a build running right now (here or in another process) keeps its log."""
    keep = set()
    for e in history:
        f = e.get("logFile") if isinstance(e, dict) else None
        if isinstance(f, str):
            keep.add(os.path.realpath(f))
    removed = []
    cutoff = _now() - min_age
    try:
        files = list(BUILD_LOGS.glob("*.log"))
    except OSError:
        return removed
    for f in files:
        try:
            if os.path.realpath(f) in keep or not f.is_file() or f.stat().st_mtime > cutoff:
                continue
            f.unlink()
            removed.append(str(f))
        except OSError:
            continue
    return removed


def cmd_history(flags: dict) -> int:
    h = _history()
    if flags.get("path"):
        sp = _norm(flags["path"])
        h = [e for e in h if e.get("path") == sp]
    limit = int(flags.get("limit") or 50)
    return output.ok({"builds": list(reversed(h))[:limit]})


def _estimate(path: str, kind: str) -> float | None:
    durs = [e["duration"] for e in _history()
            if e.get("path") == path and e.get("kind") == kind and e.get("result") == "ok" and e.get("duration")]
    if not durs:
        return None
    recent = sorted(durs[-5:])
    return recent[len(recent) // 2]


# ================================================================ provisioning profile

def _plist_ts(value: Any) -> float | None:
    """plistlib returns naive datetimes in UTC; .timestamp() on those would read them as local time."""
    if not isinstance(value, datetime):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


def _profile_from_bytes(data: bytes) -> dict | None:
    start = data.find(b"<?xml")
    end = data.find(b"</plist>")
    if start < 0 or end < 0:
        return None
    try:
        pl = plistlib.loads(data[start:end + len(b"</plist>")])
    except Exception:
        return None
    exp = pl.get("ExpirationDate")
    created = pl.get("CreationDate")
    ents = pl.get("Entitlements") or {}
    return {
        "expires": _plist_ts(exp),
        "created": _plist_ts(created),
        "name": pl.get("Name"),
        "uuid": pl.get("UUID"),
        "team": (pl.get("TeamIdentifier") or [None])[0],
        "appId": ents.get("application-identifier"),
        "devices": pl.get("ProvisionedDevices") or [],
    }


def _read_profile(artifact: str | None) -> dict | None:
    if not artifact:
        return None
    p = Path(artifact)
    try:
        if p.suffix == ".ipa" and p.is_file():
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    if n.count("/") == 2 and n.endswith(".app/embedded.mobileprovision"):
                        return _profile_from_bytes(z.read(n))
        elif p.is_dir() and (p / "embedded.mobileprovision").is_file():
            return _profile_from_bytes((p / "embedded.mobileprovision").read_bytes())
    except (OSError, zipfile.BadZipFile):
        return None
    return None


def _record_signing(path: str, udid: str | None, bundle: str | None, via: str, profile: dict | None) -> dict:
    rec = {
        "udid": udid,
        "bundleId": bundle,
        "signedAt": _now(),
        "via": via,
        "expires": (profile or {}).get("expires") or _now() + FREE_PROFILE_DAYS * 86400,
        "estimated": not (profile and profile.get("expires")),
    }
    if profile:
        rec["profile"] = {k: profile.get(k) for k in ("uuid", "team", "appId", "name")}
    with _file_lock("renewals"):
        data = fileio.load_json(RENEWALS_FILE, {}) or {}
        data[path] = rec
        fileio.save_json(RENEWALS_FILE, data)
    return rec


def cmd_record_install(flags: dict) -> int:
    """The UI calls this after the device daemon installed an .ipa we built (Wi-Fi path)."""
    p = _require_path(flags)
    info = _project_info(p)
    ipa = flags.get("ipa") if isinstance(flags.get("ipa"), str) else info.get("ipa")
    rec = _record_signing(str(p), flags.get("udid") if isinstance(flags.get("udid"), str) else None,
                          info.get("bundleId"), str(flags.get("via") or "wifi"), _read_profile(ipa))
    return output.ok({"signing": _signing_status(rec)})


# ================================================================ process runner

_libc = None


def _child_setup(open_files: int | None = None) -> None:
    """preexec: own process group, die with our parent, big open-file limit (SDK module builds; a project's
    openFiles build setting, else 65536 capped at the hard limit). Only the child's limit changes."""
    os.setsid()
    try:
        global _libc
        _libc = _libc or ctypes.CDLL("libc.so.6", use_errno=True)
        _libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass
    build_settings.raise_nofile(open_files)


Cancelled = subproc.Cancelled


class BuildRunner:
    """Runs one xtool command, turning its output into step/progress/log/diagnostic/prompt events."""

    WEIGHTS = {"prebuild": 0.04, "resolve": 0.06, "compile": 0.62, "link": 0.08, "sign": 0.12, "package": 0.04,
               "install": 0.12}
    TITLES = {"prebuild": "Running the prebuild command", "resolve": "Resolving packages", "compile": "Compiling", "link": "Linking",
              "sign": "Signing", "package": "Packaging", "install": "Installing", "launch": "Launching"}

    def __init__(self, plan: list[str], interactive: bool, log_file: Path, estimate: float | None,
                 ui_steps: list[str] | None = None):
        self.plan = plan
        self.ui_steps = ui_steps or []
        self.interactive = interactive
        self.estimate = estimate
        self.state = {s: "pending" for s in plan}
        self.current: str | None = None
        self.sub = 0.0
        self.diags: list[dict] = []
        self.parser = diagnostics.Parser()
        self.wrote: str | None = None
        self.build_secs: float | None = None
        self.revoke_certs: list[str] = []
        self._collect_revoke = False
        self.revoke_answer: str | None = None
        self.tail: list[str] = []
        self.started = _now()
        self.last_progress = 0.0
        self.waiting_device_since: float | None = None
        self.log_fh = open(log_file, "w", buffering=1)
        self._skip_cmdline = False
        self._prompts = 0
        # stdin carries the UI's prompt answers and cancel; the child's stdin gets ours (write_child)
        self.runner = subproc.Runner(lambda text: self.on_line(text), on_partial=lambda text: self.on_partial(text),
                                     on_tick=self._check_device_wait, watch_stdin=True, tail_lines=1)
        self.raw = False            # True while the prebuild runs: log its output, parse nothing
        self.env: dict | None = None
        self.open_files: int | None = None
        self.device_timeout = 90.0

    # ---- events
    def announce(self) -> None:
        total = len(self.plan) + len(self.ui_steps)
        i = 0
        for s in self.plan:
            output.step(s, self.TITLES[s], "pending", index=i, total=total)
            i += 1
        for s in self.ui_steps:
            output.step(s, self.TITLES[s], "pending", index=i, total=total, owner="ui")
            i += 1

    def enter(self, sid: str, detail: str = "") -> None:
        if sid not in self.state or self.state[sid] in ("done",):
            return
        if self.current == sid:
            return
        idx = self.plan.index(sid)
        for prev in self.plan[:idx]:
            if self.state[prev] in ("pending", "running"):
                self.state[prev] = "done"
                output.step(prev, self.TITLES[prev], "done")
        self.current = sid
        self.sub = 0.0
        self.state[sid] = "running"
        output.step(sid, self.TITLES[sid], "running", detail=detail)
        self.emit_progress(detail)

    STEP_OF = {"compiler": "compile", "linker": "link", "package": "resolve"}

    def finish_all(self, ok: bool) -> None:
        # Compiler, linker or package errors name the step that failed; later steps never really ran.
        failed = None if ok else next((self.STEP_OF[c] for d in self.diags if d["severity"] == "error"
                                       for c in [d.get("category") or "compiler"]
                                       if self.STEP_OF.get(c) in self.state), None)
        if not ok and self.state.get("prebuild") in ("running", "failed"):
            failed = "prebuild"
        if failed:
            idx = self.plan.index(failed)
            for earlier in self.plan[:idx]:
                if self.state[earlier] == "running":
                    self.state[earlier] = "done"
                    output.step(earlier, self.TITLES[earlier], "done")
            self.state[failed] = "failed"
            output.step(failed, self.TITLES[failed], "failed")
            for later in self.plan[idx + 1:]:
                if self.state[later] != "pending":
                    self.state[later] = "skipped"
                    output.step(later, self.TITLES[later], "skipped")
            return
        for s in self.plan:
            if self.state[s] == "running" or (ok and self.state[s] == "pending"):
                self.state[s] = "done" if ok else "failed"
                output.step(s, self.TITLES[s], self.state[s])

    def fraction(self) -> float:
        total = sum(self.WEIGHTS.get(s, 0.05) for s in self.plan) or 1.0
        acc = 0.0
        for s in self.plan:
            w = self.WEIGHTS.get(s, 0.05)
            if self.state[s] == "done":
                acc += w
            elif s == self.current:
                acc += w * min(1.0, self.sub)
        self._frac = max(getattr(self, "_frac", 0.0), min(0.99, acc / total))
        return self._frac

    def emit_progress(self, detail: str = "", force: bool = False) -> None:
        now = _now()
        if not force and now - self.last_progress < 0.25:
            return
        self.last_progress = now
        frac = self.fraction()
        eta = None
        elapsed = now - self.started
        if self.estimate:
            eta = max(1.0, self.estimate - elapsed)
        elif frac > 0.15:
            eta = elapsed / frac - elapsed
        output.progress(round(frac, 3), detail, round(eta) if eta else None)

    # ---- line parsing
    def on_line(self, raw: str) -> None:
        line = ANSI.sub("", raw).replace(" ", " ").rstrip()
        self.log_fh.write(line + "\n")
        if not line.strip():
            return
        self.tail.append(line)
        if len(self.tail) > 200:
            self.tail = self.tail[-200:]
        # Swift's "Failed frontend command:" is followed by one gigantic command line: keep it in the
        # file log only.
        if self._skip_cmdline:
            self._skip_cmdline = False
            if len(line) > 600:
                output.log(line[:300] + " …")
                return
        if line.startswith("Failed frontend command:") or line.endswith("Command line:"):
            self._skip_cmdline = True
        output.log(line)
        if not self.raw:
            self.classify(line)

    def classify(self, line: str) -> None:
        s = line.strip()
        found = self.parser.feed(line)
        for d in found:
            self.add_diag(d)
        if diagnostics.DIAG.match(s):
            if (self.current is None or self.current == "resolve") and found and found[-1]["category"] == "compiler":
                self.enter("compile")
            return
        if self._collect_revoke:
            if s.startswith("- "):
                self.revoke_certs.append(s[2:])
                return
            self._collect_revoke = False
        if s.startswith("The following certificates must be revoked"):
            self._collect_revoke = True
            if "sign" in self.state:
                self.enter("sign")
            return
        if re.match(r"^(Fetching|Updating|Cloning|Resolving|Computing version|Working copy|Creating working copy)", s):
            self.enter("resolve", s)
            return
        if s.startswith("Building for") or s.startswith("[Computing dependencies") or s.startswith("[Pre-planning") \
                or s.startswith("[Planning"):
            self.enter("compile", "Preparing")
            return
        t = TASK.match(s)
        if t:
            n, total, what = int(t.group(1)), int(t.group(2)), t.group(3)
            if what.endswith("-product") or what.startswith("Linking"):
                self.enter("link", "Linking the app")
                self.sub = 0.5
            else:
                self.enter("compile")
                if self.current == "compile":
                    self.sub = max(self.sub, n / max(total, 1))
            self.emit_progress(f"{n} of {total} tasks" + (f" · {what}" if what else ""))
            return
        if s.startswith("Compiling") or s.startswith("Emitting module"):
            self.enter("compile", s)
            return
        if s.startswith("Linking"):
            self.enter("link", s)
            return
        b = BUILD_DONE.match(s)
        if b:
            self.build_secs = float(b.group("secs"))
            if "link" in self.state:
                self.enter("link")
                self.sub = 1.0
            return
        st = STAGE.match(s)
        if st and not TASK.match(s):
            stage = st.group("stage").strip()
            pct = st.group("pct")
            low = stage.lower()
            target = None
            if low in ("unpacking app", "preparing device", "provisioning", "signing", "logging in"):
                target = "sign"
            elif low in ("connecting", "installing", "verifying", "uploading"):
                target = "install"
            elif low == "packaging":
                target = "package" if "package" in self.state else ("install" if "install" in self.state else "sign")
            nice = STAGE_TEXT.get(low, stage)
            if target and target in self.state:
                self.enter(target, nice)
                if pct is not None:
                    self.sub = int(pct) / 100.0
                self.emit_progress(f"{nice} · {pct}%" if pct else nice)
            return
        if s.startswith("Planning...") and "sign" in self.state and self.build_secs is not None:
            return
        if s.startswith("Applying entitlements"):
            return
        if s.startswith("Packaging"):
            if "package" in self.state:
                self.enter("package", "Packaging")
                pm = re.search(r"(\d+)%", s)
                if pm:
                    self.sub = int(pm.group(1)) / 100
                    self.emit_progress(s)
            return
        if s.startswith("Installing to device"):
            return
        if s.startswith("Waiting for device to be connected"):
            self.waiting_device_since = _now()
            return
        w = WROTE.match(s)
        if w:
            self.wrote = w.group("path")
            return

    def add_diag(self, d: dict) -> None:
        """One parsed diagnostic (already de-duplicated by the parser) -> the list and a `diagnostic` event."""
        self.diags.append(d)
        output.event("diagnostic", **d)

    def flush_diags(self, failed: bool) -> None:
        """End of output: an open linker/package/crash block becomes a diagnostic, and a failed build with
        no error at all gets one last-resort diagnostic from the tool's final words."""
        for d in self.parser.finish(failed):
            self.add_diag(d)

    # ---- prompts
    def on_partial(self, text: str) -> bool:
        """Called with the unterminated tail of xtool's output. Returns True if it was a prompt we answered."""
        if self.raw:
            return False
        clean = ANSI.sub("", text)
        if "Continue? (yes/no)" in clean:
            self.log_fh.write(clean + "\n")
            output.log(clean.strip())
            self.answer_revoke()
            return True
        if re.search(r"Choice \(\d+-\d+\):\s*$", clean):
            output.log(clean.strip())
            self.write_child("\n")  # can't happen with --udid; answer anything rather than hang
            return True
        if clean.rstrip().endswith("Password:") or clean.rstrip().endswith("Code:"):
            # xtool wants to sign in: never from a build. Stop and send the user to the Account page.
            raise HelperError("not-signed-in", "Your Apple ID sign-in has expired. Sign in again, then build.",
                              fix("Sign in", "account", action="signin"))
        return False

    def answer_revoke(self) -> None:
        certs = self.revoke_certs or ["your current development certificate"]
        answer = "no"
        if self.interactive:
            message = (
                "Apple only lets a free account keep one development certificate, and this computer doesn't have "
                "the private key for the one you already have (it was probably made on another computer or in a "
                "previous install).\n\n"
                "To sign this app here, iOS Studio has to revoke (cancel) it and create a new one. If you do:\n"
                "• apps you signed on other computers stop opening until you re-install them from there,\n"
                "• on your iPhone you'll need to trust the new certificate once (Settings › General › VPN & Device "
                "Management),\n"
                "• your Apple ID itself is not affected.\n\n"
                "Certificates to revoke:\n" + "\n".join(f"• {c}" for c in certs)
            )
            value = self.ask("confirm", message, [
                {"value": "no", "label": "Don't revoke"},
                {"value": "yes", "label": "Revoke and continue", "destructive": True},
            ], title="Replace your signing certificate?", default="no", certificates=certs)
            answer = "yes" if value.strip().lower() in ("yes", "y", "true", "1") else "no"
        self.revoke_answer = answer
        output.log(f"Answered: {answer}")
        self.write_child(answer + "\n")

    def ask(self, kind: str, message: str, options: list[dict], **extra: Any) -> str:
        """Prompt the UI and wait for {"prompt": id, "value": ...} on our stdin (raw reads, one buffer)."""
        self._prompts += 1
        pid = f"p{self._prompts}"
        output.event("prompt", id=pid, kind=kind, message=message, options=options, **extra)
        while True:
            msg = self.runner.next_message()
            if msg is None:
                return str(extra.get("default", ""))
            if msg.get("prompt") == pid:
                return str(msg.get("value", ""))

    @property
    def proc(self) -> subprocess.Popen | None:
        return self.runner.proc

    def write_child(self, text: str) -> None:
        self.runner.write(text)

    def _check_device_wait(self) -> None:
        if self.waiting_device_since and _now() - self.waiting_device_since > self.device_timeout \
                and self.state.get("install") != "running":
            raise HelperError("device-not-found",
                              "Your iPhone didn't show up over the USB cable. Check the cable, unlock the "
                              "phone and tap Trust if it asks.", fix("Open iPhone", "device"))

    # ---- run
    def run(self, argv: list[str], cwd: Path, device_timeout: float = 90) -> int:
        """Run one command to the end: own process group (setsid in _child_setup) with a reaper, so the whole
        tree dies with us; {"cancel": true} on stdin or SIGTERM/SIGINT/SIGHUP raise Cancelled. A prebuild
        command (raw) gets no input: a script that asks something reads EOF, not a hang."""
        env = self.env if self.env is not None else subproc.tool_env("xtool")
        self.log_fh.write(f"$ {' '.join(shlex.quote(a) for a in argv)}\n  (in {cwd})\n")
        open_files = self.open_files
        self.device_timeout = device_timeout
        return self.runner.run(argv, cwd=cwd, env=env, child_stdin=not self.raw, new_session=False,
                               preexec_fn=lambda: _child_setup(open_files), reaper=True, signals=True,
                               exit_grace=EXIT_GRACE, kill_grace=2.0)

    def close(self) -> None:
        try:
            self.log_fh.close()
        except OSError:
            pass


def classify_failure(runner: BuildRunner, rc: int) -> HelperError:
    text = "\n".join(runner.tail[-120:])
    low = text.lower()
    errors = [d for d in runner.diags if d["severity"] == "error" and d.get("file")
              and (d.get("category") or "compiler") == "compiler"]
    other = next((d for d in runner.diags if d["severity"] == "error" and d not in errors), None)
    if runner.revoke_answer == "no" or re.search(r"revok|continue\?", low) and runner.revoke_answer is None:
        return HelperError("needs-certificate",
                           "Signing stopped because a new certificate is needed and it wasn't replaced. "
                           "Nothing was changed on your Apple account.",
                           {"label": "Review certificates", "page": "account", "action": "certificates"}, detail=text)
    if errors:
        n = len(errors)
        first = errors[0]
        return HelperError("compile-errors",
                           f"Your code has {n} error{'s' if n != 1 else ''}. The first is in "
                           f"{Path(first['file']).name}, line {first['line']}: {first['message']}",
                           {"label": "Open in editor", "page": "projects", "action": "open-diagnostic"}, detail=text)
    # Linker, package, config and compiler-crash problems are named by their diagnostic; signing and device
    # ones go through the checks below, which have the right fix buttons.
    lead = {"linker": "Linking failed", "package": "Swift packages couldn't be resolved",
            "config": "The project settings are invalid"}
    manifest = next((d for d in runner.diags if d["severity"] == "error" and d.get("category") == "package"
                     and str(d.get("file") or "").endswith("Package.swift")), None)
    if manifest:
        return HelperError("build-failed", f"Package.swift has errors. The first is on line {manifest['line']}: "
                           f"{manifest['message']}",
                           {"label": "Open in editor", "page": "projects", "action": "open-diagnostic"}, detail=text)
    if other and other.get("category") in lead:
        return HelperError("build-failed", f"{lead[other['category']]}: {other['message']}",
                           {"label": "Show log", "page": "projects", "action": "log"}, detail=text)
    if other and other["message"].startswith("The Swift compiler crashed"):
        return HelperError("build-failed", other["message"] + ".",
                           {"label": "Show log", "page": "projects", "action": "log"}, detail=text)
    if re.search(r"requires logging in|unauthori[sz]ed|not logged in|log ?in again|invalid.*token|session.*expired|"
                 r"(http|status|code)[ :]*401", low):
        return HelperError("not-signed-in", "Apple didn't accept your sign-in. Sign in again, then build.",
                           fix("Sign in", "account", action="signin"), detail=text)
    if re.search(r"anisette|timed out|could not connect|couldn.t connect|network connection|internet|"
                 r"(http|status|code)[ :]*5\d\d|server busy", low):
        return HelperError("apple-unreachable",
                           "Apple's signing service (or the helper server xtool uses) didn't answer. This is usually "
                           "temporary; try again in a minute.", {"label": "Try again", "page": "projects", "action": "retry"},
                           detail=text)
    if re.search(r"maximum number of (installed )?apps|free develop\w* (provisioning )?profiles? (limit|has been)",
                 low):
        return HelperError("app-limit",
                           "A free Apple account can have 3 of your apps on an iPhone at once. Delete one of them from "
                           "the iPhone, then try again.", fix("Open iPhone", "device"), detail=text)
    if re.search(r"maximum number of app ids|max(imum)? app ids|app id limit|up to 10 app ids|too many app ids|"
                 r"no more app ids", low):
        return HelperError("app-id-limit",
                           "A free Apple account can register at most 10 app IDs per week. Reuse an existing bundle "
                           "ID, or wait a few days.", fix("See app IDs", "account", action="identifiers"), detail=text)
    if re.search(r"darwin sdk|swift-sdks|no swift sdk|could not find.*sdk|sdk.*not (found|installed)|unknown sdk", low):
        return HelperError("missing-sdk", "The iOS SDK isn't installed (or needs rebuilding).",
                           fix("Set up the iOS SDK", "setup", step="sdk"), detail=text)
    if "too many open files" in low:
        return HelperError("open-files", "The build hit the system's open-file limit.",
                           {"label": "Try again", "page": "projects", "action": "retry"}, detail=text)
    if re.search(r"device not found|no devices|could not connect to (the )?device|usbmux|lockdown|pairing", low):
        return HelperError("device-unreachable", "Couldn't reach your iPhone. Unlock it, keep it nearby and check the cable.",
                           fix("Open iPhone", "device"), detail=text)
    if re.search(r"provision|profile|entitlement|not registered|device.*not.*(included|registered)", low):
        return HelperError("signing-failed",
                           "Signing failed. If this is a new iPhone, connect it with a cable and choose Run once so it "
                           "gets registered with your account.", fix("Open iPhone", "device"), detail=text)
    last = other["message"] if other else \
        next((ln for ln in reversed(runner.tail) if ln.strip().lower().startswith(("error", "fatal"))), None)
    return HelperError("build-failed", "The build failed." + (f" {last.strip()[:300]}" if last else ""),
                       {"label": "Show log", "page": "projects", "action": "log"}, detail=text)


# ================================================================ build group

@contextmanager
def _lock(path: Path):
    """Hold the project's build lock for the with-block; HelperError('busy') if another build holds it."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(str(path).encode()).hexdigest()[:16]
    with open(LOCK_DIR / f"{key}.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HelperError("busy", "This project is already being built. Wait for it to finish, or cancel it.")
        yield


def _usb_udids() -> list[str]:
    x = _xtool_path()
    if not x:
        return []
    try:
        r = subproc.run_cmd([x, "devices", "--usb", "--no-wait"], timeout=8, env=subproc.tool_env("xtool"))
    except HelperError:
        return []
    return re.findall(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9a-f]{40}", r.stdout)


def _auth_ok() -> bool:
    x = _xtool_path()
    if not x:
        return False
    try:
        r = subproc.run_cmd([x, "auth", "status"], timeout=20, env=subproc.tool_env("xtool"))
    except HelperError:
        return False
    return r.stdout.strip().startswith("Logged in.")


def _streaming(fn):
    """Wrap a streaming command: HelperErrors become a final done event, not a one-shot object."""
    def wrapper(flags: dict) -> int:
        try:
            return fn(flags)
        except HelperError as exc:
            output.done(False, error=exc)
            return 1
        except Cancelled:
            output.done(False, error=HelperError("cancelled", "Build cancelled."))
            return 1
    return wrapper


def _do_build(flags: dict, kind: str, interactive: bool = True) -> tuple[int, dict | None, HelperError | None]:
    """kind: compile | ipa | run | renew | install. Streams events; returns (rc, data, error) without a done event."""
    p = _require_path(flags)
    preset = _launch_preset(p, flags) if kind == "run" else None   # an unknown preset fails before any build
    bs = build_settings.current(_state()["meta"].get(str(p)))
    prebuild = bs["prebuild"] if kind != "install" else None
    # A prebuild command may write xtool.yml itself, so a fresh checkout without one can still build.
    if not _is_project(p) and not (prebuild and (p / "Package.swift").is_file()):
        raise HelperError("not-a-project", "That folder isn't an xtool app (it needs Package.swift and xtool.yml).")
    xtool = _xtool()
    config = str(flags.get("config") or "debug").lower()
    if config not in ("debug", "release"):
        raise HelperError("usage", "--config must be debug or release.")
    udid = flags.get("udid") if isinstance(flags.get("udid"), str) else None
    info = _project_info(p)
    product, bundle = info["product"], info["bundleId"]
    if info["generated"] and not flags.get("force") and not bs["allowGenerated"]:
        raise HelperError("generated-project",
                          "This project's xtool.yml is generated by its own scripts, so iOS Studio won't build it until "
                          "you allow that: in its Build settings, set the script that regenerates xtool.yml as the "
                          "prebuild command and turn on \"Build generated project\".",
                          {"label": "Build settings", "page": "projects", "action": "build-settings", "path": str(p)})
    nofile_err = build_settings.check_nofile(bs["openFiles"]) if kind != "install" else None
    if nofile_err:
        raise HelperError("open-files-limit", nofile_err,
                          {"label": "Build settings", "page": "projects", "action": "build-settings", "path": str(p)})

    via = None
    if kind in ("run", "renew", "install"):
        if not udid:
            raise HelperError("no-device", "Connect your iPhone first (cable or Wi-Fi), then press Run.",
                              fix("Open iPhone", "device"))
        via = "usb" if (flags.get("via") != "wifi" and udid in _usb_udids()) else "wifi"
        if flags.get("via") == "usb" and via != "usb":
            raise HelperError("device-not-on-usb", "Your iPhone isn't connected with a cable right now.",
                              fix("Open iPhone", "device"))
    if kind in ("ipa", "run", "renew") and not _auth_ok():
        raise HelperError("not-signed-in", "Sign in with your Apple ID so iOS Studio can sign the app for your iPhone.",
                          fix("Sign in", "account", action="signin"))

    with _lock(p):
        BUILD_LOGS.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_file = BUILD_LOGS / f"{stamp}-{re.sub(r'[^A-Za-z0-9_-]', '_', product)}-{kind}.log"
        ui_steps: list[str] = []
        if kind == "compile":
            plan = ["resolve", "compile", "link"]
            argv = [xtool, "dev", "build", "-c", config]
        elif kind == "ipa":
            plan = ["resolve", "compile", "link", "sign", "package"]
            argv = [xtool, "dev", "build", "-c", config, "--sign", "--ipa"]
        elif kind == "install":
            ipa = info.get("ipa")
            if not ipa:
                raise HelperError("no-build", "There's no signed build of this app yet. Choose Run to build and install it.",
                                  {"label": "Run", "page": "projects", "action": "run"})
            if via == "usb":
                plan = ["install"]
                argv = [xtool, "install", "-u", udid, "--usb", ipa]
            else:
                output.step("install", "Installing", "pending", owner="ui")
                return 0, {"ipa": ipa, "bundleId": bundle, "product": product, "udid": udid, "via": via,
                           "needsInstall": True, "installed": False, "kind": kind, "path": str(p)}, None
        else:  # run / renew
            if via == "usb":
                plan = ["resolve", "compile", "link", "sign", "install"]
                argv = [xtool, "dev", "run", "-c", config, "--usb", "--udid", udid]
            else:
                plan = ["resolve", "compile", "link", "sign", "package"]
                ui_steps = ["install"]
                argv = [xtool, "dev", "build", "-c", config, "--sign", "--ipa"]
            if kind == "run":
                ui_steps.append("launch")

        if prebuild:
            plan = ["prebuild"] + plan
        est = _estimate(str(p), kind)
        runner = BuildRunner(plan, interactive, log_file, est, ui_steps)
        runner.env = build_settings.merged_env(subproc.tool_env("xtool"), bs)
        runner.open_files = bs["openFiles"]
        output.event("build", kind=kind, path=str(p), project=product, bundleId=bundle, config=config, udid=udid, via=via,
                     logFile=str(log_file), estimate=est, plan=plan + ui_steps)
        runner.announce()
        runner.enter(plan[0], prebuild or "")
        started = _now()
        rc = 1
        err: HelperError | None = None
        data: dict | None = None
        try:
            if prebuild:
                rc = _run_prebuild(runner, prebuild, p)
                if rc == 0:   # the prebuild may have rewritten xtool.yml: build what it wrote
                    if not _is_project(p):
                        raise HelperError("not-a-project", "The prebuild command finished, but the folder still has no "
                                          "xtool.yml, so there's nothing to build.",
                                          {"label": "Build settings", "page": "projects", "action": "build-settings",
                                           "path": str(p)})
                    info = _project_info(p)
                    product, bundle = info["product"], info["bundleId"]
                    runner.enter(plan[1])
                    _precheck(runner, p)
                    rc = runner.run(argv, p)
                else:
                    err = _prebuild_failed(runner, prebuild, rc, p)
            else:
                if kind != "install":
                    _precheck(runner, p)
                rc = runner.run(argv, p)
        except Cancelled:
            err = HelperError("cancelled", "Build cancelled.")
        except HelperError as exc:
            err = exc
    runner.flush_diags(failed=err is None and rc != 0)
    duration = round(_now() - started, 1)
    errors = sum(1 for d in runner.diags if d["severity"] == "error")
    warnings = sum(1 for d in runner.diags if d["severity"] == "warning")
    if err is None and rc != 0:
        err = classify_failure(runner, rc)
    ok_ = err is None
    runner.finish_all(ok_)
    if ok_:
        output.progress(1.0, "Done", 0)
    artifact = runner.wrote or (_existing_artifact(p, product, ".ipa" if kind in ("ipa",) or (kind in ("run", "renew") and via == "wifi") else ".app"))
    if kind == "install":
        artifact = info.get("ipa")
    ipa = artifact if artifact and artifact.endswith(".ipa") else None
    profile = _read_profile(artifact) if ok_ and kind != "compile" else None
    installed = ok_ and via == "usb" and kind in ("run", "renew", "install")
    signing = None
    if installed:
        signing = _signing_status(_record_signing(str(p), udid, bundle, "usb", profile))
    entry = {
        "project": product, "name": product, "path": str(p), "kind": kind, "config": config,
        "result": "ok" if ok_ else ("cancelled" if err and err.code == "cancelled" else "failed"),
        "duration": duration, "time": started, "ipa": ipa, "app": artifact if artifact and artifact.endswith(".app") else None,
        "errors": errors, "warnings": warnings, "logFile": str(log_file), "udid": udid, "via": via,
        "bundleId": bundle, "errorCode": err.code if err else None, "errorMessage": err.message if err else None,
        "preset": preset["name"] if preset else None,
        "buildId": flags["build_id"] if isinstance(flags.get("build_id"), str) else None,   # watch mode's w-<n>
        "trigger": flags["trigger"] if isinstance(flags.get("trigger"), str) else None,     # "watch": compile on save
    }
    _append_history({**entry, "diagnostics": _stored_diags(runner.diags)})
    runner.log_fh.write(f"\n# {entry['result']} in {duration}s\n")
    runner.close()
    if ok_ and kind != "install":
        _editor_auto(p)
    if ok_ and kind in ("ipa", "run", "renew"):
        from iosstudio.features import (
            crash_symbols,  # keep this build's binary + dSYM so its crash reports resolve to file:line
        )
        crash_symbols.remember_build(p, product, config)
    if ok_:
        data = {
            **entry,
            "diagnostics": runner.diags,
            "buildSeconds": runner.build_secs,
            "installed": installed,
            "needsInstall": bool(kind in ("run", "renew") and via == "wifi"),
            "launch": {"bundleId": bundle, "udid": udid, "args": preset["args"] if preset else [],
                       "env": preset["env"] if preset else {}, "preset": preset["name"] if preset else None}
                      if kind == "run" else None,
            "profile": profile,
            "signing": signing,
        }
    else:
        err.detail = None  # the UI has the full log already; keep the done event small
        data = {"diagnostics": runner.diags, **entry}
    return (0 if ok_ else 1), data, err


PRECHECK_BUDGET = 0.8    # seconds; the checks stop early and report what they have
PRECHECK_MAX = 20


def _precheck(runner: BuildRunner, p: Path) -> None:
    """Config warnings (project_check) as the build's first diagnostics. Warnings only, never an error, so they
    can't change how the build is judged; any failure here is swallowed and the build goes on."""
    try:
        from iosstudio.features import project_check
        res = project_check.check(p, deadline=time.monotonic() + PRECHECK_BUDGET)
        for issue in [i for i in res["issues"] if i["severity"] == "warning"][:PRECHECK_MAX]:
            d = {k: v for k, v in issue.items() if v is not None}
            if d.get("file"):
                d["file"] = os.path.normpath(os.path.join(p, d["file"]))
            runner.add_diag(d)
    except Exception:  # noqa: BLE001  a check must never fail or block a build
        pass


def _run_prebuild(runner: BuildRunner, cmd: str, p: Path) -> int:
    """Runs the project's prebuild command (argv from shlex, no shell) in the project folder, its output going to
    the build log as-is. Returns its exit code; 127 if it can't be started."""
    runner.enter("prebuild", cmd)
    try:
        argv = build_settings.prebuild_argv(cmd)
    except ValueError as exc:
        runner.on_line(f"Can't split the prebuild command into arguments: {exc}")
        return 2
    exe = argv[0]
    if "/" in exe and not os.path.isabs(exe):
        exe = str(p / exe)
    if "/" in exe and not os.access(exe, os.X_OK):
        runner.on_line(f"{argv[0]}: not found or not executable" if not os.path.exists(exe)
                       else f"{argv[0]}: not executable (chmod +x it, or run it through its interpreter, "
                            f"e.g. bash {argv[0]})")
        return 127
    if "/" not in exe and not shutil.which(exe, path=(runner.env or {}).get("PATH")):
        runner.on_line(f"{exe}: command not found")
        return 127
    runner.raw = True
    try:
        return runner.run(argv, p)
    finally:
        runner.raw = False


def _prebuild_failed(runner: BuildRunner, cmd: str, rc: int, p: Path) -> HelperError:
    """A failed prebuild: one `config` diagnostic (its last output lines as details) and the error."""
    tail = [ln for ln in runner.tail[-40:] if ln.strip() and not ln.startswith("$ ")]
    last = tail[-1].strip() if tail else ""
    msg = f"The prebuild command `{cmd}` failed (exit code {rc})" + (f": {last}" if last else ".")
    runner.add_diag(runner.parser._diag(
        msg, "config", hint="Fix the prebuild command or the script it runs (its full output is in the build log), "
        "or change it in the project's build settings (ios_project_build_settings).", details=tail[-6:]))
    return HelperError("prebuild-failed", msg[:400],
                       {"label": "Build settings", "page": "projects", "action": "build-settings", "path": str(p)})


def _finish(rc: int, data: dict | None, err: HelperError | None) -> int:
    if err is None:
        return output.done(True, data)
    payload = err.to_json()
    if data:
        payload["build"] = data
    output.event("done", ok=False, error=payload)
    return 1


@_streaming
def cmd_build_compile(flags: dict) -> int:
    return _finish(*_do_build(flags, "compile"))


@_streaming
def cmd_build_ipa(flags: dict) -> int:
    return _finish(*_do_build(flags, "ipa"))


@_streaming
def cmd_build_run(flags: dict) -> int:
    return _finish(*_do_build(flags, "run"))


@_streaming
def cmd_build_install(flags: dict) -> int:
    return _finish(*_do_build(flags, "install"))


@_streaming
def cmd_renew(flags: dict) -> int:
    rc, data, err = _do_build(flags, "renew", interactive=not flags.get("background"))
    return _finish(rc, data, err)


def cmd_build_clean(flags: dict) -> int:
    p = _require_path(flags)
    removed = []
    for d in (".build", "xtool"):
        t = p / d
        if t.is_dir() and _is_project(p):
            shutil.rmtree(t, ignore_errors=True)
            removed.append(str(t))
    return output.ok({"removed": removed})


def cmd_build_log(flags: dict) -> int:
    f = flags.get("file")
    if not isinstance(f, str):
        h = _history()
        if flags.get("path"):
            sp = _norm(flags["path"])
            h = [e for e in h if e.get("path") == sp]
        if not h:
            raise HelperError("not-found", "No builds yet.")
        f = h[-1].get("logFile")
    p = Path(f).resolve()
    if BUILD_LOGS.resolve() not in p.parents or not p.is_file():
        raise HelperError("not-found", "That build log is gone.")
    text = p.read_text(errors="replace")
    return output.ok({"file": str(p), "text": text[-400000:], "truncated": len(text) > 400000})


# ================================================================ auto-renew

def _renew_due(days: float) -> list[dict]:
    state = _state()
    renewals = fileio.load_json(RENEWALS_FILE, {}) or {}
    due = []
    for sp, meta in state["meta"].items():
        if not meta.get("autoRenew"):
            continue
        rec = renewals.get(sp) or {}
        udid = rec.get("udid") or meta.get("udid")
        exp = rec.get("expires")
        left = (exp - _now()) / 86400 if exp else None
        due.append({"path": sp, "udid": udid, "daysLeft": round(left, 2) if left is not None else None,
                    "due": bool(udid) and (left is None or left < days), "exists": Path(sp).is_dir()})
    return due


def _deviced_once(cmd: str, args: dict, timeout: float = 300) -> dict | None:
    """Ask the device daemon for one request without the app (background renewals over Wi-Fi).

    Contract request to the device slice: `bin/iosstudio-deviced --once <cmd> '<json args>'` prints one
    reply object {"ok": ..., "data"|"error": ...} and exits. Returns None if unsupported.
    """
    exe = appdirs.REPO / "bin/iosstudio-deviced"
    if not exe.exists():
        return None
    try:
        r = subprocess.run([str(exe), "--once", cmd, json.dumps(args)], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "ok" in obj and "event" not in obj:
            return obj
    return None


def cmd_renew_due(flags: dict) -> int:
    """Background job (systemd timer): renew every auto-renew project whose signing has < N days left.

    Exit 0 ok/nothing due, 75 try later (phone unreachable, Apple busy), 78 needs the user, 1 failed.
    Streams the same JSON lines as a build so `journalctl --user -u ios-studio-renew` is readable.
    """
    days = float(flags.get("days") or appdirs.settings().get("autoRenewDays") or 2)
    dry = bool(flags.get("dry_run"))
    items = _renew_due(days)
    due = [i for i in items if i["due"] and i["exists"]]
    output.event("renew-plan", days=days, projects=items, dryRun=dry)
    if dry or not due:
        return output.done(True, {"renewed": [], "due": due, "dryRun": dry})
    codes: list[int] = []
    renewed, failed = [], []
    for it in due:
        name = Path(it["path"]).name
        try:
            _rc, data, err = _do_build({"path": it["path"], "udid": it["udid"]}, "renew", interactive=False)
        except HelperError as exc:
            data, err = None, exc
        if err is None and data and data.get("needsInstall"):
            reply = _deviced_once("app.install", {"udid": it["udid"], "path": data["ipa"]})
            if reply and reply.get("ok"):
                _record_signing(it["path"], it["udid"], data.get("bundleId"), "wifi", data.get("profile"))
            else:
                err = HelperError("device-unreachable", "Your iPhone wasn't reachable to install the renewed app.")
        if err is None:
            renewed.append(it["path"])
            continue
        failed.append({"path": it["path"], "error": err.to_json()})
        code = EXIT_USER if err.code in ("needs-certificate", "not-signed-in", "signing-failed") else (
            EXIT_TEMP if err.code in ("apple-unreachable", "device-unreachable", "device-not-found", "no-device", "busy")
            else 1)
        codes.append(code)
        if code == EXIT_USER:
            _notify(f"{name} needs you to renew", err.message + " Open iOS Studio › Projects.", "critical")
    if renewed:
        _notify("Apps renewed", ", ".join(Path(p).name for p in renewed) + " will keep opening for another week.", "low")
    output.done(not failed, {"renewed": renewed, "failed": failed},
                None if not failed else HelperError(failed[0]["error"]["code"], failed[0]["error"]["message"]))
    if not failed:
        return 0
    return EXIT_USER if EXIT_USER in codes else (EXIT_TEMP if EXIT_TEMP in codes else 1)


def _unit_paths() -> tuple[Path, Path]:
    return SYSTEMD_DIR / f"{UNIT}.service", SYSTEMD_DIR / f"{UNIT}.timer"


def _unit_texts() -> tuple[str, str]:
    helper = appdirs.REPO / "bin/iosstudio-helper"
    esc = str(helper).replace("%", "%%")
    service = f"""# GENERATED by iOS Studio (projects autorenew --enable); removed by --disable. Don't edit.
[Unit]
Description=iOS Studio: renew free 7-day app signing on your iPhone
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart="{esc}" projects renew-due --background
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=XDG_CONFIG_HOME=%h/.config
Nice=10
TimeoutStartSec=45min
# 75: phone unreachable or Apple busy, 1: build failed -> retry in 3 hours. 78: needs you.
Restart=on-failure
RestartSec=3h
RestartPreventExitStatus=78
"""
    timer = """# GENERATED by iOS Studio (projects autorenew --enable); removed by --disable. Don't edit.
[Unit]
Description=iOS Studio: daily check of app signing expiry

[Timer]
OnCalendar=*-*-* 10:00:00
Persistent=true
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
"""
    return service, timer


SYSTEMCTL_TIMEOUT = 20


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    """`systemctl --user ...`, bounded: a wedged user manager must not hang the helper."""
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                              timeout=SYSTEMCTL_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise HelperError("systemd-timeout", f"systemctl --user {args[0]} didn't answer in {SYSTEMCTL_TIMEOUT} s. "
                          "Your user systemd may be busy; try again in a moment.")


def _timer_status() -> dict:
    svc, tmr = _unit_paths()
    out: dict[str, Any] = {"installed": svc.exists() and tmr.exists(), "unit": f"{UNIT}.timer"}
    if shutil.which("systemctl"):
        try:
            r = _systemctl("is-enabled", f"{UNIT}.timer")
            out["enabled"] = r.stdout.strip() == "enabled"
            r = _systemctl("show", f"{UNIT}.timer", "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec")
            for ln in r.stdout.splitlines():
                k, _, v = ln.partition("=")
                out["next" if k == "NextElapseUSecRealtime" else "last"] = v or None
            r = _systemctl("show", f"{UNIT}.service", "-p", "Result", "-p", "ExecMainStatus")
            for ln in (r.stdout.splitlines() if out["installed"] else []):
                k, _, v = ln.partition("=")
                out["lastResult" if k == "Result" else "lastExit"] = v
        except HelperError:
            pass    # status is best-effort: show what the unit files say
    out["projects"] = _renew_due(float(appdirs.settings().get("autoRenewDays") or 2))
    return out


def cmd_autorenew(flags: dict) -> int:
    dry = bool(flags.get("dry_run"))
    svc, tmr = _unit_paths()
    if flags.get("enable"):
        service, timer = _unit_texts()
        actions = [f"write {svc}", f"write {tmr}", "systemctl --user daemon-reload",
                   f"systemctl --user enable --now {UNIT}.timer"]
        if dry:
            return output.ok({"dryRun": True, "actions": actions, "service": service, "timer": timer})
        fileio.atomic_write(svc, service)
        fileio.atomic_write(tmr, timer)
        _systemctl("daemon-reload")
        r = _systemctl("enable", "--now", f"{UNIT}.timer")
        if r.returncode != 0:
            raise HelperError("systemd", "Couldn't turn on automatic renewal.", detail=r.stderr)
        return output.ok(_timer_status())
    if flags.get("disable"):
        actions = [f"systemctl --user disable --now {UNIT}.timer", f"remove {svc}", f"remove {tmr}",
                   "systemctl --user daemon-reload"]
        if dry:
            return output.ok({"dryRun": True, "actions": actions})
        _systemctl("disable", "--now", f"{UNIT}.timer")
        for f in (svc, tmr):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        _systemctl("daemon-reload")
        return output.ok(_timer_status())
    return output.ok(_timer_status())


# ================================================================ summary (Home / sidebar)

def cmd_summary(flags: dict) -> int:
    h = _history()
    renewals = fileio.load_json(RENEWALS_FILE, {}) or {}
    soon = []
    for sp, rec in renewals.items():
        st = _signing_status(rec)
        if st and st["daysLeft"] is not None and st["daysLeft"] < 2:
            soon.append({"path": sp, "name": Path(sp).name, **st})
    return output.ok({"lastBuild": h[-1] if h else None, "recentBuilds": list(reversed(h))[:8], "expiring": soon})


def _format():
    from iosstudio.features import format as swift_format  # lazy: only `projects format` needs it
    return swift_format


GROUPS = {
    "projects": {
        "list": cmd_list,
        "info": cmd_info,
        "check": cmd_check,
        "format": lambda flags: _format().cmd_format(flags),        # iosstudio/features/format.py
        "format-lint": lambda flags: _format().cmd_lint(flags),
        "build-settings": cmd_build_settings,
        "presets": cmd_presets,
        "add": cmd_add,
        "remove": cmd_remove,
        "unhide": cmd_unhide,
        "set": cmd_set,
        "templates": cmd_templates,
        "new": cmd_new,
        "convert": cmd_convert,
        "open": cmd_open,
        "reveal": cmd_reveal,
        "renew": cmd_renew,
        "renew-due": cmd_renew_due,
        "autorenew": cmd_autorenew,
        "record-install": cmd_record_install,
        "summary": cmd_summary,
        "pick-folder": cmd_pick_folder,
    },
    "build": {
        "run": cmd_build_run,
        "compile": cmd_build_compile,
        "ipa": cmd_build_ipa,
        "install": cmd_build_install,
        "history": cmd_history,
        "log": cmd_build_log,
        "clean": cmd_build_clean,
    },
}
