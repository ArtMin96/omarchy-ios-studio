"""Where editor setup looks (home, XDG dirs, PATH), the Swift toolchain it finds there (swift version, xtool's
iOS SDK, sourcekit-lsp) and the editor remembered in iOS Studio's settings.json."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from iosstudio.core import appdirs, fileio

EDITORS = {"vscode": "VS Code", "zed": "Zed", "neovim": "Neovim"}
EDITOR_USAGE = "--editor must be vscode, zed or neovim."


@dataclass
class Env:
    """Where to look: home, XDG config/data dirs and PATH. Tests pass fake ones; the CLI uses the real ones."""
    home: Path
    config: Path
    data: Path
    path: str
    state: Path | None = None          # iOS Studio's state dir (default: <home>/.local/state/ios-studio)
    swift_version: tuple[int, int] | None = None
    probe_swift: bool = True
    _probed: bool = field(default=False, repr=False)

    @classmethod
    def from_os(cls) -> Env:
        return cls(home=Path.home(), config=appdirs.config_home(), data=appdirs.data_home(),
                   path=os.environ.get("PATH", "/usr/bin"), state=appdirs.state_dir())

    def __post_init__(self) -> None:
        if self.state is None:
            self.state = self.home / ".local/state/ios-studio"

    @property
    def settings_file(self) -> Path:
        return self.config / "ios-studio/settings.json"

    def which(self, name: str) -> str | None:
        return shutil.which(name, path=self.path)

    def swift(self) -> tuple[int, int] | None:
        if self.swift_version is None and self.probe_swift and not self._probed:
            self._probed = True
            self.swift_version = _swift_version(self.which("swift"))
        return self.swift_version


def _swift_version(swift: str | None) -> tuple[int, int] | None:
    """(major, minor) from `swift --version`, cached per binary (it takes ~0.5 s)."""
    if not swift:
        return None
    try:
        st = os.stat(swift)
        key = f"{os.path.realpath(swift)}:{int(st.st_mtime)}"
    except OSError:
        return None
    hit = appdirs.cache_get("editor-swift-version")
    if hit and isinstance(hit.get("data"), dict) and hit["data"].get("key") == key and hit["data"].get("v"):
        return tuple(hit["data"]["v"])[:2]  # type: ignore[return-value]
    try:
        out = subprocess.run([swift, "--version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Swift version (\d+)\.(\d+)", out)
    if not m:
        return None
    v = (int(m.group(1)), int(m.group(2)))
    try:
        appdirs.cache_put("editor-swift-version", {"key": key, "v": list(v)})
    except OSError:
        pass
    return v


def flatpak_installed(env: Env, app_id: str) -> bool:
    return any((d / app_id).is_dir() for d in (env.data / "flatpak/app", Path("/var/lib/flatpak/app")))


# ================================================================ toolchain

def ios_sdk(env: Env) -> str | None:
    """xtool's Darwin Swift SDK bundle (appdirs.sdk_bundles), or None."""
    d = appdirs.sdk_bundle(env.config, env.home)
    return str(d) if d else None


def sourcekit(env: Env) -> str | None:
    """sourcekit-lsp on PATH."""
    return env.which("sourcekit-lsp")


def _next_to_swift(env: Env, name: str) -> str | None:
    """A toolchain program next to the real swift binary (a toolchain that isn't on PATH as a whole)."""
    sw = env.which("swift")
    if not sw:
        return None
    cand = Path(os.path.realpath(sw)).parent / name
    return str(cand) if cand.is_file() and os.access(cand, os.X_OK) else None


def sourcekit_next_to_swift(env: Env) -> str | None:
    """sourcekit-lsp next to the real swift binary."""
    return _next_to_swift(env, "sourcekit-lsp")


def sourcekit_for_settings(env: Env) -> str | None:
    """A sourcekit-lsp path to write into editor settings: only when it isn't on PATH (on PATH, every editor
    finds it, and a machine path in a shared settings file breaks it for others)."""
    return None if sourcekit(env) else sourcekit_next_to_swift(env)


def lldb_dap_for_settings(env: Env) -> str | None:
    """The toolchain's lldb-dap (next to swift and lldb) when it isn't on PATH, for "Debug in editor": Zed's
    Swift extension finds lldb-dap only through swiftly, xcrun or PATH."""
    return None if env.which("lldb-dap") else _next_to_swift(env, "lldb-dap")


# ================================================================ settings: the preferred editor

SETTING_KEY = "editorSetup"   # in iOS Studio's settings.json, next to editorCommand ("Open in editor")
_OPEN_TO_EDITOR = {"zeditor": "zed", "zed": "zed", "zedit": "zed", "zed-editor": "zed", "code": "vscode",
                   "code-oss": "vscode", "codium": "vscode", "vscodium": "vscode", "code-insiders": "vscode",
                   "nvim": "neovim"}


def load_settings(env: Env) -> dict:
    data = fileio.load_json(env.settings_file, {})
    return data if isinstance(data, dict) else {}


def preferred(env: Env) -> str | None:
    """The editor set up last (settings.editorSetup); without one, the one "Open in editor" uses when it's
    one of ours (settings.editorCommand)."""
    s = load_settings(env)
    v = s.get(SETTING_KEY)
    if v in EDITORS:
        return v
    cmd = str(s.get("editorCommand") or "").split()
    if cmd:
        return _OPEN_TO_EDITOR.get(Path(cmd[0]).name)
    return None
