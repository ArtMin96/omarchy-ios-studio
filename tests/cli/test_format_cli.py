"""Format and lint through bin/iosstudio-helper with a fake swift-format, and demo mode."""
from __future__ import annotations

import json
from pathlib import Path

from support import helper_json as helper
from support.projects import project_with_files as make_project
from support.swift_format import TempHome


class Cli(TempHome):
    def env(self) -> dict:
        return {"IOSSTUDIO_SWIFT_FORMAT": str(self.fake), "IOSSTUDIO_SWIFTLINT": str(self.dir / "none"),
                "XDG_CACHE_HOME": str(self.dir / "xc"), "XDG_STATE_HOME": str(self.dir / "xs")}

    def test_format_and_lint(self):
        root = make_project(self.dir / "App", {"Sources/App/A.swift": "let c = a+b\n", "Sources/App/B.swift": "x\n"})
        out = helper("format", "--project", str(root), env=self.env())
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["files_changed"], ["Sources/App/A.swift"])
        out = helper("projects", "format", "--path", str(root), "--file", "Sources/App/B.swift", env=self.env())
        self.assertEqual(out["data"]["files_changed"], [])
        self.assertEqual(out["data"]["files_checked"], 1)
        out = helper("format", "--project", str(root), "--files", json.dumps(["Sources/App/A.swift", "/etc/passwd"]),
                     "--apply", env=self.env())
        self.assertEqual(out["data"]["files_changed"], ["Sources/App/A.swift"])
        self.assertEqual(out["data"]["errors"][0]["file"], "/etc/passwd")
        self.assertEqual((root / "Sources/App/A.swift").read_text(), "let c = a + b\n")
        lint = helper("format-lint", "--project", str(root), env=self.env())
        self.assertTrue(lint["ok"])
        self.assertEqual(lint["data"]["total"], 0)
        last = helper("format-lint", "--project", str(root), "--last", env=self.env())
        self.assertTrue(last["data"]["cached"])
        self.assertTrue((self.dir / "xs/ios-studio/lint").is_dir())

    def test_errors(self):
        out = helper("format", "--project", str(self.dir / "missing"), env=self.env())
        self.assertEqual(out["error"]["code"], "not-found")
        root = make_project(self.dir / "App", {"Sources/App/A.swift": ""})
        out = helper("format", "--project", str(root), env={**self.env(), "IOSSTUDIO_SWIFT_FORMAT": "/nonexistent"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "missing-swift-format")
        self.assertIn("hint", out["error"])
        out = helper("format-lint", "--project", str(root), "--max", "x", env=self.env())
        self.assertEqual(out["error"]["code"], "usage")

    def test_demo(self):
        home = str(Path.home())
        out = helper("format", "--project", home + "/Developer/WeatherGlance", env={"IOSSTUDIO_DEMO": "1"})
        self.assertTrue(out["data"]["demo"])
        self.assertEqual(out["data"]["files_changed_total"], 2)
        self.assertIn("+  let days: [Day]", out["data"]["diff"])
        out = helper("format", "--project", home + "/Developer/RecipeBox", "--apply", env={"IOSSTUDIO_DEMO": "1"})
        self.assertEqual((out["data"]["files_changed"], out["data"]["note"]), ([], "Already formatted."))
        out = helper("format-lint", "--project", home + "/Developer/WeatherGlance", "--last",
                     env={"IOSSTUDIO_DEMO": "1"})
        d = out["data"]
        self.assertTrue(d["cached"])
        self.assertTrue(all(x["file"].startswith(home + "/Developer/WeatherGlance/Sources/") for x in d["diagnostics"]))
        self.assertEqual({x["category"] for x in d["diagnostics"]}, {"style"})
