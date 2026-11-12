"""helper/iosstudio/features/hotreload/checks.py and macho.py, review fixes for PR #46: `class var` members of
non-final classes, type aliases, static and global stored properties, static protocol requirements, bare regex
literals, and the cached app symbol tables behind interposable()."""
from __future__ import annotations

import unittest
from unittest import mock

from iosstudio.features.hotreload import checks, macho
from support.hot_reload import app_and_patch


def blockers(old: str, new: str) -> list[dict]:
    return checks.structural_blockers(old, new, "Sources/App/Model.swift")


def kinds(old: str, new: str) -> list[str]:
    return [b["kind"] for b in blockers(old, new)]


CLASS = "class A {\n    var x = 1\n    func a() {}\n}\n"
FINAL = "final class A {\n    var x = 1\n    func a() {}\n}\n"


class ClassMembers(unittest.TestCase):
    def test_class_var_added_to_non_final_class(self):
        b = blockers(CLASS, CLASS.replace("func a() {}", "func a() {}\n    class var b: Int { 1 }"))
        self.assertEqual([x["kind"] for x in b], ["class-method"])
        self.assertEqual(b[0]["message"], "Adds the overridable member b to A.")
        self.assertEqual(b[0]["line"], 4)

    def test_class_var_removed_or_retyped(self):
        src = CLASS.replace("func a() {}", "func a() {}\n    class var b: Int { 1 }")
        self.assertEqual(kinds(src, CLASS), ["class-method"])
        self.assertEqual(kinds(src, src.replace("b: Int", "b: String")), ["class-method"])
        self.assertEqual(kinds(src, src.replace("{ 1 }", "{ 2 }")), [])

    def test_class_subscript_added(self):
        self.assertEqual(kinds(CLASS, CLASS.replace("func a() {}", "func a() {}\n    class subscript(i: Int) -> Int "
                                                                    "{ i }")), ["class-method"])
        self.assertEqual(kinds(CLASS, CLASS.replace("func a() {}", "func a() {}\n    static subscript(i: Int) -> Int "
                                                                    "{ i }")), [])

    def test_final_class_var_is_fine(self):
        self.assertEqual(kinds(CLASS, CLASS.replace("func a() {}", "func a() {}\n    final class var b: Int { 1 }")),
                         [])
        self.assertEqual(kinds(FINAL, FINAL.replace("func a() {}", "func a() {}\n    class var b: Int { 1 }")), [])
        self.assertEqual(kinds(CLASS, CLASS.replace("func a() {}", "func a() {}\n    static var b: Int { 1 }")), [])

    def test_override_class_var(self):
        src = "class B: A {\n    func a() {}\n}\n"
        self.assertEqual(kinds(src, src.replace("func a() {}", "func a() {}\n    override class var b: Int { 2 }")),
                         ["class-method"])

    def test_overloads_with_the_same_labels(self):
        src = "class A {\n    func f(_ x: Int) {}\n    func f(_ x: String) {}\n}\n"
        self.assertEqual(kinds(src, src.replace("String) {}", "String) { print(x) }")), [])
        self.assertEqual(kinds(src, src.replace("String) {}", "String) {}\n    func f(_ x: Double) {}")),
                         ["class-method"])
        self.assertEqual(kinds(src, src.replace("    func f(_ x: Int) {}\n", "")), ["class-method", "class-method"])


class StaticProtocolRequirements(unittest.TestCase):
    def test_static_var_requirement_added(self):
        src = "protocol P {\n    var a: Int { get }\n}\n"
        self.assertEqual(kinds(src, src.replace("{ get }", "{ get }\n    static var b: Int { get }")),
                         ["class-method"])
        src2 = src.replace("{ get }", "{ get }\n    static var b: Int { get }")
        self.assertEqual(kinds(src2, src2.replace("b: Int", "b: String")), ["class-method"])


    def test_bodiless_function_requirement_ends_at_its_line(self):
        src = "protocol P {\n    func a()\n    var v: Int { get }\n}\n"
        self.assertEqual(kinds(src, src.replace("{ get }", "{ get set }")), ["class-method"])
        b = blockers(src, src.replace("v: Int", "v: String"))
        self.assertEqual([(x["kind"], x["name"]) for x in b], [("class-method", "v")])


