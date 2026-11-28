"""mcp/requirements.lock, the hash-pinned dependencies bin/iosstudio-mcp installs, is in step with mcp/pyproject.toml:
every top-level dependency is pinned there at a version its range allows, and every pin carries --hash lines.
Offline: reads the two files only. To refresh the lock see docs/mcp.md#dependencies.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "mcp" / "pyproject.toml"
LOCK = ROOT / "mcp" / "requirements.lock"
REFRESH = "refresh it: see docs/mcp.md#dependencies"


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v))


def allows(spec: str, version: str) -> bool:
    """Whether a simple PEP 440 range ("A>=1,<2", "==1.2", "!=1.3") allows a release version."""
    have = version_key(version)
    for clause in filter(None, (c.strip() for c in spec.split(","))):
        op, want = re.fullmatch(r"(>=|<=|==|!=|~=|>|<)\s*([\w.]+)", clause).groups()
        w = version_key(want)
        n = max(len(have), len(w))
        a, b = have + (0,) * (n - len(have)), w + (0,) * (n - len(w))
        ok = {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "==": a == b, "!=": a != b,
              "~=": a >= b and a[:len(w) - 1] == b[:len(w) - 1]}[op]
        if not ok:
            return False
    return True


def top_level() -> dict[str, str]:
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    out = {}
    for dep in deps:
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;]*)", dep.strip())
        assert m, f"can't read {dep!r} in mcp/pyproject.toml"
        out[norm(m.group(1))] = m.group(3).strip()
    return out


def pins() -> dict[str, tuple[str, int]]:
    """name -> (version, number of --hash lines) for each requirement in the lock."""
    out: dict[str, tuple[str, int]] = {}
    current = None
    for line in LOCK.read_text().splitlines():
        if m := re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line):
            current = norm(m.group(1))
            out[current] = (m.group(2), 0)
        elif current and re.match(r"\s+--hash=sha256:[0-9a-f]{64}", line):
            version, n = out[current]
            out[current] = (version, n + 1)
        elif line.strip() and not line.lstrip().startswith("#"):
            assert line.startswith(" "), f"unexpected line in the lock: {line!r}"
    return out


def test_every_top_level_dependency_is_pinned_in_range():
    locked = pins()
    for name, spec in top_level().items():
        assert name in locked, f"{name} is in mcp/pyproject.toml but not in mcp/requirements.lock; {REFRESH}"
        version = locked[name][0]
        assert allows(spec, version), f"the lock pins {name}=={version}, outside {spec!r}; {REFRESH}"


def test_every_pin_has_hashes():
    locked = pins()
    assert len(locked) >= len(top_level())
    missing = sorted(name for name, (_, hashes) in locked.items() if hashes == 0)
    assert not missing, f"pins without --hash: {missing}; {REFRESH}"


def test_lock_is_universal_and_hashed():
    """Made for every Python the venv may use (3.11 and newer), not just the one that compiled it."""
    head = LOCK.read_text()[:600]
    assert "uv pip compile mcp/pyproject.toml" in head
    for flag in ("--universal", "--python-version 3.11", "--generate-hashes"):
        assert flag in head, f"the lock wasn't made with {flag}; {REFRESH}"


def test_version_ranges():
    assert allows(">=2.3,<3", "2.3.0") and allows(">=2.3,<3", "2.14.4")
    assert not allows(">=2.3,<3", "3.0") and not allows(">=10", "9.5.0")
    assert allows("", "1.0") and allows("~=2.3", "2.9") and not allows("~=2.3", "3.0")
