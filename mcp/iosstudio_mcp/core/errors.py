"""Errors: one line that says what failed, its [code], and the next step (a tool to call or a human step).

    device_error(exc, hints)  a DeviceError from the device daemon -> ToolError "<message> [code] Next: <step>"
    rehint(exc, hints)        an error already in that form -> the same line with a feature's own next step
                              for its [code] (for errors kept on background jobs)
    next_step(code, fix)      the next step for a device error code (or the helper's fix page)

`hints` maps error codes to a feature's own next step; it takes precedence over DEVICE_NEXT (e.g.
{"not-found": "Call ios_crash_list for valid names."}). The tool() wrapper clips every error to one line
(budget.clip_error), so a tool raises a ToolError with the whole text and never trims it itself.
"""
from __future__ import annotations

import re

from mcp.server.mcpserver.exceptions import ToolError

from .. import helper

_PAIR = ("Pairing is a human step: ask the user to connect the iPhone by cable, unlock it and tap Trust "
         "(or pair over Wi-Fi in iOS Studio): ios_open_app(page='device'). Then ios_device_list.")

DEVICE_NEXT: dict[str, str] = {
    "no-device": _PAIR, "not-paired": _PAIR, "not-trusted": _PAIR, "device-not-found": _PAIR,
    "unreachable": "Ask the user to wake and unlock the iPhone and keep it on the same Wi-Fi (or plug it in), "
                   "then ios_device_reconnect and retry.",
    "locked": "Ask the user to unlock the iPhone, then retry.",
    "developer-mode-off": "Call ios_device_prepare: it tells the user how to turn on Developer Mode.",
    "ddi": "Call ios_device_prepare(mount=true); mounting the developer disk image needs internet.",
    "busy": "Another operation (e.g. pairing in the iOS Studio window) is running. Wait a moment and retry.",
    "pmd3-api": "The iPhone tools need an update: ios_toolchain_check, then ios_toolchain_install.",
    "needs-setup": "The iPhone tools aren't installed: ios_toolchain_check, then ios_toolchain_install.",
    "app-limit": "Free Apple IDs allow 3 of your own apps per iPhone. Show ios_app_list(scope='own') to the "
                 "user and, only with their OK, remove one with ios_app_uninstall.",
    "device-not-registered": "Install once over USB so the iPhone is registered: plug it in and run "
                             "ios_run(project_path, via='usb').",
    "no-space": "The iPhone is out of storage. Ask the user to free some space.",
    "incompatible": "The app's minimum iOS or architecture doesn't match this iPhone (ios_device_info shows its "
                    "iOS version).",
    "bad-signature": "Re-sign the app: ios_run(project_path) or ios_project_renew.",
    "bad-package": "The file isn't a valid signed .ipa or .app. Build one with ios_build_start(action='ipa').",
    "timeout": "The iPhone was slow to answer. Make sure it's awake and unlocked, then retry "
               "(ios_device_list shows its connection).",
    "disconnected": "The device service restarted. Retry the call.",
    "daemon-unavailable": "The iPhone service couldn't start. Run ios_toolchain_check (pymobiledevice3 step), "
                          "or ask the user to open iOS Studio: ios_open_app(page='device').",
    # App containers (container.* in docs/deviced-protocol.md)
    "no-file-sharing": "Only apps that share files can be browsed. Pick one of your own apps: "
                       "ios_app_list(scope='own').",
    "denied": "The iPhone refused that folder. List the app's top level with ios_files_list(bundle_id) and pick "
              "a folder it shows.",
    "socket-path-too-long": "Set IOSSTUDIO_RUNTIME_DIR to a shorter folder (socket paths max out at 107 bytes), "
                            "then restart the MCP server.",
    # UI inspection (ui.* in docs/deviced-protocol.md)
    "no-element": "No element has that label or id. Use one of the close labels above, or call ios_ui_tree "
                  "(filter=...) to see what's on screen now.",
    "ambiguous-label": "Several elements match. Press one by element_id (listed above), or pass the full "
                       "label with match='exact'.",
    "stale-element": "The screen changed since that id was read. Call ios_ui_tree again and use a fresh id.",
    "press-failed": "The iPhone didn't activate that element (some controls ignore accessibility presses). "
                    "Try its parent cell or a nearby button from ios_ui_tree, and check with ios_screenshot.",
    "bad-args": "Check the arguments named in the message (ios_ui_press takes exactly one of element_id or label).",
    "ax-unavailable": "The iPhone's accessibility inspection service isn't available. Make sure the iPhone is "
                      "unlocked with Developer Mode on (ios_device_prepare), then ios_device_reconnect and retry.",
    "wrong-app": "That app isn't the one in front (or isn't installed or running). Launch it with "
                 "ios_app_launch(bundle_id) first (ios_app_list shows what's installed), then retry.",
    "no-app-in-front": "No app was detected in front. Open the app with ios_app_launch(bundle_id) first, or pass "
                       "bundle_id, then retry.",
}

# Codes whose detail (the daemon's own words) is worth a few hundred characters.
DETAIL_CODES = ("failed", "install-failed", "ddi", "bad-package", "daemon-unavailable")
UI_DETAIL_CODES = ("no-element", "ambiguous-label", "wrong-app", "press-failed", "ax-unavailable")

_CODE = re.compile(r"\[([a-z0-9-]+)\]")


def next_step(code: str, fix: dict | None = None, hints: dict[str, str] | None = None) -> str | None:
    if hints and code in hints:
        return hints[code]
    if code in DEVICE_NEXT:
        return DEVICE_NEXT[code]
    page = (fix or {}).get("page")
    if page == "device":
        return "Ask the user to check the iPhone in iOS Studio: ios_open_app(page='device')."
    return helper.NEXT_BY_PAGE.get(page) if page else None


def device_error(exc: Exception, hints: dict[str, str] | None = None) -> ToolError:
    code = str(getattr(exc, "code", "failed") or "failed")
    msg = str(getattr(exc, "message", None) or exc).strip()
    nxt = next_step(code, getattr(exc, "fix", None), hints)
    detail = getattr(exc, "detail", None)
    out = f"{msg} [{code}]"
    if detail and code in DETAIL_CODES:
        out += f" Detail: {str(detail).strip()[:300]}"
    elif detail and code in UI_DETAIL_CODES:
        if isinstance(detail, (list, tuple)):
            detail = "; ".join(str(x) for x in detail)
        out += f" Detail: {str(detail).strip()[:250]}"
    if nxt:
        out += f" Next: {nxt}"
    return ToolError(out)


def rehint(exc: BaseException, hints: dict[str, str]) -> ToolError:
    """The error's text with hints[code] as its next step (replacing the generic one), for the first
    [code] in it; unchanged text when the code has no hint."""
    text = str(exc)
    m = _CODE.search(text)
    nxt = hints.get(m.group(1)) if m else None
    if nxt:
        text = text.split(" Next: ", 1)[0] + f" Next: {nxt}"
    return ToolError(text)
