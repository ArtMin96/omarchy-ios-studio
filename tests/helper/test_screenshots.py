"""Screenshot compare (helper/iosstudio/features/screenshots.py): the pure-Python PNG codec, diffs, the status-bar mask, regions,
storage (save/list/compare/delete, pruning, cleanup), matrix plans and manifests (the CLI and demo mode:
tests/cli/test_screenshots_cli.py).

Runs with the system python (no Pillow: the pure-Python codec). With Pillow importable (e.g. the MCP venv) the
Parity tests also check that both backends give the same numbers.

    python -m pytest tests/helper/test_screenshots.py
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iosstudio.core import appdirs, output
from iosstudio.features import screenshots as S
from support import ROOT

FIX = ROOT / "tests" / "fixtures" / "screenshots"


def pure():
    return mock.patch.dict(os.environ, {"IOSSTUDIO_SCREENSHOTS_PURE": "1"})


def img(name: str) -> S.Img:
    with pure():
        return S.load(FIX / name)


def has_pillow() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


class Codec(unittest.TestCase):
    def test_filters_and_rgba_decode_to_the_same_pixels(self):
        a, b = img("base.png"), img("same-rgba.png")
        self.assertEqual((a.w, a.h), (120, 260))
        self.assertEqual(a.rgb, b.rgb)

    def test_roundtrip(self):
        a = img("base.png")
        with tempfile.TemporaryDirectory() as t, pure():
            p = Path(t) / "x.png"
            S.save_png(p, a)
            self.assertEqual(S.load(p).rgb, a.rgb)
            self.assertEqual(S.image_size(p), (120, 260))
            self.assertEqual(list(Path(t).iterdir()), [p])     # no temp file left

    def test_gray_and_palette(self):
        import struct
        import zlib

        def png(ctype, rows, plte=b""):
            def chunk(k, b):
                return struct.pack(">I", len(b)) + k + b + struct.pack(">I", zlib.crc32(k + b) & 0xFFFFFFFF)
            raw = b"".join(b"\x00" + r for r in rows)
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, ctype, 0, 0, 0))
                    + (chunk(b"PLTE", plte) if plte else b"") + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        p = Path("gray.png")
        self.assertEqual(S._png_decode(png(0, [b"\x10\x20"]), p).rgb, b"\x10\x10\x10\x20\x20\x20")
        self.assertEqual(S._png_decode(png(3, [b"\x01\x00"], b"\x01\x02\x03\x0a\x0b\x0c"), p).rgb,
                         b"\x0a\x0b\x0c\x01\x02\x03")

    def test_bad_images(self):
        with self.assertRaises(output.HelperError) as cm:
            S._png_decode(b"GIF89a....", Path("x.gif"))
        self.assertEqual(cm.exception.code, "bad-image")
        bad = bytearray((FIX / "base.png").read_bytes())
        bad[24] = 1        # bit depth 1
        with self.assertRaises(output.HelperError):
            S._png_decode(bytes(bad), Path("x.png"))


class Compare(unittest.TestCase):
    def run_cmp(self, other: str, **kw):
        with pure():
            return S.compare_images(img("base.png"), img(other), **kw)

    def test_identical_is_zero(self):
        r = self.run_cmp("same-rgba.png")
        self.assertEqual((r["verdict"], r["diff_percent"], r["regions"]), ("same", 0.0, []))
        self.assertEqual(r["masked_top"], 54)

    def test_changed_label_gives_one_region(self):
        r = self.run_cmp("label.png")
        self.assertEqual(r["verdict"], "changed")
        self.assertEqual(len(r["regions"]), 1)
        reg = r["regions"][0]
        self.assertEqual((reg["x"], reg["y"], reg["w"], reg["h"]), (61, 120, 30, 12))
        self.assertEqual(reg["pixels"], 30 * 12)
        self.assertAlmostEqual(r["diff_percent"], round(100 * 360 / (120 * 260 - 120 * 54), 3))

    def test_status_bar_is_masked(self):
        self.assertEqual(self.run_cmp("status.png")["diff_percent"], 0.0)
        r = self.run_cmp("status.png", mask_top=0)
        self.assertEqual(r["verdict"], "changed")
        self.assertEqual(r["regions"][0]["y"], 8)

    def test_extra_mask(self):
        r = self.run_cmp("label.png", masks=[{"x": 60, "y": 110, "w": 40, "h": 30}])
        self.assertEqual((r["verdict"], r["changed_pixels"]), ("same", 0))

    def test_noise_under_tolerance(self):
        self.assertEqual(self.run_cmp("noise.png")["verdict"], "same")
        self.assertEqual(self.run_cmp("noise.png", tolerance=2)["verdict"], "changed")

    def test_threshold(self):
        r = self.run_cmp("label.png", threshold=50)
        self.assertEqual(r["verdict"], "same")
        self.assertTrue(r["regions"])          # the region is still reported

    def test_size_mismatch(self):
        r = self.run_cmp("wide.png")
        self.assertEqual(r["verdict"], "size-mismatch")
        self.assertIsNone(r["diff_percent"])
        self.assertEqual((r["baseline_width"], r["width"]), (120, 130))

    def test_many_regions_are_capped(self):
        a = img("base.png")
        b = bytearray(a.rgb)
        n = 0
        for y in range(60, 256, 32):         # 2x2 dots more than two grid cells apart: one region each
            for x in range(2, 118, 32):
                for dy in (0, 1):
                    for dx in (0, 1):
                        i = ((y + dy) * a.w + x + dx) * 3
                        b[i:i + 3] = bytes(255 - v for v in b[i:i + 3])
                n += 1
        with pure(), mock.patch.object(S, "MAX_REGIONS", 10):
            r = S.compare_images(a, S.Img(a.w, a.h, bytes(b)))
        self.assertEqual(r["regions_total"], n)
        self.assertEqual(len(r["regions"]), 10)
        self.assertTrue(all(x["w"] == 2 and x["h"] == 2 for x in r["regions"]))

    def test_status_bar_heights(self):
        self.assertEqual(S.status_bar_px(1206, 2622), 162)     # iPhone 17 Pro: 54 pt x 3
        self.assertEqual(S.status_bar_px(1179, 2556), 162)
        self.assertEqual(S.status_bar_px(750, 1334), 40)       # SE: 20 pt x 2
        self.assertEqual(S.status_bar_px(390, 844), 54)        # demo art
        self.assertEqual(S.status_bar_px(2622, 1206), 0)       # landscape

    def test_diff_image(self):
        with pure():
            cur = img("label.png")
            r = S.compare_images(img("base.png"), cur)
            d = S.render_diff(cur, r)
        px = lambda x, y: d.rgb[(y * d.w + x) * 3:(y * d.w + x) * 3 + 3]  # noqa: E731
        self.assertEqual(px(75, 125), bytes(S.PINK))           # changed pixel
        self.assertEqual(px(61 - 3, 125), bytes(S.YELLOW))     # region outline (3 px padding)
        self.assertEqual(px(5, 150), bytes(cur.rgb[(150 * cur.w + 5) * 3:][:3]).translate(S.DIM))   # dimmed


@unittest.skipUnless(has_pillow(), "Pillow not installed")
class Parity(unittest.TestCase):
    def test_backends_agree(self):
        for other in ("label.png", "status.png", "noise.png", "same-rgba.png"):
            for kw in ({}, {"mask_top": 0}, {"tolerance": 2}):
                with pure():
                    a = S.compare_images(img("base.png"), img(other), **kw)
                    da = S.render_diff(img(other), a)
                b = S.compare_images(S.load(FIX / "base.png"), S.load(FIX / other), **kw)
                self.assertEqual(b["backend"], "pillow")
                for k in ("verdict", "diff_percent", "changed_pixels", "regions", "masked_top"):
                    self.assertEqual(a[k], b[k], (other, kw, k))
                db = S.render_diff(S.load(FIX / other), b)
                self.assertEqual(da.rgb, db.rgb, (other, kw))


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.proj = t / "MyApp"
        self.proj.mkdir()
        self.patches = [mock.patch.object(appdirs, "DATA_DIR", t / "data"), pure(),
                        mock.patch.dict(os.environ, {"IOSSTUDIO_DEMO": ""})]
        for p in self.patches:
            p.start()
        self.d = S.root() / S.project_key(str(self.proj))

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def cli(self, action, **flags):
        import contextlib
        import io
        buf = io.StringIO()
        f = {"project": str(self.proj), **{k: v for k, v in flags.items() if v is not None}}
        with contextlib.redirect_stdout(buf):
            try:
                S.GROUPS["screenshots"][action](f)
            except output.HelperError as exc:
                output.fail(exc.code, exc.message)
        return json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_save_list_compare_delete(self):
        self.assertEqual(self.cli("list")["data"]["baselines"], [])
        r = self.cli("save", name="Home screen!", shot=str(FIX / "base.png"))["data"]
        self.assertEqual((r["name"], r["width"], r["height"], r["replaced"]), ("Home-screen", 120, 260, False))
        self.assertTrue(Path(r["baseline_path"]).is_file())
        lst = self.cli("list")["data"]["baselines"]
        self.assertEqual([b["name"] for b in lst], ["Home-screen"])
        self.assertEqual((lst[0]["width"], lst[0]["height"]), (120, 260))
        c = self.cli("compare", name="home-screen", shot=str(FIX / "label.png"))["data"]
        self.assertEqual((c["verdict"], c["name"]), ("changed", "Home-screen"))
        for k in ("baseline_path", "current_path", "diff_path"):
            self.assertTrue(os.path.isabs(c[k]) and Path(c[k]).is_file(), k)
        self.assertEqual(c["regions"][0]["w"], 30)
        self.assertEqual(self.cli("compare", name="Nope", shot=str(FIX / "base.png"))["data"]["verdict"], "no-baseline")
        dry = self.cli("delete", name="Home-screen")["data"]
        self.assertEqual((dry["deleted"], dry["dryRun"]), (False, True))
        self.assertTrue(Path(dry["baseline_path"]).is_file())
        gone = self.cli("delete", name="Home-screen", confirm=True)["data"]
        self.assertTrue(gone["deleted"])
        self.assertFalse((self.d / "baselines").exists())               # image and meta both gone (and the empty folder)
        self.assertEqual(self.cli("delete", name="Home-screen")["error"]["code"], "no-baseline")

    def test_delete_takes_its_compare_runs(self):
        self.cli("save", name="Home", shot=str(FIX / "base.png"))
        self.cli("save", name="Home-dark", shot=str(FIX / "base.png"))
        for n in ("Home", "home", "Home-dark"):
            self.cli("compare", name=n, shot=str(FIX / "label.png"))
        (self.d / "compare" / "20200101-000000-000-HOME").mkdir()          # an older spelling of the name
        dry = self.cli("delete", name="home")["data"]
        self.assertEqual((dry["name"], dry["compare_runs"], dry["dryRun"], dry["project_dir_removed"]),
                         ("Home", 3, True, False))
        self.assertGreater(dry["compare_bytes"], 0)
        self.assertEqual(len(list((self.d / "compare").iterdir())), 4)       # dry run: nothing gone
        done = self.cli("delete", name="Home", confirm=True)["data"]
        self.assertEqual((done["deleted"], done["compare_runs"], done["project_dir_removed"]), (True, 3, False))
        left = [p.name for p in (self.d / "compare").iterdir()]
        self.assertEqual(len(left), 1)
        self.assertTrue(left[0].endswith("-Home-dark"))                     # another screen's run stays
        done = self.cli("delete", name="Home-dark", confirm=True)["data"]
        self.assertTrue(done["project_dir_removed"])                        # nothing left: folder gone
        self.assertFalse(self.d.exists())
        self.assertEqual(self.cli("list")["data"]["baselines"], [])

    def test_delete_keeps_folder_with_matrix_sets(self):
        self.cli("save", name="Home", shot=str(FIX / "base.png"))
        S.plan_matrix(str(self.proj), self.d, None, None)
        self.assertFalse(self.cli("delete", name="Home", confirm=True)["data"]["project_dir_removed"])
        self.assertTrue((self.d / "matrix").is_dir())
        self.assertFalse((self.d / "baselines").exists())

    def test_errors(self):
        self.assertEqual(self.cli("save", name="!!", shot=str(FIX / "base.png"))["error"]["code"], "invalid-name")
        self.assertEqual(self.cli("save", name="a", shot="/nope.png")["error"]["code"], "no-shot")
        self.assertEqual(self.cli("compare", name="a", shot=str(FIX / "base.png"), threshold="x")["error"]["code"],
                         "usage")
        self.proj.rmdir()
        self.assertEqual(self.cli("list")["error"]["code"], "not-found")

    def test_failed_compare_leaves_nothing(self):
        self.cli("save", name="Home", shot=str(FIX / "base.png"))
        with mock.patch.object(S, "render_diff", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                S.compare_shot(self.d, "Home", FIX / "label.png")
        self.assertEqual(list((self.d / "compare").glob("*")), [])

    def test_compare_runs_are_pruned(self):
        self.cli("save", name="Home", shot=str(FIX / "base.png"))
        with mock.patch.object(S, "KEEP_COMPARES", 4):
            for i in range(7):
                r = S.compare_shot(self.d, "Home", FIX / "same-rgba.png")
                os.utime(Path(r["current_path"]).parent, (1000 + i, 1000 + i))   # distinct ages
        runs = list((self.d / "compare").iterdir())
        self.assertEqual(len(runs), 4)
        self.assertTrue(Path(r["current_path"]).is_file())                      # the newest stays

    def test_forget_project(self):
        self.cli("save", name="Home", shot=str(FIX / "base.png"))
        self.assertEqual(S.forget_project(str(self.proj)), 1)
        self.assertFalse(self.d.exists())
        self.assertEqual(S.forget_project(str(self.proj)), 0)

    def test_projects_remove_deletes_screenshots(self):
        import contextlib
        import io

        from iosstudio.features.projects import commands, store
        self.cli("save", name="Home", shot=str(FIX / "base.png"))
        state = Path(self.tmp.name) / "state"
        with mock.patch.object(appdirs, "STATE_DIR", state), \
                mock.patch.object(store, "PROJECTS_FILE", state / "projects.json"), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            commands.cmd_remove({"path": str(self.proj)})
        out = json.loads(buf.getvalue())["data"]
        self.assertEqual((out["screenshotsDeleted"], out["filesDeleted"]), (1, False))
        self.assertFalse(self.d.exists())
        self.assertTrue(self.proj.is_dir())

    def test_project_keys(self):
        self.assertNotEqual(S.project_key("/a/App"), S.project_key("/b/App"))
        self.assertTrue(S.project_key("/a/My App").startswith("My-App-"))
        self.assertEqual(S.slug(" settings / dark "), "settings-dark")

    def test_matrix_plan_and_save(self):
        presets = [{"name": "German", "args": ["-X"], "env": {}, "language": "de", "region": "de_DE",
                    "uiTestMode": False}]
        with mock.patch.object(S, "_project_presets", return_value=presets):
            plan = S.plan_matrix(str(self.proj), self.d, ["German", {"language": "fr"}, {"name": "German"}],
                                 ["Home", {"name": "Settings", "press": "Settings"}], "com.x.app")
        self.assertEqual([c["key"] for c in plan["configs"]], ["German", "fr", "German-2"])
        self.assertEqual(plan["configs"][0]["args"], ["-X", "-AppleLanguages", "(de)", "-AppleLocale", "de_DE"])
        self.assertEqual(plan["configs"][1]["args"], ["-AppleLanguages", "(fr)"])
        self.assertEqual(plan["names"], [{"name": "Home", "press": None, "wait": 0.0},
                                         {"name": "Settings", "press": "Settings", "wait": 1.5}])
        a = S.matrix_save(self.d, plan["set_id"], "German", "Home", FIX / "base.png")
        b = S.matrix_save(self.d, plan["set_id"], "fr", "Home", None, "Launch failed")
        self.assertTrue(Path(a["path"]).is_file())
        self.assertEqual(Path(a["path"]).relative_to(plan["set_dir"]).as_posix(), "German/Home.png")
        man = json.loads(Path(plan["manifest_path"]).read_text())
        self.assertEqual(len(man["shots"]), 2)
        self.assertEqual(man["shots"][1]["error"], "Launch failed")
        self.assertEqual(b["count"], 2)
        with self.assertRaises(output.HelperError) as cm:
            S.plan_configs(str(self.proj), ["Nope"])
        self.assertEqual(cm.exception.code, "unknown-preset")
        with self.assertRaises(output.HelperError):
            S.plan_names(["a", "A"])
        self.assertEqual(man["state"], "running")
        fin = S.matrix_finish(self.d, plan["set_id"], "cancelled", None, "stopped (it wasn't running before)")
        man = json.loads(Path(plan["manifest_path"]).read_text())
        self.assertEqual((man["state"], man["app_restored"], man["expected"], man["captured"], man["failed"],
                          man["complete"]), ("cancelled", "stopped (it wasn't running before)", 6, 1, 1, False))
        self.assertIsNone(man["error"])
        self.assertGreater(man["finished"], 0)
        self.assertEqual(fin["state"], "cancelled")
        S.matrix_finish(self.d, plan["set_id"], "failed", "Launch failed: locked.")
        self.assertEqual(json.loads(Path(plan["manifest_path"]).read_text())["error"], "Launch failed: locked.")
        with self.assertRaises(output.HelperError):
            S.matrix_finish(self.d, plan["set_id"], "running")
        self.assertEqual(S.plan_configs(str(self.proj), None)[0]["key"], "default")
        self.assertEqual(S.plan_names(None)[0]["name"], "launch")

    def test_matrix_sets_are_pruned(self):
        with mock.patch.object(S, "KEEP_SETS", 4):
            for i in range(7):
                plan = S.plan_matrix(str(self.proj), self.d, None, None)
                os.utime(plan["set_dir"], (1000 + i, 1000 + i))
        self.assertLessEqual(len(list((self.d / "matrix").iterdir())), 4)


if __name__ == "__main__":
    unittest.main()
