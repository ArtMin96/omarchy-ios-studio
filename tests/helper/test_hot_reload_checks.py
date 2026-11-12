"""helper/iosstudio/features/hotreload/checks.py: the Swift structural scanner (what a reload can and can't change),
the opaque-type check on small real objects (tests/fixtures/hot-reload/, made by make.sh) and the interposable
check on synthetic binaries."""
from __future__ import annotations

import unittest

from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import checks, macho
from support import FIXTURES
from support.hot_reload import app_and_patch

FIX = FIXTURES / "hot-reload"

VIEW = '''import SwiftUI

struct ContentView: View {
    @State private var taps = 0
    let title: String
    var subtitle: String { "Sub" }

    var body: some View {
        VStack {
            Text("Hello")
            Button("Tap") { taps += 1 }
        }
    }

    func helper() -> Int { 1 }
}
'''


def kinds(old: str, new: str) -> list[str]:
    return [b["kind"] for b in checks.structural_blockers(old, new, "Sources/App/ContentView.swift")]


def edit(old: str, new: str, src: str = VIEW) -> str:
    assert old in src, old
    return src.replace(old, new, 1)


class Allowed(unittest.TestCase):
    def test_body_text_edit(self):
        self.assertEqual(kinds(VIEW, edit('Text("Hello")', 'Text("Hi there")')), [])

    def test_formatting_and_comments(self):
        new = edit("let title: String", "let   title :String // the title { not a brace }")
        new = edit("var body", "/* { */ var body", new)
        self.assertEqual(kinds(VIEW, new), [])

    def test_function_body_and_computed_property(self):
        new = edit("func helper() -> Int { 1 }", "func helper() -> Int {\n        let x = 2\n        return x * 3\n    }")
        new = edit('var subtitle: String { "Sub" }', 'var subtitle: String { title + "!" }', new)
        self.assertEqual(kinds(VIEW, new), [])

    def test_computed_property_with_accessors_added(self):
        new = edit("func helper()", "var count: Int {\n        get { taps }\n        set { taps = newValue }\n    }\n"
                                    "    func helper()")
        self.assertEqual(kinds(VIEW, new), [])

    def test_new_function_and_extension(self):
        new = VIEW + "\nextension ContentView {\n    func more() -> some View { Text(\"x\") }\n    " \
                     "static let shared = 1\n}\n"
        self.assertEqual(kinds(VIEW, new), [])

    def test_new_method_in_struct(self):
        self.assertEqual(kinds(VIEW, edit("func helper()", "func other() {}\n    func helper()")), [])

    def test_strings_with_braces_and_quotes(self):
        new = edit('Text("Hello")', 'Text("Hello { \\(taps) } \\("}")")\n'
                                    '            Text(#"raw " { "#)\n'
                                    '            Text("""\n            multi { struct X {}\n            """)')
        self.assertEqual(kinds(VIEW, new), [])

    def test_static_stored_property(self):
        self.assertEqual(kinds(VIEW, edit("let title: String", "let title: String\n    static var cache = [Int]()")),
                         [])

    def test_initial_value_edit(self):
        self.assertEqual(kinds(VIEW, edit("@State private var taps = 0", "@State private var taps = 5")), [])

    def test_access_level_of_stored_property(self):
        self.assertEqual(kinds(VIEW, edit("@State private var taps", "@State fileprivate var taps")), [])

    def test_if_config(self):
        src = VIEW.replace("    let title: String\n", "    #if DEBUG\n    let title: String\n    #else\n"
                                                         "    let title: String = \"r\"\n    #endif\n")
        self.assertEqual(kinds(src, src.replace('Text("Hello")', 'Text("Bye")')), [])

    def test_final_class_method_added(self):
        src = "final class Model {\n    var x = 1\n    func a() {}\n}\n"
        self.assertEqual(kinds(src, src.replace("func a() {}", "func a() {}\n    func b() {}")), [])

    def test_final_method_in_open_class(self):
        src = "class Model {\n    func a() {}\n}\n"
        self.assertEqual(kinds(src, src.replace("func a() {}", "func a() {}\n    final func b() {}")), [])
        self.assertEqual(kinds(src, src.replace("func a() {}", "func a() {}\n    static func c() {}")), [])
        self.assertEqual(kinds(src, src + "extension Model {\n    func d() {}\n}\n"), [])

    def test_optional_types_end_declarations(self):
        src = "struct V {\n    let crash: Crash?\n\n    var body: some View { Text(\"x\") }\n}\n"
        self.assertEqual(kinds(src, src.replace('"x"', '"y"')), [])
        model = checks.scan(src)
        self.assertEqual([m.name for m in model.types["V"].stored], ["crash"])


