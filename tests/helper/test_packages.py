"""Swift package dependencies (helper/iosstudio/features/packages.py, helper/iosstudio/features/package_manifest.py): Package.resolved v1, v2 and v3
(real files in tests/fixtures/packages/), version rules from `dump-package` (captured output), the list merge
(declared packages + pins + target dependencies, bare-name products), resolved diffs (`changes`), product picking,
URL and version checks, swift output -> progress, the remove edit on many manifest shapes (fixtures in
tests/fixtures/packages/remove/: trailing commas, comments, multi-line entries, .package(name:url:), path:, raw and
multi-line strings, emptied arrays, bare product strings vs local targets, variables, CRLF) and the lexer.
The CLI with a fake `swift` is in tests/cli/test_packages_cli.py; the real swift (and network) in
tests/e2e/test_packages_e2e.py.

    python -m pytest tests/helper/test_packages.py
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from iosstudio.features import package_manifest as M
from iosstudio.features import packages as P
from support import ROOT

FIX = ROOT / "tests/fixtures/packages"


# ================================================================ Package.resolved

class Resolved(unittest.TestCase):
    def test_v1(self):
        pins = M.parse_resolved((FIX / "resolved-v1.json").read_text())
        self.assertEqual(len(pins), 5)
        n = pins["nimble"]
        self.assertEqual(n["url"], "https://github.com/Quick/Nimble.git")
        self.assertEqual(n["version"], "12.3.0")
        self.assertEqual(n["revision"], "edaedc1ec86f14ac6e2ca495b94f0ff7150d98d0")
        self.assertNotIn("branch", n)                       # "branch": null in v1
        self.assertIn("cwlcatchexception", pins)            # identity from the URL, lowercased

    def test_v2(self):
        pins = M.parse_resolved((FIX / "resolved-v2.json").read_text())
        self.assertEqual(set(pins), {"swift-argument-parser", "swift-system"})
        self.assertEqual(pins["swift-system"]["version"], "1.4.2")
        self.assertEqual(pins["swift-system"]["kind"], "remoteSourceControl")

    def test_v3_branch_pin(self):
        pins = M.parse_resolved((FIX / "resolved-v3.json").read_text())
        ap = pins["swift-argument-parser"]
        self.assertEqual((ap["branch"], ap.get("version")), ("main", None))
        self.assertEqual(M.pin_label(ap), "main@38ca4ec")
        self.assertEqual(M.pin_label(pins["swift-collections"]), "1.7.5")

    def test_bad_files(self):
        with self.assertRaises(ValueError):
            M.parse_resolved("{nope")
        with self.assertRaises(ValueError):
            M.parse_resolved('{"version": 9}')
        with self.assertRaises(ValueError):
            M.parse_resolved("[]")

    def test_changes(self):
        before = M.parse_resolved((FIX / "resolved-v3.json").read_text())
        after = json.loads((FIX / "resolved-v3.json").read_text())
        after["pins"][1]["state"] = {"revision": "1" * 40, "version": "1.8.0"}
        after["pins"][0]["state"]["revision"] = "2" * 40
        after["pins"].append({"identity": "swift-log", "kind": "remoteSourceControl",
                              "location": "https://github.com/apple/swift-log", "state": {"version": "1.6.1",
                                                                                          "revision": "3" * 40}})
        changes = M.resolved_changes(before, M.parse_resolved(json.dumps(after)))
        self.assertEqual(changes, [
            {"identity": "swift-argument-parser", "from": "main@38ca4ec", "to": "main@2222222"},
            {"identity": "swift-collections", "from": "1.7.5", "to": "1.8.0"},
            {"identity": "swift-log", "from": None, "to": "1.6.1"}])
        self.assertEqual(M.resolved_changes(before, {}), [
            {"identity": "swift-argument-parser", "from": "main@38ca4ec", "to": None},
            {"identity": "swift-collections", "from": "1.7.5", "to": None}])
        self.assertEqual(M.resolved_changes(before, before), [])

    def test_drop_pins_keeps_swiftpm_layout(self):
        text = (FIX / "resolved-v3.json").read_text()
        out = M.drop_pins(text, {"swift-collections"})
        self.assertIn('"identity" : "swift-argument-parser"', out)
        self.assertNotIn("swift-collections", out)
        self.assertIn('"originHash" : ', out)
        self.assertIsNone(M.drop_pins(text, {"swift-collections", "swift-argument-parser"}))
        v1 = M.drop_pins((FIX / "resolved-v1.json").read_text(), {"nimble"})
        self.assertEqual(len(M.parse_resolved(v1)), 4)


# ================================================================ requirements, list merge

class Requirements(unittest.TestCase):
    def test_rules(self):
        r = M.requirement
        self.assertEqual(r({"range": [{"lowerBound": "1.1.0", "upperBound": "2.0.0"}]})["text"], "from 1.1.0")
        up = r({"range": [{"lowerBound": "1.2.0", "upperBound": "1.3.0"}]})
        self.assertEqual((up["rule"], up["text"]), ("upToNextMinor", "up to next minor from 1.2.0"))
        rg = r({"range": [{"lowerBound": "1.0.0", "upperBound": "1.5.0"}]})
        self.assertEqual((rg["rule"], rg["text"]), ("range", "1.0.0 ..< 1.5.0"))
        self.assertEqual(r({"exact": ["1.2.0"]})["text"], "exactly 1.2.0")
        self.assertEqual(r({"branch": ["main"]})["text"], "branch main")
        self.assertEqual(r({"revision": ["abcdef0123456"]})["text"], "revision abcdef0")
        self.assertEqual(r(None, local=True)["rule"], "local")
        self.assertEqual(r({"weird": 1})["rule"], "unknown")
        self.assertEqual(r({"range": [{"lowerBound": "0.9.0-beta.1", "upperBound": "1.0.0"}]})["rule"], "from")

    def test_declared_kinds(self):
        dump = json.loads((FIX / "dump-kinds.json").read_text())
        got = {d["identity"]: d for d in M.declared(dump)}
        self.assertEqual(list(got), ["b", "c", "d", "e", "explore", "f", "scope.name"])
        self.assertEqual(got["b"]["requirement"]["rule"], "upToNextMinor")
        self.assertEqual(got["c"]["requirement"]["text"], "exactly 1.2.0")
        self.assertEqual(got["d"]["requirement"]["branch"], "main")
        self.assertEqual(got["e"]["requirement"]["revision"], "abcdef")
        self.assertEqual((got["explore"]["kind"], got["explore"]["path"]), ("local", "/home/u/Developer/explore"))
        self.assertEqual(got["f"]["requirement"]["rule"], "range")
        self.assertEqual((got["scope.name"]["kind"], got["scope.name"]["requirement"]["rule"]), ("registry", "from"))
        self.assertEqual(got["b"]["url"], "https://github.com/a/b")

    def test_legacy_name(self):
        dump = json.loads((FIX / "dump-legacy-name.json").read_text())
        names = {d["identity"]: d.get("name") for d in M.declared(dump)}
        self.assertEqual(names, {"kingfisher": "Kingfisher", "lottie-spm": "Lottie"})

    def test_identity(self):
        self.assertEqual(M.identity_of("https://github.com/apple/swift-collections.git/"), "swift-collections")
        self.assertEqual(M.identity_of("git@github.com:onevcat/Kingfisher.git"), "kingfisher")
        self.assertEqual(M.identity_of("../WeatherKitMock"), "weatherkitmock")


class ListMerge(unittest.TestCase):
    def test_products_and_bare_names(self):
        dump = json.loads((FIX / "dump-bare-strings.json").read_text())
        pins = {"alamofire": {"identity": "alamofire", "version": "5.9.1", "revision": "f455c2975872ccd2d9c81594c658af65716e9b9a"}}
        products = {"alamofire": ["Alamofire"], "snapkit": ["SnapKit"]}
        out = {p["identity"]: p for p in P.build_list(Path("/x"), dump, pins, {}, lambda i: products.get(i, []))}
        a = out["alamofire"]
        self.assertEqual(a["resolved"], {"version": "5.9.1", "revision": "f455c29"})
        self.assertEqual(a["requirement"]["text"], "from 5.8.0")
        self.assertEqual(a["used_by"], ["Legacy", "LegacyTests"])
        self.assertEqual(a["products_used"], ["Alamofire"])
        self.assertIsNone(out["snapkit"]["resolved"])
        self.assertEqual(out["snapkit"]["used_by"], ["Legacy"])
        self.assertEqual(out["alamofire"]["url"], "https://github.com/Alamofire/Alamofire.git")

    def test_product_deps_and_local_target_names(self):
        dump = json.loads((FIX / "dump-product-vs-target.json").read_text())
        products = ["Collections", "DequeModule", "OrderedCollections"]
        (entry,) = P.build_list(Path("/x"), dump, {}, {}, lambda i: products)
        # "Collections" is a local target there, so only DequeModule (bare) and OrderedCollections (.product) count
        self.assertEqual(sorted(entry["products_used"]), ["DequeModule", "OrderedCollections"])
        self.assertEqual(entry["used_by"], ["Mixed"])

    def test_legacy_name_product_refs(self):
        dump = json.loads((FIX / "dump-legacy-name.json").read_text())
        out = {p["identity"]: p for p in P.build_list(Path("/x"), dump, {}, {})}
        self.assertEqual(out["lottie-spm"]["products_used"], ["Lottie"])
        self.assertEqual(out["lottie-spm"]["name"], "Lottie")

    def test_targets_and_app_target(self):
        dump = json.loads((FIX / "dump-bare-strings.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(P.app_target(root, dump), "Legacy")       # the library product's target
            (root / "xtool.yml").write_text("version: 1\nproduct: Legacy\n")
            ts = P.target_list(root, dump)
            self.assertEqual([t["name"] for t in ts], ["Legacy", "Core", "LegacyTests"])
            self.assertTrue(ts[0]["app"])

    def test_find_declared(self):
        dump = json.loads((FIX / "dump-legacy-name.json").read_text())
        self.assertEqual(P.find_declared(dump, "Kingfisher")["identity"], "kingfisher")
        self.assertEqual(P.find_declared(dump, "https://github.com/airbnb/lottie-spm")["identity"], "lottie-spm")
        self.assertIsNone(P.find_declared(dump, "nope"))


# ================================================================ small pieces

class Pieces(unittest.TestCase):
    def test_pick_product(self):
        self.assertEqual(P.pick_product(["ArgumentParser"]), "ArgumentParser")
        self.assertEqual(P.pick_product(["Collections", "_RopeModule"]), "Collections")
        self.assertIsNone(P.pick_product(["Collections", "DequeModule"]))
        self.assertIsNone(P.pick_product([]))

    def test_newest_tag(self):
        lines = ["abc\trefs/tags/1.0.0", "abc\trefs/tags/v1.10.0", "abc\trefs/tags/1.9.3", "abc\trefs/tags/2.0.0-beta.1",
                 "abc\trefs/tags/latest", "abc\trefs/heads/main"]
        self.assertEqual(P.newest_tag(lines), "1.10.0")
        self.assertIsNone(P.newest_tag(["abc\trefs/tags/nightly"]))

    def test_check_location(self):
        root = Path("/tmp/x")
        self.assertEqual(P.check_location("https://github.com/apple/swift-collections.git/", root),
                         ("url", "https://github.com/apple/swift-collections.git", "swift-collections"))
        self.assertEqual(P.check_location("git@github.com:onevcat/Kingfisher.git", root)[2], "kingfisher")
        self.assertEqual(P.check_location("https://github.com/apple/swift-log/tree/main/Sources", root)[1],
                         "https://github.com/apple/swift-log")
        for bad in ("", "swift-collections", "http://github.com/a/b", "https://github.com/a/b\"x", "ftp://x/y"):
            with self.assertRaises(P.HelperError) as cm:
                P.check_location(bad, root)
            self.assertEqual(cm.exception.code, "bad-url", bad)
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "App"
            local = Path(tmp) / "Kit"
            proj.mkdir()
            local.mkdir()
            with self.assertRaises(P.HelperError):
                P.check_location("../Kit", proj)                      # no Package.swift there
            (local / "Package.swift").write_text("")
            self.assertEqual(P.check_location("../Kit", proj), ("path", "../Kit", "kit"))

    def test_check_requirement(self):
        P.check_requirement("from", "1.2.0")
        P.check_requirement("exact", "2.0.0-beta.1")
        P.check_requirement("branch", "feature/x")
        P.check_requirement("revision", "38ca4ec")
        for rule, v in (("from", "1.2"), ("from", None), ("branch", "two words"), ("revision", "xyz"),
                        ("nope", "1.0.0")):
            with self.assertRaises(P.HelperError):
                P.check_requirement(rule, v)

    def test_progress_detail(self):
        self.assertEqual(P.progress_detail("Fetching https://github.com/apple/swift-collections from cache"),
                         ("Fetching swift-collections", "swift-collections", False))
        self.assertEqual(P.progress_detail("Computed https://github.com/apple/swift-collections at 1.7.5 (1.60s)"),
                         ("swift-collections 1.7.5", "swift-collections", True))
        self.assertEqual(P.progress_detail("Working copy of https://github.com/apple/swift-argument-parser resolved "
                                           "at main (38ca4ec)")[0], "swift-argument-parser main (38ca4ec)")
        self.assertEqual(P.progress_detail("[120/29848] Fetching swift-collections")[0],
                         "Fetching swift-collections 0%")
        self.assertIsNone(P.progress_detail("    Cloning into bare repository '/x'..."))
        self.assertIn("Waiting", P.progress_detail("Another instance of SwiftPM is already running using "
                                                   "'/x/.build', waiting until that process has finished")[0])

    def test_summary(self):
        self.assertEqual(P.summary([], "update"), "Everything is up to date")
        self.assertEqual(P.summary([{"identity": "swift-collections", "from": "1.1.3", "to": "1.1.4"},
                                    {"identity": "swift-log", "from": None, "to": "1.6.1"}], "update"),
                         "2 packages updated: swift-collections 1.1.3 → 1.1.4, swift-log 1.6.1 (new)")

    def test_swift_failure_classifies(self):
        e = P.swift_failure(["Fetching https://x/y", "error: Failed to clone repository https://x/y:",
                             "    fatal: repository 'https://x/y/' not found"], "Couldn't fetch y")
        self.assertEqual(e.code, "fetch-failed")
        self.assertEqual(e.extra["diagnostics"][0]["category"], "package")
        self.assertEqual(e.extra["diagnostics"][0]["hint"], P.HINT_FETCH)
        e = P.swift_failure(["error: Dependencies could not be resolved because no versions of 'a' match"], "x")
        self.assertEqual(e.code, "resolve-failed")
        e = P.swift_failure(["fatal: unable to access 'https://x/': Could not resolve host: x"], "x")
        self.assertEqual(e.code, "network")
        self.assertEqual(len(e.extra["diagnostics"]), 1)      # synthesized from the message


# ================================================================ lexer and the remove edit

class Lexer(unittest.TestCase):
    def kinds(self, text):
        return [(t.kind, text[t.start:t.end]) for t in M.tokenize(text) if t.kind not in ("ws", "nl")]

    def test_strings_and_comments(self):
        text = ('let a = #"x ")" y"# // .package(\n/* a /* nested ) */ ] */ "b\\"(" """\n  ok ) "\n  """ '
                '"\\(f("q)"))" ##"a "# b"##')
        toks = self.kinds(text)
        strings = [s for k, s in toks if k == "string"]
        self.assertEqual(strings, ['#"x ")" y"#', '"b\\"("', '"""\n  ok ) "\n  """', '"\\(f("q)"))"',
                                   '##"a "# b"##'])
        self.assertEqual([s for k, s in toks if k == "comment"], ["// .package(", "/* a /* nested ) */ ] */"])
        self.assertNotIn(("punct", ")"), [t for t in toks if t[1] == ")" and t[0] == "punct"][1:])

    def test_values(self):
        def val(src):
            (tok,) = [t for t in M.tokenize(src) if t.kind == "string"]
            return M.string_value(src, tok)
        self.assertEqual(val('"a\\"b\\u{41}"'), 'a"bA')
        self.assertEqual(val('#"a\\nb"#'), "a\\nb")
        self.assertEqual(val('"""\n    one\n    two\n    """'), "one\ntwo")
        self.assertIsNone(val('"\\(x)/y"'))
        self.assertIsNone(val('#"\\#(x)"#'))

    def test_errors(self):
        for bad in ('"open', '/* open', '"""\nnever', '"\\(x"'):
            with self.assertRaises(M.LexError):
                M.tokenize(bad)
        with self.assertRaises(M.LexError):
            M.Code.parse("let a = [1, 2)")


