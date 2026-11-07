"""Per project: the build server file (.bsp/xtool.json) SourceKit-LSP needs, VS Code's project files, the
automatic part after a build / create / add / convert, the project's status and --fix."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from iosstudio.core import fileio, xtool_yml
from iosstudio.core.output import HelperError

from .actions import apply_actions, apply_or_fail, is_pending, public, read_json_file
from .editors import vscode_project_actions
from .env import EDITOR_USAGE, EDITORS, Env, ios_sdk, preferred

# Exactly what xtool writes (Sources/XToolSupport/LSPConfig.swift): the BSP file for Swift 6.4+, the older
# SourceKit-LSP config before that. xtool only writes the one that's missing, so matching it keeps a later
# `xtool dev build` quiet.
BSP_REL = ".bsp/xtool.json"
BSP_TEXT = """{
    "name": "xtool",
    "version": "1.0",
    "bspVersion": "2.2.0",
    "languages": ["c", "cpp", "objective-c", "objective-cpp", "swift"],
    "argv": ["/usr/bin/env", "xtool", "dev", "build-server"]
}
"""
LEGACY_REL = ".sourcekit-lsp/config.json"
LEGACY_TEXT = """{
    "swiftPM": {
        "swiftSDK": "arm64-apple-ios"
    }
}
"""
BSP_DETAIL = "Lets the Swift language server build this project for the iPhone (xtool writes the same file)."
BSP_MIN_SWIFT = (6, 4)   # `xtool dev build-server` needs Swift 6.4 (swift-build)
SDK_REASON = "xtool's iOS SDK isn't set up: finish it on the Toolchain page"
NOT_BUILT_NOTE = "Build once so the Swift packages are fetched; until then completion for them is missing."
AUTO_LOG_MAX = 200       # projects remembered in editor-auto.json


# ================================================================ the build server file (.bsp)

def bsp_status(project: Path, env: Env) -> dict:
    """Is the file SourceKit-LSP needs there, and does it still work? kind: 'bsp' (.bsp/xtool.json, Swift
    6.4+) or 'legacy' (.sourcekit-lsp/config.json, older Swift)."""
    ver = env.swift()
    kind = "legacy" if ver is not None and ver < BSP_MIN_SWIFT else "bsp"
    rel = BSP_REL if kind == "bsp" else LEGACY_REL
    path = project / rel
    xtool = env.which("xtool")
    out: dict[str, Any] = {"present": path.is_file(), "path": str(path), "kind": kind, "stale": False,
                           "reason": None, "built": (project / ".build").is_dir(), "xtoolOnPath": bool(xtool),
                           "skipLSP": xtool_yml.read(project / "xtool.yml").flag("skipLSP"), "note": None}
    if out["present"]:
        text, problem = read_json_file(path)
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            data = None
        if kind == "bsp":
            argv = data.get("argv") if isinstance(data, dict) else None
            if problem or not isinstance(argv, list) or not argv:
                out.update(stale=True, reason=f"{rel} isn't a build server file iOS Studio can read")
            elif "build-server" not in [str(a) for a in argv]:
                out.update(stale=True, reason=f"{rel} starts {' '.join(map(str, argv))[:80]}, not xtool's "
                                              "build server")
            elif "xtool" in [str(a) for a in argv] and not xtool:
                if (env.home / ".local/bin/xtool").is_file():
                    out["note"] = ("xtool is in ~/.local/bin, which isn't on this PATH: start your editor from a "
                                   "shell that has it, or it can't start xtool's build server.")
                else:
                    out.update(stale=True, reason="xtool isn't on PATH, so the editor can't start xtool's build "
                                                  "server")
        elif problem or not isinstance(data, dict):
            out.update(stale=True, reason=f"{rel} isn't valid JSON")
        if kind == "bsp" and not out["stale"] and not ios_sdk(env):
            # without it `xtool dev build-server` exits at once and editors only say "Failed to initialize
            # build server: JSON-RPC connection closed"
            out.update(stale=True, sdkMissing=True, reason=SDK_REASON)
    if kind == "legacy" and (project / BSP_REL).is_file():
        out["note"] = (f"Swift {ver[0]}.{ver[1]} is older than 6.4, so xtool's build server can't run; "  # type: ignore[index]
                       f"SourceKit-LSP uses {LEGACY_REL} instead.")
    elif not out["built"] and not out["note"]:
        out["note"] = NOT_BUILT_NOTE
    return out


def bsp_action(project: Path, env: Env) -> dict:
    st = bsp_status(project, env)
    act = {"path": st["path"], "scope": "project"}
    if st["skipLSP"] and not st["present"]:
        return {**act, "kind": "skip", "detail": "Turned off by skipLSP: true in xtool.yml. Remove that line to "
                "get completion."}
    if st["present"] and st.get("sdkMissing"):
        return {**act, "kind": "skip", "detail": f"Not usable yet: {SDK_REASON}."}
    if st["present"] and st["stale"]:
        return {**act, "kind": "skip", "detail": f"Stale: {st['reason']}. It's left as it is; delete it "
                "and build again."}
    if st["present"]:
        return {**act, "kind": "skip", "detail": "Already there.", "configured": True}
    text = BSP_TEXT if st["kind"] == "bsp" else LEGACY_TEXT
    return {**act, "kind": "write", "detail": BSP_DETAIL, "preview": text, "_text": text}


# ================================================================ per project: automatic, status, --fix

def created_here(project: Path, env: Env) -> bool:
    """True for a project iOS Studio created from a template (projects.json meta createdFrom). Only these get
    editor files (.vscode/) written by themselves; added or converted projects get them through --fix."""
    data = fileio.load_json(env.state / "projects.json", {})
    meta = (data.get("meta") or {}).get(str(project)) if isinstance(data, dict) else None
    return isinstance(meta, dict) and bool(meta.get("createdFrom"))


def project_actions(project: Path, env: Env, editors: list[str | None] | None = None) -> list[dict]:
    """What a project needs: the build server file, plus VS Code's project files when VS Code is the
    preferred editor (or asked for). Zed and Neovim need nothing in the project."""
    editors = [preferred(env)] if editors is None else editors
    acts = [bsp_action(project, env)]
    if "vscode" in editors:
        acts += vscode_project_actions(project)
    return acts


def _auto_file(env: Env) -> Path:
    return env.state / "editor-auto.json"


def auto_project(project: Path, env: Env | None = None) -> dict:
    """The automatic part (after a successful build, on create / add / convert): write what's missing,
    quietly, never overwriting. A file it can't merge is left alone. Remembers what it wrote per project.
    .bsp for every project, .vscode/ only for projects created here."""
    env = env or Env.from_os()
    acts = project_actions(project, env)
    if not created_here(project, env):
        acts = acts[:1]
    acts = [a for a in acts if is_pending(a)]
    if not acts:
        return {"wrote": []}
    apply_actions(acts, env)
    wrote = [{"kind": a["kind"], "path": a["path"]} for a in acts if a.get("done")]
    if wrote:
        log = fileio.load_json(_auto_file(env), {})
        log = log if isinstance(log, dict) else {}
        log[str(project)] = {"at": time.time(), "wrote": wrote}
        if len(log) > AUTO_LOG_MAX:   # keep the newest
            log = dict(sorted(log.items(), key=lambda kv: kv[1].get("at", 0) if isinstance(kv[1], dict) else 0)
                       [-AUTO_LOG_MAX:])
        fileio.save_json(_auto_file(env), log)
    return {"wrote": wrote}


def project_status(project: Path, env: Env | None = None) -> dict:
    """{project, bsp: {present, stale, reason, path, kind}, ready, hint, preferred, auto, pending}.
    ready: an editor gets completion and errors here once the machine-wide part is done."""
    env = env or Env.from_os()
    b = bsp_status(project, env)
    pref = preferred(env)
    acts = project_actions(project, env)
    pending = [public(a) for a in acts[1:] if is_pending(a)]
    created = created_here(project, env)
    hint = None
    if not b["present"]:
        hint = ("xtool.yml has skipLSP: true, so editors get no completion" if b["skipLSP"]
                else "Build once to enable completion")
    elif b["stale"]:
        hint = b["reason"]
    elif pending and created:
        hint = "VS Code's Swift settings for this project are added on the next build"
    elif pending:
        hint = "VS Code settings for this project: set up from Settings › Code editor"
    auto = fileio.load_json(_auto_file(env), {})
    auto = auto.get(str(project)) if isinstance(auto, dict) else None
    return {"project": str(project),
            "bsp": {k: b[k] for k in ("present", "stale", "reason", "path", "kind", "built", "note")},
            "ready": b["present"] and not b["stale"] and not pending, "hint": hint, "preferred": pref,
            "created": created,
            "auto": auto if isinstance(auto, dict) else None, "pending": pending}


def brief_of(status: dict) -> dict:
    """The `editor` field of `projects info` from a project status (real or demo): without the pending list."""
    return {"bsp": {k: status["bsp"][k] for k in ("present", "stale", "reason")}, "ready": status["ready"],
            "hint": status["hint"], "preferred": status["preferred"], "auto": status["auto"],
            "created": status["created"]}


def brief(project: Path, env: Env | None = None) -> dict:
    """The `editor` field of `projects info`: project_status without the pending list."""
    return brief_of(project_status(project, env))


def project_fix(project: Path, confirm: bool = False, env: Env | None = None, editor: str | None = None) -> dict:
    """--fix: write the project's missing files now (dry run unless confirm)."""
    env = env or Env.from_os()
    if editor and editor not in EDITORS:
        raise HelperError("usage", EDITOR_USAGE)
    acts = project_actions(project, env, [editor] if editor else None)
    if confirm:
        apply_or_fail(acts, env)
    pending = any(is_pending(a) for a in acts)
    nxt = (None if not confirm and pending else "Nothing left to write for this project." if not pending
           else "Reopen the folder in your editor if it was open.")
    return {"dryRun": not confirm, "actions": [public(a) for a in acts],
            "applied": any(a.get("done") for a in acts), "next": nxt}
