"""Updating through the plugin: after `omarchy plugin update` moves the plugin folder on, a click finds the app
folder behind it (plugin/app.sh status -> "outdated") and plugin/app.sh update fast-forwards the app folder from the
local plugin folder (install.sh --update), never over local changes or commits of its own, then repairs the install.
Everything runs under a private HOME with fake tools and throwaway git repos (see test_omarchy_plugin).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import test_omarchy_plugin as plug

home = plug.home   # the private-HOME fixture
plugin_folder, run = plug.plugin_folder, plug.run
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
         "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(h: dict, repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], env={**IDENT, **h["env"]}, capture_output=True, text=True,
                       timeout=60, check=True)
    return r.stdout.strip()


def commit(h: dict, repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    git(h, repo, "add", name)
    git(h, repo, "commit", "-qm", f"change {name}")
    return git(h, repo, "rev-parse", "HEAD")


def installed(h: dict) -> Path:
    folder = plugin_folder(h)
    r = run(h, folder / "plugin/app.sh", "install")
    assert r.returncode == 0, r.stdout + r.stderr
    return folder


def status(h: dict, folder: Path) -> str:
    return run(h, folder / "plugin/app.sh", "status").stdout.strip()


def test_a_fresh_install_is_up_to_date(home: dict) -> None:
    assert status(home, installed(home)) == "installed"


def test_a_plugin_update_is_offered_and_applied(home: dict) -> None:
    folder = installed(home)
    settings = home["home"] / ".config/ios-studio/settings.json"
    settings.write_text('{"opaqueWindow": true}')
    state = home["home"] / ".local/state/ios-studio/history.json"
    state.write_text("[]")
    new = commit(home, folder, "NEWS.txt", "a new version\n")   # what omarchy plugin update does: fast-forward
    assert status(home, folder) == "outdated"
    r = run(home, folder / "plugin/app.sh", "update")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Updating iOS Studio" in r.stdout and "Installing iOS Studio" in r.stdout
    app = home["app"]
    assert git(home, app, "rev-parse", "HEAD") == new and (app / "NEWS.txt").read_text() == "a new version\n"
    assert git(home, app, "remote", "get-url", "origin") == plug.ORIGIN   # not repointed at the plugin folder
    assert home["launcher"].resolve() == (app / "bin/ios-studio").resolve()
    assert settings.read_text() == '{"opaqueWindow": true}' and state.read_text() == "[]"
    assert status(home, folder) == "installed"
    assert plug.links_in(folder) == [] and git(home, folder, "status", "--porcelain") == ""
    again = run(home, folder / "plugin/app.sh", "update")   # nothing to do: still repairs, still fine
    assert again.returncode == 0 and "already up to date" in again.stdout


def test_local_changes_are_never_clobbered(home: dict) -> None:
    folder = installed(home)
    app = home["app"]
    before = git(home, app, "rev-parse", "HEAD")
    (app / "README.md").write_text("my notes\n")
    commit(home, folder, "NEWS.txt", "new\n")
    assert status(home, folder) == "outdated"
    r = run(home, folder / "plugin/app.sh", "update")
    assert r.returncode == 1 and "has changes of its own" in r.stdout
    assert git(home, app, "rev-parse", "HEAD") == before and (app / "README.md").read_text() == "my notes\n"


def test_a_diverged_copy_is_left_alone(home: dict) -> None:
    folder = installed(home)
    app = home["app"]
    mine = commit(home, app, "MINE.txt", "my commit\n")
    commit(home, folder, "NEWS.txt", "new\n")
    assert status(home, folder) == "installed"   # not simply behind: never offered
    r = run(home, folder / "install.sh", "--update")
    assert r.returncode == 1 and "has commits the plugin doesn't" in r.stderr
    assert git(home, app, "rev-parse", "HEAD") == mine and not (app / "NEWS.txt").exists()


def test_an_untracked_file_in_the_way_stops_the_update(home: dict) -> None:
    folder = installed(home)
    app = home["app"]
    before = git(home, app, "rev-parse", "HEAD")
    (app / "NEWS.txt").write_text("mine, untracked\n")
    commit(home, folder, "NEWS.txt", "theirs\n")
    r = run(home, folder / "plugin/app.sh", "update")
    assert r.returncode == 1 and "in the way" in r.stdout
    assert (app / "NEWS.txt").read_text() == "mine, untracked\n" and git(home, app, "rev-parse", "HEAD") == before


def test_a_copy_without_git_is_never_outdated_or_updated(home: dict) -> None:
    folder = installed(home)
    shutil.rmtree(home["app"] / ".git")
    commit(home, folder, "NEWS.txt", "new\n")
    assert status(home, folder) == "installed"
    r = run(home, folder / "plugin/app.sh", "update")
    assert r.returncode == 1 and "can't be updated in place" in r.stdout


def test_an_app_installed_elsewhere_is_not_offered_an_update(home: dict) -> None:
    folder = installed(home)
    commit(home, folder, "NEWS.txt", "new\n")
    elsewhere = home["home"] / "src/omarchy-ios-studio/bin/ios-studio"
    elsewhere.parent.mkdir(parents=True)
    plug.support.write_exe(elsewhere, "#!/bin/sh\nexit 0\n")
    home["launcher"].unlink()
    home["launcher"].symlink_to(elsewhere)
    assert status(home, folder) == "installed"


def test_update_only_runs_from_a_plugin_folder(home: dict) -> None:
    clone = home["home"] / "src/omarchy-ios-studio"
    plugin = plugin_folder(home)
    shutil.copytree(plugin, clone, symlinks=True)
    shutil.rmtree(plugin.parent)
    r = run(home, clone / "install.sh", "--update")
    assert r.returncode == 2 and "plugin folder" in r.stderr
    assert not home["launcher"].exists()
