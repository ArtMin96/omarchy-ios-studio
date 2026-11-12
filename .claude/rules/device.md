---
paths:
  - "helper/iosstudio/device/**"
---
# Device daemon rules (helper/iosstudio/device)

Recipe: [CONTRIBUTING.md#add-a-device-feature](../../CONTRIBUTING.md#add-a-device-feature). Session API and
`Resources`: [docs/development.md#device-daemon-features](../../docs/development.md#device-daemon-features).
Wire format: [docs/deviced-protocol.md](../../docs/deviced-protocol.md).

## Layout

- `protocol.py` is the wire format (JSON lines, `DeviceError`), `backend.py` what the broker calls on a
  daemon (`Backend`, `TaskSet`), `kit.py` the feature registry, `Resources` and `CursorRing`. Change the
  protocol only together with docs/deviced-protocol.md.
- A feature is `features/<name>/real.py` + `demo.py`, each with `register(reg)` and the same
  `@reg.command("name.verb")` names and reply shapes. Shared stdlib-only logic goes in `core.py` (or a named
  module such as `apps/index.py`). `tests/helper/test_device_protocol.py::Parity` must pass.
- `real.py` imports from `iosstudio.device.real.core` (`emit`, `err`), never `real/daemon.py` (it runs as
  `__main__`). `demo.py` imports from `iosstudio.device.demo.core`. pymobiledevice3 classes the core
  doesn't load are imported at the top of the feature module.
- `client.py` stays stdlib-only with no package imports: the MCP server loads it by file path.

## Talking to the phone

- Only through the session's tunnel: `s = d.need_session(a)`, then `async with s.open_service(Cls)`,
  `s.cached_service`, `s.house_arrest`, `s.get_dvt()` / `s.dvt_channel(...)`, `s.port_forward(...)`. Never
  open a second tunnel (`UserspaceRsdTunnel`, `pymobiledevice3 remote …`): it breaks the daemon's link.
- Anything that changes the phone or stays open for a client goes through
  `d.resources.acquire(key, client, open_fn, close_fn, scope=..., timeout=...)`, so a crashed client never
  leaves it changed.
- One installed app: `find_app()` in `features/apps/lookup.py` or `INDEX.lookup(...)` (the cached
  `AppIndex`), never a full app list with every attribute. Call `INDEX.invalidate(udid)` after install or uninstall.
- Errors: `raise err(code, message, fix, detail)` with plain language; other exceptions go through
  `classify()`.

## The event loop

- Never block it: blocking pymobiledevice3 calls, file walks and parsing go through `asyncio.to_thread` or a
  worker thread. A demo handler written as plain `def` runs in a worker thread.
- Every background task is tracked and cancelled: `d.bg.spawn(coro, name)` (a `TaskSet`) or a feature's own
  set, cleaned up in `@reg.on_tunnel_drop`, `on_client_gone`, `on_session_end`, `on_shutdown`.
- Every await on the phone has a timeout; long-polls cap their wait (e.g. `min(wait, 20)`).
- A request is cancelled when its client disconnects (and at shutdown): a handler cleans up in
  `try/finally` (bounded: about 15 s in all) and doesn't swallow `CancelledError`. A client that starts long
  work (an install) keeps its connection open until the reply; ending a `--once` process cancels its request.
- Streams buffer in `kit.CursorRing(max_items=…, max_bytes=…)`; every queue, cache and dict that grows per
  event has a count or byte cap. Files you write are pruned (`fileio.prune`).

## Testing

- Fake pymobiledevice3 at the service boundary (see `tests/helper/test_app_index.py`,
  `test_app_lookups.py`); `tests/support/pmd3_stub.stub_pmd3()` imports real feature modules without
  pymobiledevice3. Device tests also run under the real library:
  `cd tests && PYTHONPATH=../helper:. ~/pymobile3-venv/bin/python -m unittest helper.test_dev_<name>`.
- A test or manual daemon gets its own `IOSSTUDIO_RUNTIME_DIR` (short path: sockets max out at 107 bytes);
  never connect to the owner's live socket. `tests/conftest.py` already does this for pytest.
- Try a command by hand in demo mode: `python3 helper/run.py iosstudio.device.client name.verb '{"arg": 1}'`,
  then `daemon.shutdown '{"all": true}'`. Using the real iPhone needs the owner's OK (CrashLab only).
