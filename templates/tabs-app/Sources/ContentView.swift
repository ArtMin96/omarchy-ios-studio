import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            HomeView()
                .tabItem { Label("Home", systemImage: "house") }
            FavoritesView()
                .tabItem { Label("Favorites", systemImage: "star") }
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gear") }
        }
    }
}
