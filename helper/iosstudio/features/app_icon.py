"""The app icon from any image: `iosstudio-helper projects icon` (stdlib only; Pillow or ImageMagick does the pixels).

iOS wants one square 1024×1024 PNG without transparency: the system rounds the corners itself, and transparent
parts show black. xtool takes exactly that file from `iconPath` in xtool.yml (relative to the project, must end in
.png) and copies it into the app. This module turns any picture into that file:

  fill  scale to cover the square and crop the middle (photos)
  fit   the whole picture centred on the background with a 10 % margin (logos)
  auto  fit when the picture has transparent parts, else fill (the default)

Any transparency is flattened onto the background colour (#ffffff unless --background). The result is an 8-bit
RGB PNG in sRGB (a Display P3 profile is kept; CMYK and other colour spaces are converted). Nothing is pre-rounded.

    projects icon --path P --image FILE [--background "#RRGGBB"] [--fit auto|fill|fit] [--confirm]
    projects pick-file [--title T] [--kind image] [--start DIR]

Without --confirm it is a dry run: the icon is rendered to a preview in CACHE_DIR/icons/ (newest 20 kept, none older
than 7 days; pruned on every call) and the project is left alone. With --confirm the preview is checked (PNG
signature, IHDR 1024×1024, 8-bit RGB) and moved into place through a temp file and a rename, so a failed render never
leaves a broken icon; then `iconPath` is set with core/xtool_yml.set_key (the rest of xtool.yml stays byte for byte)
and the project check's icon findings come back as `check` ([] = it passes). A dry run's `check` holds the current
icon's findings. The target is the existing iconPath when it is a .png inside the project, else
Resources/AppIcon.png. An xtool.yml that a script writes (GENERATED near the top) is refused.

Backends, in order: Pillow when importable; else this command re-runs once with the device daemon's venv python
(subproc.pymd3_python(), which has Pillow), like screenshots._maybe_reexec; else ImageMagick (`magick`, or IM6
`convert` + `identify`), which Omarchy ships. Every subprocess has a timeout. Inputs are capped at 50 MB and
100 megapixels. IOSSTUDIO_ICON_BACKEND=pillow|imagemagick pins one (tests), IOSSTUDIO_MAGICK names the magick binary.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from iosstudio.core import appdirs, fileio, output, subproc, xtool_yml
from iosstudio.core.output import HelperError, fix

SIZE = 1024
MARGIN = 0.10                     # fit: space left around the picture on each side
MAX_BYTES = 50 << 20
MAX_PIXELS = 100_000_000
DEFAULT_TARGET = "Resources/AppIcon.png"
DEFAULT_BACKGROUND = "#ffffff"
KEEP_PREVIEWS = 20
PREVIEW_MAX_AGE = 7 * 86400
IDENTIFY_TIMEOUT = 30
RENDER_TIMEOUT = 120
IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "heic", "gif", "bmp", "tiff", "tif", "svg")
PNG_SIG = b"\x89PNG\r\n\x1a\n"
FITS = ("auto", "fill", "fit")


@dataclass
class Source:
    path: str
    width: int
    height: int
    format: str
    hasAlpha: bool
    icc: str = ""        # the embedded colour profile's description ('' for none); not in the reply
    cmyk: bool = False


def sheet_fix(path: Path | str, label: str = "Set icon…") -> dict:
    """The deep link to the UI's "Set icon…" sheet for a project."""
    return {**fix(label, "projects", action="icon"), "path": str(path)}


# ================================================================ inputs

