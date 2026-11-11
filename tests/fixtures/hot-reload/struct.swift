import SwiftUI

struct Card: View {
    var body: some View {
        VStack {
            Text("Hello")
            Text("More")
        }
    }
}

struct Wrapped: View {
    var body: some View {
        VStack {
            Text("Hello")
            Image(systemName: "star")
        }
        .hotReloadable()
    }
}
