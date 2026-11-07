"""Files on the iPhone (mcp/iosstudio_mcp/tools/files.py): camera roll, app containers and the camera test.

Every tool runs through the real @tool wrapper against a fake device daemon that speaks the real protocol and
answers with the shapes of helper/iosstudio/device/features/files/real.py and features/cameratest/real.py
(docs/deviced-protocol.md, "Files"): replies, `transfer` / `cameratest` events and error codes.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

from iosstudio_mcp.core import budget as B
from iosstudio_mcp.tools import files as FILES
from iosstudio_mcp.tools import jobs as JOBS
from support.mcp_harness import ToolCase, err, registered, size_of

OWN = "XTL-ABCDE12345.com.example.app"
APPS = [{"bundleId": OWN, "name": "App", "developer": True, "running": False},
        {"bundleId": "com.store.reader", "name": "Reader", "developer": False}]


def media_item(n: int) -> dict:
    p = f"DCIM/100APPLE/IMG_{n:04d}.HEIC"
    return {"id": p, "path": p, "name": p.rsplit("/", 1)[1], "kind": "photo", "date": "2026-10-30T18:12:05",
            "ts": 1793383925.0, "width": 4032, "height": 3024, "duration": None, "size": 2310044,
            "thumb": f"/home/u/.cache/ios-studio/thumbs/U/{n}.jpg"}


def listing(path: str, entries: list[dict]) -> dict:
    return {"bundleId": OWN, "name": "App", "path": path, "root": "/", "mode": "container", "developer": True,
            "fileSharing": True, "writable": True, "entries": entries}


class FilesCase(ToolCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.daemon.replies["apps.list"] = (0, {"apps": APPS, "stale": False})
        p = mock.patch.dict(os.environ, {"IOSSTUDIO_MCP_ALLOW_PATHS": str(self.tmp)})
        p.start()
        self.addCleanup(p.stop)

    async def status(self, job_id: str) -> dict:
        return await self.data(JOBS, "ios_job_status", job_id=job_id, wait_seconds=0)

    def assert_fits(self, res, limit: int = B.DEFAULT_BYTES):
        self.assertLessEqual(size_of(res), limit)


class Annotations(unittest.TestCase):
    def test_kinds(self):
        ann = registered(FILES).annotations
        reads = {"ios_media_list", "ios_files_list"}
        destructive = {"ios_files_push", "ios_files_delete"}
        self.assertEqual(len(ann), 7)
        for name, a in ann.items():
            self.assertEqual(a.read_only_hint, name in reads, name)
            self.assertEqual(a.destructive_hint, name in destructive, name)
            self.assertTrue(a.open_world_hint, name)          # every one talks to the iPhone


class MediaList(FilesCase):
    async def test_a_page_of_the_camera_roll(self):
        self.daemon.replies["media.list"] = lambda a: (0, {
            "total": 30, "offset": a["offset"], "source": "photos-db", "photos": 28, "videos": 2,
            "items": [media_item(i) for i in range(a["offset"], min(30, a["offset"] + a["limit"]))]})
        res = await self.call(FILES, "ios_media_list", kind="photo", limit=10, refresh=True)
        self.assert_fits(res)
        d = res.structured_content
        self.assertEqual((d["total"], d["count"], d["next_offset"], d["has_more"]), (30, 10, 10, True))
        self.assertEqual(d["items"][0]["id"], "DCIM/100APPLE/IMG_0000.HEIC")
        self.assertEqual(d["source"], "photos-db")
        self.assertIn("offset=10", d["note"])
        self.assertEqual(self.daemon.sent("media.list")[-1],
                         {"offset": 0, "limit": 10, "refresh": True, "kind": "photo"})
        last = await self.data(FILES, "ios_media_list", offset=20, limit=10)
        self.assertNotIn("next_offset", last)
        self.assertEqual(last["items"][-1]["id"], "DCIM/100APPLE/IMG_0029.HEIC")

    async def test_a_big_page_is_cut_to_the_budget_with_a_resume_offset(self):
        self.daemon.replies["media.list"] = (0, {"total": 1400, "source": "photos-db", "photos": 1400, "videos": 0,
                                                 "items": [media_item(i) for i in range(200)]})
        res = await self.call(FILES, "ios_media_list", limit=200)
        self.assert_fits(res)
        d = res.structured_content
        self.assertTrue(d["has_more"])
        self.assertLess(d["count"], 200)
        self.assertEqual(d["next_offset"], d["count"])

    async def test_an_empty_roll_says_so(self):
        self.daemon.replies["media.list"] = (0, {"total": 0, "source": "dcim", "photos": 0, "videos": 0, "items": []})
        d = await self.data(FILES, "ios_media_list")
        self.assertIn("empty", d["next_steps"][0])

    async def test_a_locked_phone_is_one_line_with_a_next_step(self):
        self.daemon.replies["media.list"] = (0, err("locked", "The iPhone is locked."))
        text = await self.error(FILES, "ios_media_list")
        self.assertIn("[locked]", text)
        self.assertIn("Next: Ask the user to unlock", text)


class MediaPull(FilesCase):
    async def test_a_slow_import_is_a_job_with_progress_then_its_files(self):
        dest = self.tmp / "Pictures"
        gate = asyncio.Event()                         # the daemon answers once the test has seen progress
        self.daemon.replies["media.pull"] = lambda a: (gate, {
            "transferId": a["transferId"], "dest": a["dest"], "files": [f"{a['dest']}/IMG_0001.HEIC"],
            "skipped": [], "failed": [], "bytes": 2310044, "converted": 0, "notConverted": 0, "seconds": 0.6})
        first = await self.data(FILES, "ios_media_pull", ids=["DCIM/100APPLE/IMG_0001.HEIC"], dest=str(dest),
                                wait_seconds=0)
        self.assertEqual(first["state"], "running")
        jid = first["job_id"]
        self.assertIn(f"ios_media_pull(job_id='{jid}'", first["next_steps"][0])
        sent = (await self.until(lambda: self.daemon.sent("media.pull"), what="media.pull"))[0]
        self.assertEqual((sent["transferId"], sent["ids"], sent["dest"], sent["convertHeic"]),
                         (jid, ["DCIM/100APPLE/IMG_0001.HEIC"], str(dest), False))
        self.daemon.emit("transfer", {"id": jid, "kind": "media.pull", "state": "running", "bytes": 1000,
                                      "total": 4000, "files": 0, "count": 1, "file": "IMG_0001.HEIC", "seconds": 0.1,
                                      "dest": str(dest)})

        async def progressed():
            d = await self.data(FILES, "ios_media_pull", job_id=jid, wait_seconds=0)
            return d if d.get("progress") else None
        mid = await self.until(progressed, what="transfer progress")
        self.assertEqual((mid["progress"], mid["current_file"], mid["total_bytes"]), (0.25, "IMG_0001.HEIC", 4000))
        st = await self.status(jid)
        self.assertEqual((st["job_id"], st["kind"], st["state"]), (jid, "transfer", "running"))
        gate.set()
        done = await self.poll(FILES, "ios_media_pull", jid, wait_seconds=3)
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["files"], [f"{dest}/IMG_0001.HEIC"])
        self.assertEqual(done["progress"], 1.0)
        self.assertEqual((await self.status(jid))["state"], "succeeded")

    async def test_latest_n_lists_then_pulls_those_ids(self):
        self.daemon.replies["media.list"] = (0, {"total": 3, "items": [media_item(i) for i in range(3)]})
        self.daemon.replies["media.pull"] = (0, {"dest": str(self.tmp), "files": [], "skipped": ["a", "b", "c"],
                                                 "failed": [], "bytes": 0})
        d = await self.data(FILES, "ios_media_pull", latest=3, kind="video", dest=str(self.tmp), wait_seconds=3)
        self.assertEqual(d["state"], "succeeded")
        self.assertEqual(d["skipped"], 3)
        self.assertEqual(self.daemon.sent("media.list")[0], {"offset": 0, "limit": 3, "kind": "video"})
        self.assertEqual(len(self.daemon.sent("media.pull")[0]["ids"]), 3)

    async def test_bad_arguments_never_reach_the_phone(self):
        self.assertIn("ios_media_list", await self.error(FILES, "ios_media_pull", ids=["Documents/x.txt"],
                                                         dest=str(self.tmp)))
        self.assertIn("latest=N", await self.error(FILES, "ios_media_pull", dest=str(self.tmp)))
        self.assertIn("home folder", await self.error(FILES, "ios_media_pull", ids=["DCIM/1/a.jpg"], dest="/etc/x"))
        self.assertEqual(self.daemon.count("media.pull"), 0)

    async def test_a_failed_pull_is_a_failed_status_with_the_daemon_error(self):
        self.daemon.replies["media.pull"] = (0, err("no-space", "This computer's disk is full."))
        d = await self.data(FILES, "ios_media_pull", ids=["DCIM/1/a.jpg"], dest=str(self.tmp), wait_seconds=3)
        self.assertEqual(d["state"], "failed")
        self.assertIn("[no-space]", d["error"])
        self.assertTrue(d["next_steps"])

    async def test_a_job_is_polled_with_the_tool_that_started_it(self):
        self.daemon.replies["media.pull"] = (asyncio.Event(), {"files": []})     # never answers: stays running
        jid = (await self.data(FILES, "ios_media_pull", ids=["DCIM/1/a.jpg"], dest=str(self.tmp),
                               wait_seconds=0))["job_id"]
        text = await self.error(FILES, "ios_files_pull", job_id=jid)
        self.assertIn(f"ios_media_pull(job_id='{jid}')", text)


class FilesList(FilesCase):
    def tree(self, a):
        path = a.get("path") or "/"
        tree = {
            "/": [{"path": "/Documents", "type": "dir", "size": 0, "mtime": 1793383925},
                  {"path": "/Library", "type": "dir", "size": 0},
                  {"path": "/tmp", "type": "dir", "size": 0}],
            "/Documents": [{"path": "/Documents/notes.json", "type": "file", "size": 120, "mtime": 1793383925},
                           {"path": "/Documents/Inbox", "type": "dir", "size": 0}],
            "/Library": [{"path": "/Library/Preferences", "type": "dir", "size": 0}],
            "/tmp": [],
        }
        return (0, listing(path, tree.get(path, [])))

    async def test_the_container_two_levels_deep(self):
        self.daemon.replies["container.list"] = self.tree
        res = await self.call(FILES, "ios_files_list", bundle_id="com.example.app", depth=2)
        self.assert_fits(res)
        d = res.structured_content
        self.assertEqual(d["bundle_id"], OWN)                 # the plain id finds the XTL- form
        self.assertEqual((d["mode"], d["path"], d["total"], d["dirs"], d["files"], d["bytes"]),
                         ("container", "/", 6, 5, 1, 120))
        self.assertEqual([e["path"] for e in d["entries"]][:3], ["/Documents", "/Documents/Inbox",
                                                                 "/Documents/notes.json"])
        self.assertEqual(d["entries"][0]["modified"][:4], "2026")
        self.assertNotIn("/Documents/Inbox", [a.get("path") for a in self.daemon.sent("container.list")])  # depth 2
        self.assertTrue(all(a["bundleId"] == OWN for a in self.daemon.sent("container.list")))

    async def test_many_entries_are_paged(self):
        many = [{"path": f"/Documents/f{i:04d}.txt", "type": "file", "size": 10} for i in range(400)]
        self.daemon.replies["container.list"] = (0, listing("/Documents", many))
        res = await self.call(FILES, "ios_files_list", bundle_id=OWN, path="/Documents", limit=500)
        self.assert_fits(res)
        d = res.structured_content
        self.assertTrue(d["has_more"])
        self.assertEqual(d["next_offset"], d["count"])
        self.assertEqual(d["total"], 400)

    async def test_an_app_that_is_not_installed_names_the_install_tools(self):
        text = await self.error(FILES, "ios_files_list", bundle_id="com.nope")
        self.assertIn("ios_run", text)
        self.assertEqual(self.daemon.count("container.list"), 0)

    async def test_a_phone_error_is_one_line_with_a_next_step(self):
        self.daemon.replies["container.list"] = (0, err("unreachable", "The iPhone isn't reachable."))
        text = await self.error(FILES, "ios_files_list", bundle_id=OWN)
        self.assertIn("[unreachable]", text)
        self.assertIn("ios_device_reconnect", text)

    async def test_an_app_without_file_sharing_names_a_next_step(self):
        self.daemon.replies["container.list"] = (0, err(
            "no-file-sharing", "This app doesn't share files. Your own apps (built here) always do; App Store apps "
            "only when they turn on file sharing.", detail="InstallationLookupFailed"))
        text = await self.error(FILES, "ios_files_list", bundle_id="com.store.reader")
        self.assertIn("[no-file-sharing]", text)
        self.assertIn("Next:", text)
        self.assertIn("ios_app_list(scope='own')", text)


class FilesPull(FilesCase):
    async def test_a_fast_copy_answers_in_one_call(self):
        self.daemon.replies["container.pull"] = lambda a: (0, {
            "transferId": a["transferId"], "dest": a["dest"], "files": [f"{a['dest']}/notes.json"], "bytes": 120,
            "seconds": 0.1})
        d = await self.data(FILES, "ios_files_pull", bundle_id=OWN, paths=["/Documents/notes.json"],
                            dest=str(self.tmp), wait_seconds=3)
        self.assertEqual((d["state"], d["kind"], d["bytes"]), ("succeeded", "container.pull", 120))
        self.assertEqual(d["files"], [f"{self.tmp}/notes.json"])
        self.assertEqual(self.daemon.sent("container.pull")[0]["paths"], ["/Documents/notes.json"])

    async def test_cancel_stops_the_transfer_on_the_phone(self):
        # Like the real daemon: the copy runs until transfer.cancel, then its request fails with `cancelled`.
        stop = asyncio.Event()
        answer = self.daemon._answer

        async def cancellable(w, msg):
            if msg["cmd"] == "container.pull":
                self.daemon.requests.append((msg["cmd"], msg.get("args") or {}))
                await asyncio.wait_for(stop.wait(), 5)
                self.daemon._write(w, {"id": msg["id"], "ok": False,
                                       "error": {"code": "cancelled", "message": "The copy was cancelled."}})
                return
            await answer(w, msg)
        self.daemon._answer = cancellable
        self.daemon.replies["transfer.cancel"] = lambda a: (stop.set(), (0, {"transferId": a["transferId"],
                                                                             "cancelled": True}))[1]
        jid =(await self.data(FILES, "ios_files_pull", bundle_id=OWN, paths=["/Documents"], dest=str(self.tmp),
                               wait_seconds=0))["job_id"]
        st = await self.data(JOBS, "ios_job_cancel", job_id=jid)
        self.assertEqual(st["state"], "cancelled")
        self.assertEqual(self.daemon.sent("transfer.cancel"), [{"transferId": jid}])
        self.assertEqual((await self.data(FILES, "ios_files_pull", job_id=jid, wait_seconds=0))["state"], "cancelled")

    async def test_missing_arguments(self):
        self.assertIn("ios_files_list", await self.error(FILES, "ios_files_pull", bundle_id=OWN))

    async def test_many_files_are_listed_up_to_a_cap(self):
        self.daemon.replies["container.pull"] = (0, {"dest": str(self.tmp), "bytes": 1,
                                                     "files": [f"{self.tmp}/f{i}.txt" for i in range(500)]})
        res = await self.call(FILES, "ios_files_pull", bundle_id=OWN, paths=["/Documents"], dest=str(self.tmp),
                              wait_seconds=3)
        self.assert_fits(res)
        d = res.structured_content
        self.assertEqual((len(d["files"]), d["files_total"], d["truncated"]), (FILES.FILES_SHOWN, 500, True))


class FilesPush(FilesCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.src = self.tmp / "in.txt"
        self.src.write_text("hi")

    async def test_an_existing_file_needs_confirm_and_nothing_is_kept(self):
        self.daemon.replies["container.push"] = lambda a: (0, {"transferId": a["transferId"], "path": a["path"],
                                                               "files": ["/Documents/in.txt"], "bytes": 2}) \
            if a["overwrite"] else (0, err("exists", "1 of these already exist on the iPhone. Replace them?",
                                           detail="/Documents/in.txt"))
        d = await self.data(FILES, "ios_files_push", bundle_id=OWN, sources=[str(self.src)], wait_seconds=3)
        self.assertEqual(d["state"], "needs_confirm")
        self.assertEqual(d["would"], ["Replace /Documents/in.txt"])
        self.assertIn("confirm=true", d["next_steps"][0])
        self.assertNotIn(d["job_id"], self.state.jobs.jobs)
        done = await self.data(FILES, "ios_files_push", bundle_id=OWN, sources=[str(self.src)], confirm=True,
                               wait_seconds=3)
        self.assertEqual(done["state"], "succeeded")
        last = self.daemon.sent("container.push")[-1]
        self.assertEqual((last["srcs"], last["path"], last["overwrite"]), ([str(self.src)], "/Documents", True))

    async def test_sources_must_exist_inside_the_allowed_folders(self):
        self.assertIn("Nothing at", await self.error(FILES, "ios_files_push", bundle_id=OWN,
                                                     sources=[str(self.tmp / "missing")]))
        self.assertIn("home folder", await self.error(FILES, "ios_files_push", bundle_id=OWN,
                                                      sources=["/etc/hostname"]))
        self.assertIn("sources", await self.error(FILES, "ios_files_push", bundle_id=OWN))
        self.assertEqual(self.daemon.count("container.push"), 0)


class FilesDelete(FilesCase):
    async def test_a_dry_run_lists_what_would_go_and_deletes_nothing(self):
        self.daemon.replies["container.list"] = (0, listing("/Documents", [
            {"path": "/Documents/notes.json", "type": "file", "size": 120},
            {"path": "/Documents/Inbox", "type": "dir", "size": 0}]))
        res = await self.call(FILES, "ios_files_delete", bundle_id=OWN,
                              paths=["/Documents/notes.json", "/Documents/Inbox/", "/Documents/gone.txt"])
        self.assert_fits(res)
        d = res.structured_content
        self.assertFalse(d.get("done"))
        self.assertEqual(d["would"], ["Delete /Documents/notes.json (120 bytes)",
                                      "Delete folder /Documents/Inbox/ and everything in it",
                                      "/Documents/gone.txt (not found; nothing to delete)"])
        self.assertIn("confirm=true", d["next_steps"][0])
        self.assertEqual(self.daemon.count("container.delete"), 0)
        self.assertEqual(self.daemon.count("container.list"), 1)       # one listing per parent folder

    async def test_confirm_deletes_and_reports_partial_failures(self):
        self.daemon.replies["container.delete"] = (0, {"deleted": ["/Documents/a"],
                                                       "failed": [{"path": "/Documents/b", "message": "denied"}]})
        d = await self.data(FILES, "ios_files_delete", bundle_id=OWN, paths=["/Documents/a", "/Documents/b"],
                            confirm=True)
        self.assertFalse(d.get("done"))
        self.assertIn("1 couldn't be deleted", d["message"])
        self.assertEqual(self.daemon.sent("container.delete")[0], {"bundleId": OWN,
                                                                   "paths": ["/Documents/a", "/Documents/b"]})

    async def test_a_phone_error_on_delete_is_one_line_with_a_next_step(self):
        self.daemon.replies["container.delete"] = (0, err("locked", "The iPhone is locked."))
        text = await self.error(FILES, "ios_files_delete", bundle_id=OWN, paths=["/Documents/a"], confirm=True)
        self.assertIn("[locked]", text)
        self.assertIn("Next:", text)


CAMERA_RESULT = {
    "testId": "?", "bundleId": OWN, "ok": True, "timedOut": False, "exited": False,
    "done": {"results": [{"file": f"cat{i}.jpg", "frame": i, "label": "cat" if i % 3 else "dog", "score": i / 10}
                         for i in range(40)]},
    "doneRaw": None, "outputDir": "/home/u/.cache/ios-studio/cameratest/ct-1/pulled/CameraTestOutput",
    "outputs": ["/home/u/.cache/ios-studio/cameratest/ct-1/pulled/CameraTestOutput/done.json"],
    "logLines": [f"14:22:{i % 60:02d} info    App: CameraTest frame {i} " + "x" * 250 for i in range(399)]
    + ["14:23:00 error   App: CameraTest failed to decode cat3.jpg"],
    "logCount": 400, "logFile": "/home/u/.cache/ios-studio/cameratest/ct-1/syslog.txt", "newCrashes": [],
    "pid": 812, "seconds": 6.4, "args": ["-cameraTestMode", "1"], "env": {}, "timeout": 60, "steps": []}


class CameraTest(FilesCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.img = self.tmp / "cat.jpg"
        self.img.write_bytes(b"\xff\xd8\xff")

    async def test_a_run_is_a_job_with_steps_then_a_bounded_result(self):
        gate = asyncio.Event()
        self.daemon.replies["cameratest.run"] = lambda a: (gate, {**CAMERA_RESULT, "testId": a["testId"]})
        first = await self.data(FILES, "ios_camera_test", bundle_id="com.example.app", inputs=[str(self.img)],
                                timeout_s=30, wait_seconds=0)
        self.assertEqual(first["state"], "running")
        jid = first["job_id"]
        sent = (await self.until(lambda: self.daemon.sent("cameratest.run"), what="cameratest.run"))[0]
        self.assertEqual((sent["testId"], sent["bundleId"], sent["inputs"], sent["timeout"]),
                         (jid, OWN, [str(self.img)], 30))
        self.assertNotIn("args", sent)                 # the daemon's default -cameraTestMode 1
        self.daemon.emit("cameratest", {"testId": jid, "step": "push", "state": "running", "title": "Copy inputs",
                                        "detail": ""})

        async def stepped():
            d = await self.data(FILES, "ios_camera_test", job_id=jid, wait_seconds=0)
            return d if d.get("steps") else None
        mid = await self.until(stepped, what="cameratest step")
        self.assertEqual((mid["step"], mid["steps"][0]["id"]), ("push", "push"))
        self.assertEqual((await self.status(jid))["kind"], "cameratest")
        gate.set()
        res = None
        for _ in range(10):
            res = await self.call(FILES, "ios_camera_test", job_id=jid, wait_seconds=2)
            if res.structured_content["state"] != "running":
                break
        self.assert_fits(res)
        d = res.structured_content
        self.assertEqual((d["state"], d["ok"], d["results_count"]), ("succeeded", True, 40))
        self.assertEqual(d["results_summary"]["label"]["top"], {"cat": 26, "dog": 14})
        self.assertEqual(d["results_summary"]["score"]["max"], 3.9)
        self.assertTrue(d["done_json_path"].endswith("CameraTestOutput/done.json"))
        self.assertIn("error", d["log_lines"][0])      # faults and errors first
        self.assertLessEqual(len(d["log_lines"]), FILES.LOG_SHOWN)

    async def test_a_new_crash_is_read_and_summarized(self):
        crash = "/var/mobile/Library/Logs/CrashReporter/App-2026-10-30-182200.ips"
        self.daemon.replies["cameratest.run"] = (0, {**CAMERA_RESULT, "ok": False, "done": None, "exited": True,
                                                     "logLines": [], "logCount": 0,
                                                     "newCrashes": [{"name": crash, "file": crash.rsplit("/", 1)[1]}]})
        self.daemon.replies["crash.pull"] = (0, {"path": "", "summary": {
            "process": "App", "exceptionType": "EXC_BAD_ACCESS", "terminationReason": "SIGSEGV"}})
        d = await self.data(FILES, "ios_camera_test", bundle_id=OWN, inputs=[str(self.img)], wait_seconds=3)
        self.assertEqual(d["state"], "succeeded")
        self.assertFalse(d["ok"])
        self.assertIn("stopped before writing done.json", d["summary"])
        self.assertIn("CRASHED: EXC_BAD_ACCESS", d["summary"])
        self.assertEqual(d["new_crash"]["exception_type"], "EXC_BAD_ACCESS")
        self.assertTrue(any("ios_crash_get" in s for s in d["next_steps"]))

    async def test_not_a_dev_app_fails_the_job_with_a_next_step(self):
        self.daemon.replies["cameratest.run"] = (0, err("not-dev-app", "Camera tests need one of your own apps."))
        d = await self.data(FILES, "ios_camera_test", bundle_id=OWN, inputs=[str(self.img)], wait_seconds=3)
        self.assertEqual(d["state"], "failed")
        self.assertIn("[not-dev-app]", d["error"])
        self.assertIn("ios_app_list(scope='own')", d["next_steps"][0])

    async def test_bad_arguments_never_reach_the_phone(self):
        self.assertIn("isn't one of your development builds",
                      await self.error(FILES, "ios_camera_test", bundle_id="com.store.reader", inputs=[str(self.img)]))
        self.assertIn("inputs", await self.error(FILES, "ios_camera_test", bundle_id=OWN))
        self.assertIn("must be files", await self.error(FILES, "ios_camera_test", bundle_id=OWN,
                                                        inputs=[str(self.tmp)]))
        self.assertEqual(self.daemon.count("cameratest.run"), 0)

    async def test_a_wrong_job_kind_is_refused(self):
        self.daemon.replies["container.pull"] = (asyncio.Event(), {})           # never answers: stays running
        jid = (await self.data(FILES, "ios_files_pull", bundle_id=OWN, paths=["/Documents"], dest=str(self.tmp),
                               wait_seconds=0))["job_id"]
        text = await self.error(FILES, "ios_camera_test", job_id=jid)
        self.assertIn(jid, text)
