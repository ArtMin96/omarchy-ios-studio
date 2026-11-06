"""UI inspection (helper/ui_inspect.py): raw payload conversion, the focus walk, element shaping,
label matching, tree diffs, audit normalization; plus the demo daemon's ui.* commands.

    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "helper"))

import ui_inspect as U  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ui-inspect"


def load(name):
    return json.loads((FIX / name).read_text())


def walk_elements(fx=None):
    fx = fx or load("synthetic-walk.json")
    return [U.normalize_element(x["focus"], x["attrs"], f"e{i}") for i, x in enumerate(fx["walk"], 1)]


class Fake:
    """Stand-in for pymobiledevice3 SerializedObject subclasses."""
    def __init__(self, fields):
        self._fields = fields


class Plain(unittest.TestCase):
    def test_serialized_objects_and_bytes(self):
        obj = Fake({"CaptionTextValue_v1": "Go, Button",
                    "ElementValue_v1": Fake({"PlatformElementValue_v1": b"\x01\x02"}), "n": [1, (2, 3)]})
        p = U.to_plain(obj)
        self.assertEqual(p["_type"], "Fake")
        self.assertEqual(p["ElementValue_v1"]["PlatformElementValue_v1"], {"_bytes": "0102"})
        self.assertEqual(p["n"], [1, [2, 3]])
        self.assertEqual(U.element_bytes(p), b"\x01\x02")
        json.dumps(p)

    def test_no_element(self):
        self.assertIsNone(U.element_bytes({"CaptionTextValue_v1": "x"}))
        self.assertIsNone(U.element_key("nope"))


class Walking(unittest.TestCase):
    def test_stops_at_first_repeat(self):
        fx = load("synthetic-walk.json")
        w = U.Walk(100)
        for x in fx["walk"]:
            self.assertTrue(w.add(x["focus"]))
        self.assertFalse(w.add(fx["loopsBackTo"]["focus"]))
        self.assertTrue(w.looped)
        self.assertEqual(len(w.items), len(fx["walk"]))

    def test_stops_at_max(self):
        fx = load("synthetic-walk.json")
        w = U.Walk(3)
        results = [w.add(x["focus"]) for x in fx["walk"][:3]]
        self.assertEqual(results, [True, True, False])
        self.assertTrue(w.full)
        self.assertFalse(w.looped)

    def test_skips_events_without_element(self):
        w = U.Walk(5)
        self.assertTrue(w.add({"CaptionTextValue_v1": "app"}))
        self.assertEqual(w.items, [])


class Elements(unittest.TestCase):
    def test_shapes_from_fixture(self):
        els = walk_elements()
        head, count, inc, toggle, reset = els[0], els[1], els[3], els[6], els[8]
        self.assertEqual((head["role"], head["label"]), ("header", "UI Probe"))
        self.assertEqual(count["identifier"], "countLabel")
        self.assertEqual(count["role"], "staticText")
        self.assertEqual(inc["role"], "button")
        self.assertEqual(inc["hint"], "Adds one to the count")
        self.assertEqual(inc["frame"], {"x": 202, "y": 170, "w": 120, "h": 34})
        self.assertTrue(inc["enabled"])
        self.assertEqual(toggle["role"], "toggle")
        self.assertEqual(toggle["value"], "1")
        self.assertEqual(reset["label"], "Reset")   # from the caption when Label didn't answer
        for e in els:
            self.assertEqual(set(e), {"id", "role", "label", "value", "hint", "identifier", "traits", "frame", "enabled"})

    def test_caption_only(self):
        e = U.normalize_element({"CaptionTextValue_v1": "Save, Dimmed, Button"}, None, "e1")
        self.assertEqual(e["label"], "Save")
        self.assertEqual(e["role"], "button")
        self.assertIn("notEnabled", e["traits"])
        self.assertFalse(e["enabled"])
        self.assertIsNone(e["frame"])

    def test_rects(self):
        self.assertEqual(U.parse_rect("{{1.5, 2}, {3, 4.25}}"), {"x": 1.5, "y": 2, "w": 3, "h": 4.2})
        self.assertEqual(U.parse_rect([[0, 0], [10, 20]]), {"x": 0, "y": 0, "w": 10, "h": 20})
        self.assertEqual(U.parse_rect({"X": 1, "Y": 2, "Width": 3, "Height": 4}), {"x": 1, "y": 2, "w": 3, "h": 4})
        self.assertIsNone(U.parse_rect("garbage"))
        self.assertIsNone(U.parse_rect([1, 2, 3]))

    def test_traits(self):
        self.assertEqual(U.parse_traits(1 | (1 << 16)), ["button", "header"])
        self.assertEqual(U.parse_traits("UIAccessibilityTraitButton, Selected"), ["button", "selected"])
        self.assertEqual(U.parse_traits(None), [])

    def test_attributes_in_sections(self):
        raw = {"CaptionTextValue_v1": "x", "Sections": [{"_type": "AXAuditInspectorSection_v1", "Items": [
            {"AttributeNameValue_v1": "Label", "ValueValue_v1": "Deep label"},
            {"AttributeNameValue_v1": "Identifier", "ValueValue_v1": "deep"}]}]}
        e = U.normalize_element(raw, None, "e1")
        self.assertEqual((e["label"], e["identifier"]), ("Deep label", "deep"))


class Labels(unittest.TestCase):
    def setUp(self):
        self.els = walk_elements()

    def test_exact_case_insensitive(self):
        self.assertEqual([e["id"] for e in U.find_by_label(self.els, "  increment ")], ["e4"])
        self.assertEqual([e["id"] for e in U.find_by_label(self.els, "incrementButton")], ["e4"])

    def test_contains(self):
        self.assertEqual({e["label"] for e in U.find_by_label(self.els, "crement", "contains")},
                         {"Increment", "Decrement"})

    def test_pick_target_errors_carry_lists(self):
        with self.assertRaises(U.TargetError) as c:
            U.pick_target(self.els, label="Incremnt")
        self.assertEqual(c.exception.code, "no-element")
        self.assertIn("Increment", c.exception.detail)
        self.assertLess(len(c.exception.message), 300)
        with self.assertRaises(U.TargetError) as c:
            U.pick_target(self.els, label="crement", match="contains")
        self.assertEqual(c.exception.code, "ambiguous-label")
        self.assertEqual({d["id"] for d in c.exception.detail}, {"e3", "e4"})
        with self.assertRaises(U.TargetError) as c:
            U.pick_target(self.els, eid="e99")
        self.assertEqual(c.exception.code, "stale-element")
        self.assertEqual(U.pick_target(self.els, eid="e5")["label"], "Add item")

    def test_match_bundle(self):
        ids = ["XTL-2WX6B428SL.dev.iosstudio.uiprobe", "com.apple.mobilesafari"]
        self.assertEqual(U.match_bundle(ids, "dev.iosstudio.uiprobe"), ids[0])
        self.assertEqual(U.match_bundle(ids, ids[0]), ids[0])
        self.assertEqual(U.match_bundle(ids, "XTL-OTHERTEAM.dev.iosstudio.uiprobe"), ids[0])
        self.assertEqual(U.match_bundle(ids, "com.apple.mobilesafari"), ids[1])
        self.assertIsNone(U.match_bundle(ids, "iosstudio.uiprobe.x"))
        self.assertIsNone(U.match_bundle(ids, "mobilesafari"))


class Diff(unittest.TestCase):
    def test_added_row_and_changed_label(self):
        before = walk_elements()
        after = [dict(e) for e in before]
        after[1] = {**after[1], "label": "Count: 1"}
        after.insert(6, {**after[5], "id": "e99", "label": "Item 2"})
        ch = U.diff_trees(before, after)
        self.assertEqual(ch["added"], ["Item 2"])
        self.assertEqual(ch["removed"], [])
        self.assertEqual(ch["changedValues"], [{"label": "countLabel", "from": "Count: 0", "to": "Count: 1"}])

    def test_value_change_and_removal(self):
        before = walk_elements()
        after = [dict(e) for e in before if e["label"] != "Water plants"]
        for e in after:
            if e["label"] == "Notes":
                e["value"] = "0"
        ch = U.diff_trees(before, after)
        self.assertEqual(ch["removed"], ["Water plants"])
        self.assertEqual(ch["changedValues"], [{"label": "Notes", "from": "1", "to": "0"}])

    def test_no_change(self):
        els = walk_elements()
        self.assertEqual(U.diff_trees(els, els), {"added": [], "removed": [], "changedValues": []})


class RealCaptures(unittest.TestCase):
    """Payloads captured from an iPhone 17 Pro on iOS 27.3 with the UIProbe test app."""

    def elements(self, name):
        fx = load(name)
        return fx, [U.normalize_element(r["focus"], r["attrs"], f"e{i}") for i, r in enumerate(fx["raw"], 1)]

    def test_tree_shapes(self):
        fx, els = self.elements("real-tree.json")
        self.assertEqual(len(els), 10)
        by = {e["label"]: e for e in els}
        self.assertEqual((by["UI Probe"]["role"], by["UI Probe"]["traits"]), ("header", ["staticText", "header"]))
        self.assertEqual(by["Increment"]["identifier"], "incrementButton")
        self.assertEqual(by["Increment"]["hint"], "Adds one to the count")
        self.assertEqual(by["Notes"]["role"], "toggle")
        self.assertEqual(by["Counterclockwise Curved Arrow"]["identifier"], "arrow.counterclockwise")
        self.assertEqual(by["Count: 1"]["identifier"], "countLabel")
        self.assertTrue(all(e["frame"] is None for e in els))   # iOS 27.3 answers no frame attribute
        self.assertEqual([e["label"] for e in els], [e["label"] for e in fx["elements"]])

    def test_element_pid_is_the_app(self):
        fx = load("real-tree.json")
        for r in fx["raw"]:
            self.assertEqual(U.element_pid(U.element_bytes(r["focus"])), fx["pid"])
        self.assertIsNone(U.element_pid(b"\x01"))

    def test_walk_loops_on_real_events(self):
        fx = load("real-tree.json")
        w = U.Walk(100)
        for r in fx["raw"]:
            self.assertTrue(w.add(r["focus"]))
        self.assertFalse(w.add(fx["raw"][0]["focus"]))
        self.assertTrue(w.looped)

    def test_diff_after_add_item(self):
        _, before = self.elements("real-tree.json")
        _, after = self.elements("real-tree-after-add.json")
        self.assertEqual(U.diff_trees(before, after), {"added": ["Item 2"], "removed": [], "changedValues": []})

    def test_audit_issues_match_walked_elements_by_bytes(self):
        fx, els = self.elements("real-tree-after-add.json")
        ids = {e["id"]: U.element_bytes(r["focus"]) for e, r in zip(els, fx["raw"])}
        audit = load("real-audit.json")
        self.assertEqual(audit["raw"]["supported"], ["testTypeSufficientElementDescription", "testTypeContrast",
                                                     "testTypeHitRegion", "testTypeElementDetection",
                                                     "testTypeTextClipped", "testTypeTrait", "testTypeDynamicText"])
        issues = [U.normalize_issue(i, els, ids) for i in audit["raw"]["issues"]]
        got = [(i["type"], i["element"]["label"], i["details"].get("elementId")) for i in issues]
        self.assertEqual(got, [
            ("hit-region", "Counterclockwise Curved Arrow", "e9"),
            ("dynamic-type", "This sentence is far too long to fit in its tiny fixed box", "e10"),
            ("clipped-text", "This sentence is far too long to fit in its tiny fixed box", "e10"),
            ("contrast", "Increment", "e3"),
        ])
        hit, contrast = issues[0], issues[3]
        self.assertEqual(hit["element"]["frame"], {"x": 196.4, "y": 463.1, "w": 9.5, "h": 11.3})
        self.assertTrue(hit["message"].endswith("Current size is 10 x 11."))
        self.assertEqual((contrast["details"]["foreground"], contrast["details"]["background"]), ("#0091FF", "#FFFFFF"))
        self.assertNotIn("poi.", contrast["message"])
        self.assertLessEqual(len(contrast["message"]), 220)
        # without a walk: the issue's own text / identifier still names the element
        bare = [U.normalize_issue(i) for i in audit["raw"]["issues"]]
        self.assertEqual(bare[0]["element"]["label"], "arrow.counterclockwise")
        self.assertEqual(bare[1]["element"]["label"], "This sentence is far too long to fit in its tiny fixed box")
        self.assertEqual(U.audit_plan(["contrast", "hit-region", "nope"], audit["raw"]["supported"]),
                         (["testTypeContrast", "testTypeHitRegion"], ["contrast", "hit-region"], ["nope"]))

    def test_press_reply_shape(self):
        r = load("real-press.json")
        self.assertEqual(set(r) - {"_note"}, {"pressed", "changes", "after", "elapsedMs", "bundleId", "pid"})
        self.assertEqual(r["changes"]["changedValues"], [{"label": "countLabel", "from": "Count: 1", "to": "Count: 0"}])
        self.assertLess(r["elapsedMs"], 20000)

    def test_sentences(self):
        self.assertEqual(U.first_sentences("One. Two two. Three.", 9), "One.")
        self.assertEqual(U.first_sentences("no stop", 50), "no stop.")
        self.assertTrue(U.first_sentences("x" * 300, 50).endswith("…"))


class Audit(unittest.TestCase):
    def test_normalize_with_elements(self):
        fx = load("synthetic-audit.json")
        els = walk_elements()
        issues = [U.normalize_issue(i, els) for i in fx["issues"]]
        self.assertEqual([i["type"] for i in issues], ["contrast", "hit-region", "clipped-text"])
        self.assertEqual(issues[0]["rawType"], "testTypeContrast")
        self.assertEqual(issues[0]["element"]["label"], "Faint text that is hard to read")
        self.assertEqual(issues[0]["details"]["foreground"], "#E0E0E0")
        self.assertEqual(issues[1]["element"]["label"], "Reset")
        self.assertEqual(issues[2]["element"], {"label": None, "frame": {"x": 141, "y": 440, "w": 120, "h": 18}})
        for i in issues:
            self.assertTrue(i["message"])
            json.dumps(i)

    def test_plan(self):
        sup = load("synthetic-audit.json")["supported"]
        run, slugs, missing = U.audit_plan(["contrast", "testTypeHitRegion", "made-up"], sup)
        self.assertEqual(run, [12, 13, 100])
        self.assertEqual(slugs, ["contrast", "hit-region"])
        self.assertEqual(missing, ["made-up"])
        run, slugs, missing = U.audit_plan(None, sup)
        self.assertEqual(run, sup)
        self.assertIn("missing-label", slugs)
        self.assertEqual(missing, [])

    def test_string_supported_types(self):
        run, slugs, missing = U.audit_plan(["dynamic-type"], ["testTypeDynamicText", "testTypeContrast"])
        self.assertEqual((run, slugs, missing), (["testTypeDynamicText"], ["dynamic-type"], []))

    def test_slugs(self):
        self.assertEqual(U.audit_slug(5000), "missing-label")
        self.assertEqual(U.audit_slug("3003"), "clipped-text")
        self.assertEqual(U.audit_slug("testTypeSomethingNew"), "something-new")
        self.assertEqual(U.audit_slug(42), "type-42")


class DemoCommands(unittest.TestCase):
    """The demo feature module, called directly through a registry (no daemon process)."""

    def setUp(self):
        from deviced_kit import FeatureRegistry
        import deviced_demo_core as core
        from deviced_demo_features import ui as demo_ui
        self.core, self.mod = core, demo_ui
        self.saved_running = set(core.running)
        self.saved_state = {k: (set(v) if isinstance(v, set) else list(v) if isinstance(v, list) else v)
                            for k, v in demo_ui._state.items()}
        demo_ui.time.sleep = lambda s: None
        self.reg = FeatureRegistry(log=lambda *_: None)
        m = self.reg.module("ui")
        demo_ui.register(m)
        m.commit()

    def tearDown(self):
        import time
        self.mod.time.sleep = time.sleep
        self.core.running.clear()
        self.core.running.update(self.saved_running)
        self.mod._state.clear()
        self.mod._state.update(self.saved_state)

    def call(self, cmd, a):
        return self.reg.lookup(cmd)(None, a)

    def test_press_add_habit_changes_tree(self):
        self.core.running.add(self.mod.HABITS)
        tree = self.call("ui.tree", {"bundleId": "com.example.habits"})   # plain form accepted
        self.assertTrue(tree["complete"])
        out = self.call("ui.press", {"label": "add habit"})
        self.assertEqual(out["pressed"]["label"], "Add habit")
        self.assertEqual(len(out["changes"]["added"]), 1)
        self.assertEqual(out["after"]["count"], tree["count"] + 1)

    def test_errors(self):
        from common import HelperError
        self.core.running.discard(self.mod.HABITS)
        with self.assertRaises(HelperError) as c:
            self.call("ui.tree", {"bundleId": "com.example.habits"})
        self.assertEqual(c.exception.code, "wrong-app")
        with self.assertRaises(HelperError) as c:
            self.call("ui.press", {"label": "Refrsh"})
        self.assertEqual(c.exception.code, "no-element")
        self.assertIn("Refresh", c.exception.detail)
        with self.assertRaises(HelperError) as c:
            self.call("ui.press", {"id": "e999"})
        self.assertEqual(c.exception.code, "stale-element")

    def test_audit_reports_unsupported(self):
        out = self.call("ui.audit", {"types": ["contrast", "nope"]})
        self.assertEqual(out["unsupported"], ["nope"])
        self.assertTrue(all(i["type"] == "contrast" for i in out["issues"]))
        self.assertTrue(out["issues"])

    def test_tree_max_truncates(self):
        out = self.call("ui.tree", {"max": 2})
        self.assertEqual(out["count"], 2)
        self.assertTrue(out["truncated"])
        self.assertFalse(out["complete"])


if __name__ == "__main__":
    unittest.main()
