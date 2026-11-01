import SwiftUI

struct ContentView: View {
    @State private var taps = 0

    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "sparkles")
                .font(.system(size: 56))
                .foregroundStyle(.tint)
            Text("Hello from {{NAME}}")
                .font(.largeTitle.bold())
                .multilineTextAlignment(.center)
            Text("Built on Linux with iOS Studio.")
                .foregroundStyle(.secondary)
            Button {
                taps += 1
            } label: {
                Label(taps == 0 ? "Tap me" : "Tapped \(taps) times", systemImage: "hand.tap")
                    .padding(.horizontal, 8)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
        .padding()
    }
}
