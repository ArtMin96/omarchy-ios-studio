"""The `agents` command group: list, connect, disconnect, test, prepare."""
from __future__ import annotations

import os
import time

from iosstudio.core import appdirs
from iosstudio.core.output import HelperError, ok

from .clients import DEMO_ENV, HOME, LAUNCHER, client, clients, describe, launcher, registration


def _need_client(flags: dict) -> dict:
    cid = str(flags.get("client") or "")
    if not cid:
        raise HelperError("usage", "Pass --client (claude, codex, opencode, cursor, gemini, crush or zed).")
    return client(cid)


def _change(c: dict, connect: bool, flags: dict) -> dict:
    from .writers import change
    demo = flags.get("demo") is True or str(flags.get("demo", "")).lower() in ("1", "true", "on", "yes")
    return change(c, connect, demo=demo, dry=bool(flags.get("dry_run")), via=str(flags.get("via") or ""))


def list_(flags: dict) -> int:
    from .server import server_status
    return ok({"server": server_status(), "clients": [describe(c) for c in clients()]})


def connect(flags: dict) -> int:
    """--client X [--demo] [--dry-run] [--via file]"""
    c = _need_client(flags)
    if not LAUNCHER.exists():
        raise HelperError("no-launcher", "iOS Studio's MCP server is missing from the app folder.")
    return ok(_change(c, True, flags))


def disconnect(flags: dict) -> int:
    """--client X [--dry-run]"""
    return ok(_change(_need_client(flags), False, flags))


def test(flags: dict) -> int:
    """[--client X] [--demo] [--timeout S]: start the server (the one X is registered with) and count its tools."""
    from .rpc import list_tools
    cmd, demo, who = launcher(), bool(flags.get("demo")), None
    if flags.get("client"):
        c = client(str(flags["client"]))
        reg = registration(c)
        if not reg["registered"]:
            raise HelperError("not-connected", f"{c['name']} isn't connected to iOS Studio yet.")
        cmd, demo, who = os.path.expanduser(reg["command"] or ""), demo or reg["demo"], c["name"]
        if not os.access(cmd, os.X_OK):
            raise HelperError("stale", f"{c['name']} points to {appdirs.tilde(cmd)}, which doesn't exist anymore. "
                              "Connect it again to fix that.")
    env = os.environ.copy()
    env.pop("IOSSTUDIO_DEMO", None)
    if demo:
        env.update(DEMO_ENV)
    t0 = time.monotonic()
    res = list_tools(cmd, env, HOME, float(flags.get("timeout") or 180))
    init, names = res["init"], res["names"]
    info = init.get("serverInfo") or {}
    return ok({"tools": len(names), "names": names, "client": who, "demo": demo,
               "protocol": init.get("protocolVersion"), "server": info.get("name"), "version": info.get("version"),
               "seconds": round(time.monotonic() - t0, 1)})


def prepare(flags: dict) -> int:
    """[--repair] (streaming)"""
    from .server import prepare as run_prepare
    return run_prepare(bool(flags.get("repair")))


GROUPS = {
    "agents": {
        "list": list_,
        "connect": connect,
        "disconnect": disconnect,
        "test": test,
        "prepare": prepare,
    }
}
