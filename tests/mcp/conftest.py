"""The MCP server's tests need its dependencies (mcp, pydantic, pillow): without them this folder is skipped, so the
stdlib-only run (`python -m pytest tests/helper tests/cli`) and a bare `pytest` both work."""
import importlib.util


def _have(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:   # the parent package is missing
        return False


# `mcp.server`, not `mcp`: with tests/ on sys.path, this folder alone can pass for a namespace package `mcp`.
collect_ignore_glob = [] if _have("mcp.server") and _have("pydantic") else ["test_*.py"]
