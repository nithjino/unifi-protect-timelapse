import SwiftUI

struct MainView: View {
    private enum JobListTab: String, CaseIterable {
        case downloads = "Downloads"
        case dailyAutomations = "Daily Automations"

        var showsDailyAutomations: Bool { self == .dailyAutomations }
    }

    @ObservedObject var model: AppModel
    @Environment(\.openWindow) private var openWindow
    @State private var selectedJobIDs: Set<UUID> = []
    @State private var selectedJobListTab = JobListTab.downloads

    var body: some View {
        ZStack {
            VStack(spacing: 12) {
                connectionGroup
                newTimelapseGroup
                downloadsGroup
                statusBar
            }
            .padding(16)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)

            if model.showingConnectionSheet {
                connectionOverlay
            } else if model.showingCameraSheet {
                cameraOverlay
            } else if model.showingDailyScheduleSheet {
                dailyScheduleOverlay
            }
        }
        .onAppear { model.start() }
        .alert(item: $model.alert) { alert in
            Alert(title: Text(alert.title), message: Text(alert.message), dismissButton: .default(Text("OK")))
        }
    }

    private var connectionOverlay: some View {
        ZStack {
            modalBackdrop
            ConnectionSettingsView(
                profileName: model.profileBeingEdited?.name ?? "",
                settings: model.profileBeingEdited?.settings ?? ConnectionSettings(),
                firstRun: model.isFirstRun,
                isNewProfile: model.profileBeingEdited == nil,
                onSave: model.saveConnection,
                onCancel: model.cancelConnectionSheet
            )
            .background(Color(nsColor: .windowBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            .shadow(radius: 18, y: 8)
        }
        .transition(.opacity.combined(with: .scale(scale: 0.98)))
    }

    private var cameraOverlay: some View {
        ZStack {
            modalBackdrop
            CameraSelectionView(
                cameras: model.cameras,
                selectedIDs: model.selectedCameraIDs,
                onSave: model.applyCameraSelection,
                onCancel: { model.showingCameraSheet = false }
            )
            .background(Color(nsColor: .windowBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            .shadow(radius: 18, y: 8)
        }
        .transition(.opacity.combined(with: .scale(scale: 0.98)))
    }

    private var dailyScheduleOverlay: some View {
        ZStack {
            modalBackdrop
            DailyScheduleView(
                cameras: model.cameras,
                initialSelectedIDs: model.selectedCameraIDs,
                initialOutputDirectory: model.outputDirectory,
                onSave: model.configureDailySchedule,
                onCancel: model.cancelDailyScheduleSheet
            )
            .background(Color(nsColor: .windowBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            .shadow(radius: 18, y: 8)
        }
        .transition(.opacity.combined(with: .scale(scale: 0.98)))
    }

    private var modalBackdrop: some View {
        Color.black.opacity(0.18)
            .ignoresSafeArea()
            .contentShape(Rectangle())
    }

    private var connectionGroup: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 10) {
                Label("Connection", systemImage: "network")
                    .font(.headline)
                Picker("Profile", selection: profileSelection) {
                    if model.profiles.isEmpty {
                        Text("No profiles").tag(nil as UUID?)
                    }
                    ForEach(model.profiles) { profile in
                        Text(profile.displayName).tag(profile.id as UUID?)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .frame(minWidth: 180, idealWidth: 250, maxWidth: 330)
                Spacer()
                Button("New", systemImage: "plus") { model.presentNewProfile() }
                Button("Edit", systemImage: "pencil") { model.presentConnectionSettings() }
                    .disabled(model.selectedProfile == nil)
            }
            Text(model.settings.instanceURL.isEmpty ? "Create a profile to connect to UniFi Protect." : model.settings.instanceURL)
                .font(.callout)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
        }
        .modernCard()
    }

    private var newTimelapseGroup: some View {
        VStack(alignment: .leading, spacing: 11) {
            Label("New Timelapse", systemImage: "timelapse")
                .font(.headline)
            HStack(alignment: .top, spacing: 18) {
                Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 9) {
                    GridRow {
                        Text("Start").foregroundStyle(.secondary)
                        compactDatePicker("Start", selection: $model.startDate)
                    }
                    GridRow {
                        Text("End").foregroundStyle(.secondary)
                        compactDatePicker("End", selection: $model.endDate)
                    }
                    GridRow {
                        Text("Speed").foregroundStyle(.secondary)
                        Picker("Speed", selection: $model.speed) {
                            ForEach(model.speeds, id: \.self) { speed in Text(speed).tag(speed) }
                        }
                        .labelsHidden()
                        .pickerStyle(.menu)
                        .frame(width: 105, alignment: .leading)
                    }
                    GridRow {
                        Text("")
                        Toggle("24-hour timelapse", isOn: fullDayModeBinding)
                            .help("Use date-only controls and export exactly one complete local calendar day.")
                    }
                }
                .fixedSize(horizontal: true, vertical: false)
                Divider()
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 8) {
                        Label("Save to", systemImage: "folder")
                            .foregroundStyle(.secondary)
                            .frame(width: 86, alignment: .leading)
                        Text(model.outputDirectory.path)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .help(model.outputDirectory.path)
                        Button("Choose…") { model.chooseOutputDirectory() }
                    }
                    HStack(spacing: 8) {
                        Label("Cameras", systemImage: "video")
                            .foregroundStyle(.secondary)
                            .frame(width: 86, alignment: .leading)
                        Text(model.cameraSummary)
                            .lineLimit(1)
                            .frame(width: 150, alignment: .leading)
                            .help(model.selectedCameras.map(\.name).joined(separator: ", "))
                        Button("Select…") { model.requestCameraSelection() }
                            .disabled(model.isLoadingCameras)
                        Button("Refresh", systemImage: "arrow.clockwise") { model.loadCameras(openSelection: true) }
                            .labelStyle(.iconOnly)
                            .disabled(model.isLoadingCameras)
                            .help("Refresh cameras")
                    }
                    HStack {
                        Button("Start Downloads", systemImage: "arrow.down.circle.fill") { model.startDownloads() }
                            .buttonStyle(.borderedProminent)
                            .disabled(model.selectedCameras.isEmpty)
                        Spacer()
                    }
                    Toggle("Daily automatic timelapses", isOn: dailyAutomaticBinding)
                        .help("Export each completed local day while this program remains open.")
                }
                .frame(maxWidth: .infinity)
            }
        }
        .modernCard()
    }

    private func compactDatePicker(_ title: String, selection: Binding<Date>) -> some View {
        DatePicker(
            title,
            selection: model.fullDayMode ? fullDayBinding(title == "Start") : selection,
            in: minimumDate...,
            displayedComponents: model.fullDayMode ? [.date] : [.date, .hourAndMinute]
        )
            .labelsHidden()
            .datePickerStyle(.compact)
            .fixedSize(horizontal: true, vertical: false)
            .help("Type a date and time or open the calendar to choose a date.")
    }

    private var fullDayModeBinding: Binding<Bool> {
        Binding(get: { model.fullDayMode }, set: { enabled in model.setFullDayMode(enabled) })
    }

    private var dailyAutomaticBinding: Binding<Bool> {
        Binding(
            get: { model.dailyAutomaticEnabled },
            set: { enabled in
                if enabled { model.requestDailySchedule() } else { model.stopDailySchedule() }
            }
        )
    }

    private func fullDayBinding(_ isStart: Bool) -> Binding<Date> {
        Binding(
            get: { isStart ? model.startDate : model.endDate },
            set: { value in
                if isStart { model.setFullDayStart(value) } else { model.setFullDayEnd(value) }
            }
        )
    }

    private var profileSelection: Binding<UUID?> {
        Binding(get: { model.selectedProfileID }, set: { id in
            if let id { model.selectProfile(id) }
        })
    }

    private var downloadsGroup: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 8) {
                Label("Jobs", systemImage: "arrow.down.circle")
                    .font(.headline)
                Button("Clear All") { clearFinishedJobs() }
                    .buttonStyle(.borderedProminent)
                    .disabled(!model.hasClearableJobs(dailyAutomations: selectedJobListTab.showsDailyAutomations))
                Button(selectedJobListTab.showsDailyAutomations ? "Stop All" : "Cancel All") {
                    model.cancelAllJobs(dailyAutomations: selectedJobListTab.showsDailyAutomations)
                }
                    .buttonStyle(.borderedProminent)
                    .disabled(!model.hasCancellableJobs(dailyAutomations: selectedJobListTab.showsDailyAutomations))
                Spacer()
            }
            Picker("Job list", selection: $selectedJobListTab) {
                ForEach(JobListTab.allCases, id: \.self) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .frame(width: 300)
            .onChange(of: selectedJobListTab) {
                selectedJobIDs.removeAll()
            }
            if visibleJobs.isEmpty {
                HStack(spacing: 12) {
                    Image(systemName: selectedJobListTab.showsDailyAutomations ? "calendar.badge.clock" : "tray")
                        .font(.title2)
                        .foregroundStyle(.tertiary)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(selectedJobListTab.showsDailyAutomations ? "No daily automations" : "No downloads yet")
                            .font(.headline)
                        Text(emptyJobListMessage)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                }
                .frame(maxWidth: .infinity, minHeight: 58)
            } else {
                downloadsTable
                    .frame(minHeight: 155)
            }
        }
        .modernCard()
        .frame(maxHeight: visibleJobs.isEmpty ? 150 : .infinity)
    }

    private var downloadsTable: some View {
        Table(visibleJobs, selection: $selectedJobIDs) {
            TableColumn("Job") { job in Text("\(job.groupNumber)") }.width(42)
            TableColumn("Camera") { job in Text(job.camera.name).lineLimit(1) }.width(min: 100, ideal: 135)
            TableColumn("Status") { job in DownloadStatusCell(job: job) }.width(min: 110, ideal: 145)
            TableColumn("Progress") { job in DownloadProgressCell(job: job) }.width(min: 130, ideal: 180)
            TableColumn("Downloaded") { job in DownloadBytesCell(job: job, expected: false) }.width(min: 78, ideal: 92)
            TableColumn("Expected") { job in DownloadBytesCell(job: job, expected: true) }.width(min: 72, ideal: 88)
            TableColumn("Speed") { job in DownloadSpeedCell(job: job) }.width(min: 72, ideal: 90)
            TableColumn("Output") { job in
                Text(job.outputURL.lastPathComponent)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(job.outputURL.path)
            }
            .width(min: 120, ideal: 210)
            TableColumn("Action") { job in DownloadActionCell(model: model, job: job) }.width(min: 70, ideal: 95)
        }
        .contextMenu(forSelectionType: UUID.self) { selectedIDs in
            if let job = visibleJobs.first(where: { selectedIDs.contains($0.id) }) {
                jobMenu(for: job)
            }
        } primaryAction: { selectedIDs in
            guard let job = visibleJobs.first(where: { selectedIDs.contains($0.id) }) else { return }
            model.openVideo(job)
        }
    }

    private var visibleJobs: [DownloadJob] {
        model.jobs.filter { $0.isDailySchedule == selectedJobListTab.showsDailyAutomations }
    }

    private var emptyJobListMessage: String {
        if selectedJobListTab.showsDailyAutomations {
            "Enable daily automatic timelapses to track the schedule here."
        } else {
            "Select cameras and start a timelapse to track it here."
        }
    }

    private var statusBar: some View {
        HStack(spacing: 8) {
            Text(model.statusMessage)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer()
            if model.isBusy {
                ProgressView()
                    .controlSize(.small)
                    .help("Background work is active. This animation stops if the interface freezes.")
                Text("Working")
                    .foregroundStyle(.secondary)
            }
            Button("Logs", systemImage: "text.alignleft") {
                openWindow(id: "logs")
            }
            .buttonStyle(.bordered)
            .help("Open application logs in a separate window.")
        }
    }

    private var minimumDate: Date {
        Calendar.current.date(from: DateComponents(year: 2000, month: 1, day: 1)) ?? .distantPast
    }

    @ViewBuilder
    private func jobMenu(for job: DownloadJob) -> some View {
        switch job.state {
        case .scheduled:
            Button("Stop Daily Job", systemImage: "stop.circle") { model.stopDailySchedule() }
        case .stopped:
            EmptyView()
        case .cancelled, .failed:
            Button("Restart", systemImage: "arrow.clockwise") { model.restart(job) }
        case .completed:
            Button("Show in Finder", systemImage: "folder") { model.reveal(job) }
        case .preparing, .downloading, .cancelling:
            Button("Cancel", systemImage: "xmark.circle") { model.cancel(job) }
                .disabled(job.state == .cancelling)
        }
        Divider()
        Button("Remove from List", systemImage: "trash", role: .destructive) {
            removeFromList(job)
        }
        .disabled(!job.state.isTerminal)
    }

    private func removeFromList(_ job: DownloadJob) {
        model.remove(job)
        selectedJobIDs.remove(job.id)
    }

    private func clearFinishedJobs() {
        model.clearFinishedJobs(dailyAutomations: selectedJobListTab.showsDailyAutomations)
        selectedJobIDs.formIntersection(visibleJobs.map(\.id))
    }

}

