"""The Omarchy plugin (a bar button that opens iOS Studio or installs it): manifest.json, plugin/app.sh, and install.sh
run from the plugin folder that `omarchy plugin add` makes (~/.config/omarchy/plugins/<id>/, a clone of this repo).

Everything runs under a private HOME with a PATH of plain tools only: no hyprctl, no omarchy-* commands, no real qs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import support

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
PLUGIN_ID = "io.github.artmin96.ios-studio"
ORIGIN = "https://github.com/ArtMin96/omarchy-ios-studio"
TOOLS = ("bash", "sh", "env", "git", "readlink", "dirname", "basename", "mkdir", "ln", "chmod", "install", "cat", "mv",
         "rm", "mktemp", "cp", "timeout", "jq", "tr", "sed", "head", "find", "ls", "grep", "sort", "uname")
# The marketplace's limits for community listings (omarchy-plugin-marketplace scripts/build-catalog.mjs).
FIELD_LIMITS = {"id": 128, "name": 120, "version": 64, "author": 120, "description": 500, "license": 120}


@pytest.fixture
def home(tmp_path: Path) -> dict:
    """A private HOME, a fake Omarchy shell (Commons/ and Ui/ to link) and a PATH without hyprctl or omarchy-*."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in TOOLS:
        if found := shutil.which(tool):
            (bindir / tool).symlink_to(found)
    support.write_exe(bindir / "qs", "#!/bin/sh\nexit 0\n")
    omarchy = tmp_path / "omarchy"
    for kit in ("Commons", "Ui"):
        (omarchy / "shell" / kit).mkdir(parents=True)
    h = tmp_path / "home"
    h.mkdir()
    env = {"HOME": str(h), "PATH": str(bindir), "OMARCHY_PATH": str(omarchy), "LANG": "C.UTF-8",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return {"home": h, "env": env, "omarchy": omarchy, "app": h / ".local/share/omarchy-ios-studio",
            "launcher": h / ".local/bin/ios-studio"}


def plugin_folder(h: dict, *, git: bool = True) -> Path:
    """This checkout (tracked and new files, as committed) where `omarchy plugin add` would clone it."""
    folder = h["home"] / ".config/omarchy/plugins" / PLUGIN_ID
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
                           capture_output=True, text=True, check=True, timeout=60).stdout.split("\0")
    for rel in filter(None, files):
        src = ROOT / rel
        if src.is_symlink() or not src.is_file():
            continue
        dst = folder / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    if git:
        g = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@example.com", **h["env"]}
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "plugin"],
                     ["remote", "add", "origin", ORIGIN]):
            subprocess.run(["git", "-C", str(folder), *args], env=g, check=True, capture_output=True, timeout=60)
    return folder


def run(h: dict, *argv: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], env=h["env"], capture_output=True, text=True, timeout=120)


def links_in(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.is_symlink() and ".git" not in p.relative_to(folder).parts]


# --- manifest.json -------------------------------------------------------------------------------------------------

def test_manifest_matches_the_marketplace_schema() -> None:
    m = MANIFEST
    assert m["schemaVersion"] == 1 and m["id"] == PLUGIN_ID and m["name"] == "iOS Studio"
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*", m["id"]) and ".." not in m["id"] and not m["id"].startswith("omarchy.")
    for field, limit in FIELD_LIMITS.items():
        assert isinstance(m[field], str) and 0 < len(m[field]) <= limit, field
    assert m["license"] == "MIT" and (ROOT / "LICENSE").read_text().startswith("MIT License")
    assert m["kinds"] == ["bar-widget"] and list(m["entryPoints"]) == ["barWidget"]
    entry = m["entryPoints"]["barWidget"]
    assert not entry.startswith("/") and ".." not in entry and (ROOT / entry).is_file()
    assert m["barWidget"]["defaultSection"] in ("left", "center", "right")   # the marketplace allows only these


def test_preview_is_a_root_png_the_marketplace_takes() -> None:
    """The marketplace's one listing image: a root preview.png, at most 50 MB and 40 megapixels (build-catalog.mjs)."""
    preview = ROOT / "preview.png"
    assert preview.is_file() and not preview.is_symlink()
    data = preview.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    assert (width, height) == (1920, 1080) and len(data) <= 50 * 1024 * 1024
    tracked = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "preview.png"], timeout=60)
    assert tracked.returncode == 1   # not ignored


def test_manifest_version_is_the_app_version() -> None:
    app = re.search(r'readonly property string version: "([^"]+)"', (ROOT / "AppState.qml").read_text())
    assert app and MANIFEST["version"] == app.group(1)
    assert "manifest.json" in (ROOT / ".github/scripts/check-version.sh").read_text()


def test_no_symlinks_are_committed() -> None:
    staged = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s"], capture_output=True, text=True, check=True,
                            timeout=60).stdout
    assert not [ln for ln in staged.splitlines() if ln.startswith("120000")]


def test_widget_text_is_plain() -> None:
    qml = (ROOT / "plugin/BarWidget.qml").read_text()
    texts = qml.count("Text {")
    assert texts and qml.count("textFormat: Text.PlainText") == texts
    assert "hyprctl" not in qml and "bar.run(" not in qml   # it runs only plugin/app.sh, with fixed arguments


