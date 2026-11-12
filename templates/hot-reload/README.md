# Hot reload runtime (maintainers)

The Swift package iOS Studio copies into a project when hot reload is turned on (`hotreload enable`). It is the
only code of iOS Studio that runs inside the user's app. User-facing docs: `docs/features/hot-reload.md`.

```
version.txt                       bump on any change here: enable re-copies the package when it differs
full/Package.swift                library "HotReload"; C target HotReloadLoader + Swift target HotReload
full/Sources/HotReloadLoader/     loader.c, hr_rules.h (its symbol rules, also compiled by the tests), include/HotReloadLoader.h
full/Sources/HotReload/           HotReload.swift: the public API
stub/                             same product, module and API as no-ops; no C target, no linker flags
```

`full/` or `stub/` (the contents) goes to `<project>/.iosstudio/hot-reload/` (SwiftPM identity `hot-reload`),
next to a `.iosstudio/.gitignore` of `*`. The managed block at the end of the app's Package.swift adds the
dependency and, for `full` only, `-Xlinker -interposable -Xlinker -u -Xlinker _iosstudio_hot_reload_loader` in
debug. `stub` is used when hot reload is off but sources still `import HotReload`, so they keep building.

## Public API (identical in full and stub)

```swift
@MainActor @propertyWrapper public struct ObserveHotReload: DynamicProperty {
    public init()
    public var wrappedValue: Int                       // the reload generation
}
extension View { public func hotReloadable() -> AnyView }     // AnyView(self) in full and stub
public enum HotReload {
    public static var isActive: Bool { get }           // the loader is watching (full debug build)
    public static var generation: Int { get }          // patches applied since launch
    @discardableResult
    public static func onReload(_ body: @escaping @MainActor () -> Void) -> AnyObject  // keep the token
}
```

`@ObserveHotReload var reload` holds a `@StateObject` that publishes on each reload, so the view redraws even when
`reload` is never read. `.hotReloadable()` erases `body`'s type to `AnyView` (it must be declared `-> AnyView`: with `-> some View` the opaque type still carries the inner view type), so a patch may return another kind of view.
`onReload` callbacks run on the main thread and stop when the token is released.

