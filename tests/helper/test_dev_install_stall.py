"""app.install that the phone stops answering, and requests whose client goes away (a real-iPhone run over
Wi-Fi: the .ipa uploaded, then the installer stayed at 0% until the client's 600 s timeout; the daemon then
logged "coroutine ignored GeneratorExit" in app.install and "Task was destroyed but it is pending!").

apps/install.py: the upload and every installer message are bounded, so a silent installer fails with
`install-stalled` (plain message + a Reconnect fix) after INSTALL_STALL_S; cancelling an install closes the AFC and
installation_proxy connections and removes the staged package; closing the coroutine (what garbage collection does
to a destroyed task) raises no RuntimeError; the next install after either works.

broker.py: a request whose client disconnects is cancelled (its cleanup runs) instead of being left unreferenced
and garbage-collected mid-flight; requests still running at shutdown are cancelled the same way.

    .venv/bin/pytest -q tests/helper/test_dev_install_stall.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_install_stall
"""
from __future__ import annotations

import asyncio
import gc
import inspect
import io
import plistlib
import shutil
import tempfile
import types
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from support import devtest
from support.pmd3_stub import have_pmd3, stub_pmd3

devtest.isolate()

from iosstudio.core.output import HelperError  # noqa: E402
from iosstudio.device import broker as B  # noqa: E402
from iosstudio.device import protocol  # noqa: E402
from iosstudio.device.protocol import DeviceError  # noqa: E402

UDID = "00008150-INSTALLSTALL"
BID = "dev.iosstudio.crashlab"
INSTALLED = f"XTL-TEAM1.{BID}"
WAIT = 5.0

DONE = [{"Status": "CreatingStagingDirectory", "PercentComplete": 5},
        {"Status": "VerifyingApplication", "PercentComplete": 40},
        {"Status": "InstallingApplication", "PercentComplete": 80},
        {"Status": "Complete"}]


def make_ipa(folder: Path, size: int = 3 * 1024 * 1024 + 5) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Payload/CrashLab.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": BID}))
        z.writestr("Payload/CrashLab.app/blob", b"x" * size)
    p = folder / "CrashLab.ipa"
    p.write_bytes(buf.getvalue())
    return p


