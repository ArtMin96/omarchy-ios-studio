"""Shared test helpers: repo paths, an isolated environment for helper subprocesses, and running the helper.

tests/ is on pytest's pythonpath (pyproject.toml), so test files `import support` / `from support.fakes import …`.
Feature-specific helpers live in the submodules (fakes, projects, crash, swift_format, mcp).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
HELPER = ROOT / "bin" / "iosstudio-helper"
HELPER_PY = [sys.executable, str(ROOT / "helper" / "main.py")]   # the helper without the bin/ wrapper

XDG_VARS = ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")


def isolated_env(tmp: Path, xdg: tuple[str, ...] = XDG_VARS, **extra: str) -> dict:
    """os.environ with each XDG dir pointed at tmp/<var lowercased> and demo mode off, plus `extra`."""
    env = dict(os.environ)
    for var in xdg:
        env[var] = str(tmp / var.lower())
    env.pop("IOSSTUDIO_DEMO", None)
    env.update(extra)
    return env


def write_exe(path: Path, text: str) -> Path:
    """Write an executable script (a fake xtool, swift, …)."""
    path.write_text(text)
    path.chmod(0o755)
    return path


def helper_json(*args: str, env: dict | None = None) -> dict:
    """Run bin/iosstudio-helper (demo mode off unless env turns it on) and parse its last stdout line."""
    full = {k: v for k, v in os.environ.items() if k != "IOSSTUDIO_DEMO"}
    full.update(env or {})
    r = subprocess.run([str(HELPER), *args], capture_output=True, text=True, env=full, timeout=60)
    return json.loads(r.stdout.strip().splitlines()[-1])
