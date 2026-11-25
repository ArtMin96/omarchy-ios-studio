"""The "App with unit tests" template, and what create_app copies from a template (Package.swift.template,
Sources/<Target>/**, Tests/**) on top of the flat Sources/*.swift copy every template has."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from iosstudio.core import subproc
from iosstudio.core.output import HelperError
from iosstudio.features.projects import model

SWIFT = Path("/usr/bin/swift")


@pytest.fixture
def no_xtool(monkeypatch):
    """create_app writes the built-in skeleton (what `xtool new --skip-setup` writes) instead of running xtool."""
    monkeypatch.setattr(subproc, "which", lambda name: None)


def _new(tmp_path: Path, name: str, template: str) -> Path:
    dest, tmpl_id, used_xtool, _ = model.create_app({"name": name, "template": template, "dir": str(tmp_path),
                                                     "git": "0", "agent_files": "off"})
    assert (tmpl_id, used_xtool) == (template, False)
    return dest


def _tree(root: Path) -> dict[str, str]:
    return {str(f.relative_to(root)): f.read_text() for f in sorted(root.rglob("*"))
            if f.is_file() and ".bsp" not in f.parts}


def test_templates_lists_it():
    meta = next(m for m in model.templates() if m["id"] == "app-with-tests")
    assert meta["title"] == "App with unit tests"
    assert "AppCore" in meta["description"] and "computer" in meta["description"]
    assert meta["glyph"] == "testTube" and not meta.get("default")
    assert meta["files"] == ["App.swift", "ContentView.swift"]
    assert model.templates()[0]["id"] == "swiftui-app"     # still the default


def test_create_app_writes_the_package_targets_and_tests(tmp_path, no_xtool):
    p = _new(tmp_path, "TipJar", "app-with-tests")
    assert sorted(_tree(p)) == [".gitignore", "Package.swift", "Sources/AppCore/TipCalculator.swift",
                                "Sources/TipJar/ContentView.swift", "Sources/TipJar/TipJarApp.swift",
                                "Tests/AppCoreTests/ParseAmountTests.swift",
                                "Tests/AppCoreTests/TipCalculatorTests.swift", "xtool.yml"]
    pkg = (p / "Package.swift").read_text()
    assert "{{NAME}}" not in pkg and 'name: "TipJar"' in pkg
    assert '.testTarget(\n            name: "AppCoreTests",\n            dependencies: ["AppCore"]' in pkg
    assert '.target(\n            name: "TipJar",\n            dependencies: ["AppCore"]' in pkg
    info = model.package_info(p / "Package.swift")
    assert info["libraries"] == ["TipJar"] and info["iosMin"] == "17"   # xtool: exactly one library, the app
    assert model.is_project(p)
    assert "struct TipJarApp: App" in (p / "Sources/TipJar/TipJarApp.swift").read_text()
    assert ".navigationTitle(\"TipJar\")" in (p / "Sources/TipJar/ContentView.swift").read_text()
    core = (p / "Sources/AppCore/TipCalculator.swift").read_text()
    assert "import Foundation" in core and "import SwiftUI" not in core and "import UIKit" not in core
    tests = _tree(p / "Tests")
    assert "import XCTest" in tests["AppCoreTests/TipCalculatorTests.swift"]
    swift_testing = tests["AppCoreTests/ParseAmountTests.swift"]
    assert "import Testing" in swift_testing and "@Test" in swift_testing and "#expect" in swift_testing


def test_app_name_cannot_be_a_template_target(tmp_path, no_xtool):
    for taken in ("AppCore", "AppCoreTests"):
        with pytest.raises(HelperError) as ei:
            model.create_app({"name": taken, "template": "app-with-tests", "dir": str(tmp_path), "git": "0"})
        assert ei.value.code == "bad-name"
        assert not (tmp_path / taken).exists()


@pytest.mark.parametrize("template", ["swiftui-app", "list-detail", "tabs-app"])
def test_existing_templates_create_the_same_files(tmp_path, no_xtool, template):
    """Templates with only top-level Sources/*.swift: the skeleton's manifest, the sources flat in Sources/<name>/."""
    p = _new(tmp_path / "new", "Demo", template)
    expected_root = tmp_path / "expected"
    model._fallback_skeleton(expected_root, "Demo")
    expected = _tree(expected_root)
    for f in (model.TEMPLATES / template / "Sources").iterdir():
        target = "DemoApp.swift" if f.name == "App.swift" else f.name
        expected[f"Sources/Demo/{target}"] = f.read_text().replace("{{NAME}}", "Demo")
    expected["xtool.yml"] = "version: 1\nbundleID: com.example.Demo\n"
    assert _tree(p) == expected


def test_non_swift_files_are_copied_as_they_are(tmp_path, no_xtool, monkeypatch):
    tmpl = tmp_path / "templates" / "custom"
    (tmpl / "Sources" / "Core" / "Resources").mkdir(parents=True)
    (tmpl / "Tests" / "CoreTests").mkdir(parents=True)
    (tmpl / "template.json").write_text('{"id": "custom", "title": "Custom"}')
    (tmpl / "Sources" / "App.swift").write_text("struct {{NAME}}App {}\n")
    (tmpl / "Sources" / "Core" / "Core.swift").write_text("let name = \"{{NAME}}\"\n")
    (tmpl / "Sources" / "Core" / "Resources" / "data.json").write_text('{"name": "{{NAME}}"}')
    (tmpl / "Tests" / "CoreTests" / "CoreTests.swift").write_text("// {{NAME}}\n")
    monkeypatch.setattr(model, "TEMPLATES", tmp_path / "templates")
    p = _new(tmp_path / "out", "Zed", "custom")
    assert (p / "Sources/Zed/ZedApp.swift").read_text() == "struct ZedApp {}\n"
    assert (p / "Sources/Core/Core.swift").read_text() == 'let name = "Zed"\n'
    assert (p / "Sources/Core/Resources/data.json").read_text() == '{"name": "{{NAME}}"}'
    assert (p / "Tests/CoreTests/CoreTests.swift").read_text() == "// Zed\n"
    assert 'name: "Zed"' in (p / "Package.swift").read_text()    # no Package.swift.template: the skeleton's


@pytest.mark.slow
@pytest.mark.skipif(not SWIFT.exists(), reason="needs the host Swift toolchain (/usr/bin/swift)")
def test_template_tests_pass_with_real_swift(tmp_path, no_xtool):
    """Builds only the test product (never the SwiftUI app target) and runs XCTest + Swift Testing on the host."""
    p = _new(tmp_path, "TipJar", "app-with-tests")
    env = {**os.environ, "SWIFT_BACKTRACE": "enable=no"}
    common = ["--scratch-path", ".build/host-tests", "--build-system", "native"]
    build = subprocess.run([str(SWIFT), "build", *common, "--product", "TipJarPackageTests", "--no-color-diagnostics"],
                           cwd=p, env=env, capture_output=True, text=True, timeout=600)
    assert build.returncode == 0, build.stdout[-4000:] + build.stderr[-4000:]
    assert "Compiling TipJar " not in build.stdout          # the app target (SwiftUI) isn't built
    test = subprocess.run([str(SWIFT), "test", *common, "--skip-build"],
                          cwd=p, env=env, capture_output=True, text=True, timeout=600)
    out = test.stdout + test.stderr
    assert test.returncode == 0, out[-4000:]
    assert "Executed 4 tests, with 0 failures" in out
    assert "Test run with 3 tests" in out and "passed" in out
