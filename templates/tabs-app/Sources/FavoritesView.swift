import SwiftUI

struct FavoritesView: View {
    @State private var favorites = ["Coffee", "Sunsets", "Long walks"]

    var body: some View {
        NavigationStack {
            List {
                ForEach(favorites, id: \.self) { item in
                    Label(item, systemImage: "star.fill")
                }
                .onDelete { favorites.remove(atOffsets: $0) }
            }
            .navigationTitle("Favorites")
        }
    }
}
