"""Transport to the iPhone for iosstudio-deviced: USB (usbmux) and Wi-Fi, both without root.

Adapted from omarchy-apple-dev and earlier helper scripts (verified with pymobiledevice3 11.49.0 on
iOS 27.2). Wi-Fi: the phone advertises _remotepairing._tcp over Bonjour; the answer is matched to
our RemotePairing record (~/.local/share/pymobiledevice3/remote_<udid>.plist) and pair-verified,
then a userspace TCP/IP stack (PyTCP) carries the tunnel, so there's no TUN device and no root.
USB: the CoreDeviceProxy lockdown service over usbmuxd on the same userspace stack.

PyTCP's stack is a process-global singleton, so the daemon keeps at most ONE tunnel open.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import logging
import plistlib
from datetime import UTC, datetime
from pathlib import Path

TESTED_PYMOBILEDEVICE3 = "11.49.0"
BONJOUR_TIMEOUT_S = 10
RSD_TIMEOUT_S = 30
LOCKDOWN_DIR = Path("/var/lib/lockdown")
REMOTE_DIR = Path.home() / ".local/share/pymobiledevice3"


class UnreachableError(Exception):
    """The phone did not answer over the requested transport."""


class NotPairedError(Exception):
    """No (valid) pairing for that transport."""


class ApiError(Exception):
    """The installed pymobiledevice3 lacks an API we use."""


def load_pmd3() -> argparse.Namespace:
    """Every pymobiledevice3 name the daemon uses (raises ApiError with details)."""
    try:
        version = importlib.metadata.version("pymobiledevice3")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ApiError("pymobiledevice3 isn't installed in this Python") from exc
    try:
        from pymobiledevice3 import exceptions, usbmux
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.pair_records import (
            get_remote_pairing_record_filename,
            iter_remote_pair_records_by_identifier,
        )
        from pymobiledevice3.remote import tunnel_service
        from pymobiledevice3.remote.core_device.app_service import AppServiceService
        from pymobiledevice3.remote.core_device.icon_service import IconService
        from pymobiledevice3.remote import userspace_tunnel
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
        from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane, UserspaceRsdTunnel
        from pymobiledevice3.services.afc import AfcService
        from pymobiledevice3.services.amfi import AmfiService
        from pymobiledevice3.services.crash_reports import CrashReportsManager
        from pymobiledevice3.services.diagnostics import DiagnosticsService
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
        from pymobiledevice3.services.misagent import MisagentService
        from pymobiledevice3.services.mobile_image_mounter import (
            MobileImageMounterService,
            auto_mount,
            image_type_for_device,
        )
        from pymobiledevice3.services.os_trace import OsTraceService

        for module, attr in (
            (tunnel_service, "get_remote_pairing_tunnel_services"),
            (tunnel_service, "RemotePairingLockdownService"),
            (tunnel_service, "USE_USERSPACE_TUNNEL"),
            (tunnel_service, "serve_pairable_host"),
            (tunnel_service, "PairableHostInfo"),
            (exceptions, "RemotePairingCompletedError"),
            (exceptions, "CoreDeviceError"),
            (exceptions, "ConnectionFailedToUsbmuxdError"),
            (exceptions, "ConnectionTerminatedError"),
            (exceptions, "PyMobileDevice3Exception"),
            (usbmux, "list_devices"),
            (AppServiceService, "list_processes"),
            (AppServiceService, "send_signal_to_process"),
            (IconService, "fetch_icon"),
            (CrashReportsManager, "flush"),
            (OsTraceService, "syslog"),
        ):
            getattr(module, attr)
    except (ImportError, AttributeError) as exc:
        raise ApiError(f"pymobiledevice3 {version}: {type(exc).__name__}: {exc}") from exc
    return argparse.Namespace(
        version=version,
        exceptions=exceptions,
        usbmux=usbmux,
        create_using_usbmux=create_using_usbmux,
        get_remote_pairing_record_filename=get_remote_pairing_record_filename,
        iter_remote_pair_records_by_identifier=iter_remote_pair_records_by_identifier,
        tunnel_service=tunnel_service,
        userspace_tunnel=userspace_tunnel,
        AppServiceService=AppServiceService,
        IconService=IconService,
        RemoteServiceDiscoveryService=RemoteServiceDiscoveryService,
        UserspaceDialPlane=UserspaceDialPlane,
        UserspaceRsdTunnel=UserspaceRsdTunnel,
        AfcService=AfcService,
        AmfiService=AmfiService,
        CrashReportsManager=CrashReportsManager,
        DiagnosticsService=DiagnosticsService,
        DvtProvider=DvtProvider,
        LocationSimulation=LocationSimulation,
        Screenshot=Screenshot,
        InstallationProxyService=InstallationProxyService,
        MisagentService=MisagentService,
        MobileImageMounterService=MobileImageMounterService,
        auto_mount=auto_mount,
        image_type_for_device=image_type_for_device,
        OsTraceService=OsTraceService,
    )


# ---------------------------------------------------------------- discovery (cheap, no tunnel)

async def usb_udids(pmd3) -> list[str]:
    """UDIDs usbmuxd sees on a cable; none when usbmuxd isn't running (no cable → no socket)."""
    try:
        devices = await pmd3.usbmux.list_devices()
    except pmd3.exceptions.ConnectionFailedToUsbmuxdError:
        return []
    except (OSError, ConnectionError):
        return []
    return sorted({d.serial for d in devices if d.connection_type == "USB"})


