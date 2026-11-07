"""UI inspection tools: read the screen's accessibility tree, press an element, run Apple's accessibility audit.

All three go through the shared device daemon (ui.tree, ui.press, ui.audit in docs/deviced-protocol.md), which
uses the iPhone's accessibility-audit service: no WebDriverAgent, nothing installed on the phone. The tree is
read by walking VoiceOver focus over the screen, so it is a flat list in reading order, and element ids
(e1, e2, …) stay valid until the screen changes.

The loop an agent runs: ios_run / ios_app_launch -> ios_ui_tree -> ios_ui_press -> ios_ui_tree or
ios_screenshot to check the result. Typing text, swipes and other gestures are out of scope here.

A walk takes seconds, so the last tree is kept per (udid, bundle_id, max_elements): a call with a cursor pages
through that same walk instead of walking again; a call without a cursor reads the screen again.
"""
from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..backends.device import dcall
from ..core import budget as B
from ..core.params import Udid
from ..core.registry import fit_page, page_of, tool
from ..models import Out
from ..state import AppState, get_state

TREE_MAX = 400           # the daemon's cap on elements per walk
TEXT_CUT = 120           # characters kept of a label, value, hint or identifier
CHANGES_CAP = 25         # labels per list in a press's change summary
CACHE_SECONDS = 120      # a cursor pages through a walk at most this old

# Roles and traits that make an element something to press or edit (matched case-insensitively, by substring).
INTERACTIVE = ("button", "link", "field", "textview", "switch", "toggle", "slider", "adjustable", "stepper",
               "picker", "segment", "tab", "menu", "cell", "checkbox", "keyboardkey", "searchfield", "incrementable")

BundleOpt = Annotated[str | None, Field(description="Bundle id of the app that must be in front (e.g. "
                                                    "'com.example.myapp'); the call fails with wrong-app if "
                                                    "another app is showing")]


# ------------------------------------------------------------------ models

class UiElement(Out):
    id: str = Field(description="Pass to ios_ui_press(element_id=...); valid until the screen changes")
    role: str | None = None
    label: str | None = None
    value: str | None = None
    hint: str | None = None
    identifier: str | None = Field(None, description="accessibilityIdentifier set in the app's code")
    traits: list[str] = Field(default_factory=list)
    frame: list[int] | None = Field(None, description="[x, y, w, h] in screen points")
    enabled: bool | None = Field(None, description="Only present (false) when the element is disabled")


class UiTree(B.Budgeted, Out):
    elements: list[UiElement] = Field(description="In VoiceOver reading order")
    count: int = Field(description="Elements in this page")
    total: int = Field(description="Elements matching the filters in this walk")
    walked: int = Field(description="Elements the walk read before filtering")
    complete: bool = Field(description="False when the walk stopped early (time or element limit): the screen "
                                       "has more than was read")
    offset: int = 0
    next_cursor: int | None = Field(None, description="Pass as cursor= to read the next page of this same walk")
    screen: dict | None = Field(None, description="Screen size in points {w, h}")
    method: str | None = None
    elapsed_ms: int | None = None
    stopped_by: str | None = Field(None, description="Why the walk ended: loop (whole screen read), max, timeout, "
                                                     "no-focus, foreign (focus left the app)")
    bundle_id: str | None = Field(None, description="The app that was read (detected in front, or the one asked for)")
    pid: int | None = Field(None, description="Its process id")
    other_process_elements: int | None = Field(None, description="Elements of other processes (system UI) that "
                                                                 "were left out")
    cached: bool = Field(False, description="True when this page came from the previous walk (cursor given)")
    next_steps: list[str] = Field(default_factory=list)


class ValueChange(Out):
    label: str | None = None
    before: str | None = None
    after: str | None = None


class UiPressResult(B.Budgeted, Out):
    pressed: UiElement
    added: list[str] = Field(default_factory=list, description="Labels that appeared after the press")
    removed: list[str] = Field(default_factory=list, description="Labels that went away")
    changed_values: list[ValueChange] = Field(default_factory=list, description="Elements whose value changed")
    elements_after: int | None = Field(None, description="Elements on screen after the press")
    after_complete: bool | None = Field(None, description="False when the walk after the press stopped early, so "
                                                          "the change summary may miss elements")
    elapsed_ms: int | None = None
    bundle_id: str | None = Field(None, description="The app that was pressed in")
    pid: int | None = None
    screen_changed: bool = Field(description="Anything was added, removed or changed")
    next_steps: list[str] = Field(default_factory=list)


