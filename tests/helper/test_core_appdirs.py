"""helper/iosstudio/core/appdirs.py: XDG folders read at call time, the Darwin SDK bundle lookup and tilde().
Also checks the MCP server reads the same folders. Stdlib only:
    python -m pytest tests/helper/test_core_appdirs.py
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.core import appdirs
from iosstudio.features import account, crash_symbols, editor_setup

XDG = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME")


class XdgDirs(unittest.TestCase):
    def test_follow_the_environment_at_call_time(self):
        with tempfile.TemporaryDirectory() as t:
            env = {v: f"{t}/{v.lower()}" for v in XDG}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(appdirs.config_home(), Path(t, "xdg_config_home"))
                self.assertEqual(appdirs.state_dir(), Path(t, "xdg_state_home", "ios-studio"))
                self.assertEqual(appdirs.cache_dir(), Path(t, "xdg_cache_home", "ios-studio"))
                self.assertEqual(appdirs.data_dir(), Path(t, "xdg_data_home", "ios-studio"))
                self.assertEqual(account.xtool_data_dir(), Path(t, "xdg_config_home", "xtool", "data"))

    def test_unset_or_empty_means_the_default(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"HOME": home, "XDG_CONFIG_HOME": "", "XDG_STATE_HOME": ""}):
                os.environ.pop("XDG_CACHE_HOME", None)
                os.environ.pop("XDG_DATA_HOME", None)
                self.assertEqual(appdirs.config_dir(), Path(home, ".config/ios-studio"))
                self.assertEqual(appdirs.state_dir(), Path(home, ".local/state/ios-studio"))
                self.assertEqual(appdirs.cache_dir(), Path(home, ".cache/ios-studio"))
                self.assertEqual(appdirs.data_dir(), Path(home, ".local/share/ios-studio"))

    def test_editor_env_uses_the_same_folders(self):
        env = editor_setup.Env.from_os()
        self.assertEqual((env.config, env.data, env.state),
                         (appdirs.config_home(), appdirs.data_home(), appdirs.state_dir()))


class SdkBundle(unittest.TestCase):
    def test_candidates_are_deduplicated_and_config_first(self):
        home = Path("/h")
        self.assertEqual(appdirs.sdk_bundles(home / ".config", home),
                         [home / ".config/swiftpm/swift-sdks/darwin.artifactbundle",
                          home / ".swiftpm/swift-sdks/darwin.artifactbundle"])
        self.assertEqual(appdirs.sdk_bundles(Path("/c"), home)[0], Path("/c/swiftpm/swift-sdks/darwin.artifactbundle"))

    def test_finds_the_installed_bundle_and_its_tools(self):
        with tempfile.TemporaryDirectory() as t:
            with mock.patch.dict(os.environ, {"HOME": f"{t}/home", "XDG_CONFIG_HOME": f"{t}/cfg"}):
                self.assertIsNone(appdirs.sdk_bundle())
                bundle = Path(t, "home/.swiftpm/swift-sdks/darwin.artifactbundle")
                (bundle / "toolset/bin").mkdir(parents=True)
                tool = bundle / "toolset/bin/iosstudio-test-tool"
                tool.write_text("")
                self.assertEqual(appdirs.sdk_bundle(), bundle)
                self.assertEqual(crash_symbols.find_tool("iosstudio-test-tool"), str(tool))
                env = editor_setup.Env.from_os()
                self.assertEqual(editor_setup.ios_sdk(env), str(bundle))


class Tilde(unittest.TestCase):
    def test_tilde(self):
        self.assertEqual(appdirs.tilde("/home/a/x/y", "/home/a"), "~/x/y")
        self.assertEqual(appdirs.tilde("/home/a", "/home/a/"), "~")
        self.assertEqual(appdirs.tilde("/home/ab/x", "/home/a"), "/home/ab/x")   # not a prefix match
        self.assertEqual(appdirs.tilde("/opt/x", "/home/a"), "/opt/x")
        self.assertEqual(appdirs.tilde(None), "")
        self.assertEqual(appdirs.tilde(Path.home() / "f"), "~/f")


class McpUsesTheSameFolders(unittest.TestCase):
    def test_mcp_paths(self):
        try:
            from iosstudio_mcp import helper, state
            from iosstudio_mcp.tools import build
        except ImportError as exc:   # the MCP SDK isn't installed in this environment
            self.skipTest(str(exc))
        self.assertIs(helper.appdirs, appdirs)
        self.assertEqual(state.DEVICE_STATE, appdirs.state_dir() / "device.json")
        self.assertEqual(build.BUILD_LOGS, appdirs.state_dir() / "build-logs")


if __name__ == "__main__":
    unittest.main()
