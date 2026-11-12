"""helper/iosstudio/features/hotreload/signing.py: xtool's identity (throwaway keys made with openssl in a temp
folder; never the owner's), key format conversion in memory, and signing a dylib with the real rcodesign (skipped
without it). The key must never reach the disk: signing is checked to write no file besides the dylib."""
from __future__ import annotations

import base64
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from iosstudio.core.output import HelperError
from iosstudio.features.hotreload import macho, signing
from support.hot_reload import MH_DYLIB, OPENSSL, Sect, Sym, build_macho, make_identity

RCODESIGN = signing.rcodesign_path()


def dylib() -> bytes:
    text = Sect("__TEXT", "__text", b"\xc0\x03\x5f\xd6" * 4)
    return build_macho([text], [Sym("_f")], filetype=MH_DYLIB, id_dylib="@rpath/patch-1.dylib")


@unittest.skipUnless(OPENSSL, "needs openssl")
class Identity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        self.data = self.tmp / "xtool-data"

    def test_every_key_format_becomes_pkcs8_pem(self):
        for fmt in ("pkcs8-pem", "pkcs1-pem", "pkcs1-der", "pkcs8-der"):
            with self.subTest(fmt):
                make_identity(self.data, key_format=fmt)
                ident = signing.load_identity("ABCDE12345", self.data)
                self.assertTrue(ident.key_pem.startswith(b"-----BEGIN PRIVATE KEY-----\n"))
                der = base64.b64decode(b"".join(ident.key_pem.splitlines()[1:-1]))
                self.assertEqual(signing.key_kind(der), "pkcs8")
                self.assertEqual(ident.certificate[0], 0x30)
                self.assertNotIn("PRIVATE", repr(ident))
                self.assertNotIn(str(ident.key_pem[30:60]), repr(ident))

    def test_unwrapped_key_and_pem_certificate(self):
        make_identity(self.data, key_format="pkcs1-der", wrap_key=False, cert_format="pem")
        ident = signing.load_identity(None, self.data)
        self.assertEqual(ident.team, "ABCDE12345")
        self.assertIn(b"-----BEGIN CERTIFICATE-----", ident.pem())

    def test_env_points_at_the_data_folder(self):
        make_identity(self.data)
        os.environ["IOSSTUDIO_XTOOL_DATA"] = str(self.data)
        self.assertEqual(signing.xtool_data_dir(), self.data)
        self.assertEqual(signing.teams(), ["ABCDE12345"])

    def test_not_signed_in(self):
        for setup in ("missing", "garbage", "incomplete", "bad-key", "bad-team"):
            with self.subTest(setup):
                d = self.tmp / setup
                d.mkdir()
                team = "ABCDE12345"
                if setup == "garbage":
                    (d / team).write_bytes(b"not a plist")
                elif setup == "incomplete":
                    import plistlib
                    (d / team).write_bytes(plistlib.dumps({"certificate": b"x"}, fmt=plistlib.FMT_BINARY))
                elif setup == "bad-key":
                    import plistlib
                    (d / team).write_bytes(plistlib.dumps({"privateKey": {"data": b"nope"}, "certificate": b"0\x00"},
                                                          fmt=plistlib.FMT_BINARY))
                elif setup == "bad-team":
                    team = "../etc"
                with self.assertRaises(HelperError) as cm:
                    signing.load_identity(team if setup != "missing" else None, d)
                self.assertEqual(cm.exception.code, "not-signed-in")
                self.assertEqual(cm.exception.fix["page"], "account")

    def test_der_helpers(self):
        with self.assertRaises(ValueError):
            signing.key_kind(b"\x04\x01\x00")
        with self.assertRaises(ValueError):
            signing.private_key_pem(b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nAAAA\n-----END ENCRYPTED PRIVATE KEY-----\n")
        long = signing._der(0x04, b"x" * 300)
        self.assertEqual(signing._der_header(long, 0), (0x04, 4, 304))
        self.assertEqual(signing._scrub("a -----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY----- b"),
                         "a [pem] b")

BID = "XTL-ABCDE12345.dev.example.app"   # the installed bundle id: the patch's code-signing identifier


@unittest.skipUnless(OPENSSL and RCODESIGN, "needs openssl and rcodesign")
class Sign(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = make_identity(self.tmp / "xtool-data", key_format="pkcs1-der")
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.path = self.work / "patch-1.dylib"
        self.path.write_bytes(dylib())

    def test_signs_without_writing_the_key(self):
        before = set(Path(tempfile.gettempdir()).rglob("*"))
        secs = signing.sign_dylib(self.path, "ABCDE12345", self.data, identifier=BID)
        self.assertLess(secs, signing.SIGN_TIMEOUT)
        self.assertTrue(macho.MachO.load(self.path).has_code_signature)
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["patch-1.dylib"])
        new = [p for p in set(Path(tempfile.gettempdir()).rglob("*")) - before if p.is_file()]
        key = signing.load_identity("ABCDE12345", self.data).key_pem
        for p in new:
            self.assertNotIn(key[40:120], p.read_bytes(), p)
        self.assertTrue(stat.S_ISREG(self.path.stat().st_mode))

    def test_rcodesign_failure(self):
        self.path.write_bytes(b"not a mach-o")
        with self.assertRaises(HelperError) as cm:
            signing.sign_dylib(self.path, "ABCDE12345", self.data, identifier=BID)
        self.assertEqual(cm.exception.code, "sign-failed")
        self.assertNotIn("PRIVATE KEY", cm.exception.detail or "")

    def test_missing_tool(self):
        with self.assertRaises(HelperError) as cm:
            signing.sign_dylib(self.path, "ABCDE12345", self.data, rcodesign=str(self.tmp / "no-rcodesign"),
                               identifier=BID)
        self.assertEqual(cm.exception.code, "missing-tool")


class FakeRcodesign(unittest.TestCase):
    """The key reaches rcodesign only on its stdin: a fake records what it got and where it was told to read."""

    def test_key_only_on_stdin(self):
        if not OPENSSL:
            self.skipTest("needs openssl")
        tmp = Path(tempfile.mkdtemp())
        data = make_identity(tmp / "d")
        fake = tmp / "rcodesign"
        fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$(dirname \"$0\")/argv\"\n"
                        "cat /dev/stdin > /dev/null && echo signed\n")
        fake.chmod(0o755)
        target = tmp / "x.dylib"
        target.write_bytes(b"x")
        signing.sign_dylib(target, None, data, rcodesign=str(fake), identifier=BID)
        argv = (tmp / "argv").read_text().split("\n")
        self.assertEqual(argv[:10], ["sign", "--pem-file", "/dev/stdin", "--team-name", "ABCDE12345",
                                     "--binary-identifier", BID, "--timestamp-url", "none", str(target)])
        shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
