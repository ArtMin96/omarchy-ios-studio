"""`projects icon` and `projects pick-file` through bin/iosstudio-helper in isolated folders.

A fake magick (tests/support/fake_magick.py) checks the ImageMagick command line at the process boundary; the real
ImageMagick and whatever backend this machine has (Pillow in-process, the daemon venv's Pillow, ImageMagick) render
the fixture pictures for real when installed. Demo mode answers the same shapes and writes nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from iosstudio.features import app_icon as A
from support import ROOT, helper_json, isolated_env
from support.fake_magick import write_fake_magick

FIX = ROOT / "tests" / "fixtures" / "app-icon"
LOGO, PHOTO = FIX / "logo-transparent.png", FIX / "photo.jpg"
HAS_MAGICK = bool(shutil.which("magick") or (shutil.which("convert") and shutil.which("identify")))
YML = "# GlowApp: built with xtool\nversion: 1   # schema\n\nbundleID: com.example.Glow  # keep\nproduct: Glow\n"


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.log = self.dir / "magick.log"
        (self.dir / "bin").mkdir()
        self.fake = write_fake_magick(self.dir / "bin" / "magick")
        self.root = self.dir / "Glow"
        self.root.mkdir()
        (self.root / "Package.swift").write_text("// swift-tools-version: 6.0\n")
        (self.root / "xtool.yml").write_text(YML)

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra: str) -> dict:
        return isolated_env(self.dir, **{"IOSSTUDIO_ICON_BACKEND": "imagemagick", "IOSSTUDIO_MAGICK": str(self.fake),
                                         "FAKE_MAGICK_LOG": str(self.log),
                                         "IOSSTUDIO_RUNTIME_DIR": str(self.dir / "run"), **extra})

    def icon(self, *args: str, env: dict | None = None) -> dict:
        return helper_json("projects", "icon", "--path", str(self.root), *args, env=env or self.env())

    def calls(self) -> list[list[str]]:
        return [json.loads(ln) for ln in self.log.read_text().splitlines()] if self.log.exists() else []

    # ---------------------------------------------------------------- the fake magick

    def test_dry_run_writes_nothing_to_the_project(self):
        before = snapshot(self.root)
        out = self.icon("--image", str(LOGO))
        self.assertTrue(out["ok"], out)
        d = out["data"]
        self.assertEqual(set(d), {"name", "path", "source", "fit", "background", "target", "targetPath", "replaces",
                                  "yml", "previous", "preview", "warnings", "backend", "applied", "check"})
        self.assertEqual((d["name"], d["fit"], d["background"], d["target"], d["yml"], d["previous"], d["replaces"],
                          d["applied"], d["backend"]),
                         ("Glow", "fit", "#ffffff", "Resources/AppIcon.png", "add", None, False, False, "imagemagick"))
        self.assertEqual(d["source"], {"path": str(LOGO), "width": 96, "height": 96, "format": "PNG", "hasAlpha": True})
        self.assertEqual(d["targetPath"], str(self.root / "Resources/AppIcon.png"))
        self.assertEqual([i["check"] for i in d["check"]], ["icon"])     # today: no icon yet
        self.assertEqual(d["check"][0]["fix"]["action"], "icon")
        self.assertTrue(Path(d["preview"]).is_file())
        self.assertEqual(Path(d["preview"]).parent, self.dir / "xdg_cache_home/ios-studio/icons")
        self.assertIsNone(A.valid_icon(Path(d["preview"])))
        self.assertEqual(snapshot(self.root), before)
        identify, convert = self.calls()
        self.assertEqual(identify[:3], ["identify", "-format", A.IDENTIFY_FORMAT])
        self.assertEqual(identify[-1], f"{LOGO}[0]")
        self.assertEqual(convert[0], f"{LOGO}[0]")
        self.assertIn("819x819", convert)                                   # fit: 10 % margin
        self.assertEqual(convert[convert.index("-background") + 1], "#ffffff")
        self.assertTrue(convert[-1].startswith("PNG24:" + str(self.dir / "xdg_cache_home/ios-studio/icons/.")))

    def test_confirm_writes_the_icon_and_keeps_xtool_yml(self):
        out = self.icon("--image", str(PHOTO), "--background", "#123ABC", "--confirm")
        self.assertTrue(out["ok"], out)
        d = out["data"]
        self.assertEqual((d["fit"], d["background"], d["applied"], d["check"]), ("fill", "#123abc", True, []))
        self.assertEqual((self.root / "xtool.yml").read_text(), YML + "iconPath: Resources/AppIcon.png\n")
        self.assertIsNone(A.valid_icon(self.root / "Resources/AppIcon.png"))
        convert = self.calls()[-1]
        self.assertIn("1024x1024^", convert)
        self.assertEqual(convert[convert.index("-background") + 1], "#123abc")
        check = helper_json("projects", "check", "--path", str(self.root), env=self.env())
        self.assertEqual([i for i in check["data"]["issues"] if i["check"] == "icon"], [])
        again = self.icon("--image", str(PHOTO), "--background", "#123abc", "--fit", "fill", "--confirm")
        self.assertEqual((again["data"]["yml"], again["data"]["replaces"]), ("keep", True))
        self.assertEqual(len(self.calls()), 2 + 1)                         # same picture + options: cached preview

    def test_existing_icon_path_is_reused(self):
        yml = YML + "iconPath: Art/Icon.png   # the icon\n"
        (self.root / "xtool.yml").write_text(yml)
        (self.root / "Art").mkdir()
        (self.root / "Art/Icon.png").write_bytes(b"old")
        out = self.icon("--image", str(PHOTO), "--confirm")
        d = out["data"]
        self.assertEqual((d["target"], d["yml"], d["previous"], d["replaces"], d["check"]),
                         ("Art/Icon.png", "keep", "Art/Icon.png", True, []))
        self.assertEqual((self.root / "xtool.yml").read_text(), yml)
        self.assertIsNone(A.valid_icon(self.root / "Art/Icon.png"))
        self.assertFalse((self.root / "Resources").exists())

    def test_a_jpg_icon_path_is_changed(self):
        (self.root / "xtool.yml").write_text("version: 1\niconPath: icon.jpg  # old\nproduct: Glow\n")
        d = self.icon("--image", str(PHOTO), "--confirm")["data"]
        self.assertEqual((d["yml"], d["previous"]), ("change", "icon.jpg"))
        self.assertEqual((self.root / "xtool.yml").read_text(),
                         "version: 1\niconPath: Resources/AppIcon.png  # old\nproduct: Glow\n")

    def test_generated_yml_is_refused(self):
        (self.root / "xtool.yml").write_text("# GENERATED by make.sh\nversion: 1\nbundleID: com.example.Glow\n")
        before = snapshot(self.root)
        for extra in ((), ("--confirm",)):
            out = self.icon("--image", str(PHOTO), *extra)
            self.assertEqual(out["error"]["code"], "generated-yml")
            self.assertTrue(out["error"]["fix"])
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.calls(), [])

    def test_a_bad_render_leaves_the_project_alone(self):
        (self.root / "Resources").mkdir()
        (self.root / "Resources/AppIcon.png").write_bytes(b"old")
        (self.root / "xtool.yml").write_text(YML + "iconPath: Resources/AppIcon.png\n")
        before = snapshot(self.root)
        for mode in ("bad", "fail", "unreadable"):
            out = self.icon("--image", str(PHOTO), "--confirm", env=self.env(FAKE_MAGICK_MODE=mode))
            self.assertEqual(out["error"]["code"], "bad-image", mode)
            self.assertNotIn("terminal", out["error"]["message"])
            self.assertEqual(snapshot(self.root), before, mode)
        self.assertEqual(list((self.dir / "xdg_cache_home/ios-studio/icons").glob("*.png")), [])

    def test_errors(self):
        cases = [((), "usage"), (("--image", str(self.dir / "nope.png")), "no-image"),
                 (("--image", str(PHOTO), "--background", "teal"), "bad-color"),
                 (("--image", str(PHOTO), "--fit", "stretch"), "usage")]
        for args, code in cases:
            out = self.icon(*args)
            self.assertEqual(out["error"]["code"], code, args)
            self.assertTrue(out["error"]["message"])
        self.assertEqual(self.icon("--image", str(self.dir / "nope.png"))["error"]["fix"]["action"], "icon")
        out = self.icon("--image", str(PHOTO), env=self.env(IOSSTUDIO_MAGICK=str(self.dir / "none")))
        self.assertEqual(out["error"]["code"], "no-image-tool")
        self.assertEqual(out["error"]["fix"]["page"], "setup")
        out = helper_json("projects", "icon", "--path", str(self.dir), "--image", str(PHOTO), env=self.env())
        self.assertEqual(out["error"]["code"], "not-a-project")

    def test_previews_are_pruned(self):
        d = self.dir / "xdg_cache_home/ios-studio/icons"
        d.mkdir(parents=True)
        now = time.time()
        for i in range(25):
            f = d / f"old{i:02}.png"
            f.write_bytes(b"x")
            os.utime(f, (now - 1000 + i, now - 1000 + i))
        stale = d / "stale.png"
        stale.write_bytes(b"x")
        os.utime(stale, (now - 8 * 86400, now - 8 * 86400))
        out = self.icon("--image", str(PHOTO))
        left = list(d.glob("*.png"))
        self.assertEqual(len(left), A.KEEP_PREVIEWS)
        self.assertIn(Path(out["data"]["preview"]), left)
        self.assertFalse(stale.exists())

    # ---------------------------------------------------------------- real backends

    @unittest.skipUnless(HAS_MAGICK, "ImageMagick isn't installed")
    def test_real_imagemagick(self):
        env = self.env()
        env.pop("IOSSTUDIO_MAGICK")
        self.real_renders(env, "imagemagick")

    def test_whatever_backend_this_machine_has(self):
        """Unpinned: Pillow, the daemon venv's Pillow (re-exec) or ImageMagick, whichever is here first."""
        env = self.env()
        env.pop("IOSSTUDIO_MAGICK")
        env.pop("IOSSTUDIO_ICON_BACKEND")
        probe = self.icon("--image", str(PHOTO), env=env)
        if not probe["ok"] and probe["error"]["code"] == "no-image-tool":
            self.skipTest("no image backend on this machine")
        self.real_renders(env, probe["data"]["backend"])

    def real_renders(self, env: dict, backend: str) -> None:
        for img, fit, alpha in ((LOGO, "fit", True), (PHOTO, "fill", False)):
            (self.root / "xtool.yml").write_text(YML)
            out = self.icon("--image", str(img), "--confirm", env=env)
            self.assertTrue(out["ok"], out)
            d = out["data"]
            self.assertEqual((d["backend"], d["fit"], d["source"]["hasAlpha"], d["check"]), (backend, fit, alpha, []))
            self.assertIsNone(A.valid_icon(self.root / "Resources/AppIcon.png"), img)
            self.assertEqual((self.root / "xtool.yml").read_text(), YML + "iconPath: Resources/AppIcon.png\n")
            check = helper_json("projects", "check", "--path", str(self.root), env=env)
            self.assertEqual([i for i in check["data"]["issues"] if i["check"] == "icon"], [])

    # ---------------------------------------------------------------- demo mode

    def test_demo(self):
        env = self.env(IOSSTUDIO_DEMO="1", XDG_RUNTIME_DIR=str(self.dir / "xdg_runtime"))
        picked = helper_json("projects", "pick-file", env=env)
        self.assertTrue(picked["ok"], picked)
        self.assertTrue(Path(picked["data"]["path"]).is_file())
        home = str(Path.home())
        out = helper_json("projects", "icon", "--path", home + "/Developer/WeatherGlance", "--image",
                          picked["data"]["path"], "--confirm", env=env)
        d = out["data"]
        self.assertTrue(d["demo"])
        self.assertFalse(d["applied"])
        self.assertEqual((d["target"], d["yml"]), ("Resources/AppIcon.png", "keep"))
        self.assertTrue(Path(d["preview"]).is_file())
        self.assertTrue(d["preview"].startswith(str(self.dir / "xdg_runtime")))   # the demo folder, no project
        out = helper_json("projects", "icon", "--path", home + "/Developer/RecipeBox", "--image",
                          str(self.dir / "nope.png"), env=env)
        self.assertEqual(out["error"]["code"], "no-image")
        check = helper_json("projects", "check", "--path", home + "/Developer/WeatherGlance", env=env)
        [icon] = [i for i in check["data"]["issues"] if i["check"] == "icon"]
        self.assertEqual(icon["fix"]["action"], "icon")


class Picker(unittest.TestCase):
    def test_argv(self):
        z = A.picker_argv("Pick", "/home/u/Pictures", "zenity")
        self.assertEqual(z[:2], ["zenity", "--file-selection"])
        self.assertIn("--filename=/home/u/Pictures/", z)
        self.assertTrue(any(a.startswith("--file-filter=Images | *.png") and "*.heic" in a for a in z))
        k = A.picker_argv("Pick", "/home/u", "kdialog")
        self.assertEqual(k[:3], ["kdialog", "--getopenfilename", "/home/u"])
        self.assertTrue(k[3].endswith("|Images"))

    def test_cancel_and_choice_with_a_fake_zenity(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            z = bindir / "zenity"
            z.write_text('#!/bin/sh\n[ -n "$PICK" ] && { echo "$PICK"; exit 0; }\nexit 1\n')
            z.chmod(0o755)
            env = isolated_env(Path(tmp), PATH=f"{bindir}:/usr/bin:/bin")
            self.assertEqual(helper_json("projects", "pick-file", env=env)["data"], {"path": None})
            out = helper_json("projects", "pick-file", "--title", "Icon", env={**env, "PICK": "/x/logo.png"})
            self.assertEqual(out["data"], {"path": "/x/logo.png"})
