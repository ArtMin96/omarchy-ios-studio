# Editor setup: Swift completion in Zed, Neovim and VS Code

Completion, errors as you type, go to definition and hover docs for Swift come from SourceKit-LSP, the Swift language server (`/usr/bin/sourcekit-lsp`, installed with Swift). On its own it would treat an iPhone app as a Linux package and fail on `import SwiftUI`. An xtool project points it at xtool instead: the file `.bsp/xtool.json` in the project tells SourceKit-LSP to start `xtool dev build-server`, which compiles for the iPhone (`arm64-apple-ios`) with the iOS SDK.

Setup has two parts:

1. **Once per editor, on this computer.** Settings › Code editor (or `ios_editor_setup` for agents). This turns on Swift support in the editor itself and remembers which editor you use.
2. **Per project, by itself.** Each project needs `.bsp/xtool.json`. iOS Studio writes it when you create or convert a project and after every successful build, if it's missing (xtool writes the same file on its first build too). When VS Code is your editor, projects created in iOS Studio from a template also get `.vscode/settings.json` and `.vscode/extensions.json`. Projects you added or converted never get editor files written by themselves; their hint says "VS Code settings for this project: set up from Settings › Code editor", and an explicit fix (`--fix --confirm`, or `ios_editor_setup(project_path=…, confirm=true)`) writes them. You don't press anything per project; the project card only shows a quiet hint such as "Build once to enable completion" when something is missing.

Nothing is overwritten. When a file already exists, only missing keys are added and every value already there stays. A value you set differently is kept and listed. A file that isn't valid JSON is left alone, and setup says so. Files with comments and trailing commas (JSONC, what VS Code and Zed use) are edited as text: new keys go in, everything else stays byte for byte.

## Zed

1. Install Zed's Swift extension: Extensions (ctrl-shift-x), search for Swift, Install. iOS Studio checks for it (`~/.local/share/zed/extensions/installed/swift`) but can't install it for you.
2. Settings › Code editor › Zed › Set up. This only remembers Zed as your editor; Zed needs no other file.
3. Open the project folder itself in Zed (`zeditor ~/Developer/MyApp`), not a folder above it: SourceKit-LSP looks for `.bsp/` in the folder Zed opened.

Zed needs no `.zed/settings.json`. The Swift extension starts the `sourcekit-lsp` it finds on PATH, and SourceKit-LSP finds `.bsp/xtool.json` by itself. Only when `sourcekit-lsp` isn't on PATH (a Swift toolchain unpacked somewhere else) does setup add its path to your Zed user settings, `~/.config/zed/settings.json`, after a backup:

```json
{
  "lsp": {
    "sourcekit-lsp": {
      "binary": { "path": "/opt/swift/usr/bin/sourcekit-lsp" }
    }
  }
}
```

The first time you open a folder, Zed asks whether to trust it ("Unrecognized Project"). Until you click **Trust and Continue**, the window is in Restricted Mode: no language servers, so no completion or errors.

## Neovim

Neovim reads no per-project settings, so setup changes your Neovim config. The dry run says so, and every change can be undone.