class TypeAliases(unittest.TestCase):
    SRC = "typealias T = Int\n\nstruct S {\n    typealias U = [String]\n    var v: T\n    var w: U\n}\n"

    def test_changed_top_level_alias(self):
        b = blockers(self.SRC, self.SRC.replace("T = Int", "T = String"))
        self.assertEqual([x["kind"] for x in b], ["type-alias"])
        self.assertEqual(b[0]["message"], "Changes the type alias T.")
        self.assertEqual(b[0]["name"], "T")
        self.assertEqual(b[0]["line"], 1)

    def test_changed_nested_alias(self):
        b = blockers(self.SRC, self.SRC.replace("U = [String]", "U = [Int]"))
        self.assertEqual([x["kind"] for x in b], ["type-alias"])
        self.assertEqual(b[0]["message"], "Changes the type alias U of S.")
        self.assertEqual(b[0]["type"], "S")

    def test_alias_in_extension(self):
        src = self.SRC + "extension S {\n    typealias Z = Double\n}\n"
        self.assertEqual(kinds(src, src.replace("Z = Double", "Z = Float")), ["type-alias"])

    def test_removed_alias(self):
        self.assertEqual(kinds(self.SRC, self.SRC.replace("    typealias U = [String]\n", "")), ["type-alias"])

    def test_added_alias_and_formatting_are_fine(self):
        self.assertEqual(kinds(self.SRC, self.SRC.replace("typealias T = Int", "typealias T = Int\ntypealias N = "
                                                                                "Int")), [])
        self.assertEqual(kinds(self.SRC, self.SRC.replace("typealias T = Int", "typealias  T=Int // same")), [])


class StaticAndGlobalStorage(unittest.TestCase):
    SRC = ("let limit = 10\nvar cache: [String: Int] = [:]\nvar computed: Int { limit * 2 }\n\n"
           "struct S {\n    static var shared = S()\n    static let name: String = \"s\"\n"
           "    static var label: String { name }\n    var v = 1\n}\n"
           "extension S {\n    static let other = 1.5\n}\n")

    def test_static_type_changed(self):
        b = blockers(self.SRC, self.SRC.replace('static let name: String = "s"', "static let name: Int = 1"))
        self.assertEqual([x["kind"] for x in b], ["stored-property"])
        self.assertEqual(b[0]["message"], "Changes the static property name of S.")
        self.assertEqual((b[0]["type"], b[0]["name"], b[0]["line"]), ("S", "name", 7))
        self.assertEqual(kinds(self.SRC, self.SRC.replace("S()", "T()")), ["stored-property"])
        self.assertEqual(kinds(self.SRC, self.SRC.replace("other = 1.5", "other = 1")), ["stored-property"])

    def test_global_type_changed(self):
        b = blockers(self.SRC, self.SRC.replace("let limit = 10", "let limit = 10.0"))
        self.assertEqual([x["kind"] for x in b], ["stored-property"])
        self.assertEqual(b[0]["message"], "Changes the global variable limit.")
        self.assertEqual(kinds(self.SRC, self.SRC.replace("[String: Int]", "[String: Double]")),
                         ["stored-property"])

    def test_removed(self):
        self.assertEqual(kinds(self.SRC, self.SRC.replace("let limit = 10\n", "let limit2 = 10\n")),
                         ["stored-property"])
        b = blockers(self.SRC, self.SRC.replace("    static let other = 1.5\n", ""))
        self.assertEqual([x["message"] for x in b], ["Removes the static property other from S."])

    def test_stored_to_computed(self):
        self.assertEqual(kinds(self.SRC, self.SRC.replace("let limit = 10", "var limit: Int { 10 }")),
                         ["stored-property"])

    def test_wrapper_and_weak_count(self):
        src = "struct S {\n    @Wrapped static var a = 1\n}\n"
        self.assertEqual(kinds(src, src.replace("@Wrapped ", "")), ["stored-property"])

    def test_allowed(self):
        new = self.SRC.replace("let limit = 10", "let limit = 20")                    # same type, new value
        new = new.replace("{ limit * 2 }", "{ limit * 3 }")                           # computed global
        new = new.replace("static var label: String { name }", "static var label: String { name + \"!\" }")
        new = new + "let added = 1\nextension S {\n    static var fresh: [Int] = []\n}\n"   # new storage
        self.assertEqual(kinds(self.SRC, new), [])
        self.assertEqual(kinds(self.SRC, self.SRC.replace("var computed: Int { limit * 2 }",
                                                          "var computed: Int {\n    get { limit }\n    set { }\n}")),
                         [])

    def test_locals_in_functions_dont_count(self):
        src = "func f() {\n    let a = 1\n    print(a)\n}\n"
        self.assertEqual(kinds(src, src.replace("let a = 1", "let a = \"x\"")), [])


