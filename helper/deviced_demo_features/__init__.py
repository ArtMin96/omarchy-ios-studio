"""Feature modules of the demo device daemon (helper/deviced_demo.py): one per feature, mirroring
helper/deviced_features/. Each exposes `register(reg)` (deviced_kit.ModuleRegistrar). Handlers get
`(ctx, args)` where ctx is the DemoBackend (ctx.resources, ctx.client()); a plain `def` handler runs
in a worker thread, an `async def` one on the event loop. Shared fake-iPhone data and event() come
from deviced_demo_core. See docs/development.md#device-daemon-features.
"""