private extension View {
    func modernCard() -> some View {
        padding(13)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(.quaternary, lineWidth: 1)
            }
    }
}

private struct DownloadStatusCell: View {
    @ObservedObject var job: DownloadJob

    var body: some View {
        Text(job.state.text)
            .lineLimit(1)
            .help(job.state.text)
    }
}

private struct DownloadProgressCell: View {
    @ObservedObject var job: DownloadJob

    var body: some View {
        if job.isDailySchedule {
            ProgressView(value: 0)
        } else if let total = job.totalBytes, total > 0 {
            ProgressView(value: min(Double(job.downloadedBytes) / Double(total), 1))
        } else if job.state == .completed {
            ProgressView(value: 1)
        } else if job.state.isTerminal {
            ProgressView(value: 0)
        } else {
            ProgressView()
                .controlSize(.small)
        }
    }
}

private struct DownloadBytesCell: View {
    @ObservedObject var job: DownloadJob
    let expected: Bool

    var body: some View {
        Text(job.isDailySchedule ? "—" : expected ? job.totalBytes.map(formatBytes) ?? "Unknown" : formatBytes(job.downloadedBytes))
            .monospacedDigit()
    }
}

private struct DownloadSpeedCell: View {
    @ObservedObject var job: DownloadJob

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let fresh = job.lastProgressAt.map { context.date.timeIntervalSince($0) < 2 } ?? false
            Text(fresh && job.bytesPerSecond > 0 ? "\(formatBytes(Int64(job.bytesPerSecond)))/s" : "—")
                .monospacedDigit()
        }
    }
}

private struct DownloadActionCell: View {
    @ObservedObject var model: AppModel
    @ObservedObject var job: DownloadJob

    var body: some View {
        switch job.state {
        case .scheduled:
            Button("Stop") { model.stopDailySchedule() }
        case .stopped:
            Button("Remove") { model.remove(job) }
        case .completed:
            Button("Show") { model.reveal(job) }
                .help("Show the output location in Finder.")
        case .cancelled, .failed:
            Button("Restart") { model.restart(job) }
                .help("Retry this download with its original settings.")
        case .preparing, .downloading, .cancelling:
            Button("Cancel") { model.cancel(job) }
                .disabled(job.state == .cancelling)
        }
    }
}

private func formatBytes(_ count: Int64) -> String {
    let formatter = ByteCountFormatter()
    formatter.countStyle = .binary
    formatter.allowedUnits = [.useBytes, .useKB, .useMB, .useGB, .useTB]
    formatter.includesUnit = true
    formatter.isAdaptive = true
    return formatter.string(fromByteCount: max(count, 0))
}