class BareRegex(unittest.TestCase):
    def test_regex_with_parens(self):
        src = 'struct S {\n    func f(s: String) -> Bool {\n        s.contains(/(a|b)+/)\n    }\n}\n'
        self.assertEqual(kinds(src, src.replace("(a|b)", "(a|c)")), [])
        toks = checks.tokenize("let r = /\\/(x/\nlet y = 1")
        self.assertEqual([t.kind for t in toks], ["id", "id", "op", "str", "id", "id", "op", "num"])

    def test_regex_after_keywords_and_operators(self):
        for code in ("return /a(b/", "x = /(/", "case /\\)(a)/:", "if /[(]/.matches(s) {}", "f(a, /\\(/)"):
            toks = checks.tokenize(code)
            self.assertIn("str", [t.kind for t in toks], code)
            checks.scan(code if code.endswith("{}") or code.startswith("f(") else "func g() {\n" + code + "\n}\n")

    def test_division_is_not_a_regex(self):
        for code in ("let a = b / c / d", "let a = (b)/c/(d)", "let a = x[0] / 2 / y", "let a = 1/2/3",
                     "func / (l: V, r: V) -> V { l }", "let a = xs.reduce(1, /)", "let a = (b / (c)) / d",
                     "let a = self / 2 / b", "let a = / b/"):
            toks = checks.tokenize(code)
            self.assertNotIn("str", [t.kind for t in toks], code)
            checks.scan(code)

    def test_regex_ends_on_its_line(self):
        toks = checks.tokenize("let a = /x\nlet b = 1/")
        self.assertNotIn("str", [t.kind for t in toks])

    def test_extended_regex(self):
        src = "func f() {\n    let r = #/\n      (a\n      /#\n    _ = 1\n}\n"
        self.assertEqual(checks.scan(src).types, {})
        self.assertEqual([t.line for t in checks.tokenize(src) if t.text == "_"], [5])


class _NoReads(list):
    def __iter__(self):
        raise AssertionError("the symbol table was read again")


class CachedSymbolTables(unittest.TestCase):
    def test_second_interposable_call_reuses_the_app_tables(self):
        app_bytes, patch_bytes = app_and_patch(bound=["_body"], app_funcs=["_body", "_init"],
                                               patch_funcs=["_body", "_init"])
        app = macho.MachO(app_bytes)
        first = checks.interposable(macho.MachO(patch_bytes), app)
        patch = macho.MachO(patch_bytes)
        app.symbols = _NoReads(app.symbols)
        with mock.patch.object(macho.MachO, "indirect_entries", side_effect=AssertionError("indirect read again")):
            second = checks.interposable(patch, app)
        self.assertEqual(first, second)
        self.assertEqual(second.swappable, ["_body"])

    def test_cached_tables_are_read_only(self):
        app_bytes, _patch_bytes = app_and_patch(bound=["_body"], app_funcs=["_body"], patch_funcs=["_body"])
        app = macho.MachO(app_bytes)
        self.assertIs(app.defined(), app.defined())
        self.assertIs(app.indirect_names(), app.indirect_names())
        self.assertIsNot(app.defined(), app.defined(ext_only=False))
        self.assertIsNot(app.indirect_names(), app.indirect_names(pointers_only=False))
        with self.assertRaises(TypeError):
            app.defined()["_x"] = None          # type: ignore[index]
        with self.assertRaises(AttributeError):
            app.indirect_names().add("_x")      # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
