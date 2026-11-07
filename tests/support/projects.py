"""Throwaway SwiftPM + xtool projects for tests."""
from __future__ import annotations

import os
from pathlib import Path


def project_with_files(root: Path, files: dict[str, str]) -> Path:
    """A minimal project at root with the given files (relative path -> text); returns its real path."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Package.swift").write_text("// swift-tools-version: 6.0\nimport PackageDescription\n")
    (root / "xtool.yml").write_text("version: 1\nbundleID: com.example.App\n")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return Path(os.path.realpath(root))


def package_project(root: Path, deps: str = "", target_deps: str = "") -> Path:
    """A project whose Package.swift has an App library target (with target_deps) and package `deps`."""
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


def app_project(base: Path, name: str = "App") -> Path:
    """base/name with Sources/<name>/ContentView.swift."""
    p = base / name
    (p / "Sources" / name).mkdir(parents=True)
    (p / "Package.swift").write_text('// swift-tools-version: 6.0\nimport PackageDescription\n'
                                     f'let package = Package(name: "{name}")\n')
    (p / "xtool.yml").write_text(f"version: 1\nbundleID: com.example.{name}\n")
    (p / "Sources" / name / "ContentView.swift").write_text("import SwiftUI\nlet x = 1\n")
    return p


def atomic_save(path: Path, text: str, tmp_name: str = ".tmpA1b2C3") -> None:
    """How Neovim (backupcopy=no), Zed and JetBrains save: write a temp file, rename it over the original."""
    tmp = path.parent / tmp_name
    tmp.write_text(text)
    os.rename(tmp, path)
