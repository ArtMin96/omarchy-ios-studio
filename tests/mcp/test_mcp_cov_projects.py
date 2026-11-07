"""tools/projects.py against a fake iosstudio-helper (real helper.call, real @tool wrapper): list (paged and
budgeted), info, templates, create (a job), add, convert, set and remove (destructive: a dry run until
confirm=true).

    python -m pytest tests/mcp/test_mcp_cov_projects.py
"""
from __future__ import annotations

import os
from unittest import mock

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.tools import jobs as JOBS
from iosstudio_mcp.tools import projects as M
from support.mcp_harness import FakeHelper, ToolCase, size_of

TEMPLATES = {"templates": [{"id": "swiftui-app", "title": "SwiftUI app", "default": True},
                           {"id": "tabs", "title": "Tabs"}], "bundlePrefix": "com.dev", "defaultDir": "/home/u/Developer"}


class Base(ToolCase):
    DAEMON = False

    async def asyncSetUp(self):
        await super().asyncSetUp()
        p = mock.patch.dict(os.environ, {"IOSSTUDIO_MCP_ALLOW_PATHS": str(self.tmp)})
        p.start()
        self.addCleanup(p.stop)

    def entry(self, name="App", **extra):
        path = self.tmp / name
        path.mkdir(exist_ok=True)
        return {"name": name, "path": str(path), "bundleId": f"com.dev.{name}", "product": name, **extra}

    def listed(self, *entries, **extra):
        self.helper.reply("projects", "list", {"projects": list(entries), **extra})


class Annotations(Base):
    def test_kinds(self):
        ann = self.rec(M).annotations
        for name in ("ios_project_list", "ios_project_info", "ios_project_templates"):
            self.assertTrue(ann[name].read_only_hint, name)
            self.assertFalse(ann[name].open_world_hint, name)
        for name in ("ios_project_create", "ios_project_add", "ios_project_convert", "ios_project_set"):
            self.assertFalse(ann[name].read_only_hint, name)
            self.assertFalse(ann[name].destructive_hint, name)
        self.assertTrue(ann["ios_project_remove"].destructive_hint)


class ListAndInfo(Base):
    async def test_list_filters_and_pages(self):
        self.listed(*[self.entry(f"App{i}") for i in range(5)], self.entry("Weather"),
                    convertible=[{"name": "Pkg", "path": "/home/u/Pkg", "reason": "SwiftUI @main"}])
        d = await self.data(M, "ios_project_list", limit=2)
        self.assertEqual((d["total"], d["count"], d["next_offset"]), (6, 2, 2))
        self.assertIn("offset=2", d["note"])
        self.assertEqual(d["convertible"][0]["name"], "Pkg")
        d = await self.data(M, "ios_project_list", query="weather")
        self.assertEqual([p["name"] for p in d["projects"]], ["Weather"])

    async def test_many_projects_fit_the_budget(self):
        long = "x" * 250
        self.listed(*[self.entry(f"App{i}", branch=long, lastBuild={"result": "failed", "errorMessage": long})
                      for i in range(100)])
        res = await self.call(M, "ios_project_list", limit=100)
        d = res.structured_content
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        self.assertTrue(d["has_more"])
        self.assertEqual(d["next_offset"], d["count"])

    async def test_empty_list_says_how_to_start(self):
        self.listed()
        d = await self.data(M, "ios_project_list")
        self.assertIn("ios_project_create", d["next_steps"][0])

    async def test_info_by_name(self):
        e = self.entry()
        (self.tmp / "App" / "Sources" / "App").mkdir(parents=True)
        self.listed(e)
        self.helper.reply("projects", "info", {**e, "lastBuild": {"result": "failed"}})
        res = await self.call(M, "ios_project_info", project_path="app")
        d = res.structured_content
        self.assertEqual(d["sources"], f"{e['path']}/Sources/App")
        self.assertIn("ios_build_diagnostics", d["next_steps"][0])
        self.assertEqual(FakeHelper.flag(self.helper.calls("projects", "info")[0], "path"), e["path"])
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)

    async def test_info_of_a_generated_project_points_to_build_settings(self):
        e = self.entry(generated=True, suggestedBuildSettings={"prebuild": "./gen.sh"})
        self.listed(e)
        self.helper.reply("projects", "info", e)
        d = await self.data(M, "ios_project_info", project_path=e["path"])
        self.assertIn("apply_suggested=true", d["next_steps"][0])

    async def test_a_plain_package_folder_is_told_to_convert(self):
        self.listed()
        pkg = self.tmp / "Pkg"
        pkg.mkdir()
        (pkg / "Package.swift").write_text("// swift-tools-version:5.9\n")
        text = await self.error(M, "ios_project_info", project_path=str(pkg))
        self.assertIn("ios_project_convert", text)

    async def test_helper_failure_is_one_line_with_next_step(self):
        e = self.entry()
        self.listed(e)
        self.helper.fail("projects", "info", "not-found", "That project is gone.")
        text = await self.error(M, "ios_project_info", project_path="App")
        self.assertIn("[not-found] Next: Call ios_project_list", text)


class Templates(Base):
    async def test_templates(self):
        self.helper.reply("projects", "templates", TEMPLATES)
        d = await self.data(M, "ios_project_templates")
        self.assertEqual([t["id"] for t in d["templates"]], ["swiftui-app", "tabs"])
        self.assertEqual(d["bundle_prefix"], "com.dev")


