"""tools/debug.py folds a stop's locals that arrive after the stop (the helper sends `stopped` with localsPending
over a slow link, then a `variables` event with the same stopId): the session view shows them pending, then in the
stop; locals that weren't read in time say so; a late answer for an older stop is ignored."""
from __future__ import annotations

from iosstudio_mcp.tools import debug as DEBUG


def stop_event(stop_id: int, **extra) -> dict:
    frames = [{"index": 0, "function": "tick", "module": "App", "inApp": True, "file": "/p/A.swift", "line": 7}]
    return {"event": "stopped", "stopId": stop_id, "reason": "breakpoint", "thread": {"index": 1, "numFrames": 1,
                                                                                      "frames": frames}, **extra}


def test_late_locals_join_their_stop():
    s = DEBUG.Session(id="dbg-s1", bundle_id="com.x", proc=None)
    s.apply({"event": "running"})
    s.apply(stop_event(1, localsPending=True))
    v = DEBUG.session_view(s)
    assert v.stop.locals_pending and v.stop.locals == []
    assert any("still being read" in n for n in v.next_steps)
    s.apply({"event": "variables", "stopId": 1, "frame": 0, "function": "tick", "partial": True, "more": False,
             "variables": [{"name": "n", "type": "Int", "value": "3", "childCount": 0},
                           {"name": "big", "type": "[Day]", "childCount": 0, "unread": True}]})
    v = DEBUG.session_view(s)
    assert not v.stop.locals_pending and v.stop.locals_partial
    assert [(x.name, x.value) for x in v.stop.locals] == [("n", "3"), ("big", "(not read in time)")]
    assert not any("still being read" in n for n in v.next_steps)


def test_an_answer_for_an_older_stop_is_ignored_and_fast_stops_are_unchanged():
    s = DEBUG.Session(id="dbg-s2", bundle_id="com.x", proc=None)
    s.apply(stop_event(2, localsPending=False, locals={"variables": [{"name": "a", "type": "Int", "value": "1"}]}))
    s.apply({"event": "variables", "stopId": 1, "variables": [{"name": "old", "type": "Int", "value": "9"}]})
    v = DEBUG.session_view(s)
    assert [x.name for x in v.stop.locals] == ["a"] and not v.stop.locals_pending
