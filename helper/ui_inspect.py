"""UI inspection logic (ui.tree / ui.press / ui.audit), with no device and no pymobiledevice3 import.

The daemon feature (deviced_features/ui.py) walks the accessibility focus of the foreground app
with pymobiledevice3's AccessibilityAudit service and hands each raw focus event here, already
turned into plain JSON (`to_plain`). Everything that shapes the output lives in this module so it
can be unit-tested against captured payloads (tests/fixtures/ui-inspect/):

  to_plain(obj)                     pymobiledevice3 SerializedObject trees -> JSON-able dicts
  Walk                              collects focus events, stops at the first element seen again
  normalize_element(raw, attrs, id) one Element dict (role, label, value, hint, traits, frame, ...)
  find_by_label(elements, label)    exact / contains, case-insensitive; close labels for errors
  diff_trees(before, after)         added / removed labels and changed values
  normalize_issue(raw, elements)    one audit issue (friendly type slug, message, element)
  audit_plan(requested, supported)  which raw audit types to run; unsupported slugs
"""
from __future__ import annotations

import difflib
import re
from collections import Counter

DEFAULT_MAX = 150
HARD_MAX = 400
DEFAULT_TIMEOUT = 12.0
MAX_TIMEOUT = 20.0

# ---------------------------------------------------------------- raw payloads


