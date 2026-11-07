"""Run presets (helper/run_presets.py and `projects presets` / `build run --preset` in helper/projects.py):
argument building, validation and the list logic. Storage, the fail-fast check before a build and demo mode
through the helper are in tests/cli/test_run_presets_cli.py. Stdlib only:
    python -m pytest tests/helper/test_run_presets.py
"""
from __future__ import annotations

import json
import unittest

import run_presets as R
from common import HelperError

GERMAN = {"name": "German", "language": "de", "region": "de_DE"}
DEBUG = {"name": "Debug flags", "args": ["-ShowDebugMenu", "YES"], "env": {"LOG_LEVEL": "debug"}}


def ok(raw):
    return R.validate(R.normalize(raw))


class Arguments(unittest.TestCase):
    def test_german_language_and_region(self):
        self.assertEqual(R.resolve(GERMAN), {"args": ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"], "env": {}})

    def test_user_args_first_then_generated(self):
        r = R.resolve({"name": "All", "args": ["-ShowDebugMenu", "YES"], "env": {"LOG_LEVEL": "debug"},
                       "language": "pt-BR", "region": "pt_BR", "uiTestMode": True})
        self.assertEqual(r["args"], ["-ShowDebugMenu", "YES", "-AppleLanguages", "(pt-BR)", "-AppleLocale", "pt_BR",
                                     "-UITestMode", "YES"])
        self.assertEqual(r["env"], {"LOG_LEVEL": "debug"})

    def test_empty_preset_resolves_to_nothing(self):
        self.assertEqual(R.resolve({"name": "Plain"}), {"args": [], "env": {}})


class Validation(unittest.TestCase):
    def test_normalize_fills_defaults_and_fixes_spelling(self):
        p = R.normalize({"name": "  German ", "language": "PT_br", "region": "de-de", "uiTestMode": "yes",
                         "env": {"N": 1}, "args": "-X"})
        self.assertEqual(p, {"name": "German", "args": ["-X"], "env": {"N": "1"}, "language": "pt-BR",
                             "region": "de_DE", "uiTestMode": True})
        self.assertEqual(R.normalize({"name": "A"}), {**R.default(), "name": "A"})
        self.assertEqual(R.normalize({"name": "Z", "language": "zh-hans"})["language"], "zh-Hans")

    def test_valid_presets(self):
        for raw in (GERMAN, DEBUG, {"name": "x" * 40}, {"name": "LatAm", "language": "es-419", "region": "es_419"},
                    {"name": "Hans", "language": "zh-Hans", "region": "zh_Hans_CN"}):
            with self.subTest(raw=raw):
                ok(raw)

    def test_invalid_presets(self):
        for raw in ({}, {"name": "   "}, {"name": "x" * 41}, {"name": "a\nb"},
                    {"name": "A", "env": {"1BAD": "x"}}, {"name": "A", "env": {"A-B": "x"}},
                    {"name": "A", "env": {"A": "x" * 5000}}, {"name": "A", "args": ["x"] * 65},
                    {"name": "A", "args": ["x" * 1001]}, {"name": "A", "language": "german"},
                    {"name": "A", "language": "d"}, {"name": "A", "region": "Germany"},
                    {"name": "A", "region": "de_DE_x"}):
            with self.subTest(raw=raw), self.assertRaises(HelperError) as cm:
                ok(raw)
            self.assertEqual(cm.exception.code, "invalid-preset")

    def test_env_name_message(self):
        with self.assertRaises(HelperError) as cm:
            ok({"name": "A", "env": {"1BAD": "x"}})
        self.assertIn("'1BAD' isn't a valid environment variable name", cm.exception.message)


class ListLogic(unittest.TestCase):
    def setUp(self):
        self.presets = [ok(GERMAN), ok(DEBUG)]

    def test_unknown_preset_lists_names(self):
        with self.assertRaises(HelperError) as cm:
            R.find(self.presets, "French")
        self.assertEqual(cm.exception.code, "unknown-preset")
        self.assertEqual(cm.exception.message, "No run preset named 'French'. This project has: German, Debug flags.")
        with self.assertRaises(HelperError) as cm:
            R.find([], "French")
        self.assertEqual(cm.exception.message, "No run preset named 'French'. This project has no run presets yet.")

    def test_find_is_case_insensitive(self):
        self.assertEqual(R.find(self.presets, "  german ")["name"], "German")

    def test_save_replaces_by_name_and_renames(self):
        out, p = R.save(self.presets, {"name": "GERMAN", "language": "de"})
        self.assertEqual([x["name"] for x in out], ["GERMAN", "Debug flags"])
        self.assertIsNone(out[0]["region"])
        out, p = R.save(self.presets, {"name": "Deutsch", "language": "de"}, rename_from="german")
        self.assertEqual([x["name"] for x in out], ["Deutsch", "Debug flags"])
        with self.assertRaises(HelperError) as cm:      # renaming onto another preset's name
            R.save(self.presets, {"name": "debug FLAGS"}, rename_from="German")
        self.assertEqual(cm.exception.code, "invalid-preset")
        with self.assertRaises(HelperError) as cm:
            R.save(self.presets, {"name": "X"}, rename_from="Nope")
        self.assertEqual(cm.exception.code, "unknown-preset")

    def test_act_select_rename_and_delete_follow_the_selection(self):
        presets, sel, extra, changed = R.act(self.presets, None, {"select": "german"})
        self.assertEqual((sel, changed), ("German", True))
        presets, sel, extra, changed = R.act(presets, sel, {"save": json.dumps({"name": "Deutsch", "language": "de"}),
                                                            "rename_from": "German"})
        self.assertEqual((sel, extra), ("Deutsch", {"saved": "Deutsch"}))
        _p, sel2, extra, changed = R.act(presets, sel, {"delete": "Deutsch", "dry_run": True})
        self.assertEqual((sel2, extra, changed), ("Deutsch", {"deleted": "Deutsch", "dryRun": True}, False))
        presets, sel, extra, changed = R.act(presets, sel, {"delete": "Deutsch"})
        self.assertEqual((sel, [p["name"] for p in presets], changed), (None, ["Debug flags"], True))
        with self.assertRaises(HelperError):
            R.act(presets, sel, {"select": "x", "delete": "y"})

    def test_stored_reads_old_and_messy_meta(self):
        self.assertEqual(R.stored({}), [])
        self.assertEqual(R.stored(None), [])
        self.assertEqual(R.stored({"autoRenew": True}), [])
        self.assertIsNone(R.selected({"autoRenew": True}))
        messy = {"runPresets": [GERMAN, {"name": "german"}, {"name": ""}, "junk", {"name": "Bad", "language": "x y"},
                                {"name": "Partial"}], "runPreset": "Gone"}
        self.assertEqual([p["name"] for p in R.stored(messy)], ["German", "Partial"])
        self.assertIsNone(R.selected(messy))
        self.assertEqual(R.selected({**messy, "runPreset": "partial"}), "Partial")

    def test_store_drops_empty_keys(self):
        meta = {"autoRenew": True}
        R.store(meta, self.presets, "german")
        self.assertEqual(meta["runPreset"], "German")
        self.assertEqual(meta["runPresets"][1], {"name": "Debug flags", "args": ["-ShowDebugMenu", "YES"],
                                                 "env": {"LOG_LEVEL": "debug"}, "language": None, "region": None,
                                                 "uiTestMode": False})
        R.store(meta, [], None)
        self.assertEqual(meta, {"autoRenew": True})


if __name__ == "__main__":
    unittest.main()
