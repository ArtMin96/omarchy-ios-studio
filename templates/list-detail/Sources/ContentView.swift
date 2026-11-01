import SwiftUI

struct ContentView: View {
    @State private var store = NoteStore()

    var body: some View {
        NavigationStack {
            List {
                ForEach($store.notes) { $note in
                    NavigationLink {
                        NoteDetailView(note: $note)
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(note.title).font(.headline)
                            if !note.body.isEmpty {
                                Text(note.body)
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                    }
                }
                .onDelete { store.notes.remove(atOffsets: $0) }
            }
            .overlay {
                if store.notes.isEmpty {
                    ContentUnavailableView("No notes", systemImage: "note.text", description: Text("Tap + to add one."))
                }
            }
            .navigationTitle("{{NAME}}")
            .toolbar {
                Button("Add", systemImage: "plus") { store.add() }
            }
        }
    }
}
