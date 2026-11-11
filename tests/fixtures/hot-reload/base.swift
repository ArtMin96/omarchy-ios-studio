import SwiftUI

struct Card: View {
    var body: some View {
        VStack {
            Text("Hello")
        }
    }
}

struct Wrapped: View {
    var body: some View {
        VStack {
            Text("Hello")
        }
        .hotReloadable()
    }
}
