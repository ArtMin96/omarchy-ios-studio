"""The `projects` and `build` command groups: flag parsing and output around model/store/build/renew.

The build machinery (runner, build, renew) is imported inside the handlers that need it, so one-shot commands
like `projects list` stay quick to start."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from iosstudio.core import appdirs, output, subproc
from iosstudio.core.output import HelperError, fix
from iosstudio.features import agent_instructions, build_settings, run_presets
from iosstudio.features.projects import model, store

TUI_EDITORS = {"nvim", "vim", "vi", "nano", "micro", "hx", "helix", "emacs"}


# ================================================================ projects: list and info

def cmd_list(flags: dict) -> int:
    state = store.load_state()
    found, conv = model.scan()
    paths: dict[str, Path] = {}
    for p in found:
        paths[str(p)] = p
    for a in state["added"]:
        p = Path(a)
        if p.is_dir() and model.is_project(p):
            paths[str(p)] = p
    hidden = set(state["hidden"])
    history = store.load_history()
    renewals = store.load_renewals()
    items = [model.project_info(p, history, renewals, state) for k, p in paths.items() if k not in hidden]
    missing = [a for a in state["added"] if not Path(a).is_dir()]

    def sort_key(it: dict) -> tuple:
        lb = (it.get("lastBuild") or {}).get("time") or 0
        return (-(lb or 0), -(it.get("modified") or 0), it["name"].lower())

    items.sort(key=sort_key)
    return output.ok({
        "projects": items,
        "convertible": [c for c in conv if c["path"] not in hidden],
        "missing": missing,
        "roots": [str(r) for r in model.roots()],
        "defaultRoots": [str(r) for r in appdirs.default_project_roots()],
        "defaultDir": str(appdirs.new_app_dir()),
        "xtool": bool(subproc.which("xtool")),
    })


def cmd_info(flags: dict) -> int:
    p = model.require_project(flags)
    info = model.project_info(p)
    info["suggestedBuildSettings"] = build_settings.suggest(p, info["buildSettings"])
    try:   # editor support in this project (.bsp file, ready, hint); see features/editor_setup/
        from iosstudio.features import editor_setup
        info["editor"] = editor_setup.brief(p)
    except Exception:  # noqa: BLE001 - never let it break project info
        info["editor"] = None
    return output.ok(info)


def cmd_check(flags: dict) -> int:
    """projects check --path P [--max N]: config mistakes found before building (warnings only; reads, never
    writes). See helper/iosstudio/features/project_check.py."""
    p = model.require_project(flags)
    try:
        cap = max(0, int(flags.get("max") or 50))
    except (TypeError, ValueError):
        raise HelperError("usage", "--max must be a number.") from None
    from iosstudio.features import project_check
    t0 = time.monotonic()
    res = project_check.check(p)
    return output.ok(project_check.summarize(p, res["issues"], res["keysSource"],
                                             int((time.monotonic() - t0) * 1000), cap))


def cmd_summary(flags: dict) -> int:
    """Home / sidebar: the last builds and apps whose signing expires within 2 days."""
    h = store.load_history()
    soon = []
    for sp, rec in store.load_renewals().items():
        st = store.signing_status(rec)
        if st and st["daysLeft"] is not None and st["daysLeft"] < 2:
            soon.append({"path": sp, "name": Path(sp).name, **st})
    return output.ok({"lastBuild": h[-1] if h else None, "recentBuilds": list(reversed(h))[:8], "expiring": soon})


# ================================================================ projects: settings stored in projects.json

def cmd_build_settings(flags: dict) -> int:
    """projects build-settings --path P [--set '<json patch>' [--dry-run]]: show or change a project's build
    settings (prebuild, env, openFiles, allowGenerated). The patch is merged (see build_settings.apply_patch);
    --suggested starts the patch from what the project's own scripts imply. Returns
    {path, settings, before, changes, saved, suggested}."""
    p = model.require_path(flags)
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
    with store.edit_state() as state:
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


def cmd_presets(flags: dict) -> int:
    """projects presets --path P [--save '<preset json>' [--rename-from OLD] | --delete NAME [--dry-run] |
    --select NAME|'' | --resolve NAME]: list or change a project's run presets (see run_presets.py).
    Returns {path, presets: [preset + resolved], selected} (+ saved / deleted, dryRun), or {name, args, env}
    for --resolve."""
    sp = str(model.require_project(flags))
    with store.edit_state() as state:
        meta = state["meta"].get(sp) or {}
        presets = run_presets.stored(meta)
        sel = run_presets.selected(meta, presets)
        presets, sel, extra, changed = run_presets.act(presets, sel, flags)
        if changed:
            run_presets.store(state["meta"].setdefault(sp, {}), presets, sel)
            if not state["meta"][sp]:
                state["meta"].pop(sp)
    return output.ok(run_presets.result(sp, presets, sel, extra))


def cmd_set(flags: dict) -> int:
    p = model.require_path(flags)
    with store.edit_state() as state:
        meta = state["meta"].setdefault(str(p), {})
        if "auto_renew" in flags:
            meta["autoRenew"] = str(flags["auto_renew"]).lower() in ("1", "true", "yes", "on")
        if flags.get("udid") and flags["udid"] is not True:
            meta["udid"] = flags["udid"]
    return output.ok(model.project_info(p, state=state))


# ================================================================ projects: the list itself

def cmd_add(flags: dict) -> int:
    return output.ok(_add(flags))


def _add(flags: dict) -> dict:
    """Put an xtool app on the list (and take it off the hidden list); its project_info."""
    p = model.require_path(flags)
    if not model.is_project(p):
        if (p / "Package.swift").is_file():
            raise HelperError("needs-conversion", "This Swift package has no xtool.yml yet, so it can't be built as an iPhone app.",
                              {"label": "Make it an app", "page": "projects", "action": "convert", "path": str(p)})
        raise HelperError("not-a-project", "That folder doesn't contain an iPhone app project (Package.swift and xtool.yml).",
                          fix("Create a new app", "projects", action="new"))
    sp = str(p)
    with store.edit_state() as state:
        if sp not in state["added"]:
            state["added"].append(sp)
        if sp in state["hidden"]:
            state["hidden"].remove(sp)
    return model.project_info(p, state=state)


def cmd_remove(flags: dict) -> int:
    raw = flags.get("path")
    if not raw or raw is True:
        raise HelperError("usage", "Missing --path.")
    sp = model.norm_path(raw)
    with store.edit_state() as state:
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
    sp = model.norm_path(flags.get("path") or "")
    with store.edit_state() as state:
        if sp in state["hidden"]:
            state["hidden"].remove(sp)
    return output.ok({"path": sp})


def cmd_templates(flags: dict) -> int:
    return output.ok({"templates": [{k: v for k, v in m.items() if k != "_dir"} for m in model.templates()],
                      "bundlePrefix": appdirs.settings().get("bundlePrefix") or "com.example",
                      "defaultDir": str(appdirs.new_app_dir())})


def cmd_new(flags: dict) -> int:
    dest, tmpl_id, used_xtool, agent_files = model.create_app(flags)
    return output.ok({**model.project_info(dest), "template": tmpl_id, "usedXtool": used_xtool,
                      "agentFiles": agent_files})


def cmd_convert(flags: dict) -> int:
    """Make a Swift package an app (writes xtool.yml) and add it. A folder that already has xtool.yml is just
    added. Either way the agent instruction files go in when wanted and missing: converting is the moment the
    folder becomes an iOS Studio project, and an existing AGENTS.md / CLAUDE.md is never touched."""
    p = model.require_path(flags)
    if not (p / "xtool.yml").exists():
        _write_xtool_yml(p, flags)
    info = _add({"path": str(p)})
    files = []
    if agent_instructions.wanted(flags):
        files = agent_instructions.write(p, str(info.get("name") or p.name), str(info.get("bundleId") or "(not set in xtool.yml yet)"))
    return output.ok({**info, "agentFiles": files})


def _write_xtool_yml(p: Path, flags: dict) -> None:
    libs = model.package_info(p / "Package.swift").get("libraries") or []
    if len(libs) != 1:
        raise HelperError("not-convertible", "To become an app, the package needs exactly one library product (the app).")
    prefix = appdirs.settings().get("bundlePrefix") or "com.example"
    bundle = flags.get("bundle_id") if isinstance(flags.get("bundle_id"), str) else f"{prefix}.{libs[0]}"
    (p / "xtool.yml").write_text(f"version: 1\nbundleID: {bundle}\nproduct: {libs[0]}\n")
    model.write_editor_files(p)


# ================================================================ projects: editor, file manager, folder picker

def _editor_cmd() -> list[str]:
    """The user's editor: settings editorCommand/editor, else Omarchy's default editor, else the first found."""
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


def _spawn_detached(argv: list[str], cwd: str | None = None) -> None:
    try:
        subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=subproc.tool_env("xtool"))
    except OSError as exc:
        raise HelperError("launch-failed", f"Couldn't open {Path(argv[0]).name}.", fix("Choose editor", "settings"),
                          detail=str(exc))


def cmd_open(flags: dict) -> int:
    p = model.require_path(flags)
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
    _spawn_detached(["setsid", "uwsm-app", "--", *argv] if gui else argv, cwd=str(p))
    return output.ok({"editor": exe, "argv": argv})


def cmd_reveal(flags: dict) -> int:
    p = model.require_path(flags)
    opener = subproc.which("nautilus") or subproc.which("xdg-open")
    if not opener:
        raise HelperError("no-file-manager", "No file manager is installed.")
    _spawn_detached([opener, str(p)])
    return output.ok({"path": str(p)})


def cmd_pick_folder(flags: dict) -> int:
    """Native folder chooser (zenity/kdialog). Returns {"path": str|None} (None = cancelled)."""
    roots = model.roots()
    start = os.path.expanduser(str(flags.get("start") or (roots[0] if roots else appdirs.HOME)))
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
                      "isProject": bool(path) and model.is_project(Path(path)),
                      "isPackage": bool(path) and (Path(path) / "Package.swift").is_file()})


# ================================================================ projects: signing and auto-renew

def cmd_record_install(flags: dict) -> int:
    """The UI calls this after the device daemon installed an .ipa we built (Wi-Fi path)."""
    p = model.require_path(flags)
    info = model.project_info(p)
    ipa = flags.get("ipa") if isinstance(flags.get("ipa"), str) else info.get("ipa")
    rec = store.record_signing(str(p), flags.get("udid") if isinstance(flags.get("udid"), str) else None,
                               info.get("bundleId"), str(flags.get("via") or "wifi"), store.read_profile(ipa))
    return output.ok({"signing": store.signing_status(rec)})


def cmd_renew(flags: dict) -> int:
    from iosstudio.features.projects import build
    return build.streaming(lambda f: build.finish(*build.do_build(f, "renew", interactive=not f.get("background"))))(flags)


def cmd_renew_due(flags: dict) -> int:
    """Background job (systemd timer): renew every auto-renew project whose signing has < N days left."""
    from iosstudio.features.projects import renew
    days = float(flags.get("days") or appdirs.settings().get("autoRenewDays") or 2)
    return renew.renew_due(days, bool(flags.get("dry_run")))


def cmd_autorenew(flags: dict) -> int:
    """projects autorenew [--enable | --disable] [--dry-run]: the renew timer's status, or turn it on/off."""
    from iosstudio.features.projects import renew
    dry = bool(flags.get("dry_run"))
    if flags.get("enable"):
        return output.ok(renew.enable_timer(dry))
    if flags.get("disable"):
        return output.ok(renew.disable_timer(dry))
    return output.ok(renew.timer_status())


