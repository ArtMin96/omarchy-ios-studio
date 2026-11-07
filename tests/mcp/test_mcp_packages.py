"""MCP side of Swift packages (mcp/iosstudio_mcp/tools/packages.py): ios_package_list against a mocked helper (shape,
max_items and byte budget, next steps), ios_package_remove's dry run, confirm and refused edit (diff kept, hint naming
MCP tools), ios_package_add routing a product for an already-declared package to `packages add-product`, and the job
flow with the real helper and a fake `swift` (tests/fixtures/packages/fake_swift.py): add -> running -> poll with
job_id -> needs_product -> add with product; update with changes; a failed add with package diagnostics; cancel
through the job manager putting Package.resolved back. Also demo mode and registration.

Needs the MCP server's dependencies (the dev venv; skipped otherwise):
    python -m pytest tests/mcp/test_mcp_packages.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from iosstudio_mcp import budget as B
from iosstudio_mcp import helper as H
from iosstudio_mcp.state import AppState
from iosstudio_mcp.tools import packages as T
from support.fakes import FAKE_SWIFT
from support.mcp_fakes import FakeMCP, size_of
from support.projects import package_project as make_project

PROJ = "/home/u/Developer/Glance"


def pkg(i: int, **kw) -> dict:
    d = {"identity": f"pkg-{i}", "name": f"pkg-{i}", "kind": "remote", "url": f"https://github.com/acme/pkg-{i}",
         "requirement": {"rule": "from", "version": "1.0.0", "lower": "1.0.0", "upper": "2.0.0", "text": "from 1.0.0"},
         "resolved": {"version": "1.4.2", "revision": "abcdef1"}, "used_by": ["Glance"], "products_used": [f"P{i}"]}
    d.update(kw)
    return d


def list_payload(n: int = 2, unresolved: list | None = None) -> dict:
    return {"path": PROJ, "package": "Glance", "packages": [pkg(i) for i in range(n)], "total": n, "truncated": False,
            "targets": [{"name": "Glance", "type": "regular", "app": True},
                        {"name": "GlanceTests", "type": "test", "app": False}],
            "app_target": "Glance", "resolved_file": True, "unresolved": unresolved or [], "manifest_mtime": 1.0,
            "elapsed_ms": 12}


class Mocked(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mcp = FakeMCP()
        T.register(mcp)
        self.tools = mcp.tools
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=AppState()))
        self.calls: list = []

    async def call(self, name, results: dict, **kw):
        async def fake_call(group, action, flags=None, **_):
            self.calls.append((group, action, dict(flags or {})))
            r = results[action]
            if isinstance(r, Exception):
                raise r
            return r

        async def fake_resolve(state, project):
            return {"name": "Glance", "path": PROJ}

        with mock.patch.object(T.helper, "call", fake_call), mock.patch.object(T, "resolve_project", fake_resolve):
            res = await self.tools[name](self.ctx, **kw)
        return res.structured_content, res

    async def test_list(self):
        sc, _ = await self.call("ios_package_list", {"list": list_payload(2, ["pkg-1"])}, project_path="Glance")
        self.assertEqual(self.calls[0][:2], ("packages", "list"))
        self.assertEqual(self.calls[0][2]["project"], PROJ)
        self.assertEqual(sc["count"], 2)
        self.assertEqual(sc["packages"][0]["requirement"]["text"], "from 1.0.0")
        self.assertEqual(sc["packages"][0]["resolved"]["version"], "1.4.2")
        self.assertEqual(sc["targets"][0], {"name": "Glance", "type": "regular", "app": True})
        self.assertIn("ios_package_resolve", sc["next_steps"][0])

    async def test_list_empty_and_capped(self):
        sc, _ = await self.call("ios_package_list", {"list": list_payload(0)}, project_path="Glance")
        self.assertIn("ios_package_add", sc["next_steps"][0])
        big = list_payload(200)
        for p in big["packages"]:
            p["used_by"] = [f"Target{i}" for i in range(20)]
        sc, res = await self.call("ios_package_list", {"list": big}, project_path="Glance", max_items=200)
        self.assertTrue(sc["truncated"])
        self.assertLess(sc["count"], 200)
        self.assertEqual(sc["total"], 200)
        self.assertLessEqual(size_of(res), B.DEFAULT_BYTES)
        sc, _ = await self.call("ios_package_list", {"list": list_payload(30)}, project_path="Glance", max_items=5)
        self.assertEqual((sc["count"], sc["omitted"]), (5, 25))
        self.assertIn("max_items", sc["note"])

    async def test_remove_dry_run_and_confirm(self):
        diff = ("--- a/Package.swift\n+++ b/Package.swift\n@@ -1,3 +1,2 @@\n-        .package(url: \"x\", "
                "from: \"1.0.0\"),\n")
        dry = {"identity": "pkg-0", "would_remove": {"package": "pkg-0", "target_deps": [{"target": "Glance",
                                                                                          "product": "P0"}]},
               "valid": True, "diff": diff, "diff_bytes": len(diff), "truncated": False, "dry_run": True,
               "removed": False}
        sc, res = await self.call("ios_package_remove", {"remove": dry}, project_path="Glance", package="pkg-0")
        self.assertEqual(sc["state"], "dry_run")
        self.assertNotIn("confirm", self.calls[0][2])
        self.assertTrue(sc["valid"])
        self.assertEqual(sc["would_remove"], [{"target": "Glance", "product": "P0"}])
        self.assertIn("confirm=true", sc["next_steps"][0])
        text = "".join(c.text for c in res.content if getattr(c, "text", None))
        self.assertIn('-        .package(url: "x"', text)                    # the diff as plain lines
        done = {**dry, "dry_run": False, "removed": True, "pins_removed": ["pkg-0"]}
        sc, _ = await self.call("ios_package_remove", {"remove": done}, project_path="Glance", package="pkg-0",
                                confirm=True)
        self.assertTrue(self.calls[-1][2]["confirm"])
        self.assertEqual((sc["state"], sc["removed"], sc["pins_removed"]), ("succeeded", True, ["pkg-0"]))
        self.assertIn("ios_build_start", sc["next_steps"][0])

    async def test_remove_refused(self):
        err = H.HelperFailure({"code": "edit-invalid", "message": "Removing x can't be done safely: …",
                               "hint": "Nothing was changed. Edit Package.swift by hand.",
                               "problems": ["The .package entry on line 12 isn't a plain array entry."],
                               "diff": "--- a/Package.swift\n", "diff_bytes": 20, "valid": False})
        sc, _ = await self.call("ios_package_remove", {"remove": err}, project_path="Glance", package="x")
        self.assertEqual((sc["state"], sc["error"]["code"], sc["valid"]), ("failed", "edit-invalid", False))
        self.assertIn("line 12", sc["problems"][0])
        self.assertIn("by hand", sc["next_steps"][0])
        self.assertEqual(sc["diff"], "--- a/Package.swift\n")

    async def test_add_product_for_declared_package(self):
        linked = {"package": pkg(0), "identity": "pkg-0", "target": "Glance", "products": ["P0", "Q0"],
                  "product_added": "Q0", "products_added": ["Q0"], "needs_product": False, "already_linked": [],
                  "diff": "--- a/Package.swift\n", "diff_bytes": 20, "truncated": False}
        sc, _ = await self.call("ios_package_add", {"list": list_payload(1), "add-product": linked},
                                project_path="Glance", url="https://github.com/acme/pkg-0.git", product="Q0")
        self.assertEqual([c[1] for c in self.calls], ["list", "add-product"])
        self.assertEqual(json.loads(self.calls[1][2]["products"]), ["Q0"])
        self.assertEqual(self.calls[1][2]["package"], "pkg-0")
        self.assertEqual((sc["state"], sc["action"], sc["products_added"]), ("succeeded", "add-product", ["Q0"]))
        self.assertIn("ios_build_start", sc["next_steps"][0])

    async def test_add_needs_url(self):
        with self.assertRaises(ToolError):
            await self.tools["ios_package_add"](self.ctx, project_path="Glance")


class Jobs(unittest.IsolatedAsyncioTestCase):
    """The real helper with the fake swift, through the job manager."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.proj = make_project(self.dir / "App")
        env = {"IOSSTUDIO_SWIFT": str(FAKE_SWIFT), "XDG_CACHE_HOME": str(self.dir / "cache"),
               "XDG_STATE_HOME": str(self.dir / "state"),
               "FAKE_SWIFT_PACKAGES": json.dumps({
                   "swift-algorithms": {"products": ["Algorithms"], "version": "1.2.0", "update": "1.2.1"},
                   "swift-collections": {"products": ["Collections", "DequeModule"], "version": "1.1.4"},
                   "missing": {"fail": "clone"}})}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("IOSSTUDIO_DEMO", None)
        mcp = FakeMCP()
        T.register(mcp)
        self.tools = mcp.tools
        self.state = AppState()
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=self.state))
        proj = {"name": "App", "path": str(self.proj)}

        async def fake_resolve(state, project):
            return proj
        self.resolve = mock.patch.object(T, "resolve_project", fake_resolve)
        self.resolve.start()

    async def asyncTearDown(self):
        await self.state.jobs.shutdown()
        self.resolve.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def until_done(self, tool: str, res) -> dict:
        sc = res.structured_content
        for _ in range(40):
            if sc["state"] != "running":
                return sc
            self.assertIn(f"{tool}(job_id='{sc['job_id']}'", sc["next_steps"][0])
            sc = (await self.tools[tool](self.ctx, job_id=sc["job_id"], wait_seconds=5)).structured_content
        self.fail("job didn't finish")

    async def test_add_choose_product_update(self):
        res = await self.tools["ios_package_add"](self.ctx, project_path="App",
                                                  url="https://github.com/apple/swift-collections", version="1.1.0",
                                                  wait_seconds=0)
        self.assertEqual(res.structured_content["state"], "running")
        self.assertTrue(res.structured_content["job_id"].startswith("pkg-"))
        sc = await self.until_done("ios_package_add", res)
        self.assertEqual(sc["state"], "succeeded", sc)
        self.assertTrue(sc["needs_product"])
        self.assertEqual(sc["products"], ["Collections", "DequeModule"])
        self.assertIn("product='<name>'", sc["next_steps"][0])
        self.assertIn("https://github.com/apple/swift-collections", sc["next_steps"][0])
        self.assertEqual({s["id"]: s["state"] for s in sc["steps"]}["link"], "skipped")
        res = await self.tools["ios_package_add"](self.ctx, project_path="App",
                                                  url="https://github.com/apple/swift-collections", product="DequeModule")
        sc = res.structured_content
        self.assertEqual((sc["state"], sc["action"], sc["products_added"]), ("succeeded", "add-product", ["DequeModule"]))
        self.assertIn('.product(name: "DequeModule", package: "swift-collections")', sc["diff"])

        res = await self.tools["ios_package_add"](self.ctx, project_path="App",
                                                  url="https://github.com/apple/swift-algorithms", version="1.2.0")
        sc = await self.until_done("ios_package_add", res)
        self.assertEqual(sc["products_added"], ["Algorithms"])
        self.assertEqual(sc["package"]["resolved"]["version"], "1.2.0")

        res = await self.tools["ios_package_update"](self.ctx, project_path="App")
        sc = await self.until_done("ios_package_update", res)
        self.assertEqual(sc["changes"], [{"identity": "swift-algorithms", "from_version": "1.2.0",
                                          "to_version": "1.2.1"}])
        self.assertIn("swift-algorithms 1.2.0 → 1.2.1", sc["message"])
        res = await self.tools["ios_package_resolve"](self.ctx, project_path="App")
        sc = await self.until_done("ios_package_resolve", res)
        self.assertEqual(sc.get("changes", []), [])

        lst = (await self.tools["ios_package_list"](self.ctx, project_path="App")).structured_content
        self.assertEqual({p["identity"] for p in lst["packages"]}, {"swift-collections", "swift-algorithms"})

    async def test_failed_add_has_diagnostics(self):
        before = (self.proj / "Package.swift").read_text()
        res = await self.tools["ios_package_add"](self.ctx, project_path="App", url="https://github.com/acme/missing",
                                                  version="1.0.0")
        sc = await self.until_done("ios_package_add", res)
        self.assertEqual((sc["state"], sc["error"]["code"]), ("failed", "fetch-failed"))
        self.assertEqual(sc["diagnostics"][0]["category"], "package")
        self.assertIn("ios_package_add", sc["diagnostics"][0]["hint"])       # agent wording, not the app's
        self.assertEqual((self.proj / "Package.swift").read_text(), before)

    async def test_cancel_restores(self):
        await self.until_done("ios_package_add", await self.tools["ios_package_add"](
            self.ctx, project_path="App", url="https://github.com/apple/swift-algorithms", version="1.2.0"))
        resolved = (self.proj / "Package.resolved").read_text()
        with mock.patch.dict(os.environ, {"FAKE_SWIFT_SLEEP": "30"}):
            res = await self.tools["ios_package_update"](self.ctx, project_path="App", wait_seconds=0)
            job = self.state.jobs.get(res.structured_content["job_id"])
            for _ in range(100):
                if "partial-pin" in (self.proj / "Package.resolved").read_text():
                    break
                await asyncio.sleep(0.1)
            with self.assertRaises(ToolError):        # one package job per project
                await self.tools["ios_package_resolve"](self.ctx, project_path="App")
            await self.state.jobs.cancel(job)
        sc = (await self.tools["ios_package_update"](self.ctx, job_id=job.id, wait_seconds=0)).structured_content
        self.assertEqual(sc["state"], "cancelled")
        self.assertIn("as they were", sc["next_steps"][0])
        self.assertEqual((self.proj / "Package.resolved").read_text(), resolved)