class Phone:
    """The phone's AFC, installation_proxy and app services. `script` is what the installer answers to the next
    Install, one message per recv (a number: that many seconds of silence first); past its end it never answers
    again (as on the iPhone while the app being replaced stays in the foreground). `running` holds the pids of
    the installed app; `log` records the order of uploads, kills and installer requests."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.afcs: list = []
        self.ips: list = []
        self.script: list[dict] = list(DONE)
        self.requests: list[dict] = []
        self.waiting = asyncio.Event()       # the installer is being waited on and won't answer
        self.running: list[int] = []
        self.kill_fails = False
        self.log: list[str] = []
        phone = self

        class AfcService(devtest.Svc):
            def __init__(self, rsd=None, *a, **k):
                super().__init__(rsd, *a, **k)
                self.closed = False
                self.handles: dict[int, str] = {}
                phone.afcs.append(self)

            async def __aexit__(self, *exc):
                self.closed = True
                return False

            async def makedirs(self, p):
                pass

            async def fopen(self, p, mode="r"):
                self.handles[len(self.handles) + 1] = p
                phone.files[p] = b""
                return len(self.handles)

            async def fwrite(self, h, data):
                await asyncio.sleep(0)
                phone.files[self.handles[h]] += data

            async def fclose(self, h):
                self.handles.pop(h, None)
                phone.log.append("uploaded")

            async def set_file_contents(self, p, data, progress=None):   # what install_from_local used
                phone.files[p] = data

            async def rm_single(self, p, force=False):
                phone.files.pop(p, None)
                return True

        class Conn:
            async def send_plist(self, d):
                phone.requests.append(d)
                phone.log.append(d.get("Command", "?"))

            async def recv_plist(self):
                while phone.script and isinstance(phone.script[0], (int, float)):
                    await asyncio.sleep(phone.script.pop(0))
                if phone.script:
                    await asyncio.sleep(0)
                    return phone.script.pop(0)
                phone.waiting.set()
                # an answer that never comes: a future only this frame references (as a dead connection)
                return await asyncio.get_running_loop().create_future()

        class InstallationProxyService(devtest.Svc):
            def __init__(self, rsd=None, *a, **k):
                super().__init__(rsd, *a, **k)
                self.closed = False
                self.service = Conn()
                phone.ips.append(self)

            async def __aexit__(self, *exc):
                self.closed = True
                return False

            async def lookup(self, opts):
                return {INSTALLED: {"CFBundleIdentifier": INSTALLED, "CFBundleDisplayName": "CrashLab",
                                    "CFBundleExecutable": "CrashLab", "ApplicationType": "User",
                                    "Path": "/private/var/containers/Bundle/Application/A/CrashLab.app"}}

            # pymobiledevice3's own flow (what app.install called before): upload, Install, wait for Complete
            async def install_from_local(self, path, cmd="Install", options=None, handler=None):
                async with AfcService(self.rsd) as afc:
                    name = "/PublicStaging/pymobiledevice3/x.ipa"
                    try:
                        await afc.set_file_contents(name, Path(path).read_bytes())
                        await self.service.send_plist({"Command": cmd, "PackagePath": name})
                        while (r := await self.service.recv_plist()).get("Status") != "Complete":
                            if r.get("PercentComplete") and handler:
                                handler(r["PercentComplete"])
                    finally:
                        await afc.rm_single(name, force=True)

        class AppServiceService(devtest.Svc):
            async def list_processes(self):
                if phone.kill_fails:
                    raise ConnectionResetError("app service")
                return [{"processIdentifier": pid, "executableURL": {
                    "relative": "file:///private/var/containers/Bundle/Application/A/CrashLab.app/CrashLab"}}
                    for pid in phone.running]

            async def send_signal_to_process(self, pid, sig):
                phone.running.remove(pid)
                phone.log.append(f"kill {pid}")

        self.AfcService = AfcService
        self.InstallationProxyService = InstallationProxyService
        self.AppServiceService = AppServiceService

    def all_closed(self) -> bool:
        return all(x.closed for x in self.afcs + self.ips)


class InstallCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.ipa = make_ipa(tmp)
        stub = stub_pmd3()
        stub.__enter__()
        self.addCleanup(stub.__exit__, None, None, None)
        from iosstudio.device.features.apps import index as AI
        from iosstudio.device.features.apps import install as INS
        from iosstudio.device.real import core, daemon
        self.INS = INS
        for p in (mock.patch.object(AI.INDEX, "cache_dir", tmp), mock.patch.object(AI.INDEX, "_devs", {}),
                  mock.patch.object(AI.INDEX, "_tasks", {}), mock.patch.object(AI.INDEX, "_gen", {}),
                  mock.patch.object(core, "dlog", lambda *a, **k: None),
                  mock.patch.object(INS, "dlog", lambda *a, **k: None, create=True)):
            p.start()
            self.addCleanup(p.stop)
        self.events: list = []
        p = mock.patch.object(core, "BROKER", types.SimpleNamespace(emit=lambda n, d: self.events.append((n, d)),
                                                                    syslog=None))
        p.start()
        self.addCleanup(p.stop)
        self.phone = Phone()
        self.d = daemon.Daemon(types.SimpleNamespace(
            version="fake", iter_remote_pair_records_by_identifier=lambda: iter(()),
            AfcService=self.phone.AfcService, InstallationProxyService=self.phone.InstallationProxyService,
            AppServiceService=self.phone.AppServiceService,
            exceptions=types.SimpleNamespace()))
        self.addAsyncCleanup(self.d.shutdown)
        s = daemon.Session(self.d, UDID)
        s.rsd = types.SimpleNamespace(udid=UDID, all_values={})
        s.where = {"transport": "wifi"}
        s.connected.set()
        self.d.session = self.s = s
        self.unraisable: list = []
        p = mock.patch("sys.unraisablehook", lambda u: self.unraisable.append(u))
        p.start()
        self.addCleanup(p.stop)
        self.loop_errors: list = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: self.loop_errors.append(ctx))

    def install(self):
        return self.d.dispatch("app.install", {"path": str(self.ipa)})

    def progress(self):
        return [(d["status"], d["percent"]) for n, d in self.events if n == "installProgress"]

    async def assert_next_install_works(self):
        self.phone.script = list(DONE)
        self.phone.waiting.clear()
        self.events.clear()
        out = await asyncio.wait_for(self.install(), WAIT)
        self.assertEqual((out["bundleId"], out["packageBundleId"]), (INSTALLED, BID))
        self.assertEqual(self.progress()[-1], ("done", 100))
        self.assertEqual(self.phone.files, {})
        self.assertTrue(self.phone.all_closed())

    def assert_clean(self):
        gc.collect()
        self.assertEqual(self.phone.files, {}, "the staged package is removed")
        self.assertTrue(self.phone.all_closed(), "AFC and installation_proxy are closed")
        self.assertEqual(self.unraisable, [], "nothing raised while being garbage-collected")
        self.assertEqual([c.get("message") for c in self.loop_errors], [])


class Install(InstallCase):
    async def test_uploads_in_chunks_installs_and_cleans_up(self):
        out = await asyncio.wait_for(self.install(), WAIT)
        self.assertEqual((out["path"], out["bundleId"], out["packageBundleId"]), (str(self.ipa), INSTALLED, BID))
        req = self.phone.requests[0]
        self.assertEqual(req["Command"], "Install")
        self.assertTrue(req["PackagePath"].startswith(self.INS.STAGING_DIR + "/"))
        self.assertEqual(self.progress(), [("uploading", 0), ("installing", 0), ("installing", 5),
                                           ("installing", 40), ("installing", 80), ("done", 100)])
        self.assert_clean()

    async def test_app_folder_is_zipped_into_an_ipa(self):
        app = self.ipa.parent / "CrashLab.app"
        app.mkdir()
        (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": BID}))
        (app / "Frameworks").mkdir()
        (app / "Frameworks" / "x.dylib").write_bytes(b"dylib")
        with zipfile.ZipFile(io.BytesIO(self.INS.package_bytes(app))) as z:
            self.assertEqual(sorted(z.namelist()),
                             ["Payload/CrashLab.app/Frameworks/x.dylib", "Payload/CrashLab.app/Info.plist"])
        out = await asyncio.wait_for(self.d.dispatch("app.install", {"path": str(app)}), WAIT)
        self.assertEqual(out["packageBundleId"], BID)

    async def test_installer_error_keeps_its_plain_message(self):
        self.phone.script = [{"Error": "ApplicationVerificationFailed", "ErrorDescription": "no profile"}]
        with self.assertRaises(HelperError) as cm:
            await asyncio.wait_for(self.install(), WAIT)
        self.assertEqual(cm.exception.code, "device-not-registered")
        self.assertIn("ApplicationVerificationFailed: no profile", cm.exception.detail)
        self.assert_clean()
        await self.assert_next_install_works()


class Stall(InstallCase):
    async def test_silent_installer_fails_soon_with_a_fix_and_the_next_install_works(self):
        self.phone.script = [{"Status": "CreatingStagingDirectory", "PercentComplete": 0}]
        with mock.patch.object(self.INS, "INSTALL_STALL_S", 0.2), self.assertRaises(DeviceError) as cm:
            await asyncio.wait_for(self.install(), WAIT)
        e = cm.exception
        self.assertEqual(e.code, "install-stalled")
        self.assertIn("stopped answering", e.message)
        self.assertNotIn("run ", e.message.lower())
        self.assertEqual(e.fix["page"], "device")
        self.assertEqual(e.fix["action"], "reconnect")
        self.assertIn("CreatingStagingDirectory", e.detail)
        self.assertIsNotNone(self.d.session, "a stall doesn't drop the tunnel")
        self.assert_clean()
        await self.assert_next_install_works()

    async def test_stalled_upload_fails_too(self):
        async def hang(self_, h, data):
            await asyncio.Event().wait()
        with mock.patch.object(self.phone.AfcService, "fwrite", hang), \
                mock.patch.object(self.INS, "UPLOAD_STALL_S", 0.2), self.assertRaises(DeviceError) as cm:
            await asyncio.wait_for(self.install(), WAIT)
        self.assertEqual(cm.exception.code, "install-stalled")
        self.assertIn("uploading", cm.exception.detail)
        self.assertEqual(self.phone.ips, [], "the installer isn't asked to install a partial upload")
        self.assert_clean()
        await self.assert_next_install_works()


class Cancel(InstallCase):
    async def test_cancel_mid_install_closes_everything_and_the_next_install_works(self):
        self.phone.script = []
        t = asyncio.create_task(self.install())
        await asyncio.wait_for(self.phone.waiting.wait(), WAIT)
        t.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(t, WAIT)
        self.assert_clean()
        self.assertEqual({x.get_name() for x in asyncio.all_tasks()} & {"afc-reader"}, set())
        await self.assert_next_install_works()

    async def test_generator_exit_is_not_turned_into_an_install_error(self):
        """A task garbage-collected while pending gets its coroutine closed (GeneratorExit). The old
        `except BaseException` in app.install turned that into an install error, so the coroutine "ignored
        GeneratorExit" (RuntimeError in the daemon log). It must pass through untouched, after the cleanup."""
        async def gen_exit(*a):
            raise GeneratorExit
        with mock.patch.object(self.INS, "close_running_app", gen_exit, create=True), \
                mock.patch.object(self.phone.InstallationProxyService, "install_from_local", gen_exit), \
                self.assertRaises(GeneratorExit):
            await asyncio.wait_for(self.install(), WAIT)
        self.assert_clean()
        await self.assert_next_install_works()


