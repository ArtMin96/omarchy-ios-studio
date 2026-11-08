"""AppState.qml's config/state/cache folders use the same XDG variables and defaults as the helper
(helper/iosstudio/core/appdirs.py), so the app reads device.json, builds.json and settings.json where the
daemon and helper write them. The rule itself (Model.xdgHome) is tested in tst_xdg_home.qml."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPSTATE = (ROOT / "AppState.qml").read_text()
APPDIRS = (ROOT / "helper/iosstudio/core/appdirs.py").read_text()


def test_appstate_folders_match_the_helper() -> None:
    for var, default in (("XDG_CONFIG_HOME", ".config"), ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
        assert f'os.environ.get("{var}") or Path.home() / "{default}"' in APPDIRS
        assert re.search(rf'Model\.xdgHome\(Quickshell\.env\("{var}"\), home, "{re.escape(default)}"\)', APPSTATE), var
    for name in ("configDir", "stateDir", "cacheDir"):
        line = next(ln for ln in APPSTATE.splitlines() if f"property string {name}:" in ln)
        assert '"/ios-studio"' in line and "home +" not in line, line
