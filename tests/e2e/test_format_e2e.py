"""Format and lint with the real swift-format (/usr/lib/swift/usr/bin/swift-format; skipped without it): a .swift-format
with 4-space indentation gives a different diff than the default 2."""
from __future__ import annotations

import json
import os
import textwrap
import unittest
from pathlib import Path

from iosstudio.features import format as F
from support.projects import project_with_files as make_project
from support.swift_format import TempHome

REAL_SF = Path("/usr/lib/swift/usr/bin/swift-format")


@unittest.skipUnless(REAL_SF.is_file() and os.access(REAL_SF, os.X_OK), "swift-format isn't installed")
class RealSwiftFormat(TempHome):
    SRC = textwrap.dedent("""\
        import SwiftUI
        struct ContentView: View {
            var body: some View {
                VStack{
                    Text("Hi")
                }
            }
        }
        func foo( a:Int,b :Int )->Int{ return a+b }
        """)

    def test_end_to_end_and_config(self):
        root = make_project(self.dir / "App", {"Sources/App/ContentView.swift": self.SRC,
                                               "Sources/App/Broken.swift": "func x( {\n"})
        default = F.run_format(root, sf=[str(REAL_SF)])
        self.assertIn("Sources/App/ContentView.swift", default["files_changed"])
        self.assertIn("+  var body: some View {", default["diff"])           # default: 2 spaces
        self.assertIn("+func foo(a: Int, b: Int) -> Int { return a + b }", default["diff"])
        self.assertEqual([e["file"] for e in default["errors"]], ["Sources/App/Broken.swift"])
        self.assertIsNone(default["config_path"])
        (root / ".swift-format").write_text(json.dumps({"version": 1, "indentation": {"spaces": 4}}))
        four = F.run_format(root, sf=[str(REAL_SF)])
        self.assertEqual(four["config_path"], ".swift-format")
        self.assertNotEqual(four["diff"], default["diff"])
        self.assertNotIn("+  var body: some View {", four["diff"])            # 4 spaces: that line stays
        self.assertIn("+        VStack {", four["diff"])
        applied = F.run_format(root, apply=True, sf=[str(REAL_SF)])
        self.assertIn("    var body: some View {\n", (root / "Sources/App/ContentView.swift").read_text())
        self.assertEqual(applied["files_changed"], four["files_changed"])
        self.assertEqual(F.run_format(root, sf=[str(REAL_SF)])["files_changed"], [])
        lint = F.run_lint(root, sf=[str(REAL_SF)], use_swiftlint=False)
        rules = {d["rule"] for d in lint["diagnostics"]}
        self.assertIn("syntax", rules)
        self.assertNotIn("Indentation", rules)       # formatted with the same config: no whitespace findings