# --- plugin/app.sh -------------------------------------------------------------------------------------------------

def test_status_and_open_follow_the_launcher(home: dict) -> None:
    app = ROOT / "plugin/app.sh"
    assert run(home, app, "status").stdout.strip() == "missing"
    assert run(home, app, "open").returncode == 1
    calls = home["home"] / "calls"
    home["launcher"].parent.mkdir(parents=True)
    support.write_exe(home["launcher"], f'#!/bin/sh\necho "[$*]" >> {calls}\n')
    assert run(home, app, "status").stdout.strip() == "installed"
    assert run(home, app, "open").returncode == 0
    assert run(home, app, "open", "setup").returncode == 0
    bad = run(home, app, "open", "; rm -rf ~")
    assert bad.returncode == 2
    assert calls.read_text().splitlines() == ["[]", "[setup]"]
    assert run(home, app, "bogus").returncode == 2


def test_a_dangling_launcher_counts_as_missing(home: dict) -> None:
    home["launcher"].parent.mkdir(parents=True)
    home["launcher"].symlink_to(home["home"] / "gone/bin/ios-studio")
    assert run(home, ROOT / "plugin/app.sh", "status").stdout.strip() == "missing"


# --- install.sh from the plugin folder -----------------------------------------------------------------------------

def test_install_from_the_plugin_copies_the_app_out(home: dict) -> None:
    folder = plugin_folder(home)
    r = run(home, folder / "plugin/app.sh", "install")
    assert r.returncode == 0, r.stdout + r.stderr
    app = home["app"]
    assert (app / "install.sh").is_file() and (app / "manifest.json").is_file()
    origin = subprocess.run(["git", "-C", str(app), "remote", "get-url", "origin"], capture_output=True, text=True,
                            env=home["env"], timeout=30).stdout.strip()
    assert origin == ORIGIN   # a local clone of the plugin's commit, pointing at the real repo
    assert (app / "Commons").resolve() == (home["omarchy"] / "shell/Commons").resolve()
    assert home["launcher"].resolve() == (app / "bin/ios-studio").resolve()
    desktop = (home["home"] / ".local/share/applications/ios-studio.desktop").read_text()
    assert f"Exec={home['home']}/.local/bin/ios-studio" in desktop
    hook = (home["home"] / ".config/omarchy/hooks/theme-set.d/ios-studio-theme").read_text()
    assert str(app) in hook and str(folder) not in hook
    # The plugin folder stays a clean, valid plugin: no Commons/Ui links, nothing written.
    assert links_in(folder) == []
    status = subprocess.run(["git", "-C", str(folder), "status", "--porcelain"], capture_output=True, text=True,
                            env=home["env"], timeout=30).stdout
    assert status == ""
    assert run(home, folder / "plugin/app.sh", "status").stdout.strip() == "installed"
    validate = shutil.which("omarchy-plugin-validate")
    if validate:
        v = subprocess.run([validate, str(folder)], capture_output=True, text=True, timeout=60)
        assert v.returncode == 0, v.stderr


def test_install_again_keeps_the_existing_copy(home: dict) -> None:
    folder = plugin_folder(home)
    assert run(home, folder / "install.sh").returncode == 0
    marker = home["app"] / "kept"
    marker.write_text("mine")
    r = run(home, folder / "install.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert marker.read_text() == "mine" and "Copying" not in r.stdout


def test_uninstall_from_the_plugin_folder(home: dict) -> None:
    folder = plugin_folder(home)
    assert run(home, folder / "install.sh").returncode == 0
    r = run(home, folder / "install.sh", "--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not home["launcher"].exists() and not home["launcher"].is_symlink()
    assert run(home, folder / "plugin/app.sh", "status").stdout.strip() == "missing"
    assert (home["app"] / "install.sh").is_file()   # the app folder and the data stay, as from a git clone


def test_a_folder_in_the_way_is_reported(home: dict) -> None:
    folder = plugin_folder(home)
    home["app"].mkdir(parents=True)
    r = run(home, folder / "plugin/app.sh", "install")
    assert r.returncode == 1
    assert "isn't an iOS Studio folder" in r.stdout
    assert not home["launcher"].exists()


def test_install_from_a_plugin_folder_without_git(home: dict) -> None:
    folder = plugin_folder(home, git=False)
    r = run(home, folder / "install.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home["app"] / "bin/ios-studio").is_file() and links_in(folder) == []


def test_a_git_clone_still_installs_in_place(home: dict) -> None:
    clone = home["home"] / "src/omarchy-ios-studio"
    plugin = plugin_folder(home)
    shutil.copytree(plugin, clone, symlinks=True)
    shutil.rmtree(plugin.parent)
    r = run(home, clone / "install.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (clone / "Commons").is_symlink() and not home["app"].exists()
    assert home["launcher"].resolve() == (clone / "bin/ios-studio").resolve()
    assert os.access(home["launcher"], os.X_OK)
