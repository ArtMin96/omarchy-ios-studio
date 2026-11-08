"""Apple's HTTP 409 "no current iOS devices on this team" (an iPhone xtool never registered) is a signing
problem with a USB hint, not an xtool.yml one.

build-output/signing/real-unregistered-device.txt is real `xtool dev build --sign` output from an iPhone that was
only ever connected over Wi-Fi. It sits in a subfolder because tests/mcp/test_mcp_diagnostics.py puts one group of
every top-level fixture on a single page and checks it stays under the default output budget.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iosstudio.core import output
from iosstudio.features import diagnostics as D
from iosstudio.features.projects import runner as build_runner
from support import ROOT

FIXTURE = ROOT / "tests" / "fixtures" / "build-output" / "signing" / "real-unregistered-device.txt"
USB_HINT = "Connect the iPhone with a USB cable once so xtool can register it, then build again."


class UnregisteredDevice(unittest.TestCase):
    def test_classified_as_signing_with_usb_hint(self):
        (d,) = D.parse(FIXTURE.read_text().splitlines(), demangler=None)
        self.assertEqual(d["category"], "signing")
        self.assertEqual(d["hint"], USB_HINT)

    def test_message_keeps_apples_explanation(self):
        (d,) = D.parse(FIXTURE.read_text().splitlines(), demangler=None)
        self.assertEqual(d["message"], "Apple refused ProfilesCreateInstance (409 ENTITY_ERROR): There are no current "
                                       "IOS devices on this team matching the provided device IDs.")

    def test_rule_wins_over_config_words(self):
        for text in ("There are no current IOS devices on this team",
                     'DeveloperAPI.Components.Schemas.ErrorResponse(status: "409", code: "ENTITY_ERROR")',
                     "response: conflict(DeveloperAPI.Operations.ProfilesCreateInstance.Output.Conflict(…"):
            self.assertEqual(D.classify(text), ("signing", USB_HINT), text)

    def test_schemas_alone_is_not_xtool_yml(self):
        self.assertNotEqual(D.classify("DeveloperAPI.Components.Schemas.Something failed")[0], "config")
        self.assertEqual(D.classify("unknown key in schema")[0], "config")

    def test_provisioning_profile_errors_are_signing(self):
        self.assertEqual(D.classify("Failed to create provisioning profiles")[0], "signing")
        self.assertEqual(D.classify("No profiles for 'com.x.app' were found")[0], "signing")

    def test_unrelated_unexpected_response_is_untouched(self):
        self.assertEqual(D._clean("Unexpected response, expected status code: ok"),
                         "Unexpected response, expected status code: ok")

    def test_runner_failure_message(self):
        events: list[dict] = []
        old = output._write
        output._write = events.append
        try:
            runner = build_runner.BuildRunner(["resolve", "compile", "link", "sign"], False,
                                              Path(tempfile.mkdtemp()) / "b.log", None)
            for ln in FIXTURE.read_text().splitlines():
                runner.on_line(ln)
            runner.flush_diags(failed=True)
            err = build_runner.classify_failure(runner, 1)
            runner.finish_all(False)
            runner.close()
        finally:
            output._write = old
        self.assertEqual(err.code, "signing-failed")
        self.assertIn("USB cable", err.message)
        diags = [e for e in events if e.get("event") == "diagnostic"]
        self.assertEqual([d["category"] for d in diags], ["signing"])


if __name__ == "__main__":
    unittest.main()
