"""Moved to core/budget.py. This alias keeps `from iosstudio_mcp import budget` working for code outside mcp/
(tests/support/mcp_fakes.py); new code imports iosstudio_mcp.core.budget."""
import sys

from .core import budget as _budget

sys.modules[__name__] = _budget