class Blocked(unittest.TestCase):
    def test_stored_property_added_removed_renamed(self):
        self.assertEqual(kinds(VIEW, edit("let title: String", "let title: String\n    var count = 0")),
                         ["stored-property"])
        self.assertEqual(kinds(VIEW, edit("    let title: String\n", "")), ["stored-property"])
        self.assertEqual(sorted(kinds(VIEW, edit("let title: String", "let heading: String"))),
                         ["stored-property", "stored-property"])

    def test_stored_property_type_changed(self):
        self.assertEqual(kinds(VIEW, edit("let title: String", "let title: Int")), ["stored-property"])
        self.assertEqual(kinds(VIEW, edit("@State private var taps = 0", "@State private var taps = 0.5")),
                         ["stored-property"])
        self.assertEqual(kinds(VIEW, edit("let title: String", "var title: String")), ["stored-property"])

    def test_property_wrapper(self):
        b = checks.structural_blockers(VIEW, edit("let title: String", "let title: String\n    @State var on = false"),
                                       "V.swift")
        self.assertEqual([x["kind"] for x in b], ["stored-property"])
        self.assertEqual(b[0]["name"], "on")
        self.assertEqual(b[0]["type"], "ContentView")
        self.assertEqual(b[0]["line"], 6)
        self.assertIn("on", b[0]["message"])
        self.assertEqual(kinds(VIEW, edit("@State private var taps", "@Binding var taps")), ["stored-property"])
        self.assertEqual(kinds(VIEW, edit("@State private var taps = 0", "private var taps = 0")),
                         ["stored-property"])

    def test_observed_and_lazy_are_stored(self):
        self.assertEqual(kinds(VIEW, edit("let title: String", "let title: String\n    var x = 1 { didSet { } }")),
                         ["stored-property"])
        self.assertEqual(kinds(VIEW, edit("let title: String", "let title: String\n    lazy var y: [Int] = []")),
                         ["stored-property"])

    def test_computed_to_stored(self):
        self.assertEqual(kinds(VIEW, edit('var subtitle: String { "Sub" }', 'var subtitle: String = "Sub"')),
                         ["stored-property"])

    def test_reordered_stored_properties(self):
        new = edit("    @State private var taps = 0\n    let title: String\n",
                   "    let title: String\n    @State private var taps = 0\n")
        self.assertEqual(kinds(VIEW, new), ["stored-property"])

    def test_new_and_removed_type(self):
        self.assertEqual(kinds(VIEW, VIEW + "\nstruct Extra: View {\n    var body: some View { EmptyView() }\n}\n"),
                         ["new-type"])
        self.assertEqual(kinds(VIEW + "\nenum Gone {}\n", VIEW), ["removed-type"])
        self.assertEqual(kinds(VIEW, edit("func helper()", "struct Inner {}\n    func helper()")), ["new-type"])

    def test_types_in_function_bodies_dont_count(self):
        self.assertEqual(kinds(VIEW, edit("func helper() -> Int { 1 }",
                                          "func helper() -> Int {\n        struct Local { var a = 1 }\n"
                                          "        return Local().a\n    }")), [])

    def test_enum_case(self):
        src = "enum Kind: String {\n    case a, b\n    case c(Int)\n    var label: String { rawValue }\n}\n"
        self.assertEqual(kinds(src, src.replace("case a, b", "case a, b, d")), ["enum-case"])
        self.assertEqual(kinds(src, src.replace("case c(Int)", "case c(String)")), ["enum-case"])
        self.assertEqual(kinds(src, src.replace("case a, b", "case b, a")), ["enum-case"])
        self.assertEqual(kinds(src, src.replace("{ rawValue }", '{ "x" + rawValue }')), [])

    def test_class_method_added_or_removed(self):
        src = "class Model {\n    var x = 1\n    func a() {}\n}\n"
        self.assertEqual(kinds(src, src.replace("func a() {}", "func a() {}\n    func b() {}")), ["class-method"])
        self.assertEqual(kinds(src, src.replace("    func a() {}\n", "")), ["class-method"])
        self.assertEqual(kinds(src, src.replace("func a() {}", "func a(_ v: Int) {}")),
                         ["class-method", "class-method"])
        self.assertEqual(kinds(src, src.replace("func a() {}", "func a() { print(1) }")), [])

    def test_generic_signature_and_conformances(self):
        self.assertEqual(kinds(VIEW, edit("struct ContentView: View", "struct ContentView: View, Equatable")),
                         ["generic-signature"])
        src = "struct Box<T> {\n    var v: T\n}\n"
        self.assertEqual(kinds(src, src.replace("Box<T>", "Box<T: Equatable>")), ["generic-signature"])
        self.assertEqual(kinds(VIEW, VIEW + "extension ContentView: Equatable {}\n"), ["generic-signature"])
        self.assertEqual(kinds("class A: B {}\n", "class A: C {}\n"), ["generic-signature"])

    def test_protocol_requirement(self):
        src = "protocol P {\n    func a()\n    var v: Int { get }\n}\n"
        self.assertEqual(kinds(src, src.replace("func a()", "func a()\n    func b()")), ["class-method"])

    def test_unreadable_source_is_a_blocker(self):
        self.assertEqual(kinds(VIEW, VIEW.replace('Text("Hello")', 'Text("Hello)')), ["unparsed"])
        self.assertEqual(kinds(VIEW, VIEW + "}\n"), ["unparsed"])
        self.assertEqual(kinds(VIEW, VIEW + "/* open"), ["unparsed"])


