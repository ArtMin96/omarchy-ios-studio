import Foundation
import Observation

struct Note: Identifiable, Hashable {
    let id = UUID()
    var title: String
    var body: String
}

@Observable
final class NoteStore {
    var notes: [Note] = [
        Note(title: "Welcome", body: "This is {{NAME}}. Tap a note to edit it."),
        Note(title: "Ideas", body: "Swipe left on a note to delete it."),
    ]

    func add() {
        notes.insert(Note(title: "New note", body: ""), at: 0)
    }
}
