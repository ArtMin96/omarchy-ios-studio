"""Project metadata and discovery: what a project folder is (Package.swift + xtool.yml), the scan, templates, new apps."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from iosstudio.core import appdirs, fileio, subproc, xtool_yml
from iosstudio.core.output import HelperError, fix
from iosstudio.features import agent_instructions, build_settings, run_presets
from iosstudio.features.projects import store

TEMPLATES = appdirs.REPO / "templates"
SKIP_DIRS = {"node_modules", ".build", ".git", "xtool", "DerivedData", "Pods", ".swiftpm", "build",
             "dist", "vendor", ".cache", "venv", ".venv", "__pycache__", "target", ".bsp", ".idea",
             ".vscode", "Carthage", ".gradle"}
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,48}$")
NOT_A_PROJECT = "That folder isn't an xtool app (it needs Package.swift and xtool.yml)."


# ================================================================ paths

def norm_path(path: str | Path) -> str:
    """~-expanded, absolute, symlink-free form of a path (the key used in every state file)."""
    return str(Path(os.path.expanduser(str(path))).resolve())


def require_path(flags: dict, key: str = "path") -> Path:
    """The existing folder named by --<key> (usage / not-found errors otherwise)."""
    raw = flags.get(key)
    if not raw or raw is True:
        raise HelperError("usage", f"Missing --{key}.")
    p = Path(norm_path(raw))
    if not p.is_dir():
        raise HelperError("not-found", "That project folder doesn't exist anymore.",
                          {**fix("Remove from list", "projects", action="remove"), "path": str(p)})
    return p


def require_project(flags: dict) -> Path:
    """require_path, and the folder must be an xtool app."""
    p = require_path(flags)
    if not is_project(p):
        raise HelperError("not-a-project", NOT_A_PROJECT)
    return p


def is_project(d: Path) -> bool:
    return (d / "Package.swift").is_file() and (d / "xtool.yml").is_file()


# ================================================================ metadata

def package_info(pkg: Path) -> dict:
    """Name, library products, iOS minimum and tools version read from a Package.swift (regex, no swift run)."""
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


def existing_artifact(path: Path, product: str, ext: str) -> str | None:
    """xtool/<product><ext> of a project if a build left one."""
    cand = path / "xtool" / f"{product}{ext}"
    return str(cand) if cand.exists() else None


def presets_info(meta: dict | None) -> dict:
    """runPresets (each with its resolved args/env) and runPreset (the selected name or None) for project info."""
    presets = run_presets.stored(meta)
    return {"runPresets": [run_presets.with_resolved(p) for p in presets],
            "runPreset": run_presets.selected(meta, presets)}


def launch_preset(p: Path, flags: dict) -> dict | None:
    """The --preset of a `build run`, resolved before anything builds (raises unknown-preset)."""
    name = flags.get("preset")
    if not isinstance(name, str) or not name.strip():
        return None
    preset = run_presets.find(run_presets.stored(store.load_state()["meta"].get(str(p))), name)
    return {"name": preset["name"], **run_presets.resolve(preset)}


def project_info(path: Path, history: list | None = None, renewals: dict | None = None,
                 state: dict | None = None) -> dict:
    """Everything the app shows about one project. history/renewals/state are read when not given."""
    yml = xtool_yml.read(path / "xtool.yml")
    pkg = package_info(path / "Package.swift")
    product = yml.get("product") or (pkg.get("libraries") or [None])[0] or pkg.get("package") or path.name
    bundle = yml.get("bundleID") or (f"{yml.get('orgID')}.{product}" if yml.get("orgID") else None)
    icon = yml.get("iconPath")
    icon_abs = str((path / icon).resolve()) if icon and (path / icon).is_file() else None
    sp = str(path)
    state = state if state is not None else store.load_state()
    renewals = renewals if renewals is not None else store.load_renewals()
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
        "generated": yml.generated,
        "added": sp in state["added"],
        "autoRenew": bool(state["meta"].get(sp, {}).get("autoRenew")),
        "buildSettings": build_settings.current(state["meta"].get(sp)),
        **presets_info(state["meta"].get(sp)),
        "lastBuild": store.last_build(sp, history),
        "signing": store.signing_status(renewals.get(sp)),
        "modified": mtime,
        "ipa": existing_artifact(path, product, ".ipa"),
    }
    if out["generated"] and not out["buildSettings"]["allowGenerated"]:
        out["note"] = ("This project's xtool.yml is generated by its own scripts, so iOS Studio doesn't build it. "
                       "To build it here, turn on \"Build generated project\" in its build settings and set the "
                       "script that regenerates xtool.yml as the prebuild command.")
    elif out["generated"] and not out["buildSettings"]["prebuild"]:
        out["note"] = ("This project's xtool.yml is generated, and no prebuild command regenerates it before a build, "
                       "so a build uses the file as it is now.")
    return out


# ================================================================ discovery

def roots() -> list[Path]:
    return appdirs.project_roots()


def _scan_depth() -> int:
    try:
        return max(1, min(8, int(appdirs.settings().get("projectScanDepth", 4))))
    except (TypeError, ValueError):
        return 4


def convertible(d: Path) -> dict | None:
    """A SwiftPM package that targets iOS with exactly one library could become an xtool app."""
    pkg = d / "Package.swift"
    if not pkg.is_file() or (d / "xtool.yml").exists():
        return None
    info = package_info(pkg)
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


def scan() -> tuple[list[Path], list[dict]]:
    """Projects (and convertible packages) under the project roots, never descending into a project."""
    found: list[Path] = []
    conv: list[dict] = []
    max_depth = _scan_depth()
    seen: set[str] = set()
    for root in roots():
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
            if is_project(d):
                found.append(d.resolve())
                continue  # never descend into a project
            c = convertible(d)
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


# ================================================================ templates and new apps

def templates() -> list[dict]:
    """The app templates in templates/ (template.json + Sources/), default first. `_dir` is internal."""
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


def template_dir(tmpl_id: Any) -> Path:
    """The folder of a listed template. Only ids from templates() are accepted, never a path."""
    listed = templates()
    for meta in listed:
        if meta.get("id") == tmpl_id:
            return meta["_dir"]
    ids = ", ".join(str(m.get("id")) for m in listed)
    raise HelperError("bad-template", f"That template isn't available. Choose one of: {ids}.")


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


def create_app(flags: dict) -> tuple[Path, str, bool, list[str]]:
    """`projects new`: a new app from a template (xtool new, else a built-in skeleton), with agent instructions
    (when wanted), git-initialised and added to the list. Returns (folder, template id, whether xtool made the
    skeleton, the agent instruction files written)."""
    name = str(flags.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise HelperError("bad-name", "App names start with a letter and use only letters, numbers and _ (no spaces).")
    tmpl_id = flags.get("template") or "swiftui-app"
    tmpl = template_dir(tmpl_id)
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
    yml.write_text(xtool_yml.set_key(yml.read_text() if yml.exists() else "version: 1\n", "bundleID", bundle))
    # before the first commit, so they're in it
    agent_files = agent_instructions.write(dest, name, bundle) if agent_instructions.wanted(flags) else []

    git = shutil.which("git")
    if git and not (dest / ".git").exists() and flags.get("git", True) not in ("0", "false", "no"):
        if subproc.run_cmd([git, "init", "-q"], cwd=str(dest), timeout=20).returncode == 0:
            subproc.run_cmd([git, "add", "-A"], cwd=str(dest), timeout=20)
            subproc.run_cmd([git, "commit", "-q", "-m", f"New app {name} (iOS Studio template {tmpl_id})"],
                            cwd=str(dest), timeout=20)

    dest = dest.resolve()
    with store.edit_state() as state:
        if str(dest) not in state["added"]:
            state["added"].append(str(dest))
        # marks it as made by iOS Studio: editor files (.vscode/) are only written by themselves into these
        state["meta"].setdefault(str(dest), {})["createdFrom"] = tmpl_id
    write_editor_files(dest)
    return dest, tmpl_id, used_xtool, agent_files


def write_editor_files(p: Path) -> None:
    """Editor support files a project needs, written when missing, never overwriting (editor_setup.auto_project):
    .bsp/xtool.json for every project; .vscode/ only for projects created from a template here (meta
    createdFrom) when VS Code is the preferred editor. Best effort: never fails the caller."""
    try:
        from iosstudio.features import editor_setup
        editor_setup.auto_project(p)
    except Exception:  # noqa: BLE001
        pass
