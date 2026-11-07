"""Files: the camera roll (media.*, AFC, read-only) and app containers (container.*, house_arrest),
with cancellable transfers (transfer.cancel) that report `transfer` progress events.

Camera roll: DCIM is listed directly (fast) and joined with a cached copy of PhotoData/Photos.sqlite
for dates, sizes and kinds. The copy is refreshed only when the database's size or mtime changes;
if its schema lacks the columns we read, the plain DCIM listing is used. Nothing here ever writes
to DCIM or PhotoData.

Containers: your own development builds (get-task-allow) vend the whole container
(VendContainer: Documents, Library, tmp; some entries are denied and skipped). App Store apps with
UIFileSharingEnabled vend Documents only (VendDocuments, paths under /Documents). Other apps can't
be opened ("This app doesn't share files"). Writes (push, mkdir, delete) are allowed for both.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import posixpath
import time
from pathlib import Path

from pymobiledevice3.exceptions import (
    AfcException,
    AfcFileNotFoundError,
    AppNotInstalledError,
    PyMobileDevice3Exception,
)
from pymobiledevice3.services.house_arrest import HouseArrestService

import device_files as df
from common import CACHE_DIR, HOME, fix
from deviced_core import classify, dlog, emit, err, installed_apps, safe_name
from deviced_core import is_connection_error as _is_conn

MEDIA_DIR = CACHE_DIR / "media"
THUMBS_DIR = CACHE_DIR / "thumbs"
PHOTOS_DB = "/PhotoData/Photos.sqlite"
INDEX_TTL = 15.0          # seconds a DCIM listing is reused between media.list pages
THUMB_SEM = asyncio.Semaphore(4)
STAT_SEM = 16             # concurrent AFC stats while listing a folder
PULL_FALLBACK_MAX = 80 << 20   # biggest original pulled just to make a thumbnail

NO_SHARING = "This app doesn't share files. Your own apps (built here) always do; App Store apps only when they turn on file sharing."


# ---------------------------------------------------------------- errors

def is_connection_error(exc: BaseException) -> bool:
    """AfcException is an OSError, which classify() reads as a dropped link; it never is one."""
    return not isinstance(exc, (AfcException, PyMobileDevice3Exception)) and _is_conn(exc)


def afc_error(exc: BaseException, what: str = "that file") -> BaseException:
    """Plain-language error for an AFC / house_arrest failure (connection errors pass through)."""
    if isinstance(exc, df.PathError):
        return err("bad-args", str(exc))
    if isinstance(exc, AppNotInstalledError):
        return err("not-found", "That app isn't installed on your iPhone anymore.", None, str(exc))
    if isinstance(exc, AfcFileNotFoundError):
        return err("not-found", f"{what[:1].upper() + what[1:]} isn't on your iPhone anymore.", None, str(exc))
    text = str(exc)
    if "InstallationLookupFailed" in text or "refused to open the container" in text:
        return err("no-file-sharing", NO_SHARING, None, text)
    if isinstance(exc, AfcException):
        st = int(exc.status) if exc.status is not None else -1
        if st == 10:     # PERM_DENIED
            return err("denied", f"Your iPhone doesn't allow opening {what}.", None, text)
        if st == 16:     # OBJECT_EXISTS
            return err("exists", f"{what[:1].upper() + what[1:]} already exists.", None, text)
        if st == 18:     # NO_SPACE_LEFT
            return err("no-space", "Your iPhone is out of storage.", None, text)
        if st == 9:      # OBJECT_IS_DIR
            return err("bad-args", f"{what[:1].upper() + what[1:]} is a folder.", None, text)
        return err("failed", f"Your iPhone couldn't open {what}.", None, text)
    if isinstance(exc, PyMobileDevice3Exception):
        return err("failed", f"Your iPhone couldn't open {what}.", None, text)
    return exc


def _mtime(st: dict) -> float | None:
    m = st.get("st_mtime")
    with contextlib.suppress(Exception):
        return m.timestamp()
    return None


def entry(path: str, st: dict | None) -> dict:
    name = posixpath.basename(path) or "/"
    if st is None:
        return {"name": name, "path": path, "type": "unknown", "size": None, "mtime": None, "denied": True}
    t = {"S_IFDIR": "dir", "S_IFREG": "file", "S_IFLNK": "link"}.get(st.get("st_ifmt"), "other")
    return {"name": name, "path": path, "type": t, "size": int(st.get("st_size") or 0) if t == "file" else None,
            "mtime": _mtime(st)}


async def stat_many(afc, paths: list[str]) -> list[dict | None]:
    sem = asyncio.Semaphore(STAT_SEM)

    async def one(p):
        async with sem:
            try:
                return await afc.stat(p)
            except AfcException:
                return None
    return await asyncio.gather(*(one(p) for p in paths))


# ---------------------------------------------------------------- AFC file copy

async def read_to(afc, remote: str, local: Path, size: int, prog: df.Progress) -> None:
    """Copy one phone file to `local` (through a .part file) in CHUNK pieces."""
    local.parent.mkdir(parents=True, exist_ok=True)
    tmp = df.part_path(local)
    h = await afc.fopen(remote, "r")
    try:
        with open(tmp, "wb") as f:
            left = size
            while left > 0:
                chunk = await afc.fread(h, min(df.CHUNK, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
                prog.add(len(chunk))
        os.replace(tmp, local)
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.shield(afc.fclose(h))
        with contextlib.suppress(OSError):
            tmp.unlink()


async def write_from(afc, local: Path, remote: str, prog: df.Progress) -> None:
    h = await afc.fopen(remote, "w")
    try:
        with open(local, "rb") as f:
            while True:
                chunk = f.read(df.CHUNK)
                if not chunk:
                    break
                await afc.fwrite(h, chunk)
                prog.add(len(chunk))
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.shield(afc.fclose(h))


async def walk_remote(afc, top: str, limit: int = 20000) -> list[tuple[str, int]]:
    """[(file path, size)] under `top` (a file gives itself). Unreadable entries are skipped."""
    st = await afc.stat(top)
    if st.get("st_ifmt") != "S_IFDIR":
        return [(top, int(st.get("st_size") or 0))]
    out: list[tuple[str, int]] = []
    todo = [top]
    while todo and len(out) < limit:
        d = todo.pop()
        try:
            names = await afc.listdir(d)
        except AfcException:
            continue
        paths = [posixpath.join(d, n) for n in names]
        for p, s in zip(paths, await stat_many(afc, paths)):
            if s is None:
                continue
            if s.get("st_ifmt") == "S_IFDIR":
                todo.append(p)
            elif s.get("st_ifmt") == "S_IFREG":
                out.append((p, int(s.get("st_size") or 0)))
    return out


async def pull_tree(afc, items: list[str], dest: Path, prog: df.Progress, root: str) -> list[str]:
    """Copy phone files/folders into dest, keeping folder structure under each item."""
    plan: list[tuple[str, Path, int]] = []
    for top in items:
        files = await walk_remote(afc, top)
        base = posixpath.dirname(top.rstrip("/")) or "/"
        for p, size in files:
            rel = posixpath.relpath(p, base)
            plan.append((p, dest.joinpath(*rel.split("/")), size))
    prog.total = sum(s for _, _, s in plan)
    prog.count = len(plan)
    prog.send(force=True)
    written: list[str] = []
    for remote, local, size in plan:
        prog.start_file(posixpath.basename(remote))
        target = local if not local.exists() else df.unique_path(local.parent, local.name)
        await read_to(afc, remote, target, size, prog)
        written.append(str(target))
        prog.file_done()
    return written


# ---------------------------------------------------------------- app info

async def app_info(d, s, bid: str) -> dict:
    """{bundleId, name, developer, fileSharing, root} for an installed app (cached per tunnel)."""
    cache = s.tunnel_data.setdefault("files.apps", {})
    if bid in cache:
        return cache[bid]
    rsd = await s.get_rsd()
    apps = await installed_apps(d.pmd3, rsd, application_type="Any", bundle_identifiers=[bid])
    info = apps.get(bid)
    if not info:
        raise err("not-found", "That app isn't installed on your iPhone.")
    ent = info.get("Entitlements") or {}
    signer = str(info.get("SignerIdentity") or "")
    dev = bool(ent.get("get-task-allow")) or signer.startswith(("Apple Development", "iPhone Developer"))
    sharing = bool(info.get("UIFileSharingEnabled"))
    out = {"bundleId": bid, "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or bid,
           "developer": dev, "fileSharing": sharing, "executable": info.get("CFBundleExecutable") or "",
           "root": "/" if dev else "/Documents"}
    cache[bid] = out
    return out


async def resolve_access(d, s, a: dict) -> tuple[dict, bool, str]:
    """(app info, documents mode, root) for a container command; raises no-file-sharing."""
    bid = str(a.get("bundleId") or "")
    if not bid:
        raise err("bad-args", "Which app? (bundleId missing)")
    info = await app_info(d, s, bid)
    if not info["developer"] and not info["fileSharing"]:
        raise err("no-file-sharing", NO_SHARING)
    docs = bool(a.get("documentsOnly")) or not info["developer"]
    return info, docs, "/Documents" if docs else "/"


async def cached_container(s, bid: str, docs: bool):
    return await s.cached_service(f"files.ha:{bid}:{int(docs)}", HouseArrestService.create, bid, documents_only=docs)


async def forget_container(s, bid: str, docs: bool):
    await s.forget_service(f"files.ha:{bid}:{int(docs)}")


# ---------------------------------------------------------------- camera roll index

async def dcim_listing(afc) -> list[str]:
    out = []
    for folder in await afc.listdir("/DCIM"):
        if folder.startswith("."):
            continue
        try:
            names = await afc.listdir(f"/DCIM/{folder}")
        except AfcException:
            continue
        out.extend(f"DCIM/{folder}/{n}" for n in names if not n.startswith(".") and df.media_kind(n))
    return out


async def photos_db(afc, s) -> list[dict] | None:
    """Rows of the cached Photos.sqlite copy; re-pulled when the DB or its WAL changed."""
    key = []
    for suffix in ("", "-wal"):
        try:
            st = await afc.stat(PHOTOS_DB + suffix)
            key.append((int(st.get("st_size") or 0), _mtime(st)))
        except AfcException:
            if not suffix:
                return None
            key.append(None)
    folder = MEDIA_DIR / safe_name(s.udid)
    mem = s.data.get("files.db")
    if mem and mem["key"] == key:
        return mem["rows"]
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    tmp = folder / "pull"
    tmp.mkdir(exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        try:
            data = await afc.get_file_contents(PHOTOS_DB + suffix)
        except AfcException:
            data = None
        p = tmp / ("Photos.sqlite" + suffix)
        if data is None:
            with contextlib.suppress(OSError):
                p.unlink()
        else:
            p.write_bytes(data)
    rows = await asyncio.to_thread(df.read_photos_db, tmp / "Photos.sqlite")
    dlog(f"media: Photos.sqlite copied and read in {time.monotonic() - t0:.1f} s "
         f"({'%d assets' % len(rows) if rows is not None else 'schema not recognised, using DCIM only'})")
    s.data["files.db"] = {"key": key, "rows": rows}
    return rows


async def media_index(d, s, refresh: bool = False) -> dict:
    mem = s.tunnel_data.get("files.index")
    if mem and not refresh and time.monotonic() - mem["at"] < INDEX_TTL:
        return mem
    afc = await s.cached_service("afc", d.pmd3.AfcService)
    try:
        dcim = await dcim_listing(afc)
        try:
            rows = await photos_db(afc, s)
        except Exception as exc:  # noqa: BLE001  the DB is a bonus; DCIM alone still works
            if is_connection_error(exc):
                raise
            dlog(f"media: Photos.sqlite unusable: {exc!r}")
            rows = None
    except BaseException as exc:
        if is_connection_error(exc):
            await s.forget_service("afc")
            raise
        raise afc_error(exc, "the camera roll")
    items = df.merge_index(rows, dcim)
    thumbs = THUMBS_DIR / safe_name(s.udid)
    for it in items:
        t = thumbs / (safe_name(it["path"]) + ".jpg")
        it["thumb"] = str(t) if t.exists() else None
    mem = {"at": time.monotonic(), "items": items, "source": "photos-db" if rows is not None else "dcim",
           "byPath": {it["path"]: it for it in items}}
    s.tunnel_data["files.index"] = mem
    return mem


def thumb_path(udid: str, path: str) -> Path:
    return THUMBS_DIR / safe_name(udid) / (safe_name(path) + ".jpg")


async def make_thumb(d, s, path: str, item: dict | None) -> dict:
    local = thumb_path(s.udid, path)
    if local.exists():
        return {"path": str(local), "source": "cache"}
    async with THUMB_SEM:
        if local.exists():
            return {"path": str(local), "source": "cache"}
        afc = await s.cached_service("afc", d.pmd3.AfcService)
        for cand in (f"/PhotoData/Thumbnails/V2/{path}/5005.JPG",
                     f"/PhotoData/Thumbnails/VideoKeyFrames/{path}.JPG"):
            try:
                data = await afc.get_file_contents(cand)
            except AfcException:
                continue
            if data[:2] == b"\xff\xd8":
                local.parent.mkdir(parents=True, exist_ok=True)
                tmp = local.with_name("." + local.name)
                tmp.write_bytes(data)
                os.replace(tmp, local)
                return {"path": str(local), "source": "phone"}
        # No phone thumbnail: pull the original (if it's not huge) and make one here.
        try:
            st = await afc.stat("/" + path)
        except AfcException as exc:
            raise afc_error(exc, "that photo")
        size = int(st.get("st_size") or 0)
        if size > PULL_FALLBACK_MAX:
            raise err("no-thumbnail", "No preview for this one; import it to see it.")
        tmp_dir = MEDIA_DIR / safe_name(s.udid) / "thumb-src"
        src = tmp_dir / posixpath.basename(path)
        prog = df.Progress(lambda *_: None, "thumb", "thumb", size, 1)
        await read_to(afc, "/" + path, src, size, prog)
        try:
            ok = await asyncio.to_thread(df.thumbnail, src, local, df.media_kind(path) == "video")
        finally:
            with contextlib.suppress(OSError):
                src.unlink()
        if not ok:
            raise err("no-converter", "Previews for this kind of file need image tools that aren't installed.",
                      fix("Install", "setup", step="packages"))
        return {"path": str(local), "source": "generated"}


# ---------------------------------------------------------------- module

def register(reg):
    tr = df.Transfers()
    reg.provide("transfers", tr)

    # ------------------------------------------------ camera roll
    @reg.command("media.list")
    async def c_media_list(d, a):
        s = d.need_session(a)
        idx = await media_index(d, s, bool(a.get("refresh")))
        items = idx["items"]
        kind = a.get("kind")
        if kind in ("photo", "video"):
            items = [it for it in items if it["kind"] == kind]
        off = max(0, int(a.get("offset") or 0))
        lim = max(1, min(int(a.get("limit") or 200), 1000))
        page = [{k: it.get(k) for k in ("id", "path", "name", "kind", "date", "ts", "width", "height", "duration",
                                        "size", "thumb")} for it in items[off:off + lim]]
        return {"total": len(items), "offset": off, "items": page, "source": idx["source"],
                "photos": sum(1 for it in idx["items"] if it["kind"] == "photo"),
                "videos": sum(1 for it in idx["items"] if it["kind"] == "video")}

    @reg.command("media.thumb")
    async def c_media_thumb(d, a):
        s = d.need_session(a)
        try:
            path = df.clean_dcim_path(a.get("path") or a.get("id") or "")
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        try:
            return {"id": path, **await make_thumb(d, s, path, None)}
        except BaseException as exc:
            if is_connection_error(exc):
                await s.forget_service("afc")
                raise
            raise afc_error(exc, "that photo")

    @reg.command("media.pull")
    async def c_media_pull(d, a):
        s = d.need_session(a)
        raw = list(a.get("ids") or a.get("paths") or [])
        if not raw:
            raise err("bad-args", "Pick at least one photo or video.")
        try:
            paths = [df.clean_dcim_path(p) for p in raw]
            dest = df.expand_dest(a.get("dest"), HOME / "Pictures" / "iPhone")
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        dest.mkdir(parents=True, exist_ok=True)
        convert = bool(a.get("convertHeic"))
        tid = str(a.get("transferId") or tr.new_id("m"))
        idx = s.tunnel_data.get("files.index") or {}
        known = idx.get("byPath") or {}
        with tr.track(tid, d.client(), "media.pull"):
            prog = df.Progress(emit, tid, "media.pull", None, len(paths), {"dest": str(dest)})
            prog.send(force=True)
            files, skipped, failed = [], [], []
            converted = not_converted = 0
            try:
                async with s.open_service(d.pmd3.AfcService) as afc:
                    sizes = {}
                    for p in paths:
                        sz = (known.get(p) or {}).get("size")
                        if sz is None:
                            try:
                                sz = int((await afc.stat("/" + p)).get("st_size") or 0)
                            except AfcException:
                                sz = None
                        sizes[p] = sz
                    prog.total = sum(v or 0 for v in sizes.values())
                    for p in paths:
                        name = posixpath.basename(p)
                        prog.start_file(name)
                        if sizes[p] is None:
                            failed.append({"path": p, "message": "It isn't on your iPhone anymore."})
                            prog.file_done()
                            continue
                        want_jpg = convert and df.is_heic(name)
                        final_name = os.path.splitext(name)[0] + ".jpg" if want_jpg else name
                        existing = dest / final_name
                        if existing.exists() and (want_jpg or existing.stat().st_size == sizes[p]):
                            skipped.append(str(existing))   # imported before
                            prog.add(sizes[p])
                            prog.file_done()
                            continue
                        target = df.unique_path(dest, name)
                        await read_to(afc, "/" + p, target, sizes[p], prog)
                        if want_jpg:
                            jpg = df.unique_path(dest, final_name)
                            if await asyncio.to_thread(df.to_jpeg, target, jpg):
                                target.unlink(missing_ok=True)
                                target = jpg
                                converted += 1
                            else:
                                not_converted += 1
                        files.append(str(target))
                        prog.file_done()
            except asyncio.CancelledError:
                prog.send("cancelled", force=True)
                raise
            except BaseException as exc:  # noqa: BLE001
                e = afc_error(exc, "that photo")
                prog.send("failed", force=True, error=classify(e).to_json())
                raise e
            prog.send("done", force=True)
            return {"transferId": tid, "dest": str(dest), "files": files, "skipped": skipped, "failed": failed,
                    "bytes": prog.bytes, "converted": converted, "notConverted": not_converted,
                    "seconds": round(time.monotonic() - prog.started, 1)}

    # ------------------------------------------------ app containers
    @reg.command("container.list")
    async def c_container_list(d, a):
        s = d.need_session(a)
        info, docs, root = await resolve_access(d, s, a)
        try:
            path = df.clean_phone_path(a.get("path") or root, root)
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        try:
            afc = await cached_container(s, info["bundleId"], docs)
            names = await afc.listdir(path)
            paths = [posixpath.join(path, n) for n in names]
            stats = await stat_many(afc, paths)
        except BaseException as exc:
            if is_connection_error(exc) or isinstance(exc, (AfcException, PyMobileDevice3Exception)):
                await forget_container(s, info["bundleId"], docs)
            raise afc_error(exc, "this folder")
        items = [entry(p, st) for p, st in zip(paths, stats)]
        items = [x for x in items if not (path == "/" and x["name"] in ("SystemData",) and x.get("denied"))]
        items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return {"bundleId": info["bundleId"], "name": info["name"], "path": path, "root": root,
                "mode": "documents" if docs else "container", "developer": info["developer"],
                "fileSharing": info["fileSharing"], "writable": True, "entries": items}

    @reg.command("container.pull")
    async def c_container_pull(d, a):
        s = d.need_session(a)
        info, docs, root = await resolve_access(d, s, a)
        try:
            paths = [df.clean_phone_path(p, root) for p in (a.get("paths") or ([a["path"]] if a.get("path") else []))]
            dest = df.expand_dest(a.get("dest"), HOME / "Downloads")
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        if not paths:
            raise err("bad-args", "Pick at least one file or folder.")
        dest.mkdir(parents=True, exist_ok=True)
        tid = str(a.get("transferId") or tr.new_id("c"))
        with tr.track(tid, d.client(), "container.pull"):
            prog = df.Progress(emit, tid, "container.pull", None, 0, {"bundleId": info["bundleId"], "dest": str(dest)})
            try:
                async with s.house_arrest(info["bundleId"], documents=docs) as afc:
                    files = await pull_tree(afc, paths, dest, prog, root)
            except asyncio.CancelledError:
                prog.send("cancelled", force=True)
                raise
            except BaseException as exc:  # noqa: BLE001
                e = afc_error(exc, "that file")
                prog.send("failed", force=True, error=classify(e).to_json())
                raise e
            prog.send("done", force=True)
            return {"transferId": tid, "dest": str(dest), "files": files, "bytes": prog.bytes,
                    "seconds": round(time.monotonic() - prog.started, 1)}

    @reg.command("container.push")
    async def c_container_push(d, a):
        s = d.need_session(a)
        info, docs, root = await resolve_access(d, s, a)
        srcs = a.get("srcs") or ([a["src"]] if a.get("src") else [])
        try:
            folder = df.clean_phone_path(a.get("path") or root, root)
            plan = df.local_tree([str(x) for x in srcs])
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        if not plan:
            raise err("bad-args", "Pick at least one file to copy to the iPhone.")
        tid = str(a.get("transferId") or tr.new_id("p"))
        overwrite = bool(a.get("overwrite"))
        with tr.track(tid, d.client(), "container.push"):
            prog = df.Progress(emit, tid, "container.push", sum(f.stat().st_size for f, _ in plan), len(plan),
                               {"bundleId": info["bundleId"], "path": folder})
            try:
                async with s.house_arrest(info["bundleId"], documents=docs) as afc:
                    if not await afc.exists(folder):
                        await afc.makedirs(folder)
                    targets = [posixpath.join(folder, rel) for _, rel in plan]
                    if not overwrite:
                        clash = [t for t, st in zip(targets, await stat_many(afc, targets)) if st is not None]
                        if clash:
                            raise err("exists", f"{len(clash)} of these already exist on the iPhone. Replace them?",
                                      None, "\n".join(clash[:50]))
                    prog.send(force=True)
                    made = {folder}
                    for (src, _), target in zip(plan, targets):
                        parent = posixpath.dirname(target)
                        if parent not in made:
                            await afc.makedirs(parent)
                            made.add(parent)
                        prog.start_file(src.name)
                        await write_from(afc, src, target, prog)
                        prog.file_done()
            except asyncio.CancelledError:
                prog.send("cancelled", force=True)
                raise
            except BaseException as exc:  # noqa: BLE001
                e = afc_error(exc, "that folder")
                prog.send("failed", force=True, error=classify(e).to_json())
                raise e
            await forget_container(s, info["bundleId"], docs)   # browsing reopens with fresh listings
            prog.send("done", force=True)
            return {"transferId": tid, "path": folder, "files": targets, "bytes": prog.bytes,
                    "seconds": round(time.monotonic() - prog.started, 1)}

    @reg.command("container.mkdir")
    async def c_container_mkdir(d, a):
        s = d.need_session(a)
        info, docs, root = await resolve_access(d, s, a)
        try:
            path = df.clean_phone_path(a.get("path") or "", root)
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        if path == root:
            raise err("bad-args", "Name the new folder.")
        try:
            afc = await cached_container(s, info["bundleId"], docs)
            if await afc.exists(path):
                raise err("exists", "A file or folder with that name already exists.")
            await afc.makedirs(path)
        except BaseException as exc:
            if is_connection_error(exc):
                await forget_container(s, info["bundleId"], docs)
            raise afc_error(exc, "that folder")
        return {"path": path}

    @reg.command("container.delete")
    async def c_container_delete(d, a):
        s = d.need_session(a)
        info, docs, root = await resolve_access(d, s, a)
        try:
            paths = [df.clean_phone_path(p, root) for p in (a.get("paths") or [])]
        except df.PathError as exc:
            raise err("bad-args", str(exc))
        if not paths or any(p in ("/", root) for p in paths):
            raise err("bad-args", "Pick the files or folders to delete (not the whole container).")
        deleted, failed = [], []
        try:
            afc = await cached_container(s, info["bundleId"], docs)
            for p in paths:
                try:
                    left = await afc.rm(p, force=True)
                except AfcFileNotFoundError:
                    left = []
                (failed if left else deleted).append(p)
        except BaseException as exc:
            if is_connection_error(exc):
                await forget_container(s, info["bundleId"], docs)
            raise afc_error(exc, "that file")
        return {"deleted": deleted, "failed": failed}

    # ------------------------------------------------ transfers
    @reg.command("transfer.cancel")
    async def c_transfer_cancel(d, a):
        tid = str(a.get("transferId") or a.get("id") or "")
        return {"transferId": tid, "cancelled": tr.cancel(tid)}

    @reg.command("transfer.list")
    async def c_transfer_list(d, a):
        return tr.describe()

    @reg.on_client_gone
    def gone(d, client):
        n = tr.cancel_client(client)
        if n:
            dlog(f"files: cancelled {n} transfer(s) of a client that left")