class RemoveEdit(unittest.TestCase):
    cases = json.loads((FIX / "remove/cases.json").read_text())

    def run_case(self, name: str):
        c = self.cases[name]
        with open(FIX / "remove" / f"{c.get('source', name)}.swift", newline="") as fh:
            text = fh.read()
        return c, text, M.remove_package(text, c["identity"], url=c.get("url"), names=c.get("names", []),
                                         products=c.get("products", []), local_targets=c.get("local_targets", []))

    def test_fixtures(self):
        for name, c in self.cases.items():
            with self.subTest(name):
                c, text, edit = self.run_case(name)
                if c.get("problems"):
                    self.assertTrue(edit.problems)
                    self.assertFalse(edit.removed_package)
                    continue
                self.assertEqual(edit.problems, [])
                self.assertTrue(edit.removed_package)
                with open(FIX / "remove" / f"{name}.expected.swift", newline="") as fh:
                    self.assertEqual(edit.text, fh.read())
                self.assertEqual(sorted([d["target"] or "", d["product"]] for d in edit.target_deps),
                                 sorted([t or "", p] for t, p in c["target_deps"]))
                again = M.remove_package(edit.text, c["identity"], url=c.get("url"), names=c.get("names", []),
                                         products=c.get("products", []), local_targets=c.get("local_targets", []))
                self.assertFalse(again.removed_package)        # nothing left to remove
                self.assertEqual(again.text, edit.text)

    def test_untouched_lines(self):
        _c, text, edit = self.run_case("comments")
        self.assertIn('// .package(url: "https://github.com/apple/swift-collections", branch: "main"),', edit.text)
        self.assertIn("/* nested comment */", edit.text)
        _c, text, edit = self.run_case("tricky-strings")
        self.assertIn('let banner = #"Use .package(url: "https://github.com/apple/swift-collections"', edit.text)
        self.assertIn('        .package(url: "https://github.com/apple/swift-collections", from: "1.1.0"),\n    ]\n'
                      '    """', edit.text)
        _c, text, edit = self.run_case("crlf")
        self.assertNotIn("\n", edit.text.replace("\r\n", ""))

    def test_unknown_package_changes_nothing(self):
        text = (FIX / "remove/template.swift").read_text()
        edit = M.remove_package(text, "not-there")
        self.assertEqual((edit.text, edit.removed_package, edit.target_deps), (text, False, []))

if __name__ == "__main__":
    unittest.main()
