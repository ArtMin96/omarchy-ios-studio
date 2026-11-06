"""Swift package dependencies (helper/packages.py, helper/package_manifest.py): Package.resolved v1, v2 and v3
(real files in tests/fixtures/packages/), version rules from `dump-package` (captured output), the list merge
(declared packages + pins + target dependencies, bare-name products), resolved diffs (`changes`), product picking,
URL and version checks, swift output -> progress, the remove edit on many manifest shapes (fixtures in
tests/fixtures/packages/remove/: trailing commas, comments, multi-line entries, .package(name:url:), path:, raw and
multi-line strings, emptied arrays, bare product strings vs local targets, variables, CRLF) and the lexer, demo
mode, and the CLI through bin/iosstudio-helper with a fake `swift` (tests/fixtures/packages/fake_swift.py): add
with one product, add needing a choice then add-product, already-added, failures and cancel putting
Package.swift/Package.resolved back and killing swift, update with changes, remove dry run and confirm.

With Swift installed, every remove fixture (before and after) is checked with the real `swift package
dump-package`; with Swift and network, an end-to-end run adds swift-collections to a throwaway project, links
one product and resolves.

    python3 -m unittest tests.test_packages
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "helper"))

import package_manifest as M  # noqa: E402
import packages as P  # noqa: E402

FIX = ROOT / "tests/fixtures/packages"
HELPER = ROOT / "bin/iosstudio-helper"
FAKE_SWIFT = FIX / "fake_swift.py"
SWIFT = shutil.which("swift")


def _network() -> bool:
    try:
        socket.create_connection(("github.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


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

    @unittest.skipUnless(SWIFT, "needs swift")
    def test_fixtures_are_valid_manifests(self):
        names = {c.get("source", n) for n, c in self.cases.items()} | {n + ".expected" for n, c in self.cases.items()
                                                                      if not c.get("problems")}
        with tempfile.TemporaryDirectory() as tmp:
            for name in sorted(names):
                with self.subTest(name):
                    d = Path(tmp) / name.replace(".", "-")
                    d.mkdir()
                    shutil.copy(FIX / "remove" / f"{name}.swift", d / "Package.swift")
                    r = subprocess.run([SWIFT, "package", "--package-path", str(d), "--scratch-path",
                                        str(Path(tmp) / "scratch" / name), "dump-package"], capture_output=True,
                                       text=True, timeout=120)
                    self.assertEqual(r.returncode, 0, r.stderr[-500:])


# ================================================================ CLI (fake swift) and demo

def make_project(root: Path, deps: str = "", target_deps: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    dep_block = f"    dependencies: [\n{deps}    ],\n" if deps else ""
    tdeps = f",\n            dependencies: [\n{target_deps}            ]" if target_deps else ""
    (root / "Package.swift").write_text(
        '// swift-tools-version: 6.0\nimport PackageDescription\n\nlet package = Package(\n    name: "App",\n'
        '    platforms: [.iOS(.v17)],\n    products: [\n        .library(name: "App", targets: ["App"]),\n    ],\n'
        f'{dep_block}    targets: [\n        .target(\n            name: "App"{tdeps}\n        ),\n'
        '        .testTarget(\n            name: "AppTests"\n        ),\n    ]\n)\n')
    (root / "xtool.yml").write_text("version: 1\nbundleID: com.example.App\n")
    return Path(os.path.realpath(root))


class CLI(unittest.TestCase):
    """bin/iosstudio-helper packages … with tests/fixtures/packages/fake_swift.py as swift."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.env = {**os.environ, "IOSSTUDIO_SWIFT": str(FAKE_SWIFT), "XDG_CACHE_HOME": str(self.dir / "cache"),
                    "XDG_STATE_HOME": str(self.dir / "state"), "XDG_CONFIG_HOME": str(self.dir / "config"),
                    "FAKE_SWIFT_PACKAGES": json.dumps({
                        "swift-algorithms": {"products": ["Algorithms"], "version": "1.2.0", "update": "1.2.1"},
                        "swift-collections": {"products": ["Collections", "DequeModule", "_RopeModule"],
                                              "version": "1.1.4", "update": "1.2.0"},
                        "missing": {"fail": "clone"}, "too-new": {"fail": "resolve"}})}
        self.env.pop("IOSSTUDIO_DEMO", None)
        self.proj = make_project(self.dir / "App")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, stdin: str = "", env: dict | None = None) -> tuple[list[dict], dict]:
        r = subprocess.run([str(HELPER), "packages", *args, "--project", str(self.proj)], capture_output=True,
                           text=True, input=stdin, env=env or self.env, timeout=60)
        lines = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
        last = lines[-1]
        if last.get("event") == "done":
            last = {"ok": last["ok"], "data": last.get("data"), "error": last.get("error")}
        return lines, last

    def test_list_empty_then_add_single_product(self):
        _, res = self.run_cli("list")
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["packages"], [])
        self.assertEqual(res["data"]["app_target"], "App")
        self.assertEqual([t["name"] for t in res["data"]["targets"]], ["App", "AppTests"])
        before = (self.proj / "Package.swift").read_text()
        events, res = self.run_cli("add", "--url", "https://github.com/apple/swift-algorithms", "--version", "1.2.0")
        self.assertTrue(res["ok"], res)
        data = res["data"]
        self.assertEqual((data["product_added"], data["target"], data["needs_product"]), ("Algorithms", "App", False))
        self.assertEqual(data["package"]["resolved"]["version"], "1.2.0")
        self.assertEqual(data["package"]["used_by"], ["App"])
        self.assertIn('+        .package(url: "https://github.com/apple/swift-algorithms", from: "1.2.0"),',
                      data["diff"])
        self.assertIn('.product(name: "Algorithms", package: "swift-algorithms")', data["diff"])
        self.assertEqual(data["next"], "Check build")
        steps = {}
        for e in events:
            if e.get("event") == "step":
                steps[e["id"]] = e["state"]
        self.assertEqual(steps, {s: "done" for s, _ in P.STEPS_ADD})
        self.assertTrue(any(e.get("event") == "progress" and e.get("fraction") == 1.0 for e in events))
        self.assertTrue(any(e.get("event") == "log" and "Fetched" in e["text"] for e in events))
        self.assertNotEqual((self.proj / "Package.swift").read_text(), before)
        _, res = self.run_cli("list")
        (pkg,) = res["data"]["packages"]
        self.assertEqual((pkg["identity"], pkg["requirement"]["text"], pkg["products_used"]),
                         ("swift-algorithms", "from 1.2.0", ["Algorithms"]))
        self.assertEqual(res["data"]["unresolved"], [])
        # already added
        _, res = self.run_cli("add", "--url", "https://github.com/apple/swift-algorithms.git", "--version", "1.0.0")
        self.assertEqual(res["error"]["code"], "already-added")
        self.assertEqual(res["error"]["identity"], "swift-algorithms")

    def test_add_needs_product_then_add_product(self):
        _, res = self.run_cli("add", "--url", "https://github.com/apple/swift-collections", "--version", "1.1.0")
        data = res["data"]
        self.assertTrue(data["needs_product"])
        self.assertIsNone(data["product_added"])
        self.assertEqual(data["products"], ["Collections", "DequeModule", "_RopeModule"])
        self.assertNotIn(".product(", (self.proj / "Package.swift").read_text())
        _, res = self.run_cli("add-product", "--package", "swift-collections", "--product", "Nope")
        self.assertEqual(res["error"]["code"], "unknown-product")
        _, res = self.run_cli("add-product", "--package", "swift-collections", "--products",
                              '["DequeModule", "Collections"]')
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["products_added"], ["DequeModule", "Collections"])
        self.assertEqual(sorted(res["data"]["package"]["products_used"]), ["Collections", "DequeModule"])
        _, res = self.run_cli("add-product", "--package", "swift-collections", "--product", "Collections")
        self.assertEqual((res["data"]["products_added"], res["data"]["already_linked"]), ([], ["Collections"]))
        _, res = self.run_cli("add-product", "--package", "swift-collections", "--product", "Collections",
                              "--target", "Nope")
        self.assertEqual(res["error"]["code"], "unknown-target")

    def test_failures_put_files_back(self):
        resolved = '{"pins": [], "version": 3}'
        (self.proj / "Package.resolved").write_text(resolved)
        before = (self.proj / "Package.swift").read_text()
        _, res = self.run_cli("add", "--url", "https://github.com/acme/missing", "--version", "1.0.0")
        self.assertEqual(res["error"]["code"], "fetch-failed")
        self.assertEqual(res["error"]["diagnostics"][0]["category"], "package")
        self.assertEqual((self.proj / "Package.swift").read_text(), before)
        self.assertEqual((self.proj / "Package.resolved").read_text(), resolved)
        events, res = self.run_cli("add", "--url", "https://github.com/acme/too-new", "--version", "9.0.0")
        self.assertEqual(res["error"]["code"], "resolve-failed")
        self.assertIn(("fetch", "failed"), [(e["id"], e["state"]) for e in events if e.get("event") == "step"])
        self.assertEqual((self.proj / "Package.swift").read_text(), before)
        for args, code in ((["--url", "nope"], "bad-url"),
                           (["--url", "https://github.com/a/b", "--version", "1.0"], "bad-version"),
                           (["--url", "https://github.com/a/b", "--rule", "branch", "--version", "a b"], "bad-version"),
                           (["--url", "https://github.com/a/b", "--version", "1.0.0", "--target", "Nope"],
                            "unknown-target")):
            _, res = self.run_cli("add", *args)
            self.assertEqual(res["error"]["code"], code, args)
        self.assertEqual((self.proj / "Package.swift").read_text(), before)

    def test_broken_manifest(self):
        p = self.proj / "Package.swift"
        p.write_text(p.read_text().replace('name: "App",', 'name: "App", SYNTAX ERROR', 1))
        _, res = self.run_cli("list")
        self.assertEqual(res["error"]["code"], "manifest-invalid")
        self.assertIn("line 5", res["error"]["message"])

    def test_cancel_kills_swift_and_restores(self):
        self.run_cli("add", "--url", "https://github.com/apple/swift-algorithms", "--version", "1.2.0")
        resolved = (self.proj / "Package.resolved").read_text()
        pidfile = self.dir / "swift.pid"
        env = {**self.env, "FAKE_SWIFT_SLEEP": "30", "FAKE_SWIFT_PIDFILE": str(pidfile)}
        proc = subprocess.Popen([str(HELPER), "packages", "update", "--project", str(self.proj)], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            for line in proc.stdout:
                if "example.com/slow" in line:       # the fake swift is hanging now
                    break
            self.assertIn("partial-pin", (self.proj / "Package.resolved").read_text())
            proc.stdin.write('{"cancel": true}\n')
            proc.stdin.flush()
            out = proc.stdout.read()
            proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdin.close()
            proc.stdout.close()
        done = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(done["error"]["code"], "cancelled")
        self.assertEqual((self.proj / "Package.resolved").read_text(), resolved)
        pid = int(pidfile.read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_sigterm_during_add_restores(self):
        before = (self.proj / "Package.swift").read_text()
        env = {**self.env, "FAKE_SWIFT_SLEEP": "30"}
        proc = subprocess.Popen([str(HELPER), "packages", "add", "--project", str(self.proj), "--url",
                                 "https://github.com/apple/swift-algorithms", "--version", "1.2.0"], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            for line in proc.stdout:
                if "example.com/slow" in line:
                    break
            self.assertIn("swift-algorithms", (self.proj / "Package.swift").read_text())
            proc.send_signal(signal.SIGTERM)
            out = proc.stdout.read()
            proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdin.close()
            proc.stdout.close()
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["error"]["code"], "cancelled")
        self.assertEqual((self.proj / "Package.swift").read_text(), before)
        self.assertFalse((self.proj / "Package.resolved").exists())

    def test_update_reports_changes(self):
        self.run_cli("add", "--url", "https://github.com/apple/swift-algorithms", "--version", "1.2.0")
        self.run_cli("add", "--url", "https://github.com/apple/swift-collections", "--version", "1.1.0",
                     "--product", "Collections")
        events, res = self.run_cli("update", "--package", "swift-collections")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["changes"], [{"identity": "swift-collections", "from": "1.1.4", "to": "1.2.0"}])
        self.assertEqual(res["data"]["summary"], "1 package updated: swift-collections 1.1.4 → 1.2.0")
        self.assertEqual([e["id"] for e in events if e.get("event") == "step" and e["state"] == "done"],
                         ["update", "changes"])
        _, res = self.run_cli("update")
        self.assertEqual(res["data"]["changes"], [{"identity": "swift-algorithms", "from": "1.2.0", "to": "1.2.1"}])
        _, res = self.run_cli("update")
        self.assertTrue(res["data"]["up_to_date"])
        self.assertEqual(res["data"]["summary"], "Everything is up to date")
        _, res = self.run_cli("resolve")
        self.assertEqual(res["data"]["changes"], [])
        _, res = self.run_cli("update", "--package", "nope")
        self.assertEqual(res["error"]["code"], "unknown-package")

    def test_remove_dry_run_and_confirm(self):
        self.run_cli("add", "--url", "https://github.com/apple/swift-algorithms", "--version", "1.2.0")
        self.run_cli("add", "--url", "https://github.com/apple/swift-collections", "--version", "1.1.0",
                     "--product", "Collections")
        manifest = (self.proj / "Package.swift").read_text()
        _, res = self.run_cli("remove", "--package", "swift-collections")
        data = res["data"]
        self.assertTrue(data["dry_run"])
        self.assertTrue(data["valid"])
        self.assertEqual(data["would_remove"], {"package": "swift-collections",
                                                "target_deps": [{"target": "App", "product": "Collections"}]})
        self.assertIn('-        .package(url: "https://github.com/apple/swift-collections", from: "1.1.0"),',
                      data["diff"])
        self.assertEqual((self.proj / "Package.swift").read_text(), manifest)      # nothing written
        _, res = self.run_cli("remove", "--package", "swift-collections", "--confirm")
        self.assertTrue(res["data"]["removed"], res)
        self.assertEqual(res["data"]["pins_removed"], ["swift-collections"])
        text = (self.proj / "Package.swift").read_text()
        self.assertNotIn("swift-collections", text)
        self.assertIn("swift-algorithms", text)
        self.assertNotIn("swift-collections", (self.proj / "Package.resolved").read_text())
        _, res = self.run_cli("remove", "--package", "swift-collections")
        self.assertEqual(res["error"]["code"], "unknown-package")

    def test_remove_refuses_invalid_edit(self):
        p = self.proj / "Package.swift"
        p.write_text(p.read_text().replace(
            "    targets: [",
            '    dependencies: [\n        .package(url: "https://github.com/apple/swift-log", from: "1.5.0"),\n    ],\n'
            "    targets: [", 1) + '\npackage.dependencies.append(.package(url: "https://github.com/apple/'
                                   'swift-collections", from: "1.1.0"))\n')
        before = p.read_text()
        _, res = self.run_cli("remove", "--package", "swift-collections", "--confirm")
        self.assertEqual(res["error"]["code"], "edit-invalid")
        self.assertFalse(res["error"]["valid"])
        self.assertIn("plain array entry", res["error"]["problems"][0])
        self.assertEqual(p.read_text(), before)

    def test_busy(self):
        root = Path(os.path.realpath(self.proj))
        with mock.patch.object(P, "LOCKS", self.dir / "state/ios-studio/packages"):
            with P.project_lock(root):
                _, res = self.run_cli("add-product", "--package", "x", "--product", "X")
        self.assertEqual(res["error"]["code"], "package-busy")

    def test_demo(self):
        env = {**self.env, "IOSSTUDIO_DEMO": "1"}
        proj = str(Path.home() / "Developer/WeatherGlance")

        def demo(*args):
            r = subprocess.run([str(HELPER), "packages", *args, "--project", proj], capture_output=True, text=True,
                               env=env, timeout=60)
            lines = [json.loads(ln) for ln in r.stdout.splitlines()]
            return lines, lines[-1]

        _, res = demo("list")
        self.assertEqual(len(res["data"]["packages"]), 3)
        self.assertEqual(res["data"]["packages"][0]["resolved"]["version"], "1.1.4")
        self.assertEqual(res["data"]["packages"][2]["kind"], "local")
        events, done = demo("add", "--url", "https://github.com/apple/swift-argument-parser")
        self.assertTrue(done["data"]["demo"])
        self.assertEqual(done["data"]["product_added"], "ArgumentParser")
        self.assertTrue(any(e.get("event") == "progress" for e in events))
        _, done = demo("add", "--url", "https://github.com/apple/swift-collections")
        self.assertEqual(done["error"]["code"], "already-added")
        _, done = demo("update")
        self.assertEqual(done["data"]["changes"][0]["to"], "1.2.1")
        _, res = demo("remove", "--package", "swift-algorithms")
        self.assertTrue(res["data"]["dry_run"])
        self.assertIn("-", res["data"]["diff"])
        self.assertFalse((Path.home() / "Developer/WeatherGlance/.iosstudio-demo-written").exists())


# ================================================================ end to end (real swift + network)

@unittest.skipUnless(SWIFT and _network(), "needs swift and network")
class EndToEnd(unittest.TestCase):
    def test_add_link_resolve_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(os.path.realpath(tmp))
            proj = make_project(d / "E2E")
            env = {**os.environ, "XDG_CACHE_HOME": str(d / "cache"), "XDG_STATE_HOME": str(d / "state")}
            env.pop("IOSSTUDIO_DEMO", None)
            env.pop("IOSSTUDIO_SWIFT", None)

            def cli(*args):
                r = subprocess.run([str(HELPER), "packages", *args, "--project", str(proj)], capture_output=True,
                                   text=True, env=env, timeout=600, stdin=subprocess.DEVNULL)
                last = json.loads(r.stdout.strip().splitlines()[-1])
                return {"ok": last["ok"], "data": last.get("data"), "error": last.get("error")}

            res = cli("add", "--url", "https://github.com/apple/swift-collections", "--version", "1.1.0",
                      "--product", "DequeModule")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["data"]["product_added"], "DequeModule")
            self.assertIn("Collections", res["data"]["products"])
            pins = M.parse_resolved((proj / "Package.resolved").read_text())
            self.assertIn("swift-collections", pins)
            res = cli("list")
            (pkg,) = res["data"]["packages"]
            self.assertEqual(pkg["products_used"], ["DequeModule"])
            self.assertEqual(pkg["resolved"]["version"], pins["swift-collections"]["version"])
            res = cli("resolve")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["data"]["changes"], [])
            res = cli("remove", "--package", "swift-collections", "--confirm")
            self.assertTrue(res["data"]["removed"], res)
            self.assertNotIn("swift-collections", (proj / "Package.swift").read_text())


if __name__ == "__main__":
    unittest.main()
