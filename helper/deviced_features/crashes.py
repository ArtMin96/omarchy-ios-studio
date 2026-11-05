"""crash.list, crash.pull (crash reports, used by the Logs page and the MCP server)."""
from __future__ import annotations

import asyncio
import contextlib
import posixpath

import crash_symbols
import device_crash
from deviced_core import CRASH_DIR, classify, err, safe_name, write_bytes


def register(reg):
    @reg.command("crash.list")
    async def c_crash_list(d, a):
        s, rsd = await d.rsd(a)
        want = (a.get("process") or "").strip()
        async with d.pmd3.CrashReportsManager(rsd) as c:
            if a.get("flush", True):
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(c.flush(), 20)
            paths = await c.ls("/", depth=2)
        out = []
        for p in paths:
            item = device_crash.classify(p, (s.info or {}).get("timeZone"))
            if not item:
                continue
            if want and item["process"] != want:
                continue
            out.append(item)
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out

    @reg.command("crash.pull")
    async def c_crash_pull(d, a):
        name = a.get("name") or ""
        if not name or ".." in name.split("/"):
            raise err("bad-args", "Which crash report? (name missing)")
        s, rsd = await d.rsd(a)
        local = CRASH_DIR / safe_name(s.udid) / safe_name(posixpath.basename(name))
        if not local.exists():
            async with d.pmd3.CrashReportsManager(rsd) as c:
                try:
                    data = await c.afc.get_file_contents(name if name.startswith("/") else "/" + name)
                except BaseException as exc:  # noqa: BLE001
                    e = classify(exc)
                    if e.code == "failed":
                        raise err("not-found", "That crash report is no longer on your iPhone.", None, repr(exc))
                    raise e
            await asyncio.to_thread(write_bytes, local, data)
        text = await asyncio.to_thread(local.read_text, "utf-8", "replace")
        summary = device_crash.parse_ips(text)
        with contextlib.suppress(Exception):  # file:line for the app's frames; the report still opens without
            crash_symbols.apply(summary, await asyncio.to_thread(crash_symbols.symbolicate_file, local))
        return {"path": str(local), "summary": summary}
