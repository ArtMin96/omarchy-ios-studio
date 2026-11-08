"""Machine-wide, once per editor: VS Code, Zed and Neovim detection, the user-level config each needs for
Swift (sourcekit-lsp's path, LazyVim's Swift extra or a plugin file), the remembered editor, status and setup."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, jsonc
from iosstudio.core.jsonc import JsoncError
from iosstudio.core.output import HelperError

from .actions import apply_or_fail, backup_action, is_pending, json_action, public, read_json_file, user_json_actions
from .env import (
    EDITOR_USAGE,
    EDITORS,
    SETTING_KEY,
    Env,
    flatpak_installed,
    load_settings,
    preferred,
    sourcekit,
    sourcekit_for_settings,
    sourcekit_next_to_swift,
)

VSCODE_EXT = "swiftlang.swift-vscode"
VSCODE_OLD_EXT = "sswg.swift-lang"
VSCODE_SETTINGS = ".vscode/settings.json"
VSCODE_EXTENSIONS = ".vscode/extensions.json"
LAZY_EXTRA = "lazyvim.plugins.extras.lang.swift"
NVIM_PLUGIN_REL = "lua/plugins/ios-studio-swift.lua"
DOCS = "docs/features/editor-setup.md"
NVIM_PLUGIN_DETAIL = "Turns on Swift completion (sourcekit-lsp) for every project. Delete the file to undo."
SOURCEKIT_PATH_DETAIL = "Tells the editor where sourcekit-lsp is (it isn't on PATH)."

NVIM_SCAN_FILES = 300        # lua files read when looking for a sourcekit setup
NVIM_SCAN_BYTES = 256 * 1024


# ================================================================ VS Code

def vscode_installed(env: Env) -> str | None:
    for b in ("code", "code-oss", "codium", "vscodium", "code-insiders"):
        p = env.which(b)
        if p:
            return p
    for app in ("com.visualstudio.code", "com.vscodium.codium"):
        if flatpak_installed(env, app):
            return "flatpak:" + app
    return None


def vscode_extension(env: Env) -> str | None:
    for d in (env.home / ".vscode/extensions", env.home / ".vscode-oss/extensions",
              env.home / ".var/app/com.visualstudio.code/data/vscode/extensions",
              env.home / ".var/app/com.vscodium.codium/data/codium/extensions"):
        for prefix in (VSCODE_EXT, VSCODE_OLD_EXT):
            try:
                hit = next(iter(sorted(d.glob(prefix + "-*"))), None)
            except OSError:
                hit = None
            if hit:
                return str(hit)
    return None


def vscode_user_settings(env: Env) -> Path:
    """VS Code's own (user) settings.json for the installed flavour."""
    b = Path(vscode_installed(env) or "code").name
    folder = {"code-oss": "Code - OSS", "codium": "VSCodium", "vscodium": "VSCodium",
              "code-insiders": "Code - Insiders"}.get(b, "Code")
    return env.config / folder / "User/settings.json"


# Project settings: portable (no machine paths), so .vscode/ can be committed.
VSCODE_PROJECT_SETTINGS = {
    # Host executables don't exist for an iPhone app; a debug launch.json is a separate step (lldb-dap)
    "swift.autoGenerateLaunchConfigurations": False,
    # .build holds tens of thousands of files that change on every build
    "files.watcherExclude": {"**/.build/**": True},
}
VSCODE_PROJECT_EXTENSIONS = {"recommendations": [VSCODE_EXT]}


def vscode_project_actions(project: Path) -> list[dict]:
    acts = [json_action(project, VSCODE_SETTINGS, VSCODE_PROJECT_SETTINGS,
                        what="Swift settings for this project (no machine paths, safe to commit).")]
    text, _ = read_json_file(project / VSCODE_EXTENSIONS)
    unwanted: Any = []
    if text:
        try:
            v = jsonc.parse(text).value
            unwanted = (v.get("unwantedRecommendations") or []) if isinstance(v, dict) else []
        except JsoncError:
            pass
    if isinstance(unwanted, list) and VSCODE_EXT in unwanted:
        acts.append({"path": str(project / VSCODE_EXTENSIONS), "scope": "project", "kind": "skip",
                     "detail": "The Swift extension is on this project's unwanted list; that's kept.",
                     "kept": [{"key": "unwantedRecommendations", "yours": unwanted, "ours": None}],
                     "configured": True})
    else:
        acts.append(json_action(project, VSCODE_EXTENSIONS, VSCODE_PROJECT_EXTENSIONS,
                                what="VS Code offers the Swift extension when the folder opens."))
    return acts


