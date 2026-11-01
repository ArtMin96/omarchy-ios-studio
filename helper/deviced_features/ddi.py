"""ddi.status, ddi.mount, devmode.status, devmode.reveal."""
from __future__ import annotations

from deviced_core import err


def register(reg):
    @reg.command("ddi.status")
    async def c_ddi_status(d, a):
        s, rsd = await d.rsd(a)
        async with d.pmd3.MobileImageMounterService(lockdown=rsd) as m:
            mounted = bool(await m.is_image_mounted(d.pmd3.image_type_for_device(rsd)))
        s.info["ddiMounted"] = mounted
        return {"mounted": mounted}

    @reg.command("ddi.mount")
    async def c_ddi_mount(d, a):
        s, _ = await d.rsd(a)
        await s.ensure_ddi()
        return {"mounted": True}

    @reg.command("devmode.status")
    async def c_devmode_status(d, a):
        s, rsd = await d.rsd(a)
        enabled = bool(await rsd.get_developer_mode_status())
        s.info["developerMode"] = enabled
        return {"enabled": enabled}

    @reg.command("devmode.reveal")
    async def c_devmode_reveal(d, a):
        """Show the Developer Mode switch in Settings (needs lockdown: USB, or RSD if supported)."""
        s = d.need_session(a)
        if s.udid in d.usb:
            async with s.usb_lockdown() as lockdown:
                await d.pmd3.AmfiService(lockdown).reveal_developer_mode_option_in_ui()
            return {}
        rsd = await s.get_rsd()
        try:
            await d.pmd3.AmfiService(rsd).reveal_developer_mode_option_in_ui()
        except BaseException as exc:  # noqa: BLE001
            raise err("needs-usb", "To show the Developer Mode switch, plug your iPhone in with a cable once. "
                      "Or open Settings › Privacy & Security on the iPhone — the switch is at the bottom.",
                      None, repr(exc))
        return {}
