# Feature pages

One page per feature: what it does, how it works, its limits, the agent (MCP) tools and where the code and
tests are. For the overall picture see [ARCHITECTURE.md](../../ARCHITECTURE.md); for the user-facing walkthrough
see [guide.md](../guide.md).

| Page | Feature |
|---|---|
| [agent-instructions.md](agent-instructions.md) | `AGENTS.md` and `CLAUDE.md` in new and converted projects, the `verify_change` prompt |
| [app-console.md](app-console.md) | What your app prints (`print()`, stdout and stderr) in Logs › Console and `ios_app_console_read` |
| [app-data.md](app-data.md) | An app's UserDefaults and SwiftData / Core Data stores, editing a default, wipe and clean reinstall |
| [crash-symbolication.md](crash-symbolication.md) | File and line numbers for your app's frames in crash reports |
| [editor-setup.md](editor-setup.md) | Swift completion in Zed, Neovim and VS Code |
| [format-lint.md](format-lint.md) | Format with swift-format, style findings from swift-format and SwiftLint |
| [packages.md](packages.md) | Swift package dependencies: list, add by URL, update, resolve, remove |
| [project-build-settings.md](project-build-settings.md) | Projects that build through their own scripts: prebuild command, environment, open-file limit |
| [project-check.md](project-check.md) | Config mistakes found before building (`xtool.yml` keys, usage descriptions, icon, entitlements) |
| [run-presets.md](run-presets.md) | Saved launch arguments, environment, language and region per project |
| [screenshot-compare.md](screenshot-compare.md) | Screenshot baselines, visual diffs and screenshot matrices |
| [ui-inspect.md](ui-inspect.md) | The screen as data, pressing elements, Apple's accessibility audit |
| [watch-mode.md](watch-mode.md) | Compile on save |

Related pages outside this folder: [camera-testing.md](../camera-testing.md) (feeding camera code test images),
[mcp.md](../mcp.md) (every agent tool), [deviced-protocol.md](../deviced-protocol.md) (device daemon commands)
and [setup-steps.md](../setup-steps.md) (what Setup installs).

A new feature gets a page here and a row in this table, in the same pull request.
