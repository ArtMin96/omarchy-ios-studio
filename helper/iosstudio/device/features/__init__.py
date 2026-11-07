"""Feature modules of the real device daemon (helper/iosstudio/device/deviced.py).

Every `<name>.py` here (not starting with `_`) is imported at daemon start and its
`register(reg)` called with a deviced_kit.ModuleRegistrar. A module that fails to import or
register is logged to deviced.log and listed by `daemon.features`; the rest keep working.
Handlers get `(d, args)`: d is the deviced.Daemon, a backend.Backend (d.need_session(), d.rsd(), d.pmd3,
d.resources, d.bg, d.client()); as in the demo, a plain `def` handler runs in a worker thread, an
`async def` one on the event loop. Open phone services with `s.open_service(...)` (bounded, see its
docstring). Shared helpers come from deviced_core. Never `import deviced`.
See docs/development.md#device-daemon-features.
"""
