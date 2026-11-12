"""helper/iosstudio/features/hotreload/checks.py: a stored property's type is its annotation, else the type its
initial value plainly shows; a value edit whose type can't be told that way isn't a blocker (review of PR #46)."""
from __future__ import annotations

import unittest

from iosstudio.features.hotreload import checks


def kinds(old: str, new: str) -> list[str]:
    return [b["kind"] for b in checks.structural_blockers(old, new, "Sources/App/Model.swift")]


def inferred(expr: str) -> str:
    return checks._inferred(checks.tokenize(expr))


def edit(src: str, old: str, new: str) -> str:
    assert old in src, old
    return src.replace(old, new)


SAMPLES = ('struct Item {\n    let n: String\n}\n\n'
           'struct Store {\n    static let samples = [Item(n: "a")]\n'
           '    static let date = { let f = DateFormatter(); f.dateStyle = .short; return f }()\n}\n\n'
           'let sampleItems = ["a", "b"]\nlet total = 3 * 4\n')


class Inferred(unittest.TestCase):
    def test_plain_types(self):
        cases = {"1": "Int", "-2": "Int", "1.5": "Double", "1e3": "Double", "0x1p3": "Double", "0xff": "Int",
                 '"a"': "String", '#"a"#': "String", "true": "Bool", '["a", "b",]': "[ String ]",
                 '[1: "a"]': "[ Int : String ]", "[[1], [2]]": "[ [ Int ] ]", '[Item(n: "a")]': "[ Item ]",
                 "[Int]()": "[ Int ]", "[String: Int]()": "[ String : Int ]", "S()": "S",
                 "S.init(x: 1)": "S", "Foo.Bar(1)": "Foo . Bar", "Color.red": "Color", "Date.now": "Date",
                 "Stack { Text(\"a\") }": "Stack", "Button(\"a\") { }": "Button"}
        for expr, want in cases.items():
            self.assertEqual(inferred(expr), want, expr)

    def test_unknown(self):
        for expr in ("3 * 4", "{ 1 }()", "make()", "Foo.make()", "Foo.shared.items", "[]", "[:]", "[1, 2.5]",
                     "[1, x]", "x", "nil", "URL(string: \"a\")!", "Foo().bar", "Foo<Int>()", "!true", "(1, 2)",
                     "Foo.Bar", "S() + S()"):
            self.assertEqual(inferred(expr), checks.UNKNOWN, expr)

    def test_regex_literal_is_its_text(self):
        self.assertNotEqual(inferred("/(a)b/"), inferred("/ab/"))


class ValueEditsAllowed(unittest.TestCase):
    def test_static_array_of_calls(self):
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, 'Item(n: "a")', 'Item(n: "b")')), [])

    def test_global_array_grows(self):
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, '["a", "b"]', '["a", "b", "c"]')), [])

    def test_global_expression(self):
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, "3 * 4", "3 * 5")), [])

    def test_static_closure(self):
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, ".short", ".long")), [])

    def test_annotation_added_or_value_unknown(self):
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, "let total = 3 * 4", "let total: Int = 12")), [])
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, "let total = 3 * 4", "let total = 12")), [])
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, '["a", "b"]', '[String]()')), [])

    def test_instance_property_value(self):
        src = 'struct V {\n    @State private var items = ["a"]\n    var tint = Color.red\n}\n'
        new = edit(edit(src, '["a"]', '["a", "b"]'), "Color.red", "Color(red: 1, green: 0, blue: 0)")
        self.assertEqual(kinds(src, new), [])


class TypeChangesBlocked(unittest.TestCase):
    def test_annotation_changed(self):
        src = "struct S {\n    static var limit: Int = 3 * 4\n}\n"
        self.assertEqual(kinds(src, edit(src, ": Int", ": Double")), ["stored-property"])
        self.assertEqual(kinds(src, edit(src, ": Int = 3 * 4", ": Double = 1")), ["stored-property"])

    def test_literal_kind_changed(self):
        src = "let total = 3\n"
        self.assertEqual(kinds(src, edit(src, "3", '"3"')), ["stored-property"])
        self.assertEqual(kinds(src, edit(src, "= 3", ": String = \"3\"")), ["stored-property"])
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, '["a", "b"]', "[1, 2]")), ["stored-property"])

    def test_callee_changed(self):
        src = "struct S {\n    static let shared = Foo()\n}\n"
        self.assertEqual(kinds(src, edit(src, "Foo()", "Bar()")), ["stored-property"])
        self.assertEqual(kinds(src, edit(src, "Foo()", "Foo.Bar()")), ["stored-property"])
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, '[Item(n: "a")]', '[Other(n: "a")]')), ["stored-property"])

    def test_removed_and_let_to_var(self):
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, "let total = 3 * 4\n", "")), ["stored-property"])
        self.assertEqual(kinds(SAMPLES, edit(SAMPLES, "let total", "var total")), ["stored-property"])


class RealisticSwiftUI(unittest.TestCase):
    """Edits people make while tuning a screen: none of them change a layout."""
    SRC = ('import SwiftUI\n\n'
           'struct Recipe: Identifiable {\n    let id = UUID()\n    var name: String\n    var minutes = 10\n}\n\n'
           'let accent = Color.orange\nlet spacing: CGFloat = 12\n\n'
           'struct RecipeList: View {\n'
           '    static let samples = [Recipe(name: "Soup"), Recipe(name: "Pie", minutes: 40)]\n'
           '    static let formatter: DateComponentsFormatter = {\n'
           '        let f = DateComponentsFormatter()\n        f.unitsStyle = .short\n        return f\n    }()\n'
           '    @State private var query = ""\n    @State private var recipes = RecipeList.samples\n'
           '    @State private var sort = Sort.name\n\n'
           '    var body: some View {\n        List(recipes) { r in Text(r.name) }\n'
           '            .searchable(text: $query)\n            .tint(accent)\n    }\n}\n\n'
           'enum Sort {\n    case name, time\n}\n')

    def test_tuning_edits(self):
        edits = [('Recipe(name: "Pie", minutes: 40)', 'Recipe(name: "Pie", minutes: 45), Recipe(name: "Tea")'),
                 (".short", ".abbreviated"), ("Color.orange", "Color.teal"), ("= 12", "= 16"),
                 ('query = ""', 'query = "so"'), ("Sort.name", "Sort.time"), ("minutes = 10", "minutes = 15"),
                 ("Text(r.name)", "Text(r.name).bold()")]
        for old, new in edits:
            self.assertEqual(kinds(self.SRC, edit(self.SRC, old, new)), [], old)
        new = self.SRC
        for old, n in edits:
            new = edit(new, old, n)
        self.assertEqual(kinds(self.SRC, new), [])

    def test_real_changes_still_blocked(self):
        self.assertEqual(kinds(self.SRC, edit(self.SRC, "minutes = 10", "minutes = 10.5")), ["stored-property"])
        self.assertEqual(kinds(self.SRC, edit(self.SRC, "Color.orange", "Gradient(colors: [])")),
                         ["stored-property"])
        self.assertEqual(kinds(self.SRC, edit(self.SRC, '    @State private var query = ""\n', "")),
                         ["stored-property"])
        self.assertEqual(kinds(self.SRC, edit(self.SRC, "CGFloat = 12", "Double = 12")), ["stored-property"])


if __name__ == "__main__":
    unittest.main()
