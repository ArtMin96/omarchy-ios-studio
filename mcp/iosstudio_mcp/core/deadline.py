"""The time budget of one tool call: no call runs past MAX_WAIT (opencode gives up at 30 s).

    with call_budget():   tool() opens one per call; a nested one never extends it
    clamp(timeout, what)  a step's timeout cut to what is left of the budget (raises when nothing is left)
    wait_time(seconds)    a long-poll's wait cut the same way (never raises; 0 when nothing is left)
    remaining()           seconds left, or None outside a tool call
    clear()               leave the budget (background job tasks: they outlive the call that started them)

helper.call(), dcall() and the job long-polls go through clamp() / wait_time(), so a tool that makes several
slow steps in a row still answers within the budget, with a timeout error that says which step ran out.
Work that can take longer is a job (core/jobs.py) and runs with no budget.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from mcp.server.mcpserver.exceptions import ToolError

MAX_WAIT = 25      # seconds; stays under opencode's 30 s and Codex's 60 s default tool timeouts
RESERVE = 1.0      # seconds kept back for building the result after the last step
MIN_STEP = 0.5     # a step with less than this left isn't started

_deadline: ContextVar[float | None] = ContextVar("iosstudio_deadline", default=None)


@contextmanager
def call_budget(seconds: float | None = None) -> Iterator[None]:
    """The budget of one tool call (MAX_WAIT by default). Inside another budget (a tool calling a tool) the
    earlier deadline wins."""
    end = time.monotonic() + (MAX_WAIT if seconds is None else seconds)
    outer = _deadline.get()
    token = _deadline.set(end if outer is None else min(outer, end))
    try:
        yield
    finally:
        _deadline.reset(token)


def clear() -> None:
    """No budget in the current context (a background task's own context: call it first thing in the task)."""
    _deadline.set(None)


def remaining() -> float | None:
    end = _deadline.get()
    return None if end is None else end - time.monotonic()


def clamp(timeout: float, what: str = "this step") -> float:
    """`timeout`, cut to what is left of the call's budget (less RESERVE). Outside a tool call: unchanged."""
    left = remaining()
    if left is None:
        return timeout
    left -= RESERVE
    if left < MIN_STEP:
        raise ToolError(f"The call ran out of its {MAX_WAIT} s time budget before {what} [timeout]. "
                        "Next: retry; if it keeps happening, check ios_status.")
    return min(timeout, left)


def wait_time(seconds: float) -> float:
    """How long a long-poll may wait: `seconds`, cut to the budget (less RESERVE), never below 0."""
    seconds = max(0.0, seconds)
    left = remaining()
    return seconds if left is None else max(0.0, min(seconds, left - RESERVE))