class Demo(unittest.IsolatedAsyncioTestCase):
    async def test_demo(self):
        mcp = FakeMCP()
        T.register(mcp)
        state = AppState()
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
        proj = str(Path.home() / "Developer/HabitTracker")

        async def fake_resolve(state, project):
            return {"name": "HabitTracker", "path": proj}

        with mock.patch.dict(os.environ, {"IOSSTUDIO_DEMO": "1"}), \
                mock.patch.object(T, "resolve_project", fake_resolve):
            lst = (await mcp.tools["ios_package_list"](ctx, project_path=proj)).structured_content
            res = await mcp.tools["ios_package_update"](ctx, project_path=proj, wait_seconds=20)
            sc = res.structured_content
            while sc["state"] == "running":
                sc = (await mcp.tools["ios_package_update"](ctx, job_id=sc["job_id"])).structured_content
            rm = (await mcp.tools["ios_package_remove"](ctx, project_path=proj, package="kingfisher")
                  ).structured_content
            await state.jobs.shutdown()
        self.assertEqual(len(lst["packages"]), 3)
        self.assertEqual(lst["unresolved"], ["confetti-swiftui"])
        self.assertEqual(len(sc["changes"]), 2)
        self.assertEqual(rm["state"], "dry_run")
        self.assertIn("Kingfisher", rm["diff"])


class Registration(unittest.TestCase):
    def test_registered(self):
        from iosstudio_mcp.tools import MODULES
        self.assertIn("packages", MODULES)
        from iosstudio_mcp import server
        self.assertIn("ios_package_add", server.INSTRUCTIONS)
        mcp = FakeMCP()
        T.register(mcp)
        self.assertEqual(set(mcp.tools), {"ios_package_list", "ios_package_add", "ios_package_update",
                                          "ios_package_resolve", "ios_package_remove"})
        for code in ("already-added", "fetch-failed", "resolve-failed", "no-versions"):
            self.assertIn("ios_package_add", H.NEXT_BY_CODE[code])
        for code in ("edit-invalid", "package-busy", "unknown-product", "manifest-invalid"):
            self.assertIn(code, H.NEXT_BY_CODE)


if __name__ == "__main__":
    unittest.main()