class CloseRunningApp(InstallCase):
    async def test_running_app_is_closed_before_the_installer_runs(self):
        self.phone.running = [777]
        out = await asyncio.wait_for(self.install(), WAIT)
        self.assertEqual(out["bundleId"], INSTALLED)
        self.assertEqual(self.phone.log, ["uploaded", "kill 777", "Install"])
        self.assertEqual(self.phone.running, [])
        self.assert_clean()

    async def test_app_that_is_not_running_is_left_alone(self):
        await asyncio.wait_for(self.install(), WAIT)
        self.assertEqual(self.phone.log, ["uploaded", "Install"])

    async def test_a_failed_close_does_not_stop_the_install(self):
        self.phone.running = [777]
        self.phone.kill_fails = True
        out = await asyncio.wait_for(self.install(), WAIT)
        self.assertEqual(out["bundleId"], INSTALLED)
        self.assertEqual(self.phone.log, ["uploaded", "Install"])
        self.assert_clean()

    async def test_silence_says_it_waits_for_the_app_then_the_install_finishes(self):
        self.phone.script = [{"Status": "TakingInstallLock", "PercentComplete": 0}, 0.3, *DONE]
        with mock.patch.object(self.INS, "WAITING_NOTICE_S", 0.05):
            out = await asyncio.wait_for(self.install(), WAIT)
        self.assertEqual(out["bundleId"], INSTALLED)
        notes = [d for n, d in self.events if n == "installProgress" and "message" in d]
        self.assertEqual(notes, [{"path": str(self.ipa), "percent": 0, "status": "installing",
                                  "message": "Waiting for CrashLab to close on your iPhone"}])
        self.assertEqual(self.progress()[-1], ("done", 100))
        self.assert_clean()

    async def test_stall_message_names_the_app(self):
        self.phone.script = []
        with mock.patch.object(self.INS, "WAITING_NOTICE_S", 0.05), mock.patch.object(self.INS, "INSTALL_STALL_S", 0.2), \
                self.assertRaises(DeviceError) as cm:
            await asyncio.wait_for(self.install(), WAIT)
        self.assertEqual(cm.exception.code, "install-stalled")
        self.assertIn("Close CrashLab on the iPhone", cm.exception.message)
        self.assertEqual(len([d for n, d in self.events if n == "installProgress" and "message" in d]), 1)
        self.assert_clean()
        await self.assert_next_install_works()


