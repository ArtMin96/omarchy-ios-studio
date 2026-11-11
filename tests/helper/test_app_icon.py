"""The app icon from any image (helper/iosstudio/features/app_icon.py), in process.

Inputs, the PNG checks, the target and xtool.yml plan, the ImageMagick command line, the preview cache, and real
renders with Pillow and ImageMagick when they are installed (skipped otherwise: CI's python has only pytest).
The command through bin/iosstudio-helper with a fake magick: tests/cli/test_app_icon_cli.py. Stdlib only:
    python -m pytest tests/helper/test_app_icon.py
"""
from __future__ import annotations

import os
import shutil
import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

from iosstudio.core import xtool_yml
from iosstudio.core.output import HelperError
from iosstudio.features import app_icon as A
from iosstudio.features import project_check
from support import ROOT

FIX = ROOT / "tests" / "fixtures" / "app-icon"
LOGO, PHOTO, WIDE = FIX / "logo-transparent.png", FIX / "photo.jpg", FIX / "wide.png"

try:
    import PIL  # noqa: F401
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
HAS_MAGICK = bool(shutil.which("magick") or (shutil.which("convert") and shutil.which("identify")))


def png(path: Path, w: int, h: int, color: int = 2, depth: int = 8, trns: bool = False) -> Path:
    """A PNG header (and a token IDAT) with the given IHDR, enough for the header checks."""
    def chunk(k: bytes, b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + k + b + struct.pack(">I", zlib.crc32(k + b) & 0xFFFFFFFF)
    data = A.PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, color, 0, 0, 0))
    if trns:
        data += chunk(b"tRNS", b"\x00\x00")
    path.write_bytes(data + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b""))
    return path


def pixel(path: Path, x: int, y: int) -> tuple:
    from PIL import Image
    with Image.open(path) as im:
        return im.convert("RGB").getpixel((x, y))


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.dir / "cache")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def project(self, yml: str = "version: 1\nbundleID: com.example.App\n") -> Path:
        p = self.dir / "App"
        p.mkdir(exist_ok=True)
        (p / "Package.swift").write_text("// swift-tools-version: 6.0\n")
        (p / "xtool.yml").write_text(yml)
        return p


class Inputs(unittest.TestCase):
    def test_colors(self):
        self.assertEqual(A.parse_color(None), "#ffffff")
        self.assertEqual(A.parse_color("#ABCDEF"), "#abcdef")
        self.assertEqual(A.parse_color("0a0"), "#00aa00")
        for bad in ("red", "#12345", "#gggggg", "rgb(1,2,3)"):
            with self.assertRaises(HelperError) as cm:
                A.parse_color(bad)
            self.assertEqual(cm.exception.code, "bad-color")

    def test_fit(self):
        self.assertEqual(A.parse_fit(None), "auto")
        self.assertEqual(A.parse_fit("FILL"), "fill")
        with self.assertRaises(HelperError) as cm:
            A.parse_fit("stretch")
        self.assertEqual(cm.exception.code, "usage")

    def test_image_path_errors(self):
        for flags, code in (({}, "usage"), ({"image": True}, "usage"), ({"image": "/no/such.png"}, "no-image")):
            with self.assertRaises(HelperError) as cm:
                A.image_path(flags, "/proj")
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(A.image_path({"image": "tests/fixtures/app-icon/photo.jpg"}, ROOT), PHOTO)   # cwd or project
        with mock.patch.object(A, "MAX_BYTES", 10):
            with self.assertRaises(HelperError) as cm:
                A.image_path({"image": str(PHOTO)})
            self.assertEqual(cm.exception.code, "too-large")

    def test_pixel_cap(self):
        with self.assertRaises(HelperError) as cm:
            A._check_pixels(PHOTO, 20000, 20000)
        self.assertEqual(cm.exception.code, "too-large")
        with self.assertRaises(HelperError) as cm:
            A._check_pixels(PHOTO, 0, 5)
        self.assertEqual(cm.exception.code, "bad-image")


