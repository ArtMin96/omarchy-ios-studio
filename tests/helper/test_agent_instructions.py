"""Agent instruction files (AGENTS.md + CLAUDE.md): the templates, wanted() (flag over setting), write() never
overwriting and never failing, and the projects new / convert hooks in process."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from iosstudio.core import appdirs, subproc
from iosstudio.features import agent_instructions as A
from iosstudio.features.projects import commands, model, store
from support.projects import package_project


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Own settings.json; returns a function that writes it."""
    f = tmp_path / "settings.json"
    monkeypatch.setattr(appdirs, "SETTINGS_FILE", f)
    return lambda data: f.write_text(json.dumps(data))


@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(appdirs, "STATE_DIR", d)
    for name, val in {"PROJECTS_FILE": d / "projects.json", "BUILDS_FILE": d / "builds.json",
                      "RENEWALS_FILE": d / "renewals.json", "BUILD_LOGS": d / "build-logs"}.items():
        monkeypatch.setattr(store, name, val)
    return d


def reply(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# ---------------------------------------------------------------- templates

def test_templates_dir_is_not_an_app_template():
    assert A.TEMPLATE_DIR.is_dir() and not (A.TEMPLATE_DIR / "template.json").exists()
    ids = [m["id"] for m in model.templates()]
    assert "agent-instructions" not in ids and ids   # the app templates are still listed


def test_claude_md_imports_agents_md():
    text = (A.TEMPLATE_DIR / "CLAUDE.md").read_text()
    assert "@AGENTS.md" in text.splitlines() and len(text.splitlines()) <= 5


def test_agents_md_has_both_placeholders_and_no_mac_tools():
    text = (A.TEMPLATE_DIR / "AGENTS.md").read_text()
    assert "{{NAME}}" in text and "{{BUNDLE_ID}}" in text
    assert "verify_change" in text and 'ios_build_start(action="compile")' in text
    assert 40 <= len(text.splitlines()) <= 100


# ---------------------------------------------------------------- wanted

@pytest.mark.parametrize("value, expected", [("on", True), ("true", True), ("1", True), ("YES", True), (True, True),
                                             ("off", False), ("false", False), ("0", False), ("no", False)])
def test_flag_wins_over_setting(settings, value, expected):
    settings({"agentInstructions": not expected})
    assert A.wanted({"agent_files": value}) is expected


@pytest.mark.parametrize("stored, expected", [(None, True), (True, True), (False, False), ("off", False)])
def test_setting_decides_without_flag(settings, stored, expected):
    settings({} if stored is None else {"agentInstructions": stored})
    assert A.wanted({}) is expected
    assert A.wanted({"agent_files": "maybe"}) is expected   # an unknown flag value falls back to the setting


def test_on_without_settings_file(settings):
    assert A.wanted({}) is True


# ---------------------------------------------------------------- write

def test_write_fills_placeholders(tmp_path):
    assert A.write(tmp_path, "Habit", "com.me.Habit") == ["AGENTS.md", "CLAUDE.md"]
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "{{" not in agents and "Sources/Habit/" in agents and "com.me.Habit" in agents
    assert "@AGENTS.md" in (tmp_path / "CLAUDE.md").read_text()
    assert A.write(tmp_path, "Habit", "com.me.Habit") == []   # second time: nothing to do


def test_write_never_replaces_anything(tmp_path):
    (tmp_path / "AGENTS.md").write_text("mine\n")
    (tmp_path / "CLAUDE.md").mkdir()
    assert A.write(tmp_path, "X", "com.x.X") == []
    assert (tmp_path / "AGENTS.md").read_text() == "mine\n" and (tmp_path / "CLAUDE.md").is_dir()


def test_write_leaves_symlinks_alone_even_broken_ones(tmp_path):
    target = tmp_path / "elsewhere.md"
    target.write_text("shared\n")
    (tmp_path / "AGENTS.md").symlink_to(target)
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "missing.md")
    assert A.write(tmp_path, "X", "com.x.X") == []
    assert target.read_text() == "shared\n" and not (tmp_path / "missing.md").exists()


