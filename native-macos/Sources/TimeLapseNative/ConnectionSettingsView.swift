import SwiftUI

struct ConnectionSettingsView: View {
    @State private var profileName: String
    @State private var draft: ConnectionSettings
    @State private var validationError: String?
    let firstRun: Bool
    let isNewProfile: Bool
    let onSave: (String, ConnectionSettings) -> String?
    let onCancel: () -> Void

    init(
        profileName: String,
        settings: ConnectionSettings,
        firstRun: Bool,
        isNewProfile: Bool,
        onSave: @escaping (String, ConnectionSettings) -> String?,
        onCancel: @escaping () -> Void
    ) {
        _profileName = State(initialValue: profileName)
        _draft = State(initialValue: settings)
        self.firstRun = firstRun
        self.isNewProfile = isNewProfile
        self.onSave = onSave
        self.onCancel = onCancel
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(firstRun ? "Set Up UniFi Protect" : isNewProfile ? "New Connection Profile" : "Edit Connection Profile")
                .font(.title2.bold())
            Text(
                firstRun
                    ? "Enter the connection details needed to list cameras and export recordings."
                    : "Update the connection details used for future camera lists and downloads."
            )
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 10) {
                GridRow {
                    credentialLabel("Profile name")
                    TextField("Defaults to Protect URL", text: $profileName)
                        .accessibilityLabel("Profile name")
                        .credentialField()
                        .help("A friendly name shown in the profile menu. Leave it blank to use the Protect URL.")
                }
                Divider().gridCellColumns(2)
                GridRow {
                    credentialLabel("Protect URL")
                    TextField("", text: $draft.instanceURL)
                        .accessibilityLabel("Protect URL")
                        .credentialField()
                        .help(urlHelp)
                }
                Divider().gridCellColumns(2)
                GridRow {
                    credentialLabel("API token")
                    SecureField("", text: $draft.token)
                        .accessibilityLabel("API token")
                        .credentialField()
                        .help(tokenHelp)
                }
                Divider().gridCellColumns(2)
                GridRow {
                    credentialLabel("Local username")
                    TextField("", text: $draft.username)
                        .accessibilityLabel("Local username")
                        .credentialField()
                        .help(usernameHelp)
                }
                Divider().gridCellColumns(2)
                GridRow {
                    credentialLabel("Local password")
                    SecureField("", text: $draft.password)
                        .accessibilityLabel("Local password")
                        .credentialField()
                        .help(passwordHelp)
                }
                Divider().gridCellColumns(2)
                GridRow {
                    HStack {
                        Text("Verify the server’s TLS certificate")
                        Spacer()
                        Toggle("", isOn: $draft.verifySSL)
                            .labelsHidden()
                            .toggleStyle(.switch)
                            .help(verifySSLHelp)
                    }
                    .gridCellColumns(2)
                }
            }
            .padding(12)
            .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
            if let validationError {
                Label(validationError, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Label("Stored securely in your macOS login Keychain.", systemImage: "key.fill")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .help("The URL, API token, username, and password are encrypted by macOS Keychain Services and are not written to a .env file.")
            HStack {
                Spacer()
                Button(firstRun ? "Quit" : "Cancel", role: .cancel) { onCancel() }
                    .keyboardShortcut(.cancelAction)
                Button("Save") {
                    validationError = onSave(profileName, draft)
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 600)
    }

    private let urlHelp = "The UniFi Protect Integration API address, for example https://protect.local/proxy/protect/integration/v1."
    private let tokenHelp = "Used to query the Protect Integration API and retrieve the list of cameras."
    private let usernameHelp = "A dedicated local Protect user used to authenticate video exports. Grant it only permission to view and export recordings."
    private let passwordHelp = "The password for the dedicated local Protect user. The API token can list cameras but cannot export recordings."
    private let verifySSLHelp = "Verifies the Protect server’s TLS certificate. Disable only for a trusted local console using a self-signed certificate."

    private func credentialLabel(_ title: String) -> some View {
        Text(title)
            .frame(width: 120, alignment: .leading)
    }
}

private extension View {
    func credentialField() -> some View {
        textFieldStyle(.roundedBorder)
            .lineLimit(1)
            .frame(width: 390)
    }
}
