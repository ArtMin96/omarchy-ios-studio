"""Is the toolchain's lldb usable for the editor bridge, by the helper's own check (`debug check`)?

Tests that run the real `debug editor` bridge skip on this, not on the files alone: CI's preinstalled Swift has
lldb and lldb-dap but not the Python lldb needs (Setup's "Debugger support"), so the bridge stops at its lldb step.
"""
from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import tempfile

from support import ROOT


@functools.cache
def debugger_ready() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items() if k != "IOSSTUDIO_DEMO"}
        env["XDG_CACHE_HOME"] = tmp    # don't read or write a cached toolchain answer
        try:
            r = subprocess.run([sys.executable, str(ROOT / "helper/run.py"), "iosstudio", "debug", "check"],
                               capture_output=True, text=True, timeout=60, env=env)
            return bool(json.loads(r.stdout.strip().splitlines()[-1]).get("data", {}).get("ready"))
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError, AttributeError):
            return False