class Scanner(unittest.TestCase):
    def test_tokens_skip_strings_and_comments(self):
        toks = checks.tokenize('let a = "x { \\(f("}")) y" // { \n/* /* nested */ { */ b')
        self.assertEqual([t.kind for t in toks], ["id", "id", "op", "str", "id"])
        self.assertEqual(toks[-1].line, 2)

    def test_multiline_and_raw_strings_count_lines(self):
        toks = checks.tokenize('a\n"""\none {\ntwo\n"""\nb ##"x"# "##\nc')
        self.assertEqual([(t.text[:1], t.line) for t in toks if t.kind == "id"], [("a", 1), ("b", 6), ("c", 7)])

    def test_tails_and_hot_reloadable(self):
        src = edit("        }\n    }\n\n    func", "        }\n        .hotReloadable()\n    }\n\n    func")
        self.assertTrue(checks.ends_with_call(src, "ContentView"))
        self.assertFalse(checks.ends_with_call(VIEW, "ContentView"))
        self.assertFalse(checks.ends_with_call(src, "Missing"))

    def test_nested_type_names(self):
        model = checks.scan("struct A {\n    struct B { let x: Int }\n}\nextension A.B {\n    enum C { case z }\n}\n")
        self.assertEqual(sorted(model.types), ["A", "A.B", "A.B.C"])
        self.assertEqual([m.sig for m in model.types["A.B"].stored], ["let x: Int"])

    def test_describe_opaque(self):
        self.assertEqual(checks.describe_opaque("_$s8HotSpike11ContentViewV4bodyQrvpQOMQ"), ("ContentView", "body"))
        self.assertEqual(checks.describe_opaque("garbage"), (None, None))
        # a private type: a word substitution (Crash from CrashLab) and a private discriminator
        self.assertEqual(checks.describe_opaque("_$s8CrashLab0A3Row33_0123456789ABCDEF0123456789ABCDEFLLV4bodyQrvpQOMQ"),
                         ("CrashRow", "body"))
        self.assertEqual(checks.describe_opaque("_$s3App4CardV7contentQrvpQOMQ"), ("Card", "content"))


class Opaque(unittest.TestCase):
    def blockers(self, old: str, new: str) -> list[dict]:
        src = (FIX / f"{new.split('-')[0]}.swift").read_text()
        return checks.opaque_blockers(FIX / f"{old}.o", FIX / f"{new}.o", src, "Card.swift")

    def test_text_edit_keeps_the_type(self):
        self.assertEqual(self.blockers("base-any", "text-any"), [])

    def test_structure_change_without_hot_reloadable(self):
        b = self.blockers("base-any", "struct-any")
        self.assertEqual([(x["kind"], x.get("type"), x.get("name")) for x in b], [("opaque-type", "Card", "body")])
        self.assertIn(".hotReloadable()", b[0]["message"])

    def test_hot_reloadable_returning_some_view_still_changes(self):
        """.hotReloadable() -> some View carries the inner type; only -> AnyView erases it."""
        b = self.blockers("base-some", "struct-some")
        self.assertEqual(sorted(x["type"] for x in b), ["Card", "Wrapped"])
        wrapped = next(x for x in b if x["type"] == "Wrapped")
        self.assertIn("AnyView", wrapped["message"])

    def test_anyview_descriptor(self):
        d = checks.opaque_descriptors(macho.MachO.load(FIX / "base-any.o"))
        flags = {k.split("3Fix")[1].split("V4")[0]: checks.underlying_is_anyview(v) for k, v in d.items()}
        self.assertEqual(flags, {"4Card": False, "7Wrapped": True})


class Interposable(unittest.TestCase):
    def test_split(self):
        app, patch = app_and_patch(bound=["_body", "_TMa"], app_funcs=["_body", "_init", "_TMa"],
                                   patch_funcs=["_body", "_init", "_TMa", "_new"])
        res = checks.require_interposable(macho.MachO(patch), macho.MachO(app))
        self.assertEqual(res.swappable, ["_body"])
        self.assertEqual(res.missing, ["_init"])
        self.assertEqual(res.new, ["_new"])

    def test_none_bound_is_an_error(self):
        app, patch = app_and_patch(bound=[], app_funcs=["_body"], patch_funcs=["_body"])
        with self.assertRaises(HelperError) as cm:
            checks.require_interposable(macho.MachO(patch), macho.MachO(app))
        self.assertEqual(cm.exception.code, "not-interposable")
        self.assertEqual(cm.exception.fix["page"], "projects")


if __name__ == "__main__":
    unittest.main()
