"""lldb commands for "Debug in editor". This file runs inside the editor's lldb-dap (its embedded Python), never
in the helper: editor_debug.py copies it to <state>/editor-debug/iosstudio_editor_attach.py and the attach
file loads it with `command script import`.

  iosstudio-check <pid> <starttime>   fails unless that iOS Studio bridge process is still running, so a
                                      stale attach file never connects to an old port
  iosstudio-remote-file <path>        the target's executable lives at <path> on the iPhone
  iosstudio-connect <url> [timeout]   `process connect` the way lldb_driver.py does it: asynchronously (a
                                      synchronous connect to a debugserver without a process fails), with
                                      the API call as a fallback, then wait until the connection stands
  iosstudio-fail <message>            fails with the message (the attach file while nothing runs)

Each command fails with a plain message (result.SetError), which stops the attach file there; lldb-dap shows
it in the editor's Debug Console.
"""
from __future__ import annotations

import shlex
import time

import lldb

NOT_RUNNING = "iOS Studio isn't debugging this app right now. Start 'Debug in editor' in iOS Studio first, then attach again."


def _args(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def check(debugger, command, result, _internal):
    a = _args(command)
    ok = False
    if len(a) == 2:
        try:
            with open(f"/proc/{int(a[0])}/stat") as f:
                ok = f.read().rsplit(")", 1)[1].split()[19] == a[1]
        except (OSError, IndexError, ValueError):
            ok = False
    if not ok:
        result.SetError(NOT_RUNNING)


def remote_file(debugger, command, result, _internal):
    a = _args(command)
    m = debugger.GetSelectedTarget().GetModuleAtIndex(0)
    if a and m.IsValid():
        m.SetPlatformFileSpec(lldb.SBFileSpec(a[0]))


def connect(debugger, command, result, _internal):
    a = _args(command)
    if not a:
        result.SetError("iosstudio-connect needs a connect:// address.")
        return
    url, timeout = a[0], float(a[1]) if len(a) > 1 else 15.0
    target = debugger.GetSelectedTarget()
    if not target.IsValid():
        target = debugger.CreateTarget("")
        debugger.SetSelectedTarget(target)
    was_async = debugger.GetAsync()
    debugger.SetAsync(True)
    try:
        r = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand("process connect " + url, r)
        if not target.GetProcess().IsValid():
            e = lldb.SBError()
            target.ConnectRemote(debugger.GetListener(), url, "gdb-remote", e)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            p = target.GetProcess()
            if p.IsValid() and p.GetState() not in (lldb.eStateInvalid, lldb.eStateExited, lldb.eStateDetached):
                return
            time.sleep(0.1)
        detail = (r.GetError() or "").strip()
        result.SetError("Couldn't reach the debugger on your iPhone. Check that it's connected in iOS Studio, then "
                        "attach again." + (f" ({detail})" if detail else ""))
    finally:
        debugger.SetAsync(was_async)


def fail(debugger, command, result, _internal):
    a = _args(command)
    result.SetError(a[0] if a else NOT_RUNNING)


def __lldb_init_module(debugger, _internal):
    for name, fn in (("check", check), ("remote-file", remote_file), ("connect", connect), ("fail", fail)):
        debugger.HandleCommand(f"command script add -o -f {__name__}.{fn.__name__} iosstudio-{name}")