def vscode_machine_actions(env: Env) -> list[dict]:
    sk = sourcekit_for_settings(env)
    if not sk:
        return []
    return user_json_actions(vscode_user_settings(env), {"swift.sourcekit-lsp.serverPath": sk},
                             SOURCEKIT_PATH_DETAIL)


# ================================================================ Zed
# Zed needs no project file: its Swift extension starts the sourcekit-lsp it finds on PATH (else
# lsp.sourcekit-lsp.binary.path), and sourcekit-lsp finds .bsp/xtool.json in the opened folder itself.

def zed_installed(env: Env) -> str | None:
    for b in ("zeditor", "zed", "zedit", "zed-editor"):
        p = env.which(b)
        if p:
            return p
    return "flatpak:dev.zed.Zed" if flatpak_installed(env, "dev.zed.Zed") else None


def zed_extension(env: Env) -> str | None:
    for d in (env.data / "zed/extensions/installed/swift",
              env.home / ".var/app/dev.zed.Zed/data/zed/extensions/installed/swift"):
        if d.is_dir():
            return str(d)
    return None


def zed_machine_actions(env: Env) -> list[dict]:
    sk = sourcekit_for_settings(env)
    if not sk:
        return []
    return user_json_actions(env.config / "zed/settings.json", {"lsp": {"sourcekit-lsp": {"binary": {"path": sk}}}},
                             SOURCEKIT_PATH_DETAIL)


# ================================================================ Neovim

def nvim_dirs(env: Env) -> tuple[Path, Path]:
    """(config dir, LazyVim checkout) honouring NVIM_APPNAME."""
    app = os.environ.get("NVIM_APPNAME") or "nvim"
    return env.config / app, env.data / app / "lazy/LazyVim"


def _nvim_config_files(cfg: Path) -> list[Path]:
    """init.lua/init.vim and the .lua/.vim files under the usual folders, at most NVIM_SCAN_FILES."""
    files: list[Path] = []
    for name in ("init.lua", "init.vim"):
        if (cfg / name).is_file():
            files.append(cfg / name)
    for sub in ("lua", "plugin", "after", "lsp"):
        d = cfg / sub
        if not d.is_dir():
            continue
        for root, dirs, names in os.walk(d):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for n in sorted(names):
                if n.endswith((".lua", ".vim")):
                    files.append(Path(root) / n)
                    if len(files) >= NVIM_SCAN_FILES:
                        return files
    return files


_SOURCEKIT_RX = re.compile(r"sourcekit|lang\.swift")


def _nvim_file_with_swift(cfg: Path) -> str | None:
    """The first config file that sets up sourcekit (outside comments), or None."""
    for f in _nvim_config_files(cfg):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read(NVIM_SCAN_BYTES)
        except OSError:
            continue
        for line in text.splitlines():
            code = line.split("--", 1)[0] if f.suffix == ".lua" else line.split('"', 1)[0]
            if _SOURCEKIT_RX.search(code):
                return str(f)
    return None


def nvim_status(env: Env) -> dict:
    cfg, lazy = nvim_dirs(env)
    lazyvim_json = cfg / "lazyvim.json"
    is_lazy = lazyvim_json.is_file() or (lazy / "lua/lazyvim").is_dir()
    extra_file = lazy / "lua/lazyvim/plugins/extras/lang/swift.lua"
    extras: list = []
    lj_problem = None
    if lazyvim_json.is_file():
        text, lj_problem = read_json_file(lazyvim_json)
        if text is not None:
            try:
                data = json.loads(text)
                extras = data.get("extras") if isinstance(data, dict) and isinstance(data.get("extras"), list) else []
            except json.JSONDecodeError:
                lj_problem = "isn't valid JSON"
    found = str(lazyvim_json) if LAZY_EXTRA in extras else _nvim_file_with_swift(cfg)
    return {"config": str(cfg), "configExists": cfg.is_dir(), "lazyvim": is_lazy,
            "lazyvimJson": str(lazyvim_json) if lazyvim_json.is_file() else None, "lazyvimJsonProblem": lj_problem,
            "extraAvailable": extra_file.is_file(), "extraEnabled": LAZY_EXTRA in extras, "swiftLsp": found,
            "pluginFile": str(cfg / NVIM_PLUGIN_REL)}


