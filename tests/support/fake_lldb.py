"""A stand-in `lldb` Python module for helper/iosstudio/features/lldb_driver.py's pure parts.

write_fake_lldb(dir) writes dir/lldb.py; pass dir as the driver config's lldbPythonPath. It has the constants
and the SB classes the driver touches at import and in main(); FAKE_LLDB_CREATE=raise makes SBDebugger.Create
fail, otherwise CreateTarget returns an invalid target (so connect() raises Fail)."""
from __future__ import annotations

from pathlib import Path

FAKE_LLDB = r'''
import os
LLDB_INVALID_ADDRESS = 0xFFFFFFFFFFFFFFFF
(eStopReasonNone, eStopReasonTrace, eStopReasonBreakpoint, eStopReasonWatchpoint, eStopReasonSignal,
 eStopReasonException, eStopReasonPlanComplete) = range(7)
(eStateInvalid, eStateUnloaded, eStateStopped, eStateRunning, eStateStepping, eStateCrashed, eStateDetached,
 eStateExited) = range(8)
CALLS = []

class SBError:
    def GetCString(self): return "no such file"
    def Fail(self): return True
    def Success(self): return False

class SBCommandReturnObject:
    def __init__(self): self.out = ""
    def GetOutput(self): return self.out
    def GetError(self): return ""
    def Succeeded(self): return True

class _Proc:
    def IsValid(self): return False

class _Target:
    def IsValid(self): return False
    def GetProcess(self): return _Proc()

class _CI:
    def HandleCommand(self, cmd, r):
        CALLS.append(cmd)
        r.out = "ok\n"

class _Dbg:
    def SetAsync(self, v): CALLS.append(f"async {v}")
    def GetCommandInterpreter(self): return _CI()
    def CreateTarget(self, *a): return _Target()

class SBDebugger:
    @staticmethod
    def Initialize(): CALLS.append("init")
    @staticmethod
    def Terminate(): CALLS.append("terminate")
    @staticmethod
    def Create(flag):
        if os.environ.get("FAKE_LLDB_CREATE") == "raise":
            raise RuntimeError("no lldb today")
        return _Dbg()

class SBFileSpec:
    def __init__(self, p): self.p = p

class SBEvent: pass
class SBExpressionOptions: pass
class SBProcess: pass
'''


def write_fake_lldb(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "lldb.py").write_text(FAKE_LLDB)
    return folder
