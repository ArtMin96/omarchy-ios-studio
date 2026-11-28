"""install.sh safety: it only hands over to a real iOS Studio checkout, stages the theme hook in a private
temporary folder that is always removed, names every folder it keeps on uninstall, and the plugin's install prompt
lists every change install.sh makes. Runs install.sh under a private HOME with fake tools (see test_omarchy_plugin).
"""
from __future__ import annotations

import json
import re
import shutil
import stat
from pathlib import Path

import test_omarchy_plugin as plug

import support

ROOT = plug.ROOT
home = plug.home   # the private-HOME fixture
plugin_folder, run = plug.plugin_folder, plug.run


def foreign_folder(h: dict, manifest: dict | None) -> Path:
    """A folder at the app path with an install.sh that would leave a mark if it ran."""
    app = h["app"]
    app.mkdir(parents=True)
    support.write_exe(app / "install.sh", f"#!/bin/sh\ntouch {h['home'] / 'ran'}\n")
    if manifest is not None:
        (app / "manifest.json").write_text(json.dumps(manifest))
    return app


def git_clone_of_this_repo(h: dict) -> Path:
    clone = h["home"] / "src/omarchy-ios-studio"
    plugin = plugin_folder(h)
    shutil.copytree(plugin, clone, symlinks=True)
    shutil.rmtree(plugin.parent)
    return clone


def test_a_folder_without_a_manifest_is_never_run(home: dict) -> None:
    folder = plugin_folder(home)
    foreign_folder(home, None)
    r = run(home, folder / "plugin/app.sh", "install")
    assert r.returncode == 1 and "isn't an iOS Studio folder" in r.stdout + r.stderr
    assert not (home["home"] / "ran").exists() and not home["launcher"].exists()


def test_a_folder_with_another_plugin_id_is_never_run(home: dict) -> None:
    folder = plugin_folder(home)
    foreign_folder(home, {"schemaVersion": 1, "id": "io.github.someone.ios-studio-evil", "name": "iOS Studio"})
    r = run(home, folder / "install.sh")
    assert r.returncode == 1 and "isn't an iOS Studio folder" in r.stderr
    assert not (home["home"] / "ran").exists()