- **LazyVim with a `lang.swift` extra.** Setup copies `~/.config/nvim/lazyvim.json` to `lazyvim.json.bak.<time>` and adds `lazyvim.plugins.extras.lang.swift` to its `extras`. Undo it with `:LazyExtras`.
- **LazyVim without that extra** (it isn't in every LazyVim version). Setup writes a new file, `~/.config/nvim/lua/plugins/ios-studio-swift.lua`, and never replaces one that's there. Delete the file to undo:

  ```lua
  return {
    {
      "nvim-treesitter/nvim-treesitter",
      opts = { ensure_installed = { "swift" } },
    },
    {
      "neovim/nvim-lspconfig",
      opts = {
        servers = {
          sourcekit = {
            mason = false, -- comes with Swift, not with Mason
          },
        },
      },
    },
  }
  ```

- **Any other Neovim config** is left alone. With nvim-lspconfig on Neovim 0.11 or later, add this to your `init.lua`:

  ```lua
  vim.lsp.enable("sourcekit")
  ```

  On older nvim-lspconfig setups, use `require("lspconfig").sourcekit.setup({})` instead. nvim-lspconfig's `sourcekit` config already looks for `.bsp` to find the project root.

Setup counts Swift as done when your config already enables it: the `lang.swift` extra in `lazyvim.json`, or `sourcekit` in a Lua file under `~/.config/nvim` (comments don't count). Restart Neovim after setup; LazyVim installs the Swift parser on the next start.

## VS Code

1. Settings › Code editor › VS Code › Set up. This remembers VS Code as your editor.
2. From then on, each project you create from a template gets two files, when it's created and when it's built. Added and converted projects get them only through an explicit fix (see Command line). You can add them to git:

   `.vscode/extensions.json`, so VS Code offers the Swift extension (`swiftlang.swift-vscode`, formerly `sswg.swift-lang`) when the folder opens:

   ```json
   {
     "recommendations": [
       "swiftlang.swift-vscode"
     ]
   }
   ```

   `.vscode/settings.json`:

   ```json
   {
     "swift.autoGenerateLaunchConfigurations": false,
     "files.watcherExclude": {
       "**/.build/**": true
     }
   }
   ```

   The first setting keeps the extension from writing `launch.json` entries for a Linux executable that doesn't exist; debugging on the iPhone is a separate setup. The second keeps VS Code from watching `.build`, which changes thousands of files on every build. No path is written into the project files, so they work for teammates on other computers.
3. Install the Swift extension when VS Code offers it.

When `sourcekit-lsp` isn't on PATH, setup adds `swift.sourcekit-lsp.serverPath` to your VS Code user settings (`~/.config/Code/User/settings.json`; Code - OSS and VSCodium have their own folders), after a backup.

The template's `.gitignore` ignores `/.bsp`: xtool and iOS Studio write it on every machine. `.vscode/` and `.zed/` aren't ignored, so shared editor settings can be committed.

## Status

Each editor in Settings › Code editor shows one of these:

| State | Meaning |
|---|---|
| ready | Swift support is on. Projects get completion once they have `.bsp/xtool.json`. |
| needs-setup | Set up has changes to make (shown before you confirm). |
| partial | A step only you can do remains: install the editor's Swift extension, a config file setup couldn't read, a Neovim config that isn't LazyVim. The note says which. |
| not-installed | The editor isn't installed. |

A project is ready when `.bsp/xtool.json` exists and still points at xtool's build server (and, with VS Code, the two `.vscode` files have the keys above). `.bsp/xtool.json` is written by itself for every project; `created` in the status says whether iOS Studio made the project (stored as `createdFrom` in its entry in `~/.local/state/ios-studio/projects.json`). Otherwise the project card shows one of these hints:

- "Build once to enable completion": the file is missing. Build the project, or let an agent call `ios_editor_setup(project_path=…)`.
- The file is there but stale: it starts something other than `xtool dev build-server`, or `xtool` isn't on PATH. iOS Studio doesn't replace a file it didn't write; delete it and build again.
- "xtool's iOS SDK isn't set up: finish it on the Toolchain page": `xtool dev build-server` can't run without the Darwin Swift SDK (`~/.config/swiftpm/swift-sdks/darwin.artifactbundle`), and editors only report "Failed to initialize build server: JSON-RPC connection closed".
- "xtool.yml has skipLSP: true": you turned off xtool's editor support in `xtool.yml`. Remove that line.

With Swift older than 6.4, xtool's build server can't run, and xtool uses `.sourcekit-lsp/config.json` (the iOS Swift SDK for SwiftPM) instead. iOS Studio follows the same rule.

## Command line

```bash
iosstudio-helper editor status                                  # every editor, machine-wide
iosstudio-helper editor setup --editor zed|neovim|vscode        # dry run: the actions it would take
iosstudio-helper editor setup --editor neovim --confirm         # do it
iosstudio-helper editor status --project ~/Developer/MyApp      # one project: bsp, ready, hint
iosstudio-helper editor status --project ~/Developer/MyApp --fix [--editor vscode] [--confirm]
```

Results have `dryRun`, `applied` (anything written) and `next` (a short sentence about what to do next, or null on a dry run). Each action has `done` once it's applied, a short `detail`, and a `kind`: `write` (a new file), `merge` (keys added to an existing file), `skip` (nothing to do, or a file it can't change safely) or `backup` (a copy of one of your config files before it changes), and a `scope`: `project` or `user` (a file in your home folder). `--prefer no` sets up an editor without remembering it. The Neovim entry of `editor status` also has `lazyvim` (true when your config is LazyVim; otherwise setup leaves it alone and the snippet above is the way). The preferred editor is stored as `editorSetup` in `~/.config/ios-studio/settings.json`; without it, iOS Studio goes by the Open in editor command (`editorCommand`). What the automatic part wrote last is kept per project in `~/.local/state/ios-studio/editor-auto.json` and shown as `auto` in the project status.

## For agents

`ios_editor_setup(editor="auto"|"zed"|"neovim"|"vscode", project_path=None, confirm=false)` is a dry run until `confirm=true`. `auto` sets up every installed editor and remembers the one already remembered (else the first). With `project_path` it also writes that project's missing files now. `ios_project_info` has an `editor` field: `{bsp: {present, stale, reason}, ready, hint, preferred, created, auto}`. See [../mcp.md](../mcp.md).

## Demo mode

With `IOSSTUDIO_DEMO=1`, Zed is ready, Neovim (LazyVim) needs setup and VS Code isn't installed; HabitTracker has never been built, so it has no `.bsp` yet. Setup and `--fix` list their actions and change nothing.
