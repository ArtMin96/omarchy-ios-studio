"""Developer disk image, Developer Mode and provisioning profiles
(helper/iosstudio/device/features/{ddi,profiles}/{real,demo}.py).

The real side runs in a real Daemon with a connected Session and pymobiledevice3's services faked on the `pmd3`
namespace (MobileImageMounterService, auto_mount, AmfiService, create_using_usbmux, MisagentService).

    .venv/bin/pytest -q tests/helper/test_dev_ddi_profiles.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_ddi_profiles
"""
from __future__ import annotations

import plistlib
import types
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from support import devtest
from support.pmd3_stub import stub_pmd3

devtest.isolate()

from iosstudio.core.output import HelperError  # noqa: E402

UDID = "00008150-DDI"


class Phone:
    def __init__(self):
        self.mounted = False
        self.mount_error: BaseException | None = None
        self.calls: list = []
        self.profiles: list = []
        self.amfi_error: BaseException | None = None

    def pmd3(self):
        phone = self

        class MobileImageMounterService(devtest.Svc):
            async def is_image_mounted(self, kind):
                phone.calls.append(("is_image_mounted", kind))
                return phone.mounted

        async def auto_mount(rsd):
            phone.calls.append("auto_mount")
            if phone.mount_error:
                raise phone.mount_error
            phone.mounted = True

        class AmfiService:
            def __init__(self, lockdown):
                self.lockdown = lockdown

            async def reveal_developer_mode_option_in_ui(self):
                phone.calls.append(("reveal", self.lockdown))
                if phone.amfi_error:
                    raise phone.amfi_error

        class Lockdown:
            async def close(self):
                phone.calls.append("lockdown.close")

        async def create_using_usbmux(serial, autopair=False):
            phone.calls.append(("usbmux", serial))
            return Lockdown()

        class MisagentService(devtest.Svc):
            async def copy_all(self):
                return phone.profiles

        return types.SimpleNamespace(
            version="fake", iter_remote_pair_records_by_identifier=lambda: iter(()),
            image_type_for_device=lambda rsd: "Personalized", auto_mount=auto_mount,
            MobileImageMounterService=MobileImageMounterService, AmfiService=AmfiService,
            create_using_usbmux=create_using_usbmux, MisagentService=MisagentService)


def profile_plist(name: str, days: int, **extra) -> dict:
    now = datetime.now(UTC).replace(microsecond=0)
    return {"Name": name, "UUID": f"uuid-{name}", "TeamIdentifier": ["A1B2C3D4E5"], "TeamName": "Alex",
            "CreationDate": now - timedelta(days=7), "ExpirationDate": now + timedelta(days=days),
            "Entitlements": {"application-identifier": f"A1B2C3D4E5.com.example.{name}", "get-task-allow": True},
            "ProvisionedDevices": [UDID], "DeveloperCertificates": [b"cert"], **extra}


def blob(plist: dict) -> bytes:
    """A provisioning profile as the phone sends it: the plist inside a CMS envelope."""
    return b"\x30\x82junk" + plistlib.dumps(plist) + b"\x00sig"


class RealHarness:
    async def setup_real(self):
        stub = stub_pmd3()
        stub.__enter__()
        self.addCleanup(stub.__exit__, None, None, None)
        from iosstudio.device.real import core, daemon
        self.daemon_mod = daemon
        self.events: list = []
        p = mock.patch.object(core, "BROKER", types.SimpleNamespace(emit=lambda n, d: self.events.append((n, d)),
                                                                    syslog=None))
        p.start()
        self.addCleanup(p.stop)
        self.phone = Phone()
        self.d = daemon.Daemon(self.phone.pmd3())
        self.addAsyncCleanup(self.d.shutdown)

    def connect(self, transport="wifi", devmode=True):
        s = self.daemon_mod.Session(self.d, UDID)

        async def get_developer_mode_status():
            return devmode
        s.rsd = types.SimpleNamespace(udid=UDID, all_values={}, get_developer_mode_status=get_developer_mode_status)
        s.where = {"transport": transport}
        s.connected.set()
        if transport == "usb":
            self.d.usb.add(UDID)
        self.d.session = s
        return s


class DemoHarness:
    async def setup_demo(self):
        from iosstudio.device.demo.daemon import DemoBackend
        self.b = DemoBackend(None)
        self.addAsyncCleanup(self.b.shutdown)


