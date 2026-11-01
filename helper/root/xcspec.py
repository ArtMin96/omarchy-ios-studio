#!/usr/bin/env python3
# Adapted from omarchy-apple-dev and earlier helper scripts for iOS Studio (run as root via helper/root/root-tasks.sh).
"""Check or apply the three SwiftBuild xcspec defaults that .strings copies need on Linux.

Usage: python3 -I xcspec.py check|apply <toolchain root>

The toolchain root is the directory above usr/bin/swift-driver (/usr/lib/swift for swift-bin 6.4).
SwiftBuild re-encodes .strings files only on Darwin, so the input encoding must be spelled out and
the output must be the binary plist (omarchy-apple-dev FINDINGS.md item 24). install-toolchain.sh
makes the same edit but only warns when the files are root-owned, as swift-bin's are; setup.sh runs
`apply` with sudo. A swift-bin reinstall or upgrade puts the stock files back, so `check` runs on
every setup.sh and setup.sh --check.

Each edit: the first `DefaultValue =` line within 12 lines after the setting's `Name =` line gets
the value. Exit 0: every file holds its value (after `apply`: holds it now). Exit 1: a file is
missing or still needs the edit. Exit 2: wrong arguments.
"""

import os
import re
import sys

PATCHES = (
    (
        "SwiftBuild_SWBUniversalPlatform.bundle/CopyStringsFile.xcspec",
        "STRINGS_FILE_INPUT_ENCODING",
        "utf-8",
    ),
    (
        "SwiftBuild_SWBCore.bundle/CoreBuildSystem.xcspec",
        "STRINGS_FILE_OUTPUT_ENCODING",
        "binary",
    ),
    (
        "SwiftBuild_SWBCore.bundle/NativeBuildSystem.xcspec",
        "STRINGS_FILE_OUTPUT_ENCODING",
        "binary",
    ),
)


def patched(text, setting, value):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if f'Name = "{setting}";' in line or f"Name = {setting};" in line:
            for j in range(i + 1, min(i + 12, len(lines))):
                if "DefaultValue =" in lines[j]:
                    lines[j] = re.sub(r'"[^"]*"\s*;', f'"{value}";', lines[j])
                    break
    return "\n".join(lines)


def main(argv):
    if len(argv) != 3 or argv[1] not in ("check", "apply"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    mode, root = argv[1], argv[2]
    bad = 0
    for rel, setting, value in PATCHES:
        path = os.path.join(root, "usr/share/pm", rel)
        name = os.path.basename(path)
        try:
            with open(path) as f:
                old = f.read()
        except FileNotFoundError:
            print(f"  missing  {path}")
            bad += 1
            continue
        new = patched(old, setting, value)
        if new == old:
            print(f"  ok       {name} {setting}={value}")
        elif mode == "apply":
            with open(path, "w") as f:
                f.write(new)
            print(f"  patched  {name} {setting}={value}")
        else:
            print(f"  needed   {name} {setting}={value}")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
