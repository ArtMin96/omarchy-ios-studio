import SwiftUI

struct NoteDetailView: View {
    @Binding var note: Note

    var body: some View {
        Form {
            TextField("Title", text: $note.title)
                .font(.headline)
            TextEditor(text: $note.body)
                .frame(minHeight: 200)
        }
        .navigationTitle(note.title.isEmpty ? "Note" : note.title)
        .navigationBarTitleDisplayMode(.inline)
    }
}
