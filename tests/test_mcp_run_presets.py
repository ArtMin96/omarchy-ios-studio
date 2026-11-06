"""MCP side of run presets (mcp/iosstudio_mcp/tools/run_presets.py and the preset= parameters): ios_project_presets
against a mocked `projects presets` helper call (list, save new vs replace, delete dry run vs confirm, select,
the unknown-preset error), ios_run passing --preset and its installer launching with the build's launch args/env,
ios_app_launch merging a preset with explicit args/env (project found by bundle id), and registration.

Needs the MCP server's dependencies (skipped otherwise):
    ~/.local/share/ios-studio/mcp-venv/bin/python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))

try:
    from mcp.server.mcpserver.exceptions import ToolError
    from iosstudio_mcp import budget as B
    from iosstudio_mcp import helper as H
    from iosstudio_mcp.jobs import Job
    from iosstudio_mcp.state import AppState
    from iosstudio_mcp.tools import build as BUILD
    from iosstudio_mcp.tools import device as D
    from iosstudio_mcp.tools import logs as LOGS
    from iosstudio_mcp.tools import run_presets as T
except ImportError as exc:  # the system python has no `mcp` package
    raise unittest.SkipTest(f"MCP dependencies not installed ({exc})")

PROJ = "/home/u/Developer/Glance"
BUNDLE = "com.example.glance"
GERMAN = {"name": "German", "args": [], "env": {}, "language": "de", "region": "de_DE", "uiTestMode": False,
          "resolved": {"args": ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"], "env": {}}}
DEBUG = {"name": "Debug", "args": ["-ShowDebugMenu", "YES"], "env": {"API_ENV": "staging", "LOG": "1"},
         "language": None, "region": None, "uiTestMode": True,
         "resolved": {"args": ["-ShowDebugMenu", "YES", "-UITestMode", "YES"],
                      "env": {"API_ENV": "staging", "LOG": "1"}}}
UNKNOWN = H.HelperFailure({"code": "unknown-preset",
                           "message": "No run preset named 'French'. This project has: German, Debug."})


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def listing(presets=(GERMAN, DEBUG), selected=None, **extra) -> dict:
    return {"path": PROJ, "presets": [dict(p) for p in presets], "selected": selected, **extra}


def ctx_for(state: AppState):
    async def report_progress(*a, **k):
        return None
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state), report_progress=report_progress)


async def fake_resolve_project(state, project):
    return {"name": "Glance", "path": PROJ, "bundleId": BUNDLE}


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = AppState()
        self.state._projects = (time.time(), {"projects": [
            {"name": "Glance", "path": PROJ, "bundleId": BUNDLE},
            {"name": "Other", "path": "/home/u/Developer/Other", "bundleId": "com.example.other"}]})
        self.ctx = ctx_for(self.state)
        self.calls: list = []
        self.replies: list = []     # consumed in order; an Exception is raised

    def fake_call(self):
        async def call(group, action, flags=None, **_):
            self.calls.append((group, action, dict(flags or {})))
            r = self.replies.pop(0) if self.replies else {}
            if isinstance(r, Exception):
                raise r
            return r
        return call

    def patches(self, *extra):
        ps = [mock.patch.object(H, "call", self.fake_call()),
              mock.patch.object(T, "resolve_project", fake_resolve_project)] + list(extra)
        stack = __import__("contextlib").ExitStack()
        for p in ps:
            stack.enter_context(p)
        return stack


class PresetsTool(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        mcp = FakeMCP()
        T.register(mcp)
        self.tool = mcp.tools["ios_project_presets"]

    async def run_tool(self, **kw):
        with self.patches():
            res = await self.tool(self.ctx, **{"project_path": "Glance", **kw})
        text = "".join(c.text for c in res.content if getattr(c, "text", None))
        return res.structured_content, text

    async def test_list(self):
        self.replies = [listing(selected="German")]
        sc, _ = await self.run_tool()
        self.assertEqual(self.calls, [("projects", "presets", {"path": PROJ})])
        self.assertFalse(sc["done"])
        self.assertEqual((sc["total"], sc["count"], sc["selected"]), (2, 2, "German"))
        de, dbg = sc["presets"]
        self.assertEqual((de["name"], de["language"], de["region"]), ("German", "de", "de_DE"))
        self.assertTrue(de["selected"])
        self.assertEqual(de["resolved"]["args"], ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"])
        self.assertTrue(dbg["ui_test_mode"])
        self.assertEqual(dbg["env"], {"API_ENV": "staging", "LOG": "1"})
        self.assertNotIn("selected", dbg)
        self.assertIn("preset=", sc["next_steps"][0])

    async def test_list_one_and_empty(self):
        self.replies = [listing()]
        sc, _ = await self.run_tool(name="Debug")
        self.assertEqual([p["name"] for p in sc["presets"]], ["Debug"])
        self.replies = [listing()]
        with self.assertRaises(ToolError) as cm:
            await self.run_tool(name="Nope")
        self.assertIn("German, Debug", str(cm.exception))
        self.replies = [listing(presets=())]
        sc, _ = await self.run_tool()
        self.assertEqual(sc["total"], 0)
        self.assertIn("action='save'", sc["next_steps"][0])

    async def test_save_new_maps_to_camel_case(self):
        self.replies = [listing(presets=(DEBUG,)), listing(presets=(DEBUG, GERMAN))]
        sc, _ = await self.run_tool(action="save", preset={"name": "German", "language": "de", "region": "de_DE",
                                                           "ui_test_mode": True, "env": {"A": "1"}})
        (_, _, first), (_, _, flags) = self.calls
        self.assertEqual(first, {"path": PROJ})
        sent = json.loads(flags["save"])
        self.assertEqual(sent, {"name": "German", "args": [], "env": {"A": "1"}, "language": "de",
                                "region": "de_DE", "uiTestMode": True})
        self.assertNotIn("rename_from", {k: v for k, v in flags.items() if v is not None})
        self.assertIn("--save", H.build_argv("projects", "presets", flags))
        self.assertTrue(sc["done"])
        self.assertFalse(sc["replaced"])
        self.assertEqual(sc["presets"][0]["name"], "German")     # the saved one first
        self.assertIn("Saved preset 'German'", sc["message"])
        self.assertIn("preset='German'", sc["next_steps"][0])

    async def test_save_replace_says_so(self):
        self.replies = [listing(), listing()]
        sc, _ = await self.run_tool(action="save", preset={"name": "German", "language": "de"})
        self.assertTrue(sc["replaced"])
        self.assertIn("Replaced the existing preset 'German'", sc["message"])

    async def test_rename(self):
        self.replies = [listing(), listing()]
        sc, _ = await self.run_tool(action="save", preset={"name": "Deutsch", "language": "de"},
                                    rename_from="German")
        self.assertEqual(self.calls[1][2]["rename_from"], "German")
        self.assertIn("--rename-from", H.build_argv("projects", "presets", self.calls[1][2]))
        self.assertFalse(sc["replaced"])
        self.assertIn("Renamed preset 'German'", sc["message"])
        self.replies = [listing()]
        with self.assertRaises(ToolError):
            await self.run_tool(action="save", preset={"name": "X"}, rename_from="Missing")

    async def test_names_match_ignoring_case(self):
        self.replies = [listing(), listing()]
        sc, _ = await self.run_tool(action="save", preset={"name": "german", "language": "de"})
        self.assertTrue(sc["replaced"])
        self.assertEqual(sc["presets"][0]["name"], "German")
        self.replies = [listing(), listing()]
        sc, _ = await self.run_tool(action="save", preset={"name": "Deutsch"}, rename_from="german")
        self.assertFalse(sc["replaced"])
        self.assertEqual(self.calls[-1][2]["rename_from"], "german")
        self.replies = [listing(), listing()]
        sc, _ = await self.run_tool(action="save", preset={"name": "german"}, rename_from="German")   # case only
        self.assertFalse(sc["replaced"])
        self.replies = [listing(selected="german")]
        sc, _ = await self.run_tool(name="GERMAN")
        self.assertEqual([p["name"] for p in sc["presets"]], ["German"])
        self.assertTrue(sc["presets"][0]["selected"])
        self.replies = [listing(selected="german", deleted="German", dryRun=True)]
        sc, _ = await self.run_tool(action="delete", name="German")
        self.assertEqual(len(sc["would"]), 2)

    async def test_name_length(self):
        with self.assertRaises(Exception):
            T.PresetIn(name="x" * 41)
        self.assertEqual(T.PresetIn(name="x" * 40).name, "x" * 40)

    async def test_save_needs_preset(self):
        with self.assertRaises(ToolError):
            await self.run_tool(action="save")

    async def test_delete_dry_run_then_confirm(self):
        self.replies = [listing(selected="Debug", deleted="Debug", dryRun=True)]
        sc, _ = await self.run_tool(action="delete", name="Debug")
        self.assertEqual(self.calls[0][2], {"path": PROJ, "delete": "Debug", "dry_run": True})
        self.assertFalse(sc["done"])
        self.assertIn("Dry run", sc["message"])
        self.assertIn("confirm=true", sc["message"])
        self.assertEqual(len(sc["would"]), 2)          # also: it is the selected preset
        self.assertIn("confirm=true", sc["next_steps"][0])

        self.calls.clear()
        self.replies = [listing(presets=(GERMAN,), deleted="Debug", dryRun=False)]
        sc, _ = await self.run_tool(action="delete", name="Debug", confirm=True)
        self.assertEqual(self.calls[0][2], {"path": PROJ, "delete": "Debug", "dry_run": None})
        self.assertNotIn("--dry-run", H.build_argv("projects", "presets", self.calls[0][2]))
        self.assertTrue(sc["done"])
        self.assertEqual([p["name"] for p in sc["presets"]], ["German"])
        self.assertNotIn("would", sc)

    async def test_select_and_clear(self):
        self.replies = [listing(selected="German")]
        sc, _ = await self.run_tool(action="select", name="German")
        self.assertEqual(self.calls[0][2], {"path": PROJ, "select": "German"})
        self.assertTrue(sc["done"])
        self.assertEqual(sc["selected"], "German")
        self.calls.clear()
        self.replies = [listing()]
        sc, _ = await self.run_tool(action="select", name="")
        self.assertEqual(H.build_argv("projects", "presets", self.calls[0][2])[-2:], ["--select", ""])
        self.assertNotIn("selected", sc)
        self.assertIn("Cleared", sc["message"])

    async def test_unknown_preset_error(self):
        self.replies = [UNKNOWN]
        with self.assertRaises(ToolError) as cm:
            await self.run_tool(action="delete", name="French")
        msg = str(cm.exception)
        self.assertIn("German, Debug", msg)
        self.assertIn("[unknown-preset]", msg)
        self.assertIn("ios_project_presets", msg)

    async def test_output_is_bounded(self):
        many = [{"name": f"P{i}", "args": ["-Flag" + "x" * 280] * 30, "env": {f"K{j}": "v" * 280 for j in range(10)},
                 "resolved": {"args": ["-Flag" + "x" * 280] * 30, "env": {}}} for i in range(20)]
        self.replies = [listing(presets=many)]
        sc, text = await self.run_tool()
        size = B.nbytes(text) + B.nbytes(json.dumps(sc, ensure_ascii=False, separators=(",", ":")))
        self.assertLessEqual(size, B.DEFAULT_BYTES)
        self.assertTrue(sc["truncated"])
        self.assertLess(sc["count"], 20)
        self.assertIn("name=<preset>", sc["note"])


class RunWithPreset(Base):
    async def test_ios_run_passes_preset(self):
        seen = {}

        async def fake_start_and_wait(state, ctx, **kw):
            seen.update(kw)
            return "status"

        self.replies = [{"name": "German", "args": ["-AppleLanguages", "(de)"], "env": {}}]
        with self.patches(mock.patch.object(BUILD, "resolve_project", fake_resolve_project),
                          mock.patch.object(BUILD, "start_and_wait", fake_start_and_wait)):
            await BUILD.start_build(self.state, self.ctx, "Glance", "run", "debug", "UDID1", "auto", 0, "German")
        self.assertEqual(self.calls, [("projects", "presets", {"path": PROJ, "resolve": "German"})])
        self.assertEqual(seen["flags"]["preset"], "German")
        argv = H.build_argv("build", "run", seen["flags"])
        self.assertEqual(argv[argv.index("--preset") + 1], "German")

    async def test_ios_run_unknown_preset_fails_before_building(self):
        started = []

        async def fake_start_and_wait(state, ctx, **kw):
            started.append(kw)

        self.replies = [UNKNOWN]
        with self.patches(mock.patch.object(BUILD, "resolve_project", fake_resolve_project),
                          mock.patch.object(BUILD, "start_and_wait", fake_start_and_wait)):
            with self.assertRaises(ToolError) as cm:
                await BUILD.start_build(self.state, self.ctx, "Glance", "run", "debug", "UDID1", "auto", 0, "French")
        self.assertIn("German, Debug", str(cm.exception))
        self.assertEqual(started, [])

    async def test_ios_run_without_preset_sends_none(self):
        seen = {}

        async def fake_start_and_wait(state, ctx, **kw):
            seen.update(kw)

        with self.patches(mock.patch.object(BUILD, "resolve_project", fake_resolve_project),
                          mock.patch.object(BUILD, "start_and_wait", fake_start_and_wait)):
            await BUILD.start_build(self.state, self.ctx, "Glance", "run", "debug", "UDID1", "auto", 0)
        self.assertNotIn("preset", seen["flags"])
        self.assertEqual(self.calls, [])

    def job(self, launch_info=None) -> Job:
        r = {"needsInstall": True, "udid": "UDID1", "ipa": "/tmp/G.ipa", "bundleId": BUNDLE}
        if launch_info is not None:
            r["launch"] = launch_info
        j = Job(id="b-1", kind="build", action="run", title="Run", project_path=PROJ)
        j.result = r
        return j

    async def test_installer_launches_with_the_builds_launch_args(self):
        launched = []

        async def install_app(udid, ipa, job):
            return {}

        async def launch_app(udid, bundle_id, args=None, env=None):
            launched.append((udid, bundle_id, args, env))
            return {"pid": 99}

        self.state.install_app, self.state.launch_app = install_app, launch_app
        job = self.job({"bundleId": BUNDLE, "udid": "UDID1", "args": ["-AppleLanguages", "(de)"],
                        "env": {"API_ENV": "staging"}, "preset": "German"})
        with self.patches():
            await BUILD.make_installer(self.state, launch=True)(job)
        self.assertEqual(launched, [("UDID1", BUNDLE, ["-AppleLanguages", "(de)"], {"API_ENV": "staging"})])
        self.assertTrue(job.result["launched"])
        from iosstudio_mcp.tools import _result_model, job_next_steps
        res = _result_model(job)
        self.assertEqual((res.preset, res.launch_args), ("German", ["-AppleLanguages", "(de)"]))
        job.state = "succeeded"
        self.assertTrue(any("preset='German'" in s for s in job_next_steps(job)))

    async def test_installer_without_preset_keeps_the_two_argument_call(self):
        launched = []

        async def install_app(udid, ipa, job):
            return {}

        async def launch_app(udid, bundle_id):      # an old-style slot still works
            launched.append((udid, bundle_id))
            return {"pid": 7}

        self.state.install_app, self.state.launch_app = install_app, launch_app
        job = self.job({"bundleId": BUNDLE, "udid": "UDID1", "args": [], "env": {}, "preset": None})
        with self.patches():
            await BUILD.make_installer(self.state, launch=True)(job)
        self.assertEqual(launched, [("UDID1", BUNDLE)])

    def slots(self, launched: list, installs: list, fail: bool = False):
        async def install_app(udid, ipa, job):
            installs.append(ipa)
            return {}

        async def launch_app(udid, bundle_id, args=None, env=None):
            launched.append((udid, bundle_id, args, env))
            if fail:
                raise ToolError("The iPhone is locked.")
            return {"pid": 99}

        self.state.install_app, self.state.launch_app = install_app, launch_app

    def usb_job(self) -> Job:
        j = Job(id="b-3", kind="build", action="run", title="Run", project_path=PROJ)
        j.result = {"installed": True, "needsInstall": False, "udid": "UDID1", "via": "usb", "bundleId": BUNDLE,
                    "preset": "German", "launch": {"bundleId": BUNDLE, "udid": "UDID1",
                                                   "args": ["-AppleLanguages", "(de)"], "env": {"A": "1"},
                                                   "preset": "German"}}
        return j

    async def test_usb_run_launches_with_the_preset(self):
        launched, installs = [], []
        self.slots(launched, installs)
        job = self.usb_job()
        with self.patches():
            await BUILD.make_installer(self.state, launch=True)(job)
        self.assertEqual(installs, [])                  # xtool already installed it over USB
        self.assertEqual(self.calls, [])                # no record-install either
        self.assertEqual(launched, [("UDID1", BUNDLE, ["-AppleLanguages", "(de)"], {"A": "1"})])
        self.assertEqual((job.result["launched"], job.result["pid"]), (True, 99))
        self.assertEqual(job.steps["launch"]["state"], "done")

    async def test_usb_run_launch_failure_stays_green(self):
        launched, installs = [], []
        self.slots(launched, installs, fail=True)
        job = self.usb_job()
        with self.patches():
            await BUILD.make_installer(self.state, launch=True)(job)
        self.assertEqual(job.steps["launch"]["state"], "failed")
        self.assertFalse(job.result.get("launched"))
        self.assertTrue(job.result["installed"])
        self.assertTrue(any("didn't open" in s for s in job.next_steps))

    async def test_usb_already_launched_is_left_alone(self):
        launched, installs = [], []
        self.slots(launched, installs)
        job = self.usb_job()
        job.result["launched"] = True
        with self.patches():
            await BUILD.make_installer(self.state, launch=True)(job)
        self.assertEqual(launched, [])

    async def test_wifi_run_installs_then_launches(self):
        launched, installs = [], []
        self.slots(launched, installs)
        job = self.job({"bundleId": BUNDLE, "udid": "UDID1", "args": ["-X"], "env": {}, "preset": "P"})
        with self.patches():
            await BUILD.make_installer(self.state, launch=True)(job)
        self.assertEqual(installs, ["/tmp/G.ipa"])
        self.assertEqual(self.calls[0][1], "record-install")
        self.assertEqual(launched, [("UDID1", BUNDLE, ["-X"], None)])
        self.assertTrue(job.result["installed"] and job.result["launched"])

    async def test_install_action_never_launches(self):
        for job in (self.usb_job(), self.job({"bundleId": BUNDLE, "udid": "UDID1", "args": ["-X"]})):
            launched, installs = [], []
            self.slots(launched, installs)
            with self.patches():
                await BUILD.make_installer(self.state, launch=False)(job)
            self.assertEqual(launched, [])
            self.assertFalse(job.result.get("launched"))
            self.assertNotIn("launch", job.steps)

    async def test_device_slot_passes_args_to_the_daemon(self):
        sent = []

        async def fake_resolve_app(state, bundle_id, udid=None):
            return {"bundleId": "XTL-ABC." + BUNDLE}

        async def fake_dcall(state, cmd, a=None, udid=None, timeout=None):
            sent.append((cmd, a))
            return {"pid": 5}

        with mock.patch.object(D, "resolve_app", fake_resolve_app), mock.patch.object(D, "dcall", fake_dcall):
            await D.on_startup(self.state)
            await self.state.launch_app("UDID1", BUNDLE, args=["-X"], env={"A": "1"})
            await self.state.launch_app("UDID1", BUNDLE)
        self.assertEqual(sent[0], ("app.launch", {"bundleId": "XTL-ABC." + BUNDLE, "console": True, "args": ["-X"],
                                                  "env": {"A": "1"}}))
        self.assertEqual(sent[1], ("app.launch", {"bundleId": "XTL-ABC." + BUNDLE, "console": True}))


class AppLaunchWithPreset(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        mcp = FakeMCP()
        D.register(mcp)
        self.tool = mcp.tools["ios_app_launch"]
        self.sent: list = []

    async def launch(self, **kw):
        async def fake_resolve_app(state, bundle_id, udid=None):
            return {"bundleId": "XTL-ABC." + BUNDLE, "name": "Glance"}

        async def fake_dcall(state, cmd, a=None, udid=None, timeout=None):
            self.sent.append((cmd, a))
            return {"pid": 31}

        with self.patches(mock.patch.object(D, "resolve_app", fake_resolve_app),
                          mock.patch.object(D, "dcall", fake_dcall)):
            res = await self.tool(self.ctx, **kw)
        return res.structured_content

    async def test_preset_merged_with_explicit_args_and_env(self):
        self.replies = [{"name": "Debug", "args": ["-ShowDebugMenu", "YES", "-UITestMode", "YES"],
                         "env": {"API_ENV": "staging", "LOG": "1"}}]
        sc = await self.launch(bundle_id="XTL-ABC." + BUNDLE, preset="Debug", args=["-Extra", "1"],
                               env={"API_ENV": "prod"})
        # the project was found by the app's bundle id (XTL- form)
        self.assertEqual(self.calls, [("projects", "presets", {"path": PROJ, "resolve": "Debug"})])
        (cmd, a), = self.sent
        self.assertEqual(cmd, "app.launch")
        self.assertEqual(a["args"], ["-ShowDebugMenu", "YES", "-UITestMode", "YES", "-Extra", "1"])
        self.assertEqual(a["env"], {"API_ENV": "prod", "LOG": "1"})
        self.assertEqual((sc["preset"], sc["pid"]), ("Debug", 31))
        self.assertEqual(sc["env"], {"API_ENV": "prod", "LOG": "1"})

    async def test_project_path_wins(self):
        self.replies = [{"name": "German", "args": ["-AppleLanguages", "(de)"], "env": {}}]
        await self.launch(bundle_id="com.unlisted.app", preset="German", project_path="Glance")
        self.assertEqual(self.calls[0][2], {"path": PROJ, "resolve": "German"})
        self.assertEqual(self.sent[0][1]["args"], ["-AppleLanguages", "(de)"])
        self.assertNotIn("env", self.sent[0][1])

    async def test_no_project_for_bundle(self):
        with self.assertRaises(ToolError) as cm:
            await self.launch(bundle_id="com.unlisted.app", preset="German")
        self.assertIn("project_path", str(cm.exception))
        self.assertEqual(self.sent, [])

    async def test_unknown_preset(self):
        self.replies = [UNKNOWN]
        with self.assertRaises(ToolError) as cm:
            await self.launch(bundle_id=BUNDLE, preset="French")
        self.assertIn("This project has: German, Debug", str(cm.exception))
        self.assertEqual(self.sent, [])

    async def test_without_preset_unchanged(self):
        sc = await self.launch(bundle_id=BUNDLE, args=["-A"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sent[0][1], {"bundleId": "XTL-ABC." + BUNDLE, "args": ["-A"]})
        self.assertNotIn("preset", sc)
        self.assertNotIn("args", sc)


class Merge(unittest.TestCase):
    def test_merge_launch(self):
        self.assertEqual(T.merge_launch(None, ["-a"], {"A": "1"}), (["-a"], {"A": "1"}))
        self.assertEqual(T.merge_launch({"args": ["-p"], "env": {"A": "0", "B": "2"}}, ["-a"], {"A": "1"}),
                         (["-p", "-a"], {"A": "1", "B": "2"}))
        self.assertEqual(T.merge_launch({"args": [], "env": {}}, None, None), (None, None))

    def test_plain_bundle(self):
        self.assertEqual(T.plain_bundle("XTL-ABC123.com.example.app"), "com.example.app")
        self.assertEqual(T.plain_bundle("com.example.app"), "com.example.app")


class BuildOutputs(unittest.TestCase):
    def test_history_and_result_show_the_preset(self):
        from iosstudio_mcp.models import HistoryEntry
        from iosstudio_mcp.tools import _result_model
        self.assertEqual(HistoryEntry.from_entry({"kind": "run", "preset": "German"}).preset, "German")
        self.assertIsNone(HistoryEntry.from_entry({"kind": "run"}).preset)
        j = Job(id="b-2", kind="build", action="run", title="Run")
        j.result = {"bundleId": BUNDLE, "preset": "German"}           # top-level preset, no launch data
        self.assertEqual(_result_model(j).preset, "German")
        j.result = {"bundleId": BUNDLE}
        self.assertIsNone(_result_model(j).preset)


class ProjectInfoPresets(unittest.TestCase):
    def test_project_detail_lists_presets(self):
        from iosstudio_mcp.models import ProjectDetail
        d = ProjectDetail.from_helper({"name": "Glance", "path": PROJ, "runPresets": [GERMAN, DEBUG],
                                       "runPreset": "German"})
        self.assertEqual((d.run_presets, d.run_preset), (["German", "Debug"], "German"))
        e = B.dump(ProjectDetail.from_helper({"name": "Glance", "path": PROJ}))
        self.assertNotIn("run_presets", e)
        self.assertNotIn("run_preset", e)


class Docstrings(unittest.TestCase):
    def test_launch_docs(self):
        mcp = FakeMCP()
        D.register(mcp)
        doc = mcp.tools["ios_app_launch"].__doc__
        self.assertIn("ended and started", doc)
        self.assertNotIn("kill it first", doc)
        mcp = FakeMCP()
        BUILD.register(mcp)
        doc = mcp.tools["ios_run"].__doc__
        self.assertIn("Either way the server then launches it", doc)
        self.assertIn("Without preset=", doc)


class Registration(unittest.TestCase):
    def test_server_lists_the_tools(self):
        from iosstudio_mcp import server
        mcp = server.create_server()
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        t = tools["ios_project_presets"]
        self.assertEqual(t.parameters["properties"]["action"]["enum"], ["list", "save", "delete", "select"])
        self.assertTrue(t.annotations.destructive_hint)
        for name in ("ios_run", "ios_app_launch", "ios_app_run_and_capture"):
            self.assertIn("preset", tools[name].parameters["properties"], name)
        self.assertIn("project_path", tools["ios_app_launch"].parameters["properties"])
        self.assertIn("ios_project_presets", server.INSTRUCTIONS)
        self.assertIn("unknown-preset", H.NEXT_BY_CODE)
        self.assertTrue(hasattr(LOGS, "RunCaptureResult"))


if __name__ == "__main__":
    unittest.main()