class PngChecks(Temp):
    def test_valid_icon(self):
        self.assertIsNone(A.valid_icon(png(self.dir / "ok.png", 1024, 1024)))
        self.assertIn("512×512", A.valid_icon(png(self.dir / "s.png", 512, 512)))
        self.assertIn("RGB", A.valid_icon(png(self.dir / "a.png", 1024, 1024, color=6)))
        self.assertIn("RGB", A.valid_icon(png(self.dir / "d.png", 1024, 1024, depth=16)))
        self.assertIn("RGB", A.valid_icon(png(self.dir / "t.png", 1024, 1024, trns=True)))
        self.assertEqual(A.valid_icon(PHOTO), "it isn't a PNG")
        self.assertEqual(A.png_header(LOGO)["color"], 6)

    def test_valid_icon_passes_project_check(self):
        p = self.project("version: 1\nbundleID: com.example.App\niconPath: icon.png\n")
        png(p / "icon.png", 1024, 1024)
        self.assertEqual(A.icon_findings(p), [])


class Target(Temp):
    def plan(self, yml: str) -> tuple:
        p = self.project(yml)
        return A.plan_target(p, xtool_yml.read(p / "xtool.yml"))

    def test_plans(self):
        self.assertEqual(self.plan("version: 1\n"), ("Resources/AppIcon.png", "add", None))
        self.assertEqual(self.plan("version: 1\niconPath: Art/Icon.png  # the icon\n"),
                         ("Art/Icon.png", "keep", "Art/Icon.png"))
        self.assertEqual(self.plan("version: 1\niconPath: icon.jpg\n"), ("Resources/AppIcon.png", "change", "icon.jpg"))
        self.assertEqual(self.plan("version: 1\niconPath: ../shared/icon.png\n"),
                         ("Resources/AppIcon.png", "change", "../shared/icon.png"))
        self.assertEqual(self.plan("version: 1\niconPath: /etc/icon.png\n")[1], "change")

    def test_apply_keeps_the_rest_of_xtool_yml(self):
        yml = "# My app\nversion: 1   # schema\n\nbundleID: com.example.App\nproduct: App\nextensions:\n  - product: W\n"
        p = self.project(yml)
        prev = png(self.dir / "prev.png", 1024, 1024)
        A.apply(p, prev, "Resources/AppIcon.png", "add")
        self.assertEqual((p / "xtool.yml").read_text(), yml + "iconPath: Resources/AppIcon.png\n")
        self.assertIsNone(A.valid_icon(p / "Resources/AppIcon.png"))
        (p / "xtool.yml").write_text("version: 1\niconPath: old.jpg  # set by hand\nproduct: App\n")
        A.apply(p, prev, "Resources/AppIcon.png", "change")
        self.assertEqual((p / "xtool.yml").read_text(),
                         "version: 1\niconPath: Resources/AppIcon.png  # set by hand\nproduct: App\n")
        before = (p / "xtool.yml").read_bytes()
        A.apply(p, prev, "Resources/AppIcon.png", "keep")
        self.assertEqual((p / "xtool.yml").read_bytes(), before)

    def test_apply_refuses_a_bad_render_and_leaves_the_old_icon(self):
        p = self.project()
        (p / "Resources").mkdir()
        (p / "Resources/AppIcon.png").write_bytes(b"old")
        with self.assertRaises(HelperError) as cm:
            A.apply(p, png(self.dir / "bad.png", 512, 512), "Resources/AppIcon.png", "add")
        self.assertEqual(cm.exception.code, "bad-image")
        self.assertEqual((p / "Resources/AppIcon.png").read_bytes(), b"old")
        self.assertNotIn("iconPath", (p / "xtool.yml").read_text())
        self.assertEqual([f.name for f in (p / "Resources").iterdir()], ["AppIcon.png"])   # no temp file left

    def test_yml_value_quotes_odd_paths(self):
        self.assertEqual(A._yml_value("Resources/AppIcon.png"), "Resources/AppIcon.png")
        self.assertEqual(A._yml_value("My Icons/a#b.png"), '"My Icons/a#b.png"')
        self.assertEqual(xtool_yml.scalar(A._yml_value("My Icons/a#b.png")), "My Icons/a#b.png")

    def test_warnings(self):
        small = A.Source("x", 300, 200, "PNG", True)
        w = A._warnings(small, "fill")
        self.assertEqual(len(w), 3)
        self.assertEqual(A._warnings(A.Source("x", 2000, 2000, "PNG", False), "fill"), [])
        self.assertEqual(A._warnings(A.Source("x", 900, 400, "PNG", True), "fit"), [])

    def test_icon_findings_link_to_the_sheet(self):
        p = self.project()
        [f] = A.icon_findings(p)
        self.assertEqual(f["fix"], {"label": "Set icon…", "page": "projects", "action": "icon", "path": str(p)})
        self.assertEqual(project_check.icon_fix(p), f["fix"])


