"""Device transport and pure helpers (helper/iosstudio/device/link.py) plus model names (models.py): usbmux discovery,
pairing-record discovery and its signature cache, Wi-Fi vs USB transport selection with pymobiledevice3 faked at
its boundary, pinning records, provisioning-profile summaries, launch/install error mapping.

    .venv/bin/pytest -q tests/helper/test_dev_link.py
    cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_link
"""
from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import plistlib
import re
import shutil
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

from support import devtest

devtest.isolate()

from iosstudio.core.output import HelperError  # noqa: E402
from iosstudio.device import link, models  # noqa: E402

UDID = "00008110-000A1B2C3D4E5F60"


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


# ====================================================================== discovery

class UsbmuxErr(Exception):
    pass


def usb_pmd3(devices=None, raises=None):
    async def list_devices():
        if raises:
            raise raises
        return devices or []
    return types.SimpleNamespace(usbmux=types.SimpleNamespace(list_devices=list_devices),
                                 exceptions=types.SimpleNamespace(ConnectionFailedToUsbmuxdError=UsbmuxErr))


def dev(serial, kind="USB"):
    return types.SimpleNamespace(serial=serial, connection_type=kind)


class UsbDiscovery(unittest.IsolatedAsyncioTestCase):
    async def test_only_usb_sorted_and_unique(self):
        pmd3 = usb_pmd3([dev("B"), dev("A"), dev("B"), dev("C", "Network")])
        self.assertEqual(await link.usb_udids(pmd3), ["A", "B"])

    async def test_no_usbmuxd_is_no_devices(self):
        for exc in (UsbmuxErr(), FileNotFoundError(), ConnectionRefusedError()):
            self.assertEqual(await link.usb_udids(usb_pmd3(raises=exc)), [])


class Records(Tmp):
    def test_wifi_records_survive_a_bad_record(self):
        def records():
            yield "U1", self.tmp / "remote_U1.plist", {}
            raise plistlib.InvalidFileException("bad")
        pmd3 = types.SimpleNamespace(iter_remote_pair_records_by_identifier=records)
        self.assertEqual(link.wifi_records(pmd3), {"U1": str(self.tmp / "remote_U1.plist")})

    def test_lockdown_records(self):
        for name in ("U1.plist", "U2.plist", "SystemConfiguration.plist", "notes.txt"):
            (self.tmp / name).write_bytes(b"x")
        self.assertEqual(link.lockdown_records(self.tmp), {"U1", "U2"})
        self.assertEqual(link.lockdown_records(self.tmp / "missing"), set())

    def test_signature_only_counts_prefixed_entries(self):
        (self.tmp / "remote_U1.plist").write_bytes(b"x")
        sig = link.dir_signature(self.tmp, "remote_")
        self.assertEqual([e[0] for e in sig[1]], ["remote_U1.plist"])
        (self.tmp / "other.plist").write_bytes(b"x")
        os.utime(self.tmp, ns=(sig[0], sig[0]))            # same folder mtime: the other file is invisible
        self.assertEqual(link.dir_signature(self.tmp, "remote_"), sig)
        (self.tmp / "remote_U1.plist").write_bytes(b"longer")
        os.utime(self.tmp, ns=(sig[0], sig[0]))
        self.assertNotEqual(link.dir_signature(self.tmp, "remote_"), sig)   # rewrite seen through size/mtime

    @unittest.skipIf(os.geteuid() == 0, "root can list any folder")
    def test_signature_of_unlistable_folder_is_its_mtime(self):
        locked = self.tmp / "lockdown"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        self.assertEqual(link.dir_signature(locked), (locked.stat().st_mtime_ns,))

    def test_cache_recomputes_on_signature_change_only(self):
        sig = ["a"]
        values = iter([{"x": 1}, {"x": 2}])
        c = link.CachedRecords(lambda: next(values), lambda: sig[0])
        self.assertEqual(c.get(), {"x": 1})
        self.assertEqual(c.get(), {"x": 1})
        self.assertEqual(c.reads, 1)
        sig[0] = "b"
        self.assertEqual(c.get(), {"x": 2})
        self.assertEqual(c.reads, 2)

    def test_cache_keeps_an_empty_result(self):
        calls = []
        c = link.CachedRecords(lambda: calls.append(1) or {}, lambda: None)   # None signature = unreadable folder
        c.get()
        c.get()
        self.assertEqual(len(calls), 1)

    def test_pair_records_read_returns_copies(self):
        pmd3 = types.SimpleNamespace(remote_dir=self.tmp,
                                     iter_remote_pair_records_by_identifier=lambda: iter([("U1", "p", {})]))
        pr = link.PairRecords(pmd3, self.tmp / "lockdown")
        wifi, usb = pr.read()
        wifi["junk"] = "x"
        usb.add("junk")
        self.assertEqual(pr.read(), ({"U1": "p"}, set()))

    def test_record_path_found_or_default(self):
        pmd3 = types.SimpleNamespace(
            iter_remote_pair_records_by_identifier=lambda: iter([("U1", "/x/remote_U1.plist", {})]),
            get_remote_pairing_record_filename=lambda udid: f"remote_{udid}")
        self.assertEqual(link.record_path(pmd3, "U1"), Path("/x/remote_U1.plist"))
        self.assertEqual(link.record_path(pmd3, "U9"), link.REMOTE_DIR / "remote_U9.plist")

    def test_pin_record(self):
        p = self.tmp / "remote_U1.plist"
        p.write_bytes(plistlib.dumps({"private_key": b"k"}))
        p.chmod(0o644)
        self.assertTrue(link.pin_record(p, "HOST-1"))
        self.assertEqual(plistlib.loads(p.read_bytes()), {"private_key": b"k", "host_identifier": "HOST-1"})
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertFalse(link.pin_record(p, "HOST-2"))            # already pinned: kept
        self.assertEqual(plistlib.loads(p.read_bytes())["host_identifier"], "HOST-1")
        self.assertEqual([x.name for x in self.tmp.iterdir()], ["remote_U1.plist"])   # no .tmp left