def wifi_records(pmd3) -> dict[str, str]:
    """udid -> path of each RemotePairing record."""
    out = {}
    try:
        for identifier, path, _record in pmd3.iter_remote_pair_records_by_identifier():
            out[identifier] = str(path)
    except Exception:  # noqa: BLE001 - an unreadable record must not break discovery
        pass
    return out


def lockdown_records() -> set[str]:
    try:
        return {p.stem for p in LOCKDOWN_DIR.glob("*.plist") if p.stem != "SystemConfiguration"}
    except OSError:
        return set()


def scope_interface(host: str) -> str:
    return host.split("%", 1)[1] if "%" in host else ""


class _SkippedAddresses(logging.Handler):
    """Addresses pymobiledevice3 skipped (debug log only): tells 'advertised but not answering'
    from 'not advertised at all'."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.skipped: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if msg.startswith("Skipping remote pairing service"):
            self.skipped.append(msg.removeprefix("Skipping remote pairing service "))


class _WifiTunnelHandle:
    """Stands in for pymobiledevice3's UserspaceRsdTunnel in userspace_tunnel._active_tunnel.

    pymobiledevice3 only publishes a tunnel there when UserspaceRsdTunnel opens it (our USB path).
    The Wi-Fi path builds the same stack by hand, so without this USERSPACE_ACTIVE stays False and
    device-initiated media (DisplayService video via screen_stream.open_media_receiver ->
    UserspaceUdp) would bind a host kernel socket the phone can't reach. userspace_stack_addr()
    reads `.tun.addr`; establish_userspace_rsd() reads `.rsd` and `.serial`.
    """

    def __init__(self, tun, rsd, serial: str):
        self.tun = tun
        self.rsd = rsd
        self.serial = serial


@contextlib.contextmanager
def _published(pmd3, handle):
    ut = pmd3.userspace_tunnel
    if not hasattr(ut, "_active_tunnel") or ut._active_tunnel is not None:
        # Older/newer pymobiledevice3 without the attribute, or (never with the daemon's single
        # tunnel) one is published already: leave it alone.
        yield
        return
    ut._active_tunnel = handle
    ut.USERSPACE_ACTIVE = True
    try:
        yield
    finally:
        if ut._active_tunnel is handle:
            ut._active_tunnel = None
            ut.USERSPACE_ACTIVE = False


@contextlib.asynccontextmanager
async def wifi_rsd(pmd3, udid: str):
    """A connected RSD over Wi-Fi, and where the phone answered."""
    tunnel_logger = logging.getLogger(pmd3.tunnel_service.__name__)
    skipped = _SkippedAddresses()
    level = tunnel_logger.level
    tunnel_logger.addHandler(skipped)
    tunnel_logger.setLevel(logging.DEBUG)
    try:
        services = await pmd3.tunnel_service.get_remote_pairing_tunnel_services(
            bonjour_timeout=BONJOUR_TIMEOUT_S, udid=udid)
    finally:
        tunnel_logger.removeHandler(skipped)
        tunnel_logger.setLevel(level)
    if not services and skipped.skipped:
        raise UnreachableError("advertises itself but didn't accept the connection "
                               f"(Wi-Fi asleep?): {'; '.join(skipped.skipped)}")
    if not services:
        raise UnreachableError(f"no answer on Wi-Fi within {BONJOUR_TIMEOUT_S} s")
    for extra in services[1:]:
        with contextlib.suppress(Exception):
            await extra.close()
    service = services[0]
    where = {"transport": "wifi", "address": service.hostname,
             "interface": scope_interface(service.hostname)}
    pmd3.tunnel_service.USE_USERSPACE_TUNNEL = True
    try:
        async with service.start_tcp_tunnel() as tunnel:
            tun = tunnel.client.tun
            tun.set_peer(tunnel.address)
            async with pmd3.UserspaceDialPlane(tun, tunnel.address) as dial:
                rsd = pmd3.RemoteServiceDiscoveryService(
                    (tunnel.address, tunnel.port), open_connection=dial.dial,
                    auxiliary_metadata=tunnel.auxiliary_metadata)
                await asyncio.wait_for(rsd.connect(), timeout=RSD_TIMEOUT_S)
                try:
                    with _published(pmd3, _WifiTunnelHandle(tun, rsd, udid)):
                        yield rsd, where
                finally:
                    with contextlib.suppress(Exception):
                        await rsd.close()
    finally:
        with contextlib.suppress(Exception):
            await service.close()


@contextlib.asynccontextmanager
async def usb_rsd(pmd3, udid: str):
    """A connected RSD over USB (CoreDeviceProxy, no root)."""
    if udid not in await usb_udids(pmd3):
        raise UnreachableError("not on USB")
    tunnel = pmd3.UserspaceRsdTunnel(serial=udid, autopair=False, remotepairing_fallback=False)
    async with tunnel as rsd:
        yield rsd, {"transport": "usb", "address": "usb", "interface": ""}


def rsd_for(pmd3, transport: str, udid: str):
    return wifi_rsd(pmd3, udid) if transport == "wifi" else usb_rsd(pmd3, udid)


# ---------------------------------------------------------------- pairing helpers

def record_path(pmd3, udid: str) -> Path:
    for identifier, path, _record in pmd3.iter_remote_pair_records_by_identifier():
        if identifier == udid:
            return Path(path)
    return REMOTE_DIR / f"{pmd3.get_remote_pairing_record_filename(udid)}.plist"


def pin_record(path: Path, host_identifier: str) -> bool:
    """chmod 600 and pin host_identifier, so renaming this computer doesn't break pair-verify."""
    record = plistlib.loads(path.read_bytes())
    added = not record.get("host_identifier")
    if added and host_identifier:
        record["host_identifier"] = host_identifier
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(plistlib.dumps(record))
        tmp.chmod(0o600)
        tmp.replace(path)
    path.chmod(0o600)
    return added


