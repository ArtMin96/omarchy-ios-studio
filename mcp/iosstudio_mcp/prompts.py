"""Prompts: slash commands in clients that support them (Claude Code, Gemini CLI, Zed, Cursor)."""
from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field


def register(mcp: MCPServer) -> None:

    @mcp.prompt(title="Fix build errors")
    def fix_build_errors(
        project: Annotated[str, Field(description="Project name or path (see ios_project_list)")],
    ) -> str:
        """Build an iOS project, fix every compiler error, and rebuild until it compiles cleanly."""
        return f"""Make the iOS project "{project}" compile cleanly.

1. ios_project_info(project_path="{project}") to find the project folder and its Sources/ directory.
2. ios_build_start(project_path="{project}", action="compile"), then poll ios_build_status(build_id, wait_seconds=20) until it finishes.
3. If it failed: ios_build_diagnostics(build_id), errors first. Each entry has a category. For compiler errors, read each file around its file:line, fix the root cause (not just the symptom), and keep fixes minimal and idiomatic SwiftUI/Swift. Linker, package, signing, device, toolchain and config entries come with a one-line hint (often naming the tool that fixes it) and details such as the undefined symbols: follow the hint.
   Only if nothing was recognised, read ios_build_log(build_id, level="error").
4. Rebuild and repeat until state is "succeeded" with 0 errors. Then fix warnings you introduced.
5. Summarize what was wrong and what you changed. Offer ios_run to try it on the iPhone."""

    @mcp.prompt(title="Diagnose a crash")
    def diagnose_crash(
        bundle_id: Annotated[str, Field(description="The app's bundle id, e.g. com.example.MyApp")],
    ) -> str:
        """Find out why an app crashes on the iPhone and fix it."""
        return f"""The app {bundle_id} crashes on the iPhone. Find out why and fix it.

1. ios_status to confirm the iPhone is connected.
2. ios_crash_list(bundle_id="{bundle_id}") for this app's newest crash reports, then ios_crash_get(name) for the exception type, termination reason and the crashed thread's top frames (section='threads' for every thread, section='raw' for the report text, paged with start_line).
3. Reproduce it while capturing logs: ios_app_run_and_capture(bundle_id="{bundle_id}", seconds=15) launches the app fresh, records its log and reports any new crash. For a crash that needs user interaction, ios_logs_start(bundle_id="{bundle_id}"), ask the user to trigger it, then ios_logs_read(session_id, wait_seconds=20) and ios_logs_stop.
   If the iPhone isn't reachable, ask the user to open iOS Studio's crash view: ios_open_app(page="crashes").
4. Map the top frames in the app's own module to source files in the project (ios_project_list finds the folder by bundle id).
5. Explain the cause, fix it, rebuild with ios_run, and verify with ios_app_run_and_capture that the crash is gone."""

    @mcp.prompt(title="New iPhone app")
    def new_app(
        name: Annotated[str, Field(description="App name: letters, digits and _, e.g. WeatherGlance")],
        template: Annotated[str, Field(description="Template id from ios_project_templates")] = "swiftui-app",
    ) -> str:
        """Create a new iPhone app from a template, build it and run it on the iPhone."""
        return f"""Create a new iPhone app called "{name}".

1. ios_status: if the toolchain isn't ready or no Apple ID is signed in, walk the user through the fixes first.
2. ios_project_templates to confirm "{template}" exists (pick the closest one if not), then ios_project_create(name="{name}", template="{template}").
3. Ask the user what the app should do, then implement it in the project's Sources/{name}/ folder (SwiftUI).
4. ios_build_start(action="compile") and fix errors (ios_build_diagnostics) until it builds.
5. ios_run to install and launch it on the iPhone. Free Apple IDs allow 3 apps per iPhone and re-signing every 7 days; mention that."""

    @mcp.prompt(title="Get ready for iPhone development")
    def get_ready() -> str:
        """Check everything needed to build and run iPhone apps, and walk through fixing what's missing."""
        return """Get this computer ready to build and run iPhone apps with iOS Studio.

1. ios_status. For each entry in `problems`, follow its `fix`:
   - toolchain: ios_toolchain_check, then ios_toolchain_install(all=true). It asks for confirmation (state needs_input): show the user the prompt and answer with ios_build_respond. A polkit password dialog will appear on their screen; tell them.
   - Human-only steps (Apple ID sign-in, the Xcode .xip download, iPhone pairing/Trust, Developer Mode): explain what to do and call ios_open_app with the right page. Never ask for passwords or 2FA codes.
2. Re-run ios_status until ready is true.
3. Finish with a short summary of the setup and suggest the new_app prompt or ios_project_list."""
