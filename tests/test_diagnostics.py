"""Build output parser (helper/diagnostics.py) and its use in BuildRunner (helper/projects.py).

Stdlib only, like the helper. Run from the repo root:
    python3 -m unittest discover -s tests          (or: python3 -m pytest tests)

Fixtures in tests/fixtures/build-output/: real-*.txt are real `xtool dev build` output captured on Linux
(ANSI codes stripped, paths shortened to /home/user/Developer); synthetic-*.txt are written from the
formats xtool, SwiftPM, ld64 and swift-frontend are known to print, for failures that need an Apple ID,
an iPhone or a crashing compiler to reproduce.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "build-output"
_TMP = tempfile.mkdtemp(prefix="ios-studio-tests-")
for var in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
    os.environ[var] = os.path.join(_TMP, var.lower())   # never touch the real state/config
sys.path.insert(0, str(ROOT / "helper"))

import common  # noqa: E402
import diagnostics as D  # noqa: E402
import projects  # noqa: E402

READABLE = {"$s7LinkErr5useIts5Int32VyF": "useIt()"}


def fake_demangle(names):
    return {n: READABLE[n] for n in names if n in READABLE}


def parse(name: str, failed: bool = True) -> list[dict]:
    return D.parse((FIXTURES / name).read_text().splitlines(), failed=failed, demangler=fake_demangle)


def by_cat(diags: list[dict], cat: str) -> list[dict]:
    return [d for d in diags if d["category"] == cat]


class RealOutput(unittest.TestCase):
    """Real xtool / swift-build output."""

    def test_success_has_no_diagnostics(self):
        self.assertEqual(parse("real-success.txt", failed=False), [])

    def test_compiler_error_keeps_old_shape(self):
        ds = parse("real-compiler.txt")
        self.assertEqual(len(ds), 1)          # notes from Swift's interface aren't absolute paths: skipped
        d = ds[0]
        self.assertEqual(d["category"], "compiler")
        self.assertEqual((d["severity"], d["line"], d["col"]), ("error", 4, 8))
        self.assertTrue(d["file"].endswith("/Sources/Bad2/App.swift"))
        self.assertEqual(d["message"], "type 'S' does not conform to protocol 'Identifiable'")
        self.assertNotIn("hint", d)

    def test_linker_blocks_collapse(self):
        ds = parse("real-linker-lld.txt")
        self.assertEqual([d["category"] for d in ds], ["linker", "linker"])   # no noise warnings, no trailers
        undef, dup = ds
        self.assertEqual(undef["message"], "Undefined symbols: another_missing_one, totally_missing_function")
        self.assertTrue(undef["file"].endswith("/Sources/LinkErr/App.swift"))
        self.assertEqual(undef["line"], 5)
        self.assertIn("useIt() in LinkErr.o(App.o) (App.swift:5)", undef["details"][0])
        self.assertIn("Package.swift", undef["hint"])
        self.assertEqual(dup["message"], "Duplicate symbol: dup_symbol_x")
        self.assertEqual(dup["details"], ["dup_symbol_x defined in LinkErr.o(App.o), LinkErr.o(Other.o)"])
        self.assertNotIn("file", dup)          # <compiler-generated> isn't a place to open

    def test_no_such_module(self):
        (d,) = parse("real-no-module.txt")
        self.assertEqual(d["category"], "compiler")
        self.assertEqual(d["line"], 1)
        self.assertIn("NotARealModule", d["message"])
        self.assertIn("Package.swift", d["hint"])

    def test_package_clone_failure(self):
        (d,) = parse("real-package-clone.txt")
        self.assertEqual(d["category"], "package")
        self.assertIn("Failed to clone repository https://github.com/nonexistent-org-zzq", d["message"])
        self.assertIn("not found", d["message"])
        self.assertIn("URL", d["hint"])
        self.assertFalse(any("Cloning into" in x for x in d["details"]))

    def test_package_resolution(self):
        (d,) = parse("real-package-resolve.txt")
        self.assertEqual(d["category"], "package")
        self.assertTrue(d["message"].startswith("Dependencies could not be resolved because"))
        self.assertIn("version requirement", d["hint"])

    def test_manifest_errors(self):
        ds = parse("real-package-manifest.txt")
        self.assertEqual({d["category"] for d in ds}, {"package"})
        self.assertEqual(ds[0]["message"], "'manifest': Invalid manifest")    # the swiftc command line is gone
        located = [d for d in ds if d.get("file")]
        self.assertEqual(len(located), 3)
        self.assertTrue(all(d["file"].endswith("/Package.swift") and d["line"] == 10 for d in located))

    def test_unknown_package(self):
        (d,) = parse("real-package-unknown.txt")
        self.assertEqual(d["category"], "package")
        self.assertEqual(d["message"], "'missingprod': unknown package 'bar' in dependencies of target 'MissingProd'")

    def test_xtool_yml(self):
        (a,) = parse("real-xtool-yml-decoding.txt")
        (b,) = parse("real-xtool-yml-product.txt")
        self.assertEqual((a["category"], b["category"]), ("config", "config"))
        self.assertIn("Path: version", a["message"])
        self.assertIn("ios_project_info", b["hint"])


class AssumedFormats(unittest.TestCase):
    """Formats taken from xtool's source and the tools' documented output."""

    def test_ld64_blocks(self):
        ds = parse("synthetic-ld64-undefined.txt")
        self.assertEqual(len(ds), 2)
        undef, dup = ds
        self.assertTrue(undef["message"].startswith("Undefined symbols: _sqlite3_open, "))
        self.assertIn("Store.o", undef["details"][0])
        self.assertIn("StatsView.o", undef["details"][1])
        self.assertEqual(dup["details"], ["_helper defined in A.o, B.o"])

    def test_missing_library_and_framework(self):
        ds = parse("synthetic-lld-missing-lib.txt")
        self.assertEqual([d["message"] for d in ds],
                         ["library not found for -lsqlite4", "framework not found for -framework AppKit"])
        self.assertTrue(all(d["category"] == "linker" and "iOS SDK" in d["hint"] for d in ds))

    def test_compiler_crash_is_one_diagnostic(self):
        (d,) = parse("synthetic-compiler-crash.txt")
        self.assertEqual(d["category"], "toolchain")
        self.assertEqual(d["message"], "The Swift compiler crashed (while type-checking expression)")
        self.assertEqual((d["line"], d["col"]), (19, 13))
        self.assertTrue(d["file"].endswith("StreakView.swift"))
        self.assertLessEqual(len(d["details"]), D.MAX_DETAILS + 1)
        self.assertFalse(any("0x0000" in x for x in d["details"]))

    def test_signing(self):
        ds = parse("synthetic-signing.txt")
        self.assertEqual({d["category"] for d in ds}, {"signing"})
        hints = [d["hint"] for d in ds]
        self.assertIn("10 new app ids", hints[0])
        self.assertEqual(ds[0]["message"], "Your maximum App ID limit has been reached. You may create up to 10 "
                                           "App IDs every 7 days. (9401)")
        self.assertIn("3 of your apps", hints[1])
        self.assertIn("ios_account_status", hints[2])
        self.assertIn("certificates", hints[3])
        self.assertIn("ios_project_renew", hints[4])
        self.assertIn("ios_signing_status", hints[5])

    def test_device(self):
        ds = parse("synthetic-device.txt")
        self.assertEqual({d["category"] for d in ds}, {"device"})
        self.assertEqual(len(ds), 3)          # "Please unlock your device..." is progress, not an error
        self.assertIn("ios_device_prepare", ds[1]["hint"])

    def test_toolchain_config_other(self):
        ds = parse("synthetic-toolchain-config.txt")
        self.assertEqual([d["category"] for d in ds], ["toolchain", "config", "config", "other"])
        self.assertIn("ios_toolchain_check", ds[0]["hint"])
        self.assertNotIn("hint", ds[3])

    def test_package_network_and_product(self):
        (net,) = parse("synthetic-package-network.txt")
        self.assertEqual(net["category"], "package")
        self.assertIn("internet", net["hint"])
        self.assertIn("Could not resolve host", net["message"])
        (prod,) = parse("synthetic-package-product.txt")
        self.assertEqual(prod["category"], "package")


class Bounds(unittest.TestCase):
    def test_message_and_details_are_capped(self):
        long = "x" * 5000
        ds = D.parse([f"error: {long}"], demangler=None)
        self.assertLessEqual(len(ds[0]["message"]), D.MAX_MESSAGE)
        lines = [f"ld64.lld: error: undefined symbol: sym{i}\n>>> referenced by f{i}.o" for i in range(40)]
        (d,) = D.parse("\n".join(lines).splitlines(), demangler=None)
        self.assertEqual(len(d["details"]), D.MAX_DETAILS + 1)
        self.assertEqual(d["details"][-1], f"+{40 - D.MAX_DETAILS} more")
        self.assertIn("and 35 more", d["message"])
        self.assertTrue(all(len(x) <= D.MAX_DETAIL_CHARS for x in d["details"]))

    def test_location_less_diagnostics_are_capped_and_deduplicated(self):
        ds = D.parse([f"error: problem {i}" for i in range(200)] + ["error: problem 1"] * 5, demangler=None)
        self.assertEqual(len(ds), D.MAX_LOOSE)

    def test_block_stops_after_max_lines(self):
        lines = ["Stack dump:"] + [f"{i}.\tWhile doing step {i}" for i in range(D.MAX_BLOCK_LINES + 50)]
        ds = D.parse(lines, demangler=None)
        self.assertEqual(len(ds), 1)

    def test_trailer_only_when_nothing_else(self):
        self.assertEqual(D.parse(["error: Build failed", "Error: exited(1)"], failed=False), [])
        (d,) = D.parse(["error: Build failed", "Error: exited(1)"], failed=True, demangler=None)
        self.assertEqual((d["category"], d["message"]), ("other", "Build failed"))
        self.assertIn("ios_build_log", d["hint"])
        (d,) = D.parse(["error: Ld /x/App normal failed with a nonzero exit code. Command line: cd /x",
                        "error: Build failed"], demangler=None)
        self.assertEqual((d["category"], d["message"]), ("linker", "Ld /x/App normal failed with a nonzero exit code."))


class Runner(unittest.TestCase):
    """BuildRunner wiring: events, failed step, failure message."""

    def run_fixture(self, name: str, plan: list[str], only: int | None = None):
        events: list[dict] = []
        old = common._write
        common._write = events.append
        try:
            runner = projects.BuildRunner(plan, False, Path(_TMP) / f"{name}.log", None)
            for ln in (FIXTURES / name).read_text().splitlines()[:only]:
                runner.on_line(ln)
            runner.flush_diags(failed=True)
            err = projects.classify_failure(runner, 1)
            runner.finish_all(False)
            runner.close()
        finally:
            common._write = old
        return runner, err, events

    def test_linker_failure(self):
        runner, err, events = self.run_fixture("real-linker-lld.txt", ["resolve", "compile", "link"])
        diags = [e for e in events if e.get("event") == "diagnostic"]
        self.assertEqual(len(diags), 2)
        self.assertEqual(err.code, "build-failed")
        self.assertTrue(err.message.startswith("Linking failed: Undefined symbols"))
        self.assertEqual(runner.state["link"], "failed")

    def test_compile_failure_unchanged(self):
        runner, err, _ = self.run_fixture("real-compiler.txt", ["resolve", "compile", "link"])
        self.assertEqual(err.code, "compile-errors")
        self.assertIn("App.swift, line 4", err.message)
        self.assertEqual(runner.state["compile"], "failed")

    def test_package_failure(self):
        runner, err, _ = self.run_fixture("real-package-resolve.txt", ["resolve", "compile", "link"])
        self.assertTrue(err.message.startswith("Swift packages couldn't be resolved: Dependencies"))
        self.assertEqual(runner.state["resolve"], "failed")

    def test_manifest_failure_names_the_line(self):
        _, err, _ = self.run_fixture("real-package-manifest.txt", ["resolve", "compile", "link"])
        self.assertEqual(err.message, "Package.swift has errors. The first is on line 10: expected initial value "
                                      "after '='")

    def test_app_id_limit_keeps_its_code(self):
        _, err, _ = self.run_fixture("synthetic-signing.txt", ["resolve", "compile", "link", "sign"], only=2)
        self.assertEqual(err.code, "app-id-limit")

    def test_crash_names_the_crash(self):
        runner, err, _ = self.run_fixture("synthetic-compiler-crash.txt", ["resolve", "compile", "link"])
        self.assertEqual(err.message, "The Swift compiler crashed (while type-checking expression).")


if __name__ == "__main__":
    unittest.main()
