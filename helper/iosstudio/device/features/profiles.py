"""profiles.list (provisioning profiles installed on the phone)."""
from __future__ import annotations

from iosstudio.device import device_link as link
from iosstudio.device.deviced_core import dlog


def register(reg):
    @reg.command("profiles.list")
    async def c_profiles_list(d, a):
        s = d.need_session(a)
        async with s.open_service(d.pmd3.MisagentService) as m:
            raw = await m.copy_all()
        out = []
        for p in raw:
            try:
                plist = p.plist if hasattr(p, "plist") else link.parse_profile(bytes(p))
                out.append(link.profile_summary(plist))
            except (KeyError, TypeError, ValueError) as exc:
                dlog(f"profile skipped: {exc!r}")
        out.sort(key=lambda x: x["expires"], reverse=True)
        return out
