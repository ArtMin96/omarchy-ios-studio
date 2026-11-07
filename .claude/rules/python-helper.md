---
paths:
  - "helper/iosstudio/core/**"
  - "helper/iosstudio/features/**"
  - "helper/iosstudio/demo/**"
  - "helper/iosstudio/cli.py"
---
# Helper rules (helper/iosstudio)

Recipe: [CONTRIBUTING.md#add-a-helper-command](../../CONTRIBUTING.md#add-a-helper-command). Protocol:
[docs/development.md#helper-protocol](../../docs/development.md#helper-protocol).

## Stdlib only

- Runs as `/usr/bin/python3 -I` on Python 3.11+. Import only the standard library and `iosstudio.*`.
  An optional third-party module (Pillow) is imported inside the function that needs it, with a fallback.
- No `sys.path` edits inside the package (`tests/helper/test_package_layout.py` checks it).
- CI runs `tests/helper` and `tests/cli` with nothing but pytest installed.

## Use the core modules

- `core.output`: `ok(data)` / `fail(code, message, fix)` for one-shot commands; `event`, `step`, `progress`,
  `log`, `prompt` and a final `done()` for streams. Raise `output.HelperError(code, message, fix, detail)`
  for a handled error; build `fix` with `output.fix(label, page, step, action)`. Never `print()` to stdout.
- `core.subproc`: every external program goes through `run_cmd(argv, timeout=…)` (always pass an honest
  timeout), `run_group` or `Runner` (streaming, prompts, cancel, process-group kill). The environment comes
  from `tool_env(kind)`. Never `subprocess.run` / `Popen` directly, never `shell=True`.
- `core.fileio`: `atomic_write` / `save_json` for files other processes read, `replacing(path)` when a writer
  needs a path, `lock(path)` around read-modify-write of shared files, `prune(...)` for every folder of
  outputs you create (count and byte caps), `tail_text` for big logs.
- `core.appdirs`: `state_dir()`, `cache_dir()`, `config_dir()`, `data_dir()` read the XDG variables at call
  time; use them in new code. The module constants (`STATE_DIR`, …) are fixed at import, so tests patch them
  with `mock.patch.object(appdirs, "STATE_DIR", …)`. Use the module qualified (`appdirs.x`, `output.ok`)
  so patches apply.
- `core.xtool_yml` for xtool.yml (`read`, `set_key` keeps the rest of the file), `core.jsonc` for JSON with
  comments (span-preserving edits), `core.flags.parse_flags` for argv.

## Feature package layout

- One package (or module) per group in `helper/iosstudio/features/`: `commands.py` is thin (flags in, JSON
  out, `GROUPS = {"group": {"action": handler}}`), the work is in named logic modules next to it.
- `__init__.py` re-exports `GROUPS` and has a docstring listing the commands, flags and modules.
- Import heavy or rarely used modules inside the handler that needs them, so `iosstudio-helper` starts fast
  and a broken module only breaks its own commands.
- A new group is one line in `MODULES` in `cli.py`. Demo mode answers in `helper/iosstudio/demo/demo.py`
  (`run()` routes by group/action); keep demo replies the same shape as real ones.

## Protocol

- One-shot: exactly one JSON object on stdout, exit 0 (ok), 1 (handled error) or 2 (bad usage,
  `output.EXIT_USAGE`).
- Streams: JSON lines ending with one `done`; stop on `{"cancel": true}` on stdin or SIGTERM, kill the
  process group and put back anything half-changed.
- `message` is plain language for end users and never says "run X in a terminal". Secrets (password, 2FA)
  travel only over stdin, never argv. Root work is one `pkexec helper/root/root-tasks.sh` per step, never `sudo`.

## Tests

- In process: `tests/helper/test_<feature>.py`. Subprocess: `tests/cli/` with `support.helper_json("group",
  "action", ...)` and fake tools from `tests/support/fakes.py` (the real code path, no real xtool/swift).
- Document new commands and flags in the feature's page in `docs/features/`.
