"""console.launch / read / list / stop / clear: an app's stdout and stderr (Swift print()).

CoreDevice attaches a launched process's stdio to a socket from com.apple.coredevice.openstdiosocket
(pymobiledevice3's OpenStdioSocketService, the same thing `pymobiledevice3 developer core-device
launch-application --console` and `devicectl ... --console` use). The socket's 16-byte identifier
goes into the launch request; the phone then writes the app's output to it through a
pseudoterminal (so stdout is line-buffered like in a terminal, lines end in CR LF) and closes it when
the app exits. The commands and buffers are in helper/app_console.py.

stdout and stderr share one socket by default, so they arrive interleaved as stream "out".
`splitStderr: true` (experimental) asks for a second socket for stderr; if the phone refuses, the
launch falls back to the shared one.
"""
from __future__ import annotations

import plistlib
import uuid

import app_console
from deviced_core import dlog, emit, err

from pymobiledevice3.remote.core_device.open_stdio_socket import OpenStdioSocketService

READ_CHUNK = 64 * 1024


def _launch_input(bid: str, args: list, env: dict, out_id: uuid.UUID, err_id: uuid.UUID) -> dict:
    """AppServiceService.launch_application's request with separate stdout and stderr sockets."""
    return {
        "applicationSpecifier": {"bundleIdentifier": {"_0": bid}},
        "options": {
            "arguments": args,
            "environmentVariables": env,
            "standardIOUsesPseudoterminals": True,
            "startStopped": False,
            "terminateExisting": True,
            "user": {"shortName": "mobile"},
            "platformSpecificOptions": plistlib.dumps({}),
        },
        "standardIOIdentifiers": {"standardInput": out_id, "standardOutput": out_id, "standardError": err_id},
    }


class RealBackend:
    emit = staticmethod(emit)
    err = staticmethod(err)

    def __init__(self, reg):
        self.reg = reg

    async def plain_launch(self, d, a):
        fn = d.registry.lookup("app.launch")
        return await fn(d, {**a, "console": False})

    async def _open(self, rsd) -> OpenStdioSocketService:
        stdio = OpenStdioSocketService(rsd)
        try:
            await stdio.connect()
        except BaseException:
            await stdio.close()
            raise
        return stdio

    async def launch(self, d, sess, a):
        s, rsd = await d.rsd(a)
        await s.ensure_ddi()
        try:
            out = await self._open(rsd)
        except Exception as exc:  # noqa: BLE001 - older iOS, no service: launch without the console
            dlog(f"console: can't open the stdio socket: {exc!r}")
            raise app_console.ConsoleUnavailable("this iPhone didn't open a console for the app.") from exc
        socks = [out]
        try:
            if a.get("splitStderr"):
                res = await self._launch_split(d, rsd, a, out, socks)
            else:
                res = None
            if res is None:
                # app.launch (deviced_features/apps.py) does the launch and its error messages
                res = await d.registry.lookup("app.launch")(d, {**a, "console": False, "_stdio": out.identifier})
        except BaseException:
            for sock in socks:
                await sock.close()
            raise
        readers = [app_console.Reader("out" if len(socks) == 1 else "stdout", lambda: out.read(READ_CHUNK), out.close)]
        if len(socks) == 2:
            e = socks[1]
            readers.append(app_console.Reader("stderr", lambda: e.read(READ_CHUNK), e.close))
        return res, readers

    async def _launch_split(self, d, rsd, a, out, socks):
        """Launch with stderr on its own socket. None (and only `out` left open) if the phone refuses."""
        try:
            errs = await self._open(rsd)
        except Exception as exc:  # noqa: BLE001
            dlog(f"console: no second stdio socket, stdout and stderr stay together: {exc!r}")
            return None
        socks.append(errs)
        bid = str(a["bundleId"])
        try:
            async with d.pmd3.AppServiceService(rsd) as svc:
                res = await svc.invoke("com.apple.coredevice.feature.launchapplication",
                                       _launch_input(bid, list(a.get("args") or []), dict(a.get("env") or {}),
                                                     out.identifier, errs.identifier))
        except Exception as exc:  # noqa: BLE001 - fall back to one socket (and app.launch's error messages)
            dlog(f"console: split launch failed, retrying with one socket: {exc!r}")
            socks.remove(errs)
            await errs.close()
            return None
        pid = None
        tok = res.get("processToken") if isinstance(res, dict) else None
        if isinstance(tok, dict):
            pid = tok.get("processIdentifier")
        return {"bundleId": bid, "pid": pid}


def register(reg):
    app_console.register_commands(reg, RealBackend(reg))
