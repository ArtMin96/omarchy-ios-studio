"""Project config checks (helper/project_check.py, `projects check`, the pre-build run in projects.py).

One fixture project per check in tests/fixtures/project-check/<case>/, plus key parsing from xtool's source,
the time budget, the output cap and a compile-only build through a fake xtool. Stdlib only:
    python3 -m unittest tests.test_project_check
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "helper"))

import project_check as C  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "project-check"
NO_CACHE = Path(tempfile.gettempdir()) / "ios-studio-test-no-xtool-cache"


def run(case: str, **kw) -> list[dict]:
    return C.check(FIX / case, cache_dir=kw.pop("cache_dir", NO_CACHE), **kw)["issues"]


def by(issues: list[dict], check: str) -> list[dict]:
    return [i for i in issues if i["check"] == check]


class Shape(unittest.TestCase):
    def test_issue_shape_and_warnings_only(self):
        for case in sorted(p.name for p in FIX.iterdir() if p.is_dir()):
            for i in run(case):
                self.assertEqual(set(i), {"severity", "category", "check", "message", "file", "line", "hint"}, i)
                self.assertIn(i["severity"], ("warning", "note"))
                self.assertEqual(i["category"], "config")
                self.assertIn(i["check"], C.CHECKS)
                self.assertTrue(i["hint"])
                self.assertFalse(i["file"] and os.path.isabs(i["file"]), i)

    def test_clean_project_has_nothing(self):
        self.assertEqual(run("clean"), [])

    def test_never_writes(self):
        before = {p: p.stat().st_mtime_ns for p in FIX.rglob("*")}
        for case in (p for p in FIX.iterdir() if p.is_dir()):
            C.check(case, cache_dir=NO_CACHE)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in FIX.rglob("*")})


class XtoolKeys(unittest.TestCase):
    def test_typos_suggest_the_right_key(self):
        issues = by(run("xtool-keys"), "xtool-keys")
        msgs = {i["message"]: i for i in issues}
        typo = msgs["xtool.yml has an unknown key 'bundleId': did you mean 'bundleID'?"]
        self.assertEqual((typo["file"], typo["line"]), ("xtool.yml", 2))
        self.assertIn("Rename it to 'bundleID'", typo["hint"])
        self.assertIn("xtool.yml has an unknown key 'iconFile': did you mean 'iconPath'?", msgs)
        self.assertIn("xtool.yml has an unknown key 'foo'.", msgs)
        self.assertIn("xtool.yml has an unknown key 'bundelID' in extensions: did you mean 'bundleID'?", msgs)
        self.assertIn("infoPath points to Missing.plist, which doesn't exist.", msgs)
        # the bundleId typo already explains the missing bundle id
        self.assertFalse(any("neither bundleID nor orgID" in m for m in msgs))

    def test_suggest(self):
        k = C.BUILTIN_KEYS
        for wrong, right in [("bundleId", "bundleID"), ("bundelID", "bundleID"), ("bundleIdentifier", "bundleID"),
                             ("BundleID", "bundleID"), ("orgId", "orgID"), ("infoPlist", "infoPath"),
                             ("entitlements", "entitlementsPath"), ("skipLsp", "skipLSP"), ("resource", "resources")]:
            self.assertEqual(C.suggest(wrong, k), right, wrong)
        self.assertIsNone(C.suggest("foo", k))

    def test_keys_from_cached_xtool_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(C.valid_keys(Path(tmp))[2], "builtin")
            src = Path(tmp) / "xtool-abc123/Sources/PackLib"
            src.mkdir(parents=True)
            (src / "PackSchema.swift").write_text(
                "public struct PackSchemaBase: Codable, Sendable {\n"
                "    public enum Version: Int, Codable, Sendable {\n        case v1 = 1\n    }\n"
                "    public var version: Version\n    public var bundleID: String?\n"
                "    public var newKey: Bool?\n"
                "    public struct Extension: Codable, Sendable {\n        public var product: String\n"
                "        public var extOnly: String?\n    }\n}\n")
            top, ext, source = C.valid_keys(Path(tmp))
        self.assertEqual(source, "xtool-cache")
        self.assertEqual(top, ["version", "bundleID", "newKey"])
        self.assertEqual(ext, ["product", "extOnly"])

    def test_real_cache_matches_builtin_when_present(self):
        top, ext, source = C.valid_keys()
        if source != "xtool-cache":
            self.skipTest("no cached xtool source on this machine")
        self.assertEqual(sorted(top), sorted(C.BUILTIN_KEYS))
        self.assertEqual(sorted(ext), sorted(C.BUILTIN_EXTENSION_KEYS))


class UsageDescriptions(unittest.TestCase):
    def test_missing_and_empty(self):
        issues = by(run("usage"), "usage-descriptions")
        cam = next(i for i in issues if "NSCameraUsageDescription" in i["message"])
        # line 3 mentions AVCaptureSession in a comment; the first real use is on line 5
        self.assertEqual((cam["file"], cam["line"]), ("Sources/App/Camera.swift", 5))
        loc = next(i for i in issues if "NSLocationWhenInUseUsageDescription" in i["message"])
        self.assertEqual((loc["file"], loc["line"]), ("Sources/App/Map.swift", 4))
        empty = next(i for i in issues if "is empty" in i["message"])
        self.assertEqual((empty["file"], empty["line"]), ("Info.plist", 5))
        self.assertFalse(any("Photo" in i["message"] for i in issues))     # PhotosPicker needs no permission
        self.assertEqual(len(issues), 3)

    def test_no_info_plist_at_all(self):
        [i] = by(run("usage-noplist"), "usage-descriptions")
        self.assertIn("NSBluetoothAlwaysUsageDescription", i["message"])
        self.assertIn("infoPath", i["hint"])

    def test_present_key_is_quiet(self):
        self.assertEqual(by(run("clean"), "usage-descriptions"), [])

    def test_one_table(self):
        for keys, what, pats in C.USAGE:
            self.assertTrue(keys and what and pats)
            self.assertTrue(all(C._needle(p) for p in pats), pats)


class Icon(unittest.TestCase):
    def test_size_and_alpha(self):
        msgs = [i["message"] for i in by(run("icon-size-alpha"), "icon")]
        self.assertEqual(msgs, ["The icon is 512×512; it should be 1024×1024.",
                                "The icon has transparency (an alpha channel)."])

    def test_trns_chunk_counts_as_alpha(self):
        self.assertEqual([i["message"] for i in by(run("icon-trns"), "icon")],
                         ["The icon has transparency (an alpha channel)."])

    def test_not_png_and_missing(self):
        self.assertIn("isn't a PNG", by(run("icon-not-png"), "icon")[0]["message"])
        [m] = by(run("icon-missing"), "icon")
        self.assertEqual((m["file"], m["line"]), ("xtool.yml", 3))

    def test_no_icon_is_a_note(self):
        [n] = by(run("bundle-id"), "icon")
        self.assertEqual(n["severity"], "note")


class Entitlements(unittest.TestCase):
    def test_paid_only(self):
        issues = by(run("entitlements"), "entitlements")
        self.assertEqual([i["message"].split(" ")[0] for i in issues],
                         ["aps-environment", "com.apple.developer.icloud-container-identifiers"])  # iCloud once
        self.assertTrue(all(i["file"] == "App.entitlements" and i["line"] for i in issues))
        self.assertFalse(any("healthkit" in i["message"] for i in issues))       # free teams may use HealthKit


class BundleAndVersion(unittest.TestCase):
    def test_bundle_id(self):
        [b] = by(run("bundle-id"), "bundle-id")
        self.assertIn("com.example.My_App", b["message"])
        self.assertIn("underscore", b["message"])
        self.assertIn("space", by(run("bundle-id-space"), "bundle-id")[0]["message"])
        self.assertEqual(by(run("clean"), "bundle-id"), [])

    def test_versions(self):
        issues = by(run("version"), "version")
        self.assertEqual(len(issues), 2)
        self.assertIn("1.0 beta", issues[0]["message"])
        self.assertIn("build variable", issues[1]["message"])
        self.assertEqual(issues[1]["file"], "Info.plist")


class Bounds(unittest.TestCase):
    def test_budget_stops_early(self):
        t0 = time.monotonic()
        res = C.check(FIX / "usage", deadline=time.monotonic() - 1, cache_dir=NO_CACHE)
        self.assertTrue(res["partial"])
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(by(res["issues"], "usage-descriptions"), [])   # skipped, not guessed

    def test_big_project_stays_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "Package.swift").write_text("// swift-tools-version: 6.0\n")
            (p / "xtool.yml").write_text("version: 1\nbundleID: com.example.Big\n")
            src = p / "Sources/App"
            src.mkdir(parents=True)
            body = "let x = 1 // CLLocationManager in a comment\n" * 2000
            for n in range(400):
                (src / f"F{n}.swift").write_text(body)
            t0 = time.monotonic()
            res = C.check(p, deadline=time.monotonic() + 0.8)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(by(res["issues"], "usage-descriptions"), [])

    def test_output_cap(self):
        issues = [C._issue("xtool-keys", f"m{n}", "h") for n in range(30)] + [C._issue("icon", "n", "h", severity="note")]
        s = C.summarize("/p", issues, "builtin", 5, max_issues=10)
        self.assertEqual((s["total"], s["shown"], s["truncated"]), (31, 10, True))
        self.assertEqual(len(s["issues"]), 10)
        self.assertEqual(s["counts"], {"error": 0, "warning": 30, "note": 1})
        self.assertEqual(s["checks"], C.CHECKS)


# ---------------------------------------------------------------- CLI and builds

FAKE_XTOOL = """\
#!/usr/bin/env python3
import sys
print("fake xtool", " ".join(sys.argv[1:]))
print("Building for debugging...")
print("Build complete! (0.1 secs)")
"""


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-cli-"))
        self.env = dict(os.environ)
        for var in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            self.env[var] = str(self.tmp / var.lower())
        self.env.pop("IOSSTUDIO_DEMO", None)
        xtool = self.tmp / "xtool"
        xtool.write_text(FAKE_XTOOL)
        xtool.chmod(0o755)
        self.env["IOS_STUDIO_XTOOL"] = str(xtool)

    def helper(self, *args, env=None) -> list[dict]:
        r = subprocess.run([sys.executable, str(ROOT / "helper" / "main.py"), *args], env=env or self.env,
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]

    def test_projects_check(self):
        [out] = self.helper("projects", "check", "--path", str(FIX / "xtool-keys"), "--max", "2")
        d = out["data"]
        self.assertEqual(set(d), {"path", "issues", "total", "shown", "truncated", "counts", "elapsedMs",
                                  "keysSource", "checks"})
        self.assertEqual((d["shown"], d["truncated"]), (2, True))
        self.assertGreater(d["total"], 2)
        self.assertEqual(d["counts"]["error"], 0)

    def test_not_a_project(self):
        [out] = self.helper("projects", "check", "--path", str(self.tmp))
        self.assertEqual(out["error"]["code"], "not-a-project")

    def test_demo(self):
        env = dict(self.env, IOSSTUDIO_DEMO="1")
        [out] = self.helper("projects", "check", "--path", "/x", env=env)
        msgs = " ".join(i["message"] for i in out["data"]["issues"])
        self.assertIn("did you mean 'bundleID'", msgs)
        self.assertIn("NSCameraUsageDescription", msgs)

    def test_prebuild_warnings_come_first_and_never_fail_the_build(self):
        proj = self.tmp / "App"
        proj.mkdir()
        (proj / "Package.swift").write_text('// swift-tools-version: 6.0\nlet package = Package(name: "App")\n')
        (proj / "xtool.yml").write_text("version: 1\nbundleID: com.example.App\nbundleId: x\n")
        (proj / "Sources/App").mkdir(parents=True)
        (proj / "Sources/App/Cam.swift").write_text("import AVFoundation\nlet s = AVCaptureSession()\n")
        ev = self.helper("build", "compile", "--path", str(proj))
        done = ev[-1]
        self.assertTrue(done["ok"], done)
        kinds = [e.get("event") for e in ev]
        first_diag, first_log = kinds.index("diagnostic"), kinds.index("log")
        self.assertLess(first_diag, first_log)
        diags = [e for e in ev if e.get("event") == "diagnostic"]
        self.assertEqual({d["check"] for d in diags}, {"xtool-keys", "usage-descriptions"})
        self.assertTrue(all(d["severity"] == "warning" and d["category"] == "config" for d in diags))
        cam = next(d for d in diags if d["check"] == "usage-descriptions")
        self.assertEqual(cam["file"], str(proj / "Sources/App/Cam.swift"))   # absolute, like compiler ones
        self.assertEqual(done["data"]["errors"], 0)
        hist = self.helper("build", "history")[-1]["data"]["builds"][0]
        self.assertEqual(hist["result"], "ok")
        self.assertTrue(any(d.get("check") == "xtool-keys" for d in hist["diagnostics"]))


if __name__ == "__main__":
    unittest.main()
