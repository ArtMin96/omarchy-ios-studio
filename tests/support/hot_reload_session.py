"""A whole fake world for `hotreload run` in real mode: the real helper code with fakes at the process boundary.

    w = SessionWorld(tmp)            # project with the hot reload block, fake xtool / swiftc / ld / rcodesign,
    with w.deviced():                # a throwaway signing identity, a connected iPhone in device.json
        p = subprocess.Popen(HELPER_PY + ["hotreload", "run", "--project", str(w.proj)], env=w.env, ...)

The fake xtool is signed in, sees the iPhone on USB and "builds" in FAKE_XTOOL_SECONDS (FAKE_XTOOL_FAIL=1: a
compile error; FAKE_XTOOL_EDIT="<path>::<text>": that file is saved during the build). The build tree it would
leave is made up front by support.hot_reload.fake_build, so the real
buildinfo.capture_baseline and patch.make_patch run (compile, link, interposable check, sign) against fakes.
The fake device daemon answers apps.list, hotreload.clean/runtime, app.launch and hotreload.push (its patch path
checked as the real daemon checks it); `push_mode` picks the push answer: ok, noack, load-failed, gone (not-installed).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from support import FIXTURES, isolated_env, write_exe
from support.fake_deviced import FakeDeviced
from support.hot_reload import OPENSSL, app_and_patch, fake_build, fake_toolchain, make_identity

FIX = FIXTURES / "hot-reload"
CARD = (FIX / "base.swift").read_text()
APP_SWIFT = "import SwiftUI\n\n@main\nstruct TheApp: App {\n    var body: some Scene { WindowGroup { Card() } }\n}\n"
UDID = "00008150-000000000000001C"
BUNDLE = "dev.example.app"
INSTALLED = f"XTL-ABCDE12345.{BUNDLE}"
APP_PID = 4242

FAKE_XTOOL = f"""\
#!/usr/bin/env python3
import os, sys, time
a = sys.argv[1:]
if a[:2] == ["auth", "status"]:
    print("Logged in."); sys.exit(0)
if a[:1] == ["devices"]:
    print("{UDID}"); sys.exit(0)
print("fake xtool", " ".join(a), flush=True)
time.sleep(float(os.environ.get("FAKE_XTOOL_SECONDS") or 0.2))
edit = os.environ.get("FAKE_XTOOL_EDIT")       # "<path>::<text>": the user saves a file while the app builds
if edit:
    path, _, text = edit.partition("::")
    with open(path, "w") as f:
        f.write(text)
if os.environ.get("FAKE_XTOOL_FAIL"):
    src = os.path.abspath("Sources/App/Card.swift")
    print(f"{{src}}:3:5: error: cannot find 'BROKEN' in scope")
    print("error: fatalError")
    sys.exit(1)
print("Build complete! (0.1 secs)")
"""

FAKE_RCODESIGN = "#!/bin/sh\ncat > /dev/null\necho \"sign $*\" >> \"$(dirname \"$0\")/tools/calls\"\n"


class SessionWorld:
    def __init__(self, tmp: Path, enabled: bool = True):
        self.tmp = tmp
        tmp.mkdir(parents=True, exist_ok=True)
        self.env = isolated_env(tmp)
        self.env["IOSSTUDIO_RUNTIME_DIR"] = str(tmp / "rt")
        self.env["IOS_STUDIO_XTOOL"] = str(write_exe(tmp / "xtool", FAKE_XTOOL))
        self.env["IOSSTUDIO_RCODESIGN"] = str(write_exe(tmp / "rcodesign", FAKE_RCODESIGN))
        self.tools = fake_toolchain(tmp / "tools")
        objs = tmp / "objs"
        objs.mkdir()
        shutil.copy(FIX / "text-any.o", objs / "default.o")
        app, dylib = app_and_patch(bound=["_f"], app_funcs=["_f", "_g"], patch_funcs=["_f", "_g"])
        (tmp / "patch.dylib").write_bytes(dylib)
        data = tmp / "xtool-data"
        if OPENSSL:
            make_identity(data)
        self.env.update(FAKE_OBJ_DIR=str(objs), FAKE_PATCH=str(tmp / "patch.dylib"), IOSSTUDIO_XTOOL_DATA=str(data))
        self.proj = Path(os.path.realpath(tmp)) / "App"
        fake_build(self.proj, self.tools, files={"Card.swift": CARD, "TheApp.swift": APP_SWIFT},
                   objects={"Card.swift": (FIX / "base-any.o").read_bytes(),
                            "TheApp.swift": (FIX / "base-any.o").read_bytes()}, app_binary=app, bundle_id=BUNDLE)
        self.card = self.proj / "Sources/App/Card.swift"
        (self.proj / "xtool.yml").write_text(f"version: 1\nbundleID: {BUNDLE}\nproduct: App\n")
        base = ('// swift-tools-version: 6.0\nimport PackageDescription\n\nlet package = Package(\n    name: "App",\n'
                '    platforms: [.iOS(.v17)],\n    products: [.library(name: "App", targets: ["App"])],\n'
                '    targets: [.target(name: "App")]\n)\n')
        if enabled:
            from iosstudio.features.hotreload import project_setup
            base = project_setup.append_block(base, "App")
        (self.proj / "Package.swift").write_text(base)
        state = tmp / "xdg_state_home" / "ios-studio"
        state.mkdir(parents=True, exist_ok=True)
        (state / "device.json").write_text(f'{{"udid": "{UDID}", "name": "Test iPhone", "connection": "connected"}}')
        self.push_mode = "ok"

    def push(self, a: dict) -> dict:
        seq = int(a["seq"])
        if self.push_mode == "gone":
            return {"error": {"code": "not-installed", "message": "The app isn't installed on your iPhone."}}
        from iosstudio.core.output import HelperError
        from iosstudio.device.features.hotreload import core
        try:
            core.check_patch(a["src"], a.get("key"))      # the real daemon's check: what the helper sends passes it
        except HelperError as exc:
            return {"error": {"code": exc.code, "message": exc.message}}
        if self.push_mode == "noack":
            return {"seq": seq, "bytes": 10, "pushMs": 5, "acked": False}
        ok = self.push_mode == "ok"
        ack = {"seq": seq, "ok": ok, "ms": 2.5, "swapped": 1 if ok else 0, "kept": 3, "vtable": 0, "reverted": 0,
               "error": None if ok else "dlopen(patch.dylib): symbol not found: _missing", "generation": seq}
        return {"seq": seq, "bytes": 10, "pushMs": 5, "acked": True, "ack": ack, "ackMs": 50}

    def deviced(self) -> FakeDeviced:
        return FakeDeviced(self.tmp / "rt", {
            "apps.list": {"apps": [{"bundleId": "com.apple.mobilesafari"}, {"bundleId": INSTALLED}]},
            "hotreload.clean": {"removed": 0},
            "app.launch": {"bundleId": INSTALLED, "pid": APP_PID},
            "hotreload.runtime": {"present": True, "runtime": {"version": 1, "pid": APP_PID, "started": 0}},
            "hotreload.push": self.push,
        })

    def calls(self) -> list[str]:
        p = self.tools / "calls"
        return p.read_text().splitlines() if p.exists() else []
