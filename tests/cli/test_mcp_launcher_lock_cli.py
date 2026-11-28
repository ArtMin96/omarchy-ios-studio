"""bin/iosstudio-mcp --prepare installs exactly mcp/requirements.lock with hashes required: `uv pip sync
--require-hashes` when uv is on PATH, else `python -I -m pip install --require-hashes --no-deps -r`. Its stamp hashes
the lock and pyproject.toml, so a normal start runs nothing, and a changed lock reinstalls.

Fake uv and python3 on a PATH of plain tools only: no network, no real venv.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ("bash", "readlink", "dirname", "mkdir", "flock", "sha256sum", "cut", "cat", "rm", "chmod", "ln")

FAKE_UV = r"""#!/bin/bash
echo "uv $*" >>"$FAKE_LOG"
[[ ${FAKE_FAIL:-} == 1 && $1 == pip ]] && exit 3
if [[ $1 == venv ]]; then mkdir -p "${@: -1}/bin"; printf '#!/bin/bash\n' >"${@: -1}/bin/python"; chmod +x "${@: -1}/bin/python"; fi
exit 0
"""
FAKE_PY = r"""#!/bin/bash
echo "python3 $*" >>"$FAKE_LOG"
if [[ $* == "-I -m venv "* ]]; then
  mkdir -p "$4/bin"
  printf '#!/bin/bash\necho "venv-python $*" >>"$FAKE_LOG"\n' >"$4/bin/python"; chmod +x "$4/bin/python"
fi
"""


@pytest.fixture
def env(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "mcp").mkdir()
    shutil.copy(ROOT / "bin/iosstudio-mcp", repo / "bin/iosstudio-mcp")
    for name in ("pyproject.toml", "requirements.lock"):
        shutil.copy(ROOT / "mcp" / name, repo / "mcp" / name)
    plain, fakes = tmp_path / "plain", tmp_path / "fakes"
    plain.mkdir()
    fakes.mkdir()
    for tool in TOOLS:
        os.symlink(shutil.which(tool), plain / tool)
    (fakes / "python3").write_text(FAKE_PY)
    (fakes / "python3").chmod(0o755)
    uvbin = tmp_path / "uvbin"
    uvbin.mkdir()
    (uvbin / "uv").write_text(FAKE_UV)
    (uvbin / "uv").chmod(0o755)
    cwd = tmp_path / "agent-cwd"
    cwd.mkdir()
    log = tmp_path / "calls.log"

    class Env:
        root, venv = repo, tmp_path / "venv"

        def run(self, uv: bool = True, **extra: str) -> subprocess.CompletedProcess:
            path = f"{uvbin}:{fakes}:{plain}" if uv else f"{fakes}:{plain}"
            e = {"PATH": path, "HOME": str(tmp_path), "FAKE_LOG": str(log),
                 "IOSSTUDIO_MCP_VENV": str(self.venv), **extra}
            return subprocess.run([str(repo / "bin/iosstudio-mcp"), "--prepare"], cwd=cwd, env=e,
                                  capture_output=True, text=True, timeout=30)

        def calls(self) -> list[str]:
            lines = log.read_text().splitlines() if log.exists() else []
            log.unlink(missing_ok=True)
            return lines

        def want(self) -> str:
            data = (repo / "mcp/pyproject.toml").read_bytes() + (repo / "mcp/requirements.lock").read_bytes()
            return hashlib.sha256(data).hexdigest()[:16]

        def stamp(self) -> str:
            p = self.venv / ".iosstudio-deps"
            return p.read_text().strip() if p.exists() else ""

    return Env()


def test_uv_syncs_the_lock_with_hashes_once(env):
    r = env.run()
    assert r.returncode == 0, r.stderr
    lock = env.root / "mcp/requirements.lock"
    assert env.calls() == [f"uv venv --quiet --allow-existing --python >=3.11 {env.venv}",
                           f"uv pip sync --quiet --require-hashes {lock}"]
    assert env.stamp() == env.want()
    assert r.stdout == ""                                   # stdout is the MCP protocol

    assert env.run().returncode == 0
    assert env.calls() == []                                # a normal start: no uv, no python, no network


def test_a_changed_lock_or_pyproject_reinstalls(env):
    env.run()
    env.calls()
    with (env.root / "mcp/requirements.lock").open("a") as f:
        f.write("# refreshed\n")
    assert env.run().returncode == 0
    assert any("pip sync" in c for c in env.calls())
    assert env.stamp() == env.want()
    with (env.root / "mcp/pyproject.toml").open("a") as f:
        f.write("\n")
    env.run()
    assert any("pip sync" in c for c in env.calls())


def test_pip_without_uv_requires_hashes_and_isolates_python(env):
    r = env.run(uv=False)
    assert r.returncode == 0, r.stderr
    lock = env.root / "mcp/requirements.lock"
    assert env.calls() == [f"python3 -I -m venv {env.venv}",
                           "venv-python -I -m pip install --quiet --disable-pip-version-check --require-hashes "
                           f"--no-deps -r {lock}"]
    assert env.stamp() == env.want()


def test_a_failed_install_leaves_no_stamp(env):
    env.run()
    env.calls()
    (env.root / "mcp/requirements.lock").write_text((env.root / "mcp/requirements.lock").read_text() + "#\n")
    r = env.run(FAKE_FAIL="1")
    assert r.returncode != 0
    assert env.stamp() == ""                                # the next start tries again
    assert env.run().returncode == 0 and env.stamp() == env.want()


def test_missing_lock_is_an_error(env):
    (env.root / "mcp/requirements.lock").unlink()
    r = env.run()
    assert r.returncode == 1 and "requirements.lock" in r.stderr
    assert env.calls() == []