# ====================================================================== transport selection

class Recorder:
    def __init__(self):
        self.calls: list[str] = []


def wifi_pmd3(rec: Recorder, hosts=("fe80::1%en0",), skipped=()):
    """pymobiledevice3's Wi-Fi pieces: Bonjour lookup -> services -> TCP tunnel -> dial plane -> RSD."""
    log_name = "fake.pmd3.tunnel_service"

    class Tun:
        def set_peer(self, addr):
            rec.calls.append(f"peer {addr}")

    class Service:
        def __init__(self, host):
            self.hostname = host

        @contextlib.asynccontextmanager
        async def start_tcp_tunnel(self):
            rec.calls.append(f"tunnel {self.hostname}")
            try:
                yield types.SimpleNamespace(client=types.SimpleNamespace(tun=Tun()), address="fd00::2", port=58783,
                                            auxiliary_metadata={"m": 1})
            finally:
                rec.calls.append("tunnel closed")

        async def close(self):
            rec.calls.append(f"close {self.hostname}")

    async def get_services(bonjour_timeout, udid):
        rec.calls.append(f"bonjour {udid} {bonjour_timeout}")
        for s in skipped:
            logging.getLogger(log_name).debug("Skipping remote pairing service %s", s)
        return [Service(h) for h in hosts]

    class Dial:
        def __init__(self, tun, addr):
            self.addr = addr

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            rec.calls.append("dial closed")

        def dial(self, *a):
            return None

    class Rsd:
        def __init__(self, where, open_connection, auxiliary_metadata):
            rec.calls.append(f"rsd {where[0]}:{where[1]}")
            self.udid = UDID

        async def connect(self):
            rec.calls.append("rsd connect")

        async def close(self):
            rec.calls.append("rsd close")

    ut = types.SimpleNamespace(_active_tunnel=None, USERSPACE_ACTIVE=False)
    ts = types.SimpleNamespace(__name__=log_name, get_remote_pairing_tunnel_services=get_services,
                               USE_USERSPACE_TUNNEL=False)
    return types.SimpleNamespace(tunnel_service=ts, userspace_tunnel=ut, UserspaceDialPlane=Dial,
                                 RemoteServiceDiscoveryService=Rsd)