def nvim_plugin_text(env: Env) -> str:
    sk = sourcekit_for_settings(env)
    cmd = f"\n          cmd = {{ {json.dumps(sk)} }}," if sk else ""
    return f"""-- Added by iOS Studio (editor setup): Swift completion and errors through sourcekit-lsp.
-- sourcekit-lsp finds an xtool project through its .bsp/xtool.json (xtool's build server), so it
-- compiles for the iPhone. Delete this file to undo. See {DOCS} in iOS Studio.
return {{
  {{
    "nvim-treesitter/nvim-treesitter",
    opts = {{ ensure_installed = {{ "swift" }} }},
  }},
  {{
    "neovim/nvim-lspconfig",
    opts = {{
      servers = {{
        sourcekit = {{
          mason = false, -- comes with Swift, not with Mason{cmd}
        }},
      }},
    }},
  }},
}}
"""


def neovim_actions(env: Env) -> list[dict]:
    st = nvim_status(env)
    cfg = Path(st["config"])
    if st["swiftLsp"]:
        return [{"path": st["swiftLsp"], "scope": "user", "kind": "skip", "configured": True,
                 "detail": "Swift is already on in your Neovim config."}]
    if not st["lazyvim"]:
        where = str(cfg / "init.lua") if st["configExists"] else str(cfg)
        return [{"path": where, "scope": "user", "kind": "skip",
                 "detail": "Not a LazyVim config, so it's left alone. Add the snippet from the docs to it."}]
    if st["extraAvailable"] and st["lazyvimJson"]:
        lj = cfg / "lazyvim.json"
        if st["lazyvimJsonProblem"]:
            return [{"path": str(lj), "scope": "user", "kind": "skip",
                     "detail": f"The file {st['lazyvimJsonProblem']}, so it's left alone. Turn on Swift with "
                               ":LazyExtras instead."}]
        data = json.loads(lj.read_text(encoding="utf-8"))
        data["extras"] = sorted(set(data.get("extras") or []) | {LAZY_EXTRA})
        return [backup_action(lj),
                {"path": str(lj), "scope": "user", "kind": "merge",
                 "detail": "Turns on LazyVim's Swift extra for every project. Undo with :LazyExtras.",
                 "keys": [f"extras + {LAZY_EXTRA}"], "_text": json.dumps(data, indent=2) + "\n"}]
    plugin = cfg / NVIM_PLUGIN_REL
    if plugin.exists():
        return [{"path": str(plugin), "scope": "user", "kind": "skip",
                 "detail": "A file with this name is already there; it's left as it is."}]
    body = nvim_plugin_text(env)
    return [{"path": str(plugin), "scope": "user", "kind": "write",
             "detail": NVIM_PLUGIN_DETAIL, "preview": body, "_text": body}]


# ================================================================ machine-wide status and setup

def _preference_action(editor: str, env: Env) -> dict:
    path = env.settings_file
    if load_settings(env).get(SETTING_KEY) == editor:
        return {"path": str(path), "scope": "user", "kind": "skip", "configured": True,
                "detail": f"{EDITORS[editor]} is already your editor."}
    extra = " Projects get its settings files when they're built." if editor == "vscode" else ""
    return {"path": str(path), "scope": "user", "kind": "merge" if path.is_file() else "write",
            "detail": f"Remember {EDITORS[editor]} as your editor.{extra}", "keys": [SETTING_KEY],
            "_setting": editor}


