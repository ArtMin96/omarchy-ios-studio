import SwiftUI

struct HomeView: View {
    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                Image(systemName: "house.fill")
                    .font(.system(size: 48))
                    .foregroundStyle(.tint)
                Text("Welcome to {{NAME}}")
                    .font(.title.bold())
                Text("Pick a tab below to explore.")
                    .foregroundStyle(.secondary)
            }
            .padding()
            .navigationTitle("Home")
        }
    }
}
