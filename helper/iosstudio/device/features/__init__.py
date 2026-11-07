"""Device daemon features: one folder per feature, real and demo side by side.

    features/<name>/real.py   the real iPhone (pymobiledevice3), loaded by real/daemon.py
    features/<name>/demo.py   the made-up iPhone (stdlib only), loaded by demo/daemon.py
    features/<name>/*.py      anything else is shared logic either side imports (core.py, ...); never loaded itself

kit.load_features() imports every folder's real.py or demo.py and calls its `register(reg)` with a
kit.ModuleRegistrar named after the folder. A module that fails to import or register is logged to deviced.log and
listed by `daemon.features`; the rest keep working. Both sides of a feature register the same commands (the parity
test in tests/helper/test_device_protocol.py checks it).

Handlers get `(ctx, args)`: the backend.Backend (real: real.daemon.Daemon with d.need_session(), d.rsd(), d.pmd3;
demo: demo.daemon.DemoBackend), with ctx.resources, ctx.bg and ctx.client(). A plain `def` handler runs in a worker
thread, an `async def` one on the event loop. Real modules open phone services with `s.open_service(...)` and take
shared helpers from real/core.py; demo modules take the fake iPhone's data and event() from demo/core.py. Never
import a daemon module. See docs/development.md#device-daemon-features.
"""
