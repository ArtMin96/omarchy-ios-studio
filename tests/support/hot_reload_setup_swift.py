#!/usr/bin/env python3
"""A stand-in `swift` for the hot reload setup tests: tests/fixtures/packages/fake_swift.py, plus the managed hot
reload block evaluated like Swift would: the HotReload product goes to the target the block names, and a manifest
without `let package = Package(` (or with FAKE_HR_SWIFT_BROKEN=1) fails at the block's first line.

    swift package [--package-path P] [--scratch-path S] dump-package
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path

FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "packages" / "fake_swift.py"
BLOCK = re.compile(r'(?m)^// >>> iOS Studio hot reload.*\n(?:.*\n)*?for target in package\.targets where '
                   r'target\.name == "([^"]+)"')


def main(argv: list[str]) -> int:
    spec = importlib.util.spec_from_file_location("fake_swift", FAKE)
    fake = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake)
    if "dump-package" not in argv:
        return fake.main(argv)
    pkg_dir = argv[argv.index("--package-path") + 1] if "--package-path" in argv else os.getcwd()
    text = (Path(pkg_dir) / "Package.swift").read_text()
    m = BLOCK.search(text)
    if m and (os.environ.get("FAKE_HR_SWIFT_BROKEN") or "let package = Package(" not in text):
        line = text[:m.start()].count("\n") + 2
        sys.stderr.write(f"{pkg_dir}/Package.swift:{line}:1: error: cannot find 'package' in scope\n")
        sys.stderr.write(f"error: invalid manifests at [{pkg_dir}]\n")
        return 1
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fake.dump(pkg_dir)
    if rc != 0:
        return rc
    data = json.loads(buf.getvalue())
    if m:
        hr = {"product": ["HotReload", "hot-reload", None, None]}
        for t in data["targets"]:
            t["dependencies"] = [d for d in t["dependencies"] if d != hr]
            if t["name"] == m.group(1):
                t["dependencies"].append(hr)
    print(json.dumps(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