class MagickArgv(unittest.TestCase):
    def test_fill_and_fit(self):
        src = A.Source("/p/a b.jpg", 10, 20, "JPEG", False)
        fill = A.magick_argv(["magick"], src, Path("/o/x.png"), "fill", "#102030")
        self.assertEqual(fill[:2], ["magick", "/p/a b.jpg[0]"])
        self.assertIn("1024x1024^", fill)
        self.assertEqual(fill[fill.index("-background") + 1], "#102030")
        self.assertEqual(fill[fill.index("-extent") + 1], "1024x1024")
        self.assertIn("-strip", fill)
        self.assertEqual(fill[-1], "PNG24:/o/x.png")
        self.assertIn("819x819", A.magick_argv(["magick"], src, Path("/o/x.png"), "fit", "#fff000"))

    def test_profiles(self):
        p3 = A.magick_argv(["convert"], A.Source("/a.png", 9, 9, "PNG", False, icc="Display P3"), Path("/o"), "fill", "#ffffff")
        self.assertNotIn("-strip", p3)
        self.assertNotIn("-colorspace", p3)
        self.assertIn("!icc,*", p3)
        cmyk = A.magick_argv(["magick"], A.Source("/a.jpg", 9, 9, "JPEG", False, icc="U.S. Web Coated", cmyk=True),
                             Path("/o"), "fill", "#ffffff")
        self.assertEqual(cmyk[cmyk.index("-colorspace") + 1], "sRGB")
        self.assertIn("-strip", cmyk)


class Backends(Temp):
    def test_none_installed(self):
        with mock.patch.object(A, "_pil", lambda: None), mock.patch.object(A, "_magick", lambda: None):
            with self.assertRaises(HelperError) as cm:
                A.probe(PHOTO)
        self.assertEqual(cm.exception.code, "no-image-tool")
        self.assertEqual(cm.exception.fix["page"], "setup")

    def test_forced_backend(self):
        with mock.patch.dict(os.environ, {"IOSSTUDIO_ICON_BACKEND": "imagemagick", "IOSSTUDIO_MAGICK": "/x/magick"}):
            self.assertIsNone(A._pil())
            self.assertEqual(A._magick(), (["/x/magick"], ["/x/magick", "identify"]))
        with mock.patch.dict(os.environ, {"IOSSTUDIO_ICON_BACKEND": "pillow"}):
            self.assertIsNone(A._magick())

    def test_reexec_only_without_pillow_and_once(self):
        with mock.patch.object(A.os, "execv") as ex, mock.patch.object(A, "_pil", lambda: None), \
                mock.patch.object(A.subproc, "pymd3_python", lambda: "/venv/bin/python"), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IOSSTUDIO_ICON_BACKEND", None)
            os.environ.pop("IOSSTUDIO_ICON_CHILD", None)
            A.maybe_reexec()
            self.assertEqual(ex.call_args[0][0], "/venv/bin/python")
            self.assertEqual(os.environ["IOSSTUDIO_ICON_CHILD"], "1")
            ex.reset_mock()
            A.maybe_reexec()
            ex.assert_not_called()

    def test_bad_render_is_caught(self):
        src = A.Source(str(PHOTO), 200, 200, "JPEG", False)
        with mock.patch.object(A, "_im_render", lambda s, out, f, b: png(out, 512, 512)):
            with self.assertRaises(HelperError) as cm:
                A.render(src, "imagemagick", self.dir / "o.png", "fill", "#ffffff")
        self.assertEqual(cm.exception.code, "bad-image")
        self.assertFalse((self.dir / "o.png").exists())