def to_plain(obj, depth: int = 0):
    """pymobiledevice3 accessibility objects (SerializedObject subclasses with `_fields`) as plain
    JSON: {"_type": "AXAuditInspectorFocus_v1", <field>: ...}; bytes as {"_bytes": "<hex>"}."""
    if depth > 30:
        return None
    if isinstance(obj, (bytes, bytearray)):
        return {"_bytes": bytes(obj).hex()}
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    fields = getattr(obj, "_fields", None)
    if isinstance(fields, dict):
        return {"_type": type(obj).__name__, **{str(k): to_plain(v, depth + 1) for k, v in fields.items()}}
    if isinstance(obj, dict):
        return {str(k): to_plain(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_plain(v, depth + 1) for v in obj]
    return repr(obj)


def element_bytes(raw) -> bytes | None:
    """The platform element value (what perform_press needs) inside a plain focus event."""
    el = raw.get("ElementValue_v1") if isinstance(raw, dict) else None
    if isinstance(el, dict):
        pv = el.get("PlatformElementValue_v1")
        if isinstance(pv, dict) and "_bytes" in pv:
            return bytes.fromhex(pv["_bytes"])
    return None


def element_pid(b: bytes | None) -> int | None:
    """The process an element belongs to: the first 4 bytes (little-endian) of its platform element
    value (checked on iOS 27.3: they equal the app's pid)."""
    if not b or len(b) < 4:
        return None
    return int.from_bytes(b[:4], "little")


def element_key(raw) -> str | None:
    b = element_bytes(raw)
    return b.hex() if b is not None else None


class Walk:
    """Accumulates focus events in order. add() returns False at the first element seen before
    (the focus wrapped around) or once `limit` elements are in."""

    def __init__(self, limit: int = DEFAULT_MAX):
        self.limit = limit
        self.items: list[dict] = []
        self._seen: set[str] = set()
        self.looped = False

    def add(self, raw) -> bool:
        key = element_key(raw)
        if key is None:   # a focus event without an element (e.g. the app itself): skip it
            return not self.full
        if key in self._seen:
            self.looped = True
            return False
        self._seen.add(key)
        self.items.append(raw)
        return not self.full

    @property
    def full(self) -> bool:
        return len(self.items) >= self.limit


# ---------------------------------------------------------------- elements

# UIAccessibilityTraits bits (public values from UIKit's UIAccessibilityConstants.h)
TRAIT_BITS = [
    (1 << 0, "button"), (1 << 1, "link"), (1 << 2, "image"), (1 << 3, "selected"),
    (1 << 4, "playsSound"), (1 << 5, "keyboardKey"), (1 << 6, "staticText"), (1 << 7, "summaryElement"),
    (1 << 8, "notEnabled"), (1 << 9, "updatesFrequently"), (1 << 10, "searchField"),
    (1 << 11, "startsMediaSession"), (1 << 12, "adjustable"), (1 << 13, "allowsDirectInteraction"),
    (1 << 14, "causesPageTurn"), (1 << 15, "tabBar"), (1 << 16, "header"), (1 << 53, "toggle"),
]
# first trait that names a role wins
ROLE_ORDER = ["searchField", "toggle", "button", "link", "adjustable", "tabBar", "keyboardKey",
              "header", "image", "staticText"]
# words VoiceOver appends to a caption, mapped to traits
CAPTION_TRAITS = {
    "button": "button", "link": "link", "image": "image", "heading": "header", "header": "header",
    "selected": "selected", "dimmed": "notEnabled", "search field": "searchField", "adjustable": "adjustable",
    "switch button": "toggle", "toggle": "toggle", "tab": "tabBar", "text field": "textField",
    "secure text field": "textField", "static text": "staticText",
}
ROLE_FROM_TRAIT = {"textField": "textField"}

ATTR_NAMES = {
    "label": ("Label", "AXLabel", "label"),
    "value": ("Value", "AXValue", "value"),
    "hint": ("Hint", "AXHint", "hint"),
    "identifier": ("Identifier", "AXIdentifier", "identifier"),
    "traits": ("Traits", "AXTraits", "traits"),
    "frame": ("Frame", "AXFrame", "frame"),
    "enabled": ("Enabled", "AXEnabled", "enabled"),
}

_NUM = r"(-?\d+(?:\.\d+)?(?:e[-+]?\d+)?)"
_RECT = re.compile(r"\{\{\s*" + _NUM + r"\s*,\s*" + _NUM + r"\s*\}\s*,\s*\{\s*" + _NUM + r"\s*,\s*" + _NUM + r"\s*\}\}", re.I)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else round(f, 1)


def parse_rect(v) -> dict | None:
    """'{{x, y}, {w, h}}', [[x, y], [w, h]], [x, y, w, h] or {x,y,w,h | X,Y,Width,Height} -> {x,y,w,h}."""
    if v is None:
        return None
    if isinstance(v, dict) and "_bytes" not in v and "_type" not in v:
        low = {str(k).lower(): x for k, x in v.items()}
        vals = [low.get(k) for k in ("x", "y")] + [low.get("w", low.get("width")), low.get("h", low.get("height"))]
        if all(x is not None for x in vals):
            v = vals
        elif "origin" in low and "size" in low:
            return parse_rect([low["origin"], low["size"]])
        else:
            return None
    if isinstance(v, str):
        m = _RECT.search(v)
        if not m:
            return None
        vals = list(m.groups())
    elif isinstance(v, (list, tuple)):
        flat = []
        for x in v:
            flat.extend(x if isinstance(x, (list, tuple)) else [x])
        if len(flat) != 4:
            return None
        vals = flat
    else:
        return None
    nums = [_num(x) for x in vals]
    if any(n is None for n in nums):
        return None
    x, y, w, h = nums
    return {"x": x, "y": y, "w": w, "h": h}


def parse_traits(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, bool):
        return []
    if isinstance(v, int):
        return [name for bit, name in TRAIT_BITS if v & bit]
    if isinstance(v, str):
        parts = [p.strip() for p in re.split(r"[,|]", v) if p.strip()]
        out = []
        for p in parts:
            key = re.sub(r"^(UIAccessibilityTrait|AXTrait)", "", p)
            key = key[:1].lower() + key[1:]
            out.append(CAPTION_TRAITS.get(p.lower(), key))
        return _dedupe(out)
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out.extend(parse_traits(x))
        return _dedupe(out)
    return []


def _dedupe(xs):
    seen, out = set(), []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def collect_attributes(raw) -> dict:
    """Name -> value for every attribute record nested in a focus event (inspector sections), if the
    device sends any. Keys are the attribute names as sent (e.g. 'Label')."""
    out: dict = {}

    def visit(node, depth=0):
        if depth > 25:
            return
        if isinstance(node, dict):
            name = node.get("AttributeNameValue_v1") or node.get("HumanReadableNameValue_v1")
            if isinstance(name, str):
                for k in ("ValueValue_v1", "CurrentValue_v1", "AttributeValue_v1", "Value_v1", "value"):
                    if k in node:
                        out.setdefault(name, node[k])
                        break
            for k, v in node.items():
                if k != "ElementValue_v1":
                    visit(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                visit(v, depth + 1)
    visit(raw)
    return out


def _pick(attrs: dict, field: str):
    for name in ATTR_NAMES[field]:
        if name in attrs and attrs[name] not in (None, ""):
            return attrs[name]
    return None


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v if x not in (None, ""))
    if isinstance(v, dict):
        return None
    s = " ".join(str(v).split())
    return s or None


def split_caption(caption: str | None) -> tuple[str | None, list[str]]:
    """'Increment, Button' -> ('Increment', ['button']): VoiceOver appends trait words."""
    if not caption:
        return None, []
    parts = [p.strip() for p in str(caption).split(",")]
    traits = []
    while len(parts) > 1 and parts[-1].lower() in CAPTION_TRAITS:
        traits.insert(0, CAPTION_TRAITS[parts.pop().lower()])
    label = ", ".join(p for p in parts if p) or None
    return label, traits


def role_for(traits: list[str]) -> str | None:
    for r in ROLE_ORDER + ["textField"]:
        if r in traits:
            return ROLE_FROM_TRAIT.get(r, r)
    return None


def normalize_element(raw: dict, attrs: dict | None, eid: str) -> dict:
    """One Element from a plain focus event plus any attributes read for it (attrs may be None)."""
    attrs = {**collect_attributes(raw), **(attrs or {})}
    caption = raw.get("CaptionTextValue_v1") if isinstance(raw, dict) else None
    el = raw.get("ElementValue_v1") if isinstance(raw, dict) else None
    ax_id = el.get("AccessibilityIdentifier_v1") if isinstance(el, dict) else None
    cap_label, cap_traits = split_caption(caption)
    traits = _dedupe(parse_traits(_pick(attrs, "traits")) + cap_traits)
    label = _text(_pick(attrs, "label")) or cap_label
    value = _text(_pick(attrs, "value"))
    enabled = _pick(attrs, "enabled")
    if isinstance(enabled, bool):
        pass
    elif isinstance(enabled, int):
        enabled = bool(enabled)
    else:
        enabled = False if "notEnabled" in traits else (True if traits else None)
    return {
        "id": eid,
        "role": role_for(traits),
        "label": label,
        "value": value,
        "hint": _text(_pick(attrs, "hint")),
        "identifier": _text(_pick(attrs, "identifier")) or _text(ax_id),
        "traits": traits,
        "frame": parse_rect(_pick(attrs, "frame")),
        "enabled": enabled,
    }


# ---------------------------------------------------------------- labels

def _norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def find_by_label(elements: list[dict], label: str, match: str = "exact") -> list[dict]:
    """Elements whose label (or identifier) matches, case-insensitively. 'exact' or 'contains'."""
    want = _norm(label)
    if not want:
        return []
    out = []
    for e in elements:
        names = [_norm(e.get("label")), _norm(e.get("identifier"))]
        if match == "contains":
            hit = any(want in n for n in names if n)
        else:
            hit = want in names
        if hit:
            out.append(e)
    if match == "exact" and len(out) > 1:   # prefer label hits over identifier hits
        by_label = [e for e in out if _norm(e.get("label")) == want]
        out = by_label or out
    return out


def close_labels(elements: list[dict], label: str, n: int = 5) -> list[str]:
    labels = _dedupe([e.get("label") for e in elements if e.get("label")])
    want = _norm(label)
    near = [lab for lab in labels if want and (want in _norm(lab) or _norm(lab) in want)]
    by_norm = {_norm(lab): lab for lab in labels}
    near += [by_norm[x] for x in difflib.get_close_matches(want, list(by_norm), n=n, cutoff=0.5)]
    near = _dedupe(near)
    return (near or labels)[:n]


def describe_matches(elements: list[dict], n: int = 8) -> str:
    return ", ".join(f"{e['id']} {e.get('label')!r}" + (f" ({e['role']})" if e.get("role") else "")
                     for e in elements[:n])


# ---------------------------------------------------------------- diff

def _name(e) -> str | None:
    return e.get("label") or e.get("identifier") or e.get("value")


def diff_trees(before: list[dict], after: list[dict], cap: int = 25) -> dict:
    """What changed between two walks of the same screen.

    Elements with the same identifier whose label or value changed, and elements with the same label
    (and role) whose value changed, are 'changedValues'; the rest compare as label multisets."""
    changed = []
    used_b, used_a = set(), set()

    def pair(key_fn):
        idx: dict = {}
        for i, e in enumerate(before):
            k = key_fn(e)
            if k and i not in used_b:
                idx.setdefault(k, []).append(i)
        for j, e in enumerate(after):
            k = key_fn(e)
            if not k or j in used_a or not idx.get(k):
                continue
            i = idx[k].pop(0)
            used_b.add(i)
            used_a.add(j)
            b = before[i]
            if (b.get("label"), b.get("value")) != (e.get("label"), e.get("value")):
                if b.get("label") != e.get("label"):   # the label itself changed: name it by identifier
                    frm, to = b.get("label"), e.get("label")
                    name = e.get("identifier") or e.get("label")
                else:
                    frm, to = b.get("value"), e.get("value")
                    name = e.get("label") or e.get("identifier")
                changed.append({"label": name, "from": frm, "to": to})

    pair(lambda e: ("id", e["identifier"]) if e.get("identifier") else None)
    pair(lambda e: ("lr", _norm(e.get("label")), e.get("role")) if e.get("label") else None)
    rest_b = Counter(_name(e) for i, e in enumerate(before) if i not in used_b and _name(e))
    rest_a = Counter(_name(e) for j, e in enumerate(after) if j not in used_a and _name(e))
    added = list((rest_a - rest_b).elements())
    removed = list((rest_b - rest_a).elements())
    return {"added": added[:cap], "removed": removed[:cap], "changedValues": changed[:cap]}


# ---------------------------------------------------------------- audit

# raw audit classification codes (pymobiledevice3 AuditType) and names -> friendly slugs
AUDIT_CODES = {
    12: "contrast", 13: "contrast", 100: "hit-region", 1000: "element-detection",
    3001: "dynamic-type", 3002: "dynamic-type", 3003: "clipped-text", 5000: "missing-label",
}
AUDIT_NAMES = {
    "testTypeContrast": "contrast", "testTypeHitRegion": "hit-region",
    "testTypeElementDetection": "element-detection", "testTypeDynamicText": "dynamic-type",
    "testTypeTextClipped": "clipped-text", "testTypeSufficientElementDescription": "missing-label",
    "testTypeTrait": "traits", "testTypeAction": "actions", "testTypeParentChild": "parent-child",
    "testTypeHasLabel": "missing-label", "testTypeLabel": "missing-label",
}
AUDIT_MESSAGES = {
    "contrast": "The text or icon contrast against its background is too low.",
    "hit-region": "The touch target is smaller than recommended (44×44 points).",
    "element-detection": "Something on screen isn't reachable by VoiceOver.",
    "dynamic-type": "The text doesn't grow with the Dynamic Type text size setting.",
    "clipped-text": "The text is cut off (clipped) at larger text sizes.",
    "missing-label": "The element has no useful accessibility label.",
    "traits": "The element's accessibility traits look wrong.",
    "actions": "The element's accessibility actions look wrong.",
    "parent-child": "The element hierarchy looks wrong to accessibility.",
}


def audit_slug(raw) -> str:
    if isinstance(raw, bool):
        return str(raw)
    if isinstance(raw, int):
        return AUDIT_CODES.get(raw, f"type-{raw}")
    s = str(raw)
    if s.isdigit():
        return audit_slug(int(s))
    if s in AUDIT_NAMES:
        return AUDIT_NAMES[s]
    if s.startswith("testType"):
        t = re.sub(r"(?<!^)(?=[A-Z])", "-", s[len("testType"):]).lower()
        return t or s
    return s


def raw_type_name(raw) -> str:
    """A readable raw type: 'testTypeContrast' for known codes, the value as text otherwise."""
    names = {12: "testTypeContrast", 13: "testTypeContrast", 100: "testTypeHitRegion",
             1000: "testTypeElementDetection", 3001: "testTypeDynamicText", 3002: "testTypeDynamicText",
             3003: "testTypeTextClipped", 5000: "testTypeSufficientElementDescription"}
    if isinstance(raw, int) and not isinstance(raw, bool):
        return names.get(raw, str(raw))
    return str(raw)


def audit_plan(requested: list | None, supported_raw: list) -> tuple[list, list[str], list[str]]:
    """(raw types to run, slugs that will run, requested slugs the device doesn't support)."""
    by_slug: dict[str, list] = {}
    for r in supported_raw:
        by_slug.setdefault(audit_slug(r), []).append(r)
    if not requested:
        run = list(supported_raw)
        return run, _dedupe([audit_slug(r) for r in run]), []
    run, slugs, missing = [], [], []
    for want in requested:
        slug = audit_slug(want)
        hits = by_slug.get(slug) or [r for r in supported_raw if str(r) == str(want)]
        if hits:
            run.extend(h for h in hits if h not in run)
            slugs.append(slug)
        else:
            missing.append(slug)
    return run, _dedupe(slugs), _dedupe(missing)


def _overlap(a: dict, b: dict) -> float:
    ix = max(0.0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0.0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def element_at(elements: list[dict], rect: dict | None, min_overlap: float = 0.5) -> dict | None:
    """The walked element whose frame best matches an audit issue's rect."""
    if not rect:
        return None
    best, score = None, min_overlap
    for e in elements:
        f = e.get("frame")
        if f:
            s = _overlap(f, rect)
            if s >= score:
                best, score = e, s
    return best


def _color(v):
    if v is None:
        return None
    if isinstance(v, (str, int, float)):
        return v
    if isinstance(v, dict) and "_bytes" in v:
        return None
    if isinstance(v, (list, tuple)) and all(isinstance(x, (int, float)) for x in v):
        return [round(float(x), 3) for x in v]
    return str(v)[:80]


def first_sentences(text: str, limit: int) -> str:
    """Whole sentences of `text` up to `limit` characters (at least the first one, cut if needed)."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out = ""
    for p in parts:
        if out and len(out) + 1 + len(p) > limit:
            break
        out = f"{out} {p}".strip()
    if len(out) > limit:
        out = out[:limit - 1].rstrip() + "…"
    return out if out[-1:] in ".!?…" else out + "."


def normalize_issue(raw: dict, elements: list[dict] | None = None, ids: dict | None = None) -> dict:
    """One audit issue from a plain AXAuditIssue_v1 (to_plain output). The issue's element is found
    in the last walk by its element bytes (ids: id -> bytes), else by frame overlap."""
    code = raw.get("IssueClassificationValue_v1")
    name = raw.get("auditTestTypeValue_v1")
    slug = audit_slug(name) if name else (audit_slug(code) if code is not None else "unknown")
    rect = parse_rect(raw.get("ElementRectValue_v1"))
    el = None
    b = element_bytes({"ElementValue_v1": raw.get("AuditElementValue_v1")})
    if b is not None and ids:
        eid = next((i for i, v in ids.items() if v == b), None)
        el = next((e for e in elements or [] if e["id"] == eid), None) if eid else None
    if el is None:
        el = element_at(elements or [], rect)
    audit_el = raw.get("AuditElementValue_v1") if isinstance(raw.get("AuditElementValue_v1"), dict) else {}
    desc = raw.get("MLGeneratedDescriptionValue_v1")
    text = raw.get("ElementTextValue_v1")
    label = (el or {}).get("label") or _text(text) or _text(audit_el.get("AccessibilityIdentifier_v1")) \
        or (_text(desc) if isinstance(desc, str) and len(desc) < 80 else None)
    extra = raw.get("ElementLongDescExtraInfo_v1")
    if isinstance(extra, list):
        extra = "; ".join(str(x) for x in extra if x not in (None, ""))
    msg = AUDIT_MESSAGES.get(slug, "Accessibility issue.")
    if isinstance(extra, str) and extra:
        msg = f"{msg} {first_sentences(extra, 120)}"
    details = {k: v for k, v in {
        "code": code,
        "fontSize": _num(raw.get("FontSizeValue_v1")) or None,
        "foreground": _color(raw.get("ForegroundColorValue_v1")),
        "background": _color(raw.get("BackgroundColorValue_v1")),
        "description": _text(desc)[:200] if isinstance(desc, str) else None,
        "extra": extra[:200] if isinstance(extra, str) and extra else None,
        "elementId": (el or {}).get("id"),
        "identifier": (el or {}).get("identifier") or _text(audit_el.get("AccessibilityIdentifier_v1")),
    }.items() if v is not None}
    element = {"label": label, "frame": rect} if (label or rect) else None
    return {"type": slug, "rawType": str(name) if name else raw_type_name(code), "message": msg,
            "element": element, "details": details}


# ---------------------------------------------------------------- targets and apps

class TargetError(Exception):
    """A press target that can't be resolved: code, short message, detail (a list for the caller)."""

    def __init__(self, code: str, message: str, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


def pick_target(elements: list[dict], eid: str | None = None, label: str | None = None,
                match: str = "exact") -> dict:
    """The element to press, by id or label. Raises TargetError(no-element | ambiguous-label |
    stale-element | bad-args). Lists go in .detail (close labels, or matches as {id, label, role})."""
    if eid:
        for e in elements:
            if e["id"] == eid:
                return e
        raise TargetError("stale-element", f"No element {eid} in the last walk. Read the screen again for fresh ids.")
    match = match or "exact"
    if match not in ("exact", "contains"):
        raise TargetError("bad-args", "match must be 'exact' or 'contains'.")
    hits = find_by_label(elements, str(label or ""), match)
    if not hits:
        near = close_labels(elements, str(label or ""))
        raise TargetError("no-element", f"No element labelled {str(label)[:60]!r}."
                          + (f" Close: {', '.join(repr(x[:40]) for x in near[:3])}." if near else ""), near)
    if len(hits) > 1:
        raise TargetError("ambiguous-label", f"{len(hits)} elements match {str(label)[:60]!r}; press one by id.",
                          [{"id": e["id"], "label": e.get("label"), "role": e.get("role")} for e in hits[:12]])
    return hits[0]


def match_bundle(bundle_ids, want: str) -> str | None:
    """The installed bundle id for `want`: exact, or the plain/`XTL-<TEAM>.` form of a free team's id."""
    want = str(want or "").strip()
    ids = [str(b) for b in bundle_ids]
    if want in ids:
        return want
    plain = want.split(".", 1)[-1] if want.startswith("XTL-") else want
    for b in ids:
        if b == plain or b.endswith("." + plain) and b.startswith("XTL-"):
            return b
    return None