# ---------------------------------------------------------------- provisioning profiles (pure)

def parse_profile(blob: bytes) -> dict:
    start = blob.find(b"<?xml")
    end = blob.find(b"</plist>", start)
    if start < 0 or end < 0:
        raise ValueError("not a provisioning profile")
    return plistlib.loads(blob[start:end + len(b"</plist>")])


def _epoch(value) -> int:
    if not isinstance(value, datetime):
        return 0
    return int((value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp())


def profile_summary(profile: dict) -> dict:
    ent = profile.get("Entitlements") or {}
    certs = profile.get("DeveloperCertificates") or []
    expires = _epoch(profile.get("ExpirationDate"))
    return {
        "name": profile.get("Name", ""),
        "uuid": profile.get("UUID", ""),
        "appId": ent.get("application-identifier", ""),
        "team": (profile.get("TeamIdentifier") or [""])[0],
        "teamName": profile.get("TeamName", ""),
        "created": _epoch(profile.get("CreationDate")),
        "expires": expires,
        "expired": bool(expires) and expires < datetime.now(UTC).timestamp(),
        "devices": len(profile.get("ProvisionedDevices") or []),
        "isXcodeManaged": bool(profile.get("IsXcodeManaged", False)),
        "getTaskAllow": bool(ent.get("get-task-allow", False)),
        "certSha1": [hashlib.sha1(c, usedforsecurity=False).hexdigest() for c in certs],
    }


def launch_failure(user_info) -> tuple[str, str]:
    """(innermost BSErrorCodeDescription, failure reason) from a CoreDevice error's userInfo."""
    code, reason = "", ""
    info = user_info
    while isinstance(info, dict):
        code = info.get("BSErrorCodeDescription", code)
        reason = info.get("NSLocalizedFailureReason", reason)
        underlying = info.get("NSUnderlyingError")
        info = underlying.get("userInfo") if isinstance(underlying, dict) else None
    return code, reason


# ---------------------------------------------------------------- app install (pure helpers)

def package_bundle_id(package: Path) -> str:
    """CFBundleIdentifier of a signed .ipa (Payload/X.app/Info.plist) or an .app folder."""
    import zipfile
    if package.is_dir():
        return plistlib.loads((package / "Info.plist").read_bytes()).get("CFBundleIdentifier", "")
    with zipfile.ZipFile(package) as ipa:
        names = [n for n in ipa.namelist()
                 if n.startswith("Payload/") and n.count("/") == 2 and n.endswith(".app/Info.plist")]
        if len(names) != 1:
            raise ValueError(f"{len(names)} Payload/*.app/Info.plist in the .ipa, expected 1")
        return plistlib.loads(ipa.read(names[0])).get("CFBundleIdentifier", "")


# (pattern in the installer's error text, code, plain message)
_INSTALL_ERRORS = [
    (r"maximum number of apps|free development profiles|0xe8008021", "app-limit",
     "Free Apple IDs can only have 3 of your own apps on an iPhone at a time. Remove one of them "
     "from the iPhone page (Apps), then try again."),
    (r"not enough (free )?space|insufficient ?space|InsufficientStorage|0xe800800[cd]", "no-space",
     "There isn't enough free storage on your iPhone. Free up some space and try again."),
    (r"DeviceLocked|device is locked|PasswordProtected|passcode", "locked",
     "Your iPhone is locked. Unlock it and try again."),
    (r"valid provisioning profile|ProvisioningProfile|not (been )?included in|0xe8008015|0xe8008016|"
     r"ApplicationVerificationFailed|device is not registered|MismatchedApplicationIdentifierEntitlement",
     "device-not-registered",
     "This iPhone isn't registered with your Apple ID yet. Run once over USB."),
    (r"0xe800801c|IncorrectArchitecture|DeviceOSVersionTooLow|MinimumOSVersion", "incompatible",
     "This app can't run on this iPhone (its iOS version or architecture doesn't match)."),
    (r"0xe8000067|integrity|signature|codesign|CodeSignature", "bad-signature",
     "The app's signature isn't valid. Build and sign it again."),
]


def install_error(exc: BaseException, classify):
    """Plain-language HelperError for a failed installation (classify = deviced.classify)."""
    import re
    from common import HelperError, fix
    text = f"{type(exc).__name__}: {exc}"
    for pattern, code, message in _INSTALL_ERRORS:
        if re.search(pattern, text, re.I):
            fx = None
            if code == "app-limit":
                fx = fix("Open iPhone apps", "device")
            elif code == "device-not-registered":
                fx = fix("Open Projects", "projects")
            return HelperError(code, message, fx, text)
    e = classify(exc)
    if e.code == "failed":
        return HelperError("install-failed", "The app couldn't be installed on your iPhone.", None, text)
    return e