def parse_color(raw: object) -> str:
    """'#RRGGBB' (or #RGB, with or without #) as lowercase #rrggbb; bad-color otherwise."""
    s = DEFAULT_BACKGROUND if raw in (None, True, "") else str(raw).strip()
    m = re.fullmatch(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", s)
    if not m:
        raise HelperError("bad-color", f"“{s}” isn't a colour. Use a hex colour like #ffffff.")
    h = m.group(1).lower()
    return "#" + ("".join(c * 2 for c in h) if len(h) == 3 else h)


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def parse_fit(raw: object) -> str:
    s = "auto" if raw in (None, True, "") else str(raw).strip().lower()
    if s not in FITS:
        raise HelperError("usage", "--fit must be auto, fill or fit.")
    return s


def image_path(flags: dict, project: Path | str | None = None) -> Path:
    raw = flags.get("image")
    if not isinstance(raw, str) or not raw.strip():
        raise HelperError("usage", "Missing --image (the picture to make the icon from).")
    p = Path(os.path.expanduser(raw.strip()))
    if not p.is_absolute() and not p.is_file() and project:     # relative: to the current folder, else the project
        p = Path(project) / p
    if not p.is_file():
        raise HelperError("no-image", f"There's no picture at {appdirs.tilde(p)}.",
                          sheet_fix(project, "Choose another picture") if project else None)
    if p.stat().st_size > MAX_BYTES:
        raise HelperError("too-large", f"{p.name} is larger than {MAX_BYTES >> 20} MB. Pick a smaller picture.",
                          sheet_fix(project, "Choose another picture") if project else None)
    return p.resolve()


# ================================================================ PNG checks (stdlib)

def png_header(path: Path) -> dict | None:
    """{width, height, depth, color, trns} from a PNG's chunks before the image data, or None for a non-PNG."""
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != PNG_SIG:
                return None
            out: dict = {"trns": False}
            for _ in range(64):
                head = fh.read(8)
                if len(head) < 8:
                    break
                length, kind = struct.unpack(">I4s", head)
                if kind == b"IHDR":
                    w, h, depth, color = struct.unpack(">IIBB", fh.read(10))
                    out.update(width=w, height=h, depth=depth, color=color)
                    fh.seek(length - 10 + 4, 1)
                    continue
                if kind == b"tRNS":
                    out["trns"] = True
                if kind in (b"IDAT", b"IEND"):
                    break
                fh.seek(length + 4, 1)
            return out if "width" in out else None
    except (OSError, struct.error):
        return None


def valid_icon(path: Path) -> str | None:
    """None when `path` is what iOS wants (PNG, 1024×1024, 8-bit RGB, no tRNS), else why not."""
    h = png_header(path)
    if h is None:
        return "it isn't a PNG"
    if (h["width"], h["height"]) != (SIZE, SIZE):
        return f"it is {h['width']}×{h['height']}, not {SIZE}×{SIZE}"
    if h["depth"] != 8 or h["color"] != 2 or h["trns"]:
        return "it isn't an 8-bit RGB image without transparency"
    return None


# ================================================================ backends

def _forced() -> str:
    return os.environ.get("IOSSTUDIO_ICON_BACKEND", "").strip().lower()


def _pil():
    if _forced() == "imagemagick":
        return None
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def _magick() -> tuple[list[str], list[str]] | None:
    """(convert argv prefix, identify argv prefix) of ImageMagick 7 (`magick`) or 6 (`convert`/`identify`)."""
    if _forced() == "pillow":
        return None
    env = os.environ.get("IOSSTUDIO_MAGICK")
    if env:
        return [env], [env, "identify"]
    m = subproc.which("magick")
    if m:
        return [m], [m, "identify"]
    c, i = subproc.which("convert"), subproc.which("identify")
    if c and i:
        return [c], [i]
    return None


def backends() -> list[str]:
    return [n for n, ok in (("pillow", _pil() is not None), ("imagemagick", _magick() is not None)) if ok]


def maybe_reexec() -> None:
    """Re-run this command once with a python that has Pillow (the device daemon's venv) when ours hasn't."""
    if _forced() or _pil() or os.environ.get("IOSSTUDIO_ICON_CHILD"):
        return
    py = subproc.pymd3_python()
    if not py or os.path.abspath(py) == os.path.abspath(sys.executable):
        return
    os.environ["IOSSTUDIO_ICON_CHILD"] = "1"
    run_py = str(appdirs.REPO / "helper" / "run.py")
    pycache = str(appdirs.cache_dir() / "pycache")
    try:
        sys.stdout.flush()
        os.execv(py, [py, "-I", "-X", f"pycache_prefix={pycache}", run_py, "iosstudio"] + sys.argv[1:])
    except OSError:
        pass      # carry on with ImageMagick


def _no_tool() -> HelperError:
    return HelperError("no-image-tool", "Making an icon needs ImageMagick or the iPhone connection tools (they "
                       "bring an image library), and neither is installed.",
                       fix("Repair iPhone tools", "setup", step="pymobiledevice3"))


def _bad(p: Path, why: str = "the file isn't a picture this computer can read") -> HelperError:
    return HelperError("bad-image", f"{p.name} can't be used as an icon: {why}.")


def _check_pixels(p: Path, w: int, h: int) -> None:
    if w <= 0 or h <= 0:
        raise _bad(p, "it has no pixels")
    if w * h > MAX_PIXELS:
        raise HelperError("too-large", f"{p.name} is {w}×{h}, more than {MAX_PIXELS // 1_000_000} megapixels. "
                          "Pick a smaller picture.")


def _is_p3(desc: str) -> bool:
    return "p3" in desc.lower()


# ---------------------------------------------------------------- Pillow

def _pil_open(Image, p: Path):
    Image.MAX_IMAGE_PIXELS = None           # our own cap below, from the header (nothing is decoded yet)
    try:
        im = Image.open(p)
    except Exception as exc:  # noqa: BLE001 - any decoder error is "not a picture"
        raise _bad(p) from exc
    _check_pixels(p, *im.size)
    return im


def _pil_has_alpha(im) -> bool:
    if im.mode in ("RGBA", "LA", "PA", "RGBa", "La") or (im.mode == "P" and "transparency" in im.info):
        try:
            return im.convert("RGBA").getchannel("A").getextrema()[0] < 255
        except Exception:  # noqa: BLE001
            return True
    return False


def _icc_desc(icc: bytes | None) -> str:
    if not icc:
        return ""
    try:
        from PIL import ImageCms
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()
    except Exception:  # noqa: BLE001 - no littlecms, or an odd profile: look for the name in the bytes
        flat = icc.replace(b"\x00", b"")
        return "Display P3" if b"P3" in flat else "embedded"


def _pil_probe(p: Path) -> Source:
    Image = _pil()
    with _pil_open(Image, p) as im:
        return Source(str(p), im.width, im.height, (im.format or p.suffix.lstrip(".")).upper(), _pil_has_alpha(im),
                      _icc_desc(im.info.get("icc_profile")), im.mode == "CMYK")


def _pil_render(src: Source, out: Path, fit: str, bg: str) -> None:
    Image = _pil()
    from PIL import ImageOps
    with _pil_open(Image, Path(src.path)) as im:
        try:
            im.seek(0)
            im = ImageOps.exif_transpose(im)
            icc = im.info.get("icc_profile")
            keep_icc = icc if icc and _is_p3(src.icc) else None
            if icc and not keep_icc:
                try:                            # to sRGB through the embedded profile (CMYK, Adobe RGB, …)
                    from PIL import ImageCms
                    target = "RGBA" if "A" in im.getbands() else "RGB"
                    if im.mode not in ("RGB", "RGBA", "CMYK", "L", "LA"):
                        im = im.convert("RGBA")
                        target = "RGBA"
                    im = ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                                   ImageCms.createProfile("sRGB"), outputMode=target)
                except Exception:  # noqa: BLE001 - no littlecms or a bad profile: plain conversion below
                    pass
            rgba = im.convert("RGBA")
        except HelperError:
            raise
        except Exception as exc:  # noqa: BLE001 - truncated or odd file
            raise _bad(Path(src.path), "the file is damaged or in a format that can't be read") from exc
    canvas = Image.new("RGB", (SIZE, SIZE), _rgb(bg))
    w, h = rgba.size
    if fit == "fill":
        scale = max(SIZE / w, SIZE / h)
        nw, nh = max(SIZE, math.ceil(w * scale)), max(SIZE, math.ceil(h * scale))
        big = rgba.resize((nw, nh), Image.Resampling.LANCZOS)
        left, top = (nw - SIZE) // 2, (nh - SIZE) // 2
        part = big.crop((left, top, left + SIZE, top + SIZE))
        canvas.paste(part, (0, 0), part)
    else:
        box = round(SIZE * (1 - 2 * MARGIN))
        scale = min(box / w, box / h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        small = rgba.resize((nw, nh), Image.Resampling.LANCZOS)
        canvas.paste(small, ((SIZE - nw) // 2, (SIZE - nh) // 2), small)
    with fileio.replacing(out) as tmp:
        canvas.save(tmp, "PNG", **({"icc_profile": keep_icc} if keep_icc else {}))


# ---------------------------------------------------------------- ImageMagick

def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subproc.run_group(argv, timeout, env=subproc.tool_env("plain"))
    except subprocess.TimeoutExpired:
        raise HelperError("timeout", "Making the icon took too long. Try a smaller picture.") from None
    except OSError as exc:
        raise _no_tool() from exc


IDENTIFY_FORMAT = "%w %h %m %[opaque] %[colorspace] %[profile:icc]\\n"


def _im_probe(p: Path) -> Source:
    tool = _magick()
    if tool is None:
        raise _no_tool()
    r = _run([*tool[1], "-format", IDENTIFY_FORMAT, f"{p}[0]"], IDENTIFY_TIMEOUT)
    first = (r.stdout or "").strip().splitlines()[:1]
    parts = first[0].split(" ", 5) if first else []
    if r.returncode != 0 or len(parts) < 5 or not (parts[0].isdigit() and parts[1].isdigit()):
        raise _bad(p)
    w, h = int(parts[0]), int(parts[1])
    _check_pixels(p, w, h)
    return Source(str(p), w, h, parts[2].upper(), parts[3].lower() != "true", parts[5].strip() if len(parts) > 5 else "",
                  parts[4].upper() == "CMYK")


def magick_argv(tool: list[str], src: Source, out: Path, fit: str, bg: str) -> list[str]:
    """The ImageMagick command line that renders `src` into `out` (tests check it at the process boundary)."""
    argv = [*tool, f"{src.path}[0]", "-auto-orient"]
    if src.cmyk or not _is_p3(src.icc):
        argv += ["-colorspace", "sRGB"]
    if fit == "fill":
        argv += ["-resize", f"{SIZE}x{SIZE}^"]
    else:
        box = round(SIZE * (1 - 2 * MARGIN))
        argv += ["-resize", f"{box}x{box}"]
    argv += ["-background", bg, "-gravity", "center", "-extent", f"{SIZE}x{SIZE}", "-alpha", "remove",
             "-alpha", "off", "-depth", "8"]
    argv += ["+profile", "!icc,*"] if _is_p3(src.icc) and not src.cmyk else ["-strip"]
    return argv + [f"PNG24:{out}"]


def _im_render(src: Source, out: Path, fit: str, bg: str) -> None:
    tool = _magick()
    if tool is None:
        raise _no_tool()
    with fileio.replacing(out) as tmp:
        r = _run(magick_argv(tool[0], src, tmp, fit, bg), RENDER_TIMEOUT)
        if r.returncode != 0 or not tmp.is_file():
            raise HelperError("bad-image", f"{Path(src.path).name} couldn't be turned into an icon.",
                              detail=(r.stderr or r.stdout or "")[-2000:])


# ---------------------------------------------------------------- probe + render with the first backend that works

def probe(p: Path) -> tuple[Source, str]:
    """The picture's size, format and transparency, and the backend that read it."""
    names = backends()
    if not names:
        raise _no_tool()
    err: HelperError | None = None
    for name in names:
        try:
            return (_pil_probe(p) if name == "pillow" else _im_probe(p)), name
        except HelperError as exc:
            if exc.code != "bad-image":
                raise
            err = exc               # Pillow can't read SVG/HEIC; ImageMagick may
    raise err or _bad(p)


def render(src: Source, backend: str, out: Path, fit: str, bg: str) -> None:
    (_pil_render if backend == "pillow" else _im_render)(src, out, fit, bg)
    why = valid_icon(out)
    if why:
        out.unlink(missing_ok=True)
        raise HelperError("bad-image", f"The icon made from {Path(src.path).name} came out wrong: {why}.")


# ================================================================ preview cache

def preview_dir() -> Path:
    return appdirs.cache_dir() / "icons"


def prune_previews(d: Path, protect: Path | None = None, now: float | None = None) -> int:
    """Newest KEEP_PREVIEWS kept, none older than PREVIEW_MAX_AGE (except `protect`); returns how many went."""
    now = time.time() if now is None else now
    removed = 0
    files = list(d.glob("*.png")) + list(d.glob(".*.tmp"))
    for f in files:
        try:
            old = now - f.lstat().st_mtime > PREVIEW_MAX_AGE
        except OSError:
            continue
        if old and (protect is None or f != protect):
            f.unlink(missing_ok=True)
            removed += 1
    left = [f for f in d.glob("*.png") if f.exists()]
    return removed + fileio.prune(left, KEEP_PREVIEWS, min_keep=1, protect=[protect] if protect else [])


def _preview_path(d: Path, project: str, src: Path, fit: str, bg: str, backend: str) -> Path:
    st = src.stat()
    key = hashlib.sha1(f"{src}|{st.st_size}|{st.st_mtime_ns}|{fit}|{bg}|{backend}".encode()).hexdigest()[:16]
    return d / f"{fileio.path_key(project)}-{key}.png"


def make_preview(project: str, src: Source, backend: str, fit: str, bg: str, d: Path | None = None) -> Path:
    """Render into the preview cache (an unchanged picture with the same options reuses its preview) and prune."""
    d = d or preview_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = _preview_path(d, project, Path(src.path), fit, bg, backend)
    if out.is_file() and valid_icon(out) is None:
        os.utime(out)
    else:
        render(src, backend, out, fit, bg)
    prune_previews(d, protect=out)
    return out


# ================================================================ the project side

def _inside(root: Path, p: Path) -> bool:
    try:
        return p.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def plan_target(root: Path, yml: xtool_yml.XtoolYml) -> tuple[str, str, str | None]:
    """(target relative to the project, yml change "add"|"change"|"keep", previous iconPath or None)."""
    prev = yml.get("iconPath")
    if prev and prev.endswith(".png") and not os.path.isabs(os.path.expanduser(prev)) and _inside(root, root / prev):
        return prev, "keep", prev
    return DEFAULT_TARGET, ("change" if prev else "add"), prev


def _yml_value(rel: str) -> str:
    return rel if re.fullmatch(r"[\w./-]+", rel) else '"' + rel.replace("\\", "\\\\").replace('"', '\\"') + '"'


def icon_findings(root: Path) -> list[dict]:
    from iosstudio.features import project_check
    return project_check._check_icon(root, xtool_yml.read(root / "xtool.yml"))


def _warnings(src: Source, fit: str) -> list[str]:
    out = []
    needed = SIZE if fit == "fill" else round(SIZE * (1 - 2 * MARGIN))     # pixels the picture's edge covers
    if (min if fit == "fill" else max)(src.width, src.height) < needed:
        out.append(f"The picture is only {src.width}×{src.height}, so it was scaled up and may look soft. "
                   f"A picture of at least {SIZE}×{SIZE} looks sharpest.")
    if fit == "fill" and src.width != src.height:
        out.append("The picture isn't square, so its edges were cropped. Choose Fit to keep all of it.")
    if fit == "fill" and src.hasAlpha:
        out.append("The picture has transparent parts; they are filled with the background colour.")
    return out


def _reply(name: str, root: str, src: Source, fit: str, bg: str, target: str, replaces: bool, yml: str,
           prev: str | None, preview: Path, backend: str, applied: bool, check: list) -> dict:
    s = asdict(src)
    s.pop("icc")
    s.pop("cmyk")
    return {"name": name, "path": root, "source": s, "fit": fit, "background": bg, "target": target,
            "targetPath": str(Path(root) / target), "replaces": replaces, "yml": yml, "previous": prev,
            "preview": str(preview), "warnings": _warnings(src, fit), "backend": backend, "applied": applied,
            "check": check}


def apply(root: Path, preview: Path, target: str, yml_change: str) -> None:
    """Copy the checked preview to the target (temp file + rename) and set iconPath (rest of xtool.yml kept)."""
    why = valid_icon(preview)
    if why:
        raise HelperError("bad-image", f"The icon came out wrong ({why}), so the project wasn't changed.")
    dest = root / target
    with fileio.replacing(dest, 0o644) as tmp:
        shutil.copyfile(preview, tmp)
        if valid_icon(tmp):
            raise HelperError("write-failed", "The icon couldn't be copied into the project.")
    if yml_change != "keep":
        yml_path = root / "xtool.yml"
        text = yml_path.read_text()
        try:
            mode = yml_path.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
        fileio.atomic_write(yml_path, xtool_yml.set_key(text, "iconPath", _yml_value(target)), mode)


def cmd_icon(flags: dict) -> int:
    from iosstudio.features.projects import model
    root = model.require_project(flags)
    img = image_path(flags, root)
    fit_opt, bg = parse_fit(flags.get("fit")), parse_color(flags.get("background"))
    yml_path = root / "xtool.yml"
    yml = xtool_yml.parse(yml_path.read_text(errors="replace"))
    if yml.generated:
        raise HelperError("generated-yml", "This project's xtool.yml is written by a script (it says GENERATED "
                          "at the top), so an icon set here would be lost on the next build. Set iconPath where "
                          "the script writes it.", {**fix("Open in editor", "projects", action="open"),
                                                    "path": str(root)})
    maybe_reexec()
    src, backend = probe(img)
    fit = fit_opt if fit_opt != "auto" else ("fit" if src.hasAlpha else "fill")
    target, yml_change, prev = plan_target(root, yml)
    replaces = (root / target).is_file()
    preview = make_preview(str(root), src, backend, fit, bg)
    applied = bool(flags.get("confirm"))
    if applied:
        apply(root, preview, target, yml_change)
    name = yml.get("product") or root.name
    return output.ok(_reply(name, str(root), src, fit, bg, target, replaces, yml_change, prev, preview, backend,
                            applied, icon_findings(root)))


# ================================================================ file picker

def picker_argv(title: str, start: str, tool: str) -> list[str]:
    pats = " ".join(f"*.{e} *.{e.upper()}" for e in IMAGE_EXTS)
    if tool == "zenity":
        return [tool, "--file-selection", f"--title={title}", f"--filename={start.rstrip('/')}/",
                f"--file-filter=Images | {pats}", "--file-filter=All files | *"]
    return [tool, "--getopenfilename", start, f"{pats}|Images", "--title", title]


def cmd_pick_file(flags: dict) -> int:
    """Native file chooser (zenity/kdialog) for a picture. {"path": str|None} (None = cancelled)."""
    pictures = appdirs.HOME / "Pictures"
    start = os.path.expanduser(str(flags.get("start") if isinstance(flags.get("start"), str) else
                                   (pictures if pictures.is_dir() else appdirs.HOME)))
    title = str(flags.get("title")) if isinstance(flags.get("title"), str) else "Choose a picture"
    tool = next((t for t in ("zenity", "kdialog") if subproc.which(t)), None)
    if not tool:
        raise HelperError("no-picker", "No file chooser is installed. Type the picture's path instead.")
    try:
        r = subprocess.run(picker_argv(title, start, tool), capture_output=True, text=True, timeout=600,
                           stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return output.ok({"path": None})
    path = r.stdout.strip() if r.returncode == 0 else ""
    return output.ok({"path": path or None})


# ================================================================ demo mode

def demo(action: str, flags: dict) -> int:
    """Demo mode: the same reply shapes; nothing is written to a project. A real picture is rendered into the
    demo folder when a backend is there, so the preview looks right; otherwise a painted stand-in."""
    from iosstudio.demo import demo_art
    if action == "pick-file":
        logo = demo_art.icon(demo_art.demo_dir() / "pick" / "Logo.png", 0.62, "drop", size=256)
        return output.ok({"path": str(logo)})
    raw = str(flags.get("path") or "")
    if not raw or raw == "True":
        return output.fail("usage", "Missing --path.")
    path = raw.rstrip("/")
    name = path.split("/")[-1]
    try:
        img = image_path(flags, path)
        fit_opt, bg = parse_fit(flags.get("fit")), parse_color(flags.get("background"))
    except HelperError as exc:
        return output.fail(exc.code, exc.message, exc.fix)
    d = demo_art.demo_dir() / "icon-previews"
    try:
        src, backend = probe(img)
        fit = fit_opt if fit_opt != "auto" else ("fit" if src.hasAlpha else "fill")
        preview = make_preview(path, src, backend, fit, bg, d)
    except HelperError:
        src = Source(str(img), 256, 256, "PNG", True)
        fit, backend = (fit_opt if fit_opt != "auto" else "fit"), "pillow"
        preview = demo_art.icon(d / f"{name}.png", 0.62, "drop", size=256)
    has_icon = name != "RecipeBox"
    data = _reply(name, path, src, fit, bg, DEFAULT_TARGET, has_icon, "keep" if has_icon else "add",
                  DEFAULT_TARGET if has_icon else None, preview, backend, False, [])
    return output.ok({**data, "demo": True})
