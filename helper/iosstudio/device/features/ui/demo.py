"""Demo: ui.tree, ui.press, ui.audit on made-up screens.

The app in front is HabitTracker once it runs (launch it), else WeatherGlance (what the demo
screenshot shows). Pressing "Add habit" adds a row, a habit row toggles Done/Not done and Units toggles °C/°F, so ui.press has
a real diff to report. Output goes through helper/iosstudio/features/ui_inspect.py like the real daemon's.
"""
from __future__ import annotations

import threading
import time

from iosstudio.core.subproc import demo_delay
from iosstudio.device.demo.core import APPS, pids, running
from iosstudio.features import ui_inspect as U

WEATHER = next(b for n, b, *_ in APPS if n == "WeatherGlance")
HABITS = next(b for n, b, *_ in APPS if n == "HabitTracker")
SCREEN = {"w": 390, "h": 844}

_lock = threading.Lock()
_state = {"habits": ["Drink water", "Read 20 pages", "Stretch"], "done": set(), "unit": "°C", "gen": 0,
          "ids": None}


def _el(role, label, frame, value=None, hint=None, identifier=None, traits=None, enabled=True):
    traits = list(traits or ([role] if role in ("button", "header", "image", "staticText", "toggle", "searchField")
                             else []))
    return {"role": role, "label": label, "value": value, "hint": hint, "identifier": identifier,
            "traits": traits, "frame": dict(zip("xywh", frame)), "enabled": enabled}


def _weather() -> list[dict]:
    u = _state["unit"]
    t = "18" if u == "°C" else "64"
    return [
        _el("header", "Lisbon", (24, 64, 200, 41), traits=["header"], identifier="cityName"),
        _el("staticText", f"{t}{u}, Partly cloudy", (24, 112, 260, 96), identifier="currentTemp"),
        _el("button", "Units", (300, 64, 66, 32), value=u, hint="Switches between Celsius and Fahrenheit",
            identifier="unitsButton"),
        _el("staticText", "Hourly forecast", (24, 236, 200, 22), traits=["header", "staticText"]),
        *[_el("staticText", f"{h}:00, {int(t) + d}{u}", (24 + 70 * i, 268, 60, 80))
          for i, (h, d) in enumerate(((14, 0), (15, 1), (16, 1), (17, -1), (18, -2)))],
        _el("image", None, (24, 380, 342, 180), traits=["image"]),   # a chart without a label
        _el("button", "Refresh", (24, 760, 342, 50), identifier="refresh"),
    ]


def _habits() -> list[dict]:
    rows = []
    for i, h in enumerate(_state["habits"]):
        done = h in _state["done"]
        rows.append(_el("button", h, (16, 150 + 56 * i, 358, 48), value="Done" if done else "Not done",
                        hint="Marks the habit as done for today", traits=["button"] + (["selected"] if done else [])))
    n = len(_state["habits"])
    return [
        _el("header", "Habits", (16, 60, 200, 41), traits=["header"]),
        _el("staticText", f"{len(_state['done'])} of {n} done today", (16, 108, 300, 22), identifier="progress"),
        *rows,
        _el("button", "Add habit", (16, 160 + 56 * n, 358, 50), identifier="addHabit"),
        _el("button", "Settings", (340, 60, 34, 34), identifier="settings"),
    ]


def front() -> str:
    return HABITS if HABITS in running else WEATHER


def _tree(a) -> dict:
    elements = _habits() if front() == HABITS else _weather()
    limit = max(1, min(int(a.get("max") or U.DEFAULT_MAX), U.HARD_MAX))
    out = []
    for i, e in enumerate(elements[:limit], start=1):
        out.append({"id": f"e{i}", **e})
    _state["gen"] += 1
    _state["ids"] = {e["id"]: e for e in out}
    _state["elements"] = out
    return {"elements": out, "count": len(out), "truncated": len(elements) > limit,
            "complete": len(elements) <= limit, "method": "focus-walk", "elapsedMs": 40 * len(out) + 120,
            "screen": SCREEN, "stoppedBy": "loop" if len(elements) <= limit else "max",
            "bundleId": front(), "pid": pids.get(front(), 812)}