class Previews(Temp):
    def test_pruned_by_count_and_age(self):
        d = self.dir / "icons"
        d.mkdir()
        now = time.time()
        for i in range(25):
            f = png(d / f"p{i:02}.png", 1024, 1024)
            os.utime(f, (now - 100 + i, now - 100 + i))
        old = png(d / "old.png", 1024, 1024)
        os.utime(old, (now - 8 * 86400, now - 8 * 86400))
        A.prune_previews(d, protect=d / "p00.png", now=now)
        left = {f.name for f in d.glob("*.png")}
        self.assertNotIn("old.png", left)
        self.assertIn("p00.png", left)             # the one just made is never pruned
        self.assertIn("p24.png", left)
        self.assertLessEqual(len(left), A.KEEP_PREVIEWS + 1)

    def test_preview_cached_and_keyed_by_options(self):
        calls = []

        def fake(src, backend, out, fit, bg):
            calls.append((fit, bg))
            png(out, 1024, 1024)
        src = A.Source(str(PHOTO), 200, 200, "JPEG", False)
        with mock.patch.object(A, "render", fake):
            a = A.make_preview("/proj", src, "pillow", "fill", "#ffffff")
            b = A.make_preview("/proj", src, "pillow", "fill", "#ffffff")
            c = A.make_preview("/proj", src, "pillow", "fit", "#ffffff")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(calls), 2)
        self.assertEqual(a.parent, self.dir / "cache/ios-studio/icons")


@unittest.skipUnless(HAS_PIL, "Pillow isn't installed")
class PillowRenders(Temp):
    def setUp(self):
        super().setUp()
        os.environ["IOSSTUDIO_ICON_BACKEND"] = "pillow"

    def render(self, img: Path, fit: str, bg: str = "#ffffff") -> tuple[Path, A.Source]:
        src, backend = A.probe(img)
        self.assertEqual(backend, "pillow")
        out = self.dir / f"{img.stem}-{fit}.png"
        A.render(src, backend, out, fit, bg)
        return out, src

    def test_transparent_png_fit(self):
        out, src = self.render(LOGO, "fit", "#00ff00")
        self.assertTrue(src.hasAlpha)
        self.assertIsNone(A.valid_icon(out))
        self.assertEqual(pixel(out, 5, 5), (0, 255, 0))           # the margin and the transparent corners
        self.assertEqual(pixel(out, 512, 512), (220, 40, 60))

    def test_jpeg_and_wide_fill(self):
        out, src = self.render(PHOTO, "fill")
        self.assertEqual((src.width, src.height, src.format, src.hasAlpha), (200, 200, "JPEG", False))
        self.assertIsNone(A.valid_icon(out))
        out, _ = self.render(WIDE, "fill")
        self.assertIsNone(A.valid_icon(out))
        self.assertEqual(pixel(out, 512, 512), (250, 250, 250))   # the middle stays; the sides are cropped

    def test_cmyk_and_opaque_alpha(self):
        from PIL import Image
        Image.new("CMYK", (64, 64), (0, 255, 255, 0)).save(self.dir / "cmyk.jpg")
        out, src = self.render(self.dir / "cmyk.jpg", "fill")
        self.assertTrue(src.cmyk)
        self.assertIsNone(A.valid_icon(out))
        Image.new("RGBA", (64, 64), (1, 2, 3, 255)).save(self.dir / "opaque.png")
        self.assertFalse(A.probe(self.dir / "opaque.png")[0].hasAlpha)   # an alpha channel with nothing see-through

    def test_not_a_picture(self):
        (self.dir / "x.png").write_text("hello")
        with self.assertRaises(HelperError) as cm:
            A.probe(self.dir / "x.png")
        self.assertEqual(cm.exception.code, "bad-image")


@unittest.skipUnless(HAS_MAGICK, "ImageMagick isn't installed")
class MagickRenders(Temp):
    def setUp(self):
        super().setUp()
        os.environ["IOSSTUDIO_ICON_BACKEND"] = "imagemagick"

    def test_transparent_png_and_jpeg(self):
        for img, fit, alpha in ((LOGO, "fit", True), (PHOTO, "fill", False), (WIDE, "fill", False)):
            src, backend = A.probe(img)
            self.assertEqual((backend, src.hasAlpha, src.width), ("imagemagick", alpha, A.png_header(img)["width"]
                                                                  if img.suffix == ".png" else 200))
            out = self.dir / f"{img.stem}.png"
            A.render(src, backend, out, fit, "#ffffff")
            self.assertIsNone(A.valid_icon(out), img)

    def test_not_a_picture(self):
        (self.dir / "x.png").write_text("hello")
        with self.assertRaises(HelperError) as cm:
            A.probe(self.dir / "x.png")
        self.assertEqual(cm.exception.code, "bad-image")
