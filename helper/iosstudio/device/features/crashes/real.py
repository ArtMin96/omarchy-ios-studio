"""crash.list, crash.pull (crash reports, used by the Logs page and the MCP server)."""
from __future__ import annotations

import asyncio
import contextlib
import posixpath

from iosstudio.device.features.crashes import core as device_crash
from iosstudio.device.real.core import CRASH_DIR, err, reclassify, safe_name, write_bytes
from iosstudio.features import crash_symbols


def register(reg):
    @reg.command("crash.list")
    async def c_crash_list(d, a):
        s, _ = await d.rsd(a)
        want = (a.get("process") or "").strip()
        async with s.open_service(d.pmd3.CrashReportsManager) as c:
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
        s, _ = await d.rsd(a)
        local = CRASH_DIR / safe_name(s.udid) / safe_name(posixpath.basename(name))
        if not local.exists():
            async with s.open_service(d.pmd3.CrashReportsManager) as c:
                try:
                    data = await c.afc.get_file_contents(name if name.startswith("/") else "/" + name)
                except Exception as exc:  # noqa: BLE001
                    raise reclassify(exc, "not-found", "That crash report is no longer on your iPhone.") from exc
            await asyncio.to_thread(write_bytes, local, data)
        text = await asyncio.to_thread(local.read_text, "utf-8", "replace")
        summary = device_crash.parse_ips(text)
        # file:line for the app's frames. The report still opens if this fails, and says why there are none.
        try:
            res = await asyncio.to_thread(crash_symbols.symbolicate_file, local)
        except Exception as exc:  # noqa: BLE001
            res = crash_symbols.make_result("none", "error", f"Couldn't work out file and line numbers "
                                            f"({type(exc).__name__}: {exc}).")
        with contextlib.suppress(Exception):
            crash_symbols.apply(summary, res)
        return {"path": str(local), "summary": summary}
