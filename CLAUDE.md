<!--
Maintainer note (stripped before Claude sees this file).
Organization: this file holds only what applies to every task: what the project is, exact commands, where
code goes, the rules that aren't visible in the code, how to use subagents and when work is done. Topic rules
live in .claude/rules/*.md and load only when Claude touches matching files (their `paths:` frontmatter):
qml.md, python-helper.md, device.md, mcp.md, tests.md. Details belong in ARCHITECTURE.md, CONTRIBUTING.md and
docs/; link to them, don't copy them. Keep this file under 150 lines and each rule file under 80.
Personal notes go in CLAUDE.local.md (gitignored). Change this file and the rules in the same PR as the
convention they describe. Run `/doctor prompt-audit` now and then to find stale or conflicting instructions.
-->
# iOS Studio

A Linux desktop app for Omarchy that builds, signs, installs and debugs iPhone apps without a Mac. Four
processes: a Quickshell/QML UI, a stdlib-only Python helper CLI (one JSON object per call, JSON lines for
jobs), one shared device daemon per user (pymobiledevice3; the only tunnel to the phone) and an MCP server for
AI agents. Read [ARCHITECTURE.md](ARCHITECTURE.md) (the map) and [CONTRIBUTING.md](CONTRIBUTING.md) (setup,
recipes, PR rules) before larger changes; [docs/development.md](docs/development.md) is the full reference.

## Commands

Run from the repo root (your worktree).

```bash
uv sync --group dev                                    # .venv with pytest, ruff, mcp, pydantic, pillow
ln -sfn "$OMARCHY_PATH/shell/Commons" Commons && ln -sfn "$OMARCHY_PATH/shell/Ui" Ui   # UI kit, in a worktree

.venv/bin/pytest                                       # everything CI runs (skips `network`)
.venv/bin/pytest -m "not slow"                         # quick: no helper subprocesses or toolchain
.venv/bin/pytest tests/helper                          # helper + daemon modules in process
.venv/bin/pytest tests/cli                             # the helper as a subprocess with fake tools
.venv/bin/pytest tests/mcp                             # MCP tools
.venv/bin/pytest tests/qml                             # Resource, lib/Model.js (needs qmltestrunner)
.venv/bin/ruff check                                   # lint (CI pins ruff 0.17.4)
/usr/lib/qt6/bin/qmllint path/to/File.qml              # fails only on syntax errors; read your file's warnings
```

Run things in demo mode with private folders, so nothing reaches the owner's settings, state or daemon:

```bash
t=$(mktemp -d)
export XDG_STATE_HOME=$t/state XDG_CONFIG_HOME=$t/config XDG_CACHE_HOME=$t/cache XDG_DATA_HOME=$t/data
export IOSSTUDIO_RUNTIME_DIR=$t/run IOSSTUDIO_DEMO=1   # keep XDG_RUNTIME_DIR (Wayland); socket path ≤ 107 bytes
bin/iosstudio-helper setup check                                   # helper
python3 helper/run.py iosstudio.device.client device.info          # starts this checkout's demo daemon
python3 helper/run.py iosstudio.device.client daemon.shutdown '{"all": true}'   # stop it when done
bin/iosstudio-mcp                                                  # MCP over stdio (venv under $XDG_DATA_HOME)
IOSSTUDIO_DEMO=ready qs -n -p .                                    # the app window (UI work only)
```

Real-path checks without a phone: the same commands without `IOSSTUDIO_DEMO` (the helper with fake tools from
`tests/support/`), and the device tests under the real pymobiledevice3:
`cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest $(ls helper/test_dev_*.py | sed 's#/#.#;s#\.py$##')`.

## Where new code goes

- Shared helper utilities: `helper/iosstudio/core/` (output, subproc, fileio, appdirs, jsonc, xtool_yml). Reuse them.
- A helper command group: `helper/iosstudio/features/<pkg>/` (thin `commands.py` + logic modules), one line in
  `MODULES` in `helper/iosstudio/cli.py`, a demo answer in `helper/iosstudio/demo/`.
- A phone feature: `helper/iosstudio/device/features/<name>/real.py` and `demo.py` (+ `core.py` for shared logic);
  every command and event documented in `docs/deviced-protocol.md`.
- An MCP tool: the area's module in `mcp/iosstudio_mcp/tools/`; code two tool modules need goes in `backends/`,
  shared parameter types in `core/params.py`.
- QML: data in a service in `services/` (as a `Resource`), plumbing in `core/`, pure helpers in `lib/Model.js`,
  widgets in `components/common/` (used by 2+ areas) or `components/<area>/`, one page per sidebar entry in `pages/`.
- Tests: a new file named for your change in `tests/helper`, `tests/cli`, `tests/mcp` or `tests/qml`; fakes in `tests/support/`.

Dependencies point down (pages → components → services → core → helper/daemon); a tool module never imports
another tool module; a device feature never imports `real/daemon.py`.

## Rules that aren't visible in the code

- The helper is stdlib-only and must run on Python 3.11 under `/usr/bin/python3 -I`. Only the real daemon
  imports pymobiledevice3; Pillow is optional, imported lazily with a stdlib fallback (`features/screenshots.py`).
- Real mode must work, not just demo. Real and demo device features register the same commands with the same
  reply shapes; verify real code paths with fakes at the process boundary (fake xtool/swift) or the service
  boundary (pymobiledevice3), never by changing real behavior to match demo.
- The owner's live install runs from `~/Projects/omarchy-ios-studio`. Never switch branches, commit or leave
  files there. Work in a git worktree under `~/Projects/ios-studio-wt/`.
- Never use `git stash`: the stash is shared by every worktree. Make a temporary commit or copy to scratch.
- Only one daemon may hold the phone. Ask the owner before stopping their live daemon or using the real iPhone.
- Real-iPhone tests use `~/Projects/CrashLab`. Don't create new test apps and don't commit to CrashLab.
- A feature ships complete in one PR: helper + daemon + UI + MCP tool with bounded output (paging, truncation,
  overflow to a file, no image bytes by default) + pruning of files it stores + cancel/cleanup of its jobs.
- Machine-wide setup goes in Settings once; per-project steps happen automatically. Question per-project
  buttons in issue specs.
- Performance: cheap data first and expensive work in the background, cache with invalidation, lazy pages and
  tabs, no polling while hidden, a timeout on every subprocess, a cap on every buffer, cache and file store.
- Errors are for people: a plain `message` plus a `fix` that deep-links the UI. Never tell the user to run a
  terminal command; root work goes through one `pkexec` per step, never `sudo`.
- Docs ship with the change: README.md and every affected file in `docs/`, `docs/features/`, ARCHITECTURE.md,
  CONTRIBUTING.md, and CLAUDE.md / `.claude/rules/` when a convention changes.
- PRs branch from and target `main`; never stack a PR on another feature branch. Before pushing to a PR
  branch, check `gh pr view N --json state`; push follow-ups before calling a PR ready.
- Commit messages end with the attribution lines the session gives you.
- Keep it simple: modern Python (3.11+, type hints, dataclasses, pathlib) and modern QML, no enterprise layers.

## Working with subagents

- Start every subagent prompt with: `FIRST: invoke the omarchy skill (Skill tool, skill "omarchy").`
- Give each agent one focused job and an explicit OWNED FILES list. Parallel agents get disjoint files and
  each its own worktree under `~/Projects/ios-studio-wt/`, branched from the latest merged commit.
- Subagents don't get the owner's auto memory: copy the rules they need (this file covers most) into the prompt,
  including the isolated-folders setup above, no `git stash`, and hands off the live checkout and the phone.
- Ask for a short report: commit SHA, what changed, test results, what was verified only in demo mode, and
  changes needed in files the agent didn't own.
- Check the work yourself (diff, tests, screenshots for UI). An agent's self-report isn't verification.

## Definition of done

- [ ] Tests added for the change; `.venv/bin/pytest` passes in any order.
- [ ] `.venv/bin/ruff check` clean; `qmllint` shows no new warnings in the QML files you touched.
- [ ] Works in demo mode, and the real code path is exercised (fakes at the process or service boundary).
      List anything only checked in demo, so it gets tried on the phone with CrashLab.
- [ ] MCP tool, pruning and job cleanup included when the feature needs them.
- [ ] README.md and affected docs updated in the same change.
- [ ] UI: zoomed demo-mode screenshots reviewed by you (icon/text alignment, spacing, narrow-width wrapping).
