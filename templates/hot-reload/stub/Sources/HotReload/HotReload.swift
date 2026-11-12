// iOS Studio hot reload, turned off: the runtime's API, doing nothing, so sources that still use it build.
// Turn hot reload back on in iOS Studio to get the real one; remove `import HotReload` and this goes away.

import SwiftUI

/// Hot reload state and the reload callback, for code outside SwiftUI views. Off: never reloads.
public enum HotReload {
    /// Always false: hot reload is off.
    public static var isActive: Bool { false }

    /// Always 0.
    public static var generation: Int { 0 }

    /// Never calls `body`. Returns an object to keep, as the runtime does.
    @discardableResult
    public static func onReload(_ body: @escaping @MainActor () -> Void) -> AnyObject {
        ReloadToken()
    }
}

/// Does nothing while hot reload is off. Its value is always 0.
@MainActor
@propertyWrapper
public struct ObserveHotReload: DynamicProperty {
    public init() {}
    public var wrappedValue: Int { 0 }
}

extension View {
    /// Same signature as the full package (`AnyView`), so code keeps compiling while hot reload is off.
    public func hotReloadable() -> AnyView { AnyView(self) }
}

final class ReloadToken: Sendable {}