def _guard(a):
    want = a.get("bundleId")
    if want and U.match_bundle([b for _, b, *_ in APPS], want) != front():
        from iosstudio.core.output import HelperError
        raise HelperError("wrong-app", f"Another app is in front, not {want}. Bring it to the front and try again.")


def _press(e: dict) -> None:
    label = e.get("label")
    if front() == HABITS:
        if label == "Add habit":
            _state["habits"].append(f"New habit {len(_state['habits']) + 1}")
        elif label in _state["habits"]:
            _state["done"].symmetric_difference_update({label})
    elif label == "Units":
        _state["unit"] = "°F" if _state["unit"] == "°C" else "°C"


AUDIT = {
    WEATHER: [
        {"IssueClassificationValue_v1": 12, "ElementRectValue_v1": "{{24, 268}, {60, 80}}", "FontSizeValue_v1": 13,
         "ForegroundColorValue_v1": "#A7B0BA", "BackgroundColorValue_v1": "#FFFFFF"},
        {"IssueClassificationValue_v1": 5000, "ElementRectValue_v1": "{{24, 380}, {342, 180}}",
         "MLGeneratedDescriptionValue_v1": "Line chart"},
        {"IssueClassificationValue_v1": 3001, "ElementRectValue_v1": "{{24, 112}, {260, 96}}", "FontSizeValue_v1": 64},
    ],
    HABITS: [
        {"IssueClassificationValue_v1": 100, "ElementRectValue_v1": "{{340, 60}, {34, 34}}"},
        {"IssueClassificationValue_v1": 3003, "ElementRectValue_v1": "{{16, 108}, {300, 22}}", "FontSizeValue_v1": 17},
    ],
}
SUPPORTED = [12, 13, 100, 1000, 3001, 3002, 3003, 5000]


def register(reg):
    @reg.command("ui.tree")
    def ui_tree(ctx, a):
        _guard(a)
        time.sleep(demo_delay(0.3))
        with _lock:
            return _tree(a)

    @reg.command("ui.press")
    def ui_press(ctx, a):
        from iosstudio.core.output import HelperError
        if not a.get("id") and not a.get("label"):
            raise HelperError("bad-args", "Which element? Pass an element id from the last screen read, or a label.")
        _guard(a)
        with _lock:
            if a.get("id"):
                if not _state.get("ids") or a["id"] not in _state["ids"]:
                    raise HelperError("stale-element", f"There's no element {a['id']} in the last walk. "
                                      "Read the screen again for fresh ids.")
                before = _state["elements"]
                target = _state["ids"][a["id"]]
            else:
                before = _tree(a)["elements"]
                try:
                    target = U.pick_target(before, None, a["label"], a.get("match") or "exact")
                except U.TargetError as e:
                    raise HelperError(e.code, e.message, None, e.detail)
            _press(target)
            after = _tree(a)
        time.sleep(demo_delay(0.8))
        return {"pressed": target, "changes": U.diff_trees(before, after["elements"]),
                "after": {"count": after["count"], "complete": after["complete"]}, "elapsedMs": 1100,
                "bundleId": front(), "pid": pids.get(front(), 812)}

    @reg.command("ui.audit")
    def ui_audit(ctx, a):
        _guard(a)
        types = a.get("types")
        run, slugs, missing = U.audit_plan(types if isinstance(types, list) else None, SUPPORTED)
        with _lock:
            elements = _state.get("elements") or _tree({})["elements"]
        want = {U.audit_slug(r) for r in run}
        issues = [U.normalize_issue(i, elements) for i in AUDIT[front()]
                  if U.audit_slug(i["IssueClassificationValue_v1"]) in want]
        time.sleep(demo_delay(0.6))
        return {"issues": issues, "supported": slugs, "unsupported": missing, "elapsedMs": 2400,
                "bundleId": front(), "pid": pids.get(front(), 812)}
