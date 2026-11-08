"""Demo: media.list, media.thumb, media.pull, container.list/pull/push/mkdir/delete, transfer.cancel.

A made-up camera roll (painted pictures, no real photos) and made-up app containers that live in
real folders under the demo directory, so pushes, pulls, new folders and deletes all work and
can be seen. Imported "originals" are PNG stand-ins named after the made-up assets.
"""
from __future__ import annotations

import asyncio
import os
import random
import shutil
import time
from pathlib import Path

from iosstudio.core.output import HelperError
from iosstudio.core.subproc import demo_delay
from iosstudio.demo import demo_art
from iosstudio.device.demo.core import APPS, DIR, FILE_SHARING, event, installed
from iosstudio.device.features.files import core as df

ROOT = DIR / "containers"
THUMBS = DIR / "thumbs"
DEMO_RATE = 6_000_000      # bytes per second the fake phone "transfers"
NO_SHARING = "This app doesn't share files. Your own apps (built here) always do; App Store apps only when they turn on file sharing."


def err(code: str, message: str, fix_=None, detail=None) -> HelperError:
    return HelperError(code, message, fix_, detail)


# ---------------------------------------------------------------- camera roll

def _camera_roll() -> list[dict]:
    rnd = random.Random(27)
    now = (time.time() // 86400) * 86400 + 18 * 3600
    items = []
    n = 1000
    ts = now
    for i in range(148):
        ts -= rnd.uniform(0.2, 14) * 3600
        video = rnd.random() < 0.16
        n += 1
        if video:
            name, w, h = f"IMG_{n:04d}.MOV", 1920, 1080
            size = rnd.randint(8, 160) * 1_000_000
            dur = round(rnd.uniform(3, 95), 2)
        else:
            heic = rnd.random() < 0.85
            name = f"IMG_{n:04d}.{'HEIC' if heic else 'JPG'}"
            w, h = (4032, 3024) if rnd.random() < 0.6 else (3024, 4032)
            size = rnd.randint(1_400_000, 4_800_000)
            dur = None
        folder = "100APPLE" if i > 60 else "101APPLE"
        path = f"DCIM/{folder}/{name}"
        items.append({"id": path, "path": path, "name": name, "kind": "video" if video else "photo",
                      "ts": ts, "date": df.iso(ts), "width": w, "height": h, "duration": dur, "size": size,
                      "seed": i})
    return items


MEDIA = _camera_roll()
BY_PATH = {m["path"]: m for m in MEDIA}


def _thumb_path(item: dict) -> Path:
    return THUMBS / (item["path"].replace("/", "_") + ".png")


# ---------------------------------------------------------------- containers

def _seed_containers() -> None:
    files = {
        "com.example.weatherglance": {
            "Documents/places.json": '{"places": ["Lisbon", "Porto", "Faro"]}\n',
            "Documents/Exports/forecast-2026-10-30.csv": "date,high,low\n2026-10-30,21,14\n2026-10-31,19,13\n",
            "Library/Preferences/com.example.weatherglance.plist": "<plist version=\"1.0\"><dict/></plist>\n",
            "Library/Caches/forecast-cache.json": '{"updated": "2026-11-01T08:00:00Z"}\n',
            "tmp/.keep": "",
        },
        "com.example.habits": {
            "Documents/habits.sqlite": "SQLite format 3 (demo stand-in)\n",
            "Documents/Backups/habits-2026-10-01.json": '{"habits": 4, "streak": 12}\n',
            "Library/Preferences/com.example.habits.plist": "<plist version=\"1.0\"><dict/></plist>\n",
            "tmp/.keep": "",
        },
        "com.example.podcasts": {
            "Documents/Downloads/episode-212.m4a": "demo audio stand-in\n" * 400,
            "Documents/OPML/subscriptions.opml": "<opml version=\"2.0\"><body/></opml>\n",
        },
        "com.example.dailysketch": {
            "Documents/Sketch 14.png": None,
            "Documents/Sketch 15.png": None,
        },
    }
    for plain, tree in files.items():
        bid = _bid(plain)
        base = ROOT / bid
        if base.exists():
            continue
        for rel, text in tree.items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                demo_art.photo(p, sum(map(ord, rel)), 160, 120)
            else:
                p.write_text(text)


def _bid(plain: str) -> str:
    return next((b for _, b, *_ in APPS if b == plain or b.endswith("." + plain)), plain)


def _app(bid: str) -> dict:
    for name, b, _, _, dev, *_ in APPS:
        if b == bid and b in installed:
            return {"bundleId": b, "name": name, "developer": dev, "fileSharing": b in FILE_SHARING,
                    "executable": name.replace(" ", "")}
    raise err("not-found", "That app isn't installed on your iPhone.")


def _access(a: dict) -> tuple[dict, bool, str, Path]:
    bid = str(a.get("bundleId") or "")
    if not bid:
        raise err("bad-args", "Which app? (bundleId missing)")
    info = _app(bid)
    if not info["developer"] and not info["fileSharing"]:
        raise err("no-file-sharing", NO_SHARING)
    _seed_containers()
    docs = bool(a.get("documentsOnly")) or not info["developer"]
    base = ROOT / bid
    (base / "Documents").mkdir(parents=True, exist_ok=True)
    return info, docs, "/Documents" if docs else "/", base


def _local(base: Path, phone: str) -> Path:
    return base / phone.lstrip("/")


def _entry(base: Path, p: Path) -> dict:
    st = p.stat()
    phone = "/" + p.relative_to(base).as_posix()
    return {"name": p.name, "path": phone, "type": "dir" if p.is_dir() else "file",
            "size": None if p.is_dir() else st.st_size, "mtime": st.st_mtime}


async def _copy(src: Path, dst: Path, prog: df.Progress) -> None:
    """Copy at the fake phone's speed, in chunks, so progress and cancel can be seen."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = df.part_path(dst)
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                chunk = fi.read(256 * 1024)
                if not chunk:
                    break
                fo.write(chunk)
                prog.add(len(chunk))
                await asyncio.sleep(demo_delay(len(chunk) / DEMO_RATE))
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


async def _fake_bytes(n: int, prog: df.Progress) -> None:
    sent = 0
    while sent < n:
        step = min(n - sent, 1_000_000)
        sent += step
        prog.add(step)
        await asyncio.sleep(demo_delay(step / DEMO_RATE))


def register(reg):
    tr = df.Transfers()
    reg.provide("transfers", tr)

    @reg.command("media.list")
    def media_list(ctx, a):
        items = MEDIA
        if a.get("kind") in ("photo", "video"):
            items = [m for m in items if m["kind"] == a["kind"]]
        off = max(0, int(a.get("offset") or 0))
        lim = max(1, min(int(a.get("limit") or 200), 1000))
        page = []
        for m in items[off:off + lim]:
            t = _thumb_path(m)
            page.append({**{k: v for k, v in m.items() if k != "seed"}, "thumb": str(t) if t.exists() else None})
        return {"total": len(items), "offset": off, "items": page, "source": "photos-db",
                "photos": sum(1 for m in MEDIA if m["kind"] == "photo"),
                "videos": sum(1 for m in MEDIA if m["kind"] == "video")}

    @reg.command("media.thumb")
    def media_thumb(ctx, a):
        try:
            path = df.clean_dcim_path(a.get("path") or a.get("id") or "")
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        m = BY_PATH.get(path)
        if not m:
            raise err("not-found", "That photo isn't on your iPhone anymore.")
        t = _thumb_path(m)
        src = "cache" if t.exists() else "phone"
        demo_art.photo(t, m["seed"])
        return {"id": path, "path": str(t), "source": src}

    @reg.command("media.pull")
    async def media_pull(ctx, a):
        raw = list(a.get("ids") or a.get("paths") or [])
        if not raw:
            raise err("bad-args", "Pick at least one photo or video.")
        try:
            paths = [df.clean_dcim_path(p) for p in raw]
            dest = df.expand_dest(a.get("dest"), Path.home() / "Pictures" / "iPhone")
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        dest.mkdir(parents=True, exist_ok=True)
        tid = str(a.get("transferId") or tr.new_id("m"))
        convert = bool(a.get("convertHeic"))
        with tr.track(tid, ctx.client(), "media.pull"):
            items = [BY_PATH.get(p) for p in paths]
            prog = df.Progress(event, tid, "media.pull", sum(m["size"] for m in items if m) // 20, len(paths),
                               {"dest": str(dest)})
            prog.send(force=True)
            files, skipped, failed, converted = [], [], [], 0
            try:
                for p, m in zip(paths, items):
                    prog.start_file(p.rsplit("/", 1)[-1])
                    if not m:
                        failed.append({"path": p, "message": "It isn't on your iPhone anymore."})
                        prog.file_done()
                        continue
                    stem = os.path.splitext(m["name"])[0]
                    # demo stand-in: a painted PNG (never real image data from anywhere)
                    if (dest / (stem + ".png")).exists():   # imported before
                        skipped.append(str(dest / (stem + ".png")))
                        prog.add(m["size"] // 20)
                        prog.file_done()
                        continue
                    target = dest / (stem + ".png")
                    await _fake_bytes(m["size"] // 20, prog)
                    await asyncio.to_thread(demo_art.photo, target, m["seed"], 480, 360)
                    if convert and df.is_heic(m["name"]):
                        converted += 1
                    files.append(str(target))
                    prog.file_done()
            except asyncio.CancelledError:
                prog.send("cancelled", force=True)
                raise
            prog.send("done", force=True)
            return {"transferId": tid, "dest": str(dest), "files": files, "skipped": skipped, "failed": failed,
                    "bytes": prog.bytes, "converted": converted, "notConverted": 0,
                    "seconds": round(time.monotonic() - prog.started, 1)}

    @reg.command("container.list")
    def container_list(ctx, a):
        info, docs, root, base = _access(a)
        try:
            path = df.clean_phone_path(a.get("path") or root, root)
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        d = _local(base, path)
        if not d.exists():
            raise err("not-found", "This folder isn't on your iPhone anymore.")
        if not d.is_dir():
            raise err("bad-args", "That's a file, not a folder.")
        items = [_entry(base, p) for p in d.iterdir() if p.name != ".keep" and not p.name.endswith(".part")]
        items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return {"bundleId": info["bundleId"], "name": info["name"], "path": path, "root": root,
                "mode": "documents" if docs else "container", "developer": info["developer"],
                "fileSharing": info["fileSharing"], "writable": True, "entries": items}

    @reg.command("container.pull")
    async def container_pull(ctx, a):
        info, docs, root, base = _access(a)
        try:
            paths = [df.clean_phone_path(p, root) for p in (a.get("paths") or ([a["path"]] if a.get("path") else []))]
            dest = df.expand_dest(a.get("dest"), Path.home() / "Downloads")
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        if not paths:
            raise err("bad-args", "Pick at least one file or folder.")
        dest.mkdir(parents=True, exist_ok=True)
        tid = str(a.get("transferId") or tr.new_id("c"))
        with tr.track(tid, ctx.client(), "container.pull"):
            plan = []
            for p in paths:
                src = _local(base, p)
                if not src.exists():
                    raise err("not-found", "That file isn't on your iPhone anymore.")
                top = src.parent
                for f in ([src] if src.is_file() else sorted(x for x in src.rglob("*") if x.is_file())):
                    plan.append((f, dest / f.relative_to(top)))
            prog = df.Progress(event, tid, "container.pull", sum(f.stat().st_size for f, _ in plan), len(plan),
                               {"bundleId": info["bundleId"], "dest": str(dest)})
            prog.send(force=True)
            files = []
            try:
                for src, dst in plan:
                    prog.start_file(src.name)
                    target = dst if not dst.exists() else df.unique_path(dst.parent, dst.name)
                    await _copy(src, target, prog)
                    files.append(str(target))
                    prog.file_done()
            except asyncio.CancelledError:
                prog.send("cancelled", force=True)
                raise
            prog.send("done", force=True)
            return {"transferId": tid, "dest": str(dest), "files": files, "bytes": prog.bytes,
                    "seconds": round(time.monotonic() - prog.started, 1)}

    @reg.command("container.push")
    async def container_push(ctx, a):
        info, docs, root, base = _access(a)
        srcs = a.get("srcs") or ([a["src"]] if a.get("src") else [])
        try:
            folder = df.clean_phone_path(a.get("path") or root, root)
            plan = df.local_tree([str(x) for x in srcs])
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        if not plan:
            raise err("bad-args", "Pick at least one file to copy to the iPhone.")
        targets = [folder.rstrip("/") + "/" + rel for _, rel in plan]
        if not a.get("overwrite"):
            clash = [t for t in targets if _local(base, t).exists()]
            if clash:
                raise err("exists", f"{len(clash)} of these already exist on the iPhone. Replace them?", None,
                          "\n".join(clash[:50]))
        tid = str(a.get("transferId") or tr.new_id("p"))
        with tr.track(tid, ctx.client(), "container.push"):
            prog = df.Progress(event, tid, "container.push", sum(f.stat().st_size for f, _ in plan), len(plan),
                               {"bundleId": info["bundleId"], "path": folder})
            prog.send(force=True)
            try:
                for (src, _), t in zip(plan, targets):
                    prog.start_file(src.name)
                    await _copy(src, _local(base, t), prog)
                    prog.file_done()
            except asyncio.CancelledError:
                prog.send("cancelled", force=True)
                raise
            prog.send("done", force=True)
            return {"transferId": tid, "path": folder, "files": targets, "bytes": prog.bytes,
                    "seconds": round(time.monotonic() - prog.started, 1)}

    @reg.command("container.mkdir")
    def container_mkdir(ctx, a):
        info, docs, root, base = _access(a)
        try:
            path = df.clean_phone_path(a.get("path") or "", root)
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        if path == root:
            raise err("bad-args", "Name the new folder.")
        p = _local(base, path)
        if p.exists():
            raise err("exists", "A file or folder with that name already exists.")
        p.mkdir(parents=True)
        return {"path": path}

    @reg.command("container.delete")
    def container_delete(ctx, a):
        info, docs, root, base = _access(a)
        try:
            paths = [df.clean_phone_path(p, root) for p in (a.get("paths") or [])]
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        if not paths or any(p in ("/", root) for p in paths):
            raise err("bad-args", "Pick the files or folders to delete (not the whole container).")
        deleted = []
        for p in paths:
            q = _local(base, p)
            if q.is_dir():
                shutil.rmtree(q)
            elif q.exists():
                q.unlink()
            deleted.append(p)
        return {"deleted": deleted, "failed": []}

    @reg.command("transfer.cancel")
    async def transfer_cancel(ctx, a):   # async: Task.cancel() must run on the event loop
        tid = str(a.get("transferId") or a.get("id") or "")
        return {"transferId": tid, "cancelled": tr.cancel(tid)}

    reg.command("transfer.list", lambda ctx, a: tr.describe())

    @reg.on_client_gone
    def gone(ctx, client):
        tr.cancel_client(client)