class A11yIssue(Out):
    type: str = Field(description="contrast, dynamic-type, clipped-text, hit-region, missing-label, …")
    message: str
    label: str | None = Field(None, description="Label of the element with the problem")
    frame: list[int] | None = Field(None, description="[x, y, w, h] in screen points")
    raw_type: str | None = Field(None, description="Apple's audit type name, when it differs from type")
    details: dict | None = None


class A11yAudit(B.Budgeted, Out):
    counts: dict[str, int] = Field(description="Issues per type, most frequent first")
    total: int = Field(description="All issues found")
    count: int = Field(description="Issues listed below")
    issues: list[A11yIssue] = Field(description="Grouped by type, in the order of `counts`")
    supported: list[str] = Field(default_factory=list, description="Audit types this iPhone ran")
    unsupported: list[str] = Field(default_factory=list, description="Types this iOS version can't run "
                                                                     "(informational, not a failure)")
    elapsed_ms: int | None = None
    timed_out: bool | None = Field(None, description="True when the audit hit its time cap: results are partial")
    bundle_id: str | None = Field(None, description="The app that was audited")
    pid: int | None = None
    next_steps: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ conversion

def _txt(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return B.clip(" ".join(str(v).split()), TEXT_CUT)


def _frame(f: Any) -> list[int] | None:
    if not isinstance(f, dict):
        return None
    try:
        return [round(float(f.get(k) or 0)) for k in ("x", "y", "w", "h")]
    except (TypeError, ValueError):
        return None


def element(e: dict) -> UiElement:
    role = _txt(e.get("role"))
    traits = [str(t) for t in (e.get("traits") or []) if t and str(t) != role][:8]
    return UiElement(id=str(e.get("id") or "?"), role=role, label=_txt(e.get("label")), value=_txt(e.get("value")),
                     hint=_txt(e.get("hint")), identifier=_txt(e.get("identifier")), traits=traits,
                     frame=_frame(e.get("frame")), enabled=False if e.get("enabled") is False else None)


def is_interactive(e: dict) -> bool:
    words = [str(e.get("role") or "")] + [str(t) for t in e.get("traits") or []]
    low = " ".join(words).lower()
    return any(k in low for k in INTERACTIVE)


def filter_elements(items: list[dict], text: str | None = None, roles: list[str] | None = None,
                    interactive_only: bool = False) -> list[dict]:
    """Elements matching all given filters: `text` (case-insensitive substring of label, value, identifier or
    role), `roles` (role or a trait equal to one of them, case-insensitive), interactive_only."""
    needle = (text or "").strip().lower()
    want = {r.strip().lower() for r in roles or [] if r and r.strip()}
    out = []
    for e in items:
        if not isinstance(e, dict):
            continue
        if needle and not any(needle in str(e.get(k) or "").lower() for k in ("label", "value", "identifier", "role")):
            continue
        if want and not ({str(e.get("role") or "").lower()} | {str(t).lower() for t in e.get("traits") or []}) & want:
            continue
        if interactive_only and not is_interactive(e):
            continue
        out.append(e)
    return out


def _labels(xs: Any) -> tuple[list[str], int]:
    items = [s for s in (_txt(x) for x in xs or []) if s]
    return items[:CHANGES_CAP], max(0, len(items) - CHANGES_CAP)


def group_issues(raw: list[dict]) -> tuple[dict[str, int], list[dict]]:
    """(counts per type, most frequent first; issues re-ordered so each type's issues sit together in that order)."""
    counts: dict[str, int] = {}
    for i in raw:
        t = str(i.get("type") or "other")
        counts[t] = counts.get(t, 0) + 1
    order = sorted(counts, key=lambda t: -counts[t])          # stable: ties keep first-seen order
    rank = {t: n for n, t in enumerate(order)}
    ordered = sorted(raw, key=lambda i: rank[str(i.get("type") or "other")])
    return {t: counts[t] for t in order}, ordered


def issue(i: dict) -> A11yIssue:
    el = i.get("element") if isinstance(i.get("element"), dict) else {}
    t = str(i.get("type") or "other")
    raw = i.get("rawType")
    det = i.get("details") if isinstance(i.get("details"), dict) and i.get("details") else None
    return A11yIssue(type=t, message=B.clip(" ".join(str(i.get("message") or "").split()) or t, 240),
                     label=_txt(el.get("label")), frame=_frame(el.get("frame")),
                     raw_type=str(raw) if raw and str(raw) != t else None,
                     details=B.clip_data(det, 8) if det else None)


# ------------------------------------------------------------------ the walk cache

def _cache(state: AppState) -> dict:
    return state.extras.setdefault("ui_inspect", {})


def _forget(state: AppState) -> None:
    _cache(state).clear()


async def _walk(state: AppState, udid: str | None, bundle_id: str | None, max_elements: int,
                reuse: bool) -> tuple[dict, bool]:
    key = (udid or "", bundle_id or "", max_elements)
    c = _cache(state)
    hit = c.get("tree")
    if reuse and hit and hit["key"] == key and time.monotonic() - hit["at"] < CACHE_SECONDS:
        return hit["data"], True
    args: dict = {"max": max_elements}
    if bundle_id:
        args["bundleId"] = bundle_id
    data = await dcall(state, "ui.tree", args, udid=udid, timeout=45) or {}
    if not isinstance(data, dict):
        data = {}
    c["tree"] = {"key": key, "at": time.monotonic(), "data": data}
    return data, False


# ------------------------------------------------------------------ tools

def register(mcp: MCPServer) -> None:

    @tool(mcp, "Read the screen's UI tree", kind="read_remote")
    async def ios_ui_tree(
        ctx: Context,
        udid: Udid = None,
        bundle_id: BundleOpt = None,
        filter: Annotated[str | None, Field(description="Keep elements whose label, value, identifier or role "
                                                        "contains this text (case-insensitive)")] = None,
        roles: Annotated[list[str] | None, Field(description="Keep only these roles/traits, e.g. ['button', "
                                                             "'staticText'] (case-insensitive)")] = None,
        interactive_only: Annotated[bool, Field(description="Keep only things to press or edit: buttons, links, "
                                                            "text fields, switches, sliders, tabs, cells…")] = False,
        cursor: Annotated[int | None, Field(ge=0, description="next_cursor of the previous call: next page of the "
                                                              "same walk (no new walk)")] = None,
        max_bytes: Annotated[int | None, Field(ge=1000, le=B.MAX_BYTES, description="Raise the size cap of the "
                                                                                     "result (default ~12 KB)")] = None,
        max_elements: Annotated[int, Field(ge=1, le=TREE_MAX, description="Stop the walk after this many "
                                                                          "elements")] = 150,
    ) -> UiTree:
        """Read what is on the iPhone's screen as structured data: every accessibility element in VoiceOver
        reading order with its id, role, label, value, hint, identifier, traits and frame ([x, y, w, h] in
        points). Element positions may be unavailable on recent iOS (frame is then left out; ios_a11y_audit
        issues still have frames). bundle_id/pid in the result say which app was read. Use it to check that a change works without a human: build and launch the app (ios_run or
        ios_app_launch), call ios_ui_tree, press something with ios_ui_press(element_id=...), then call
        ios_ui_tree again (or ios_screenshot) to see the result. Pass bundle_id to make sure your app is the
        one in front. Ids stay valid until the screen changes. The walk takes about 5 s; complete=false
        means it stopped early and the screen has more elements. Results are paged (~12 KB): pass next_cursor
        as cursor to read on from the same walk; call without cursor to read the screen again. Read-only.
        Typing text, swipes and other gestures aren't supported."""
        state = get_state(ctx)
        offset = int(cursor or 0)
        data, cached = await _walk(state, udid, bundle_id, max_elements, reuse=cursor is not None)
        raw = [e for e in data.get("elements") or [] if isinstance(e, dict)]
        matched = filter_elements(raw, filter, roles, interactive_only)
        total = len(matched)
        if offset > total:
            raise ToolError(f"cursor={offset} is past the end ({total} elements). Call ios_ui_tree without cursor "
                            "to read the screen again.")
        items, nxt = page_of(matched, offset, TREE_MAX, element, max_bytes)
        complete = bool(data.get("complete", True)) and not data.get("truncated")

        def page_note(end: int) -> str:
            return (f"Showing elements {offset}–{end - 1} of {total}; call again with cursor={end} for more "
                    "(or narrow with filter/roles/interactive_only, or raise max_bytes).")
        walk_note = None
        stopped = data.get("stoppedBy")
        if data.get("truncated") or stopped == "max":
            complete = False
            walk_note = (f"The walk stopped at max_elements={max_elements}; the screen has more "
                         f"(raise max_elements, up to {TREE_MAX}).")
        elif stopped == "foreign":
            complete = False
            walk_note = ("VoiceOver focus left the app (into a system alert, a permission prompt or another app's "
                         "UI); the list is partial. Check with ios_screenshot and ask the user to answer any alert.")
        elif stopped == "no-focus":
            complete = False
            walk_note = ("VoiceOver focus couldn't move on this screen (an alert, the keyboard or a system sheet may "
                         "be up); the list is partial. Check with ios_screenshot.")
        elif not data.get("complete", True) or stopped == "timeout":
            complete = False
            walk_note = ("The walk timed out before reaching the end of the screen; the list is partial. Call "
                         "again, or filter to what you need.")
        steps: list[str] = []
        if not raw:
            steps.append("No elements were read. Make sure the iPhone is unlocked with the app in front "
                         "(ios_app_launch(bundle_id)), then ios_screenshot to see the screen.")
        elif not matched:
            steps.append("Nothing matched the filters; call again without filter/roles to see every element.")
        else:
            steps.append("ios_ui_press(element_id='<id>') presses an element; then call ios_ui_tree again "
                         "(or ios_screenshot) to check what changed.")
        model = UiTree(elements=items, count=len(items), total=total, walked=len(raw), complete=complete,
                       offset=offset, next_cursor=nxt, screen=data.get("screen") if isinstance(data.get("screen"), dict)
                       else None, method=data.get("method"), elapsed_ms=data.get("elapsedMs"), cached=cached,
                       stopped_by=stopped, bundle_id=data.get("bundleId") or bundle_id, pid=data.get("pid"),
                       other_process_elements=data.get("otherProcessElements") or None,
                       truncated=nxt is not None or not complete, omitted=total - offset - len(items),
                       next_steps=steps).with_budget(max_bytes)
        first = page_note(offset + len(items)) if nxt is not None else None
        model.note = " ".join(x for x in (first, walk_note) if x) or None
        # page_of() sized the page; fit_page() trims it further if the whole result is still over the cap
        model = fit_page(model, "elements", offset, lambda k, end: " ".join(
            x for x in (page_note(end), walk_note) if x))
        end = offset + model.count
        model.next_cursor = end if end < total else None
        model.omitted = total - end
        model.truncated = model.next_cursor is not None or not complete
        return model

    @tool(mcp, "Press a UI element", kind="write", open_world=True)
    async def ios_ui_press(
        ctx: Context,
        element_id: Annotated[str | None, Field(description="Element id from ios_ui_tree, e.g. 'e12'")] = None,
        label: Annotated[str | None, Field(description="Press the element with this label (or identifier, e.g. "
                                                        "'incrementButton') instead of an id")] = None,
        match: Annotated[Literal["exact", "contains"], Field(description="exact: the whole label, or the "
                                                                         "accessibilityIdentifier; contains: a part "
                                                                         "of the label")] = "exact",
        bundle_id: BundleOpt = None,
        udid: Udid = None,
    ) -> UiPressResult:
        """Press (activate) one element on the iPhone's screen, by element_id from ios_ui_tree or by label,
        then report what changed: labels that appeared or went away and values that changed. This taps the
        REAL phone, in whatever app is in front: pass bundle_id so it fails (wrong-app) instead of pressing
        something in another app. Give exactly one of element_id or label. An id goes stale when the screen
        changes (call ios_ui_tree again); a label that matches several elements fails with their ids.
        match='exact' also matches the accessibilityIdentifier. Pressing by element_id from a fresh ios_ui_tree is
        faster (about 8 s) than by label (about 12 s, it reads the screen first). In changed_values, `label` may
        be the element's identifier when the label itself changed (countLabel: 'Count: 0' -> 'Count: 1'). Check
        the result with ios_ui_tree or ios_screenshot. Typing text, swipes and other gestures aren't supported."""
        if bool(element_id and element_id.strip()) == bool(label and label.strip()):
            raise ToolError("Give exactly one of element_id (from ios_ui_tree) or label.")
        state = get_state(ctx)
        args: dict = {"id": element_id.strip()} if element_id and element_id.strip() else \
            {"label": label.strip(), "match": match}
        if bundle_id:
            args["bundleId"] = bundle_id
        try:
            data = await dcall(state, "ui.press", args, udid=udid, timeout=40) or {}
        finally:
            _forget(state)              # pressed or not, the screen may have moved: no paging an old walk
        ch = data.get("changes") or {}
        after = data.get("after") if isinstance(data.get("after"), dict) else {}
        added, a_more = _labels(ch.get("added"))
        removed, r_more = _labels(ch.get("removed"))
        vals_raw = [v for v in ch.get("changedValues") or [] if isinstance(v, dict)]
        vals = [ValueChange(label=_txt(v.get("label")), before=_txt(v.get("from")), after=_txt(v.get("to")))
                for v in vals_raw[:CHANGES_CAP]]
        v_more = max(0, len(vals_raw) - CHANGES_CAP)
        omitted = a_more + r_more + v_more
        changed = bool(added or removed or vals or omitted)
        pressed = data.get("pressed") if isinstance(data.get("pressed"), dict) else {"id": element_id or "?",
                                                                                     "label": label}
        steps = ["Call ios_ui_tree to read the new screen (old ids may be stale), or ios_screenshot to see it."]
        if not changed:
            steps.insert(0, "Nothing visible changed yet. The app may still be loading: wait a moment and call "
                            "ios_ui_tree, or check that this was the right element.")
        return UiPressResult(
            pressed=element(pressed), added=added, removed=removed, changed_values=vals,
            elements_after=after.get("count"), screen_changed=changed, omitted=omitted,
            after_complete=after.get("complete"), elapsed_ms=data.get("elapsedMs"),
            bundle_id=data.get("bundleId") or bundle_id, pid=data.get("pid"),
            truncated=omitted > 0 or after.get("complete") is False,
            note=" ".join(x for x in (
                f"{omitted} more changes not listed; ios_ui_tree shows the whole screen." if omitted else None,
                "The walk after the press stopped early, so changes further down may be missing."
                if after.get("complete") is False else None) if x) or None,
            next_steps=steps)

    @tool(mcp, "Accessibility audit", kind="read_remote")
    async def ios_a11y_audit(
        ctx: Context,
        udid: Udid = None,
        bundle_id: BundleOpt = None,
        types: Annotated[list[str] | None, Field(description="Only these issue types, e.g. ['contrast', "
                                                             "'dynamic-type', 'clipped-text', 'hit-region', "
                                                             "'missing-label']; default: all")] = None,
        max_issues: Annotated[int, Field(ge=1, le=200, description="Issues listed (counts cover all)")] = 50,
    ) -> A11yAudit:
        """Run Apple's accessibility audit on the screen showing now: low contrast, Dynamic Type problems,
        clipped text, hit areas that are too small, missing labels and more. Returns counts per type, a capped
        list of issues with the element's label and frame, and the types this iOS version can't check
        (reported as unsupported, not as an error). The app must be in front: launch it with
        ios_app_launch(bundle_id) and pass bundle_id here. Only audits the current screen; navigate with
        ios_ui_press to audit others. Read-only."""
        state = get_state(ctx)
        args: dict = {}
        if bundle_id:
            args["bundleId"] = bundle_id
        if types:
            args["types"] = [t.strip() for t in types if t and t.strip()]
        data = await dcall(state, "ui.audit", args, udid=udid, timeout=60) or {}
        raw = [i for i in data.get("issues") or [] if isinstance(i, dict)]
        counts, ordered = group_issues(raw)
        shown = [issue(i) for i in ordered[:max_issues]]
        unsupported = [str(t) for t in data.get("unsupported") or []]
        steps: list[str] = []
        if raw:
            steps.append("Fix the issues in the app's SwiftUI/UIKit code (labels: .accessibilityLabel, Dynamic Type: "
                         "system text styles, hit regions: at least 44×44 pt), rebuild with ios_run and audit again.")
        elif not data.get("timedOut"):
            steps.append("No issues on this screen. Navigate with ios_ui_press and audit the next screen.")
        if unsupported:
            steps.append(f"Not checked on this iOS version: {', '.join(unsupported)} (informational).")
        timed_out = bool(data.get("timedOut"))
        partial = ("The audit hit its time cap, so these results are partial; audit again, or pass types=[...] to "
                   "check fewer kinds at a time.") if timed_out else None
        cut = (f"Showing {len(shown)} of {len(raw)} issues; counts cover all of them. Pass types=[...] to list one "
               "kind, or raise max_issues.") if len(shown) < len(raw) else None
        model = A11yAudit(counts=counts, total=len(raw), count=len(shown), issues=shown,
                          supported=[str(t) for t in data.get("supported") or []], unsupported=unsupported,
                          elapsed_ms=data.get("elapsedMs"), timed_out=timed_out or None,
                          bundle_id=data.get("bundleId") or bundle_id, pid=data.get("pid"), omitted=len(raw) - len(shown),
                          truncated=len(shown) < len(raw) or timed_out,
                          note=" ".join(x for x in (cut, partial) if x) or None, next_steps=steps)
        return fit_page(model, "issues", 0, lambda k, end: " ".join(x for x in (
            f"Showing {k} of {len(raw)} issues to fit the size cap; counts cover all of them. Pass types=[...] "
            "to list one kind.", partial) if x))