class Transport(unittest.IsolatedAsyncioTestCase):
    async def test_wifi_connects_publishes_and_closes(self):
        rec = Recorder()
        pmd3 = wifi_pmd3(rec, hosts=("fe80::1%en0", "192.168.1.9"))
        async with link.rsd_for(pmd3, "wifi", UDID) as (rsd, where):
            self.assertEqual(where, {"transport": "wifi", "address": "fe80::1%en0", "interface": "en0"})
            self.assertTrue(pmd3.tunnel_service.USE_USERSPACE_TUNNEL)
            handle = pmd3.userspace_tunnel._active_tunnel
            self.assertIs(handle.rsd, rsd)                  # media streams find the userspace stack
            self.assertEqual(handle.serial, UDID)
            self.assertTrue(pmd3.userspace_tunnel.USERSPACE_ACTIVE)
        self.assertIsNone(pmd3.userspace_tunnel._active_tunnel)
        self.assertFalse(pmd3.userspace_tunnel.USERSPACE_ACTIVE)
        self.assertEqual(rec.calls, [
            f"bonjour {UDID} {link.BONJOUR_TIMEOUT_S}", "close 192.168.1.9",   # extra answers closed first
            "tunnel fe80::1%en0", "peer fd00::2", "rsd fd00::2:58783", "rsd connect",
            "rsd close", "dial closed", "tunnel closed", "close fe80::1%en0"])

    async def test_wifi_leaves_an_already_published_tunnel(self):
        rec = Recorder()
        pmd3 = wifi_pmd3(rec)
        other = object()
        pmd3.userspace_tunnel._active_tunnel = other
        async with link.wifi_rsd(pmd3, UDID):
            self.assertIs(pmd3.userspace_tunnel._active_tunnel, other)
        self.assertIs(pmd3.userspace_tunnel._active_tunnel, other)

    async def test_wifi_closes_on_error_in_body(self):
        rec = Recorder()
        pmd3 = wifi_pmd3(rec)
        with self.assertRaises(KeyError):
            async with link.wifi_rsd(pmd3, UDID):
                raise KeyError("x")
        self.assertEqual(rec.calls[-4:], ["rsd close", "dial closed", "tunnel closed", "close fe80::1%en0"])
        self.assertIsNone(pmd3.userspace_tunnel._active_tunnel)

    async def test_wifi_no_answer(self):
        pmd3 = wifi_pmd3(Recorder(), hosts=())
        with self.assertRaises(link.UnreachableError) as cm:
            async with link.wifi_rsd(pmd3, UDID):
                pass
        self.assertIn("no answer on Wi-Fi", str(cm.exception))

    async def test_wifi_advertised_but_not_answering(self):
        pmd3 = wifi_pmd3(Recorder(), hosts=(), skipped=("192.168.1.9:49152",))
        logger = logging.getLogger(pmd3.tunnel_service.__name__)
        level = logger.level
        with self.assertRaises(link.UnreachableError) as cm:
            async with link.wifi_rsd(pmd3, UDID):
                pass
        self.assertIn("advertises itself", str(cm.exception))
        self.assertIn("192.168.1.9:49152", str(cm.exception))
        self.assertEqual(logger.level, level)              # debug logging and handler undone
        self.assertEqual(logger.handlers, [])

    async def test_usb(self):
        opened = []

        class Tunnel:
            def __init__(self, serial, autopair, remotepairing_fallback):
                opened.append((serial, autopair, remotepairing_fallback))

            async def __aenter__(self):
                return "RSD"

            async def __aexit__(self, *exc):
                opened.append("closed")
        pmd3 = usb_pmd3([dev(UDID)])
        pmd3.UserspaceRsdTunnel = Tunnel
        async with link.rsd_for(pmd3, "usb", UDID) as (rsd, where):
            self.assertEqual((rsd, where), ("RSD", {"transport": "usb", "address": "usb", "interface": ""}))
        self.assertEqual(opened, [(UDID, False, False), "closed"])      # never pairs on its own

    async def test_usb_not_plugged_in(self):
        pmd3 = usb_pmd3([dev(UDID, "Network")])
        pmd3.UserspaceRsdTunnel = lambda **k: self.fail("no tunnel without a cable")
        with self.assertRaises(link.UnreachableError):
            async with link.usb_rsd(pmd3, UDID):
                pass

    def test_scope_interface(self):
        self.assertEqual(link.scope_interface("fe80::1%wlan0"), "wlan0")
        self.assertEqual(link.scope_interface("192.168.1.2"), "")


# ====================================================================== profiles, errors, packages

def profile_blob(**over) -> bytes:
    p = {"Name": "iOS Team Provisioning Profile: com.example.app", "UUID": "1234-ABCD",
         "TeamIdentifier": ["TEAM123456"], "TeamName": "Jane Doe",
         "CreationDate": dt.datetime(2026, 11, 1, 12, 0, 0),
         "ExpirationDate": dt.datetime(2099, 1, 1),
         "ProvisionedDevices": [UDID, "other"], "IsXcodeManaged": True,
         "DeveloperCertificates": [b"cert-one", b"cert-two"],
         "Entitlements": {"application-identifier": "TEAM123456.com.example.app", "get-task-allow": True}}
    p.update(over)
    return b"\x30\x82CMS-header" + plistlib.dumps(p) + b"\x00signature"


