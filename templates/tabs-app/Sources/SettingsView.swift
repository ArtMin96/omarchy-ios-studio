import SwiftUI

struct SettingsView: View {
    @AppStorage("notifications") private var notifications = true
    @AppStorage("username") private var username = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Profile") {
                    TextField("Your name", text: $username)
                }
                Section("Preferences") {
                    Toggle("Notifications", isOn: $notifications)
                }
            }
            .navigationTitle("Settings")
        }
    }
}