def machine_actions(editor: str, env: Env) -> list[dict]:
    if editor == "vscode":
        return vscode_machine_actions(env)
    if editor == "zed":
        return zed_machine_actions(env)
    if editor == "neovim":
        return neovim_actions(env)
    raise HelperError("usage", EDITOR_USAGE)


def _pending_note(pending: list[dict], env: Env) -> str:
    return "Set up changes " + ", ".join(appdirs.tilde(a["path"], env.home) for a in pending
                                         if a["kind"] != "backup") + "."


def editor_info(editor: str, env: Env, acts: list[dict] | None = None) -> dict:
    """One editor's machine-wide state. state: ready | needs-setup (setup can do the rest) | partial (a step
    only you can do remains; `notes` says which) | not-installed."""
    acts = machine_actions(editor, env) if acts is None else acts
    pending = [a for a in acts if a["kind"] in ("write", "merge", "backup")]
    blocked = [a for a in acts if a["kind"] == "skip" and not a.get("configured")]
    notes: list[str] = []
    ext = None
    if editor == "vscode":
        installed = vscode_installed(env)
        ext = vscode_extension(env)
        if not ext:
            notes.append(f"Install the Swift extension ({VSCODE_EXT}): VS Code offers it when you open a project "
                         "set up for it, or search Swift in Extensions (ctrl-shift-x).")
    elif editor == "zed":
        installed = zed_installed(env)
        ext = zed_extension(env)
        if not ext:
            notes.append("Install Zed's Swift extension: open Extensions (ctrl-shift-x), search for Swift, click "
                         "Install.")
    else:
        installed = env.which("nvim")
    if not sourcekit(env) and not sourcekit_next_to_swift(env):
        notes.append("sourcekit-lsp isn't installed: install Swift on iOS Studio's Setup page.")
    notes += [a["detail"] for a in blocked]
    if not installed:
        state, notes = "not-installed", []
    elif pending:
        state = "needs-setup"
    elif notes:
        state = "partial"
    else:
        state = "ready"
    files: list[str] = []
    extra: dict[str, Any] = {}
    if editor == "neovim":
        st = nvim_status(env)
        files = [st["swiftLsp"]] if st["swiftLsp"] else []
        extra["lazyvim"] = bool(st["lazyvim"])
    return {"id": editor, "name": EDITORS[editor], "installed": bool(installed), "binary": installed or None,
            "state": state, "extension": ext, "configured": not pending and not blocked, "files": files,
            "note": notes[0] if notes else (_pending_note(pending, env) if pending else None), "notes": notes,
            **extra}


def machine_status(env: Env | None = None) -> dict:
    env = env or Env.from_os()
    ver = env.swift()
    return {"swift": f"{ver[0]}.{ver[1]}" if ver else None, "xtool": env.which("xtool"),
            "sourcekit_lsp": sourcekit(env) or sourcekit_next_to_swift(env), "preferred": preferred(env),
            "editors": [editor_info(ed, env) for ed in EDITORS]}


def next_step(editor: str) -> str:
    """What to do once an editor is set up."""
    if editor == "neovim":
        return "Restart Neovim; Swift files in your projects then get completion and errors."
    return f"Open a project in {EDITORS[editor]}; completion starts once sourcekit-lsp has indexed it."


def machine_setup(editor: str, confirm: bool = False, env: Env | None = None, prefer: bool = True) -> dict:
    """prefer=False leaves the remembered editor as it is (setting up several editors at once)."""
    env = env or Env.from_os()
    if editor not in EDITORS:
        raise HelperError("usage", EDITOR_USAGE)
    acts = machine_actions(editor, env) + ([_preference_action(editor, env)] if prefer else [])
    if confirm:
        apply_or_fail(acts, env)
    info = editor_info(editor, env, None if confirm else [a for a in acts if "_setting" not in a])
    pending = any(is_pending(a) for a in acts)
    if not confirm and pending:
        nxt = None
    elif info["notes"]:
        nxt = info["notes"][0]
    else:
        nxt = next_step(editor)
    return {"editor": editor, "name": EDITORS[editor], "dryRun": not confirm,
            "actions": [public(a) for a in acts], "applied": any(a.get("done") for a in acts),
            "state": info["state"], "next": nxt, "notes": info["notes"]}