class Profiles(unittest.TestCase):
    def test_summary_from_signed_blob(self):
        s = link.profile_summary(link.parse_profile(profile_blob()))
        self.assertEqual(s["name"], "iOS Team Provisioning Profile: com.example.app")
        self.assertEqual((s["appId"], s["team"], s["teamName"]), ("TEAM123456.com.example.app", "TEAM123456", "Jane Doe"))
        self.assertEqual(s["created"], int(dt.datetime(2026, 11, 1, 12, tzinfo=dt.UTC).timestamp()))
        self.assertFalse(s["expired"])
        self.assertEqual(s["devices"], 2)
        self.assertTrue(s["isXcodeManaged"])
        self.assertTrue(s["getTaskAllow"])
        self.assertEqual(len(s["certSha1"]), 2)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", h) for h in s["certSha1"]))

    def test_expired_and_sparse(self):
        s = link.profile_summary(link.parse_profile(profile_blob(ExpirationDate=dt.datetime(2020, 1, 1))))
        self.assertTrue(s["expired"])
        empty = link.profile_summary({})
        self.assertEqual((empty["team"], empty["expires"], empty["expired"], empty["certSha1"]), ("", 0, False, []))

    def test_not_a_profile(self):
        with self.assertRaises(ValueError):
            link.parse_profile(b"garbage")


class Errors(unittest.TestCase):
    def test_launch_failure_takes_innermost(self):
        info = {"BSErrorCodeDescription": "RequestDenied", "NSLocalizedFailureReason": "outer",
                "NSUnderlyingError": {"userInfo": {"BSErrorCodeDescription": "Security",
                                                   "NSUnderlyingError": {"userInfo": {"NSLocalizedFailureReason":
                                                                                      "untrusted developer"}}}}}
        self.assertEqual(link.launch_failure(info), ("Security", "untrusted developer"))
        self.assertEqual(link.launch_failure(None), ("", ""))
        self.assertEqual(link.launch_failure({"NSUnderlyingError": "not a dict"}), ("", ""))

    def test_install_errors(self):
        classify = lambda exc: HelperError("failed", "x")   # noqa: E731
        cases = {"ApplicationVerificationFailed: no valid provisioning profile": "device-not-registered",
                 "0xe8008021 maximum number of apps": "app-limit",
                 "DeviceLocked": "locked", "InsufficientStorage": "no-space",
                 "DeviceOSVersionTooLow": "incompatible", "0xe8000067 bad CodeSignature": "bad-signature",
                 "something else": "install-failed"}
        for text, code in cases.items():
            e = link.install_error(RuntimeError(text), classify)
            self.assertEqual(e.code, code, text)
            self.assertIn(text, e.detail)
        self.assertEqual(link.install_error(RuntimeError("0xe8008021"), classify).fix["page"], "device")
        self.assertEqual(link.install_error(RuntimeError("DeviceLocked"), classify).fix, None)

    def test_install_error_keeps_classified_connection_errors(self):
        lost = HelperError("unreachable", "Lost the iPhone.")
        self.assertIs(link.install_error(ConnectionResetError("x"), lambda exc: lost), lost)


class Packages(Tmp):
    def test_bundle_id_of_app_folder_and_ipa(self):
        info = plistlib.dumps({"CFBundleIdentifier": "com.example.app"})
        app = self.tmp / "App.app"
        app.mkdir()
        (app / "Info.plist").write_bytes(info)
        self.assertEqual(link.package_bundle_id(app), "com.example.app")
        ipa = self.tmp / "App.ipa"
        with zipfile.ZipFile(ipa, "w") as z:
            z.writestr("Payload/App.app/Info.plist", info)
            z.writestr("Payload/App.app/PlugIns/Ext.appex/Info.plist", plistlib.dumps({"CFBundleIdentifier": "ext"}))
        self.assertEqual(link.package_bundle_id(ipa), "com.example.app")

    def test_ipa_without_one_app(self):
        ipa = self.tmp / "Bad.ipa"
        with zipfile.ZipFile(ipa, "w") as z:
            z.writestr("Payload/A.app/Info.plist", b"")
            z.writestr("Payload/B.app/Info.plist", b"")
        with self.assertRaises(ValueError):
            link.package_bundle_id(ipa)


# ====================================================================== models.py

class Models(unittest.TestCase):
    def test_known_and_unknown(self):
        self.assertEqual(models.marketing_name("iPhone14,5"), "iPhone 13")
        self.assertEqual(models.marketing_name("iPhone17,5"), "iPhone 16e")
        self.assertEqual(models.marketing_name("iPad8,1"), "iPad Pro (11-inch)")
        self.assertEqual(models.marketing_name("iPhone99,9"), "")
        self.assertEqual(models.marketing_name(""), "")
        self.assertEqual(models.marketing_name(None), "")

    def test_table_is_well_formed(self):
        for key, name in models.MODELS.items():
            self.assertRegex(key, r"^(iPhone|iPad|iPod)\d+,\d+$")
            self.assertTrue(name.startswith(key.rstrip("0123456789,")), (key, name))   # iPad ids name iPads


if __name__ == "__main__":
    unittest.main()
