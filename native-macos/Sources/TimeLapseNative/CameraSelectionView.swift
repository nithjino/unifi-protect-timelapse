import SwiftUI

struct CameraSelectionView: View {
    let cameras: [CameraInfo]
    @State private var selectedIDs: Set<String>
    let onSave: (Set<String>) -> Void
    let onCancel: () -> Void

    init(
        cameras: [CameraInfo],
        selectedIDs: Set<String>,
        onSave: @escaping (Set<String>) -> Void,
        onCancel: @escaping () -> Void
    ) {
        self.cameras = cameras
        _selectedIDs = State(initialValue: selectedIDs)
        self.onSave = onSave
        self.onCancel = onCancel
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Select Cameras")
                .font(.title2.bold())
            Text("Choose one or more cameras. Each camera downloads in its own background process.")
                .foregroundStyle(.secondary)
            List(cameras) { camera in
                Toggle(isOn: binding(for: camera.id)) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(camera.name)
                        let details = [camera.state, camera.model].compactMap { $0 }.filter { !$0.isEmpty }
                        if !details.isEmpty {
                            Text(details.joined(separator: " • "))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .toggleStyle(.checkbox)
                .help("Camera ID: \(camera.id)")
            }
            HStack {
                Button("Select All") { selectedIDs = Set(cameras.map(\.id)) }
                Button("Clear") { selectedIDs.removeAll() }
                Spacer()
                Button("Cancel", role: .cancel) { onCancel() }
                    .keyboardShortcut(.cancelAction)
                Button("OK") { onSave(selectedIDs) }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 540, height: 450)
    }

    private func binding(for id: String) -> Binding<Bool> {
        Binding(
            get: { selectedIDs.contains(id) },
            set: { selected in
                if selected { selectedIDs.insert(id) } else { selectedIDs.remove(id) }
            }
        )
    }
}
