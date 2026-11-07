"""Swift packages through bin/iosstudio-helper with tests/fixtures/packages/fake_swift.py as swift: add with one
product, a product choice then add-product, failures and cancel/SIGTERM putting Package.swift and Package.resolved
back and killing swift, update with changes, remove dry run and confirm, a refused edit, busy, and demo mode."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import packages as P
from support import HELPER
from support.fakes import FAKE_SWIFT
from support.projects import package_project as make_project


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
