# Agent instructions: AGENTS.md and CLAUDE.md in new projects

Coding agents assume a Mac. Asked to check a change, they reach for the Simulator, `xcodebuild` or
on-device XCTest, and they expect UIKit and SwiftUI to build on the computer they run on. None of that
exists here. iOS Studio puts a short instructions file in every new project, so an agent working in it
learns how to build and test through iOS Studio before it wastes turns on tools that aren't there.

## What's added

- `AGENTS.md`: the instructions, read by Codex, opencode, Cursor, Gemini CLI, Crush, Zed and others. The
  project name and bundle ID are filled in.
- `CLAUDE.md`: imports `AGENTS.md` (`@AGENTS.md`), so Claude Code reads the same text.

The text says:

- There's no Mac, no Simulator, no `xcodebuild` and no on-device XCTest. UIKit and SwiftUI don't build
  for the Linux host, so `swift build` on the computer isn't a check.
- How to check a change with iOS Studio's tools: `ios_project_check` → `ios_build_start(action='compile')`
  → `ios_build_diagnostics` → `ios_run` → `ios_app_run_and_capture` or `ios_app_console_read` →
  `ios_screenshot`, `ios_ui_tree`, `ios_a11y_audit`.
- How long jobs answer (`state="running"`, `job_id`, `needs_input`) and which steps only you can do
  (sign-in, Trust, Developer Mode).
- What a free Apple ID allows: 3 apps per iPhone, re-signing every 7 days, 10 new app IDs a week.

## When

- **New app.** Every template (SwiftUI app, tabs app, list and detail) gets both files.
- **Make it an app.** Converting a plain Swift package adds them too, when they're missing. The confirm
  dialog says so.

A file that's already there is never overwritten or changed, even if it says something else. You can
edit or delete both files like any other file in the project.

## Turning it off

Settings › AI agents › Add agent instructions to new apps. It's on by default and applies to every new and
converted project on this computer (`agentInstructions` in `settings.json`; a missing key counts as on).

The helper takes `--agent-files on|off` on `projects new` and `projects convert` to override the
setting for one call. Both replies list the files they wrote in `agentFiles`, for example
`["AGENTS.md", "CLAUDE.md"]`, or `[]` when nothing was written.

## MCP

- `ios_project_create` and `ios_project_convert` take `agent_files` (`true`, `false`, or left out to follow
  the setting).
- The prompt `verify_change(project, what_changed)` walks an agent through checking one change: project
  check, compile, diagnostics, run on the iPhone, capture the console, screenshot and UI tree, then the
  accessibility audit. In Claude Code it's `/mcp__iosstudio__verify_change`.

See [mcp.md](../mcp.md).

## Editing the text

The shared text lives in `templates/agent-instructions/` (`AGENTS.md` and `CLAUDE.md`); `{{NAME}}` and
`{{BUNDLE_ID}}` are filled in per project. Change it there and every project created afterwards gets
the new version. Existing projects keep theirs. Keep the tool names in it in step with the MCP server's
tools when one is renamed.

## Code

`helper/iosstudio/features/agent_instructions.py` (writes the files, never overwrites),
`helper/iosstudio/features/projects/` (`projects new`, `projects convert`), `templates/agent-instructions/`,
`mcp/iosstudio_mcp/tools/` (the `agent_files` parameter), `mcp/iosstudio_mcp/prompts.py` (`verify_change`),
`components/settings/AgentsSection.qml` (the setting), `pages/ProjectsPage.qml` (the convert dialog).
