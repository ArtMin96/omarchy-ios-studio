"""iOS Studio MCP server (stdio). Run with `bin/iosstudio-mcp` or `python -m iosstudio_mcp`.

stdout carries the MCP protocol only; all logging goes to stderr (IOSSTUDIO_MCP_LOG=DEBUG for more).
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from . import __version__, prompts, resources
from .state import AppState
from .tools import MODULES, slim_schemas

log = logging.getLogger("iosstudio")

INSTRUCTIONS = """\
iOS Studio builds, signs, installs and debugs iPhone apps on this Linux computer (Swift + xtool, no Mac).

- Start with ios_status: it reports toolchain, Apple ID, iPhone and projects, and names the tool that fixes each problem.
- Projects are SwiftPM packages with an xtool.yml; sources are in Sources/<App>/. Find them with ios_project_list and pass the absolute path (or the project name) as project_path.
- Builds are background jobs: ios_build_start (or ios_run to build+install+launch) returns a build_id; poll ios_build_status(build_id, wait_seconds=20) until succeeded/failed, then ios_build_diagnostics: each error has a category (compiler file:line, linker, package, signing, device, toolchain, config) and the non-compiler ones a one-line fix hint. ios_build_log has the raw output, rarely needed. action='compile' needs neither an Apple ID nor an iPhone.
- ios_project_check finds xtool.yml typos, missing NS*UsageDescription keys, bad icons and entitlements a free account can't use before a build (under a second, warnings only, never changes files).
- A project that builds through its own scripts (a GENERATED xtool.yml, env vars, ulimit -n) needs ios_project_build_settings first (dry run, then confirm=true after the user agrees); ios_project_info shows suggested settings.
- Run presets (saved launch args, env, language/region, UI-test flag per project): ios_project_presets lists/saves/deletes/selects them; pass preset='<name>' to ios_run, ios_app_launch or ios_app_run_and_capture.
- If a job's state is needs_input, show its pending_prompt to the user and send THEIR answer with ios_build_respond.
- On the iPhone: ios_app_run_and_capture(bundle_id) launches an app and reports its logs, what it printed (print()) and any new crash in one call; ios_app_console_read pages through its print() output later (apps launched by ios_run, ios_app_run_and_capture or ios_app_launch(console=true)); ios_logs_start/read/stop follow the log with a cursor; ios_crash_list/get read crash reports; ios_screenshot shows the screen and ios_screen_record records a short MP4 of it (for bug reports); ios_app_list/ios_device_info describe the phone. Developer Mode is turned on by the human (ios_device_prepare explains how).
- Screen as data: ios_ui_tree(bundle_id) lists the screen's accessibility elements (id, role, label, value, frame) in reading order, paged with cursor; ios_ui_press(element_id or label, bundle_id) taps one on the real iPhone and reports what changed; ios_a11y_audit(bundle_id) runs Apple's accessibility audit. Typical loop: ios_run -> ios_ui_tree -> ios_ui_press -> ios_ui_tree/ios_screenshot. No typing or swipes.
- Network testing: ios_network_conditions lists slow-network, thermal and GPU profiles; ios_network_condition_set (dry run first, then confirm=true) applies one to the whole phone for a few minutes, ios_network_condition_clear ends it. ios_pcap_capture records traffic over USB and returns a header-only summary and a pcapng path.
- Performance: ios_metrics_start(bundle_id) samples CPU, memory, energy, FPS, network and battery about once a second; ios_metrics_summary gives stats and anomalies (possible leak, spikes, hangs, jetsam); ios_metrics_stop ends it. ios_launch_profile and ios_memory_leaks change the app's state: confirm with the user first.
- Files: ios_media_list/ios_media_pull read the camera roll; ios_files_list/pull/push/delete work in an app's container; ios_camera_test runs the app's camera code on test files (needs the hook in docs/camera-testing.md). Long copies, tests and installs (ios_app_install) return state='running' with an id: call the same tool again with it.
- Debugger (your own apps only; attaching pauses the app): ios_debug_backtrace(bundle_id, confirm=true) for every thread's stack; ios_debug_session_start with breakpoints, then ios_debug_session_wait / ios_debug_session_command, and always ios_debug_session_end.
- udid parameters are optional and default to the active iPhone.
- Results are kept small (about 12 KB). A cut result has truncated=true and a `note` saying how to get more (before=/offset=, grep=, a filter, or max_bytes up to 48000); big outputs are saved to a file whose path comes back as full_output_path.
- Free Apple IDs: apps stop opening 7 days after signing (ios_project_renew re-signs), at most 3 of your apps per iPhone, 10 new app ids per week; bundle ids are registered as XTL-<TEAM>.<bundle id>.
- Human-only steps: Apple ID sign-in (never ask for passwords or 2FA codes), the Xcode .xip download, iPhone pairing/Trust, Developer Mode, and polkit password dialogs. Explain the step and use ios_open_app(page=...).
- Destructive tools (ios_project_remove, ios_build_clean, ios_toolchain_clean, ios_account_logout, ios_app_uninstall, ios_files_delete, ios_project_autorenew enable/disable) take confirm=false by default and return a dry run; so does a destructive answer in ios_build_respond. Ask the user, then repeat with confirm=true.
"""


def _load_modules() -> list:
    mods = []
    for name in MODULES:
        full = f"{__package__}.tools.{name}"
        try:
            mods.append(importlib.import_module(full))
        except ModuleNotFoundError as exc:
            if exc.name == full:   # optional module missing from this install
                log.debug("tool module %s not present", name)
                continue
            raise
    return mods


TOOL_MODULES = _load_modules()


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[AppState]:
    state = AppState()
    started = []
    try:
        for mod in TOOL_MODULES:
            hook = getattr(mod, "on_startup", None)
            if hook:
                try:
                    await hook(state)
                    started.append(mod)
                except Exception:  # noqa: BLE001  one module's startup must not take the server down
                    log.exception("startup hook of %s failed", mod.__name__)
            else:
                started.append(mod)
        yield state
    finally:
        await state.jobs.shutdown()
        for mod in reversed(started):
            hook = getattr(mod, "on_shutdown", None)
            if hook:
                try:
                    await hook(state)
                except Exception:  # noqa: BLE001
                    log.exception("shutdown hook of %s failed", mod.__name__)


def create_server() -> MCPServer:
    mcp = MCPServer("iosstudio", title="iOS Studio", version=__version__, instructions=INSTRUCTIONS,
                    website_url="https://github.com/ArtMin96/omarchy-ios-studio", lifespan=lifespan,
                    log_level=os.environ.get("IOSSTUDIO_MCP_LOG", "INFO").upper())
    for mod in TOOL_MODULES:
        mod.register(mcp)
    prompts.register(mcp)
    resources.register(mcp)
    slim_schemas(mcp)
    return mcp


mcp = create_server()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("IOSSTUDIO_MCP_LOG", "INFO").upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("iOS Studio MCP %s starting (demo=%s)", __version__, bool(os.environ.get("IOSSTUDIO_DEMO")))
    mcp.run("stdio")


if __name__ == "__main__":
    main()
