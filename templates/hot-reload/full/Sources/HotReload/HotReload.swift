// iOS Studio hot reload: the Swift side of the runtime. The same API as the stub package (which does nothing).
//
//     struct ContentView: View {
//         @ObserveHotReload var reload
//         var body: some View {
//             VStack { … }
//                 .hotReloadable()
//         }
//     }
//
// In a debug build iOS Studio swaps the new code of a saved file into the running app; `@ObserveHotReload`
// redraws the view afterwards, and `.hotReloadable()` lets `body` return a different kind of view after a
// change. In a release build both do nothing and the loader isn't in the package.

import SwiftUI
#if IOSSTUDIO_HOT_RELOAD
import CoreFoundation
import Foundation
internal import HotReloadLoader
#endif

/// Hot reload state and the reload callback, for code outside SwiftUI views.
public enum HotReload {
    /// True when the loader is running in this process (a debug build with hot reload on).
    public static var isActive: Bool {
        #if IOSSTUDIO_HOT_RELOAD
        iosstudio_hot_reload_is_active() != 0
        #else
        false
        #endif
    }

    /// How many patches have been applied since launch.
    public static var generation: Int {
        #if IOSSTUDIO_HOT_RELOAD
        Int(iosstudio_hot_reload_generation())
        #else
        0
        #endif
    }

    /// Calls `body` on the main thread after each applied patch (for UIKit: reload a table, redo a layout).
    /// Keep the returned object: the callback stops when it is released.
    @discardableResult
    public static func onReload(_ body: @escaping @MainActor () -> Void) -> AnyObject {
        let token = ReloadToken(body)
        #if IOSSTUDIO_HOT_RELOAD
        ReloadRegistry.shared.add(token)
        #endif
        return token
    }
}

/// Redraws the view after each hot reload. Its value is the reload generation.
@MainActor
@propertyWrapper
public struct ObserveHotReload: DynamicProperty {
    #if IOSSTUDIO_HOT_RELOAD
    @StateObject private var observer = ReloadObserver()
    #endif

    public init() {}

    public var wrappedValue: Int {
        #if IOSSTUDIO_HOT_RELOAD
        observer.generation
        #else
        0
        #endif
    }
}

extension View {
    /// Lets `body` return a different kind of view after a hot reload. It returns `AnyView` (not `some View`):
    /// only then is `body`'s concrete type the same before and after a structure change, which the helper's
    /// opaque-type check requires. The stub returns `AnyView` too, so the signature never changes.
    public func hotReloadable() -> AnyView { AnyView(self) }
}

// MARK: - Internals

final class ReloadToken: @unchecked Sendable {   // immutable after init
    let body: @MainActor () -> Void
    init(_ body: @escaping @MainActor () -> Void) { self.body = body }
}

#if IOSSTUDIO_HOT_RELOAD
/// The callbacks, weakly held. The loader posts "dev.iosstudio.hot-reload" to the local notification center
/// on the main queue; one CoreFoundation observer fans it out.
final class ReloadRegistry: @unchecked Sendable {   // state guarded by `lock`
    static let shared = ReloadRegistry()

    private struct Weak { weak var token: ReloadToken? }
    private let lock = NSLock()
    private var tokens: [Weak] = []
    private var observing = false

    func add(_ token: ReloadToken) {
        lock.lock()
        defer { lock.unlock() }
        tokens.removeAll { $0.token == nil }
        tokens.append(Weak(token: token))
        guard !observing else { return }
        observing = true
        CFNotificationCenterAddObserver(
            CFNotificationCenterGetLocalCenter(),
            Unmanaged.passUnretained(self).toOpaque(),
            { _, _, _, _, _ in ReloadRegistry.shared.fire() },
            "dev.iosstudio.hot-reload" as CFString,
            nil,
            .deliverImmediately)
    }

    private func fire() {
        lock.lock()
        let live = tokens.compactMap(\.token)
        lock.unlock()
        guard Thread.isMainThread else {   // the loader always posts on the main queue
            DispatchQueue.main.async { for token in live { MainActor.assumeIsolated { token.body() } } }
            return
        }
        for token in live { MainActor.assumeIsolated { token.body() } }
    }
}

@MainActor
final class ReloadObserver: ObservableObject {
    @Published private(set) var generation = HotReload.generation
    private var token: AnyObject?

    init() {
        token = HotReload.onReload { [weak self] in self?.generation = HotReload.generation }
    }
}
#endif