class RealDdi(RealHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_real()

    async def test_status_asks_for_the_devices_image_type(self):
        s = self.connect()
        self.assertEqual(await self.d.dispatch("ddi.status", {}), {"mounted": False})
        self.assertEqual(self.phone.calls, [("is_image_mounted", "Personalized")])
        self.assertIs(s.info["ddiMounted"], False)
        self.phone.mounted = True
        self.assertEqual(await self.d.dispatch("ddi.status", {}), {"mounted": True})
        self.assertIs(s.info["ddiMounted"], True)

    async def test_mount(self):
        s = self.connect()
        self.assertEqual(await self.d.dispatch("ddi.mount", {}), {"mounted": True})
        self.assertIn("auto_mount", self.phone.calls)
        self.assertIn(("deviceInfo", {"udid": UDID, "ddiMounted": True}), self.events)
        self.assertTrue(s.info["ddiMounted"])
        # already mounted: no second mount
        self.phone.calls.clear()
        await self.d.dispatch("ddi.mount", {})
        self.assertNotIn("auto_mount", self.phone.calls)

    async def test_mount_failure_is_a_ddi_error(self):
        self.connect()
        self.phone.mount_error = RuntimeError("TSS refused")
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("ddi.mount", {})
        self.assertEqual(cm.exception.code, "ddi")
        self.assertIn("TSS refused", cm.exception.detail)
        self.assertEqual(cm.exception.fix["action"], "ddi")
        # a connection problem keeps its own code
        self.phone.mount_error = ConnectionResetError("gone")
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("ddi.mount", {})
        self.assertEqual(cm.exception.code, "unreachable")

    async def test_devmode_status(self):
        s = self.connect(devmode=False)
        self.assertEqual(await self.d.dispatch("devmode.status", {}), {"enabled": False})
        self.assertIs(s.info["developerMode"], False)

    async def test_reveal_over_usb_uses_lockdown(self):
        self.connect("usb")
        self.assertEqual(await self.d.dispatch("devmode.reveal", {}), {})
        self.assertEqual(self.phone.calls[0], ("usbmux", UDID))
        self.assertEqual(self.phone.calls[1][0], "reveal")
        self.assertEqual(self.phone.calls[-1], "lockdown.close")

    async def test_reveal_over_wifi(self):
        s = self.connect("wifi")
        self.assertEqual(await self.d.dispatch("devmode.reveal", {}), {})
        self.assertEqual(self.phone.calls, [("reveal", s.rsd)])
        self.phone.amfi_error = RuntimeError("not over RSD")
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("devmode.reveal", {})
        self.assertEqual(cm.exception.code, "needs-usb")
        self.assertIn("not over RSD", cm.exception.detail)

    async def test_no_device_and_wrong_device(self):
        for cmd in ("ddi.status", "ddi.mount", "devmode.status", "devmode.reveal", "profiles.list"):
            with self.assertRaises(HelperError) as cm:
                await self.d.dispatch(cmd, {})
            self.assertEqual(cm.exception.code, "no-device", cmd)
        self.connect()
        with self.assertRaises(HelperError) as cm:
            await self.d.dispatch("ddi.status", {"udid": "another"})
        self.assertEqual(cm.exception.code, "bad-args")


class RealProfiles(RealHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_real()

    async def test_list_parses_sorts_and_skips_junk(self):
        self.connect()
        self.phone.profiles = [blob(profile_plist("soon", 2)), b"not a profile",
                               types.SimpleNamespace(plist=profile_plist("later", 6, IsXcodeManaged=True)),
                               blob(profile_plist("old", -1))]
        out = await self.d.dispatch("profiles.list", {})
        self.assertEqual([p["name"] for p in out], ["later", "soon", "old"])   # newest expiry first
        later, _, old = out
        self.assertEqual(later["appId"], "A1B2C3D4E5.com.example.later")
        self.assertEqual((later["team"], later["teamName"], later["devices"]), ("A1B2C3D4E5", "Alex", 1))
        self.assertTrue(later["isXcodeManaged"])
        self.assertTrue(later["getTaskAllow"])
        self.assertEqual(len(later["certSha1"]), 1)
        self.assertFalse(later["expired"])
        self.assertTrue(old["expired"])

    async def test_empty(self):
        self.connect()
        self.assertEqual(await self.d.dispatch("profiles.list", {}), [])


class DemoSide(DemoHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_demo()

    async def test_demo_answers(self):
        self.assertEqual(await self.b.dispatch("ddi.status", {}), {"mounted": True})
        self.assertEqual(await self.b.dispatch("ddi.mount", {}), {"mounted": True})
        self.assertEqual(await self.b.dispatch("devmode.status", {}), {"enabled": True})
        self.assertEqual(await self.b.dispatch("devmode.reveal", {}), {})
        profiles = await self.b.dispatch("profiles.list", {})
        self.assertEqual(len(profiles), 2)
        self.assertEqual([p["expires"] for p in profiles], sorted((p["expires"] for p in profiles), reverse=True))


class Parity(RealHarness, DemoHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.setup_real()
        await self.setup_demo()
        self.connect()
        self.phone.mounted = True
        self.phone.profiles = [blob(profile_plist("a", 3))]

    async def test_ddi_devmode_same_replies(self):
        for cmd in ("ddi.status", "ddi.mount", "devmode.status", "devmode.reveal"):
            self.assertEqual(await self.d.dispatch(cmd, {}), await self.b.dispatch(cmd, {}), cmd)

    async def test_profile_keys(self):
        real = await self.d.dispatch("profiles.list", {})
        demo = await self.b.dispatch("profiles.list", {})
        self.assertEqual(set(real[0]), set(demo[0]))

    async def test_profile_keys_demo_subset(self):
        real = await self.d.dispatch("profiles.list", {})
        demo = await self.b.dispatch("profiles.list", {})
        self.assertLessEqual(set(demo[0]), set(real[0]))
        for k in demo[0]:
            self.assertIsInstance(demo[0][k], (int, float) if isinstance(real[0][k], (int, float))
                                  and not isinstance(real[0][k], bool) else type(real[0][k]), k)


if __name__ == "__main__":
    unittest.main()