class Create(Base):
    async def test_create_is_a_job_and_the_same_call_joins_it(self):
        self.helper.reply("projects", "templates", TEMPLATES)
        e = self.entry("Glance")
        self.helper.reply("projects", "new", e, delay=0.5)
        first = await self.data(M, "ios_project_create", name="Glance", directory=str(self.tmp), wait_seconds=0)
        self.assertFalse(first["done"])
        job_id = first["job_id"]
        self.assertIn("job_id", first["next_steps"][0])
        again = await self.data(M, "ios_project_create", name="Glance", directory=str(self.tmp), wait_seconds=0)
        self.assertEqual(again["job_id"], job_id)                      # joined, not a second create
        st = await self.data(JOBS, "ios_job_status", job_id=job_id, wait_seconds=3)
        self.assertEqual(st["state"], "succeeded")
        done = await self.data(M, "ios_project_create", job_id=job_id)
        self.assertTrue(done["done"])
        self.assertEqual(done["project"]["name"], "Glance")
        argv = self.helper.calls("projects", "new")
        self.assertEqual(len(argv), 1)
        self.assertEqual(FakeHelper.flag(argv[0], "dir"), str(self.tmp))

    async def test_bad_arguments_never_reach_the_helper(self):
        self.helper.reply("projects", "templates", TEMPLATES)
        for kw, want in (({"name": "my app"}, "no spaces"), ({"name": "App", "bundle_id": "nodots"}, "com.yourname"),
                         ({"name": "App", "template": "nope"}, "Valid ids: swiftui-app, tabs"),
                         ({"name": "App", "directory": "/etc"}, "inside your home folder")):
            text = await self.error(M, "ios_project_create", **kw)
            self.assertIn(want, text)
        self.assertFalse(self.helper.calls("projects", "new"))

    async def test_a_failed_create_raises_its_message(self):
        self.helper.reply("projects", "templates", TEMPLATES)
        self.helper.fail("projects", "new", "exists", "The folder already exists.")
        first = await self.data(M, "ios_project_create", name="Glance", wait_seconds=0)
        await self.data(JOBS, "ios_job_status", job_id=first["job_id"], wait_seconds=3)
        text = await self.error(M, "ios_project_create", job_id=first["job_id"])
        self.assertIn("already exists", text)


class AddConvertSet(Base):
    async def test_add(self):
        e = self.entry()
        self.helper.reply("projects", "add", e)
        d = await self.data(M, "ios_project_add", project_path=e["path"])
        self.assertTrue(d["done"])
        self.assertEqual(d["project"]["bundle_id"], "com.dev.App")

    async def test_add_missing_folder(self):
        text = await self.error(M, "ios_project_add", project_path=str(self.tmp / "nope"))
        self.assertIn("No project folder at", text)

    async def test_add_outside_allowed_roots(self):
        with mock.patch.dict(os.environ, {"IOSSTUDIO_MCP_ALLOW_PATHS": ""}), mock.patch.object(
                M.helper, "HOME", self.tmp / "home"):
            text = await self.error(M, "ios_project_add", project_path=str(self.tmp))
        self.assertIn("inside your home folder", text)

    async def test_convert(self):
        e = self.entry("Pkg")
        (self.tmp / "Pkg" / "Package.swift").write_text("// swift-tools-version:5.9\n")
        self.helper.reply("projects", "convert", e)
        d = await self.data(M, "ios_project_convert", project_path=e["path"], bundle_id="com.dev.Pkg")
        self.assertTrue(d["done"])
        self.assertEqual(FakeHelper.flag(self.helper.calls("projects", "convert")[0], "bundle_id"), "com.dev.Pkg")

    async def test_convert_needs_a_package(self):
        e = self.entry("Empty")
        text = await self.error(M, "ios_project_convert", project_path=e["path"])
        self.assertIn("no Package.swift", text)

    async def test_convert_helper_error_has_next_step(self):
        e = self.entry("Pkg")
        (self.tmp / "Pkg" / "Package.swift").write_text("")
        self.helper.fail("projects", "convert", "not-convertible", "No SwiftUI @main App found.")
        text = await self.error(M, "ios_project_convert", project_path=e["path"])
        self.assertIn("Next: Create a fresh app with ios_project_create", text)

    async def test_set(self):
        e = self.entry()
        self.listed(e)
        self.helper.reply("projects", "set", {**e, "autoRenew": True})
        d = await self.data(M, "ios_project_set", project_path="App", auto_renew=True, udid="UDID-1")
        self.assertTrue(d["project"]["auto_renew"])
        self.assertIn("ios_project_autorenew(action='status')", d["next_steps"][0])
        argv = self.helper.calls("projects", "set")[0]
        self.assertEqual((FakeHelper.flag(argv, "auto_renew"), FakeHelper.flag(argv, "udid")), ("1", "UDID-1"))

    async def test_set_nothing(self):
        text = await self.error(M, "ios_project_set", project_path="App")
        self.assertIn("Nothing to change", text)


class Remove(Base):
    async def test_dry_run_then_confirm(self):
        e = self.entry()
        self.listed(e)
        d = await self.data(M, "ios_project_remove", project_path="App")
        self.assertFalse(d["done"])
        self.assertIn("Keep every file in the project folder", d["would"])
        self.assertFalse(self.helper.calls("projects", "remove"))
        self.helper.reply("projects", "remove", {"removed": e["path"]})
        d = await self.data(M, "ios_project_remove", project_path="App", confirm=True)
        self.assertTrue(d["done"])
        self.assertIn("ios_project_add", d["next_steps"][0])
        self.assertTrue((self.tmp / "App").is_dir())

    async def test_unknown_project(self):
        self.listed()
        text = await self.error(M, "ios_project_remove", project_path="Ghost", confirm=True)
        self.assertIn("No project named 'Ghost'", text)
