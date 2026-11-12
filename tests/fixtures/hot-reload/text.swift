import SwiftUI

struct Card: View {
    var body: some View {
        VStack {
            Text("Changed")
        }
    }
}

struct Wrapped: View {
    var body: some View {
        VStack {
            Text("Changed")
        }
        .hotReloadable()
    }
}
