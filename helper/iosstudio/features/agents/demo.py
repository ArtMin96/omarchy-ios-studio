"""Demo mode (`ios-studio --demo`, screenshots): made-up answers; nothing is read or changed except a small
file in the demo runtime folder that remembers which tools are "connected"."""
from __future__ import annotations

from iosstudio.core import appdirs, fileio, output
from iosstudio.core.output import ok

from .clients import clients, method

LAUNCHER = "~/.local/share/omarchy-ios-studio/bin/iosstudio-mcp"
INSTALLED = {"claude", "codex", "cursor", "zed"}


def demo(action: str, flags: dict) -> int:
    from iosstudio.device import client as device_client
    state_file = device_client.runtime_dir(demo=True) / "agents.json"
    connected = set(fileio.load_json(state_file, ["claude"]) or [])
    if action in ("connect", "disconnect"):
        cid = str(flags.get("client") or "")
        if not flags.get("dry_run"):
            (connected.add if action == "connect" else connected.discard)(cid)
            fileio.save_json(state_file, sorted(connected))
        return ok({"client": cid, "demo": True, "connected": action == "connect", "method": "demo",
                   "restartHint": "Demo mode: nothing was changed."})
    if action == "test":
        return ok({"tools": 34, "names": [], "demo": True, "seconds": 0.4})
    if action == "prepare":
        output.step("venv", "Server environment", "done", detail="Already set up")
        return output.done(True, {"state": "ready", "ready": True})
    rows = []
    for c in clients():
        cid = c["id"]
        rows.append({"id": cid, "name": c["name"], "installed": cid in INSTALLED, "method": method(c),
                     "registered": cid in connected, "current": cid in connected, "stale": False, "demo": False,
                     "command": LAUNCHER if cid in connected else None, "fileShort": appdirs.tilde(c["file"]),
                     "whereShort": appdirs.tilde(c["file"]), "restartHint": c["restart"],
                     "snippet": {"kind": "shell", "text": f"# demo mode\n{LAUNCHER}"}})
    return ok({"server": {"state": "ready", "ready": True, "launcherShort": LAUNCHER, "launcher": LAUNCHER,
                          "venvShort": "~/.local/share/ios-studio/mcp-venv", "uv": True, "launcherOk": True},
               "clients": rows})
