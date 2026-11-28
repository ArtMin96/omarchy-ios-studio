"""Settings › AI agents reads the MCP venv's stamp in Python (agents/server.py server_status) and calls it ready only
when it equals what bin/iosstudio-mcp writes. Both hash pyproject.toml + requirements.lock; this runs the real
launcher with a fake uv in a copy of the repo and checks that the Python formula agrees.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from iosstudio.features.agents import server

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ("bash", "readlink", "dirname", "mkdir", "flock", "sha256sum", "cut", "cat", "rm", "chmod")
FAKE_UV = r"""#!/bin/bash
if [[ $1 == venv ]]; then mkdir -p "${@: -1}/bin"; printf '#!/bin/bash\n' >"${@: -1}/bin/python"; chmod +x "${@: -1}/bin/python"; fi
exit 0
"""


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    repo, tools, venv = tmp_path / "repo", tmp_path / "tools", tmp_path / "venv"
    (repo / "bin").mkdir(parents=True)
    (repo / "mcp").mkdir()
    tools.mkdir()
    shutil.copy(ROOT / "bin/iosstudio-mcp", repo / "bin/iosstudio-mcp")
    for name in ("pyproject.toml", "requirements.lock"):
        shutil.copy(ROOT / "mcp" / name, repo / "mcp" / name)
    for tool in TOOLS:
        os.symlink(shutil.which(tool), tools / tool)
    (tools / "uv").write_text(FAKE_UV)
    (tools / "uv").chmod(0o755)
    r = subprocess.run([str(repo / "bin/iosstudio-mcp"), "--prepare"], cwd=tmp_path, capture_output=True, text=True,
                       env={"PATH": str(tools), "HOME": str(tmp_path), "IOSSTUDIO_MCP_VENV": str(venv)}, timeout=30)
    assert r.returncode == 0, r.stderr
    return repo, venv


def test_status_formula_matches_the_launcher_stamp(tmp_path, monkeypatch):
    repo, venv = _prepare(tmp_path)
    monkeypatch.setattr(server, "PYPROJECT", repo / "mcp/pyproject.toml")
    monkeypatch.setattr(server, "LOCK", repo / "mcp/requirements.lock")
    monkeypatch.setenv("IOSSTUDIO_MCP_VENV", str(venv))
    assert server.server_status()["state"] == "ready"

    with (repo / "mcp/requirements.lock").open("a") as f:   # a refreshed lock: the launcher will reinstall
        f.write("# refreshed\n")
    assert server.server_status()["state"] == "outdated"


def test_the_real_files_are_what_status_hashes():
    assert server.PYPROJECT == ROOT / "mcp/pyproject.toml" and server.LOCK == ROOT / "mcp/requirements.lock"