# ================================================================ build group

def _build_command(kind: str):
    """A streaming `build <kind>` handler (events, then one done event)."""
    def handler(flags: dict) -> int:
        from iosstudio.features.projects import build
        return build.streaming(lambda f: build.finish(*build.do_build(f, kind)))(flags)
    return handler


def cmd_history(flags: dict) -> int:
    h = store.load_history()
    if flags.get("path"):
        sp = model.norm_path(flags["path"])
        h = [e for e in h if e.get("path") == sp]
    limit = int(flags.get("limit") or 50)
    return output.ok({"builds": list(reversed(h))[:limit]})


def cmd_build_log(flags: dict) -> int:
    f = flags.get("file")
    if not isinstance(f, str):
        h = store.load_history()
        if flags.get("path"):
            sp = model.norm_path(flags["path"])
            h = [e for e in h if e.get("path") == sp]
        if not h:
            raise HelperError("not-found", "No builds yet.")
        f = h[-1].get("logFile")
    p = Path(f).resolve()
    if store.BUILD_LOGS.resolve() not in p.parents or not p.is_file():
        raise HelperError("not-found", "That build log is gone.")
    text = p.read_text(errors="replace")
    return output.ok({"file": str(p), "text": text[-400000:], "truncated": len(text) > 400000})


def cmd_build_clean(flags: dict) -> int:
    from iosstudio.features.projects import build
    return output.ok({"removed": build.clean(model.require_path(flags))})


def _format():
    from iosstudio.features import format as swift_format  # lazy: only `projects format` needs it
    return swift_format


def _app_icon():
    from iosstudio.features import app_icon  # lazy: only `projects icon` / `pick-file` need it
    return app_icon


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
        "icon": lambda flags: _app_icon().cmd_icon(flags),          # iosstudio/features/app_icon.py
        "pick-file": lambda flags: _app_icon().cmd_pick_file(flags),
    },
    "build": {
        "run": _build_command("run"),
        "compile": _build_command("compile"),
        "ipa": _build_command("ipa"),
        "install": _build_command("install"),
        "history": cmd_history,
        "log": cmd_build_log,
        "clean": cmd_build_clean,
    },
}
