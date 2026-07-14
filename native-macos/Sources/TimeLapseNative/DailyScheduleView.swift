import AppKit
import SwiftUI

struct DailyScheduleView: View {
    let cameras: [CameraInfo]
    let initialSelectedIDs: Set<String>
    let initialOutputDirectory: URL
    let onSave: (Set<String>, URL) -> Void
    let onCancel: () -> Void

    @State private var selectedIDs: Set<String>
    @State private var outputDirectory: URL

    init(
        cameras: [CameraInfo],
        initialSelectedIDs: Set<String>,
        initialOutputDirectory: URL,
        onSave: @escaping (Set<String>, URL) -> Void,
        onCancel: @escaping () -> Void
    ) {
        self.cameras = cameras
        self.initialSelectedIDs = initialSelectedIDs
        self.initialOutputDirectory = initialOutputDirectory
        self.onSave = onSave
        self.onCancel = onCancel
        _selectedIDs = State(initialValue: initialSelectedIDs)
        _outputDirectory = State(initialValue: initialOutputDirectory)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Daily Automatic Timelapses").font(.title2.bold())
            Text("The latest completed day is exported now, then each completed local day is exported while TimeLapse stays open.")
                .foregroundStyle(.secondary)
            List(cameras) { camera in
                Toggle(isOn: selectionBinding(for: camera.id)) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(camera.name)
                        Text([camera.state, camera.model].compactMap { $0 }.joined(separator: " • "))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .frame(minHeight: 210)
            HStack {
                Text("Save to").foregroundStyle(.secondary)
                Text(outputDirectory.path)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(outputDirectory.path)
                Spacer()
                Button("Choose…", action: chooseOutputDirectory)
            }
            HStack {
                Button("Select All") { selectedIDs = Set(cameras.map(\.id)) }
                Button("Clear") { selectedIDs.removeAll() }
                Spacer()
                Button("Cancel", action: onCancel)
                Button("Enable Daily Job") { onSave(selectedIDs, outputDirectory) }
                    .buttonStyle(.borderedProminent)
                    .disabled(selectedIDs.isEmpty)
            }
        }
        .padding(20)
        .frame(width: 590, height: 480)
    }

    private func selectionBinding(for id: String) -> Binding<Bool> {
        Binding(
            get: { selectedIDs.contains(id) },
            set: { selected in
                if selected { selectedIDs.insert(id) } else { selectedIDs.remove(id) }
            }
        )
    }

    private func chooseOutputDirectory() {
        let panel = NSOpenPanel()
        panel.title = "Choose Daily Timelapse Folder"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = outputDirectory
        if panel.runModal() == .OK, let selected = panel.url {
            outputDirectory = selected
        }
    }
}
