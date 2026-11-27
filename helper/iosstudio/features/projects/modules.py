"""The precompiled module files (.pcm) lldb reads an app's Swift types from, and getting them back once pruned.

xtool builds with explicit modules: the SDK's modules are precompiled into
<project>/.build/out/SDKExplicitPrecompiledModules and the app's object files name those files for the debugger.
The compiler prunes that folder of files it hasn't used for a while (its modules.timestamp), but a later build
only recompiles what changed, so the objects keep naming deleted files: lldb then can't rebuild the app's Swift
types ("type for self cannot be reconstructed") and Swift expressions fail. Measured on a CrashLab copy (2026-11):
deleting the .pcm files and building again brought none back; deleting the project's own object files made the next
build recompile them (~10 s) and regenerate 63 of the 66 files (the rest belong to package dependencies).

So a build first checks (stat only) whether the folder has lost its .pcm files, or whether a debug session saw lldb
report missing ones (note_missing, kept in ~/.cache/ios-studio/missing-modules.json), and if so removes the project's
own objects so this build recompiles them.
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from iosstudio.core import appdirs, fileio

PCM_DIR = Path(".build/out/SDKExplicitPrecompiledModules")
INTERMEDIATES = Path(".build/out/Intermediates.noindex")
NOTES_KEEP = 20             # projects remembered in missing-modules.json


def _notes_file() -> Path:
    return appdirs.cache_dir() / "missing-modules.json"


def pruned(path: Path) -> bool:
    """True when the project was built with explicit modules and its SDK .pcm files are gone. Stat only."""
    inter = path / INTERMEDIATES
    if not inter.is_dir():
        return False
    try:
        with os.scandir(path / PCM_DIR) as it:
            return not any(e.name.endswith(".pcm") for e in it)
    except FileNotFoundError:   # the whole folder went: stale only if this build used explicit modules
        return (inter / "SwiftExplicitPrecompiledModules").is_dir() or (inter / "ExplicitPrecompiledModules").is_dir()
    except OSError:
        return False


def note_missing(path: Path) -> None:
    """A debug session saw lldb report .pcm files of this project as missing: the next build recompiles."""
    f = _notes_file()
    with contextlib.suppress(OSError), fileio.lock(f.with_suffix(".lock")):
        notes = fileio.load_json(f, {})
        notes = notes if isinstance(notes, dict) else {}
        notes[str(path)] = time.time()
        newest = sorted(notes.items(), key=lambda kv: kv[1] if isinstance(kv[1], (int, float)) else 0)[-NOTES_KEEP:]
        fileio.save_json(f, dict(newest))


def _take_note(path: Path) -> bool:
    f = _notes_file()
    if not f.is_file():
        return False
    with contextlib.suppress(OSError), fileio.lock(f.with_suffix(".lock")):
        notes = fileio.load_json(f, {})
        if isinstance(notes, dict) and str(path) in notes:
            notes.pop(str(path))
            fileio.save_json(f, notes)
            return True
    return False


def stale(path: Path) -> bool:
    """Does the debugger lack module files for this project's build? (cheap: a stat and a small JSON read)"""
    if pruned(path):
        return True
    notes = fileio.load_json(_notes_file(), {}) if _notes_file().is_file() else {}
    return isinstance(notes, dict) and str(path) in notes


def own_objects(path: Path, package: str, config: str) -> list[Path]:
    """The object files of the project's own package (not its dependencies') for one configuration."""
    root = path / INTERMEDIATES / f"{package}.build" / f"{config.capitalize()}-iphoneos"
    return sorted(root.glob("*.build/Objects-normal/*/*.o"))


def refresh(path: Path, package: str, config: str) -> int:
    """Before a build: when the module files are gone, remove the project's own objects so the build recompiles
    them and regenerates the files. Returns how many objects were removed (0: nothing to do)."""
    noted = _take_note(path)
    if not (noted or pruned(path)):
        return 0
    n = 0
    for obj in own_objects(path, package, config):
        with contextlib.suppress(OSError):
            obj.unlink()
            n += 1
    return n
