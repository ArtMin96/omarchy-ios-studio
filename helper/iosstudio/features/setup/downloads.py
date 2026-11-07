"""Downloads: HTTPS with progress events and SHA-256 checks (urllib loads only when one runs), and AUR recipes."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from iosstudio.core.output import HelperError, fix

from . import runtime as rt
from .facts import arch
from .pins import AUR_DIR, AUR_SNAPSHOT, AUR_SNAPSHOT_HEAD
from .runtime import fmt_bytes, log, progress


def download(url: str, dest: Path, step_id: str | None, *, sha256: str | None = None, label: str = "",
             frac_range: tuple[float, float] = (0.0, 1.0)) -> Path:
    import urllib.error
    import urllib.request  # ~40 ms: only setup steps that download pay for it
    if rt.DRY:
        log(step_id, f"would download {url} -> {dest}" + (f" (sha256 {sha256[:12]}…)" if sha256 else ""))
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and sha256 and sha256_of(dest) == sha256:
        log(step_id, f"already downloaded: {dest.name}")
        return dest
    part = dest.with_name(dest.name + ".part")
    lo, hi = frac_range
    log(step_id, f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "iOS-Studio/1.0"})
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            got, last, start = 0, 0.0, time.time()
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                got += len(chunk)
                now = time.time()
                if now - last > 0.5:
                    last = now
                    rate = got / max(0.1, now - start)
                    frac = got / total if total else None
                    eta = (total - got) / rate if total and rate else None
                    progress(step_id, None if frac is None else lo + (hi - lo) * frac,
                          f"{label or 'Downloading'} · {fmt_bytes(got)}" + (f" of {fmt_bytes(total)}" if total else "")
                          + f" · {fmt_bytes(rate)}/s", eta)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        try:
            part.unlink()
        except OSError:
            pass
        raise HelperError("network", f"Couldn't download {label or Path(url).name}. Check your internet "
                          "connection, then try again.", fix("Try again", "setup", step=step_id) if step_id else None,
                          detail=f"{url}: {exc}")
    if sha256 and h.hexdigest() != sha256:
        part.unlink()
        raise HelperError("checksum", f"The download of {label or Path(url).name} was damaged (checksum mismatch). "
                          "Try again.", fix("Try again", "setup", step=step_id) if step_id else None)
    os.replace(part, dest)
    return dest


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ AUR recipes (built as the user)

def aur_fetch(pkg: str, ref: str | None, step_id: str) -> Path:
    """Unpack an AUR snapshot (pinned commit or HEAD) into ~/.cache/ios-studio/aur/<pkg> as the user."""
    dest = AUR_DIR / pkg
    url = AUR_SNAPSHOT.format(ref=ref) if ref else AUR_SNAPSHOT_HEAD.format(pkg=pkg)
    if rt.DRY:
        log(step_id, f"would fetch the AUR recipe {url} into {dest}")
        return dest
    marker = dest / ".ios-studio-ref"
    if ref and marker.exists() and marker.read_text().strip() == ref and (dest / "PKGBUILD").exists():
        return dest
    AUR_DIR.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix=f".{pkg}.", dir=AUR_DIR))
    tgz = download(url, tmpdir / "snap.tar.gz", step_id, label=f"{pkg} recipe")
    with tarfile.open(tgz) as tf:
        members = [m for m in tf.getmembers() if not (m.name.startswith("/") or ".." in Path(m.name).parts)]
        tf.extractall(tmpdir, members=members, filter="data") if sys.version_info >= (3, 12) else tf.extractall(tmpdir, members=members)
    top = next((p for p in tmpdir.iterdir() if p.is_dir() and (p / "PKGBUILD").exists()), None)
    if not top:
        raise HelperError("aur", f"The {pkg} recipe from the AUR looks wrong.", detail=url)
    # keep downloaded sources between attempts
    old_sources = [p for p in dest.glob("*") if p.is_file() and p.suffix in (".gz", ".xz", ".zst", ".tar")] if dest.exists() else []
    for p in old_sources:
        os.replace(p, top / p.name)
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(top, dest)
    shutil.rmtree(tmpdir, ignore_errors=True)
    if ref:
        marker.write_text(ref + "\n")
    return dest


def srcinfo(dir_: Path) -> dict[str, list[str]]:
    """A PKGBUILD's .SRCINFO as key -> values."""
    out: dict = {}
    try:
        for line in (dir_ / ".SRCINFO").read_text().splitlines():
            if "=" in line:
                k, v = line.strip().split("=", 1)
                out.setdefault(k.strip(), []).append(v.strip())
    except OSError:
        pass
    return out


def strip_ver(dep: str) -> str:
    """'foo>=1.2' -> 'foo'."""
    return re.split(r"[<>=]", dep, 1)[0]


def prefetch_swift_sources(d: Path, step_id: str) -> None:
    """Download swift.org's tarball ourselves (real progress), so makepkg only verifies it."""
    info = srcinfo(d)
    a = arch()
    srcs = info.get(f"source_{a}", [])
    sums = info.get(f"sha256sums_{a}", [])
    for i, src in enumerate(srcs):
        url = src.split("::", 1)[-1]
        name = src.split("::", 1)[0] if "::" in src else url.rsplit("/", 1)[-1]
        if not url.startswith("https://"):
            continue
        sha = sums[i] if i < len(sums) and sums[i] != "SKIP" else None
        download(url, d / name, step_id, sha256=sha, label="Swift", frac_range=(0.02, 0.6))
