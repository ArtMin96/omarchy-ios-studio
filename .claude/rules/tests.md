---
paths:
  - "tests/**"
---
# Test rules (tests/)

Reference: [docs/development.md#tests](../../docs/development.md#tests).

## Where tests go

| Folder | What | Marker (set by folder) |
|---|---|---|
| `tests/helper/` | helper and daemon modules in process, stdlib + pytest only | none |
| `tests/cli/` | the helper as a subprocess with fake tools | `slow` |
| `tests/mcp/` | MCP tools through the real `@tool` wrapper | `mcp` |
| `tests/qml/` | `tst_*.qml` for `core/Resource.qml`, `lib/Model.js` (qmltestrunner) | none |
| `tests/e2e/` | real Swift toolchain; add `network` if it needs the internet | `slow` |

- Put new tests in a new file named for your change; fixtures in `tests/fixtures/<area>/`.
- `tests/helper` and `tests/cli` must not import mcp, pydantic or Pillow at module level (CI runs them with
  pytest only). A test that needs Swift, `swift-format` or `llvm-symbolizer` skips without it.
- `device` marks tests that need a real iPhone; none run in CI.

## Support helpers (tests/support, `import support`)

- `support.isolated_env(tmp, **extra)`, `support.helper_json("group", "action", ...)`, `support.write_exe`.
- `support.fakes`: fake xtool/swift/swift-format scripts; `support.fake_lldb`, `support.swift_format`.
- `support.mcp_harness`: `ToolCase`, `FakeDaemon`, `FakeHelper`, `err()`; `support.mcp_fakes.FakeMCP`.
- `support.pmd3_stub.stub_pmd3()`: import real device features without pymobiledevice3.
- `support.devtest`: `isolate()`, `Svc`, `FakeClock` for `test_dev_*.py` (they also run under plain unittest).
- `support.projects`: `app_project`, `package_project`, `project_with_files`.

## Isolation

- `tests/conftest.py` points every `XDG_*`, `IOSSTUDIO_RUNTIME_DIR` and `TMPDIR` into pytest's rotated temp
  folder (it keeps the last 3 runs; never write loose folders in /tmp), sets
  `IOSSTUDIO_DEMO_DELAY=0`, removes `IOSSTUDIO_DEMO`, and restores `os.environ` and the machine-wide
  device state files after each test. Don't undo it, and never read the real home folder or live socket.
- Patch a shared folder where the code reads it: `mock.patch.object(appdirs, "STATE_DIR", tmp)`.
- Use `tmp_path` / `tempfile` for files; clean up daemons, subprocesses and tasks you start.

## Fakes at the boundary

- Fake at the process boundary (an executable on PATH or passed in) or the service boundary
  (pymobiledevice3 service classes), so the real parsing, timeouts and error mapping run.
  Don't mock the function under test or the module's own helpers.
- MCP: `FakeDaemon` replies over the real socket protocol; `FakeHelper` is a real executable.
- Keep fake replies the same shape as the real ones (the parity tests compare demo and real shapes).

## Timing

- No fixed sleeps to wait for something. Use `await until(cond, timeout=…, what=…)` (`from support.waiting import until`),
  an `asyncio.Event` (e.g. as a `FakeDaemon` reply delay), or `devtest.FakeClock`.
- Timeouts in tests are upper bounds for a slow machine, never the expected duration.

## Must hold

- Every test passes alone, in any order and in the full run: `.venv/bin/pytest`, and
  `python3 -m pytest tests/helper tests/cli` with a Python that has only pytest installed.
- `.venv/bin/ruff check` is clean for test files too.