def test_write_only_the_missing_one(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("mine\n")
    assert A.write(tmp_path, "X", "com.x.X") == ["AGENTS.md"]
    assert (tmp_path / "CLAUDE.md").read_text() == "mine\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_write_is_best_effort(tmp_path, monkeypatch):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        assert A.write(ro, "X", "com.x.X") == []
    finally:
        ro.chmod(0o700)
    assert A.write(tmp_path / "gone", "X", "com.x.X") == []
    monkeypatch.setattr(A, "TEMPLATE_DIR", tmp_path / "no-templates")
    assert A.write(tmp_path, "X", "com.x.X") == []


# ---------------------------------------------------------------- projects new / convert

@pytest.fixture
def no_xtool(monkeypatch):
    monkeypatch.setattr(subproc, "which", lambda name: None)   # the built-in skeleton, no real xtool


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_new_app_has_the_files_in_its_first_commit(tmp_path, settings, state, no_xtool, capsys, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    commands.cmd_new({"name": "Habit", "dir": str(tmp_path), "bundle_id": "com.me.Habit"})
    data = reply(capsys)["data"]
    assert data["agentFiles"] == ["AGENTS.md", "CLAUDE.md"]
    dest = Path(data["path"])
    assert "com.me.Habit" in (dest / "AGENTS.md").read_text()
    tracked = subprocess.run(["git", "ls-files"], cwd=dest, capture_output=True, text=True).stdout.split()
    assert {"AGENTS.md", "CLAUDE.md"} <= set(tracked)


def test_new_app_without_files_when_off(tmp_path, settings, state, no_xtool, capsys):
    settings({"agentInstructions": False})
    commands.cmd_new({"name": "Plain", "dir": str(tmp_path), "git": "0"})
    data = reply(capsys)["data"]
    assert data["agentFiles"] == []
    assert not (Path(data["path"]) / "AGENTS.md").exists()
    commands.cmd_new({"name": "Forced", "dir": str(tmp_path), "git": "0", "agent_files": "on"})
    assert reply(capsys)["data"]["agentFiles"] == ["AGENTS.md", "CLAUDE.md"]


def _package(root: Path) -> Path:
    """A plain Swift package with one library (no xtool.yml)."""
    p = package_project(root)
    (p / "xtool.yml").unlink()
    return p


def test_convert_adds_files_and_never_overwrites(tmp_path, settings, state, capsys):
    p = _package(tmp_path / "Pkg")
    (p / "CLAUDE.md").write_text("my notes\n")
    commands.cmd_convert({"path": str(p), "bundle_id": "com.me.App"})
    data = reply(capsys)["data"]
    assert data["agentFiles"] == ["AGENTS.md"]
    assert (p / "CLAUDE.md").read_text() == "my notes\n"
    text = (p / "AGENTS.md").read_text()
    assert "Sources/App/" in text and "com.me.App" in text


def test_convert_of_an_existing_app_adds_missing_files(tmp_path, settings, state, capsys):
    p = package_project(tmp_path / "Ready")
    commands.cmd_convert({"path": str(p)})
    assert reply(capsys)["data"]["agentFiles"] == ["AGENTS.md", "CLAUDE.md"]
    commands.cmd_convert({"path": str(p)})
    assert reply(capsys)["data"]["agentFiles"] == []


def test_convert_off_and_add_never_write(tmp_path, settings, state, capsys):
    p = _package(tmp_path / "Pkg")
    commands.cmd_convert({"path": str(p), "agent_files": "off"})
    assert reply(capsys)["data"]["agentFiles"] == []
    commands.cmd_add({"path": str(p)})
    assert "agentFiles" not in reply(capsys)["data"]
    assert not (p / "AGENTS.md").exists() and not (p / "CLAUDE.md").exists()
