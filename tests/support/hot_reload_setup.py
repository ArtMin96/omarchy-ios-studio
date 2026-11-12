"""Fixtures for the hot reload setup tests: a stand-in templates/hot-reload/ (full and stub runtime packages, until
the real one lands; the tests point IOSSTUDIO_HOT_RELOAD_TEMPLATES at it) and the fake swift that understands the
managed block (hot_reload_setup_swift.py)."""
from __future__ import annotations

from pathlib import Path

FAKE_SWIFT = Path(__file__).resolve().parent / "hot_reload_setup_swift.py"

_PACKAGE = ('// swift-tools-version: 5.9\nimport PackageDescription\n\nlet package = Package(\n'
            '    name: "hot-reload",\n    platforms: [.iOS(.v17)],\n'
            '    products: [.library(name: "HotReload", targets: ["HotReload"])],\n    targets: [{targets}]\n)\n')


def make_templates(base: Path, version: str = "1") -> Path:
    """base/hot-reload/{version.txt, full/, stub/} shaped like templates/hot-reload/; returns its path."""
    root = base / "hot-reload"
    full, stub = root / "full", root / "stub"
    files = {
        full / "Package.swift": _PACKAGE.format(targets='.target(name: "HotReloadLoader"), '
                                                        '.target(name: "HotReload", dependencies: ["HotReloadLoader"])'),
        full / "Sources/HotReloadLoader/loader.c": "// loader v" + version + "\n",
        full / "Sources/HotReloadLoader/include/HotReloadLoader.h": "#pragma once\n",
        full / "Sources/HotReload/HotReload.swift": "public enum HotReload { public static var isActive: Bool { true } }\n",
        stub / "Package.swift": _PACKAGE.format(targets='.target(name: "HotReload")'),
        stub / "Sources/HotReload/HotReload.swift": "public enum HotReload { public static var isActive: Bool { false } }\n",
        root / "version.txt": version + "\n",
    }
    for p, text in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root