@unittest.skipUnless(have_pmd3(), "needs pymobiledevice3 (the device daemon's venv)")
class RealApi(unittest.TestCase):
    """The pymobiledevice3 calls app.install makes, with the arguments it passes (the fakes above mirror them)."""

    def test_afc_and_installation_proxy_calls_exist(self):
        from pymobiledevice3.service_connection import ServiceConnection
        from pymobiledevice3.services.afc import AfcService
        for name, args, kw in (("makedirs", ("/d",), {}), ("fopen", ("/f", "w"), {}), ("fwrite", (1, b"x"), {}),
                               ("fclose", (1,), {}), ("rm_single", ("/f",), {"force": True})):
            inspect.signature(getattr(AfcService, name)).bind(None, *args, **kw)   # (some are wrapped)
        for name, args in (("send_plist", ({},)), ("recv_plist", ())):
            fn = getattr(ServiceConnection, name)
            self.assertTrue(inspect.iscoroutinefunction(fn), name)
            inspect.signature(fn).bind(None, *args)


# ---------------------------------------------------------------- broker: requests of a client that leaves

class HangingBackend:
    """A backend whose `install` waits on a future nothing else references (a dead phone connection)."""

    def __init__(self):
        self.started = asyncio.Event()
        self.cleaned: list[str] = []

    async def welcome(self):
        return []

    async def dispatch(self, cmd, args):
        self.started.set()
        try:
            await asyncio.get_running_loop().create_future()
        except BaseException as exc:
            self.cleaned.append(type(exc).__name__)
            raise

    def error_json(self, exc, cmd):
        return protocol.error_json(exc)

    async def client_gone(self, client):
        pass

    async def shutdown(self):
        pass