def test_uninstall_ignores_a_foreign_folder_and_removes_the_links(home: dict) -> None:
    folder = plugin_folder(home)
    assert run(home, folder / "install.sh").returncode == 0
    real = home["app"]
    real.rename(real.with_name("moved-aside"))
    foreign_folder(home, {"id": "something.else"})
    home["launcher"].unlink()
    home["launcher"].symlink_to(folder / "bin/ios-studio")
    r = run(home, folder / "install.sh", "--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (home["home"] / "ran").exists()
    assert not home["launcher"].exists() and not home["launcher"].is_symlink()


def test_a_checkout_with_the_same_id_is_used(home: dict) -> None:
    folder = plugin_folder(home)
    assert run(home, folder / "install.sh").returncode == 0
    r = run(home, folder / "install.sh")
    assert r.returncode == 0 and "Copying" not in r.stdout
    assert home["launcher"].resolve() == (home["app"] / "bin/ios-studio").resolve()


def hook_installer(h: dict, *, fail: bool = False) -> Path:
    """A fake omarchy-hook-install that records the path it was given and its folder's mode."""
    log = h["home"] / "hook-calls"
    body = (f'#!/bin/sh\necho "$2" >> {log}\n/usr/bin/stat -c %a "$(/usr/bin/dirname "$2")" >> {log}\n'
            + ("exit 3\n" if fail else
               'd="$HOME/.config/omarchy/hooks/$1.d"; mkdir -p "$d"; cp "$2" "$d/ios-studio-theme"\n'))
    support.write_exe(Path(h["env"]["PATH"]) / "omarchy-hook-install", body)
    return log


def private_tmp(h: dict) -> Path:
    tmp = h["home"].parent / "tmpdir"
    tmp.mkdir()
    h["env"]["TMPDIR"] = str(tmp)
    return tmp


def test_theme_hook_is_staged_in_a_private_folder_that_is_removed(home: dict) -> None:
    tmp = private_tmp(home)
    log = hook_installer(home)
    clone = git_clone_of_this_repo(home)
    r = run(home, clone / "install.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    staged, mode = log.read_text().split()
    assert Path(staged).name == "ios-studio-theme"
    assert Path(staged).parent.parent == tmp   # its own mktemp -d folder, not a fixed name in /tmp
    assert mode == "700"
    assert list(tmp.iterdir()) == []
    hook = home["home"] / ".config/omarchy/hooks/theme-set.d/ios-studio-theme"
    assert str(clone) in hook.read_text()
    assert "/tmp/ios-studio-theme" not in (ROOT / "install.sh").read_text()


def test_a_failed_hook_install_still_cleans_up(home: dict) -> None:
    tmp = private_tmp(home)
    hook_installer(home, fail=True)
    r = run(home, git_clone_of_this_repo(home) / "install.sh")
    assert r.returncode != 0
    assert list(tmp.iterdir()) == []
    assert not list((home["home"] / ".local/share/applications").glob("*.tmp"))


def test_uninstall_names_every_folder_it_keeps(home: dict) -> None:
    folder = plugin_folder(home)
    assert run(home, folder / "install.sh").returncode == 0
    out = run(home, folder / "install.sh", "--uninstall").stdout
    for kept in ("~/.config/ios-studio", "~/.local/state/ios-studio", "~/.local/share/ios-studio", "~/.cache/ios-studio"):
        assert kept in out
    readme = (ROOT / "README.md").read_text()
    assert "~/.cache/ios-studio" in readme.split("## Uninstall", 1)[1]


def test_the_hyprland_rule_is_written_marked_and_validated(home: dict) -> None:
    """With hyprctl present, install.sh adds the marked opacity block and reloads Hyprland, as the prompt says."""
    calls = home["home"] / "hyprctl-calls"
    support.write_exe(Path(home["env"]["PATH"]) / "hyprctl", f'#!/bin/sh\necho "$*" >> {calls}\n')   # no config errors
    hypr = home["home"] / ".config/hypr/hyprland.lua"
    hypr.parent.mkdir(parents=True)
    hypr.write_text("-- mine\n")
    r = run(home, git_clone_of_this_repo(home) / "install.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    text = hypr.read_text()
    assert text.startswith("-- mine\n")
    assert "-- ios-studio: opacity rule start" in text and "-- ios-studio: opacity rule end" in text
    assert "reload" in calls.read_text() and "configerrors" in calls.read_text()
    assert list((home["home"] / ".local/state/ios-studio/backups").glob("hyprland.lua.*"))


# --- consent: the prompt lists what install.sh changes ------------------------------------------------------------

def test_the_install_prompt_lists_every_change() -> None:
    ask = json.loads((ROOT / "plugin/i18n/en.json").read_text())["askDetail"]
    for change in ("~/.local/share/omarchy-ios-studio", "launcher", "~/.local/bin/ios-studio", "theme hook",
                   "~/.config/hypr/hyprland.lua", "reloads Hyprland", "timers"):
        assert change in ask, change
    installer = (ROOT / "install.sh").read_text()
    # What the prompt names is what install.sh touches, and nothing system-wide.
    for target in ("$HOME/.local/bin", "$HOME/.local/share/applications", "theme-set.d", "app opacity --opaque on",
                   "$HOME/.local/share/omarchy-ios-studio"):
        assert target in installer, target
    code = "\n".join(ln for ln in installer.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"\b(sudo|pkexec|pacman|systemctl)\b", code)
    for doc in ("README.md", "docs/features/omarchy-plugin.md"):
        text = (ROOT / doc).read_text()
        assert "~/.config/hypr/hyprland.lua" in text and "~/.local/bin/ios-studio" in text, doc


def test_readme_installs_through_the_plugin_and_claims_no_release() -> None:
    readme = (ROOT / "README.md").read_text()
    install = readme.split("## Install", 1)[1].split("## First run", 1)[0]
    assert install.index("omarchy plugin add https://github.com/ArtMin96/omarchy-ios-studio") < install.index("git clone")
    assert "omarchy plugin remove io.github.artmin96.ios-studio" in readme
    assert not re.search(r"--branch v\d", readme)   # no tags exist yet: never name a version to clone


def test_the_prompt_is_never_cut_off() -> None:
    qml = (ROOT / "plugin/BarWidget.qml").read_text()
    assert 'maximumLineCount: root.stage === "failed" ? 4' in qml


# --- scripts are strict and the button does nothing until clicked -------------------------------------------------

def test_scripts_are_strict() -> None:
    for script in ("install.sh", "plugin/app.sh"):
        assert "\nset -euo pipefail\n" in (ROOT / script).read_text(), script
        assert (ROOT / script).stat().st_mode & stat.S_IXUSR


def test_the_button_runs_nothing_at_load() -> None:
    qml = (ROOT / "plugin/BarWidget.qml").read_text()
    assert "running: true" not in qml and "Timer" not in qml and "Component.onCompleted" not in qml
    assert qml.count("Process {") == 2   # status and installer, started only from activate() / install()