Everything is gated on `IOSSTUDIO_HOT_RELOAD`, which the package defines for the debug configuration only (C and
Swift), rather than on `DEBUG`, so it doesn't depend on what the build system defines. A release build of `full`
has a C object holding only the constant `iosstudio_hot_reload_loader`, and the Swift side compiles the no-op
branches. The constant stays because SwiftPM's SwiftBuild backend (xtool) drops the `.when(configuration:
.debug)` condition of the block's `unsafeFlags` linker settings (checked in the generated PIF), so release links
get `-u _iosstudio_hot_reload_loader` (and `-interposable`) too.

## The on-device folder protocol

Folder: `$HOME/Library/Caches/HotReload/` in the app's data container (house_arrest reaches it as
`Library/Caches/HotReload/`).

| File | Writer | Meaning |
|---|---|---|
| `runtime.json` | runtime, at launch | `{"version": 1, "pid": N, "started": unix_seconds}`: a hot-reload build is running |
| `patch-<seq>.dylib.part` | iOS Studio | upload in progress (ignored) |
| `patch-<seq>.dylib` | iOS Studio | renamed from `.part` when complete; seq starts at 1 and only grows |
| `ack-<seq>.json` | runtime | `{"seq", "ok", "ms", "swapped", "kept", "vtable", "reverted", "error", "generation"}` |

All runtime writes go through `<name>.tmp` + `rename`. At launch the runtime deletes every `patch-*` and `ack-*`.
A `DISPATCH_SOURCE_TYPE_VNODE` source on the folder (write/delete/rename; no polling) wakes it; it then loads
only the newest `patch-<seq>.dylib` with a seq above the last one it looked at (patches are cumulative), acks it,
and deletes patches and acks older than seq − 3. If the folder itself is removed (also with all of
`Library/Caches`), it is made again with a new `runtime.json` (same pid and start time) and watched anew. Patches
are never `dlclose`d; after 100 loaded patches in one run, later ones are refused (see Limits).

`reverted` is an addition to the design contract: the number of functions an earlier patch had replaced that
this patch no longer contains (the file went back to how it was built), now pointing at the app's code again.

After an applied patch the main queue bumps the generation, posts `dev.iosstudio.hot-reload` on
`CFNotificationCenterGetLocalCenter()` and logs one line, e.g. `[HotReload] patch 3: swapped 4 functions (12
kept) in 3.1 ms` or `[HotReload] patch 3 failed: …`. The line goes to stderr (Logs › Console, when iOS Studio
launched the app with the console) and to `os_log` (Logs › Device log). The Swift side listens with
`CFNotificationCenterAddObserver` on the same center, so it doesn't rely on that center being
`NotificationCenter.default`.

## The rebinding algorithm

Built with `-interposable`, the app calls its own exported functions through `__stubs` → `__got` (listed in the
indirect symbol table) instead of directly, and lld writes those self-references as flat-namespace binds. The
patch is linked the same way plus `-undefined dynamic_lookup`. After `dlopen(path, RTLD_NOW | RTLD_LOCAL)` the
runtime finds the app (the `MH_EXECUTE` image) and the patch (the newest image named `…/HotReload/patch-N.dylib`,
or the same device and inode) and sorts each exported symbol the patch defines (`iextdefsym`/`nextdefsym`):

1. **swap**: defined in the patch's `__TEXT,__text`, also defined by the app (`dlsym(RTLD_MAIN_ONLY)`), not a
   Swift metadata accessor (`$s…Ma`) and not an addressor (`$s…v` + `a`/`l` + `u`/`O`/`o`/`p`, optional `Z`;
   in practice `…vau`, which Swift emits for every global and static stored property). The app's pointers for it
   should hold the patch's function. An addressor reaches its own storage and its `Wz` once token / `WZ`
   initializer directly, so swapping it would hand old and new code the patch's fresh storage (a second
   `static let shared` instance, globals back to their initial values).
2. **async function pointer** (`$s…Tu`, a `{int32 relative pointer to the function, uint32 context size}` record
   in `__DATA,__data`) that the app also defines: callers read the context size from the app's record, allocate
   that much and call through the swapped pointers. If the patch's record asks for more than the app's for any
   function, the whole patch is refused before anything changes, with the ack error `<function> now needs more
   memory per call than the running app reserved. Press Run to rebuild.` (the name through the runtime's
   `swift_demangle`). Otherwise it is swapped like a function, which matters for vtables: an async method's slot
   holds its `…Tu`, not the function.
3. **keep**: any other symbol the app also defines (metadata, descriptors, accessors, addressors, witness tables,
   global storage). The patch's pointers for it should hold the app's, so new code uses the original types and
   state.
4. not in the app at all (a new function): the patch's pointers for it should hold its own. Flat-namespace binds
   search the app and older patches first, so without this, calls between the patch's own new or changed
   functions could reach old copies. The same goes for swap symbols, which the patch's pointers also get.

Then, in this order:

- **patch pass**: every `S_NON_LAZY_SYMBOL_POINTERS` / `S_LAZY_SYMBOL_POINTERS` section of a `__DATA*` or
  `__AUTH*` segment of the patch: each slot whose indirect symbol is in the table gets its target. A failure
  here stops the reload before any of the patch's code can be reached.
- **app pass**: the same over the app with swap symbols → patch, plus functions installed by an earlier patch
  that this one lacks → the app's original (reverted).
- **vtables**: Swift class metadata (and so vtables) lives in the app's `__DATA,__data` (checked on a built
  app); with `-interposable` its entries are binds too. Every aligned word of `__DATA,__data` and
  `__DATA,__objc_data` (also in `__DATA_DIRTY`) equal to a function's (or async function pointer's) original
  address or the address the previous patch installed becomes the new one. Capped at 64 MB scanned.

Writes: the page range of the section is queried with `vm_region_64`; if it isn't writable (`__DATA_CONST` after
launch), `vm_protect(…, VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY)`, write each slot with one aligned
store, restore the old protection. A section that can't be made writable is skipped and counted; the ack then
says `ok: false`. `swapped` counts swapped functions that at least one app pointer or vtable word now reaches; a
class method (the app has its method descriptor, `…Tq`) counts only when a vtable word was rewritten for it or its
`…Tu`, since calls to it go through the vtable.

Chained fixups (what the app and patch get on iOS 17): dyld has already rewritten the chains into plain pointers
by the time the runtime runs, and the indirect symbol table and `__got` still exist, so nothing differs. arm64 only;
under arm64e (`ptrauth_calls`) the `__auth*` sections are skipped, since an unsigned pointer there would fault.

Safety: every Mach-O offset is checked against its load command or `__LINKEDIT`, loops are bounded (load
commands, directory entries, symbol counts, scan bytes), allocations are proportional to the patch's own symbol
table, and all work runs on a private utility queue off the launch path.

## Limits

- Only what goes through a pointer the runtime can find is replaced: calls to exported functions (via `__got`)
  and class vtables. Direct references to data (a `let` global, a non-generic type's metadata) inside the patch
  bind to the patch's copy only if the linker sent them through `__got`; `checks.py` refuses changes to types and
  stored properties for this reason, and `private`/`fileprivate` functions are local symbols reached only
  through their (swapped) callers.
- Code already running, closures and function values captured before the reload (a `Task`, a stored callback,
  a protocol witness table built elsewhere) keep the old code until they are created again.
- A patch carries its own copy of the type metadata of the types its files declare, and its code mostly reaches
  that copy directly. To the Swift runtime and SwiftUI those are other types: a child view declared in a patched
  file can lose its `@State` on the first reload, and `as!`/`as?` between a value made by the app's code and the
  type as the patch sees it can fail. Globals and static stored properties are shared (their addressors are kept).
- Static initializers in the patch run on `dlopen`. Every class a patch declares (Swift classes are Objective-C
  classes on Apple platforms, not only `@objc` ones) registers a second time, and the Objective-C runtime logs
  "Class … is implemented in both …" once per class and reload.
- An async function whose new body needs a bigger async context than the build's (more values kept across an
  `await`) is refused, since callers allocate the size the app was built with: press Run. A smaller one is fine.
- Methods of a generic class (or one whose metadata is built at run time) aren't swapped for calls through the
  vtable: its vtable lives in heap metadata the scan never sees. They don't count in `swapped`.
- Patches stay loaded until the app exits (a few hundred KB each, mostly clean file-backed pages; each also adds
  its metadata and conformance records to the Swift runtime's lookups and its classes to the Objective-C
  runtime). After 100 in one run the runtime refuses more (`Too many reloads in this run (100). Press Run to start
  fresh.`): enough for a long session of saves, and it keeps that growth bounded without a measurement on a phone.
- A body whose concrete view type changes needs `.hotReloadable()`, else the helper refuses it (opaque type check).

## Verified on Linux, and what needs an iPhone

Checked here: both packages build for `arm64-apple-ios` in debug and release, in Swift 6 language mode, with
`-Wall -Wextra -Wconversion` and the clang static analyzer clean on the loader; a release build's loader object
holds only the `-u` constant; an xtool build (debug and release) of an app with the managed block contains `_iosstudio_hot_reload_loader`, the
constructor (in `__TEXT,__init_offsets`, the chained-fixups form of `__mod_init_func`) and indirect `__got`
entries for its own Swift functions; class metadata sits in `__DATA,__data` with its vtable entries as binds. The
stub builds with the block without linker flags. `tests/cli/test_hot_reload_runtime.py` repeats the package builds (the release one with both SwiftPM build
systems: native and SwiftBuild, what xtool uses) and compiles `hr_rules.h` for the host to check it against a small
app and patch built for `arm64-apple-ios`: addressors are kept and reached through the patch's symbol pointers,
async function pointer records have the layout the loader reads, a grown context is caught, and an async method's
vtable slot binds its `…Tu`.

Only a device can show:

- the constructor runs, `runtime.json` appears and the vnode source fires on a rename into the folder from
  house_arrest;
- `dlopen` of a patch signed with the app's team passes library validation from the data container (`Caches`);
- `dlsym(RTLD_MAIN_ONLY, …)` finds the app's Swift symbols (they are in the export trie with `-rdynamic`);
- `vm_protect` with `VM_PROT_COPY` is allowed on `__DATA_CONST` of a development-signed app;
- after the swap SwiftUI shows the new body, `@ObserveHotReload` redraws, `onReload` fires, and the vtable
  rewrite takes effect for a changed class method;
- the log line shows in Logs › Console and Logs › Device log;
- the Swift runtime copes with the patch's duplicate type and conformance records (it should use the app's);
- after a reload of the file declaring `static let shared = …`, the app keeps its one instance (init doesn't run
  again), and a patch with a grown async function is refused with the ack message;
- an async class method's change takes effect through the vtable;
- the folder and `runtime.json` come back after `Library/Caches` is deleted while the app runs;
- reverting a file (cumulative patch without it) brings back the original behaviour.