class BrokerRequests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import os
        base = Path(os.environ["IOSSTUDIO_RUNTIME_DIR"])
        base.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(dir=base, prefix="r"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        for p in (mock.patch.dict(os.environ, {"IOSSTUDIO_RUNTIME_DIR": str(self.base)}),
                  mock.patch.object(B, "log", lambda text: None)):
            p.start()
            self.addCleanup(p.stop)
        self.backend = HangingBackend()
        self.b = B.Broker(demo=None, idle=60)
        B._prepare_dir(self.b.p["dir"])
        self.loop_errors: list = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: self.loop_errors.append(ctx))

        async def make_backend(broker):
            return self.backend
        self.run_task = asyncio.create_task(self.b.run(make_backend))
        while self.b._server is None:
            await asyncio.sleep(0)

    async def asyncTearDown(self):
        if not self.run_task.done():
            self.b.stop()
        await asyncio.wait_for(asyncio.gather(self.run_task, return_exceptions=True), WAIT)

    async def send_hanging_request(self):
        r, w = await asyncio.open_unix_connection(str(self.b.p["sock"]))
        await protocol.write_line_async(w, protocol.request(1, "app.install", {"path": "/x.ipa"}))
        await asyncio.wait_for(self.backend.started.wait(), WAIT)
        return w

    async def test_request_of_a_client_that_leaves_is_cancelled_not_garbage_collected(self):
        w = await self.send_hanging_request()
        w.close()
        async with asyncio.timeout(WAIT):
            while not self.backend.cleaned:
                gc.collect()
                await asyncio.sleep(0.01)
        self.assertEqual(self.backend.cleaned, ["CancelledError"])
        await asyncio.sleep(0.05)
        gc.collect()
        self.assertEqual(self.b._requests, set())
        self.assertEqual([c.get("message") for c in self.loop_errors], [])

    async def test_requests_still_running_at_shutdown_are_cancelled(self):
        w = await self.send_hanging_request()
        self.b.stop()
        self.assertEqual(await asyncio.wait_for(self.run_task, WAIT), 0)
        self.assertEqual(self.backend.cleaned, ["CancelledError"])
        w.close()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        self.assertEqual([c.get("message") for c in self.loop_errors], [])


if __name__ == "__main__":
    unittest.main()
