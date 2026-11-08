"""A .ips crash report for tests/fixtures/crash-symbolication/Crashy (see build.sh there)."""
from __future__ import annotations

import json

from support import FIXTURES

FIX = FIXTURES / "crash-symbolication"
DWARF = FIX / "Crashy.dwarf"
UUID = "4c4c4476-5555-3144-a14b-4068033367a8"
APP = "/private/var/containers/Bundle/Application/0D6C1F00-0000-4000-8000-000000000000/Crashy.app/Crashy"
# Offsets in Crashy (from its disassembly): the return address after the call that traps in first()
# (`return d!`, line 6) and the return address of main's call to first() (line 14).
OFF_FIRST = 0x41D0
OFF_MAIN = 0x438C


def ips(uuid: str = UUID, crashed_frames=None, other_frames=None) -> str:
    header = {"app_name": "Crashy", "timestamp": "2026-11-05 10:00:00.00 +0000", "bug_type": "309",
              "os_version": "iPhone OS 27.1 (24A1)", "incident_id": "11111111-2222-4333-8444-555555555555"}
    images = [
        {"source": "P", "arch": "arm64", "base": 4365664256, "size": 32768, "uuid": uuid, "path": APP,
         "name": "Crashy"},
        {"source": "P", "arch": "arm64e", "base": 6000000000, "size": 4000000,
         "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "path": "/usr/lib/swift/libswiftCore.dylib",
         "name": "libswiftCore.dylib"},
    ]
    crashed = crashed_frames if crashed_frames is not None else [
        {"imageOffset": 0x1234, "symbol": "_assertionFailure(_:_:file:line:flags:)", "symbolLocation": 1240,
         "imageIndex": 1},
        {"imageOffset": OFF_FIRST, "imageIndex": 0},
        {"imageOffset": OFF_MAIN, "imageIndex": 0},
    ]
    threads = [{"triggered": True, "frames": crashed},
               {"frames": other_frames if other_frames is not None else [{"imageOffset": OFF_MAIN, "imageIndex": 0}]}]
    body = {"procName": "Crashy", "exception": {"type": "EXC_BREAKPOINT", "signal": "SIGTRAP"},
            "faultingThread": 0, "threads": threads, "usedImages": images}
    return json.dumps(header) + "\n" + json.dumps(body)
