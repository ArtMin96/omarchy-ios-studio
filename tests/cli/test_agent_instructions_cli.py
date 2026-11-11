"""Agent instruction files through the helper process: `projects new` (fake xtool that fails, so the built-in
skeleton) and `projects convert` on a temp home with real git, the --agent-files flag and the setting, and the
demo answers' shape."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from support import HELPER_PY, isolated_env, write_exe
from support.projects import package_project


def helper(*args: str, env: dict) -> dict:
    r = subprocess.run([*HELPER_PY, *args], capture_output=True, text=True, env=env, timeout=60)
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture
def env(tmp_path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_exe(bin_dir / "xtool", "#!/bin/sh\necho 'xtool: not set up' >&2\nexit 1\n")
    return isolated_env(tmp_path, HOME=str(tmp_path), PATH=f"{bin_dir}{os.pathsep}/usr/bin:/bin",
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")


def settings(env: dict, data: dict) -> None:
    d = Path(env["XDG_CONFIG_HOME"]) / "ios-studio"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps(data))


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_new_project_commits_the_files(tmp_path, env):
    out = helper("projects", "new", "--name", "Habit", "--dir", str(tmp_path / "apps"),
                 "--bundle-id", "com.me.Habit", env=env)
    assert out["ok"], out
    data = out["data"]
    assert data["agentFiles"] == ["AGENTS.md", "CLAUDE.md"] and data["usedXtool"] is False
    dest = Path(data["path"])
    assert "com.me.Habit" in (dest / "AGENTS.md").read_text()
    tracked = subprocess.run(["git", "ls-files"], cwd=dest, capture_output=True, text=True).stdout.split()
    assert {"AGENTS.md", "CLAUDE.md"} <= set(tracked)


def test_setting_off_and_flag_on(tmp_path, env):
    settings(env, {"agentInstructions": False})
    data = helper("projects", "new", "--name", "Plain", "--dir", str(tmp_path / "apps"), "--git", "0", env=env)["data"]
    assert data["agentFiles"] == [] and not (Path(data["path"]) / "AGENTS.md").exists()
    data = helper("projects", "new", "--name", "Forced", "--dir", str(tmp_path / "apps"), "--git", "0",
                  "--agent-files", "on", env=env)["data"]
    assert data["agentFiles"] == ["AGENTS.md", "CLAUDE.md"]


def test_convert_keeps_existing_files(tmp_path, env):
    p = package_project(tmp_path / "Pkg")
    (p / "xtool.yml").unlink()
    (p / "AGENTS.md").write_text("ours\n")
    out = helper("projects", "convert", "--path", str(p), env=env)
    assert out["ok"], out
    assert out["data"]["agentFiles"] == ["CLAUDE.md"]
    assert (p / "AGENTS.md").read_text() == "ours\n" and (p / "xtool.yml").is_file()
    out = helper("projects", "convert", "--path", str(p), "--agent-files", "off", env=env)
    assert out["ok"] and out["data"]["agentFiles"] == []


def test_demo_answers_have_the_field(tmp_path, env):
    demo = {**env, "IOSSTUDIO_DEMO": "1"}
    assert helper("projects", "new", "--name", "X", env=demo)["data"]["agentFiles"] == ["AGENTS.md", "CLAUDE.md"]
    out = helper("projects", "convert", "--path", str(tmp_path), "--agent-files", "off", env=demo)
    assert out["data"]["agentFiles"] == []
    assert not any(tmp_path.glob("*.md"))   # demo mode writes nothing
